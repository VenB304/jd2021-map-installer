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

from PyQt6.QtCore import Qt, QSize, QTimer
from PyQt6.QtGui import QIcon, QImage, QPixmap
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
    QAbstractItemView,
    QSizePolicy,
)

logger = logging.getLogger("jd2021.ui.widgets.albumcoach_dialog")


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
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Customize AlbumCoach Composite")
        self.setMinimumSize(660, 560)
        self.setModal(True)

        self._coach_paths = coach_paths
        self._n = len(coach_paths)
        self._result_image: Optional["Image.Image"] = None

        # Debounce timer for preview regeneration.
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(80)
        self._preview_timer.timeout.connect(self._update_preview)

        # Pre-load PIL images with alpha fade applied (cached for dialog lifetime).
        from PIL import Image
        self._cached_pil: dict[int, "Image.Image"] = {}
        for i, p in enumerate(coach_paths):
            try:
                raw = Image.open(p).convert("RGBA")
                self._cached_pil[i] = _apply_bottom_alpha_fade(raw)
            except Exception:
                logger.warning("Could not open coach texture: %s", p)

        self._build_ui()
        self._preview_scale_reference_overlap_pct = 0.0
        # Defer initial preview so the layout has settled and label size is valid.
        QTimer.singleShot(0, self._update_preview)

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # ---- Title ----
        title = QLabel("AlbumCoach Compositor")
        title.setStyleSheet("font-size: 15px; font-weight: bold;")
        root.addWidget(title)

        subtitle = QLabel(
            "Drag coaches to reorder horizontal placement. "
            "Adjust overlap and Z-order to control layering."
        )
        subtitle.setWordWrap(True)
        root.addWidget(subtitle)

        # ---- Live preview ----
        self._preview_label = QLabel()
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setMinimumSize(256, 256)
        self._preview_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._preview_label.setStyleSheet(
            "QLabel { background: #1a1a2e; border: 1px solid #333; border-radius: 6px; }"
        )
        root.addWidget(self._preview_label, 1)

        # ---- Overlap slider ----
        slider_row = QHBoxLayout()
        slider_label = QLabel("Overlap:")
        slider_row.addWidget(slider_label)

        self._overlap_slider = QSlider(Qt.Orientation.Horizontal)
        self._overlap_slider.setRange(0, 100)
        self._overlap_slider.setValue(25)
        self._overlap_slider.setTickInterval(5)
        self._overlap_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self._overlap_slider.valueChanged.connect(self._schedule_preview)
        slider_row.addWidget(self._overlap_slider, 1)

        self._overlap_value_label = QLabel("25 %")
        self._overlap_value_label.setMinimumWidth(42)
        slider_row.addWidget(self._overlap_value_label)
        root.addLayout(slider_row)

        # ---- Horizontal order list (drag-and-drop) + Z-order spinboxes ----
        order_label = QLabel("Horizontal Order (drag to reorder) — Z-Order (higher = in front):")
        root.addWidget(order_label)

        self._order_list = QListWidget()
        self._order_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._order_list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self._order_list.setFlow(QListWidget.Flow.LeftToRight)
        self._order_list.setWrapping(False)
        self._order_list.setIconSize(QSize(80, 80))
        self._order_list.setSpacing(6)
        self._order_list.setFixedHeight(140)
        self._order_list.model().rowsMoved.connect(self._schedule_preview)

        self._z_spinboxes: list[QSpinBox] = []
        for i in range(self._n):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, i)  # original coach index

            # Thumbnail from cached PIL
            if i in self._cached_pil:
                thumb = self._cached_pil[i].copy()
                thumb.thumbnail((80, 80))
                item.setIcon(QIcon(_pil_to_qpixmap(thumb)))

            item.setText(f"Coach {i + 1}")
            self._order_list.addItem(item)

        root.addWidget(self._order_list)

        # Z-order spinbox row
        z_row = QHBoxLayout()
        z_row.setSpacing(8)
        default_z = _default_z_order(self._n)
        for i in range(self._n):
            frame = QVBoxLayout()
            frame.setSpacing(2)
            lbl = QLabel(f"C{i + 1} Z:")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            frame.addWidget(lbl)
            spin = QSpinBox()
            spin.setRange(0, self._n * 2)
            spin.setValue(default_z[i])
            spin.setToolTip(f"Z-index for Coach {i + 1}. Higher = drawn in front.")
            spin.valueChanged.connect(self._schedule_preview)
            self._z_spinboxes.append(spin)
            frame.addWidget(spin)
            z_row.addLayout(frame)
        z_row.addStretch()
        root.addLayout(z_row)

        # ---- Button ----
        btn_row = QHBoxLayout()
        btn_row.addStretch()

        btn_apply = QPushButton("Apply")
        btn_apply.setMinimumWidth(120)
        btn_apply.setToolTip("Use the compositing settings shown in the preview.")
        btn_apply.clicked.connect(self._on_apply)
        btn_row.addWidget(btn_apply)

        root.addLayout(btn_row)

    # ------------------------------------------------------------------
    # Preview update
    # ------------------------------------------------------------------

    def _schedule_preview(self) -> None:
        self._overlap_value_label.setText(f"{self._overlap_slider.value()} %")
        self._preview_timer.start()

    def _get_horizontal_order(self) -> list[int]:
        order: list[int] = []
        for row in range(self._order_list.count()):
            item = self._order_list.item(row)
            if item is not None:
                order.append(int(item.data(Qt.ItemDataRole.UserRole)))
        return order

    def _get_z_order(self) -> list[int]:
        return [s.value() for s in self._z_spinboxes]

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
        dlg = AlbumCoachCustomizerDialog(coach_paths, parent)
        dlg.exec()

        new_behavior = "always_customize" if never_ask else ""
        return dlg.result_image, never_ask, new_behavior

