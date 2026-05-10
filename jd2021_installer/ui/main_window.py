"""Main window for the JD2021 Map Installer — PyQt6 GUI.

Composes modular widgets from ``ui/widgets/`` and acts as the central
controller, wiring user-facing signals to backend ``QObject`` workers
running on dedicated ``QThread`` instances.

Layout
------
::

    ┌───────────────── QSplitter ────────────────────┐
    │  Top: Install Panel (Mode, Config, Action)     │
    ├───────────────── QSplitter ────────────────────┤
    │  Left: Progress           │  Right: Preview    │
    ├───────────────── QSplitter ────────────────────┤
    │  Bottom: Log Console      │  Sync Refinement   │
    └───────────────────────────┴────────────────────┘
    [                 QProgressBar (status bar)       ]
"""

from __future__ import annotations

import logging
import importlib.util
import re
import shutil
import sys
import json
import subprocess
import os
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QThread, QTimer
from PyQt6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QSplitter,
    QStatusBar,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
    QLabel,
    QDialog,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QAbstractItemView,
    QFileDialog,
    QProgressDialog,
    QSizePolicy,
)

from jd2021_installer.core.config import AppConfig
from jd2021_installer.core.logging_config import apply_log_detail, get_file_log_level
from jd2021_installer.core.install_summary import InstallSummary, build_install_summary, render_install_summary
from jd2021_installer.core.theme import load_theme_stylesheet, resolve_theme_stylesheet_path
from jd2021_installer.core.models import (
    MapMedia,
    MapSync,
    MusicTrackStructure,
    NormalizedMapData,
    SongDescription,
)
from jd2021_installer.core.readjust_index import (
    ReadjustIndexEntry,
    load_index,
    prune_stale_entries,
    read_video_start_time_from_trk,
    update_offsets,
    upsert_entry,
)
from jd2021_installer.installers.sku_scene import list_registered_maps
from jd2021_installer.ui.widgets import (
    ActionWidget,
    ConfigWidget,
    ModeSelectorWidget,
    PreviewWidget,
    ProgressLogWidget,
    StepStatus,
    SyncRefinementWidget,
    LogConsoleWidget,
)
from jd2021_installer.ui.widgets.log_console import SUCCESS_LEVEL
from jd2021_installer.ui.workers.media_workers import (
    SyncRefinementWorker,
)
from jd2021_installer.ui.workers.pipeline_workers import (
    ExtractAndNormalizeWorker,
    InstallMapWorker,
    ApplyAndFinishWorker,
    ApplyOffsetsBatchWorker,
    ApplyReadjustOffsetsBatchWorker,
    UninstallBatchResult,
    UninstallMapsWorker,
)

logger = logging.getLogger("jd2021.ui.main_window")

_SETTINGS_CHANGE_LABELS: dict[str, str] = {
    "skip_preflight": "Skip pre-flight checks",
    "suppress_offset_notification": "Offset reminder",
    "cleanup_behavior": "After install cleanup",
    "locked_status_behavior": "Song unlock status",
    "show_preflight_success_popup": "Pre-flight success popup",
    "show_install_summary_popup": "Install summary popup",
    "show_quickstart_on_launch": "Quick-start on launch",
    "albumcoach_behavior": "AlbumCoach compositing",
    "log_detail_level": "Log detail level",
    "theme": "Theme",
    "enforce_min_window_size": "Enforce minimum window size",
    "min_window_width": "Minimum window width",
    "min_window_height": "Minimum window height",
    "show_window_size_overlay": "Window size overlay",
    "style_debug_mode": "Style debug mode",
    "video_quality": "Default download quality",
    "ffmpeg_hwaccel": "FFmpeg acceleration",
    "vp9_handling_mode": "VP9 handling",
    "preview_video_mode": "Preview source",
    "discord_channel_url": "Discord channel URL",
    "download_timeout_s": "Download timeout",
    "max_retries": "Download retries",
    "retry_base_delay_s": "Retry base delay",
    "inter_request_delay_s": "Inter-request delay",
    "fetch_login_timeout_s": "Fetch login timeout",
    "fetch_bot_response_timeout_s": "Fetch bot response timeout",
    "window_size_overlay_timeout_ms": "Window size overlay timeout",
    "preview_fps": "Preview FPS",
    "preview_startup_compensation_ms": "Preview startup compensation",
    "preview_only_audio_offset_ms": "Audio-only preview offset",
    "audio_preview_fade_s": "Audio preview fade",
    "ffmpeg_path": "FFmpeg executable",
    "ffprobe_path": "FFprobe executable",
    "vgmstream_path": "vgmstream executable",
    "third_party_tools_root": "3rd-party tools root",
    "assetstudio_cli_path": "AssetStudio CLI",
    "check_updates_on_launch": "Check updates on launch",
    "update_branch": "Update branch",
}

_SETTINGS_CHANGE_ORDER: tuple[str, ...] = (
    "skip_preflight",
    "suppress_offset_notification",
    "cleanup_behavior",
    "locked_status_behavior",
    "show_preflight_success_popup",
    "show_install_summary_popup",
    "show_quickstart_on_launch",
    "albumcoach_behavior",
    "log_detail_level",
    "theme",
    "enforce_min_window_size",
    "min_window_width",
    "min_window_height",
    "show_window_size_overlay",
    "style_debug_mode",
    "video_quality",
    "ffmpeg_hwaccel",
    "vp9_handling_mode",
    "preview_video_mode",
    "discord_channel_url",
    "download_timeout_s",
    "max_retries",
    "retry_base_delay_s",
    "inter_request_delay_s",
    "fetch_login_timeout_s",
    "fetch_bot_response_timeout_s",
    "window_size_overlay_timeout_ms",
    "preview_fps",
    "preview_startup_compensation_ms",
    "preview_only_audio_offset_ms",
    "audio_preview_fade_s",
    "ffmpeg_path",
    "ffprobe_path",
    "vgmstream_path",
    "third_party_tools_root",
    "assetstudio_cli_path",
    "check_updates_on_launch",
    "update_branch",
)

_READY_STATUS_VALUE = 3
# Editable status meaning labels live in code (not installer_settings.json).
# Add or change entries here as new statuses are understood.
_SONG_STATUS_MEANINGS: dict[int, str] = {
    1: "Hidden",
    2: "MojoLocked",
    3: "Available",
    4: "RedeemLocked",
    5: "UplayLocked",
    9: "GachaLocked",
    10: "StarLocked",
    11: "DLCLocked",
    12: "ObjectiveLocked",
    13: "AnthologyLocked",
}

# Granular checklist steps (V1 Parity)
PIPELINE_STEPS = [
    "Extracting map data...",
    "Parsing CKDs and metadata...",
    "Normalizing assets...",
    "Decoding XMA2 audio...",
    "Converting audio (pad/trim)...",
    "Generating intro AMB...",
    "Copying video files...",
    "Converting dance tapes...",
    "Converting karaoke tapes...",
    "Converting cinematic tapes...",
    "Processing ambient sounds...",
    "Decoding MenuArt textures...",
    "Decoding pictograms...",
    "Integrating move data...",
    "Registering in SkuScene...",
    "Finalizing offsets...",
]

# Runtime dependencies required for end-user operation.
# key: pip package name, value: import module name
RUNTIME_DEPENDENCIES = {
    "pydantic": "pydantic",
    "requests": "requests",
    "Pillow": "PIL",
    "playwright": "playwright",
}


class MainWindow(QMainWindow):
    """Primary application window — orchestrates widgets and workers."""

    def __init__(self) -> None:
        super().__init__()

        # -- Application state ------------------------------------------------
        self._config = self._load_settings()
        self._config.log_detail_level = apply_log_detail(self._config.log_detail_level)
        self._current_map: Optional[NormalizedMapData] = None
        self._current_target: Optional[str] = None
        self._current_mode: str = "Fetch JDU"

        self._active_threads: set[QThread] = set()
        self._active_worker: Optional[object] = None
        self._startup_update_thread: Optional[QThread] = None
        self._startup_update_worker: Optional[object] = None
        self._startup_update_completed: bool = False
        self._file_logger_handlers: list[logging.Handler] = []
        self._preview_audio_warning_shown = False
        self._size_overlay: Optional[QLabel] = None
        self._preview_hint_label: Optional[QLabel] = None
        self._sync_hint_label: Optional[QLabel] = None
        self._log_hint_label: Optional[QLabel] = None
        self._active_stylesheet_path: Optional[Path] = None
        self._active_stylesheet_mtime: Optional[float] = None
        self._size_overlay_hide_timer = QTimer(self)
        self._size_overlay_hide_timer.setSingleShot(True)
        self._size_overlay_hide_timer.timeout.connect(self._hide_size_overlay)
        self._style_reload_timer = QTimer(self)
        self._style_reload_timer.setInterval(350)
        self._style_reload_timer.timeout.connect(self._poll_stylesheet_changes)

        # Phase 4: Multi-map navigation
        self._nav_maps: list[NormalizedMapData] = []
        self._nav_index: int = 0
        self._pending_offsets: dict[str, tuple[float, float]] = {}
        self._readjust_pending_updates: list[tuple[str, float, float]] = []
        self._install_started_at: Optional[float] = None
        self._completed_install_maps: list[NormalizedMapData] = []
        self._quickstart_shown_this_session = False

        # -- Window setup -----------------------------------------------------
        self.setWindowTitle("JD2021PC Map Installer")
        self._apply_window_size_config()

        self._build_ui()
        self._apply_window_size_config(force_to_configured_size=True)
        self._config.log_detail_level = apply_log_detail(self._config.log_detail_level)
        self._wire_signals()

        self._status_bar = QStatusBar()
        self.setStatusBar(self._status_bar)
        self._status_bar.showMessage("Ready")
        self._init_size_overlay()

        # Show Quickstart Guide after first paint.
        QTimer.singleShot(500, self._show_quickstart_if_needed)
        QTimer.singleShot(900, self._run_startup_dependency_guardrail)
        QTimer.singleShot(1500, self._run_startup_update_check)

    def closeEvent(self, event) -> None:
        """Ensure all background processes (especially ffplay) are stopped."""
        logger.info("Closing application. Cleaning up...")
        self._preview_widget.stop()

        root_logger = logging.getLogger()
        try:
            root_logger.removeHandler(self._log_console.log_handler)
        except Exception:
            pass
        
        # Give threads a moment to finish, but don't hang if they are stuck
        for thread in list(self._active_threads):
            if thread.isRunning():
                thread.quit()
                thread.wait(1000)
        
        self._save_settings()
        event.accept()

    # ==================================================================
    # SETTINGS PERSISTENCE
    # ==================================================================

    def _load_settings(self) -> AppConfig:
        settings_file = self._settings_file_path()
        if settings_file.exists():
            try:
                with settings_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                return AppConfig(**data)
            except Exception as e:
                logger.error("Failed to load settings from %s: %s", settings_file, e)
        return AppConfig()

    def _save_settings(self) -> None:
        settings_file = self._settings_file_path()
        try:
            # handle pydantic v2 vs v1
            if hasattr(self._config, "model_dump"):
                data = self._config.model_dump(mode="json")
            else:
                data = json.loads(self._config.json())
            with settings_file.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.error("Failed to save settings to %s: %s", settings_file, e)

    @staticmethod
    def _settings_file_path() -> Path:
        return Path(__file__).resolve().parents[2] / "installer_settings.json"

    @staticmethod
    def _config_snapshot(config: AppConfig) -> dict:
        # Compare JSON-safe values so Path and optional fields are stable.
        if hasattr(config, "model_dump"):
            data = config.model_dump(mode="json")
        else:
            data = json.loads(config.json())
        return dict(data)

    @staticmethod
    def _format_setting_value(value: object) -> str:
        if isinstance(value, bool):
            return "enabled" if value else "disabled"
        if value is None:
            return "none"
        return str(value)

    def _summarize_settings_changes(self, old_snapshot: dict, new_snapshot: dict) -> list[str]:
        changes: list[str] = []
        for key in _SETTINGS_CHANGE_ORDER:
            old_value = old_snapshot.get(key)
            new_value = new_snapshot.get(key)
            if old_value == new_value:
                continue
            label = _SETTINGS_CHANGE_LABELS.get(key, key)
            changes.append(
                f"{label}: {self._format_setting_value(old_value)} -> {self._format_setting_value(new_value)}"
            )
        return changes

    def _apply_window_size_config(self, force_to_configured_size: bool = False) -> None:
        if getattr(self._config, "enforce_min_window_size", True):
            min_w = max(640, int(getattr(self._config, "min_window_width", 1000)))
            min_h = max(480, int(getattr(self._config, "min_window_height", 920)))
            self.setMinimumSize(min_w, min_h)

            if force_to_configured_size:
                self.resize(min_w, min_h)
            elif self.width() < min_w or self.height() < min_h:
                self.resize(max(self.width(), min_w), max(self.height(), min_h))
            return

        self.setMinimumSize(0, 0)

    def _apply_theme(self) -> None:
        app_instance = QApplication.instance()
        if not isinstance(app_instance, QApplication):
            return
        app = app_instance

        project_root = Path(__file__).resolve().parents[2]
        debug_mode = bool(getattr(self._config, "style_debug_mode", False))
        app.setStyleSheet(
            load_theme_stylesheet(self._config.theme, project_root, debug_mode)
        )
        if hasattr(self, "_log_console") and self._log_console is not None:
            self._log_console.set_theme_mode(self._config.theme)
        self._update_stylesheet_watch(debug_mode, project_root)
        self._set_panel_map_hints_visible(debug_mode)

    def _update_stylesheet_watch(self, enabled: bool, project_root: Path) -> None:
        if not enabled:
            self._active_stylesheet_path = None
            self._active_stylesheet_mtime = None
            self._style_reload_timer.stop()
            return

        style_path = resolve_theme_stylesheet_path(self._config.theme, project_root)
        self._active_stylesheet_path = style_path
        self._active_stylesheet_mtime = None
        if style_path.exists():
            try:
                self._active_stylesheet_mtime = style_path.stat().st_mtime
            except OSError:
                self._active_stylesheet_mtime = None
        self._style_reload_timer.start()

    def _poll_stylesheet_changes(self) -> None:
        style_path = self._active_stylesheet_path
        if style_path is None or not style_path.exists():
            return

        try:
            current_mtime = style_path.stat().st_mtime
        except OSError:
            return

        if self._active_stylesheet_mtime is None:
            self._active_stylesheet_mtime = current_mtime
            return

        if current_mtime <= self._active_stylesheet_mtime:
            return

        self._active_stylesheet_mtime = current_mtime
        self._apply_theme()
        self._set_status("Live style reload applied")

    def _set_panel_map_hints_visible(self, visible: bool) -> None:
        for label in (
            self._preview_hint_label,
            self._sync_hint_label,
            self._log_hint_label,
        ):
            if label is not None:
                label.setVisible(visible)

    def _show_quickstart_if_needed(self) -> None:
        if self._quickstart_shown_this_session:
            return
        if not getattr(self._config, "show_quickstart_on_launch", True):
            return

        self._quickstart_shown_this_session = True
        from jd2021_installer.ui.widgets.quickstart_dialog import QuickstartDialog
        dont_show_again = QuickstartDialog.show_guide(self)
        if dont_show_again:
            # Keep legacy flag in sync for users carrying older configs.
            self._config.show_quickstart_on_launch = False
            self._config.skip_quickstart = True
            self._save_settings()
        elif getattr(self._config, "skip_quickstart", False):
            self._config.skip_quickstart = False
            self._save_settings()

    def _offer_ffmpeg_install(self, missing: list[str]) -> bool:
        """Prompt user to auto-download and install FFmpeg toolchain."""
        msg = (
            f"The following required tools were not found: {', '.join(missing)}\n\n"
            "Would you like the installer to automatically download and configure "
            "FFmpeg for you? (Requires Internet connection)"
        )
        reply = QMessageBox.question(
            self, "Missing Dependencies", msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )

        if reply != QMessageBox.StandardButton.Yes:
            return False

        from jd2021_installer.ui.widgets.ffmpeg_dialog import FFmpegInstallDialog

        tools_dir = Path("tools/ffmpeg")
        tools_dir.mkdir(parents=True, exist_ok=True)

        if FFmpegInstallDialog.install(tools_dir, self):
            resolved = self._refresh_media_tool_configuration(persist=True)
            missing_after_install = [name for name, path in resolved.items() if not path]
            if missing_after_install:
                QMessageBox.warning(
                    self,
                    "Install Incomplete",
                    "FFmpeg auto-install completed, but required tools are still missing: "
                    + ", ".join(missing_after_install),
                )
                return False
            QMessageBox.information(
                self,
                "Success",
                "FFmpeg toolchain installed and configured successfully.",
            )
            return True

        QMessageBox.warning(self, "Failed", "FFmpeg installation was cancelled or failed.")
        return False

    def _find_missing_python_dependencies(self) -> list[str]:
        """Return missing pip package names from runtime dependency list."""
        missing: list[str] = []
        for pip_name, module_name in RUNTIME_DEPENDENCIES.items():
            if importlib.util.find_spec(module_name) is None:
                missing.append(pip_name)
        return missing

    def _offer_python_dependencies_install(self, missing: list[str]) -> bool:
        """Prompt user to install missing Python packages using current interpreter."""
        msg = (
            "The following required Python package(s) are missing:\n\n"
            f"- {'\n- '.join(missing)}\n\n"
            "Install them now with pip using this app's Python environment?"
        )
        reply = QMessageBox.question(
            self,
            "Missing Python Packages",
            msg,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", *missing],
                capture_output=True,
                text=True,
                timeout=600,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Install Failed",
                f"Could not run pip install:\n{exc}",
            )
            return False
        finally:
            QApplication.restoreOverrideCursor()

        if result.returncode != 0:
            output = (result.stderr or result.stdout or "Unknown pip error")[:1200]
            QMessageBox.critical(
                self,
                "Install Failed",
                "Pip failed to install required packages.\n\n"
                f"Output:\n{output}",
            )
            return False

        still_missing = self._find_missing_python_dependencies()
        if still_missing:
            QMessageBox.warning(
                self,
                "Dependency Check",
                "Package installation completed, but some modules are still unavailable:\n\n"
                + "\n".join(still_missing)
                + "\n\nPlease restart the installer and run Pre-flight Check again.",
            )
            return False

        QMessageBox.information(
            self,
            "Dependencies Installed",
            "Required Python packages were installed successfully.",
        )
        return True

    def _playwright_chromium_available(self) -> tuple[bool, str]:
        """Verify that Playwright can launch Chromium in this environment."""
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            return False, str(exc)

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def _offer_playwright_browser_install(self) -> bool:
        """Prompt user to install Chromium used by Playwright."""
        reply = QMessageBox.question(
            self,
            "Playwright Browser Missing",
            "Playwright Chromium is not installed or not usable.\n\n"
            "Would you like to install Chromium now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return False

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = subprocess.run(
                [sys.executable, "-m", "playwright", "install", "chromium"],
                capture_output=True,
                text=True,
                timeout=900,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Playwright Install Failed",
                f"Could not run Playwright install:\n{exc}",
            )
            return False
        finally:
            QApplication.restoreOverrideCursor()

        if result.returncode != 0:
            output = (result.stderr or result.stdout or "Unknown Playwright error")[:1200]
            QMessageBox.critical(
                self,
                "Playwright Install Failed",
                f"Could not install Chromium.\n\nOutput:\n{output}",
            )
            return False

        ok, reason = self._playwright_chromium_available()
        if not ok:
            QMessageBox.warning(
                self,
                "Playwright Check",
                "Chromium was installed, but launch verification still failed.\n\n"
                f"Details:\n{reason[:1000]}",
            )
            return False

        QMessageBox.information(
            self,
            "Playwright Ready",
            "Playwright Chromium is installed and ready.",
        )
        return True

    def _collect_missing_media_binaries(self) -> list[str]:
        """Return missing media binaries required by install/preview flows."""
        resolved = self._refresh_media_tool_configuration(persist=True)
        return [name for name, path in resolved.items() if not path]

    def _resolve_media_binary(
        self,
        binary_name: str,
        configured_path: Optional[str] = None,
    ) -> Optional[str]:
        """Resolve media tools with configured override, PATH, then local fallback."""
        if configured_path:
            configured_candidate = Path(configured_path)
            if configured_candidate.exists() and configured_candidate.is_file():
                return str(configured_candidate.resolve())
            configured_found = shutil.which(configured_path)
            if configured_found:
                return configured_found

        system_path = shutil.which(binary_name)
        if system_path:
            return system_path

        exe_name = f"{binary_name}.exe" if sys.platform == "win32" else binary_name
        local_candidate = (Path("tools/ffmpeg") / exe_name).resolve()
        if local_candidate.exists() and local_candidate.is_file():
            return str(local_candidate)

        return None

    def _refresh_media_tool_configuration(self, persist: bool = False) -> dict[str, Optional[str]]:
        """Resolve ffmpeg tool paths and propagate them to preview/runtime state."""
        repo_root = Path(__file__).resolve().parents[2]

        resolved_ffmpeg = self._resolve_media_binary("ffmpeg", self._config.ffmpeg_path)
        resolved_ffprobe = self._resolve_media_binary("ffprobe", self._config.ffprobe_path)
        resolved_ffplay = self._resolve_media_binary("ffplay")

        def _resolve_existing_path(candidate: Path) -> Optional[str]:
            if candidate.exists() and candidate.is_file():
                return str(candidate.resolve())
            return None

        def _resolve_configured_tool(
            configured_path: Optional[str],
            candidate_paths: list[Path],
            command_names: tuple[str, ...] = (),
        ) -> Optional[str]:
            if configured_path:
                configured_candidate = Path(configured_path).expanduser()
                resolved = _resolve_existing_path(configured_candidate)
                if resolved:
                    return resolved
                configured_found = shutil.which(configured_path)
                if configured_found:
                    return configured_found

            for candidate in candidate_paths:
                resolved = _resolve_existing_path(candidate)
                if resolved:
                    return resolved

            for command_name in command_names:
                on_path = shutil.which(command_name)
                if on_path:
                    return on_path

            return None

        resolved_vgmstream = _resolve_configured_tool(
            getattr(self._config, "vgmstream_path", None),
            [
                repo_root / "tools" / "vgmstream" / "vgmstream-cli.exe",
                repo_root / "tools" / "vgmstream" / "vgmstream.exe",
            ],
            command_names=("vgmstream-cli.exe", "vgmstream.exe"),
        )
        resolved_assetstudio = _resolve_configured_tool(
            getattr(self._config, "assetstudio_cli_path", None),
            [
                repo_root / "tools" / "Unity2UbiArt" / "bin" / "AssetStudioModCLI" / "AssetStudioModCLI.exe",
                repo_root / "tools" / "AssetStudioModCLI" / "AssetStudioModCLI.exe",
                repo_root / "tools" / "AssetStudio" / "AssetStudioModCLI.exe",
            ],
        )

        updated = False
        if resolved_ffmpeg and self._config.ffmpeg_path != resolved_ffmpeg:
            self._config.ffmpeg_path = resolved_ffmpeg
            updated = True
        if resolved_ffprobe and self._config.ffprobe_path != resolved_ffprobe:
            self._config.ffprobe_path = resolved_ffprobe
            updated = True
        if resolved_vgmstream and self._config.vgmstream_path != resolved_vgmstream:
            self._config.vgmstream_path = resolved_vgmstream
            updated = True
        if resolved_assetstudio and self._config.assetstudio_cli_path != resolved_assetstudio:
            self._config.assetstudio_cli_path = resolved_assetstudio
            updated = True

        if hasattr(self, "_preview_widget"):
            self._preview_widget.set_tool_paths(
                ffmpeg_path=resolved_ffmpeg or self._config.ffmpeg_path,
                ffprobe_path=resolved_ffprobe or self._config.ffprobe_path,
                ffmpeg_hwaccel=getattr(self._config, "ffmpeg_hwaccel", "auto"),
                preview_video_mode=getattr(self._config, "preview_video_mode", "proxy_low"),
                preview_fps=getattr(self._config, "preview_fps", 24),
            )

        if persist and updated:
            self._save_settings()

        return {
            "ffmpeg": resolved_ffmpeg,
            "ffprobe": resolved_ffprobe,
            "ffplay": resolved_ffplay,
            "vgmstream": resolved_vgmstream,
            "assetstudio": resolved_assetstudio,
        }

    def _ensure_runtime_dependencies(self, include_fetch_checks: bool) -> bool:
        """Comprehensive guard rail for Python packages + toolchain dependencies."""
        missing_python = self._find_missing_python_dependencies()
        if missing_python and not self._offer_python_dependencies_install(missing_python):
            return False

        missing_media = self._collect_missing_media_binaries()
        if missing_media and not self._offer_ffmpeg_install(missing_media):
            return False

        if include_fetch_checks:
            ok, reason = self._playwright_chromium_available()
            if not ok and not self._offer_playwright_browser_install():
                if reason:
                    QMessageBox.warning(
                        self,
                        "Fetch Mode Dependency",
                        "Fetch mode cannot continue until Playwright Chromium is available.\n\n"
                        f"Details:\n{reason[:1000]}",
                    )
                return False

        return True

    def _run_startup_dependency_guardrail(self) -> None:
        """Run a lightweight startup dependency health check with remediation."""
        # Do not force Playwright browser launch at startup; that check is mode-specific.
        self._ensure_runtime_dependencies(include_fetch_checks=False)

    def _run_startup_update_check(self) -> None:
        """Silently check for updates on launch if enabled in settings.

        Only shows a dialog if an update is available.  Network errors
        are swallowed silently so users are never nagged on startup.
        """
        if not getattr(self._config, "check_updates_on_launch", True):
            return

        import sys
        project_root = Path(__file__).resolve().parents[2]
        sys.path.insert(0, str(project_root))
        try:
            from updater import Updater
        except ImportError:
            logger.debug("Updater module not found, skipping startup update check.")
            return
        finally:
            try:
                sys.path.remove(str(project_root))
            except ValueError:
                pass

        updater = Updater(project_root)
        updater.initialize_state()

        # Use the configured branch; fall back to whatever the updater detects
        # so a stale setting (e.g. "master" on a v2-only repo) doesn't cause a
        # silent 404 that makes the check appear to succeed.
        configured_branch = (getattr(self._config, "update_branch", "") or "").strip()
        branch = configured_branch if configured_branch else updater.get_current_branch()
        self._set_status(f"Checking for updates on {branch}...")
        self._startup_update_completed = False

        def _check():
            return updater.check_for_updates(branch)

        thread = QThread(self)
        from jd2021_installer.ui.widgets.update_dialog import UpdateResultDialog
        from PyQt6.QtCore import QObject, pyqtSignal

        class _Worker(QObject):
            finished = pyqtSignal(object)
            error = pyqtSignal(str)

            def run(self):
                try:
                    result = _check()
                    self.finished.emit(result)
                except Exception as exc:
                    self.error.emit(str(exc))

        worker = _Worker()
        worker.moveToThread(thread)
        self._startup_update_thread = thread
        self._startup_update_worker = worker

        timeout_timer = QTimer(self)
        timeout_timer.setSingleShot(True)

        def _on_timeout():
            if self._startup_update_completed:
                return
            logger.warning("Startup update check timed out on branch %s", branch)
            self._set_status(f"Update check timed out for {branch}")

        timeout_timer.timeout.connect(_on_timeout)
        timeout_timer.start(25000)

        def _on_finished(result):
            self._startup_update_completed = True
            timeout_timer.stop()
            if result.error:
                logger.info("Startup update check failed: %s", result.error)
                self._set_status("Update check failed")
            elif not result.is_up_to_date:
                logger.info(
                    "Update available: %s -> %s on branch %s",
                    result.local_commit,
                    result.remote_commit,
                    result.branch,
                )
                self._set_status(f"Updates Available on {result.branch}")
                dialog = UpdateResultDialog(result, updater, self)
                dialog.exec()
            else:
                logger.info(
                    "Up to date on branch %s (commit %s)",
                    result.branch,
                    result.local_commit,
                )
                self._set_status(f"You are running on the latest version for {result.branch}")
            thread.quit()

        def _on_error(msg):
            self._startup_update_completed = True
            timeout_timer.stop()
            logger.info("Startup update check error: %s", msg)
            self._set_status("Update check failed")
            thread.quit()

        def _cleanup():
            self._startup_update_thread = None
            self._startup_update_worker = None
            self._active_threads.discard(thread)

        thread.started.connect(worker.run)
        worker.finished.connect(_on_finished)
        worker.error.connect(_on_error)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(_cleanup)

        self._active_threads.add(thread)
        thread.start()

    # ==================================================================
    # UI COMPOSITION  (Phase 3)
    # ==================================================================

    def resizeEvent(self, event) -> None:
        """Handle window resize by restarting/scaling the preview if active."""
        super().resizeEvent(event)
        self._show_size_overlay()
        if hasattr(self, "_preview_widget") and self._preview_widget.is_playing:
            # Re-trigger preview with current offsets to pick up new dimensions
            self._restart_preview_now()

    def _init_size_overlay(self) -> None:
        self._size_overlay = QLabel(self)
        self._size_overlay.setObjectName("windowSizeOverlay")
        self._size_overlay.setVisible(False)
        self._size_overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._size_overlay.raise_()

    def _hide_size_overlay(self) -> None:
        if self._size_overlay is not None:
            self._size_overlay.hide()

    def _show_size_overlay(self) -> None:
        if not getattr(self._config, "show_window_size_overlay", True):
            self._hide_size_overlay()
            return

        if self._size_overlay is None:
            return

        self._size_overlay.setText(f"{self.width()} x {self.height()}")
        self._size_overlay.adjustSize()
        margin = 14
        x = max(margin, self.width() - self._size_overlay.width() - margin)
        y = margin
        self._size_overlay.move(x, y)
        self._size_overlay.show()
        self._size_overlay.raise_()

        timeout_ms = int(getattr(self._config, "window_size_overlay_timeout_ms", 1100))
        self._size_overlay_hide_timer.start(max(200, timeout_ms))

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("mainWindowCentral")
        self.setCentralWidget(central)
        root_layout = QHBoxLayout(central)
        root_layout.setContentsMargins(8, 8, 8, 8)
        root_layout.setSpacing(8)

        # -- Column 1 (Left): Sidebar ----------------------------------------
        left_col = QWidget()
        left_col.setObjectName("mainWindowLeftPanel")
        left_col.setMinimumWidth(380)
        left_col.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding,
        )
        left_layout = QVBoxLayout(left_col)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(8)
        
        self._mode_selector = ModeSelectorWidget()
        self._mode_selector.setObjectName("mainWindowModeSelector")
        self._mode_selector.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        left_layout.addWidget(self._mode_selector, stretch=0)

        self._config_panel = ConfigWidget()
        self._config_panel.setObjectName("mainWindowConfigPanel")
        left_layout.addWidget(self._config_panel, stretch=0)

        self._action_panel = ActionWidget()
        self._action_panel.setObjectName("mainWindowActionPanel")
        left_layout.addWidget(self._action_panel, stretch=0)

        self._feedback_panel = ProgressLogWidget()
        self._feedback_panel.setObjectName("mainWindowFeedbackPanel")
        self._feedback_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        left_layout.addWidget(self._feedback_panel, stretch=1)
        
        root_layout.addWidget(left_col)

        # -- Column 2 (Right): Expanding -------------------------------------
        right_col = QWidget()
        right_col.setObjectName("mainWindowRightPanel")
        right_col.setMinimumWidth(500)
        right_col.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        right_layout = QVBoxLayout(right_col)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        self._preview_hint_label = QLabel("Preview Area")
        self._preview_hint_label.setObjectName("panelMapHintLabel")
        right_layout.addWidget(self._preview_hint_label)

        self._preview_widget = PreviewWidget()
        self._preview_widget.set_tool_paths(
            ffmpeg_path=self._config.ffmpeg_path,
            ffprobe_path=self._config.ffprobe_path,
            ffmpeg_hwaccel=getattr(self._config, "ffmpeg_hwaccel", "auto"),
            preview_video_mode=getattr(self._config, "preview_video_mode", "proxy_low"),
            preview_fps=getattr(self._config, "preview_fps", 24),
        )
        self._preview_widget.setObjectName("mainWindowPreviewWidget")
        self._preview_widget.setMinimumHeight(300)
        self._preview_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        right_layout.addWidget(self._preview_widget, stretch=1)

        self._sync_hint_label = QLabel("Sync Controls")
        self._sync_hint_label.setObjectName("panelMapHintLabel")
        right_layout.addWidget(self._sync_hint_label)

        self._sync_refinement = SyncRefinementWidget()
        self._sync_refinement.setObjectName("mainWindowSyncRefinement")
        self._sync_refinement.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        right_layout.addWidget(self._sync_refinement, stretch=0)

        self._log_hint_label = QLabel("Log Console")
        self._log_hint_label.setObjectName("panelMapHintLabel")
        right_layout.addWidget(self._log_hint_label)

        self._log_console = LogConsoleWidget()
        self._log_console.setObjectName("mainWindowLogConsole")
        self._log_console.setMinimumHeight(30)
        self._log_console.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Minimum,
        )
        # Wire root logger to our console
        root_logger = logging.getLogger()
        if self._log_console.log_handler not in root_logger.handlers:
            root_logger.addHandler(self._log_console.log_handler)
        right_layout.addWidget(self._log_console, stretch=0)

        self._set_panel_map_hints_visible(bool(getattr(self._config, "style_debug_mode", False)))

        root_layout.addWidget(right_col, stretch=6)
        root_layout.setStretch(0, 4)
        root_layout.setStretch(1, 6)
        
        # Apply loaded settings to config panel
        if self._config.game_directory:
            self._config_panel.set_game_directory(str(self._config.game_directory))

        self._config_panel.set_video_quality(self._config.video_quality)
        self._set_preview_controls_ready(False)

    # ==================================================================
    # SIGNAL / SLOT WIRING  (Phase 4)
    # ==================================================================

    def _wire_signals(self) -> None:
        # -- Mode & config signals ------------------------------------------
        self._mode_selector.mode_changed.connect(self._on_mode_changed)
        self._mode_selector.target_selected.connect(self._on_target_selected)
        self._mode_selector.source_state_changed.connect(
            lambda state: self._on_target_selected(str(state.get("target", "")))
        )
        self._config_panel.game_dir_changed.connect(self._on_game_dir_changed)
        self._config_panel.quality_changed.connect(self._on_quality_changed)

        # -- Action panel signals -------------------------------------------
        self._action_panel.install_requested.connect(self._on_install_requested)
        self._action_panel.preflight_requested.connect(self._on_preflight)
        self._action_panel.uninstall_requested.connect(self._on_uninstall_map)
        self._action_panel.readjust_offset_requested.connect(self._on_readjust)
        self._action_panel.settings_requested.connect(self._on_settings)
        self._action_panel.reset_state_requested.connect(self._on_reset_state)

        # -- Sync refinement signals ----------------------------------------
        self._sync_refinement.preview_requested.connect(self._on_preview_toggle)
        self._sync_refinement.apply_requested.connect(self._on_apply_offset)
        self._sync_refinement.offsets_changed.connect(self._on_offset_spin_changed)
        self._sync_refinement.nav_requested.connect(self._on_nav_requested)
        
        # -- Preview widget signals -----------------------------------------
        self._preview_widget.preview_stopped.connect(
            lambda: self._sync_refinement.set_preview_state(False)
        )
        self._preview_widget.audio_unavailable.connect(self._on_preview_audio_unavailable)

    # ==================================================================
    # SLOT IMPLEMENTATIONS
    # ==================================================================

    # -- Config / mode slots ------------------------------------------------

    def _on_mode_changed(self, mode: str) -> None:
        self._current_mode = mode
        # Prevent stale targets from a previous mode from passing install checks.
        self._current_target = None
        self._set_preview_controls_ready(False)
        self._set_status(f"Mode: {mode}")

    def _on_target_selected(self, target: str) -> None:
        normalized_target = target.strip()
        previous_target = (self._current_target or "").strip()

        # Avoid duplicate events from overlapping UI signals.
        if normalized_target == previous_target:
            return

        self._current_target = normalized_target or None
        if normalized_target:
            logger.debug("Target selected: %s", normalized_target)
        elif previous_target:
            logger.debug("Target cleared")
        self._set_preview_controls_ready(False)

    def _on_game_dir_changed(self, path: str) -> None:
        self._config.game_directory = Path(path)
        self._set_status(f"Game directory: {path}")
        self._save_settings()

    def _on_quality_changed(self, quality: str) -> None:
        self._config.video_quality = quality
        self._save_settings()

    def _collect_game_dir_checks(self) -> tuple[list[str], list[str]]:
        """Return (blocking issues, non-blocking warnings) for game dir checks."""
        issues: list[str] = []
        warnings: list[str] = []
        game_dir = self._config.game_directory

        if not game_dir:
            issues.append("Game directory is not set.")
            return issues, warnings

        if not game_dir.is_dir():
            issues.append(f"Game directory does not exist: {game_dir}")
            return issues, warnings

        sku_scene = game_dir / "data" / "World" / "SkuScenes" / "SkuScene_Maps_PC_All.isc"
        if not sku_scene.is_file():
            issues.append(
                "SkuScene_Maps_PC_All.isc not found under the selected game directory."
            )

        game_dir_str = str(game_dir)
        try:
            game_dir_str.encode("ascii")
        except UnicodeEncodeError:
            warnings.append("Game path contains non-ASCII characters; some tools may fail.")
        if "Program Files" in game_dir_str:
            warnings.append("Game appears to be in Program Files; admin rights may be required.")

        test_file = game_dir / "data" / ".write_test"
        try:
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_text("test", encoding="utf-8")
            test_file.unlink(missing_ok=True)
        except PermissionError:
            issues.append("Cannot write to game directory; check permissions or run as admin.")
        except OSError as exc:
            issues.append(f"Cannot write to game directory: {exc}")

        try:
            free_mb = shutil.disk_usage(game_dir).free // (1024 * 1024)
            if free_mb < 500:
                warnings.append(f"Low disk space on destination drive: {free_mb} MB free.")
        except OSError:
            warnings.append("Could not determine free disk space.")

        return issues, warnings

    def _collect_source_target_issues(self) -> list[str]:
        """Validate mode-specific source inputs and refresh the active target."""
        issues: list[str] = []
        from jd2021_installer.ui.widgets.mode_selector import (
            MODE_FETCH,
            MODE_JDNEXT,
            MODE_HTML,
            MODE_HTML_JDNEXT,
            MODE_IPK,
            MODE_BATCH,
            MODE_MANUAL,
        )

        source_state = self._mode_selector.get_current_state()
        idx = int(source_state.get("mode_index", MODE_FETCH))
        fields = source_state.get("fields", {})

        if idx in (MODE_FETCH, MODE_JDNEXT):
            fetch_mode_key = "jdnext" if idx == MODE_JDNEXT else "fetch"
            fetch_fields = fields.get(fetch_mode_key, {}) if isinstance(fields, dict) else {}
            raw = str(fetch_fields.get("codenames", "")).strip()
            codenames = [c.strip() for c in raw.split(",") if c.strip()]
            if not codenames:
                if idx == MODE_JDNEXT:
                    issues.append("Enter at least one codename for Fetch JDNext mode.")
                else:
                    issues.append("Enter at least one codename for Fetch JDU mode.")
            else:
                self._current_target = ",".join(codenames)
            return issues

        if idx == MODE_HTML:
            html_fields = fields.get("html", {}) if isinstance(fields, dict) else {}
            asset_html = str(html_fields.get("asset", "")).strip()
            nohud_html = str(html_fields.get("nohud", "")).strip()
            if not asset_html or not nohud_html:
                issues.append("Both Asset HTML and NOHUD HTML files are required.")
                return issues
            if not Path(asset_html).is_file():
                issues.append(f"Asset HTML file was not found: {asset_html}")
            if not Path(nohud_html).is_file():
                issues.append(f"NOHUD HTML file was not found: {nohud_html}")
            if not issues:
                self._current_target = asset_html
            return issues

        if idx == MODE_HTML_JDNEXT:
            html_jdnext_fields = fields.get("html_jdnext", {}) if isinstance(fields, dict) else {}
            asset_html = str(html_jdnext_fields.get("asset", "")).strip()
            if not asset_html:
                issues.append("Asset HTML file is required for HTML JDNext mode.")
                return issues
            if not Path(asset_html).is_file():
                issues.append(f"Asset HTML file was not found: {asset_html}")
            if not issues:
                self._current_target = asset_html
            return issues

        if idx == MODE_IPK:
            ipk_fields = fields.get("ipk", {}) if isinstance(fields, dict) else {}
            target = str(ipk_fields.get("file", "")).strip()
            if not target:
                issues.append("Select an IPK archive first.")
            elif not Path(target).is_file():
                issues.append(f"IPK file was not found: {target}")
            else:
                self._current_target = target
            return issues

        if idx == MODE_BATCH:
            batch_fields = fields.get("batch", {}) if isinstance(fields, dict) else {}
            target = str(batch_fields.get("dir", "")).strip()
            if not target:
                issues.append("Select a batch directory first.")
            elif not Path(target).is_dir():
                issues.append(f"Batch directory was not found: {target}")
            else:
                self._current_target = target
            return issues

        if idx == MODE_MANUAL:
            manual_inputs = fields.get("manual", {}) if isinstance(fields, dict) else {}
            codename = str(manual_inputs.get("codename", "")).strip()
            root_dir = str(manual_inputs.get("root", "")).strip()
            manual_submode = str(source_state.get("manual_submode", "select")).strip().lower()
            if not codename and not root_dir:
                issues.append("Manual mode requires a codename or a root directory.")
                return issues

            if root_dir and not Path(root_dir).is_dir():
                issues.append(f"Manual root directory was not found: {root_dir}")

            if manual_submode != "scan":
                required_files = [
                    ("audio", "Audio file is required."),
                    ("video", "Video file (.webm) is required."),
                    ("mtrack", "Musictrack CKD / .trk is required (fatal for config generation)."),
                ]
                for key, missing_msg in required_files:
                    value = str(manual_inputs.get(key, "")).strip()
                    if not value:
                        issues.append(missing_msg)
                        continue
                    if not Path(value).is_file():
                        issues.append(f"Manual {key} file was not found: {value}")

            if not issues:
                self._current_target = root_dir or codename
            return issues

        if not self._current_target:
            issues.append("No input target selected.")
        return issues

    # -- Pre-flight ---------------------------------------------------------

    def _on_preflight(self) -> None:
        """Run install pre-flight checks (V1 parity-oriented validations)."""
        issues, warnings = self._collect_game_dir_checks()
        issues.extend(self._collect_source_target_issues())

        if issues:
            QMessageBox.warning(
                self,
                "Pre-flight Check Failed",
                "\n".join(f"• {i}" for i in issues),
            )
            self._set_status("Pre-flight failed")
            return

        from jd2021_installer.ui.widgets.mode_selector import MODE_FETCH, MODE_JDNEXT
        source_state = self._mode_selector.get_current_state()
        include_fetch_checks = int(source_state.get("mode_index", -1)) in (MODE_FETCH, MODE_JDNEXT)
        if not self._ensure_runtime_dependencies(include_fetch_checks=include_fetch_checks):
            self._set_status("Pre-flight failed")
            return

        if warnings:
            QMessageBox.warning(
                self,
                "Pre-flight Check Warnings",
                "\n".join(f"• {w}" for w in warnings),
            )
            self._set_status("Pre-flight passed with warnings")
        else:
            if self._config.show_preflight_success_popup:
                QMessageBox.information(
                    self, "Pre-flight Check", "All checks passed! Ready to install."
                )
            self._set_status("Pre-flight passed")

    # -- Clear cache --------------------------------------------------------

    def _on_clear_cache(self) -> None:
        legacy_path_cache = Path("installer_paths.json")
        has_saved_game_dir = bool(self._config.game_directory)
        has_legacy_cache = legacy_path_cache.is_file()

        if not has_saved_game_dir and not has_legacy_cache:
            QMessageBox.information(self, "Cache Already Empty", "No cached game path was found.")
            return

        reply = QMessageBox.question(
            self,
            "Clear Path Cache",
            "Clear saved game path cache and force path re-discovery on next install/pre-flight?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            if has_legacy_cache:
                legacy_path_cache.unlink(missing_ok=True)

            self._config.game_directory = None
            self._config_panel.set_game_directory("")
            self._save_settings()

            self.append_log("Path cache cleared.")
            self._set_status("Path cache cleared")
            QMessageBox.information(
                self,
                "Cache Cleared",
                "Path cache cleared. The next pre-flight/install will require path resolution again.",
            )
        except Exception as exc:
            logger.exception("Failed to clear cache: %s", exc)
            QMessageBox.warning(self, "Clear Cache Failed", f"Failed to clear cache:\n{exc}")

    # -- Actions ------------------------------------------------------------

    def _collect_uninstall_candidates(self) -> list[tuple[str, str, str]]:
        """Return uninstall candidates as (codename, source_mode, location)."""
        if not self._config.game_directory:
            return []

        game_dir = self._config.game_directory
        while game_dir.name.lower() in ("world", "data"):
            game_dir = game_dir.parent

        index_entries = load_index().entries
        index_by_codename = {entry.codename.lower(): entry for entry in index_entries}

        candidates: dict[str, tuple[str, str, str]] = {}

        registered_codenames = list_registered_maps(game_dir)
        for codename in registered_codenames:
            key = codename.lower()
            source_mode = "Unknown"
            location = "Registered in SkuScene (map folder not found)"

            if key in index_by_codename:
                entry = index_by_codename[key]
                source_mode = entry.source_mode or "Unknown"
                installed_dir = Path(entry.installed_map_dir)
                if installed_dir.is_dir():
                    location = str(installed_dir)

            if location.startswith("Registered in SkuScene"):
                map_dir_candidates = [
                    game_dir / "data" / "World" / "MAPS" / codename,
                    game_dir / "data" / "world" / "maps" / codename,
                ]
                for map_dir in map_dir_candidates:
                    if map_dir.is_dir():
                        location = str(map_dir)
                        break

            candidates[key] = (codename, source_mode, location)

        return sorted(candidates.values(), key=lambda item: item[0].lower())

    def _pick_maps_for_uninstall(self) -> list[str]:
        candidates = self._collect_uninstall_candidates()
        if not candidates:
            QMessageBox.information(
                self,
                "Uninstall Map",
                "No installed maps were detected in the selected game directory.",
            )
            return []

        dialog = QDialog(self)
        dialog.setWindowTitle("Select Maps to Uninstall")
        dialog.setMinimumSize(620, 420)
        root = QVBoxLayout(dialog)
        root.addWidget(QLabel("Choose one or more installed maps to uninstall."))

        list_widget = QListWidget(dialog)
        list_widget.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        list_widget.itemChanged.connect(lambda _item: _refresh_selection_count())
        for codename, source_mode, location in candidates:
            item = QListWidgetItem(f"{codename}  [{source_mode}]")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, codename)
            item.setToolTip(location)
            list_widget.addItem(item)
        root.addWidget(list_widget)

        btns = QHBoxLayout()
        btn_select_all = QPushButton("Select All")
        btn_select_all.setToolTip("Check every map in the list")

        def _set_all_checked(checked: bool) -> None:
            state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            for i in range(list_widget.count()):
                item = list_widget.item(i)
                if item is not None:
                    item.setCheckState(state)
            _refresh_selection_count()

        btn_select_all.clicked.connect(lambda: _set_all_checked(True))
        btns.addWidget(btn_select_all)

        btn_clear = QPushButton("Unselect All")
        btn_clear.setToolTip("Uncheck every map in the list")
        btn_clear.clicked.connect(lambda: _set_all_checked(False))
        btns.addWidget(btn_clear)

        count_label = QLabel()
        btns.addWidget(count_label)

        def _refresh_selection_count() -> None:
            total = list_widget.count()
            selected = 0
            for i in range(total):
                item = list_widget.item(i)
                if item is not None and item.checkState() == Qt.CheckState.Checked:
                    selected += 1
            count_label.setText(f"{selected} of {total} selected")

        _refresh_selection_count()

        btns.addStretch()

        btn_uninstall = QPushButton("Uninstall Selected")
        btn_uninstall.setToolTip("Uninstall all checked maps")
        btn_uninstall.clicked.connect(dialog.accept)
        btns.addWidget(btn_uninstall)

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setToolTip("Close this dialog without uninstalling maps")
        btn_cancel.clicked.connect(dialog.reject)
        btns.addWidget(btn_cancel)

        root.addLayout(btns)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return []

        selected_codes: list[str] = []
        for i in range(list_widget.count()):
            item = list_widget.item(i)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                continue
            selected_codes.append(str(item.data(Qt.ItemDataRole.UserRole)))

        if not selected_codes:
            QMessageBox.information(self, "No Map Selected", "Select at least one map to uninstall.")

        return selected_codes

    def _on_uninstall_map(self) -> None:
        if not self._config.game_directory:
            QMessageBox.warning(self, "Game Directory Missing", "Set a game directory before uninstalling maps.")
            return

        if self._active_worker is not None:
            QMessageBox.information(
                self,
                "Operation In Progress",
                "Wait for the current operation to finish before uninstalling maps.",
            )
            return

        selected_codes = self._pick_maps_for_uninstall()
        if not selected_codes:
            return

        code_lines = "\n".join(f"- {code}" for code in selected_codes)
        confirm = QMessageBox.question(
            self,
            "Confirm Uninstall",
            (
                f"Remove {len(selected_codes)} selected map(s)?\n\n"
                f"{code_lines}\n\n"
                "This will delete map files and cooked cache, unregister from SkuScene, "
                "remove installer cache, and remove each map from the readjust index."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._preview_widget.stop()
        self._lock_ui(True)
        self._feedback_panel.reset()
        self._feedback_panel.set_checklist_steps(selected_codes)
        self._feedback_panel.set_progress(0)
        self._set_status("Uninstalling selected maps...")
        self.append_log(f"Starting uninstall for {len(selected_codes)} map(s)...")

        worker = UninstallMapsWorker(
            game_dir=self._config.game_directory,  # type: ignore[arg-type]
            selected_codenames=selected_codes,
            config=self._config,
        )
        thread = QThread()
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self._feedback_panel.set_progress)
        worker.status.connect(self._on_status_updated)
        worker.error.connect(self._on_uninstall_error)
        worker.finished.connect(self._on_uninstall_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread: self._cleanup_thread(t, "uninstall"))

        self._active_threads.add(thread)
        self._active_worker = worker
        thread.start()

    def _on_uninstall_error(self, msg: str) -> None:
        self.append_log(f"ERROR: {msg}")
        QMessageBox.critical(
            self,
            "Uninstall Failed",
            f"Failed to uninstall selected maps:\n{msg}",
        )
        self._set_status("Uninstall failed")
        self._lock_ui(False)

    def _on_uninstall_finished(self, result: UninstallBatchResult) -> None:
        changed_lowers = set(result.changed_codenames)
        self._nav_maps = [m for m in self._nav_maps if m.codename.lower() not in changed_lowers]
        self._completed_install_maps = [
            m for m in self._completed_install_maps if m.codename.lower() not in changed_lowers
        ]
        self._pending_offsets = {
            name: offsets
            for name, offsets in self._pending_offsets.items()
            if name.lower() not in changed_lowers
        }

        if self._current_map and self._current_map.codename.lower() in changed_lowers:
            self._current_map = self._nav_maps[0] if self._nav_maps else None
            self._nav_index = 0

        if self._current_map:
            if len(self._nav_maps) > 1:
                self._sync_refinement.set_nav_visible(True, f"Map 1 / {len(self._nav_maps)}")
            else:
                self._sync_refinement.set_nav_visible(False)
            self._set_preview_controls_ready(True)
        else:
            self._sync_refinement.set_nav_visible(False)
            self._set_preview_controls_ready(False)
            self._preview_widget.reset()

        if result.failed:
            QMessageBox.warning(
                self,
                "Uninstall Completed With Errors",
                "Some maps could not be uninstalled:\n\n" + "\n".join(result.failed),
            )
            self.append_log("WARNING: Uninstall finished with errors.")
            self._set_status("Uninstall completed with errors")
        elif not changed_lowers:
            self.append_log("No selected maps required uninstall changes.")
            self._set_status("Uninstall completed (no changes)")
        else:
            self.append_log("Selected maps uninstalled successfully.")
            self._set_status("Uninstall complete")

        self._lock_ui(False)

    def _on_settings(self) -> None:
        from jd2021_installer.ui.widgets.settings_dialog import SettingsDialog

        old_snapshot = self._config_snapshot(self._config)
        dialog = SettingsDialog(
            self._config,
            self,
            bulk_install_request=self.launch_songdb_bulk_install,
        )
        if dialog.exec():
            new_config = dialog.get_config()
            new_config.log_detail_level = apply_log_detail(new_config.log_detail_level)
            new_snapshot = self._config_snapshot(new_config)
            changes = self._summarize_settings_changes(old_snapshot, new_snapshot)

            self._config = new_config
            self._apply_window_size_config(force_to_configured_size=True)
            self._apply_theme()
            self._refresh_media_tool_configuration(persist=True)
            self._save_settings()
            if not getattr(self._config, "show_window_size_overlay", True):
                self._hide_size_overlay()
            self._config_panel.set_video_quality(self._config.video_quality)

            if changes:
                self.append_log("Settings changed: " + "; ".join(changes))
            else:
                self.append_log("Settings changed: none.")

            old_log_detail = str(old_snapshot.get("log_detail_level", "")).strip() or "unknown"
            new_log_detail = str(new_snapshot.get("log_detail_level", "")).strip() or "unknown"
            if old_log_detail != new_log_detail:
                self.append_log(
                    f"Logging detail profile changed: '{old_log_detail}' -> '{new_log_detail}'."
                )

            self._set_status("Settings saved.")

    def launch_songdb_bulk_install(self, source_game: str, codenames: list[str]) -> bool:
        """Queue a bulk fetch install by injecting songdb codenames into fetch mode."""
        if self._active_worker is not None:
            self._set_status("Please wait for the current operation to finish.")
            return False

        clean_codenames = [c.strip() for c in codenames if isinstance(c, str) and c.strip()]
        if not clean_codenames:
            self._set_status("No valid codenames were provided for bulk install.")
            return False

        from jd2021_installer.ui.widgets.mode_selector import MODE_FETCH, MODE_JDNEXT

        mode_source = (source_game or "jdu").strip().lower()
        is_jdnext = mode_source == "jdnext"
        mode_index = MODE_JDNEXT if is_jdnext else MODE_FETCH
        mode_key = "jdnext" if is_jdnext else "fetch"

        self._mode_selector.set_mode_index(mode_index)
        self._mode_selector.set_mode_codenames(mode_key, ",".join(clean_codenames))

        self.append_log(
            f"Bulk songdb install requested: source={mode_source}, codenames={len(clean_codenames)}"
        )
        self._set_status(f"Starting bulk install for {len(clean_codenames)} map(s)...")
        self._on_install_requested()
        return True

    def _on_readjust(self) -> None:
        if self._active_worker is not None:
            self._set_status("Please wait for the current operation to finish.")
            return

        self._preview_widget.stop(reset_position=False, clear_canvas=False)
        self._sync_refinement.set_preview_state(False)
        self._clear_offset_review_state()

        entries, pruned = prune_stale_entries()
        if pruned:
            self.append_log(
                f"Readjust index auto-pruned {len(pruned)} stale entr{'y' if len(pruned) == 1 else 'ies'}."
            )

        dialog = QDialog(self)
        dialog.setWindowTitle("Select Maps for Offset Readjust")
        dialog.setMinimumSize(560, 380)
        root = QVBoxLayout(dialog)
        root.addWidget(QLabel("Choose one or more maps from the index, or browse source folder manually."))

        list_widget = QListWidget(dialog)
        list_widget.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        list_widget.itemChanged.connect(lambda _item: _refresh_selection_count())
        for entry in sorted(entries, key=lambda e: e.codename.lower()):
            label = f"{entry.codename}  [{entry.source_mode}]"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            item.setData(Qt.ItemDataRole.UserRole, entry.codename)
            item.setToolTip(entry.source_root)
            list_widget.addItem(item)
        root.addWidget(list_widget)

        btns = QHBoxLayout()
        btn_select_all = QPushButton("Select All")
        btn_select_all.setToolTip("Check every map in the list")
        
        def _set_all_checked(checked: bool) -> None:
            state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
            for i in range(list_widget.count()):
                list_widget.item(i).setCheckState(state)
            _refresh_selection_count()

        btn_select_all.clicked.connect(lambda: _set_all_checked(True))
        btns.addWidget(btn_select_all)

        btn_clear = QPushButton("Unselect All")
        btn_clear.setToolTip("Uncheck every map in the list")
        btn_clear.clicked.connect(lambda: _set_all_checked(False))
        btns.addWidget(btn_clear)

        count_label = QLabel()
        btns.addWidget(count_label)

        def _refresh_selection_count() -> None:
            total = list_widget.count()
            selected = 0
            for i in range(total):
                if list_widget.item(i).checkState() == Qt.CheckState.Checked:
                    selected += 1
            count_label.setText(f"{selected} of {total} selected")

        _refresh_selection_count()

        btns.addStretch()

        picked_folder: dict[str, Optional[str]] = {"value": None}

        def _browse_fallback() -> None:
            folder = QFileDialog.getExistingDirectory(dialog, "Select Source Folder for Readjust")
            if folder:
                picked_folder["value"] = folder
                dialog.accept()

        btn_browse = QPushButton("Browse Folder…")
        btn_browse.setToolTip("Load maps for readjust directly from a folder")
        btn_browse.clicked.connect(_browse_fallback)
        btns.addWidget(btn_browse)

        btn_load = QPushButton("Load Selected")
        btn_load.setToolTip("Load checked maps into Sync Refinement")
        btn_load.clicked.connect(dialog.accept)
        btns.addWidget(btn_load)

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setToolTip("Close this dialog without loading maps")
        btn_cancel.clicked.connect(dialog.reject)
        btns.addWidget(btn_cancel)

        root.addLayout(btns)

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        if picked_folder["value"]:
            self._load_readjust_from_folder(picked_folder["value"])
            return

        selected_codes = {
            str(list_widget.item(i).data(Qt.ItemDataRole.UserRole))
            for i in range(list_widget.count())
            if list_widget.item(i).checkState() == Qt.CheckState.Checked
        }
        if not selected_codes:
            QMessageBox.information(self, "No Map Selected", "Select at least one map or use Browse Folder.")
            return

        selected_entries = [e for e in entries if e.codename in selected_codes]
        loaded_maps: list[NormalizedMapData] = []
        failed: list[str] = []
        progress = QProgressDialog("Loading selected maps for readjust...", "Cancel", 0, len(selected_entries), self)
        progress.setWindowTitle("Loading Readjust Maps")
        progress.setWindowModality(Qt.WindowModality.ApplicationModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        for index, entry in enumerate(selected_entries, start=1):
            progress.setLabelText(f"Loading {entry.codename} ({index}/{len(selected_entries)})")
            QApplication.processEvents()
            if progress.wasCanceled():
                break
            try:
                loaded_maps.append(self._load_readjust_map_from_index(entry))
            except Exception as exc:
                failed.append(f"{entry.codename}: {exc}")
            progress.setValue(index)

        progress.close()

        if failed:
            QMessageBox.warning(
                self,
                "Readjust Load Warning",
                "Some maps could not be loaded:\n\n" + "\n".join(failed),
            )

        if not loaded_maps:
            return

        self._activate_readjust_maps(loaded_maps)

    def _load_readjust_from_folder(self, folder: str) -> None:
        from jd2021_installer.parsers.normalizer import normalize

        try:
            map_data = normalize(folder)
            setattr(map_data, "_readjust_profile", "generic")
            setattr(map_data, "_readjust_update_audio", True)
            setattr(map_data, "_readjust_update_video", True)
            target_dir = self._resolve_target_map_dir(map_data.codename)
            setattr(map_data, "_readjust_target_dir", target_dir)
            self._activate_readjust_maps([map_data])
        except Exception as exc:
            QMessageBox.warning(
                self,
                "Readjust Unavailable",
                (
                    "Readjust could not load this folder.\n\n"
                    "Readjust requires valid source files for preview/apply.\n"
                    "If sources were deleted after install, readjust is not available.\n\n"
                    f"Details: {exc}"
                ),
            )

    def _build_minimal_readjust_map(self, entry: ReadjustIndexEntry) -> NormalizedMapData:
        source_root = Path(entry.source_root)
        discovered_audio, discovered_video = self._discover_readjust_media_paths(source_root, entry.codename)
        audio_path = discovered_audio or Path(entry.source_audio)
        video_path = discovered_video or Path(entry.source_video)

        map_data = NormalizedMapData(
            codename=entry.codename,
            song_desc=SongDescription(map_name=entry.codename, title=entry.codename),
            music_track=MusicTrackStructure(),
            media=MapMedia(
                audio_path=audio_path,
                video_path=video_path,
            ),
            sync=MapSync(),
            source_dir=source_root,
            has_autodance=False,
        )
        return map_data

    def _discover_readjust_media_paths(self, source_root: Path, codename: str) -> tuple[Optional[Path], Optional[Path]]:
        if not source_root.is_dir():
            return None, None

        codename_low = codename.lower()
        audio_matches: list[Path] = []
        video_matches: list[Path] = []

        try:
            for path in source_root.rglob("*"):
                if not path.is_file():
                    continue
                lower_name = path.name.lower()
                if lower_name.endswith(".webm") and "mappreview" not in lower_name and "videopreview" not in lower_name:
                    video_matches.append(path)
                elif lower_name.endswith(".ogg") or lower_name.endswith(".wav") or lower_name.endswith(".wav.ckd"):
                    if "audiopreview" not in lower_name:
                        audio_matches.append(path)
        except OSError:
            return None, None

        def _pick_best(candidates: list[Path]) -> Optional[Path]:
            if not candidates:
                return None
            codename_hits = [p for p in candidates if codename_low in p.as_posix().lower().split("/") or p.name.lower().startswith(codename_low)]
            if codename_hits:
                return sorted(codename_hits)[0]
            return sorted(candidates)[0]

        return _pick_best(audio_matches), _pick_best(video_matches)

    def _resolve_target_map_dir(self, codename: str) -> Path:
        if not self._config.game_directory:
            raise RuntimeError("Game directory not configured.")
        game_dir = self._config.game_directory
        while game_dir.name.lower() in ("world", "data"):
            game_dir = game_dir.parent
        return game_dir / "data" / "World" / "MAPS" / codename

    def _load_readjust_map_from_index(self, entry: ReadjustIndexEntry) -> NormalizedMapData:
        from jd2021_installer.parsers.normalizer import normalize

        source_root = Path(entry.source_root)
        if not source_root.is_dir():
            raise RuntimeError("Source folder no longer exists.")

        try:
            map_data = normalize(source_root, codename=entry.codename, search_root=source_root)
        except Exception:
            map_data = self._build_minimal_readjust_map(entry)

        discovered_audio, discovered_video = self._discover_readjust_media_paths(source_root, entry.codename)
        if discovered_audio is not None:
            map_data.media.audio_path = discovered_audio
        elif not map_data.media.audio_path or not map_data.media.audio_path.exists():
            map_data.media.audio_path = Path(entry.source_audio)

        if discovered_video is not None:
            map_data.media.video_path = discovered_video
        elif not map_data.media.video_path or not map_data.media.video_path.exists():
            map_data.media.video_path = Path(entry.source_video)

        installed_trk = Path(entry.installed_trk)
        vst = read_video_start_time_from_trk(installed_trk)
        if vst is None:
            source_trk = source_root / "Audio" / f"{entry.codename}.trk"
            vst = read_video_start_time_from_trk(source_trk)
        if vst is None:
            vst = map_data.sync.video_ms / 1000.0

        mode_low = entry.source_mode.lower()
        is_ipk = "ipk" in mode_low
        is_fetch_html = ("fetch" in mode_low) or ("html" in mode_low)

        # Always trust the persisted index values (including legitimate 0.0 values).
        default_audio_ms = float(entry.last_audio_ms)
        default_video_ms = float(entry.last_video_ms)

        if is_ipk:
            map_data.sync.audio_ms = 0.0
            map_data.sync.video_ms = default_video_ms
            setattr(map_data, "_readjust_profile", "ipk")
            setattr(map_data, "_readjust_update_audio", False)
            setattr(map_data, "_readjust_update_video", True)
        elif is_fetch_html:
            map_data.sync.audio_ms = default_audio_ms
            map_data.sync.video_ms = default_video_ms
            setattr(map_data, "_readjust_profile", "fetch_html")
            setattr(map_data, "_readjust_source_mode", entry.source_mode)
            setattr(map_data, "_readjust_update_audio", True)
            setattr(map_data, "_readjust_update_video", False)
        else:
            map_data.sync.audio_ms = default_audio_ms
            map_data.sync.video_ms = default_video_ms
            setattr(map_data, "_readjust_profile", "generic")
            setattr(map_data, "_readjust_update_audio", True)
            setattr(map_data, "_readjust_update_video", True)

        map_data.video_start_time_override = map_data.sync.video_ms / 1000.0
        setattr(map_data, "_readjust_target_dir", Path(entry.installed_map_dir))
        setattr(map_data, "_readjust_indexed", True)
        return map_data

    def _apply_readjust_profile(self, map_data: NormalizedMapData) -> None:
        profile = str(getattr(map_data, "_readjust_profile", "generic"))
        self._sync_refinement.apply_profile(profile)
        self._sync_refinement.set_ipk_mode(is_ipk=self._is_ipk_source_map(map_data))

    def _is_ipk_source_map(self, map_data: Optional[NormalizedMapData]) -> bool:
        """Best-effort detection for maps originating from IPK sources."""
        if map_data is None:
            return False

        profile = str(getattr(map_data, "_readjust_profile", "")).strip().lower()
        if profile == "ipk":
            return True

        if bool(getattr(map_data, "_is_ipk_source", False)):
            return True

        mode_low = (self._current_mode or "").lower()
        if "ipk" in mode_low:
            return True

        source_dir = getattr(map_data, "source_dir", None)
        if source_dir:
            src_low = str(source_dir).lower().replace("\\", "/")
            if "_batch_temp" in src_low and "/world/maps/" in src_low:
                return True

        audio_path = map_data.media.audio_path
        if audio_path and audio_path.name.lower().endswith(".wav.ckd"):
            return True

        return False

    def _is_jdnext_source_map(self, map_data: Optional[NormalizedMapData]) -> bool:
        """Best-effort detection for maps originating from JDNext sources."""
        if map_data is None:
            return False

        # Never apply JDNext preview-only corrections to explicit IPK sources.
        if self._is_ipk_source_map(map_data):
            return False

        profile = str(getattr(map_data, "_readjust_profile", "")).strip().lower()
        if profile == "fetch_html":
            source_mode = str(getattr(map_data, "_readjust_source_mode", "")).strip().lower()
            if "jdnext" in source_mode:
                return True

        mode_low = (self._current_mode or "").lower()
        if "jdnext" in mode_low:
            return True

        source_dir = getattr(map_data, "source_dir", None)
        if source_dir:
            src = Path(str(source_dir))
            src_low = str(src).lower().replace("\\", "/")
            if "/mapdownloads/" in src_low:
                assets_html = src / "assets.html"
                if assets_html.exists():
                    try:
                        content = assets_html.read_text(encoding="utf-8", errors="ignore").lower()
                        if "/jdnext/maps/" in content or "server:jdnext" in content:
                            return True
                    except OSError:
                        pass

        return False

    def _get_preview_fps_for_map(self, map_data: Optional[NormalizedMapData]) -> float:
        """Return preview FPS, auto-switching JDNext maps to 25 FPS."""
        default_fps = float(getattr(self._config, "preview_fps", 24) or 24)
        if self._is_jdnext_source_map(map_data):
            return 25.0
        return default_fps if default_fps > 0 else 24.0

    def _get_preview_only_audio_nudge_s(self, _map_data: Optional[NormalizedMapData]) -> float:
        """Return preview-only audio nudge (seconds), never applied to install output."""
        nudge_ms = float(getattr(self._config, "preview_only_audio_offset_ms", 0.0) or 0.0)

        return nudge_ms / 1000.0

    def _activate_readjust_maps(self, maps: list[NormalizedMapData]) -> None:
        self._nav_maps = maps
        self._nav_index = 0
        self._current_map = self._nav_maps[0]
        self._pending_offsets = {
            m.codename: (m.sync.audio_ms, m.sync.video_ms) for m in self._nav_maps
        }

        if len(self._nav_maps) > 1:
            self._sync_refinement.set_nav_visible(True, f"Map 1 / {len(self._nav_maps)}")
        else:
            self._sync_refinement.set_nav_visible(False)

        first_audio_ms, first_video_ms = self._pending_offsets.get(
            self._current_map.codename,
            (self._current_map.sync.audio_ms, self._current_map.sync.video_ms),
        )
        self._sync_refinement.set_offsets(first_audio_ms, first_video_ms)
        self._apply_readjust_profile(self._current_map)

        self._set_preview_controls_ready(True)
        self.append_log(f"Loaded {len(self._nav_maps)} map(s) for offset readjustment.")
        self._set_status(f"Readjusting offset for {self._current_map.codename}")
        self._on_preview_toggle(True)

    # -- Reset state --------------------------------------------------------

    def _on_reset_state(self) -> None:
        self._current_map = None
        self._current_target = None
        self._nav_maps = []
        self._nav_index = 0
        self._pending_offsets.clear()
        self._readjust_pending_updates.clear()
        self._install_started_at = None
        self._completed_install_maps = []
        self._sync_refinement.reset()
        self._preview_widget.reset()
        self._set_preview_controls_ready(False)
        self._feedback_panel.reset()
        self._set_status("State reset.")

    # ==================================================================
    # PIPELINE EXECUTION  (Install flow)
    # ==================================================================

    def _on_install_requested(self) -> None:
        """Launch the Extract → Normalize → Install pipeline."""
        if self._active_worker is not None:
            self._set_status("Please wait for the current operation to finish.")
            return

        self._install_started_at = time.monotonic()
        self._completed_install_maps = []

        game_issues, game_warnings = self._collect_game_dir_checks()
        if game_issues:
            QMessageBox.warning(self, "No Game Dir", "\n".join(game_issues))
            return

        # v1 parity: codename whitespace sanitization prompt before fetch scrape starts.
        from jd2021_installer.ui.widgets.mode_selector import MODE_FETCH, MODE_JDNEXT
        source_state = self._mode_selector.get_current_state()
        source_fields = source_state.get("fields", {})
        mode_index = int(source_state.get("mode_index", -1))
        if mode_index in (MODE_FETCH, MODE_JDNEXT):
            fetch_mode_key = "jdnext" if mode_index == MODE_JDNEXT else "fetch"
            fetch_fields = source_fields.get(fetch_mode_key, {}) if isinstance(source_fields, dict) else {}
            raw_value = str(fetch_fields.get("codenames", ""))
            if re.search(r"\s", raw_value):
                sanitized = re.sub(r"\s+", "", raw_value)
                reply = QMessageBox.question(
                    self,
                    "Sanitize Codename",
                    (
                        "The codename contains whitespace.\n\n"
                        f"Current: {raw_value}\n"
                        f"Sanitized: {sanitized}\n\n"
                        "Use sanitized codename and continue?"
                    ),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self._mode_selector.set_mode_codenames(fetch_mode_key, sanitized)
                    self._current_target = sanitized
                else:
                    self.append_log("Install aborted: codename sanitization declined.")
                    self._set_status("Install aborted")
                    return

        source_issues = self._collect_source_target_issues()
        if source_issues:
            QMessageBox.warning(self, "No Target", "\n".join(source_issues))
            return

        if game_warnings:
            QMessageBox.warning(
                self,
                "Install Warnings",
                "\n".join(f"• {w}" for w in game_warnings),
            )

        from jd2021_installer.ui.widgets.mode_selector import MODE_FETCH, MODE_JDNEXT
        source_state = self._mode_selector.get_current_state()
        mode_index = int(source_state.get("mode_index", -1))
        include_fetch_checks = mode_index in (MODE_FETCH, MODE_JDNEXT)
        if not self._ensure_runtime_dependencies(include_fetch_checks=include_fetch_checks):
            return

        # Ensure readjust/bundle review state from a prior operation doesn't leak
        # into the next install/finalize flow.
        self._clear_offset_review_state()

        # Start dynamic per-map logging immediately if target is available
        self._start_file_logging(self._current_target)

        # Intercept batch mode - it has a completely different pipeline structure
        from jd2021_installer.ui.widgets.mode_selector import MODE_BATCH
        if mode_index == MODE_BATCH:
            self._start_batch_install()
            return

        # Multi-codename Fetch should use the same multi-map review/apply flow as Batch/IPK bundle.
        from jd2021_installer.ui.widgets.mode_selector import MODE_FETCH, MODE_JDNEXT
        if mode_index in (MODE_FETCH, MODE_JDNEXT):
            fetch_mode_key = "jdnext" if mode_index == MODE_JDNEXT else "fetch"
            fetch_source = "jdnext" if mode_index == MODE_JDNEXT else "jdu"
            fetch_fields = source_fields.get(fetch_mode_key, {}) if isinstance(source_fields, dict) else {}
            raw_fetch = str(fetch_fields.get("codenames", "")).strip()
            fetch_codenames = [c.strip() for c in raw_fetch.split(",") if c.strip()]
            if len(fetch_codenames) > 1:
                self._sync_refinement.set_ipk_mode(is_ipk=False)
                self._start_batch_install(
                    selected_maps=set(fetch_codenames),
                    map_names=fetch_codenames,
                    fetch_codenames=fetch_codenames,
                    fetch_source=fetch_source,
                )
                return

        # Bundle IPK support
        from jd2021_installer.ui.widgets.mode_selector import MODE_IPK
        if mode_index == MODE_IPK and Path(self._current_target).is_file():
            from jd2021_installer.extractors.archive_ipk import validate_ipk_magic
            try:
                validate_ipk_magic(self._current_target)
            except Exception as exc:
                QMessageBox.critical(self, "Invalid IPK", f"Could not open IPK archive:\n{exc}")
                return

            from jd2021_installer.extractors.archive_ipk import inspect_ipk
            maps_found = inspect_ipk(self._current_target)
            if len(maps_found) > 1:
                from jd2021_installer.ui.widgets.bundle_dialog import BundleSelectDialog
                selected_maps = BundleSelectDialog.show_dialog(Path(self._current_target).name, maps_found, self)
                if not selected_maps:
                    return # User cancelled
                
                # Defer to batch installer to handle everything cleanly
                self._sync_refinement.set_ipk_mode(is_ipk=True)
                self._start_batch_install(selected_maps=set(selected_maps), map_names=sorted(list(selected_maps)))
                return

        # Resolve the correct extractor based on mode
        from jd2021_installer.ui.widgets.mode_selector import MODE_IPK
        is_ipk = mode_index == MODE_IPK
        self._sync_refinement.set_ipk_mode(is_ipk=is_ipk)

        extractor = self._resolve_extractor()
        if extractor is None:
            return

        # Prepare UI
        self._lock_ui(True)
        self._feedback_panel.reset()
        self._feedback_panel.set_checklist_steps(PIPELINE_STEPS)
        self._feedback_panel.update_checklist_step("Extracting map data...", StepStatus.IN_PROGRESS)

        # Create worker + thread
        worker_codename: str | None = None
        if mode_index in (MODE_FETCH, MODE_JDNEXT):
            fetch_mode_key = "jdnext" if mode_index == MODE_JDNEXT else "fetch"
            fetch_fields = source_fields.get(fetch_mode_key, {}) if isinstance(source_fields, dict) else {}
            raw_codenames = str(fetch_fields.get("codenames", "")).strip()
            fetch_codenames = [c.strip() for c in raw_codenames.split(",") if c.strip()]
            if len(fetch_codenames) == 1:
                worker_codename = fetch_codenames[0]

        worker = ExtractAndNormalizeWorker(
            extractor=extractor,
            output_dir=self._config.temp_directory / "_extraction",
            codename=worker_codename,
        )
        thread = QThread()
        worker.moveToThread(thread)

        # Wire signals
        thread.started.connect(worker.run)
        worker.progress.connect(self._feedback_panel.set_progress)
        worker.status.connect(self._on_status_updated)
        worker.error.connect(self._on_extract_error)
        worker.finished.connect(lambda data: self._on_extract_finished(data))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread: self._cleanup_thread(t, "extract"))

        self._active_threads.add(thread)
        self._active_worker = worker
        thread.start()

    def _on_extract_error(self, stage: str, msg: str) -> None:
        failed_stage = stage if stage in PIPELINE_STEPS else "Extracting map data..."
        self._feedback_panel.update_checklist_step(failed_stage, StepStatus.ERROR)
        self.append_log(f"ERROR: {msg}")
        QMessageBox.critical(
            self,
            "Pipeline Error",
            f"{failed_stage} failed:\n{msg}",
        )
        self._lock_ui(False)
        self._stop_file_logging()

    def _on_extract_finished(self, map_data: Optional[NormalizedMapData]) -> None:
        if map_data is None:
            return  # error already handled

        self._current_map = map_data
        self._completed_install_maps = [map_data]
        self._feedback_panel.update_checklist_step("Extracting map data...", StepStatus.DONE)
        self._feedback_panel.update_checklist_step("Parsing CKDs and metadata...", StepStatus.DONE)
        self._feedback_panel.update_checklist_step("Normalizing assets...", StepStatus.DONE)
        self._feedback_panel.update_checklist_step(
            "Decoding XMA2 audio...", StepStatus.IN_PROGRESS
        )

        # Update UI offsets from calculated normalization data
        logger.info("Setting UI offsets from normalization: audio=%.1f ms, video=%.1f ms", 
                    map_data.sync.audio_ms, map_data.sync.video_ms)
        self._sync_refinement.set_offsets(
            audio_ms=map_data.sync.audio_ms,
            video_ms=map_data.sync.video_ms
        )

        # JDU/Fetch/HTML mode parity: Visually disable video offset but keep value applied
        is_fetch = "Fetch" in self._current_mode or "HTML" in self._current_mode
        self._sync_refinement.set_video_editable(not is_fetch)

        # Check metadata for non-ASCII characters
        self._check_metadata(map_data)

        # Detect maps that are locked behind unlock/login conditions and ask how to proceed.
        if not self._apply_locked_status_policy_single(map_data):
            self.append_log("Install aborted by user during locked-status prompt.")
            self._set_status("Install aborted")
            self._lock_ui(False)
            self._stop_file_logging()
            return

        # Pre-install AlbumCoach customization for JDNext multi-coach maps.
        self._apply_albumcoach_customization(map_data)
        
        # Start install worker
        self._start_install_worker(map_data)

    def _check_metadata(self, map_data: NormalizedMapData) -> None:
        """Verify song metadata fields and prompt for correction if non-ASCII found."""
        fields_to_check = {
            "title": map_data.song_desc.title,
            "artist": map_data.song_desc.artist,
            "dancer_name": map_data.song_desc.dancer_name,
        }
        
        from jd2021_installer.ui.widgets.metadata_dialog import MetadataCorrectionDialog
        
        for field, value in fields_to_check.items():
            if any(ord(c) > 127 for c in value):
                corrected = MetadataCorrectionDialog.get_corrected_value(field, value, self)
                if field == "title": map_data.song_desc.title = corrected
                elif field == "artist": map_data.song_desc.artist = corrected
                elif field == "dancer_name": map_data.song_desc.dancer_name = corrected

    def _apply_locked_status_policy_single(self, map_data: NormalizedMapData) -> bool:
        """Prompt user when a map has a non-default song status and apply choice."""
        status_value = int(getattr(map_data.song_desc, "status", _READY_STATUS_VALUE))
        if status_value == _READY_STATUS_VALUE:
            return True

        behavior = getattr(self._config, "locked_status_behavior", "ask")
        codename = map_data.codename
        status_meaning = _SONG_STATUS_MEANINGS.get(status_value, "Unknown status")
        if behavior == "force3":
            map_data.song_desc.status = _READY_STATUS_VALUE
            self.append_log(
                f"[{codename}] Non-default status {status_value} ({status_meaning}) detected. Policy=force3, forcing Status={_READY_STATUS_VALUE}."
            )
            return True
        if behavior == "keep":
            self.append_log(
                f"[{codename}] Non-default status {status_value} ({status_meaning}) detected. Policy=keep, preserving original status."
            )
            return True

        reply = QMessageBox.question(
            self,
            "Non-default Song Status Detected",
            (
                f"Detected Status = {status_value} for '{codename}'.\n"
                f"Meaning: {status_meaning}.\n\n"
                f"Yes: Force Status to {_READY_STATUS_VALUE} (default playable behavior).\n"
                "No: Keep original status value.\n"
                "Cancel: Abort installation."
            ),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )

        if reply == QMessageBox.StandardButton.Cancel:
            return False

        if reply == QMessageBox.StandardButton.Yes:
            map_data.song_desc.status = _READY_STATUS_VALUE
            self.append_log(
                f"[{codename}] Non-default status {status_value} ({status_meaning}) detected. Forcing Status={_READY_STATUS_VALUE}."
            )
        else:
            self.append_log(
                f"[{codename}] Non-default status {status_value} ({status_meaning}) detected. Preserving original status."
            )
        return True

    def _apply_albumcoach_customization(self, map_data: NormalizedMapData) -> None:
        """Offer AlbumCoach customization for JDNext multi-coach maps lacking one.

        Conditions to trigger:
          1. Map is from a JDNext source.
          2. Map has ≥ 2 coach images.
          3. No cover_albumcoach_path already provided by the source.
          4. Setting is not ``always_default``.
        """
        if not getattr(map_data, "is_jdnext_source", False):
            return

        coach_images = [p for p in map_data.media.coach_images if p.exists()]
        if len(coach_images) < 2:
            return

        # Deduplicate by coach number — media discovery may find multiple
        # format variants (.png, .tga, .tga.ckd) for the same coach.
        import re as _re
        coaches_by_idx: dict[int, Path] = {}
        fallback_idx = 1
        for cpath in coach_images:
            m = _re.search(r"coach[_-]?(\d+)", cpath.name.lower())
            idx = int(m.group(1)) if m else fallback_idx
            if not m:
                while fallback_idx in coaches_by_idx:
                    fallback_idx += 1
                idx = fallback_idx
            # Prefer .png over encoded formats
            if idx not in coaches_by_idx or cpath.suffix.lower() == ".png":
                coaches_by_idx[idx] = cpath
        coach_images = [coaches_by_idx[k] for k in sorted(coaches_by_idx)]
        if len(coach_images) < 2:
            return

        if map_data.media.cover_albumcoach_path and map_data.media.cover_albumcoach_path.exists():
            return

        behavior = getattr(self._config, "albumcoach_behavior", "ask")
        if behavior == "always_default":
            return

        codename = map_data.codename

        if behavior == "always_customize":
            # Skip the ask dialog, go straight to the editor.
            from jd2021_installer.ui.widgets.albumcoach_dialog import AlbumCoachCustomizerDialog

            self.append_log(
                f"[{codename}] Opening AlbumCoach customizer (always_customize)."
            )
            dlg = AlbumCoachCustomizerDialog(coach_images, parent=self,
                                              theme=getattr(self._config, "theme", "dark"))
            dlg.exec()
            result_image = dlg.result_image
            never_ask = False
            new_behavior = ""
        else:
            # "ask" — show the two-step prompt.
            from jd2021_installer.ui.widgets.albumcoach_dialog import AlbumCoachCustomizerDialog

            self.append_log(
                f"[{codename}] JDNext multi-coach map detected without albumcoach. "
                "Prompting user."
            )
            result_image, never_ask, new_behavior = AlbumCoachCustomizerDialog.prompt(
                coach_images, parent=self,
                theme=getattr(self._config, "theme", "dark"),
            )

        # Handle "never ask again" preference persistence.
        if never_ask and new_behavior:
            self._config.albumcoach_behavior = new_behavior
            self._save_settings()
            self.append_log(
                f"[{codename}] AlbumCoach preference saved: {new_behavior}"
            )

        if result_image is not None:
            # Save the user-customized image to temp and set the path so the
            # pipeline copies it into the target instead of auto-synthesizing.
            import tempfile
            tmp_dir = Path(tempfile.mkdtemp(prefix="jd2021_albumcoach_"))
            dst = tmp_dir / f"{codename}_cover_albumcoach.png"
            result_image.save(str(dst))
            map_data.media.cover_albumcoach_path = dst
            self.append_log(
                f"[{codename}] Custom AlbumCoach composite applied."
            )
        else:
            self.append_log(
                f"[{codename}] Using default AlbumCoach compositing."
            )

    def _ask_batch_locked_status_policy(self) -> Optional[bool]:
        """Ask once for batch mode: force non-3 statuses to 3 or preserve originals."""
        behavior = getattr(self._config, "locked_status_behavior", "ask")
        if behavior == "force3":
            self.append_log("Batch status policy: force3 (auto-convert any non-3 status to 3).")
            return True
        if behavior == "keep":
            self.append_log("Batch status policy: keep (preserve all original status values).")
            return False

        reply = QMessageBox.question(
            self,
            "Batch Song Status Policy",
            (
                "Batch mode can auto-detect any song status that is not 3.\n\n"
                "Yes: For every detected non-3 status, force Status to 3.\n"
                "No: Keep original status values.\n"
                "Cancel: Abort batch install."
            ),
            QMessageBox.StandardButton.Yes
            | QMessageBox.StandardButton.No
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Cancel:
            return None
        return reply == QMessageBox.StandardButton.Yes

    def _start_install_worker(self, map_data: NormalizedMapData) -> None:
        worker = InstallMapWorker(
            map_data=map_data,
            target_dir=self._config.game_directory,  # type: ignore[arg-type]
            source_mode=self._current_mode,
            config=self._config,
        )
        thread = QThread()
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self._feedback_panel.set_progress)
        worker.status.connect(self._on_status_updated)
        worker.error.connect(self._on_install_error)
        worker.finished.connect(lambda ok: self._on_install_finished(ok))
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread: self._cleanup_thread(t, "install"))

        self._active_threads.add(thread)
        self._active_worker = worker
        thread.start()

    def _on_status_updated(self, msg: str) -> None:
        """Map backend status messages to checklist steps for visual feedback."""
        self._log_with_level(msg, self._classify_status_level(msg))

        # Support both batch status styles:
        # - "[Koi] Normalize assets"
        # - "[1/10] Normalize assets (Koi)"
        clean_msg = msg
        raw_prefix = ""
        if msg.startswith("[") and "]" in msg:
            try:
                parts = msg.split("]", 1)
                raw_prefix = parts[0][1:].strip()
                step_part = parts[1].strip()

                if "(" in step_part:
                    step_part = step_part.split("(", 1)[0].strip()
                clean_msg = step_part
            except Exception:
                pass

        prefix = f"[{raw_prefix}]" if raw_prefix else ""
        map_step_names = self._feedback_panel._step_items
        pipeline_mode = any(step in map_step_names for step in PIPELINE_STEPS)

        indexed_map_step: Optional[str] = None
        if "/" in raw_prefix:
            left, right = raw_prefix.split("/", 1)
            if left.isdigit() and right.isdigit():
                idx_1_based = int(left)
                list_item = self._feedback_panel._checklist.item(idx_1_based - 1)
                if list_item is not None:
                    indexed_map_step = str(list_item.data(Qt.ItemDataRole.UserRole) or "").strip()
                    if indexed_map_step not in map_step_names:
                        indexed_map_step = None

        if clean_msg in PIPELINE_STEPS:
            if raw_prefix in map_step_names:
                self._feedback_panel.update_checklist_step(raw_prefix, StepStatus.IN_PROGRESS, suffix=clean_msg)
            elif indexed_map_step is not None:
                self._feedback_panel.update_checklist_step(indexed_map_step, StepStatus.IN_PROGRESS, suffix=clean_msg)
            elif clean_msg in map_step_names:
                self._feedback_panel.update_checklist_step(clean_msg, StepStatus.IN_PROGRESS, prefix=prefix)
            else:
                self._feedback_panel.update_checklist_step(clean_msg, StepStatus.IN_PROGRESS, prefix=prefix)

            # Mark preceding pipeline steps only when checklist is in pipeline mode.
            if pipeline_mode and clean_msg in map_step_names:
                try:
                    idx = PIPELINE_STEPS.index(clean_msg)
                    for i in range(idx):
                        prev_step = PIPELINE_STEPS[i]
                        if prev_step in map_step_names:
                            self._feedback_panel.update_checklist_step(prev_step, StepStatus.DONE, prefix=prefix)
                except ValueError:
                    pass
        elif raw_prefix in map_step_names:
            self._feedback_panel.update_checklist_step(raw_prefix, StepStatus.IN_PROGRESS, suffix=clean_msg)
        elif indexed_map_step is not None:
            self._feedback_panel.update_checklist_step(indexed_map_step, StepStatus.IN_PROGRESS, suffix=clean_msg)

    def _on_install_error(self, msg: str) -> None:
        self.append_log(f"ERROR: {msg}")
        QMessageBox.critical(
            self,
            "Pipeline Error",
            f"Installation failed:\n{msg}",
        )
        self._set_preview_controls_ready(False)
        self._lock_ui(False)
        self._stop_file_logging()

    def _on_install_finished(self, success: bool) -> None:
        if success:
            if "Finalizing offsets..." in self._feedback_panel._step_items:
                self._feedback_panel.update_checklist_step("Finalizing offsets...", StepStatus.DONE)
            self._set_status("Installation complete!")
            if not self._config.suppress_offset_notification and len(self._nav_maps) <= 1:
                QMessageBox.information(
                    self,
                    "Check and Evaluate Offsets",
                    "Review the preview and sync controls now to verify the installed map offsets.",
                )
            self.append_log("Map installed successfully.")

            # If we don't have a nav list yet (single install), set current as the only one
            if not self._nav_maps and self._current_map:
                self._nav_maps = [self._current_map]
                self._nav_index = 0
                self._pending_offsets[self._current_map.codename] = (
                    self._current_map.sync.audio_ms,
                    self._current_map.sync.video_ms,
                )
                self._sync_refinement.set_nav_visible(False)

                # Ensure the sync panel reflects the current map's calculated offsets
                logger.info("Installation finished. Syncing UI offsets: audio=%.1f ms, video=%.1f ms", 
                            self._current_map.sync.audio_ms, self._current_map.sync.video_ms)
                self._sync_refinement.set_offsets(
                    self._current_map.sync.audio_ms,
                    self._current_map.sync.video_ms
                )

                self._sync_refinement.set_ipk_mode(
                    is_ipk=self._is_ipk_source_map(self._current_map)
                )

            # Start preview for the current map
            if self._current_map:
                self._register_map_in_readjust_index(self._current_map)
                self._set_preview_controls_ready(True)
                self._on_preview_toggle(True)

        summaries = self._build_install_summaries(success=success)
        self._log_install_summaries(summaries)
        self._show_install_summary_popup(summaries)
        self._lock_ui(False)
        self._stop_file_logging()
        self._install_started_at = None
        self._completed_install_maps = []

    def _on_batch_finished_with_data(self, installed_maps: list[NormalizedMapData]) -> None:
        """Called when a batch install completes with a list of map data."""
        if not installed_maps:
            return

        self._completed_install_maps = list(installed_maps)

        for map_data in installed_maps:
            self._register_map_in_readjust_index(map_data)
        
        self._nav_maps = installed_maps
        self._nav_index = 0
        self._current_map = self._nav_maps[0]
        self._pending_offsets = {
            m.codename: (m.sync.audio_ms, m.sync.video_ms) for m in self._nav_maps
        }
        
        # Show nav controls if multiple maps
        if len(self._nav_maps) > 1:
            self._sync_refinement.set_nav_visible(True, f"Map 1 / {len(self._nav_maps)}")
            self.append_log(f"Multi-map review: {len(self._nav_maps)} maps ready for offset adjustment.")
        else:
            self._sync_refinement.set_nav_visible(False)
            
        # Preview the first map
        logger.info("Batch finished: setting UI offsets for first map: audio=%.1f, video=%.1f", 
                    self._current_map.sync.audio_ms, self._current_map.sync.video_ms)
        first_audio_ms, first_video_ms = self._pending_offsets.get(
            self._current_map.codename,
            (self._current_map.sync.audio_ms, self._current_map.sync.video_ms),
        )
        self._sync_refinement.set_offsets(
            first_audio_ms,
            first_video_ms,
        )
        self._apply_readjust_profile(self._current_map)
        self._set_preview_controls_ready(True)
        self._on_preview_toggle(True)

    def _compute_install_duration_s(self) -> float:
        if self._install_started_at is None:
            return 0.0
        return max(0.0, time.monotonic() - self._install_started_at)

    def _build_install_summaries(self, success: bool) -> list[InstallSummary]:
        maps = self._completed_install_maps or ([self._current_map] if self._current_map else [])
        maps = [m for m in maps if m is not None]
        if not maps:
            return []

        duration_total = self._compute_install_duration_s()
        duration_per_map = duration_total / max(1, len(maps))

        summaries: list[InstallSummary] = []
        for map_data in maps:
            try:
                target_map_dir = self._resolve_target_map_dir(map_data.codename)
            except Exception as exc:
                logger.debug("Could not resolve install summary target for '%s': %s", map_data.codename, exc)
                continue

            summaries.append(
                build_install_summary(
                    map_data,
                    target_map_dir,
                    source_mode=self._current_mode,
                    quality=self._config.video_quality,
                    duration_s=duration_per_map,
                    success=success,
                )
            )
        return summaries

    def _log_install_summaries(self, summaries: list[InstallSummary]) -> None:
        if not summaries:
            return
        for summary in summaries:
            logger.info("===== Installation Summary =====\n%s\n===============================", render_install_summary(summary))

    def _show_install_summary_popup(self, summaries: list[InstallSummary]) -> None:
        if not getattr(self._config, "show_install_summary_popup", True):
            return

        if not summaries:
            return

        from jd2021_installer.ui.widgets.installation_summary_dialog import InstallationSummaryDialog

        InstallationSummaryDialog.show_summaries(summaries, self)

    def _register_map_in_readjust_index(self, map_data: NormalizedMapData) -> None:
        """Upsert one map's source metadata for readjust discovery."""
        if not map_data or not map_data.codename:
            return

        if not map_data.media.audio_path or not map_data.media.video_path:
            return

        audio_path = map_data.media.audio_path.resolve()
        video_path = map_data.media.video_path.resolve()

        source_root = self._infer_readjust_source_root(map_data, audio_path, video_path)
        source_mode = self._infer_readjust_source_mode(map_data)

        target_dir = self._resolve_target_map_dir(map_data.codename)
        trk_path = target_dir / "Audio" / f"{map_data.codename}.trk"

        entry = ReadjustIndexEntry(
            codename=map_data.codename,
            source_mode=source_mode,
            source_root=str(source_root),
            source_audio=str(audio_path),
            source_video=str(video_path),
            installed_map_dir=str(target_dir.resolve()),
            installed_trk=str(trk_path.resolve()),
            last_audio_ms=float(map_data.sync.audio_ms),
            last_video_ms=float(map_data.sync.video_ms),
        )
        upsert_entry(entry)

    def _infer_readjust_source_mode(self, map_data: NormalizedMapData) -> str:
        mode = (self._current_mode or "").strip()
        audio_suffix = map_data.media.audio_path.suffix.lower() if map_data.media.audio_path else ""
        mode_low = mode.lower()

        if "jdnext" in mode_low and "fetch" in mode_low:
            return "Fetch JDNext"
        if "jdnext" in mode_low and "html" in mode_low:
            return "HTML JDNext"
        if "jdnext" in mode_low:
            return "JDNext"
        if "fetch" in mode_low:
            return "Fetch"
        if "html" in mode_low:
            return "HTML"
        if "ipk" in mode_low:
            return "IPK Archive"
        if "batch" in mode_low:
            if audio_suffix == ".wav":
                return "IPK Bundle"
            return "Batch"
        return mode or "unknown"

    def _infer_readjust_source_root(
        self,
        map_data: NormalizedMapData,
        audio_path: Path,
        video_path: Path,
    ) -> Path:
        mode_low = (self._current_mode or "").lower()

        if "fetch" in mode_low or "html" in mode_low:
            candidate = (self._config.download_root / map_data.codename).resolve()
            if candidate.is_dir():
                return candidate

        if map_data.source_dir and map_data.source_dir.exists():
            src = map_data.source_dir.resolve()
            if src.is_dir() and "_batch_temp" not in str(src).lower() and "_extraction" not in str(src).lower():
                return src

        audio_parent = audio_path.parent
        video_parent = video_path.parent
        try:
            common = Path(os.path.commonpath([str(audio_parent), str(video_parent)]))
            if common.exists():
                return common.resolve()
        except Exception:
            pass

        if audio_parent.exists():
            return audio_parent.resolve()
        return video_parent.resolve()

    def _on_nav_requested(self, direction: int) -> None:
        """Switch between maps in a batch/bundle review."""
        if not self._nav_maps:
            return

        # Preserve current map edits before switching.
        if self._current_map is not None:
            audio_ms, video_ms = self._sync_refinement.get_offsets()
            self._pending_offsets[self._current_map.codename] = (
                audio_ms,
                video_ms,
            )
            
        new_index = self._nav_index + direction
        if 0 <= new_index < len(self._nav_maps):
            # Stop current preview
            self._on_preview_toggle(False)
            
            self._nav_index = new_index
            self._current_map = self._nav_maps[self._nav_index]
            
            # Update UI
            self._sync_refinement.set_nav_visible(True, f"Map {new_index + 1} / {len(self._nav_maps)}")
            self.append_log(f"Switched to: {self._current_map.codename}")
            
            # Update UI offsets
            logger.info("Nav requested: setting UI offsets: audio=%.1f, video=%.1f", 
                        self._current_map.sync.audio_ms, self._current_map.sync.video_ms)
            audio_ms, video_ms = self._pending_offsets.get(
                self._current_map.codename,
                (self._current_map.sync.audio_ms, self._current_map.sync.video_ms),
            )
            self._sync_refinement.set_offsets(
                audio_ms,
                video_ms,
            )
            self._apply_readjust_profile(self._current_map)
            
            # Start preview for the new map
            self._on_preview_toggle(True)

    # ==================================================================
    # SYNC REFINEMENT / PREVIEW
    # ==================================================================

    def _on_preview_toggle(self, start: bool) -> None:
        """Start or stop the embedded FFmpeg preview."""
        if start:
            if self._current_map and self._current_map.media.video_path and self._current_map.media.video_path.exists():
                video = str(self._current_map.media.video_path)
                audio = (
                    str(self._current_map.media.audio_path)
                    if self._current_map.media.audio_path and self._current_map.media.audio_path.exists()
                    else None
                )
                if not audio:
                    self.append_log("No audio available for preview.")
                    self._sync_refinement.set_preview_state(False)
                    return

                v_override = self._sync_refinement.get_video_offset() / 1000.0
                a_offset = self._sync_refinement.get_audio_offset() / 1000.0
                preview_nudge_s = self._get_preview_only_audio_nudge_s(self._current_map)
                a_offset += preview_nudge_s
                loop_start, loop_end = self._get_preview_loop_seconds(self._current_map)
                preview_fps = self._get_preview_fps_for_map(self._current_map)
                is_jdnext_preview = self._is_jdnext_source_map(self._current_map)
                
                logger.debug(
                    "Preview launch: v_override=%.3f, a_offset=%.3f, preview_nudge=%.3f, fps=%.3f",
                    v_override,
                    a_offset,
                    preview_nudge_s,
                    preview_fps,
                )

                self._preview_widget.launch(
                    video, audio,
                    v_override=v_override,
                    a_offset=a_offset,
                    loop_start=loop_start,
                    loop_end=loop_end,
                    preview_fps=preview_fps,
                    accurate_seek=is_jdnext_preview,
                )
            else:
                self.append_log("No video available for preview.")
                self._sync_refinement.set_preview_state(False)
        else:
            self._preview_widget.stop()

    def _on_offset_spin_changed(self, audio_ms: float, video_ms: float) -> None:
        """Debounced preview restart when offsets change."""
        if self._current_map is not None:
            self._pending_offsets[self._current_map.codename] = (
                audio_ms,
                video_ms,
            )

        if not self._preview_widget.is_playing:
            return
            
        # Debounce to prevent spam-launching ffplay while rapid clicking
        from PyQt6.QtCore import QTimer
        if not hasattr(self, "_preview_debounce_timer"):
            self._preview_debounce_timer = QTimer(self)
            self._preview_debounce_timer.setSingleShot(True)
            self._preview_debounce_timer.timeout.connect(self._restart_preview_now)
        
        self._preview_debounce_timer.start(500) # 0.5s delay

    def _restart_preview_now(self) -> None:
        if not self._current_map:
            return

        if not self._current_map.media.video_path or not self._current_map.media.video_path.exists():
            self.append_log("No video available for preview.")
            self._sync_refinement.set_preview_state(False)
            return

        if not self._current_map.media.audio_path or not self._current_map.media.audio_path.exists():
            self.append_log("No audio available for preview.")
            self._sync_refinement.set_preview_state(False)
            return

        if self._current_map and self._current_map.media.video_path and self._current_map.media.video_path.exists():
            v_override = self._sync_refinement.get_video_offset() / 1000.0
            a_offset = self._sync_refinement.get_audio_offset() / 1000.0
            preview_nudge_s = self._get_preview_only_audio_nudge_s(self._current_map)
            a_offset += preview_nudge_s
            loop_start, loop_end = self._get_preview_loop_seconds(self._current_map)
            preview_fps = self._get_preview_fps_for_map(self._current_map)
            is_jdnext_preview = self._is_jdnext_source_map(self._current_map)
            
            logger.debug("Debounced preview restart...")
            self._preview_widget.launch(
                str(self._current_map.media.video_path),
                str(self._current_map.media.audio_path),
                v_override=v_override,
                a_offset=a_offset,
                start_time=self._preview_widget.get_current_position(),
                loop_start=loop_start,
                loop_end=loop_end,
                preview_fps=preview_fps,
                accurate_seek=is_jdnext_preview,
            )

    def _on_apply_offset(self, audio_ms: float, video_ms: float) -> None:
        """Apply the combined offset to the current map data."""
        if self._current_map is None:
            QMessageBox.warning(self, "No Map", "Load a map before applying offsets.")
            return

        # V1 Parity: Stop preview and DON'T restart it
        self._preview_widget.stop()
        self._sync_refinement.set_preview_state(False)

        if not self._config.game_directory:
            QMessageBox.warning(self, "No Game Dir", "Cannot apply without a game directory set.")
            return

        # 1. Update videoStartTime override (in seconds)
        # V1 Parity: use the absolute offset from the spinbox directly
        self._current_map.video_start_time_override = (video_ms / 1000.0)
        self._pending_offsets[self._current_map.codename] = (audio_ms, video_ms)
        is_readjust_index_mode = bool(getattr(self._current_map, "_readjust_indexed", False))
        self._readjust_pending_updates.clear()

        # Bundle parity: apply reviewed offsets to every map in the bundle, not only the active one.
        if len(self._nav_maps) > 1:
            entries: list[tuple[NormalizedMapData, Path, float]] = []
            readjust_entries: list[tuple[NormalizedMapData, Path, float, float, bool, bool]] = []
            base_game_dir = self._config.game_directory
            while base_game_dir.name.lower() in ("world", "data"):
                base_game_dir = base_game_dir.parent

            for map_data in self._nav_maps:
                map_audio_ms, map_video_ms = self._pending_offsets.get(
                    map_data.codename,
                    (map_data.sync.audio_ms, map_data.sync.video_ms),
                )
                map_data.video_start_time_override = map_video_ms / 1000.0
                target_dir_attr = getattr(map_data, "_readjust_target_dir", None)
                target_dir = Path(target_dir_attr) if target_dir_attr else (
                    base_game_dir / "data" / "World" / "MAPS" / map_data.codename
                )

                entries.append(
                    (
                        map_data,
                        target_dir,
                        map_audio_ms / 1000.0,
                    )
                )

                readjust_entries.append(
                    (
                        map_data,
                        target_dir,
                        map_audio_ms / 1000.0,
                        map_video_ms / 1000.0,
                        bool(getattr(map_data, "_readjust_update_video", True)),
                        bool(getattr(map_data, "_readjust_update_audio", True)),
                    )
                )

            for map_data in self._nav_maps:
                map_audio_ms, map_video_ms = self._pending_offsets.get(
                    map_data.codename,
                    (map_data.sync.audio_ms, map_data.sync.video_ms),
                )
                self._readjust_pending_updates.append((map_data.codename, map_audio_ms, map_video_ms))

            if any(bool(getattr(m, "_readjust_indexed", False)) for m in self._nav_maps):
                worker = ApplyReadjustOffsetsBatchWorker(entries=readjust_entries, config=self._config)
            else:
                worker = ApplyOffsetsBatchWorker(entries=entries, config=self._config)
            thread = QThread()
            worker.moveToThread(thread)

            thread.started.connect(worker.run)
            worker.progress.connect(self._feedback_panel.set_progress)
            worker.status.connect(self._on_status_updated)
            worker.error.connect(self._on_install_error)
            worker.finished.connect(self._on_reprocess_finished)
            worker.finished.connect(thread.quit)
            worker.finished.connect(worker.deleteLater)
            thread.finished.connect(thread.deleteLater)
            thread.finished.connect(lambda t=thread: self._cleanup_thread(t, "apply_finish_batch"))

            self._active_threads.add(thread)
            self._active_worker = worker
            self._lock_ui(True)
            thread.start()
            return

        # 2. Launch worker to rewrite configs and reprocess audio
        from jd2021_installer.ui.workers.pipeline_workers import ApplyAndFinishWorker
        
        base_game_dir = self._config.game_directory
        while base_game_dir.name.lower() in ("world", "data"):
            base_game_dir = base_game_dir.parent

        if is_readjust_index_mode:
            target_dir_attr = getattr(self._current_map, "_readjust_target_dir", None)
            target_dir = Path(target_dir_attr) if target_dir_attr else (
                base_game_dir / "data" / "World" / "MAPS" / self._current_map.codename
            )
            worker = ApplyReadjustOffsetsBatchWorker(
                entries=[
                    (
                        self._current_map,
                        target_dir,
                        audio_ms / 1000.0,
                        video_ms / 1000.0,
                        bool(getattr(self._current_map, "_readjust_update_video", True)),
                        bool(getattr(self._current_map, "_readjust_update_audio", True)),
                    )
                ],
                config=self._config,
            )
            self._readjust_pending_updates = [(self._current_map.codename, audio_ms, video_ms)]
        else:
            worker = ApplyAndFinishWorker(
                self._current_map,
                base_game_dir / "data" / "World" / "MAPS" / self._current_map.codename,
                self._config.cache_directory / self._current_map.codename,
                a_offset=audio_ms / 1000.0,
                config=self._config,
            )
            self._readjust_pending_updates = [(self._current_map.codename, audio_ms, video_ms)]
        thread = QThread()
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.status.connect(self._on_status_updated)
        worker.error.connect(self._on_install_error)
        worker.finished.connect(self._on_reprocess_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread: self._cleanup_thread(t, "apply_finish"))

        self._active_threads.add(thread)
        self._active_worker = worker
        thread.start()

    def _on_reprocess_finished(self, success: bool) -> None:
        self._lock_ui(False)
        if success:
            logger.info("Offsets applied and audio reprocessed.")
            for codename, audio_ms, video_ms in self._readjust_pending_updates:
                update_offsets(codename, audio_ms=audio_ms, video_ms=video_ms)
            self._readjust_pending_updates.clear()
            if len(self._nav_maps) > 1:
                for map_data in self._nav_maps:
                    if map_data.codename in self._feedback_panel._step_items:
                        self._feedback_panel.update_checklist_step(map_data.codename, StepStatus.DONE)
            if "Finalizing offsets..." in self._feedback_panel._step_items:
                self._feedback_panel.update_checklist_step("Finalizing offsets...", StepStatus.DONE)
            self._preview_widget.reset()
            self._sync_refinement.set_preview_state(False)
            self._sync_refinement.set_nav_visible(False)
            self._set_preview_controls_ready(False)
            # V1 Parity: Don't auto-restart preview anymore after apply
            self._prompt_cleanup()
            self._clear_offset_review_state()
        else:
            self._readjust_pending_updates.clear()

    def _clear_offset_review_state(self) -> None:
        """Clear map-review/readjust context so a new flow starts cleanly."""
        self._nav_maps = []
        self._nav_index = 0
        self._pending_offsets.clear()
        self._readjust_pending_updates.clear()
        self._current_map = None
        self._sync_refinement.set_nav_visible(False)

    def _on_preview_audio_unavailable(self) -> None:
        if self._preview_audio_warning_shown:
            return
        self._preview_audio_warning_shown = True
        QMessageBox.information(
            self,
            "ffplay Not Found",
            "ffplay was not found. Video will play without audio.\n\n"
            "Install FFmpeg to enable audio preview.",
        )

    def _get_preview_loop_seconds(self, map_data: NormalizedMapData) -> tuple[float, float]:
        mt = map_data.music_track
        markers = mt.markers if mt and mt.markers else []
        if not markers:
            return 0.0, 0.0

        try:
            loop_start_idx = int(mt.preview_loop_start)
            loop_end_idx = int(mt.preview_loop_end)
        except (TypeError, ValueError):
            return 0.0, 0.0

        if loop_start_idx < 0 or loop_end_idx < 0:
            return 0.0, 0.0
        if loop_start_idx >= len(markers) or loop_end_idx >= len(markers):
            return 0.0, 0.0
        if loop_end_idx <= loop_start_idx:
            return 0.0, 0.0

        loop_start = markers[loop_start_idx] / 48.0 / 1000.0
        loop_end = markers[loop_end_idx] / 48.0 / 1000.0
        return max(0.0, loop_start), max(0.0, loop_end)

    def _prompt_cleanup(self) -> None:
        """Ask user or auto-delete source files based on cleanup_behavior."""
        if not self._current_map:
            return
            
        # 1. Check if we should skip (batch mid-review)
        if len(self._nav_maps) > 1 and self._nav_index < len(self._nav_maps) - 1:
            return # Don't prompt yet
            
        behavior = self._config.cleanup_behavior
        should_delete = False
        
        if behavior in ("delete", "aggressive"):
            should_delete = True
        elif behavior == "keep":
            should_delete = False
        else: # behavior == "ask"
            reply = QMessageBox.question(
                self,
                "Cleanup Source Files?",
                "The installation and sync are complete.\n\n"
                "Would you like to delete the temporary downloaded/extracted source files to save space? You will lose the ability to re-adjust offset if the installed map turns out to be out of sync.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            should_delete = (reply == QMessageBox.StandardButton.Yes)
        
        if should_delete:
            self._do_cleanup()

    def _do_cleanup(self) -> None:
        """Perform actual deletion of temporary files."""
        self.append_log("Cleaning up source files...")
        try:
            # 1. Clean up _extraction temp
            temp_dir = self._config.temp_directory / "_extraction"
            if temp_dir.exists():
                import shutil
                shutil.rmtree(temp_dir, ignore_errors=True)

            # 1b. Clean up the downloaded asset cache for this map.
            if self._current_map:
                download_dir = self._config.download_root / self._current_map.codename
                if download_dir.exists():
                    import shutil
                    shutil.rmtree(download_dir, ignore_errors=True)
                
                # 1c. Aggressive cache wipe
                if getattr(self._config, "cleanup_behavior", "ask") == "aggressive":
                    map_cache = self._config.cache_directory / self._current_map.codename
                    if map_cache.exists():
                        import shutil
                        shutil.rmtree(map_cache, ignore_errors=True)
            
            # 2. Clean up _batch_temp if it exists
            batch_temp = self._config.cache_directory / "_batch_temp"
            if batch_temp.exists():
                import shutil
                shutil.rmtree(batch_temp, ignore_errors=True)
            
            self.append_log("Source files cleaned up.")
        except Exception as e:
            self.append_log(f"Warning: Cleanup failed ({e})")

    # ==================================================================
    # HELPERS
    # ==================================================================

    def _resolve_extractor(self):
        """Create the correct BaseExtractor for the current mode/target.

        Returns ``None`` (and shows a warning) if the mode is not yet
        implemented or the target is invalid.
        """
        from jd2021_installer.ui.widgets.mode_selector import (
            MODE_FETCH,
            MODE_JDNEXT,
            MODE_HTML,
            MODE_HTML_JDNEXT,
            MODE_IPK,
            MODE_BATCH,
            MODE_MANUAL,
        )

        source_state = self._mode_selector.get_current_state()
        idx = int(source_state.get("mode_index", MODE_FETCH))
        source_fields = source_state.get("fields", {})

        if idx == MODE_IPK:
            from jd2021_installer.extractors.archive_ipk import ArchiveIPKExtractor

            ipk_path = Path(self._current_target)  # type: ignore[arg-type]
            if not ipk_path.is_file():
                QMessageBox.warning(self, "Invalid Path", f"IPK not found: {ipk_path}")
                return None

            # Provide a codename hint for bundle selection parity when available.
            desired_codename = re.sub(
                r"_(x360|durango|scarlett|nx|orbis|prospero|pc)$",
                "",
                ipk_path.stem,
                flags=re.IGNORECASE,
            )
            return ArchiveIPKExtractor(ipk_path, desired_codename=desired_codename)

        if idx in (MODE_FETCH, MODE_JDNEXT):
            from jd2021_installer.extractors.web_playwright import WebPlaywrightExtractor
            fetch_mode_key = "jdnext" if idx == MODE_JDNEXT else "fetch"
            fetch_source = "jdnext" if idx == MODE_JDNEXT else "jdu"
            fetch_fields = source_fields.get(fetch_mode_key, {}) if isinstance(source_fields, dict) else {}
            raw_codenames = str(fetch_fields.get("codenames", "")).strip()
            codenames = [c.strip() for c in raw_codenames.split(",") if c.strip()]

            return WebPlaywrightExtractor(
                codenames=codenames,
                source_game=fetch_source,
                config=self._config,
                quality=self._config.video_quality,
            )

        if idx == MODE_HTML:
            from jd2021_installer.extractors.web_playwright import WebPlaywrightExtractor

            html_fields = source_fields.get("html", {}) if isinstance(source_fields, dict) else {}
            asset_html = str(html_fields.get("asset", ""))
            nohud_html = str(html_fields.get("nohud", ""))
            
            if not asset_html and not nohud_html:
                QMessageBox.warning(self, "Missing Files", "Please select at least one HTML file.")
                return None

            return WebPlaywrightExtractor(
                asset_html=asset_html,
                nohud_html=nohud_html,
                source_game="jdu",
                config=self._config,
                quality=self._config.video_quality,
            )

        if idx == MODE_HTML_JDNEXT:
            from jd2021_installer.extractors.web_playwright import WebPlaywrightExtractor

            html_jdnext_fields = source_fields.get("html_jdnext", {}) if isinstance(source_fields, dict) else {}
            asset_html = str(html_jdnext_fields.get("asset", ""))

            if not asset_html:
                QMessageBox.warning(self, "Missing File", "Please select the JDNext Asset HTML file.")
                return None

            return WebPlaywrightExtractor(
                asset_html=asset_html,
                nohud_html=None,
                source_game="jdnext",
                config=self._config,
                quality=self._config.video_quality,
            )

        if idx == MODE_MANUAL:
            from jd2021_installer.extractors.manual_extractor import ManualExtractor
            manual_fields = source_fields.get("manual", {}) if isinstance(source_fields, dict) else {}
            codename = str(manual_fields.get("codename", "")).strip()
            root_dir = str(manual_fields.get("root", "")).strip()
            
            if not codename and not root_dir:
                QMessageBox.warning(self, "Missing Data", "Codename or Root Directory is required for Manual mode.")
                return None
                
            return ManualExtractor(
                codename=codename,
                source_type=str(source_state.get("manual_source_type", "auto")),
                root_dir=root_dir,
                files={
                    "audio": str(manual_fields.get("audio", "")).strip(),
                    "video": str(manual_fields.get("video", "")).strip(),
                    "mtrack": str(manual_fields.get("mtrack", "")).strip(),
                    "sdesc": str(manual_fields.get("sdesc", "")).strip(),
                    "dtape": str(manual_fields.get("dtape", "")).strip(),
                    "ktape": str(manual_fields.get("ktape", "")).strip(),
                    "mseq": str(manual_fields.get("mseq", "")).strip(),
                    "jdu_menuart_cover_generic": str(manual_fields.get("jdu_menuart_cover_generic", "")).strip(),
                    "jdu_menuart_cover_online": str(manual_fields.get("jdu_menuart_cover_online", "")).strip(),
                    "jdu_menuart_banner": str(manual_fields.get("jdu_menuart_banner", "")).strip(),
                    "jdu_menuart_banner_bkg": str(manual_fields.get("jdu_menuart_banner_bkg", "")).strip(),
                    "jdu_menuart_map_bkg": str(manual_fields.get("jdu_menuart_map_bkg", "")).strip(),
                    "jdu_menuart_cover_albumcoach": str(manual_fields.get("jdu_menuart_cover_albumcoach", "")).strip(),
                    "jdu_menuart_cover_albumbkg": str(manual_fields.get("jdu_menuart_cover_albumbkg", "")).strip(),
                    "jdu_menuart_coach1": str(manual_fields.get("jdu_menuart_coach1", "")).strip(),
                    "jdu_menuart_coach2": str(manual_fields.get("jdu_menuart_coach2", "")).strip(),
                    "jdu_menuart_coach3": str(manual_fields.get("jdu_menuart_coach3", "")).strip(),
                    "jdu_menuart_coach4": str(manual_fields.get("jdu_menuart_coach4", "")).strip(),
                },
                dirs={
                    "moves": str(manual_fields.get("moves", "")).strip(),
                    "pictos": str(manual_fields.get("pictos", "")).strip(),
                    "menuart": str(manual_fields.get("menuart", "")).strip(),
                    "amb": str(manual_fields.get("amb", "")).strip(),
                }
            )

        # Mode not implemented yet
        QMessageBox.information(
            self,
            "Not Implemented",
            f"The '{self._current_mode}' mode is not yet fully implemented.",
        )
        return None

    def _start_batch_install(
        self,
        selected_maps: set[str] | None = None,
        map_names: list[str] | None = None,
        fetch_codenames: list[str] | None = None,
        fetch_source: str = "jdu",
    ) -> None:
        """Launches the dedicated Batch mode worker."""
        if not self._current_target:
            return

        force_unlock_locked_status = self._ask_batch_locked_status_policy()
        if force_unlock_locked_status is None:
            self.append_log("Batch install aborted by user during locked-status policy prompt.")
            self._set_status("Install aborted")
            return
            
        from jd2021_installer.ui.workers.pipeline_workers import BatchInstallWorker

        self._lock_ui(True)
        self._feedback_panel.reset()
        if map_names:
            self._feedback_panel.set_checklist_steps(map_names)
        else:
            self._feedback_panel.set_checklist_steps(PIPELINE_STEPS)
            self._feedback_panel.update_checklist_step("Extracting map data...", StepStatus.IN_PROGRESS)

        worker = BatchInstallWorker(
            batch_source_dir=Path(self._current_target),
            target_game_dir=self._config.game_directory, # type: ignore[arg-type]
            config=self._config,
            selected_maps=selected_maps,
            fetch_codenames=fetch_codenames,
            fetch_source=fetch_source,
            force_unlock_locked_status=force_unlock_locked_status,
        )
        thread = QThread()
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progress.connect(self._feedback_panel.set_progress)
        worker.status.connect(self._on_status_updated)
        worker.discovered_maps.connect(self._feedback_panel.set_checklist_steps)
        
        # Share the error and success callbacks so the UI gets unlocked
        worker.error.connect(self._on_install_error)
        worker.finished_with_data.connect(self._on_batch_finished_with_data)
        worker.finished.connect(self._on_install_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda t=thread: self._cleanup_thread(t, "batch"))

        self._active_threads.add(thread)
        self._active_worker = worker
        thread.start()

    def _lock_ui(self, locked: bool) -> None:
        """Disable input panels while a worker is active."""
        self._mode_selector.setEnabled(not locked)
        self._config_panel.setEnabled(not locked)
        self._action_panel.set_all_enabled(not locked)
        if locked:
            self._set_preview_controls_ready(False)

    def _set_preview_controls_ready(self, ready: bool) -> None:
        self._preview_widget.setEnabled(ready)
        self._sync_refinement.setEnabled(ready)

    def _cleanup_thread(self, thread: QThread, label: str) -> None:
        logger.debug("Thread cleaned up: %s", label)
        if thread in self._active_threads:
            self._active_threads.remove(thread)
        self._active_worker = None

    def _start_file_logging(self, current_target: str) -> None:
        """Starts a dynamic FileHandler log for this installation."""
        if self._file_logger_handlers:
            self._stop_file_logging()

        # Sanitize codename for filename and cap to avoid Windows path-length issues.
        raw_target = str(current_target or "").strip()
        codename = Path(raw_target).name if raw_target else "unknown"
        codename = "".join(c for c in codename if c.isalnum() or c in ("-", "_")).strip()

        raw_fetch_tokens = [token.strip() for token in raw_target.split(",") if token.strip()]
        is_fetch_batch = len(raw_fetch_tokens) > 1
        max_codename_len = 96
        if len(codename) > max_codename_len:
            if is_fetch_batch:
                codename = f"FetchBatch_{len(raw_fetch_tokens)}"
            else:
                codename = codename[:max_codename_len]

        if not codename:
            codename = "unknown"

        logs_dir = Path("logs")
        logs_dir.mkdir(exist_ok=True)
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

        file_prefix = "install_"
        file_suffix = f"_{timestamp}.log"
        max_filename_len = 180
        max_segment_len = max(8, max_filename_len - len(file_prefix) - len(file_suffix))
        if len(codename) > max_segment_len:
            codename = codename[:max_segment_len]

        log_paths: list[Path] = []
        if is_fetch_batch:
            log_paths.append(logs_dir / f"{file_prefix}FetchBatch_{len(raw_fetch_tokens)}{file_suffix}")
            seen: set[str] = set()
            for token in raw_fetch_tokens:
                safe_token = "".join(c for c in token if c.isalnum() or c in ("-", "_")).strip()
                if not safe_token:
                    continue
                if len(safe_token) > max_segment_len:
                    safe_token = safe_token[:max_segment_len]
                key = safe_token.lower()
                if key in seen:
                    continue
                seen.add(key)
                log_paths.append(logs_dir / f"{file_prefix}{safe_token}{file_suffix}")
        else:
            log_paths.append(logs_dir / f"{file_prefix}{codename}{file_suffix}")

        level = get_file_log_level(self._config.log_detail_level)
        for log_path in log_paths:
            handler = logging.FileHandler(str(log_path), encoding="utf-8")
            handler.setLevel(level)
            logging.getLogger("jd2021").addHandler(handler)
            self._file_logger_handlers.append(handler)

        self._config.log_detail_level = apply_log_detail(self._config.log_detail_level)
        for log_path in log_paths:
            logger.info("Install log file: %s", log_path)

    def _stop_file_logging(self) -> None:
        """Removes the active FileHandler and cleanly closes handles."""
        if self._file_logger_handlers:
            for handler in self._file_logger_handlers:
                logging.getLogger("jd2021").removeHandler(handler)
                handler.close()
            self._file_logger_handlers = []

    def _set_status(self, text: str) -> None:
        self._status_bar.showMessage(text)

    # -- Public convenience methods (kept for compatibility) ----------------

    def _log_with_level(self, text: str, level: int) -> None:
        if level >= logging.ERROR:
            logger.error(text)
        elif level >= logging.WARNING:
            logger.warning(text)
        elif level <= logging.DEBUG:
            logger.debug(text)
        elif level == SUCCESS_LEVEL:
            logger.log(SUCCESS_LEVEL, text)
        else:
            logger.info(text)

    def _classify_status_level(self, text: str) -> int:
        lowered = text.strip().lower()
        # Check for ERROR patterns
        if lowered.startswith("error") or " failed" in lowered or lowered.startswith("failed"):
            return logging.ERROR
        # Check for WARNING patterns
        if lowered.startswith("warning") or " warning" in lowered:
            return logging.WARNING
        # Check for DEBUG patterns
        if lowered.startswith("debug"):
            return logging.DEBUG
        # Check for SUCCESS patterns
        success_keywords = (
            "successfully", "completed", "complete", "cleared", "cleaned up",
            "persisted", "installed", "removed", "processed"
        )
        if any(keyword in lowered for keyword in success_keywords):
            return SUCCESS_LEVEL
        return logging.INFO

    def append_log(self, text: str) -> None:
        """Append text to the GUI log console."""
        self._log_with_level(text, self._classify_status_level(text))

    def set_progress(self, value: int) -> None:
        """Set the progress bar value (delegated to ProgressLogWidget)."""
        self._feedback_panel.set_progress(value)

    def set_status(self, text: str) -> None:
        """Update the status bar message."""
        self._set_status(text)
