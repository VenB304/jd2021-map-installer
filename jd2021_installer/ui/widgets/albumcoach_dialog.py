"""AlbumCoach customizer dialog for JDNext multi-coach maps.

Provides a live-preview GUI where users can adjust the overlap, horizontal
placement, and Z-order of coach textures before compositing them into
the final ``cover_albumcoach`` texture.

The pure PIL compositing logic lives in ``create_composited_albumcoach()``
so it can be called from both the dialog (interactive) and the pipeline
(automatic fallback) without coupling to Qt.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QSize, QTimer, QRect
from PyQt6.QtGui import QColor, QFont, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QSpinBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
    QSizePolicy,
)

logger = logging.getLogger("jd2021.ui.widgets.albumcoach_dialog")

# ---------------------------------------------------------------------------
# Color palette (Catppuccin Mocha)
# ---------------------------------------------------------------------------
_C_BASE    = QColor("#181825")   # list / panel background
_C_SURFACE = QColor("#1e1e2e")   # dialog background
_C_ITEM    = QColor("#313244")   # default item background
_C_HOVER   = QColor("#3d3f55")   # hovered item
_C_SEL     = QColor("#45475a")   # selected item background
_C_ACCENT  = QColor("#cba6f7")   # accent / selected border
_C_TEXT    = QColor("#cdd6f4")   # primary text
_C_SUB     = QColor("#6c7086")   # subdued text / drag handle


# ---------------------------------------------------------------------------
# Bottom alpha fade (mirrors _apply_jdnext_bottom_alpha_fade_if_needed)
# ---------------------------------------------------------------------------

def _apply_bottom_alpha_fade(img: "Image.Image") -> "Image.Image":
    """Apply JDNext-style bottom alpha fade to an RGBA image in-memory.

    Replicates the fade logic from ``pipeline_workers.py`` so that the
    dialog preview matches the final installed textures.
    """
    rgba = img.copy()
    width, height = rgba.size
    if width < 4 or height < 8:
        return rgba

    # Check if fade already applied.
    alpha = rgba.getchannel("A")
    px_a = alpha.load()
    if px_a is None:
        return rgba

    def _row_mean(y: int) -> float:
        s = sum(int(px_a[x, y]) for x in range(width))
        return s / max(1, width)

    top_end = max(1, int(height * 0.35))
    fade_start_check = max(0, int(height * 0.70))
    top_mean = sum(_row_mean(y) for y in range(top_end)) / max(1, top_end)
    bottom_min = min(_row_mean(y) for y in range(fade_start_check, height))
    tail = _row_mean(height - 1)
    if tail <= 8 and bottom_min <= (top_mean * 0.35):
        return rgba  # already faded

    fade_start = max(0, int(height * 0.70))
    if fade_start >= height - 1:
        return rgba
    fade_den = max(1, (height - 1) - fade_start)
    px = rgba.load()
    if px is None:
        return rgba
    for y in range(fade_start, height):
        fade = ((height - 1) - y) / fade_den
        if fade < 0.0:
            fade = 0.0
        fade = fade ** 1.35
        for x in range(width):
            px_value = px[x, y]
            if not isinstance(px_value, tuple) or len(px_value) < 4:
                continue
            r, g, b, a = px_value
            new_a = int(round(a * fade))
            if new_a < a:
                px[x, y] = (r, g, b, new_a)
    return rgba


# ---------------------------------------------------------------------------
# Pure PIL compositing function (no Qt dependency)
# ---------------------------------------------------------------------------

def create_composited_albumcoach(
    coach_paths: list[Path],
    overlap_pct: float = 25.0,
    horizontal_order: Optional[list[int]] = None,
    z_order: Optional[list[int]] = None,
    canvas_size: tuple[int, int] = (1024, 1024),
    preloaded_images: Optional[dict[int, "Image.Image"]] = None,
    scale_reference_overlap_pct: Optional[float] = None,
) -> "Image.Image":
    """Composite coach textures into a single albumcoach image.

    Parameters
    ----------
    coach_paths:
        Ordered list of coach image file paths (index 0 = Coach 1, etc.).
    overlap_pct:
        Overlap percentage (0–100). 25 means 25 % of avg visual width is shared.
    horizontal_order:
        Indices into *coach_paths* defining left-to-right placement.
        ``None`` → natural order ``[0, 1, 2, …]``.
    z_order:
        Z-index per coach (same length as *coach_paths*). Higher = drawn later
        (in front). ``None`` → back-to-front heuristic matching original code.
    canvas_size:
        ``(W, H)`` of the output image.
    preloaded_images:
        Optional dict mapping coach index → already-loaded RGBA PIL Image.
        When provided, these are used instead of reading from *coach_paths*.
        This allows passing images with pre-applied effects (e.g. alpha fade).
    scale_reference_overlap_pct:
        If set, the output scale is computed using this overlap percentage,
        so changing *overlap_pct* only moves coaches left/right without
        changing the overall vertical scale.

    Returns
    -------
    PIL.Image.Image
        RGBA composite at *canvas_size*.
    """
    from PIL import Image

    N = len(coach_paths)
    if N == 0:
        raise ValueError("No coach paths provided.")

    W, H = canvas_size

    if horizontal_order is None:
        horizontal_order = list(range(N))
    if z_order is None:
        z_order = _default_z_order(N)

    h_pos_map = {idx: pos for pos, idx in enumerate(horizontal_order)}

    # Load, resize, and crop transparent padding from each coach.
    coach_imgs: dict[int, "Image.Image"] = {}
    total_visual_width = 0
    for idx in range(N):
        if preloaded_images and idx in preloaded_images:
            img = preloaded_images[idx].copy()
        else:
            img = Image.open(coach_paths[idx]).convert("RGBA")
        if img.size != (W, H):
            img = img.resize((W, H), Image.Resampling.LANCZOS)
        bbox = img.getbbox()
        if bbox:
            left, _upper, right, _lower = bbox
            img = img.crop((left, 0, right, H))
        coach_imgs[idx] = img
        total_visual_width += img.width

    avg_vw = total_visual_width / float(N) if N > 0 else W
    overlap_ratio = overlap_pct / 100.0
    spacing = avg_vw * (1.0 - overlap_ratio)

    def _composite_width_for_spacing(spacing_value: float) -> float:
        min_x: Optional[float] = None
        max_x: Optional[float] = None
        for idx, img in coach_imgs.items():
            h_pos = h_pos_map[idx]
            offset = (h_pos - (N - 1) / 2.0) * spacing_value
            left = offset - (img.width / 2.0)
            right = offset + (img.width / 2.0)
            min_x = left if min_x is None else min(min_x, left)
            max_x = right if max_x is None else max(max_x, right)
        if min_x is None or max_x is None:
            return 0.0
        return max_x - min_x

    # Build a draw order: iterate horizontal_order sorted by z_order (ascending)
    # so that higher z values are painted last (in front).
    draw_order = sorted(range(N), key=lambda i: z_order[i])

    # Create an oversized canvas, then crop + fit to final size.
    huge_W = int(W * N) + W
    huge_canvas = Image.new("RGBA", (huge_W, H), (0, 0, 0, 0))

    for draw_idx in draw_order:
        if draw_idx not in coach_imgs:
            continue
        c_img = coach_imgs[draw_idx]
        # Horizontal placement is by the position of this coach in
        # *horizontal_order*.
        h_pos = h_pos_map[draw_idx]
        center_x = (huge_W / 2.0) + (h_pos - (N - 1) / 2.0) * spacing
        paste_x = int(center_x - c_img.width / 2.0)
        huge_canvas.alpha_composite(c_img, (paste_x, 0))

    bbox = huge_canvas.getbbox()
    if not bbox:
        raise ValueError("Composited image is completely transparent.")

    cropped = huge_canvas.crop(bbox)

    margin_factor = 0.96
    max_w = W * margin_factor
    max_h = H * margin_factor
    if scale_reference_overlap_pct is None:
        scale = min(max_w / float(cropped.width), max_h / float(cropped.height))
    else:
        ref_ratio = scale_reference_overlap_pct / 100.0
        ref_spacing = avg_vw * (1.0 - ref_ratio)
        ref_width = _composite_width_for_spacing(ref_spacing)
        if ref_width <= 0:
            ref_width = float(cropped.width)
        scale = min(max_w / float(ref_width), max_h / float(cropped.height))
    new_w = int(cropped.width * scale)
    new_h = int(cropped.height * scale)
    resized = cropped.resize((new_w, new_h), Image.Resampling.LANCZOS)

    final = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    final.paste(resized, ((W - new_w) // 2, (H - new_h) // 2))
    return final


def _default_z_order(n: int) -> list[int]:
    """Replicate the hardcoded Z-order heuristic from the original pipeline.

    Returns a list where each element is the z-index for that coach index.
    Higher numbers are drawn in front.
    """
    if n == 1:
        return [0]
    if n == 2:
        # draw_order was [1, 0] → P2 behind, P1 in front
        return [1, 0]
    if n == 3:
        # draw_order was [0, 2, 1] → P1 behind, P3, then P2 in front
        return [0, 2, 1]
    if n == 4:
        # draw_order was [0, 3, 2, 1] → P1 behind, P4, P3, P2 in front
        return [0, 3, 2, 1]

    # General: outside-in, center on top
    z = [0] * n
    left_idx, right_idx = 0, n - 1
    draw_order: list[int] = []
    while left_idx <= right_idx:
        if left_idx == right_idx:
            draw_order.append(left_idx)
        else:
            draw_order.extend([left_idx, right_idx])
        left_idx += 1
        right_idx -= 1
    draw_order.reverse()
    for rank, idx in enumerate(draw_order):
        z[idx] = rank
    return z


# ---------------------------------------------------------------------------
# PIL Image ↔ QPixmap helpers
# ---------------------------------------------------------------------------

def _pil_to_qpixmap(pil_img: "Image.Image") -> QPixmap:
    """Convert a PIL RGBA image to a QPixmap."""
    data = pil_img.tobytes("raw", "RGBA")
    qimg = QImage(data, pil_img.width, pil_img.height, QImage.Format.Format_RGBA8888)
    # QImage doesn't own the data, so we must keep it alive → copy.
    return QPixmap.fromImage(qimg.copy())


# ---------------------------------------------------------------------------
# CoachItemDelegate — custom QPainter delegate for both list widgets
# ---------------------------------------------------------------------------

class CoachItemDelegate(QStyledItemDelegate):
    """Paint coach items for both the Z-order (vertical) and placement (horizontal) lists.

    Parameters
    ----------
    orientation:
        ``"vertical"``  → thumbnail left, label right, drag-handle glyph.
        ``"horizontal"`` → thumbnail top, label bottom.
    """

    THUMB_V = 52   # thumbnail px — vertical mode
    THUMB_H = 76   # thumbnail px — horizontal mode
    RADIUS  = 7    # rounded-corner radius

    def __init__(self, orientation: str = "vertical", theme: str = "dark", parent=None) -> None:
        super().__init__(parent)
        self._orientation = orientation
        # Colour palette — varies by host-app theme.
        if theme == "dark":
            self._c = {
                "item":   QColor("#313244"),
                "hover":  QColor("#3d3f55"),
                "sel":    QColor("#45475a"),
                "accent": QColor("#cba6f7"),
                "text":   QColor("#cdd6f4"),
                "sub":    QColor("#6c7086"),
            }
        else:  # light
            self._c = {
                "item":   QColor("#eef3fb"),
                "hover":  QColor("#e3ebf8"),
                "sel":    QColor("#d9e7ff"),
                "accent": QColor("#3f7cff"),
                "text":   QColor("#1f2a38"),
                "sub":    QColor("#8896a9"),
            }

    # ------------------------------------------------------------------
    # Size hint
    # ------------------------------------------------------------------

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # type: ignore[override]
        if self._orientation == "vertical":
            return QSize(200, 68)
        return QSize(108, 140)

    # ------------------------------------------------------------------
    # Paint
    # ------------------------------------------------------------------

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:  # type: ignore[override]
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        is_selected = bool(option.state & QStyle.StateFlag.State_Selected)
        is_hovered  = bool(option.state & QStyle.StateFlag.State_MouseOver)

        # Item rect with small margin so items don't touch
        rect = option.rect.adjusted(3, 2, -3, -2)

        # --- Background ---
        if is_selected:
            bg_color     = self._c["sel"]
            border_color = self._c["accent"]
        elif is_hovered:
            bg_color     = self._c["hover"]
            border_color = QColor("transparent")
        else:
            bg_color     = self._c["item"]
            border_color = QColor("transparent")

        path = QPainterPath()
        path.addRoundedRect(
            float(rect.x()), float(rect.y()),
            float(rect.width()), float(rect.height()),
            self.RADIUS, self.RADIUS,
        )
        painter.fillPath(path, bg_color)
        if is_selected:
            painter.setPen(QPen(border_color, 1.5))
            painter.drawPath(path)

        # --- Retrieve item data ---
        thumb: QPixmap | None = index.data(Qt.ItemDataRole.DecorationRole)
        text:  str            = index.data(Qt.ItemDataRole.DisplayRole) or ""

        if self._orientation == "vertical":
            self._paint_vertical(painter, rect, thumb, text)
        else:
            self._paint_horizontal(painter, rect, thumb, text)

        painter.restore()

    # ------------------------------------------------------------------
    # Vertical mode helpers
    # ------------------------------------------------------------------

    def _paint_vertical(
        self,
        painter: QPainter,
        rect: QRect,
        thumb: "QPixmap | None",
        text: str,
    ) -> None:
        ts = self.THUMB_V
        thumb_rect = QRect(
            rect.left() + 7,
            rect.top() + (rect.height() - ts) // 2,
            ts, ts,
        )
        self._draw_thumb(painter, thumb_rect, thumb)

        # Coach name
        text_x = thumb_rect.right() + 10
        handle_space = 26
        text_rect = QRect(
            text_x,
            rect.top(),
            rect.right() - text_x - handle_space,
            rect.height(),
        )
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(self._c["text"])
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)

        # Drag handle — three horizontal lines on right edge
        hx = rect.right() - 18
        cy = rect.center().y()
        painter.setPen(QPen(self._c["sub"], 1.5))
        for dy in (-5, 0, 5):
            painter.drawLine(hx, cy + dy, hx + 12, cy + dy)

    # ------------------------------------------------------------------
    # Horizontal mode helpers
    # ------------------------------------------------------------------

    def _paint_horizontal(
        self,
        painter: QPainter,
        rect: QRect,
        thumb: "QPixmap | None",
        text: str,
    ) -> None:
        ts = self.THUMB_H
        thumb_x = rect.left() + (rect.width() - ts) // 2
        thumb_rect = QRect(thumb_x, rect.top() + 8, ts, ts)
        self._draw_thumb(painter, thumb_rect, thumb)

        # Coach name below thumbnail
        label_y = thumb_rect.bottom() + 5
        label_rect = QRect(
            rect.left(),
            label_y,
            rect.width(),
            18,
        )
        font = QFont()
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(self._c["text"])
        painter.drawText(
            label_rect,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
            text,
        )

        # Drag handle — three horizontal lines below label
        handle_y = label_y + 20
        hx = rect.center().x() - 8
        painter.setPen(QPen(self._c["sub"], 1.5))
        for dy in (0, 5, 10):
            painter.drawLine(hx, handle_y + dy, hx + 16, handle_y + dy)

    # ------------------------------------------------------------------
    # Shared thumbnail painter
    # ------------------------------------------------------------------

    def _draw_thumb(self, painter: QPainter, rect: QRect, thumb: "QPixmap | None") -> None:
        if thumb and not thumb.isNull():
            scaled = thumb.scaled(
                rect.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            # Centre the scaled thumb inside rect
            dx = (rect.width()  - scaled.width())  // 2
            dy = (rect.height() - scaled.height()) // 2
            painter.drawPixmap(rect.left() + dx, rect.top() + dy, scaled)
        else:
            # Placeholder — draw a subtle rounded rect
            painter.setPen(QPen(self._c["sub"], 1))
            painter.setBrush(QColor("transparent"))
            painter.drawRoundedRect(rect, 4, 4)


# ---------------------------------------------------------------------------
# AlbumCoachAskDialog — lightweight "do you want to customize?" prompt
# ---------------------------------------------------------------------------

class AlbumCoachAskDialog(QDialog):
    """Small dialog asking whether to customize or use the default compositing."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AlbumCoach")
        self.setModal(True)
        self.setFixedWidth(420)

        self._choice: str = "default"  # "default" | "customize"
        self._never_ask = False

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        msg = QLabel(
            "This JDNext map has multiple coaches but no albumcoach texture.\n"
            "Would you like to customize how the coaches are composited?"
        )
        msg.setWordWrap(True)
        root.addWidget(msg)

        self._cb_never_ask = QCheckBox("Don't ask again")
        self._cb_never_ask.setToolTip(
            "Remember your choice for all future maps.\n"
            "You can change this later in Settings → AlbumCoach compositing."
        )
        root.addWidget(self._cb_never_ask)

        btn_row = QHBoxLayout()
        btn_row.addStretch()

        btn_default = QPushButton("Use Default")
        btn_default.setMinimumWidth(110)
        btn_default.clicked.connect(self._on_default)
        btn_row.addWidget(btn_default)

        btn_customize = QPushButton("Customize")
        btn_customize.setMinimumWidth(110)
        btn_customize.clicked.connect(self._on_customize)
        btn_row.addWidget(btn_customize)

        root.addLayout(btn_row)

    def _on_default(self) -> None:
        self._choice = "default"
        self._never_ask = self._cb_never_ask.isChecked()
        self.accept()

    def _on_customize(self) -> None:
        self._choice = "customize"
        self._never_ask = self._cb_never_ask.isChecked()
        self.accept()

    @property
    def choice(self) -> str:
        return self._choice

    @property
    def never_ask_again(self) -> bool:
        return self._never_ask


# ---------------------------------------------------------------------------
# AlbumCoachCustomizerDialog — full editor with live preview
# ---------------------------------------------------------------------------

class AlbumCoachCustomizerDialog(QDialog):
    """Interactive dialog for adjusting albumcoach coach compositing.

    Shows a live preview updated as the user adjusts the overlap slider,
    reorders coaches, or modifies Z-order values.  Has a single "Apply"
    button — no default/cancel options.

    Parameters
    ----------
    coach_paths:
        Ordered list of coach texture files (Coach 1 first).
    parent:
        Parent widget (typically the main window).
    """

    def __init__(
        self,
        coach_paths: list[Path],
        parent: Optional[QWidget] = None,
        theme: str = "dark",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Customize AlbumCoach Composite")
        self.setMinimumSize(820, 620)
        self.setModal(True)

        self._coach_paths = coach_paths
        self._n = len(coach_paths)
        self._theme = theme
        self._result_image: Optional["Image.Image"] = None

        # Debounce timer for preview regeneration.
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(80)
        self._preview_timer.timeout.connect(self._update_preview)

        # Pre-load PIL images with alpha fade applied (cached for dialog lifetime).
        from PIL import Image
        self._cached_pil: dict[int, "Image.Image"] = {}
        # Also build QPixmap thumbnails up-front for the delegates.
        self._thumb_pixmaps: dict[int, QPixmap] = {}
        for i, p in enumerate(coach_paths):
            try:
                raw = Image.open(p).convert("RGBA")
                faded = _apply_bottom_alpha_fade(raw)
                self._cached_pil[i] = faded
                self._thumb_pixmaps[i] = _pil_to_qpixmap(faded)
            except Exception:
                logger.warning("Could not open coach texture: %s", p)

        self._build_ui()
        self._apply_styles()
        self._populate_lists()
        self._preview_scale_reference_overlap_pct = 0.0
        # Defer initial preview so the layout has settled and label size is valid.
        QTimer.singleShot(0, self._update_preview)

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        # ---- Header ----
        hdr = QHBoxLayout()
        title = QLabel("AlbumCoach Compositor")
        title.setObjectName("dialogTitle")
        hdr.addWidget(title)
        hdr.addStretch()
        root.addLayout(hdr)

        subtitle = QLabel(
            "Drag rows in the Z-Order Stack to set layering (top → front). "
            "Drag cards in X-Axis Placement to set left‑to‑right position."
        )
        subtitle.setObjectName("dialogSubtitle")
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        # ---- Content row: left panel + right preview ----
        content_row = QHBoxLayout()
        content_row.setSpacing(12)

        # -- Left panel: Z-order stack --
        left_panel = QFrame()
        left_panel.setObjectName("leftPanel")
        left_panel.setFixedWidth(230)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(8, 10, 8, 10)
        left_layout.setSpacing(4)

        z_section = QLabel("■  Z-ORDER STACK")
        z_section.setObjectName("sectionLabel")
        left_layout.addWidget(z_section)

        z_hint = QLabel("top → rendered in front")
        z_hint.setObjectName("hintLabel")
        left_layout.addWidget(z_hint)

        self._z_list = QListWidget()
        self._z_list.setObjectName("zList")
        self._z_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._z_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._z_list.setItemDelegate(CoachItemDelegate("vertical", self._theme, self._z_list))
        self._z_list.setSpacing(2)
        self._z_list.setMouseTracking(True)
        self._z_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._z_list.model().rowsMoved.connect(self._schedule_preview)
        left_layout.addWidget(self._z_list, 1)

        content_row.addWidget(left_panel)

        # -- Right panel: preview + overlap slider --
        right_layout = QVBoxLayout()
        right_layout.setSpacing(8)

        self._preview_label = QLabel()
        self._preview_label.setObjectName("previewLabel")
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setMinimumSize(340, 340)
        self._preview_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        right_layout.addWidget(self._preview_label, 1)

        # Overlap slider
        slider_row = QHBoxLayout()
        slider_row.setSpacing(8)
        slider_lbl = QLabel("Overlap")
        slider_lbl.setObjectName("sliderLabel")
        slider_row.addWidget(slider_lbl)

        self._overlap_slider = QSlider(Qt.Orientation.Horizontal)
        self._overlap_slider.setRange(0, 100)
        self._overlap_slider.setValue(25)
        self._overlap_slider.setTickInterval(10)
        self._overlap_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._overlap_slider.valueChanged.connect(self._schedule_preview)
        slider_row.addWidget(self._overlap_slider, 1)

        self._overlap_value_label = QLabel("25 %")
        self._overlap_value_label.setObjectName("overlapValueLabel")
        self._overlap_value_label.setMinimumWidth(44)
        self._overlap_value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        slider_row.addWidget(self._overlap_value_label)
        right_layout.addLayout(slider_row)

        content_row.addLayout(right_layout, 1)
        root.addLayout(content_row, 1)

        # ---- Horizontal placement strip ----
        h_section = QLabel("■  X-AXIS PLACEMENT  ←  →")
        h_section.setObjectName("sectionLabel")
        root.addWidget(h_section)

        self._h_list = QListWidget()
        self._h_list.setObjectName("hList")
        self._h_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._h_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._h_list.setFlow(QListWidget.Flow.LeftToRight)
        self._h_list.setWrapping(False)
        self._h_list.setItemDelegate(CoachItemDelegate("horizontal", self._theme, self._h_list))
        self._h_list.setSpacing(4)
        self._h_list.setFixedHeight(160)
        self._h_list.setMouseTracking(True)
        self._h_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._h_list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Prevent wheel-scrolling entirely — all items are visible, no scroll needed.
        self._h_list.wheelEvent = lambda e: e.accept()
        self._h_list.model().rowsMoved.connect(self._schedule_preview)

        h_row = QHBoxLayout()
        h_row.addStretch()
        h_row.addWidget(self._h_list)
        h_row.addStretch()
        root.addLayout(h_row)

        # Explicitly paint the viewport of each list so the app-level
        # QWidget { background } rule cannot bleed through.
        vp_bg = "background: #181825;" if self._theme == "dark" else "background: #f4f7fb;"
        self._z_list.viewport().setStyleSheet(vp_bg)
        self._h_list.viewport().setStyleSheet(vp_bg)

        # ---- Button row ----
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_apply = QPushButton("Apply")
        btn_apply.setObjectName("applyBtn")
        btn_apply.setMinimumWidth(120)
        btn_apply.setToolTip("Use the compositing settings shown in the preview.")
        btn_apply.clicked.connect(self._on_apply)
        btn_row.addWidget(btn_apply)
        root.addLayout(btn_row)

    # ------------------------------------------------------------------
    # List population
    # ------------------------------------------------------------------

    def _populate_lists(self) -> None:
        """Populate both lists from cached thumbnails.

        Z-list is seeded in default Z-order (highest Z first = top).
        H-list is seeded in natural order (Coach 1 leftmost).
        """
        default_z = _default_z_order(self._n)
        # Sort descending by default z-value: highest z (front) at top
        coaches_by_z_desc = sorted(range(self._n), key=lambda i: default_z[i], reverse=True)

        for coach_idx in coaches_by_z_desc:
            self._z_list.addItem(self._make_item(coach_idx))

        for coach_idx in range(self._n):
            self._h_list.addItem(self._make_item(coach_idx))

        # Size the horizontal list to fit its items exactly so the stretch
        # wrappers centre it.  Each item is 108px wide + 4px spacing.
        item_w = 108 + 4  # sizeHint width + spacing
        self._h_list.setFixedWidth(self._n * item_w + 16)  # +16 for margins

    def _make_item(self, coach_idx: int) -> QListWidgetItem:
        """Build a QListWidgetItem for *coach_idx* with thumbnail + label data."""
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, coach_idx)
        item.setText(f"Coach {coach_idx + 1}")
        if coach_idx in self._thumb_pixmaps:
            item.setData(Qt.ItemDataRole.DecorationRole, self._thumb_pixmaps[coach_idx])
        return item

    # ------------------------------------------------------------------
    # Styles
    # ------------------------------------------------------------------

    def _apply_styles(self) -> None:
        if self._theme == "dark":
            self._apply_dark_styles()
        else:
            self._apply_light_styles()

    def _apply_dark_styles(self) -> None:
        self.setStyleSheet("""
            /* Baseline reset: prevent app-level light-mode rules from bleeding in. */
            QWidget {
                background: transparent;
                color: #cdd6f4;
            }

            QDialog {
                background: #1e1e2e;
                color: #cdd6f4;
                font-family: 'Segoe UI', 'Inter', sans-serif;
            }

            /* Header */
            QLabel#dialogTitle {
                font-size: 16px;
                font-weight: 700;
                color: #cdd6f4;
            }
            QLabel#dialogSubtitle {
                font-size: 11px;
                color: #7f849c;
            }

            /* Section labels */
            QLabel#sectionLabel {
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 1.2px;
                color: #bac2de;
                padding: 2px 0px 4px 0px;
            }
            QLabel#hintLabel {
                font-size: 9px;
                color: #585b70;
                padding-bottom: 4px;
            }

            /* Left panel */
            QFrame#leftPanel {
                background: #181825;
                border-right: 1px solid #313244;
                border-radius: 10px;
            }

            /* Both list widgets */
            QListWidget#zList, QListWidget#hList {
                background: #181825;
                border: 1px solid #313244;
                border-radius: 8px;
                outline: none;
            }
            QListWidget#zList::item, QListWidget#hList::item {
                background: transparent;
                border: none;
            }
            /* The delegate paints hover/selected — suppress Qt's default highlight */
            QListWidget#zList::item:selected,
            QListWidget#hList::item:selected {
                background: transparent;
                border: none;
            }

            /* Preview area */
            QLabel#previewLabel {
                background: #11111b;
                border: 1px solid #313244;
                border-radius: 8px;
            }

            /* Overlap controls */
            QLabel#sliderLabel {
                font-size: 11px;
                color: #bac2de;
                min-width: 52px;
            }
            QLabel#overlapValueLabel {
                font-size: 11px;
                font-weight: 700;
                color: #cba6f7;
                min-width: 44px;
            }
            QSlider::groove:horizontal {
                height: 4px;
                background: #313244;
                border-radius: 2px;
            }
            QSlider::sub-page:horizontal {
                background: #cba6f7;
                border-radius: 2px;
            }
            QSlider::handle:horizontal {
                background: #cba6f7;
                border: 2px solid #1e1e2e;
                width: 14px;
                height: 14px;
                margin: -5px 0;
                border-radius: 7px;
            }
            QSlider::handle:horizontal:hover {
                background: #d4b3ff;
            }

            /* Apply button */
            QPushButton#applyBtn {
                background: #cba6f7;
                color: #1e1e2e;
                border: none;
                border-radius: 6px;
                padding: 7px 24px;
                font-size: 12px;
                font-weight: 700;
            }
            QPushButton#applyBtn:hover {
                background: #d4b3ff;
            }
            QPushButton#applyBtn:pressed {
                background: #b794e8;
            }
        """)

    def _apply_light_styles(self) -> None:
        """Minimal QSS for light mode — lets the app stylesheet handle standard widgets."""
        self.setStyleSheet("""
            /* Header */
            QLabel#dialogTitle {
                font-size: 16px;
                font-weight: 700;
            }
            QLabel#dialogSubtitle {
                font-size: 11px;
            }

            /* Section labels */
            QLabel#sectionLabel {
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 1.2px;
                color: #3a4b62;
                padding: 2px 0px 4px 0px;
            }
            QLabel#hintLabel {
                font-size: 9px;
                color: #7a8a9e;
                padding-bottom: 4px;
            }

            /* Left panel */
            QFrame#leftPanel {
                background: #f0f4fa;
                border: 1px solid #d2dce8;
                border-radius: 10px;
            }

            /* List widgets */
            QListWidget#zList, QListWidget#hList {
                background: #f4f7fb;
                border: 1px solid #c5d1e4;
                border-radius: 8px;
                outline: none;
            }
            QListWidget#zList::item, QListWidget#hList::item {
                background: transparent;
                border: none;
            }
            QListWidget#zList::item:selected,
            QListWidget#hList::item:selected {
                background: transparent;
                border: none;
            }

            /* Preview area */
            QLabel#previewLabel {
                background: #e9eff8;
                border: 1px solid #c5d1e4;
                border-radius: 8px;
            }

            /* Overlap value readout */
            QLabel#overlapValueLabel {
                font-weight: 700;
                color: #3f7cff;
            }

            /* Apply button */
            QPushButton#applyBtn {
                background: #3f7cff;
                color: #ffffff;
                border: none;
                border-radius: 6px;
                padding: 7px 24px;
                font-size: 12px;
                font-weight: 700;
            }
            QPushButton#applyBtn:hover {
                background: #5289ff;
            }
            QPushButton#applyBtn:pressed {
                background: #2b63de;
            }
        """)

    # ------------------------------------------------------------------
    # Preview update
    # ------------------------------------------------------------------

    def _schedule_preview(self) -> None:
        self._overlap_value_label.setText(f"{self._overlap_slider.value()} %")
        self._preview_timer.start()

    def _get_horizontal_order(self) -> list[int]:
        return [
            int(self._h_list.item(r).data(Qt.ItemDataRole.UserRole))
            for r in range(self._h_list.count())
            if self._h_list.item(r) is not None
        ]

    def _get_z_order(self) -> list[int]:
        """Convert Z-list row positions to a z_order list for the compositor.

        Row 0 (top) maps to the highest Z-value (rendered in front).
        """
        n = self._z_list.count()
        z_order = [0] * self._n
        for row in range(n):
            item = self._z_list.item(row)
            if item is not None:
                coach_idx = int(item.data(Qt.ItemDataRole.UserRole))
                z_order[coach_idx] = n - 1 - row  # top row → highest z
        return z_order

    def _update_preview(self) -> None:
        try:
            img = create_composited_albumcoach(
                self._coach_paths,
                overlap_pct=float(self._overlap_slider.value()),
                horizontal_order=self._get_horizontal_order(),
                z_order=self._get_z_order(),
                preloaded_images=self._cached_pil,
                scale_reference_overlap_pct=self._preview_scale_reference_overlap_pct,
            )

            # Scale pixmap to fit preview label.
            pm = _pil_to_qpixmap(img)
            # Use the label's current size; fall back to a reasonable
            # default when the layout hasn't settled yet.
            label_w = max(self._preview_label.width(), 400)
            label_h = max(self._preview_label.height(), 400)
            scaled = pm.scaled(
                label_w,
                label_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._preview_label.setPixmap(scaled)
        except Exception as exc:
            logger.debug("Preview update failed: %s", exc)
            self._preview_label.setText(f"Preview error:\n{exc}")

    def _compose_result(self) -> None:
        try:
            self._result_image = create_composited_albumcoach(
                self._coach_paths,
                overlap_pct=float(self._overlap_slider.value()),
                horizontal_order=self._get_horizontal_order(),
                z_order=self._get_z_order(),
                preloaded_images=self._cached_pil,
            )
        except Exception as exc:
            logger.debug("Result compositing failed: %s", exc)
            self._preview_label.setText(f"Preview error:\n{exc}")
            self._result_image = None

    # ------------------------------------------------------------------
    # Button handler
    # ------------------------------------------------------------------

    def _on_apply(self) -> None:
        # Create the final image with default scaling behavior.
        self._compose_result()
        if self._result_image is not None:
            self.accept()

    @property
    def result_image(self) -> Optional["Image.Image"]:
        """PIL Image produced by the compositor."""
        return self._result_image

    # ------------------------------------------------------------------
    # Static convenience — orchestrates the two-step flow
    # ------------------------------------------------------------------

    @staticmethod
    def prompt(
        coach_paths: list[Path],
        parent: Optional[QWidget] = None,
        theme: str = "dark",
    ) -> tuple[Optional["Image.Image"], bool, str]:
        """Orchestrate the two-step albumcoach customization flow.

        Step 1: ``AlbumCoachAskDialog`` — "Use Default" vs "Customize"
                with an optional "Don't ask again" checkbox.
        Step 2: If "Customize" was chosen, open the full
                ``AlbumCoachCustomizerDialog``.

        Returns
        -------
        tuple[Optional[Image], bool, str]
            ``(result_image, never_ask_again, new_behavior)``.
            *result_image* is ``None`` when using defaults.
            *new_behavior* is ``""`` unless *never_ask_again* is True,
            in which case it's ``"always_default"`` or ``"always_customize"``.
        """
        ask = AlbumCoachAskDialog(parent)
        ask.exec()

        never_ask = ask.never_ask_again
        choice = ask.choice  # "default" | "customize"

        if choice == "default":
            new_behavior = "always_default" if never_ask else ""
            return None, never_ask, new_behavior

        # User chose "Customize" — open the full editor.
        dlg = AlbumCoachCustomizerDialog(coach_paths, parent, theme=theme)
        dlg.exec()

        new_behavior = "always_customize" if never_ask else ""
        return dlg.result_image, never_ask, new_behavior

