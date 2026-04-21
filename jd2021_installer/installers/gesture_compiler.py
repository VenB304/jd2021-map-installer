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
import random
import shutil
import statistics
import struct
from pathlib import Path

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

# Gate value distribution for threshold_a (empirically derived from real files).
# Values are (weight_value, probability) tuples.
_GATE_SMALL = [0.0, 0.1, -0.1, 0.2, -0.2, 0.4, -0.4, 0.5, -0.5,
               0.8, -0.8, 1.0, -1.0, 1.03, -1.03]
_GATE_LARGE = [30, 48, 50, 56, 68, 74, 86, 88, 96, 120, 124, 142, 156,
               -20, -30, -50, -60, -90, -110, -120, -180, -230, -250, -270]
_GATE_MEDIUM = [2.0, -2.0, 3.0, -3.0, 4.0, -4.0, 4.5, -4.5, 5.0, -5.0,
                6.0, -6.0, 8.0, -8.0, 10.0, -10.0]

# Probability distribution: 72% small, 8% medium, 20% large gating
_GATE_SMALL_PCT = 0.72
_GATE_MEDIUM_PCT = 0.08
# _GATE_LARGE_PCT = 0.20   (remainder)

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

def _decompile_jdnext(gesture_path: Path) -> tuple[list[float], list[float]]:
    """Read a JDNext Camera .gesture file (raw Float64 array) and return
    the extracted tracking data as ``(xy_constraints, timing_values)``.

    The JDNext format is a headerless sequential array of float64 doubles.
    Index 0 is the declared sequence length.  The early indices contain
    integer-valued opcodes; the tail contains fractional tracking data.
    We partition the two zones by detecting the first sustained run of
    fractional (non-integer) values, then classify the constraint zone:

    - **X/Y constraints** (``-1.0 <= v <= 1.0``): planar body-position
      bounding values suitable for injection into Durango edge thresholds.
    - **Timing values** (``v > 1.0``): gate duration / weight parameters
      that control when each gesture phase is evaluated.  Used to
      calibrate the template's parameters block.

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
    endian: str,
) -> None:
    """Scale the 13-float parameters block using JDNext timing statistics.

    The parameters block sits immediately before the edge table in both
    X360 and Durango formats (52 bytes = 13 × float32).  Params[0] and
    Params[11] appear to be tempo/duration-related calibration constants.
    We scale them proportionally to the JDNext gesture's timing
    characteristics to approximate move-specific calibration.
    """
    if len(timing_values) < _TIMING_MIN_SAMPLES:
        logger.debug(
            "Too few timing values (%d < %d) — skipping parameter injection",
            len(timing_values), _TIMING_MIN_SAMPLES,
        )
        return

    params_start = edge_start - _DURANGO_PARAMS_SIZE
    if params_start < 0:
        logger.warning("Parameters block would overlap header — skipping")
        return

    timing_median = statistics.median(timing_values)
    duration_scale = timing_median / _TIMING_BASELINE
    duration_scale = max(_TIMING_SCALE_MIN, min(duration_scale, _TIMING_SCALE_MAX))

    # Param indices to scale (tempo/duration-related)
    TEMPO_PARAM_INDICES = (0, 11)

    for pidx in TEMPO_PARAM_INDICES:
        poff = params_start + pidx * 4
        orig_val = struct.unpack_from(f"{endian}f", template_data, poff)[0]
        scaled_val = orig_val * duration_scale
        struct.pack_into(f"{endian}f", template_data, poff, scaled_val)
        logger.debug(
            "Timing injection param[%d]: %.1f → %.1f (scale=%.3f)",
            pidx, orig_val, scaled_val, duration_scale,
        )


def _generate_gate_values(num_edges: int, seed: int = _GATE_RNG_SEED) -> list[float]:
    """Generate threshold_a (gate/weight) values matching the real Durango
    distribution observed in 894 Kinect gesture files.

    Distribution:
        ~72% small: {0.0, ±0.1, ±0.2, ±0.5, ±1.0, ...}
        ~8%  medium: {±2.0, ±4.0, ±5.0, ±10.0, ...}
        ~20% large gating: {50, 74, 120, -180, ...}
    """
    rng = random.Random(seed)
    gates = []
    for _ in range(num_edges):
        r = rng.random()
        if r < _GATE_SMALL_PCT:
            gates.append(rng.choice(_GATE_SMALL))
        elif r < _GATE_SMALL_PCT + _GATE_MEDIUM_PCT:
            gates.append(rng.choice(_GATE_MEDIUM))
        else:
            gates.append(rng.choice(_GATE_LARGE))
    return gates


def _map_constraints_to_thresholds(
    xy_constraints: list[float],
    num_edges: int,
) -> list[float]:
    """Map JDNext X/Y constraints to threshold_b values for edge injection.

    Samples ``num_edges`` values from the constraint list, scaled by the
    JDNext→Durango factor (0.63×) to match the real P5-P95 range of
    [-0.42, +0.45].

    The constraints are distributed evenly across the edge table to
    provide broad positional coverage.
    """
    if not xy_constraints:
        return [0.0] * num_edges

    n = len(xy_constraints)
    thresholds = []

    for edge_idx in range(num_edges):
        # Map edge index proportionally into the constraints list
        ci = int(edge_idx * n / num_edges) % n
        raw_value = xy_constraints[ci]
        # Scale to match real Durango threshold_b range
        thresholds.append(raw_value * _JDNEXT_TO_DURANGO_SCALE)

    return thresholds


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

    # Phase 2: Inject timing data into the parameters block
    if timing_values:
        _inject_timing_into_params(
            template_data, edge_start, timing_values, endian,
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

    # Phase 3: Generate edge values matching real Durango distribution
    gate_values = _generate_gate_values(num_edges)
    position_thresholds = _map_constraints_to_thresholds(filtered, num_edges)

    # Inject into every edge
    for edge_idx in range(num_edges):
        eoff = edge_start + edge_idx * _DURANGO_EDGE_SIZE

        threshold_a = gate_values[edge_idx]
        threshold_b = position_thresholds[edge_idx]

        # Write threshold_a and threshold_b, preserve state_id
        struct.pack_into(f"{endian}f", template_data, eoff, threshold_a)
        struct.pack_into(f"{endian}f", template_data, eoff + 4, threshold_b)
        # eoff + 8 (state_id) is LEFT UNTOUCHED — preserving HMM topology

    logger.debug(
        "Injected %d edges (dead_zone=%.3f, strictness=%.2f, "
        "scale=%.2f, format=%s)",
        num_edges, dead_zone, strictness, _JDNEXT_TO_DURANGO_SCALE, fmt_name,
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
