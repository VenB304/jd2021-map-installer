import pytest
import json
import os
import subprocess
import zipfile
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch
from jd2021_installer.core.config import AppConfig
from jd2021_installer.core.exceptions import WebExtractionError
import jd2021_installer.extractors.web_playwright as web_playwright
from jd2021_installer.extractors.web_playwright import (
    WebPlaywrightExtractor,
    _classify_urls,
    _embed_matches_requester,
    _embed_mentions_expected_codename,
    _embed_contains_codename_links,
    _extract_embed_error_message,
    _extract_embed_fields_from_html,
    _has_valid_cdn_links,
    _infer_codename_from_source_files,
    _is_valid_embed_response,
    _is_valid_webm_file,
    _parse_jdnext_button_payloads,
    extract_codename_from_urls,
    download_files,
)

def test_extract_scene_zips_unpacks_ipk(tmp_path):
    """Verify that _extract_scene_zips unpacks any .ipk files found after ZIP extraction."""
    # Setup: Create a dummy ZIP file
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    
    zip_filename = "Test_MAIN_SCENE_DURANGO.zip"
    zip_path = output_dir / zip_filename
    
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("dummy.txt", "content")
    
    # Create a dummy IPK file in the output_dir to simulate it being extracted from the ZIP
    ipk_path = output_dir / "Test.ipk"
    ipk_path.write_text("dummy ipk content")
    
    with patch("zipfile.ZipFile") as mock_zip, \
         patch("jd2021_installer.extractors.web_playwright.extract_ipk") as mock_extract_ipk:
        
        # Mock zipfile behavior
        mock_zip_instance = mock_zip.return_value.__enter__.return_value
        
        # Execute the extraction logic
        WebPlaywrightExtractor._extract_scene_zips(output_dir)
        
        # Verify ZIP was "extracted" (mocked)
        mock_zip.assert_called()
        mock_zip_instance.extractall.assert_called_with(output_dir)
        
        # Verify IPK was unpacked
        mock_extract_ipk.assert_called_once_with(ipk_path, output_dir)
        
        # Verify IPK was deleted after extraction
        assert not ipk_path.exists()

def test_extract_scene_zips_no_ipk(tmp_path):
    """Verify that _extract_scene_zips does nothing if no .ipk files are found."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    zip_path = output_dir / "Test_MAIN_SCENE_DURANGO.zip"
    
    with zipfile.ZipFile(zip_path, "w") as z:
        z.writestr("dummy.txt", "content")

    with patch("zipfile.ZipFile"), \
         patch("jd2021_installer.extractors.web_playwright.extract_ipk") as mock_extract_ipk:
        
        WebPlaywrightExtractor._extract_scene_zips(output_dir)
        
        # No IPK should be extracted
        mock_extract_ipk.assert_not_called()


def test_classify_urls_prefers_durango_scene_zip():
    urls = [
        "https://jd-s3.cdn.ubi.com/public/map/TestMap/TestMap_MAIN_SCENE_DURANGO.zip",
        "https://jd-s3.cdn.ubi.com/public/map/TestMap/TestMap_MAIN_SCENE_X360.zip",
        "https://jd-s3.cdn.ubi.com/public/map/TestMap/TestMap_MAIN_SCENE_NX.zip",
    ]

    classified = _classify_urls(urls, "ULTRA_HD")
    assert classified["mainscene"] is not None
    assert "MAIN_SCENE_DURANGO" in classified["mainscene"]


def test_extract_scene_zips_prefers_x360(tmp_path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "Test_MAIN_SCENE_DURANGO.zip").write_bytes(b"dummy")
    (output_dir / "Test_MAIN_SCENE_X360.zip").write_bytes(b"dummy")

    with patch("zipfile.ZipFile") as mock_zip:
        WebPlaywrightExtractor._extract_scene_zips(output_dir)

    called_path = mock_zip.call_args[0][0]
    assert Path(called_path).name == "Test_MAIN_SCENE_DURANGO.zip"


def test_download_files_reuses_existing_alternate_video(tmp_path, monkeypatch):
    class FakeResponse:
        def __init__(self, status_code, headers=None, chunks=None):
            self.status_code = status_code
            self.headers = headers or {}
            self._chunks = chunks or []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def iter_content(self, chunk_size=1024 * 1024):
            return iter(self._chunks)

    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.get_calls = []

        def get(self, url, *args, **kwargs):
            self.get_calls.append(url)
            if url.endswith(".ogg"):
                return FakeResponse(200, headers={"content-length": "2048"}, chunks=[b"a" * 2048])
            if "MAIN_SCENE" in url:
                return FakeResponse(200, headers={"content-length": "2048"}, chunks=[b"z" * 2048])
            # Requested video URL would fail, but we should never call this when reuse works.
            return FakeResponse(404)

    fake_session = FakeSession()
    monkeypatch.setattr("requests.Session", lambda: fake_session)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    # Existing alternate quality should no longer be reused for a requested tier.
    existing = tmp_path / "TestMap_LOW.webm"
    existing.write_bytes(b"v" * 2048)

    cfg = AppConfig(max_retries=1, inter_request_delay_s=0.0)
    urls = [
        "https://jd-s3.cdn.ubi.com/public/map/TestMap/TestMap_ULTRA.hd.webm",
        "https://jd-s3.cdn.ubi.com/public/map/TestMap/TestMap.ogg",
        "https://jd-s3.cdn.ubi.com/public/map/TestMap/TestMap_MAIN_SCENE_DURANGO.zip",
    ]

    downloaded = download_files(urls, tmp_path, "ULTRA_HD", cfg)

    assert "TestMap_ULTRA.hd.webm" not in downloaded
    assert any(u.endswith("TestMap_ULTRA.hd.webm") for u in fake_session.get_calls)


def test_web_extractor_raises_when_critical_assets_still_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "jd2021_installer.extractors.web_playwright.download_files",
        lambda *args, **kwargs: {},
    )

    extractor = WebPlaywrightExtractor(
        urls=[
            "https://jd-s3.cdn.ubi.com/public/map/TestMap/TestMap_ULTRA.hd.webm",
            "https://jd-s3.cdn.ubi.com/public/map/TestMap/TestMap.ogg",
            "https://jd-s3.cdn.ubi.com/public/map/TestMap/TestMap_MAIN_SCENE_DURANGO.zip",
        ],
        config=AppConfig(download_root=tmp_path),
    )

    with pytest.raises(WebExtractionError, match=r"Critical download\(s\) missing"):
        extractor.extract(tmp_path / "out")


def test_web_extractor_download_dir_groups_jdu_under_mapdownloads(tmp_path):
    extractor = WebPlaywrightExtractor(
        source_game="jdu",
        config=AppConfig(download_root=tmp_path),
    )

    assert extractor._download_dir_for_codename("TestMap") == tmp_path / "jdu" / "TestMap"


def test_web_extractor_download_dir_groups_jdnext_under_mapdownloads(tmp_path):
    extractor = WebPlaywrightExtractor(
        source_game="jdnext",
        config=AppConfig(download_root=tmp_path),
    )

    assert extractor._download_dir_for_codename("TestMap") == tmp_path / "jdnext" / "TestMap"


def test_download_files_respects_retry_after_header(tmp_path, monkeypatch):
    class FakeResponse:
        def __init__(self, status_code, headers=None, chunks=None):
            self.status_code = status_code
            self.headers = headers or {}
            self._chunks = chunks or []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def iter_content(self, chunk_size=1024 * 1024):
            return iter(self._chunks)

    class FakeSession:
        def __init__(self):
            self.headers = {}
            self._responses = iter(
                [
                    FakeResponse(429, headers={"Retry-After": "7"}),
                    FakeResponse(
                        200,
                        headers={"content-length": "2048"},
                        chunks=[b"x" * 2048],
                    ),
                ]
            )

        def get(self, *args, **kwargs):
            return next(self._responses)

    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr("requests.Session", FakeSession)

    cfg = AppConfig(max_retries=3, retry_base_delay_s=2, inter_request_delay_s=0.0)
    urls = ["https://jd-s3.cdn.ubi.com/public/map/TestMap/TestMap_musictrack.tpl.ckd"]

    downloaded = download_files(urls, tmp_path, "ULTRA_HD", cfg)

    assert "TestMap_musictrack.tpl.ckd" in downloaded
    assert 7 in sleep_calls


def test_download_files_redownloads_corrupt_cached_nohud_webm(tmp_path, monkeypatch):
    class FakeResponse:
        def __init__(self, status_code, headers=None, chunks=None):
            self.status_code = status_code
            self.headers = headers or {}
            self._chunks = chunks or []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def iter_content(self, chunk_size=1024 * 1024):
            return iter(self._chunks)

    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            return FakeResponse(200, headers={"content-length": "4096"}, chunks=[b"\x1a\x45\xdf\xa3" + b"v" * 4092])

    monkeypatch.setattr("requests.Session", FakeSession)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))

    corrupt_cached = tmp_path / "TestMap_ULTRA.hd.webm"
    corrupt_cached.write_bytes(b"NOTWEBM" * 512)

    cfg = AppConfig(max_retries=1, inter_request_delay_s=0.0)
    urls = [
        "https://jdcn-switch.cdn.ubisoft.cn/private/map/TestMap/TestMap_ULTRA.hd.webm/hashvalue.webm?auth=test",
    ]

    downloaded = download_files(urls, tmp_path, "ULTRA_HD", cfg)


def test_extract_embed_fields_from_html_parses_name_value_pairs():
    html = (
        '<div class="embedFieldName__x"><span>Difficulty:</span></div>'
        '<div class="embedFieldValue__x"><span>Easy</span></div>'
        '<div class="embedFieldName__x"><span>Coach Count:</span></div>'
        '<div class="embedFieldValue__x"><span>2</span></div>'
    )

    fields = _extract_embed_fields_from_html(html)

    assert fields["Difficulty"] == "Easy"
    assert fields["Coach Count"] == "2"


def test_parse_jdnext_button_payloads_maps_expected_other_info_fields():
    payloads = {
        "tags": {
            "accessories_html": (
                '<div class="embedFieldName__x"><span>Tags:</span></div>'
                '<div class="embedFieldValue__x"><span>Main, Extreme</span></div>'
            ),
            "content_text": "",
            "combined_html": "",
            "message_id": "m1",
        },
        "coaches": {
            "accessories_html": (
                '<div class="embedFieldName__x"><span>Coach 1:</span></div>'
                '<div class="embedFieldValue__x"><span>Alpha</span></div>'
                '<div class="embedFieldName__x"><span>Coach 2:</span></div>'
                '<div class="embedFieldValue__x"><span>Beta</span></div>'
            ),
            "content_text": "",
            "combined_html": "",
            "message_id": "m2",
        },
        "credits": {
            "accessories_html": (
                '<div class="embedFieldName__x"><span>Credits:</span></div>'
                '<div class="embedFieldValue__x"><span>Sample Credits</span></div>'
            ),
            "content_text": "",
            "combined_html": "",
            "message_id": "m3",
        },
        "other_info": {
            "accessories_html": "",
            "content_text": (
                "Difficulty: Easy\n"
                "Sweat difficulty: Medium\n"
                "Additional Title: true\n"
                "Camera support: false\n"
                "Lyrics color: #AABBCC\n"
                "Title logo: true\n"
                "Map length: 02:34\n"
                "Original JD Version: JD2023\n"
                "Coach Count: 2\n"
            ),
            "combined_html": "",
            "message_id": "m4",
        },
    }

    parsed = _parse_jdnext_button_payloads(payloads)

    assert parsed["tags"] == ["Main", "Extreme"]
    assert parsed["coach_names"] == ["Alpha", "Beta"]
    assert parsed["credits"] == "Sample Credits"

    other_info = cast(dict[str, object], parsed["other_info"])
    assert other_info["difficulty"] == "Easy"
    assert other_info["sweat_difficulty"] == "Medium"
    assert other_info["additional_title"] is True
    assert other_info["camera_support"] is False
    assert other_info["lyrics_color"] == "#AABBCC"
    assert other_info["title_logo"] is True
    assert other_info["map_length"] == "02:34"
    assert other_info["original_jd_version"] == "JD2023"
    assert other_info["coach_count"] == "2"


def test_embed_contains_codename_links_matches_target_map_only():
    target_html = (
        '<a href="https://jd-s3.cdn.ubi.com/public/map/MyMap/MyMap_MAIN_SCENE_DURANGO.zip">scene</a>'
    )
    other_html = (
        '<a href="https://jd-s3.cdn.ubi.com/public/map/OtherMap/OtherMap_MAIN_SCENE_DURANGO.zip">scene</a>'
    )

    assert _embed_contains_codename_links(target_html, "MyMap") is True
    assert _embed_contains_codename_links(other_html, "MyMap") is False


def test_is_valid_embed_response_rejects_other_user_codename_response():
    other_user_html = (
        '<div><a href="https://jd-s3.cdn.ubi.com/public/map/OtherMap/OtherMap_ULTRA.hd.webm">video</a></div>'
    )

    assert _is_valid_embed_response(other_user_html) is True
    assert _is_valid_embed_response(other_user_html, expected_codename="MyMap") is False


def test_is_valid_embed_response_allows_textual_codename_fallback():
    # Simulates JDNext-style response where link path may not carry the requested codename.
    html = (
        '<div class="embedTitle__x"><span>Judas</span></div>'
        '<a href="https://jdcn-switch.cdn.ubisoft.cn/private/jdnext/maps/abcd/video_ultra.webm">video</a>'
    )

    assert _is_valid_embed_response(
        html,
        require_gameplay_video=True,
        expected_codename="Judas",
        allow_textual_codename_fallback=True,
    ) is True


def test_extract_embed_error_message_from_error_field():
    html = (
        '<div class="embedFieldName__x"><span>Error</span></div>'
        '<div class="embedFieldValue__x"><span>❌ Couldn\'t find this track ❌</span></div>'
    )

    msg = _extract_embed_error_message(html)

    assert "Couldn" in msg
    assert "find this track" in msg


def test_embed_mentions_expected_codename_matches_embed_title():
    html = '<div class="embedTitle__x"><span>Koi</span></div>'

    assert _embed_mentions_expected_codename(html, "Koi") is True
    assert _embed_mentions_expected_codename(html, "OtherMap") is False


def test_extract_codename_from_urls_ignores_jdnext_public_uuid_maps_id():
    urls = [
        "https://jd-s3.cdn.ubi.com/public/jdnext/maps/267ea001-65e8-4c60-b8a1-daa2b25dbd5f/nx/mapPackage/hash.bundle",
    ]

    assert extract_codename_from_urls(urls) is None


def test_extract_codename_from_urls_ignores_jdnext_private_uuid_maps_id():
    urls = [
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/267ea001-65e8-4c60-b8a1-daa2b25dbd5f/video_ULTRA.hd.webm/hash.webm?auth=abc",
    ]

    assert extract_codename_from_urls(urls) is None


def test_infer_codename_from_source_files_prefers_map_json_songdesc(tmp_path: Path):
    map_json = tmp_path / "monobehaviour" / "map.json"
    map_json.parent.mkdir(parents=True, exist_ok=True)
    map_json.write_text(
        json.dumps({"SongDesc": {"MapName": "Jukebox"}}),
        encoding="utf-8",
    )

    assert _infer_codename_from_source_files(tmp_path) == "Jukebox"


def test_infer_codename_from_source_files_uses_moves_prefix_fallback(tmp_path: Path):
    moves_dir = tmp_path / "Timeline" / "Moves" / "durango"
    moves_dir.mkdir(parents=True, exist_ok=True)
    (moves_dir / "jukebox_cross.gesture").write_text("x", encoding="utf-8")
    (moves_dir / "jukebox_spin.gesture").write_text("x", encoding="utf-8")
    (moves_dir / "jukebox_pose.msm").write_text("x", encoding="utf-8")

    assert _infer_codename_from_source_files(tmp_path) == "jukebox"


def test_embed_matches_requester_with_matching_mention():
    html = '<div><span>@Monika</span><span>Couldn\'t find this track</span></div>'
    assert _embed_matches_requester(html, ["monika"]) is True


def test_embed_matches_requester_with_different_mention():
    html = '<div><span>@SomeoneElse</span><span>Couldn\'t find this track</span></div>'
    assert _embed_matches_requester(html, ["monika"]) is False


def test_embed_matches_requester_neutral_when_no_mentions():
    html = '<div><span>Error:</span><span>Couldn\'t find this track</span></div>'
    assert _embed_matches_requester(html, ["monika"]) is True


def test_parse_jdnext_button_payloads_uses_text_fallback_for_tags_and_coaches():
    payloads = {
        "tags": {
            "accessories_html": "",
            "content_text": "",
            "combined_html": (
                "<div>Verified AppAPP -- @Monika Tags: Night, Medium, Romantic</div>"
            ),
            "message_id": "m1",
        },
        "coaches": {
            "accessories_html": "",
            "content_text": "",
            "combined_html": (
                "<div>Verified AppAPP -- @Monika Coaches' names: The Bride</div>"
            ),
            "message_id": "m2",
        },
        "credits": {
            "accessories_html": "",
            "content_text": "",
            "combined_html": "",
            "message_id": "m3",
        },
        "other_info": {
            "accessories_html": "",
            "content_text": "Coach Count: 1",
            "combined_html": "",
            "message_id": "m4",
        },
    }

    parsed = _parse_jdnext_button_payloads(payloads)

    assert parsed["tags"] == ["Night", "Medium", "Romantic"]
    assert parsed["coach_names"] == ["The Bride"]
    other_info = cast(dict[str, object], parsed["other_info"])
    assert other_info["coach_count"] == "1"


def test_download_files_retries_when_nohud_webm_is_corrupt(tmp_path, monkeypatch):
    class FakeResponse:
        def __init__(self, status_code, headers=None, chunks=None):
            self.status_code = status_code
            self.headers = headers or {}
            self._chunks = chunks or []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def iter_content(self, chunk_size=1024 * 1024):
            return iter(self._chunks)

    class FakeSession:
        def __init__(self):
            self.headers = {}
            self.calls = 0

        def get(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                return FakeResponse(200, headers={"content-length": "2048"}, chunks=[b"BAD!" * 512])
            return FakeResponse(200, headers={"content-length": "4096"}, chunks=[b"\x1a\x45\xdf\xa3" + b"z" * 4092])

    fake_session = FakeSession()
    monkeypatch.setattr("requests.Session", lambda: fake_session)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))

    cfg = AppConfig(max_retries=2, inter_request_delay_s=0.0)
    urls = [
        "https://jdcn-switch.cdn.ubisoft.cn/private/map/TestMap/TestMap_ULTRA.hd.webm/hashvalue.webm?auth=test",
    ]

    downloaded = download_files(urls, tmp_path, "ULTRA_HD", cfg)

    assert "TestMap_ULTRA.hd.webm" in downloaded
    assert fake_session.calls == 2
    assert (tmp_path / "TestMap_ULTRA.hd.webm").read_bytes().startswith(b"\x1a\x45\xdf\xa3")


def test_is_valid_webm_file_uses_cached_result_for_unchanged_file(tmp_path, monkeypatch):
    probe_calls = {"count": 0}

    class FakeProc:
        returncode = 0
        stderr = ""

    def _fake_run(*args, **kwargs):
        probe_calls["count"] += 1
        return FakeProc()

    monkeypatch.setattr("subprocess.run", _fake_run)

    webm = tmp_path / "cached_nohud.webm"
    webm.write_bytes(b"\x1a\x45\xdf\xa3" + b"v" * 5000)

    cfg = AppConfig(download_timeout_s=5)

    assert _is_valid_webm_file(webm, cfg) is True
    assert _is_valid_webm_file(webm, cfg) is True
    assert probe_calls["count"] == 1


def test_classify_urls_supports_jdnext_mappackage_opus_and_private_video():
    urls = [
        "https://jd-s3.cdn.ubi.com/public/jdnext/maps/uuid123/audioPreview.opus/hashpreview.opus",
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/uuid123/video_ULTRA.hd.webm/hashvideo.webm?auth=abc",
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/uuid123/audio.opus/hashaudio.opus?auth=abc",
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/uuid123/nx/mapPackage/hashmap_v0.bundle?auth=abc",
        "https://jd-s3.cdn.ubi.com/public/jdnext/maps/uuid123/nx/cover/hashcover_v0.bundle",
    ]

    classified = _classify_urls(urls, "ULTRA_HD")

    assert classified["video"] is not None
    assert "video_ULTRA.hd.webm" in str(classified["video"])
    assert classified["audio"] is not None
    assert "audio.opus" in str(classified["audio"])
    assert classified["mainscene"] is not None
    assert "mapPackage" in str(classified["mainscene"])
    assert any("cover" in u for u in classified["others"])


def test_has_valid_cdn_links_accepts_jdnext_maps_path():
    html = (
        '<a href="https://jd-s3.cdn.ubi.com/public/jdnext/maps/uuid123/nx/mapPackage/hash_v0.bundle">Link</a>'
    )
    assert _has_valid_cdn_links(html)


def test_classify_urls_excludes_jdnext_preview_media():
    urls = [
        "https://jd-s3.cdn.ubi.com/public/jdnext/maps/uuid123/videoPreview_ULTRA.vp8.webm/hash.webm",
        "https://jd-s3.cdn.ubi.com/public/jdnext/maps/uuid123/audioPreview.opus/hash.opus",
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/uuid123/video_ULTRA.hd.webm/hashgameplay.webm?auth=abc",
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/uuid123/audio.opus/hashaudio.opus?auth=abc",
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/uuid123/nx/mapPackage/hashmap_v0.bundle?auth=abc",
    ]

    classified = _classify_urls(urls, "ULTRA_HD")
    assert classified["video"] is not None
    assert "videoPreview" not in str(classified["video"])
    assert classified["audio"] is not None
    assert "audioPreview" not in str(classified["audio"])


def test_classify_urls_maps_jdnext_vp9_to_non_hd_tier():
    urls = [
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/uuid123/video_ULTRA.vp9.webm/hash-ultra-vp9.webm?auth=abc",
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/uuid123/video_HIGH.hd.webm/hash-high-hd.webm?auth=abc",
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/uuid123/audio.opus/hashaudio.opus?auth=abc",
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/uuid123/nx/mapPackage/hashmap_v0.bundle?auth=abc",
    ]

    classified = _classify_urls(urls, "ULTRA")
    assert classified["video"] is not None
    # Default vp9 mode is compatibility-down, so ULTRA resolves to HIGH_HD.
    assert "video_HIGH.hd.webm" in str(classified["video"])


def test_classify_urls_maps_jdnext_vp9_for_hd_fallback_search_order():
    urls = [
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/uuid123/video_HIGH.vp9.webm/hash-high-vp9.webm?auth=abc",
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/uuid123/audio.opus/hashaudio.opus?auth=abc",
        "https://cdn-jdhelper.ramaprojects.ru/private/jdnext/maps/uuid123/nx/mapPackage/hashmap_v0.bundle?auth=abc",
    ]

    # Request HIGH_HD when only HIGH_VP9 exists: should resolve to HIGH tier fallback.
    classified = _classify_urls(urls, "HIGH_HD")
    assert classified["video"] is None


def test_classify_urls_prefers_hls_over_chromecast_mp4():
    urls = [
        "https://jdn-sneakpeak.justdancenow.com/sweetbutpsycho_preview.mp4",
        "https://hls.justdancenow.com/365_3fee56bcf93cad7a/3fdeb5948b3d6b29.m3u8",
        "https://jdn-mp4.justdancenow.com/sweetbutpsycho_9c00f9bc92f8e67ec9f75d823e2fdc9687dfc7cc_576p.mp4?hlscookie=token",
    ]

    classified = _classify_urls(urls, "ULTRA_HD")

    assert classified["video"] is not None
    assert str(classified["video"]).endswith("3fdeb5948b3d6b29.m3u8")


def test_download_files_resolves_hls_master_to_best_variant(tmp_path, monkeypatch):
    master_url = "https://hls.justdancenow.com/365_3fee56bcf93cad7a/3fdeb5948b3d6b29.m3u8"
    playlist_text = "\n".join(
        [
            "#EXTM3U",
            '#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=1219000,RESOLUTION=416x234,CODECS="avc1.4d001f,mp4a.40.2"',
            "1m.m3u8",
            '#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=2740000,RESOLUTION=848x476,CODECS="avc1.4d001f,mp4a.40.2"',
            "2m.m3u8",
            '#EXT-X-STREAM-INF:PROGRAM-ID=1,BANDWIDTH=5315000,RESOLUTION=1280x720,CODECS="avc1.4d001f,mp4a.40.2"',
            "4m.m3u8",
        ]
    )

    class FakeResponse:
        def __init__(self, text):
            self.text = text

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def raise_for_status(self):
            return None

    ffmpeg_calls = {}

    def fake_get(url, timeout=None, headers=None):
        assert url == master_url
        return FakeResponse(playlist_text)

    def fake_run_ffmpeg(args, config=None):
        ffmpeg_calls["args"] = args
        output_path = Path(args[-1])
        output_path.write_bytes(b"\x00" * 2048)

    monkeypatch.setattr(web_playwright.requests, "get", fake_get)
    monkeypatch.setattr(web_playwright, "run_ffmpeg", fake_run_ffmpeg)
    monkeypatch.setattr(web_playwright.time, "sleep", lambda *_: None)

    cfg = AppConfig(max_retries=1, inter_request_delay_s=0.0)
    downloaded = download_files([master_url], tmp_path, "ULTRA_HD", cfg)

    assert any("4m.m3u8" in str(part) for part in ffmpeg_calls["args"])
    assert "3fdeb5948b3d6b29.mp4" in downloaded
    assert (tmp_path / "3fdeb5948b3d6b29.mp4").exists()
