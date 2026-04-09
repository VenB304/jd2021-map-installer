import struct
from pathlib import Path
from unittest.mock import patch

from jd2021_installer.core.models import BeatsTape
from jd2021_installer.installers.tape_converter import auto_convert_tapes, convert_tape_file
from jd2021_installer.parsers.binary_ckd import parse_binary_ckd


_ACTOR_TEMPLATE_CRC = 0x1B857BCE
_TAPE_CRC = 0x2AFED161
_BEAT_CLIP_CRC = 0x364811D4


def _pack_entry(
    entry_class: int,
    entry_id: int,
    track_id: int,
    is_active: int,
    start_time: int,
    duration: int,
    beat_type: int,
) -> bytes:
    return struct.pack(
        ">IIIIIIII",
        0,
        entry_class,
        entry_id,
        track_id,
        is_active,
        start_time,
        duration,
        beat_type,
    )


def _build_raw_btape(entries: list[bytes]) -> bytes:
    return b"\x00" * 12 + struct.pack(">II", 1, len(entries)) + b"".join(entries)


def _build_actor_wrapped_btape(entries: list[bytes]) -> bytes:
    actor_header = struct.pack(
        ">IIII",
        1,
        0,
        _ACTOR_TEMPLATE_CRC,
        0,
    )
    actor_header += b"\x00" * (7 * 4)
    actor_header += struct.pack(">I", 1)
    actor_header += struct.pack(">I", _TAPE_CRC)
    actor_header += struct.pack(">III", 0, 0, 0)

    tape_payload = struct.pack(">II", 1, len(entries)) + b"".join(entries)
    return actor_header + tape_payload


def test_parse_binary_ckd_raw_btape_returns_beats_tape():
    data = _build_raw_btape([
        _pack_entry(_BEAT_CLIP_CRC, 12, 0, 1, 1000, 250, 1),
        _pack_entry(0x11111111, 99, 0, 1, 2000, 400, 9),
    ])

    result = parse_binary_ckd(data, "Koi.btape.ckd")

    assert isinstance(result, BeatsTape)
    assert result.map_name == "Koi"
    assert len(result.clips) == 1
    assert result.clips[0].id == 12
    assert result.clips[0].start_time == 1000
    assert result.clips[0].duration == 250
    assert result.clips[0].beat_type == 1


def test_parse_binary_ckd_actor_wrapped_btape_returns_beats_tape():
    data = _build_actor_wrapped_btape([
        _pack_entry(_BEAT_CLIP_CRC, 7, 0, 1, 333, 111, 2),
    ])

    result = parse_binary_ckd(data, "nailships_btape.tpl.ckd")

    assert isinstance(result, BeatsTape)
    assert result.map_name == "nailships"
    assert len(result.clips) == 1
    assert result.clips[0].id == 7
    assert result.clips[0].beat_type == 2


def test_auto_convert_tapes_converts_btape(tmp_path: Path):
    source = tmp_path / "src" / "world" / "maps" / "Koi" / "timeline"
    source.mkdir(parents=True)
    btape_ckd = source / "Koi.btape.ckd"
    btape_ckd.write_bytes(
        _build_raw_btape([
            _pack_entry(_BEAT_CLIP_CRC, 1, 0, 1, 10, 20, 1),
        ])
    )

    target = tmp_path / "out"

    converted = auto_convert_tapes(tmp_path / "src", target, "Koi")

    output = target / "timeline" / "Koi.btape"
    assert converted == 1
    assert output.exists()
    text = output.read_text(encoding="utf-8")
    assert 'NAME = "BeatClip"' in text
    assert "MapName = \"Koi\"" in text


def test_convert_tape_file_accepts_dict_parse_results(tmp_path: Path):
    source = tmp_path / "legacy_autodance.tpl.ckd"
    source.write_bytes(b"\x00\x01\x02")
    output = tmp_path / "legacy_autodance.tpl"

    with patch(
        "jd2021_installer.installers.tape_converter.parse_binary_ckd",
        return_value={"type": "autodance", "map_name": "legacy_autodance"},
    ):
        ok = convert_tape_file(source, output)

    assert ok is True
    assert output.exists()
    text = output.read_text(encoding="utf-8")
    assert "KEY = \"type\"" in text
    assert "VAL = \"autodance\"" in text
