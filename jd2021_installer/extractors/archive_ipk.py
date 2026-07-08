"""IPK archive extractor.

Extracts .ipk files (Ubisoft's proprietary archive format used on
console mods) into a temporary directory for normalizer processing.

Refactored from the original ``ipk_unpack.py`` with path-traversal
protection and proper error handling.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import struct
import zlib
import lzma
from pathlib import Path
from typing import Iterator, Optional

from jd2021_installer.core.exceptions import IPKExtractionError
from jd2021_installer.extractors.base import BaseExtractor

logger = logging.getLogger("jd2021.extractors.archive_ipk")

# Big-endian for IPK format
_ENDIAN = ">"
_STRUCT_SIGNS = {1: "c", 2: "H", 4: "I", 8: "Q"}

_IPK_MAGIC = b"\x50\xEC\x12\xBA"

# Guard against corrupted headers that claim absurd file counts.
_MAX_IPK_FILE_COUNT = 100_000

# Streaming thresholds for decompression.
# Payloads below the threshold are fully buffered (faster for the many tiny
# CKD / metadata files a typical IPK contains).  Larger payloads — audio,
# textures, video — use streaming zlib.decompressobj to avoid holding both
# the compressed and decompressed representations in RAM simultaneously.
_STREAMING_CHUNK = 256 * 1024       # 256 KB read chunk
_STREAMING_THRESHOLD = 4 * 1024 * 1024  # Switch to streaming above 4 MB


def validate_ipk_magic(target_file: str | Path) -> None:
    """Validate IPK magic bytes before expensive extraction work.

    Raises:
        IPKExtractionError: If the file is missing or does not start with IPK magic.
    """
    target_path = Path(target_file)
    if not target_path.exists():
        raise IPKExtractionError(f"IPK file not found: {target_path}")

    with open(target_path, "rb") as f:
        magic = f.read(4)
    if magic != _IPK_MAGIC:
        raise IPKExtractionError("Not a valid IPK file (bad magic bytes)")


def _unpack(data: bytes) -> int:
    """Unpack a big-endian integer of 1/2/4/8 bytes."""
    return struct.unpack(_ENDIAN + _STRUCT_SIGNS[len(data)], data)[0]


def _read_header_fields(f, template: dict) -> dict:
    """Read header fields from file into dict with 'value' keys."""
    result = {k: dict(v) for k, v in template.items()}
    for v in result.values():
        v["value"] = f.read(v["size"])
    return result


_IPK_HEADER_TEMPLATE = {
    "magic": {"size": 4},
    "version": {"size": 4},
    "platformsupported": {"size": 4},
    "base_offset": {"size": 4},
    "num_files": {"size": 4},
    "compressed": {"size": 4},
    "binaryscene": {"size": 4},
    "binarylogic": {"size": 4},
    "datasignature": {"size": 4},
    "enginesignature": {"size": 4},
    "engineversion": {"size": 4},
    "num_files2": {"size": 4},
}


def _get_file_header() -> dict:
    return {
        "numOffset": {"size": 4},
        "size": {"size": 4},
        "compressed_size": {"size": 4},
        "time_stamp": {"size": 8},
        "offset": {"size": 8},
        "name_size": {"size": 4},
        "file_name": {"size": 0},   # resolved at read-time from name_size
        "path_size": {"size": 4},
        "path_name": {"size": 4},   # resolved at read-time from path_size
        "checksum": {"size": 4},
        "flag": {"size": 4},
    }


def _iter_file_headers(f, num_files: int) -> Iterator[dict]:
    """Lazily yield file-entry header dicts from an open IPK stream.

    The caller must position ``f`` immediately after the global IPK header
    before invoking this generator.  The iterator is lazy so callers that
    only need the first ``N`` entries can break early without reading the
    rest of the file.

    This generator is shared between :func:`extract_ipk` and
    :func:`inspect_ipk` to eliminate the previously duplicated header-parsing
    loop.
    """
    for _ in range(num_files):
        fheader = _get_file_header()
        for v in fheader:
            size = fheader[v]["size"]
            if v == "path_name":
                size = _unpack(fheader["path_size"]["value"])
            if v == "file_name":
                size = _unpack(fheader["name_size"]["value"])
            fheader[v]["value"] = f.read(size)
        yield fheader


def _sniff_compression(probe: bytes) -> str:
    """Identify the compression codec from the leading bytes of a payload.

    Returns ``'zlib'``, ``'lzma'``, or ``'raw'``.
    """
    if len(probe) >= 2 and probe[0] == 0x78 and probe[1] in (0x9C, 0x01, 0xDA, 0x5E):
        return "zlib"
    if len(probe) >= 6 and probe[:6] == b"\xfd7zXZ\x00":
        return "lzma"
    if len(probe) >= 2 and probe[:2] == b"]\x00":
        return "lzma"
    return "raw"


def _decompress_to_file(f_in, f_out, data_size: int) -> None:
    """Read ``data_size`` bytes from ``f_in``, decompress, and write to ``f_out``.

    Small payloads (< 4 MB) are fully buffered before decompression —
    this is marginally faster for the many tiny CKD / metadata files a
    typical IPK contains.

    Large payloads use :class:`zlib.decompressobj` in streaming mode so
    only one 256 KB chunk of compressed data is held in RAM at a time,
    avoiding the peak where both the full compressed *and* decompressed
    representations would otherwise coexist.

    The compression codec is auto-detected from the leading magic bytes;
    unrecognised payloads are written as-is (raw copy).
    """
    if data_size < _STREAMING_THRESHOLD:
        # Small payload: buffer fully, try zlib → lzma → raw.
        raw = f_in.read(data_size)
        try:
            f_out.write(zlib.decompress(raw))
            return
        except zlib.error:
            pass
        try:
            f_out.write(lzma.decompress(raw))
            return
        except lzma.LZMAError:
            pass
        f_out.write(raw)
        return

    # Large payload: probe codec from first bytes, then stream-decompress.
    probe = f_in.read(min(16, data_size))
    remaining = data_size - len(probe)
    codec = _sniff_compression(probe)

    if codec == "zlib":
        try:
            dobj = zlib.decompressobj()
            f_out.write(dobj.decompress(probe))
            while remaining > 0:
                n = min(_STREAMING_CHUNK, remaining)
                f_out.write(dobj.decompress(f_in.read(n)))
                remaining -= n
            f_out.write(dobj.flush())
            return
        except zlib.error:
            # Skip any unread compressed bytes to leave the stream positioned
            # correctly for the next entry, then bail out.
            logger.debug(
                "Streaming zlib decompression failed for %d-byte payload; "
                "output may be partial — skipping remaining %d bytes.",
                data_size,
                remaining,
            )
            if remaining > 0:
                f_in.read(remaining)
            return

    # LZMA or raw: collect remaining bytes, then decompress or copy.
    tail = f_in.read(remaining)
    full = probe + tail

    if codec == "lzma":
        try:
            f_out.write(lzma.decompress(full))
            return
        except lzma.LZMAError:
            pass

    # Raw write — chunk through the already-collected buffer.
    offset = 0
    while offset < len(full):
        end = min(offset + _STREAMING_CHUNK, len(full))
        f_out.write(full[offset:end])
        offset = end


_MAX_EXTRACT_WORKERS = min(4, os.cpu_count() or 4)


def _extract_entry(
    target_file: Path,
    seek_pos: int,
    disk_size: int,
    out_dir: Path,
    out_name: str,
) -> None:
    """Decompress and write one IPK file entry using an independent file handle.

    Safe to call from multiple threads simultaneously — each invocation opens
    its own read handle and writes to a distinct output path.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(target_file, "rb") as f_in:
        f_in.seek(seek_pos)
        with open(out_dir / out_name, "wb") as f_out:
            _decompress_to_file(f_in, f_out, disk_size)


def extract_ipk(
    target_file: str | Path,
    output_dir: str | Path | None = None,
) -> tuple[Path, list[str]]:
    """Extract an IPK archive to the given output directory.

    Args:
        target_file: Path to the .ipk file.
        output_dir:  Directory to extract into. If omitted, defaults to
                 ``target_file.stem`` (V1-compatible behavior).

    Returns:
        ``(output_path, codenames)`` — the extraction directory and the
        sorted list of map codenames discovered in the file-entry headers.
        Callers that previously discarded the return value are unaffected;
        callers such as :meth:`ArchiveIPKExtractor.extract` can consume the
        codenames directly to skip a redundant :func:`inspect_ipk` pass.

    Raises:
        IPKExtractionError: If extraction fails.
    """
    target_file = Path(target_file)
    output_path = Path(output_dir) if output_dir is not None else Path(target_file.stem)

    # validate_ipk_magic raises a typed, descriptive error — no need to
    # recheck the magic bytes again inside the open() block below.
    validate_ipk_magic(target_file)

    try:
        output_path.mkdir(exist_ok=True)

        with open(target_file, "rb") as f:
            ipk_header = _read_header_fields(f, _IPK_HEADER_TEMPLATE)

            num_files = _unpack(ipk_header["num_files"]["value"])
            if num_files > _MAX_IPK_FILE_COUNT:
                raise IPKExtractionError(
                    f"Suspicious file count in IPK header: {num_files:,}. "
                    "The archive may be corrupted or non-standard."
                )
            logger.debug("IPK: Found %d files...", num_files)

            file_chunks = list(_iter_file_headers(f, num_files))

            base_offset = _unpack(ipk_header["base_offset"]["value"])
            codenames_found: set[str] = set()
            extracted_files = 0

            # Pass 1: resolve paths and collect valid entries (no decompression yet).
            # Codename discovery and traversal checks happen here while we still
            # hold the file handle; no seeking is needed for this pass.
            entries: list[tuple[Path, str, int, int]] = []  # (out_dir, name, seek_pos, disk_size)
            for chunk in file_chunks:
                path_ori = chunk["path_name"]["value"].decode().lower().replace('\\', '/')
                if "world/maps/" in path_ori:
                    after_maps = path_ori.split("world/maps/")[1]
                    parts = after_maps.split("/")
                    if parts and parts[0]:
                        codenames_found.add(parts[0])

                offset = _unpack(chunk["offset"]["value"])
                uncompressed_size = _unpack(chunk["size"]["value"])
                compressed_size = _unpack(chunk["compressed_size"]["value"])
                disk_size = compressed_size if compressed_size > 0 else uncompressed_size

                path_ori_raw = chunk["path_name"]["value"].decode()
                file_ori_raw = chunk["file_name"]["value"].decode()

                # Check if fields are swapped (if file contains slashes but path doesn't)
                if ("/" in file_ori_raw or "\\" in file_ori_raw) and not ("/" in path_ori_raw or "\\" in path_ori_raw):
                    file_path = output_path / file_ori_raw
                    file_name = path_ori_raw
                else:
                    file_path = output_path / path_ori_raw
                    file_name = file_ori_raw

                # Path traversal protection
                resolved = os.path.normpath(os.path.join(str(file_path), file_name))
                if not resolved.startswith(str(output_path)):
                    logger.debug("Skipping path-traversal entry: %s", resolved)
                    continue

                if os.path.abspath(resolved) == os.path.abspath(target_file):
                    logger.debug("Skipping self-referential entry that would overwrite the source IPK: %s", resolved)
                    continue

                entries.append((file_path, file_name, offset + base_offset, disk_size))

        logger.debug(
            "IPK: Extracting %d/%d valid files (workers=%d)...",
            len(entries), num_files, _MAX_EXTRACT_WORKERS,
        )

        # Pass 2: decompress all valid entries in parallel.  Each worker opens
        # its own read handle so there is no shared file-position state.
        with concurrent.futures.ThreadPoolExecutor(max_workers=_MAX_EXTRACT_WORKERS) as pool:
            futs = {
                pool.submit(_extract_entry, target_file, seek_pos, disk_size, out_dir, out_name): out_name
                for out_dir, out_name, seek_pos, disk_size in entries
            }
            for fut in concurrent.futures.as_completed(futs):
                fut.result()  # re-raise any per-file exception immediately
                extracted_files += 1

        logger.info(
            "IPK: Extracted %d/%d files to %s",
            extracted_files,
            num_files,
            output_path,
        )
        if extracted_files == 0 and num_files > 0:
            logger.warning(
                "IPK extraction produced no materialized files for %s; continuing for V1 parity.",
                target_file,
            )
        return output_path, sorted(codenames_found)

    except IPKExtractionError:
        raise
    except Exception as exc:
        raise IPKExtractionError(f"Failed to extract IPK ({type(exc).__name__}): {exc}") from exc


def inspect_ipk(target_file: str | Path) -> list[str]:
    """Fast scan of the IPK to discover top-level map directories.

    Reads only file-entry headers without decompressing any data.

    .. note::
        When you have already called :func:`extract_ipk`, prefer consuming
        the codename list it returns rather than calling this function again —
        that avoids a redundant full-file header walk.
    """
    target_file = Path(target_file)
    if not target_file.exists():
        return []

    try:
        with open(target_file, "rb") as f:
            ipk_header = _read_header_fields(f, _IPK_HEADER_TEMPLATE)
            if ipk_header["magic"]["value"] != _IPK_MAGIC:
                return []

            num_files = _unpack(ipk_header["num_files"]["value"])
            if num_files > _MAX_IPK_FILE_COUNT:
                logger.debug(
                    "inspect_ipk: Suspicious file count %d in %s; skipping inspection.",
                    num_files,
                    target_file.name,
                )
                return []

            root_dirs: set[str] = set()

            # V1 Parity: support both standard (world/maps/) and legacy (world/jd20XX/) structures.
            for chunk in _iter_file_headers(f, num_files):
                raw_path = chunk["path_name"]["value"].decode(errors="ignore").replace('\\', '/')
                raw_file = chunk["file_name"]["value"].decode(errors="ignore").replace('\\', '/')

                candidates = []
                if "/" in raw_path:
                    candidates.append(raw_path)
                if "/" in raw_file:
                    candidates.append(raw_file)
                if not candidates:
                    candidates = [raw_path, raw_file]

                for candidate in candidates:
                    path_ori = candidate.lower()
                    if "world/maps/" in path_ori:
                        after_maps = path_ori.split("world/maps/")[1]
                        parts = [p for p in after_maps.split("/") if p]
                        if parts:
                            root_dirs.add(parts[0])
                        continue

                    if "world/jd" in path_ori:
                        parts = [p for p in path_ori.split("/") if p]
                        try:
                            idx = parts.index("world")
                            if idx + 2 < len(parts) and parts[idx + 1].startswith("jd"):
                                root_dirs.add(parts[idx + 2])
                        except (ValueError, IndexError):
                            pass

            # V1 Parity: filter out engine-specific internal folders.
            ignore_list = {
                "cache", "common", "etc", "enginedata",
                "audio", "videoscoach", "localization",
            }
            return sorted(
                {d for d in root_dirs if d and not d.startswith(".") and d.lower() not in ignore_list}
            )

    except Exception as exc:
        logger.debug("Fast inspect failed for IPK %s: %s", target_file, exc)
        return []


def _detect_maps_in_dir(directory: Path) -> list[str]:
    """Scan a directory for map codenames using the UbiArt structure.

    Supports both standard (world/maps/) and legacy (world/jd20XX/) layouts.
    """
    codenames: set[str] = set()

    # 1. Standard layout: world/maps/<codename>/
    maps_dirs = list(directory.rglob("world/maps"))
    for maps_dir in maps_dirs:
        if maps_dir.is_dir():
            for entry in maps_dir.iterdir():
                if entry.is_dir() and not entry.name.startswith('.'):
                    codenames.add(entry.name)

    # 2. Legacy layout: world/jd20XX/<codename>/
    for world_dir in directory.rglob("world"):
        if world_dir.is_dir():
            for jd_dir in world_dir.iterdir():
                if jd_dir.is_dir() and re.match(r"jd\d+", jd_dir.name, re.I):
                    for entry in jd_dir.iterdir():
                        if entry.is_dir() and not entry.name.startswith('.'):
                            codenames.add(entry.name)

    ignore_list = {"cache", "common", "etc", "enginedata", "audio", "videoscoach", "localization"}
    return sorted({c for c in codenames if c and c.lower() not in ignore_list})


def find_bundle_ipks(
    folder: Path,
    exclude: Optional[Path] = None,
) -> tuple[Optional[Path], Optional[Path]]:
    """Locate bundle and bundlelogic IPKs within a folder.

    Returns (bundle_ipk, bundlelogic_ipk); any entry can be None.
    """
    if not folder.exists() or not folder.is_dir():
        return None, None

    exclude_resolved = exclude.resolve() if exclude else None
    ipks = sorted(
        [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".ipk"],
        key=lambda p: p.name.lower(),
    )

    bundle_ipk: Optional[Path] = None
    bundlelogic_ipk: Optional[Path] = None
    for path in ipks:
        if exclude_resolved and path.resolve() == exclude_resolved:
            continue
        name = path.name.lower()
        if "bundlelogic" in name:
            if bundlelogic_ipk is None:
                bundlelogic_ipk = path
            continue
        if "bundle" in name:
            is_chunk = bool(re.search(r"bundle_\d+(_|\.ipk)", name))
            if bundle_ipk is None:
                bundle_ipk = path
            elif not is_chunk:
                existing_name = bundle_ipk.name.lower()
                existing_is_chunk = bool(re.search(r"bundle_\d+(_|\.ipk)", existing_name))
                if existing_is_chunk:
                    bundle_ipk = path

    return bundle_ipk, bundlelogic_ipk


class ArchiveIPKExtractor(BaseExtractor):
    """Extractor for IPK archive files."""

    def __init__(self, ipk_path: str | Path, desired_codename: str | None = None) -> None:
        self._ipk_path = Path(ipk_path)
        self._codename: Optional[str] = None
        self._desired_codename = desired_codename.strip() if desired_codename else None
        self.bundle_maps: list[str] = []

    def extract(self, output_dir: Path) -> Path:
        # extract_ipk now returns (path, header_codenames) — no second inspect_ipk
        # pass is needed, saving a full file-header re-read.
        result, header_codenames = extract_ipk(self._ipk_path, output_dir)

        # V1 Parity: also detect maps from the filesystem structure after extraction,
        # since some IPKs use path layouts not captured by the header scan alone.
        actual_maps = _detect_maps_in_dir(result)

        # Combine: header codenames + filesystem-detected codenames.
        discovered = sorted(set(actual_maps) | set(header_codenames))
        self.bundle_maps = discovered

        if len(discovered) == 1:
            self._codename = discovered[0]
            logger.info("Inferred codename from IPK: %s", self._codename)
        elif len(discovered) > 1:
            logger.info("Multiple maps discovered in IPK: %s", ", ".join(discovered))
            if self._desired_codename:
                target_matches = [m for m in discovered if m.lower() == self._desired_codename.lower()]
                if target_matches:
                    self._codename = target_matches[0]
                    logger.debug("Matched bundle codename from requested target: %s", self._codename)
                    return result

            # Try to match the codename from the IPK filename
            base = self._ipk_path.stem
            stem = re.sub(
                r"_(x360|durango|scarlett|nx|orbis|prospero|pc|ps3|wiiu)$",
                "",
                base,
                flags=re.IGNORECASE,
            )

            matches = [m for m in discovered if m.lower() == stem.lower()]
            if matches:
                self._codename = matches[0]
                logger.debug("Matched bundle codename from filename: %s", self._codename)
            else:
                self._codename = discovered[0]
                logger.debug("Auto-selected first candidate for bundle: %s", self._codename)
        else:
            # Fallback to filename inference if no maps found in structure
            base = self._ipk_path.stem
            stem = re.sub(
                r"_(x360|durango|scarlett|nx|orbis|prospero|pc|ps3|wiiu)$",
                "",
                base,
                flags=re.IGNORECASE,
            )
            self._codename = stem
            logger.debug(
                "No maps found in structure, using fallback from filename: %s",
                self._codename,
            )

        return result

    def get_codename(self) -> Optional[str]:
        return self._codename

    def get_ipk_path(self) -> Path:
        return self._ipk_path

    def get_source_dir(self) -> Path:
        """Return the folder that contains the selected .ipk file."""
        return self._ipk_path.parent
