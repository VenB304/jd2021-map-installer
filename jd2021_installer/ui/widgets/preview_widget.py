"""Embedded FFmpeg preview widget — PyQt6 port of V1 ``gui_preview.py``.

Displays raw RGB24 video frames from ``ffmpeg`` piped into a ``QLabel``
at ~24 FPS, with audio played via a separate ``ffplay`` subprocess.
All heavy I/O runs in ``QThread``-based workers so the Qt event loop
is never blocked.

Layout::

    ┌──────────────────────────────────────────┐
    │              Video Canvas                │
    │            (480 × 270, black)            │
    ├──────────────────────────────────────────┤
    │  0:00  ═══════ seek ═══════  3:45       │
    │        [−5s]  [▶/⏸]  [+5s]  [⏹]        │
    └──────────────────────────────────────────┘
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QObject, QThread, QTimer, Qt, QUrl, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QImage, QPixmap
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

logger = logging.getLogger("jd2021.ui.widgets.preview")

PREVIEW_FPS = 24
PREVIEW_PROXY_WIDTH = 960
_CFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class _AspectRatioLabel(QLabel):
    """A QLabel that automatically scales its pixmap while maintaining aspect ratio on resize."""

    clicked = pyqtSignal()

    def __init__(self, text: str = "") -> None:
        super().__init__(text)
        self._base_pixmap = QPixmap()

    def setPixmap(self, pixmap: QPixmap) -> None:
        self._base_pixmap = pixmap
        super().setPixmap(self._scaled_pixmap())

    def resizeEvent(self, event) -> None:
        if not self._base_pixmap.isNull():
            super().setPixmap(self._scaled_pixmap())
        super().resizeEvent(event)

    def _scaled_pixmap(self) -> QPixmap:
        if self._base_pixmap.isNull():
            return QPixmap()
        return self._base_pixmap.scaled(
            self.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class _AspectRatioViewport(QWidget):
    """Container that forces a fixed aspect ratio for its child widget."""

    def __init__(self, child: QWidget, ratio_w: int = 16, ratio_h: int = 9) -> None:
        super().__init__()
        self._child = child
        self._ratio_w = max(1, int(ratio_w))
        self._ratio_h = max(1, int(ratio_h))
        self._child.setParent(self)
        self._child.show()
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def resizeEvent(self, event) -> None:
        rect = self.contentsRect()
        max_w = max(1, rect.width())
        max_h = max(1, rect.height())

        target_w = min(max_w, int(max_h * self._ratio_w / self._ratio_h))
        target_h = min(max_h, int(target_w * self._ratio_h / self._ratio_w))

        x = rect.x() + (max_w - target_w) // 2
        y = rect.y() + (max_h - target_h) // 2
        self._child.setGeometry(x, y, target_w, target_h)
        super().resizeEvent(event)


# ---------------------------------------------------------------------------
# Frame reader thread (runs ffmpeg, emits QPixmap frames)
# ---------------------------------------------------------------------------

class _FrameReaderWorker(QObject):
    """Reads raw RGB24 frames from ffmpeg stdout in a background thread.

    Emits:
        frame_ready(QPixmap): a new video frame for display
        position_updated(float): current playback position in seconds
        playback_ended(): the ffmpeg process reached EOF
    """

    frame_ready = pyqtSignal(QImage)
    position_updated = pyqtSignal(float)
    playback_ended = pyqtSignal()

    def __init__(
        self,
        ffmpeg_cmd: list[str],
        width: int,
        height: int,
        start_position: float = 0.0,
        fps: float = PREVIEW_FPS,
    ) -> None:
        super().__init__()
        self._ffmpeg_cmd = ffmpeg_cmd
        self._width = width
        self._height = height
        self._start_position = start_position
        self._fps = max(1.0, float(fps))
        self._stop_flag = threading.Event()
        self._ticking_event = threading.Event()
        self._ffmpeg: Optional[subprocess.Popen] = None

    # -- public ------------------------------------------------------------

    def request_stop(self) -> None:
        self._stop_flag.set()
<<<<<<< Updated upstream
=======
        self._ticking_event.set()  # Unblock if waiting
        if self._ffmpeg is not None:
            try:
                self._ffmpeg.kill()
            except Exception as e:
                logger.debug("Error killing ffmpeg on stop: %s", e)

    def start_ticking(self, advance_s: float = 0.0) -> None:
        self._advance_s = advance_s
        self._ticking_event.set()
        self._reset_clock_requested = True
>>>>>>> Stashed changes

    @pyqtSlot()
    def run(self) -> None:
        """Main loop — launched via ``thread.started.connect(worker.run)``."""
        frame_size = self._width * self._height * 3
        frames_read = 0
        wall_start: float = 0.0
        position = self._start_position

        try:
            self._ffmpeg = subprocess.Popen(
                self._ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=_CFLAGS,
            )

            wall_start = 0.0

            while not self._stop_flag.is_set():
                data = b""
                ffmpeg_stdout = self._ffmpeg.stdout if self._ffmpeg else None
                if ffmpeg_stdout is None:
                    self.playback_ended.emit()
                    return
                while len(data) < frame_size:
                    chunk = ffmpeg_stdout.read(frame_size - len(data))
                    if not chunk:
                        # EOF — video over
                        self.playback_ended.emit()
                        return
                    data += chunk

                frames_read += 1

                if self._stop_flag.is_set():
                    return

                # Convert raw bytes to QImage
                q_img = QImage(
                    data,
                    self._width,
                    self._height,
                    self._width * 3,
                    QImage.Format.Format_RGB888,
                )
<<<<<<< Updated upstream
                pixmap = QPixmap.fromImage(q_img.copy())  # .copy() — data outlives loop
                self.frame_ready.emit(pixmap)
=======

                if frames_read == 1:
                    # Signal readiness by emitting the first frame, then wait for the handshake
                    self.frame_ready.emit(q_img.copy())
                    self._ticking_event.wait()
                    advance = getattr(self, "_advance_s", 0.0)
                    wall_start = time.time() - advance
                    self._reset_clock_requested = False
                elif getattr(self, "_reset_clock_requested", False):
                    self._ticking_event.wait()
                    advance = getattr(self, "_advance_s", 0.0)
                    wall_start = time.time() - advance
                    self._reset_clock_requested = False
>>>>>>> Stashed changes

                if wall_start > 0:
                    now = time.time()
                    expected_pos = self._start_position + (now - wall_start)
                    current_frame_time = self._start_position + (frames_read - 1) / float(self._fps)
                    
                    # Catch-up logic: if we are more than 2 frames behind the wall clock,
                    # skip rendering this frame and read the next one from FFmpeg immediately.
                    if expected_pos > (current_frame_time + (2.0 / self._fps)):
                        continue
                        
                    position = current_frame_time
                else:
                    position = self._start_position

                # Emit subsequent frames normally
                if frames_read > 1:
                    self.frame_ready.emit(q_img.copy())
                self.position_updated.emit(position)

                # Keep preview rendering near target FPS without relying on ffmpeg -re.
                if wall_start > 0:
                    expected = frames_read / float(self._fps)
                    now = time.time()
                    remaining = (wall_start + expected) - now
                    if remaining > 0:
                        time.sleep(remaining)

        except Exception as exc:
            logger.debug("Frame reader ended: %s", exc)
        finally:
            self._cleanup()

    # -- internal ----------------------------------------------------------

    def _cleanup(self) -> None:
        if self._ffmpeg is not None:
            try:
                if self._ffmpeg.stdout:
                    self._ffmpeg.stdout.close()
            except OSError:
                pass
            if self._ffmpeg.poll() is None:
                try:
                    self._ffmpeg.kill()
                    self._ffmpeg.wait(timeout=3)
                except OSError:
                    pass
        self._ffmpeg = None


# ---------------------------------------------------------------------------
# Preview widget (public API)
# ---------------------------------------------------------------------------

class PreviewWidget(QWidget):
    """Embedded video-preview widget with playback controls.

    Signals:
        preview_started(): emitted when playback begins.
        preview_stopped(): emitted when playback ends.
    """

    preview_started = pyqtSignal()
    preview_stopped = pyqtSignal()
    audio_unavailable = pyqtSignal()
    position_changed = pyqtSignal(float)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)

        # State
        self._playing = False
        self._position: float = 0.0
        self._duration: float = 120.0  # fallback

<<<<<<< Updated upstream
=======
        # Media Player
        self._player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.positionChanged.connect(self._on_player_position_changed)
        self._player.mediaStatusChanged.connect(self._on_media_status_changed)
        
        self._audio_output.setVolume(1.0)
        self._aud_delay_ms = 0

>>>>>>> Stashed changes
        # Subprocess tracking
        self._worker: Optional[_FrameReaderWorker] = None
        self._thread: Optional[QThread] = None

        # Stashed launch params (for resume / seek)
        self._video_path: Optional[str] = None
        self._audio_path: Optional[str] = None
        self._v_override: float = 0.0
        self._a_offset: float = 0.0
        self._resume_after_seek: bool = False
        self._loop_start: float = 0.0
        self._loop_end: float = 0.0
        self._stop_requested: bool = False
        self._ffmpeg_path: str = "ffmpeg"
        self._ffprobe_path: str = "ffprobe"
        self._ffmpeg_hwaccel: str = "auto"
        self._preview_video_mode: str = "proxy_low"
        
        # Caching
        self._last_probed_video: Optional[str] = None
        self._last_resolved_audio_src: Optional[str] = None
        self._last_resolved_audio_result: Optional[str] = None
        self._raw_video_dur_cache: dict[str, float] = {}
        self._raw_audio_dur_cache: dict[str, float] = {}
        self._audio_proxy_cache: dict[str, str] = {}
        self._preview_fps_default: float = float(PREVIEW_FPS)
        self._playback_fps: float = float(PREVIEW_FPS)
        self._accurate_seek: bool = False
        self._preview_proxy_cache: dict[str, str] = {}
        self._ended_naturally: bool = False
        self._dying_threads: list[QThread] = []

        self._build_ui()

    # ==================================================================
    # UI CONSTRUCTION
    # ==================================================================

    def _set_play_button_icon(self, playing: bool) -> None:
        tooltip = "Pause Preview" if playing else "Play Preview"
        self._btn_play.setText("Stop" if playing else "Play")
        self._btn_play.setToolTip(tooltip)

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)
        root.setSpacing(4)
        self.setObjectName("previewWidget")

        # -- Video canvas ---------------------------------------------------
        self._canvas = _AspectRatioLabel("No Preview")
        self._canvas.setObjectName("previewCanvas")
        self._canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._canvas.setMinimumSize(480, 270)
        self._canvas.setToolTip("Video preview area for sync checking. Click here to play or pause the video.")
        self._canvas.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding,
        )
        self._canvas.clicked.connect(self._toggle_playback)

        self._canvas_host = _AspectRatioViewport(self._canvas, ratio_w=16, ratio_h=9)
        self._canvas_host.setMinimumSize(480, 270)
        root.addWidget(self._canvas_host, stretch=1)

        # -- Seek bar -------------------------------------------------------
        seek_row = QHBoxLayout()
        seek_row.setContentsMargins(4, 0, 4, 0)

        self._lbl_time = QLabel("0:00")
        self._lbl_time.setObjectName("previewCurrentTimeLabel")
        self._lbl_time.setMinimumWidth(40)
        self._lbl_time.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self._lbl_time.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        seek_row.addWidget(self._lbl_time)

        self._seek_slider = QSlider(Qt.Orientation.Horizontal)
        self._seek_slider.setRange(0, 1000)
        self._seek_slider.setValue(0)
        self._seek_slider.setTracking(True)
        self._seek_slider.setToolTip("Drag to seek within the preview timeline. The video will update as you scrub.")
        self._seek_slider.valueChanged.connect(self._on_seek_value_changed)
        self._seek_slider.sliderPressed.connect(self._on_seek_pressed)
        self._seek_slider.sliderReleased.connect(self._on_seek_released)
        seek_row.addWidget(self._seek_slider)

        self._lbl_dur = QLabel("0:00")
        self._lbl_dur.setObjectName("previewDurationLabel")
        self._lbl_dur.setMinimumWidth(40)
        self._lbl_dur.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self._lbl_dur.setToolTip("Total preview duration. Compare this to the song length to ensure nothing is cut off.")
        seek_row.addWidget(self._lbl_dur)

        root.addLayout(seek_row)

        # -- Buttons --------------------------------------------------------
        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(4, 0, 4, 0)
        btn_row.addStretch()

        self._btn_rewind = QPushButton()
        self._btn_rewind.setObjectName("previewRewindButton")
        self._btn_rewind.setText("-5s")
        self._btn_rewind.setMinimumWidth(52)
        self._btn_rewind.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self._btn_rewind.setToolTip("Rewind the preview by 5 seconds.")
        self._btn_rewind.clicked.connect(lambda: self._seek_relative(-5))
        btn_row.addWidget(self._btn_rewind)

        self._btn_play = QPushButton()
        self._btn_play.setObjectName("previewPlayButton")
        self._btn_play.setMinimumWidth(52)
        self._btn_play.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self._set_play_button_icon(False)
        self._btn_play.clicked.connect(self._toggle_playback)
        btn_row.addWidget(self._btn_play)

        self._btn_forward = QPushButton()
        self._btn_forward.setObjectName("previewForwardButton")
        self._btn_forward.setText("+5s")
        self._btn_forward.setMinimumWidth(52)
        self._btn_forward.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        self._btn_forward.setToolTip("Advance the preview by 5 seconds.")
        self._btn_forward.clicked.connect(lambda: self._seek_relative(5))
        btn_row.addWidget(self._btn_forward)

        btn_row.addStretch()
        root.addLayout(btn_row)

    # ==================================================================
    # PUBLIC API
    # ==================================================================

    def set_tool_paths(
        self,
        ffmpeg_path: str,
        ffprobe_path: str,
        ffmpeg_hwaccel: str = "auto",
        preview_video_mode: str = "proxy_low",
        preview_fps: int = PREVIEW_FPS,
        **kwargs
    ) -> None:
        """Update ffmpeg tool paths used by preview subprocesses."""
        self._ffmpeg_path = ffmpeg_path or "ffmpeg"
        self._ffprobe_path = ffprobe_path or "ffprobe"
        self._ffmpeg_hwaccel = ffmpeg_hwaccel or "auto"
        self._preview_video_mode = preview_video_mode or "proxy_low"
        try:
            fps = float(preview_fps)
        except (TypeError, ValueError):
            fps = float(PREVIEW_FPS)
        self._preview_fps_default = fps if fps > 0 else float(PREVIEW_FPS)

    def launch(
        self,
        video_path: str,
        audio_path: str,
        v_override: float = 0.0,
        a_offset: float = 0.0,
        start_time: float = 0.0,
        loop_start: float = 0.0,
        loop_end: float = 0.0,
        preview_fps: Optional[float] = None,
        accurate_seek: bool = False,
<<<<<<< Updated upstream
=======
        display_offset: float = 0.0,
        display_duration: float = 0.0,
        auto_loop: bool = True,
        force_refresh: bool = False,
>>>>>>> Stashed changes
    ) -> None:
        """Start (or restart) embedded preview playback.

        Args:
            video_path:  Absolute path to the ``.webm`` video file.
            audio_path:  Absolute path to the ``.ogg`` audio file.
            v_override:  Video start-time override (seconds, negative = intro).
            a_offset:    Audio offset in seconds.
            start_time:  Seek position in seconds to start from.
            loop_start:  Loop start in seconds (0 disables looping).
            loop_end:    Loop end in seconds.
            display_offset:   Optional visual offset for time labels/seekbar.
            display_duration: Optional visual duration override for seekbar.
        """
        if not video_path or not audio_path:
            return

        if preview_fps is None:
            effective_fps = self._preview_fps_default
        else:
            try:
                fps_val = float(preview_fps)
            except (TypeError, ValueError):
                fps_val = self._preview_fps_default
            effective_fps = fps_val if fps_val > 0 else self._preview_fps_default

        resolved_video_path = self._resolve_preview_video_path(video_path)
        # Resolve paths with proxying for heavy files
        resolved_audio_path = self._resolve_preview_audio_path(audio_path)
        resolved_audio_path = self._resolve_heavy_audio_proxy(resolved_audio_path)

        # Stash for resume / seek
        self._video_path = video_path
        self._audio_path = resolved_audio_path
        self._v_override = v_override
        self._a_offset = a_offset
        self._loop_start = max(0.0, loop_start)
        self._loop_end = max(0.0, loop_end)
        self._playback_fps = effective_fps
        self._accurate_seek = bool(accurate_seek)
        self._auto_loop = bool(auto_loop)

        # Prioritize clean state over speed. Abandoning soft-seek to ensure perfect sync.
        self._stop_worker()
        self.stop(
            reset_position=True,
            clear_canvas=not self._auto_loop,
            release_media=True
        )
        
        self._player.setSource(QUrl.fromLocalFile(resolved_audio_path))
        
        self._stop_requested = False
        self._ended_naturally = False
        
        self._display_offset = max(0.0, display_offset)
        self._display_duration = max(0.0, display_duration)

        # Probe raw durations if not cached
        v_raw = self._raw_video_dur_cache.get(resolved_video_path)
        if v_raw is None or force_refresh:
            try:
                v_raw = self._ffprobe_duration(resolved_video_path)
                self._raw_video_dur_cache[resolved_video_path] = v_raw
            except Exception as exc:
                logger.debug("Video probe failed: %s", exc)
                v_raw = None

        a_raw = self._raw_audio_dur_cache.get(resolved_audio_path)
        if a_raw is None or force_refresh:
            try:
                a_raw = self._ffprobe_duration(resolved_audio_path)
                self._raw_audio_dur_cache[resolved_audio_path] = a_raw
            except Exception:
                # Try candidates if direct probe failed
                for cand in self._audio_probe_candidates(audio_path):
                    try:
                        a_raw = self._ffprobe_duration(cand)
                        self._raw_audio_dur_cache[resolved_audio_path] = a_raw
                        break
                    except Exception:
                        continue

        # Compute playable duration from raw + offsets
        playable_values: list[float] = []
        if v_raw is not None:
            if v_override < 0:
                playable_values.append(v_raw - abs(v_override))
            else:
                playable_values.append(v_raw + v_override)
        
        if a_raw is not None:
            if a_offset < 0:
                playable_values.append(a_raw - abs(a_offset))
            else:
                playable_values.append(a_raw + a_offset)
                
        self._duration = max(playable_values) if playable_values else 120.0
        
        # UI Duration logic
        ui_dur = self._display_duration if self._display_duration > 0 else self._duration
        self._lbl_dur.setText(self._fmt(ui_dur))

        # Canvas dimensions
        w = max(self._canvas.width(), 320)
        h = max(self._canvas.height(), 180)

        # Compute seek positions
        vid_seek = abs(v_override) if v_override < 0 else 0.0
        video_delay_s = v_override if v_override > 0 else 0.0
        aud_delay_ms = 0
        if a_offset and a_offset < 0:
            aud_seek = abs(a_offset)
        elif a_offset and a_offset > 0:
            aud_seek = 0.0
            aud_delay_ms = int(a_offset * 1000)
        else:
            aud_seek = 0.0

        vid_seek += start_time
        aud_seek += start_time
        fine_video_seek = max(0.0, vid_seek)
        fine_audio_seek = max(0.0, aud_seek)

        vf_filters: list[str] = []
        # Accurate seek via trim is only used for non-looping playback.
        # For loops, we use input seeking (-ss before -i) which is faster and compatible with -stream_loop.
        if self._accurate_seek and not self._auto_loop:
            if fine_video_seek > 1e-6:
                vf_filters.append(f"trim=start={fine_video_seek:.6f}")
            vf_filters.append("setpts=PTS-STARTPTS")
        if video_delay_s > 0:
            vf_filters.append(f"tpad=start_duration={video_delay_s:.6f}")
        vf_filters.append(
            f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos"
        )
        vf_filters.append(f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black")
        vf_chain = ",".join(vf_filters)

        fps_arg = str(int(effective_fps)) if abs(effective_fps - round(effective_fps)) < 1e-6 else f"{effective_fps:.6f}"
        
        # Calculate duration limit if looping or hard bound requested
        duration_s = 0.0
        if self._loop_end > start_time:
            duration_s = self._loop_end - start_time

        # Command assembly
        # We use low probesize and analyzeduration to make FFmpeg start outputting frames instantly.
        ffmpeg_cmd: list[str] = [
            self._ffmpeg_path, 
            "-loglevel", "error",
            "-probesize", "32",
            "-analyzeduration", "0",
        ]
        if self._ffmpeg_hwaccel == "auto":
            ffmpeg_cmd += ["-hwaccel", "auto"]
            
        # Input options
        # We always use input seeking for loops to ensure speed and process stability.
        if self._auto_loop or not self._accurate_seek:
            if fine_video_seek > 1e-6:
                ffmpeg_cmd += ["-ss", f"{fine_video_seek:.6f}"]
            
        ffmpeg_cmd += ["-i", resolved_video_path]
        
        # Output options
        if duration_s > 0:
            ffmpeg_cmd += ["-t", f"{duration_s:.6f}"]
            
        ffmpeg_cmd += [
            "-vf", vf_chain,
            "-r", fps_arg,
            "-pix_fmt", "rgb24",
            "-f", "rawvideo",
            "-an", "-sn",
            "-"
        ]

<<<<<<< Updated upstream
        ffplay_cmd: list[str] = [
            self._ffplay_path, "-nodisp", "-autoexit", "-loglevel", "quiet",
        ]
        fine_audio_seek = max(0.0, aud_seek)
        if not self._accurate_seek:
            ffplay_cmd += ["-ss", f"{fine_audio_seek:.6f}"]
        ffplay_cmd += ["-i", resolved_audio_path]

        afilters: list[str] = []
        if self._accurate_seek:
            # Fine decoder-side trim preserves fractional precision after coarse seek.
            if fine_audio_seek > 1e-6:
                afilters.append(f"atrim=start={fine_audio_seek:.6f}")
            afilters.append("asetpts=PTS-STARTPTS")
        if aud_delay_ms > 0:
            afilters.append(f"adelay={aud_delay_ms}|{aud_delay_ms}")
        if afilters:
            ffplay_cmd += ["-af", ",".join(afilters)]

=======
>>>>>>> Stashed changes
        # Build worker + thread
        self._position = start_time
        visual_pos = self._position - self._display_offset
        self.position_changed.emit(visual_pos)
        self._playing = True
        self._first_frame_rendered = False
        self._set_play_button_icon(True)
        
        # Bi-directional Sync State
        self._video_ready = False
        self._audio_ready = False
        self._pending_audio_seek_ms = int(fine_audio_seek * 1000)
        self._aud_delay_ms = aud_delay_ms
            
        worker = _FrameReaderWorker(
            ffmpeg_cmd, w, h,
            start_position=start_time,
            fps=effective_fps,
        )
        thread = QThread()
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.frame_ready.connect(self._on_frame)
        worker.position_updated.connect(self._on_position)
        worker.playback_ended.connect(self._on_playback_ended)

        # Clean-up chain
        worker.playback_ended.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self._worker = worker
        self._thread = thread
        thread.start()
        
        # Prepare audio engine
        audio_target = QUrl.fromLocalFile(resolved_audio_path)
        if self._player.source() != audio_target:
            self._player.setSource(audio_target)
            
        self._start_audio_playback()
        
        self.preview_started.emit()

    def _stop_worker(self) -> None:
        """Kill only the background video worker, leaving the audio player untouched."""
        worker = self._worker
        thread = self._thread
        
        self._worker = None
        self._thread = None

        if worker is not None:
            try:
                worker.frame_ready.disconnect()
                worker.position_updated.disconnect()
                worker.playback_ended.disconnect()
            except (TypeError, RuntimeError):
                pass
            try:
                worker.request_stop()
            except (TypeError, RuntimeError):
                pass

        if thread is not None:
            try:
                if thread.isRunning():
                    thread.quit()
                    # Give the thread 100ms to exit natively before we move it to the dying list.
                    # This prevents the QThread destroyed warning in most cases.
                    if not thread.wait(100):
                        thread.terminate() # Forceful as a last resort
                    
                    self._dying_threads.append(thread)
                    thread.finished.connect(
                        lambda t=thread: self._dying_threads.remove(t) if t in self._dying_threads else None
                    )
            except RuntimeError:
                pass

    def stop(self, reset_position: bool = True, clear_canvas: bool = True, release_media: bool = True) -> None:
        """Stop any running preview subprocess safely.
        
        Args:
            reset_position: If True, sets _position back to 0.0 and clears labels.
            clear_canvas: If True, clears the preview canvas to "No Preview".
            release_media: If True, clears the QMediaPlayer source.
        """
        self._stop_requested = True
        self._ended_naturally = False
<<<<<<< Updated upstream
        if self._worker is not None:
            self._worker.request_stop()
=======
        
        if release_media:
            self._player.stop()
            self._player.setSource(QUrl())  # Release file handle
        else:
            self._player.pause()
        
        worker = self._worker
        thread = self._thread
        
        self._worker = None
        self._thread = None

        if worker is not None:
            try:
                worker.frame_ready.disconnect()
                worker.position_updated.disconnect()
                worker.playback_ended.disconnect()
            except (TypeError, RuntimeError):
                # Guard against "wrapped C/C++ object has been deleted"
                pass
            try:
                worker.request_stop()
            except (TypeError, RuntimeError):
                pass
>>>>>>> Stashed changes
        
        # Guard against RuntimeError if the C++ object was already deleted
        if thread is not None:
            try:
                if thread.isRunning():
                    thread.quit()
                    # Do not wait() in the GUI thread to avoid blocking.
                    # Keep a reference to prevent Python from garbage collecting the QThread
                    # wrapper while the C++ thread is still running.
                    self._dying_threads.append(thread)
                    thread.finished.connect(
                        lambda t=thread: self._dying_threads.remove(t) if t in self._dying_threads else None
                    )
            except RuntimeError:
                logger.debug("Preview thread already deleted.")

        if reset_position:
            self._position = 0.0
            self._lbl_time.setText("0:00")
            self._seek_slider.setValue(0)
            self.position_changed.emit(self._position)
        
        if clear_canvas:
            self._canvas.clear()
            self._canvas.setText("No Preview")

        if self._playing:
            self._playing = False
            self._set_play_button_icon(False)
            self.preview_stopped.emit()

    def reset(self) -> None:
        """Stop playback and reset all state."""
        self.stop()
        self._position = 0.0
        self.position_changed.emit(self._position)
        self._duration = 120.0
        self._loop_start = 0.0
        self._loop_end = 0.0
        self._canvas.setPixmap(QPixmap())
        self._canvas.setText("No Preview")
        self._seek_slider.setValue(0)
        self._lbl_time.setText("0:00")
        self._lbl_dur.setText("0:00")

    @property
    def is_playing(self) -> bool:
        return self._playing

    def get_current_position(self) -> float:
        """Return current preview playback position in seconds."""
        return self._position

    # ==================================================================
    # SLOTS
    # ==================================================================

<<<<<<< Updated upstream
    @pyqtSlot(QPixmap)
    def _on_frame(self, pixmap: QPixmap) -> None:
        self._canvas.setPixmap(pixmap)
=======
    @pyqtSlot(QImage)
    def _on_frame(self, image: QImage) -> None:
        if self._stop_requested:
            return

        # Handshake: First frame signals video readiness
        if not getattr(self, "_video_ready", False):
            self._video_ready = True
            self._check_sync_start(self._player.position())

        self._canvas.setPixmap(QPixmap.fromImage(image))
        self._first_frame_rendered = True
>>>>>>> Stashed changes

    @pyqtSlot(float)
    def _on_position(self, pos: float) -> None:
        self._position = pos
        
        visual_pos = max(0.0, pos - self._display_offset)
        self.position_changed.emit(visual_pos)
        self._lbl_time.setText(self._fmt(visual_pos))
        
        ui_dur = self._display_duration if self._display_duration > 0 else self._duration
        if ui_dur > 0 and not self._seek_slider.isSliderDown():
            pct = int((visual_pos / ui_dur) * 1000)
            self._seek_slider.blockSignals(True)
            self._seek_slider.setValue(min(pct, 1000))
            self._seek_slider.blockSignals(False)

    @pyqtSlot()
    def _on_playback_ended(self) -> None:
        if self._stop_requested:
            return

        self._playing = False
        self._ended_naturally = True
        self._set_play_button_icon(False)
        
        if self._auto_loop:
            self._relaunch(self._loop_start)
            return

        self._playing = False
        self._ended_naturally = True
        self._set_play_button_icon(False)
        self.preview_stopped.emit()

    @pyqtSlot()
<<<<<<< Updated upstream
    def _on_ffplay_missing(self) -> None:
        if self._ffplay_warned:
            return
        self._ffplay_warned = True
        self.audio_unavailable.emit()
=======
    def _start_audio_playback(self) -> None:
        if self._stop_requested:
            return

        if self._player.mediaStatus() in {QMediaPlayer.MediaStatus.LoadingMedia, QMediaPlayer.MediaStatus.NoMedia}:
            # Media not ready yet, retry in a bit.
            QTimer.singleShot(20, self._start_audio_playback)
            return

        self._waiting_for_audio_pos = True
        target_ms = getattr(self, "_pending_audio_seek_ms", 0)
        if target_ms > 0:
            self._player.setPosition(target_ms)
        
        # Start persistent polling until handshake completes
        if not hasattr(self, "_sync_poll_timer"):
            self._sync_poll_timer = QTimer(self)
            self._sync_poll_timer.timeout.connect(self._poll_audio_pos)
            
        self._sync_poll_timer.start(16)
        
        # Fallback to ensure video starts if audio engine is exceptionally slow
        QTimer.singleShot(10000, self._force_worker_tick)

    @pyqtSlot(QMediaPlayer.MediaStatus)
    def _on_media_status_changed(self, status: QMediaPlayer.MediaStatus) -> None:
        """Apply pending seek as soon as media is ready."""
        if status in {QMediaPlayer.MediaStatus.LoadedMedia, QMediaPlayer.MediaStatus.BufferedMedia}:
            target_ms = getattr(self, "_pending_audio_seek_ms", 0)
            if target_ms > 0:
                self._player.setPosition(target_ms)
            
            # Start polling to confirm seek success
            for delay in range(16, 1000, 16):
                QTimer.singleShot(delay, self._poll_audio_pos)

    @pyqtSlot()
    def _force_worker_tick(self) -> None:
        if self._stop_requested:
            return
        if getattr(self, "_waiting_for_audio_pos", False):
            self._waiting_for_audio_pos = False
            if self._worker is not None:
                self._worker.start_ticking(0.0)

    def _poll_audio_pos(self) -> None:
        """Manually poll position to check for seek completion."""
        if not getattr(self, "_waiting_for_audio_pos", False) or self._stop_requested:
            return
        
        pos_ms = self._player.position()
        target_ms = getattr(self, "_pending_audio_seek_ms", 0)
        
        # Audio is "ready" if it's near the target position.
        # For target 0, any position >= 0 is ready.
        is_ready = (target_ms == 0 and pos_ms >= 0) or (target_ms > 0 and pos_ms >= (target_ms - 100))
        
        if is_ready:
            self._check_sync_start(pos_ms)

    def _check_sync_start(self, current_pos_ms: int) -> None:
        """Handshake: Called when audio position moves."""
        if getattr(self, "_waiting_for_audio_pos", False) and getattr(self, "_video_ready", False):
            target_ms = getattr(self, "_pending_audio_seek_ms", 0)
            
            # Ensure the audio engine has actually reached the target area.
            if (target_ms == 0 and current_pos_ms >= 0) or (target_ms > 0 and current_pos_ms >= (target_ms - 100)):
                self._waiting_for_audio_pos = False
                if hasattr(self, "_sync_poll_timer"):
                    self._sync_poll_timer.stop()
                
                # Firing sequence: Start audio then unblock video
                self._player.play()
                
                # Calculate precise offset
                pos_ms = self._player.position()
                diff_s = (pos_ms - target_ms) / 1000.0
                
                if self._worker is not None:
                    # Sync lock: unblock the video worker
                    self._worker.start_ticking(diff_s)

    @pyqtSlot('qint64')
    def _on_player_position_changed(self, pos_ms: int) -> None:
        """Handshake: Called by QMediaPlayer signal."""
        if not getattr(self, "_waiting_for_audio_pos", False) or self._stop_requested:
            return
            
        target_ms = getattr(self, "_pending_audio_seek_ms", 0)
        
        # Check if audio is ready at target
        if not getattr(self, "_audio_ready", False):
            # Audio is ready if it's near target or moving
            if (target_ms == 0 and pos_ms >= 0) or (target_ms > 0 and pos_ms >= (target_ms - 100)):
                self._audio_ready = True
                self._check_sync_start(pos_ms)
>>>>>>> Stashed changes

    # ==================================================================
    # UI CALLBACKS
    # ==================================================================

    def _toggle_playback(self) -> None:
        if self._playing:
            self.stop(reset_position=False, clear_canvas=False)
        else:
            if self._ended_naturally:
                self._position = 0.0
                self.position_changed.emit(self._position)
            self._relaunch(self._position)

    def _seek_relative(self, delta: float) -> None:
        new_pos = max(0.0, min(self._position + delta, self._duration))
        self._position = new_pos
        self.position_changed.emit(self._position)
        if self._playing:
            self._relaunch(new_pos)
        elif self._ended_naturally and self._video_path and self._audio_path:
            self._relaunch(new_pos)
        else:
            self._lbl_time.setText(self._fmt(new_pos))
            if self._duration > 0:
                pct = int((new_pos / self._duration) * 1000)
                self._seek_slider.setValue(min(pct, 1000))

    def _on_seek_released(self) -> None:
        pct = self._seek_slider.value() / 1000.0
        ui_dur = self._display_duration if self._display_duration > 0 else self._duration
        
        visual_pos = pct * ui_dur
        self._position = visual_pos + self._display_offset
        
        self.position_changed.emit(visual_pos)
        self._lbl_time.setText(self._fmt(visual_pos))
        
        if self._playing or self._resume_after_seek:
            self._relaunch(self._position)
        elif self._ended_naturally and self._video_path and self._audio_path:
            self._relaunch(self._position)
        self._resume_after_seek = False

    def _on_seek_pressed(self) -> None:
        self._resume_after_seek = self._playing

    def _on_seek_value_changed(self, value: int) -> None:
        ui_dur = self._display_duration if self._display_duration > 0 else self._duration
        target_visual = (value / 1000.0) * ui_dur
        self._lbl_time.setText(self._fmt(target_visual))
        
        if self._seek_slider.isSliderDown():
            return

        # Actual position for internal logic
        actual_target = target_visual + self._display_offset
        
        # Clicking directly on the timeline groove may not set slider-down state.
        # Seek immediately so click-to-seek behaves as expected.
        if self._playing and abs(actual_target - self._position) >= 0.25:
            self._position = actual_target
            self.position_changed.emit(target_visual)
            self._relaunch(self._position)
            return

        if self._ended_naturally and abs(actual_target - self._position) >= 0.25:
            self._position = actual_target
            self.position_changed.emit(target_visual)
            self._relaunch(self._position)

    def _relaunch(self, start_time: float = 0.0) -> None:
        if self._video_path and self._audio_path:
            self.launch(
                self._video_path, self._audio_path,
                self._v_override, self._a_offset,
                start_time=start_time,
                loop_start=self._loop_start,
                loop_end=self._loop_end,
                preview_fps=self._playback_fps,
                accurate_seek=self._accurate_seek,
                display_offset=self._display_offset,
                display_duration=self._display_duration,
                auto_loop=self._auto_loop,
            )

    # ==================================================================
    # HELPERS
    # ==================================================================

    def _resolve_preview_audio_path(self, audio_path: str) -> str:
        """Resolve a preview-playable audio path.

        Preview input may be a cooked `.wav.ckd` path that ffplay cannot decode.
        Prefer existing decoded siblings first, then try a one-time decode fallback.
        """
        path = Path(audio_path)

        # Convert streaming formats to a seek-friendly PCM WAV cache for stable preview jumps.
        if path.suffix.lower() in {".ogg", ".opus"}:
            try:
                stat = path.stat()
                cache_key_src = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
                cache_key = hashlib.sha1(cache_key_src.encode("utf-8")).hexdigest()
                cache_dir = Path(tempfile.gettempdir()) / "jd2021_preview_audio_cache"
                cache_dir.mkdir(parents=True, exist_ok=True)
                cached_wav = cache_dir / f"{path.stem}_{cache_key[:10]}_seek.wav"
                if cached_wav.exists() and cached_wav.stat().st_size > 1024:
                    return str(cached_wav)

                cmd = [
                    self._ffmpeg_path,
                    "-y",
                    "-v",
                    "error",
                    "-i",
                    str(path),
                    "-ac",
                    "2",
                    "-ar",
                    "48000",
                    "-c:a",
                    "pcm_s16le",
                    str(cached_wav),
                ]
                completed = subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=_CFLAGS,
                    timeout=120,
                    check=False,
                )
                if completed.returncode == 0 and cached_wav.exists() and cached_wav.stat().st_size > 1024:
                    logger.info("Preview audio cache created: %s", cached_wav.name)
                    self._last_resolved_audio_src = audio_path
                    self._last_resolved_audio_result = str(cached_wav)
                    return str(cached_wav)
            except Exception as exc:
                logger.debug("Preview audio cache conversion skipped for %s: %s", path.name, exc)

        if path.suffix.lower() != ".ckd":
            return audio_path

        base_no_ckd = path.with_suffix("")
        stem = base_no_ckd.stem
        preview_cache = path.parent / "_preview_audio"
        candidates: list[Path] = [
            base_no_ckd,
            base_no_ckd.with_suffix(".wav"),
            base_no_ckd.with_suffix(".ogg"),
            path.parent / f"{stem}_raw_vgm.wav",
            path.parent / f"{stem}_decoded.wav",
            path.parent / f"{stem}_fixed.wav",
            path.parent / f"{stem}_fallback_fixed.wav",
            preview_cache / f"{stem}_raw_vgm.wav",
            preview_cache / f"{stem}_decoded.wav",
            preview_cache / f"{stem}_fixed.wav",
            preview_cache / f"{stem}_fallback_fixed.wav",
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.suffix.lower() in {".wav", ".ogg"}:
                if str(candidate) != audio_path:
                    logger.debug("Preview audio fallback selected: %s", candidate)
                return str(candidate)

        try:
            from jd2021_installer.installers.media_processor import extract_ckd_audio_v1

            preview_cache.mkdir(parents=True, exist_ok=True)
            decoded = extract_ckd_audio_v1(path, preview_cache)
            if decoded and Path(decoded).exists():
                logger.info("Preview audio decoded from CKD: %s", Path(decoded).name)
                return str(decoded)
        except Exception as exc:
            logger.warning("Preview audio decode failed for %s: %s", path.name, exc)

        logger.warning("Preview audio remains cooked CKD; ffplay may be silent: %s", path.name)
        return audio_path

    def _resolve_preview_video_path(self, video_path: str) -> str:
        """Resolve a preview-friendly proxy video for heavy source codecs.

        For VP9/large WebM sources, repeated seeks can become expensive on some
        machines. A cached low-res H.264 proxy keeps preview controls responsive.
        """
        path = Path(video_path)
        try:
            stat = path.stat()
        except OSError:
            return video_path

        # Proxy if it's a WebM (VP9 is slow to seek) or any file > 100MB
        is_heavy = (path.suffix.lower() == ".webm") or (stat.st_size > 100 * 1024 * 1024)
        if not is_heavy or self._preview_video_mode == "original":
            return video_path

        cache_key_src = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}"
        cache_key = hashlib.sha1(cache_key_src.encode("utf-8")).hexdigest()
        cached = self._preview_proxy_cache.get(cache_key)
        if cached and Path(cached).exists():
            return cached

        cache_dir = Path(tempfile.gettempdir()) / "jd2021_preview_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        proxy_path = cache_dir / f"{path.stem}_{cache_key[:10]}_preview.mp4"

        if proxy_path.exists() and proxy_path.stat().st_size > 1024:
            self._preview_proxy_cache[cache_key] = str(proxy_path)
            return str(proxy_path)

        # Keep transcode fast; quality only needs to be good enough for sync checks.
        cmd = [
            self._ffmpeg_path,
            "-y",
            "-v",
            "error",
        ]
        if self._ffmpeg_hwaccel == "auto":
            cmd += ["-hwaccel", "auto"]
        cmd += [
            "-i",
            str(path),
            "-vf",
            f"scale=min({PREVIEW_PROXY_WIDTH}\\,iw):-2:flags=lanczos",
            "-an",
            "-pix_fmt",
            "yuv420p",
            # Keep proxy generation fast and game-like (WebM/VP8 style).
            "-c:v", "libvpx",
            "-deadline", "realtime",
            "-cpu-used", "8",
            "-b:v", "900k",
            str(proxy_path),
        ]

        try:
            completed = subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=_CFLAGS,
                timeout=120,
                check=False,
            )
            if completed.returncode == 0 and proxy_path.exists() and proxy_path.stat().st_size > 1024:
                logger.info("Preview proxy created: %s", proxy_path.name)
                self._preview_proxy_cache[cache_key] = str(proxy_path)
                return str(proxy_path)
        except Exception as exc:
            logger.debug("Preview proxy transcode skipped for %s: %s", path.name, exc)

        return video_path

    def _ffprobe_duration(self, path: str) -> float:
        cmd = [
            self._ffprobe_path, "-v", "error", "-show_entries",
            "format=duration", "-of", "default=nw=1:nk=1",
            path,
        ]
        return float(
            subprocess.check_output(cmd, text=True, creationflags=_CFLAGS).strip()
        )

    def _resolve_heavy_audio_proxy(self, audio_path: str) -> str:
        """Create a lightweight proxy for large WAV/lossless files to speed up QMediaPlayer."""
        p = Path(audio_path)
        ext = p.suffix.lower()
        
        # Proxy everything except WAV to ensure instant seeking and high quality.
        if ext == ".wav":
            return audio_path
            
        try:
            stat = p.stat()
        except OSError:
            return audio_path

        # Cache check
        cache_key_src = f"aud_proxy_v4_{p.resolve()}_{stat.st_size}_{stat.st_mtime_ns}"
        cache_key = hashlib.sha1(cache_key_src.encode("utf-8")).hexdigest()
        
        if cache_key in self._audio_proxy_cache:
            cached = self._audio_proxy_cache[cache_key]
            if Path(cached).exists():
                return cached

        cache_dir = Path(tempfile.gettempdir()) / "jd2021_preview_audio_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        # 44.1kHz Stereo WAV (PCM) provides the most stable synchronization and seeking.
        proxy_path = cache_dir / f"{p.stem}_{cache_key[:10]}_v44.wav"

        if proxy_path.exists() and proxy_path.stat().st_size > 1024:
            self._audio_proxy_cache[cache_key] = str(proxy_path)
            return str(proxy_path)

        # High-quality WAV transcode (Whole file)
        cmd = [
            self._ffmpeg_path, "-y", "-v", "error",
            "-i", str(p),
            "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le",
            str(proxy_path)
        ]
        try:
            subprocess.run(cmd, creationflags=_CFLAGS, check=True)
            self._audio_proxy_cache[cache_key] = str(proxy_path)
            return str(proxy_path)
        except Exception as exc:
            logger.warning("Audio proxy creation failed: %s", exc)
            return audio_path

    def _audio_probe_candidates(self, path: str) -> list[str]:
        # Preview sometimes points to extracted .wav.ckd, which ffprobe cannot read.
        # Try sibling decoded files before giving up.
        p = Path(path)
        candidates = [str(p)]
        if p.suffix.lower() == ".ckd":
            no_ckd = p.with_suffix("")
            candidates.extend(
                [
                    str(no_ckd),
                    str(no_ckd.with_suffix(".wav")),
                    str(no_ckd.with_suffix(".ogg")),
                ]
            )
        seen: set[str] = set()
        ordered: list[str] = []
        for item in candidates:
            key = item.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(item)
        return ordered

    @staticmethod
    def _fmt(seconds: float) -> str:
        s = max(0.0, seconds)
        return f"{int(s // 60)}:{int(s % 60):02d}"
