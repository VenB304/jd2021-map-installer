"""CKD texture decoder — binary CKD → DDS → TGA/PNG.

Supports three platforms:
- **PC**: CKD strip 44-byte header → DDS → Pillow → TGA/PNG
- **NX (Switch)**: CKD strip header → XTX → deswizzle → DDS → TGA/PNG
- **X360**: CKD strip header → byte-swap + untile → DDS → TGA/PNG

Ported from V1's ``ckd_decode.py``.
"""

from __future__ import annotations

import logging
import os
import shutil
import struct
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from PIL import Image
except ImportError:
    Image = None

logger = logging.getLogger("jd2021.installers.texture_decoder")

CKD_HEADER_SIZE = 44
CKD_MAGIC = b'\x00\x00\x00\x09'
TEX_MAGIC = b'TEX'
NVFD_MAGIC = b'\x44\x46\x76\x4E'  # "DFvN" (NvFD little-endian)

# Xbox 360 GPU texture format codes
_X360_FMT_DXT1 = 0x52
_X360_FMT_DXT3 = 0x53
_X360_FMT_DXT5 = 0x54
_X360_GPU_DESC_SIZE = 52


def _save_picto_on_canvas(
    img,
    output_path: Path,
    canvas_size: Optional[int | Tuple[int, int]],
) -> None:
    """Save a pictogram either directly or centered on a transparent canvas.

    When ``canvas_size`` is provided, the image is never upscaled; if the
    source exceeds the canvas, it is proportionally downscaled to fit.
    The final image is placed bottom-center on the canvas.
    """
    if canvas_size is None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(output_path)
        return

    if isinstance(canvas_size, tuple):
        canvas_width, canvas_height = int(canvas_size[0]), int(canvas_size[1])
    else:
        canvas_width = int(canvas_size)
        canvas_height = int(canvas_size)

    if canvas_width <= 0 or canvas_height <= 0:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(output_path)
        return

    src = img.convert("RGBA")
    width, height = src.size
    if width <= 0 or height <= 0:
        return

    # Preserve JDNext-native width exactly as provided.
    # Any picto already authored at width 512 is kept untouched, regardless
    # of height. Only non-512-width pictos are fit onto the configured canvas.
    if width == 512:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        src.save(output_path)
        return

    scale = min(1.0, min(float(canvas_width) / float(width), float(canvas_height) / float(height)))
    fit_width = max(1, int(round(width * scale)))
    fit_height = max(1, int(round(height * scale)))
    if (fit_width, fit_height) != (width, height):
        src = src.resize((fit_width, fit_height), Image.Resampling.LANCZOS)

    canvas = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 0))
    offset_x = max(0, (canvas_width - src.size[0]) // 2)
    offset_y = max(0, canvas_height - src.size[1])
    canvas.paste(src, (offset_x, offset_y), src)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path)


# ---------------------------------------------------------------------------
# Xbox 360 untiling (ported from Xenia)
# ---------------------------------------------------------------------------

def _x360_tiled_combine(outer_inner_bytes, bank, pipe, y_lsb):
    result = (y_lsb << 4) | (pipe << 6) | (bank << 11)
    result |= (outer_inner_bytes & 0b1111)
    result |= (((outer_inner_bytes >> 4) & 0b1) << 5)
    result |= (((outer_inner_bytes >> 5) & 0b111) << 8)
    result |= ((outer_inner_bytes >> 8) << 12)
    return result


def _x360_tiled_2d(x, y, pitch_aligned, bytes_per_block_log2):
    outer_blocks = ((y >> 5) * (pitch_aligned >> 5) + (x >> 5)) << 6
    inner_blocks = (((y >> 1) & 0b111) << 3) | (x & 0b111)
    outer_inner_bytes = (outer_blocks | inner_blocks) << bytes_per_block_log2
    bank = (y >> 4) & 0b1
    pipe = ((x >> 3) & 0b11) ^ (((y >> 3) & 0b1) << 1)
    return _x360_tiled_combine(outer_inner_bytes, bank, pipe, y & 1)


def _x360_untile_dxt(data, pixel_width, pixel_height, block_bytes):
    bw = max(1, (pixel_width + 3) // 4)
    bh = max(1, (pixel_height + 3) // 4)
    bpp_log2 = {8: 3, 16: 4}[block_bytes]
    pitch_aligned = (bw + 31) & ~31
    output = bytearray(bw * bh * block_bytes)
    for by in range(bh):
        for bx in range(bw):
            src_offset = _x360_tiled_2d(bx, by, pitch_aligned, bpp_log2)
            dst_offset = (by * bw + bx) * block_bytes
            if 0 <= src_offset and src_offset + block_bytes <= len(data):
                output[dst_offset:dst_offset + block_bytes] = (
                    data[src_offset:src_offset + block_bytes])
    return bytes(output)


def _x360_byte_swap_16(data):
    out = bytearray(len(data))
    for i in range(0, len(data) - 1, 2):
        out[i] = data[i + 1]
        out[i + 1] = data[i]
    return bytes(out)


def _x360_build_dds(pixel_data, width, height, fourcc, block_bytes):
    bw = max(1, (width + 3) // 4)
    linear_size = bw * block_bytes

    dds = b'DDS '
    dds += struct.pack('<I', 124)
    dds += struct.pack('<I', 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000)
    dds += struct.pack('<I', height)
    dds += struct.pack('<I', width)
    dds += struct.pack('<I', linear_size)
    dds += struct.pack('<I', 0)
    dds += struct.pack('<I', 1)
    dds += b'\x00' * (4 * 11)

    dds += struct.pack('<I', 32)
    dds += struct.pack('<I', 0x4)
    dds += fourcc
    dds += b'\x00' * 20

    dds += struct.pack('<I', 0x1000)
    dds += b'\x00' * 16

    dds += pixel_data
    return dds


# ---------------------------------------------------------------------------
# Format detection and decoding
# ---------------------------------------------------------------------------

def strip_ckd_header(ckd_path: Path) -> Tuple[bytes, str]:
    """Strip the 44-byte UbiArt CKD header and detect the payload format.

    Returns:
        (payload_bytes, format_str) where format_str is 'xtx', 'dds', or 'x360'.

    Raises:
        ValueError: If the file is not a valid texture CKD.
    """
    with open(ckd_path, 'rb') as f:
        header = f.read(CKD_HEADER_SIZE)
        payload = f.read()

    if header[:4] != CKD_MAGIC:
        raise ValueError(f"Not a CKD file: missing magic bytes (got {header[:4].hex()})")
    if header[4:7] != TEX_MAGIC:
        raise ValueError(f"Not a texture CKD: missing TEX marker (got {header[4:7]})")

    if payload[:4] == NVFD_MAGIC:
        return payload, 'xtx'
    if payload[:4] == b'DDS ':
        return payload, 'dds'

    if len(payload) > _X360_GPU_DESC_SIZE:
        fmt_code = struct.unpack_from('>I', payload, 32)[0]
        if fmt_code in (_X360_FMT_DXT1, _X360_FMT_DXT3, _X360_FMT_DXT5):
            return payload, 'x360'

    raise ValueError(f"Unknown texture format: {payload[:4].hex()}")


def x360_to_dds(payload: bytes) -> bytes:
    """Decode Xbox 360 CKD payload: GPU descriptor + tiled DXT data → DDS."""
    if len(payload) <= _X360_GPU_DESC_SIZE:
        raise ValueError("X360 payload too small")

    fmt_code = struct.unpack_from('>I', payload, 32)[0]
    size_word = struct.unpack_from('>I', payload, 36)[0]
    width = (size_word & 0x1FFF) + 1
    height = ((size_word >> 13) & 0x1FFF) + 1

    fmt_map = {
        _X360_FMT_DXT1: (b'DXT1', 8),
        _X360_FMT_DXT3: (b'DXT3', 16),
        _X360_FMT_DXT5: (b'DXT5', 16),
    }
    if fmt_code not in fmt_map:
        raise ValueError(f"Unsupported X360 format: 0x{fmt_code:02X}")

    fourcc, block_bytes = fmt_map[fmt_code]
    dxt_data = payload[_X360_GPU_DESC_SIZE:]
    swapped = _x360_byte_swap_16(dxt_data)
    untiled = _x360_untile_dxt(swapped, width, height, block_bytes)
    return _x360_build_dds(untiled, width, height, fourcc, block_bytes)


def dds_to_image(
    dds_data: bytes,
    output_path: Path,
    canvas_size: Optional[int | Tuple[int, int]] = None,
) -> bool:
    """Convert DDS data to TGA or PNG using Pillow."""
    try:
        from PIL import Image
    except ImportError:
        logger.debug("Pillow not installed; saving raw DDS instead")
        dds_path = output_path.with_suffix('.dds')
        dds_path.write_bytes(dds_data)
        return True

    temp_dds = output_path.with_suffix('.tmp.dds')
    try:
        temp_dds.write_bytes(dds_data)
        with Image.open(str(temp_dds)) as img:
            _save_picto_on_canvas(img, output_path, canvas_size)
            logger.debug("Saved texture: %s (%dx%d)", output_path.name, img.size[0], img.size[1])
        return True
    except Exception as e:
        # Fallback: save as DDS
        dds_fallback = output_path.with_suffix('.dds')
        if temp_dds.exists():
            shutil.copy2(str(temp_dds), str(dds_fallback))
        logger.debug("Pillow can't decode this DDS format (%s); saved raw DDS", e)
        return True
    finally:
        if temp_dds.exists():
            try:
                temp_dds.unlink()
            except Exception as e:
                logger.warning("Failed to clean up temporary DDS file %s: %s", temp_dds, e)


def decode_ckd_texture(
    ckd_path: Path,
    output_path: Optional[Path] = None,
    canvas_size: Optional[int | Tuple[int, int]] = None,
) -> bool:
    """Full pipeline: CKD → detect format → DDS → TGA/PNG.

    Args:
        ckd_path:    Path to the binary texture CKD.
        output_path: Path for output image. Defaults to same name with .tga.

    Returns:
        True if decoding succeeded.
    """
    if output_path is None:
        output_path = ckd_path.with_suffix('.tga')

    try:
        raw_data, fmt = strip_ckd_header(ckd_path)
    except ValueError as e:
        logger.debug("Skipping non-texture CKD %s: %s", ckd_path.name, e)
        return False

    if fmt == 'dds':
        return dds_to_image(raw_data, output_path, canvas_size=canvas_size)

    if fmt == 'x360':
        try:
            dds_data = x360_to_dds(raw_data)
        except Exception as e:
            logger.error("X360 decode failed for %s: %s", ckd_path.name, e)
            return False
        return dds_to_image(dds_data, output_path, canvas_size=canvas_size)

    # NX/Switch XTX format
    try:
        try:
            from jd2021_installer.extractors.xtx_extractor import xtx_extract
        except ImportError:
            # Backward-compatible fallback for legacy external layout.
            from xtx_extractor import xtx_extract
        nv = xtx_extract.readNv(raw_data)
        if nv.numImages == 0:
            raise ValueError("No images in XTX data")
        hdr, result = xtx_extract.get_deswizzled_data(0, nv)
        if hdr == b'' or result == []:
            raise ValueError("Failed to deswizzle XTX")
        dds_data = hdr
        for mip in result:
            dds_data += mip
    except ImportError:
        logger.debug("xtx_extractor not available; saving raw XTX for %s", ckd_path.name)
        xtx_path = output_path.with_suffix('.xtx')
        xtx_path.write_bytes(raw_data)
        return False
    except Exception as e:
        logger.error("XTX decode failed for %s: %s", ckd_path.name, e)
        xtx_path = output_path.with_suffix('.xtx')
        xtx_path.write_bytes(raw_data)
        return False

    return dds_to_image(dds_data, output_path, canvas_size=canvas_size)


# ---------------------------------------------------------------------------
# Batch decoders
# ---------------------------------------------------------------------------

def decode_pictograms(
    picto_dir: Path,
    output_dir: Path,
    canvas_size: Optional[int | Tuple[int, int]] = None,
) -> int:
    """Decode all pictogram CKD textures in a directory.

    Args:
        picto_dir:  Directory containing ``*picto*.ckd`` files.
        output_dir: Directory to write decoded PNG/TGA files.

    Returns:
        Number of textures successfully decoded.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    success = 0

    for ckd in picto_dir.rglob("*.ckd"):
        # Determine output name: ensure .png extension for pictograms
        out_name = ckd.stem  # e.g. "mapname_picto_001.png"
        if not out_name.lower().endswith('.png'):
            if out_name.lower().endswith('.tga'):
                out_name = out_name[:-4] + '.png'
            else:
                out_name += '.png'
        out_path = output_dir / out_name

        if decode_ckd_texture(ckd, out_path, canvas_size=canvas_size):
            success += 1

    # Manual/IPK maps can ship already-decoded pictos (png/tga/jpg).
    # Keep them usable by copying/converting into timeline/pictos as PNG.
    for src in picto_dir.rglob("*"):
        if not src.is_file():
            continue
        ext = src.suffix.lower()
        if ext not in (".png", ".tga", ".jpg", ".jpeg"):
            continue

        out_name = src.stem + ".png"
        out_path = output_dir / out_name

        # Avoid self-copy when input and output directories are the same.
        if ext == ".png" and src.resolve() == out_path.resolve():
            success += 1
            continue

        try:
            if ext == ".png":
                if Image is None:
                    logger.debug("Pillow missing; skipping non-PNG picto %s", src.name)
                    continue
                with Image.open(src) as img:
                    _save_picto_on_canvas(img, out_path, canvas_size)
                success += 1
                continue

            # Convert TGA/JPG to PNG when Pillow is available.
            if Image is None:
                logger.debug("Pillow missing; skipping non-PNG picto %s", src.name)
                continue

            with Image.open(src) as img:
                _save_picto_on_canvas(img, out_path, canvas_size)
            success += 1
        except Exception as e:
            logger.debug("Failed to process loose picto %s: %s", src.name, e)

    logger.debug("Decoded %d pictogram textures from %s", success, picto_dir)
    return success


def decode_menuart_textures(menuart_dir: Path, output_dir: Path) -> int:
    """Decode all MenuArt CKD textures in a directory.

    Args:
        menuart_dir: Directory containing MenuArt ``*.ckd`` files.
        output_dir:  Directory to write decoded TGA files.

    Returns:
        Number of textures successfully decoded.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    success = 0

    non_texture_companion_suffixes = {
        ".act",
        ".isc",
        ".tpl",
        ".ilu",
        ".tape",
        ".dtape",
        ".ktape",
        ".stape",
    }

    for ckd in menuart_dir.rglob("*.ckd"):
        # MenuArt folders can contain actor/scene companion CKDs that are not
        # texture payloads. Skip them up-front to avoid noisy decode attempts.
        inner_suffix = Path(ckd.stem).suffix.lower()
        if inner_suffix in non_texture_companion_suffixes:
            continue

        out_name = ckd.stem
        if not out_name.lower().endswith(('.tga', '.png')):
            out_name += '.tga'
        out_path = output_dir / out_name

        if decode_ckd_texture(ckd, out_path):
            success += 1

    # JDNext sources can already contain decoded PNG/TGA/JPG menuart files.
    for src in menuart_dir.rglob("*"):
        if not src.is_file():
            continue
        ext = src.suffix.lower()
        if ext not in (".png", ".tga", ".jpg", ".jpeg"):
            continue

        out_ext = ".png" if ext in (".jpg", ".jpeg") else ext
        out_path = output_dir / f"{src.stem}{out_ext}"

        try:
            if src.resolve() == out_path.resolve():
                success += 1
                continue

            if ext in (".png", ".tga"):
                shutil.copy2(src, out_path)
                success += 1
                continue

            # Convert JPG/JPEG to PNG for predictable downstream handling.
            try:
                from PIL import Image
            except ImportError:
                logger.debug("Pillow missing; skipping loose MenuArt %s", src.name)
                continue

            with Image.open(src) as img:
                img.save(out_path)
            success += 1
        except Exception as e:
            logger.debug("Failed to process loose MenuArt %s: %s", src.name, e)

    logger.debug("Decoded %d MenuArt textures from %s", success, menuart_dir)
    return success
