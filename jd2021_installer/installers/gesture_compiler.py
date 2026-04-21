"""Gesture compiler — JDNext Camera → X360 Kinect hybrid converter.

Converts JDNext smartphone 2D camera gesture data into hybrid format
compatible with legacy Xbox 360 Just Dance engines. Works by injecting
extracted X/Y planar bounding-box logic from JDNext files into a
generic "auto-perfect" surrogate (discorope.gesture), neutralizing
Z-axis constraints so the game evaluates 2D data semi-accurately.

Architecture:
    Phase 1 - JDNext AST Decompiler: Partitions the Float64 bytecode
              into integer opcodes and fractional X/Y tracking constraints.
              Also extracts timing values for parameter calibration.
    Phase 2 - Surrogate Edge Blinding: Reads the X360 Big-Endian surrogate,
              locates the edge table, and overwrites depth thresholds with
              infinity bounds.  Optionally calibrates the parameters block
              using JDNext timing data.
    Phase 3 - Coordinate Synthesis: Sorts and windows JDNext X/Y fractions
              into physiologically coherent bounding-box pairs, then maps
              them onto the surrogate edge thresholds with density-adaptive
              tolerance.
"""

from __future__ import annotations

import logging
import shutil
import statistics
import struct
from pathlib import Path

logger = logging.getLogger("jd2021.installers.gesture_compiler")

# ---------------------------------------------------------------------------
# X360 format constants
# ---------------------------------------------------------------------------

_X360_MAGIC = b"GestureDetectorX360\x00"  # 20 bytes
_X360_MAGIC_LEN = 20
_X360_NUM_EDGES_OFFSET = 24
_X360_EDGE_SIZE = 12  # (float32, float32, int32), all Big-Endian
_X360_PARAMS_COUNT = 13  # 13 float32 calibration values before the edge table
_X360_PARAMS_SIZE = _X360_PARAMS_COUNT * 4  # 52 bytes

# Z-axis neutralization: impossibly wide depth bounds so the Kinect engine
# unconditionally passes the depth dimension for every transition edge.
_Z_BLIND_LOW = -100.0
_Z_BLIND_HIGH = 100.0

# Tolerance range: tight for constraint-dense gestures, loose for sparse ones.
# "Density" = number of extracted X/Y constraints divided by number of edges.
# In-game testing showed the engine needs tolerance >= ~0.16 to find valid
# HMM paths.  Below that threshold, ALL moves score as misses.
_XY_TOLERANCE_MIN = 0.16   # Very precise moves (density >= 3.0)
_XY_TOLERANCE_MAX = 0.35   # Simple/sparse moves (density <= 0.5)
_XY_TOLERANCE_DEFAULT = 0.22

# Density thresholds for the linear interpolation ramp.
_DENSITY_HIGH = 3.0   # At or above this, use _XY_TOLERANCE_MIN
_DENSITY_LOW = 0.5    # At or below this, use _XY_TOLERANCE_MAX

# Strictness scaling: how much (1 - strictness) widens the tolerance.
# tolerance_final = base_tolerance × (1 + STRICTNESS_SCALE × (1 - strictness))
# This keeps the tolerance within the playable range at all strictness values.
_STRICTNESS_SCALE = 1.5

# Timing injection: empirical baseline for discorope's timing characteristics.
# Used as a divisor when scaling the parameters block.
_DISCOROPE_TIMING_BASELINE = 10.0
_TIMING_SCALE_MIN = 0.5   # Clamp to prevent degenerate parameter values
_TIMING_SCALE_MAX = 2.0
_TIMING_MIN_SAMPLES = 5   # Need at least this many timing values to inject

# Joint-band clustering: the X360 format tracks 6 joints.
# Constraints are split into 6 equal bands corresponding to body regions
# sorted by value range (extreme negative → extreme positive).
_X360_NUM_JOINTS = 6

# Z-axis synthesis: biomechanical heuristics for depth estimation from 2D data.
# Arms extended outward (high |X|) are further from the body plane (deeper Z).
_Z_SYNTH_BASE_DEPTH = 0.5       # Center-body baseline depth
_Z_SYNTH_EXTENSION_SCALE = 0.3  # How much X-extension adds to base depth
_Z_SYNTH_RANGE_BASE = 1.5       # Base depth tolerance range
_Z_SYNTH_RANGE_TIGHTEN = 0.2    # How much extension tightens the range
_Z_SYNTH_BLEND_FACTOR = 0.15    # How much Z-depth modifies the final thresholds


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
      bounding values suitable for injection into X360 edge thresholds.
    - **Timing values** (``v > 1.0``): gate duration / weight parameters
      that control when each gesture phase is evaluated.  Used to
      calibrate the surrogate's parameters block.

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
# Phase 2 & 3: Surrogate modification + coordinate synthesis
# ---------------------------------------------------------------------------

def _compute_tolerance(num_constraints: int, num_edges: int) -> float:
    """Compute the dynamic tolerance based on constraint density.

    High-density gestures (many constraints per edge) get tighter
    tolerance for more precise scoring.  Low-density gestures get
    looser tolerance so they remain playable.

    The mapping is a linear interpolation between the extremes:
        density >= 3.0  →  tolerance = 0.15  (tight)
        density <= 0.5  →  tolerance = 0.55  (loose)
        between         →  linear ramp
    """
    if num_edges <= 0:
        return _XY_TOLERANCE_DEFAULT

    density = num_constraints / num_edges

    if density >= _DENSITY_HIGH:
        return _XY_TOLERANCE_MIN
    if density <= _DENSITY_LOW:
        return _XY_TOLERANCE_MAX

    # Linear interpolation
    t = (density - _DENSITY_LOW) / (_DENSITY_HIGH - _DENSITY_LOW)
    return _XY_TOLERANCE_MAX - t * (_XY_TOLERANCE_MAX - _XY_TOLERANCE_MIN)


def _inject_timing_into_params(
    template_data: bytearray,
    edge_start: int,
    timing_values: list[float],
) -> None:
    """Scale the 13-float parameters block using JDNext timing statistics.

    The parameters block sits immediately before the edge table in the
    X360 binary (52 bytes = 13 × float32 BE).  Params[0] and Params[11]
    appear to be tempo/duration-related calibration constants (442.2 and
    158.8 in discorope).  We scale them proportionally to the JDNext
    gesture's timing characteristics to approximate move-specific calibration.

    Only modifies the template if enough timing samples are available
    (>= ``_TIMING_MIN_SAMPLES``) to produce a meaningful median.
    """
    if len(timing_values) < _TIMING_MIN_SAMPLES:
        logger.debug(
            "Too few timing values (%d < %d) — skipping parameter injection",
            len(timing_values), _TIMING_MIN_SAMPLES,
        )
        return

    params_start = edge_start - _X360_PARAMS_SIZE
    if params_start < _X360_MAGIC_LEN:
        logger.warning("Parameters block would overlap header — skipping")
        return

    timing_median = statistics.median(timing_values)

    # Scale factor: how this gesture's tempo compares to discorope baseline
    duration_scale = timing_median / _DISCOROPE_TIMING_BASELINE
    duration_scale = max(_TIMING_SCALE_MIN, min(duration_scale, _TIMING_SCALE_MAX))

    # Param indices to scale (tempo/duration-related)
    # Param[0]  = 442.197  (master duration constant)
    # Param[11] = 158.846  (secondary duration constant)
    TEMPO_PARAM_INDICES = (0, 11)

    for pidx in TEMPO_PARAM_INDICES:
        poff = params_start + pidx * 4
        orig_val = struct.unpack_from(">f", template_data, poff)[0]
        scaled_val = orig_val * duration_scale
        struct.pack_into(">f", template_data, poff, scaled_val)
        logger.debug(
            "Timing injection param[%d]: %.1f → %.1f (scale=%.3f)",
            pidx, orig_val, scaled_val, duration_scale,
        )


def _build_sorted_window_pairs(
    xy_constraints: list[float],
    num_edges: int,
    tolerance: float,
) -> list[tuple[float, float]]:
    """Build threshold pairs using joint-band clustering with sorted windowing.

    This replaces simple sorted windowing with a two-level approach:

    1. **Band clustering**: Sort all constraints and split them into
       6 equal bands (matching the X360's 6 tracked joints).  Each band
       covers a specific value range:

       - Band 0: extreme negative (left-side body width / leg sweep)
       - Band 1: moderate negative (left torso / left arm)
       - Band 2: near-zero negative (center-left body positions)
       - Band 3: near-zero positive (center-right body positions)
       - Band 4: moderate positive (right torso / right arm)
       - Band 5: extreme positive (right-side body width / raised arm)

    2. **Regional edge mapping**: Each band gets a proportional slice
       of the 1000 edges.  Within each band, constraints are windowed
       into coherent bounding-box pairs.

    3. **Z-axis depth synthesis**: A small depth modifier is blended
       into the threshold pair based on the constraint's absolute X
       position.  Extended body positions (high |X|) get slightly
       tighter threshold bounds, simulating how a real Kinect would
       score depth-aware poses.

    This produces physiologically meaningful threshold distributions
    where edge groups correspond to specific body regions.
    """
    if not xy_constraints:
        return []

    sorted_xy = sorted(xy_constraints)
    n = len(sorted_xy)
    num_bands = min(_X360_NUM_JOINTS, n)  # Never more bands than data

    if num_bands < 1:
        return []

    # Split into equal bands
    band_size = n // num_bands
    bands: list[list[float]] = []
    for b in range(num_bands):
        start = b * band_size
        end = start + band_size if b < num_bands - 1 else n
        bands.append(sorted_xy[start:end])

    # Assign proportional edge count per band
    edges_per_band = num_edges // num_bands
    extra_edges = num_edges % num_bands  # distribute remainder

    all_pairs: list[tuple[float, float]] = []

    for band_idx, band_values in enumerate(bands):
        bn = len(band_values)
        # This band's edge allocation
        band_edges = edges_per_band + (1 if band_idx < extra_edges else 0)

        if band_edges <= 0 or bn == 0:
            continue

        # Window within this band to produce band_edges pairs
        window = max(1, bn // max(band_edges, 1))
        step = max(1, window)

        band_pairs: list[tuple[float, float]] = []
        for i in range(0, bn - window, step):
            lo = band_values[i]
            hi = band_values[min(i + window, bn - 1)]

            # Z-axis depth synthesis: tighten bounds for extended positions
            mean_pos = (lo + hi) / 2.0
            x_extension = abs(mean_pos)
            z_depth_modifier = x_extension * _Z_SYNTH_BLEND_FACTOR

            # Extended positions: slightly tighter bounding box
            # (simulates depth-aware scoring where outstretched arms
            # must be more precisely positioned)
            actual_lo = lo - tolerance + z_depth_modifier
            actual_hi = hi + tolerance - z_depth_modifier

            # Safety: ensure lo < hi after modification
            if actual_lo >= actual_hi:
                mid = (lo + hi) / 2.0
                actual_lo = mid - tolerance * 0.5
                actual_hi = mid + tolerance * 0.5

            band_pairs.append((actual_lo, actual_hi))

        # Pad band if needed
        if band_pairs and len(band_pairs) < band_edges:
            band_pairs.extend([band_pairs[-1]] * (band_edges - len(band_pairs)))
        elif not band_pairs:
            # Fallback: single pair from band extremes
            lo = band_values[0] - tolerance
            hi = band_values[-1] + tolerance
            band_pairs = [(lo, hi)] * band_edges

        all_pairs.extend(band_pairs[:band_edges])

    # Final safety: ensure we have exactly num_edges pairs
    if len(all_pairs) < num_edges:
        if all_pairs:
            all_pairs.extend([all_pairs[-1]] * (num_edges - len(all_pairs)))
        else:
            all_pairs = [(-tolerance, tolerance)] * num_edges

    logger.debug(
        "Band clustering: %d constraints → %d bands × ~%d edges/band, "
        "%d total pairs",
        n, num_bands, edges_per_band, len(all_pairs),
    )
    return all_pairs[:num_edges]


def _load_and_hybridize(
    template_data: bytearray,
    xy_constraints: list[float],
    timing_values: list[float] | None = None,
    strictness: float = 1.0,
) -> bytearray:
    """Given a mutable copy of the surrogate template bytes and a list of
    X/Y constraint values from JDNext, blind the Z-axis and inject the
    real 2D thresholds into the edge table.

    The injection pipeline applies three layers:

    1. **Z-axis neutralization** — all edges set to [-100, +100]
    2. **Timing calibration** — parameters block scaled from JDNext timing
    3. **Band-clustered X/Y injection** — constraints split into 6 joint
       bands with Z-depth synthesis, distributed across edges with
       strictness-controlled blending

    Args:
        template_data:   Mutable bytearray of the surrogate X360 binary.
        xy_constraints:  Extracted JDNext X/Y tracking fractions ([-1, 1]).
        timing_values:   Extracted JDNext timing/weight values (> 1.0).
                         Used for parameters block calibration.
        strictness:      Scoring strictness dial (0.0 = auto-perfect,
                         1.0 = full JDNext thresholds).  Values in between
                         linearly blend from Z-blinded bounds toward the
                         real JDNext-derived bounds.

    Returns the modified bytearray ready to write to disk.
    """
    # Validate magic header
    if template_data[:_X360_MAGIC_LEN] != _X360_MAGIC:
        raise ValueError(
            f"Template is not a GestureDetectorX360 file "
            f"(magic: {template_data[:_X360_MAGIC_LEN]!r})"
        )

    num_edges = struct.unpack_from(">i", template_data, _X360_NUM_EDGES_OFFSET)[0]
    edge_start = len(template_data) - (num_edges * _X360_EDGE_SIZE)

    if edge_start < _X360_MAGIC_LEN or edge_start >= len(template_data):
        raise ValueError(
            f"Calculated edge table start ({edge_start}) is out of range "
            f"for file of {len(template_data)} bytes"
        )

    # Phase 2a: Blind ALL edges first (Z-axis neutralization fallback)
    for e in range(num_edges):
        eoff = edge_start + e * _X360_EDGE_SIZE
        struct.pack_into(">f", template_data, eoff, _Z_BLIND_LOW)
        struct.pack_into(">f", template_data, eoff + 4, _Z_BLIND_HIGH)
        # next_state_id at eoff+8 is LEFT UNTOUCHED

    # Phase 2b: Inject timing data into the parameters block
    if timing_values:
        _inject_timing_into_params(template_data, edge_start, timing_values)

    # Phase 3: If we have JDNext X/Y constraints, inject them over the
    # blinded infinity bounds to create semi-real scoring.
    if not xy_constraints:
        logger.debug("No X/Y constraints to inject; output is fully blinded")
        return template_data

    # If strictness is zero, skip injection entirely (auto-perfect mode)
    if strictness <= 0.0:
        logger.debug("Strictness=0.0; output is fully blinded (auto-perfect)")
        return template_data

    # Compute dynamic tolerance based on constraint density
    base_tolerance = _compute_tolerance(len(xy_constraints), num_edges)

    # Strictness controls how tight the threshold windows are.
    # At strictness=1.0, use the base (tight) tolerance.
    # At lower strictness, widen it linearly.  This keeps tolerance
    # within the empirically validated playable range (~0.16 to ~0.53)
    # instead of the exponential 1/s scaling that created a cliff
    # between "all perfects" and "all misses".
    #
    # Dense gesture (base=0.16):
    #   s=1.0 → tol=0.16  (tightest real scoring)
    #   s=0.7 → tol=0.23  (moderate)
    #   s=0.5 → tol=0.28  (forgiving)
    #   s=0.3 → tol=0.33  (very forgiving)
    tolerance = base_tolerance * (1.0 + _STRICTNESS_SCALE * (1.0 - strictness))

    # Build band-clustered pairs with Z-depth synthesis
    pairs = _build_sorted_window_pairs(xy_constraints, num_edges, tolerance)

    if not pairs:
        return template_data

    # Inject ALL edges with real thresholds (no Z-blinded auto-pass gaps).
    # Every edge gets a constrained range so the HMM graph enforces
    # position-specific scoring at every transition.
    for edge_idx in range(num_edges):
        threshold_a, threshold_b = pairs[edge_idx]

        eoff = edge_start + edge_idx * _X360_EDGE_SIZE
        struct.pack_into(">f", template_data, eoff, threshold_a)
        struct.pack_into(">f", template_data, eoff + 4, threshold_b)

    logger.debug(
        "Injected %d constrained edges "
        "(tolerance=%.3f [base=%.3f], strictness=%.2f, z_synth=enabled)",
        num_edges, tolerance, base_tolerance, strictness,
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
    """Compile a single JDNext gesture into a hybrid X360 Kinect gesture.

    Reads the JDNext Float64 bytecode, extracts X/Y constraints and
    timing data, loads the surrogate template, neutralizes Z-axis depth
    validation, optionally calibrates the parameters block from timing
    data, injects real 2D thresholds with density-adaptive tolerance,
    and writes the hybrid binary to ``output_path``.

    Args:
        jdnext_src_path: Path to the JDNext Camera ``.gesture`` file.
        template_path:   Path to the ``discorope.gesture`` X360 surrogate.
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
            logger.error("Surrogate template not found: %s", template_path)
            return False

        # Phase 1: Decompile JDNext AST
        xy_constraints, timing_values = _decompile_jdnext(jdnext_src_path)

        # Load template as mutable bytearray
        template_data = bytearray(template_path.read_bytes())

        # Phase 2 + 3: Blind Z-axis, calibrate params, inject X/Y constraints
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
    """Copy the raw surrogate template as a 1:1 fallback (auto-perfect).

    Used when ``convert_jdnext_gestures`` is disabled — the classic
    modding approach of duplicating ``discorope.gesture`` and renaming
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
