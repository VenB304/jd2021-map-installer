"""Action-button panel widget.

Groups all the primary action buttons into a single reusable widget:
- **Install Map** — kick off the full Extract → Normalize → Install pipeline.
- **Pre-flight Check** — validate paths and configuration before install.
- **Uninstall Map** — remove a previously installed map from the game.
- **Re-adjust Offset** — launch the sync-refinement workflow.
- **Settings** — open a configuration dialog (stub for now).
- **Reset State** — clear all in-memory state back to defaults.
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger("jd2021.ui.widgets.action_panel")


class WrapButton(QPushButton):
    """Push button with simple width-aware text wrapping."""

    def __init__(self, text: str, parent: Optional[QWidget] = None) -> None:
        super().__init__(text, parent)
        self._raw_text = text
        self._updating_text = False
        self.setProperty("wrapButton", True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self._apply_wrapped_text()

    def setText(self, text: str) -> None:
        self._raw_text = text
        self._apply_wrapped_text()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._apply_wrapped_text()

    def _apply_wrapped_text(self) -> None:
        if self._updating_text:
            return

        max_width = max(72, self.width() - 20)
        words = self._raw_text.split()
        if not words:
            return

        fm = self.fontMetrics()
        lines: list[str] = []
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if fm.horizontalAdvance(candidate) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)

        self._updating_text = True
        super().setText("\n".join(lines))
        self._updating_text = False


class ActionWidget(QWidget):
    """Panel of action buttons, each wired to a named signal."""

    install_requested = pyqtSignal()
    preflight_requested = pyqtSignal()
    uninstall_requested = pyqtSignal()
    readjust_offset_requested = pyqtSignal()
    settings_requested = pyqtSignal()
    reset_state_requested = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        section_label = QLabel("Actions")
        section_label.setObjectName("actionSectionLabel")
        root.addWidget(section_label)

        # -- Primary actions ------------------------------------------------
        primary = QHBoxLayout()
        primary.setSpacing(6)

        self._btn_install = WrapButton("Install Map")
        self._btn_install.setObjectName("btn_install")
        self._btn_install.setMinimumHeight(38)
        self._btn_install.setToolTip("Run the full pipeline and install the selected map into your game")
        self._btn_install.clicked.connect(self.install_requested.emit)
        primary.addWidget(self._btn_install)
        root.addLayout(primary)

        # -- Secondary actions ---------------------------------------------
        secondary = QHBoxLayout()
        secondary.setSpacing(6)

        self._btn_uninstall = WrapButton("Uninstall a Map")
        self._btn_uninstall.setObjectName("btn_uninstall")
        self._btn_uninstall.setMinimumHeight(38)
        self._btn_uninstall.setToolTip("Remove an installed map and its generated cache/index entries")
        self._btn_uninstall.clicked.connect(self.uninstall_requested.emit)
        secondary.addWidget(self._btn_uninstall)

        self._btn_readjust = WrapButton("Re-adjust Offset")
        self._btn_readjust.setObjectName("btn_readjust")
        self._btn_readjust.setMinimumHeight(38)
        self._btn_readjust.setToolTip("Open sync refinement to preview and fine-tune installed map timing")
        self._btn_readjust.clicked.connect(self.readjust_offset_requested.emit)
        secondary.addWidget(self._btn_readjust)

        self._primary_row_buttons = [self._btn_install]
        self._secondary_row_buttons = [self._btn_uninstall, self._btn_readjust]
        root.addLayout(secondary)

        # -- Separator ----------------------------------------------------
        sep = QFrame()
        sep.setObjectName("sectionSeparator")
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFrameShadow(QFrame.Shadow.Plain)
        sep.setLineWidth(1)
        root.addWidget(sep)

        # -- Utility row --------------------------------------------------
        utils = QHBoxLayout()
        utils.setSpacing(6)

        self._btn_reset = WrapButton("Reset State")
        self._btn_reset.setObjectName("btn_reset")
        self._btn_reset.setMinimumHeight(38)
        self._btn_reset.setToolTip("Clear current mode inputs and reset temporary installer state")
        self._btn_reset.clicked.connect(self.reset_state_requested.emit)
        utils.addWidget(self._btn_reset)

        self._btn_settings = WrapButton("Settings")
        self._btn_settings.setObjectName("btn_settings")
        self._btn_settings.setMinimumHeight(38)
        self._btn_settings.setToolTip("Open installer settings and behavior preferences")
        self._btn_settings.clicked.connect(self.settings_requested.emit)
        utils.addWidget(self._btn_settings)

        self._btn_preflight = WrapButton("Pre-flight Check")
        self._btn_preflight.setObjectName("btn_preflight")
        self._btn_preflight.setMinimumHeight(38)
        self._btn_preflight.setToolTip("Validate selected inputs, paths, and required tools before install")
        self._btn_preflight.clicked.connect(self.preflight_requested.emit)
        utils.addWidget(self._btn_preflight)

        self._utility_row_buttons = [
            self._btn_reset,
            self._btn_preflight,
            self._btn_settings,
        ]
        root.addLayout(utils)
        QTimer.singleShot(0, self._sync_button_row_heights)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._sync_button_row_heights()

    def _sync_button_row_heights(self) -> None:
        self._sync_row_heights(self._primary_row_buttons)
        self._sync_row_heights(self._secondary_row_buttons)
        self._sync_row_heights(self._utility_row_buttons)

    @staticmethod
    def _sync_row_heights(buttons: list[QPushButton]) -> None:
        if not buttons:
            return
        max_height = max(max(btn.sizeHint().height(), 38) for btn in buttons)
        for btn in buttons:
            btn.setFixedHeight(max_height)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_install_enabled(self, enabled: bool) -> None:
        """Enable / disable the Install button (e.g. while a worker runs)."""
        self._btn_install.setEnabled(enabled)

    def set_all_enabled(self, enabled: bool) -> None:
        """Bulk-enable / disable every button in the panel."""
        for btn in (
            self._btn_install,
            self._btn_preflight,
            self._btn_uninstall,
            self._btn_readjust,
            self._btn_settings,
            self._btn_reset,
        ):
            btn.setEnabled(enabled)

    def set_readjust_visible(self, visible: bool) -> None:
        """Show or hide the readjust offset button based on config."""
        self._btn_readjust.setVisible(visible)
        # Force layout update for heights if visibility changes
        QTimer.singleShot(0, self._sync_button_row_heights)
