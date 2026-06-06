"""Dialog displaying install completion checklists and stats in a rich UI."""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QDialog,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from jd2021_installer.core.install_summary import InstallSummary, format_size, InstallChecklistItem


def make_stat_widget(value: str, label_str: str) -> QWidget:
    w = QWidget()
    l = QVBoxLayout(w)
    l.setContentsMargins(0, 0, 0, 0)
    l.setSpacing(2)
    l.setAlignment(Qt.AlignmentFlag.AlignCenter)
    
    val = QLabel(value)
    val.setAlignment(Qt.AlignmentFlag.AlignCenter)
    val.setStyleSheet("font-size: 16px; font-weight: bold;")
    
    lbl = QLabel(label_str)
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lbl.setStyleSheet("font-size: 10px; color: #80848e; font-weight: bold; text-transform: uppercase;")
    
    l.addWidget(val)
    l.addWidget(lbl)
    return w


class SummaryCard(QGroupBox):
    """A rich card displaying a single InstallSummary with terminal-style checklists."""

    def __init__(self, summary: InstallSummary, is_single: bool = False, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("summaryCard")
        
        if is_single:
            self.setStyleSheet("""
                QGroupBox#summaryCard {
                    background: transparent;
                    border: none;
                    margin: 0;
                    padding: 0;
                }
            """)
        else:
            # Let the application's global QGroupBox QSS style handle it!
            pass
        
        self._build_ui(summary)

    def _build_ui(self, summary: InstallSummary) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # 1. Header Row
        header_layout = QHBoxLayout()
        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        
        title_label = QLabel(summary.map_name)
        title_label.setStyleSheet("font-size: 20px; font-weight: bold;")
        
        subtitle_label = QLabel(f"Codename: {summary.codename}")
        subtitle_label.setStyleSheet("font-size: 13px; color: #80848e;")
        
        title_box.addWidget(title_label)
        title_box.addWidget(subtitle_label)
        header_layout.addLayout(title_box)
        header_layout.addStretch()

        # Status Badge
        status_label = QLabel(summary.status_label)
        status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if summary.success:
            if summary.has_required_missing or summary.missing_optional_count > 0:
                status_color = "#f1c40f"
                status_bg = "rgba(241, 196, 15, 0.1)"
            else:
                status_color = "#2ecc71"
                status_bg = "rgba(46, 204, 113, 0.1)"
        else:
            status_color = "#e74c3c"
            status_bg = "rgba(231, 76, 60, 0.1)"

        status_label.setStyleSheet(f"""
            QLabel {{
                background-color: {status_bg};
                color: {status_color};
                border: 1px solid {status_color};
                border-radius: 4px;
                padding: 6px 16px;
                font-weight: bold;
                font-size: 14px;
                letter-spacing: 1px;
            }}
        """)
        header_layout.addWidget(status_label)
        layout.addLayout(header_layout)

        # Separator
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setStyleSheet("background-color: rgba(128, 128, 128, 0.2);")
        layout.addWidget(line)

        # 2. Stats Dashboard
        stats_layout = QHBoxLayout()
        stats_layout.addWidget(make_stat_widget(summary.source_mode, "Mode"))
        stats_layout.addWidget(make_stat_widget(summary.quality, "Quality"))
        stats_layout.addWidget(make_stat_widget(f"{summary.duration_s:.1f}s", "Duration"))
        stats_layout.addWidget(make_stat_widget(str(summary.files_written_count), "Files"))
        stats_layout.addWidget(make_stat_widget(format_size(summary.total_size_bytes), "Size"))
        layout.addLayout(stats_layout)

        # 3. Checklists Layout (No extra background to avoid jarring greys)
        console_frame = QFrame()
        console_frame.setObjectName("consoleFrame")
        console_frame.setStyleSheet("""
            QFrame#consoleFrame {
                background: transparent;
                border: none;
            }
        """)
        console_layout = QHBoxLayout(console_frame)
        console_layout.setContentsMargins(16, 12, 16, 12)
        console_layout.setSpacing(32)

        req_section = self._create_checklist_section("REQUIRED FILES", summary.required_items, is_required=True)
        opt_section = self._create_checklist_section("OPTIONAL FILES", summary.optional_items, is_required=False)

        console_layout.addWidget(req_section)
        console_layout.addWidget(opt_section)
        console_layout.setStretch(0, 1)
        console_layout.setStretch(1, 1)

        layout.addWidget(console_frame)

        # 4. Actionable Note (Banner)
        note_banner = QLabel(summary.actionable_note)
        note_banner.setWordWrap(True)
        note_banner.setStyleSheet(f"""
            QLabel {{
                color: {status_color};
                font-style: italic;
                font-size: 13px;
                margin-top: 4px;
            }}
        """)
        layout.addWidget(note_banner)

    def _create_checklist_section(self, title: str, items: list[InstallChecklistItem], is_required: bool) -> QWidget:
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(0, 0, 0, 0)
        l.setSpacing(10)
        
        lbl_title = QLabel(title)
        lbl_title.setStyleSheet("font-size: 13px; font-weight: bold; color: #80848e; font-family: monospace;")
        l.addWidget(lbl_title)
        
        list_layout = QVBoxLayout()
        list_layout.setContentsMargins(0, 0, 0, 0)
        list_layout.setSpacing(6)
        
        for item in items:
            row_widget = QWidget()
            row_layout = QHBoxLayout(row_widget)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            
            status_label = QLabel()
            status_label.setFixedWidth(24)
            status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            
            if item.present:
                status_label.setText("✅")
                status_label.setStyleSheet("font-size: 14px;")
                item_color = ""
            else:
                if is_required:
                    status_label.setText("❌")
                    status_label.setStyleSheet("font-size: 14px;")
                    item_color = "color: #e74c3c;"
                else:
                    status_label.setText("⚠️")
                    status_label.setStyleSheet("font-size: 14px;")
                    item_color = "color: #80848e;"
            
            name_label = QLabel(item.label)
            name_label.setStyleSheet(f"{item_color} font-family: Consolas, monospace; font-size: 13px;")
            
            row_layout.addWidget(status_label)
            row_layout.addWidget(name_label)
            row_layout.addStretch()
            
            list_layout.addWidget(row_widget)
                
        l.addLayout(list_layout)
        l.addStretch()
        return w


class InstallationSummaryDialog(QDialog):
    """Modal summary dialog shown after install pipeline completion."""

    def __init__(self, summaries: list[InstallSummary], parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowTitle("Installation Summary")
        self.setMinimumWidth(850)
        
        # Fix dark mode labeling artifacts where QLabel inherits the QWidget solid background
        self.setStyleSheet("QLabel { background: transparent; }")
        
        if len(summaries) > 1:
            self.setMinimumHeight(750)
            
        self._summaries = summaries
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # Dialog Title
        heading = QLabel("Installation Summary")
        heading.setStyleSheet("font-size: 24px; font-weight: bold;")
        layout.addWidget(heading)

        if len(self._summaries) == 1:
            # Single Map: No scroll area, directly embed the content without card borders
            card = SummaryCard(self._summaries[0], is_single=True)
            layout.addWidget(card)
            layout.addStretch()
        else:
            # Batch Maps: Use Scroll Area with Card styling
            scroll_area = QScrollArea()
            scroll_area.setWidgetResizable(True)
            scroll_area.setFrameShape(QFrame.Shape.NoFrame)
            scroll_area.setStyleSheet("""
                QScrollArea { background: transparent; border: none; }
                QScrollArea > QWidget > QWidget { background: transparent; }
            """)
            
            scroll_content = QWidget()
            scroll_content.setObjectName("scrollContent")
            scroll_content.setStyleSheet("QWidget#scrollContent { background: transparent; }")
            scroll_layout = QVBoxLayout(scroll_content)
            scroll_layout.setContentsMargins(0, 0, 8, 0)
            scroll_layout.setSpacing(20)
    
            for summary in self._summaries:
                card = SummaryCard(summary, is_single=False)
                scroll_layout.addWidget(card)
    
            scroll_layout.addStretch()
            scroll_area.setWidget(scroll_content)
            layout.addWidget(scroll_area)

        # Bottom Button Row
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QPushButton("Done")
        close_btn.clicked.connect(self.accept)
        close_btn.setMinimumWidth(120)
        close_btn.setMinimumHeight(36)
        close_btn.setStyleSheet("""
            QPushButton {
                font-weight: bold;
                font-size: 14px;
                border-radius: 4px;
            }
        """)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

    @staticmethod
    def show_summaries(summaries: list[InstallSummary], parent: Optional[QWidget] = None) -> None:
        if not summaries:
            return
        dialog = InstallationSummaryDialog(summaries, parent)
        dialog.exec()