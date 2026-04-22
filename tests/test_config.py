"""Tests for dynamic FFmpeg/FFprobe path resolution in AppConfig.

Verifies that ``model_post_init`` correctly discovers local tool binaries on
Windows, falls back to the bare ``"ffmpeg"`` / ``"ffprobe"`` commands when
the local files are absent, and skips the Windows-specific logic entirely
on Linux.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jd2021_installer.core.config import AppConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ORIGINAL_IS_FILE = Path.is_file


def _make_is_file_side_effect(
    positive_suffixes: set[str],
) -> Any:
    """Return a new-style ``Path.is_file`` replacement.

    When the path's string representation ends with any of the given
    *positive_suffixes*, returns ``True``; otherwise ``False``.

    Because ``patch`` replaces the descriptor on the *class*, the
    patched callable is invoked as an unbound function receiving ``self``
    (the ``Path`` instance) as the first positional argument.
    """

    def _replacement(path_self: Path) -> bool:
        path_str = str(path_self)
        return any(path_str.endswith(suffix) for suffix in positive_suffixes)

    return _replacement


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@patch("jd2021_installer.core.config.platform.system", return_value="Windows")
def test_config_resolves_local_ffmpeg_on_windows(
    mock_system: MagicMock,
) -> None:
    """On Windows, when local ffmpeg.exe and ffprobe.exe exist, AppConfig
    must override the default ``"ffmpeg"`` / ``"ffprobe"`` values with
    absolute paths pointing into the ``tools/`` directory."""
    with patch.object(
        Path,
        "is_file",
        new=_make_is_file_side_effect({"ffmpeg.exe", "ffprobe.exe"}),
    ):
        cfg: AppConfig = AppConfig()

    resolved_ffmpeg = Path(cfg.ffmpeg_path)
    resolved_ffprobe = Path(cfg.ffprobe_path)

    # Paths must be absolute (resolved) and point to the correct binaries.
    assert resolved_ffmpeg.is_absolute(), (
        f"Expected absolute path, got: {cfg.ffmpeg_path}"
    )
    assert resolved_ffprobe.is_absolute(), (
        f"Expected absolute path, got: {cfg.ffprobe_path}"
    )

    assert resolved_ffmpeg.name == "ffmpeg.exe"
    assert resolved_ffprobe.name == "ffprobe.exe"

    # The parent folder must be ``ffmpeg/bin`` inside the tools root.
    assert resolved_ffmpeg.parent.name == "bin"
    assert resolved_ffprobe.parent.name == "bin"
    assert resolved_ffmpeg.parent.parent.name == "ffmpeg"
    assert resolved_ffprobe.parent.parent.name == "ffmpeg"


@patch("jd2021_installer.core.config.platform.system", return_value="Windows")
def test_config_falls_back_to_system_ffmpeg_if_local_missing(
    mock_system: MagicMock,
) -> None:
    """On Windows, when no local binaries exist in ``tools/``, AppConfig
    must leave the paths at their bare-command defaults so that the system
    ``PATH`` is used at runtime."""
    with patch.object(Path, "is_file", new=lambda self: False):
        cfg: AppConfig = AppConfig()

    assert cfg.ffmpeg_path == "ffmpeg"
    assert cfg.ffprobe_path == "ffprobe"


@patch("jd2021_installer.core.config.platform.system", return_value="Linux")
def test_config_skips_local_resolution_on_linux(
    mock_system: MagicMock,
) -> None:
    """On Linux, the Windows-specific local resolution must be completely
    bypassed, leaving the paths as bare commands regardless of whether
    ``tools/`` contains any files."""
    cfg: AppConfig = AppConfig()

    assert cfg.ffmpeg_path == "ffmpeg"
    assert cfg.ffprobe_path == "ffprobe"
