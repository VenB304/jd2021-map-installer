"""Manual Extractor for JD2021 Map Installer.

Assembles an extraction directory from a collection of manually-specified
local file paths (audio, video, musictrack, tapes, assets) provided by
the user via the UI.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Dict, Optional

from jd2021_installer.core.exceptions import DownloadError
from jd2021_installer.extractors.base import BaseExtractor

logger = logging.getLogger("jd2021.extractors.manual")


class ManualExtractor(BaseExtractor):
    """Assembles a map directory from manually specified paths."""

    @staticmethod
    def _has_ipk_structure(root: Path) -> bool:
        if (root / "world" / "maps").is_dir():
            return True
        world_root = root / "world"
        if world_root.is_dir():
            return any(
                d.is_dir() and d.name.lower().startswith("jd") and d.name[2:].isdigit()
                for d in world_root.iterdir()
            )
        return False

    def _warn(self, message: str) -> None:
        self._warnings.append(message)
        logger.warning(message)

    def get_warnings(self) -> list[str]:
        return list(self._warnings)

    def _detect_musictrack(self, root: Path) -> bool:
        codename_lower = self._codename.lower() if self._codename else ""
        for pattern in ("*musictrack*.tpl.ckd", "*musictrack*.trk", "*.trk"):
            for p in root.rglob(pattern):
                if not p.is_file():
                    continue
                if not codename_lower:
                    return True
                parts_lower = [part.lower() for part in p.parts]
                if codename_lower in parts_lower or p.name.lower().startswith(codename_lower):
                    return True
        return False

    def _find_html_pair(self, root: Path) -> tuple[bool, bool]:
        asset = False
        nohud = False
        try:
            html_files = sorted(
                [
                    p
                    for p in root.iterdir()
                    if p.is_file() and p.suffix.lower() in {".html", ".htm"}
                ],
                key=lambda p: p.name.lower(),
            )
        except OSError:
            return False, False

        for html in html_files:
            lower = html.name.lower()
            if "nohud" in lower:
                nohud = True
            elif "asset" in lower:
                asset = True

        if len(html_files) >= 2:
            asset = True
            nohud = True

        return asset, nohud

    def _validate_manual_explicit_inputs(self, root: Optional[Path]) -> None:
        """Validate explicit manual selections before assembling output."""
        provided_files = {k: Path(v) for k, v in self._files.items() if v}
        provided_dirs = {k: Path(v) for k, v in self._dirs.items() if v}

        if not provided_files and not provided_dirs:
            return

        # Required files mirror V1 manual-v2 behavior.
        required_labels = {
            "audio": "Audio file",
            "video": "Video (.webm)",
            "mtrack": "Musictrack CKD / .trk",
        }
        missing_required: list[str] = []

        explicit_audio_ok = bool(provided_files.get("audio") and provided_files["audio"].is_file())
        explicit_video_ok = bool(provided_files.get("video") and provided_files["video"].is_file())
        explicit_mtrack_ok = bool(provided_files.get("mtrack") and provided_files["mtrack"].is_file())

        root_audio_ok = False
        root_video_ok = False
        root_mtrack_ok = False
        if root and root.is_dir():
            root_audio_ok, root_video_ok = self._resolve_codename_media(root)
            root_mtrack_ok = self._detect_musictrack(root)

        if not (explicit_audio_ok or root_audio_ok):
            missing_required.append(required_labels["audio"])
        if not (explicit_video_ok or root_video_ok):
            missing_required.append(required_labels["video"])
        if not (explicit_mtrack_ok or root_mtrack_ok):
            missing_required.append(required_labels["mtrack"])

        if missing_required:
            raise DownloadError(
                "Manual mode missing required inputs: " + ", ".join(missing_required) + "."
            )

        optional_file_labels = {
            "sdesc": "Songdesc CKD",
            "dtape": "Dance tape",
            "ktape": "Karaoke tape",
            "mseq": "Mainsequence tape",
        }
        optional_dir_labels = {
            "moves": "Moves directory",
            "pictos": "Pictos directory",
            "menuart": "MenuArt directory",
            "amb": "AMB directory",
        }

        for key, p in provided_files.items():
            if p.is_file():
                continue
            if key in required_labels:
                self._warn(f"Manual override for {required_labels[key]} was not found and will be ignored: {p}")
            else:
                label = optional_file_labels.get(key, key)
                self._warn(f"Manual optional file missing ({label}): {p}")

        for key, p in provided_dirs.items():
            if p.is_dir():
                continue
            label = optional_dir_labels.get(key, key)
            self._warn(f"Manual optional directory missing ({label}): {p}")

    def _resolve_root_dir(self, root: Path) -> Path:
        """Resolve an IPK-structured root to the inner map directory.

        If the root contains ``world/maps/<codename>/``, return that inner
        path so downstream copytree operations don't create broken nested
        structures like ``output/Codename/world/maps/Codename/``.

        For non-IPK roots (HTML-downloaded folders, flat layouts), the
        original root is returned unchanged.
        """
        if not self._has_ipk_structure(root):
            return root

        codename = self._codename
        if not codename:
            return root

        # Try world/maps/<codename>/ first (standard layout)
        world_maps = root / "world" / "maps"
        if world_maps.is_dir():
            # Case-insensitive match
            for d in world_maps.iterdir():
                if d.is_dir() and d.name.lower() == codename.lower():
                    logger.debug(
                        "Resolved IPK root to inner map dir: %s -> %s",
                        root, d,
                    )
                    return d

        # Try world/jd20XX/<codename>/ (legacy bundle layout)
        world_root = root / "world"
        if world_root.is_dir():
            for jd_dir in world_root.iterdir():
                if not jd_dir.is_dir():
                    continue
                name = jd_dir.name.lower()
                if not name.startswith("jd") or not name[2:].isdigit():
                    continue
                for d in jd_dir.iterdir():
                    if d.is_dir() and d.name.lower() == codename.lower():
                        logger.debug(
                            "Resolved legacy IPK root to inner map dir: %s -> %s",
                            root, d,
                        )
                        return d

        return root

    def is_ipk_source(self) -> bool:
        """True when manual mode is operating on unpacked IPK content."""
        if self._source_type in {"ipk", "mixed"}:
            return True
        if self._source_type == "auto" and self._root_dir and self._root_dir.is_dir():
            return self._has_ipk_structure(self._root_dir)
        return False

    def _validate_ipk_root(self, root: Path) -> None:
        """Validate codename/root consistency for manual IPK roots."""
        if not self.is_ipk_source():
            return

        candidates = set()

        world_maps = root / "world" / "maps"
        if world_maps.is_dir():
            candidates.update(d.name for d in world_maps.iterdir() if d.is_dir())

        # V1 parity: legacy bundles may use world/jd20XX/<codename>/
        world_root = root / "world"
        if world_root.is_dir():
            for jd_dir in world_root.iterdir():
                if not jd_dir.is_dir():
                    continue
                name = jd_dir.name.lower()
                if not name.startswith("jd"):
                    continue
                if not name[2:].isdigit():
                    continue
                candidates.update(d.name for d in jd_dir.iterdir() if d.is_dir())

        candidates = sorted(candidates)
        if not candidates:
            return

        self.bundle_maps = candidates
        self._is_multi_map = len(candidates) > 1

        if not self._codename:
            self._codename = candidates[0]
            if self._is_multi_map:
                logger.warning(
                    "Manual IPK source contains multiple maps; auto-selected first candidate '%s'.",
                    self._codename,
                )
            else:
                logger.debug("Inferred manual IPK codename from root: %s", self._codename)
            return

        lower_candidates = {c.lower() for c in candidates}
        if self._codename.lower() not in lower_candidates:
            fallback = candidates[0]
            logger.warning(
                "Manual IPK codename '%s' does not match discovered maps (%s); using '%s'.",
                self._codename,
                ", ".join(candidates),
                fallback,
            )
            self._codename = fallback

    def _resolve_codename_media(self, root: Path) -> tuple[bool, bool]:
        """Return flags for audio/video presence scoped to codename when possible."""
        codename_lower = self._codename.lower() if self._codename else ""

        has_audio = False
        has_video = False
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            name_low = p.name.lower()
            path_low = str(p).lower().replace('\\', '/')

            if "audiopreview" in name_low:
                continue

            if codename_lower:
                in_codename_scope = codename_lower in [part.lower() for part in p.parts]
                if not in_codename_scope and not name_low.startswith(codename_lower):
                    continue

            if name_low.endswith((".ogg", ".wav", ".wav.ckd")):
                if "/amb/" in path_low or "/autodance/" in path_low:
                    continue
                if name_low.startswith("amb_"):
                    continue
                has_audio = True

            if name_low.endswith(".webm") and "mappreview" not in name_low and "videopreview" not in name_low:
                has_video = True

            if has_audio and has_video:
                break

        return has_audio, has_video

    def _validate_root_source_readiness(self, root: Path) -> None:
        """Eagerly validate root-only manual mode to match V1 readiness behavior."""
        has_audio, has_video = self._resolve_codename_media(root)
        has_musictrack = self._detect_musictrack(root)
        missing: list[str] = []

        has_ipk_structure = self._has_ipk_structure(root)

        has_asset, has_nohud = self._find_html_pair(root)

        if self._source_type in {"mixed", "auto"}:
            if not (has_ipk_structure or (has_asset and has_nohud)):
                missing.append("Manual source needs either world/maps/ or an assets.html + nohud.html pair.")
        elif self.is_ipk_source():
            if not has_ipk_structure:
                missing.append("Unpacked IPK folder must contain world/maps/ or world/jd20XX/.")
        else:
            if not (has_asset and has_nohud):
                missing.append("Downloaded assets mode requires assets.html or nohud.html.")

        if not has_audio:
            missing.append("Audio (.ogg/.wav/.wav.ckd) not found in source folder.")
        if not has_video:
            missing.append("Gameplay video (.webm) not found in source folder.")
        if not has_musictrack:
            missing.append("Musictrack CKD / .trk is required (fatal for config generation).")

        if missing:
            raise DownloadError(" ".join(missing))

    def __init__(
        self,
        codename: str,
        source_type: str = "auto",
        root_dir: Optional[str] = None,
        files: Optional[Dict[str, str]] = None,
        dirs: Optional[Dict[str, str]] = None,
    ) -> None:
        """Initialize the extractor.

        Args:
            codename: The name of the map.
            root_dir: Optional base directory if files are already bundled.
            files:    Dict of logical name → absolute file path (e.g. dict(audio="...", dtape="...")).
            dirs:     Dict of logical name → absolute directory path for assets (moves, pictos, etc).
        """
        inferred_codename = codename.strip() if codename else ""
        if not inferred_codename and root_dir:
            inferred_codename = Path(root_dir).name.strip()
        self._codename = inferred_codename
        self._source_type = source_type.strip().lower() if source_type else "auto"
        self._root_dir = Path(root_dir) if root_dir else None
        self._files = files or {}
        self._dirs = dirs or {}
        self._warnings: list[str] = []
        self.bundle_maps: list[str] = []
        self._is_multi_map = False

    def get_codename(self) -> Optional[str]:
        return self._codename or None

    def get_source_dir(self) -> Optional[Path]:
        """Return the user-selected root directory as the primary source."""
        return self._root_dir

    # Map file override types to their canonical subdirectory within the
    # assembled extraction output.  Files are placed here so the normalizer
    # and install pipeline find them in the expected locations.
    _FILE_SUBDIRS: dict[str, str] = {
        "audio": ".",
        "video": ".",
        "mtrack": ".",
        "sdesc": ".",
        "dtape": ".",
        "ktape": ".",
        "mseq": ".",
        # JDU MenuArt individual textures → menuart/textures/
        "jdu_menuart_cover_generic": "menuart/textures",
        "jdu_menuart_cover_online": "menuart/textures",
        "jdu_menuart_banner": "menuart/textures",
        "jdu_menuart_banner_bkg": "menuart/textures",
        "jdu_menuart_map_bkg": "menuart/textures",
        "jdu_menuart_cover_albumcoach": "menuart/textures",
        "jdu_menuart_cover_albumbkg": "menuart/textures",
        "jdu_menuart_coach1": "menuart/textures",
        "jdu_menuart_coach2": "menuart/textures",
        "jdu_menuart_coach3": "menuart/textures",
        "jdu_menuart_coach4": "menuart/textures",
    }

    def extract(self, output_dir: Path) -> Path:
        """Copy manual files to the extraction output_dir.

        If a root folder was provided and no granular files were given,
        we can simply return the root folder as the extracted data.
        Otherwise, we assemble a clean directory.
        """
        # Resolve root: for IPK-structured folders this resolves into
        # the inner world/maps/<codename>/ directory to prevent nesting.
        resolved_root = None
        if self._root_dir and self._root_dir.is_dir():
            # Validate IPK root on the *original* root before resolution
            # so bundle_maps discovery operates on the full tree.
            self._validate_ipk_root(self._root_dir)
            resolved_root = self._resolve_root_dir(self._root_dir)

        # If there are NO explicit files/dirs configured but there IS a root,
        # just yield the root directly as the extraction source for the normalizer.
        if resolved_root and not any(self._files.values()) and not any(self._dirs.values()):
            # For root-only mode, pass the *original* root (not resolved)
            # so the normalizer's own _resolve_map_source_dir can handle
            # both IPK and flat layouts consistently.
            self._validate_root_source_readiness(self._root_dir)
            logger.info(
                "Manual extraction using root dir directly (%s): %s",
                self._source_type,
                self._root_dir,
            )
            return self._root_dir

        if not self._codename:
            raise DownloadError("Codename is required for manual mode.")

        self._validate_manual_explicit_inputs(resolved_root)

        map_output_dir = output_dir / self._codename
        map_output_dir.mkdir(parents=True, exist_ok=True)

        logger.debug("Assembling manual files into %s (source_type=%s)", map_output_dir, self._source_type)

        # Base case: copy the *resolved* root contents (inner map dir
        # for IPK layouts, or the original root for flat layouts).
        # This prevents the old nesting bug where copytree(outer_root)
        # would create output/Codename/world/maps/Codename/.
        if resolved_root:
            logger.debug("Copying contents of resolved root dir %s", resolved_root)
            shutil.copytree(resolved_root, map_output_dir, dirs_exist_ok=True)

        # Copy override files into their canonical subdirectories.
        for ftype, path_str in self._files.items():
            if not path_str:
                continue
            src = Path(path_str)
            if src.is_file():
                subdir = self._FILE_SUBDIRS.get(ftype, ".")
                dest_parent = map_output_dir / subdir if subdir != "." else map_output_dir
                dest_parent.mkdir(parents=True, exist_ok=True)
                dest = dest_parent / src.name
                shutil.copy2(src, dest)
                logger.debug("Copied manual file (%s): %s -> %s", ftype, src.name, dest_parent)
            else:
                self._warn(f"Manual file not found and skipped ({ftype}): {src}")

        # Copy specific asset directories
        for dtype, dpath_str in self._dirs.items():
            if not dpath_str:
                continue
            src_dir = Path(dpath_str)
            if src_dir.is_dir():
                dest_dir = map_output_dir / src_dir.name
                shutil.copytree(src_dir, dest_dir, dirs_exist_ok=True)
                logger.debug("Copied manual dir: %s", src_dir.name)
            else:
                self._warn(f"Manual directory not found and skipped ({dtype}): {src_dir}")

        return map_output_dir
