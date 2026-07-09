import pytest
import os
import subprocess
from pathlib import Path
from PyQt6.QtGui import QPixmap
from PyQt6.QtWidgets import QApplication
from jd2021_installer.core.models import NormalizedMapData, SongDescription, MusicTrackStructure, MapMedia
from jd2021_installer.core.config import AppConfig
from jd2021_installer.installers import media_processor
from jd2021_installer.parsers.normalizer import _find_source_trk_path, normalize_sync
from jd2021_installer.parsers.normalizer import _infer_coach_count_from_media
from jd2021_installer.ui.widgets.preview_widget import PreviewWidget
from jd2021_installer.ui.widgets.bundle_dialog import BundleSelectDialog
from jd2021_installer.ui.workers.pipeline_workers import BatchInstallWorker
from jd2021_installer.extractors import web_playwright

_RUN_QT_WIDGET_TESTS = os.environ.get("JD2021_RUN_QT_WIDGET_TESTS") == "1"


def _build_map(codename: str) -> NormalizedMapData:
    return NormalizedMapData(
        codename=codename,
        song_desc=SongDescription(map_name=codename, title=codename, artist="artist"),
        music_track=MusicTrackStructure(
            markers=[0, 48000],
            signatures=[],
            sections=[],
            start_beat=0,
            end_beat=1,
            video_start_time=0.0,
        ),
        media=MapMedia(),
    )


def _get_qapp() -> QApplication:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app

def test_effective_video_start_time_units():
    """Verify NormalizedMapData properly handles overrides in seconds."""
    mt = MusicTrackStructure(
        markers=[0, 48000],
        signatures=[],
        sections=[],
        start_beat=0,
        end_beat=1,
        video_start_time=0.5  # 0.5 seconds
    )
    map_data = NormalizedMapData(
        codename="test",
        song_desc=SongDescription(map_name="test", title="test", artist="test"),
        music_track=mt,
        media=MapMedia()
    )
    
    # Default (no override)
    assert map_data.effective_video_start_time == 0.5
    
    # With override (should be in seconds)
    map_data.video_start_time_override = 1.2
    assert map_data.effective_video_start_time == 1.2

def test_main_window_unit_conversion_logic():
    """Verify logic used in MainWindow for ms->sec conversion."""
    # This simulates the logic in _on_apply_offset
    original_sec = 0.5
    offset_ms = 500.0  # +500ms
    
    new_v_override_sec = original_sec + (offset_ms / 1000.0)
    assert new_v_override_sec == 1.0
    
    # This simulates the logic in _on_readjust
    vo_sec = 0.5
    vo_ms = vo_sec * 1000.0
    assert vo_ms == 500.0


def test_preview_probe_duration_uses_video_when_audio_ckd_unprobeable(monkeypatch):
    def fake_check_output(cmd, text=True, creationflags=0):
        target = str(cmd[-1]).lower()
        if target.endswith("video.webm"):
            return "200.0\n"
        if target.endswith("audio.wav.ckd"):
            raise subprocess.CalledProcessError(returncode=1, cmd=cmd)
        raise AssertionError(f"Unexpected ffprobe target: {cmd[-1]}")

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    duration = PreviewWidget._probe_duration(
        video_path="C:/tmp/video.webm",
        audio_path="C:/tmp/audio.wav.ckd",
        v_override=-5.865,
        a_offset=0.0,
    )

    assert duration == pytest.approx(194.135, abs=1e-3)


def test_preview_probe_duration_audio_fallback_from_ckd(monkeypatch):
    attempted: list[str] = []

    def fake_check_output(cmd, text=True, creationflags=0):
        target = str(cmd[-1])
        attempted.append(target)
        lowered = target.lower()
        if lowered.endswith("video.webm"):
            return "100.0\n"
        if lowered.endswith("audio.wav"):
            return "120.0\n"
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd)

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    duration = PreviewWidget._probe_duration(
        video_path="C:/tmp/video.webm",
        audio_path="C:/tmp/audio.wav.ckd",
        v_override=0.0,
        a_offset=2.0,
    )

    assert duration == pytest.approx(122.0, abs=1e-3)
    assert any(path.lower().endswith("audio.wav") for path in attempted)


def test_find_source_trk_path_accepts_jdnext_variant_name(tmp_path: Path):
    source_dir = tmp_path / "source"
    audio_dir = source_dir / "Audio"
    audio_dir.mkdir(parents=True)
    trk_path = audio_dir / "mapname_musictrack.trk"
    trk_path.write_text("videoStartTime = -2.145000\n", encoding="utf-8")

    found = _find_source_trk_path(source_dir, "mapname")
    assert found == trk_path


def test_normalize_sync_inherits_video_from_variant_trk_name(tmp_path: Path):
    source_dir = tmp_path / "source"
    audio_dir = source_dir / "Audio"
    audio_dir.mkdir(parents=True)
    trk_path = audio_dir / "jdnext_map_musictrack.trk"
    trk_path.write_text("videoStartTime = -1.750000\n", encoding="utf-8")

    mt = MusicTrackStructure(
        markers=[0, 48000],
        signatures=[],
        sections=[],
        start_beat=0,
        end_beat=1,
        video_start_time=0.0,
    )

    found = _find_source_trk_path(source_dir, "jdnext_map")
    assert found == trk_path

    sync = normalize_sync(mt, is_html_source=False, existing_trk_path=found)
    assert sync.audio_ms == pytest.approx(0.0)
    assert sync.video_ms == pytest.approx(-1750.0)


def test_infer_coach_count_from_media_supports_variant_names():
    media = MapMedia(
        coach_images=[
            Path("TelephoneALT_coach1.png"),
            Path("TelephoneALT_coach_2.png"),
        ]
    )
    assert _infer_coach_count_from_media(media) == 2


def test_download_prefers_curl_resolve_for_jdhelper(monkeypatch, tmp_path: Path):
    url = (
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/x/"
        "video_ULTRA.vp9.webm/hash.webm?auth=token"
    )

    monkeypatch.setattr(web_playwright, "_classify_urls", lambda urls, quality, config=None: {
        "video": url,
        "audio": None,
        "mainscene": None,
        "others": [],
    })

    calls = {"curl": 0, "session_get": 0}

    def _fake_curl(download_url, target, timeout_s):
        calls["curl"] += 1
        target.write_bytes(b"\x1a\x45\xdf\xa3" + b"0" * 2048)
        return True

    class _FakeSession:
        headers = {}

        def get(self, *args, **kwargs):
            calls["session_get"] += 1
            raise AssertionError("requests session should not run when curl primary succeeds")

    monkeypatch.setattr(web_playwright, "_download_with_curl_resolve", _fake_curl)
    monkeypatch.setattr(web_playwright, "_is_valid_webm_file", lambda path, config: True)
    monkeypatch.setattr(web_playwright.requests, "Session", lambda: _FakeSession())

    cfg = AppConfig()
    result = web_playwright.download_files([url], tmp_path, config=cfg)

    assert calls["curl"] == 1
    assert calls["session_get"] == 0
    assert result


def test_preview_probe_duration_handles_positive_video_offset(monkeypatch):
    def fake_check_output(cmd, text=True, creationflags=0):
        target = str(cmd[-1]).lower()
        if target.endswith("video.webm"):
            return "100.0\n"
        if target.endswith("audio.ogg"):
            return "100.0\n"
        raise AssertionError(f"Unexpected ffprobe target: {cmd[-1]}")

    monkeypatch.setattr(subprocess, "check_output", fake_check_output)

    duration = PreviewWidget._probe_duration(
        video_path="C:/tmp/video.webm",
        audio_path="C:/tmp/audio.ogg",
        v_override=2.0,
        a_offset=0.0,
    )

    assert duration == pytest.approx(102.0, abs=1e-3)


def test_copy_video_preserves_webm_without_reencode(monkeypatch, tmp_path: Path):
    src = tmp_path / "src.webm"
    dst = tmp_path / "dst.webm"
    src.write_bytes(b"original_webm_data")

    called: dict[str, bool] = {"ffmpeg": False}

    def _fake_run_ffmpeg(args, config=None, timeout=300):
        called["ffmpeg"] = True
        class _Ok:
            returncode = 0
        return _Ok()

    monkeypatch.setattr(media_processor, "run_ffmpeg", _fake_run_ffmpeg)

    out = media_processor.copy_video(src, dst)

    assert out == dst
    assert called["ffmpeg"] is False
    assert dst.exists()
    assert dst.read_bytes() == b"original_webm_data"


def test_copy_video_keeps_vp9_yuv420p(monkeypatch, tmp_path: Path):
    src = tmp_path / "src.webm"
    dst = tmp_path / "dst.webm"
    src.write_bytes(b"data")

    class _Probe:
        returncode = 0
        stdout = "vp9\nyuv420p\n"

    copied: dict[str, bool] = {"copy": False}

    def _fake_run_ffprobe(args, config=None, timeout=30):
        return _Probe()

    def _fake_copy2(src_path, dst_path):
        copied["copy"] = True
        Path(dst_path).write_bytes(Path(src_path).read_bytes())

    monkeypatch.setattr(media_processor, "run_ffprobe", _fake_run_ffprobe)
    monkeypatch.setattr(media_processor.shutil, "copy2", _fake_copy2)

    out = media_processor.copy_video(src, dst)

    assert out == dst
    assert copied["copy"] is True
    assert dst.exists()


def test_copy_video_force_reencode_even_if_vp9(monkeypatch, tmp_path: Path):
    src = tmp_path / "src.webm"
    dst = tmp_path / "dst.webm"
    src.write_bytes(b"data")

    class _Probe:
        returncode = 0
        stdout = "vp9\nyuv420p\n"

    called: dict[str, bool] = {"ffmpeg": False}

    def _fake_run_ffprobe(args, config=None, timeout=30):
        return _Probe()

    def _fake_run_ffmpeg(args, config=None, timeout=300):
        called["ffmpeg"] = True
        dst.write_bytes(b"reencoded")
        class _Ok:
            returncode = 0
        return _Ok()

    monkeypatch.setattr(media_processor, "run_ffprobe", _fake_run_ffprobe)
    monkeypatch.setattr(media_processor, "run_ffmpeg", _fake_run_ffmpeg)

    out = media_processor.copy_video(src, dst, force_reencode=True)

    assert out == dst
    assert called["ffmpeg"] is True
    assert dst.exists()


def test_classify_urls_non_jdnext_keeps_requested_tier_under_vp9_compat_mode():
    urls = [
        "https://example.com/private/map/x/video_ULTRA.hd.webm/hash.webm",
        "https://example.com/private/map/x/video_ULTRA.vp8.webm/hash.webm",
        "https://example.com/private/map/x/video_HIGH.hd.webm/hash.webm",
    ]

    classified = web_playwright._classify_urls(urls, "ULTRA")
    selected = classified.get("video")

    assert isinstance(selected, str)
    # VP9 compatibility downgrade must remain JDNext-only.
    assert "video_ULTRA." in selected
    assert "video_HIGH." not in selected


def test_classify_urls_ignores_jdnext_vp9_variants():
    urls = [
        "https://example.com/private/map/x/video_HIGH.vp9.webm/hash.webm",
        "https://example.com/private/map/x/video_HIGH.hd.webm/hash.webm",
        "https://example.com/private/map/x/video_MID.vp9.webm/hash.webm",
        "https://example.com/private/map/x/video_MID.hd.webm/hash.webm",
    ]

    classified = web_playwright._classify_urls(urls, "HIGH")
    selected = classified.get("video")

    assert isinstance(selected, str)
    assert "vp9" not in selected.lower()
    assert "video_MID.hd.webm" in selected


@pytest.mark.skipif(not _RUN_QT_WIDGET_TESTS, reason="Set JD2021_RUN_QT_WIDGET_TESTS=1 to run Qt widget behavior tests.")
def test_preview_pause_does_not_clear_canvas():
    _get_qapp()
    widget = PreviewWidget()
    pixmap = QPixmap(8, 8)
    widget._canvas.setPixmap(pixmap)
    widget._canvas.setText("")
    widget._playing = True
    widget._toggle_playback()
    assert widget._canvas.text() == ""
    assert widget.is_playing is False


@pytest.mark.skipif(not _RUN_QT_WIDGET_TESTS, reason="Set JD2021_RUN_QT_WIDGET_TESTS=1 to run Qt widget behavior tests.")
def test_preview_seek_does_not_jump_while_dragging(monkeypatch):
    _get_qapp()
    widget = PreviewWidget()
    widget._duration = 100.0
    widget._seek_slider.setValue(700)

    monkeypatch.setattr(widget._seek_slider, "isSliderDown", lambda: True)
    widget._on_position(20.0)

    assert widget._seek_slider.value() == 700


@pytest.mark.skipif(not _RUN_QT_WIDGET_TESTS, reason="Set JD2021_RUN_QT_WIDGET_TESTS=1 to run Qt widget behavior tests.")
def test_bundle_dialog_select_all_toggle():
    _get_qapp()
    dlg = BundleSelectDialog("bundle.ipk", ["MapA", "MapB", "MapC"])

    dlg._select_all.setChecked(False)
    assert dlg.get_selected_maps() == []

    dlg._select_all.setChecked(True)
    assert dlg.get_selected_maps() == ["MapA", "MapB", "MapC"]


def test_batch_worker_discovered_maps_respect_selection(tmp_path: Path, monkeypatch):
    from jd2021_installer.extractors import archive_ipk
    from jd2021_installer.parsers import normalizer

    ipk = tmp_path / "bundle.ipk"
    ipk.touch()
    extracted = tmp_path / "extracted" / "world" / "maps"
    map_a = extracted / "MapA"
    map_b = extracted / "MapB"
    map_a.mkdir(parents=True)
    map_b.mkdir(parents=True)
    (map_a / "songdesc.tpl.ckd").touch()
    (map_b / "songdesc.tpl.ckd").touch()

    monkeypatch.setattr(archive_ipk, "inspect_ipk", lambda _: ["MapA", "MapB"])
    monkeypatch.setattr(archive_ipk.ArchiveIPKExtractor, "extract", lambda self, _: extracted.parent.parent)
    monkeypatch.setattr(
        normalizer,
        "normalize",
        lambda root, codename=None, search_root=None, supplemental_roots=None: _build_map(codename or "MapA"),
    )
    installed: list[str] = []
    monkeypatch.setattr(
        BatchInstallWorker,
        "_install_map_synchronously",
        lambda self, map_data: installed.append(map_data.codename),
    )

    worker = BatchInstallWorker(
        batch_source_dir=ipk,
        target_game_dir=tmp_path / "game",
        config=AppConfig(cache_directory=tmp_path / "cache"),
        selected_maps={"MapB"},
    )
    discovered: list[list[str]] = []
    worker.discovered_maps.connect(lambda names: discovered.append(names))
    worker.run()
    assert discovered
    assert discovered[0] == ["MapB"]
    assert installed == ["MapB"]


def test_batch_worker_progress_is_monotonic(tmp_path: Path, monkeypatch):
    from jd2021_installer.extractors import archive_ipk
    from jd2021_installer.parsers import normalizer

    ipk = tmp_path / "bundle.ipk"
    ipk.touch()
    extracted = tmp_path / "extracted" / "world" / "maps"
    map_a = extracted / "MapA"
    map_b = extracted / "MapB"
    map_a.mkdir(parents=True)
    map_b.mkdir(parents=True)
    (map_a / "songdesc.tpl.ckd").touch()
    (map_b / "songdesc.tpl.ckd").touch()

    monkeypatch.setattr(archive_ipk, "inspect_ipk", lambda _: ["MapA", "MapB"])
    monkeypatch.setattr(archive_ipk.ArchiveIPKExtractor, "extract", lambda self, _: extracted.parent.parent)
    monkeypatch.setattr(
        normalizer,
        "normalize",
        lambda root, codename=None, search_root=None, supplemental_roots=None: _build_map(codename or "MapA"),
    )
    monkeypatch.setattr(BatchInstallWorker, "_install_map_synchronously", lambda self, map_data: None)

    worker = BatchInstallWorker(
        batch_source_dir=ipk,
        target_game_dir=tmp_path / "game",
        config=AppConfig(cache_directory=tmp_path / "cache"),
    )
    progress: list[int] = []
    worker.progress.connect(lambda value: progress.append(value))
    worker.run()
    assert progress
    assert progress[0] > 0
    assert progress[-1] == 100
    assert all(a <= b for a, b in zip(progress, progress[1:]))


def test_batch_worker_accepts_html_map_folder(tmp_path: Path, monkeypatch):
    from jd2021_installer.extractors import web_playwright
    from jd2021_installer.parsers import normalizer

    batch_root = tmp_path / "batch"
    map_folder = batch_root / "MyMap"
    map_folder.mkdir(parents=True)
    (map_folder / "assets.html").write_text("<html></html>", encoding="utf-8")
    (map_folder / "nohud.html").write_text("<html></html>", encoding="utf-8")

    prepared = tmp_path / "prepared" / "MyMap"
    prepared.mkdir(parents=True)

    monkeypatch.setattr(
        web_playwright.WebPlaywrightExtractor,
        "extract",
        lambda self, _: prepared,
    )
    monkeypatch.setattr(
        normalizer,
        "normalize",
        lambda root, codename=None, search_root=None, supplemental_roots=None: _build_map(codename or "MyMap"),
    )

    installed: list[str] = []
    monkeypatch.setattr(
        BatchInstallWorker,
        "_install_map_synchronously",
        lambda self, map_data: installed.append(map_data.codename),
    )

    worker = BatchInstallWorker(
        batch_source_dir=batch_root,
        target_game_dir=tmp_path / "game",
        config=AppConfig(cache_directory=tmp_path / "cache"),
    )

    discovered: list[list[str]] = []
    worker.discovered_maps.connect(lambda names: discovered.append(names))
    worker.run()

    assert discovered
    assert discovered[0] == ["MyMap"]
    assert installed == ["MyMap"]


def test_batch_worker_fetch_codenames_use_multi_map_flow(tmp_path: Path, monkeypatch):
    from jd2021_installer.extractors import web_playwright
    from jd2021_installer.parsers import normalizer

    prepared_root = tmp_path / "prepared"
    prepared_root.mkdir(parents=True)

    def _extract(self, output_dir):
        codename = self._codenames[0]
        target = prepared_root / codename
        target.mkdir(parents=True, exist_ok=True)
        return target

    monkeypatch.setattr(web_playwright.WebPlaywrightExtractor, "extract", _extract)
    monkeypatch.setattr(
        normalizer,
        "normalize",
        lambda root, codename=None, search_root=None, supplemental_roots=None: _build_map(codename or Path(root).name),
    )

    installed: list[str] = []
    monkeypatch.setattr(
        BatchInstallWorker,
        "_install_map_synchronously",
        lambda self, map_data: installed.append(map_data.codename),
    )

    worker = BatchInstallWorker(
        batch_source_dir=tmp_path,
        target_game_dir=tmp_path / "game",
        config=AppConfig(cache_directory=tmp_path / "cache"),
        selected_maps={"MapB"},
        fetch_codenames=["MapA", "MapB"],
    )

    discovered: list[list[str]] = []
    worker.discovered_maps.connect(lambda names: discovered.append(names))
    worker.run()

    assert discovered
    assert discovered[0] == ["MapB"]
    assert installed == ["MapB"]


def test_batch_worker_fetch_sets_source_mode_for_sync_install(tmp_path: Path, monkeypatch):
    from jd2021_installer.extractors import web_playwright
    from jd2021_installer.parsers import normalizer

    prepared_root = tmp_path / "prepared"
    prepared_root.mkdir(parents=True)

    def _extract(self, output_dir):
        codename = self._codenames[0]
        target = prepared_root / codename
        target.mkdir(parents=True, exist_ok=True)
        return target

    monkeypatch.setattr(web_playwright.WebPlaywrightExtractor, "extract", _extract)
    monkeypatch.setattr(
        normalizer,
        "normalize",
        lambda root, codename=None, search_root=None, supplemental_roots=None: _build_map(codename or Path(root).name),
    )

    captured_modes: list[str] = []

    def _capture_install(self, map_data):
        captured_modes.append(str(getattr(map_data, "_install_source_mode", "")))

    monkeypatch.setattr(BatchInstallWorker, "_install_map_synchronously", _capture_install)

    worker = BatchInstallWorker(
        batch_source_dir=tmp_path,
        target_game_dir=tmp_path / "game",
        config=AppConfig(cache_directory=tmp_path / "cache"),
        fetch_codenames=["MapA"],
        fetch_source="jdu",
    )

    worker.run()

    assert captured_modes == ["Fetch"]


def test_batch_worker_fetch_codenames_ignore_local_batch_scan(tmp_path: Path, monkeypatch):
    from jd2021_installer.extractors import archive_ipk, web_playwright
    from jd2021_installer.parsers import normalizer

    # If directory scanning ran, this IPK would be discovered and installed too.
    stray_ipk = tmp_path / "StrayBundle.ipk"
    stray_ipk.touch()

    inspect_calls = {"count": 0}

    def _inspect(_):
        inspect_calls["count"] += 1
        return ["StrayMap"]

    monkeypatch.setattr(archive_ipk, "inspect_ipk", _inspect)

    prepared_root = tmp_path / "prepared"
    prepared_root.mkdir(parents=True)

    def _extract(self, output_dir):
        codename = self._codenames[0]
        target = prepared_root / codename
        target.mkdir(parents=True, exist_ok=True)
        return target

    monkeypatch.setattr(web_playwright.WebPlaywrightExtractor, "extract", _extract)
    monkeypatch.setattr(
        normalizer,
        "normalize",
        lambda root, codename=None, search_root=None, supplemental_roots=None: _build_map(codename or Path(root).name),
    )

    installed: list[str] = []
    monkeypatch.setattr(
        BatchInstallWorker,
        "_install_map_synchronously",
        lambda self, map_data: installed.append(map_data.codename),
    )

    worker = BatchInstallWorker(
        batch_source_dir=tmp_path,
        target_game_dir=tmp_path / "game",
        config=AppConfig(cache_directory=tmp_path / "cache"),
        fetch_codenames=["Koi", "Starships"],
    )

    worker.run()

    assert inspect_calls["count"] == 0
    assert installed == ["Koi", "Starships"]


def test_batch_worker_merges_bundle_map_list_and_skips_duplicates(tmp_path: Path, monkeypatch):
    from jd2021_installer.extractors import archive_ipk
    from jd2021_installer.parsers import normalizer

    ipk_a = tmp_path / "bundle_a.ipk"
    ipk_b = tmp_path / "bundle_b.ipk"
    ipk_a.touch()
    ipk_b.touch()

    extracted_root = tmp_path / "extracted" / "world" / "maps"
    (extracted_root / "MapA").mkdir(parents=True)
    (extracted_root / "MapB").mkdir(parents=True)
    (extracted_root / "MapC").mkdir(parents=True)

    def _inspect(path: Path):
        if path.name == "bundle_a.ipk":
            return ["MapA", "MapB"]
        if path.name == "bundle_b.ipk":
            return ["MapB", "MapC"]
        return []

    monkeypatch.setattr(archive_ipk, "inspect_ipk", _inspect)
    monkeypatch.setattr(
        archive_ipk.ArchiveIPKExtractor,
        "extract",
        lambda self, _: extracted_root.parent.parent,
    )
    monkeypatch.setattr(
        normalizer,
        "normalize",
        lambda root, codename=None, search_root=None, supplemental_roots=None: _build_map(codename or "MapA"),
    )

    installed: list[str] = []
    monkeypatch.setattr(
        BatchInstallWorker,
        "_install_map_synchronously",
        lambda self, map_data: installed.append(map_data.codename),
    )

    worker = BatchInstallWorker(
        batch_source_dir=tmp_path,
        target_game_dir=tmp_path / "game",
        config=AppConfig(cache_directory=tmp_path / "cache"),
    )

    discovered: list[list[str]] = []
    worker.discovered_maps.connect(lambda names: discovered.append(names))
    worker.run()

    assert discovered
    assert discovered[0] == ["MapA", "MapB", "MapC"]
    assert installed == ["MapA", "MapB", "MapC"]


def test_binary_reader_null_termination():
    from jd2021_installer.parsers.binary_ckd import BinaryReader
    # Test len_string with trailing null byte
    data = b"\x00\x00\x00\x06hello\x00"
    reader = BinaryReader(data)
    assert reader.len_string() == "hello"
    assert reader.pos == 10

    # Test clean_string with middle null byte
    data2 = b"\x00\x00\x00\x07abc\x00def"
    reader2 = BinaryReader(data2)
    assert reader2.len_string() == "abc"
    assert reader2.pos == 11

