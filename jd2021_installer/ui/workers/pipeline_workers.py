"""QObject-based workers for background processing via QThread.

Each worker encapsulates a heavy operation (extraction, normalization,
installation, media preview) and communicates with the GUI through
Qt signals exclusively — never touching widgets directly.

Usage (in controller):
    worker = InstallMapWorker(map_data, target_dir)
    thread = QThread()
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import re
import shutil
import struct
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import QObject, pyqtSignal

from jd2021_installer.core.config import AppConfig
from jd2021_installer.core.exceptions import ExtractionError, IPKExtractionError
from jd2021_installer.core.logging_config import log_exception_for_profile
from jd2021_installer.core.models import NormalizedMapData
from jd2021_installer.core.readjust_index import remove_entry
from jd2021_installer.extractors.base import BaseExtractor
from jd2021_installer.extractors.archive_ipk import ArchiveIPKExtractor
from jd2021_installer.installers.game_writer import write_game_files
from jd2021_installer.parsers.normalizer import normalize

logger = logging.getLogger("jd2021.ui.workers")

_READY_STATUS_VALUE = 3

_V1_RECOVERABLE_IPK_ERRORS = (IPKExtractionError, AssertionError, OSError, struct.error)


def _is_user_cancelled_browser_close(exc: BaseException) -> bool:
    text = str(exc).lower()
    return (
        "target page, context or browser has been closed" in text
        or "browser has been closed" in text
        or "target closed" in text
        or "fetch cancelled" in text
    )


def _path_has_codename_component(path: Path, codename: str) -> bool:
    codename_low = codename.lower()
    parts = [p.lower() for p in path.parts]
    if codename_low in parts:
        return True
    name_low = path.name.lower()
    return name_low.startswith(codename_low)


def _pick_ipk_audio(search_dirs: list[Path], codename: Optional[str]) -> Optional[Path]:
    candidates: list[Path] = []
    for pattern in ("*.ogg", "*.wav", "*.wav.ckd"):
        for root in search_dirs:
            if not root or not root.is_dir():
                continue
            for p in root.rglob(pattern):
                low_path = str(p).lower().replace("\\", "/")
                low_name = p.name.lower()
                if "audiopreview" in low_name:
                    continue
                if "/amb/" in low_path or "/autodance/" in low_path:
                    continue
                if low_name.startswith("amb_"):
                    continue
                candidates.append(p)

    if not candidates:
        return None

    if codename:
        scoped = [p for p in candidates if _path_has_codename_component(p, codename)]
        if scoped:
            candidates = scoped
        else:
            # V1 behavior: do not pick random media from another map when codename is known.
            return None

    codename_low = (codename or "").lower()
    if codename_low:
        exact_wav_ckd = [p for p in candidates if p.name.lower() == f"{codename_low}.wav.ckd"]
        if exact_wav_ckd:
            return exact_wav_ckd[0]

    has_x360_path = any("/x360/" in str(p).lower().replace("\\", "/") for p in candidates)
    preferred_suffixes = (".wav.ckd", ".wav", ".ogg") if has_x360_path else (".ogg", ".wav", ".wav.ckd")

    for suffix in preferred_suffixes:
        for p in candidates:
            low_name = p.name.lower()
            if suffix == ".wav.ckd":
                if low_name.endswith(".wav.ckd"):
                    return p
            elif low_name.endswith(suffix):
                return p

    return candidates[0]


def _pick_ipk_video(search_dirs: list[Path], codename: Optional[str]) -> Optional[Path]:
    candidates: list[Path] = []
    for root in search_dirs:
        if not root or not root.is_dir():
            continue
        for p in root.rglob("*.webm"):
            low_name = p.name.lower()
            if "mappreview" in low_name or "videopreview" in low_name:
                continue
            candidates.append(p)

    if not candidates:
        return None

    if codename:
        scoped = [p for p in candidates if _path_has_codename_component(p, codename)]
        if scoped:
            candidates = scoped
        else:
            # V1 behavior: avoid selecting cross-map videos when codename is known.
            return None

    for quality in ("ULTRA_HD", "ULTRA", "HIGH_HD", "HIGH", "MID_HD", "MID", "LOW_HD", "LOW"):
        suffix = f"_{quality}.webm"
        for p in candidates:
            if p.name.upper().endswith(suffix):
                return p
    return candidates[0]


def _collect_menuart_texture_sources(source_dir: Path, codename: str) -> list[Path]:
    """Collect candidate MenuArt texture directories for decode fallback.

    Some IPK layouts only place MenuArt textures under cache/itf_cooked/<platform>
    and not under world/maps/<codename>/MenuArt. This helper discovers both.
    """
    seen: set[str] = set()
    sources: list[Path] = []

    def _add(path: Path) -> None:
        key = str(path).lower()
        if key in seen:
            return
        if path.is_dir():
            seen.add(key)
            sources.append(path)

    _add(source_dir / "MenuArt")
    _add(source_dir / "menuart")
    _add(source_dir / "MenuArt" / "textures")
    _add(source_dir / "menuart" / "textures")

    extraction_root: Optional[Path] = None
    for candidate in [source_dir, *source_dir.parents]:
        if (candidate / "cache" / "itf_cooked").exists():
            extraction_root = candidate
            break
        if candidate.name.lower() in {"_extraction", "temp_extraction"}:
            extraction_root = candidate
            break

    if extraction_root is None:
        return sources

    itf_cooked = extraction_root / "cache" / "itf_cooked"
    if not itf_cooked.is_dir():
        return sources

    codename_low = codename.lower()
    for platform_dir in itf_cooked.iterdir():
        if not platform_dir.is_dir():
            continue
        _add(platform_dir / "world" / "maps" / codename / "menuart")
        _add(platform_dir / "world" / "maps" / codename_low / "menuart")
        _add(platform_dir / "world" / "maps" / codename / "menuart" / "textures")
        _add(platform_dir / "world" / "maps" / codename_low / "menuart" / "textures")

    return sources


def _ensure_optional_menuart_actors_from_textures(map_target: Path, codename: str) -> int:
    """Create optional MenuArt actor files when matching textures exist.

    Some sources (notably JDNext extraction paths) can populate optional MenuArt
    textures later in the install flow (pictos/cache decode), after initial
    `write_game_files` actor generation has already run.
    """
    texture_dirs = [
        map_target / "menuart" / "textures",
        map_target / "MenuArt" / "textures",
    ]
    actor_dir = map_target / "MenuArt" / "Actors"
    actor_dir.mkdir(parents=True, exist_ok=True)

    optional_suffixes = (
        "cover_albumbkg",
        "cover_albumcoach",
        "banner_bkg",
        "map_bkg",
    )
    texture_exts = (".tga", ".png", ".jpg", ".jpeg", ".tga.ckd", ".png.ckd", ".jpg.ckd", ".jpeg.ckd")

    created = 0
    for suffix in optional_suffixes:
        has_texture = False
        for tex_dir in texture_dirs:
            for ext in texture_exts:
                if (tex_dir / f"{codename}_{suffix}{ext}").exists():
                    has_texture = True
                    break
            if has_texture:
                break

        if not has_texture:
            continue

        act_path = actor_dir / f"{codename}_{suffix}.act"
        if act_path.exists():
            continue

        act_path.write_text(
            f'''params =
{{
    NAME="Actor",
    Actor =
    {{
        RELATIVEZ = 0,
        LUA = "enginedata/actortemplates/tpl_materialgraphiccomponent2d.tpl",
        COMPONENTS =
        {{
            {{
                NAME = "MaterialGraphicComponent",
                MaterialGraphicComponent =
                {{
                    disableLight = 0,
                    material =
                    {{
                        GFXMaterialSerializable =
                        {{
                            textureSet =
                            {{
                                GFXMaterialTexturePathSet =
                                {{
                                    diffuse = "World/MAPS/{codename}/menuart/textures/{codename}_{suffix}.tga"
                                }}
                            }},
                            shaderPath = "World/_COMMON/MatShader/MultiTexture_1Layer.msh"
                        }}
                    }}
                }}
            }}
        }}
    }}
}}''',
            encoding="utf-8",
        )
        created += 1

    return created


def _ensure_jdnext_albumcoach_texture_from_coach(map_target: Path, codename: str) -> bool:
    """Synthesize missing albumcoach texture from coach_1 for JDNext maps.

    JDNext sources commonly do not ship a dedicated albumcoach texture. In that
    case, mirror the primary coach texture so downstream actor references can be
    generated consistently. For multi-coach maps, composites all coaches side-by-side.
    """
    texture_dirs = [
        map_target / "menuart" / "textures",
        map_target / "MenuArt" / "textures",
    ]
    texture_exts = (".tga", ".png", ".jpg", ".jpeg", ".tga.ckd", ".png.ckd", ".jpg.ckd", ".jpeg.ckd")

    # If albumcoach already exists in any recognized extension, do nothing.
    for tex_dir in texture_dirs:
        for ext in texture_exts:
            if (tex_dir / f"{codename}_cover_albumcoach{ext}").exists():
                return False

    # Find all available coach textures for the map
    import re
    coach_pattern = re.compile(f"^{re.escape(codename.lower())}_coach_([1-9]){{1}}(\\.[a-z0-9.]+)?$")
    
    available_coaches: dict[int, Path] = {}
    found_dir: Optional[Path] = None
    
    for tex_dir in texture_dirs:
        if not tex_dir.exists():
            continue
        for child in tex_dir.iterdir():
            if not child.is_file():
                continue
            match = coach_pattern.match(child.name.lower())
            if match:
                coach_num = int(match.group(1))
                # Prefer .png over .tga if duplicates exist
                if coach_num not in available_coaches or child.suffix.lower() == ".png":
                    available_coaches[coach_num] = child
                    found_dir = tex_dir
    
    if not available_coaches or found_dir is None:
        return False
        
    sorted_coach_files = [available_coaches[k] for k in sorted(available_coaches.keys())]
    
    dst = found_dir / f"{codename}_cover_albumcoach.png"
    
    if len(sorted_coach_files) == 1:
        # Single coach, just copy directly
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sorted_coach_files[0], dst)
            return True
        except OSError:
            return False
            
    # Multi-coach: Compose them using PIL
    try:
        from PIL import Image
        
        base_img = Image.open(sorted_coach_files[0]).convert("RGBA")
        W, H = base_img.size
        
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        N = len(sorted_coach_files)
        
        # Scale, spacing, and Z-order rules based on N
        if N == 2:
            scale, spacing_ratio = 0.85, 0.43
            draw_order = [1, 0] # P2, P1
        elif N == 3:
            scale, spacing_ratio = 0.70, 0.35
            draw_order = [0, 2, 1] # P1, P3, P2
        elif N == 4:
            scale, spacing_ratio = 0.60, 0.28
            draw_order = [0, 3, 2, 1] # P1, P4, P3, P2
        else:
            scale = max(0.40, 1.0 - (N - 1) * 0.15)
            spacing_ratio = 0.8 / N
            draw_order = []
            left, right = 0, N - 1
            while left <= right:
                if left == right:
                    draw_order.append(left)
                else:
                    draw_order.extend([left, right])
                left += 1
                right -= 1
            draw_order.reverse()
            
        spacing = W * spacing_ratio
        
        # Load and resize all coaches first
        coach_imgs = []
        for c_file in sorted_coach_files:
            c_img = Image.open(c_file).convert("RGBA")
            if c_img.size != (W, H):
                c_img = c_img.resize((W, H), Image.Resampling.LANCZOS)
            cw, ch = int(W * scale), int(H * scale)
            c_img = c_img.resize((cw, ch), Image.Resampling.LANCZOS)
            coach_imgs.append((c_img, cw, ch))

        # Draw them in specified order
        for idx in draw_order:
            if idx >= len(coach_imgs):
                continue
            c_img, cw, ch = coach_imgs[idx]
            
            # Distribute centers horizontally around W/2
            center_x = (W / 2.0) + (idx - (N - 1) / 2.0) * spacing
            paste_x = int(center_x - cw / 2.0)
            # Anchor to the bottom edge
            paste_y = H - ch
            
            canvas.paste(c_img, (paste_x, paste_y), c_img)
            
        canvas.save(dst)
        return True
        
    except Exception as exc:
        logger.debug("Failed to composite multi-coach albumcoach: %s", exc)
        # Fallback to copy the first coach if PIL compositing fails
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sorted_coach_files[0], dst)
            return True
        except OSError:
            return False


def _apply_jdnext_bottom_alpha_fade_if_needed(map_target: Path, codename: str) -> int:
    """Apply JDNext-style bottom alpha fade to coach textures when missing.

    Returns number of textures updated. Files that already have a bottom fade are
    left untouched.
    """
    try:
        from PIL import Image
    except Exception:
        return 0

    texture_dirs = [
        map_target / "menuart" / "textures",
        map_target / "MenuArt" / "textures",
    ]

    def _candidate_texture(path: Path) -> bool:
        name_low = path.name.lower()
        if not name_low.startswith(f"{codename.lower()}_"):
            return False
        if "coach_" not in name_low and "cover_albumcoach" not in name_low:
            return False
        return path.suffix.lower() in {".png", ".tga"}

    def _row_alpha_means(alpha_img: "Image.Image") -> list[float]:
        width, height = alpha_img.size
        alpha_px = alpha_img.load()
        if alpha_px is None:
            return []
        means: list[float] = []
        for y in range(height):
            row_sum = 0
            for x in range(width):
                value = alpha_px[x, y]
                if isinstance(value, tuple):
                    row_sum += int(value[0])
                elif value is None:
                    row_sum += 0
                else:
                    row_sum += int(value)
            means.append(row_sum / max(1, width))
        return means

    def _already_has_bottom_fade(row_means: list[float]) -> bool:
        h = len(row_means)
        if h < 8:
            return False
        top_end = max(1, int(h * 0.35))
        fade_start = max(0, int(h * 0.70))
        top_mean = sum(row_means[:top_end]) / max(1, top_end)
        bottom_min = min(row_means[fade_start:])
        tail = row_means[-1]
        # Consider it already faded when the bottom approaches full transparency
        # and differs strongly from the opaque upper region.
        return tail <= 8 and bottom_min <= (top_mean * 0.35)

    updated = 0
    seen: set[str] = set()
    for tex_dir in texture_dirs:
        if not tex_dir.is_dir():
            continue
        for path in tex_dir.iterdir():
            key = str(path).lower()
            if key in seen:
                continue
            seen.add(key)

            if not path.is_file() or not _candidate_texture(path):
                continue

            try:
                with Image.open(path) as img:
                    rgba = img.convert("RGBA")
                width, height = rgba.size
                if width < 4 or height < 8:
                    continue

                alpha = rgba.getchannel("A")
                row_means = _row_alpha_means(alpha)
                if _already_has_bottom_fade(row_means):
                    continue

                fade_start = max(0, int(height * 0.70))
                if fade_start >= height - 1:
                    continue

                fade_den = max(1, (height - 1) - fade_start)
                px = rgba.load()
                if px is None:
                    continue
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

                rgba.save(path)
                updated += 1
            except OSError:
                continue

    return updated


def _install_menuart_companion_assets(menuart_sources: list[Path], map_target: Path) -> int:
    """Install non-texture MenuArt companion files shipped as *.ckd payloads.

    Some source layouts provide MenuArt actor/scene files as `.act.ckd` and
    `.isc.ckd` that are already plain payloads. These should be installed as
    `.act` / `.isc` files rather than fed to the texture decoder.
    """
    actor_dir = map_target / "MenuArt" / "Actors"
    scene_dir = map_target / "MenuArt"
    actor_dir.mkdir(parents=True, exist_ok=True)
    scene_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    seen_sources: set[str] = set()

    for src_dir in menuart_sources:
        if not src_dir.is_dir():
            continue
        for ckd_path in src_dir.rglob("*.ckd"):
            if not ckd_path.is_file():
                continue

            src_key = str(ckd_path).lower()
            if src_key in seen_sources:
                continue
            seen_sources.add(src_key)

            inner_suffix = Path(ckd_path.stem).suffix.lower()
            if inner_suffix not in {".act", ".isc"}:
                continue

            out_name = ckd_path.stem
            if inner_suffix == ".act":
                dst_path = actor_dir / out_name
            else:
                dst_path = scene_dir / out_name

            try:
                shutil.copy2(ckd_path, dst_path)
                copied += 1
            except OSError as exc:
                logger.debug("Failed to install MenuArt companion %s: %s", ckd_path.name, exc)

    return copied


def _collect_pictogram_sources(source_dir: Path, codename: str, preferred: Optional[Path] = None) -> list[Path]:
    """Collect candidate pictogram directories, including IPK cache fallbacks."""
    seen: set[str] = set()
    sources: list[Path] = []

    def _add(path: Optional[Path]) -> None:
        if path is None:
            return
        key = str(path).lower()
        if key in seen:
            return
        if path.is_dir():
            seen.add(key)
            sources.append(path)

    _add(preferred)
    _add(source_dir / "pictos")
    _add(source_dir / "timeline" / "pictos")

    extraction_root: Optional[Path] = None
    for candidate in [source_dir, *source_dir.parents]:
        if (candidate / "cache" / "itf_cooked").exists():
            extraction_root = candidate
            break
        if candidate.name.lower() in {"_extraction", "temp_extraction"}:
            extraction_root = candidate
            break

    if extraction_root is None:
        return sources

    itf_cooked = extraction_root / "cache" / "itf_cooked"
    if not itf_cooked.is_dir():
        return sources

    codename_low = codename.lower()
    for platform_dir in itf_cooked.iterdir():
        if not platform_dir.is_dir():
            continue
        _add(platform_dir / "world" / "maps" / codename / "timeline" / "pictos")
        _add(platform_dir / "world" / "maps" / codename_low / "timeline" / "pictos")

    return sources


def _validate_ipk_media_presence(
    map_output_dir: Path,
    codename: Optional[str],
    search_root: Optional[Path],
) -> list[str]:
    search_dirs = [map_output_dir]
    if search_root and search_root not in search_dirs:
        search_dirs.append(search_root)

    warnings: list[str] = []

    audio = _pick_ipk_audio(search_dirs, codename)
    if not audio:
        warnings.append(
            "No audio file found after IPK extraction. "
            "Ensure the IPK contains .ogg, .wav, or .wav.ckd audio."
        )

    video = _pick_ipk_video(search_dirs, codename)
    if not video:
        warnings.append(
            "No gameplay video (.webm) found after IPK extraction. "
            "Ensure a .webm file is in the source directory."
        )

    return warnings


class ExtractAndNormalizeWorker(QObject):
    """Extract map data and normalize it in a background thread."""

    progress = pyqtSignal(int)          # 0-100
    status = pyqtSignal(str)            # human-readable status message
    error = pyqtSignal(str, str)        # stage name, error message
    finished = pyqtSignal(object)       # NormalizedMapData or None

    def __init__(
        self,
        extractor: BaseExtractor,
        output_dir: Path,
        codename: Optional[str] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._extractor = extractor
        self._output_dir = output_dir
        self._codename = codename

    def run(self) -> None:
        failed_stage = "Extracting map data..."
        try:
            # Clear output_dir (temp extraction dir) before starting
            import shutil
            if self._output_dir.exists():
                logger.debug("Cleaning temp extraction dir: %s", self._output_dir)
                shutil.rmtree(self._output_dir)
            self._output_dir.mkdir(parents=True, exist_ok=True)

            self.status.emit("Extracting map data...")
            self.progress.emit(5)
            try:
                map_output_dir = self._extractor.extract(self._output_dir)
            except Exception as exc:
                if isinstance(self._extractor, ArchiveIPKExtractor) and isinstance(exc, _V1_RECOVERABLE_IPK_ERRORS):
                    logger.debug("IPK extraction issue (continuing for parity): %s", exc)
                    self.status.emit(f"Warning: IPK extraction issue: {exc}")
                    # V1 parity: continue with any partial extraction state.
                    map_output_dir = self._output_dir
                else:
                    raise

            for warning in self._extractor.get_warnings():
                self.status.emit(f"Warning: {warning}")

            codename = self._codename or self._extractor.get_codename()
            search_root: Optional[Path] = None
            normalize_search_root: Optional[Path] = None
            media_errors: list[str] = []

            if isinstance(self._extractor, ArchiveIPKExtractor):
                # V1 parity: IPK mode also probes media alongside the selected .ipk file.
                search_root = self._extractor.get_source_dir()
                media_errors.extend(_validate_ipk_media_presence(map_output_dir, codename, search_root))
                # For normalization, scan the extracted tree so map-local optional assets
                # (e.g., albumcoach/map_bkg) are discovered before ACT generation.
                normalize_search_root = map_output_dir
            elif hasattr(self._extractor, "is_ipk_source"):
                if bool(self._extractor.is_ipk_source()):  # type: ignore[attr-defined]
                    media_errors.extend(_validate_ipk_media_presence(map_output_dir, codename, None))
                    normalize_search_root = map_output_dir

            if media_errors:
                for error in media_errors:
                    logger.error("IPK media validation failed: %s", error)
                raise RuntimeError(" ".join(media_errors))

            failed_stage = "Parsing CKDs and metadata..."
            self.status.emit("Parsing CKDs and metadata...")
            self.progress.emit(10)

            failed_stage = "Normalizing assets..."
            self.status.emit("Normalizing assets...")
            self.progress.emit(15)

            map_data = normalize(
                map_output_dir,
                codename,
                search_root=normalize_search_root,
            )

            # Extraction/normalisation is only the first phase of the overall pipeline.
            # Emit 20 here so the install worker's 5→100 range continues forward without
            # the bar snapping back to a lower value.
            self.progress.emit(20)
            self.status.emit("Normalization completed.")
            self.finished.emit(map_data)

        except Exception as e:
            if isinstance(e, ExtractionError) or _is_user_cancelled_browser_close(e):
                user_msg = str(e)
                if _is_user_cancelled_browser_close(e):
                    user_msg = "Browser was closed by user. Fetch cancelled."
                logger.debug("ExtractAndNormalize failed: %s", user_msg)
                self.error.emit(failed_stage, user_msg)
                self.finished.emit(None)
                return

            log_exception_for_profile(logger, "ExtractAndNormalize failed", e)
            self.error.emit(failed_stage, str(e))
            self.finished.emit(None)


class InstallMapWorker(QObject):
    """Install a normalized map into the game directory."""

    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal(bool)         # success / failure

    def __init__(
        self,
        map_data: NormalizedMapData,
        target_dir: Path,
        source_mode: str = "",
        config: Optional[AppConfig] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._map_data = map_data
        self._target_dir = target_dir
        self._source_mode = source_mode
        self._config = config

    def run(self) -> None:
        try:
            install_map_to_game(
                self._map_data, 
                self._target_dir, 
                self._config,
                source_mode=self._source_mode,
                status_callback=self.status.emit,
                progress_callback=self.progress.emit
            )
            self.finished.emit(True)

        except Exception as e:
            log_exception_for_profile(logger, "InstallMap failed", e)
            self.error.emit(str(e))
            self.finished.emit(False)


def reprocess_audio(
    map_data: NormalizedMapData, 
    target_dir: Path, 
    a_offset: float = 0.0,
    config: Optional[AppConfig] = None
) -> None:
    """Rebuild game configuration files and reprocess physical audio files."""
    codename = map_data.codename
    mainsequence_path = target_dir / "Cinematics" / f"{codename}_MainSequence.tape"
    mainsequence_backup: Optional[str] = None
    if mainsequence_path.exists():
        try:
            mainsequence_backup = mainsequence_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            mainsequence_backup = None

    # 1. Update UbiArt config (musictrack.trk, etc)
    write_game_files(map_data, target_dir, config)

    # Keep converted cinematic tape content across offset reprocess runs.
    if mainsequence_backup:
        try:
            mainsequence_path.parent.mkdir(parents=True, exist_ok=True)
            mainsequence_path.write_text(mainsequence_backup, encoding="utf-8")
        except OSError as exc:
            logger.debug("Could not restore existing MainSequence tape for '%s': %s", codename, exc)
    
    # 2. Ported V1 FFmpeg logic: pad/trim main audio and generate intro AMB
    from jd2021_installer.installers.media_processor import (
        convert_audio, 
        generate_intro_amb,
        extract_amb_clips,
    )

    source_dir = map_data.source_dir
    source_is_html = bool(getattr(map_data, "is_html_source", False))
    if not source_is_html and source_dir and source_dir.exists():
        source_is_html = any(source_dir.glob("*.html")) or any(source_dir.glob("**/assets.html"))

    source_is_jdnext = bool(getattr(map_data, "is_jdnext_source", False))
    if not source_is_jdnext:
        if source_dir and source_dir.exists() and (
            (source_dir / "jdnext_metadata.json").exists()
            or (source_dir / "monobehaviour" / "map.json").exists()
        ):
            source_is_jdnext = True
        elif map_data.media.video_path and re.match(
            r"^video_(ultra|high|mid|low)\.(hd|vp8|vp9)\.webm$",
            map_data.media.video_path.name.lower(),
        ):
            source_is_jdnext = True

    # Preserve native IPK intro AMB assets when present; apply generated intro
    # flow only for JDNext sources.
    intro_amb_attempt_enabled = source_is_jdnext
    
    media = map_data.media

    if (not media.audio_path or not media.audio_path.exists()) and map_data.source_dir:
        fallback_audio = _pick_ipk_audio([map_data.source_dir], codename)
        if fallback_audio and fallback_audio.exists():
            media.audio_path = fallback_audio
            logger.info("Recovered missing audio source from extraction tree: %s", fallback_audio)
    
    if not media.audio_path or not media.audio_path.exists():
        raise RuntimeError(
            f"Audio source missing for '{codename}'. "
            "Bundle/IPK map cannot be finalized without decoded audio."
        )

    # Generates audio/<codename>.wav and .ogg
    convert_audio(media.audio_path, codename, target_dir, a_offset, config)
    
    # Generates audio/amb/<intro>.wav/tpl/ilu and injects into audio ISC
    ogg_path = target_dir / "audio" / f"{codename}.ogg"
    v_override = map_data.effective_video_start_time
    
    if intro_amb_attempt_enabled:
        # Use beat marker data if available for precise pre-roll
        preroll = None
        if map_data.music_track and map_data.music_track.markers:
            from jd2021_installer.parsers.binary_ckd import calculate_marker_preroll

            preroll = calculate_marker_preroll(
                map_data.music_track.markers,
                map_data.music_track.start_beat,
                include_calibration=True,
            )

        generate_intro_amb(
            ogg_path,
            codename,
            target_dir,
            a_offset,
            v_override,
            preroll,
            True,
            config,
        )

        # Ensure intro AMB is triggerable even when source cinematic clip data is sparse.
        try:
            from jd2021_installer.installers.ambient_processor import _inject_intro_amb_soundset_clip

            _inject_intro_amb_soundset_clip(
                target_dir,
                codename,
                attempt_enabled=True,
            )
        except Exception as exc:
            logger.debug("AMB SoundSetClip injection skipped for '%s': %s", codename, exc)

    # Ported V1: Extract cinematic AMB clips from the main audio
    if map_data.cinematic_tape:
        extract_amb_clips(map_data.cinematic_tape, media.audio_path, target_dir, codename, config)


def _update_trk_video_start_time(trk_path: Path, value_seconds: float) -> None:
    """Patch videoStartTime in an existing .trk file."""
    if not trk_path.exists():
        raise RuntimeError(f"Missing .trk file for readjust apply: {trk_path}")

    content = trk_path.read_text(encoding="utf-8")
    pattern = r"videoStartTime\s*=\s*([-+]?\d*\.?\d+)"
    replacement = f"videoStartTime = {value_seconds:.6f}"
    updated, count = re.subn(pattern, replacement, content, count=1)
    if count == 0:
        raise RuntimeError(f"Could not find videoStartTime in {trk_path}")
    trk_path.write_text(updated, encoding="utf-8")


def reprocess_audio_readjust(
    map_data: NormalizedMapData,
    target_dir: Path,
    *,
    a_offset: float,
    v_override: float,
    update_video: bool,
    update_audio: bool,
    config: Optional[AppConfig] = None,
) -> None:
    """Apply readjust offsets without rewriting full map config files.

    This mode is used for readjust sessions restored from index entries where
    source CKD payloads may no longer be complete.
    """
    from jd2021_installer.installers.media_processor import (
        convert_audio,
        generate_intro_amb,
        extract_amb_clips,
    )

    source_dir = map_data.source_dir
    source_is_html = bool(getattr(map_data, "is_html_source", False))
    if not source_is_html and source_dir and source_dir.exists():
        source_is_html = any(source_dir.glob("*.html")) or any(source_dir.glob("**/assets.html"))

    source_is_jdnext = bool(getattr(map_data, "is_jdnext_source", False))
    if not source_is_jdnext:
        if source_dir and source_dir.exists() and (
            (source_dir / "jdnext_metadata.json").exists()
            or (source_dir / "monobehaviour" / "map.json").exists()
        ):
            source_is_jdnext = True
        elif map_data.media.video_path and re.match(
            r"^video_(ultra|high|mid|low)\.(hd|vp8|vp9)\.webm$",
            map_data.media.video_path.name.lower(),
        ):
            source_is_jdnext = True

    # Preserve native IPK intro AMB assets when present; apply generated intro
    # flow only for JDNext sources.
    intro_amb_attempt_enabled = source_is_jdnext

    codename = map_data.codename
    media = map_data.media

    if (not media.audio_path or not media.audio_path.exists()) and map_data.source_dir:
        fallback_audio = _pick_ipk_audio([map_data.source_dir], codename)
        if fallback_audio and fallback_audio.exists():
            media.audio_path = fallback_audio
            logger.info("Recovered missing audio source from extraction tree: %s", fallback_audio)

    if update_video:
        trk_path = target_dir / "Audio" / f"{codename}.trk"
        _update_trk_video_start_time(trk_path, v_override)

    if not update_audio:
        return

    if not media.audio_path or not media.audio_path.exists():
        raise RuntimeError(
            f"Audio source missing for '{codename}'. Cannot apply readjust audio offset."
        )

    convert_audio(media.audio_path, codename, target_dir, a_offset, config)

    ogg_path = target_dir / "audio" / f"{codename}.ogg"
    if intro_amb_attempt_enabled:
        preroll = None
        if map_data.music_track and map_data.music_track.markers:
            from jd2021_installer.parsers.binary_ckd import calculate_marker_preroll

            preroll = calculate_marker_preroll(
                map_data.music_track.markers,
                map_data.music_track.start_beat,
                include_calibration=True,
            )

        generate_intro_amb(
            ogg_path,
            codename,
            target_dir,
            a_offset,
            v_override,
            preroll,
            True,
            config,
        )

        try:
            from jd2021_installer.installers.ambient_processor import _inject_intro_amb_soundset_clip

            _inject_intro_amb_soundset_clip(
                target_dir,
                codename,
                attempt_enabled=True,
            )
        except Exception as exc:
            logger.debug("AMB SoundSetClip injection skipped for '%s' (readjust): %s", codename, exc)

    if map_data.cinematic_tape:
        extract_amb_clips(map_data.cinematic_tape, media.audio_path, target_dir, codename, config)


class ApplyAndFinishWorker(QObject):
    """Safely executes reprocess_audio(), clearing cache and finalizing offsets."""

    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal(bool)

    def __init__(
        self,
        map_data: NormalizedMapData,
        target_dir: Path,
        cache_dir: Path,
        a_offset: float = 0.0,
        config: Optional[AppConfig] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._map_data = map_data
        self._target_dir = target_dir
        self._cache_dir = cache_dir
        self._a_offset = a_offset
        self._config = config

    def run(self) -> None:
        try:
            self.status.emit("Finalizing offsets...")
            self.progress.emit(30)
            
            # 1. Update configs and audio via reprocess_audio
            reprocess_audio(self._map_data, self._target_dir, self._a_offset, self._config)
            
            self.progress.emit(100)
            self.status.emit("Sync offsets applied successfully.")
            self.finished.emit(True)
        except Exception as e:
            log_exception_for_profile(logger, "ApplyAndFinish failed", e)
            self.error.emit(str(e))
            self.finished.emit(False)


class ApplyOffsetsBatchWorker(QObject):
    """Apply offset finalization across multiple maps in one background task."""

    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal(bool)

    def __init__(
        self,
        entries: list[tuple[NormalizedMapData, Path, float]],
        config: Optional[AppConfig] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._entries = entries
        self._config = config

    def run(self) -> None:
        try:
            total = len(self._entries)
            if total == 0:
                self.finished.emit(True)
                return

            for idx, (map_data, target_dir, a_offset) in enumerate(self._entries, start=1):
                codename = map_data.codename
                self.status.emit(f"[{codename}] Finalizing offsets...")
                progress = int(((idx - 1) / total) * 100)
                self.progress.emit(progress)
                reprocess_audio(map_data, target_dir, a_offset, self._config)

            self.progress.emit(100)
            self.status.emit("Sync offsets applied successfully.")
            self.finished.emit(True)
        except Exception as e:
            log_exception_for_profile(logger, "ApplyOffsetsBatch failed", e)
            self.error.emit(str(e))
            self.finished.emit(False)


class ApplyReadjustOffsetsBatchWorker(QObject):
    """Apply readjust offsets across one or more maps."""

    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal(bool)

    def __init__(
        self,
        entries: list[tuple[NormalizedMapData, Path, float, float, bool, bool]],
        config: Optional[AppConfig] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._entries = entries
        self._config = config

    def run(self) -> None:
        try:
            total = len(self._entries)
            if total == 0:
                self.finished.emit(True)
                return

            for idx, (map_data, target_dir, a_offset, v_override, update_video, update_audio) in enumerate(self._entries, start=1):
                codename = map_data.codename
                self.status.emit(f"[{codename}] Finalizing offsets...")
                progress = int(((idx - 1) / total) * 100)
                self.progress.emit(progress)
                reprocess_audio_readjust(
                    map_data,
                    target_dir,
                    a_offset=a_offset,
                    v_override=v_override,
                    update_video=update_video,
                    update_audio=update_audio,
                    config=self._config,
                )

            self.progress.emit(100)
            self.status.emit("Sync offsets applied successfully.")
            self.finished.emit(True)
        except Exception as e:
            log_exception_for_profile(logger, "ApplyReadjustOffsetsBatch failed", e)
            self.error.emit(str(e))
            self.finished.emit(False)


class BatchInstallWorker(QObject):
    """Iterate through a directory and install all discovered maps (folders/IPKs)."""

    progress = pyqtSignal(int)          # overall progress 0-100
    status = pyqtSignal(str)            # status text
    error = pyqtSignal(str)             # error message
    finished = pyqtSignal(bool)         # success flag
    finished_with_data = pyqtSignal(list) # list[NormalizedMapData]
    discovered_maps = pyqtSignal(list)  # list[str] (codenames)

    def __init__(
        self,
        batch_source_dir: Path,
        target_game_dir: Path,
        config: AppConfig,
        selected_maps: Optional[set[str]] = None,
        fetch_codenames: Optional[list[str]] = None,
        fetch_source: str = "jdu",
        force_unlock_locked_status: bool = False,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._source_dir = batch_source_dir
        self._target_dir = target_game_dir
        self._config = config
        self._selected_maps = selected_maps
        self._fetch_codenames = [c.strip() for c in (fetch_codenames or []) if c and c.strip()]
        self._fetch_source = (fetch_source or "jdu").strip().lower() or "jdu"
        self._force_unlock_locked_status = force_unlock_locked_status

    def run(self) -> None:
        try:
            import shutil

            self.status.emit("Scanning for maps in batch directory...")
            progress_value = 0

            def find_html_pair(folder: Path) -> tuple[Optional[Path], Optional[Path]]:
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

                if len(html_files) >= 2:
                    if asset is None:
                        asset = next((h for h in html_files if h != nohud), html_files[0])
                    if nohud is None:
                        nohud = next((h for h in html_files if h != asset), html_files[-1])

                return asset, nohud

            def detect_html_source_game(asset_html: Optional[Path]) -> str:
                if asset_html is None or not asset_html.is_file():
                    return "jdu"
                try:
                    content = asset_html.read_text(encoding="utf-8", errors="ignore").lower()
                except OSError:
                    return "jdu"
                if "/jdnext/maps/" in content or "server:jdnext" in content:
                    return "jdnext"
                return "jdu"

            def looks_like_prepared_map_dir(folder: Path) -> bool:
                if not folder.is_dir():
                    return False

                has_audio = False
                has_video = False
                has_musictrack = False

                try:
                    for root, _dirs, files in os.walk(folder):
                        for filename in files:
                            low_name = filename.lower()

                            # Keep legacy behavior: any CKD in the tree qualifies as prepared.
                            if low_name.endswith(".ckd"):
                                return True

                            if not has_audio and (low_name.endswith(".ogg") or low_name.endswith(".wav")):
                                low_path = str(Path(root) / filename).lower().replace("\\", "/")
                                if "audiopreview" not in low_name and "/amb/" not in low_path and "/autodance/" not in low_path and not low_name.startswith("amb_"):
                                    has_audio = True

                            if not has_video and low_name.endswith(".webm"):
                                if "mappreview" not in low_name and "videopreview" not in low_name:
                                    has_video = True

                            if not has_musictrack and low_name.endswith(".tpl.ckd") and "musictrack" in low_name:
                                has_musictrack = True

                        if has_audio and has_video and has_musictrack:
                            return True
                except OSError:
                    return False

                return has_audio and has_video and has_musictrack

            def emit_progress(value: int) -> None:
                nonlocal progress_value
                clamped = max(0, min(100, value))
                if clamped > progress_value:
                    progress_value = clamped
                    self.progress.emit(clamped)
            
            candidates: list[dict[str, object]] = []
            if self._fetch_codenames:
                for codename in self._fetch_codenames:
                    candidates.append({"kind": "fetch", "name": codename, "path": self._source_dir})

            # When explicit fetch codenames are provided, treat this as a pure fetch batch.
            if self._source_dir and not self._fetch_codenames:
                if self._source_dir.is_file() and self._source_dir.suffix.lower() == ".ipk":
                    candidates.append({"kind": "ipk", "path": self._source_dir})
                elif self._source_dir.is_dir():
                    root_asset, root_nohud = find_html_pair(self._source_dir)
                    root_source_game = detect_html_source_game(root_asset)
                    if root_asset and (root_nohud or root_source_game == "jdnext"):
                        candidates.append(
                            {
                                "kind": "html_jdnext" if root_source_game == "jdnext" else "html",
                                "path": self._source_dir,
                                "name": self._source_dir.name,
                                "asset": root_asset,
                                "nohud": root_nohud,
                                "source_game": root_source_game,
                            }
                        )

                    for path in sorted(self._source_dir.iterdir(), key=lambda p: p.name.lower()):
                        if path.is_file() and path.suffix.lower() == ".ipk":
                            candidates.append({"kind": "ipk", "path": path})
                        elif path.is_dir():
                            if looks_like_prepared_map_dir(path):
                                candidates.append({"kind": "dir", "path": path})
                                continue

                            asset_html, nohud_html = find_html_pair(path)
                            source_game = detect_html_source_game(asset_html)
                            if asset_html and (nohud_html or source_game == "jdnext"):
                                candidates.append(
                                    {
                                        "kind": "html_jdnext" if source_game == "jdnext" else "html",
                                        "path": path,
                                        "name": path.name,
                                        "asset": asset_html,
                                        "nohud": nohud_html,
                                        "source_game": source_game,
                                    }
                                )
            
            total = len(candidates)
            if total == 0:
                self.error.emit(
                    "No valid IPK files, prepared map folders, or HTML map folders found in the selected batch directory."
                )
                self.finished.emit(False)
                return

            emit_progress(1)
            self.status.emit(f"Found {total} source item(s) to process.")
            
            # Emit discovered map names to the UI so it can populate the checklist
            map_names = []
            for candidate in candidates:
                kind = str(candidate["kind"])
                cpath = Path(candidate["path"])
                if kind == "fetch":
                    map_names.append(str(candidate.get("name") or cpath.name))
                elif kind == "ipk":
                    from jd2021_installer.extractors.archive_ipk import inspect_ipk
                    maps_in_ipk = inspect_ipk(cpath)
                    map_names.extend(maps_in_ipk or [cpath.stem])
                elif kind in {"html", "html_jdnext"}:
                    map_names.append(str(candidate.get("name") or cpath.name))
                else:
                    map_names.append(cpath.name)

            # Merge all discovered map names into one stable list (case-insensitive dedupe).
            merged_map_names: list[str] = []
            seen_discovered: set[str] = set()
            for name in map_names:
                key = name.lower()
                if key in seen_discovered:
                    continue
                seen_discovered.add(key)
                merged_map_names.append(name)

            selected_lookup = {m.lower() for m in self._selected_maps} if self._selected_maps else None
            display_map_names = merged_map_names
            if selected_lookup is not None:
                display_map_names = [name for name in merged_map_names if name.lower() in selected_lookup]
            self.discovered_maps.emit(display_map_names)
            emit_progress(3)

            planned_maps = len(display_map_names) if display_map_names else len(merged_map_names)
            total_units = max(planned_maps * 3, 1)
            completed_units = 0

            def emit_map_stage(stage_offset: int) -> None:
                units = min(total_units, completed_units + stage_offset)
                emit_progress(min(99, 5 + int((units / total_units) * 90)))

            success_count = 0
            attempted_maps = 0
            installed_maps: list[NormalizedMapData] = []
            html_prepared: list[tuple[str, Path, str]] = []
            installed_codenames: set[str] = set()
            
            # Temporary cache for extracted IPKs
            batch_cache = self._config.cache_directory / "_batch_temp"
            batch_cache.mkdir(parents=True, exist_ok=True)

            # V1-style parity: prepare all HTML-sourced maps first while links are fresh.
            html_candidates = [
                c
                for c in candidates
                if str(c["kind"]) in {"html", "html_jdnext"}
            ]
            if html_candidates:
                self.status.emit("Phase 1/2: Preparing HTML-sourced batch maps...")
                for idx, candidate in enumerate(html_candidates, start=1):
                    map_name = str(candidate.get("name") or Path(candidate["path"]).name)
                    if selected_lookup and map_name.lower() not in selected_lookup:
                        continue
                    # Reserve an early progress slice for HTML/JDNext preparation so UI does not appear stalled.
                    phase1_progress = 3 + int((idx / max(len(html_candidates), 1)) * 17)
                    emit_progress(min(20, phase1_progress))
                    asset_html = Path(candidate["asset"])
                    raw_nohud = candidate.get("nohud")
                    nohud_html = Path(raw_nohud) if raw_nohud else None
                    source_game = str(candidate.get("source_game") or "jdu").strip().lower() or "jdu"

                    self.status.emit(
                        f"[{idx}/{len(html_candidates)}] Downloading/Preparing HTML map {map_name}..."
                    )
                    try:
                        from jd2021_installer.extractors.web_playwright import WebPlaywrightExtractor

                        extractor = WebPlaywrightExtractor(
                            asset_html=str(asset_html),
                            nohud_html=str(nohud_html) if nohud_html else None,
                            source_game=source_game,
                            quality=self._config.video_quality,
                            config=self._config,
                        )
                        prepared_dir = extractor.extract(batch_cache)
                        html_prepared.append((map_name, prepared_dir, source_game))
                    except Exception as e:
                        logger.debug("Failed HTML prepare for %s: %s", map_name, e)
                        self.status.emit(f"Warning: Failed HTML prepare for {map_name} ({str(e)[:40]})")

                emit_progress(20)

            process_candidates = [
                c
                for c in candidates
                if str(c["kind"]) not in {"html", "html_jdnext"}
            ]

            self.status.emit("Phase 2/2: Installing prepared maps...")

            for i, candidate in enumerate(process_candidates):
                try:
                    cpath = Path(candidate["path"])
                    display_name = str(candidate.get("name") or cpath.name)
                    self.status.emit(f"[{i+1}/{len(process_candidates)}] Processing {display_name}...")
                    
                    map_dir = cpath
                    map_names_for_candidate: list[str] = []
                    is_candidate_ipk = str(candidate["kind"]) == "ipk"
                    is_candidate_fetch = str(candidate["kind"]) == "fetch"
                    if is_candidate_fetch:
                        from jd2021_installer.extractors.web_playwright import WebPlaywrightExtractor

                        map_name = str(candidate.get("name") or "").strip()
                        if selected_lookup and map_name.lower() not in selected_lookup:
                            continue

                        self.status.emit(f"[{map_name}] Fetch map data")
                        extractor = WebPlaywrightExtractor(
                            codenames=[map_name],
                            source_game=self._fetch_source,
                            quality=self._config.video_quality,
                            config=self._config,
                        )
                        map_dir = extractor.extract(batch_cache)
                        map_names_for_candidate = [map_name]
                    elif is_candidate_ipk:
                        # Extract IPK to temp dir
                        from jd2021_installer.extractors.archive_ipk import ArchiveIPKExtractor
                        # Try to get codename from IPK name for early status
                        from jd2021_installer.extractors.archive_ipk import inspect_ipk
                        maps_in_ipk = inspect_ipk(cpath)
                        ipk_name_hint = maps_in_ipk[0] if maps_in_ipk else cpath.name
                        
                        self.status.emit(f"[{ipk_name_hint}] Extracting map data...")
                        desired_codename = None
                        if selected_lookup and maps_in_ipk:
                            for discovered_name in maps_in_ipk:
                                if discovered_name.lower() in selected_lookup:
                                    desired_codename = discovered_name
                                    break

                        extractor = ArchiveIPKExtractor(cpath, desired_codename=desired_codename)
                        import shutil
                        shutil.rmtree(batch_cache, ignore_errors=True)
                        map_dir = extractor.extract(batch_cache)
                        extracted_maps = sorted(set(maps_in_ipk) | set(getattr(extractor, "bundle_maps", []) or []))
                        map_names_for_candidate = extracted_maps
                    
                    if not map_names_for_candidate:
                        songdescs = list(map_dir.rglob("*songdesc*.tpl.ckd"))
                        if songdescs:
                            map_names_for_candidate = sorted({p.parent.name for p in songdescs})
                        else:
                            map_names_for_candidate = [map_dir.name]

                    logger.info("Discovered %d map(s) in %s", len(map_names_for_candidate), cpath.name)

                    for map_name in map_names_for_candidate:
                        if selected_lookup and map_name.lower() not in selected_lookup:
                            continue
                        if map_name.lower() in installed_codenames:
                            self.status.emit(f"Warning: Duplicate map '{map_name}' detected; skipping duplicate source.")
                            continue
                        attempted_maps += 1
                        emit_map_stage(0)
                            
                        self.status.emit(f"[{map_name}] Parsing CKDs and metadata...")
                        from jd2021_installer.parsers.normalizer import normalize
                        map_data = normalize(map_dir, codename=map_name, search_root=map_dir)
                        setattr(map_data, "_is_ipk_source", is_candidate_ipk)

                        canonical_name = (map_data.codename or map_name).strip()
                        canonical_key = canonical_name.lower()
                        if canonical_key in installed_codenames:
                            self.status.emit(
                                f"Warning: Duplicate map '{canonical_name}' resolved from multiple sources; skipping duplicate install."
                            )
                            continue

                        status_value = int(getattr(map_data.song_desc, "status", _READY_STATUS_VALUE))
                        if status_value != _READY_STATUS_VALUE:
                            if self._force_unlock_locked_status:
                                map_data.song_desc.status = _READY_STATUS_VALUE
                                self.status.emit(
                                    f"[{map_data.codename}] Non-default status {status_value} detected; forcing Status={_READY_STATUS_VALUE}"
                                )
                            else:
                                self.status.emit(
                                    f"[{map_data.codename}] Non-default status {status_value} detected; preserving original status"
                                )
                        
                        emit_map_stage(1)
                        self.status.emit(f"[{map_data.codename}] Normalizing assets...")
                        
                        # V1 Parity: Persist preview assets in map cache so they remain available after batch extraction is cleared
                        map_cache = self._config.cache_directory / map_data.codename
                        map_cache.mkdir(parents=True, exist_ok=True)
                        if map_data.media.video_path and map_data.media.video_path.exists():
                            persisted_video = map_cache / map_data.media.video_path.name
                            if not persisted_video.exists():
                                shutil.copy2(map_data.media.video_path, persisted_video)
                            map_data.media.video_path = persisted_video
                        
                        if map_data.media.map_preview_video and map_data.media.map_preview_video.exists():
                            persisted_preview = map_cache / map_data.media.map_preview_video.name
                            if not persisted_preview.exists():
                                shutil.copy2(map_data.media.map_preview_video, persisted_preview)
                            map_data.media.map_preview_video = persisted_preview
                        if map_data.media.audio_path and map_data.media.audio_path.exists():
                            persisted_audio = map_cache / map_data.media.audio_path.name
                            if not persisted_audio.exists():
                                shutil.copy2(map_data.media.audio_path, persisted_audio)
                            map_data.media.audio_path = persisted_audio
                        
                        self.status.emit(f"[{map_data.codename}] Installing map...")
                        install_source_mode = ""
                        if is_candidate_fetch:
                            install_source_mode = "Fetch JDNext" if self._fetch_source == "jdnext" else "Fetch"
                        elif bool(getattr(map_data, "is_html_source", False)):
                            install_source_mode = "HTML JDNext" if bool(getattr(map_data, "is_jdnext_source", False)) else "HTML"
                        setattr(map_data, "_install_source_mode", install_source_mode)
                        self._install_map_synchronously(map_data)
                        emit_map_stage(2)
                        completed_units += 3
                        success_count += 1
                        installed_codenames.add(canonical_key)
                        installed_maps.append(map_data)
                        logger.info("Batch installed map: %s", map_data.codename)
                    
                except Exception as e:
                    cpath = Path(candidate["path"])
                    logger.debug("Failed to install map from %s: %s", cpath.name, e)
                    self.status.emit(f"Warning: Failed {cpath.name} ({str(e)[:30]})")

            # Process maps prepared from HTML folders in phase 1.
            for map_name, map_dir, source_game in html_prepared:
                try:
                    if selected_lookup and map_name.lower() not in selected_lookup:
                        continue
                    if map_name.lower() in installed_codenames:
                        self.status.emit(f"Warning: Duplicate map '{map_name}' detected; skipping duplicate source.")
                        continue
                    attempted_maps += 1
                    emit_map_stage(0)

                    self.status.emit(f"[{map_name}] Parsing CKDs and metadata...")
                    from jd2021_installer.parsers.normalizer import normalize
                    map_data = normalize(map_dir, codename=map_name, search_root=map_dir)
                    setattr(map_data, "_is_ipk_source", False)

                    canonical_name = (map_data.codename or map_name).strip()
                    canonical_key = canonical_name.lower()
                    if canonical_key in installed_codenames:
                        self.status.emit(
                            f"Warning: Duplicate map '{canonical_name}' resolved from multiple sources; skipping duplicate install."
                        )
                        continue

                    status_value = int(getattr(map_data.song_desc, "status", _READY_STATUS_VALUE))
                    if status_value != _READY_STATUS_VALUE:
                        if self._force_unlock_locked_status:
                            map_data.song_desc.status = _READY_STATUS_VALUE
                            self.status.emit(
                                f"[{map_data.codename}] Non-default status {status_value} detected; forcing Status={_READY_STATUS_VALUE}"
                            )
                        else:
                            self.status.emit(
                                f"[{map_data.codename}] Non-default status {status_value} detected; preserving original status"
                            )

                    emit_map_stage(1)
                    self.status.emit(f"[{map_data.codename}] Normalizing assets...")

                    map_cache = self._config.cache_directory / map_data.codename
                    map_cache.mkdir(parents=True, exist_ok=True)
                    if map_data.media.video_path and map_data.media.video_path.exists():
                        persisted_video = map_cache / map_data.media.video_path.name
                        if not persisted_video.exists():
                            shutil.copy2(map_data.media.video_path, persisted_video)
                        map_data.media.video_path = persisted_video

                    if map_data.media.map_preview_video and map_data.media.map_preview_video.exists():
                        persisted_preview = map_cache / map_data.media.map_preview_video.name
                        if not persisted_preview.exists():
                            shutil.copy2(map_data.media.map_preview_video, persisted_preview)
                        map_data.media.map_preview_video = persisted_preview
                    if map_data.media.audio_path and map_data.media.audio_path.exists():
                        persisted_audio = map_cache / map_data.media.audio_path.name
                        if not persisted_audio.exists():
                            shutil.copy2(map_data.media.audio_path, persisted_audio)
                        map_data.media.audio_path = persisted_audio

                    self.status.emit(f"[{map_data.codename}] Installing map...")
                    install_source_mode = "HTML JDNext" if source_game == "jdnext" else "HTML"
                    setattr(map_data, "_install_source_mode", install_source_mode)
                    self._install_map_synchronously(map_data)
                    emit_map_stage(2)
                    completed_units += 3
                    success_count += 1
                    installed_codenames.add(canonical_key)
                    installed_maps.append(map_data)
                    logger.info("Batch installed HTML map: %s", map_data.codename)
                except Exception as e:
                    logger.debug("Failed to install HTML map %s: %s", map_name, e)
                    self.status.emit(f"Warning: Failed {map_name} ({str(e)[:30]})")

            import shutil
            shutil.rmtree(batch_cache, ignore_errors=True)

            emit_progress(100)
            total_maps = attempted_maps if attempted_maps > 0 else total
            self.status.emit(f"Batch install complete. {success_count}/{total_maps} maps installed.")
            self.finished_with_data.emit(installed_maps)
            self.finished.emit(True)

        except Exception as e:
            log_exception_for_profile(logger, "BatchInstallWorker failed", e)
            self.error.emit(str(e))
            self.finished.emit(False)
            
    def _install_map_synchronously(self, map_data: NormalizedMapData) -> None:
        """Execute the same steps as InstallMapWorker.run() synchronously."""
        def callback(msg: str):
            prefix = f"[{map_data.codename}] "
            self.status.emit(prefix + msg)

        source_mode = str(getattr(map_data, "_install_source_mode", "") or "")
        install_map_to_game(
            map_data,
            self._target_dir,
            self._config,
            source_mode=source_mode,
            status_callback=callback,
        )



def pre_install_cleanup(
    game_dir: Path, 
    codename: str, 
    status_callback: Optional[Callable[[str], None]] = None
) -> None:
    """Clean up any previous installation of this map, including cooked cache."""
    import shutil
    
    # Normalize game_dir
    while game_dir.name.lower() in ("world", "data"):
        game_dir = game_dir.parent

    if status_callback:
        status_callback(f"Cleaning up previous installation of {codename}...")

    # 1. Delete main map directory (support common case variants)
    map_dir_candidates = [
        game_dir / "data" / "world" / "maps" / codename,
        game_dir / "data" / "World" / "MAPS" / codename,
    ]
    seen_paths: set[str] = set()
    for map_dir in map_dir_candidates:
        key = str(map_dir).lower()
        if key in seen_paths:
            continue
        seen_paths.add(key)
        if map_dir.exists():
            logger.info("Deleting previous map directory: %s", map_dir)
            shutil.rmtree(map_dir, ignore_errors=True)

    # 2. Delete cooked cache directories
    # V1 Parity: engine cache paths are strictly lowercase
    name_lower = codename.lower()
    cache_base = game_dir / "data" / "cache" / "itf_cooked" / "pc" / "world" / "maps" / name_lower
    
    cache_paths = [
        cache_base,
        cache_base.with_name(cache_base.name + "_autodance"),
        cache_base.with_name(cache_base.name + "_cine"),
        cache_base / "audio"
    ]

    for cp in cache_paths:
        if cp.exists():
            logger.info("Deleting cache: %s", cp.name)
            shutil.rmtree(cp, ignore_errors=True)

    # 3. Unregister from SkuScene
    from jd2021_installer.installers.sku_scene import unregister_map
    unregister_map(game_dir, codename)


@dataclass
class UninstallResult:
    codename: str
    removed_map_dirs: list[Path]
    removed_cache_dirs: list[Path]
    sku_unregistered: bool
    removed_installer_cache: bool


@dataclass
class UninstallBatchResult:
    selected_count: int
    changed_codenames: list[str]
    failed: list[str]
    no_changes: list[str]


def uninstall_map_from_game(
    game_dir: Path,
    codename: str,
    config: Optional[AppConfig] = None,
    status_callback: Optional[Callable[[str], None]] = None,
) -> UninstallResult:
    """Uninstall a map from the game and return verifiable cleanup results."""
    normalized_game_dir = game_dir
    while normalized_game_dir.name.lower() in ("world", "data"):
        normalized_game_dir = normalized_game_dir.parent

    name_lower = codename.lower()
    map_dir_candidates = [
        normalized_game_dir / "data" / "world" / "maps" / codename,
        normalized_game_dir / "data" / "World" / "MAPS" / codename,
    ]
    deduped_map_dirs: list[Path] = []
    seen_map_dirs: set[str] = set()
    for map_dir in map_dir_candidates:
        key = str(map_dir).lower()
        if key in seen_map_dirs:
            continue
        seen_map_dirs.add(key)
        deduped_map_dirs.append(map_dir)

    cache_base = normalized_game_dir / "data" / "cache" / "itf_cooked" / "pc" / "world" / "maps" / name_lower
    cache_paths = [
        cache_base,
        cache_base.with_name(cache_base.name + "_autodance"),
        cache_base.with_name(cache_base.name + "_cine"),
        cache_base / "audio",
    ]

    map_dirs_before = [p for p in deduped_map_dirs if p.exists()]
    cache_dirs_before = [p for p in cache_paths if p.exists()]

    from jd2021_installer.installers.sku_scene import is_registered

    sku_registered_before = is_registered(normalized_game_dir, codename)

    installer_cache_dir: Optional[Path] = None
    installer_cache_before = False
    if config is not None:
        installer_cache_dir = config.cache_directory / codename
        installer_cache_before = installer_cache_dir.exists()

    pre_install_cleanup(normalized_game_dir, codename, status_callback=status_callback)

    removed_installer_cache = False
    if installer_cache_dir is not None and installer_cache_before:
        if status_callback:
            status_callback(f"Removing installer cache for {codename}...")
        shutil.rmtree(installer_cache_dir, ignore_errors=True)
        removed_installer_cache = not installer_cache_dir.exists()
        if not removed_installer_cache:
            raise RuntimeError(f"Installer cache could not be removed: {installer_cache_dir}")

    for path in map_dirs_before:
        if path.exists():
            raise RuntimeError(f"Map directory still exists after uninstall: {path}")

    for path in cache_dirs_before:
        if path.exists():
            raise RuntimeError(f"Cooked cache directory still exists after uninstall: {path}")

    sku_unregistered = False
    if sku_registered_before:
        sku_unregistered = not is_registered(normalized_game_dir, codename)
        if not sku_unregistered:
            raise RuntimeError(f"Map is still registered in SkuScene after uninstall: {codename}")

    return UninstallResult(
        codename=codename,
        removed_map_dirs=[p for p in map_dirs_before if not p.exists()],
        removed_cache_dirs=[p for p in cache_dirs_before if not p.exists()],
        sku_unregistered=sku_unregistered,
        removed_installer_cache=removed_installer_cache,
    )


class UninstallMapsWorker(QObject):
    """Uninstall one or more maps in a background thread."""

    progress = pyqtSignal(int)
    status = pyqtSignal(str)
    error = pyqtSignal(str)
    finished = pyqtSignal(object)  # UninstallBatchResult

    def __init__(
        self,
        game_dir: Path,
        selected_codenames: list[str],
        config: Optional[AppConfig] = None,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._game_dir = game_dir
        self._selected_codenames = list(selected_codenames)
        self._config = config

    def run(self) -> None:
        try:
            total = len(self._selected_codenames)
            if total == 0:
                self.finished.emit(
                    UninstallBatchResult(
                        selected_count=0,
                        changed_codenames=[],
                        failed=[],
                        no_changes=[],
                    )
                )
                return

            failed: list[str] = []
            changed_lowers: set[str] = set()
            no_changes: list[str] = []

            for idx, codename in enumerate(self._selected_codenames, start=1):
                progress_value = int(((idx - 1) / total) * 100)
                self.progress.emit(progress_value)
                self.status.emit(f"[{codename}] Uninstalling map files...")

                try:
                    result = uninstall_map_from_game(
                        self._game_dir,
                        codename,
                        config=self._config,
                        status_callback=lambda msg, code=codename: self.status.emit(f"[{code}] {msg}"),
                    )

                    index_removed = remove_entry(codename)
                    if index_removed:
                        self.status.emit(f"[{codename}] Removed from readjust index.")

                    changed = bool(
                        result.removed_map_dirs
                        or result.removed_cache_dirs
                        or result.sku_unregistered
                        or result.removed_installer_cache
                        or index_removed
                    )
                    if changed:
                        changed_lowers.add(codename.lower())
                        self.status.emit(
                            (
                                f"[{codename}] Uninstall complete "
                                f"(map_dirs={len(result.removed_map_dirs)}, "
                                f"cooked_cache={len(result.removed_cache_dirs)}, "
                                f"sku_unregistered={'yes' if result.sku_unregistered else 'no'}, "
                                f"installer_cache={'yes' if result.removed_installer_cache else 'no'}, "
                                f"index_removed={'yes' if index_removed else 'no'})."
                            )
                        )
                    else:
                        no_changes.append(codename)
                        self.status.emit(
                            f"[{codename}] No uninstallable artifacts found (already removed or never installed)."
                        )

                except Exception as exc:
                    logger.exception("Failed to uninstall map '%s': %s", codename, exc)
                    failed.append(f"{codename}: {exc}")
                    self.status.emit(f"[{codename}] ERROR: {exc}")

            self.progress.emit(100)
            self.finished.emit(
                UninstallBatchResult(
                    selected_count=total,
                    changed_codenames=sorted(changed_lowers),
                    failed=failed,
                    no_changes=no_changes,
                )
            )

        except Exception as exc:
            log_exception_for_profile(logger, "UninstallMapsWorker failed", exc)
            self.error.emit(str(exc))


def install_map_to_game(
    map_data: NormalizedMapData, 
    game_dir: Path, 
    config: Optional[AppConfig],
    source_mode: str = "",
    status_callback: Optional[Callable[[str], None]] = None,
    progress_callback: Optional[Callable[[int], None]] = None
) -> None:
    """Core installation logic: files → game directory."""
    codename = map_data.codename

    def _is_jdnext_source_map() -> bool:
        mode_low = (source_mode or "").lower()
        if "jdnext" in mode_low:
            return True

        source_dir = map_data.source_dir
        if source_dir and source_dir.exists():
            assets_html = source_dir / "assets.html"
            if assets_html.exists():
                try:
                    content = assets_html.read_text(encoding="utf-8", errors="ignore").lower()
                    if "/jdnext/maps/" in content or "server:jdnext" in content:
                        return True
                except OSError:
                    pass

        video_path = map_data.media.video_path
        if video_path:
            name = video_path.name.lower()
            if re.match(r"^video_(ultra|high|mid|low)\.(hd|vp8|vp9)\.webm$", name):
                return True

        return False

    source_is_jdnext = _is_jdnext_source_map()

    def _mainsequence_has_any_clip_entries(target_root: Path, map_code: str) -> bool:
        tape_candidates = [
            target_root / "Cinematics" / f"{map_code}_MainSequence.tape",
            target_root / "cinematics" / f"{map_code}_MainSequence.tape",
        ]
        tape_path = next((p for p in tape_candidates if p.exists()), None)
        if tape_path is None:
            return False
        try:
            content = tape_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        # Detect at least one clip entry object inside Tape.Clips table.
        return re.search(r"Clips\s*=\s*\{\s*\{", content, flags=re.DOTALL) is not None
    
    # 0. Pre-install cleanup
    pre_install_cleanup(game_dir, codename, status_callback)
    if progress_callback: progress_callback(5)

    # Normalize the game directory root in case the user selected a subfolder
    while game_dir.name.lower() in ("world", "data"):
        game_dir = game_dir.parent

    # Resolve target directory: game_dir / data / world / maps / <codename>
    map_target = game_dir / "data" / "world" / "maps" / codename
    map_target.mkdir(parents=True, exist_ok=True)

    # 1. & 2. Write UbiArt config files and process audio WITH initial offsets
    # Calculate offset in seconds based on what was normalized
    initial_a_offset = map_data.sync.audio_ms / 1000.0
    
    # Note: reprocess_audio handles convert_audio, generate_intro_amb, and write_game_files
    # We'll need to manually emit status for its sub-steps if we want true granularity
    # but for now we'll wrap it.
    
    if status_callback: status_callback("Decoding XMA2 audio...")
    if progress_callback: progress_callback(20)
    
    if status_callback: status_callback("Converting audio (pad/trim)...")
    if progress_callback: progress_callback(30)
    
    if status_callback: status_callback("Generating intro AMB...")
    if progress_callback: progress_callback(40)
    
    reprocess_audio(map_data, map_target, initial_a_offset, config)

    # Fetch/HTML parity ticket: boost installed gameplay audio by +8 dB (JDU only).
    mode_low = (source_mode or "").lower()
    if ("fetch" in mode_low or "html" in mode_low) and not _is_jdnext_source_map():
        if status_callback: status_callback("Applying +8dB JDU audio boost...")
        if progress_callback: progress_callback(45)
        from jd2021_installer.installers.media_processor import apply_audio_gain

        audio_wav = map_target / "audio" / f"{codename}.wav"
        if audio_wav.exists():
            apply_audio_gain(audio_wav, gain_db=8.0, config=config)
        else:
            logger.debug("Expected gameplay WAV missing for gain boost: %s", audio_wav)

    # 2b. Copy Video
    media = map_data.media
    if media.video_path and media.video_path.exists():
        if status_callback: status_callback("Copying video files...")
        if progress_callback: progress_callback(50)
        from jd2021_installer.installers.media_processor import copy_video
        video_dst = map_target / "videoscoach" / f"{codename}.webm"
        copy_video(
            media.video_path,
            video_dst,
            config=config,
        )
        if media.map_preview_video and media.map_preview_video.exists():
            preview_dst = map_target / "videoscoach" / f"{codename}_MapPreview.webm"
            copy_video(media.map_preview_video, preview_dst, config=config)

    # 3. Copy/Rename MenuArt assets (Cover, Banner, Coach, etc.)
    textures_dir = map_target / "menuart" / "textures"
    textures_dir.mkdir(parents=True, exist_ok=True)
    
    # Map of MapMedia fields to V1 canonical art suffixes
    art_map = {
        "cover_generic_path": "cover_generic",
        "cover_online_path": "cover_online",
        "banner_bkg_path": "banner_bkg",
        "map_bkg_path": "map_bkg",
        "cover_albumbkg_path": "cover_albumbkg",
        "cover_albumcoach_path": "cover_albumcoach",
    }
    
    for field_name, art_suffix in art_map.items():
        src_path = getattr(media, field_name, None)
        if src_path and src_path.exists():
            # Canonical name: {codename}_{suffix}{ext}
            # We preserve .ckd suffix if present so texture_decoder can pick it up
            suffix = src_path.suffix.lower()
            if src_path.name.lower().endswith(".tga.ckd"):
                ext = ".tga.ckd"
            elif src_path.name.lower().endswith(".png.ckd"):
                ext = ".png.ckd"
            else:
                ext = suffix
                
            dst_name = f"{codename}_{art_suffix}{ext}"
            shutil.copy2(src_path, textures_dir / dst_name)
    
    def _extract_coach_index(path: Path) -> int:
        match = re.search(r"coach[_-]?(\d+)", path.name.lower())
        return int(match.group(1)) if match else 0

    # Coaches are now separated into main and phone lists in normalize_sync.
    # We use the index from the filename to ensure correct mapping even if some are missing.
    fallback_idx = 1
    used_indices: set[int] = set()
    for coach_img in media.coach_images:
        if coach_img.exists():
            idx = _extract_coach_index(coach_img)
            if idx == 0:
                while fallback_idx in used_indices:
                    fallback_idx += 1
                idx = fallback_idx
            used_indices.add(idx)
            
            suffix = coach_img.suffix.lower()
            if coach_img.name.lower().endswith(".tga.ckd"):
                ext = ".tga.ckd"
            else:
                ext = suffix
            dst_name = f"{codename}_coach_{idx}{ext}"
            shutil.copy2(coach_img, textures_dir / dst_name)
            
    for phone_img in media.coach_phone_images:
        if phone_img.exists():
            idx = _extract_coach_index(phone_img)
            if idx == 0:
                continue
            
            ext = ".png" # Phone assets are usually PNG
            if phone_img.name.lower().endswith(".tga.ckd"):
                ext = ".tga.ckd"
            elif phone_img.suffix.lower() == ".png" or phone_img.suffix.lower() == ".jpg":
                ext = phone_img.suffix.lower()

            dst_name = f"{codename}_coach_{idx}_phone{ext}"
            shutil.copy2(phone_img, textures_dir / dst_name)


    # 4. Physical Converters (Tape, Texture, Ambient)
    if map_data.source_dir and map_data.source_dir.exists():
        picto_src = map_data.media.pictogram_dir
        if not picto_src or not picto_src.exists():
            picto_src = map_data.source_dir / "pictos"

        if picto_src and picto_src.exists():
            menuart_tokens = ("cover_", "banner", "map_bkg", "coach_")
            for asset in picto_src.rglob("*"):
                if not asset.is_file():
                    continue
                name_low = asset.name.lower()
                if not any(token in name_low for token in menuart_tokens):
                    continue
                ext = asset.suffix.lower()
                if ext not in (".ckd", ".tga", ".png", ".jpg", ".jpeg"):
                    continue
                target_name = asset.name
                if not target_name.lower().startswith(f"{codename.lower()}_"):
                    target_name = f"{codename}_{target_name}"
                shutil.copy2(asset, textures_dir / target_name)

        if status_callback: status_callback("Converting dance tapes...")
        if progress_callback: progress_callback(60)
        from jd2021_installer.installers.tape_converter import auto_convert_tapes
        auto_convert_tapes(map_data.source_dir, map_target, codename)
        
        # We don't have separate steps for Karaoke/Cinematic yet in logic, but status can reflect them
        if status_callback: status_callback("Converting karaoke tapes...")
        if status_callback: status_callback("Converting cinematic tapes...")
        
        if status_callback: status_callback("Processing ambient sounds...")
        if progress_callback: progress_callback(70)
        from jd2021_installer.installers.ambient_processor import process_ambient_directory

        source_is_html = bool(getattr(map_data, "is_html_source", False))
        if not source_is_html and map_data.source_dir and map_data.source_dir.exists():
            source_is_html = any(map_data.source_dir.glob("*.html")) or any(map_data.source_dir.glob("**/assets.html"))

        normalize_intro_clip = source_is_jdnext
        if not normalize_intro_clip and not _mainsequence_has_any_clip_entries(map_target, codename):
            logger.warning(
                "MainSequence has no clip entries for '%s'; enabling intro clip recovery injection.",
                codename,
            )
            normalize_intro_clip = True

        process_ambient_directory(
            map_data.source_dir,
            map_target,
            codename,
            attempt_enabled=True,
            normalize_intro_clip=normalize_intro_clip,
        )
        
        if status_callback: status_callback("Decoding MenuArt textures...")
        if progress_callback: progress_callback(80)
        from jd2021_installer.installers.texture_decoder import decode_menuart_textures, decode_pictograms

        menuart_sources = _collect_menuart_texture_sources(map_data.source_dir, codename)
        installed_companions = _install_menuart_companion_assets(menuart_sources, map_target)
        if installed_companions:
            logger.debug(
                "Installed %d MenuArt companion asset(s) (.act/.isc) from source payloads.",
                installed_companions,
            )
        
        # V1 Parity: Decode textures directly in the target directory 
        # to handle loose assets copied from Fetch/HTML mode.
        decoded_menuart = decode_menuart_textures(textures_dir, textures_dir)

        if map_data.source_dir and map_data.source_dir.exists():
            for menuart_src in menuart_sources:
                decoded_menuart += decode_menuart_textures(menuart_src, textures_dir)

        if decoded_menuart == 0:
            logger.warning(
                "No MenuArt textures decoded for '%s'. "
                "Source may not include texture payloads.",
                codename,
            )

        if _is_jdnext_source_map():
            synthesized_albumcoach = _ensure_jdnext_albumcoach_texture_from_coach(map_target, codename)
            if synthesized_albumcoach:
                logger.debug(
                    "Synthesized missing albumcoach texture from coach_1 for JDNext map '%s'.",
                    codename,
                )
            faded_coaches = _apply_jdnext_bottom_alpha_fade_if_needed(map_target, codename)
            if faded_coaches:
                logger.debug(
                    "Applied JDNext bottom alpha fade to %d coach texture(s) for '%s'.",
                    faded_coaches,
                    codename,
                )

            
        # V1 Parity: Validate and heal MenuArt (case-fix + RGBA re-save)
        from jd2021_installer.installers.media_processor import process_menu_art
        process_menu_art(map_target, codename)

        ensured_acts = _ensure_optional_menuart_actors_from_textures(map_target, codename)
        if ensured_acts:
            logger.debug(
                "Created %d optional MenuArt actor file(s) from discovered textures for '%s'.",
                ensured_acts,
                codename,
            )
            
        if status_callback: status_callback("Decoding pictograms...")
        decoded_pictos = 0
        # JDNext fallback canvas: solo maps use 512x512, multi-coach maps use 512x354.
        # Decoder preserves any width-512 pictos as-is and only canvases non-512 widths.
        picto_canvas_size = None
        if _is_jdnext_source_map():
            coach_count = int(getattr(getattr(map_data, "song_desc", None), "num_coach", 1) or 1)
            picto_canvas_size = (512, 512) if coach_count <= 1 else (512, 354)
        if map_data.source_dir and map_data.source_dir.exists():
            for picto_dir in _collect_pictogram_sources(map_data.source_dir, codename, preferred=picto_src):
                decoded_pictos += decode_pictograms(
                    picto_dir,
                    map_target / "timeline" / "pictos",
                    canvas_size=picto_canvas_size,
                )
        elif picto_src and picto_src.exists():
            decoded_pictos += decode_pictograms(
                picto_src,
                map_target / "timeline" / "pictos",
                canvas_size=picto_canvas_size,
            )

        if decoded_pictos == 0:
            logger.warning(
                "No pictograms decoded for '%s'. Source may not include pictogram textures.",
                codename,
            )

    # 5. Moves
    if media.moves_dir and media.moves_dir.exists():
        if status_callback: status_callback("Integrating move data...")
        if progress_callback: progress_callback(85)
        from jd2021_installer.installers.media_processor import copy_moves
        copy_moves(media.moves_dir, map_target, skip_gestures=_is_jdnext_source_map())

    # 5b. Autodance + stape payloads (V1 step_11 parity)
    if map_data.has_autodance and map_data.source_dir and map_data.source_dir.exists():
        if status_callback: status_callback("Extracting moves and autodance...")
        from jd2021_installer.installers.autodance_processor import (
            process_autodance_directory,
            process_stape_file,
        )
        process_autodance_directory(map_data.source_dir, map_target, codename)
        process_stape_file(map_data.source_dir, map_target, codename)
    elif map_data.source_dir and map_data.source_dir.exists():
        # Some maps ship stape without autodance blocks.
        from jd2021_installer.installers.autodance_processor import process_stape_file
        process_stape_file(map_data.source_dir, map_target, codename)

    # 6. Register map in SkuScene ISC
    if status_callback: status_callback("Registering in SkuScene...")
    if progress_callback: progress_callback(95)
    try:
        from jd2021_installer.installers.sku_scene import register_map
        register_map(game_dir, codename)
    except Exception as e:
        logger.debug("SkuScene registration failed (non-fatal): %s", e)

    if status_callback: status_callback("Finalizing offsets...")
    if progress_callback: progress_callback(100)
