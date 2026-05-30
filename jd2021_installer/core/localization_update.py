"""Localization merge utilities for updating in-game ConsoleSave and loc8 entries."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

LANGUAGE_CODES_BY_INDEX: tuple[str, ...] = (
    "da",      # 0 Danish
    "de",      # 1 German
    "en",      # 2 English (US)
    "en",      # 3 English (UK)
    "es",      # 4 Spanish
    "fi",      # 5 Finnish
    "fr",      # 6 French
    "it",      # 7 Italian
    "ja",      # 8 Japanese
    "ko",      # 9 Korean
    "nl",      # 10 Dutch
    "nb",      # 11 Norwegian
    "pt-br",   # 12 Portuguese (Brazil)
    "ru",      # 13 Russian
    "sv",      # 14 Swedish
    "zh-cn",   # 15 Simplified Chinese
    "zh-tw",   # 16 Traditional Chinese
)

LOC8_LANGUAGE_FILES = {
    "da": ["localisation.itf_language_danish.loc8"],
    "de": ["localisation.itf_language_german.loc8"],
    "en": ["localisation.loc8", "localisation.itf_language_english.loc8", "localisation.itf_language_dev_reference.loc8"],
    "es": ["localisation.itf_language_spanish.loc8"],
    "fi": ["localisation.itf_language_finnish.loc8"],
    "fr": ["localisation.itf_language_french.loc8"],
    "it": ["localisation.itf_language_italian.loc8"],
    "ja": ["localisation.itf_language_japanese.loc8"],
    "ko": ["localisation.itf_language_korean.loc8"],
    "nl": ["localisation.itf_language_dutch.loc8"],
    "nb": ["localisation.itf_language_norwegian.loc8"],
    "pt-br": ["localisation.itf_language_portuguese.loc8", "localisation.itf_language_portuguese_br.loc8"],
    "ru": ["localisation.itf_language_russian.loc8"],
    "sv": ["localisation.itf_language_swedish.loc8"],
    "zh-cn": ["localisation.itf_language_simplifiedchinese.loc8"],
    "zh-tw": ["localisation.itf_language_traditionalchinese.loc8"],
}


@dataclass(frozen=True)
class LocalizationUpdateResult:
    """Summary of a localization update operation."""

    updated_existing: int
    added_new: int
    backup_path: Path | None
    console_save_path: Path | None
    updated_loc8_files: int
    loc8_added_or_updated: int


def resolve_localisation_dir(game_directory: Path) -> Path:
    """Resolve Localisation directory from a configured game directory."""
    base = Path(game_directory)
    candidates = (
        base / "data" / "EngineData" / "Localisation",
        base / "EngineData" / "Localisation",
        base / "data" / "EngineData" / "Localization",
        base / "EngineData" / "Localization",
    )
    for path in candidates:
        if path.is_dir():
            return path
    raise FileNotFoundError(
        "Localisation directory not found. Checked:\n- " + "\n- ".join(str(p) for p in candidates)
    )


def read_loc8(path: Path) -> dict[str, str]:
    with path.open("rb") as f:
        j = {}
        f.seek(8)
        amount_of_strings = int.from_bytes(f.read(4), "big")
        for _ in range(amount_of_strings):
            id_val = int.from_bytes(f.read(4), "big")
            length = int.from_bytes(f.read(4), "big")
            string_val = f.read(length).decode("utf-8")
            j[str(id_val)] = string_val
        return j


def write_loc8(path: Path, data: dict[str, str]) -> None:
    with path.open("wb") as f:
        f.write(b'\x00\x00\x00\x01\x00\x00\x00\x00')
        f.write(len(data).to_bytes(4, "big"))
        for k, v in data.items():
            string_bytes = v.encode("utf-8")
            f.write(int(k).to_bytes(4, "big"))
            f.write(len(string_bytes).to_bytes(4, "big"))
            f.write(string_bytes)
        f.write(b'\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x06\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF\xFF')


def _as_string(value: Any) -> str:
    return "" if value is None else str(value)


def _build_translations_array(loc_entry: dict[str, Any]) -> list[str]:
    translations = [""] * len(LANGUAGE_CODES_BY_INDEX)
    for idx, code in enumerate(LANGUAGE_CODES_BY_INDEX):
        translations[idx] = _as_string(loc_entry.get(code, ""))
    return translations


def _sanitize_translation_record(record: dict[str, Any]) -> None:
    record["masterText"] = _as_string(record.get("masterText", ""))
    if "isUsedInMCA" not in record:
        record["isUsedInMCA"] = False

    raw_translations = record.get("translations")
    safe_translations = [""] * len(LANGUAGE_CODES_BY_INDEX)
    if isinstance(raw_translations, list):
        for idx in range(min(len(raw_translations), len(safe_translations))):
            safe_translations[idx] = _as_string(raw_translations[idx])
    record["translations"] = safe_translations


def update_localization(
    localisation_json_path: Path,
    game_directory: Path,
) -> LocalizationUpdateResult:
    """Merge localisation data into ConsoleSave.json (if it exists) and any .loc8 files."""
    source_path = Path(localisation_json_path)

    if not source_path.is_file():
        raise FileNotFoundError(f"Localization file not found: {source_path}")

    with source_path.open("r", encoding="utf-8") as f:
        source_data = json.load(f)
    if not isinstance(source_data, dict):
        raise ValueError("Localization JSON must be a top-level object keyed by LocID.")

    localisation_dir = resolve_localisation_dir(game_directory)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 1. Update ConsoleSave.json if present
    console_save_path = localisation_dir / "Saves" / "ConsoleSave.json"
    updated_existing = 0
    added_new = 0
    backup_path = None

    if console_save_path.is_file():
        with console_save_path.open("r", encoding="utf-8") as f:
            target_data = json.load(f)
        if isinstance(target_data, dict):
            for loc_id, loc_entry in source_data.items():
                if not isinstance(loc_entry, dict):
                    continue

                master_text = _as_string(loc_entry.get("en", ""))
                translations = _build_translations_array(loc_entry)

                existing = target_data.get(loc_id)
                if isinstance(existing, dict):
                    existing["masterText"] = master_text
                    existing["translations"] = translations
                    if "isUsedInMCA" not in existing:
                        existing["isUsedInMCA"] = False
                    updated_existing += 1
                else:
                    target_data[loc_id] = {
                        "masterText": master_text,
                        "isUsedInMCA": False,
                        "translations": translations,
                    }
                    added_new += 1

            for value in target_data.values():
                if isinstance(value, dict):
                    _sanitize_translation_record(value)

            backup_path = console_save_path.with_name(f"{console_save_path.stem}.backup_{timestamp}{console_save_path.suffix}")
            shutil.copy2(console_save_path, backup_path)

            with console_save_path.open("w", encoding="utf-8") as f:
                json.dump(target_data, f, indent=2, ensure_ascii=False)
                f.write("\n")

    # 2. Update .loc8 files
    updated_loc8_files = 0
    loc8_added_or_updated = 0

    for lang_code, files in LOC8_LANGUAGE_FILES.items():
        for file_name in files:
            loc8_path = localisation_dir / file_name
            if loc8_path.is_file():
                try:
                    loc8_data = read_loc8(loc8_path)
                except Exception:
                    continue
                
                changed = False
                for loc_id, loc_entry in source_data.items():
                    if not isinstance(loc_entry, dict):
                        continue
                    
                    val = None
                    if lang_code in loc_entry and loc_entry[lang_code]:
                        val = _as_string(loc_entry[lang_code])
                    elif "en" in loc_entry and loc_entry["en"]:
                        val = _as_string(loc_entry["en"])

                    if val is not None:
                        loc8_data[str(loc_id)] = val
                        changed = True
                        loc8_added_or_updated += 1
                
                if changed:
                    loc8_backup = loc8_path.with_name(f"{loc8_path.stem}.backup_{timestamp}{loc8_path.suffix}")
                    if not loc8_backup.exists():
                        shutil.copy2(loc8_path, loc8_backup)
                    write_loc8(loc8_path, loc8_data)
                    updated_loc8_files += 1

    return LocalizationUpdateResult(
        updated_existing=updated_existing,
        added_new=added_new,
        backup_path=backup_path,
        console_save_path=console_save_path if console_save_path.is_file() else None,
        updated_loc8_files=updated_loc8_files,
        loc8_added_or_updated=loc8_added_or_updated,
    )
