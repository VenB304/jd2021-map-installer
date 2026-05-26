"""Normalizer pipeline: raw extracted files → NormalizedMapData.

Orchestrates CKD loading (JSON or binary), media asset discovery,
and validation to produce a single ``NormalizedMapData`` instance
regardless of whether the source is a web download or an IPK archive.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from pathlib import Path
from typing import Dict, List, Optional

from jd2021_installer.core.exceptions import (
    NormalizationError,
    ParseError,
    ValidationError,
)
from jd2021_installer.core.models import (
    CinematicTape,
    DanceTape,
    DefaultColors,
    KaraokeClip,
    KaraokeTape,
    MapMedia,
    MapSync,
    MotionClip,
    MusicTrackStructure,
    NormalizedMapData,
    SongDescription,
    SoundSetClip,
    TapeReferenceClip,
)
from jd2021_installer.core.songdb_update import (
    find_songdb_entry,
    resolve_songdb_synth_path,
)
from jd2021_installer.parsers.binary_ckd import parse_binary_ckd

logger = logging.getLogger("jd2021.parsers.normalizer")

JDU_AUDIO_CALIBRATION_MS = 0.0
_SONGDB_SYNTH_MISSING_WARNED = False

_PLACEHOLDER_TEXT_VALUES = {
    "unknown",
    "unknown title",
    "unknown artist",
    "unknown dancer",
    "empty credits",
    "n/a",
    "na",
    "none",
}


def _is_effectively_missing_text(value: object) -> bool:
    text = str(value or "").strip()
    if not text:
        return True
    return text.lower() in _PLACEHOLDER_TEXT_VALUES


def _is_jdnext_source(source_dir: Path, media: MapMedia) -> bool:
    """Best-effort detection for JDNext-origin maps in normalized sources."""
    if (source_dir / "jdnext_metadata.json").exists():
        return True

    # JDNext bundle flows often map mapPackage metadata to monobehaviour/map.json.
    # Presence of this payload is a strong JDNext indicator for extracted sources.
    if (source_dir / "monobehaviour" / "map.json").exists():
        return True

    assets_html = source_dir / "assets.html"
    if assets_html.exists():
        try:
            content = assets_html.read_text(encoding="utf-8", errors="ignore").lower()
            if "/jdnext/maps/" in content or "server:jdnext" in content:
                return True
        except OSError:
            pass

    video_path = media.video_path
    if video_path and re.match(r"^video_(ultra|high|mid|low)\.(hd|vp8|vp9)\.webm$", video_path.name.lower()):
        return True

    audio_path = media.audio_path
    if audio_path and audio_path.name.lower() == "audio.opus":
        return True

    return False


# ---------------------------------------------------------------------------
# CKD file loading (JSON-first, binary fallback)
# ---------------------------------------------------------------------------

def load_ckd(file_path: str | Path) -> dict | object:
    """Read a CKD file, trying JSON first and falling back to binary.

    Returns either a parsed JSON dict or a typed dataclass from
    :func:`parse_binary_ckd`.
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"CKD file not found: {path}")

    raw = path.read_bytes()

    # Try JSON first (strip null padding) using strict UTF-8 decoding.
    try:
        content = raw.replace(b"\x00", b"").strip().decode("utf-8")
        return json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError):
        pass

    # Fall back to binary parser
    return parse_binary_ckd(raw, path.name)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _prefer_non_legacy(paths: List[str]) -> List[str]:
    """Sort CKD paths so non-legacy (JSON) files come before binary."""
    non_legacy = [p for p in paths if "main_legacy" not in os.path.basename(p).lower()]
    legacy = [p for p in paths if "main_legacy" in os.path.basename(p).lower()]
    return non_legacy + legacy


def _filter_by_codename(
    paths: List[str], codename: Optional[str], base_dir: Optional[str] = None
) -> List[str]:
    """Filter paths to codename-scoped matches (exact path component or safe filename prefix)."""
    if not codename:
        return paths
    cn_lower = codename.lower()

    def _path_has_codename_component(path_str: str) -> bool:
        parts = [p for p in path_str.replace("\\", "/").lower().split("/") if p]
        return cn_lower in parts

    def _filename_matches_codename(path_str: str) -> bool:
        name = os.path.basename(path_str).lower()
        # Prevent collisions like "apt" matching "aptalt".
        return bool(re.match(rf"^{re.escape(cn_lower)}(?:[^a-z0-9]|$)", name))

    filtered = []
    for p in paths:
        if _path_has_codename_component(p) or _filename_matches_codename(p):
            filtered.append(p)
    return filtered


def _resolve_map_source_dir(directory: Path, codename: Optional[str]) -> Path:
    """Resolve the most likely map-local directory for a codename within extracted sources."""
    if not codename:
        return directory

    cn_lower = codename.lower()
    parts = [p.lower() for p in directory.parts]
    if cn_lower in parts:
        return directory

    candidates: List[Path] = []

    for p in directory.rglob("*"):
        if not p.is_dir():
            continue
        p_parts = [x.lower() for x in p.parts]
        if cn_lower not in p_parts:
            continue

        try:
            idx = p_parts.index("world")
            if idx + 2 < len(p_parts) and p_parts[idx + 1] == "maps" and p_parts[idx + 2] == cn_lower:
                candidates.append(p)
                continue
            if idx + 2 < len(p_parts) and p_parts[idx + 1].startswith("jd") and p_parts[idx + 2] == cn_lower:
                candidates.append(p)
                continue
        except ValueError:
            pass

        if p.name.lower() == cn_lower:
            candidates.append(p)

    if not candidates:
        return directory

    # Prefer shallowest candidate relative to extraction root.
    candidates.sort(key=lambda c: len(c.relative_to(directory).parts))
    resolved = candidates[0]
    logger.debug("Resolved map source dir for '%s': %s", codename, resolved)
    return resolved


def _find_ckd_files(
    directory: str,
    pattern: str,
    codename: Optional[str] = None,
) -> List[str]:
    """Find CKD files matching a glob pattern, preferring non-legacy.

    If no results are found with codename filtering, falls back to
    searching without the codename filter (handles deeply nested ZIPs).
    """
    from pathlib import Path
    base_path = Path(directory)
    # Using Path.rglob is often more robust than glob.glob with ** on some systems
    paths = [str(p) for p in base_path.rglob(pattern)]
    
    if not paths and "musictrack" in pattern:
        # Extra fallback for musictrack variations
        alt_patterns = ["*TM_MusicTrack*.ckd", "*musictrack*.ckd", "*MusicTrack*.ckd"]
        for alt in alt_patterns:
            paths = [str(p) for p in base_path.rglob(alt)]
            if paths: break

    filtered = _filter_by_codename(paths, codename, directory)
    result = _prefer_non_legacy(filtered)

    # V1 Parity: STRICT SCOPING for bundles.
    # If a codename was specified but scoped search returned 0 while candidates exist, 
    # we only fall back if there's a single candidate (meaning codename inference was just slightly off).
    # If multiple candidates exist but none match, it's safer to return 0 than to pick a random map.
    if not result and paths:
        if not codename or len(paths) == 1:
            logger.debug(
                "CKD search for '%s' returned 0 results; falling back to unfiltered (candidate count: %d)",
                codename or "None",
                len(paths),
            )
            result = _prefer_non_legacy(paths)
        else:
            logger.warning(
                "CKD search for '%s' returned 0 results in bundle (%d other candidates found). Skipping to avoid mis-assignment.",
                codename,
                len(paths),
            )

    return result


def _normalize_supplemental_roots(
    supplemental_roots: Optional[List[str | Path]],
    primary_root: Path,
) -> List[str]:
    if not supplemental_roots:
        return []

    primary_resolved = primary_root.resolve()
    deduped: list[str] = []
    seen: set[str] = set()

    for root in supplemental_roots:
        if not root:
            continue
        path = Path(root)
        if not path.exists() or not path.is_dir():
            continue
        try:
            if path.resolve() == primary_resolved:
                continue
        except OSError:
            pass
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(key)

    return deduped


def _infer_ckd_stem_alias(directory: str, codename: Optional[str]) -> Optional[str]:
    """Infer an alternate CKD stem when source filenames don't match codename.

    Some JDNext payloads expose runtime map codename in map.json (e.g. Jukebox)
    but ship CKDs with legacy stems (e.g. pigstep..._musictrack.tpl.ckd).
    """
    root = Path(directory)
    codename_low = str(codename or "").strip().lower()

    candidates: Dict[str, int] = {}
    suffixes = (
        "_musictrack.tpl",
        "_musictrack",
        "_tml_dance.dtape",
        "_tml_dance",
        "_tml_karaoke.ktape",
        "_tml_karaoke",
        "_songdesc.tpl",
        "_songdesc",
    )

    scan_patterns = (
        "*musictrack*.tpl.ckd",
        "*dtape*ckd",
        "*ktape*ckd",
        "*songdesc*.tpl.ckd",
    )

    for pattern in scan_patterns:
        for path in root.rglob(pattern):
            stem = path.stem
            stem_low = stem.lower()
            base = stem
            for suffix in suffixes:
                if stem_low.endswith(suffix):
                    base = stem[: -len(suffix)]
                    break
            base = base.strip("_- ")
            base_low = base.lower()
            if not base_low:
                continue
            if codename_low and base_low == codename_low:
                continue
            if not re.search(r"[a-z]", base_low):
                continue
            candidates[base] = candidates.get(base, 0) + 1

    if not candidates:
        return None

    alias = max(candidates.items(), key=lambda item: item[1])[0]
    logger.debug("Inferred CKD stem alias for '%s': %s", codename or "<none>", alias)
    return alias


def _has_scoped_ckd_candidates(directory: str, pattern: str, codename: Optional[str]) -> bool:
    """Return True when at least one CKD candidate matches codename scoping."""
    paths = [str(p) for p in Path(directory).rglob(pattern)]
    if not paths:
        return False
    return bool(_filter_by_codename(paths, codename, directory))


# ---------------------------------------------------------------------------
# Individual CKD extractors
# ---------------------------------------------------------------------------

def _extract_music_track(
    directory: str,
    codename: Optional[str] = None,
    supplemental_roots: Optional[List[str]] = None,
) -> MusicTrackStructure:
    """Find and parse a musictrack CKD → MusicTrackStructure."""
    ckd_paths = _find_ckd_files(directory, "*musictrack*.tpl.ckd", codename)
    if not ckd_paths and supplemental_roots:
        for root in supplemental_roots:
            ckd_paths = _find_ckd_files(root, "*musictrack*.tpl.ckd", codename)
            if ckd_paths:
                logger.info("Using supplemental musictrack from %s", root)
                break
    if not ckd_paths:
        # V1 Parity: In Readjust mode, we might not have the original CKD, 
        # but we can still work with the .trk in normalization.
        return None

    try:
        data = load_ckd(ckd_paths[0])
    except ParseError as exc:
        logger.debug("musictrack CKD parse error: %s", exc)
        data = None

    if isinstance(data, MusicTrackStructure):
        res = data
    elif isinstance(data, dict):
        # JSON dict → build MusicTrackStructure
        try:
            from jd2021_installer.core.models import MusicSection, MusicSignature
            s = data.get("COMPONENTS", [{}])[0].get("trackData", {}).get("structure", {})

            vst = s.get("videoStartTime", 0.0)
            # V1 Parity: Auto-detect units (ticks vs seconds)
            if abs(vst) > 1000:
                vst /= 48000.0
                logger.debug("Detected videoStartTime in ticks; converted to %.6fs", vst)
                
            res = MusicTrackStructure(
                markers=s.get("markers", []),
                signatures=[
                    MusicSignature(beats=sig["beats"], marker=sig["marker"])
                    for sig in s.get("signatures", [])
                ],
                sections=[
                    MusicSection(section_type=sec["sectionType"], marker=sec["marker"])
                    for sec in s.get("sections", [])
                ],
                start_beat=s.get("startBeat", 0),
                end_beat=s.get("endBeat", 0),
                video_start_time=vst,
                preview_entry=float(s.get("previewEntry", 0)),
                preview_loop_start=float(s.get("previewLoopStart", 0)),
                preview_loop_end=float(s.get("previewLoopEnd", 0)),
                volume=float(s.get("volume", 0)),
                fade_in_duration=float(s.get("fadeInDuration", 0)),
                fade_in_type=int(s.get("fadeInType", 0)),
                fade_out_duration=float(s.get("fadeOutDuration", 0)),
                fade_out_type=int(s.get("fadeOutType", 0)),
            )
        except (KeyError, IndexError, TypeError) as exc:
            raise NormalizationError(f"Invalid musictrack JSON: {exc}") from exc
    else:
        return MusicTrackStructure()

    # V1/V2 unit parity & safety: if videoStartTime is 0 but startBeat is negative, 
    # it's likely a binary CKD that needs synthesis from markers.
    if res.video_start_time == 0.0 and res.start_beat < 0:
        idx = abs(res.start_beat)
        if 0 <= idx < len(res.markers):
            # V1 Parity: NO marker offset for videoStartTime synthesis
            vst = -(res.markers[idx] / 48.0 / 1000.0)
            logger.debug("Synthesized video_start_time from markers: %.3f s", vst)
            res.video_start_time = vst
    return res


def _find_source_trk_path(source_dir: Path, codename: str) -> Optional[Path]:
    """Locate the source .trk file for a map codename (case-insensitive).

    Some JDNext sources use variant track names (for example extra suffixes
    around the codename). Prefer exact matches first, then fallback to
    codename-contained stems to mirror install-time behavior.
    """
    direct = source_dir / "Audio" / f"{codename}.trk"
    if direct.exists():
        return direct

    codename_lower = codename.lower()

    def _is_exact(candidate: Path) -> bool:
        return candidate.stem.lower() == codename_lower

    def _is_fuzzy(candidate: Path) -> bool:
        stem_lower = candidate.stem.lower()
        return codename_lower in stem_lower

    audio_dir = source_dir / "Audio"
    if audio_dir.exists():
        exact_hits: List[Path] = []
        fuzzy_hits: List[Path] = []
        for candidate in audio_dir.glob("*.trk"):
            if _is_exact(candidate):
                exact_hits.append(candidate)
            elif _is_fuzzy(candidate):
                fuzzy_hits.append(candidate)
        if exact_hits:
            return sorted(exact_hits)[0]
        if fuzzy_hits:
            return sorted(fuzzy_hits)[0]

    exact_hits: List[Path] = []
    fuzzy_hits: List[Path] = []
    for candidate in source_dir.rglob("*.trk"):
        if _is_exact(candidate):
            exact_hits.append(candidate)
        elif _is_fuzzy(candidate):
            fuzzy_hits.append(candidate)

    if exact_hits:
        return sorted(exact_hits)[0]
    if fuzzy_hits:
        return sorted(fuzzy_hits)[0]

    return None


def _read_musictrack_fields_from_trk(trk_path: Path) -> Dict[str, float]:
    """Read key timing fields from a UbiArt .trk text file."""
    if not trk_path.exists():
        return {}

    try:
        content = trk_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    fields: Dict[str, float] = {}
    number_pattern = r"([-+]?\d*\.?\d+(?:[eE][-+]?\d+)?)"
    for key in ("videoStartTime", "previewEntry", "previewLoopStart", "previewLoopEnd"):
        match = re.search(rf"{key}\s*=\s*{number_pattern}", content)
        if match:
            try:
                fields[key] = float(match.group(1))
            except ValueError:
                continue

    return fields


def _merge_preview_fields_from_trk(music_track: Optional[MusicTrackStructure], trk_path: Optional[Path]) -> None:
    """Override preview timing values from .trk when present.

    Some source CKDs (notably legacy console variants) omit or zero preview
    loop fields even when the source .trk contains valid values.
    """
    if music_track is None or trk_path is None:
        return

    trk_fields = _read_musictrack_fields_from_trk(trk_path)
    if not trk_fields:
        return

    if "previewEntry" in trk_fields:
        music_track.preview_entry = trk_fields["previewEntry"]
    if "previewLoopStart" in trk_fields:
        music_track.preview_loop_start = trk_fields["previewLoopStart"]
    if "previewLoopEnd" in trk_fields:
        music_track.preview_loop_end = trk_fields["previewLoopEnd"]

    logger.info(
        "Merged preview timing from source .trk (%s): entry=%.3f start=%.3f end=%.3f",
        trk_path,
        music_track.preview_entry,
        music_track.preview_loop_start,
        music_track.preview_loop_end,
    )


def _extract_song_desc(
    directory: str,
    codename: Optional[str] = None,
    supplemental_roots: Optional[List[str]] = None,
) -> SongDescription:
    """Find and parse a songdesc CKD → SongDescription."""
    def _songdesc_from_map_json_fallback() -> Optional[SongDescription]:
        """Best-effort SongDesc metadata from JDNext MonoBehaviour map.json."""

        def _coerce_int(value: object, default: int) -> int:
            try:
                return int(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return default

        map_json_candidates = sorted(Path(directory).rglob("monobehaviour/map.json"))
        if not map_json_candidates:
            map_json_candidates = sorted(Path(directory).rglob("map.json"))

        codename_low = (codename or "").strip().lower()

        for map_json_path in map_json_candidates:
            try:
                payload = json.loads(map_json_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            if not isinstance(payload, dict):
                continue

            sd = payload.get("SongDesc")
            if not isinstance(sd, dict):
                continue

            map_name = str(sd.get("MapName") or payload.get("MapName") or codename or "Unknown")
            if codename_low and map_name.lower() != codename_low:
                continue

            return SongDescription(
                map_name=map_name,
                title=str(sd.get("Title", codename or map_name or "Unknown") or ""),
                artist=str(sd.get("Artist", "Unknown Artist") or ""),
                dancer_name=str(sd.get("DancerName", "Unknown Dancer") or "Unknown Dancer"),
                credits=str(sd.get("Credits", "") or ""),
                num_coach=_coerce_int(sd.get("NumCoach", 1), 1),
                main_coach=_coerce_int(sd.get("MainCoach", -1), -1),
                difficulty=_coerce_int(sd.get("Difficulty", 2), 2),
                sweat_difficulty=_coerce_int(sd.get("SweatDifficulty", 1), 1),
                jd_version=_coerce_int(sd.get("JDVersion", 2021), 2021),
                original_jd_version=_coerce_int(sd.get("OriginalJDVersion", 2021), 2021),
            )

        return None

    def _songdesc_from_html_fallback() -> SongDescription:
        """Best-effort SongDesc metadata from downloaded embed HTML."""
        title = codename or "Unknown"
        artist = "Unknown Artist"

        html_candidates = sorted(Path(directory).rglob("*assets*.html"))
        if not html_candidates:
            html_candidates = sorted(Path(directory).glob("*.html"))

        for html_path in html_candidates:
            try:
                content = html_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            title_match = re.search(
                r"<div class=\"embedTitle[^\"]*\">\s*<span>([^<]+)</span>",
                content,
                re.IGNORECASE,
            )
            artist_match = re.search(
                r"<div class=\"embedDescription[^\"]*\">\s*<span>\s*by\s+([^<]+)</span>",
                content,
                re.IGNORECASE,
            )

            if title_match:
                candidate_title = title_match.group(1).strip()
                if candidate_title:
                    title = candidate_title
            if artist_match:
                candidate_artist = artist_match.group(1).strip()
                if candidate_artist:
                    artist = candidate_artist

            if title_match or artist_match:
                logger.info(
                    "Recovered SongDesc metadata from HTML fallback: title='%s', artist='%s'",
                    title,
                    artist,
                )
                break

        return SongDescription(
            map_name=codename or "Unknown",
            title=title,
            artist=artist,
        )

    ckd_paths = _find_ckd_files(directory, "*songdesc*.tpl.ckd", codename)

    if not ckd_paths and supplemental_roots:
        for root in supplemental_roots:
            ckd_paths = _find_ckd_files(root, "*songdesc*.tpl.ckd", codename)
            if ckd_paths:
                logger.info("Using supplemental songdesc from %s", root)
                break

    if not ckd_paths:
        map_json_songdesc = _songdesc_from_map_json_fallback()
        if map_json_songdesc is not None:
            if not str(map_json_songdesc.title or "").strip() or not str(map_json_songdesc.artist or "").strip():
                html_fallback = _songdesc_from_html_fallback()
                if not str(map_json_songdesc.title or "").strip() and str(html_fallback.title or "").strip():
                    map_json_songdesc.title = html_fallback.title
                if not str(map_json_songdesc.artist or "").strip() and str(html_fallback.artist or "").strip():
                    map_json_songdesc.artist = html_fallback.artist
            logger.debug("songdesc.tpl.ckd not found; recovered SongDesc from map.json fallback")
            return map_json_songdesc

        logger.debug("songdesc.tpl.ckd not found; using fallback metadata")
        return _songdesc_from_html_fallback()

    try:
        data = load_ckd(ckd_paths[0])
    except ParseError as exc:
        logger.debug("songdesc CKD parse error: %s", exc)
        data = None

    if isinstance(data, SongDescription):
        return data

    if not isinstance(data, dict):
        return SongDescription(map_name=codename or "unknown")

    # JSON dict
    try:
        sd = data["COMPONENTS"][0]
        dc_raw = sd.get("DefaultColors", {})
        dc = DefaultColors()
        if isinstance(dc_raw, dict):
            dc.theme = dc_raw.get("theme", dc.theme)
            dc.lyrics = dc_raw.get("lyrics", dc.lyrics)
            for k, v in dc_raw.items():
                if k.lower() not in ("theme", "lyrics"):
                    dc.extra[k] = v

        song_desc = SongDescription(
            map_name=sd.get("MapName", codename or "Unknown"),
            title=sd.get("Title", codename or "Unknown"),
            artist=sd.get("Artist", "Unknown Artist"),
            dancer_name=sd.get("DancerName", "Unknown Dancer"),
            credits=sd.get("Credits", ""),
            num_coach=int(sd.get("NumCoach", 1)),
            main_coach=int(sd.get("MainCoach", -1)),
            difficulty=int(sd.get("Difficulty", 2)),
            sweat_difficulty=int(sd.get("SweatDifficulty", 1)),
            background_type=int(sd.get("backgroundType", sd.get("BackgroundType", 0))),
            lyrics_type=int(sd.get("LyricsType", 0)),
            energy=int(sd.get("Energy", 1)),
            tags=sd.get("Tags", ["Main"]) or ["Main"],
            status=int(sd.get("Status", 3)),
            locale_id=int(sd.get("LocaleID", sd.get("ID", 4294967295))),
            version_loc_id=(
                int(sd["VersionLocId"])
                if "VersionLocId" in sd and sd.get("VersionLocId") is not None
                else None
            ),
            mojo_value=int(sd.get("MojoValue", 0)),
            jd_version=int(sd.get("JDVersion", 2021)),
            original_jd_version=int(sd.get("OriginalJDVersion", 2021)),
            default_colors=dc,
            phone_images=sd.get("PhoneImages", {}),
        )

        if not str(song_desc.title or "").strip() or not str(song_desc.artist or "").strip():
            html_fallback = _songdesc_from_html_fallback()
            if not str(song_desc.title or "").strip() and str(html_fallback.title or "").strip():
                song_desc.title = html_fallback.title
            if not str(song_desc.artist or "").strip() and str(html_fallback.artist or "").strip():
                song_desc.artist = html_fallback.artist

        return song_desc
    except (KeyError, IndexError, TypeError) as exc:
        raise NormalizationError(f"Invalid songdesc JSON: {exc}") from exc


def _apply_jdnext_metadata_songdesc_overrides(
    directory: str,
    song_desc: SongDescription,
) -> None:
    """Overlay JDNext fetch metadata onto SongDescription when useful.

    Uses only fields relevant to JD2021 output generation and keeps existing
    SongDesc values when they appear authoritative.
    """

    def _to_int(value: object) -> Optional[int]:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def _to_difficulty(value: object) -> Optional[int]:
        as_int = _to_int(value)
        if as_int is not None:
            return as_int

        text = str(value or "").strip().lower()
        if not text:
            return None
        mapping = {
            "easy": 1,
            "medium": 2,
            "normal": 2,
            "hard": 3,
            "extreme": 4,
        }
        return mapping.get(text)

    root = Path(directory)
    candidates = sorted(root.rglob("jdnext_metadata.json"))
    if not candidates:
        return

    metadata: Optional[dict] = None
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            metadata = payload
            break

    if not metadata:
        return

    tags = metadata.get("tags")
    if isinstance(tags, list):
        cleaned_tags = [str(t).strip() for t in tags if str(t).strip()]
        if cleaned_tags and (not song_desc.tags or song_desc.tags == ["Main"]):
            song_desc.tags = cleaned_tags

    credits = str(metadata.get("credits", "") or "").strip()
    if credits and _is_effectively_missing_text(song_desc.credits):
        song_desc.credits = credits

    other_info = metadata.get("other_info")
    if not isinstance(other_info, dict):
        return

    difficulty = _to_difficulty(other_info.get("difficulty"))
    if difficulty is not None and song_desc.difficulty in (0, 2):
        song_desc.difficulty = difficulty

    sweat_difficulty = _to_difficulty(other_info.get("sweat_difficulty"))
    if sweat_difficulty is not None and song_desc.sweat_difficulty in (0, 1):
        song_desc.sweat_difficulty = sweat_difficulty

    original_ver = _to_int(other_info.get("original_jd_version"))
    if original_ver is not None and song_desc.original_jd_version in (0, -1, 2021):
        song_desc.original_jd_version = original_ver

    coach_count = _to_int(other_info.get("coach_count"))
    if coach_count is not None and coach_count > 0 and coach_count > song_desc.num_coach:
        song_desc.num_coach = coach_count

def _apply_jdlo_metadata_songdesc_overrides(
    directory: str,
    song_desc: SongDescription,
) -> None:
    """Overlay JDLO metadata onto SongDescription when available."""
    root = Path(directory)
    candidates = sorted(root.rglob("jdlo_metadata.json"))
    if not candidates:
        return

    metadata: Optional[dict] = None
    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict):
            metadata = payload
            break

    if not metadata:
        return

    mapped_title = str(metadata.get("title", "") or "").strip()
    if mapped_title and (
        _is_effectively_missing_text(song_desc.title)
        or str(song_desc.title or "").strip().lower() == str(song_desc.map_name or "").strip().lower()
    ):
        song_desc.title = mapped_title
        
    mapped_artist = str(metadata.get("artist", "") or "").strip()
    if mapped_artist and _is_effectively_missing_text(song_desc.artist):
        song_desc.artist = mapped_artist
        
    if metadata.get("credits") and _is_effectively_missing_text(song_desc.credits):
        song_desc.credits = str(metadata["credits"])
        
    if metadata.get("difficulty") is not None:
        song_desc.difficulty = metadata["difficulty"]
        
    if metadata.get("sweatDifficulty") is not None:
        song_desc.sweat_difficulty = metadata["sweatDifficulty"]
        
    if metadata.get("coachCount") is not None:
        song_desc.num_coach = metadata["coachCount"]
        
    if metadata.get("originalJDVersion") is not None:
        song_desc.original_jd_version = metadata["originalJDVersion"]
        
    if metadata.get("tags"):
        tags = metadata["tags"]
        if isinstance(tags, list):
            song_desc.tags = [str(t) for t in tags]


def _parse_jdnext_lyrics_color(hex_str: str) -> Optional[List[float]]:
    """Parse a JDNext ``#RRGGBBAA`` or ``#RRGGBB`` lyrics color into ``[R, G, B, A]`` floats.

    Returns ``None`` if the string is unparseable.
    """
    s = hex_str.strip().lstrip("#")
    if len(s) == 8:
        try:
            r = int(s[0:2], 16) / 255.0
            g = int(s[2:4], 16) / 255.0
            b = int(s[4:6], 16) / 255.0
            a = int(s[6:8], 16) / 255.0
            return [r, g, b, a]
        except ValueError:
            return None
    elif len(s) == 6:
        try:
            r = int(s[0:2], 16) / 255.0
            g = int(s[2:4], 16) / 255.0
            b = int(s[4:6], 16) / 255.0
            return [r, g, b, 1.0]
        except ValueError:
            return None
    return None


def _apply_jdnext_songdb_cache_overrides(
    codename: str,
    song_desc: SongDescription,
    music_track: Optional[MusicTrackStructure],
) -> None:
    """Overlay synthesized JDNext songdb cache values when available.

    This cache is optional. If missing, normalizer keeps existing behavior.
    """
    global _SONGDB_SYNTH_MISSING_WARNED

    synth_path = resolve_songdb_synth_path()
    if not synth_path.exists():
        if not _SONGDB_SYNTH_MISSING_WARNED:
            logger.warning(
                "JDNext songdb cache not found at %s. Continuing with default metadata fallback.",
                synth_path,
            )
            _SONGDB_SYNTH_MISSING_WARNED = True
        return

    entry = find_songdb_entry(
        codename=codename,
        map_name=song_desc.map_name,
        title=song_desc.title,
        synth_path=synth_path,
    )
    if not entry:
        logger.debug(
            "JDNext songdb cache lookup miss for codename='%s' map_name='%s' title='%s'",
            codename,
            song_desc.map_name,
            song_desc.title,
        )
        return

    logger.debug(
        "JDNext songdb cache lookup hit for codename='%s' (entry_id=%s)",
        codename,
        entry.get("entry_id"),
    )

    applied_fields: list[str] = []

    def _as_int(value: object) -> Optional[int]:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    def _as_float(value: object) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    tags = entry.get("tags")
    if isinstance(tags, list):
        cleaned_tags = [str(t).strip() for t in tags if str(t).strip()]
        if cleaned_tags and (not song_desc.tags or song_desc.tags == ["Main"]):
            song_desc.tags = cleaned_tags
            applied_fields.append("tags")

    credits = str(entry.get("credits", "") or "").strip()
    if credits and _is_effectively_missing_text(song_desc.credits):
        song_desc.credits = credits
        applied_fields.append("credits")

    mapped_title = str(entry.get("title", "") or "").strip()
    if mapped_title and (
        _is_effectively_missing_text(song_desc.title)
        or str(song_desc.title or "").strip().lower() == str(song_desc.map_name or "").strip().lower()
    ):
        song_desc.title = mapped_title
        applied_fields.append("title")

    mapped_artist = str(entry.get("artist", "") or "").strip()
    if mapped_artist and _is_effectively_missing_text(song_desc.artist):
        song_desc.artist = mapped_artist
        applied_fields.append("artist")

    difficulty = _as_int(entry.get("difficulty"))
    if difficulty is not None and song_desc.difficulty in (0, 2):
        song_desc.difficulty = difficulty
        applied_fields.append("difficulty")

    sweat_difficulty = _as_int(entry.get("sweat_difficulty"))
    if sweat_difficulty is not None and song_desc.sweat_difficulty in (0, 1):
        song_desc.sweat_difficulty = sweat_difficulty
        applied_fields.append("sweat_difficulty")

    coach_count = _as_int(entry.get("coach_count"))
    if coach_count is not None and coach_count > 0 and coach_count > song_desc.num_coach:
        song_desc.num_coach = coach_count
        applied_fields.append("coach_count")

    original_ver = _as_int(entry.get("original_jd_version"))
    if original_ver is not None and song_desc.original_jd_version in (0, -1, 2021):
        song_desc.original_jd_version = original_ver
        applied_fields.append("original_jd_version")

    # --- Lyrics color from songdb (JDNext maps lack DefaultColors in map.json) ---
    lyrics_color_hex = str(entry.get("lyrics_color", "") or "").strip()
    if lyrics_color_hex:
        parsed_lyrics = _parse_jdnext_lyrics_color(lyrics_color_hex)
        if parsed_lyrics is not None:
            # Only override when the current value is the dataclass default
            dc_default = DefaultColors()
            if song_desc.default_colors.lyrics == dc_default.lyrics:
                song_desc.default_colors.lyrics = parsed_lyrics
                applied_fields.append("lyrics_color")

    if not music_track:
        return

    preview_entry = _as_float(entry.get("preview_entry"))
    preview_loop_start = _as_float(entry.get("preview_loop_start"))
    preview_loop_end = _as_float(entry.get("preview_loop_end"))
    video_start_time = _as_float(entry.get("video_start_time"))

    if preview_entry is not None and music_track.preview_entry <= 0:
        music_track.preview_entry = preview_entry
        applied_fields.append("preview_entry")
    if preview_loop_start is not None and music_track.preview_loop_start <= 0:
        music_track.preview_loop_start = preview_loop_start
        applied_fields.append("preview_loop_start")
    if preview_loop_end is not None and (
        music_track.preview_loop_end <= 0
        or music_track.preview_loop_end <= music_track.preview_loop_start
    ):
        music_track.preview_loop_end = preview_loop_end
        applied_fields.append("preview_loop_end")
    if video_start_time is not None and music_track.video_start_time == 0.0:
        music_track.video_start_time = video_start_time
        applied_fields.append("video_start_time")

    if applied_fields:
        logger.info(
            "Applied JDNext songdb cache overrides for '%s': %s",
            codename,
            ", ".join(applied_fields),
        )
    else:
        logger.debug(
            "JDNext songdb cache entry found for '%s' but no overrides were needed",
            codename,
        )


def _extract_dance_tape(
    directory: str, codename: Optional[str] = None
) -> Optional[DanceTape]:
    """Find and parse a dtape CKD → DanceTape (or None)."""
    ckd_paths = _find_ckd_files(directory, "*dtape*ckd", codename)
    if not ckd_paths:
        return None
    data = load_ckd(ckd_paths[0])
    if isinstance(data, DanceTape):
        return data
    if isinstance(data, dict):
        # Best-effort clip count for JSON dtapes (supports legacy and synthesized layouts).
        clips = []
        clip_sources = []

        if isinstance(data.get("Clips"), list):
            clip_sources.append(data.get("Clips", []))
        if isinstance(data.get("clips"), list):
            clip_sources.append(data.get("clips", []))

        for comp in data.get("COMPONENTS", []):
            if not isinstance(comp, dict):
                continue
            if "JD_TapeComponent_Template" not in comp:
                continue
            tape_data = comp["JD_TapeComponent_Template"].get("tape", {})
            if isinstance(tape_data.get("clips"), list):
                clip_sources.append(tape_data.get("clips", []))
            if isinstance(tape_data.get("Clips"), list):
                clip_sources.append(tape_data.get("Clips", []))

        for source in clip_sources:
            for clip in source:
                if not isinstance(clip, dict):
                    continue
                # Minimal stub to allow counting in logs/UI
                clips.append(
                    MotionClip(
                        id=0,
                        track_id=0,
                        is_active=1,
                        start_time=0,
                        duration=0,
                        classifier_path="",
                        gold_move=0,
                        coach_id=0,
                        move_type=0,
                    )
                )
        return DanceTape(clips=clips, map_name=codename or "Unknown")
    return None


def _extract_karaoke_tape(
    directory: str, codename: Optional[str] = None
) -> Optional[KaraokeTape]:
    """Find and parse a ktape CKD → KaraokeTape (or None)."""
    ckd_paths = _find_ckd_files(directory, "*ktape*ckd", codename)
    if not ckd_paths:
        return None
    data = load_ckd(ckd_paths[0])
    if isinstance(data, KaraokeTape):
        return data
    if isinstance(data, dict):
        # Best-effort clip count for JSON ktapes (supports legacy and synthesized layouts).
        clips = []
        clip_sources = []

        if isinstance(data.get("Clips"), list):
            clip_sources.append(data.get("Clips", []))
        if isinstance(data.get("clips"), list):
            clip_sources.append(data.get("clips", []))

        for comp in data.get("COMPONENTS", []):
            if not isinstance(comp, dict):
                continue
            if "JD_TapeComponent_Template" not in comp:
                continue
            tape_data = comp["JD_TapeComponent_Template"].get("tape", {})
            if isinstance(tape_data.get("clips"), list):
                clip_sources.append(tape_data.get("clips", []))
            if isinstance(tape_data.get("Clips"), list):
                clip_sources.append(tape_data.get("Clips", []))

        for source in clip_sources:
            for clip in source:
                if not isinstance(clip, dict):
                    continue
                clips.append(
                    KaraokeClip(
                        id=0,
                        track_id=0,
                        is_active=1,
                        start_time=0,
                        duration=0,
                        lyrics="",
                        pitch=0.0,
                        is_end_of_line=0,
                    )
                )
        return KaraokeTape(clips=clips, map_name=codename or "Unknown")
    return None


def _extract_cinematic_tape(
    directory: str, codename: Optional[str] = None
) -> Optional[CinematicTape]:
    """Find and parse a mainsequence/cinematic tape CKD -> CinematicTape (or None)."""
    ckd_paths = _find_ckd_files(directory, "*mainsequence*tape.ckd", codename)
    if not ckd_paths:
        ckd_paths = _find_ckd_files(directory, "*mainsequence*.tape.ckd", codename)
    if not ckd_paths:
        ckd_paths = _find_ckd_files(directory, "*mainsequence*.ckd", codename)
    if not ckd_paths:
        return None

    data = load_ckd(ckd_paths[0])
    if isinstance(data, CinematicTape):
        return data

    if not isinstance(data, dict):
        return None

    clips = []
    tape_dict = data
    if "Clips" not in tape_dict and "COMPONENTS" in tape_dict:
        # Some JSON CKD payloads store tape data under component wrappers.
        for comp in tape_dict.get("COMPONENTS", []):
            if "JD_TapeComponent_Template" in comp:
                tape_dict = comp["JD_TapeComponent_Template"].get("tape", tape_dict)
                break

    for raw_clip in tape_dict.get("Clips", []):
        if not isinstance(raw_clip, dict):
            continue

        clip_class = str(raw_clip.get("__class", "")).lower()
        if clip_class == "soundsetclip":
            clips.append(
                SoundSetClip(
                    id=int(raw_clip.get("Id", 0)),
                    track_id=int(raw_clip.get("TrackId", 0)),
                    is_active=int(raw_clip.get("IsActive", 1)),
                    start_time=int(raw_clip.get("StartTime", 0)),
                    duration=int(raw_clip.get("Duration", 0)),
                    sound_set_path=str(raw_clip.get("SoundSetPath", "")),
                    sound_channel=int(raw_clip.get("SoundChannel", 0)),
                    start_offset=int(raw_clip.get("StartOffset", 0)),
                    stops_on_end=int(raw_clip.get("StopsOnEnd", 0)),
                    accounted_for_duration=int(raw_clip.get("AccountedForDuration", 0)),
                )
            )
        elif clip_class == "tapereferenceclip":
            clips.append(
                TapeReferenceClip(
                    id=int(raw_clip.get("Id", 0)),
                    track_id=int(raw_clip.get("TrackId", 0)),
                    is_active=int(raw_clip.get("IsActive", 1)),
                    start_time=int(raw_clip.get("StartTime", 0)),
                    duration=int(raw_clip.get("Duration", 0)),
                    path=str(raw_clip.get("Path", "")),
                    loop=int(raw_clip.get("Loop", 0)),
                )
            )

    return CinematicTape(
        clips=clips,
        map_name=str(tape_dict.get("MapName", codename or "Unknown")),
        soundwich_event=str(tape_dict.get("SoundwichEvent", "")),
    )


def _discover_media(
    directory: str,
    codename: Optional[str] = None,
    search_roots: Optional[List[str | Path]] = None,
) -> MapMedia:
    """Scan directory (and optional search roots) for media assets and populate MapMedia.

    Ported from V1 source_analysis.py recursive picking logic.
    """
    media = MapMedia()
    dir_path = Path(directory)
    codename_low = codename.lower() if codename else None

    scan_roots: list[Path] = [dir_path]
    if search_roots:
        for root in search_roots:
            if not root:
                continue
            path = Path(root)
            if not path.exists() or not path.is_dir():
                continue
            if path == dir_path:
                continue
            scan_roots.append(path)

    def _path_has_codename_component(path: Path) -> bool:
        if not codename_low:
            return True
        parts = [p.lower() for p in path.as_posix().split("/") if p]
        return codename_low in parts

    def _filename_matches_codename(path: Path) -> bool:
        if not codename_low:
            return True
        return bool(re.match(rf"^{re.escape(codename_low)}(?:[^a-z0-9]|$)", path.name.lower()))

    # 1. Video files (.webm)
    # V1 Parity: prioritize quality suffixes and codename match. Scan both local and search_root.
    SUPPORTED_QUALITIES = ["ULTRA_HD", "ULTRA", "HIGH_HD", "HIGH", "MID_HD", "MID", "LOW_HD", "LOW"]
    webms: list[Path] = []
    for root in scan_roots:
        webms.extend(list(root.rglob("*.webm")))
    
    if webms:
        main_videos = []
        for w in webms:
            w_name = w.name.lower()
            if any(k in w_name for k in ("mappreview", "videopreview", "preview")):
                if "preview" in w_name and not media.map_preview_video:
                    media.map_preview_video = w
                continue
            main_videos.append(w)
        
        if main_videos:
            # Filter by codename first if in bundle
            if codename_low:
                matches = [v for v in main_videos if _filename_matches_codename(v)]
                if not matches:
                    # V1 Parity: Path-based scoping for bundle IPKs (e.g. world/maps/MapName/videos/MapName.webm)
                    matches = [v for v in main_videos if _path_has_codename_component(v)]
                
                if matches:
                    main_videos = matches
                elif len(main_videos) > 1:
                    # If we have multiple videos but none match the codename, we are likely in a bundle
                    # and picking a random video is dangerous.
                    logger.debug("No video match for codename %s in multi-map bundle; skipping video discovery", codename)
                    main_videos = []
            
            if main_videos:
                # Priority: 1. Requested quality suffix, 2. any quality suffix, 3. first available
                best_video = main_videos[0]
                found_quality = False
                for q in SUPPORTED_QUALITIES:
                    suffix = f"_{q}.webm"
                    for v in main_videos:
                        if v.name.upper().endswith(suffix):
                            best_video = v
                            found_quality = True
                            break
                    if found_quality: break
                
                media.video_path = best_video

    # 2. Audio files (.ogg, .opus, .wav, .wav.ckd)
    # V1 Priority extended for JDNext: .ogg > .opus > .wav > .wav.ckd
    audio_found = False

    def _audio_base_name(path: Path) -> str:
        name = path.name.lower()
        for suffix in (".wav.ckd", ".ogg.ckd", ".opus.ckd", ".wav", ".ogg", ".opus", ".ckd"):
            if name.endswith(suffix):
                return name[: -len(suffix)]
        return path.stem.lower()

    for ext_pattern in ("*.ogg", "*.opus", "*.wav", "*.wav.ckd", "*.ogg.ckd", "*.opus.ckd"):
        if audio_found: break
        
        candidates: list[Path] = []
        for root in scan_roots:
            candidates.extend(list(root.rglob(ext_pattern)))
        if candidates:
            # Keep first-seen order while de-duplicating between both scans.
            candidates = list(dict.fromkeys(candidates))
        if not candidates: continue
        
        # Prune exclusions: amb, autodance, preview
        filtered = []
        for c in candidates:
            c_path = str(c).lower().replace("\\", "/")
            c_name = c.name.lower()
            # Strict V1 exclusions
            if any(k in c_path for k in ("/amb/", "/autodance/", "audiopreview", "mappreview")):
                continue
            if c_name.startswith("amb_") or c_name.startswith("ad_"):
                continue
            filtered.append(c)
        
        if not filtered: continue
        
        # Codename scoping
        if codename_low:
            exact_name_matches = [a for a in filtered if _audio_base_name(a) == codename_low]
            if exact_name_matches:
                matches = exact_name_matches
            else:
                matches = [a for a in filtered if _filename_matches_codename(a)]
            if not matches:
                # Path-based scoping for deeply nested IPK structures
                matches = [a for a in filtered if _path_has_codename_component(a)]
            if matches:
                filtered = matches
            else:
                # If we're in a bundle and this extension didn't match codename, skip it
                continue
        
        best_audio = filtered[0]
        # Handle CKD extraction
        if best_audio.name.lower().endswith(".ckd"):
            from jd2021_installer.installers.media_processor import extract_ckd_audio_v1

            decoded = extract_ckd_audio_v1(best_audio, best_audio.parent)
            if decoded:
                media.audio_path = Path(decoded)
                audio_found = True
            else:
                # Keep the cooked source as a fallback so install-time conversion can
                # retry extraction/decoding instead of failing as "audio missing".
                media.audio_path = best_audio
                audio_found = True
                logger.warning(
                    "Audio CKD decode deferred for '%s'; keeping source file: %s",
                    codename or "unknown",
                    best_audio.name,
                )
        else:
            media.audio_path = best_audio
            audio_found = True

    # 3. & 4. Images (Cover/Coach/Banner/Background)
    # Collect only texture-like files. Do NOT include generic *.ckd,
    # otherwise actor files like "*_cover_generic.act.ckd" can be selected.
    all_media_files: List[Path] = []
    for ext in ("*.jpg", "*.jpeg", "*.png", "*.tga", "*.jpg.ckd", "*.jpeg.ckd", "*.png.ckd", "*.tga.ckd"):
        for root in scan_roots:
            all_media_files.extend(list(root.rglob(ext)))
    if all_media_files:
        # Preserve scan order while removing duplicates from merged roots.
        all_media_files = list(dict.fromkeys(all_media_files))

    def _get_best_asset(keyword: str, files: List[Path], allow_optional_fallback: bool = False) -> Optional[Path]:
        candidates = [f for f in files if keyword in f.name.lower()]
        if not candidates:
            return None
        
        # Priority 1: Exact codename match in filename
        if codename_low:
            exact_matches = [c for c in candidates if _filename_matches_codename(c)]
            if exact_matches:
                return exact_matches[0]
            
            # Priority 2: Codename in parent directory path
            path_matches = [c for c in candidates if _path_has_codename_component(c)]
            if path_matches:
                return path_matches[0]
        
        # Priority 3: First available (fallback)
        # V1 Parity: For required assets (covers, coaches), only fall back if NO codename scoping
        # was requested. In bundles, if we found nothing matching the codename, it's safer to 
        # return None than to pick a random map's asset.
        # However, for optional assets (banner_bkg, map_bkg, cover_albumcoach, cover_albumbkg),
        # allow fallback ONLY if candidates don't contain a DIFFERENT codename. This allows
        # discovery of generic assets without codename prefixes while preventing cross-map mixing.
        if codename_low and not allow_optional_fallback:
            return None
        
        if codename_low and allow_optional_fallback:
            # For optional assets, accept the first candidate that either:
            # 1. Has no codename-like prefix (e.g., "map_bkg.tga"), or
            # 2. Has a codename prefix that matches ours (already checked above), or
            # 3. Is in a path that doesn't suggest it belongs to a different map
            # Reject candidates that have a DIFFERENT code name prefix (e.g., "mapaalbumcoach.tga")
            
            for candidate in candidates:
                # Check if there's a prefix that looks like a code name (e.g., "mapaalbumcoach" or "mapa_banner")
                # by seeing if the filename starts with codename alternatives
                candidate_name_lower = candidate.name.lower()
                
                # Check various patterns that would indicate this belongs to a different map
                # Pattern 1: codename_something (but not "map_bkg" or other generics)
                # Pattern 2: differentcodename_keyword
                # We only reject if we find a CLEARLY DIFFERENT codename pattern
                
                # Find all "_" separators and check prefixes
                import re
                parts = candidate_name_lower.split('_')
                if len(parts) > 1:
                    first_part = parts[0]
                    # If first part is a codename-like identifier and it's not ours, reject
                    if first_part != codename_low and first_part != "map" and len(first_part) >= 3:
                        # This looks like a different map's asset (e.g., "mapb_banner_bkg.tga")
                        # Check if this first_part is followed immediately by the asset name
                        # to determine if it's a map prefix or part of the asset name
                        rest_of_name = "_".join(parts[1:])
                        if keyword in rest_of_name:  # keyword comes after the prefix
                            # This is likely "{different_map}_{keyword}", skip it
                            continue
                
                # This candidate doesn't have a conflicting prefix, so accept it
                return candidate
            
            # If we get here, all candidates have conflicting prefixes, so reject
            return None
        
        return candidates[0]

    media.cover_generic_path = _get_best_asset("cover_generic", all_media_files)
    media.cover_online_path = _get_best_asset("cover_online", all_media_files)
    
    # Fallback if specific covers missing
    if not media.cover_generic_path or not media.cover_online_path:
        general_cover = _get_best_asset("cover", all_media_files)
        if general_cover:
            if not media.cover_generic_path: media.cover_generic_path = general_cover
            if not media.cover_online_path: media.cover_online_path = general_cover

    media.banner_path = _get_best_asset("banner", all_media_files)
    # Allow optional_fallback for optional assets (may not have codename prefix)
    media.banner_bkg_path = _get_best_asset("banner_bkg", all_media_files, allow_optional_fallback=True)
    media.map_bkg_path = _get_best_asset("map_bkg", all_media_files, allow_optional_fallback=True)
    media.title_path = _get_best_asset("title", all_media_files, allow_optional_fallback=True)

    media.cover_albumbkg_path = _get_best_asset("albumbkg", all_media_files, allow_optional_fallback=True)
    media.cover_albumcoach_path = _get_best_asset("albumcoach", all_media_files, allow_optional_fallback=True)
    
    # Coaches are a list. Accept coach_1 / coach1 / coach-1 variants.
    # Avoid matching unrelated assets like albumcoach.
    coach_name_pattern = re.compile(r"(?:^|[_-])coach(?:[_-]?\d+)?(?:[_-]|$)", re.IGNORECASE)
    all_coaches = [
        f
        for f in all_media_files
        if "albumcoach" not in f.stem.lower() and coach_name_pattern.search(f.stem.lower())
    ]
    if codename_low:
        codename_coaches = [c for c in all_coaches if _filename_matches_codename(c)]
        if not codename_coaches:
            codename_coaches = [c for c in all_coaches if _path_has_codename_component(c)]
        if codename_coaches:
            all_coaches = codename_coaches

    # Sort and separate phone images
    main_coaches = [f for f in all_coaches if "_phone" not in f.name.lower()]
    phone_coaches = [f for f in all_coaches if "_phone" in f.name.lower()]
    
    media.coach_images = sorted(list(set(main_coaches)))
    media.coach_phone_images = sorted(list(set(phone_coaches)))



    # 5. Pictogram directory
    # V1 Parity: strictly scope to codename if possible
    media.pictogram_dir = None
    picto_candidates: list[Path] = []
    for root in scan_roots:
        picto_candidates.extend([d for d in root.rglob("*") if d.is_dir() and "picto" in d.name.lower()])
    if picto_candidates:
        picto_candidates = list(dict.fromkeys(picto_candidates))
    if codename_low:
        picto_candidates = [d for d in picto_candidates if _path_has_codename_component(d)]
    
    if picto_candidates:
        media.pictogram_dir = picto_candidates[0]
    else:
        # Fallback: finding parent of any picto CKD
        picto_files: list[Path] = []
        for root in scan_roots:
            picto_files.extend(list(root.rglob("*picto*.ckd")))
        if picto_files:
            picto_files = list(dict.fromkeys(picto_files))
        if codename_low:
            picto_files = [f for f in picto_files if _path_has_codename_component(f)]
        if picto_files:
            media.pictogram_dir = picto_files[0].parent

    # 6. Moves directory
    media.moves_dir = None
    move_candidates: list[Path] = []
    for root in scan_roots:
        move_candidates.extend([d for d in root.rglob("*") if d.is_dir() and "moves" in d.name.lower()])
    if move_candidates:
        move_candidates = list(dict.fromkeys(move_candidates))
    if codename_low:
        move_candidates = [d for d in move_candidates if _path_has_codename_component(d)]
    if move_candidates:
        media.moves_dir = move_candidates[0]

    return media


def _infer_coach_count_from_media(media: MapMedia) -> int:
    """Infer coach count from discovered coach image filenames.

    Supports both ``coach_2`` and ``coach2`` style suffixes. If no explicit
    index is present, each image contributes one coach.
    """
    if not media.coach_images:
        return 0

    indexed: List[int] = []
    non_indexed = 0
    for path in media.coach_images:
        name_low = path.name.lower()
        match = re.search(r"coach[_-]?(\d+)", name_low)
        if match:
            try:
                indexed.append(int(match.group(1)))
            except ValueError:
                non_indexed += 1
        else:
            non_indexed += 1

    if indexed:
        return max(indexed)
    return non_indexed


def normalize_sync(
    music_track: Optional[MusicTrackStructure], 
    is_html_source: bool = False,
    existing_trk_path: Optional[Path] = None,
    is_jdnext_source: bool = False,
) -> MapSync:
    """Determine the optimal audio/video sync offsets.
    
    Ported from V1 source_analysis.py logic.
    - HTML/Fetch maps get a constant +85ms audio calibration.
    - IPK/Readjust maps inherit existing videoStartTime.
    """
    from jd2021_installer.parsers.binary_ckd import calculate_marker_preroll
    import re
    
    audio_ms = 0.0
    video_ms = 0.0

    # Readjust mode: if .trk exists in source (e.g. from previously installed map), use it.
    if existing_trk_path and existing_trk_path.exists():
        try:
            content = existing_trk_path.read_text(encoding="utf-8")
            match = re.search(r"videoStartTime\s*=\s*([-+]?\d*\.?\d+)", content)
            if match:
                vst = float(match.group(1))
                # Auto-fix if ticks were accidentally written to the trk previously
                if abs(vst) > 1000:
                    vst /= 48000.0
                video_ms = vst * 1000.0
                logger.debug("Readjust mode: inherited videoStartTime %.6fs from existing .trk", vst)
                return MapSync(audio_ms=audio_ms, video_ms=video_ms)
        except Exception as e:
            logger.debug("Failed to read existing .trk for readjust: %s", e)

    # If no existing .trk or failed to read, proceed with standard logic
    if music_track:
        if is_html_source:
            # Fetch/HTML mode (OGG)
            # V1 parity: marker-based sync for HTML mode.
            # Audio keeps a constant +85ms calibration in all Fetch/HTML branches.
            prms_video = calculate_marker_preroll(
                music_track.markers,
                music_track.start_beat,
                include_calibration=False,
            )
            prms_audio = calculate_marker_preroll(
                music_track.markers,
                music_track.start_beat,
                include_calibration=False,
            )

            metadata_ms = music_track.video_start_time * 1000.0
            if prms_audio is not None:
                audio_ms = -prms_audio + JDU_AUDIO_CALIBRATION_MS
            else:
                audio_ms = metadata_ms + JDU_AUDIO_CALIBRATION_MS

            # V1 parity: preserve metadata videoStartTime when present.
            # Only synthesize from markers when metadata is effectively zero.
            if abs(metadata_ms) > 0.0001:
                video_ms = metadata_ms
                if is_jdnext_source:
                    logger.info(
                        "JDNext sync (metadata-preserved): audio_offset=%.3f ms, video_offset=%.3f ms (metadata)",
                        audio_ms,
                        video_ms,
                    )
                else:
                    logger.info(
                        "Fetch/HTML sync (metadata-preserved): audio_offset=%.3f ms (marker+cal), video_offset=%.3f ms (metadata)",
                        audio_ms,
                        video_ms,
                    )
            elif prms_video is not None:
                video_ms = -prms_video
                if is_jdnext_source:
                    logger.info(
                        "JDNext sync (synthesized video): audio_offset=%.3f ms, video_offset=%.3f ms (marker)",
                        audio_ms,
                        video_ms,
                    )
                else:
                    logger.info(
                        "Fetch/HTML sync (synthesized video): audio_offset=%.3f ms (marker+cal), video_offset=%.3f ms (marker)",
                        audio_ms,
                        video_ms,
                    )
            else:
                video_ms = metadata_ms
                if is_jdnext_source:
                    logger.debug("JDNext sync (fallback): using metadata offsets = %.3f ms", video_ms)
                else:
                    logger.debug("Fetch/HTML sync (fallback): using metadata offsets = %.3f ms", video_ms)

        else:
            # Binary Mode (IPK / WAV)
            # V1 Parity: No 85ms calibration for video or WAV audio.
            if abs(music_track.video_start_time) > 0.0001:
                # Use existing VST if present
                video_ms = music_track.video_start_time * 1000.0
                audio_ms = 0.0 # WAV from IPK already contains pre-roll
            else:
                # Fallback for missing VST in binary CKDs (Xbox 360)
                prms = calculate_marker_preroll(music_track.markers, music_track.start_beat, include_calibration=False)
                if prms is not None:
                    video_ms = -prms
                    logger.debug("IPK sync (synthesized): audio_offset=0, video_offset=%.3f ms", video_ms)
                else:
                    logger.debug("IPK sync (pre-synced): audio_offset=0, video_offset=%.3f ms", video_ms)
    else:
        logger.debug("No music_track provided to normalize_sync, returning default 0 offsets.")

    return MapSync(audio_ms=audio_ms, video_ms=video_ms)


# ---------------------------------------------------------------------------
# Main normalizer entry point
# ---------------------------------------------------------------------------

def normalize(
    directory: str | Path,
    codename: Optional[str] = None,
    search_root: Optional[str | Path] = None,
    supplemental_roots: Optional[List[str | Path]] = None,
) -> NormalizedMapData:
    """Normalize an extracted directory into a canonical NormalizedMapData.

    This is the single public entry-point for the normalizer pipeline.
    Works identically whether the directory was populated by the web
    extractor or the IPK extractor.

    Args:
        directory:   Path to the directory containing extracted files.
        codename:    Optional map codename for filtering in bundle IPKs.
        search_root: Optional root directory to scan for media (videos/audio).
        supplemental_roots: Optional list of extra roots for CKDs/media (bundle/bundlelogic).

    Returns:
        A fully-populated ``NormalizedMapData`` instance.

    Raises:
        NormalizationError: If critical data (musictrack) is missing.
        ValidationError:    If the normalized data fails validation.
    """
    directory = str(directory)
    source_root = Path(directory)
    source_root_str = str(source_root)

    supplemental_root_strs = _normalize_supplemental_roots(supplemental_roots, source_root)

    # V1 parity: in extracted bundle roots, resolve to this map's subtree to avoid cross-map bleed.
    map_source_dir = _resolve_map_source_dir(source_root, codename)
    source_dir = map_source_dir
    source_dir_str = str(source_dir)

    # 1. & 2. Extract basic metadata
    ckd_alias = _infer_ckd_stem_alias(source_root_str, codename)
    if ckd_alias is None and supplemental_root_strs:
        for root in supplemental_root_strs:
            ckd_alias = _infer_ckd_stem_alias(root, codename)
            if ckd_alias is not None:
                break

    preferred_ckd_key = codename
    if ckd_alias and codename and ckd_alias.lower() != codename.lower():
        codename_has_musictrack = _has_scoped_ckd_candidates(
            source_root_str,
            "*musictrack*.tpl.ckd",
            codename,
        )
        alias_has_musictrack = _has_scoped_ckd_candidates(
            source_root_str,
            "*musictrack*.tpl.ckd",
            ckd_alias,
        )
        if not codename_has_musictrack and alias_has_musictrack:
            preferred_ckd_key = ckd_alias
            logger.debug(
                "Using inferred CKD alias '%s' as primary lookup key for codename '%s'",
                ckd_alias,
                codename,
            )

    try:
        music_track = _extract_music_track(
            source_root_str,
            preferred_ckd_key,
            supplemental_roots=supplemental_root_strs,
        )
        if music_track is None and ckd_alias and preferred_ckd_key != ckd_alias:
            music_track = _extract_music_track(
                source_root_str,
                ckd_alias,
                supplemental_roots=supplemental_root_strs,
            )
    except NormalizationError:
        music_track = None

    song_desc = _extract_song_desc(
        source_root_str,
        codename,
        supplemental_roots=supplemental_root_strs,
    )
    effective_codename = codename or song_desc.map_name
    _apply_jdnext_metadata_songdesc_overrides(source_root_str, song_desc)
    _apply_jdlo_metadata_songdesc_overrides(source_root_str, song_desc)
    dance_tape = _extract_dance_tape(source_root_str, preferred_ckd_key)
    if dance_tape is None and ckd_alias and preferred_ckd_key != ckd_alias:
        dance_tape = _extract_dance_tape(source_root_str, ckd_alias)

    karaoke_tape = _extract_karaoke_tape(source_root_str, preferred_ckd_key)
    if karaoke_tape is None and ckd_alias and preferred_ckd_key != ckd_alias:
        karaoke_tape = _extract_karaoke_tape(source_root_str, ckd_alias)

    cinematic_tape = _extract_cinematic_tape(source_root_str, preferred_ckd_key)
    if cinematic_tape is None and ckd_alias and preferred_ckd_key != ckd_alias:
        cinematic_tape = _extract_cinematic_tape(source_root_str, ckd_alias)

    # Media discovery uses the resolved map subtree first, with full extraction root
    # as search_root fallback for assets that live outside world/maps/<codename>.
    media_search_roots: list[str] = []
    if search_root:
        media_search_roots.append(str(search_root))
    elif source_dir != source_root:
        media_search_roots.append(source_root_str)
    if supplemental_root_strs:
        media_search_roots.extend(supplemental_root_strs)

    media = _discover_media(
        source_dir_str,
        codename,
        search_roots=media_search_roots or None,
    )

    # Keep SongDesc coach metadata aligned with discovered media assets.
    inferred_coaches = _infer_coach_count_from_media(media)
    if inferred_coaches > song_desc.num_coach:
        logger.info(
            "Adjusted NumCoach from media discovery: %d -> %d",
            song_desc.num_coach,
            inferred_coaches,
        )
        song_desc.num_coach = inferred_coaches

    if song_desc.num_coach > 0 and (song_desc.main_coach < 0 or song_desc.main_coach >= song_desc.num_coach):
        fallback_coach = 1 if song_desc.num_coach == 3 else 0
        logger.info(
            "Adjusted MainCoach from %d to %d for NumCoach=%d",
            song_desc.main_coach,
            fallback_coach,
            song_desc.num_coach,
        )
        song_desc.main_coach = fallback_coach

    # Infer codename from song_desc if not provided
    effective_codename = codename or song_desc.map_name

    # Determine default sync values like V1
    # Ported from V1 map_installer.py Step 06
    is_html_source = any(source_dir.glob("*.html")) or any(source_dir.glob("**/assets.html"))

    source_trk_path = _find_source_trk_path(source_dir, effective_codename)
    _merge_preview_fields_from_trk(music_track, source_trk_path)

    # 5. Calculate effective video start time (with V1-style fallbacks)
    is_jdnext_source = _is_jdnext_source(source_dir, media)
    if is_jdnext_source:
        _apply_jdnext_songdb_cache_overrides(effective_codename, song_desc, music_track)

    sync_data = normalize_sync(
        music_track, 
        is_html_source=is_html_source,
        existing_trk_path=source_trk_path,
        is_jdnext_source=is_jdnext_source,
    )

    # V1 Parity: Detect whether the source contains real autodance data.
    # Many sources ship minimal stub CKDs that should be ignored.
    has_autodance = False
    cn_low = (effective_codename or "").lower()

    def _is_scoped_autodance_path(path_str: str) -> bool:
        if not cn_low:
            return True
        rel = path_str.replace("\\", "/").lower()
        parts = [p for p in rel.split("/") if p]
        if cn_low in parts:
            return True
        name = os.path.basename(path_str).lower()
        return bool(re.match(rf"^{re.escape(cn_low)}(?:[^a-z0-9]|$)", name))

    ad_tpls = _find_ckd_files(source_root_str, "*autodance*.tpl.ckd", codename)
    if ad_tpls:
        try:
            ad_data = load_ckd(ad_tpls[0])
            # Look for markers of real data (recording structure, video structure, or events)
            ad_str = str(ad_data).lower()
            if any(k in ad_str for k in ("recording_structure", "video_structure", "playback_events")):
                has_autodance = True
        except Exception:
            pass
    
    if not has_autodance:
        for ext in ("adtape", "advideo", "adrecording"):
            candidates = _find_ckd_files(source_root_str, f"*.{ext}.ckd", codename)
            loose_candidates = [
                p
                for p in glob.glob(os.path.join(source_root_str, "**", f"*.{ext}"), recursive=True)
                if _is_scoped_autodance_path(p)
            ]
            valid_candidates = []
            for candidate in candidates:
                try:
                    if Path(candidate).stat().st_size > 256:
                        valid_candidates.append(candidate)
                except OSError:
                    continue
            for candidate in loose_candidates:
                try:
                    if Path(candidate).stat().st_size > 64:
                        valid_candidates.append(candidate)
                except OSError:
                    continue
            if valid_candidates:
                has_autodance = True
                break

    if not has_autodance:
        for maybe_dir in glob.glob(os.path.join(source_root_str, "**", "*"), recursive=True):
            p = Path(maybe_dir)
            if not p.is_dir() or p.name.lower() != "autodance":
                continue
            if not _is_scoped_autodance_path(str(p)):
                continue
            for item in p.iterdir():
                if not item.is_file() or item.suffix.lower() == ".ckd":
                    continue
                if item.stat().st_size > 64:
                    has_autodance = True
                    break
            if has_autodance:
                break

    if has_autodance:
        logger.debug("Real autodance data detected for '%s'", effective_codename)

    result = NormalizedMapData(
        codename=effective_codename,
        song_desc=song_desc,
        music_track=music_track,
        dance_tape=dance_tape,
        karaoke_tape=karaoke_tape,
        cinematic_tape=cinematic_tape,
        media=media,
        sync=sync_data,
        video_start_time_override=sync_data.video_ms / 1000.0,
        source_dir=source_root,
        supplemental_roots=[Path(p) for p in supplemental_root_strs],
        is_html_source=is_html_source,
        is_jdnext_source=is_jdnext_source,
        has_autodance=has_autodance,
    )

    # Validation
    if not result.music_track:
        logger.warning("Critical data (musictrack) for '%s' is missing. Synthesizing empty track.", effective_codename)
        result.music_track = MusicTrackStructure()
        
    if not result.music_track.markers:
        logger.warning("MusicTrack for '%s' has no beat markers. Proceeding without beat synchronization.", effective_codename)

    logger.info(
        "Normalized '%s': %d markers, %d dance clips, %d karaoke clips",
        effective_codename,
        len(result.music_track.markers),
        len(result.dance_tape.clips) if result.dance_tape else 0,
        len(result.karaoke_tape.clips) if result.karaoke_tape else 0,
    )

    return result
