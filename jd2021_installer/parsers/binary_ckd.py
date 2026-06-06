"""Stateless binary CKD parser for UbiArt engine cooked-data files.

Refactored from the original ``binary_ckd_parser.py``.  All parsing functions
are pure — they accept ``bytes`` and return typed dataclasses from
:mod:`jd2021_installer.core.models` rather than raw dicts.

Two public entry-points:

* ``parse_binary_ckd(data, filename)``  — dispatch based on header CRC
* Individual helpers: ``parse_musictrack``, ``parse_songdesc``,
  ``parse_dtape``, ``parse_ktape``, ``parse_cinematic_tape``

All functions operate on in-memory ``bytes``; no filesystem access.
"""

from __future__ import annotations

import logging
import struct
import zlib
from typing import List, Optional, Union

from jd2021_installer.core.exceptions import BinaryCKDParseError
from jd2021_installer.core.models import (
    AutodanceTemplate,
    BeatClip,
    BeatsTape,
    CinematicTape,
    DanceTape,
    DefaultColors,
    GoldEffectClip,
    KaraokeClip,
    KaraokeTape,
    MotionClip,
    MusicSection,
    MusicSignature,
    MusicTrackStructure,
    PictogramClip,
    SongDescription,
    SoundComponentTemplate,
    SoundSetClip,
    TapeReferenceClip,
)

logger = logging.getLogger("jd2021.parsers.binary_ckd")


# ---------------------------------------------------------------------------
# Known InternedString CRC32 values (UbiArt engine class identifiers)
# ---------------------------------------------------------------------------

_MUSICTRACK_TEMPLATE_CRC = 0x02883A7E
_SONGDESC_TEMPLATE_CRC = 0x8AC2B5C6
_ACTOR_TEMPLATE_CRC = 0x1B857BCE
_AUTODANCE_TEMPLATE_CRC = 0x51EA2CD0
_SOUND_COMPONENT_TEMPLATE_CRC = 0xD94D6C53
_TAPE_CRC = 0x2AFED161
_BEAT_CLIP_CRC = 0x364811D4


def _string_id(s: str) -> int:
    """CRC32 of the uppercase ASCII representation (UbiArt InternedString)."""
    return zlib.crc32(s.upper().encode("ascii")) & 0xFFFF_FFFF


_THEME_CRC = _string_id("theme")
_LYRICS_CRC = _string_id("lyrics")


# ---------------------------------------------------------------------------
# Low-level big-endian sequential reader
# ---------------------------------------------------------------------------

class BinaryReader:
    """Sequential big-endian reader over an in-memory bytes buffer."""

    __slots__ = ("data", "pos")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def u32(self) -> int:
        v = struct.unpack_from(">I", self.data, self.pos)[0]
        self.pos += 4
        return v

    def i32(self) -> int:
        v = struct.unpack_from(">i", self.data, self.pos)[0]
        self.pos += 4
        return v

    def f32(self) -> float:
        v = struct.unpack_from(">f", self.data, self.pos)[0]
        self.pos += 4
        return v

    def u16(self) -> int:
        v = struct.unpack_from(">H", self.data, self.pos)[0]
        self.pos += 2
        return v

    def skip(self, n: int) -> None:
        self.pos += n

    def len_string(self) -> str:
        n = self.u32()
        s = self.data[self.pos : self.pos + n].decode("utf-8", errors="replace")
        self.pos += n
        return s

    def interned_string(self) -> int:
        return self.u32()

    def split_path(self) -> str:
        """SplitPath: filename + path + path_id + padding."""
        filename = self.len_string()
        path = self.len_string()
        self.u32()  # path_id
        self.u32()  # padding
        return (path + filename).replace("\\", "/")

    @property
    def remaining(self) -> int:
        return len(self.data) - self.pos


# ---------------------------------------------------------------------------
# Actor header
# ---------------------------------------------------------------------------

def _read_actor_header(r: BinaryReader) -> int:
    """Consume the fixed 48-byte Actor header; return template class CRC."""
    unk1 = r.u32()
    if unk1 != 1:
        raise BinaryCKDParseError(f"Actor header unk1={unk1} (expected 1)")
    r.u32()  # unk2
    actor_crc = r.interned_string()
    if actor_crc != _ACTOR_TEMPLATE_CRC:
        raise BinaryCKDParseError(
            f"Actor class 0x{actor_crc:08X} (expected 0x{_ACTOR_TEMPLATE_CRC:08X})"
        )
    r.u32()  # unk3
    for _ in range(7):
        r.u32()  # zeros
    r.u32()  # comp_count
    return r.interned_string()


# ---------------------------------------------------------------------------
# MusicTrackComponent / MusicTrackStructure
# ---------------------------------------------------------------------------

def _parse_musictrack_from_reader(r: BinaryReader) -> MusicTrackStructure:
    """Parse MusicTrackComponent_Template after the Actor header."""
    r.u32()  # mc_unk1
    r.u32()  # md_unk1

    ms_unk1 = r.u32()
    is_older = ms_unk1 == 0x6C

    n_markers = r.u32()
    markers = [r.u32() for _ in range(n_markers)]

    n_sigs = r.u32()
    signatures: List[MusicSignature] = []
    for _ in range(n_sigs):
        r.u32()  # sig_unk1
        marker = r.i32()
        beats = r.u32()
        signatures.append(MusicSignature(beats=beats, marker=marker))

    n_sections = r.u32()
    sections: List[MusicSection] = []
    for _ in range(n_sections):
        r.u32()  # sect_unk1
        marker = r.i32()
        sect_type = r.u32()
        comment_len = r.u32()
        if comment_len > 0:
            r.skip(comment_len)
        sections.append(MusicSection(section_type=sect_type, marker=marker))

    start_beat = r.i32()
    end_beat = r.u32()
    video_start_time = r.f32()
    volume = r.f32()

    preview_entry = 0.0
    preview_loop_start = 0.0
    preview_loop_end = 0.0
    fade_in_duration = 0.0
    fade_in_type = 0
    fade_out_duration = 0.0
    fade_out_type = 0

    # Some console binaries carry preview/fade values even when ms_unk1 is not 0x6C.
    # Parse the optional trailing block whenever enough bytes remain.
    if r.remaining >= 32:
        cand_preview_entry = r.f32()
        cand_preview_loop_start = r.f32()
        cand_preview_loop_end = r.f32()
        cand_fade_in_duration = r.f32()
        cand_fade_in_type = r.u32()
        cand_fade_out_duration = r.f32()
        cand_fade_out_type = r.u32()
        r.u32()  # unknown/discarded

        # Sanity-check preview fields; reject garbage floats from unsupported layouts.
        valid_preview = all(0.0 <= pval <= 10000.0 for pval in (
            cand_preview_entry,
            cand_preview_loop_start,
            cand_preview_loop_end,
        ))
        if valid_preview:
            preview_entry = cand_preview_entry
            preview_loop_start = cand_preview_loop_start
            preview_loop_end = cand_preview_loop_end
            fade_in_duration = cand_fade_in_duration
            fade_in_type = cand_fade_in_type
            fade_out_duration = cand_fade_out_duration
            fade_out_type = cand_fade_out_type
        elif is_older:
            logger.debug(
                "Discarded invalid preview fields from musictrack block: %.3f %.3f %.3f",
                cand_preview_entry,
                cand_preview_loop_start,
                cand_preview_loop_end,
            )

    return MusicTrackStructure(
        markers=markers,
        signatures=signatures,
        sections=sections,
        start_beat=start_beat,
        end_beat=end_beat,
        video_start_time=video_start_time,
        preview_entry=preview_entry,
        preview_loop_start=preview_loop_start,
        preview_loop_end=preview_loop_end,
        volume=volume,
        fade_in_duration=fade_in_duration,
        fade_in_type=fade_in_type,
        fade_out_duration=fade_out_duration,
        fade_out_type=fade_out_type,
    )


# ---------------------------------------------------------------------------
# JD_SongDescTemplate
# ---------------------------------------------------------------------------

def _parse_songdesc_from_reader(r: BinaryReader) -> SongDescription:
    """Parse JD_SongDescTemplate after the Actor header."""
    map_name = ""
    jd_version = 0
    original_jd_version = 0
    artist = ""
    dancer_name = ""
    title = ""
    num_coach = 1
    main_coach = -1
    difficulty = 1
    background_type = 0
    lyrics_type = 0
    energy = 1
    tags = ["Main"]
    default_colors = DefaultColors()

    try:
        r.u32()  # unk1

        map_name = r.len_string()
        jd_version = r.u32()
        original_jd_version = r.u32()

        # Related albums (skip)
        for _ in range(r.u32()):
            r.len_string()

        # Unknown58 array – 13 × u32 per entry (skip)
        for _ in range(r.u32()):
            for _ in range(13):
                r.u32()

        artist = r.len_string()
        dancer_name = r.len_string()
        title = r.len_string()

        num_coach = r.u32()
        main_coach = r.i32()
        difficulty = r.u32()
        background_type = r.u32()
        lyrics_type = r.i32()
        energy = r.u32()
        r.f32()  # unk17

        # Tags (skip processing but count entries)
        n_tags = r.u32()
        for _ in range(n_tags):
            r.u32()  # tag_unk1
            r.u32()  # tag CRC
            r.u32()  # tag_unk21
            r.u32()  # tag_unk22

        # DefaultColors
        for _ in range(r.u32()):
            name_crc = r.u32()
            rgba = [r.f32() for _ in range(4)]
            if name_crc == _THEME_CRC:
                default_colors.theme = rgba
            elif name_crc == _LYRICS_CRC:
                default_colors.lyrics = rgba
            else:
                default_colors.extra[f"0x{name_crc:08X}"] = rgba

        # Paths (consume but discard)
        for _ in range(r.u32()):
            r.split_path()
        for _ in range(r.u32()):
            r.split_path()
    except struct.error:
        pass

    return SongDescription(
        map_name=map_name,
        title=title,
        artist=artist,
        dancer_name=dancer_name,
        num_coach=num_coach,
        main_coach=main_coach,
        difficulty=difficulty,
        background_type=background_type,
        lyrics_type=lyrics_type,
        energy=energy,
        tags=tags,
        jd_version=jd_version,
        original_jd_version=original_jd_version,
        default_colors=default_colors,
    )


# ---------------------------------------------------------------------------
# Timeline parsers (dtape, ktape, cinematic tape)
# ---------------------------------------------------------------------------

def parse_dtape(data: bytes, map_name: str) -> DanceTape:
    """Parse a binary dance timeline tape (dtape)."""
    r = BinaryReader(data)
    r.skip(12)
    r.u32()  # timeline_ver
    entries = r.u32()

    clips: List[MotionClip | PictogramClip | GoldEffectClip] = []
    for _ in range(entries):
        r.u32()  # unknown
        entry_class = r.u32()
        entry_id = r.u32()
        entry_trackid = r.u32()
        entry_isactive = r.u32()
        entry_starttime = r.u32()
        entry_duration = r.u32()

        if entry_class in (108, 112, 56):
            namelen = r.u32()
            name = r.data[r.pos : r.pos + namelen].decode("utf-8", errors="ignore")
            r.pos += namelen
            pathlen = r.u32()
            path = r.data[r.pos : r.pos + pathlen].decode("utf-8", errors="ignore")
            r.pos += pathlen
            r.u32()  # atlindex
            r.u32()  # unknown2

            if entry_class in (108, 112):  # MotionClip
                goldmove = r.u32()
                coachid = r.u32()
                movetype = r.u32()
                r.skip(16)  # colors
                r.skip(64)  # pointing/useless

                clips.append(MotionClip(
                    id=entry_id,
                    track_id=entry_trackid,
                    is_active=entry_isactive,
                    start_time=entry_starttime,
                    duration=entry_duration,
                    classifier_path=path.replace("jd2015", "maps") + name,
                    gold_move=goldmove,
                    coach_id=coachid,
                    move_type=movetype,
                ))
            elif entry_class == 56:  # PictogramClip
                coachcount = r.u32()
                clips.append(PictogramClip(
                    id=entry_id,
                    track_id=entry_trackid,
                    is_active=entry_isactive,
                    start_time=entry_starttime,
                    duration=entry_duration,
                    picto_path=path.replace("jd2015", "maps") + name,
                    coach_count=coachcount,
                ))
        elif entry_class == 28:  # GoldEffectClip
            effecttype = r.u32()
            clips.append(GoldEffectClip(
                id=entry_id,
                track_id=entry_trackid,
                is_active=entry_isactive,
                start_time=entry_starttime,
                duration=entry_duration,
                effect_type=effecttype,
            ))
        else:
            logger.debug(f"parse_dtape: unknown clip class {entry_class}, breaking to avoid desync")
            break

    return DanceTape(clips=clips, map_name=map_name)


def parse_ktape(data: bytes, map_name: str) -> KaraokeTape:
    """Parse a binary karaoke timeline tape (ktape)."""
    r = BinaryReader(data)
    r.skip(12)
    r.u32()  # timeline_ver
    entries = r.u32()

    clips: List[KaraokeClip] = []
    for _ in range(entries):
        r.u32()  # unknown
        entry_class = r.u32()
        entry_id = r.u32()
        entry_trackid = r.u32()
        entry_isactive = r.u32()
        entry_starttime = r.u32()
        entry_duration = r.u32()

        if entry_class in (32, 80):
            pitch = struct.unpack_from(">f", r.data, r.pos)[0]
            r.pos += 4
            lyriclen = r.u32()
            lyric = r.data[r.pos : r.pos + lyriclen].decode("utf-8", errors="ignore")
            r.pos += lyriclen

            if entry_class == 80:
                isendofline = r.u32()
                content_type = r.u32()
                start_time_tol = r.u32()
                end_time_tol = r.u32()
                semitone_tol = r.f32()
            else:
                r.u32()  # unknown
                isendofline = r.u32()
                content_type = 0
                start_time_tol = 4
                end_time_tol = 4
                semitone_tol = 5.0

            clips.append(KaraokeClip(
                id=entry_id,
                track_id=entry_trackid,
                is_active=entry_isactive,
                start_time=entry_starttime,
                duration=entry_duration,
                pitch=pitch,
                lyrics=lyric,
                is_end_of_line=isendofline,
                content_type=content_type,
                start_time_tolerance=start_time_tol,
                end_time_tolerance=end_time_tol,
                semitone_tolerance=semitone_tol,
            ))
        else:
            logger.debug(f"parse_ktape: unknown clip class {entry_class}, breaking to avoid desync")
            break

    return KaraokeTape(clips=clips, map_name=map_name)


def parse_cinematic_tape(data: bytes, map_name: str) -> CinematicTape:
    """Parse a binary cinematic tape (stape / MainSequence)."""
    clips: List[SoundSetClip | TapeReferenceClip] = []
    try:
        r = BinaryReader(data)
        r.skip(12)
        r.u32()  # timeline_ver
        entries = r.u32()

        for _ in range(entries):
            r.u32()  # unknown
            entry_class = r.u32()

            if entry_class == 136:  # SoundSetClip
                entry_id = r.u32()
                entry_trackid = r.u32()
                entry_isactive = r.u32()
                entry_starttime = r.u32()
                entry_duration = r.u32()
                r.skip(4)  # empty
                pathlen = r.u32()
                path = r.data[r.pos : r.pos + pathlen].decode("utf-8", errors="ignore")
                r.pos += pathlen
                filelen = r.u32()
                filename = r.data[r.pos : r.pos + filelen].decode("utf-8", errors="ignore")
                r.pos += filelen
                r.skip(4)
                if entries > 1:
                    r.skip(12)

                clips.append(SoundSetClip(
                    id=entry_id,
                    track_id=entry_trackid,
                    is_active=entry_isactive,
                    start_time=entry_starttime,
                    duration=entry_duration,
                    sound_set_path=path.replace("jd2015", "maps") + filename,
                ))
            elif entry_class == 160:  # TapeReferenceClip
                entry_id = r.u32()
                entry_trackid = r.u32()
                entry_isactive = r.u32()
                entry_starttime = r.u32()
                entry_duration = r.u32()
                pathlen = r.u32()
                path = r.data[r.pos : r.pos + pathlen].decode("utf-8", errors="ignore")
                r.pos += pathlen
                filelen = r.u32()
                filename = r.data[r.pos : r.pos + filelen].decode("utf-8", errors="ignore")
                r.pos += filelen
                r.skip(4)
                loop = r.u32()
                if entries > 1:
                    r.skip(12)

                clips.append(TapeReferenceClip(
                    id=entry_id,
                    track_id=entry_trackid,
                    is_active=entry_isactive,
                    start_time=entry_starttime,
                    duration=entry_duration,
                    path=path.replace("jd2015", "maps") + filename,
                    loop=loop,
                ))
            else:
                break  # Unknown class, break to avoid desync

    except Exception as e:
        logger.debug("binary_ckd: partial/failed parse on cinematic tape: %s", e)

    return CinematicTape(clips=clips, map_name=map_name)


def parse_btape(data: bytes, map_name: str) -> BeatsTape:
    """Parse a binary beats timeline tape (btape)."""
    r = BinaryReader(data)

    # Actor-wrapped tape (template-based CKD) or raw tape payload.
    if data.startswith(b"\x00\x00\x00\x01"):
        template_crc = _read_actor_header(r)
        if template_crc != _TAPE_CRC:
            raise BinaryCKDParseError(
                f"btape actor wrapper has template 0x{template_crc:08X} (expected 0x{_TAPE_CRC:08X})"
            )
        r.u32()  # comp_unk1
        r.u32()  # unk2
        r.u32()  # unk3
    else:
        r.skip(12)

    r.u32()  # timeline_ver
    entries = r.u32()

    clips: List[BeatClip] = []
    for _ in range(entries):
        r.u32()  # unknown
        entry_class = r.u32()
        entry_id = r.u32()
        entry_trackid = r.u32()
        entry_isactive = r.u32()
        entry_starttime = r.u32()
        entry_duration = r.u32()
        clip_type = r.u32()

        if entry_class == _BEAT_CLIP_CRC:
            clips.append(BeatClip(
                id=entry_id,
                track_id=entry_trackid,
                is_active=entry_isactive,
                start_time=entry_starttime,
                duration=entry_duration,
                beat_type=clip_type,
            ))
        else:
            logger.debug(f"parse_btape: unknown clip class {entry_class}, breaking to avoid desync")
            break

    return BeatsTape(clips=clips, map_name=map_name)


# ---------------------------------------------------------------------------
# TPL dispatch (musictrack / songdesc) from Actor header
# ---------------------------------------------------------------------------

def parse_musictrack(data: bytes) -> MusicTrackStructure:
    """Parse a musictrack CKD from raw bytes."""
    r = BinaryReader(data)
    _read_actor_header(r)
    return _parse_musictrack_from_reader(r)


def parse_songdesc(data: bytes) -> SongDescription:
    """Parse a songdesc CKD from raw bytes."""
    r = BinaryReader(data)
    _read_actor_header(r)
    return _parse_songdesc_from_reader(r)


# ---------------------------------------------------------------------------
# Main dispatch entry point
# ---------------------------------------------------------------------------

ParseResult = Union[
    MusicTrackStructure,
    SongDescription,
    DanceTape,
    KaraokeTape,
    CinematicTape,
    BeatsTape,
    AutodanceTemplate,
    SoundComponentTemplate,
]


def _infer_map_name(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0]
    return stem.split("_")[0].split(".")[0]


def parse_binary_ckd(data: bytes, filename: str) -> ParseResult:
    """Dispatch binary CKD parse based on the Actor header template CRC.

    Args:
        data:     Raw file bytes (read entirely into memory).
        filename: Original filename (used to identify tape type).

    Returns:
        A typed dataclass matching the CKD content.

    Raises:
        BinaryCKDParseError: If the file cannot be parsed.
    """
    name_lower = filename.lower()

    # Timeline/Tape containers
    map_name = _infer_map_name(filename)

    if "dtape" in name_lower:
        return parse_dtape(data, map_name)
    if "ktape" in name_lower:
        return parse_ktape(data, map_name)
    if "btape" in name_lower:
        return parse_btape(data, map_name)
    if any(ext in name_lower for ext in (
        ".tape.ckd", ".stape.ckd", ".adtape.ckd",
        ".adrecording.ckd", ".advideo.ckd"
    )):
        return parse_cinematic_tape(data, map_name)

    # TPL files — read Actor header and dispatch on template class CRC
    r = BinaryReader(data)
    try:
        template_crc = _read_actor_header(r)
    except (struct.error, BinaryCKDParseError) as exc:
        raise BinaryCKDParseError(
            f"Failed to read Actor header in '{filename}': {exc}"
        ) from exc

    try:
        if template_crc == _MUSICTRACK_TEMPLATE_CRC:
            result = _parse_musictrack_from_reader(r)
            logger.debug(
                "binary_ckd: parsed musictrack '%s' (%d markers)",
                filename, len(result.markers),
            )
            return result

        if template_crc == _SONGDESC_TEMPLATE_CRC:
            result = _parse_songdesc_from_reader(r)
            logger.debug(
                "binary_ckd: parsed songdesc '%s' title='%s'",
                filename, result.title,
            )
            return result

        if template_crc == _AUTODANCE_TEMPLATE_CRC:
            r.u32()  # unk1
            ad_map_name = r.len_string()
            return AutodanceTemplate(map_name=ad_map_name)

        if template_crc == _SOUND_COMPONENT_TEMPLATE_CRC:
            r.u32()  # unk1
            entry_count = r.u32()
            sounds = []
            for _ in range(entry_count):
                r.skip(36)
                file_count = r.u32()
                files = [r.split_path() for _ in range(file_count)]
                sounds.append({"files": files})
            return SoundComponentTemplate(sound_list=sounds)

        if template_crc == _TAPE_CRC and "btape" in name_lower:
            return parse_btape(data, map_name)

    except (struct.error, IndexError, UnicodeDecodeError) as exc:
        raise BinaryCKDParseError(
            f"Parse error in '{filename}': {exc}"
        ) from exc

    raise BinaryCKDParseError(
        f"Unsupported template class 0x{template_crc:08X} in '{filename}'"
    )


def calculate_marker_preroll(markers: List[int], start_beat: int, include_calibration: bool = True) -> Optional[float]:
    """Calculate pre-roll duration in ms from beat markers and start_beat.
    
    start_beat (negative) indicates how many beats before beat-0 the audio begins.
    Returns duration in milliseconds.
    
    Note: Adds 85ms calibration only for OGG/Fetch maps (include_calibration=True).
    """
    idx = abs(start_beat)
    if not markers or idx >= len(markers) or idx == 0:
        return None
    # 48 ticks per ms
    preroll_ms = (markers[idx] / 48.0)
    if include_calibration:
        preroll_ms += 85.0
    return preroll_ms
