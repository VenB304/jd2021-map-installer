"""Gesture compiler — JDNext Camera → Durango Kinect hybrid converter.

Converts JDNext smartphone 2D camera gesture data into hybrid format
compatible with legacy Xbox 360 Just Dance engines.  Uses a real
Durango Kinect gesture file as a structural template and injects
JDNext X/Y constraint data into the edge table's threshold_b field,
while generating realistic gate/weight values in threshold_a.

Architecture:
    Phase 1 - JDNext AST Decompiler: Partitions the Float64 bytecode
              into integer opcodes and fractional X/Y tracking constraints.
              Also extracts timing values for parameter calibration.
    Phase 2 - Template Preparation: Reads the Durango little-endian
              template, locates the edge table, and calibrates the
              parameters block using JDNext timing data.
    Phase 3 - Edge Injection: Maps JDNext constraints to threshold_b
              (position scoring) and generates threshold_a values
              (gate/weight) matching the statistical distribution found
              in real Kinect gesture files.

Edge Field Semantics (from reverse-engineering 894 real Kinect files):
    threshold_a (float32): gate/weight value
        ~72% of edges: small values (0.0, ±0.1, ±0.2, ±0.5, ±1.0)
        ~20% of edges: large gating values (50, 74, 120, etc.)
        ~8%  of edges: moderate values (2–10)
    threshold_b (float32): body position threshold
        Range: [-0.5, +0.5] for 94% of real values
        JDNext values are scaled by 0.63× to match this range
"""

from __future__ import annotations

import logging
import math
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
# threshold_b: position threshold — compared against Kinect sensor reading.
#   - Same-song comparison (MakeItJingle in both JDU & JDNext) shows:
#     JDNext [-0.7, +1.0] maps to Durango [-2.6, +2.9] → scale ~3.0×
#   - Validated across 6 matched gesture pairs: scale range 2.0–4.2×

_JDNEXT_TO_DURANGO_SCALE = 3.0  # Scale JDNext [-1,+1] to Durango [-3,+3]

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
# Phase 1: JDNext AST Decompiler
# ---------------------------------------------------------------------------

# JDNext opcode zone structure (decoded from MakeItJingle Rosetta Stone):
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


def _decompile_jdnext(gesture_path: Path) -> tuple[list[float], list[float]]:
    """Read a JDNext Camera .gesture file (raw Float64 array) and return
    the extracted tracking data as ``(xy_constraints, timing_values)``.

    The JDNext format is a headerless sequential array of float64 doubles.
    The opcode zone encodes section structure:

    - ``opcode[0]``: total float count
    - ``opcode[1]``: number of temporal sections (keyframes)
    - ``opcode[2..29]``: body part descriptor table
    - Section descriptors define constraint/timing counts per section

    The constraint zone contains per-section data in temporal order.
    We extract all X/Y constraints and timing values, preserving the
    original temporal ordering from the section layout.

    Returns:
        A tuple of ``(xy_constraints, timing_values)``.
    """
    data = gesture_path.read_bytes()
    num_floats = len(data) // 8
    if num_floats < 10:
        logger.warning("JDNext gesture '%s' has only %d floats — too short",
                       gesture_path.name, num_floats)
        return [], []

    raw = list(struct.unpack(f"<{num_floats}d", data))

    # Walk forward from index 1 to find where fractional values start.
    # The boundary is where 3+ consecutive non-integer values appear.
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

    # Extract X/Y candidates from the constraint zone
    xy_constraints = [v for v in constraint_zone if -1.0 <= v <= 1.0]

    # Extract timing / weight values (gate durations in the scoring cadence)
    timing_values = [v for v in constraint_zone if v > 1.0]

    logger.debug(
        "JDNext decompile '%s': %d total floats, boundary=%d, "
        "%d X/Y constraints, %d timing values extracted",
        gesture_path.name, num_floats, boundary,
        len(xy_constraints), len(timing_values),
    )
    return xy_constraints, timing_values


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
    xy_constraints: list[float],
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
    if xy_constraints and len(xy_constraints) >= 20:
        scaled = [v * _JDNEXT_TO_DURANGO_SCALE for v in xy_constraints]
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
    xy_constraints: list[float],
    timing_values: list[float] | None = None,
    strictness: float = 1.0,
) -> bytearray:
    """Load a Durango/X360 template and inject JDNext data into the edge table.

    The injection pipeline:

    1. **Detect format** — Durango (LE) or X360 (BE)
    2. **Timing calibration** — parameters block scaled from JDNext timing
    3. **Edge injection** — threshold_a set to gate/weight distribution,
       threshold_b set to scaled JDNext position values.
       Center-exclusion filtering controlled by strictness.

    Args:
        template_data:   Mutable bytearray of the Durango/X360 template.
        xy_constraints:  Extracted JDNext X/Y tracking fractions ([-1, 1]).
        timing_values:   Extracted JDNext timing/weight values (> 1.0).
        strictness:      Scoring strictness (0.0 = auto-perfect,
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
        xy_constraints, endian,
    )

    # If no constraints or auto-perfect mode, leave edges untouched
    if not xy_constraints:
        logger.debug("No X/Y constraints to inject; output uses template edges")
        return template_data

    if strictness <= 0.0:
        logger.debug("Strictness=0.0; output uses template edges (auto-perfect)")
        return template_data

    # Center-exclusion dead zone filtering
    dead_zone = _DEAD_ZONE_MAX * strictness
    filtered = [v for v in xy_constraints if abs(v) > dead_zone]

    # Safety fallback
    if len(filtered) < num_edges:
        dead_zone *= 0.5
        filtered = [v for v in xy_constraints if abs(v) > dead_zone]

    if len(filtered) < 10:
        logger.warning(
            "Dead zone %.3f filtered too aggressively (%d remain); "
            "falling back to unfiltered",
            dead_zone, len(filtered),
        )
        filtered = xy_constraints
        dead_zone = 0.0

    logger.debug(
        "Center-exclusion: dead_zone=%.3f, %d/%d constraints kept (%.0f%%)",
        dead_zone, len(filtered), len(xy_constraints),
        len(filtered) / max(len(xy_constraints), 1) * 100,
    )

    # Phase 3: Inject JDNext constraints into edge thresholds.
    #
    # Strategy based on Rosetta Stone analysis of real Kinect files:
    #   - ~75% of edges have BOTH threshold_a and threshold_b as body
    #     position values (|a| <= ~1, |b| <= ~1 in normalized space).
    #   - ~19% of edges have threshold_a as a joint/feature index (|a| > 10),
    #     with threshold_b as the expected position for that joint.
    #   - ~6% boundary (1 < |a| <= 10).
    #
    # We PRESERVE the template's gating edges (|a| > 10) since they define
     # valid HMM structure (which transitions are possible). For all other
    # edges, we inject JDNext constraints into BOTH fields — this forces
    # the engine to match two body positions simultaneously per edge,
    # making random movement nearly impossible to trigger a match.

    # Sort constraints to pair nearby body positions (same choreographic pose)
    sorted_constraints = sorted(filtered)
    n = len(sorted_constraints)

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

    # --- Gating ratio matching ---
    # Real Kinect gestures have ~20% gating edges (range 17-28%).
    # Our template may have less. Promote scoring edges at the extreme
    # ends of the constraint distribution to gating edges, using
    # real Kinect joint indices (even-stepping values 12-148).
    _TARGET_GATING_PCT = 0.20
    _JOINT_INDICES = [12, 14, 16, 20, 22, 24, 26, 28, 30, 32, 34, 36,
                      38, 40, 48, 56, 64, 72, 80, 90, 96, 102, 108,
                      114, 120, 126, 132, 138, 144]

    target_gating = int(num_edges * _TARGET_GATING_PCT)
    promote_count = max(0, target_gating - len(gating_indices))

    if promote_count > 0 and len(scoring_indices) > promote_count:
        # Promote scoring edges from the extreme ends
        # (first and last in the sorted scoring list by edge index)
        promoted = scoring_indices[:promote_count]
        scoring_indices = scoring_indices[promote_count:]

        for j, edge_idx in enumerate(promoted):
            eoff = edge_start + edge_idx * _DURANGO_EDGE_SIZE
            joint_idx = _JOINT_INDICES[j % len(_JOINT_INDICES)]
            # threshold_a = joint index, threshold_b = extreme position
            extreme_val = sorted_constraints[j % n] * _JDNEXT_TO_DURANGO_SCALE * 1.5
            struct.pack_into(f"{endian}f", template_data, eoff, float(joint_idx))
            struct.pack_into(f"{endian}f", template_data, eoff + 4, extreme_val)

        gating_indices.extend(promoted)

    # --- Song-specific gating threshold_b ---
    # Update existing gating edges' threshold_b with extreme JDNext
    # positions so even structural gates reflect the song's movement.
    if n > 0:
        for j, edge_idx in enumerate(gating_indices):
            eoff = edge_start + edge_idx * _DURANGO_EDGE_SIZE
            # Keep threshold_a (joint index) — only update threshold_b
            extreme_idx = int(j * n / max(len(gating_indices), 1)) % n
            extreme_val = sorted_constraints[extreme_idx] * _JDNEXT_TO_DURANGO_SCALE
            struct.pack_into(f"{endian}f", template_data, eoff + 4, extreme_val)

    # --- Scoring edge injection ---
    num_scoring = len(scoring_indices)
    stride_b = n // 3 if n > 3 else 1  # ~33% offset for decorrelation

    for i, edge_idx in enumerate(scoring_indices):
        eoff = edge_start + edge_idx * _DURANGO_EDGE_SIZE

        # threshold_a: sequential walk through sorted constraints
        ci_a = int(i * n / max(num_scoring, 1)) % n
        # threshold_b: offset walk (decorrelated but full range)
        ci_b = (ci_a + stride_b) % n

        val_a = sorted_constraints[ci_a] * _JDNEXT_TO_DURANGO_SCALE
        val_b = sorted_constraints[ci_b] * _JDNEXT_TO_DURANGO_SCALE

        struct.pack_into(f"{endian}f", template_data, eoff, val_a)
        struct.pack_into(f"{endian}f", template_data, eoff + 4, val_b)
        # eoff + 8 (state_id) is LEFT UNTOUCHED — preserving HMM topology

    total_gating = len(gating_indices)
    logger.debug(
        "Injected %d scoring + %d gating edges (%d promoted, "
        "gating=%.0f%%, dead_zone=%.3f, strictness=%.2f, "
        "scale=%.2f, format=%s)",
        num_scoring, total_gating, promote_count,
        total_gating / num_edges * 100,
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

        # Phase 1: Decompile JDNext AST
        xy_constraints, timing_values = _decompile_jdnext(jdnext_src_path)

        # Load template as mutable bytearray
        template_data = bytearray(template_path.read_bytes())

        # Phase 2 + 3: Calibrate params + inject edges
        hybrid_data = _load_and_hybridize(
            template_data, xy_constraints, timing_values, strictness,
        )

        # Write output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(hybrid_data)

        logger.info(
            "Compiled hybrid gesture: %s (%d X/Y + %d timing → %s, "
            "strictness=%.2f)",
            jdnext_src_path.name,
            len(xy_constraints),
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
    """Compile a JDNext gesture into a Durango binary WITHOUT a template.

    Uses the dynamic HMM generator to build the entire state table from
    scratch based on the JDNext section structure, then injects the
    extracted X/Y constraints into a freshly generated edge table.

    This eliminates the template dependency entirely — every generated
    gesture has a unique, song-specific HMM topology.

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

        # Phase 1: Decompile JDNext AST
        xy_constraints, timing_values = _decompile_jdnext(jdnext_src_path)
        num_sections = _count_jdnext_sections(jdnext_src_path)

        if not xy_constraints:
            logger.warning(
                "No constraints extracted from '%s'; cannot generate",
                jdnext_src_path.name,
            )
            return False

        # Phase 2: Generate dynamic HMM state table
        state_table, num_states = generate_state_table(
            num_sections, len(xy_constraints),
        )

        # Phase 3: Build parameters from JDNext section data
        params = _build_params_from_jdnext(xy_constraints, num_sections)

        # Phase 4: Build edge table with JDNext constraint injection
        edges = _build_edge_table(
            xy_constraints, num_states, strictness,
        )

        # Phase 5: Assemble complete binary
        gesture_data = build_gesture_binary(
            state_table, num_states, params, edges, num_joints=9,
        )

        # Write output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(gesture_data)

        logger.info(
            "Compiled gesture from scratch: %s "
            "(%d X/Y, %d sections, %d states, strictness=%.2f)",
            jdnext_src_path.name,
            len(xy_constraints),
            num_sections,
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
    xy_constraints: list[float],
    num_sections: int,
) -> list[float]:
    """Build the 13-float parameters block from JDNext data.

    P0, P11, P12:  Tempo/duration/complexity derived from Section A/B descriptors.
    P1:            System constant (0.049).
    P2:            Reserved (0.0).
    P3-P10:        Statistical body position encoding from constraints.
    """
    # Map JDNext "Section A/B" timing descriptors (7/31 counts) 
    # to the actual duration/weight parameters.
    type_a_count = min(4, num_sections)
    type_b_count = max(0, num_sections - 4)
    
    # 7 timing counts for intro poses, 31 for full body poses
    total_timing_weight = (type_a_count * 7) + (type_b_count * 31)

    # Convert to Durango scaling ratios (based on Durango baseline regression)
    p0_tempo = total_timing_weight * 0.42
    p11_complexity = total_timing_weight * 0.065
    p12_duration = total_timing_weight * 0.038

    # Compute constraint statistics
    scaled = [v * _JDNEXT_TO_DURANGO_SCALE for v in xy_constraints]
    mean_val = statistics.mean(scaled) if scaled else 0.0
    std_val = statistics.stdev(scaled) if len(scaled) > 1 else 0.3

    return [
        p0_tempo,                                # P0: tempo
        0.049,                                   # P1: system constant
        0.0,                                     # P2: reserved
        abs(mean_val) + std_val * 0.3,           # P3: +
        abs(mean_val) + std_val * 0.8,           # P4: +
        abs(mean_val) + std_val * 0.5,           # P5: +
        std_val * 0.1,                           # P6: +
        -(abs(mean_val) + std_val * 0.9),        # P7: -
        -(abs(mean_val) + std_val * 0.3),        # P8: -
        -(abs(mean_val) + std_val * 0.6),        # P9: -
        std_val * 0.08,                          # P10: +
        p11_complexity,                          # P11: complexity
        p12_duration,                            # P12: duration
    ]


def _build_edge_table(
    xy_constraints: list[float],
    num_states: int,
    strictness: float,
) -> list[tuple[float, float, int]]:
    """Build a 1000-edge table with JDNext constraint injection.

    Edge distribution mirrors real Kinect files:
      ~80% scoring edges (dual threshold_a + threshold_b injection)
      ~20% gating edges (joint index in threshold_a)

    State IDs are distributed across the generated state space.
    """
    num_edges = 1000
    edges: list[tuple[float, float, int]] = []

    # Filter constraints by dead zone
    dead_zone = _DEAD_ZONE_MAX * strictness
    filtered = [v for v in xy_constraints if abs(v) > dead_zone]

    if len(filtered) < 10:
        filtered = xy_constraints if xy_constraints else [0.0]

    sorted_constraints = sorted(filtered)
    n = len(sorted_constraints)
    stride_b = n // 3 if n > 3 else 1

    # Gating parameters
    target_gating = int(num_edges * 0.20)
    joint_indices = [12, 14, 16, 20, 22, 24, 26, 28, 30, 32, 34, 36,
                     38, 40, 48, 56, 64, 72, 80, 90, 96, 102, 108,
                     114, 120, 126, 132, 138, 144]

    for i in range(num_edges):
        # State ID: distribute across all states
        state_id = i % num_states

        if i < target_gating:
            # Gating edge: joint index in threshold_a
            joint_idx = joint_indices[i % len(joint_indices)]
            extreme_val = sorted_constraints[i % n] * _JDNEXT_TO_DURANGO_SCALE * 1.5
            edges.append((float(joint_idx), extreme_val, state_id))
        else:
            # Scoring edge: dual position thresholds
            ci_a = int((i - target_gating) * n
                       / max(num_edges - target_gating, 1)) % n
            ci_b = (ci_a + stride_b) % n

            val_a = sorted_constraints[ci_a] * _JDNEXT_TO_DURANGO_SCALE
            val_b = sorted_constraints[ci_b] * _JDNEXT_TO_DURANGO_SCALE
            edges.append((val_a, val_b, state_id))

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
