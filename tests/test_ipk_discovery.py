from __future__ import annotations

import struct
from pathlib import Path

import pytest

from jd2021_installer.core.exceptions import IPKExtractionError
from jd2021_installer.extractors.archive_ipk import (
    ArchiveIPKExtractor,
    extract_ipk,
    inspect_ipk,
    validate_ipk_magic,
)


def _pack_u32(value: int) -> bytes:
    return struct.pack(">I", value)


def _pack_u64(value: int) -> bytes:
    return struct.pack(">Q", value)


def _build_fake_ipk(path: Path, entries: list[tuple[str, str]]) -> None:
    header = b"".join(
        [
            b"\x50\xEC\x12\xBA",
            _pack_u32(1),
            _pack_u32(0),
            _pack_u32(0),
            _pack_u32(len(entries)),
            _pack_u32(0),
            _pack_u32(0),
            _pack_u32(0),
            _pack_u32(0),
            _pack_u32(0),
            _pack_u32(0),
            _pack_u32(len(entries)),
        ]
    )

    body = bytearray()
    for file_name, path_name in entries:
        file_bytes = file_name.encode("utf-8")
        path_bytes = path_name.encode("utf-8")
        body += _pack_u32(0)
        body += _pack_u32(0)
        body += _pack_u32(0)
        body += _pack_u64(0)
        body += _pack_u64(0)
        body += _pack_u32(len(file_bytes))
        body += file_bytes
        body += _pack_u32(len(path_bytes))
        body += path_bytes
        body += _pack_u32(0)
        body += _pack_u32(0)

    path.write_bytes(header + bytes(body))


def _build_fake_ipk_raw(path: Path, entries: list[tuple[bytes, bytes]]) -> None:
    header = b"".join(
        [
            b"\x50\xEC\x12\xBA",
            _pack_u32(1),
            _pack_u32(0),
            _pack_u32(0),
            _pack_u32(len(entries)),
            _pack_u32(0),
            _pack_u32(0),
            _pack_u32(0),
            _pack_u32(0),
            _pack_u32(0),
            _pack_u32(0),
            _pack_u32(len(entries)),
        ]
    )

    body = bytearray()
    for file_bytes, path_bytes in entries:
        body += _pack_u32(0)
        body += _pack_u32(0)
        body += _pack_u32(0)
        body += _pack_u64(0)
        body += _pack_u64(0)
        body += _pack_u32(len(file_bytes))
        body += file_bytes
        body += _pack_u32(len(path_bytes))
        body += path_bytes
        body += _pack_u32(0)
        body += _pack_u32(0)

    path.write_bytes(header + bytes(body))


def test_inspect_ipk_detects_maps_from_swapped_path_fields(tmp_path: Path) -> None:
    ipk_path = tmp_path / "bundle_swapped.ipk"
    _build_fake_ipk(
        ipk_path,
        [
            ("world/maps/mapa/audio", "mapa_musictrack.tpl.ckd"),
            ("world/maps/mapb/audio", "mapb_musictrack.tpl.ckd"),
        ],
    )

    discovered = inspect_ipk(ipk_path)
    assert discovered == ["mapa", "mapb"]


def test_inspect_ipk_detects_legacy_world_jd_layout(tmp_path: Path) -> None:
    ipk_path = tmp_path / "legacy_bundle.ipk"
    _build_fake_ipk(
        ipk_path,
        [
            ("world/jd2015/songx/audio", "songx_musictrack.tpl.ckd"),
            ("world/jd2015/songy/audio", "songy_musictrack.tpl.ckd"),
        ],
    )

    discovered = inspect_ipk(ipk_path)
    assert discovered == ["songx", "songy"]


def test_validate_ipk_magic_rejects_invalid_archive(tmp_path: Path) -> None:
    invalid_ipk = tmp_path / "invalid.ipk"
    invalid_ipk.write_bytes(b"BAD!" + b"\x00" * 16)

    with pytest.raises(IPKExtractionError, match="bad magic bytes"):
        validate_ipk_magic(invalid_ipk)


def test_validate_ipk_magic_accepts_valid_archive_header(tmp_path: Path) -> None:
    valid_ipk = tmp_path / "valid.ipk"
    valid_ipk.write_bytes(b"\x50\xEC\x12\xBA" + b"\x00" * 16)

    validate_ipk_magic(valid_ipk)


def test_extract_ipk_raises_on_unreadable_entry(tmp_path: Path) -> None:
    unreadable_ipk = tmp_path / "unreadable.ipk"
    _build_fake_ipk_raw(unreadable_ipk, [(b"\xff", b"a")])

    with pytest.raises(IPKExtractionError, match="Failed to extract IPK"):
        extract_ipk(unreadable_ipk, tmp_path / "out")


def test_extract_ipk_allows_no_materialized_files_for_parity(tmp_path: Path) -> None:
    traversal_only_ipk = tmp_path / "traversal_only.ipk"
    _build_fake_ipk_raw(traversal_only_ipk, [(b"safe.bin", b"/blocked")])

    out_dir = tmp_path / "out"
    result, codenames = extract_ipk(traversal_only_ipk, out_dir)

    assert result == out_dir
    assert out_dir.exists()
    assert list(out_dir.rglob("*")) == []


def test_archive_ipk_extractor_prefers_requested_codename(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import jd2021_installer.extractors.archive_ipk as archive_ipk

    ipk_path = tmp_path / "bundle_pc.ipk"
    ipk_path.write_bytes(b"\x50\xEC\x12\xBA")

    monkeypatch.setattr(archive_ipk, "extract_ipk", lambda *_args, **_kwargs: (tmp_path / "out", ["MapA", "MapB"]))
    monkeypatch.setattr(archive_ipk, "_detect_maps_in_dir", lambda _dir: ["MapA", "MapB"])
    monkeypatch.setattr(archive_ipk, "inspect_ipk", lambda _target: ["MapA", "MapB"])

    extractor = ArchiveIPKExtractor(ipk_path, desired_codename="MapB")
    extractor.extract(tmp_path / "out")

    assert extractor.get_codename() == "MapB"


def test_find_bundle_ipks_prioritizes_unnumbered_bundle(tmp_path: Path) -> None:
    from jd2021_installer.extractors.archive_ipk import find_bundle_ipks

    folder = tmp_path / "content"
    folder.mkdir()

    bundle_0 = folder / "Bundle_0_WIIU.ipk"
    bundle_0.touch()
    bundle_1 = folder / "Bundle_1_WIIU.ipk"
    bundle_1.touch()
    bundle_2 = folder / "Bundle_2_WIIU.ipk"
    bundle_2.touch()
    bundle_main = folder / "Bundle_WIIU.ipk"
    bundle_main.touch()
    bundlelogic = folder / "BundleLogic_WIIU.ipk"
    bundlelogic.touch()

    # Case 1: Exclude Bundle_0_WIIU.ipk.
    # It should correctly pick Bundle_WIIU.ipk as bundle_ipk, and BundleLogic_WIIU.ipk as bundlelogic_ipk.
    b_guess, bl_guess = find_bundle_ipks(folder, exclude=bundle_0)
    assert b_guess == bundle_main
    assert bl_guess == bundlelogic

    # Case 2: Exclude Bundle_WIIU.ipk itself.
    # It should fall back to a chunk bundle (since the main bundle is excluded), e.g. Bundle_0_WIIU.ipk.
    b_guess, bl_guess = find_bundle_ipks(folder, exclude=bundle_main)
    assert b_guess == bundle_0
    assert bl_guess == bundlelogic
