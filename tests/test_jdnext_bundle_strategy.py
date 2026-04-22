from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from jd2021_installer.core.config import AppConfig
from jd2021_installer.extractors import jdnext_bundle_strategy as strategy_mod


def test_map_assetstudio_output_maps_expected_files(tmp_path: Path):
    raw = tmp_path / "raw"
    (raw / "MonoBehaviour").mkdir(parents=True)
    (raw / "TextAsset").mkdir(parents=True)
    (raw / "Texture2D").mkdir(parents=True)
    (raw / "Sprite").mkdir(parents=True)

    (raw / "MonoBehaviour" / "TestMap.json").write_text(
        json.dumps(
            {
                "MapName": "TestMap",
                "DanceData": {
                    "MotionClips": [
                        {
                            "StartTime": 0,
                            "Duration": 24,
                            "Id": 1,
                            "TrackId": 2,
                            "IsActive": 1,
                            "MoveName": "move_a",
                            "MoveType": 1,
                            "GoldMove": 0,
                            "CoachId": 0,
                            "Color": "",
                        },
                        {
                            "StartTime": 24,
                            "Duration": 24,
                            "Id": 3,
                            "TrackId": 4,
                            "IsActive": 1,
                            "MoveName": "move_b",
                            "MoveType": 0,
                            "GoldMove": 0,
                            "CoachId": 0,
                            "Color": "",
                        },
                    ],
                    "PictoClips": [
                        {
                            "StartTime": 0,
                            "Duration": 24,
                            "Id": 5,
                            "TrackId": 6,
                            "IsActive": 1,
                            "PictoPath": "picto_001",
                            "CoachCount": 1,
                        }
                    ],
                    "GoldEffectClips": [],
                },
                "KaraokeData": {
                    "Clips": [
                        {
                            "KaraokeClip": {
                                "StartTime": 0,
                                "Duration": 24,
                                "Id": 7,
                                "TrackId": 8,
                                "IsActive": 1,
                                "Lyrics": "Hi",
                                "Pitch": 1.0,
                                "IsEndOfLine": 1,
                            }
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (raw / "MonoBehaviour" / "MusicTrack.json").write_text(
        json.dumps(
            {
                "m_structure": {
                    "MusicTrackStructure": {
                        "startBeat": -4,
                        "endBeat": 100,
                        "videoStartTime": -2.5,
                        "previewEntry": 10.0,
                        "previewLoopStart": 10.0,
                        "previewLoopEnd": 40.0,
                        "markers": [{"VAL": 0}, {"VAL": 24000}],
                        "signatures": [{"MusicSignature": {"beats": 4, "marker": 1}}],
                        "sections": [{"MusicSection": {"sectionType": 0, "marker": 1}}],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (raw / "TextAsset" / "move_a.gesture").write_text("g", encoding="utf-8")
    (raw / "TextAsset" / "move_b.msm").write_text("m", encoding="utf-8")
    (raw / "TextAsset" / "scoringRules.txt").write_text("s", encoding="utf-8")
    (raw / "Texture2D" / "cover.png").write_bytes(b"PNG")
    (raw / "Sprite" / "picto_001.png").write_bytes(b"PNG")

    mapped = tmp_path / "mapped"
    summary = strategy_mod.map_assetstudio_output(raw, mapped, codename="TestMap")

    assert summary.map_json is not None
    assert summary.musictrack_json is not None
    assert summary.gestures == 1
    assert summary.msm == 1
    assert summary.pictos == 1
    assert summary.menuart == 1
    assert (mapped / "monobehaviour" / "map.json").exists()
    assert (mapped / "monobehaviour" / "musictrack.json").exists()
    assert (mapped / "testmap_musictrack.tpl.ckd").exists()
    assert (mapped / "testmap_tml_dance.dtape.ckd").exists()
    assert (mapped / "testmap_tml_karaoke.ktape.ckd").exists()
    assert (mapped / "timeline" / "moves" / "wiiu" / "move_a.gesture").exists()
    assert (mapped / "timeline" / "moves" / "wiiu" / "move_b.msm").exists()


def test_strategy_uses_assetstudio_first(tmp_path: Path, monkeypatch):
    bundle = tmp_path / "a.bundle"
    bundle.write_bytes(b"x")

    calls: list[str] = []

    def fake_assetstudio(bundle_path: Path, output_dir: Path, unity_version: str, **kwargs: Any):
        calls.append("assetstudio")
        (output_dir / "MonoBehaviour").mkdir(parents=True, exist_ok=True)
        (output_dir / "MonoBehaviour" / "TestMap.json").write_text("{}", encoding="utf-8")
        (output_dir / "MonoBehaviour" / "MusicTrack.json").write_text("{}", encoding="utf-8")
        (output_dir / "TextAsset").mkdir(parents=True, exist_ok=True)
        return output_dir

    def fake_unitypy(bundle_path: Path, output_dir: Path, **kwargs: Any):
        calls.append("unitypy")
        raise AssertionError("UnityPy should not run when AssetStudio succeeds first")

    monkeypatch.setattr(strategy_mod, "_run_assetstudio_export", fake_assetstudio)
    monkeypatch.setattr(strategy_mod, "_run_unitypy", fake_unitypy)

    out = tmp_path / "out"
    summary = strategy_mod.run_jdnext_bundle_strategy(bundle, out, strategy="assetstudio_first", codename="TestMap")

    assert summary.winner == "assetstudio"
    assert calls == ["assetstudio"]


def test_strategy_falls_back_to_assetstudio_when_unitypy_fails(tmp_path: Path, monkeypatch):
    bundle = tmp_path / "a.bundle"
    bundle.write_bytes(b"x")

    calls: list[str] = []

    def fake_assetstudio(bundle_path: Path, output_dir: Path, unity_version: str, **kwargs: Any):
        calls.append("assetstudio")
        (output_dir / "MonoBehaviour").mkdir(parents=True, exist_ok=True)
        (output_dir / "MonoBehaviour" / "RecoveredMap.json").write_text("{}", encoding="utf-8")
        (output_dir / "MonoBehaviour" / "MusicTrack.json").write_text("{}", encoding="utf-8")
        (output_dir / "TextAsset").mkdir(parents=True, exist_ok=True)
        return output_dir

    def fake_unitypy(bundle_path: Path, output_dir: Path, **kwargs: Any):
        calls.append("unitypy")
        raise RuntimeError("encrypted")

    monkeypatch.setattr(strategy_mod, "_run_assetstudio_export", fake_assetstudio)
    monkeypatch.setattr(strategy_mod, "_run_unitypy", fake_unitypy)

    out = tmp_path / "out"
    summary = strategy_mod.run_jdnext_bundle_strategy(bundle, out, strategy="unitypy_first")

    assert summary.winner == "assetstudio"
    assert calls == ["unitypy", "assetstudio"]


def test_synthesize_tape_normalizes_prefixed_or_suffixed_move_names(tmp_path: Path):
    mapped = tmp_path / "mapped"
    map_json = tmp_path / "Map.json"
    map_json.write_text(
        json.dumps(
            {
                "DanceData": {
                    "MotionClips": [
                        {
                            "StartTime": 0,
                            "Duration": 24,
                            "Id": 1,
                            "TrackId": 2,
                            "IsActive": 1,
                            "MoveName": "maps\\judas\\timeline\\moves\\judas_moto_1.gesture",
                            "MoveType": 1,
                        },
                        {
                            "StartTime": 24,
                            "Duration": 24,
                            "Id": 3,
                            "TrackId": 4,
                            "IsActive": 1,
                            "MoveName": "world/maps/judas/timeline/moves/judas_intro_judas_2.msm",
                            "MoveType": 0,
                        },
                    ]
                },
                "KaraokeData": {"Clips": []},
            }
        ),
        encoding="utf-8",
    )

    dance_ckd, _, _ = strategy_mod._synthesize_tapes_from_map_json(map_json, mapped, codename="Judas")

    assert dance_ckd is not None
    data = json.loads(dance_ckd.read_text(encoding="utf-8"))
    clips = data.get("Clips", [])
    classifier_paths = [c.get("ClassifierPath", "") for c in clips if c.get("__class") == "MotionClip"]

    assert "world/maps/judas/timeline/moves/judas_moto_1.gesture" in classifier_paths
    assert "world/maps/judas/timeline/moves/judas_intro_judas_2.msm" in classifier_paths
    assert all(".gesture.gesture" not in p for p in classifier_paths)
    assert all(".msm.msm" not in p for p in classifier_paths)


# ---------------------------------------------------------------------------
# Cross-platform AssetStudioModCLI execution tests
# ---------------------------------------------------------------------------


def _make_exists_replacement(
    positive_fragments: set[str],
) -> Any:
    """Return an unbound-method replacement for ``Path.exists``.

    Returns ``True`` when the path string contains any of the given
    *positive_fragments*; ``False`` otherwise.

    Used with ``patch.object(Path, 'exists', new=...)`` so the
    replacement receives ``self`` (the ``Path`` instance) as the first
    positional argument.
    """

    def _replacement(path_self: Path) -> bool:
        path_str = str(path_self)
        return any(frag in path_str for frag in positive_fragments)

    return _replacement


@patch(
    "jd2021_installer.extractors.jdnext_bundle_strategy.platform.system",
    return_value="Windows",
)
@patch(
    "jd2021_installer.extractors.jdnext_bundle_strategy.subprocess.run",
)
def test_assetstudio_execution_path_windows(
    mock_subprocess_run: MagicMock,
    mock_system: MagicMock,
    tmp_path: Path,
) -> None:
    """On Windows the subprocess command must invoke the ``.exe`` binary."""
    mock_subprocess_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    bundle: Path = tmp_path / "test.bundle"
    bundle.write_bytes(b"fake")
    output_dir: Path = tmp_path / "out"

    with patch.object(
        Path, "exists", new=_make_exists_replacement({"AssetStudioModCLI"}),
    ):
        strategy_mod._run_assetstudio_export(
            bundle,
            output_dir,
            unity_version="2021.3.9f1",
            config=AppConfig(),
        )

    mock_subprocess_run.assert_called_once()
    cmd: list[str] = mock_subprocess_run.call_args[0][0]
    assert cmd[0].endswith("AssetStudioModCLI.exe"), (
        f"Expected Windows .exe binary, got: {cmd[0]}"
    )


@patch(
    "jd2021_installer.extractors.jdnext_bundle_strategy.platform.system",
    return_value="Linux",
)
@patch(
    "jd2021_installer.extractors.jdnext_bundle_strategy.subprocess.run",
)
def test_assetstudio_execution_path_linux(
    mock_subprocess_run: MagicMock,
    mock_system: MagicMock,
    tmp_path: Path,
) -> None:
    """On Linux the subprocess command must invoke the extension-less binary."""
    mock_subprocess_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    bundle: Path = tmp_path / "test.bundle"
    bundle.write_bytes(b"fake")
    output_dir: Path = tmp_path / "out"

    with patch.object(
        Path, "exists", new=_make_exists_replacement({"AssetStudioModCLI"}),
    ):
        strategy_mod._run_assetstudio_export(
            bundle,
            output_dir,
            unity_version="2021.3.9f1",
            config=AppConfig(),
        )

    mock_subprocess_run.assert_called_once()
    cmd: list[str] = mock_subprocess_run.call_args[0][0]
    assert cmd[0].endswith("AssetStudioModCLI"), (
        f"Expected Linux binary without .exe, got: {cmd[0]}"
    )
    assert not cmd[0].endswith(".exe"), (
        f"Linux binary must NOT have .exe extension, got: {cmd[0]}"
    )

