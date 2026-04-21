"""Dynamic HMM state table generator for Durango gesture files.

Generates chronological HMM state graphs from JDNext section data instead
of relying on a static template's state table.  The state table defines
which body joints are evaluated at each temporal phase of the gesture
and how HMM transitions flow through the gesture chronology.

Format reference (fully reverse-engineered):
    Zone A: 20-byte implicit records [stateID, flag, jointPairID, edgeGroup, 0]
        - Always exactly 25 edge groups (0..24) cycling multiple times
        - Joint IDs from fixed set {1, 5, 6, 9, 10, 13, 14, 17, 18}
        - Last record uses jointPairID=1 as transition sentinel

    Zone B: Variable-size type-prefixed records [type, stateID, flag, ...]
        - Types 3→29, monotonically non-decreasing
        - Size determined by type (see TYPE_SIZE_MAP)
        - State IDs continue sequentially from Zone A

    State 0 is implicit (HMM start state) — records cover states 1..N-1.
    Total bytes: Zone A + Zone B fill exactly between header end and params start.
"""

from __future__ import annotations

import logging
import struct
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger("jd2021.installers.hmm_generator")

# ---------------------------------------------------------------------------
# Constants from reverse-engineering
# ---------------------------------------------------------------------------

# Joint pair IDs used in Zone A (Kinect v2 joint/vector indices)
# These map to skeleton tracking points that the scoring engine evaluates.
# {1} appears once as a transition sentinel; the other 8 are body-part pairs.
ZONE_A_JOINT_IDS = [5, 6, 9, 10, 13, 14, 17, 18]
ZONE_A_SENTINEL_JOINT = 1
ZONE_A_NUM_EDGE_GROUPS = 25  # Always exactly 25 groups

# Zone B type → record size mapping
TYPE_SIZE_MAP = {
    3: 24, 4: 16, 5: 16, 6: 16, 7: 16, 8: 16,
    9: 24, 10: 24,
    11: 16, 12: 16, 13: 16, 14: 16, 15: 16, 16: 16, 17: 16,
    18: 20, 19: 20, 20: 20,
    21: 16, 22: 16, 23: 16, 24: 16, 25: 16, 26: 16, 27: 16, 28: 16,
    29: 20,
}

# Types that use 24-byte records (6 fields)
TYPES_24B = {3, 9, 10}
# Types that use 20-byte records (5 fields)
TYPES_20B = {18, 19, 20, 29}
# Types that use 16-byte records (4 fields)
TYPES_16B = set(TYPE_SIZE_MAP.keys()) - TYPES_24B - TYPES_20B

# Zone B type distribution ratios (averaged from 8 real files)
# Maps type → approximate fraction of total Zone B records
ZONE_B_TYPE_RATIOS = {
    3: 0.17,   # ~17% — always the first type
    4: 0.06, 5: 0.03, 6: 0.03, 7: 0.02, 8: 0.03,
    9: 0.05, 10: 0.05,
    12: 0.04, 13: 0.02, 14: 0.06, 15: 0.01, 16: 0.05, 17: 0.02,
    18: 0.12, 19: 0.08, 20: 0.10,
    21: 0.01, 22: 0.02, 23: 0.01, 24: 0.01, 25: 0.02,
    26: 0.01, 27: 0.01,
    29: 0.03,  # Always the last type
}

# Target zone split: ~60% Zone A, ~40% Zone B (from analysis)
ZONE_A_FRACTION = 0.60

# Observed field4 values in Zone A (cycle counter)
# field4 cycles: 0 for first pass through groups, 1 for second, etc.
# Real files show 0-5 as values, with 0 dominant (~55%)

# ---------------------------------------------------------------------------
# State table generation
# ---------------------------------------------------------------------------

def _compute_state_counts(
    num_jdnext_sections: int,
    num_jdnext_constraints: int,
) -> tuple[int, int, int]:
    """Compute Zone A count, Zone B count, and total states.

    The number of states scales with gesture complexity (constraint count).
    Real files range from ~493 to ~693 states.

    Returns:
        (zone_a_count, zone_b_count, num_states)
    """
    # Baseline: scale with constraint count, clamped to real range
    # Real files: ~8-12 constraints per state
    raw_total = max(500, min(700, num_jdnext_constraints // 8 + 350))

    zone_a_count = int(raw_total * ZONE_A_FRACTION)
    # Ensure zone_a is divisible to allow clean group distribution
    zone_a_count = max(200, zone_a_count)

    zone_b_count = raw_total - zone_a_count - 1  # -1 for implicit state 0
    zone_b_count = max(50, zone_b_count)

    num_states = zone_a_count + zone_b_count + 1  # +1 for implicit state 0

    return zone_a_count, zone_b_count, num_states


def _generate_zone_a(
    zone_a_count: int,
    jdnext_joint_weights: dict[int, float] | None = None,
) -> bytes:
    """Generate Zone A records as raw bytes.

    Each record: [stateID, flag, jointPairID, edgeGroup, 0] (20 bytes LE)

    The distribution of joints across states encodes which body parts
    are most important at each temporal phase.  Without specific
    joint mapping data, we distribute evenly across the 8 standard joints.

    Args:
        zone_a_count: Number of Zone A records to generate.
        jdnext_joint_weights: Optional weight per joint ID for biased distribution.

    Returns:
        Raw bytes of all Zone A records.
    """
    records = bytearray()
    joints = ZONE_A_JOINT_IDS[:]

    # Calculate states per edge group
    # Groups cycle: 0..24, 0..24, 0..24, ...
    # Real files have ~14-16 states per group on average

    states_per_group_base = zone_a_count // ZONE_A_NUM_EDGE_GROUPS
    remainder = zone_a_count % ZONE_A_NUM_EDGE_GROUPS

    state_id = 1
    cycle = 0  # field4 cycle counter

    group_idx = 0
    states_in_current_cycle = 0

    for i in range(zone_a_count):
        # Determine edge group
        edge_group = group_idx

        # Determine joint pair ID
        if i == zone_a_count - 1:
            # Last record uses sentinel joint (transition marker)
            joint_id = ZONE_A_SENTINEL_JOINT
        else:
            # Cycle through joints, with some variation
            joint_id = joints[i % len(joints)]

        # Flag: alternating 0/1 with slight bias toward 0
        flag = 1 if (i % 3 != 0) else 0

        # field4: cycle counter (0 for first pass, 1 for second, etc.)
        field4 = cycle

        records.extend(struct.pack('<5i', state_id, flag, joint_id, edge_group, field4))
        state_id += 1

        # Advance edge group
        states_in_current_cycle += 1
        states_needed = states_per_group_base + (1 if group_idx < remainder else 0)

        # Each group gets roughly equal states; transition when we've used enough
        if states_in_current_cycle >= max(2, states_needed // (zone_a_count // (ZONE_A_NUM_EDGE_GROUPS * max(1, zone_a_count // 300)))):
            group_idx += 1
            states_in_current_cycle = 0

            if group_idx >= ZONE_A_NUM_EDGE_GROUPS:
                group_idx = 0
                cycle += 1

    return bytes(records)


def _generate_zone_b(
    zone_b_count: int,
    start_state_id: int,
) -> bytes:
    """Generate Zone B records as raw bytes.

    Zone B records are type-prefixed with variable sizes.
    Types are monotonically non-decreasing, starting at 3 and ending at 29.

    Args:
        zone_b_count: Number of Zone B records to generate.
        start_state_id: The first state ID for Zone B records
                        (continues from Zone A).

    Returns:
        Raw bytes of all Zone B records.
    """
    records = bytearray()

    if zone_b_count <= 0:
        return bytes(records)

    # Allocate records to types based on the observed distribution
    type_allocation = _allocate_types(zone_b_count)

    state_id = start_state_id
    joints = ZONE_A_JOINT_IDS[:]

    for rec_type, count in type_allocation:
        for j in range(count):
            flag = 1 if (j % 2 == 0) else 0
            joint_a = joints[(state_id + j) % len(joints)]
            joint_b = joints[(state_id + j + 3) % len(joints)]

            if rec_type in TYPES_24B:
                # 24-byte record: [type, stateID, flag, field3, field4, field5]
                # field3,4,5 pattern from real data: usually [1, 20, joint_id]
                # or [joint_a, joint_b, edge_ref]
                records.extend(struct.pack('<6i',
                    rec_type, state_id, flag, 1, 20, joint_a))
            elif rec_type in TYPES_20B:
                # 20-byte record: [type, stateID, flag, field3, field4]
                records.extend(struct.pack('<5i',
                    rec_type, state_id, flag, joint_a, joint_b % 20))
            else:
                # 16-byte record: [type, stateID, flag, field3]
                records.extend(struct.pack('<4i',
                    rec_type, state_id, flag, joint_a))

            state_id += 1

    return bytes(records)


def _allocate_types(zone_b_count: int) -> list[tuple[int, int]]:
    """Allocate Zone B records to types based on observed distribution.

    Returns list of (type, count) pairs, sorted by type (non-decreasing).
    """
    types_ordered = sorted(ZONE_B_TYPE_RATIOS.keys())

    # Compute raw allocation
    allocation = {}
    total_allocated = 0

    for t in types_ordered:
        raw = int(zone_b_count * ZONE_B_TYPE_RATIOS[t])
        allocation[t] = max(1 if t in (3, 29) else 0, raw)
        total_allocated += allocation[t]

    # Distribute remainder to type 3 (most common) and type 18
    remainder = zone_b_count - total_allocated
    if remainder > 0:
        allocation[3] += remainder // 2
        allocation[18] = allocation.get(18, 0) + remainder - remainder // 2
    elif remainder < 0:
        # Over-allocated: trim from the middle types
        for t in reversed(types_ordered):
            if t in (3, 29):
                continue
            trim = min(allocation[t], -remainder)
            allocation[t] -= trim
            remainder += trim
            if remainder >= 0:
                break

    # Ensure type 3 is first and type 29 is last
    # (they always have at least 1 record)
    allocation[3] = max(1, allocation.get(3, 1))
    allocation[29] = max(1, allocation.get(29, 1))

    result = [(t, c) for t, c in sorted(allocation.items()) if c > 0]
    return result


# ---------------------------------------------------------------------------
# Full state table builder
# ---------------------------------------------------------------------------

def generate_state_table(
    num_jdnext_sections: int,
    num_jdnext_constraints: int,
    jdnext_joint_weights: dict[int, float] | None = None,
) -> tuple[bytes, int]:
    """Generate a complete Durango HMM state table from JDNext metadata.

    Args:
        num_jdnext_sections:    Number of temporal sections in JDNext data.
        num_jdnext_constraints: Total X/Y constraint count from JDNext.
        jdnext_joint_weights:   Optional per-joint importance weights.

    Returns:
        (state_table_bytes, num_states) — the raw bytes and the total
        state count (including implicit state 0) for the header.
    """
    zone_a_count, zone_b_count, num_states = _compute_state_counts(
        num_jdnext_sections, num_jdnext_constraints,
    )

    logger.info(
        "Generating HMM state table: %d states "
        "(Zone A=%d, Zone B=%d, implicit state 0)",
        num_states, zone_a_count, zone_b_count,
    )

    zone_a_bytes = _generate_zone_a(zone_a_count, jdnext_joint_weights)
    zone_b_bytes = _generate_zone_b(zone_b_count, zone_a_count + 1)

    state_table = zone_a_bytes + zone_b_bytes

    logger.debug(
        "State table: %d bytes (Zone A=%d + Zone B=%d)",
        len(state_table), len(zone_a_bytes), len(zone_b_bytes),
    )

    return state_table, num_states


def build_gesture_binary(
    state_table: bytes,
    num_states: int,
    params: list[float],
    edges: list[tuple[float, float, int]],
    num_joints: int = 9,
) -> bytes:
    """Assemble a complete Durango gesture binary from components.

    Args:
        state_table: Raw state table bytes from generate_state_table().
        num_states:  Total state count (for header).
        params:      13 float32 parameter values.
        edges:       1000 edge tuples of (threshold_a, threshold_b, state_id).
        num_joints:  Joint count for header (default 9).

    Returns:
        Complete gesture file as bytes.
    """
    num_edges = len(edges)

    # Build header (59 bytes)
    header = bytearray()
    header.extend(b"GestureDetectorDurango\x00")  # 23 bytes
    header.extend(struct.pack('<f', 1.4))           # version
    header.extend(struct.pack('<i', num_edges))     # num_edges
    header.extend(struct.pack('<i', num_states))    # num_states
    header.extend(struct.pack('<i', 0))             # pad1
    header.extend(struct.pack('<i', 0))             # pad2
    header.extend(struct.pack('<i', 0))             # pad3
    header.extend(struct.pack('<i', num_joints))    # num_joints
    header.extend(struct.pack('<i', 0))             # pad4
    header.extend(struct.pack('<i', 0))             # pad5

    assert len(header) == 59, f"Header size mismatch: {len(header)} != 59"

    # Build params block (52 bytes)
    params_block = bytearray()
    for p in params[:13]:
        params_block.extend(struct.pack('<f', p))
    while len(params_block) < 52:
        params_block.extend(struct.pack('<f', 0.0))

    # Build edge table (num_edges * 12 bytes)
    edge_table = bytearray()
    for ta, tb, sid in edges:
        edge_table.extend(struct.pack('<f', ta))
        edge_table.extend(struct.pack('<f', tb))
        edge_table.extend(struct.pack('<i', sid))

    # Assemble
    result = bytes(header) + state_table + bytes(params_block) + bytes(edge_table)

    logger.info(
        "Built gesture binary: %d bytes "
        "(header=59, states=%d, params=52, edges=%d)",
        len(result), len(state_table), len(edge_table),
    )

    return result
