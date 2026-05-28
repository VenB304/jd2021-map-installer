"""Utilities for importing JDNext song database JSON into a local lookup cache."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

SONGDB_SYNTH_FILENAME = "jdnext_songdb_synth.json"
TICKS_PER_SECOND = 48000.0


@dataclass(frozen=True)
class SongDbSynthesisResult:
    """Summary of a JDNext song database synthesis operation."""

    source_entries: int
    usable_entries: int
    index_keys: int
    output_path: Path
    backup_path: Optional[Path]


_CACHE_PATH: Optional[Path] = None
_CACHE_MTIME_NS: int = -1
_CACHE_DATA: Optional[dict[str, Any]] = None


def _normalize_lookup_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").strip().lower())


def _to_int(value: Any) -> Optional[int]:
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


def _to_float(value: Any) -> Optional[float]:
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


def _extract_preview_payload(entry: dict[str, Any]) -> dict[str, Any]:
    raw = entry.get("assetsMetadata", {}).get("audioPreviewTrk")
    payload: dict[str, Any] = {}

    if isinstance(raw, dict):
        payload = raw
    elif isinstance(raw, str):
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            payload = {}

    preview_entry = _to_float(payload.get("PreviewEntry"))
    if preview_entry is None:
        preview_entry = _to_float(payload.get("previewEntry"))

    preview_loop_start = _to_float(payload.get("PreviewLoopStart"))
    if preview_loop_start is None:
        preview_loop_start = _to_float(payload.get("previewLoopStart"))

    preview_loop_end = _to_float(payload.get("PreviewLoopEnd"))
    if preview_loop_end is None:
        preview_loop_end = _to_float(payload.get("previewLoopEnd"))

    preview_duration = _to_float(payload.get("PreviewDuration"))
    if preview_duration is None:
        preview_duration = _to_float(payload.get("previewDuration"))

    def _extract_markers(raw_markers: Any) -> list[float]:
        if not isinstance(raw_markers, list):
            return []
        parsed: list[float] = []
        for marker in raw_markers:
            if isinstance(marker, dict):
                value = _to_float(marker.get("VAL"))
            else:
                value = _to_float(marker)
            if value is None:
                continue
            parsed.append(value)
        return parsed

    markers = _extract_markers(payload.get("Markers"))
    if not markers:
        markers = _extract_markers(payload.get("markers"))

    if (
        preview_loop_end is None or preview_loop_end <= 0
    ) and preview_loop_start is not None and preview_duration is not None and preview_duration > 0:
        # JDNext audioPreviewTrk uses duration in seconds while previewLoop values are beat-indexed.
        # Convert duration seconds to a beat index using marker ticks when available.
        start_beat_idx = int(round(preview_loop_start))
        if 0 <= start_beat_idx < len(markers):
            target_ticks = markers[start_beat_idx] + (preview_duration * TICKS_PER_SECOND)
            derived_idx: Optional[int] = None
            for idx, tick in enumerate(markers):
                if tick >= target_ticks:
                    derived_idx = idx
                    break

            if derived_idx is None and markers:
                derived_idx = len(markers) - 1

            if derived_idx is not None and derived_idx > start_beat_idx:
                preview_loop_end = float(derived_idx)

        if preview_loop_end is None or preview_loop_end <= preview_loop_start:
            # Fallback for malformed marker payloads: keep a forward, monotonic loop.
            preview_loop_end = preview_loop_start + preview_duration

    video_start_time = _to_float(payload.get("VideoStartTime"))
    if video_start_time is None:
        video_start_time = _to_float(payload.get("videoStartTime"))

    return {
        "preview_entry": preview_entry,
        "preview_loop_start": preview_loop_start,
        "preview_loop_end": preview_loop_end,
        "preview_duration": preview_duration,
        "video_start_time": video_start_time,
    }


def _entry_score(entry: dict[str, Any]) -> int:
    score = 0
    for key in (
        "map_name",
        "parent_map_name",
        "title",
        "artist",
        "credits",
        "difficulty",
        "sweat_difficulty",
        "coach_count",
        "original_jd_version",
        "preview_entry",
        "preview_loop_start",
        "preview_loop_end",
        "video_start_time",
    ):
        value = entry.get(key)
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        score += 1

    tags = entry.get("tags")
    if isinstance(tags, list) and tags:
        score += min(len(tags), 5)

    return score


def _upsert_index(
    index: dict[str, dict[str, Any]],
    key_raw: str,
    entry: dict[str, Any],
    *,
    key_kind: str,
) -> None:
    key = _normalize_lookup_key(key_raw)
    if not key:
        return

    existing = index.get(key)
    if existing is None:
        index[key] = entry
        return

    if key_kind == "title":
        # Title collisions are common for alternates/variants (e.g. base + ALT).
        # Prefer the entry whose map identifier actually matches this lookup key.
        existing_map_name = _normalize_lookup_key(str(existing.get("map_name", "") or ""))
        existing_parent = _normalize_lookup_key(str(existing.get("parent_map_name", "") or ""))
        entry_map_name = _normalize_lookup_key(str(entry.get("map_name", "") or ""))
        entry_parent = _normalize_lookup_key(str(entry.get("parent_map_name", "") or ""))

        existing_matches_key = existing_map_name == key or existing_parent == key
        entry_matches_key = entry_map_name == key or entry_parent == key

        if existing_matches_key and not entry_matches_key:
            return
        if entry_matches_key and not existing_matches_key:
            index[key] = entry
            return

    if _entry_score(entry) >= _entry_score(existing):
        index[key] = entry


def resolve_project_data_directory(start: Optional[Path] = None) -> Path:
    """Resolve the directory that stores installer settings and readjust index."""
    module_root = Path(__file__).resolve().parents[2]
    if (module_root / "map_readjust_index.json").exists() or (module_root / "installer_settings.json").exists():
        return module_root

    base = Path(start) if start else Path.cwd()
    for candidate in (base, *base.parents):
        if (candidate / "map_readjust_index.json").exists() or (candidate / "installer_settings.json").exists():
            return candidate

    return module_root


def resolve_songdb_synth_path(project_dir: Optional[Path] = None) -> Path:
    base = project_dir if project_dir is not None else resolve_project_data_directory()
    return Path(base) / SONGDB_SYNTH_FILENAME


def synthesize_jdnext_songdb(
    source_json_path: Path,
    output_dir: Optional[Path] = None,
) -> SongDbSynthesisResult:
    """Validate and convert a raw JDNext songdb JSON into a compact lookup file."""
    source_path = Path(source_json_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"Song database file not found: {source_path}")

    try:
        source_data = json.loads(source_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON file: {source_path}") from exc

    if not isinstance(source_data, dict):
        raise ValueError("JDNext song database must be a top-level object keyed by map UUID.")

    index: dict[str, dict[str, Any]] = {}
    usable_entries = 0

    for entry_id, raw_entry in source_data.items():
        if not isinstance(raw_entry, dict):
            continue

        map_name = str(raw_entry.get("mapName", "") or "").strip()
        if not map_name:
            continue

        usable_entries += 1
        parent_map_name = str(raw_entry.get("parentMapName", "") or "").strip()
        title = str(raw_entry.get("title", "") or "").strip()
        artist = str(raw_entry.get("artist", "") or "").strip()
        credits = str(raw_entry.get("credits", "") or "").strip()
        lyrics_color = str(raw_entry.get("lyricsColor", "") or "").strip()

        tags_raw = raw_entry.get("tags")
        tags = [str(tag).strip() for tag in tags_raw] if isinstance(tags_raw, list) else []
        tags = [tag for tag in tags if tag]

        preview = _extract_preview_payload(raw_entry)

        compact_entry: dict[str, Any] = {
            "entry_id": str(entry_id),
            "map_name": map_name,
            "parent_map_name": parent_map_name,
            "title": title,
            "artist": artist,
            "credits": credits,
            "tags": tags,
            "difficulty": _to_int(raw_entry.get("difficulty")),
            "sweat_difficulty": _to_int(raw_entry.get("sweatDifficulty")),
            "coach_count": _to_int(raw_entry.get("coachCount")),
            "original_jd_version": _to_int(raw_entry.get("originalJDVersion")),
            "status": _to_int(raw_entry.get("status")),
            "lyrics_color": lyrics_color,
            "preview_entry": preview["preview_entry"],
            "preview_loop_start": preview["preview_loop_start"],
            "preview_loop_end": preview["preview_loop_end"],
            "video_start_time": preview["video_start_time"],
        }

        _upsert_index(index, map_name, compact_entry, key_kind="map_name")
        _upsert_index(index, parent_map_name, compact_entry, key_kind="parent_map_name")
        _upsert_index(index, title, compact_entry, key_kind="title")

    if usable_entries == 0 or not index:
        raise ValueError(
            "Selected JSON does not look like a JDNext song database (no usable mapName entries found)."
        )

    destination_dir = Path(output_dir) if output_dir else resolve_project_data_directory()
    destination_dir.mkdir(parents=True, exist_ok=True)
    output_path = destination_dir / SONGDB_SYNTH_FILENAME

    backup_path: Optional[Path] = None
    if output_path.exists():
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = output_path.with_name(f"{output_path.stem}.backup_{timestamp}{output_path.suffix}")
        shutil.copy2(output_path, backup_path)

    payload = {
        "schema_version": 1,
        "source_kind": "jdnext_songdb",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source_file": str(source_path),
        "source_entries": len(source_data),
        "usable_entries": usable_entries,
        "index": index,
    }

    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return SongDbSynthesisResult(
        source_entries=len(source_data),
        usable_entries=usable_entries,
        index_keys=len(index),
        output_path=output_path,
        backup_path=backup_path,
    )


def load_songdb_synth(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """Load the synthesized songdb cache with lightweight mtime-based memoization."""
    global _CACHE_PATH, _CACHE_MTIME_NS, _CACHE_DATA

    resolved = Path(path) if path else resolve_songdb_synth_path()
    if not resolved.exists():
        _CACHE_PATH = resolved
        _CACHE_MTIME_NS = -1
        _CACHE_DATA = None
        return None

    stat = resolved.stat()
    if _CACHE_PATH == resolved and _CACHE_MTIME_NS == stat.st_mtime_ns and _CACHE_DATA is not None:
        return _CACHE_DATA

    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if not isinstance(payload.get("index"), dict):
        return None

    _CACHE_PATH = resolved
    _CACHE_MTIME_NS = stat.st_mtime_ns
    _CACHE_DATA = payload
    return payload


def find_songdb_entry(
    codename: str,
    map_name: str = "",
    title: str = "",
    synth_path: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    """Find a synthesized metadata entry by codename/mapName/title key."""
    payload = load_songdb_synth(synth_path)
    if not payload:
        return None

    index = payload.get("index", {})
    if not isinstance(index, dict):
        return None

    for candidate in (codename, map_name, title):
        key = _normalize_lookup_key(candidate)
        if not key:
            continue
        match = index.get(key)
        if isinstance(match, dict):
            return match

    return None


def _dedupe_codenames(raw_values: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        codename = str(value or "").strip()
        if not codename:
            continue
        if re.search(r"\s", codename):
            continue
        key = codename.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(codename)
    return deduped


def extract_jdu_songdb_codenames(source_json_path: Path) -> list[str]:
    """Extract codename keys from a raw JDU song database JSON file."""
    source_path = Path(source_json_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"Song database file not found: {source_path}")

    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON file: {source_path}") from exc

    if not isinstance(payload, dict):
        raise ValueError("JDU song database must be a top-level object keyed by codename.")

    codenames: list[str] = []
    for key, raw_entry in payload.items():
        if not isinstance(raw_entry, dict):
            continue
        if str(key).startswith("_"):
            continue

        key_name = str(key or "").strip()
        map_name = str(raw_entry.get("mapName", "") or "").strip()
        candidate = map_name or key_name
        if not candidate:
            continue
        codenames.append(candidate)

    result = _dedupe_codenames(codenames)
    if not result:
        raise ValueError("Selected JSON does not look like a JDU song database (no usable codenames found).")
    return result


def extract_jdnext_songdb_codenames(source_json_path: Path) -> list[str]:
    """Extract `mapName` codenames from a raw JDNext song database JSON file."""
    source_path = Path(source_json_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"Song database file not found: {source_path}")

    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON file: {source_path}") from exc

    if not isinstance(payload, dict):
        raise ValueError("JDNext song database must be a top-level object keyed by map UUID.")

    codenames: list[str] = []
    for raw_entry in payload.values():
        if not isinstance(raw_entry, dict):
            continue
        map_name = str(raw_entry.get("mapName", "") or "").strip()
        if not map_name:
            continue
        codenames.append(map_name)

    result = _dedupe_codenames(codenames)
    if not result:
        raise ValueError("Selected JSON does not look like a JDNext song database (no usable mapName entries found).")
    return result


def extract_jdlo_songdb_codenames(source_json_path: Path) -> list[str]:
    """Extract codename keys from a raw JDLO songs.json file."""
    source_path = Path(source_json_path)
    if not source_path.is_file():
        raise FileNotFoundError(f"Song database file not found: {source_path}")

    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON file: {source_path}") from exc

    if not isinstance(payload, dict):
        raise ValueError("JDLO songs database must be a top-level object keyed by codename.")

    codenames: list[str] = []
    for key, raw_entry in payload.items():
        if not isinstance(raw_entry, dict):
            continue
        key_name = str(key or "").strip()
        if not key_name:
            continue
        codenames.append(key_name)

    result = _dedupe_codenames(codenames)
    if not result:
        raise ValueError("Selected JSON does not look like a JDLO song database (no usable codenames found).")
    return result
