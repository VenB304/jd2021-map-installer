"""Gesture compiler — JDNext Camera → X360 Kinect hybrid converter.

Converts JDNext smartphone 2D camera gesture data into hybrid format
compatible with legacy Xbox 360 Just Dance engines. Works by injecting
extracted X/Y planar bounding-box logic from JDNext files into a
generic "auto-perfect" surrogate (discorope.gesture), neutralizing
Z-axis constraints so the game evaluates 2D data semi-accurately.

Architecture:
    Phase 1 - JDNext AST Decompiler: Partitions the Float64 bytecode
              into integer opcodes and fractional X/Y tracking constraints.
    Phase 2 - Surrogate Edge Blinding: Reads the X360 Big-Endian surrogate,
              locates the edge table, and overwrites depth thresholds with
              infinity bounds.
    Phase 3 - Coordinate Synthesis: Maps JDNext X/Y fractions onto the
              surrogate edge thresholds and compiles the output binary.
"""

from __future__ import annotations

import logging
import shutil
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

# Z-axis neutralization: impossibly wide depth bounds so the Kinect engine
# unconditionally passes the depth dimension for every transition edge.
_Z_BLIND_LOW = -100.0
_Z_BLIND_HIGH = 100.0

# How much leeway (in normalized coordinate units) to add around each
# extracted JDNext X/Y constraint value.  Keeps scoring playable rather
# than impossibly strict.
_XY_TOLERANCE = 0.35


# ---------------------------------------------------------------------------
# Phase 1: JDNext AST Decompiler
# ---------------------------------------------------------------------------

def _decompile_jdnext(gesture_path: Path) -> list[float]:
    """Read a JDNext Camera .gesture file (raw Float64 array) and return
    the X/Y tracking constraint values (fractional, mostly in [-1, 1]).

    The JDNext format is a headerless sequential array of float64 doubles.
    Index 0 is the declared sequence length.  The early indices contain
    integer-valued opcodes; the tail contains fractional tracking data.
    We partition the two zones by detecting the first sustained run of
    fractional (non-integer) values, then extract only the values in the
    [-1.0, 1.0] range as usable X/Y planar constraints.
    """
    data = gesture_path.read_bytes()
    num_floats = len(data) // 8
    if num_floats < 10:
        logger.warning("JDNext gesture '%s' has only %d floats — too short",
                       gesture_path.name, num_floats)
        return []

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

    # Extract X/Y candidates from the constraint zone
    xy_constraints = [v for v in raw[boundary:] if -1.0 <= v <= 1.0]

    logger.debug(
        "JDNext decompile '%s': %d total floats, boundary=%d, "
        "%d X/Y constraints extracted",
        gesture_path.name, num_floats, boundary, len(xy_constraints),
    )
    return xy_constraints


# ---------------------------------------------------------------------------
# Phase 2 & 3: Surrogate modification + coordinate synthesis
# ---------------------------------------------------------------------------

def _load_and_hybridize(
    template_data: bytearray,
    xy_constraints: list[float],
) -> bytearray:
    """Given a mutable copy of the surrogate template bytes and a list of
    X/Y constraint values from JDNext, blind the Z-axis and inject the
    real 2D thresholds into the edge table.

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

    # Phase 2: Blind ALL edges first (Z-axis neutralization fallback)
    for e in range(num_edges):
        eoff = edge_start + e * _X360_EDGE_SIZE
        struct.pack_into(">f", template_data, eoff, _Z_BLIND_LOW)
        struct.pack_into(">f", template_data, eoff + 4, _Z_BLIND_HIGH)
        # next_state_id at eoff+8 is LEFT UNTOUCHED

    # Phase 3: If we have JDNext X/Y constraints, inject them over the
    # blinded infinity bounds to create semi-real scoring.
    if not xy_constraints:
        logger.debug("No X/Y constraints to inject; output is fully blinded")
        return template_data

    # Pair consecutive constraints as (lower_bound, upper_bound)
    pairs: list[tuple[float, float]] = []
    for i in range(0, len(xy_constraints) - 1, 2):
        lo = xy_constraints[i]
        hi = xy_constraints[i + 1]
        actual_lo = min(lo, hi) - _XY_TOLERANCE
        actual_hi = max(lo, hi) + _XY_TOLERANCE
        pairs.append((actual_lo, actual_hi))

    if not pairs:
        return template_data

    # Distribute pairs proportionally across all edges
    num_pairs = len(pairs)
    for edge_idx in range(num_edges):
        pair_idx = min(int((edge_idx / num_edges) * num_pairs), num_pairs - 1)
        threshold_a, threshold_b = pairs[pair_idx]
        eoff = edge_start + edge_idx * _X360_EDGE_SIZE
        struct.pack_into(">f", template_data, eoff, threshold_a)
        struct.pack_into(">f", template_data, eoff + 4, threshold_b)

    logger.debug(
        "Injected %d constraint pairs across %d edges",
        num_pairs, num_edges,
    )
    return template_data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compile_hybrid_gesture(
    jdnext_src_path: Path,
    template_path: Path,
    output_path: Path,
) -> bool:
    """Compile a single JDNext gesture into a hybrid X360 Kinect gesture.

    Reads the JDNext Float64 bytecode, extracts X/Y constraints, loads
    the surrogate template, neutralizes Z-axis depth validation, injects
    real 2D thresholds, and writes the hybrid binary to ``output_path``.

    Args:
        jdnext_src_path: Path to the JDNext Camera ``.gesture`` file.
        template_path:   Path to the ``discorope.gesture`` X360 surrogate.
        output_path:     Destination path for the compiled hybrid file.

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
        xy_constraints = _decompile_jdnext(jdnext_src_path)

        # Load template as mutable bytearray
        template_data = bytearray(template_path.read_bytes())

        # Phase 2 + 3: Blind Z-axis and inject X/Y constraints
        hybrid_data = _load_and_hybridize(template_data, xy_constraints)

        # Write output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(hybrid_data)

        logger.info(
            "Compiled hybrid gesture: %s (%d X/Y constraints -> %s)",
            jdnext_src_path.name,
            len(xy_constraints),
            output_path.name,
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
