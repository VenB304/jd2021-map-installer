"""Test suite for the Normalizer pipeline.

The critical assertion: given the same CKD content, the normalizer must
produce the exact same NormalizedMapData whether the source is an
HTML-downloaded directory or an extracted IPK archive.
"""

from __future__ import annotations

from pathlib import Path

from jd2021_installer.core.models import NormalizedMapData
from jd2021_installer.parsers.normalizer import _discover_media, load_ckd, normalize


class TestNormalizerParity:
    """Ensure HTML and IPK sources produce identical NormalizedMapData."""

    def test_html_source_produces_valid_data(self, mock_html_dir: Path) -> None:
        """Normalize an HTML-downloaded directory successfully."""
        result = normalize(mock_html_dir)
        assert isinstance(result, NormalizedMapData)
        assert result.codename == "TestMap"
        assert result.song_desc.title == "Test Song"
        assert result.song_desc.artist == "Test Artist"
        assert len(result.music_track.markers) == 5

    def test_ipk_source_produces_valid_data(self, mock_ipk_dir: Path) -> None:
        """Normalize an IPK-extracted directory successfully."""
        result = normalize(mock_ipk_dir)
        assert isinstance(result, NormalizedMapData)
        assert result.codename == "TestMap"
        assert result.song_desc.title == "Test Song"
        assert len(result.music_track.markers) == 5

    def test_html_and_ipk_produce_same_output(
        self, mock_html_dir: Path, mock_ipk_dir: Path
    ) -> None:
        """THE critical parity test: both sources yield identical data."""
        html_result = normalize(mock_html_dir)
        ipk_result = normalize(mock_ipk_dir)

        assert html_result.codename == ipk_result.codename
        assert html_result.song_desc.title == ipk_result.song_desc.title
        assert html_result.song_desc.artist == ipk_result.song_desc.artist
        assert html_result.song_desc.num_coach == ipk_result.song_desc.num_coach
        assert html_result.music_track.markers == ipk_result.music_track.markers
        assert html_result.music_track.start_beat == ipk_result.music_track.start_beat
        assert html_result.music_track.end_beat == ipk_result.music_track.end_beat
        assert html_result.music_track.video_start_time == ipk_result.music_track.video_start_time


class TestNormalizerEdgeCases:
    """Edge case handling in the normalizer."""

    def test_missing_songdesc_uses_defaults(self, tmp_path: Path) -> None:
        """When songdesc CKD is missing, use reasonable defaults."""
        import json
        mt_data = {
            "COMPONENTS": [{
                "trackData": {
                    "structure": {
                        "markers": [0, 2400, 4800],
                        "signatures": [{"beats": 4, "marker": 0}],
                        "sections": [{"sectionType": 0, "marker": 0}],
                        "startBeat": 0,
                        "endBeat": 2,
                        "videoStartTime": 0.0,
                        "volume": 0.0,
                    }
                }
            }]
        }
        (tmp_path / "Test_musictrack.tpl.ckd").write_text(json.dumps(mt_data))

        result = normalize(tmp_path)
        assert result.song_desc.artist == "Unknown Artist"

    def test_synthesizes_empty_musictrack_if_missing(self, tmp_path: Path) -> None:
        """Normalizer must synthesize an empty music track if no CKD exists."""
        (tmp_path / "dummy.txt").write_text("not a ckd")
        result = normalize(tmp_path)
        assert result.music_track is not None
        assert result.music_track.markers == []

    def test_supplemental_roots_fill_missing_ckds(self, tmp_path: Path) -> None:
        import json

        primary = tmp_path / "primary"
        supplemental = tmp_path / "supplemental"
        primary.mkdir(parents=True)
        supplemental.mkdir(parents=True)

        mt_data = {
            "COMPONENTS": [{
                "trackData": {
                    "structure": {
                        "markers": [0, 2400, 4800],
                        "signatures": [{"beats": 4, "marker": 0}],
                        "sections": [{"sectionType": 0, "marker": 0}],
                        "startBeat": 0,
                        "endBeat": 2,
                        "videoStartTime": 0.0,
                        "volume": 0.0,
                    }
                }
            }]
        }
        sd_data = {
            "COMPONENTS": [{
                "MapName": "TestMap",
                "Title": "Bundle Title",
                "Artist": "Bundle Artist",
                "NumCoach": 1,
            }]
        }

        (supplemental / "TestMap_musictrack.tpl.ckd").write_text(json.dumps(mt_data))
        (supplemental / "TestMap_songdesc.tpl.ckd").write_text(json.dumps(sd_data))

        result = normalize(primary, codename="TestMap", supplemental_roots=[supplemental])
        assert result.song_desc.title == "Bundle Title"
        assert result.song_desc.artist == "Bundle Artist"
        assert result.music_track.markers == [0, 2400, 4800]

    def test_audio_discovery_uses_search_root_for_ipk_sidecar(self, tmp_path: Path) -> None:
        """V1 parity: audio beside the IPK source folder must be discoverable."""
        extracted_dir = tmp_path / "temp_extraction"
        extracted_dir.mkdir(parents=True)

        ipk_source_dir = tmp_path / "ipk_source"
        ipk_source_dir.mkdir(parents=True)
        sidecar_audio = ipk_source_dir / "judas.wav"
        sidecar_audio.write_bytes(b"RIFFfake")

        media = _discover_media(
            str(extracted_dir),
            codename="judas",
            search_roots=[str(ipk_source_dir)],
        )

        assert media.audio_path == sidecar_audio

    def test_audio_discovery_keeps_ckd_path_when_decode_fails(self, tmp_path: Path, monkeypatch) -> None:
        """If CKD decode fails during discovery, keep source path for install-time retry."""
        from jd2021_installer.installers import media_processor as media_processor_mod

        ckd_audio = tmp_path / "sweetbutpsycho.wav.ckd"
        ckd_audio.write_bytes(b"x" * 128)

        monkeypatch.setattr(media_processor_mod, "extract_ckd_audio_v1", lambda *_args, **_kwargs: None)

        media = _discover_media(str(tmp_path), codename="sweetbutpsycho")

        assert media.audio_path == ckd_audio

    def test_normalize_uses_ckd_stem_alias_when_codename_differs(self, tmp_path: Path) -> None:
        """JDNext map.json codename may differ from CKD filename stems."""
        import json

        mt_data = {
            "COMPONENTS": [{
                "trackData": {
                    "structure": {
                        "markers": [0, 2400, 4800],
                        "signatures": [{"beats": 4, "marker": 0}],
                        "sections": [{"sectionType": 0, "marker": 0}],
                        "startBeat": 0,
                        "endBeat": 2,
                        "videoStartTime": 0.0,
                    }
                }
            }]
        }

        (tmp_path / "pigstepjustdancexminecraftversion_musictrack.tpl.ckd").write_text(
            json.dumps(mt_data),
            encoding="utf-8",
        )

        map_json = tmp_path / "monobehaviour" / "map.json"
        map_json.parent.mkdir(parents=True, exist_ok=True)
        map_json.write_text(
            json.dumps(
                {
                    "MapName": "Jukebox",
                    "SongDesc": {
                        "MapName": "Jukebox",
                        "Title": "Jukebox",
                        "Artist": "Artist",
                        "NumCoach": 1,
                    },
                }
            ),
            encoding="utf-8",
        )

        result = normalize(tmp_path, codename="Jukebox")

        assert result.codename == "Jukebox"
        assert result.music_track is not None
        assert len(result.music_track.markers) == 3

    def test_autodance_stub_files_do_not_enable_autodance(self, tmp_path: Path) -> None:
        import json

        mt_data = {
            "COMPONENTS": [{
                "trackData": {
                    "structure": {
                        "markers": [0, 2400, 4800],
                        "signatures": [{"beats": 4, "marker": 0}],
                        "sections": [{"sectionType": 0, "marker": 0}],
                        "startBeat": 0,
                        "endBeat": 2,
                        "videoStartTime": 0.0,
                    }
                }
            }]
        }
        sd_data = {
            "COMPONENTS": [{
                "MapName": "NoAdMap",
                "Title": "No AD",
                "Artist": "Artist",
                "NumCoach": 1,
            }]
        }
        (tmp_path / "NoAdMap_musictrack.tpl.ckd").write_text(json.dumps(mt_data), encoding="utf-8")
        (tmp_path / "NoAdMap_songdesc.tpl.ckd").write_text(json.dumps(sd_data), encoding="utf-8")
        (tmp_path / "NoAdMap.adtape.ckd").write_bytes(b"{}")

        result = normalize(tmp_path, codename="NoAdMap")
        assert result.has_autodance is False

    def test_load_ckd_trailing_junk_falls_back_to_binary(self, tmp_path: Path) -> None:
        ckd_path = tmp_path / "legacy_musictrack.ckd"
        ckd_path.write_bytes(b'{"COMPONENTS": []}TRAILING_BINARY')

        from unittest.mock import patch

        with patch(
            "jd2021_installer.parsers.normalizer.parse_binary_ckd",
            return_value={"parsed": "binary"},
        ) as mock_binary:
            result = load_ckd(ckd_path)

        assert result == {"parsed": "binary"}
        mock_binary.assert_called_once()


class TestNormalizerMusicTrack:
    """Detailed music track parsing tests."""

    def test_markers_are_integers(self, mock_html_dir: Path) -> None:
        result = normalize(mock_html_dir)
        for m in result.music_track.markers:
            assert isinstance(m, int)

    def test_video_start_time_parsed(self, mock_html_dir: Path) -> None:
        result = normalize(mock_html_dir)
        assert result.music_track.video_start_time == -1.5

    def test_video_start_time_synthesis(self) -> None:
        """Verify the marker-based sync offset synthesis for IPK sources (NO 85ms offset)."""
        from jd2021_installer.parsers.normalizer import _extract_music_track, MusicTrackStructure
        import unittest.mock
        
        # Return a real MusicTrackStructure to bypass instance checks
        mock_track = MusicTrackStructure(
            start_beat=-4,
            markers=[0, 50, 100, 150, 200, 250],
            video_start_time=0.0
        )
        
        with unittest.mock.patch("jd2021_installer.parsers.normalizer.load_ckd", return_value=mock_track), \
             unittest.mock.patch("jd2021_installer.parsers.normalizer._find_ckd_files", return_value=["dummy.ckd"]):
            res = _extract_music_track("dummy_dir", "test_map")
            
            # VST = -(marker[4] / 48 / 1000)  [NO 85ms offset for video]
            # = -(200 / 48000) = -0.004166...
            expected = -(200 / 48.0 / 1000.0)
            assert abs(res.video_start_time - expected) < 0.0001


    def test_html_source_uses_marker_audio_without_additional_calibration(self):
        """HTML sources use marker-derived audio offset while preserving non-zero metadata video offset."""
        from jd2021_installer.parsers.normalizer import normalize_sync
        from jd2021_installer.core.models import MusicTrackStructure
        import unittest
        
        # Scenario: metadata has -2.658s, but markers indicate -1.139s
        # (Marker at beat 4 = 54687 ticks -> 1139.3 ms)
        mt = MusicTrackStructure(
            markers=[0, 10000, 20000, 40000, 54687],
            start_beat=-4,
            video_start_time=-2.658
        )
        
        sync = normalize_sync(mt, is_html_source=True)
        
        # Expected:
        # prms = 54687 / 48 = 1139.3125
        # video_ms preserves metadata = -2658.0
        # audio_ms = -(1139.3125)
        case = unittest.TestCase()
        case.assertAlmostEqual(sync.video_ms, -2658.0, places=3)
        case.assertAlmostEqual(sync.audio_ms, -1139.3125, places=3)

    def test_html_jdnext_source_uses_marker_audio_without_additional_calibration(self):
        """JDNext HTML sources should use marker-derived audio offset."""
        from jd2021_installer.parsers.normalizer import normalize_sync
        from jd2021_installer.core.models import MusicTrackStructure
        import unittest

        mt = MusicTrackStructure(
            markers=[0, 10000, 20000, 40000, 54687],
            start_beat=-4,
            video_start_time=-2.658,
        )

        sync = normalize_sync(mt, is_html_source=True, is_jdnext_source=True)

        case = unittest.TestCase()
        case.assertAlmostEqual(sync.video_ms, -2658.0, places=3)
        case.assertAlmostEqual(sync.audio_ms, -1139.3125, places=3)

    def test_ipk_source_preserves_vst(self):
        """Metadata vst should be preserved for IPK sources if non-zero."""
        from jd2021_installer.parsers.normalizer import normalize_sync
        from jd2021_installer.core.models import MusicTrackStructure
        
        mt = MusicTrackStructure(
            markers=[0, 10000, 20000, 40000, 54687],
            start_beat=-4,
            video_start_time=-2.658
        )
        
        sync = normalize_sync(mt, is_html_source=False)
        
        assert sync.video_ms == -2658.0
        assert sync.audio_ms == 0.0

    def test_preview_loop_values_are_merged_from_source_trk(self, tmp_path: Path) -> None:
        """Preview loop fields should come from source .trk when CKD leaves them at zero."""
        import json

        mt_data = {
            "COMPONENTS": [{
                "trackData": {
                    "structure": {
                        "markers": [0, 2400, 4800, 7200],
                        "signatures": [{"beats": 4, "marker": 0}],
                        "sections": [{"sectionType": 0, "marker": 0}],
                        "startBeat": 0,
                        "endBeat": 3,
                        "videoStartTime": 0.0,
                        "previewEntry": 0,
                        "previewLoopStart": 0,
                        "previewLoopEnd": 0,
                        "volume": 0.0,
                    }
                }
            }]
        }

        (tmp_path / "TestMap_musictrack.tpl.ckd").write_text(json.dumps(mt_data), encoding="utf-8")
        audio_dir = tmp_path / "Audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        (audio_dir / "TestMap.trk").write_text(
            "videoStartTime = 0.000000, previewEntry = 287, previewLoopStart = 287, previewLoopEnd = 574",
            encoding="utf-8",
        )

        result = normalize(tmp_path, codename="TestMap")

        assert result.music_track.preview_entry == 287
        assert result.music_track.preview_loop_start == 287
        assert result.music_track.preview_loop_end == 574

    def test_missing_songdesc_uses_assets_html_metadata(self, tmp_path: Path) -> None:
        import json

        mt_data = {
            "COMPONENTS": [{
                "trackData": {
                    "structure": {
                        "markers": [0, 2400, 4800],
                        "signatures": [{"beats": 4, "marker": 0}],
                        "sections": [{"sectionType": 0, "marker": 0}],
                        "startBeat": 0,
                        "endBeat": 2,
                        "videoStartTime": 0.0,
                    }
                }
            }]
        }
        (tmp_path / "AQueda_musictrack.tpl.ckd").write_text(json.dumps(mt_data), encoding="utf-8")
        (tmp_path / "assets.html").write_text(
            '<div class="embedTitle__x"><span>A QUEDA</span></div>'
            '<div class="embedDescription__x"><span>by Gloria Groove</span></div>',
            encoding="utf-8",
        )

        result = normalize(tmp_path, codename="AQueda")

        assert result.song_desc.title == "A QUEDA"
        assert result.song_desc.artist == "Gloria Groove"

    def test_missing_songdesc_uses_map_json_songdesc_fallback(self, tmp_path: Path) -> None:
        import json

        mt_data = {
            "COMPONENTS": [{
                "trackData": {
                    "structure": {
                        "markers": [0, 2400, 4800],
                        "signatures": [{"beats": 4, "marker": 0}],
                        "sections": [{"sectionType": 0, "marker": 0}],
                        "startBeat": 0,
                        "endBeat": 2,
                        "videoStartTime": 0.0,
                    }
                }
            }]
        }
        map_json = {
            "MapName": "SweetButPsycho",
            "SongDesc": {
                "MapName": "SweetButPsycho",
                "JDVersion": 2024,
                "OriginalJDVersion": 2024,
                "Artist": "Ava Max",
                "DancerName": "Coach",
                "Title": "Sweet but Psycho",
                "Credits": "Test Credits",
                "NumCoach": 1,
                "MainCoach": 0,
                "Difficulty": 3,
                "SweatDifficulty": 2,
            },
        }

        (tmp_path / "SweetButPsycho_musictrack.tpl.ckd").write_text(json.dumps(mt_data), encoding="utf-8")
        mono = tmp_path / "monobehaviour"
        mono.mkdir(parents=True, exist_ok=True)
        (mono / "map.json").write_text(json.dumps(map_json), encoding="utf-8")

        result = normalize(tmp_path, codename="SweetButPsycho")

        assert result.song_desc.title == "Sweet but Psycho"
        assert result.song_desc.artist == "Ava Max"
        assert result.song_desc.credits == "Test Credits"
        assert result.song_desc.main_coach == 0
        assert result.song_desc.difficulty == 3
        assert result.song_desc.sweat_difficulty == 2
        assert result.song_desc.jd_version == 2024
        assert result.song_desc.original_jd_version == 2024

    def test_map_json_songdesc_blank_text_fields_use_assets_html(self, tmp_path: Path) -> None:
        import json

        mt_data = {
            "COMPONENTS": [{
                "trackData": {
                    "structure": {
                        "markers": [0, 2400, 4800],
                        "signatures": [{"beats": 4, "marker": 0}],
                        "sections": [{"sectionType": 0, "marker": 0}],
                        "startBeat": 0,
                        "endBeat": 2,
                        "videoStartTime": 0.0,
                    }
                }
            }]
        }
        map_json = {
            "MapName": "AQueda",
            "SongDesc": {
                "MapName": "AQueda",
                "Artist": "",
                "Title": "",
                "NumCoach": 1,
                "MainCoach": 0,
                "Difficulty": 4,
                "SweatDifficulty": 3,
            },
        }

        (tmp_path / "AQueda_musictrack.tpl.ckd").write_text(json.dumps(mt_data), encoding="utf-8")
        mono = tmp_path / "monobehaviour"
        mono.mkdir(parents=True, exist_ok=True)
        (mono / "map.json").write_text(json.dumps(map_json), encoding="utf-8")
        (tmp_path / "assets.html").write_text(
            '<div class="embedTitle__x"><span>A QUEDA</span></div>'
            '<div class="embedDescription__x"><span>by Gloria Groove</span></div>',
            encoding="utf-8",
        )

        result = normalize(tmp_path, codename="AQueda")

        assert result.song_desc.title == "A QUEDA"
        assert result.song_desc.artist == "Gloria Groove"
        assert result.song_desc.difficulty == 4
        assert result.song_desc.sweat_difficulty == 3

    def test_jdnext_metadata_json_overlays_jd2021_relevant_fields(self, tmp_path: Path) -> None:
        import json

        mt_data = {
            "COMPONENTS": [{
                "trackData": {
                    "structure": {
                        "markers": [0, 2400, 4800],
                        "signatures": [{"beats": 4, "marker": 0}],
                        "sections": [{"sectionType": 0, "marker": 0}],
                        "startBeat": 0,
                        "endBeat": 2,
                        "videoStartTime": 0.0,
                    }
                }
            }]
        }
        map_json = {
            "MapName": "SweetButPsycho",
            "SongDesc": {
                "MapName": "SweetButPsycho",
                "Artist": "Ava Max",
                "Title": "Sweet but Psycho",
                "NumCoach": 1,
                "MainCoach": 0,
                "Difficulty": 2,
                "SweatDifficulty": 1,
                "OriginalJDVersion": 2021,
                "Credits": "",
            },
        }
        jdnext_metadata = {
            "tags": ["Pop", "Main"],
            "credits": "License holder credits",
            "other_info": {
                "difficulty": "Extreme",
                "sweat_difficulty": "Hard",
                "coach_count": "4",
                "original_jd_version": "2023",
                "camera_support": True,
            },
        }

        (tmp_path / "SweetButPsycho_musictrack.tpl.ckd").write_text(json.dumps(mt_data), encoding="utf-8")
        mono = tmp_path / "monobehaviour"
        mono.mkdir(parents=True, exist_ok=True)
        (mono / "map.json").write_text(json.dumps(map_json), encoding="utf-8")
        (tmp_path / "jdnext_metadata.json").write_text(json.dumps(jdnext_metadata), encoding="utf-8")

        result = normalize(tmp_path, codename="SweetButPsycho")

        assert result.song_desc.tags == ["Pop", "Main"]
        assert result.song_desc.credits == "License holder credits"
        assert result.song_desc.difficulty == 4
        assert result.song_desc.sweat_difficulty == 3
        assert result.song_desc.num_coach == 4
        assert result.song_desc.original_jd_version == 2023

    def test_jdnext_songdb_cache_overlays_placeholder_original_version(self, tmp_path: Path, monkeypatch) -> None:
        import json

        mt_data = {
            "COMPONENTS": [{
                "trackData": {
                    "structure": {
                        "markers": [0, 2400, 4800],
                        "signatures": [{"beats": 4, "marker": 0}],
                        "sections": [{"sectionType": 0, "marker": 0}],
                        "startBeat": 0,
                        "endBeat": 2,
                        "videoStartTime": 0.0,
                    }
                }
            }]
        }
        map_json = {
            "MapName": "Judas",
            "SongDesc": {
                "MapName": "Judas",
                "Artist": "Unknown Artist",
                "DancerName": "Unknown Dancer",
                "Title": "Unknown Title",
                "Credits": "Empty Credits",
                "NumCoach": 1,
                "MainCoach": 0,
                "Difficulty": 2,
                "SweatDifficulty": 1,
                "OriginalJDVersion": -1,
            },
        }
        songdb = {
            "schema_version": 1,
            "index": {
                "judas": {
                    "entry_id": "998b2cd2-02ff-4c73-8aac-54065aebee36",
                    "map_name": "Judas",
                    "parent_map_name": "Judas",
                    "title": "Judas",
                    "artist": "Lady Gaga",
                    "credits": "Licensed Credits",
                    "tags": ["Main", "jdplus"],
                    "difficulty": 2,
                    "sweat_difficulty": 1,
                    "coach_count": 1,
                    "original_jd_version": 2022,
                    "status": None,
                    "lyrics_color": "#FFA416FF",
                    "preview_entry": None,
                    "preview_loop_start": None,
                    "preview_loop_end": None,
                    "video_start_time": None,
                }
            },
        }

        (tmp_path / "Judas_musictrack.tpl.ckd").write_text(json.dumps(mt_data), encoding="utf-8")
        mono = tmp_path / "monobehaviour"
        mono.mkdir(parents=True, exist_ok=True)
        (mono / "map.json").write_text(json.dumps(map_json), encoding="utf-8")
        synth_path = tmp_path / "jdnext_songdb_synth.json"
        synth_path.write_text(json.dumps(songdb), encoding="utf-8")

        monkeypatch.setattr("jd2021_installer.parsers.normalizer.resolve_songdb_synth_path", lambda: synth_path)

        result = normalize(tmp_path, codename="Judas")

        assert result.song_desc.title == "Judas"
        assert result.song_desc.artist == "Lady Gaga"
        assert result.song_desc.credits == "Licensed Credits"
        assert result.song_desc.original_jd_version == 2022

    def test_synthesized_tapes_with_top_level_clips_are_counted(self, tmp_path: Path) -> None:
        import json

        mt_data = {
            "COMPONENTS": [{
                "trackData": {
                    "structure": {
                        "markers": [0, 2400, 4800],
                        "signatures": [{"beats": 4, "marker": 0}],
                        "sections": [{"sectionType": 0, "marker": 0}],
                        "startBeat": 0,
                        "endBeat": 2,
                        "videoStartTime": 0.0,
                    }
                }
            }]
        }
        sd_data = {
            "COMPONENTS": [{
                "MapName": "AQueda",
                "Title": "A Queda",
                "Artist": "Gloria Groove",
                "NumCoach": 1,
            }]
        }
        dtape_data = {
            "__class": "Tape",
            "Clips": [
                {"__class": "MotionClip", "StartTime": 0, "Duration": 10},
                {"__class": "PictogramClip", "StartTime": 20, "Duration": 10},
            ],
        }
        ktape_data = {
            "__class": "Tape",
            "Clips": [
                {"__class": "KaraokeClip", "StartTime": 0, "Duration": 10, "Lyrics": "a"},
            ],
        }

        (tmp_path / "aqueda_musictrack.tpl.ckd").write_text(json.dumps(mt_data), encoding="utf-8")
        (tmp_path / "aqueda_songdesc.tpl.ckd").write_text(json.dumps(sd_data), encoding="utf-8")
        (tmp_path / "aqueda_tml_dance.dtape.ckd").write_text(json.dumps(dtape_data), encoding="utf-8")
        (tmp_path / "aqueda_tml_karaoke.ktape.ckd").write_text(json.dumps(ktape_data), encoding="utf-8")

        result = normalize(tmp_path, codename="AQueda")

        assert result.dance_tape is not None
        assert result.karaoke_tape is not None
        assert len(result.dance_tape.clips) == 2
        assert len(result.karaoke_tape.clips) == 1
