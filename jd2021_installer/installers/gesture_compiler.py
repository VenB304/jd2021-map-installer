"""Gesture compiler — JDNext Camera → Durango Kinect hybrid converter.

Converts JDNext smartphone 2D camera gesture data into hybrid format
compatible with legacy Xbox 360 Just Dance engines.  Uses the actual
15-joint skeleton schema (decoded from the Just Dance Controller App's
BlazePose-to-Kinect translation layer) to properly map JDNext
constraints to Kinect joint IDs in the Durango edge table.

Architecture:
    Phase 1 - JDNext AST Decompiler: Structurally parses the Float64
              bytecode using declared section descriptors, extracting
              per-joint X/Y constraints tagged with their joint ID from
              the JointsCameraGestureQuantifier enum.  Type B sections
              (112 constraints) are decomposed as 14 joints × 8 values.
    Phase 2 - Template Preparation: Reads the Durango little-endian
              template, locates the edge table, and calibrates the
              parameters block using JDNext timing data.
    Phase 3 - Edge Injection: Places the correct Kinect Joint ID in
              threshold_a and the scaled JDNext position constraint in
              threshold_b, eliminating the prior guesswork approach.

Joint Schema (decompiled from JD Controller App v26.1.2):
    The app uses MediaPipe BlazePose (33 points) down-sampled to 15
    Kinect V1 joints via ProcessableSkeletonFrameFromBlazePose.
    The JDNext .gesture files encode constraints for 14 of these joints
    (omitting Nose), with 28 body part descriptor values = 14 joints × 2.

Edge Field Semantics (from RE analysis + Controller App decompilation):
    threshold_a (float32): Kinect Joint ID (from the 15-joint enum)
    threshold_b (float32): Scaled body position constraint for that joint
        JDNext [-1, +1] → Durango [-3, +3] via 3.0× scale factor
"""

from __future__ import annotations

import logging
import shutil
import statistics
import struct
from pathlib import Path

from jd2021_installer.installers.hmm_generator import (
    generate_state_table,
    build_gesture_binary,
)

logger = logging.getLogger("jd2021.installers.gesture_compiler")

# ---------------------------------------------------------------------------
# Durango format constants (little-endian)
# ---------------------------------------------------------------------------

_DURANGO_MAGIC = b"GestureDetectorDurango\x00"  # 23 bytes (incl. null)
_DURANGO_MAGIC_LEN = 23
_DURANGO_THRESHOLD_OFFSET = 23   # float32 LE: threshold param (1.400)
_DURANGO_NUM_EDGES_OFFSET = 27   # int32 LE: edge count (always 1000)
_DURANGO_NUM_STATES_OFFSET = 31  # int32 LE: state count (variable)
_DURANGO_EDGE_SIZE = 12          # (float32 threshold_a, float32 threshold_b, int32 state_id)
_DURANGO_PARAMS_COUNT = 13       # 13 float32 calibration values before edge table
_DURANGO_PARAMS_SIZE = _DURANGO_PARAMS_COUNT * 4  # 52 bytes
_DURANGO_ENDIAN = "<"            # little-endian

# Also support reading X360 templates for backward compatibility
_X360_MAGIC = b"GestureDetectorX360\x00"  # 20 bytes
_X360_MAGIC_LEN = 20
_X360_NUM_EDGES_OFFSET = 24
_X360_ENDIAN = ">"

# Edge field semantics (from RE analysis of 894 real Durango files +
# Rosetta Stone same-song comparison using MakeItJingle):
#
# threshold_a: gate/weight — controls whether the edge is active.
#   - Small values near 0: always-active scoring edges
#   - Large absolute values (>10): blocking gates (impossible body positions)
#
# threshold_b: position threshold — compared against normalized Kinect reading.
#   - Original analysis suggested 3.0× scale from raw Kinect ↔ JDNext
#     comparison, but this produces values too extreme for the engine.
#   - Discorope (the working auto-perfect gesture) has threshold_b in
#     [-0.97, +1.52] with mean_abs=0.44.  Real Kinect files use [-3.8, +3.8]
#     but scoring ACTUALLY works on normalized HMM-space values.
#   - JDNext camera constraints are already in [-1, +1] normalized space.
#     Scale factor of 0.63 maps to [-0.63, +0.63] — similar to discorope's
#     core scoring range and lenient enough for the engine to match.

_JDNEXT_TO_DURANGO_SCALE = 0.63  # JDNext [-1,+1] → Durango [-0.63,+0.63]

# Gating threshold: edges with |threshold_a| above this value in the
# template are treated as HMM structural gates and preserved as-is.
# Edges below this threshold are scoring edges and get JDNext data.
_GATING_THRESHOLD = 10.0

# Center-exclusion dead zone: controls which constraints are used.
# JDNext data contains many near-zero constraints (often padding/null).
# Filtering them out makes the gesture only match distinctive choreographic
# positions. Strictness maps linearly to dead zone radius.
_DEAD_ZONE_MAX = 0.14   # At strictness=1.0

# Timing injection: baseline for parameter scaling
_TIMING_BASELINE = 10.0   # Median timing value baseline
_TIMING_SCALE_MIN = 0.5   # Clamp to prevent degenerate values
_TIMING_SCALE_MAX = 2.0
_TIMING_MIN_SAMPLES = 5   # Minimum timing values for injection

# Seed for reproducible gate value generation (same input → same output)
_GATE_RNG_SEED = 42


# ---------------------------------------------------------------------------
# JDNext Joint Mapping (from JD Controller App decompilation)
# ---------------------------------------------------------------------------

# The 15-point Kinect V1 skeleton used by the JDNext scoring engine.
# Decompiled from JointsCameraGestureQuantifier.Id in the Just Dance
# Controller App v26.1.2 (com.ubisoft.dance.justdancecontroller2023).
#
# The app uses MediaPipe BlazePose (33 joints) → 15 Kinect V1 joints:
#   Direct mappings via FillJointDataFromLandmark()
#   Interpolated mappings via FillJointDataFromLandmarksCenter()
#     (ShouldersCenter = midpoint of ShoulderLeft + ShoulderRight)
#     (HipsCenter = midpoint of HipLeft + HipRight)

_JDNEXT_JOINT_ENUM = {
    0: "Nose",
    1: "ShouldersCenter",
    2: "ShoulderLeft",
    3: "ShoulderRight",
    4: "ElbowLeft",
    5: "ElbowRight",
    6: "WristLeft",
    7: "WristRight",
    8: "HipsCenter",
    9: "HipLeft",
    10: "HipRight",
    11: "KneeLeft",
    12: "KneeRight",
    13: "AnkleLeft",
    14: "AnkleRight",
}

# The gesture files encode 14 joints (omitting Nose=0), so joint IDs 1..14.
_JDNEXT_CONSTRAINT_JOINTS = list(range(1, 15))

# Durango Kinect joint indices for the edge table.  These map the 14
# JDNext joints to the Kinect V2 joint IDs used by the Durango engine
# (from HMM Zone A analysis of real gesture files).
_JDNEXT_TO_DURANGO_JOINT_MAP = {
    1: 20,   # ShouldersCenter → SpineShoulder (Kinect V2)
    2: 4,    # ShoulderLeft
    3: 8,    # ShoulderRight
    4: 5,    # ElbowLeft
    5: 9,    # ElbowRight
    6: 6,    # WristLeft
    7: 10,   # WristRight
    8: 0,    # HipsCenter → SpineBase
    9: 12,   # HipLeft
    10: 16,  # HipRight
    11: 13,  # KneeLeft
    12: 17,  # KneeRight
    13: 14,  # AnkleLeft
    14: 18,  # AnkleRight
}

# The fixed body part descriptor table found at opcode[2..29].
# Identical across ALL analyzed JDNext gesture files.
# 28 values = 14 pairs — each pair defines a joint cross-reference
# for the constraint parser's routing logic.
_JDNEXT_BODY_PART_TABLE = [
    3, 5, 5, 8, 2, 1, 8, 9, 14, 4, 10, 10, 14, 4,
    10, 14, 2, 1, 12, 19, 1, 1, 12, 0, 0, 0, 1, 5,
]

# Number of constraint values per joint in Type B sections: 112 / 14 = 8
_TYPE_B_VALUES_PER_JOINT = 8

# Metadata detection threshold: values above this in the constraint zone
# are section-level calibration data, not body position constraints.
_METADATA_THRESHOLD = 1.05


# ---------------------------------------------------------------------------
# Phase 1: JDNext AST Decompiler (Joint-Aware)
# ---------------------------------------------------------------------------

# JDNext opcode zone structure (decoded from MakeItJingle Rosetta Stone +
# JD Controller App decompilation):
#   opcode[0] = total float count
#   opcode[1] = num_sections (each = one temporal keyframe of the gesture)
#   opcode[2..29] = body part descriptor table (28 fixed values)
#   Then section descriptors: pairs of (constraint_count, timing_count)
#     - First 4 sections: 88 constraints + 7 timing = 95 floats each
#     - Remaining sections: 112 constraints + 31 timing = 143 floats each
#   Then state flags and tail

_JDNEXT_HEADER_SIZE = 30  # Fixed header (opcode[0..29])
_JDNEXT_SECTION_A_XY = 88
_JDNEXT_SECTION_A_TM = 7
_JDNEXT_SECTION_B_XY = 112
_JDNEXT_SECTION_B_TM = 31
_JDNEXT_NUM_TYPE_A = 4  # First 4 sections are type A


def _decompile_jdnext(
    gesture_path: Path,
) -> tuple[list[tuple[int, float]], list[float]]:
    """Read a JDNext Camera .gesture file and return joint-tagged constraints.

    Performs structured, joint-aware parsing instead of blind value filtering.
    Each constraint is returned as a ``(joint_id, value)`` tuple, where
    ``joint_id`` corresponds to the ``JointsCameraGestureQuantifier.Id`` enum.

    The JDNext format is a sequential array of float64 doubles with:

    - ``opcode[0]``: total float count (declared length)
    - ``opcode[1]``: number of temporal sections (keyframes)
    - ``opcode[2..29]``: body part descriptor table (28 fixed values)
    - Section descriptors: pairs of ``(constraint_count, timing_count)``
    - Constraint zone: per-section body position data

    For Type B sections (112 constraints), the data is decomposed as
    14 joints × 8 values each (4 X/Y pairs per joint per keyframe).
    Embedded metadata values (> 1.05) are filtered per-joint.

    Returns:
        A tuple of ``(joint_constraints, timing_values)`` where
        ``joint_constraints`` is a list of ``(joint_id, value)`` tuples.
    """
    data = gesture_path.read_bytes()
    num_floats = len(data) // 8
    if num_floats < _JDNEXT_HEADER_SIZE:
        logger.warning("JDNext gesture '%s' has only %d floats — too short",
                       gesture_path.name, num_floats)
        return [], []

    raw = list(struct.unpack(f"<{num_floats}d", data))

    # Parse header
    declared_length = int(raw[0])
    num_sections = int(raw[1])

    if num_sections < 1 or num_sections > 500:
        logger.warning(
            "JDNext gesture '%s' has suspicious section count %d",
            gesture_path.name, num_sections,
        )
        return _decompile_jdnext_fallback(raw, gesture_path.name)

    # Parse section descriptors (pairs of constraint_count, timing_count)
    sec_descs: list[tuple[int, int]] = []
    idx = _JDNEXT_HEADER_SIZE
    for _ in range(num_sections):
        if idx + 1 >= len(raw):
            break
        c_count = int(raw[idx])
        t_count = int(raw[idx + 1])
        sec_descs.append((c_count, t_count))
        idx += 2

    if len(sec_descs) != num_sections:
        logger.warning(
            "JDNext gesture '%s': expected %d section descriptors, got %d",
            gesture_path.name, num_sections, len(sec_descs),
        )
        return _decompile_jdnext_fallback(raw, gesture_path.name)

    # Skip any remaining opcode-zone integers before the constraint zone
    while idx < len(raw):
        v = raw[idx]
        if v == int(v) and abs(v) < 100_000:
            idx += 1
        else:
            break

    constraint_zone_start = idx

    # --- Extract joint-tagged constraints and timing values ---
    joint_constraints: list[tuple[int, float]] = []
    timing_values: list[float] = []
    offset = constraint_zone_start

    for sec_idx, (c_count, t_count) in enumerate(sec_descs):
        if offset + c_count + t_count > len(raw):
            logger.warning(
                "JDNext gesture '%s': section %d overflows (offset=%d, "
                "need=%d, have=%d)",
                gesture_path.name, sec_idx, offset,
                c_count + t_count, len(raw) - offset,
            )
            break

        section_constraints = raw[offset:offset + c_count]
        section_timing = raw[offset + c_count:offset + c_count + t_count]

        if c_count == _JDNEXT_SECTION_B_XY:
            # Type B section: 112 = 14 joints × 8 values each
            joint_constraints.extend(
                _parse_type_b_section(section_constraints, sec_idx)
            )
        else:
            # Type A section (or non-standard): extract with heuristic
            joint_constraints.extend(
                _parse_type_a_section(section_constraints, sec_idx)
            )

        # Collect timing values (filtering out near-zero padding)
        for tv in section_timing:
            if abs(tv) > 0.001:
                timing_values.append(tv)

        offset += c_count + t_count

    logger.debug(
        "JDNext decompile '%s': %d total floats, %d sections, "
        "%d joint-tagged constraints, %d timing values extracted",
        gesture_path.name, num_floats, num_sections,
        len(joint_constraints), len(timing_values),
    )
    return joint_constraints, timing_values


def _parse_type_b_section(
    constraints: list[float],
    section_idx: int,
) -> list[tuple[int, float]]:
    """Parse a Type B section's 112 constraints as 14 joints × 8 values.

    Each joint gets 8 constraint values representing 4 (X, Y) pairs.
    Embedded metadata values (|v| > threshold) are filtered out.
    The joint ID (1..14) from JointsCameraGestureQuantifier is tagged
    onto each surviving constraint value.

    Returns:
        List of ``(joint_id, constraint_value)`` tuples.
    """
    tagged: list[tuple[int, float]] = []

    for joint_slot in range(14):
        joint_id = _JDNEXT_CONSTRAINT_JOINTS[joint_slot]  # 1..14
        start = joint_slot * _TYPE_B_VALUES_PER_JOINT
        end = start + _TYPE_B_VALUES_PER_JOINT
        joint_values = constraints[start:end]

        for v in joint_values:
            if abs(v) <= _METADATA_THRESHOLD:
                tagged.append((joint_id, v))

    return tagged


def _parse_type_a_section(
    constraints: list[float],
    section_idx: int,
) -> list[tuple[int, float]]:
    """Parse a Type A section's constraints with joint assignment heuristic.

    Type A sections (88 constraints) don't have a clean 14-joint grouping.
    We filter out metadata values and distribute constraints across joints
    using a round-robin assignment based on the body part descriptor table.

    For section 0 (initialization data with padding values like 0.5, 1.0),
    we skip the entire section as it contains no real choreographic data.

    Returns:
        List of ``(joint_id, constraint_value)`` tuples.
    """
    tagged: list[tuple[int, float]] = []

    # Section 0 is typically initialization padding — skip it
    if section_idx == 0:
        # Check if it's padding (all values are 0.5 or 1.0)
        unique_vals = set(round(v, 4) for v in constraints)
        if unique_vals <= {0.5, 1.0, 0.0, -0.5, -1.0}:
            return tagged

    # Filter out embedded metadata (values > threshold)
    clean_values = [
        v for v in constraints if abs(v) <= _METADATA_THRESHOLD
    ]

    if not clean_values:
        return tagged

    # Distribute across 14 joints round-robin
    for i, v in enumerate(clean_values):
        joint_id = _JDNEXT_CONSTRAINT_JOINTS[i % 14]
        tagged.append((joint_id, v))

    return tagged


def _decompile_jdnext_fallback(
    raw: list[float],
    filename: str,
) -> tuple[list[tuple[int, float]], list[float]]:
    """Fallback decompiler for malformed files — uses the old heuristic.

    Walks the raw float array to find the opcode/constraint boundary,
    then extracts values in [-1.0, 1.0] with round-robin joint assignment.
    This preserves backward compatibility for edge cases.
    """
    boundary = len(raw)
    frac_run = 0
    for i in range(1, len(raw)):
        v = raw[i]
        is_int = (v == int(v)) and abs(v) < 100_000
        if not is_int:
            frac_run += 1
            if frac_run >= 3:
                boundary = i - 2
                break
        else:
            frac_run = 0

    constraint_zone = raw[boundary:]

    xy_values = [v for v in constraint_zone if -1.0 <= v <= 1.0]
    timing_values = [v for v in constraint_zone if v > 1.0]

    # Tag with round-robin joint IDs
    joint_constraints = [
        (_JDNEXT_CONSTRAINT_JOINTS[i % 14], v)
        for i, v in enumerate(xy_values)
    ]

    logger.debug(
        "JDNext fallback decompile '%s': %d total floats, boundary=%d, "
        "%d joint-tagged constraints, %d timing values",
        filename, len(raw), boundary,
        len(joint_constraints), len(timing_values),
    )
    return joint_constraints, timing_values


# ---------------------------------------------------------------------------
# Phase 2 & 3: Template modification + edge injection
# ---------------------------------------------------------------------------

def _detect_template_format(template_data: bytearray) -> tuple[str, str, int]:
    """Detect whether the template is Durango (LE) or X360 (BE).

    Returns:
        (magic_name, endian_char, num_edges_offset)
    """
    if template_data[:_DURANGO_MAGIC_LEN] == _DURANGO_MAGIC:
        return "Durango", _DURANGO_ENDIAN, _DURANGO_NUM_EDGES_OFFSET
    if template_data[:_X360_MAGIC_LEN] == _X360_MAGIC:
        return "X360", _X360_ENDIAN, _X360_NUM_EDGES_OFFSET
    raise ValueError(
        f"Template is not a recognized gesture format "
        f"(got: {template_data[:23]!r})"
    )


def _inject_timing_into_params(
    template_data: bytearray,
    edge_start: int,
    timing_values: list[float],
    joint_constraints: list[tuple[int, float]],
    endian: str,
) -> None:
    """Scale and inject the 13-float parameters block.

    The parameters block sits immediately before the edge table in both
    X360 and Durango formats (52 bytes = 13 × float32).

    Injection strategy (from MakeItJingle Rosetta Stone analysis):
      - P0, P11, P12: Tempo/duration/complexity — scaled from timing
      - P3-P10: Training statistics — injected from JDNext constraint
        mean/std to encode the expected body position range
      - P1, P2: System constants — left untouched
    """
    params_start = edge_start - _DURANGO_PARAMS_SIZE
    if params_start < 0:
        logger.warning("Parameters block would overlap header — skipping")
        return

    # --- Tempo scaling (P0, P11, P12) ---
    if len(timing_values) >= _TIMING_MIN_SAMPLES:
        timing_median = statistics.median(timing_values)
        duration_scale = timing_median / _TIMING_BASELINE
        duration_scale = max(_TIMING_SCALE_MIN, min(duration_scale, _TIMING_SCALE_MAX))

        for pidx in (0, 11, 12):
            poff = params_start + pidx * 4
            orig_val = struct.unpack_from(f"{endian}f", template_data, poff)[0]
            scaled_val = orig_val * duration_scale
            struct.pack_into(f"{endian}f", template_data, poff, scaled_val)
            logger.debug(
                "Timing injection param[%d]: %.1f → %.1f (scale=%.3f)",
                pidx, orig_val, scaled_val, duration_scale,
            )

    # --- Statistical injection (P3-P10) ---
    # Real Kinect gestures store mean/std of body positions in these
    # params. Inject from JDNext constraint data for song-specificity.
    raw_values = [v for _, v in joint_constraints]
    if raw_values and len(raw_values) >= 20:
        scaled = [v * _JDNEXT_TO_DURANGO_SCALE for v in raw_values]
        mean_val = statistics.mean(scaled)
        std_val = statistics.stdev(scaled) if len(scaled) > 1 else 0.3

        # P3-P10 pattern from real files: alternating positive/negative
        # values representing different axes/body parts.
        # P3,P4,P5,P6 tend to be positive (0.15-0.55)
        # P7,P8,P9 tend to be negative (-0.34 to -0.89)
        # P10 is small positive (0.04-0.06)
        stat_values = [
            abs(mean_val) + std_val * 0.3,     # P3
            abs(mean_val) + std_val * 0.8,     # P4
            abs(mean_val) + std_val * 0.5,     # P5
            std_val * 0.1,                     # P6
            -(abs(mean_val) + std_val * 0.9),  # P7
            -(abs(mean_val) + std_val * 0.3),  # P8
            -(abs(mean_val) + std_val * 0.6),  # P9
            std_val * 0.08,                    # P10
        ]
        for j, pidx in enumerate(range(3, 11)):
            poff = params_start + pidx * 4
            struct.pack_into(f"{endian}f", template_data, poff, stat_values[j])

        logger.debug(
            "Statistical injection P3-P10: mean=%.3f std=%.3f",
            mean_val, std_val,
        )


def _load_and_hybridize(
    template_data: bytearray,
    joint_constraints: list[tuple[int, float]],
    timing_values: list[float] | None = None,
    strictness: float = 1.0,
) -> bytearray:
    """Load a Durango/X360 template and inject JDNext data into the edge table.

    The injection pipeline:

    1. **Detect format** — Durango (LE) or X360 (BE)
    2. **Timing calibration** — parameters block scaled from JDNext timing
    3. **Edge injection** — threshold_a set to Kinect Joint ID,
       threshold_b set to scaled JDNext position constraint.
       Gating edges from the template are preserved.

    Args:
        template_data:      Mutable bytearray of the Durango/X360 template.
        joint_constraints:  Joint-tagged JDNext constraints as
                            ``(joint_id, value)`` tuples.
        timing_values:      Extracted JDNext timing/weight values (> 1.0).
        strictness:         Scoring strictness (0.0 = auto-perfect,
                            1.0 = full JDNext injection).

    Returns the modified bytearray ready to write to disk.
    """
    # Detect format and parse header
    fmt_name, endian, edges_offset = _detect_template_format(template_data)

    num_edges = struct.unpack_from(f"{endian}i", template_data, edges_offset)[0]
    edge_start = len(template_data) - (num_edges * _DURANGO_EDGE_SIZE)

    if edge_start < 0 or edge_start >= len(template_data):
        raise ValueError(
            f"Calculated edge table start ({edge_start}) is out of range "
            f"for file of {len(template_data)} bytes"
        )

    logger.debug(
        "Template: %s format, %d edges, edge_table at offset %d",
        fmt_name, num_edges, edge_start,
    )

    # Phase 2: Inject timing + statistics into the parameters block
    _inject_timing_into_params(
        template_data, edge_start, timing_values or [],
        joint_constraints, endian,
    )

    # If no constraints or auto-perfect mode, leave edges untouched
    if not joint_constraints:
        logger.debug("No joint constraints to inject; output uses template edges")
        return template_data

    if strictness <= 0.0:
        logger.debug("Strictness=0.0; output uses template edges (auto-perfect)")
        return template_data

    # Center-exclusion dead zone filtering (operates on values, keeps tags)
    dead_zone = _DEAD_ZONE_MAX * strictness
    filtered = [(jid, v) for jid, v in joint_constraints if abs(v) > dead_zone]

    if len(filtered) < num_edges:
        dead_zone *= 0.5
        filtered = [(jid, v) for jid, v in joint_constraints
                     if abs(v) > dead_zone]

    if len(filtered) < 10:
        logger.warning(
            "Dead zone %.3f filtered too aggressively (%d remain); "
            "falling back to unfiltered",
            dead_zone, len(filtered),
        )
        filtered = list(joint_constraints)
        dead_zone = 0.0

    logger.debug(
        "Center-exclusion: dead_zone=%.3f, %d/%d constraints kept (%.0f%%)",
        dead_zone, len(filtered), len(joint_constraints),
        len(filtered) / max(len(joint_constraints), 1) * 100,
    )

    # Phase 3: Inject JDNext constraints into edge thresholds.
    #
    # REAL KINECT PATTERN (from forensic analysis of 10 MakeItJingle files):
    #   ~76% scoring edges: threshold_a = body position [-1, +1] quantized to 0.1
    #   ~20% gating edges: threshold_a = joint pair index (11-164)
    #   ~4%  boundary edges: threshold_a = values [1, 10]
    #
    # We PRESERVE the template's gating edges (|a| > 10) and inject
    # quantized position values into scoring edges.

    n = len(filtered)

    # Build quantized threshold_a pool matching real bell-curve distribution
    _QUANT_WEIGHTS = {
        0.0: 16, 0.1: 10, -0.1: 9, 0.2: 8, -0.2: 8,
        0.3: 5, -0.3: 5, 0.4: 5, -0.4: 5, 0.5: 4, -0.5: 5,
        0.6: 3, -0.6: 5, 0.7: 2, -0.7: 1, 0.8: 1, -0.8: 2,
        -1.0: 2, 1.0: 2, -0.9: 1, 0.9: 1,
    }
    quant_pool: list[float] = []
    for val, weight in _QUANT_WEIGHTS.items():
        quant_pool.extend([val] * weight)

    # Identify template gating vs scoring edges
    gating_indices = []
    scoring_indices = []
    for edge_idx in range(num_edges):
        eoff = edge_start + edge_idx * _DURANGO_EDGE_SIZE
        orig_a = struct.unpack_from(f"{endian}f", template_data, eoff)[0]
        if abs(orig_a) > _GATING_THRESHOLD:
            gating_indices.append(edge_idx)
        else:
            scoring_indices.append(edge_idx)

    # Camera blend factor: same approach as _compile_with_donor.
    # Keep mostly template, add a touch of camera for song-specificity.
    blend = min(strictness * 0.4, 0.4)

    # --- Gating edge blending ---
    # Preserve template's gating threshold_a; blend camera into threshold_b.
    if n > 0:
        for j, edge_idx in enumerate(gating_indices):
            eoff = edge_start + edge_idx * _DURANGO_EDGE_SIZE
            orig_b = struct.unpack_from(f"{endian}f", template_data, eoff + 4)[0]
            ci = int(j * n / max(len(gating_indices), 1)) % n
            _, val = filtered[ci]
            cam_val = val * _JDNEXT_TO_DURANGO_SCALE
            blended = orig_b * (1.0 - blend) + cam_val * blend
            struct.pack_into(f"{endian}f", template_data, eoff + 4, blended)

    # --- Scoring edge blending ---
    # Preserve template's threshold_a; blend camera into threshold_b.
    num_scoring = len(scoring_indices)
    sorted_filtered = sorted(filtered, key=lambda x: x[1])

    for i, edge_idx in enumerate(scoring_indices):
        eoff = edge_start + edge_idx * _DURANGO_EDGE_SIZE
        orig_b = struct.unpack_from(f"{endian}f", template_data, eoff + 4)[0]

        ci = int(i * n / max(num_scoring, 1)) % n
        _, val = sorted_filtered[ci]
        cam_val = val * _JDNEXT_TO_DURANGO_SCALE

        # Blend: keep mostly template, add camera influence
        blended = orig_b * (1.0 - blend) + cam_val * blend
        struct.pack_into(f"{endian}f", template_data, eoff + 4, blended)
        # eoff + 0 (threshold_a) is LEFT UNTOUCHED — preserving template structure
        # eoff + 8 (state_id) is LEFT UNTOUCHED — preserving HMM topology

    total_gating = len(gating_indices)
    logger.debug(
        "Injected %d scoring + %d gating edges "
        "(dead_zone=%.3f, strictness=%.2f, scale=%.2f, format=%s)",
        num_scoring, total_gating,
        dead_zone, strictness, _JDNEXT_TO_DURANGO_SCALE, fmt_name,
    )
    return template_data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compile_hybrid_gesture(
    jdnext_src_path: Path,
    template_path: Path,
    output_path: Path,
    strictness: float = 1.0,
) -> bool:
    """Compile a single JDNext gesture into a hybrid Durango Kinect gesture.

    Reads the JDNext Float64 bytecode, extracts X/Y constraints and
    timing data, loads the Durango template, injects position thresholds
    and gate/weight values matching the real Kinect distribution, and
    writes the hybrid binary to ``output_path``.

    Args:
        jdnext_src_path: Path to the JDNext Camera ``.gesture`` file.
        template_path:   Path to the Durango template ``.gesture`` file.
        output_path:     Destination path for the compiled hybrid file.
        strictness:      Scoring strictness (0.0 = auto-perfect,
                         1.0 = full JDNext scoring).  Default 1.0.

    Returns:
        ``True`` if the hybrid was compiled successfully, ``False`` otherwise.
    """
    try:
        if not jdnext_src_path.exists():
            logger.warning("JDNext gesture not found: %s", jdnext_src_path)
            return False

        if not template_path.exists():
            logger.error("Gesture template not found: %s", template_path)
            return False

        # Phase 1: Decompile JDNext AST (joint-tagged)
        joint_constraints, timing_values = _decompile_jdnext(jdnext_src_path)

        # Load template as mutable bytearray
        template_data = bytearray(template_path.read_bytes())

        # Phase 2 + 3: Calibrate params + inject edges
        hybrid_data = _load_and_hybridize(
            template_data, joint_constraints, timing_values, strictness,
        )

        # Write output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(hybrid_data)

        logger.info(
            "Compiled hybrid gesture: %s (%d joint-tagged + %d timing → %s, "
            "strictness=%.2f)",
            jdnext_src_path.name,
            len(joint_constraints),
            len(timing_values),
            output_path.name,
            strictness,
        )
        return True

    except Exception:
        logger.exception(
            "Failed to compile hybrid gesture from '%s'",
            jdnext_src_path.name,
        )
        return False


def compile_gesture_from_scratch(
    jdnext_src_path: Path,
    output_path: Path,
    strictness: float = 1.0,
) -> bool:
    """Compile a JDNext gesture into a Durango binary.

    **Primary strategy (Donor Mode):**
    Uses ``discorope.gesture`` as a structural donor — copies its entire
    known-working state table, parameters, and edge structure, then
    ONLY injects JDNext camera constraint values into the scoring
    edges' ``threshold_b`` field.  This guarantees the output has a
    structure the engine accepts (discorope is the proven auto-perfect).

    **Fallback (Generated Mode):**
    If discorope is not found, falls back to the dynamic HMM generator.

    Args:
        jdnext_src_path: Path to the JDNext Camera ``.gesture`` file.
        output_path:     Destination path for the compiled gesture file.
        strictness:      Scoring strictness (0.0 = auto-perfect,
                         1.0 = full JDNext scoring).  Default 1.0.

    Returns:
        ``True`` if compiled successfully, ``False`` otherwise.
    """
    try:
        if not jdnext_src_path.exists():
            logger.warning("JDNext gesture not found: %s", jdnext_src_path)
            return False

        # Pre-flight check: Is this already a compiled binary gesture?
        # If the map author already included valid Durango/X360 gestures,
        # we must not parse them as float64 JDNext bytecode.
        with open(jdnext_src_path, 'rb') as f:
            magic_check = f.read(20)
        
        if magic_check.startswith(b"GestureDetector"):
            logger.info("Source gesture '%s' is already compiled. Copying directly.", jdnext_src_path.name)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(jdnext_src_path, output_path)
            return True

        # Phase 1: Decompile JDNext AST (joint-tagged)
        joint_constraints, timing_values = _decompile_jdnext(jdnext_src_path)

        if not joint_constraints:
            logger.warning(
                "No constraints extracted from '%s'; cannot generate",
                jdnext_src_path.name,
            )
            return False

        # Phase 2: Try donor-based compilation (discorope as structural base)
        donor_path = _find_donor_gesture()
        if donor_path is not None:
            return _compile_with_donor(
                donor_path, joint_constraints, timing_values,
                output_path, jdnext_src_path.name, strictness,
            )

        # Phase 3: Fallback to generated HMM (if no donor available)
        logger.warning(
            "No donor gesture found; falling back to generated HMM for '%s'",
            jdnext_src_path.name,
        )
        num_sections = _count_jdnext_sections(jdnext_src_path)

        state_table, num_states = generate_state_table(
            num_sections, len(joint_constraints),
        )
        params = _build_params_from_jdnext(joint_constraints, num_sections)
        edges = _build_edge_table(
            joint_constraints, num_states, strictness,
        )
        gesture_data = build_gesture_binary(
            state_table, num_states, params, edges, num_joints=9,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(gesture_data)

        logger.info(
            "Compiled gesture (generated HMM): %s "
            "(%d joint-tagged, %d states, strictness=%.2f)",
            jdnext_src_path.name,
            len(joint_constraints),
            num_states,
            strictness,
        )
        return True

    except Exception:
        logger.exception(
            "Failed to compile gesture from scratch for '%s'",
            jdnext_src_path.name,
        )
        return False


def _find_donor_gesture() -> Path | None:
    """Locate a known-good gesture file to use as a structural donor.

    Search order:
    1. Bundled durango_template.gesture (Durango LE format)
    2. Bundled discorope.gesture (X360 BE format)
    3. None (caller falls back to generated HMM)
    """
    assets_dir = Path(__file__).resolve().parents[1] / "assets" / "gesture_templates"
    if not assets_dir.exists():
        # Try from repo root
        assets_dir = Path(__file__).resolve().parents[2] / "assets" / "gesture_templates"

    candidates = [
        assets_dir / "durango_template.gesture",
        assets_dir / "discorope.gesture",
    ]

    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_size > 256:
            logger.debug("Found donor gesture: %s", candidate)
            return candidate

    return None


def _compile_with_donor(
    donor_path: Path,
    joint_constraints: list[tuple[int, float]],
    timing_values: list[float],
    output_path: Path,
    src_name: str,
    strictness: float,
) -> bool:
    """Compile a gesture using a known-good donor as the structural base.

    **Key insight:** The donor template (durango_template/discorope) already
    gives auto-perfect scores when used unmodified.  Fully REPLACING its
    threshold_b values with camera data BREAKS this because camera
    constraints have a compressed distribution that narrows the engine's
    scoring window → OKs/Greats instead of Perfect.

    **Solution:** BLEND camera values into the donor's existing threshold_b
    instead of replacing.  The blend factor is derived from ``strictness``:
      - strictness=0.0 → 100% donor (auto-perfect, no camera influence)
      - strictness=0.7 → 30% camera blend (song-specific, still lenient)
      - strictness=1.0 → 40% camera blend (max camera influence, still safe)

    The state table, parameters, and threshold_a values are preserved
    exactly as they appear in the donor file.
    """
    donor_data = bytearray(donor_path.read_bytes())

    # Detect format
    fmt_name, endian, edges_offset = _detect_template_format(donor_data)
    num_edges = struct.unpack_from(f"{endian}i", donor_data, edges_offset)[0]
    edge_start = len(donor_data) - (num_edges * _DURANGO_EDGE_SIZE)

    if edge_start < 0 or edge_start >= len(donor_data):
        logger.error("Donor gesture has invalid edge table offset")
        return False

    # Filter constraints
    raw_values = [v for _, v in joint_constraints]
    dead_zone = _DEAD_ZONE_MAX * strictness
    filtered = [v for v in raw_values if abs(v) > dead_zone]

    if len(filtered) < 10:
        filtered = raw_values if raw_values else [0.0]

    # Sort for structured distribution
    sorted_vals = sorted(filtered)
    n = len(sorted_vals)

    if strictness <= 0.0:
        # Auto-perfect: don't modify any edges
        logger.debug("Strictness=0; donor gesture used as-is (auto-perfect)")
    else:
        # Camera blend factor: cap at 0.4 to keep values within
        # the donor's proven scoring window.
        # At strictness=0.7 (default), blend = 0.28 → 72% donor + 28% camera
        blend = min(strictness * 0.4, 0.4)

        # Walk through ALL edges and blend camera values into threshold_b
        for i in range(num_edges):
            eoff = edge_start + i * _DURANGO_EDGE_SIZE

            # Read the donor's original threshold_b
            orig_b = struct.unpack_from(f"{endian}f", donor_data, eoff + 4)[0]

            # Select a camera constraint value (sequential walk)
            ci = int(i * n / max(num_edges, 1)) % n
            cam_val = sorted_vals[ci] * _JDNEXT_TO_DURANGO_SCALE

            # Blend: keep mostly donor, add a touch of camera
            blended = orig_b * (1.0 - blend) + cam_val * blend

            struct.pack_into(f"{endian}f", donor_data, eoff + 4, blended)

    # Write output
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(donor_data)

    logger.info(
        "Compiled gesture (donor: %s): %s "
        "(%d constraints → %d edges, strictness=%.2f, format=%s)",
        donor_path.name, src_name,
        len(joint_constraints), num_edges,
        strictness, fmt_name,
    )
    return True


def _count_jdnext_sections(gesture_path: Path) -> int:
    """Extract the section count from a JDNext gesture file.

    The section count is at opcode[1] (the second float64 value,
    interpreted as an integer).
    """
    data = gesture_path.read_bytes()
    if len(data) < 16:
        return 27  # Fallback default
    raw = struct.unpack_from('<2d', data, 0)
    num_sections = int(raw[1])
    return max(4, min(num_sections, 200))  # Clamp to sane range


def _build_params_from_jdnext(
    joint_constraints: list[tuple[int, float]],
    num_sections: int,
) -> list[float]:
    """Build the 13-float parameters block from JDNext data.

    P0, P11, P12:  Tempo/duration/complexity derived from Section A/B descriptors.
    P1:            System constant (0.049).
    P2:            Reserved (0.0).
    P3-P10:        Song-specific body position statistics computed from the
                   actual per-joint constraint means and standard deviations.

    Real Kinect parameter ranges (from 10 MakeItJingle files):
        P0:  [336, 1489]   P1: 0.049  P2: 0.0
        P3:  [0.158, 0.579]  P4:  [0.231, 0.712]  P5:  [0.175, 0.611]
        P6:  [0.015, 0.047]  P7: [-0.890,-0.373]  P8: [-0.579,-0.160]
        P9: [-0.738,-0.258]  P10: [0.041, 0.061]
        P11: [120, 264]    P12: [34, 108]
    """
    # Map JDNext "Section A/B" timing descriptors (7/31 counts) 
    # to the actual duration/weight parameters.
    type_a_count = min(4, num_sections)
    type_b_count = max(0, num_sections - 4)
    
    # 7 timing counts for intro poses, 31 for full body poses
    total_timing_weight = (type_a_count * 7) + (type_b_count * 31)

    # Change 4: Fix P0/P11/P12 scaling to match real ranges
    # Real P0 range [336, 1489], mean ~740 for ~741 total_timing_weight
    p0_tempo = total_timing_weight * 1.0
    # Real P11 range [120, 264], mean ~166
    p11_complexity = total_timing_weight * 0.225
    # Real P12 range [34, 108], mean ~67
    p12_duration = total_timing_weight * 0.090

    # Change 2: Compute song-specific P3-P10 from per-joint statistics
    # Group constraints by joint and compute per-joint means
    from collections import defaultdict
    joint_vals: dict[int, list[float]] = defaultdict(list)
    for jid, val in joint_constraints:
        joint_vals[jid].append(val * _JDNEXT_TO_DURANGO_SCALE)

    if joint_vals:
        per_joint_means = [statistics.mean(vs) for vs in joint_vals.values()]
        per_joint_stds = [
            statistics.stdev(vs) if len(vs) > 1 else 0.3
            for vs in joint_vals.values()
        ]
        abs_mean = statistics.mean([abs(m) for m in per_joint_means])
        mean_std = statistics.mean(per_joint_stds)
    else:
        abs_mean = 0.3
        mean_std = 0.4

    # P3-P6: positive body position bounds (real range ~0.02-0.71)
    # P7-P9: negative body position bounds (real range ~-0.89 to -0.16)
    # P10:   small positive stabilizer (real range ~0.04-0.06)
    p3 = max(0.15, min(abs_mean + mean_std * 0.2, 0.58))
    p4 = max(0.23, min(abs_mean + mean_std * 0.5, 0.72))
    p5 = max(0.17, min(abs_mean + mean_std * 0.3, 0.62))
    p6 = max(0.015, min(mean_std * 0.03, 0.047))
    p7 = -max(0.37, min(abs_mean + mean_std * 0.6, 0.89))
    p8 = -max(0.16, min(abs_mean + mean_std * 0.2, 0.58))
    p9 = -max(0.26, min(abs_mean + mean_std * 0.4, 0.74))
    p10 = max(0.041, min(mean_std * 0.04, 0.061))

    return [
        p0_tempo,       # P0: tempo
        0.049,          # P1: system constant
        0.0,            # P2: reserved
        p3,             # P3: song-specific body stat
        p4,             # P4: song-specific body stat
        p5,             # P5: song-specific body stat
        p6,             # P6: song-specific body stat
        p7,             # P7: song-specific body stat (negative)
        p8,             # P8: song-specific body stat (negative)
        p9,             # P9: song-specific body stat (negative)
        p10,            # P10: stabilizer
        p11_complexity, # P11: complexity
        p12_duration,   # P12: duration
    ]


def _build_edge_table(
    joint_constraints: list[tuple[int, float]],
    num_states: int,
    strictness: float,
) -> list[tuple[float, float, int]]:
    """Build a 1000-edge table matching real Kinect edge distribution.

    Real Kinect edge table structure (from forensic analysis):
      ~76% scoring edges:
        threshold_a = body position value [-1.0, +1.0] quantized to 0.1
        threshold_b = body position value (Durango scale)
      ~4%  boundary edges:
        threshold_a = values [1.0, 10.0]
        threshold_b = body position value
      ~20% gating edges:
        threshold_a = joint pair index (11-164)
        threshold_b = extreme position value

    State IDs are distributed across the generated state space.
    """
    num_edges = 1000
    edges: list[tuple[float, float, int]] = []

    # Filter constraints by dead zone (keeps joint tags)
    dead_zone = _DEAD_ZONE_MAX * strictness
    filtered = [(jid, v) for jid, v in joint_constraints if abs(v) > dead_zone]

    if len(filtered) < 10:
        filtered = list(joint_constraints) if joint_constraints else [(1, 0.0)]

    n = len(filtered)

    # --- Edge distribution targets (from real file analysis) ---
    target_gating = int(num_edges * 0.20)   # ~200 gating edges
    target_scoring = num_edges - target_gating  # ~800 scoring edges

    # Real gating edge joint pair indices (from MakeItJingle analysis)
    # These are even-stepped values used by Kinect V2 for joint pair encoding
    _GATING_JOINT_PAIRS = [
        12, 14, 16, 18, 20, 22, 30, 32, 34, 36, 38, 40, 42, 44, 46,
        48, 50, 52, 56, 58, 60, 72, 74, 76, 78, 80, 82, 84, 86, 88,
        90, 92, 94, 96, 98, 100, 102, 104, 106, 108, 110, 114, 116,
        118, 120, 124, 126, 128, 130, 132, 134, 136, 138, 140, 142,
        144, 146, 148, 150, 152, 154, 156, 158, 160, 162, 164,
    ]

    # Change 3: Quantized threshold_a bell-curve distribution for scoring edges.
    # Real distribution (from makeitjingle_cut_1.gesture):
    #   0.00: 16%, ±0.10: 19%, ±0.20: 16%, ±0.30: 10%, ±0.40: 10%,
    #   ±0.50: 9%, ±0.60: 8%, ±0.70: 3%, ±0.80: 3%, ±0.90: 1%, ±1.00: 3%
    _QUANTIZED_POOL: list[float] = []
    _QUANT_WEIGHTS = {
        0.0: 16, 0.1: 10, -0.1: 9, 0.2: 8, -0.2: 8,
        0.3: 5, -0.3: 5, 0.4: 5, -0.4: 5, 0.5: 4, -0.5: 5,
        0.6: 3, -0.6: 5, 0.7: 2, -0.7: 1, 0.8: 1, -0.8: 2,
        -1.0: 2, 1.0: 2, -0.9: 1, 0.9: 1,
    }
    for val, weight in _QUANT_WEIGHTS.items():
        _QUANTIZED_POOL.extend([val] * weight)

    # Sort constraints by value for structured pairing
    sorted_filtered = sorted(filtered, key=lambda x: x[1])
    stride_b = n // 3 if n > 3 else 1  # ~33% offset for decorrelation

    for i in range(num_edges):
        state_id = i % num_states

        if i < target_scoring:
            # --- Scoring edge: dual body position thresholds ---
            # threshold_a: quantized position value (bell-curve distribution)
            quant_a = _QUANTIZED_POOL[i % len(_QUANTIZED_POOL)]

            # threshold_b: real camera constraint scaled to Durango range
            ci_b = int(i * n / max(target_scoring, 1)) % n
            _, val_b = sorted_filtered[ci_b]
            scaled_b = val_b * _JDNEXT_TO_DURANGO_SCALE

            edges.append((quant_a, scaled_b, state_id))
        else:
            # --- Gating edge: joint pair index + extreme position ---
            gating_idx = i - target_scoring
            joint_pair = float(
                _GATING_JOINT_PAIRS[gating_idx % len(_GATING_JOINT_PAIRS)]
            )

            # threshold_b: extreme position from camera data
            ci = int(gating_idx * n / max(target_gating, 1)) % n
            _, val = sorted_filtered[ci]
            extreme_val = val * _JDNEXT_TO_DURANGO_SCALE

            edges.append((joint_pair, extreme_val, state_id))

    return edges


def copy_surrogate_as_fallback(
    template_path: Path,
    output_path: Path,
) -> bool:
    """Copy the raw template as a 1:1 fallback (auto-perfect).

    Used when ``convert_jdnext_gestures`` is disabled — the classic
    modding approach of duplicating the template and renaming
    it to match every move name.
    """
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template_path, output_path)
        logger.debug("Copied surrogate fallback: %s", output_path.name)
        return True
    except Exception:
        logger.exception(
            "Failed to copy surrogate fallback to '%s'",
            output_path.name,
        )
        return False
