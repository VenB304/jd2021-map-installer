"""CKD JSON → UbiArt Lua tape converter.

Converts normalized CKD JSON structures (dance tapes, karaoke tapes,
and cinematic/mainsequence tapes) into the UbiArt Lua format expected
by the JD2021 game engine.

Ported from V1's ``json_to_lua.py`` with typed interfaces.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger("jd2021.installers.tape_converter")


def _path_has_codename_component(path: Path, codename: str) -> bool:
    parts = [p.lower() for p in path.as_posix().split("/") if p]
    return codename.lower() in parts


def _filename_matches_codename(path: Path, codename: str) -> bool:
    cn = codename.lower()
    return bool(re.match(rf"^{re.escape(cn)}(?:[^a-z0-9]|$)", path.name.lower()))


def _pick_best_tape(candidates: List[Path], codename: str, preferred_tokens: List[str]) -> Optional[Path]:
    if not candidates:
        return None

    scoped = [p for p in candidates if _path_has_codename_component(p, codename)]
    if not scoped:
        scoped = [p for p in candidates if _filename_matches_codename(p, codename)]
    if not scoped:
        return None

    token_hits = [p for p in scoped if any(tok in p.name.lower() for tok in preferred_tokens)]
    if token_hits:
        scoped = token_hits

    # Stable selection so repeated installs pick the same source file.
    scoped = sorted(scoped, key=lambda p: p.as_posix().lower())
    return scoped[0]


# ---------------------------------------------------------------------------
# Core JSON → Lua converter  (recursive, ported from V1 json_to_lua.py)
# ---------------------------------------------------------------------------

def _convert_value(val: Any, indent_level: int = 0) -> str:
    """Recursively convert a Python value to UbiArt Lua literal syntax."""
    indent = "    " * indent_level

    if isinstance(val, dict):
        if "__class" in val:
            class_name = val["__class"]
            out = f"{{\n{indent}    NAME = \"{class_name}\",\n"
            out += f"{indent}    {class_name} = \n"
            out += f"{indent}    {{\n"
            for k, v in val.items():
                if k == "__class":
                    continue
                out += f"{indent}        {k} = {_convert_value(v, indent_level + 2)},\n"
            out += f"{indent}    }},\n{indent}}}"
            return out
        else:
            # Dictionary without __class → KEY/VAL pairs
            out = "{\n"
            for k, v in val.items():
                out += f"{indent}    {{\n"
                out += f"{indent}        KEY = \"{k}\",\n"
                out += f"{indent}        VAL = {_convert_value(v, indent_level + 2)},\n"
                out += f"{indent}    }},\n"
            out += f"{indent}}}"
            return out

    elif isinstance(val, list):
        out = "{\n"
        for item in val:
            if isinstance(item, (int, float, str, bool)) or item is None:
                out += f"{indent}    {{\n"
                out += f"{indent}        VAL = {_convert_value(item, indent_level + 2)}\n"
                out += f"{indent}    }},\n"
            else:
                out += f"{indent}    {_convert_value(item, indent_level + 1)},\n"
        out += f"{indent}}}"
        return out

    elif isinstance(val, str):
        escaped = val.replace('"', '\\"')
        return f'"{escaped}"'

    elif isinstance(val, bool):
        return "1" if val else "0"

    elif val is None:
        return "nil"

    elif isinstance(val, (float, int)):
        return str(val)

    return str(val)


def json_to_lua(data: Dict[str, Any]) -> str:
    """Convert a full CKD JSON structure to UbiArt Lua format.

    Args:
        data: Parsed CKD JSON dictionary (typically with COMPONENTS array).

    Returns:
        Complete Lua string starting with ``params = ...``.
    """
    return "params =\n" + _convert_value(data, 0)


# ---------------------------------------------------------------------------
# File-level converters
# ---------------------------------------------------------------------------

def _load_ckd_json(ckd_path: Path) -> Dict[str, Any]:
    """Load a CKD file as JSON. Handles the common UbiArt CKD JSON format.
    
    Uses raw_decode to handle files with trailing garbage and 
    binary headers before the JSON start.
    """
    try:
        content_bytes = ckd_path.read_bytes()
        if not content_bytes:
            return {}
            
        # Try to find JSON start '{' to handle leading binary junk (e.g. from some IPK tools)
        start_idx = content_bytes.find(b'{')
        if start_idx == -1:
            logger.debug("No JSON object found in CKD %s (it might be binary)", ckd_path.name)
            return {}
            
        content = content_bytes[start_idx:].decode("utf-8-sig", errors="replace").strip()
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(content)
        return obj
    except Exception as e:
        logger.debug("Non-critical tape parsing error for %s: %s", ckd_path.name, e)
        return {}


from jd2021_installer.parsers.binary_ckd import parse_binary_ckd


def convert_tape_file(ckd_path: Path, output_path: Path) -> bool:
    """Convert a CKD (JSON or Binary) tape file to UbiArt Lua format.

    Works for dance tapes (.dtape.ckd), karaoke tapes (.ktape.ckd),
    and mainsequence tapes (_mainsequence.tape.ckd).

    Args:
        ckd_path:    Path to the source CKD file.
        output_path: Path to write the Lua output.

    Returns:
        True if conversion succeeded, False otherwise.
    """
    if not ckd_path.is_file():
        logger.error("Tape CKD not found: %s", ckd_path)
        return False

    try:
        # 1. Try JSON parsing first
        data = _load_ckd_json(ckd_path)
        
        # 2. If JSON is empty/invalid, try Binary parsing
        if not data:
            logger.debug("Tape %s is not JSON, attempting binary parse", ckd_path.name)
            parsed = parse_binary_ckd(ckd_path.read_bytes(), ckd_path.name)
            if hasattr(parsed, "as_ubiart_dict"):
                data = parsed.as_ubiart_dict()
            elif isinstance(parsed, dict):
                # Some non-tape assets (for example autodance templates) parse to
                # lightweight dictionaries without clip arrays. They are still
                # representable in Lua and should not be treated as hard errors.
                data = parsed
            else:
                logger.error("Binary parse of %s returned non-convertible result", ckd_path.name)
                return False

        lua_str = json_to_lua(data)

        # 1. Ensure 'pictos' is lowercase
        # 2. Ensure '.png' is used instead of '.ckd' or '.tga' for pictos
        import re
        lua_str = re.sub(r'([Pp]ictos)/([^"]+)\.(ckd|tga)', r'pictos/\2.png', lua_str)
        # 3. Align all system paths with V1 lowercase conventions
        lua_str = lua_str.replace('"World/MAPS/', '"world/maps/')
        lua_str = lua_str.replace('"MenuArt/', '"menuart/')
        lua_str = lua_str.replace('"Autodance/', '"autodance/')
        lua_str = lua_str.replace('"Timeline/pictos/', '"timeline/pictos/')
        lua_str = lua_str.replace('"Timeline/', '"timeline/')

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(lua_str, encoding="utf-8")
        logger.debug("Converted tape: %s → %s", ckd_path.name, output_path.name)
        return True

    except Exception as e:
        logger.error("Failed to convert tape CKD %s: %s", ckd_path.name, e)
        return False


def convert_dance_tape(ckd_path: Path, target_dir: Path, codename: str) -> bool:
    """Convert a dance tape CKD to the game's timeline directory.

    Input:  ``*_TML_Dance.dtape.ckd``
    Output: ``Timeline/{codename}_TML_Dance.dtape``
    """
    output = target_dir / "timeline" / f"{codename}_TML_Dance.dtape"
    return convert_tape_file(ckd_path, output)


def convert_karaoke_tape(ckd_path: Path, target_dir: Path, codename: str) -> bool:
    """Convert a karaoke tape CKD to the game's timeline directory.

    Input:  ``*_TML_Karaoke.ktape.ckd``
    Output: ``Timeline/{codename}_TML_Karaoke.ktape``
    """
    output = target_dir / "timeline" / f"{codename}_TML_Karaoke.ktape"
    return convert_tape_file(ckd_path, output)


def convert_cinematic_tape(ckd_path: Path, target_dir: Path, codename: str) -> bool:
    """Convert a mainsequence tape CKD to the Cinematics directory.

    Input:  ``*_mainsequence.tape.ckd`` or ``*_MainSequence.tape.ckd``
    Output: ``Cinematics/{codename}_MainSequence.tape``
    """
    output = target_dir / "cinematics" / f"{codename}_MainSequence.tape"
    return convert_tape_file(ckd_path, output)


def convert_beats_tape(ckd_path: Path, target_dir: Path, codename: str) -> bool:
    """Convert a beats tape CKD to the timeline directory.

    Input:  ``*.btape.ckd``
    Output: ``timeline/{codename}.btape``
    """
    output = target_dir / "timeline" / f"{codename}.btape"
    return convert_tape_file(ckd_path, output)


def _copy_loose_tape(source_path: Path, output_path: Path, tape_label: str) -> bool:
    """Copy an already-converted tape file (non-CKD) to target timeline."""
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, output_path)
        logger.debug("Copied %s tape: %s -> %s", tape_label, source_path.name, output_path.name)
        return True
    except Exception as e:
        logger.error("Failed to copy %s tape %s: %s", tape_label, source_path.name, e)
        return False


def auto_convert_tapes(source_dir: Path, target_dir: Path, codename: str) -> int:
    """Auto-detect and convert all tape CKD files in a directory.

    Searches ``source_dir`` recursively for dtape, ktape, btape, and
    mainsequence tape CKDs and converts them to UbiArt Lua format.

    Returns:
        Number of tapes successfully converted.
    """
    converted = 0

    all_files = [p for p in source_dir.rglob("*") if p.is_file()]
    ckd_files = [p for p in all_files if p.name.lower().endswith(".ckd")]

    dance_candidates = [
        p for p in ckd_files
        if "dtape" in p.name.lower() and "adtape" not in p.name.lower()
    ]
    karaoke_candidates = [
        p for p in ckd_files
        if "ktape" in p.name.lower()
    ]
    cinematic_candidates = [
        p for p in ckd_files
        if "mainsequence" in p.name.lower() and "tape" in p.name.lower()
    ]
    beats_candidates = [
        p for p in ckd_files
        if "btape" in p.name.lower()
    ]

    dance_src = _pick_best_tape(dance_candidates, codename, ["tml_dance", "dance"])
    if dance_src:
        if convert_dance_tape(dance_src, target_dir, codename):
            converted += 1
    else:
        # Manual/IPK maps can already ship plain .dtape (non-CKD).
        loose_dance_candidates = [
            p for p in all_files
            if "dtape" in p.name.lower()
            and "adtape" not in p.name.lower()
            and ".ckd" not in p.name.lower()
        ]
        loose_dance_src = _pick_best_tape(loose_dance_candidates, codename, ["tml_dance", "dance"])
        if loose_dance_src and _copy_loose_tape(
            loose_dance_src,
            target_dir / "timeline" / f"{codename}_TML_Dance.dtape",
            "dance",
        ):
            converted += 1

    karaoke_src = _pick_best_tape(karaoke_candidates, codename, ["tml_karaoke", "karaoke"])
    if karaoke_src:
        if convert_karaoke_tape(karaoke_src, target_dir, codename):
            converted += 1
    else:
        # Manual/IPK maps can already ship plain .ktape (non-CKD).
        loose_karaoke_candidates = [
            p for p in all_files
            if "ktape" in p.name.lower() and ".ckd" not in p.name.lower()
        ]
        loose_karaoke_src = _pick_best_tape(loose_karaoke_candidates, codename, ["tml_karaoke", "karaoke"])
        if loose_karaoke_src and _copy_loose_tape(
            loose_karaoke_src,
            target_dir / "timeline" / f"{codename}_TML_Karaoke.ktape",
            "karaoke",
        ):
            converted += 1

    cinematic_src = _pick_best_tape(cinematic_candidates, codename, ["mainsequence"])
    if cinematic_src and convert_cinematic_tape(cinematic_src, target_dir, codename):
        converted += 1

    beats_src = _pick_best_tape(beats_candidates, codename, ["btape"])
    if beats_src and convert_beats_tape(beats_src, target_dir, codename):
        converted += 1

    logger.debug("Auto-converted %d tape(s) for '%s'", converted, codename)
    return converted
