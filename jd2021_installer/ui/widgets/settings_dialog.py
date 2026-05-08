"""Settings dialog for the GUI installer."""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QCheckBox,
    QComboBox,
    QLineEdit,
    QFormLayout,
    QTabWidget,
    QVBoxLayout,
    QHBoxLayout,
    QPushButton,
    QLabel,
    QFileDialog,
    QMessageBox,
    QProgressDialog,
    QSpinBox,
    QDoubleSpinBox,
    QScrollArea,
    QWidget,
    QSizePolicy,
)

from jd2021_installer.core.config import AppConfig
from jd2021_installer.core.clean_data import clean_game_data
from jd2021_installer.core.localization_update import (
    resolve_console_save_path,
    update_console_localization,
)
from jd2021_installer.core.songdb_update import (
    extract_jdnext_songdb_codenames,
    extract_jdu_songdb_codenames,
    resolve_songdb_synth_path,
    synthesize_jdnext_songdb,
)

logger = logging.getLogger("jd2021.ui.widgets.settings_dialog")


class _SettingsTaskWorker(QObject):
    """Runs a blocking maintenance task in a background thread."""

    status = pyqtSignal(str)
    finished = pyqtSignal(object)
    error = pyqtSignal(str)

    def __init__(self, task: Callable[[], object], start_status: str) -> None:
        super().__init__()
        self._task = task
        self._start_status = start_status

    def run(self) -> None:
        try:
            self.status.emit(self._start_status)
            result = self._task()
            self.finished.emit(result)
        except Exception as exc:
            logger.exception("Settings task failed: %s", exc)
            self.error.emit(str(exc))


class SettingsDialog(QDialog):
    """Modal dialog for configuring application settings."""

    def __init__(
        self,
        config: AppConfig,
        parent: Optional[QWidget] = None,
        *,
        bulk_install_request: Optional[Callable[[str, list[str]], bool]] = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(788, 620)
        self.setModal(True)
        
        # We work on a copy of the config, and only return it if Save is clicked.
        # This prevents partial settings from applying on Cancel.
        # Try to use model_copy (pydantic 2) or copy (pydantic 1)
        if hasattr(config, "model_copy"):
            self._config = config.model_copy()
        elif hasattr(config, "copy"):
            self._config = config.copy()
        else:
            self._config = AppConfig(**config.dict())

        self._task_thread: Optional[QThread] = None
        self._task_worker: Optional[_SettingsTaskWorker] = None
        self._task_progress: Optional[QProgressDialog] = None
        self._task_status_timer: Optional[QTimer] = None
        self._task_status_base: str = ""
        self._task_status_dots: int = 0
        self._bulk_install_request = bulk_install_request

        self._build_ui()

    def _set_parent_status(self, text: str) -> None:
        parent = self.parent()
        if parent is None:
            return
        status_setter = getattr(parent, "set_status", None)
        if callable(status_setter):
            status_setter(text)

    def _set_task_status_text(self, text: str) -> None:
        if self._task_progress is not None:
            self._task_progress.setLabelText(text)
        self._set_parent_status(text)

    def _stop_task_status_animation(self) -> None:
        if self._task_status_timer is not None:
            self._task_status_timer.stop()
            self._task_status_timer.deleteLater()
            self._task_status_timer = None
        self._task_status_base = ""
        self._task_status_dots = 0

    def _start_task_status_animation(self, base_text: str) -> None:
        self._stop_task_status_animation()
        self._task_status_base = base_text
        self._task_status_dots = 0

        timer = QTimer(self)
        timer.setInterval(450)

        def _tick() -> None:
            self._task_status_dots = (self._task_status_dots + 1) % 4
            dots = "." * self._task_status_dots
            self._set_task_status_text(f"{self._task_status_base}{dots}")

        timer.timeout.connect(_tick)
        timer.start()
        self._task_status_timer = timer

    def _cleanup_task_state(self) -> None:
        self._stop_task_status_animation()
        if self._task_progress is not None:
            self._task_progress.close()
            self._task_progress.deleteLater()
            self._task_progress = None
        self._task_worker = None
        self._task_thread = None

    def _run_background_task(
        self,
        *,
        window_title: str,
        initial_status: str,
        task: Callable[[], object],
        on_success: Callable[[object], None],
        error_title: str,
        show_progress_dialog: bool = True,
        show_error_dialog: bool = True,
    ) -> None:
        if self._task_thread is not None and self._task_thread.isRunning():
            if show_error_dialog:
                QMessageBox.information(
                    self,
                    "Operation In Progress",
                    "Wait for the current task to finish before starting another one.",
                )
            return

        if show_progress_dialog:
            progress = QProgressDialog(initial_status, "", 0, 0, self)
            progress.setWindowTitle(window_title)
            progress.setWindowModality(Qt.WindowModality.ApplicationModal)
            progress.setMinimumDuration(0)
            progress.setCancelButton(None)
            progress.setAutoClose(False)
            progress.setAutoReset(False)
            progress.show()
            self._task_progress = progress
        else:
            self._task_progress = None
        self._set_task_status_text(initial_status)
        self._start_task_status_animation(initial_status)

        worker = _SettingsTaskWorker(task=task, start_status=initial_status)
        thread = QThread(self)
        worker.moveToThread(thread)

        def _on_status(text: str) -> None:
            self._start_task_status_animation(text)

        def _on_error(msg: str) -> None:
            if show_error_dialog:
                QMessageBox.critical(self, error_title, f"{error_title}:\n{msg}")
            else:
                logger.warning("%s: %s", error_title, msg)
            self._set_parent_status("Ready")
            thread.quit()

        def _on_finished(result: object) -> None:
            self._set_parent_status("Ready")
            on_success(result)
            thread.quit()

        thread.started.connect(worker.run)
        worker.status.connect(_on_status)
        worker.error.connect(_on_error)
        worker.finished.connect(_on_finished)
        worker.finished.connect(worker.deleteLater)
        worker.error.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._cleanup_task_state)

        self._task_worker = worker
        self._task_thread = thread
        thread.start()

    @staticmethod
    def _set_combo_from_value(combo: QComboBox, value: str) -> None:
        idx = combo.findData(value)
        if idx >= 0:
            combo.setCurrentIndex(idx)
            return

        text_idx = combo.findText(value)
        combo.setCurrentIndex(text_idx if text_idx >= 0 else 0)

    @staticmethod
    def _combo_value(combo: QComboBox) -> str:
        data = combo.currentData()
        return str(data) if data is not None else combo.currentText()

    def _make_path_picker_row(
        self,
        line_edit: QLineEdit,
        *,
        browse_title: str,
        select_directory: bool = False,
    ) -> QWidget:
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        row_layout.addWidget(line_edit, 1)

        btn_browse = QPushButton("Browse")
        btn_browse.setMinimumWidth(70)

        def _browse() -> None:
            if select_directory:
                selected = QFileDialog.getExistingDirectory(
                    self,
                    browse_title,
                    str(Path.cwd()),
                )
                if selected:
                    line_edit.setText(selected)
                return

            selected, _ = QFileDialog.getOpenFileName(
                self,
                browse_title,
                str(Path.cwd()),
                "Executables (*.exe);;All Files (*)",
            )
            if selected:
                line_edit.setText(selected)

        btn_browse.clicked.connect(_browse)
        row_layout.addWidget(btn_browse)

        btn_clear = QPushButton("Clear")
        btn_clear.setMinimumWidth(60)
        btn_clear.clicked.connect(line_edit.clear)
        row_layout.addWidget(btn_clear)

        return row

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        self.setObjectName("settingsDialog")

        title = QLabel("Installer Settings")
        title.setObjectName("settingsDialogTitle")
        layout.addWidget(title)

        subtitle = QLabel("These settings control installer behavior, defaults, and UI appearance.")
        subtitle.setObjectName("settingsDialogSubtitle")
        layout.addWidget(subtitle)

        tabs = QTabWidget()
        tabs.setObjectName("settingsDialogTabs")
        layout.addWidget(tabs, 1)

        # ----- General tab -----
        tab_general = QWidget()
        general_layout = QVBoxLayout(tab_general)
        general_layout.setContentsMargins(10, 10, 10, 10)
        general_layout.setSpacing(10)

        # skip_preflight
        self.cb_skip_preflight = QCheckBox("Skip startup pre-flight checks")
        self.cb_skip_preflight.setChecked(self._config.skip_preflight)
        self.cb_skip_preflight.setToolTip(
            "Skip the pre-flight check on app launch.\n"
            "Use this only when your setup is stable and already validated."
        )
        general_layout.addWidget(self.cb_skip_preflight)

        # suppress_offset_notification
        self.cb_suppress = QCheckBox("Hide post-install offset refinement reminder")
        self.cb_suppress.setChecked(self._config.suppress_offset_notification)
        self.cb_suppress.setToolTip(
            "Do not show the reminder popup about offset refinement\n"
            "after install completes."
        )
        general_layout.addWidget(self.cb_suppress)

        general_form = QFormLayout()
        general_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        general_form.setHorizontalSpacing(12)
        general_form.setVerticalSpacing(10)
        
        self.combo_cleanup = QComboBox()
        self.combo_cleanup.addItem("Ask me after every installation", "ask")
        self.combo_cleanup.addItem("Keep files", "keep")
        self.combo_cleanup.addItem("Auto-delete files (Keeps UI Previews)", "delete")
        self.combo_cleanup.addItem("Aggressive cleanup (Maximum space saving, disables UI Previews)", "aggressive")
        self._set_combo_from_value(self.combo_cleanup, self._config.cleanup_behavior)
        self.combo_cleanup.setToolTip(
            "Choose what happens to installer temp files after install."
        )
        general_form.addRow("After install cleanup:", self.combo_cleanup)

        self.combo_locked_status = QComboBox()
        self.combo_locked_status.addItem("Ask when needed", "ask")
        self.combo_locked_status.addItem("Always force status to 3 (unlocked)", "force3")
        self.combo_locked_status.addItem("Keep original status values", "keep")
        self._set_combo_from_value(self.combo_locked_status, self._config.locked_status_behavior)
        self.combo_locked_status.setToolTip(
            "Controls how the installer treats locked-song status values\n"
            "when importing maps."
        )
        general_form.addRow("Song unlock status:", self.combo_locked_status)

        # show_preflight_success_popup
        self.cb_preflight_popup = QCheckBox("Show pre-flight success popup")
        self.cb_preflight_popup.setChecked(self._config.show_preflight_success_popup)
        self.cb_preflight_popup.setToolTip(
            "If disabled, passing pre-flight only enables Install\n"
            "without opening a confirmation popup."
        )
        general_layout.addWidget(self.cb_preflight_popup)

        self.cb_install_summary = QCheckBox("Show installation summary popup")
        self.cb_install_summary.setChecked(getattr(self._config, "show_install_summary_popup", True))
        self.cb_install_summary.setToolTip(
            "Shows a checklist-style summary at the end of install with\n"
            "required/optional files, counts, and warnings."
        )
        general_layout.addWidget(self.cb_install_summary)

        # show_quickstart_on_launch
        self.cb_quickstart = QCheckBox("Show quick-start help on launch")
        self.cb_quickstart.setChecked(self._config.show_quickstart_on_launch)
        self.cb_quickstart.setToolTip(
            "Shows a short beginner guide at startup.\n"
            "Helpful for users who skip documentation."
        )
        general_layout.addWidget(self.cb_quickstart)

        # convert_jdnext_gestures
        self.cb_convert_jdnext_gestures = QCheckBox("Automatically convert JDNext 2D Gestures to legacy format")
        self.cb_convert_jdnext_gestures.setChecked(getattr(self._config, "convert_jdnext_gestures", True))
        self.cb_convert_jdnext_gestures.setToolTip(
            "During pipeline conversion, translates Next-gen camera motion tracking\n"
            "into X360 volumetric depth data format for legacy compatibility."
        )
        general_layout.addWidget(self.cb_convert_jdnext_gestures)

        # No strictness slider — hybrid gestures use synthesized gating edges
        # and donor templates. The checkbox above toggles conversion only.

        # albumcoach_behavior
        self.combo_albumcoach = QComboBox()
        self.combo_albumcoach.addItem("Ask per map (default)", "ask")
        self.combo_albumcoach.addItem("Show Customizer", "always_customize")
        self.combo_albumcoach.addItem("Use Default (25%)", "always_default")
        self._set_combo_from_value(
            self.combo_albumcoach,
            getattr(self._config, "albumcoach_behavior", "ask"),
        )
        self.combo_albumcoach.setToolTip(
            "Controls how the AlbumCoach composite texture is generated\n"
            "for JDNext multi-coach maps that are missing one.\n"
            "Ask: prompt before each install.\n"
            "Always Customize: always open the editor.\n"
            "Always Default: use automatic compositing silently."
        )
        general_form.addRow("AlbumCoach compositing:", self.combo_albumcoach)

        # jdnext_cover_behavior
        self.combo_jdnext_cover = QComboBox()
        self.combo_jdnext_cover.addItem("Ask per map (default)", "ask")
        self.combo_jdnext_cover.addItem("Use Synthesized Cover (map background + Title)", "synthesized")
        self.combo_jdnext_cover.addItem("Use Original Cover (may be squished)", "original")
        self._set_combo_from_value(
            self.combo_jdnext_cover,
            getattr(self._config, "jdnext_cover_behavior", "ask"),
        )
        self.combo_jdnext_cover.setToolTip(
            "Controls how cover art is generated for JDNext maps.\n"
            "Synthesized: composites map_bkg + Title into a proper 1:1 square.\n"
            "Original: uses the source cover as-is (may appear squished in-game).\n"
            "Ask: prompt before each JDNext install."
        )
        general_form.addRow("JDNext cover art:", self.combo_jdnext_cover)

        self.combo_log_detail = QComboBox()
        self.combo_log_detail.addItem("Quiet (warnings and errors only)", "quiet")
        self.combo_log_detail.addItem("Normal (recommended)", "user")
        self.combo_log_detail.addItem("Detailed (extra debug in logs)", "detailed")
        self.combo_log_detail.addItem("Developer (maximum verbosity)", "developer")
        self._set_combo_from_value(self.combo_log_detail, self._config.log_detail_level)
        self.combo_log_detail.setToolTip(
            "Controls how much detail appears in the app and log files."
        )
        general_form.addRow("Log detail level:", self.combo_log_detail)

        self.combo_theme = QComboBox()
        self.combo_theme.addItem("Light", "light")
        self.combo_theme.addItem("Dark", "dark")
        self._set_combo_from_value(self.combo_theme, self._config.theme)
        self.combo_theme.setToolTip(
            "Pick the installer color theme."
        )
        general_form.addRow("Theme:", self.combo_theme)

        general_layout.addLayout(general_form)
        general_layout.addStretch()

        tabs.addTab(tab_general, "General")

        # ----- Window tab -----
        tab_window = QWidget()
        window_layout = QVBoxLayout(tab_window)
        window_layout.setContentsMargins(10, 10, 10, 10)
        window_layout.setSpacing(10)

        # minimum window size policy
        self.cb_enforce_min_size = QCheckBox("Enforce minimum window size")
        self.cb_enforce_min_size.setChecked(self._config.enforce_min_window_size)
        self.cb_enforce_min_size.setToolTip(
            "When disabled, the main window can be resized smaller than the default minimum."
        )
        window_layout.addWidget(self.cb_enforce_min_size)

        window_form = QFormLayout()
        window_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        window_form.setHorizontalSpacing(12)
        window_form.setVerticalSpacing(10)

        min_size_row = QHBoxLayout()
        min_size_row.setSpacing(8)

        self.spin_min_width = QSpinBox()
        self.spin_min_width.setRange(640, 3840)
        self.spin_min_width.setValue(self._config.min_window_width)
        self.spin_min_width.setSuffix(" px")
        min_size_row.addWidget(self.spin_min_width)

        min_size_row.addWidget(QLabel("x"))

        self.spin_min_height = QSpinBox()
        self.spin_min_height.setRange(480, 2160)
        self.spin_min_height.setValue(self._config.min_window_height)
        self.spin_min_height.setSuffix(" px")
        min_size_row.addWidget(self.spin_min_height)
        min_size_row.addStretch()

        min_size_widget = QWidget()
        min_size_widget.setLayout(min_size_row)
        window_form.addRow("Minimum window size:", min_size_widget)
        window_layout.addLayout(window_form)

        def _toggle_min_size_inputs(enabled: bool) -> None:
            self.spin_min_width.setEnabled(enabled)
            self.spin_min_height.setEnabled(enabled)

        self.cb_enforce_min_size.toggled.connect(_toggle_min_size_inputs)
        _toggle_min_size_inputs(self.cb_enforce_min_size.isChecked())

        self.cb_size_overlay = QCheckBox("Show floating current window size while resizing")
        self.cb_size_overlay.setChecked(
            getattr(self._config, "show_window_size_overlay", True)
        )
        self.cb_size_overlay.setToolTip(
            "Displays an overlay like 1280 x 720 when you resize the main window."
        )
        window_layout.addWidget(self.cb_size_overlay)

        self.cb_style_debug = QCheckBox("Enable Style Debug Mode (outline sections)")
        self.cb_style_debug.setChecked(
            getattr(self._config, "style_debug_mode", False)
        )
        self.cb_style_debug.setToolTip(
            "Adds colored outlines and section labels to help map widgets to QSS selectors.\n"
            "While enabled, stylesheet edits auto-reload as you save.\n"
            "Use while tuning colors, then disable for normal appearance."
        )
        window_layout.addWidget(self.cb_style_debug)
        window_layout.addStretch()

        tabs.addTab(tab_window, "Window")

        # ----- Media tab -----
        tab_media = QWidget()
        media_layout = QVBoxLayout(tab_media)
        media_layout.setContentsMargins(10, 10, 10, 10)
        media_layout.setSpacing(10)

        media_form = QFormLayout()
        media_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        media_form.setHorizontalSpacing(12)
        media_form.setVerticalSpacing(10)

        self.combo_quality = QComboBox()
        self.combo_quality.addItems([
            "ULTRA_HD", "ULTRA", "HIGH_HD", "HIGH",
            "MID_HD", "MID", "LOW_HD", "LOW"
        ])
        self.combo_quality.setCurrentText(self._config.video_quality)
        media_form.addRow("Default download quality:", self.combo_quality)

        self.combo_hwaccel = QComboBox()
        self.combo_hwaccel.addItems(["auto", "none"])
        self.combo_hwaccel.setCurrentText(getattr(self._config, "ffmpeg_hwaccel", "auto"))
        self.combo_hwaccel.setToolTip(
            "auto: let FFmpeg pick available hardware decoding acceleration\n"
            "none: disable hardware acceleration"
        )
        media_form.addRow("FFmpeg acceleration:", self.combo_hwaccel)

        self.combo_vp9_mode = QComboBox()
        self.combo_vp9_mode.addItem("Re-encode VP9 to VP8 (best compatibility)", "reencode_to_vp8")
        self.combo_vp9_mode.addItem("Use next compatible quality down (no re-encode)", "fallback_compatible_down")
        current_vp9_mode = getattr(self._config, "vp9_handling_mode", "reencode_to_vp8")
        vp9_idx = self.combo_vp9_mode.findData(current_vp9_mode)
        self.combo_vp9_mode.setCurrentIndex(vp9_idx if vp9_idx >= 0 else 0)
        self.combo_vp9_mode.setToolTip(
            "Re-encode VP9 to VP8: keeps requested tier but may reduce quality.\n"
            "Next compatible down: skips VP9 tiers and picks a lower HD-compatible tier."
        )
        media_form.addRow("VP9 handling:", self.combo_vp9_mode)

        self.combo_preview_mode = QComboBox()
        self.combo_preview_mode.addItem("Low-res proxy (faster and smoother)", "proxy_low")
        self.combo_preview_mode.addItem("Original video file", "original")
        self._set_combo_from_value(
            self.combo_preview_mode,
            getattr(self._config, "preview_video_mode", "proxy_low"),
        )
        self.combo_preview_mode.setToolTip(
            "Choose whether preview uses a generated proxy or the source file."
        )
        media_form.addRow("Preview source:", self.combo_preview_mode)

        media_layout.addLayout(media_form)
        media_layout.addStretch()

        tabs.addTab(tab_media, "Media")

        # ----- Advanced tab -----
        tab_advanced = QWidget()
        tab_advanced_layout = QVBoxLayout(tab_advanced)
        tab_advanced_layout.setContentsMargins(0, 0, 0, 0)
        tab_advanced_layout.setSpacing(0)

        advanced_scroll = QScrollArea(tab_advanced)
        advanced_scroll.setWidgetResizable(True)
        advanced_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        tab_advanced_layout.addWidget(advanced_scroll)

        advanced_content = QWidget()
        advanced_scroll.setWidget(advanced_content)

        advanced_layout = QVBoxLayout(advanced_content)
        advanced_layout.setContentsMargins(10, 10, 10, 10)
        advanced_layout.setSpacing(10)

        advanced_note = QLabel(
            "Advanced runtime behavior for downloads, preview timing, and external tool resolution. "
            "Core engine constants remain JSON-only."
        )
        advanced_note.setWordWrap(True)
        advanced_layout.addWidget(advanced_note)

        advanced_form = QFormLayout()
        advanced_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        advanced_form.setHorizontalSpacing(12)
        advanced_form.setVerticalSpacing(10)

        self.txt_ffmpeg_path = QLineEdit(str(getattr(self._config, "ffmpeg_path", "ffmpeg") or "ffmpeg"))
        self.txt_ffmpeg_path.setPlaceholderText("ffmpeg")
        self.txt_ffmpeg_path.setToolTip(
            "FFmpeg executable path or command name. Clear to use auto/default resolution."
        )
        advanced_form.addRow(
            "FFmpeg executable:",
            self._make_path_picker_row(
                self.txt_ffmpeg_path,
                browse_title="Select FFmpeg executable",
            ),
        )

        self.txt_ffprobe_path = QLineEdit(str(getattr(self._config, "ffprobe_path", "ffprobe") or "ffprobe"))
        self.txt_ffprobe_path.setPlaceholderText("ffprobe")
        self.txt_ffprobe_path.setToolTip(
            "FFprobe executable path or command name. Clear to use auto/default resolution."
        )
        advanced_form.addRow(
            "FFprobe executable:",
            self._make_path_picker_row(
                self.txt_ffprobe_path,
                browse_title="Select FFprobe executable",
            ),
        )

        self.txt_vgmstream_path = QLineEdit(str(getattr(self._config, "vgmstream_path", "") or ""))
        self.txt_vgmstream_path.setPlaceholderText("Auto (tools/vgmstream or PATH)")
        self.txt_vgmstream_path.setToolTip(
            "Optional vgmstream CLI executable for XMA2 decode. Leave empty for auto-detection."
        )
        advanced_form.addRow(
            "vgmstream executable:",
            self._make_path_picker_row(
                self.txt_vgmstream_path,
                browse_title="Select vgmstream executable",
            ),
        )

        third_party_root = getattr(self._config, "third_party_tools_root", None)
        self.txt_third_party_root = QLineEdit(str(third_party_root) if third_party_root else "")
        self.txt_third_party_root.setPlaceholderText("Auto (./tools)")
        self.txt_third_party_root.setToolTip(
            "Optional root directory for JDNext third-party tools. Leave empty for default auto path."
        )
        advanced_form.addRow(
            "3rd-party tools root:",
            self._make_path_picker_row(
                self.txt_third_party_root,
                browse_title="Select third-party tools root",
                select_directory=True,
            ),
        )

        self.txt_assetstudio_cli = QLineEdit(str(getattr(self._config, "assetstudio_cli_path", "") or ""))
        self.txt_assetstudio_cli.setPlaceholderText("Auto (search under 3rd-party tools root)")
        self.txt_assetstudio_cli.setToolTip(
            "Optional direct AssetStudioModCLI executable path for JDNext bundle extraction."
        )
        advanced_form.addRow(
            "AssetStudio CLI:",
            self._make_path_picker_row(
                self.txt_assetstudio_cli,
                browse_title="Select AssetStudioModCLI executable",
            ),
        )

        self.spin_download_timeout = QSpinBox()
        self.spin_download_timeout.setRange(15, 3600)
        self.spin_download_timeout.setValue(int(getattr(self._config, "download_timeout_s", 600)))
        self.spin_download_timeout.setSuffix(" s")
        self.spin_download_timeout.setToolTip(
            "Maximum wait time for network downloads before timeout."
        )
        advanced_form.addRow("Download timeout:", self.spin_download_timeout)

        self.spin_max_retries = QSpinBox()
        self.spin_max_retries.setRange(0, 12)
        self.spin_max_retries.setValue(int(getattr(self._config, "max_retries", 3)))
        self.spin_max_retries.setToolTip(
            "How many retry attempts are allowed for failed downloads."
        )
        advanced_form.addRow("Download retries:", self.spin_max_retries)

        self.spin_retry_base_delay = QSpinBox()
        self.spin_retry_base_delay.setRange(0, 60)
        self.spin_retry_base_delay.setValue(int(getattr(self._config, "retry_base_delay_s", 2)))
        self.spin_retry_base_delay.setSuffix(" s")
        self.spin_retry_base_delay.setToolTip(
            "Base delay used for retry backoff after failed network requests."
        )
        advanced_form.addRow("Retry base delay:", self.spin_retry_base_delay)

        self.spin_inter_request_delay = QDoubleSpinBox()
        self.spin_inter_request_delay.setRange(0.0, 20.0)
        self.spin_inter_request_delay.setDecimals(2)
        self.spin_inter_request_delay.setSingleStep(0.1)
        self.spin_inter_request_delay.setValue(float(getattr(self._config, "inter_request_delay_s", 1.5)))
        self.spin_inter_request_delay.setSuffix(" s")
        self.spin_inter_request_delay.setToolTip(
            "Delay inserted between sequential download requests."
        )
        advanced_form.addRow("Inter-request delay:", self.spin_inter_request_delay)

        self.spin_fetch_login_timeout = QSpinBox()
        self.spin_fetch_login_timeout.setRange(30, 1800)
        self.spin_fetch_login_timeout.setValue(int(getattr(self._config, "fetch_login_timeout_s", 300)))
        self.spin_fetch_login_timeout.setSuffix(" s")
        self.spin_fetch_login_timeout.setToolTip(
            "How long Fetch mode waits for Discord login before giving up."
        )
        advanced_form.addRow("Fetch login timeout:", self.spin_fetch_login_timeout)

        self.spin_fetch_bot_timeout = QSpinBox()
        self.spin_fetch_bot_timeout.setRange(10, 600)
        self.spin_fetch_bot_timeout.setValue(int(getattr(self._config, "fetch_bot_response_timeout_s", 60)))
        self.spin_fetch_bot_timeout.setSuffix(" s")
        self.spin_fetch_bot_timeout.setToolTip(
            "How long Fetch mode waits for bot links before timing out."
        )
        advanced_form.addRow("Fetch bot response timeout:", self.spin_fetch_bot_timeout)

        self.spin_overlay_timeout = QSpinBox()
        self.spin_overlay_timeout.setRange(200, 6000)
        self.spin_overlay_timeout.setSingleStep(100)
        self.spin_overlay_timeout.setValue(int(getattr(self._config, "window_size_overlay_timeout_ms", 1100)))
        self.spin_overlay_timeout.setSuffix(" ms")
        self.spin_overlay_timeout.setToolTip(
            "How long the floating window size indicator remains visible after resize stops."
        )
        advanced_form.addRow("Window size overlay timeout:", self.spin_overlay_timeout)

        self.spin_preview_fps = QSpinBox()
        self.spin_preview_fps.setRange(12, 120)
        self.spin_preview_fps.setValue(int(getattr(self._config, "preview_fps", 25)))
        self.spin_preview_fps.setToolTip(
            "Default preview FPS when source metadata does not force a specific value."
        )
        advanced_form.addRow("Preview FPS:", self.spin_preview_fps)

        self.spin_preview_audio_only_offset = QDoubleSpinBox()
        self.spin_preview_audio_only_offset.setRange(-2000.0, 2000.0)
        self.spin_preview_audio_only_offset.setDecimals(1)
        self.spin_preview_audio_only_offset.setSingleStep(5.0)
        self.spin_preview_audio_only_offset.setValue(float(getattr(self._config, "preview_only_audio_offset_ms", -125.0)))
        self.spin_preview_audio_only_offset.setSuffix(" ms")
        self.spin_preview_audio_only_offset.setToolTip(
            "Offset nudge used when previewing audio-only mode."
        )
        advanced_form.addRow("Audio-only preview offset:", self.spin_preview_audio_only_offset)

        self.spin_audio_preview_fade = QDoubleSpinBox()
        self.spin_audio_preview_fade.setRange(0.0, 10.0)
        self.spin_audio_preview_fade.setDecimals(2)
        self.spin_audio_preview_fade.setSingleStep(0.1)
        self.spin_audio_preview_fade.setValue(float(getattr(self._config, "audio_preview_fade_s", 2.0)))
        self.spin_audio_preview_fade.setSuffix(" s")
        self.spin_audio_preview_fade.setToolTip(
            "Fade duration used for generated audio preview assets."
        )
        advanced_form.addRow("Audio preview fade:", self.spin_audio_preview_fade)

        advanced_layout.addLayout(advanced_form)
        advanced_layout.addStretch()

        tabs.addTab(tab_advanced, "Advanced")

        # ----- Integrations tab -----
        tab_integrations = QWidget()
        integrations_layout = QVBoxLayout(tab_integrations)
        integrations_layout.setContentsMargins(10, 10, 10, 10)
        integrations_layout.setSpacing(10)

        integrations_form = QFormLayout()
        integrations_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        integrations_form.setHorizontalSpacing(12)
        integrations_form.setVerticalSpacing(10)

        self.txt_discord_url = QLineEdit()
        self.txt_discord_url.setText(self._config.discord_channel_url)
        self.txt_discord_url.setPlaceholderText("https://discord.com/channels/...")
        self.txt_discord_url.setToolTip(
            "The URL of the Discord channel where the JDU asset bot lives.\n"
            "Required for Fetch (Codename) mode.\n"
            "Copy from your browser's address bar while in the channel."
        )
        integrations_form.addRow("Discord channel URL:", self.txt_discord_url)
        integrations_layout.addLayout(integrations_form)

        localization_row = QHBoxLayout()
        localization_row.addWidget(QLabel("Update in-game localization from JSON:"))
        self.btn_update_localization = QPushButton("Update In-Game Localization...")
        self.btn_update_localization.clicked.connect(self._on_update_localization)
        localization_row.addWidget(self.btn_update_localization)
        localization_row.addStretch()
        integrations_layout.addLayout(localization_row)

        songdb_row = QHBoxLayout()
        songdb_row.addWidget(QLabel("Update JDNext song database cache from JSON:"))
        self.btn_update_songdb = QPushButton("Update Song Database...")
        self.btn_update_songdb.clicked.connect(self._on_update_songdb)
        songdb_row.addWidget(self.btn_update_songdb)
        songdb_row.addStretch()
        integrations_layout.addLayout(songdb_row)

        bulk_jdu_row = QHBoxLayout()
        bulk_jdu_row.addWidget(QLabel("Bulk install all JDU maps from songdb JSON:"))
        self.btn_bulk_install_jdu_songdb = QPushButton("Install All JDU Maps...")
        self.btn_bulk_install_jdu_songdb.clicked.connect(self._on_bulk_install_jdu_songdb)
        self.btn_bulk_install_jdu_songdb.setToolTip(
            "Pick a JDU songdb JSON and queue every codename through Fetch (Codename) mode."
        )
        bulk_jdu_row.addWidget(self.btn_bulk_install_jdu_songdb)
        bulk_jdu_row.addStretch()
        integrations_layout.addLayout(bulk_jdu_row)

        bulk_jdnext_row = QHBoxLayout()
        bulk_jdnext_row.addWidget(QLabel("Bulk install all JDNext maps from songdb JSON:"))
        self.btn_bulk_install_jdnext_songdb = QPushButton("Install All JDNext Maps...")
        self.btn_bulk_install_jdnext_songdb.clicked.connect(self._on_bulk_install_jdnext_songdb)
        self.btn_bulk_install_jdnext_songdb.setToolTip(
            "Pick a JDNext songdb JSON and queue every mapName through Fetch JDNext mode."
        )
        bulk_jdnext_row.addWidget(self.btn_bulk_install_jdnext_songdb)
        bulk_jdnext_row.addStretch()
        integrations_layout.addLayout(bulk_jdnext_row)

        clean_data_row = QHBoxLayout()
        clean_data_row.addWidget(QLabel("Reset installed custom maps and caches:"))
        self.btn_clean_data = QPushButton("Clean Game Data...")
        self.btn_clean_data.clicked.connect(self._on_clean_game_data)
        clean_data_row.addWidget(self.btn_clean_data)
        clean_data_row.addStretch()
        integrations_layout.addLayout(clean_data_row)

        downloads_row = QHBoxLayout()
        downloads_row.addWidget(QLabel("Clear downloaded source maps folder:"))
        self.btn_clear_mapdownloads = QPushButton("Clear mapDownloads...")
        self.btn_clear_mapdownloads.clicked.connect(self._on_clear_map_downloads)
        downloads_row.addWidget(self.btn_clear_mapdownloads)
        downloads_row.addStretch()
        integrations_layout.addLayout(downloads_row)

        cache_row = QHBoxLayout()
        cache_row.addWidget(QLabel("Clear installer cache and readjust index:"))
        self.btn_clear_cache = QPushButton("Clear Cache...")
        self.btn_clear_cache.clicked.connect(self._on_clear_cache)
        cache_row.addWidget(self.btn_clear_cache)
        cache_row.addStretch()
        integrations_layout.addLayout(cache_row)

        integrations_layout.addStretch()

        tabs.addTab(tab_integrations, "Integrations")

        # ----- Updates tab -----
        tab_updates = QWidget()
        updates_layout = QVBoxLayout(tab_updates)
        updates_layout.setContentsMargins(10, 10, 10, 10)
        updates_layout.setSpacing(10)

        updates_note = QLabel(
            "Check for new versions from the GitHub repository. "
            "Updates are applied via git pull (if available) or zip download."
        )
        updates_note.setWordWrap(True)
        updates_layout.addWidget(updates_note)

        # Version info (read-only)
        self._lbl_update_branch = QLabel("Branch: detecting...")
        self._lbl_update_commit = QLabel("Commit: detecting...")
        self._lbl_update_source = QLabel("Source: detecting...")
        for lbl in (self._lbl_update_branch, self._lbl_update_commit, self._lbl_update_source):
            lbl.setStyleSheet("color: #888; font-size: 11px;")
            updates_layout.addWidget(lbl)
        self._populate_version_info()

        # Check on launch toggle
        self.cb_check_updates_on_launch = QCheckBox("Check for updates on launch")
        self.cb_check_updates_on_launch.setChecked(
            getattr(self._config, "check_updates_on_launch", True)
        )
        self.cb_check_updates_on_launch.setToolTip(
            "When enabled, the installer will silently check for updates\n"
            "every time it starts. You will only be notified if a new\n"
            "version is available."
        )
        updates_layout.addWidget(self.cb_check_updates_on_launch)

        # Branch selector
        branch_form = QFormLayout()
        branch_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        branch_form.setHorizontalSpacing(12)
        branch_form.setVerticalSpacing(10)

        branch_row = QHBoxLayout()
        self.combo_update_branch = QComboBox()
        self.combo_update_branch.setMinimumWidth(200)
        self.combo_update_branch.setToolTip(
            "Select which branch to track for updates.\n"
            "Switching branches will check for updates on the new branch."
        )
        branch_row.addWidget(self.combo_update_branch, 1)

        self.btn_refresh_branches = QPushButton("Refresh")
        self.btn_refresh_branches.setMinimumWidth(80)
        self.btn_refresh_branches.setToolTip("Fetch the list of available branches from GitHub.")
        self.btn_refresh_branches.clicked.connect(self._on_refresh_branches)
        branch_row.addWidget(self.btn_refresh_branches)

        branch_widget = QWidget()
        branch_widget.setLayout(branch_row)
        branch_form.addRow("Update branch:", branch_widget)
        updates_layout.addLayout(branch_form)

        # Pre-populate branch combo with current branch
        self._populate_branch_combo_initial()

        # Connect branch change to trigger a check
        self.combo_update_branch.currentTextChanged.connect(self._on_branch_selection_changed)

        # Fetch all available branches immediately in the background so users
        # can switch without needing to click Refresh first.
        self._refresh_branches_in_background(silent=True)

        # Manual check button
        check_row = QHBoxLayout()
        self.btn_check_updates = QPushButton("Check for Updates")
        self.btn_check_updates.setMinimumWidth(160)
        self.btn_check_updates.clicked.connect(self._on_check_updates)
        check_row.addWidget(self.btn_check_updates)
        check_row.addStretch()
        updates_layout.addLayout(check_row)

        updates_layout.addStretch()

        tabs.addTab(tab_updates, "Updates")

        tabs.setCurrentIndex(0)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        btn_save = QPushButton("Save Settings")
        btn_save.setMinimumWidth(80)
        btn_save.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        btn_save.clicked.connect(self._on_save)
        btn_layout.addWidget(btn_save)

        btn_cancel = QPushButton("Cancel")
        btn_cancel.setMinimumWidth(80)
        btn_cancel.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        btn_cancel.clicked.connect(self.reject)
        btn_layout.addWidget(btn_cancel)

        layout.addLayout(btn_layout)

    def _on_save(self) -> None:
        self._config.skip_preflight = self.cb_skip_preflight.isChecked()
        self._config.suppress_offset_notification = self.cb_suppress.isChecked()
        self._config.cleanup_behavior = self._combo_value(self.combo_cleanup)
        self._config.locked_status_behavior = self._combo_value(self.combo_locked_status)
        self._config.show_preflight_success_popup = self.cb_preflight_popup.isChecked()
        self._config.show_install_summary_popup = self.cb_install_summary.isChecked()
        self._config.show_quickstart_on_launch = self.cb_quickstart.isChecked()
        self._config.convert_jdnext_gestures = self.cb_convert_jdnext_gestures.isChecked()
        self._config.albumcoach_behavior = self._combo_value(self.combo_albumcoach)
        self._config.jdnext_cover_behavior = self._combo_value(self.combo_jdnext_cover)
        self._config.log_detail_level = self._combo_value(self.combo_log_detail)
        self._config.theme = self._combo_value(self.combo_theme)
        self._config.enforce_min_window_size = self.cb_enforce_min_size.isChecked()
        self._config.min_window_width = self.spin_min_width.value()
        self._config.min_window_height = self.spin_min_height.value()
        self._config.show_window_size_overlay = self.cb_size_overlay.isChecked()
        self._config.style_debug_mode = self.cb_style_debug.isChecked()
        self._config.video_quality = self.combo_quality.currentText()
        self._config.ffmpeg_hwaccel = self.combo_hwaccel.currentText()
        self._config.vp9_handling_mode = str(self.combo_vp9_mode.currentData())
        self._config.preview_video_mode = self._combo_value(self.combo_preview_mode)
        self._config.discord_channel_url = self.txt_discord_url.text().strip()
        self._config.ffmpeg_path = self.txt_ffmpeg_path.text().strip() or "ffmpeg"
        self._config.ffprobe_path = self.txt_ffprobe_path.text().strip() or "ffprobe"
        self._config.vgmstream_path = self.txt_vgmstream_path.text().strip() or None
        self._config.assetstudio_cli_path = self.txt_assetstudio_cli.text().strip() or None
        third_party_root_text = self.txt_third_party_root.text().strip()
        self._config.third_party_tools_root = (
            Path(third_party_root_text).expanduser() if third_party_root_text else None
        )
        self._config.download_timeout_s = self.spin_download_timeout.value()
        self._config.max_retries = self.spin_max_retries.value()
        self._config.retry_base_delay_s = self.spin_retry_base_delay.value()
        self._config.inter_request_delay_s = self.spin_inter_request_delay.value()
        self._config.fetch_login_timeout_s = self.spin_fetch_login_timeout.value()
        self._config.fetch_bot_response_timeout_s = self.spin_fetch_bot_timeout.value()
        self._config.window_size_overlay_timeout_ms = self.spin_overlay_timeout.value()
        self._config.preview_fps = self.spin_preview_fps.value()
        self._config.preview_only_audio_offset_ms = self.spin_preview_audio_only_offset.value()
        self._config.audio_preview_fade_s = self.spin_audio_preview_fade.value()
        self._config.check_updates_on_launch = self.cb_check_updates_on_launch.isChecked()
        selected_branch = self.combo_update_branch.currentText().strip()
        if selected_branch:
            self._config.update_branch = selected_branch
        
        self.accept()

    # ==================================================================
    # UPDATES TAB HELPERS
    # ==================================================================

    def _get_updater(self):
        """Lazily import and create an Updater instance."""
        import sys
        sys.path.insert(0, str(self._project_root()))
        try:
            from updater import Updater
        finally:
            sys.path.pop(0)
        return Updater(self._project_root())

    def _populate_version_info(self) -> None:
        """Fill in the version info labels from the current environment."""
        try:
            updater = self._get_updater()
            branch = updater.get_current_branch()
            commit = updater.get_current_commit()
            is_git = updater.is_git_repo()
            self._lbl_update_branch.setText(f"Branch: {branch}")
            self._lbl_update_commit.setText(f"Commit: {commit}")
            source = "git repo" if is_git else "zip (no .git found)"
            self._lbl_update_source.setText(f"Source: {source}")
        except Exception as exc:
            logger.debug("Could not populate version info: %s", exc)
            self._lbl_update_branch.setText("Branch: unknown")
            self._lbl_update_commit.setText("Commit: unknown")
            self._lbl_update_source.setText("Source: unknown")

    def _populate_branch_combo_initial(self) -> None:
        """Set the branch combo to the current branch without a network call."""
        preferred = (getattr(self._config, "update_branch", "") or "").strip()
        try:
            updater = self._get_updater()
            current = updater.get_current_branch()
        except Exception:
            current = "v2"

        initial = preferred or current

        self.combo_update_branch.blockSignals(True)
        self.combo_update_branch.clear()
        self.combo_update_branch.addItem(initial)
        self.combo_update_branch.setCurrentText(initial)
        self.combo_update_branch.blockSignals(False)

    def _on_refresh_branches(self) -> None:
        """Fetch branch list from GitHub in a background thread."""
        self._refresh_branches_in_background(silent=False)

    def _refresh_branches_in_background(self, *, silent: bool) -> None:
        """Fetch branch list in the background, optionally without UI prompts."""
        if not silent:
            self.btn_refresh_branches.setEnabled(False)
            self.btn_refresh_branches.setText("Fetching...")

        def _task() -> object:
            updater = self._get_updater()
            return updater.fetch_remote_branches()

        def _on_success(branches: object) -> None:
            if not silent:
                self.btn_refresh_branches.setEnabled(True)
                self.btn_refresh_branches.setText("Refresh")
            if not branches:
                if not silent:
                    QMessageBox.warning(
                        self,
                        "Branch Fetch Failed",
                        "Could not fetch branches from GitHub.\n"
                        "Check your internet connection and try again.",
                    )
                return

            current_text = self.combo_update_branch.currentText()
            self.combo_update_branch.blockSignals(True)
            self.combo_update_branch.clear()
            for b in branches:
                self.combo_update_branch.addItem(b)
            # Restore selection
            idx = self.combo_update_branch.findText(current_text)
            if idx >= 0:
                self.combo_update_branch.setCurrentIndex(idx)
            self.combo_update_branch.blockSignals(False)

        self._run_background_task(
            window_title="Fetching Branches",
            initial_status="Fetching branches from GitHub",
            task=_task,
            on_success=_on_success,
            error_title="Branch Fetch Failed",
            show_progress_dialog=not silent,
            show_error_dialog=not silent,
        )

    def _on_branch_selection_changed(self, branch: str) -> None:
        """Handle branch combo selection change — switch branch and check."""
        if not branch or not branch.strip():
            return

        try:
            updater = self._get_updater()
            current = updater.get_current_branch()
        except Exception:
            current = ""

        if branch == current:
            return

        # Confirm branch switch
        reply = QMessageBox.question(
            self,
            "Switch Branch",
            f"Switch from '{current}' to '{branch}'?\n\n"
            "This will check for updates on the new branch.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply != QMessageBox.StandardButton.Yes:
            # Revert combo selection
            self.combo_update_branch.blockSignals(True)
            idx = self.combo_update_branch.findText(current)
            if idx >= 0:
                self.combo_update_branch.setCurrentIndex(idx)
            self.combo_update_branch.blockSignals(False)
            return

        def _task() -> object:
            u = self._get_updater()
            return u.switch_branch(branch)

        def _on_success(check_result: object) -> None:
            # Update version labels
            self._lbl_update_branch.setText(f"Branch: {branch}")
            try:
                u = self._get_updater()
                self._lbl_update_commit.setText(f"Commit: {u.get_current_commit()}")
            except Exception:
                pass

            # Show update result
            from jd2021_installer.ui.widgets.update_dialog import UpdateResultDialog
            dialog = UpdateResultDialog(check_result, self._get_updater(), self)
            dialog.exec()

        self._run_background_task(
            window_title="Switching Branch",
            initial_status=f"Switching to branch '{branch}'",
            task=_task,
            on_success=_on_success,
            error_title="Branch Switch Failed",
        )

    def _on_check_updates(self) -> None:
        """Run a manual update check in a background thread."""
        branch = self.combo_update_branch.currentText().strip()

        def _task() -> object:
            updater = self._get_updater()
            return updater.check_for_updates(branch or None)

        def _on_success(check_result: object) -> None:
            from jd2021_installer.ui.widgets.update_dialog import UpdateResultDialog
            dialog = UpdateResultDialog(check_result, self._get_updater(), self)
            dialog.exec()

        self._run_background_task(
            window_title="Checking for Updates",
            initial_status="Checking for updates",
            task=_task,
            on_success=_on_success,
            error_title="Update Check Failed",
        )

    def _on_update_localization(self) -> None:
        if not self._config.game_directory:
            QMessageBox.warning(
                self,
                "Game Directory Required",
                "Set your JD2021 game directory first, then try localization update again.",
            )
            return

        default_dir = str(Path.cwd())
        selected_file, _ = QFileDialog.getOpenFileName(
            self,
            "Select localisation JSON",
            default_dir,
            "JSON Files (*.json);;All Files (*)",
        )
        if not selected_file:
            return

        confirm = QMessageBox.question(
            self,
            "Confirm Localization Update",
            "Use this file to update in-game localization?\n\n"
            f"Source: {selected_file}\n\n"
            "A backup of ConsoleSave.json will be created before updating.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        try:
            console_save_path = resolve_console_save_path(Path(self._config.game_directory))
        except Exception as exc:
            logger.exception("Localization update failed: %s", exc)
            QMessageBox.critical(
                self,
                "Localization Update Failed",
                f"Could not update localization:\n{exc}",
            )
            return

        def _task() -> object:
            return update_console_localization(Path(selected_file), console_save_path)

        def _on_success(result: object) -> None:
            logger.info(
                "Localization updated: %s updated, %s added, backup=%s",
                result.updated_existing,
                result.added_new,
                result.backup_path,
            )
            QMessageBox.information(
                self,
                "Localization Updated",
                "Localization update completed successfully.\n\n"
                f"Updated IDs: {result.updated_existing}\n"
                f"New IDs: {result.added_new}\n\n"
                f"Backup: {result.backup_path}",
            )

        self._run_background_task(
            window_title="Updating Localization",
            initial_status="Updating ConsoleSave localization",
            task=_task,
            on_success=_on_success,
            error_title="Localization Update Failed",
        )

    def _on_update_songdb(self) -> None:
        default_dir = str(Path.cwd())
        selected_file, _ = QFileDialog.getOpenFileName(
            self,
            "Select JDNext song database JSON",
            default_dir,
            "JSON Files (*.json);;All Files (*)",
        )
        if not selected_file:
            return

        output_path = resolve_songdb_synth_path()
        confirm = QMessageBox.question(
            self,
            "Confirm Song Database Update",
            "Use this file to synthesize the local JDNext song database cache?\n\n"
            f"Source: {selected_file}\n"
            f"Output: {output_path}\n\n"
            "This cache helps metadata fallback when source extraction is incomplete.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        def _task() -> object:
            logger.info("Starting JDNext song database synthesis from %s", selected_file)
            return synthesize_jdnext_songdb(Path(selected_file), output_dir=output_path.parent)

        def _on_success(result: object) -> None:
            logger.info(
                "JDNext song database synthesized: source=%s usable=%s keys=%s output=%s",
                result.source_entries,
                result.usable_entries,
                result.index_keys,
                result.output_path,
            )
            backup_line = f"Backup: {result.backup_path}\n" if result.backup_path else ""
            QMessageBox.information(
                self,
                "Song Database Updated",
                "JDNext song database cache created successfully.\n\n"
                f"Source entries: {result.source_entries}\n"
                f"Usable entries: {result.usable_entries}\n"
                f"Indexed keys: {result.index_keys}\n\n"
                f"Output: {result.output_path}\n"
                f"{backup_line}"
                "If this cache is missing later, installer fallback remains active.",
            )

        self._run_background_task(
            window_title="Updating Song Database",
            initial_status="Synthesizing JDNext song database cache",
            task=_task,
            on_success=_on_success,
            error_title="Song Database Update Failed",
        )

    def _run_songdb_bulk_install(
        self,
        *,
        source_game: str,
        title: str,
        extractor: Callable[[Path], list[str]],
    ) -> None:
        if self._bulk_install_request is None:
            QMessageBox.warning(
                self,
                "Bulk Install Unavailable",
                "Bulk install callback is not available in this context.",
            )
            return

        default_dir = str(Path.cwd())
        selected_file, _ = QFileDialog.getOpenFileName(
            self,
            title,
            default_dir,
            "JSON Files (*.json);;All Files (*)",
        )
        if not selected_file:
            return

        try:
            codenames = extractor(Path(selected_file))
        except Exception as exc:
            QMessageBox.critical(
                self,
                "Bulk Install Failed",
                f"Could not parse song database JSON:\n{exc}",
            )
            return

        sample = ", ".join(codenames[:5])
        if len(codenames) > 5:
            sample += ", ..."

        confirm = QMessageBox.question(
            self,
            "Confirm Bulk Install",
            "Queue all discovered codenames for install?\n\n"
            f"Source: {selected_file}\n"
            f"Detected maps: {len(codenames)}\n"
            f"Sample: {sample}\n\n"
            "This will run the existing Fetch batch workflow.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        started = False
        try:
            started = bool(self._bulk_install_request(source_game, codenames))
        except Exception as exc:
            logger.exception("Bulk songdb install launch failed: %s", exc)
            QMessageBox.critical(
                self,
                "Bulk Install Failed",
                f"Could not launch bulk install:\n{exc}",
            )
            return

        if not started:
            QMessageBox.warning(
                self,
                "Bulk Install Not Started",
                "Bulk install could not be started. Check installer status and try again.",
            )
            return

        QMessageBox.information(
            self,
            "Bulk Install Started",
            f"Queued {len(codenames)} map(s) for {'JDNext' if source_game == 'jdnext' else 'JDU'} fetch install.",
        )
        self.reject()

    def _on_bulk_install_jdu_songdb(self) -> None:
        self._run_songdb_bulk_install(
            source_game="jdu",
            title="Select JDU song database JSON",
            extractor=extract_jdu_songdb_codenames,
        )

    def _on_bulk_install_jdnext_songdb(self) -> None:
        self._run_songdb_bulk_install(
            source_game="jdnext",
            title="Select JDNext song database JSON",
            extractor=extract_jdnext_songdb_codenames,
        )

    def _on_clean_game_data(self) -> None:
        if not self._config.game_directory:
            QMessageBox.warning(
                self,
                "Game Directory Required",
                "Set your JD2021 game directory first, then run Clean Game Data.",
            )
            return

        confirm = QMessageBox.warning(
            self,
            "Confirm Game Data Cleanup",
            "This will remove all installed maps from your game cooked map CACHE, MAPS directory, SkuScene entries, \n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        def _task() -> object:
            return clean_game_data(Path(self._config.game_directory))

        def _on_success(result: object) -> None:
            logger.info(
                "Clean data completed: game_dir=%s baseline_source=%s original_maps=%d removed_maps=%d removed_sku=%d removed_cooked=%d",
                result.game_directory,
                result.baseline_source,
                result.original_maps_count,
                result.removed_custom_maps,
                result.removed_skuscene_entries,
                result.removed_cooked_cache_maps,
            )
            source_line = f"\nBaseline source: {result.baseline_source}"
            QMessageBox.information(
                self,
                "Clean Game Data Complete",
                "Cleanup completed successfully.\n\n"
                f"Game directory: {result.game_directory}\n"
                f"Baseline maps tracked: {result.original_maps_count}\n"
                f"Custom map folders removed: {result.removed_custom_maps}\n"
                f"SkuScene entries removed: {result.removed_skuscene_entries}\n"
                f"Cooked cache map folders removed: {result.removed_cooked_cache_maps}"
                f"{source_line}",
            )

        self._run_background_task(
            window_title="Clean Game Data",
            initial_status="Cleaning game data",
            task=_task,
            on_success=_on_success,
            error_title="Clean Game Data Failed",
        )

    @staticmethod
    def _project_root() -> Path:
        return Path(__file__).resolve().parents[3]

    def _resolve_config_path(self, configured_path: Path) -> Path:
        candidate = Path(configured_path).expanduser()
        return candidate if candidate.is_absolute() else (self._project_root() / candidate)

    def _on_clear_map_downloads(self) -> None:
        downloads_dir = self._resolve_config_path(self._config.download_root)
        confirm = QMessageBox.warning(
            self,
            "Confirm mapDownloads Cleanup",
            "This will permanently delete all files and folders inside mapDownloads.\n\n"
            f"Target:\n- {downloads_dir}\n\n"
            "Consequences:\n"
            "- Downloaded source maps and extracted fetch artifacts will be removed.\n"
            "- Future installs may require re-downloading map assets.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        def _task() -> object:
            removed_items: list[str] = []
            if downloads_dir.exists():
                for child in downloads_dir.iterdir():
                    if child.is_dir():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                    removed_items.append(str(child))
            downloads_dir.mkdir(parents=True, exist_ok=True)

            return {
                "downloads_dir": str(downloads_dir),
                "removed_count": len(removed_items),
            }

        def _on_success(result: object) -> None:
            QMessageBox.information(
                self,
                "mapDownloads Cleared",
                "mapDownloads cleanup completed.\n\n"
                f"Removed items: {result.get('removed_count', 0)}\n"
                f"Folder kept at:\n{result.get('downloads_dir')}",
            )

        self._run_background_task(
            window_title="Clearing mapDownloads",
            initial_status="Clearing mapDownloads",
            task=_task,
            on_success=_on_success,
            error_title="mapDownloads Cleanup Failed",
        )

    def _on_clear_cache(self) -> None:
        cache_dir = self._resolve_config_path(self._config.cache_directory)
        readjust_index_file = self._project_root() / "map_readjust_index.json"

        confirm = QMessageBox.warning(
            self,
            "Confirm Cache Clear",
            "This will permanently remove installer cache data and readjust index history.\n\n"
            f"Will clear:\n- {cache_dir}\n"
            f"- {readjust_index_file}\n\n"
            "Consequences:\n"
            "- Re-adjust Offset entries may disappear until maps are re-installed/re-indexed.\n"
            "- Cached source artifacts will be gone and may need to be re-downloaded.\n\n"
            "Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        def _task() -> object:
            removed_items: list[str] = []
            if cache_dir.exists():
                shutil.rmtree(cache_dir)
                removed_items.append(str(cache_dir))
            cache_dir.mkdir(parents=True, exist_ok=True)

            if readjust_index_file.exists():
                readjust_index_file.unlink()
                removed_items.append(str(readjust_index_file))

            return {
                "cache_dir": str(cache_dir),
                "readjust_index": str(readjust_index_file),
                "removed_items": removed_items,
            }

        def _on_success(result: object) -> None:
            removed_items = list(result.get("removed_items", []))
            removed_text = "\n".join(f"- {item}" for item in removed_items) if removed_items else "- Nothing was present to remove"
            QMessageBox.information(
                self,
                "Cache Cleared",
                "Cache clear completed.\n\n"
                f"Removed:\n{removed_text}\n\n"
                f"Cache folder is ready at:\n{result.get('cache_dir')}",
            )

        self._run_background_task(
            window_title="Clearing Cache",
            initial_status="Clearing cache and readjust index",
            task=_task,
            on_success=_on_success,
            error_title="Clear Cache Failed",
        )

    def get_config(self) -> AppConfig:
        return self._config
