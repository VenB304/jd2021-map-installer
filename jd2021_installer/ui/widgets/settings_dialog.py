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
        file_filter: str = "Executables (*.exe);;All Files (*)",
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
                file_filter,
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

        subtitle = QLabel("Configure how the installer looks, behaves, and connects to external services.")
        subtitle.setObjectName("settingsDialogSubtitle")
        layout.addWidget(subtitle)

        tabs = QTabWidget()
        tabs.setObjectName("settingsDialogTabs")
        layout.addWidget(tabs, 1)

        # ================================================================
        # TAB 1: GENERAL  —  Startup, appearance, notifications
        # ================================================================
        tab_general = QWidget()
        general_layout = QVBoxLayout(tab_general)
        general_layout.setContentsMargins(10, 10, 10, 10)
        general_layout.setSpacing(10)

        general_form = QFormLayout()
        general_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        general_form.setHorizontalSpacing(12)
        general_form.setVerticalSpacing(10)

        self.combo_theme = QComboBox()
        self.combo_theme.addItem("Light", "light")
        self.combo_theme.addItem("Dark", "dark")
        self._set_combo_from_value(self.combo_theme, self._config.theme)
        self.combo_theme.setToolTip("Pick the installer color theme.")
        general_form.addRow("Theme:", self.combo_theme)

        self.combo_log_detail = QComboBox()
        self.combo_log_detail.addItem("Minimal (warnings & errors only)", "quiet")
        self.combo_log_detail.addItem("Normal (recommended)", "user")
        self.combo_log_detail.addItem("Detailed (extra debug info)", "detailed")
        self.combo_log_detail.addItem("Developer (maximum verbosity)", "developer")
        self._set_combo_from_value(self.combo_log_detail, self._config.log_detail_level)
        self.combo_log_detail.setToolTip(
            "Controls how much detail appears in the app and log files."
        )
        general_form.addRow("Log verbosity:", self.combo_log_detail)

        general_layout.addLayout(general_form)

        # Startup checkboxes
        self.cb_quickstart = QCheckBox("Show beginner guide on startup")
        self.cb_quickstart.setChecked(self._config.show_quickstart_on_launch)
        self.cb_quickstart.setToolTip(
            "Shows a short walkthrough for new users at launch.\n"
            "Helpful if you haven't read the documentation."
        )
        general_layout.addWidget(self.cb_quickstart)

        self.cb_skip_preflight = QCheckBox("Skip startup checks")
        self.cb_skip_preflight.setChecked(self._config.skip_preflight)
        self.cb_skip_preflight.setToolTip(
            "Skips validation on launch.\n"
            "Only use when your setup is already stable and working."
        )
        general_layout.addWidget(self.cb_skip_preflight)

        self.cb_preflight_popup = QCheckBox("Show a confirmation popup when startup checks pass")
        self.cb_preflight_popup.setChecked(self._config.show_preflight_success_popup)
        self.cb_preflight_popup.setToolTip(
            "If off, passing startup checks just silently enables\n"
            "the Install button without a popup."
        )
        general_layout.addWidget(self.cb_preflight_popup)

        # Notification checkboxes
        self.cb_install_summary = QCheckBox("Show a summary when installation finishes")
        self.cb_install_summary.setChecked(getattr(self._config, "show_install_summary_popup", True))
        self.cb_install_summary.setToolTip(
            "Displays a checklist of installed files, counts, and\n"
            "warnings after each installation completes."
        )
        general_layout.addWidget(self.cb_install_summary)

        self.cb_suppress = QCheckBox("Don't remind me about audio offset tuning after install")
        self.cb_suppress.setChecked(self._config.suppress_offset_notification)
        self.cb_suppress.setToolTip(
            "Hides the post-install reminder about fine-tuning\n"
            "audio sync for installed maps."
        )
        general_layout.addWidget(self.cb_suppress)

        # ----- Window section -----
        window_section_label = QLabel("Window")
        window_section_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        general_layout.addWidget(window_section_label)

        self.cb_enforce_min_size = QCheckBox("Prevent window from being resized too small")
        self.cb_enforce_min_size.setChecked(self._config.enforce_min_window_size)
        self.cb_enforce_min_size.setToolTip(
            "When off, the main window can be freely resized below the minimum."
        )
        general_layout.addWidget(self.cb_enforce_min_size)

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
        general_layout.addLayout(window_form)

        def _toggle_min_size_inputs(enabled: bool) -> None:
            self.spin_min_width.setEnabled(enabled)
            self.spin_min_height.setEnabled(enabled)

        self.cb_enforce_min_size.toggled.connect(_toggle_min_size_inputs)
        _toggle_min_size_inputs(self.cb_enforce_min_size.isChecked())

        self.cb_size_overlay = QCheckBox("Show window dimensions while resizing")
        self.cb_size_overlay.setChecked(
            getattr(self._config, "show_window_size_overlay", True)
        )
        self.cb_size_overlay.setToolTip(
            "Displays a floating overlay like 1280 × 720 when you resize the main window."
        )
        general_layout.addWidget(self.cb_size_overlay)

        general_layout.addStretch()
        tabs.addTab(tab_general, "General")

        # ================================================================
        # TAB 2: INSTALLATION  —  Map processing behavior
        # ================================================================
        tab_install = QWidget()
        install_layout = QVBoxLayout(tab_install)
        install_layout.setContentsMargins(10, 10, 10, 10)
        install_layout.setSpacing(10)

        install_form = QFormLayout()
        install_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        install_form.setHorizontalSpacing(12)
        install_form.setVerticalSpacing(10)

        self.combo_cleanup = QComboBox()
        self.combo_cleanup.addItem("Ask me each time", "ask")
        self.combo_cleanup.addItem("Keep files", "keep")
        self.combo_cleanup.addItem("Auto-delete (keep preview thumbnails)", "delete")
        self.combo_cleanup.addItem("Full cleanup (saves most space, removes previews)", "aggressive")
        self._set_combo_from_value(self.combo_cleanup, self._config.cleanup_behavior)
        self.combo_cleanup.setToolTip(
            "What happens to temporary files after installation finishes."
        )
        install_form.addRow("After-install file cleanup:", self.combo_cleanup)

        self.combo_locked_status = QComboBox()
        self.combo_locked_status.addItem("Ask when needed", "ask")
        self.combo_locked_status.addItem("Always unlock songs (force status 3)", "force3")
        self.combo_locked_status.addItem("Keep original lock state", "keep")
        self._set_combo_from_value(self.combo_locked_status, self._config.locked_status_behavior)
        self.combo_locked_status.setToolTip(
            "How the installer treats lock status values when importing maps."
        )
        install_form.addRow("Song lock handling:", self.combo_locked_status)

        self.combo_albumcoach = QComboBox()
        self.combo_albumcoach.addItem("Ask me each time (default)", "ask")
        self.combo_albumcoach.addItem("Open the editor", "always_customize")
        self.combo_albumcoach.addItem("Use automatic layout", "always_default")
        self._set_combo_from_value(
            self.combo_albumcoach,
            getattr(self._config, "albumcoach_behavior", "ask"),
        )
        self.combo_albumcoach.setToolTip(
            "How the album cover is arranged for JDNext songs\n"
            "with multiple dancers that are missing one.\n\n"
            "Ask: prompt before each install.\n"
            "Open the editor: always show the layout editor.\n"
            "Use automatic layout: use automatic compositing silently."
        )
        install_form.addRow("JDNext Album art layout:", self.combo_albumcoach)

        self.combo_jdnext_cover = QComboBox()
        self.combo_jdnext_cover.addItem("Ask me each time (default)", "ask")
        self.combo_jdnext_cover.addItem("Generate new cover (background + title asset)", "synthesized")
        self.combo_jdnext_cover.addItem("Use original cover (may look squished)", "original")
        self._set_combo_from_value(
            self.combo_jdnext_cover,
            getattr(self._config, "jdnext_cover_behavior", "ask"),
        )
        self.combo_jdnext_cover.setToolTip(
            "How cover art is created for JDNext maps.\n\n"
            "Generate new cover: composites the map background + title into a 1:1 square.\n"
            "Use original cover: uses the source cover as-is (may look stretched in-game).\n"
            "Ask: prompt before each JDNext install."
        )
        install_form.addRow("Cover art (JDNext maps):", self.combo_jdnext_cover)

        install_layout.addLayout(install_form)

        self.cb_convert_jdnext_gestures = QCheckBox("Convert JDNext motion data for Xbox 360 compatibility (coming soon)")
        self.cb_convert_jdnext_gestures.setChecked(False)
        self.cb_convert_jdnext_gestures.setEnabled(False)
        self.cb_convert_jdnext_gestures.setToolTip(
            "This feature is not yet ready.\n"
            "Gesture conversion is still experimental and currently disabled."
        )
        install_layout.addWidget(self.cb_convert_jdnext_gestures)

        install_layout.addStretch()
        tabs.addTab(tab_install, "Installation")

        # ================================================================
        # TAB 3: MEDIA  —  Downloads, quality, preview playback
        # ================================================================
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
            "Ultra HD", "Ultra", "High HD", "High",
            "Mid HD", "Mid", "Low HD", "Low"
        ])
        # Map display names to internal values for save
        self._quality_display_to_internal = {
            "Ultra HD": "ULTRA_HD", "Ultra": "ULTRA",
            "High HD": "HIGH_HD", "High": "HIGH",
            "Mid HD": "MID_HD", "Mid": "MID",
            "Low HD": "LOW_HD", "Low": "LOW",
        }
        self._quality_internal_to_display = {v: k for k, v in self._quality_display_to_internal.items()}
        display_quality = self._quality_internal_to_display.get(
            self._config.video_quality, self._config.video_quality
        )
        self.combo_quality.setCurrentText(display_quality)
        self.combo_quality.setToolTip(
            "The quality tier the installer tries to download first."
        )
        media_form.addRow("Video download quality:", self.combo_quality)
        
        self.combo_fallback_behavior = QComboBox()
        self.combo_fallback_behavior.addItem("Next lower quality (e.g. High -> Mid)", "fallback_down")
        self.combo_fallback_behavior.addItem("Next best quality (e.g. High -> Ultra)", "fallback_up")
        current_fallback_mode = getattr(self._config, "video_fallback_behavior", "fallback_down")
        fallback_idx = self.combo_fallback_behavior.findData(current_fallback_mode)
        self.combo_fallback_behavior.setCurrentIndex(fallback_idx if fallback_idx >= 0 else 0)
        self.combo_fallback_behavior.setToolTip(
            "What to do if the selected video quality is missing or incompatible.\n\n"
            "Next lower quality: Safely falls back to a lower resolution.\n"
            "Next best quality: Tries to find a higher resolution before falling back to lower."
        )
        media_form.addRow("Missing quality fallback:", self.combo_fallback_behavior)

        self.combo_vp9_mode = QComboBox()
        self.combo_vp9_mode.addItem("Convert to VP8 (best game compatibility)", "reencode_to_vp8")
        self.combo_vp9_mode.addItem("Use a lower compatible quality (no conversion)", "fallback_compatible_down")
        current_vp9_mode = getattr(self._config, "vp9_handling_mode", "reencode_to_vp8")
        vp9_idx = self.combo_vp9_mode.findData(current_vp9_mode)
        self.combo_vp9_mode.setCurrentIndex(vp9_idx if vp9_idx >= 0 else 0)
        self.combo_vp9_mode.setToolTip(
            "How to handle VP9-encoded videos that the game can't play natively.\n\n"
            "Convert to VP8: keeps requested quality tier but may reduce fidelity.\n"
            "Lower compatible quality: skips VP9 tiers and picks a lower one."
        )
        media_form.addRow("VP9 codec compatibility:", self.combo_vp9_mode)

        self.combo_hwaccel = QComboBox()
        self.combo_hwaccel.addItems(["auto", "none"])
        self.combo_hwaccel.setCurrentText(getattr(self._config, "ffmpeg_hwaccel", "auto"))
        self.combo_hwaccel.setToolTip(
            "auto: use GPU hardware decoding if available\n"
            "none: force software-only decoding"
        )
        media_form.addRow("Hardware video acceleration:", self.combo_hwaccel)

        self.combo_preview_mode = QComboBox()
        self.combo_preview_mode.addItem("Low-res copy (faster playback)", "proxy_low")
        self.combo_preview_mode.addItem("Original video file", "original")
        self._set_combo_from_value(
            self.combo_preview_mode,
            getattr(self._config, "preview_video_mode", "proxy_low"),
        )
        self.combo_preview_mode.setToolTip(
            "Whether preview uses a lightweight copy or the source file directly."
        )
        media_form.addRow("Video preview source:", self.combo_preview_mode)

        media_layout.addLayout(media_form)

        # ----- Preview Playback section -----
        preview_section_label = QLabel("Preview Playback")
        preview_section_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        media_layout.addWidget(preview_section_label)

        preview_form = QFormLayout()
        preview_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        preview_form.setHorizontalSpacing(12)
        preview_form.setVerticalSpacing(10)

        self.spin_preview_fps = QSpinBox()
        self.spin_preview_fps.setRange(12, 120)
        self.spin_preview_fps.setValue(int(getattr(self._config, "preview_fps", 25)))
        self.spin_preview_fps.setToolTip(
            "Default FPS when the source video doesn't specify one."
        )
        preview_form.addRow("Preview frame rate:", self.spin_preview_fps)

        self.spin_preview_audio_only_offset = QDoubleSpinBox()
        self.spin_preview_audio_only_offset.setRange(-2000.0, 2000.0)
        self.spin_preview_audio_only_offset.setDecimals(1)
        self.spin_preview_audio_only_offset.setSingleStep(5.0)
        self.spin_preview_audio_only_offset.setValue(float(getattr(self._config, "preview_only_audio_offset_ms", -125.0)))
        self.spin_preview_audio_only_offset.setSuffix(" ms")
        self.spin_preview_audio_only_offset.setToolTip(
            "Fine-tune audio sync in preview mode."
        )
        preview_form.addRow("Audio preview timing offset:", self.spin_preview_audio_only_offset)

        self.spin_audio_preview_fade = QDoubleSpinBox()
        self.spin_audio_preview_fade.setRange(0.0, 10.0)
        self.spin_audio_preview_fade.setDecimals(2)
        self.spin_audio_preview_fade.setSingleStep(0.1)
        self.spin_audio_preview_fade.setValue(float(getattr(self._config, "audio_preview_fade_s", 2.0)))
        self.spin_audio_preview_fade.setSuffix(" s")
        self.spin_audio_preview_fade.setToolTip(
            "How long audio fades in and out during preview."
        )
        preview_form.addRow("Audio preview fade duration:", self.spin_audio_preview_fade)

        media_layout.addLayout(preview_form)
        media_layout.addStretch()
        tabs.addTab(tab_media, "Media")

        # ================================================================
        # TAB 4: ADVANCED  —  Tool paths, connections, network timing, dev
        # ================================================================
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
            "Tool paths, service connections, and network timing. "
            "Most users won't need to change these."
        )
        advanced_note.setWordWrap(True)
        advanced_layout.addWidget(advanced_note)

        # ----- External Tools section -----
        tools_section_label = QLabel("External Tools")
        tools_section_label.setStyleSheet("font-weight: bold; margin-top: 4px;")
        advanced_layout.addWidget(tools_section_label)

        tools_form = QFormLayout()
        tools_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        tools_form.setHorizontalSpacing(12)
        tools_form.setVerticalSpacing(10)

        self.txt_ffmpeg_path = QLineEdit(str(getattr(self._config, "ffmpeg_path", "ffmpeg") or "ffmpeg"))
        self.txt_ffmpeg_path.setPlaceholderText("ffmpeg")
        self.txt_ffmpeg_path.setToolTip(
            "Path to FFmpeg. Clear to auto-detect."
        )
        tools_form.addRow(
            "FFmpeg executable:",
            self._make_path_picker_row(
                self.txt_ffmpeg_path,
                browse_title="Select FFmpeg executable",
            ),
        )

        self.txt_ffprobe_path = QLineEdit(str(getattr(self._config, "ffprobe_path", "ffprobe") or "ffprobe"))
        self.txt_ffprobe_path.setPlaceholderText("ffprobe")
        self.txt_ffprobe_path.setToolTip(
            "Path to FFprobe. Clear to auto-detect."
        )
        tools_form.addRow(
            "FFprobe executable:",
            self._make_path_picker_row(
                self.txt_ffprobe_path,
                browse_title="Select FFprobe executable",
            ),
        )

        self.txt_vgmstream_path = QLineEdit(str(getattr(self._config, "vgmstream_path", "") or ""))
        self.txt_vgmstream_path.setPlaceholderText("Auto (tools/vgmstream or PATH)")
        self.txt_vgmstream_path.setToolTip(
            "Optional vgmstream CLI for XMA2 audio decoding.\n"
            "Leave empty for auto-detection."
        )
        tools_form.addRow(
            "vgmstream executable:",
            self._make_path_picker_row(
                self.txt_vgmstream_path,
                browse_title="Select vgmstream executable",
            ),
        )

        self.txt_assetstudio_cli = QLineEdit(str(getattr(self._config, "assetstudio_cli_path", "") or ""))
        self.txt_assetstudio_cli.setPlaceholderText("Auto (search under third-party tools folder)")
        self.txt_assetstudio_cli.setToolTip(
            "Optional AssetStudioModCLI path for JDNext bundle extraction."
        )
        tools_form.addRow(
            "AssetStudio CLI:",
            self._make_path_picker_row(
                self.txt_assetstudio_cli,
                browse_title="Select AssetStudioModCLI executable",
            ),
        )

        third_party_root = getattr(self._config, "third_party_tools_root", None)
        self.txt_third_party_root = QLineEdit(str(third_party_root) if third_party_root else "")
        self.txt_third_party_root.setPlaceholderText("Auto (./tools)")
        self.txt_third_party_root.setToolTip(
            "Root folder for JDNext helper tools.\n"
            "Leave empty for the default ./tools path."
        )
        tools_form.addRow(
            "Third-party tools folder:",
            self._make_path_picker_row(
                self.txt_third_party_root,
                browse_title="Select third-party tools folder",
                select_directory=True,
            ),
        )

        advanced_layout.addLayout(tools_form)

        # ----- Service Connections section -----
        connections_section_label = QLabel("Service Connections")
        connections_section_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        advanced_layout.addWidget(connections_section_label)

        connections_form = QFormLayout()
        connections_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        connections_form.setHorizontalSpacing(12)
        connections_form.setVerticalSpacing(10)

        self.txt_discord_url = QLineEdit()
        self.txt_discord_url.setText(self._config.discord_channel_url)
        self.txt_discord_url.setPlaceholderText("https://discord.com/channels/...")
        self.txt_discord_url.setToolTip(
            "URL of the Discord channel where the JDU asset bot lives.\n"
            "Required for Fetch mode.\n"
            "Copy from your browser's address bar while in the channel."
        )
        connections_form.addRow("Discord channel URL:", self.txt_discord_url)

        self.txt_jdlo_auth = QLineEdit()
        jdlo_auth_val = str(getattr(self._config, "jdlo_auth_path", "")) if getattr(self._config, "jdlo_auth_path", None) else ""
        self.txt_jdlo_auth.setText(jdlo_auth_val)
        self.txt_jdlo_auth.setPlaceholderText("Select jdlo_auth.ini")
        self.txt_jdlo_auth.setToolTip(
            "Path to your jdlo_auth.ini file (from JD2017 PC).\n"
            "Required for JDLO Fetch modes."
        )
        connections_form.addRow(
            "JDLO auth file:",
            self._make_path_picker_row(
                self.txt_jdlo_auth,
                browse_title="Select jdlo_auth.ini",
                select_directory=False,
                file_filter="INI Files (*.ini);;All Files (*)",
            ),
        )

        advanced_layout.addLayout(connections_form)

        self.cb_fetch_background = QCheckBox("Hide the Fetch browser window (run in background)")
        self.cb_fetch_background.setChecked(getattr(self._config, "fetch_background_mode", False))
        self.cb_fetch_background.setToolTip(
            "Runs Chromium off-screen so it doesn't steal focus.\n"
            "Browser actions are logged to the console.\n\n"
            "Turn this OFF if you need to manually intervene when\n"
            "the bot gets stuck. The browser will appear on-screen\n"
            "if re-login is needed."
        )
        advanced_layout.addWidget(self.cb_fetch_background)

        # ----- Network Timing section -----
        network_section_label = QLabel("Network Timing")
        network_section_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        advanced_layout.addWidget(network_section_label)

        network_form = QFormLayout()
        network_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        network_form.setHorizontalSpacing(12)
        network_form.setVerticalSpacing(10)

        self.spin_download_timeout = QSpinBox()
        self.spin_download_timeout.setRange(15, 3600)
        self.spin_download_timeout.setValue(int(getattr(self._config, "download_timeout_s", 600)))
        self.spin_download_timeout.setSuffix(" s")
        self.spin_download_timeout.setToolTip(
            "How long to wait for a download before giving up."
        )
        network_form.addRow("Download timeout (max wait):", self.spin_download_timeout)

        self.spin_max_retries = QSpinBox()
        self.spin_max_retries.setRange(0, 12)
        self.spin_max_retries.setValue(int(getattr(self._config, "max_retries", 3)))
        self.spin_max_retries.setToolTip(
            "Number of retry attempts for failed downloads."
        )
        network_form.addRow("Max download retries:", self.spin_max_retries)

        self.spin_retry_base_delay = QSpinBox()
        self.spin_retry_base_delay.setRange(0, 60)
        self.spin_retry_base_delay.setValue(int(getattr(self._config, "retry_base_delay_s", 2)))
        self.spin_retry_base_delay.setSuffix(" s")
        self.spin_retry_base_delay.setToolTip(
            "Wait time before retrying a failed download."
        )
        network_form.addRow("Delay between retries:", self.spin_retry_base_delay)

        self.spin_inter_request_delay = QDoubleSpinBox()
        self.spin_inter_request_delay.setRange(0.0, 20.0)
        self.spin_inter_request_delay.setDecimals(2)
        self.spin_inter_request_delay.setSingleStep(0.1)
        self.spin_inter_request_delay.setValue(float(getattr(self._config, "inter_request_delay_s", 1.5)))
        self.spin_inter_request_delay.setSuffix(" s")
        self.spin_inter_request_delay.setToolTip(
            "Delay between consecutive download requests."
        )
        network_form.addRow("Pause between downloads:", self.spin_inter_request_delay)

        self.spin_fetch_login_timeout = QSpinBox()
        self.spin_fetch_login_timeout.setRange(30, 1800)
        self.spin_fetch_login_timeout.setValue(int(getattr(self._config, "fetch_login_timeout_s", 300)))
        self.spin_fetch_login_timeout.setSuffix(" s")
        self.spin_fetch_login_timeout.setToolTip(
            "How long Fetch mode waits for Discord login before giving up."
        )
        network_form.addRow("Discord login wait time:", self.spin_fetch_login_timeout)

        self.spin_fetch_bot_timeout = QSpinBox()
        self.spin_fetch_bot_timeout.setRange(10, 600)
        self.spin_fetch_bot_timeout.setValue(int(getattr(self._config, "fetch_bot_response_timeout_s", 60)))
        self.spin_fetch_bot_timeout.setSuffix(" s")
        self.spin_fetch_bot_timeout.setToolTip(
            "How long Fetch mode waits for the bot to respond with links."
        )
        network_form.addRow("Bot reply wait time:", self.spin_fetch_bot_timeout)

        advanced_layout.addLayout(network_form)

        # ----- Legacy Features section -----
        legacy_section_label = QLabel("Legacy Features")
        legacy_section_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        advanced_layout.addWidget(legacy_section_label)

        self.cb_legacy_sync = QCheckBox("Enable Legacy Sync Refinement")
        self.cb_legacy_sync.setChecked(
            getattr(self._config, "enable_legacy_sync_refinement", False)
        )
        self.cb_legacy_sync.setToolTip(
            "Shows the 'Readjust Offset' button and Sync Refinement section in the main window.\n"
            "This is generally not needed anymore as most modes have correct syncing."
        )
        advanced_layout.addWidget(self.cb_legacy_sync)

        # ----- Developer section -----
        dev_section_label = QLabel("Developer")
        dev_section_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        advanced_layout.addWidget(dev_section_label)

        self.cb_style_debug = QCheckBox("Show widget outlines for styling (debug mode)")
        self.cb_style_debug.setChecked(
            getattr(self._config, "style_debug_mode", False)
        )
        self.cb_style_debug.setToolTip(
            "Adds colored borders and labels to help map widgets to\n"
            "stylesheet selectors. Auto-reloads styles on save.\n"
            "Use while tuning colors, then disable for normal appearance."
        )
        advanced_layout.addWidget(self.cb_style_debug)

        dev_form = QFormLayout()
        dev_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        dev_form.setHorizontalSpacing(12)
        dev_form.setVerticalSpacing(10)

        self.spin_overlay_timeout = QSpinBox()
        self.spin_overlay_timeout.setRange(200, 6000)
        self.spin_overlay_timeout.setSingleStep(100)
        self.spin_overlay_timeout.setValue(int(getattr(self._config, "window_size_overlay_timeout_ms", 1100)))
        self.spin_overlay_timeout.setSuffix(" ms")
        self.spin_overlay_timeout.setToolTip(
            "How long the floating dimensions overlay stays visible after resizing stops."
        )
        dev_form.addRow("Size indicator display time:", self.spin_overlay_timeout)

        advanced_layout.addLayout(dev_form)
        advanced_layout.addStretch()

        tabs.addTab(tab_advanced, "Advanced")

        # ================================================================
        # TAB 5: MAINTENANCE & UPDATES  —  Data management, version updates
        # ================================================================
        tab_maintenance = QWidget()
        tab_maintenance_layout = QVBoxLayout(tab_maintenance)
        tab_maintenance_layout.setContentsMargins(0, 0, 0, 0)
        tab_maintenance_layout.setSpacing(0)

        maintenance_scroll = QScrollArea(tab_maintenance)
        maintenance_scroll.setWidgetResizable(True)
        maintenance_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        tab_maintenance_layout.addWidget(maintenance_scroll)

        maintenance_content = QWidget()
        maintenance_scroll.setWidget(maintenance_content)

        maintenance_layout = QVBoxLayout(maintenance_content)
        maintenance_layout.setContentsMargins(10, 10, 10, 10)
        maintenance_layout.setSpacing(10)

        maintenance_note = QLabel(
            "Manage installed data, import song lists, and check for new versions."
        )
        maintenance_note.setWordWrap(True)
        maintenance_layout.addWidget(maintenance_note)

        # ----- Import & Bulk Install section -----
        import_section_label = QLabel("Import & Bulk Install")
        import_section_label.setStyleSheet("font-weight: bold; margin-top: 4px;")
        maintenance_layout.addWidget(import_section_label)

        import_form = QFormLayout()
        import_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        import_form.setHorizontalSpacing(12)
        import_form.setVerticalSpacing(10)

        self.btn_update_localization = QPushButton("Select Localization JSON")
        self.btn_update_localization.setEnabled(False)
        self.btn_update_localization.clicked.connect(self._on_update_localization)
        self.btn_update_localization.setToolTip(
            "Updates in-game text such as 'Alternate Version' or 'Official Choreo'. (Currently disabled)"
        )
        l_layout1 = QHBoxLayout()
        l_layout1.setContentsMargins(0, 0, 0, 0)
        l_layout1.addWidget(self.btn_update_localization)
        l_layout1.addStretch()
        import_form.addRow("Update Localization from JSON:", l_layout1)

        self.btn_update_songdb = QPushButton("Select JDNext songdb")
        self.btn_update_songdb.clicked.connect(self._on_update_songdb)
        self.btn_update_songdb.setToolTip(
            "Loads JDNext song database entries from a JSON file."
        )
        l_layout2 = QHBoxLayout()
        l_layout2.setContentsMargins(0, 0, 0, 0)
        l_layout2.addWidget(self.btn_update_songdb)
        l_layout2.addStretch()
        import_form.addRow("Import JDNext Metadata via JDNext songdb:", l_layout2)

        self.btn_bulk_install_jdu_songdb = QPushButton("Select JDU songdb JSON")
        self.btn_bulk_install_jdu_songdb.clicked.connect(self._on_bulk_install_jdu_songdb)
        self.btn_bulk_install_jdu_songdb.setToolTip(
            "Pick a JDU songdb JSON and queue every codename through Fetch mode."
        )
        l_layout3 = QHBoxLayout()
        l_layout3.setContentsMargins(0, 0, 0, 0)
        l_layout3.addWidget(self.btn_bulk_install_jdu_songdb)
        l_layout3.addStretch()
        import_form.addRow("Attempt Install all JDU maps:", l_layout3)

        self.btn_bulk_install_jdnext_songdb = QPushButton("Select JDNext songdb JSON")
        self.btn_bulk_install_jdnext_songdb.clicked.connect(self._on_bulk_install_jdnext_songdb)
        self.btn_bulk_install_jdnext_songdb.setToolTip(
            "Pick a JDNext songdb JSON and queue every map through Fetch JDNext mode."
        )
        l_layout4 = QHBoxLayout()
        l_layout4.setContentsMargins(0, 0, 0, 0)
        l_layout4.addWidget(self.btn_bulk_install_jdnext_songdb)
        l_layout4.addStretch()
        import_form.addRow("Attempt Install all JDNext maps:", l_layout4)

        maintenance_layout.addLayout(import_form)

        # ----- Cleanup section -----
        cleanup_section_label = QLabel("Cleanup")
        cleanup_section_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        maintenance_layout.addWidget(cleanup_section_label)

        cleanup_form = QFormLayout()
        cleanup_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        cleanup_form.setHorizontalSpacing(12)
        cleanup_form.setVerticalSpacing(10)

        self.btn_clean_data = QPushButton("Clean Game Data...")
        self.btn_clean_data.clicked.connect(self._on_clean_game_data)
        self.btn_clean_data.setToolTip(
            "Deletes custom map files and resets caches in the game directory."
        )
        c_layout1 = QHBoxLayout()
        c_layout1.setContentsMargins(0, 0, 0, 0)
        c_layout1.addWidget(self.btn_clean_data)
        c_layout1.addStretch()
        cleanup_form.addRow("Remove all installed custom maps and cached data:", c_layout1)

        self.btn_clear_mapdownloads = QPushButton("Delete Downloads...")
        self.btn_clear_mapdownloads.clicked.connect(self._on_clear_map_downloads)
        self.btn_clear_mapdownloads.setToolTip(
            "Removes the mapDownloads folder contents."
        )
        c_layout2 = QHBoxLayout()
        c_layout2.setContentsMargins(0, 0, 0, 0)
        c_layout2.addWidget(self.btn_clear_mapdownloads)
        c_layout2.addStretch()
        cleanup_form.addRow("Delete downloaded map files:", c_layout2)

        self.btn_clear_cache = QPushButton("Clear Cache...")
        self.btn_clear_cache.clicked.connect(self._on_clear_cache)
        self.btn_clear_cache.setToolTip(
            "Deletes the installer cache and regenerates the map index."
        )
        c_layout3 = QHBoxLayout()
        c_layout3.setContentsMargins(0, 0, 0, 0)
        c_layout3.addWidget(self.btn_clear_cache)
        c_layout3.addStretch()
        cleanup_form.addRow("Clear cache and rebuild index:", c_layout3)

        maintenance_layout.addLayout(cleanup_form)

        # ----- Updates section -----
        updates_section_label = QLabel("Updates")
        updates_section_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        maintenance_layout.addWidget(updates_section_label)

        updates_note = QLabel(
            "Check for new versions online. Updates are downloaded automatically."
        )
        updates_note.setWordWrap(True)
        maintenance_layout.addWidget(updates_note)

        # Version info (read-only)
        self._lbl_update_branch = QLabel("Branch: detecting...")
        self._lbl_update_commit = QLabel("Commit: detecting...")
        self._lbl_update_source = QLabel("Source: detecting...")
        for lbl in (self._lbl_update_branch, self._lbl_update_commit, self._lbl_update_source):
            lbl.setStyleSheet("color: #888; font-size: 11px;")
            maintenance_layout.addWidget(lbl)
        self._populate_version_info()

        self.cb_check_updates_on_launch = QCheckBox("Automatically check for updates on startup")
        self.cb_check_updates_on_launch.setChecked(
            getattr(self._config, "check_updates_on_launch", True)
        )
        self.cb_check_updates_on_launch.setToolTip(
            "Silently checks for updates every time the installer starts.\n"
            "You'll only be notified if a new version is available."
        )
        maintenance_layout.addWidget(self.cb_check_updates_on_launch)

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

        # Pre-populate branch combo with current branch
        self._populate_branch_combo_initial()

        # Connect branch change to trigger a check
        self.combo_update_branch.currentTextChanged.connect(self._on_branch_selection_changed)

        # Fetch all available branches immediately in the background so users
        # can switch without needing to click Refresh first.
        self._refresh_branches_in_background(silent=True)

        # Manual check button
        check_row = QHBoxLayout()
        check_row.setContentsMargins(0, 0, 0, 0)
        self.btn_check_updates = QPushButton("Check for Updates")
        self.btn_check_updates.setMinimumWidth(160)
        self.btn_check_updates.clicked.connect(self._on_check_updates)
        check_row.addWidget(self.btn_check_updates)
        check_row.addStretch()
        branch_form.addRow("", check_row)

        maintenance_layout.addLayout(branch_form)

        maintenance_layout.addStretch()

        tabs.addTab(tab_maintenance, "Maintenance && Updates")

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
        self._config.enable_legacy_sync_refinement = self.cb_legacy_sync.isChecked()
        self._config.suppress_offset_notification = self.cb_suppress.isChecked()
        self._config.cleanup_behavior = self._combo_value(self.combo_cleanup)
        self._config.locked_status_behavior = self._combo_value(self.combo_locked_status)
        self._config.show_preflight_success_popup = self.cb_preflight_popup.isChecked()
        self._config.show_install_summary_popup = self.cb_install_summary.isChecked()
        self._config.show_quickstart_on_launch = self.cb_quickstart.isChecked()
        self._config.convert_jdnext_gestures = self.cb_convert_jdnext_gestures.isChecked()
        self._config.fetch_background_mode = self.cb_fetch_background.isChecked()
        self._config.albumcoach_behavior = self._combo_value(self.combo_albumcoach)
        self._config.jdnext_cover_behavior = self._combo_value(self.combo_jdnext_cover)
        self._config.log_detail_level = self._combo_value(self.combo_log_detail)
        self._config.theme = self._combo_value(self.combo_theme)
        self._config.enforce_min_window_size = self.cb_enforce_min_size.isChecked()
        self._config.min_window_width = self.spin_min_width.value()
        self._config.min_window_height = self.spin_min_height.value()
        self._config.show_window_size_overlay = self.cb_size_overlay.isChecked()
        self._config.style_debug_mode = self.cb_style_debug.isChecked()
        display_text = self.combo_quality.currentText()
        self._config.video_quality = self._quality_display_to_internal.get(display_text, display_text)
        self._config.video_fallback_behavior = str(self.combo_fallback_behavior.currentData())
        self._config.ffmpeg_hwaccel = self.combo_hwaccel.currentText()
        self._config.vp9_handling_mode = str(self.combo_vp9_mode.currentData())
        self._config.preview_video_mode = self._combo_value(self.combo_preview_mode)
        self._config.discord_channel_url = self.txt_discord_url.text().strip()
        
        jdlo_path = self.txt_jdlo_auth.text().strip()
        self._config.jdlo_auth_path = Path(jdlo_path) if jdlo_path else None
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
