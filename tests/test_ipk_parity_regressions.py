from __future__ import annotations

from pathlib import Path

import pytest

from jd2021_installer.core.exceptions import DownloadError
from jd2021_installer.extractors.archive_ipk import ArchiveIPKExtractor
from jd2021_installer.extractors.manual_extractor import ManualExtractor
from jd2021_installer.ui.workers.pipeline_workers import (
    ExtractAndNormalizeWorker,
    _pick_ipk_audio,
    _validate_ipk_media_presence,
    reprocess_audio,
)


def test_manual_ipk_root_keeps_user_root(tmp_path: Path) -> None:
    root = tmp_path / "manual_ipk_root"
    (root / "world" / "maps" / "MapA").mkdir(parents=True)
    (root / "world" / "maps" / "MapB").mkdir(parents=True)
    (root / "MapB.ogg").write_bytes(b"a" * 1024)
    (root / "MapB_LOW.webm").write_bytes(b"v" * 1024)
    (root / "MapB_musictrack.tpl.ckd").write_bytes(b"m")

    extractor = ManualExtractor(codename="MapB", source_type="ipk", root_dir=str(root))
    extracted = extractor.extract(tmp_path / "output")

    assert extracted == root


def test_manual_ipk_root_supports_legacy_world_jd_layout(tmp_path: Path) -> None:
    root = tmp_path / "manual_ipk_root"
    (root / "world" / "jd2015" / "SongX").mkdir(parents=True)
    (root / "world" / "jd2015" / "SongY").mkdir(parents=True)
    (root / "SongY.ogg").write_bytes(b"a" * 1024)
    (root / "SongY_LOW.webm").write_bytes(b"v" * 1024)
    (root / "SongY_musictrack.tpl.ckd").write_bytes(b"m")

    extractor = ManualExtractor(codename="SongY", source_type="ipk", root_dir=str(root))
    extracted = extractor.extract(tmp_path / "output")

    assert extracted == root
    assert extractor.get_codename() == "SongY"


def test_manual_ipk_root_multimap_codename_mismatch_falls_back(tmp_path: Path) -> None:
    root = tmp_path / "manual_ipk_root"
    (root / "world" / "maps" / "MapA").mkdir(parents=True)
    (root / "world" / "maps" / "MapB").mkdir(parents=True)
    (root / "MapA.ogg").write_bytes(b"a" * 1024)
    (root / "MapA_LOW.webm").write_bytes(b"v" * 1024)
    (root / "MapA_musictrack.tpl.ckd").write_bytes(b"m")

    extractor = ManualExtractor(codename="WrongMap", source_type="ipk", root_dir=str(root))
    extracted = extractor.extract(tmp_path / "output")

    assert extracted == root
    assert extractor.get_codename() == "MapA"


def test_manual_ipk_root_missing_required_media_is_fatal(tmp_path: Path) -> None:
    root = tmp_path / "manual_ipk_root"
    (root / "world" / "maps" / "MapA").mkdir(parents=True)

    extractor = ManualExtractor(codename="MapA", source_type="ipk", root_dir=str(root))

    with pytest.raises(DownloadError, match=r"Musictrack CKD / \.trk is required"):
        extractor.extract(tmp_path / "output")


def test_manual_jdu_root_accepts_flexible_html_pair(tmp_path: Path) -> None:
    root = tmp_path / "manual_jdu_root"
    root.mkdir(parents=True)
    (root / "map_asset_export.html").write_text("<html></html>", encoding="utf-8")
    (root / "map_nohud_export.html").write_text("<html></html>", encoding="utf-8")
    (root / "MapA.ogg").write_bytes(b"a" * 1024)
    (root / "MapA_LOW.webm").write_bytes(b"v" * 1024)
    (root / "MapA_musictrack.tpl.ckd").write_bytes(b"m")

    extractor = ManualExtractor(codename="MapA", source_type="jdu", root_dir=str(root))
    extracted = extractor.extract(tmp_path / "output")

    assert extracted == root


def test_archive_worker_does_not_swallow_unexpected_extraction_errors(tmp_path: Path) -> None:
    ipk_file = tmp_path / "mapa.ipk"
    ipk_file.write_bytes(b"\x50\xEC\x12\xBA" + b"\x00" * 64)

    extractor = ArchiveIPKExtractor(ipk_file)

    def _explode(_output_dir: Path) -> Path:
        raise RuntimeError("boom")

    extractor.extract = _explode  # type: ignore[method-assign]

    worker = ExtractAndNormalizeWorker(extractor=extractor, output_dir=tmp_path / "work")
    errors: list[tuple[str, str]] = []
    finished_payloads: list[object] = []

    worker.error.connect(lambda stage, message: errors.append((stage, message)))
    worker.finished.connect(finished_payloads.append)
    worker.run()

    assert errors and "boom" in errors[0][1]
    assert finished_payloads and finished_payloads[0] is None


def test_ipk_media_validation_requires_audio(tmp_path: Path) -> None:
    extract_root = tmp_path / "extracted"
    extract_root.mkdir(parents=True)
    (extract_root / "MapA_LOW.webm").write_bytes(b"webm")

    warnings = _validate_ipk_media_presence(extract_root, "MapA", None)
    assert any("No audio file found after IPK extraction" in w for w in warnings)


def test_ipk_media_validation_requires_video(tmp_path: Path) -> None:
    extract_root = tmp_path / "extracted"
    extract_root.mkdir(parents=True)
    (extract_root / "MapA.ogg").write_bytes(b"ogg")

    warnings = _validate_ipk_media_presence(extract_root, "MapA", None)
    assert any("No gameplay video (.webm) found after IPK extraction" in w for w in warnings)


def test_ipk_media_validation_accepts_sidecar_audio_search_root(tmp_path: Path) -> None:
    extract_root = tmp_path / "extracted"
    source_root = tmp_path / "source"
    extract_root.mkdir(parents=True)
    source_root.mkdir(parents=True)

    (extract_root / "MapA_HIGH.webm").write_bytes(b"webm")
    (source_root / "MapA.ogg").write_bytes(b"ogg")

    _validate_ipk_media_presence(extract_root, "MapA", source_root)


def test_pick_ipk_audio_prefers_codename_wav_ckd_for_x360_tree(tmp_path: Path) -> None:
    source_root = tmp_path / "temp_extraction"
    x360_audio_dir = source_root / "cache" / "itf_cooked" / "x360" / "world" / "maps" / "MapA" / "audio"
    x360_audio_dir.mkdir(parents=True, exist_ok=True)

    ogg = x360_audio_dir / "MapA.ogg"
    wav_ckd = x360_audio_dir / "MapA.wav.ckd"
    ogg.write_bytes(b"ogg")
    wav_ckd.write_bytes(b"ckd")

    picked = _pick_ipk_audio([source_root], "MapA")

    assert picked == wav_ckd


def test_archive_worker_uses_extraction_root_for_normalize_search_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ipk_file = tmp_path / "canttameher_x360.ipk"
    ipk_file.write_bytes(b"\x50\xEC\x12\xBA" + b"\x00" * 64)

    extraction_root = tmp_path / "temp_extraction"
    extraction_root.mkdir(parents=True, exist_ok=True)

    extractor = ArchiveIPKExtractor(ipk_file)
    extractor.extract = lambda _output_dir: extraction_root  # type: ignore[method-assign]
    extractor.get_codename = lambda: "canttameher"  # type: ignore[method-assign]

    monkeypatch.setattr(
        "jd2021_installer.ui.workers.pipeline_workers._validate_ipk_media_presence",
        lambda *_args, **_kwargs: [],
    )

    captured: dict[str, Path | str | None] = {}

    def _fake_normalize(
        directory,
        codename,
        search_root=None,
        supplemental_roots=None,
    ):  # type: ignore[no-untyped-def]
        captured["directory"] = Path(directory)
        captured["codename"] = codename
        captured["search_root"] = Path(search_root) if search_root else None
        return object()

    monkeypatch.setattr(
        "jd2021_installer.ui.workers.pipeline_workers.normalize",
        _fake_normalize,
    )

    worker = ExtractAndNormalizeWorker(extractor=extractor, output_dir=tmp_path / "work")
    errors: list[str] = []
    finished_payloads: list[object] = []

    worker.error.connect(lambda _stage, msg: errors.append(msg))
    worker.finished.connect(finished_payloads.append)
    worker.run()

    assert not errors
    assert finished_payloads and finished_payloads[0] is not None
    assert captured["directory"] == extraction_root
    assert captured["codename"] == "canttameher"
    assert captured["search_root"] == extraction_root


def test_reprocess_audio_recovers_missing_ipk_audio_from_source_tree(
    tmp_path: Path,
    sample_normalized_data,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "temp_extraction"
    recovered_audio = (
        source_root
        / "cache"
        / "itf_cooked"
        / "x360"
        / "world"
        / "maps"
        / "sweetbutpsycho"
        / "audio"
        / "sweetbutpsycho.wav.ckd"
    )
    recovered_audio.parent.mkdir(parents=True, exist_ok=True)
    recovered_audio.write_bytes(b"X" * 128)

    map_data = sample_normalized_data
    map_data.codename = "sweetbutpsycho"
    map_data.source_dir = source_root
    map_data.media.audio_path = None

    called: dict[str, Path] = {}

    monkeypatch.setattr(
        "jd2021_installer.ui.workers.pipeline_workers.write_game_files",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "jd2021_installer.installers.media_processor.convert_audio",
        lambda audio_path, *_args, **_kwargs: called.setdefault("audio", Path(audio_path)),
    )
    called_intro: list[tuple] = []
    monkeypatch.setattr(
        "jd2021_installer.installers.media_processor.generate_intro_amb",
        lambda *args, **kwargs: called_intro.append((args, kwargs)),
    )
    monkeypatch.setattr(
        "jd2021_installer.installers.media_processor.extract_amb_clips",
        lambda *_args, **_kwargs: 0,
    )

    reprocess_audio(map_data, tmp_path / "game_map", a_offset=0.0, config=None)

    assert map_data.media.audio_path == recovered_audio
    assert called["audio"] == recovered_audio
    assert not called_intro, "Intro generation should be skipped for IPK to preserve native AMB intro assets"


def test_reprocess_audio_jdnext_generates_intro_when_audio_present(
    tmp_path: Path,
    sample_normalized_data,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "temp_extraction"
    source_root.mkdir(parents=True, exist_ok=True)

    audio_src = source_root / "mapa.wav"
    audio_src.write_bytes(b"audio")

    map_data = sample_normalized_data
    map_data.codename = "mapa"
    map_data.source_dir = source_root
    map_data.media.audio_path = audio_src
    map_data.is_html_source = False
    map_data.is_jdnext_source = True

    called_intro: list[tuple] = []
    called_clip_cleanup: list[tuple] = []
    called_asset_cleanup: list[Path] = []

    monkeypatch.setattr(
        "jd2021_installer.installers.media_processor.convert_audio",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "jd2021_installer.installers.media_processor.generate_intro_amb",
        lambda *args, **_kwargs: called_intro.append(args),
    )
    monkeypatch.setattr(
        "jd2021_installer.installers.media_processor.extract_amb_clips",
        lambda *_args, **_kwargs: 0,
    )
    monkeypatch.setattr(
        "jd2021_installer.installers.ambient_processor._remove_intro_amb_soundset_clips",
        lambda *args, **_kwargs: called_clip_cleanup.append(args) or False,
    )
    monkeypatch.setattr(
        "jd2021_installer.installers.ambient_processor._remove_intro_amb_assets",
        lambda amb_dir, **_kwargs: called_asset_cleanup.append(Path(amb_dir)) or 0,
    )

    reprocess_audio(map_data, tmp_path / "game_map", a_offset=0.0, config=None)

    assert called_intro, "Expected intro generation to run for JDNext"
    assert not called_clip_cleanup, "Disabled-mode clip cleanup should not run when intro is enabled"
    assert not called_asset_cleanup, "Disabled-mode asset cleanup should not run when intro is enabled"
