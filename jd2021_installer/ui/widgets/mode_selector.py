"""Mode-selection widget: choose input mode and provide dynamic inputs.

Modes
-----
- **Fetch**   – Enter one or more song codenames to scrape from the web.
- **HTML**    – Load a previously-saved HTML page.
- **IPK**     – Load a local IPK archive file.
- **Batch**   – Select a directory containing multiple IPK files.
- **Manual**  – Point to a pre-extracted map directory or set of files.

Each mode shows a tailored input area via a ``QStackedWidget``.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QSignalBlocker, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from jd2021_installer.extractors.archive_ipk import find_bundle_ipks

logger = logging.getLogger("jd2021.ui.widgets.mode_selector")

# Mode identifiers (indices match the combo-box order)
MODE_FETCH = 0
MODE_HTML = 1
MODE_JDNEXT = 2
MODE_HTML_JDNEXT = 3
MODE_IPK = 4
MODE_BATCH = 5
MODE_MANUAL = 6

MODE_LABELS = [
    "Fetch JDU",
    "HTML JDU",
    "Fetch JDNext",
    "HTML JDNext",
    "IPK Archive",
    "Batch (Directory)",
    "Manual (Directory)",
]

MODE_KEYS = [
    "fetch",
    "html",
    "jdnext",
    "html_jdnext",
    "ipk",
    "batch",
    "manual",
]


class FileRowWidget(QWidget):
    """Reusable row for labeled file/directory input with a 'Browse' button."""

    path_changed = pyqtSignal(str)

    def __init__(
        self,
        label_text: str,
        is_dir: bool = False,
        file_filter: str = "All Files (*.*)",
        placeholder: str = "",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self.is_dir = is_dir
        self.file_filter = file_filter

        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel(label_text)
        lbl.setMinimumWidth(120)
        lay.addWidget(lbl)

        self.line_edit = QLineEdit()
        self.line_edit.setReadOnly(True)
        self.line_edit.setPlaceholderText(placeholder)
        self.line_edit.setToolTip(f"Selected path for {label_text.rstrip(':')}")
        lay.addWidget(self.line_edit)

        btn = QPushButton("Browse")
        btn.setToolTip(f"Browse and select {label_text.rstrip(':')}")
        btn.clicked.connect(self._browse)
        lay.addWidget(btn)

        btn_clear = QPushButton("Clear")
        btn_clear.setToolTip(f"Clear selected path for {label_text.rstrip(':')}")
        btn_clear.clicked.connect(self._clear)
        lay.addWidget(btn_clear)

    def _browse(self) -> None:
        if self.is_dir:
            path = QFileDialog.getExistingDirectory(self, "Select Directory")
        else:
            # For HTML files, default to mapDownloads folder
            initial_dir = ""
            if "html" in self.file_filter.lower():
                map_downloads = Path(__file__).parent.parent.parent.parent / "mapDownloads"
                if map_downloads.exists():
                    initial_dir = str(map_downloads)
            
            path, _ = QFileDialog.getOpenFileName(
                self, "Select File", initial_dir, self.file_filter
            )

        if path:
            self.line_edit.setText(path)
            self.path_changed.emit(path)

    def _clear(self) -> None:
        with QSignalBlocker(self.line_edit):
            self.line_edit.clear()
        self.path_changed.emit("")


class ModeSelectorWidget(QWidget):
    """Dropdown + dynamic input area for selecting the map-import mode."""

    mode_changed = pyqtSignal(str)  # emits the label string
    target_selected = pyqtSignal(str)  # emits the user-provided input/main path
    source_state_changed = pyqtSignal(dict)  # emits current mode + inputs snapshot

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.inputs: dict[str, dict[str, QLineEdit]] = {
            "fetch": {},
            "html": {},
            "jdnext": {},
            "html_jdnext": {},
            "ipk": {},
            "batch": {},
            "manual": {},
        }
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        self.setObjectName("modeSelectorWidget")

        # Mode row layout to keep combo box concise
        mode_row = QHBoxLayout()
        lbl = QLabel("Mode:")
        lbl.setObjectName("modeSelectorLabel")
        lbl.setMinimumWidth(80)
        mode_row.addWidget(lbl)

        self._mode_combo = QComboBox()
        self._mode_combo.addItems(MODE_LABELS)
        self._mode_combo.setToolTip("Choose the input method for map files. Use IPK for bundles, Fetch for Discord URLs, or Local for folders.")
        self._mode_combo.currentIndexChanged.connect(self._on_mode_index_changed)
        mode_row.addWidget(self._mode_combo)
        mode_row.addStretch()
        root.addLayout(mode_row)

        # Stacked widget for mode-specific inputs
        self._stack = QStackedWidget()
        self._stack.setObjectName("modeSelectorStack")
        self._stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        root.addWidget(self._stack)

        self._stack.addWidget(self._build_fetch_page())  # 0
        self._stack.addWidget(self._build_html_page())  # 1
        self._stack.addWidget(self._build_jdnext_page())  # 2
        self._stack.addWidget(self._build_html_jdnext_page())  # 3
        self._stack.addWidget(self._build_ipk_page())  # 4
        self._stack.addWidget(self._build_batch_page())  # 5
        self._stack.addWidget(self._build_manual_page())  # 6
        self._wire_state_signals()
        self._fit_current_page_height()

    # -- Mode Pages ---------------------------------------------------------

    def _build_codename_fetch_page(
        self,
        *,
        input_key: str,
        warning_text: str,
        placeholder: str,
    ) -> QWidget:
        page = QWidget()
        page.setObjectName("modePage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 0)

        warn = QLabel(warning_text)
        warn.setObjectName("modeFetchWarningLabel")
        warn.setWordWrap(True)
        lay.addWidget(warn)

        row = QHBoxLayout()

        lbl = QLabel("Codename(s):")
        lbl.setMinimumWidth(120)
        row.addWidget(lbl)

        inp = QLineEdit()
        inp.setPlaceholderText(placeholder)
        inp.setToolTip("Enter one or more codenames, separated by commas, to target specific maps.")
        inp.textChanged.connect(lambda t: self.target_selected.emit(t))
        row.addWidget(inp)

        lay.addLayout(row)

        self.inputs[input_key]["codenames"] = inp
        return page

    def _build_fetch_page(self) -> QWidget:
        return self._build_codename_fetch_page(
            input_key="fetch",
            warning_text=(
                "Fetch automates acquiring the asset and nohud HTML files and downloads. "
                "Make sure to set your Discord channel link that can access JDHelper."
            ),
            placeholder="e.g. RainOnMe, DontStartNow",
        )

    def _build_jdnext_page(self) -> QWidget:
        return self._build_codename_fetch_page(
            input_key="jdnext",
            warning_text=(
                "Fetch automates acquiring the asset HTML file and downloads. "
                "Make sure to set your Discord channel link that can access JDHelper."
            ),
            placeholder="e.g. TelephoneALT",
        )

    def _build_html_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("modePage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 0)

        warn = QLabel(
            "⚠️ Asset/NoHUD links expire after ~30 minutes! Fetch fresh links if download fails. If the assets and nohud are already downloaded, ignore this warning."
        )
        warn.setObjectName("modeHtmlWarningLabel")
        warn.setWordWrap(True)
        lay.addWidget(warn)

        asset_row = FileRowWidget(
            "Asset HTML:",
            is_dir=False,
            file_filter="HTML Files (*.html *.htm)",
            placeholder="No file selected",
        )
        lay.addWidget(asset_row)

        nohud_row = FileRowWidget(
            "NOHUD HTML:",
            is_dir=False,
            file_filter="HTML Files (*.html *.htm)",
            placeholder="No file selected",
        )
        lay.addWidget(nohud_row)

        self.inputs["html"]["asset"] = asset_row.line_edit
        self.inputs["html"]["nohud"] = nohud_row.line_edit

        # Auto-detect counterparts (V1 parity)
        def auto_detect(source_path: str, is_asset: bool):
            if not source_path:
                return
            src = Path(source_path)
            asset_guess, nohud_guess = self._find_html_pair(src.parent)

            if is_asset and nohud_guess:
                current_nohud = self.inputs["html"]["nohud"].text().strip()
                detected_nohud = str(nohud_guess)
                if current_nohud != detected_nohud:
                    self.inputs["html"]["nohud"].setText(detected_nohud)
                    logger.info("Auto-detected NOHUD HTML: %s", nohud_guess)
            elif not is_asset and asset_guess:
                current_asset = self.inputs["html"]["asset"].text().strip()
                detected_asset = str(asset_guess)
                if current_asset != detected_asset:
                    self.inputs["html"]["asset"].setText(detected_asset)
                    logger.info("Auto-detected Asset HTML: %s", asset_guess)

            # Keep a valid target selected no matter which HTML field was browsed.
            target = self.inputs["html"]["asset"].text().strip() or source_path
            self.target_selected.emit(target)

        asset_row.path_changed.connect(lambda p: auto_detect(p, True))
        nohud_row.path_changed.connect(lambda p: auto_detect(p, False))

        return page

    def _build_html_jdnext_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("modePage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 0)

        warn = QLabel(
            "⚠️ Asset links expire after ~30 minutes! Fetch fresh links if download fails. If the assets are already downloaded, ignore this warning."
        )
        warn.setObjectName("modeHtmlWarningLabel")
        warn.setWordWrap(True)
        lay.addWidget(warn)

        asset_row = FileRowWidget(
            "Asset HTML:",
            is_dir=False,
            file_filter="HTML Files (*.html *.htm)",
            placeholder="No file selected",
        )
        asset_row.path_changed.connect(lambda t: self.target_selected.emit(t))
        lay.addWidget(asset_row)

        self.inputs["html_jdnext"]["asset"] = asset_row.line_edit
        return page

    def _build_ipk_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("modePage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 0)

        warn = QLabel(
            "IPK installs .IPK maps that either contain one map or bundles that contain more than one map."
        )
        warn.setObjectName("modeIpkWarningLabel")
        warn.setWordWrap(True)
        lay.addWidget(warn)

        row = FileRowWidget(
            "IPK File:",
            is_dir=False,
            file_filter="IPK Archives (*.ipk);;All Files (*.*)",
            placeholder="No IPK selected",
        )
        lay.addWidget(row)

        bundle_row = FileRowWidget(
            "Bundle IPK (optional):",
            is_dir=False,
            file_filter="IPK Archives (*.ipk);;All Files (*.*)",
            placeholder="Auto-detected if available",
        )
        lay.addWidget(bundle_row)

        bundlelogic_row = FileRowWidget(
            "BundleLogic IPK (optional):",
            is_dir=False,
            file_filter="IPK Archives (*.ipk);;All Files (*.*)",
            placeholder="Auto-detected if available",
        )
        lay.addWidget(bundlelogic_row)

        self.inputs["ipk"]["file"] = row.line_edit
        self.inputs["ipk"]["bundle"] = bundle_row.line_edit
        self.inputs["ipk"]["bundlelogic"] = bundlelogic_row.line_edit

        def auto_detect_ipk_bundle(source_path: str) -> None:
            if not source_path:
                return
            ipk_path = Path(source_path)
            if not ipk_path.is_file():
                return

            bundle_guess, bundlelogic_guess = find_bundle_ipks(ipk_path.parent, exclude=ipk_path)

            if bundle_guess:
                current_bundle = self.inputs["ipk"]["bundle"].text().strip()
                detected_bundle = str(bundle_guess)
                if current_bundle != detected_bundle:
                    self.inputs["ipk"]["bundle"].setText(detected_bundle)
                    logger.info("Auto-detected Bundle IPK: %s", bundle_guess)

            if bundlelogic_guess:
                current_bundlelogic = self.inputs["ipk"]["bundlelogic"].text().strip()
                detected_bundlelogic = str(bundlelogic_guess)
                if current_bundlelogic != detected_bundlelogic:
                    self.inputs["ipk"]["bundlelogic"].setText(detected_bundlelogic)
                    logger.info("Auto-detected BundleLogic IPK: %s", bundlelogic_guess)

            self.target_selected.emit(source_path)

        row.path_changed.connect(auto_detect_ipk_bundle)
        return page

    def _build_batch_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("modePage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 0)

        warn = QLabel(
            "Batch installs from a folder containing map subfolders. Can be used with Fetch/HTML/IPK modes' files, this can be .html, .ipk, or already-extracted map folders."
        )
        warn.setObjectName("modeBatchHintLabel")
        warn.setWordWrap(True)
        lay.addWidget(warn)

        row = FileRowWidget(
            "Maps Folder:", is_dir=True, placeholder="No directory selected"
        )
        row.path_changed.connect(lambda t: self.target_selected.emit(t))
        lay.addWidget(row)

        self.inputs["batch"]["dir"] = row.line_edit
        return page

    def _build_manual_page(self) -> QWidget:
        """The monster manual page mirroring Tkinter V1."""
        page = QWidget()
        page.setObjectName("modePage")
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 4, 0, 0)

        # Add scroll area since there are many fields
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        
        scroll_content = QWidget()
        scroll_content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        scroll_lay = QVBoxLayout(scroll_content)
        scroll_lay.setContentsMargins(0, 0, 0, 0)
        scroll_lay.setAlignment(Qt.AlignmentFlag.AlignTop)

        warn = QLabel(
            "Manual mode is best for already-extracted map folders or when you need to point the installer at files by hand."
        )
        warn.setObjectName("modeManualWarningLabel")
        warn.setWordWrap(True)
        scroll_lay.addWidget(warn)

        source_lay = QHBoxLayout()
        source_lay.addWidget(QLabel("Source Type:"))
        self._manual_source_combo = QComboBox()
        self._manual_source_combo.addItem("JDU", "jdu")
        self._manual_source_combo.addItem("IPK", "ipk")
        self._manual_source_combo.addItem("Mixed", "mixed")
        self._manual_source_combo.setCurrentIndex(2)
        self._manual_source_combo.setToolTip(
            "Choose how to interpret Manual fields. Detection is still shown as a hint."
        )
        self._manual_source_combo.currentIndexChanged.connect(
            lambda _idx: self._on_manual_source_type_changed()
        )
        source_lay.addWidget(self._manual_source_combo)
        source_lay.addStretch()
        scroll_lay.addLayout(source_lay)

        # Top generic entries
        top_lay = QGridLayout()
        top_lay.setContentsMargins(0, 0, 0, 0)
        
        root_row = FileRowWidget("Root Folder:", is_dir=True)
        root_row.path_changed.connect(self._on_manual_root_changed)
        top_lay.addWidget(root_row, 0, 0, 1, 2)

        self._manual_scan_btn = QPushButton("Scan")
        self._manual_scan_btn.setToolTip("Rescan the selected folder to detect new or removed map files.")
        self._manual_scan_btn.clicked.connect(self._on_manual_scan_clicked)
        top_lay.addWidget(self._manual_scan_btn, 0, 2)

        lbl_code = QLabel("Codename:")
        lbl_code.setMinimumWidth(120)
        top_lay.addWidget(lbl_code, 1, 0)
        inp_code = QLineEdit()
        inp_code.setToolTip("Enter the codename to use for named assets and directory structure generation (e.g. BangBang).")
        top_lay.addWidget(inp_code, 1, 1)

        scroll_lay.addLayout(top_lay)

        # Required Files Group
        self._manual_required_group = QGroupBox("Required Files")
        lay_req = QVBoxLayout(self._manual_required_group)
        
        row_audio = FileRowWidget("Audio File:", file_filter="Audio (*.ogg *.wav *.wav.ckd);;All (*.*)")
        row_video = FileRowWidget("Video File:", file_filter="WebM (*.webm);;All (*.*)")
        row_mtrack = FileRowWidget("Musictrack:", file_filter="Musictrack (*.ckd *.trk);;All (*.*)")
        
        lay_req.addWidget(row_audio)
        lay_req.addWidget(row_video)
        lay_req.addWidget(row_mtrack)
        scroll_lay.addWidget(self._manual_required_group)

        # Optional Tapes Group
        self._manual_tapes_group = QGroupBox("Tapes & Config")
        lay_tapes = QVBoxLayout(self._manual_tapes_group)
        
        row_sdesc = FileRowWidget("Songdesc", file_filter="CKD (*.ckd);;All (*.*)")
        row_dtape = FileRowWidget("Dance Tape", file_filter="Tape Files (*.dtape *.dtape.ckd *.ckd);;All (*.*)")
        row_ktape = FileRowWidget("Karaoke Tape", file_filter="Tape Files (*.ktape *.ktape.ckd *.ckd);;All (*.*)")
        row_mseq = FileRowWidget("Mainseq Tape", file_filter="CKD (*.ckd);;All (*.*)")
        
        lay_tapes.addWidget(row_sdesc)
        lay_tapes.addWidget(row_dtape)
        lay_tapes.addWidget(row_ktape)
        lay_tapes.addWidget(row_mseq)
        scroll_lay.addWidget(self._manual_tapes_group)

        # Optional Assets Group
        self._manual_assets_group = QGroupBox("Asset Folders")
        lay_assets = QVBoxLayout(self._manual_assets_group)
        
        self._manual_row_moves = FileRowWidget("Moves Folder:", is_dir=True)
        self._manual_row_pictos = FileRowWidget("Pictos Folder:", is_dir=True)
        self._manual_row_menuart = FileRowWidget("MenuArt Folder:", is_dir=True)
        self._manual_row_amb = FileRowWidget("AMB Folder:", is_dir=True)
        
        lay_assets.addWidget(self._manual_row_moves)
        lay_assets.addWidget(self._manual_row_pictos)
        lay_assets.addWidget(self._manual_row_menuart)
        lay_assets.addWidget(self._manual_row_amb)
        scroll_lay.addWidget(self._manual_assets_group)

        self._manual_menuart_group = QGroupBox("MenuArt")
        lay_menuart = QVBoxLayout(self._manual_menuart_group)
        menuart_note = QLabel(
            "MenuArt fields are shown only when they exist in the source."
        )
        menuart_note.setWordWrap(True)
        lay_menuart.addWidget(menuart_note)
        self._manual_jdu_menuart_rows: dict[str, FileRowWidget] = {}

        row_jdu_cover_generic = FileRowWidget(
            "Cover Generic:", file_filter="Images (*.png *.tga *.jpg *.jpeg *.dds *.ckd);;All (*.*)"
        )
        row_jdu_cover_online = FileRowWidget(
            "Cover Online:", file_filter="Images (*.png *.tga *.jpg *.jpeg *.dds *.ckd);;All (*.*)"
        )
        row_jdu_banner = FileRowWidget(
            "Banner:", file_filter="Images (*.png *.tga *.jpg *.jpeg *.dds *.ckd);;All (*.*)"
        )
        row_jdu_banner_bkg = FileRowWidget(
            "Banner Bkg:", file_filter="Images (*.png *.tga *.jpg *.jpeg *.dds *.ckd);;All (*.*)"
        )
        row_jdu_map_bkg = FileRowWidget(
            "Map Bkg:", file_filter="Images (*.png *.tga *.jpg *.jpeg *.dds *.ckd);;All (*.*)"
        )
        row_jdu_cover_albumcoach = FileRowWidget(
            "Cover AlbumCoach:", file_filter="Images (*.png *.tga *.jpg *.jpeg *.dds *.ckd);;All (*.*)"
        )
        row_jdu_cover_albumbkg = FileRowWidget(
            "Cover AlbumBkg:", file_filter="Images (*.png *.tga *.jpg *.jpeg *.dds *.ckd);;All (*.*)"
        )
        row_jdu_coach1 = FileRowWidget(
            "Coach 1 Art:", file_filter="Images (*.png *.tga *.jpg *.jpeg *.dds *.ckd);;All (*.*)"
        )
        row_jdu_coach2 = FileRowWidget(
            "Coach 2 Art:", file_filter="Images (*.png *.tga *.jpg *.jpeg *.dds *.ckd);;All (*.*)"
        )
        row_jdu_coach3 = FileRowWidget(
            "Coach 3 Art:", file_filter="Images (*.png *.tga *.jpg *.jpeg *.dds *.ckd);;All (*.*)"
        )
        row_jdu_coach4 = FileRowWidget(
            "Coach 4 Art:", file_filter="Images (*.png *.tga *.jpg *.jpeg *.dds *.ckd);;All (*.*)"
        )

        self._manual_jdu_menuart_rows = {
            "jdu_menuart_cover_generic": row_jdu_cover_generic,
            "jdu_menuart_cover_online": row_jdu_cover_online,
            "jdu_menuart_banner": row_jdu_banner,
            "jdu_menuart_banner_bkg": row_jdu_banner_bkg,
            "jdu_menuart_map_bkg": row_jdu_map_bkg,
            "jdu_menuart_cover_albumcoach": row_jdu_cover_albumcoach,
            "jdu_menuart_cover_albumbkg": row_jdu_cover_albumbkg,
            "jdu_menuart_coach1": row_jdu_coach1,
            "jdu_menuart_coach2": row_jdu_coach2,
            "jdu_menuart_coach3": row_jdu_coach3,
            "jdu_menuart_coach4": row_jdu_coach4,
        }
        for row in self._manual_jdu_menuart_rows.values():
            lay_menuart.addWidget(row)
        scroll_lay.addWidget(self._manual_menuart_group)
        
        # Keep a small bottom buffer so the final field is never flush/clipped.
        scroll_lay.addSpacing(8)
        
        scroll.setWidget(scroll_content)
        lay.addWidget(scroll, 1)
        
        self.inputs["manual"].update({
            "root": root_row.line_edit,
            "codename": inp_code,
            "audio": row_audio.line_edit,
            "video": row_video.line_edit,
            "mtrack": row_mtrack.line_edit,
            "sdesc": row_sdesc.line_edit,
            "dtape": row_dtape.line_edit,
            "ktape": row_ktape.line_edit,
            "mseq": row_mseq.line_edit,
            "moves": self._manual_row_moves.line_edit,
            "pictos": self._manual_row_pictos.line_edit,
            "menuart": self._manual_row_menuart.line_edit,
            "amb": self._manual_row_amb.line_edit,
            "jdu_menuart_cover_generic": row_jdu_cover_generic.line_edit,
            "jdu_menuart_cover_online": row_jdu_cover_online.line_edit,
            "jdu_menuart_banner": row_jdu_banner.line_edit,
            "jdu_menuart_banner_bkg": row_jdu_banner_bkg.line_edit,
            "jdu_menuart_map_bkg": row_jdu_map_bkg.line_edit,
            "jdu_menuart_cover_albumcoach": row_jdu_cover_albumcoach.line_edit,
            "jdu_menuart_cover_albumbkg": row_jdu_cover_albumbkg.line_edit,
            "jdu_menuart_coach1": row_jdu_coach1.line_edit,
            "jdu_menuart_coach2": row_jdu_coach2.line_edit,
            "jdu_menuart_coach3": row_jdu_coach3.line_edit,
            "jdu_menuart_coach4": row_jdu_coach4.line_edit,
        })

        self._apply_manual_layout_sections("unknown")
        
        return page

    # ------------------------------------------------------------------
    # Slots / helpers
    # ------------------------------------------------------------------

    def _on_mode_index_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._fit_current_page_height()
        self.mode_changed.emit(MODE_LABELS[index])
        self._emit_state_changed()
        logger.debug("Mode switched to: %s", MODE_LABELS[index])

    def _fit_current_page_height(self) -> None:
        """Keep mode input panel compact for simple modes and scrollable for manual mode."""
        current = self._stack.currentWidget()
        if current is None:
            return

        hint = max(64, current.sizeHint().height())
        if self._mode_combo.currentIndex() == MODE_MANUAL:
            self._stack.setMinimumHeight(320)
            self._stack.setMaximumHeight(16777215)
            return
        else:
            target_height = max(72, min(hint + 8, 200))

        self._stack.setMinimumHeight(target_height)
        self._stack.setMaximumHeight(target_height)

    def _on_manual_root_changed(self, path: str) -> None:
        """Triggered when root folder in manual mode is changed."""
        if not path.strip():
            self._reset_manual_inputs(keep_root=False)
            return

        self.target_selected.emit(path)
        self._reset_manual_inputs(keep_root=True)

        # Scan and auto-fill
        root = Path(path)
        if not root.is_dir():
            return

        source_type = self.manual_source_type
        scan_root = self._resolve_scan_root(root, source_type)
        layout = self._detect_manual_layout(root)
        self._apply_manual_layout_sections(layout)
        codename = self.inputs["manual"]["codename"].text().strip()
        
        # 1. Infer codename from structure/file hints before falling back to folder name.
        if not codename:
            inferred = self._infer_manual_codename(root)
            if not inferred:
                inferred = scan_root.name
            self.inputs["manual"]["codename"].setText(inferred)
            codename = inferred
        
        # 2. Auto-discover common files
        from jd2021_installer.parsers.normalizer import _find_ckd_files

        if not self.inputs["manual"]["mtrack"].text().strip():
            mtrack = self._pick_manual_musictrack(scan_root, codename or None, source_type)
            if mtrack:
                self.inputs["manual"]["mtrack"].setText(str(mtrack))

        mapping = {
            "sdesc": "*songdesc*.tpl.ckd",
            "mseq": "*mainsequence*.tape.ckd",
        }
        
        for key, pattern in mapping.items():
            found = _find_ckd_files(str(scan_root), pattern, codename=codename or None)
            if found and not self.inputs["manual"][key].text().strip():
                self.inputs["manual"][key].setText(found[0])

        if not self.inputs["manual"]["dtape"].text().strip():
            dtape = self._pick_manual_tape(scan_root, codename or None, source_type, tape_kind="dance")
            if dtape:
                self.inputs["manual"]["dtape"].setText(str(dtape))

        if not self.inputs["manual"]["ktape"].text().strip():
            ktape = self._pick_manual_tape(scan_root, codename or None, source_type, tape_kind="karaoke")
            if ktape:
                self.inputs["manual"]["ktape"].setText(str(ktape))
                
        # Audio/Video
        if not self.inputs["manual"]["video"].text().strip():
            video = self._pick_manual_video(scan_root, codename or None, source_type)
            if video:
                self.inputs["manual"]["video"].setText(str(video))

        if not self.inputs["manual"]["audio"].text().strip():
            audio = self._pick_manual_audio(scan_root, codename or None, source_type)
            if audio:
                self.inputs["manual"]["audio"].setText(str(audio))
        
        # Folders
        folders = self._discover_manual_folders(scan_root)
        for key, folder in folders.items():
            if folder and not self.inputs["manual"][key].text().strip():
                self.inputs["manual"][key].setText(str(folder))

        menuart_assets = self._find_jdu_menuart_assets(root, codename or None)
        for key, asset_path in menuart_assets.items():
            if asset_path and not self.inputs["manual"][key].text().strip():
                self.inputs["manual"][key].setText(str(asset_path))
        self._apply_jdu_menuart_visibility(menuart_assets)

    def _detect_manual_layout(self, root: Path) -> str:
        """Detect likely source layout: jdu, ipk, mixed, or unknown."""
        if not root.exists() or not root.is_dir():
            return "unknown"

        has_ipk_struct = (root / "world" / "maps").is_dir() or any(
            d.is_dir() and d.name.lower().startswith("jd") and d.name[2:].isdigit()
            for d in (root / "world").iterdir()
        ) if (root / "world").is_dir() else False

        has_asset_html, has_nohud_html = self._find_html_pair(root)
        has_html_pair = bool(has_asset_html and has_nohud_html)

        if has_ipk_struct and has_html_pair:
            return "mixed"
        if has_ipk_struct:
            return "ipk"
        if has_html_pair:
            return "jdu"
        return "unknown"

    def _apply_manual_layout_sections(self, layout: str) -> None:
        source_type = self.manual_source_type
        show_jdu = source_type in {"jdu", "mixed"}

        if hasattr(self, "_manual_required_group"):
            self._manual_required_group.setVisible(True)
        if hasattr(self, "_manual_tapes_group"):
            self._manual_tapes_group.setVisible(source_type in {"ipk", "mixed"})
        if hasattr(self, "_manual_assets_group"):
            self._manual_assets_group.setVisible(True)

        # JDU maps typically do not use AMB folders and use file-based menuart assets.
        if hasattr(self, "_manual_row_amb"):
            self._manual_row_amb.setVisible(source_type in {"ipk", "mixed"})
        if hasattr(self, "_manual_row_menuart"):
            self._manual_row_menuart.setVisible(source_type in {"ipk", "mixed"})
        if hasattr(self, "_manual_menuart_group"):
            self._manual_menuart_group.setVisible(show_jdu or source_type == "mixed")
        if hasattr(self, "_manual_jdu_menuart_rows"):
            for row in self._manual_jdu_menuart_rows.values():
                row.setVisible(show_jdu)

    def _on_manual_source_type_changed(self) -> None:
        root_path = self.inputs["manual"]["root"].text().strip()
        layout = self._detect_manual_layout(Path(root_path)) if root_path else "unknown"
        self._apply_manual_layout_sections(layout)
        if root_path:
            codename = self.inputs["manual"]["codename"].text().strip() or None
            self._apply_jdu_menuart_visibility(self._find_jdu_menuart_assets(Path(root_path), codename))
        self._emit_state_changed()

    def _reset_manual_inputs(self, keep_root: bool = False) -> None:
        manual_fields = self.inputs.get("manual", {})
        blocked = []
        for key, line_edit in manual_fields.items():
            if keep_root and key == "root" and line_edit.text().strip():
                continue
            blocked.append(QSignalBlocker(line_edit))
            line_edit.clear()

        # Keep blockers alive until all clears finish.
        del blocked

        if hasattr(self, "_manual_jdu_menuart_rows"):
            for row in self._manual_jdu_menuart_rows.values():
                row.setVisible(False)
        if hasattr(self, "_manual_menuart_group"):
            self._manual_menuart_group.setVisible(False)
        if hasattr(self, "_manual_row_amb"):
            self._manual_row_amb.setVisible(False)
        if hasattr(self, "_manual_row_menuart"):
            self._manual_row_menuart.setVisible(False)

    def _find_jdu_menuart_assets(
        self, root: Path, codename: Optional[str]
    ) -> dict[str, Optional[Path]]:
        result: dict[str, Optional[Path]] = {
            "jdu_menuart_cover_generic": None,
            "jdu_menuart_cover_online": None,
            "jdu_menuart_banner": None,
            "jdu_menuart_banner_bkg": None,
            "jdu_menuart_map_bkg": None,
            "jdu_menuart_cover_albumcoach": None,
            "jdu_menuart_cover_albumbkg": None,
            "jdu_menuart_coach1": None,
            "jdu_menuart_coach2": None,
            "jdu_menuart_coach3": None,
            "jdu_menuart_coach4": None,
        }
        if not root.exists() or not root.is_dir():
            return result

        image_suffixes = (".png", ".tga", ".jpg", ".jpeg", ".dds", ".ckd")
        files = [p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in image_suffixes]
        if codename:
            scoped = [p for p in files if self._matches_codename(p, codename)]
            if scoped:
                files = scoped

        sorted_files = sorted(files, key=lambda p: p.as_posix().lower())

        def find_first(tokens: tuple[str, ...]) -> Optional[Path]:
            for p in sorted_files:
                name = p.name.lower()
                if all(tok in name for tok in tokens):
                    return p
            return None

        result["jdu_menuart_cover_generic"] = find_first(("cover_generic",)) or find_first(("cover", "generic"))
        result["jdu_menuart_cover_online"] = find_first(("cover_online",)) or find_first(("cover", "online"))
        result["jdu_menuart_banner_bkg"] = find_first(("banner_bkg",)) or find_first(("banner", "bkg"))
        result["jdu_menuart_banner"] = find_first(("banner",))
        result["jdu_menuart_map_bkg"] = find_first(("map_bkg",)) or find_first(("map", "bkg"))
        result["jdu_menuart_cover_albumcoach"] = find_first(("albumcoach",)) or find_first(("album", "coach"))
        result["jdu_menuart_cover_albumbkg"] = find_first(("albumbkg",)) or find_first(("album", "bkg"))

        for idx in range(1, 5):
            key = f"jdu_menuart_coach{idx}"
            result[key] = (
                find_first((f"coach_{idx}",))
                or find_first(("coach", str(idx)))
                or find_first((f"c{idx}",))
                or find_first((f"_0{idx}",))
            )

        return result

    def _apply_jdu_menuart_visibility(self, detected: dict[str, Optional[Path]]) -> None:
        source_type = self.manual_source_type
        if not hasattr(self, "_manual_jdu_menuart_rows"):
            return

        for key, row in self._manual_jdu_menuart_rows.items():
            if source_type != "jdu":
                row.setVisible(source_type == "mixed")
                continue
            row.setVisible(bool(detected.get(key)))

    def _wire_state_signals(self) -> None:
        """Emit a normalized source-state payload whenever inputs change."""
        for mode_inputs in self.inputs.values():
            for line_edit in mode_inputs.values():
                line_edit.textChanged.connect(lambda _text: self._emit_state_changed())
        if hasattr(self, "_manual_source_combo"):
            self._manual_source_combo.currentIndexChanged.connect(
                lambda _index: self._emit_state_changed()
            )

    def _emit_state_changed(self) -> None:
        self.source_state_changed.emit(self.get_current_state())

    def _resolve_target_for_state(self, mode_key: str, fields: dict[str, str]) -> str:
        if mode_key == "fetch":
            return fields.get("codenames", "").strip()
        if mode_key == "jdnext":
            return fields.get("codenames", "").strip()
        if mode_key == "html":
            return fields.get("asset", "").strip() or fields.get("nohud", "").strip()
        if mode_key == "html_jdnext":
            return fields.get("asset", "").strip()
        if mode_key == "ipk":
            return fields.get("file", "").strip()
        if mode_key == "batch":
            return fields.get("dir", "").strip()
        if mode_key == "manual":
            return fields.get("root", "").strip() or fields.get("codename", "").strip()
        return ""

    def _matches_codename(self, path: Path, codename: Optional[str]) -> bool:
        if not codename:
            return True
        lower_codename = codename.lower()
        lower_name = path.name.lower()
        if re.match(rf"^{re.escape(lower_codename)}(?:[^a-z0-9]|$)", lower_name):
            return True
        return lower_codename in [p.lower() for p in path.parts]

    def _manual_source_is_recursive(self, source_type: str) -> bool:
        return True

    def _infer_manual_codename(self, root: Path) -> Optional[str]:
        """Infer codename from common map structures and filenames.

        Handles non-standard layouts where root is above the actual map folder
        (for example, a folder containing both top-level files and world/maps/<codename>/).
        """
        world_maps = root / "world" / "maps"
        if world_maps.is_dir():
            candidates = sorted([d for d in world_maps.iterdir() if d.is_dir()], key=lambda p: p.name.lower())
            if len(candidates) == 1:
                return candidates[0].name

        main_scene_candidates = sorted(
            [p for p in root.rglob("*_MAIN_SCENE.isc") if p.is_file()],
            key=lambda p: p.as_posix().lower(),
        )
        if main_scene_candidates:
            stem = main_scene_candidates[0].stem
            if stem.lower().endswith("_main_scene"):
                return stem[:-11]

        for pattern in ("*musictrack*", "*songdesc*"):
            files = sorted(
                [p for p in root.rglob(pattern) if p.is_file()],
                key=lambda p: p.as_posix().lower(),
            )
            if files:
                name = files[0].stem
                if name:
                    return name.split("_")[0]

        return None

    def _pick_preferred_dir(self, candidates: list[Path], codename: Optional[str]) -> Optional[Path]:
        """Pick the most relevant directory for a map, preferring codename-scoped paths."""
        if not candidates:
            return None

        candidates = sorted(candidates, key=lambda p: p.as_posix().lower())
        if codename:
            scoped = [p for p in candidates if self._matches_codename(p, codename)]
            if scoped:
                scoped.sort(key=lambda p: len(p.parts))
                return scoped[0]

        candidates.sort(key=lambda p: len(p.parts))
        return candidates[0]

    def _pick_manual_musictrack(self, scan_root: Path, codename: Optional[str], source_type: str) -> Optional[Path]:
        priority = ("*musictrack*.tpl.ckd", "*musictrack*.trk", "*.trk")

        for pattern in priority:
            top_hits = [p for p in scan_root.glob(pattern) if p.is_file()]
            if codename:
                scoped = [p for p in top_hits if self._matches_codename(p, codename)]
                if scoped:
                    return scoped[0]
            elif top_hits:
                return top_hits[0]

        for pattern in priority:
            hits = [p for p in scan_root.rglob(pattern) if p.is_file()]
            if not hits:
                continue
            if codename:
                scoped = [p for p in hits if self._matches_codename(p, codename)]
                if scoped:
                    return scoped[0]
            else:
                return hits[0]

        return None

    def _pick_manual_tape(
        self,
        scan_root: Path,
        codename: Optional[str],
        source_type: str,
        tape_kind: str,
    ) -> Optional[Path]:
        recursive = self._manual_source_is_recursive(source_type)
        searcher = scan_root.rglob if recursive else scan_root.glob

        if tape_kind == "dance":
            patterns = (
                "*_tml_dance.dtape",
                "*_tml_dance.dtape.ckd",
                "*dance*.dtape",
                "*dance*.dtape.ckd",
            )
        else:
            patterns = (
                "*_tml_karaoke.ktape",
                "*_tml_karaoke.ktape.ckd",
                "*karaoke*.ktape",
                "*karaoke*.ktape.ckd",
            )

        for pattern in patterns:
            hits = [p for p in searcher(pattern) if p.is_file()]
            if tape_kind == "dance":
                hits = [p for p in hits if "adtape" not in p.name.lower()]
            if not hits:
                continue
            if codename:
                scoped = [p for p in hits if self._matches_codename(p, codename)]
                if scoped:
                    return sorted(scoped, key=lambda p: p.as_posix().lower())[0]
            else:
                return sorted(hits, key=lambda p: p.as_posix().lower())[0]

        # Broad fallback for atypical filenames while still matching extension type.
        ext = ".dtape" if tape_kind == "dance" else ".ktape"
        candidates = [
            p for p in searcher(f"*{ext}*")
            if p.is_file() and p.name.lower().endswith((ext, f"{ext}.ckd"))
        ]
        if tape_kind == "dance":
            candidates = [p for p in candidates if "adtape" not in p.name.lower()]
        if not candidates:
            return None

        if codename:
            scoped = [p for p in candidates if self._matches_codename(p, codename)]
            if scoped:
                return sorted(scoped, key=lambda p: p.as_posix().lower())[0]
        return sorted(candidates, key=lambda p: p.as_posix().lower())[0]

    def _pick_manual_video(self, scan_root: Path, codename: Optional[str], source_type: str) -> Optional[Path]:
        recursive = self._manual_source_is_recursive(source_type)
        candidates = [
            p for p in (scan_root.rglob("*.webm") if recursive else scan_root.glob("*.webm"))
            if "mappreview" not in p.name.lower() and "videopreview" not in p.name.lower()
        ]

        if not candidates:
            return None

        if codename:
            scoped = [p for p in candidates if self._matches_codename(p, codename)]
            if scoped:
                candidates = scoped

        for quality in ["ULTRA_HD", "ULTRA", "HIGH_HD", "HIGH", "MID_HD", "MID", "LOW_HD", "LOW"]:
            suffix = f"_{quality}.webm"
            for path in candidates:
                if path.name.upper().endswith(suffix):
                    return path
        return candidates[0]

    def _pick_manual_audio(self, scan_root: Path, codename: Optional[str], source_type: str) -> Optional[Path]:
        recursive = self._manual_source_is_recursive(source_type)
        priority = ("*.ogg", "*.wav", "*.wav.ckd")

        for pattern in priority:
            hits = [p for p in (scan_root.rglob(pattern) if recursive else scan_root.glob(pattern)) if "audiopreview" not in p.name.lower()]
            hits = [
                p
                for p in hits
                if "autodance" not in str(p).lower() and not p.name.lower().startswith("amb_")
            ]
            if not hits:
                continue
            if codename:
                scoped = [p for p in hits if self._matches_codename(p, codename)]
                if scoped:
                    return scoped[0]
            else:
                return hits[0]

        return None

    def _discover_manual_folders(self, scan_root: Path) -> dict[str, Optional[Path]]:
        codename = self.inputs["manual"]["codename"].text().strip() or None

        menuart_texture_candidates = [
            p
            for p in scan_root.rglob("textures")
            if p.is_dir() and p.parent.name.lower() == "menuart"
        ]
        menuart_dir_candidates = [p for p in scan_root.rglob("menuart") if p.is_dir()]
        amb_candidates = [
            p
            for p in scan_root.rglob("amb")
            if p.is_dir() and p.parent.name.lower() == "audio"
        ]
        moves_candidates = [p for p in scan_root.rglob("moves") if p.is_dir()]
        picto_candidates = [p for p in scan_root.rglob("pictos") if p.is_dir()]

        menuart = self._pick_preferred_dir(menuart_texture_candidates, codename)
        if menuart is None:
            menuart = self._pick_preferred_dir(menuart_dir_candidates, codename)

        amb = self._pick_preferred_dir(amb_candidates, codename)

        return {
            "moves": self._pick_preferred_dir(moves_candidates, codename),
            "pictos": self._pick_preferred_dir(picto_candidates, codename),
            "menuart": menuart,
            "amb": amb,
        }

    def _resolve_scan_root(self, root: Path, source_type: str) -> Path:
        """Use the provided root and let recursive scanners find best matches.

        This avoids losing top-level files in mixed/non-standard layouts.
        """
        return root

    def _on_manual_scan_clicked(self) -> None:
        """Manual re-scan trigger that reuses the root-change scan routine."""
        root_path = self.inputs["manual"]["root"].text().strip()
        if root_path:
            self._on_manual_root_changed(root_path)

    def _find_html_pair(self, folder: Path) -> tuple[Optional[Path], Optional[Path]]:
        """Find Asset and NOHUD HTML files in a folder (ported from V1 behavior)."""
        if not folder.exists() or not folder.is_dir():
            return None, None

        asset: Optional[Path] = None
        nohud: Optional[Path] = None
        html_files = sorted(
            [
                p
                for p in folder.iterdir()
                if p.is_file() and p.suffix.lower() in {".html", ".htm"}
            ],
            key=lambda p: p.name.lower(),
        )

        for html in html_files:
            lower = html.name.lower()
            if "nohud" in lower and nohud is None:
                nohud = html
            elif "asset" in lower and asset is None:
                asset = html

        # Fallback: if names are unconventional but two or more html files exist.
        if len(html_files) >= 2:
            if asset is None:
                asset = next((h for h in html_files if h != nohud), html_files[0])
            if nohud is None:
                nohud = next((h for h in html_files if h != asset), html_files[-1])

        return asset, nohud

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def current_mode(self) -> str:
        return MODE_LABELS[self._mode_combo.currentIndex()]

    @property
    def current_mode_index(self) -> int:
        return self._mode_combo.currentIndex()

    @property
    def manual_source_type(self) -> str:
        if hasattr(self, "_manual_source_combo"):
            selected = self._manual_source_combo.currentData()
            if isinstance(selected, str) and selected.strip():
                return selected.strip().lower()
        return "mixed"

    def set_fetch_codenames(self, raw_value: str) -> None:
        """Public setter used by MainWindow (avoid direct child-input access)."""
        self.set_mode_codenames("fetch", raw_value)

    def set_mode_codenames(self, mode_key: str, raw_value: str) -> None:
        """Set codename input for a codename-driven mode."""
        if mode_key in self.inputs and "codenames" in self.inputs[mode_key]:
            self.inputs[mode_key]["codenames"].setText(raw_value)

    def set_mode_index(self, index: int) -> None:
        """Programmatically switch active mode if index is in range."""
        if 0 <= index < self._mode_combo.count():
            self._mode_combo.setCurrentIndex(index)

    def get_current_state(self) -> dict[str, object]:
        """Return a normalized snapshot of selected mode and user-provided values."""
        mode_index = self._mode_combo.currentIndex()
        mode_key = MODE_KEYS[mode_index]

        fields: dict[str, dict[str, str]] = {}
        for key, mode_inputs in self.inputs.items():
            fields[key] = {
                name: line_edit.text().strip()
                for name, line_edit in mode_inputs.items()
            }

        return {
            "mode_index": mode_index,
            "mode_label": MODE_LABELS[mode_index],
            "mode_key": mode_key,
            "manual_source_type": self.manual_source_type,
            "manual_submode": "scan",
            "target": self._resolve_target_for_state(mode_key, fields.get(mode_key, {})),
            "fields": fields,
        }
