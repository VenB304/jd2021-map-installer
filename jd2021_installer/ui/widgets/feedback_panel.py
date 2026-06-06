"""Feedback panel: progress checklist, progress bar, and live log.

Provides three coordinated feedback mechanisms:

1. **Checklist** — A ``QListWidget`` showing named steps, each with a
   status icon (⏳/⚙/✅/❌) representing waiting / in-progress / done /
   error.
2. **Progress bar** — A standard ``QProgressBar`` (0–100).
3. **Log output** — A ``QPlainTextEdit`` for scrolling log messages.
"""

from __future__ import annotations

import logging
from enum import Enum, auto
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger("jd2021.ui.widgets.feedback_panel")


class StepStatus(Enum):
    """Visual status for a checklist step."""
    WAITING = auto()
    IN_PROGRESS = auto()
    DONE = auto()
    ERROR = auto()


_STATUS_ICONS = {
    StepStatus.WAITING: "⏳",
    StepStatus.IN_PROGRESS: "⚙️",
    StepStatus.DONE: "✅",
    StepStatus.ERROR: "❌",
}

_STEP_DONE_TEXT = {
    "Extracting map data...": "Extracted map data",
    "Parsing CKDs and metadata...": "Parsed CKDs and metadata",
    "Normalizing assets...": "Normalized assets",
    "Decoding XMA2 audio...": "Decoded XMA2 audio",
    "Converting audio (pad/trim)...": "Converted audio (pad/trim)",
    "Generating intro AMB...": "Generated intro AMB",
    "Copying video files...": "Copied video files",
    "Converting dance tapes...": "Converted dance tapes",
    "Converting karaoke tapes...": "Converted karaoke tapes",
    "Converting cinematic tapes...": "Converted cinematic tapes",
    "Processing ambient sounds...": "Processed ambient sounds",
    "Decoding MenuArt textures...": "Decoded MenuArt textures",
    "Decoding pictograms...": "Decoded pictograms",
    "Integrating move data...": "Integrated move data",
    "Registering in SkuScene...": "Registered in SkuScene",
    "Finalizing offsets...": "Finalized offsets",
}


class ProgressLogWidget(QWidget):
    """Combined progress checklist, progress bar, and live log panel."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._step_items: dict[str, QListWidgetItem] = {}
        self._last_progress: int = 0
        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        section_label = QLabel("Progress")
        section_label.setObjectName("progressSectionLabel")
        root.addWidget(section_label)

        # -- Checklist -------------------------------------------------------
        self._checklist = QListWidget()
        self._checklist.setObjectName("progressChecklist")
        self._checklist.setAlternatingRowColors(False)
        self._checklist.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self._checklist.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._checklist.setToolTip("Tracks each installation step individually. Hover over failed items for details.")
        root.addWidget(self._checklist)

        # -- Progress bar ----------------------------------------------------
        self._progress = QProgressBar()
        self._progress.setObjectName("progressMainBar")
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setToolTip("Displays the overall completion percentage of the current installation batch.")
        root.addWidget(self._progress)

    # ------------------------------------------------------------------
    # Checklist API
    # ------------------------------------------------------------------

    def set_checklist_steps(self, steps: list[str]) -> None:
        """Initialise (or reset) the checklist with named steps."""
        self._checklist.clear()
        self._step_items.clear()
        for name in steps:
            item = QListWidgetItem(f"{_STATUS_ICONS[StepStatus.WAITING]}  {name}")
            item.setData(Qt.ItemDataRole.UserRole, name)
            self._checklist.addItem(item)
            self._step_items[name] = item

    def update_checklist_step(self, step_name: str, status: StepStatus, prefix: str = "", suffix: str = "") -> None:
        """Update the icon for *step_name* to reflect *status*. 
        Optional *prefix* is prepended and *suffix* is appended to the name.
        """
        item = self._step_items.get(step_name)
        if item is None:
            logger.warning("Checklist step not found: %s", step_name)
            return
        icon = _STATUS_ICONS[status]
        step_label = _STEP_DONE_TEXT.get(step_name, step_name) if status == StepStatus.DONE else step_name
        display_text = f"{icon}  "
        if prefix:
            display_text += f"{prefix} "
        display_text += step_label
        if suffix:
            display_text += f"  ({suffix})"
        item.setText(display_text)

        # Colour hint for quick scanning
        colour_map = {
            StepStatus.WAITING: QColor("#888888"),
            StepStatus.IN_PROGRESS: QColor("#2196F3"),
            StepStatus.DONE: QColor("#4CAF50"),
            StepStatus.ERROR: QColor("#F44336"),
        }
        default_colour = self.palette().color(self.foregroundRole())
        item.setForeground(colour_map.get(status, default_colour))

        if status != StepStatus.WAITING:
            self._checklist.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)

    # ------------------------------------------------------------------
    # Progress bar API
    # ------------------------------------------------------------------

    def set_progress(self, value: int) -> None:
        """Set progress bar value (0–100). Monotonically non-decreasing to prevent
        backward jumps when transitioning between worker phases."""
        clamped = max(0, min(100, value))
        if clamped < self._last_progress:
            return
        self._last_progress = clamped
        self._progress.setValue(clamped)

    def reset_progress(self) -> None:
        """Reset the progress bar to zero."""
        self._last_progress = 0
        self._progress.setValue(0)


    # ------------------------------------------------------------------
    # Combined reset
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Clear checklist and progress bar."""
        self._checklist.clear()
        self._step_items.clear()
        self._last_progress = 0
        self._progress.setValue(0)
