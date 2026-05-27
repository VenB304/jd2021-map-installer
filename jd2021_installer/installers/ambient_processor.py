"""Ambient sound processor.

Processes ambient sound templates (`amb_*.tpl.ckd` and
`set_amb_*.tpl.ckd`) into the corresponding engine-ready `.ilu`
and `.tpl` Lua pairs.

Ported from V1's ``ubiart_lua.py`` (`process_ambient_sound`).
"""

from __future__ import annotations

import json
import logging
import re
import zlib
import wave
from pathlib import Path
from typing import Any, Dict, List, Tuple

from jd2021_installer.parsers.binary_ckd import parse_binary_ckd
from jd2021_installer.installers.tape_converter import _convert_value, _load_ckd_json

logger = logging.getLogger("jd2021.installers.ambient_processor")

# Intro AMB generation is enabled by default for all supported source modes.
INTRO_AMB_ATTEMPT_ENABLED = True


def _silence_intro_amb_wavs(amb_out_dir: Path, codename: str) -> int:
    intro_wavs = list(amb_out_dir.glob("*_intro.wav"))
    expected = amb_out_dir / f"amb_{codename.lower()}_intro.wav"
    if expected not in intro_wavs:
        intro_wavs.append(expected)

    written = 0
    frames = int(round(48000 * 0.25))
    for wav_path in intro_wavs:
        try:
            with wave.open(str(wav_path), "w") as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(48000)
                wf.writeframes(b"\x00\x00\x00\x00" * frames)
            written += 1
        except Exception as exc:
            logger.debug("Failed to silence intro AMB '%s': %s", wav_path.name, exc)
    return written


def _remove_intro_amb_assets(amb_out_dir: Path) -> int:
    removed = 0
    for p in amb_out_dir.glob("*"):
        if not p.is_file():
            continue
        name_low = p.stem.lower()
        if name_low.startswith("amb_") and "intro" in name_low and p.suffix.lower() in {".wav", ".tpl", ".ilu"}:
            try:
                p.unlink()
                removed += 1
            except Exception as exc:
                logger.debug("Failed to remove intro AMB asset '%s': %s", p.name, exc)
    return removed


def _resolve_amb_dir(target_dir: Path) -> Path:
    """Resolve AMB output dir with compatibility for existing lowercase installs."""
    candidates = [
        target_dir / "Audio" / "AMB",
        target_dir / "audio" / "AMB",
        target_dir / "Audio" / "amb",
        target_dir / "audio" / "amb",
    ]
    return next((p for p in candidates if p.exists()), candidates[0])


def _path_has_codename_component(path: Path, codename: str) -> bool:
    parts = [p.lower() for p in path.as_posix().split("/") if p]
    return codename.lower() in parts


def _filename_matches_codename(path: Path, codename: str) -> bool:
    cn = codename.lower()
    return bool(re.match(rf"^{re.escape(cn)}(?:[^a-z0-9]|$)", path.name.lower()))


def process_ambient_tpl(
    json_data: Dict[str, Any],
    map_name: str,
    amb_filename: str
) -> Tuple[str, str, List[str]]:
    """Process an ambient sound .tpl.ckd dictionary.

    Args:
        json_data:    Parsed JSON dict from the ambient .tpl.ckd
        map_name:     The map codename (e.g. "RainOnMe")
        amb_filename: Original filename (e.g. "amb_rainonme.tpl.ckd")

    Returns:
        (ilu_content, tpl_content, audio_file_paths)
    """
    try:
        components = json_data.get("COMPONENTS", [])
        if not components:
            logger.debug("No COMPONENTS block in %s", amb_filename)
            return "", "", []

        sound_component = components[0]
        if "soundList" not in sound_component:
            logger.debug("No soundList in first component of %s", amb_filename)
            return "", "", []

        raw_sound_list = sound_component["soundList"]
        audio_file_paths: List[str] = []
        sound_list: List[Dict[str, Any]] = []

        # Normalize entries into SoundDescriptor_Template-like objects so Lua
        # serialization always yields a valid descriptor list.
        for entry in raw_sound_list:
            if not isinstance(entry, dict):
                continue

            files = entry.get("files", [])
            normalized_files: List[str] = []
            first_file_name = Path(amb_filename.replace('.tpl.ckd', '')).stem

            for f in files:
                ref = None
                if isinstance(f, str):
                    ref = f
                elif isinstance(f, dict) and "VAL" in f:
                    ref = str(f["VAL"])
                if ref:
                    normalized_files.append(ref)
                    audio_file_paths.append(ref)
                    if first_file_name == Path(amb_filename.replace('.tpl.ckd', '')).stem:
                        first_file_name = Path(ref).stem

            if "__class" in entry:
                # Keep richer descriptors when present, but normalize file payload.
                normalized_entry = dict(entry)
                normalized_entry["files"] = normalized_files
            else:
                normalized_entry = {
                    "__class": "SoundDescriptor_Template",
                    "name": first_file_name,
                    "volume": 0,
                    "category": "AMB",
                    "limitCategory": "",
                    "limitMode": 0,
                    "maxInstances": 4294967295,
                    "files": normalized_files,
                    "serialPlayingMode": 0,
                    "serialStoppingMode": 0,
                }

            sound_list.append(normalized_entry)

        # Find referenced audio files
        for entry in sound_list:
            files = entry.get("files", [])
            for f in files:
                # the V1 format has raw strings in the files list initially
                if isinstance(f, str):
                    audio_file_paths.append(f)
                elif isinstance(f, dict) and "VAL" in f:
                    audio_file_paths.append(f["VAL"])

        # Use our tape converter's _convert_value for the Lua serialization
        lua_str = _convert_value(sound_list, indent_level=0)

        ilu_name = amb_filename.replace('.tpl.ckd', '.ilu')

        ilu_content = (
            f"DESCRIPTOR = {lua_str}\n"
            f"appendTable(component.SoundComponent_Template.soundList,DESCRIPTOR)"
        )

        tpl_content = (
            'params=\n{\n\tNAME="Actor_Template",\n\tActor_Template=\n\t{\n'
            '\t\tCOMPONENTS=\n\t\t{\n\t\t}\n\t}\n}\n'
            'includeReference("EngineData/Misc/Components/SoundComponent.ilu")\n'
            f'includeReference("world/maps/{map_name.lower()}/audio/amb/{ilu_name}")'
        )

        return ilu_content, tpl_content, audio_file_paths

    except Exception as e:
        logger.error("Error processing AMB template %s: %s", amb_filename, e)
        return "", "", []


def _generate_synthetic_amb(
    wav_ckd_path: Path,
    output_dir: Path,
    codename: str
) -> bool:
    """Generate synthetic ILU/TPL for a .wav.ckd without a template."""
    base = wav_ckd_path.name.replace(".wav.ckd", "")
    map_lower = codename.lower()
    wav_rel = f"world/maps/{map_lower}/audio/amb/{base}.wav"

    # Synthetic ILU
    ilu_content = (
        f'DESCRIPTOR =\n{{\n'
        f'\t{{\n\t\tNAME = "SoundDescriptor_Template",\n'
        f'\t\tSoundDescriptor_Template =\n\t\t{{\n'
        f'\t\t\tname = "{base}",\n\t\t\tvolume = 0,\n'
        f'\t\t\tcategory = "AMB",\n\t\t\tlimitCategory = "",\n'
        f'\t\t\tlimitMode = 0,\n\t\t\tmaxInstances = 4294967295,\n'
        f'\t\t\tfiles =\n\t\t\t{{\n\t\t\t\t{{\n'
        f'\t\t\t\t\tVAL = "{wav_rel}",\n'
        f'\t\t\t\t}},\n\t\t\t}},\n'
        f'\t\t\tserialPlayingMode = 0,\n\t\t\tserialStoppingMode = 0,\n'
        f'\t\t}},\n\t}},\n}}\n'
    )

    # Synthetic TPL
    tpl_content = (
        f'params =\n{{\n\tNAME = "Actor_Template",\n'
        f'\tActor_Template =\n\t{{\n\t\tCOMPONENTS =\n\t\t{{\n'
        f'\t\t\t{{\n\t\t\t\tNAME = "SoundComponent_Template",\n'
        f'\t\t\t\tSoundComponent_Template =\n\t\t\t\t{{\n'
        f'\t\t\t\t\tsoundList = {{}},\n'
        f'\t\t\t\t\tSoundwichEvent = "",\n'
        f'\t\t\t\t}},\n\t\t\t}},\n\t\t}},\n\t}},\n}}\n'
        'includeReference("EngineData/Misc/Components/SoundComponent.ilu")\n'
        f'includeReference("world/maps/{codename.lower()}/audio/amb/{base}.ilu")'
    )

    try:
        (output_dir / f"{base}.ilu").write_text(ilu_content, encoding="utf-8")
        (output_dir / f"{base}.tpl").write_text(tpl_content, encoding="utf-8")
        
        # Decode the CKD audio
        from jd2021_installer.installers.media_processor import extract_ckd_audio_v1
        decoded = extract_ckd_audio_v1(wav_ckd_path, output_dir)
        if decoded:
            decoded_path = Path(decoded)
            target_wav = output_dir / f"{base}.wav"
            if decoded_path.exists() and decoded_path != target_wav:
                if target_wav.exists():
                    target_wav.unlink()
                decoded_path.rename(target_wav)
        return True
    except Exception as e:
        logger.error("Failed to generate synthetic AMB for %s: %s", base, e)
        return False


def inject_ambient_actors(target_dir: Path, codename: str) -> bool:
    """Inject AMB actors into the map's audio.isc file."""
    audio_isc = target_dir / "audio" / f"{codename}_audio.isc"
    if not audio_isc.is_file():
        # Case insensitive fallback
        isc_files = list((target_dir / "audio").glob("*_audio.isc"))
        if isc_files:
            audio_isc = isc_files[0]
        else:
            return False

    amb_tpls = []
    for amb_root in (
        target_dir / "Audio" / "AMB",
        target_dir / "audio" / "AMB",
        target_dir / "Audio" / "amb",
        target_dir / "audio" / "amb",
    ):
        if amb_root.exists():
            amb_tpls.extend(amb_root.glob("*.tpl"))
    # De-duplicate in case path aliases point to the same folder (Windows).
    amb_tpls = sorted({tpl.resolve() for tpl in amb_tpls})
    if not amb_tpls:
        return False

    try:
        content = audio_isc.read_text(encoding="utf-8", errors="replace")
        
        # Build actor blocks, skipping any that already exist in the ISC
        amb_actors = ""
        for i, tpl in enumerate(sorted(amb_tpls)):
            amb_name = tpl.stem
            # Intro AMB is timeline-driven via SoundSetClip. Injecting an intro
            # actor here can trigger early auto-play before tape timing is applied.
            if amb_name.lower().startswith("amb_") and "intro" in amb_name.lower():
                continue
            # V1 Parity: skip inject if actor already exists (e.g. intro AMB injected by media_processor)
            if f'USERFRIENDLY="{amb_name}"' in content:
                continue
                
            z = f"0.{i + 2:06d}"
            amb_actors += (
                f'\t\t<ACTORS NAME="Actor">\n'
                f'\t\t\t<Actor RELATIVEZ="{z}" SCALE="1.000000 1.000000" '
                f'xFLIPPED="0" USERFRIENDLY="{amb_name}" '
                f'POS2D="0.000000 0.000000" ANGLE="0.000000" '
                f'INSTANCEDATAFILE="" LUA="World/MAPS/{codename}/audio/AMB/{amb_name}.tpl">\n'
                f'\t\t\t\t<COMPONENTS NAME="SoundComponent">\n'
                f'\t\t\t\t\t<SoundComponent />\n'
                f'\t\t\t\t</COMPONENTS>\n'
                f'\t\t\t</Actor>\n'
                f'\t\t</ACTORS>\n'
            )

        if not amb_actors:
            return False

        # Inject before <sceneConfigs>
        import re
        pattern = re.compile(r'([ \t]*<sceneConfigs>)', re.IGNORECASE)
        match = pattern.search(content)
        if match:
            new_content = content[:match.start()] + amb_actors + content[match.start():]
            audio_isc.write_text(new_content, encoding="utf-8")
            logger.info("Injected %d AMB actor(s) into %s", len(amb_tpls), audio_isc.name)
            return True
    except Exception as e:
        logger.error("Failed to inject AMB actors into %s: %s", audio_isc.name, e)
    
    return False


def _remove_intro_amb_actor_from_isc(target_dir: Path, codename: str) -> bool:
    """Remove stale intro AMB actor blocks from audio ISC.

    Intro AMB should be started by MainSequence SoundSetClip timing, not by
    auto-spawned actors in the scene file.
    """
    audio_isc = target_dir / "audio" / f"{codename}_audio.isc"
    if not audio_isc.exists():
        isc_files = list((target_dir / "audio").glob("*_audio.isc")) if (target_dir / "audio").exists() else []
        audio_isc = isc_files[0] if isc_files else None
    if audio_isc is None or not Path(audio_isc).exists():
        return False

    content = Path(audio_isc).read_text(encoding="utf-8", errors="replace")
    updated = re.sub(
        r"\s*<ACTORS NAME=\"Actor\">\s*"
        r"<Actor[^>]*USERFRIENDLY=\"amb_[^\"]*intro[^\"]*\"[^>]*>\s*"
        r"<COMPONENTS NAME=\"SoundComponent\">\s*<SoundComponent\s*/>\s*</COMPONENTS>\s*"
        r"</Actor>\s*</ACTORS>\s*",
        "\n",
        content,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if updated != content:
        Path(audio_isc).write_text(updated, encoding="utf-8")
        logger.debug("Removed intro AMB actor from %s", Path(audio_isc).name)
        return True
    return False


def _find_table_bounds(lua_text: str, key: str) -> tuple[int, int] | None:
    """Find bounds of a top-level Lua table assigned to `key`.

    Returns `(open_brace_index, close_brace_index)` for the table body.
    """
    m = re.search(rf"\b{re.escape(key)}\s*=\s*\{{", lua_text)
    if not m:
        return None
    open_idx = m.end() - 1
    depth = 0
    in_string = False
    escaped = False
    for idx in range(open_idx, len(lua_text)):
        ch = lua_text[idx]
        if in_string:
            if escaped:
                escaped = False
                continue
            if ch == "\\":
                escaped = True
                continue
            if ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
            continue
        if ch == "{":
            depth += 1
            continue
        if ch == "}":
            depth -= 1
            if depth == 0:
                return open_idx, idx
    return None


def _insert_lua_table_entry(lua_text: str, table_key: str, entry_block: str) -> str | None:
    bounds = _find_table_bounds(lua_text, table_key)
    if not bounds:
        return None
    _, close_idx = bounds
    insert_at = close_idx
    return lua_text[:insert_at] + entry_block + lua_text[insert_at:]


def _remove_intro_track_entries(lua_text: str, intro_tpl_name: str) -> str:
    """Remove TapeTrack entries tied to an intro AMB clip.

    Some maps play intro AMB correctly without explicit Tracks blocks. Keeping stale
    injected track entries can make generated tapes diverge from known-working files.
    """
    entry_pat = re.compile(
        r"\{\s*"
        r"TapeTrack\s*=\s*\{[^{}]*?"
        r"Name\s*=\s*\""
        + re.escape(intro_tpl_name)
        + r"\"[^{}]*?\},\s*"
        r"\},?",
        re.IGNORECASE | re.DOTALL,
    )
    return entry_pat.sub("", lua_text)


def _remove_empty_tracks_table(lua_text: str) -> str:
    """Remove an empty Tracks table block if present."""
    return re.sub(
        r"\n\s*Tracks\s*=\s*\{\s*\},\s*\n",
        "\n",
        lua_text,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _normalize_clips_table_end(lua_text: str) -> str:
    """Normalize compact Clips table closure (`},},`) into multiline form."""
    return re.sub(
        r"\},\s*\},\s*(\n\s*TapeClock\s*=)",
        "},\n        },\\1",
        lua_text,
        count=1,
    )


def _normalize_tapeclock_zero(lua_text: str) -> str:
    """Normalize TapeClock to 0 (official mainsequence behavior)."""
    return re.sub(r"(\bTapeClock\s*=\s*)\d+", r"\g<1>0", lua_text, count=1)


def _derive_intro_window_from_hide_ui(tape_lua: str) -> tuple[int, int] | None:
    """Return (StartTime, Duration) from HideUserInterfaceClip when available.

    Empirically this clip's timing matches the desired intro window on maps like
    Balance, while large negative intro durations can cause early playback.
    """
    pat = re.compile(
        r"HideUserInterfaceClip\s*=\s*\{[^{}]*?"
        r"StartTime\s*=\s*([-+]?\d+)\s*,[^{}]*?"
        r"Duration\s*=\s*(\d+)\s*,[^{}]*?"
        r"EventType\s*=\s*18\b[^{}]*?\}",
        re.IGNORECASE | re.DOTALL,
    )
    m = pat.search(tape_lua)
    if not m:
        return None
    try:
        st = int(m.group(1))
        dur = int(m.group(2))
        if dur <= 0:
            return None
        return st, dur
    except Exception:
        return None


def _remove_intro_amb_soundset_clips(target_dir: Path, codename: str) -> bool:
    """Remove intro AMB SoundSetClip entries from MainSequence tape.

    This is used when intro AMB generation is hard-disabled so stale source clips
    cannot still trigger early playback.
    """
    tape_candidates = [
        target_dir / "Cinematics" / f"{codename}_MainSequence.tape",
        target_dir / "cinematics" / f"{codename}_MainSequence.tape",
    ]
    tape_path = next((p for p in tape_candidates if p.exists()), None)
    if tape_path is None:
        extra = list((target_dir / "Cinematics").glob("*_MainSequence.tape")) if (target_dir / "Cinematics").exists() else []
        if not extra and (target_dir / "cinematics").exists():
            extra = list((target_dir / "cinematics").glob("*_MainSequence.tape"))
        tape_path = extra[0] if extra else None
    if tape_path is None or not tape_path.exists():
        return False

    content = tape_path.read_text(encoding="utf-8", errors="replace")
    map_lower = re.escape(codename.lower())

    # Remove intro AMB clip blocks for this map.
    clip_pat = re.compile(
        r"\{\s*"
        r"NAME\s*=\s*\"SoundSetClip\"\s*,\s*"
        r"SoundSetClip\s*=\s*\{[^{}]*?"
        r"SoundSetPath\s*=\s*\""
        + rf"[^\"]*world/maps/{map_lower}/audio/amb/[^\"]*intro[^\"]*\.tpl"
        + r"\"[^{}]*?\}\s*,\s*\},?",
        re.IGNORECASE | re.DOTALL,
    )
    updated = clip_pat.sub("", content)

    # Remove matching intro tracks if present.
    intro_track_pat = re.compile(
        r"\{\s*"
        r"TapeTrack\s*=\s*\{[^{}]*?"
        r"Name\s*=\s*\"[^\"]*intro[^\"]*\.tpl\""
        r"[^{}]*?\}\s*,\s*\},?",
        re.IGNORECASE | re.DOTALL,
    )
    updated = intro_track_pat.sub("", updated)

    updated = _remove_empty_tracks_table(updated)
    updated = _normalize_clips_table_end(updated)
    updated = _normalize_tapeclock_zero(updated)

    if updated != content:
        tape_path.write_text(updated, encoding="utf-8")
        logger.debug("Removed intro AMB SoundSetClip from %s (disabled mode)", tape_path.name)
        return True
    return False


def _inject_intro_amb_soundset_clip(target_dir: Path, codename: str, attempt_enabled: bool = True) -> bool:
    """Ensure MainSequence tape triggers intro AMB via SoundSetClip.

    Some converted maps end up with empty MainSequence tapes, so intro AMB files
    exist but are never triggered at gameplay start.
    """
    allow_injection = attempt_enabled

    amb_dirs = [
        target_dir / "Audio" / "AMB",
        target_dir / "audio" / "AMB",
        target_dir / "Audio" / "amb",
        target_dir / "audio" / "amb",
    ]
    intro_tpls: list[Path] = []
    for amb_dir in amb_dirs:
        if not amb_dir.is_dir():
            continue
        for tpl in amb_dir.glob("*.tpl"):
            if "intro" in tpl.stem.lower() and tpl.stem.lower().startswith("amb_"):
                intro_tpls.append(tpl)

    if not intro_tpls:
        return False

    intro_tpl = sorted(intro_tpls, key=lambda p: p.name.lower())[0]
    intro_tpl_name = intro_tpl.name
    soundset_path = f"world/maps/{codename.lower()}/audio/amb/{intro_tpl_name}"

    clip_duration_ms = 432
    intro_wav = intro_tpl.with_suffix(".wav")
    if intro_wav.exists():
        try:
            with wave.open(str(intro_wav), "rb") as wf:
                n_frames = wf.getnframes()
                sample_rate = wf.getframerate() or 48000
                clip_duration_ms = max(216, int(round((n_frames / float(sample_rate)) * 1000.0)))
        except Exception as exc:
            logger.debug("Could not probe intro AMB WAV duration for %s: %s", intro_wav.name, exc)

    trk_candidates = [
        target_dir / "Audio" / f"{codename}.trk",
        target_dir / "audio" / f"{codename}.trk",
    ]
    trk_path = next((p for p in trk_candidates if p.exists()), None)
    vst_cap_ms: int | None = None
    cutter_cap_ms: int | None = None
    beat_ms: int | None = None
    start_beat: int | None = None
    if trk_path is not None:
        try:
            trk_content = trk_path.read_text(encoding="utf-8", errors="replace")
            m = re.search(r"videoStartTime\s*=\s*([-+]?\d*\.?\d+)", trk_content)
            if m:
                vst = float(m.group(1))
                if vst < 0:
                    vst_cap_ms = max(216, int(round(abs(vst) * 1000.0)))
                    if clip_duration_ms == 432:
                        clip_duration_ms = vst_cap_ms

            sb_match = re.search(r"startBeat\s*=\s*([-+]?\d+)", trk_content)
            if allow_injection and sb_match:
                start_beat = int(sb_match.group(1))
                start_idx = abs(start_beat)
                marker_vals = [int(v) for v in re.findall(r"VAL\s*=\s*(\d+)", trk_content)]
                if len(marker_vals) > 1:
                    beat_ms = max(1, int(round((marker_vals[1] - marker_vals[0]) / 48.0)))
                if 0 < start_idx < len(marker_vals):
                    cutter_cap_ms = max(216, int(round((marker_vals[start_idx] / 48.0) + 85.0)))
                    clip_duration_ms = cutter_cap_ms
        except Exception as exc:
            logger.debug("Could not parse videoStartTime for %s: %s", codename, exc)

    if cutter_cap_ms is not None:
        logger.debug("Using cutter-style intro cap from markers for %s: %d ms", codename, cutter_cap_ms)

    # Default legacy anchor (pre-fallback), then refine below.
    clip_start_ms = -clip_duration_ms
    clip_start_offset_s: float | None = None
    intro_wav_duration_ms: int | None = None
    if intro_wav.exists():
        try:
            with wave.open(str(intro_wav), "rb") as wf:
                n_frames = wf.getnframes()
                sample_rate = wf.getframerate() or 48000
                intro_wav_duration_ms = int(round((n_frames / float(sample_rate)) * 1000.0))
        except Exception as exc:
            logger.debug("Could not probe intro WAV duration for offset synthesis (%s): %s", intro_wav.name, exc)

    tape_candidates = [
        target_dir / "Cinematics" / f"{codename}_MainSequence.tape",
        target_dir / "cinematics" / f"{codename}_MainSequence.tape",
    ]
    tape_path = next((p for p in tape_candidates if p.exists()), None)
    if tape_path is None:
        extra = list((target_dir / "Cinematics").glob("*_MainSequence.tape")) if (target_dir / "Cinematics").exists() else []
        if not extra and (target_dir / "cinematics").exists():
            extra = list((target_dir / "cinematics").glob("*_MainSequence.tape"))
        tape_path = extra[0] if extra else None
    if tape_path is None or not tape_path.exists():
        return False

    content = tape_path.read_text(encoding="utf-8", errors="replace")

    existing_window: tuple[int, int] | None = None
    existing_block_pat = re.compile(
        r"SoundSetClip\s*=\s*\{[^{}]*?"
        r"StartTime\s*=\s*([-+]?\d+)\s*,[^{}]*?"
        r"Duration\s*=\s*(\d+)\s*,[^{}]*?"
        r"SoundSetPath\s*=\s*\""
        + re.escape(soundset_path)
        + r"\"[^{}]*?\}",
        re.IGNORECASE | re.DOTALL,
    )
    existing_m = existing_block_pat.search(content)
    if existing_m:
        try:
            existing_window = (int(existing_m.group(1)), int(existing_m.group(2)))
        except Exception:
            existing_window = None

    # Prefer source tape's hide-UI window when present.
    hide_window = _derive_intro_window_from_hide_ui(content)
    if hide_window is not None:
        clip_start_ms, clip_duration_ms = hide_window
        logger.debug(
            "Using HideUserInterfaceClip timing for intro on %s: start=%d duration=%d",
            codename,
            clip_start_ms,
            clip_duration_ms,
        )
    elif existing_window is not None:
        # Preserve source intro timing when no HideUI clip exists. JDNext source
        # tapes often carry tuned intro windows that should not be compressed to
        # a tiny fallback window.
        clip_start_ms, clip_duration_ms = existing_window
        if start_beat is not None and start_beat < 0 and clip_duration_ms > 0:
            existing_end = clip_start_ms + clip_duration_ms
            if existing_end < 0:
                clip_start_ms = -clip_duration_ms
                logger.debug(
                    "Normalized intro clip to end at t=0 for %s: start=%d duration=%d (was end=%d)",
                    codename,
                    clip_start_ms,
                    clip_duration_ms,
                    existing_end,
                )
        logger.debug(
            "Preserving existing intro clip timing for %s: start=%d duration=%d",
            codename,
            clip_start_ms,
            clip_duration_ms,
        )
    elif start_beat is not None and start_beat < 0:
        # Fallback intended to mimic hide-interface-like timing when that clip is
        # absent in source tapes. Many tapes appear to use startBeat*24 scaling.
        clip_start_ms = int(start_beat * 24)
        # Keep intro window strictly before gameplay start (t=0) so StartTime
        # adjustments are observable and the intro does not mask main audio onset.
        clip_duration_ms = max(96, abs(clip_start_ms) - 48)
        logger.debug(
            "Using hide-style fallback intro timing for %s: startBeat=%d -> start=%d duration=%d",
            codename,
            start_beat,
            clip_start_ms,
            clip_duration_ms,
        )

    # Keep intro clips gapless at gameplay boundary: if the selected intro
    # window ends before timeline 0, shift start so it ends exactly at 0 while
    # preserving duration and selected StartOffset segment.
    if start_beat is not None and start_beat < 0 and clip_duration_ms > 0:
        clip_end = clip_start_ms + clip_duration_ms
        if clip_end < 0:
            clip_start_ms = -clip_duration_ms
            logger.debug(
                "Normalized intro clip to end at t=0 for %s: start=%d duration=%d (was end=%d)",
                codename,
                clip_start_ms,
                clip_duration_ms,
                clip_end,
            )

    # If source intro WAV is much longer than the clip window (common on some
    # JDNext maps), play the tail of the intro so the clip is not all silence.
    # JDNext clip timing can be beat-scaled (24 units per beat) rather than ms,
    # so convert to seconds using marker-derived beat length when available.
    if intro_wav_duration_ms is not None and clip_duration_ms > 0:
        clip_window_ms_equivalent = float(clip_duration_ms)
        clip_start_ms_equivalent = float(clip_start_ms)
        if beat_ms is not None and beat_ms > 0:
            clip_window_ms_equivalent *= (float(beat_ms) / 24.0)
            clip_start_ms_equivalent *= (float(beat_ms) / 24.0)

        # Cap the window to the portion before t=0, as the intro WAV is built to end at t=0.
        # This prevents clips that span the whole song from neutralizing the tail offset.
        if clip_start_ms_equivalent < 0:
            clip_window_ms_equivalent = min(clip_window_ms_equivalent, abs(clip_start_ms_equivalent))

        if intro_wav_duration_ms > (clip_window_ms_equivalent + 500.0):
            clip_start_offset_s = max(0.0, (intro_wav_duration_ms - clip_window_ms_equivalent) / 1000.0)
            logger.debug(
                "Using tail StartOffset for intro on %s: wav=%dms clipWindow=%.1fms offset=%.6fs",
                codename,
                intro_wav_duration_ms,
                clip_window_ms_equivalent,
                clip_start_offset_s,
            )

    if soundset_path.lower() in content.lower():
        updated_existing = content
        block_pat = re.compile(
            r"(SoundSetClip\s*=\s*\{[^{}]*?SoundSetPath\s*=\s*\""
            + re.escape(soundset_path)
            + r"\"[^{}]*?\})",
            re.IGNORECASE | re.DOTALL,
        )
        match = block_pat.search(updated_existing)
        if match:
            block = match.group(1)
            block_new = re.sub(r"(StartTime\s*=\s*)[-+]?\d+", rf"\g<1>{clip_start_ms}", block, count=1)
            block_new = re.sub(r"(Duration\s*=\s*)[-+]?\d+", rf"\g<1>{clip_duration_ms}", block_new, count=1)
            if clip_start_offset_s is not None:
                if re.search(r"\bStartOffset\s*=\s*[-+]?\d*\.?\d+\s*,", block_new):
                    block_new = re.sub(
                        r"(StartOffset\s*=\s*)[-+]?\d*\.?\d+",
                        f"\\g<1>{clip_start_offset_s:.6f}",
                        block_new,
                        count=1,
                    )
                else:
                    block_new = re.sub(
                        r"(Duration\s*=\s*[-+]?\d+\s*,)",
                        rf"\g<1>\n                    StartOffset = {clip_start_offset_s:.6f},",
                        block_new,
                        count=1,
                    )
            else:
                block_new = re.sub(r"\n\s*StartOffset\s*=\s*[-+]?\d*\.?\d+\s*,", "", block_new, count=1)
            updated_existing = updated_existing[: match.start(1)] + block_new + updated_existing[match.end(1):]

        # Keep generated tapes aligned with known-working maps: intro SoundSetClip
        # does not need a dedicated Tracks entry.
        updated_existing = _remove_intro_track_entries(updated_existing, intro_tpl_name)
        updated_existing = _remove_empty_tracks_table(updated_existing)
        updated_existing = _normalize_clips_table_end(updated_existing)
        updated_existing = _normalize_tapeclock_zero(updated_existing)

        if updated_existing != content:
            tape_path.write_text(updated_existing, encoding="utf-8")
            logger.debug("Adjusted existing intro AMB clip timing in %s: start=%d duration=%d", tape_path.name, clip_start_ms, clip_duration_ms)
            return True
        return False

    if not allow_injection:
        logger.debug("Intro AMB SoundSetClip injection disabled for '%s' (no existing intro clip to normalize)", codename)
        return False

    track_id = zlib.crc32(intro_tpl_name.lower().encode("utf-8")) & 0xFFFFFFFF
    clip_id = zlib.crc32(f"{codename.lower()}:{intro_tpl_name.lower()}:intro".encode("utf-8")) & 0xFFFFFFFF
    start_offset_line = (
        f"                    StartOffset = {clip_start_offset_s:.6f},\n"
        if clip_start_offset_s is not None
        else ""
    )

    clip_block = (
        "\n            {\n"
        "                NAME = \"SoundSetClip\",\n"
        "                SoundSetClip = \n"
        "                {\n"
        f"                    Id = {clip_id},\n"
        f"                    TrackId = {track_id},\n"
        "                    IsActive = 1,\n"
        f"                    StartTime = {clip_start_ms},\n"
        f"                    Duration = {clip_duration_ms},\n"
        f"                    SoundSetPath = \"{soundset_path}\",\n"
        "                    SoundChannel = 0,\n"
        f"{start_offset_line}"
        "                    StopsOnEnd = 0,\n"
        "                    AccountedForDuration = 0,\n"
        "                },\n"
        "            },"
    )
    updated = _insert_lua_table_entry(content, "Clips", clip_block)
    if updated is None:
        logger.debug("Could not locate Clips table in %s for AMB intro injection", tape_path.name)
        return False

    updated = _remove_intro_track_entries(updated, intro_tpl_name)
    updated = _remove_empty_tracks_table(updated)
    updated = _normalize_clips_table_end(updated)
    updated = _normalize_tapeclock_zero(updated)

    tape_path.write_text(updated, encoding="utf-8")
    logger.debug("Injected intro AMB SoundSetClip into %s: %s", tape_path.name, intro_tpl_name)
    return True


def process_ambient_directory(
    source_dir: Path,
    target_dir: Path,
    codename: str,
    attempt_enabled: bool = True,
    normalize_intro_clip: bool = True,
) -> int:
    """Process all ambient assets (templates and loose CKDs) in a directory."""
    amb_out_dir = _resolve_amb_dir(target_dir)
    amb_out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    generated_intro_base = f"amb_{codename.lower()}_intro"

    # 1. Process existing .tpl.ckd templates (amb_* and set_amb_*)
    tpl_bases = set()
    tpl_ckds = set(source_dir.rglob("amb_*.tpl.ckd"))
    tpl_ckds.update(source_dir.rglob("set_amb_*.tpl.ckd"))
    for ckd in sorted(tpl_ckds, key=lambda p: p.as_posix().lower()):
        if not (_path_has_codename_component(ckd, codename) or _filename_matches_codename(ckd, codename)):
            continue
        try:
            data = _load_ckd_json(ckd)
            if not data.get("COMPONENTS"):
                try:
                    parsed = parse_binary_ckd(ckd.read_bytes(), ckd.name)
                    if isinstance(parsed, dict) and parsed.get("type") == "sound_component":
                        data = {"COMPONENTS": [{"soundList": parsed.get("sound_list", [])}]}
                except Exception as exc:
                    logger.debug("Binary AMB parse fallback failed for %s: %s", ckd.name, exc)
            ilu_c, tpl_c, audio_files = process_ambient_tpl(data, codename, ckd.name)

            if ilu_c and tpl_c:
                base_name = ckd.name.replace(".tpl.ckd", "")
                tpl_bases.add(base_name.lower())

                ilu_path = amb_out_dir / f"{base_name}.ilu"
                tpl_path = amb_out_dir / f"{base_name}.tpl"

                ilu_path.write_text(ilu_c, encoding="utf-8")
                tpl_path.write_text(tpl_c, encoding="utf-8")

                # V1 parity: resolve referenced AMB audio paths from soundList.
                from jd2021_installer.installers.media_processor import extract_ckd_audio_v1
                for ref in audio_files:
                    ref_name = Path(str(ref).replace("\\", "/")).name
                    if not ref_name.lower().endswith(".wav"):
                        continue
                    target_wav = amb_out_dir / ref_name
                    if target_wav.exists():
                        continue

                    ckd_candidate = ckd.parent / f"{ref_name}.ckd"
                    decoded = None
                    if ckd_candidate.exists():
                        decoded = extract_ckd_audio_v1(ckd_candidate, amb_out_dir)

                    if decoded and Path(decoded).exists():
                        decoded_path = Path(decoded)
                        if decoded_path != target_wav:
                            if target_wav.exists():
                                target_wav.unlink()
                            decoded_path.rename(target_wav)
                        logger.debug("Decoded AMB referenced audio: %s", target_wav.name)
                    else:
                        # Keep parity with V1: create a tiny silent fallback when referenced audio is missing.
                        with wave.open(str(target_wav), "w") as wf:
                            wf.setnchannels(1)
                            wf.setsampwidth(2)
                            wf.setframerate(48000)
                            wf.writeframes(b"\x00\x00" * 4800)
                        logger.debug("Created silent AMB placeholder: %s", target_wav.name)

                logger.debug("Processed AMB template: %s", ckd.name)
                count += 1
        except Exception as e:
            logger.error("Failed to process ambient file %s: %s", ckd.name, e)

    # 2. Process "orphan" .wav.ckd files (no corresponding .tpl.ckd)
    for wav_ckd in source_dir.rglob("*.wav.ckd"):
        if "/amb/" not in str(wav_ckd).lower().replace("\\", "/"):
            continue
        if not (_path_has_codename_component(wav_ckd, codename) or _filename_matches_codename(wav_ckd, codename)):
            continue
            
        base = wav_ckd.name.replace(".wav.ckd", "")
        if base.lower() == generated_intro_base:
            if not attempt_enabled:
                logger.debug("Intro AMB disabled: skipping source intro decode for %s", wav_ckd.name)
                continue

            # Preserve the generated intro if the media step already wrote one.
            # Replacing it here can reintroduce the source timing gap and make the
            # intro end early relative to the synced main audio.
            target_wav = amb_out_dir / f"{base}.wav"
            if target_wav.exists():
                logger.debug("Preserving generated intro AMB audio: %s", target_wav.name)
                continue

            # Otherwise, fall back to the source intro audio when no generated
            # intro is available yet.
            from jd2021_installer.installers.media_processor import extract_ckd_audio_v1

            decoded = extract_ckd_audio_v1(wav_ckd, amb_out_dir)
            if decoded and Path(decoded).exists():
                decoded_path = Path(decoded)
                if decoded_path != target_wav:
                    if target_wav.exists():
                        target_wav.unlink()
                    decoded_path.rename(target_wav)
                logger.debug("Decoded source intro AMB audio: %s", target_wav.name)
            else:
                logger.debug("Failed to decode source intro AMB audio from %s; keeping generated intro if available", wav_ckd.name)
            continue

        if base.lower() not in tpl_bases:
            if _generate_synthetic_amb(wav_ckd, amb_out_dir, codename):
                count += 1
                logger.debug("Generated synthetic AMB for orphan audio: %s", wav_ckd.name)

    if not attempt_enabled:
        removed_count = _remove_intro_amb_assets(amb_out_dir)
        removed_clip = _remove_intro_amb_soundset_clips(target_dir, codename)
        removed_actor = _remove_intro_amb_actor_from_isc(target_dir, codename)
        # Inject non-intro AMB actors only after intro assets are removed.
        inject_ambient_actors(target_dir, codename)
        logger.warning(
            "Intro AMB attempt disabled: removed %d intro AMB asset(s) for '%s' (clip removed=%s, actor removed=%s)",
            removed_count,
            codename,
            removed_clip,
            removed_actor,
        )
        return count

    # 3. Inject actors into audio.isc
    if normalize_intro_clip:
        _remove_intro_amb_actor_from_isc(target_dir, codename)
    inject_ambient_actors(target_dir, codename)

    # 4. Ensure intro AMB is actually triggered from MainSequence.
    if normalize_intro_clip:
        _inject_intro_amb_soundset_clip(target_dir, codename, attempt_enabled=attempt_enabled)

    return count
