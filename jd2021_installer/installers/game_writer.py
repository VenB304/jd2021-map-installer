"""Game file writer — generates UbiArt engine config files from NormalizedMapData.

Takes a ``NormalizedMapData`` dataclass and writes all the .trk, .tpl,
.act, .isc, .mpd, .stape, .sfi files that JD2021 expects inside its
``World/MAPS/<codename>/`` directory structure.

Refactored from the original monolithic ``map_builder.py``.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from jd2021_installer.core.config import AppConfig
from jd2021_installer.core.exceptions import GameWriterError
from jd2021_installer.core.models import (
    MusicTrackStructure,
    NormalizedMapData,
    SongDescription,
)

logger = logging.getLogger("jd2021.installers.game_writer")


# ---------------------------------------------------------------------------
# Lua helpers
# ---------------------------------------------------------------------------

def lua_long_string(text: Optional[str]) -> str:
    """Wrap text in a Lua long string literal, handling nested brackets."""
    if text is None:
        text = ""
    text = str(text)
    level = 0
    while True:
        close = "]" + ("=" * level) + "]"
        if close not in text:
            break
        level += 1
    return f"[{'=' * level}[{text}]{'=' * level}]"


def color_array_to_hex(val: Any, default: str = "0xFFFFFFFF") -> str:
    """Convert a [R,G,B,A] float array to a 0xRRGGBBAA hex string."""
    if isinstance(val, str) and val.startswith("0x"):
        return val
    if isinstance(val, (list, tuple)) and val:
        comps = [int(round(max(0, min(1, c)) * 255)) for c in val]
        # Pad with 0xFF (255) if alpha is missing or length < 4
        comps += [255] * (4 - len(comps))
        return "0x" + "".join(f"{c:02X}" for c in comps[:4])
    return default


def _coerce_numeric_version(value: Any, fallback: int) -> int:
    """Return ``value`` as an int, or ``fallback`` when not purely numeric."""
    # ``bool`` is a subclass of ``int`` but not a meaningful version value.
    if isinstance(value, bool):
        return fallback
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return fallback


def _select_playable_jd_version(jd_version: int, original_jd_version: int) -> int:
    """Map source versions to a stable engine-compatible JDVersion.

    JD2021 accepts many numeric values in SongDesc, but practical compatibility is
    best when ``JDVersion`` is pinned to one of two known-stable engine branches:
    2016 (legacy maps) or 2021 (modern maps).
    """
    if jd_version in (2016, 2021):
        return jd_version
    return 2021 if original_jd_version >= 2016 else 2016


# ---------------------------------------------------------------------------
# Directory setup
# ---------------------------------------------------------------------------

def setup_dirs(target_dir: str | Path) -> None:
    """Create the standard UbiArt map directory structure."""
    target = Path(target_dir)
    # V1 parity: PascalCase subsystems, mixed case sub-subs
    for subdir in (
        "Audio", "Timeline", "Timeline/pictos", "Timeline/Moves",
        "Cinematics", "VideosCoach", "MenuArt/Actors",
        "MenuArt/textures", "Autodance",
    ):
        (target / subdir).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Individual file writers
# ---------------------------------------------------------------------------

def _write_musictrack_trk(target: Path, name: str, mt: MusicTrackStructure, vst: float) -> None:
    """Write the .trk music track structure file."""
    markers = ", ".join(f"{{ VAL = {m} }}" for m in mt.markers)
    sigs = ", ".join(
        f"{{ MusicSignature = {{ beats = {s.beats}, marker = {s.marker} }} }}"
        for s in mt.signatures
    )
    sects = ", ".join(
        f"{{ MusicSection = {{ sectionType = {s.section_type}, marker = {s.marker} }} }}"
        for s in mt.sections
    )

    # Preview values in legacy console conversions may be absent (all zero).
    # Match legacy tooling behavior by deriving a sane loop from endBeat.
    pe = float(mt.preview_entry) if mt.preview_entry >= 0 else 0.0
    pls = float(mt.preview_loop_start) if mt.preview_loop_start >= 0 else 0.0
    ple = float(mt.preview_loop_end) if mt.preview_loop_end >= 0 else 0.0

    if pe == 0.0 and pls == 0.0 and ple == 0.0 and mt.end_beat > 0:
        midpoint = float(mt.end_beat // 2)
        pe = midpoint
        pls = midpoint
        ple = float(mt.end_beat)

    # Runtime safety: enforce monotonic preview ordering without hard clamping.
    # Some JDNext maps ship with previewLoopEnd=0 while previewEntry is valid.
    # That can trigger JD UI conductor assertions ("adding a brick in the past").
    original = (pe, pls, ple)
    if pls < pe:
        pls = pe

    if ple <= pls:
        # Prefer the authored song end when available.
        if mt.end_beat > pls:
            ple = float(mt.end_beat)
        else:
            # Last-resort minimal forward loop to keep conductor progression valid.
            ple = pls + 1.0

    if (pe, pls, ple) != original:
        logger.warning(
            "Adjusted preview loop for '%s' to monotonic range: entry=%.1f start=%.1f end=%.1f",
            name,
            pe,
            pls,
            ple,
        )

    content = (
        f"structure = {{ MusicTrackStructure = {{ markers = {{ {markers} }}, "
        f"signatures = {{ {sigs} }}, sections = {{ {sects} }}, "
        f"startBeat = {mt.start_beat}, endBeat = {mt.end_beat}, "
        f"fadeStartBeat = 0, useFadeStartBeat = 0, "
        f"fadeEndBeat = 0, useFadeEndBeat = 0, "
        f"videoStartTime = {vst:.6f}, "
        f"previewEntry = {pe:.1f}, "
        f"previewLoopStart = {pls:.1f}, "
        f"previewLoopEnd = {ple:.1f}, "
        f"volume = {mt.volume:.6f}, "
        f"fadeInDuration = {mt.fade_in_duration}, fadeInType = {mt.fade_in_type}, "
        f"fadeOutDuration = {mt.fade_out_duration}, fadeOutType = {mt.fade_out_type}, "
        f"entryPoints = {{ }} }} }}"
    )
    (target / f"Audio/{name}.trk").write_text(content, encoding="utf-8")


def _write_songdesc(target: Path, name: str, sd: SongDescription,
                    num_coach: int, vst: float, config: AppConfig,
                    has_autodance: bool = True) -> None:
    """Write SongDesc.tpl and SongDesc.act."""
    name_lower = name.lower()

    # Keep origin year truthful (numeric only), but map runtime JDVersion to a
    # stable engine branch to avoid crashes on unsupported game config lookups.
    raw_jd_ver = _coerce_numeric_version(sd.jd_version, 2021)
    orig_ver = _coerce_numeric_version(sd.original_jd_version, raw_jd_ver)
    jd_ver = _select_playable_jd_version(raw_jd_ver, orig_ver)

    # Tags
    tags_lua = ""
    for t in (sd.tags or ["Main"]):
        tags_lua += f'\n\t\t\t\t\t\t{{\n\t\t\t\t\t\t\tVAL = "{t}"\n\t\t\t\t\t\t}},'
    tags_lua = tags_lua.rstrip(",")

    # PhoneImages
    # V1 Parity: Always use lowercase for the maps/menuart path
    map_lower = name.lower()
    if sd.phone_images:
        phone_str = ""
        for k, v in sd.phone_images.items():
            phone_str += f'\n\t\t\t\t\t\t{{\n\t\t\t\t\t\t\tKEY = "{k}",\n\t\t\t\t\t\t\tVAL = "{v}"\n\t\t\t\t\t\t}},'
        phone_str = phone_str.rstrip(",")
    else:
        phone_str = f'\n\t\t\t\t\t\t{{\n\t\t\t\t\t\t\tKEY = "cover",\n\t\t\t\t\t\t\tVAL = "World/MAPS/{map_lower}/menuart/textures/{map_lower}_cover_phone.jpg"\n\t\t\t\t\t\t}}'
        for i in range(1, num_coach + 1):
            phone_str += f',\n\t\t\t\t\t\t{{\n\t\t\t\t\t\t\tKEY = "coach{i}",\n\t\t\t\t\t\t\tVAL = "World/MAPS/{map_lower}/menuart/textures/{map_lower}_coach_{i}_phone.png"\n\t\t\t\t\t\t}}'

    # DefaultColors
    dc = sd.default_colors
    color_fallbacks = {
        "lyrics": dc.lyrics,
        "theme": dc.theme,
        "songcolor_1a": dc.song_color_1a,
        "songcolor_1b": dc.song_color_1b,
        "songcolor_2a": dc.song_color_2a,
        "songcolor_2b": dc.song_color_2b,
    }
    # Merge to ensure uniqueness and prevent Engine crash (zserializerobjectcontainers.h)
    # Using case-insensitive merge to avoid duplicate keys like 'lyrics' vs 'Lyrics'
    merged_colors = color_fallbacks.copy()
    existing_keys_lower = {k.lower(): k for k in merged_colors.keys()}
    for key, val in dc.extra.items():
        k_lower = key.lower()
        if k_lower == "defaultcolors":
            continue # CRITICAL: Prevent duplicate DefaultColors block inside the map
        if k_lower in existing_keys_lower:
            # Overwrite existing key with the case-preserved original key name
            merged_colors[existing_keys_lower[k_lower]] = val
        else:
            merged_colors[key] = val

    colors_lua = ""
    for key, val in merged_colors.items():
        hex_val = color_array_to_hex(val)
        colors_lua += f'\n\t\t\t\t\t\t{{\n\t\t\t\t\t\t\tKEY = "{key}",\n\t\t\t\t\t\t\tVAL = "{hex_val}"\n\t\t\t\t\t\t}},'

    dancer_name = str(sd.dancer_name).replace('"', '\\"').replace('\n', ' ')
    audio_fade = config.audio_preview_fade_s if jd_ver >= 2016 else 0.0
    status = 3 if sd.status == 12 else sd.status
    version_loc_line = (
        f"\n\t\t\t\t\tVersionLocId = {sd.version_loc_id},"
        if sd.version_loc_id is not None
        else ""
    )

    tpl = f'''includeReference("EngineData/Helpers/SongDatabase.ilu")
params =
{{
	NAME = "Actor_Template",
	Actor_Template =
	{{
		TAGS =
		{{
			{{
				VAL = "songdescmain"
			}}
		}},
		WIP = 0,
		LOWUPDATE = 0,
		UPDATE_LAYER = 0,
		PROCEDURAL = 0,
		STARTPAUSED = 0,
		FORCEISENVIRONMENT = 0,
		COMPONENTS =
		{{
			{{
				NAME = "JD_SongDescTemplate",
				JD_SongDescTemplate =
				{{
					MapName = "{name}",
					JDVersion = {jd_ver},
					OriginalJDVersion = {orig_ver},
					Artist = {lua_long_string(sd.artist)},
					DancerName = "{dancer_name}",
					Title = {lua_long_string(sd.title)},
					Credits = {lua_long_string(sd.credits or "All rights of the producer and other rightholders to the recorded work reserved. Unless otherwise authorized, the duplication, rental, loan, exchange or use of this video game for public performance, broadcasting and online distribution to the public are prohibited.")},
					NumCoach = {num_coach},
					NumOfAutodance = {1 if has_autodance else 0},
					MainCoach = {sd.main_coach},
					Difficulty = {sd.difficulty},
					SweatDifficulty = {sd.sweat_difficulty},
					backgroundType = {sd.background_type},
					LyricsType = {sd.lyrics_type},
					Energy = {sd.energy},
					Tags =
					{{{tags_lua}
					}},
					Status = {status},
					LocaleID = {sd.locale_id},{version_loc_line}
					CountInProgression = 0,
					PhoneImages =
					{{{phone_str}
					}},
                    DefaultColors =
                    {{{colors_lua}
					}},
					VideoPreviewPath = "",
					Mode = 0,
					AudioPreviewFadeTime = {audio_fade:.6f}
				}}
			}}
		}}
	}}
}}'''
    (target / "SongDesc.tpl").write_text(tpl, encoding="utf-8")

    act = f'''params =
{{
    NAME = "Actor",
    Actor =
    {{
        LUA = "World/MAPS/{name}/songdesc.tpl",
        COMPONENTS =
        {{
            {{
                NAME = "JD_SongDescComponent",
                JD_SongDescComponent =
                {{
                }}
            }}
        }}
    }}
}}'''
    (target / "SongDesc.act").write_text(act, encoding="utf-8")


def _write_audio_isc(target: Path, name: str) -> None:
    """Write musictrack.tpl, sequence.tpl, .stape, and audio.isc."""
    # musictrack.tpl
    (target / f"Audio/{name}_musictrack.tpl").write_text(
        f'''includeReference("World/MAPS/{name}/audio/{name}.trk")
params =
{{
\tNAME = "Actor_Template",
\tActor_Template =
\t{{
\t\tCOMPONENTS =
\t\t{{
\t\t\t{{
\t\t\t\tNAME = "MusicTrackComponent_Template",
\t\t\t\tMusicTrackComponent_Template =
\t\t\t\t{{
\t\t\t\t\ttrackData =
\t\t\t\t\t{{
\t\t\t\t\t\tMusicTrackData =
\t\t\t\t\t\t{{
\t\t\t\t\t\t\tpath = "World/MAPS/{name}/audio/{name}.wav",
\t\t\t\t\t\t\turl = "jmcs://jd-contents/{name}/{name}.ogg",
\t\t\t\t\t\t\tstructure = structure
\t\t\t\t\t\t}}
\t\t\t\t\t}}
\t\t\t\t}}
\t\t\t}}
\t\t}}
\t}}
}}''')

    # sequence.tpl
    (target / f"Audio/{name}_sequence.tpl").write_text(
        f'''params =
{{
\tNAME = "Actor_Template",
\tActor_Template =
\t{{
\t\tCOMPONENTS =
\t\t{{
\t\t\t{{
\t\t\t\tNAME = "TapeCase_Template",
                TapeCase_Template =
                {{
                    TapesRack =
                    {{
                        {{
                            TapeGroup =
                            {{
                                Entries =
                                {{
                                    {{
                                        TapeEntry =
                                        {{
                                            Label = "TML_Sequence",
                                            Path = "World/MAPS/{name}/audio/{name}.stape"
                                        }}
                                    }}
                                }}
                            }}
                        }}
                     }}
                }}
\t\t\t}}
\t\t}}
\t}}
}}''', encoding="utf-8")

    # .stape
    (target / f"Audio/{name}.stape").write_text(
        f'''params =
{{
    NAME="Tape",
    Tape =
    {{
\t\tTapeClock = 0,
        MapName = "{name}"
    }}
}}''')

    # audio.isc
    (target / f"Audio/{name}_audio.isc").write_text(
        f'''<?xml version="1.0" encoding="ISO-8859-1"?>
<root>
\t<Scene ENGINE_VERSION="55299" GRIDUNIT="0.500000" DEPTH_SEPARATOR="0" NEAR_SEPARATOR="1.000000 0.000000 0.000000 0.000000, 0.000000 1.000000 0.000000 0.000000, 0.000000 0.000000 1.000000 0.000000, 0.000000 0.000000 0.000000 1.000000" FAR_SEPARATOR="1.000000 0.000000 0.000000 0.000000, 0.000000 1.000000 0.000000 0.000000, 0.000000 0.000000 1.000000 0.000000, 0.000000 0.000000 0.000000 1.000000">
\t\t<ACTORS NAME="Actor">
\t\t\t<Actor RELATIVEZ="0.000000" SCALE="1.000000 1.000000" xFLIPPED="0" USERFRIENDLY="MusicTrack" POS2D="0.000000 0.000000" ANGLE="0.000000" INSTANCEDATAFILE="" LUA="World/MAPS/{name}/audio/{name}_musictrack.tpl">
\t\t\t\t<COMPONENTS NAME="MusicTrackComponent">
\t\t\t\t\t<MusicTrackComponent />
\t\t\t\t</COMPONENTS>
\t\t\t</Actor>
\t\t</ACTORS>
\t\t<ACTORS NAME="Actor">
\t\t\t<Actor RELATIVEZ="0.000001" SCALE="1.000000 1.000000" xFLIPPED="0" USERFRIENDLY="{name}_sequence" POS2D="0.000000 0.000000" ANGLE="0.000000" INSTANCEDATAFILE="" LUA="World/MAPS/{name}/audio/{name}_sequence.tpl">
\t\t\t\t<COMPONENTS NAME="TapeCase_Component">
\t\t\t\t\t<TapeCase_Component />
\t\t\t\t</COMPONENTS>
\t\t\t</Actor>
\t\t</ACTORS>
\t\t<sceneConfigs>
\t\t\t<SceneConfigs activeSceneConfig="0" />
\t\t</sceneConfigs>
\t</Scene>
</root>''')

    # ConfigMusic.sfi
    (target / "Audio/ConfigMusic.sfi").write_text(
        '''<root>
  <SoundConfiguration TargetName="PC" Format="PCM" IsStreamed="1" IsMusic="1"/>
  <SoundConfiguration TargetName="Durango" Format="PCM" IsStreamed="1" IsMusic="1"/>
  <SoundConfiguration TargetName="NX" Format="OPUS" IsStreamed="1" IsMusic="1"/>
  <SoundConfiguration TargetName="ORBIS" Format="ADPCM" IsStreamed="1" IsMusic="1"/>
</root>''')


def _write_timeline_files(target: Path, name: str) -> None:
    """Write Timeline TPLs, ACTs, and the tml.isc scene file."""
    for ty, tpl_name in [("Dance", "Motion"), ("Karaoke", "Karaoke")]:
        (target / f"Timeline/{name}_TML_{ty}.tpl").write_text(
            f'''params =
{{
\tNAME = "Actor_Template",
\tActor_Template =
\t{{
\t\tCOMPONENTS =
\t\t{{
\t\t\t{{
\t\t\t\tNAME = "TapeCase_Template",
                TapeCase_Template =
                {{
                    TapesRack =
                    {{
                        {{
                            TapeGroup =
                            {{
                                Entries =
                                {{
                                    {{
                                        TapeEntry =
                                        {{
                                            Label = "TML_{tpl_name}",
                                            Path = "World/MAPS/{name}/timeline/{name}_TML_{ty}.{ty[0].lower()}tape"
                                        }}
                                    }}
                                }}
                            }}
                        }}
                     }}
                }}
\t\t\t}}
\t\t}}
\t}}
}}''', encoding="utf-8")

        (target / f"Timeline/{name}_TML_{ty}.act").write_text(
            f'''params =
{{
    NAME = "Actor",
    Actor =
    {{
        LUA = "World/MAPS/{name}/timeline/{name}_TML_{ty}.tpl",
        COMPONENTS =
        {{
            {{
                NAME = "TapeCase_Component",
                TapeCase_Component =
                {{
                }}
            }}
        }}
    }}
}}''', encoding="utf-8")

    # tml.isc
    (target / f"Timeline/{name}_tml.isc").write_text(
        f'''<?xml version="1.0" encoding="ISO-8859-1"?>
<root>
    <Scene>
        <ACTORS NAME="Actor">
            <Actor name="TML_Dance" RELATIVEZ="0.0" SCALE="1.0 1.0" xFLIPPED="0" USERFRIENDLY="TML_Dance" POS2D="0 0" ANGLE="0.0" INSTANCEDATAFILE="World/MAPS/{name}/timeline/{name}_TML_Dance.act" LUA="World/MAPS/{name}/timeline/{name}_TML_Dance.tpl">
                <COMPONENTS NAME="TapeCase_Component">
                    <TapeCase_Component />
                </COMPONENTS>
            </Actor>
        </ACTORS>
        <ACTORS NAME="Actor">
            <Actor name="TML_Karaoke" RELATIVEZ="0.0" SCALE="1.0 1.0" xFLIPPED="0" USERFRIENDLY="TML_Karaoke" POS2D="0 0" ANGLE="0.0" INSTANCEDATAFILE="World/MAPS/{name}/timeline/{name}_TML_Karaoke.act" LUA="World/MAPS/{name}/timeline/{name}_TML_Karaoke.tpl">
                <COMPONENTS NAME="TapeCase_Component">
                    <TapeCase_Component />
                </COMPONENTS>
            </Actor>
        </ACTORS>
    </Scene>
</root>''', encoding="utf-8")


def _write_videoscoach_files(target: Path, name: str) -> None:
    """Write VideosCoach MPDs, video player ACTs, and video ISC files."""
    for mpd_name in [name, f"{name}_MapPreview"]:
        (target / f"VideosCoach/{mpd_name}.mpd").write_text(
            f'''<?xml version="1.0"?>
<MPD xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns="urn:mpeg:DASH:schema:MPD:2011" xsi:schemaLocation="urn:mpeg:DASH:schema:MPD:2011" type="static" mediaPresentationDuration="PT230S" minBufferTime="PT1S" profiles="urn:webm:dash:profile:webm-on-demand:2012">
\t<Period id="0" start="PT0S" duration="PT230S">
\t\t<AdaptationSet id="0" mimeType="video/webm" codecs="vp9" lang="eng" maxWidth="1920" maxHeight="1080" subsegmentAlignment="true" subsegmentStartsWithSAP="1" bitstreamSwitching="true">
\t\t\t<Representation id="0" bandwidth="4000000">
\t\t\t\t<BaseURL>jmcs://jd-contents/{name}/{mpd_name}.webm</BaseURL>
\t\t\t\t<SegmentBase indexRange="0-1000">
\t\t\t\t\t<Initialization range="0-500" />
\t\t\t\t</SegmentBase>
\t\t\t</Representation>
\t\t</AdaptationSet>
\t</Period>
</MPD>''', encoding="utf-8")

    (target / "VideosCoach/video_player_main.act").write_text(
        f'''params =
{{
    NAME="Actor",
    Actor =
    {{
        LUA = "world/_common/videoscreen/video_player_main.tpl",
        COMPONENTS =
        {{
            {{
                NAME="PleoComponent",
                PleoComponent =
                {{
                    Video = "World/MAPS/{name}/videoscoach/{name}.webm",
                    dashMPD = "World/MAPS/{name}/videoscoach/{name}.mpd"
                }}
            }}
        }}
    }}
}}''', encoding="utf-8")

    (target / "VideosCoach/video_player_map_preview.act").write_text(
        f'''params =
{{
    NAME="Actor",
    Actor =
    {{
        LUA = "world/_common/videoscreen/video_player_map_preview.tpl",
        COMPONENTS =
        {{
            {{
                NAME="PleoComponent",
                PleoComponent =
                {{
                    Video = "World/MAPS/{name}/videoscoach/{name}.webm",
                    dashMPD = "World/MAPS/{name}/videoscoach/{name}.mpd",
                    channelID = "{name}"
                }}
            }}
        }}
    }}
}}''', encoding="utf-8")

    (target / f"VideosCoach/{name}_video.isc").write_text(
        f'''<?xml version="1.0" encoding="ISO-8859-1"?>
<root>
    <Scene>
        <ACTORS NAME="Actor">
            <Actor RELATIVEZ="-1.000000" SCALE="1.000000 1.000000" xFLIPPED="0" USERFRIENDLY="VideoScreen" POS2D="0.000000 -4.500000" ANGLE="0.000000" INSTANCEDATAFILE="World/MAPS/{name}/videoscoach/video_player_main.act" LUA="world/_common/videoscreen/video_player_main.tpl">
            </Actor>
        </ACTORS>
        <ACTORS NAME="Actor">
            <Actor RELATIVEZ="0.000000" SCALE="3.941238 2.220000" xFLIPPED="0" USERFRIENDLY="VideoOutput" POS2D="0.000000 0.000000" ANGLE="0.000000" INSTANCEDATAFILE="world/_common/videoscreen/video_output_main.act" LUA="world/_common/videoscreen/video_output_main.tpl">
                <COMPONENTS NAME="PleoTextureGraphicComponent">
                    <PleoTextureGraphicComponent customAnchor="0.000000 0.000000" channelID="">
                        <material>
                            <GFXMaterialSerializable shaderPath="world/_common/matshader/pleofullscreen.msh">
                                <textureSet>
                                    <GFXMaterialTexturePathSet diffuse="" />
                                </textureSet>
                            </GFXMaterialSerializable>
                        </material>
                    </PleoTextureGraphicComponent>
                </COMPONENTS>
            </Actor>
        </ACTORS>
    </Scene>
</root>''', encoding="utf-8")

    (target / f"VideosCoach/{name}_video_map_preview.isc").write_text(
        f'''<?xml version="1.0" encoding="ISO-8859-1"?>
<root>
    <Scene>
        <ACTORS NAME="Actor">
            <Actor RELATIVEZ="-1.000000" SCALE="1.000000 1.000000" xFLIPPED="0" USERFRIENDLY="VideoScreenPreview" POS2D="0.000000 -4.500000" ANGLE="0.000000" INSTANCEDATAFILE="World/MAPS/{name}/videoscoach/video_player_map_preview.act" LUA="world/_common/videoscreen/video_player_map_preview.tpl">
            </Actor>
        </ACTORS>
    </Scene>
</root>''', encoding="utf-8")


def _write_menuart_files(
    target: Path,
    name: str,
    num_coach: int,
    optional_arts: Optional[List[str]] = None,
) -> None:
    """Write MenuArt actor ACTs and the menuart.isc scene file."""
    optional_arts = optional_arts or []
    coach_arts = [f"coach_{i}" for i in range(1, num_coach + 1)] if num_coach > 0 else []
    arts = ['cover_generic', 'cover_online'] + optional_arts + coach_arts

    for art in arts:
        (target / f"MenuArt/Actors/{name}_{art}.act").write_text(
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
                                    diffuse = "World/MAPS/{name}/menuart/textures/{name}_{art}.tga"
                                }}
                            }},
                            shaderPath = "World/_COMMON/MatShader/MultiTexture_1Layer.msh"
                        }}
                    }}
                }}
            }}
        }}
    }}
}}''', encoding="utf-8")

    # MenuArt ISC
    (target / f"MenuArt/{name}_menuart.isc").write_text(
        f'''<?xml version="1.0" encoding="ISO-8859-1"?>
<root>
\t<Scene ENGINE_VERSION="140999" GRIDUNIT="0.500000" DEPTH_SEPARATOR="0" NEAR_SEPARATOR="1.000000 0.000000 0.000000 0.000000, 0.000000 1.000000 0.000000 0.000000, 0.000000 0.000000 1.000000 0.000000, 0.000000 0.000000 0.000000 1.000000" FAR_SEPARATOR="1.000000 0.000000 0.000000 0.000000, 0.000000 1.000000 0.000000 0.000000, 0.000000 0.000000 1.000000 0.000000, 0.000000 0.000000 0.000000 1.000000" viewFamily="1">
\t\t<ACTORS NAME="Actor">
			<Actor RELATIVEZ="0.000000" SCALE="0.300000 0.300000" xFLIPPED="0" USERFRIENDLY="{name}_cover_generic" POS2D="266.087555 197.629959" ANGLE="0.000000" INSTANCEDATAFILE="World/MAPS/{name}/menuart/actors/{name}_cover_generic.act" LUA="enginedata/actortemplates/tpl_materialgraphiccomponent2d.tpl">
				<COMPONENTS NAME="MaterialGraphicComponent">
\t\t\t\t\t<MaterialGraphicComponent colorComputerTagId="0" renderInTarget="0" disableLight="0" disableShadow="-1" AtlasIndex="0" customAnchor="0.000000 0.000000" SinusAmplitude="0.000000 0.000000 0.000000" SinusSpeed="1.000000" AngleX="0.000000" AngleY="0.000000">
\t\t\t\t\t\t<PrimitiveParameters>
\t\t\t\t\t\t\t<GFXPrimitiveParam colorFactor="1.000000 1.000000 1.000000 1.000000" />
\t\t\t\t\t\t</PrimitiveParameters>
\t\t\t\t\t\t<ENUM NAME="anchor" SEL="1" />
\t\t\t\t\t\t<material>
\t\t\t\t\t\t\t<GFXMaterialSerializable ATL_Channel="0" shaderPath="World/_COMMON/MatShader/MultiTexture_1Layer.msh" stencilTest="0" alphaTest="4294967295" alphaRef="4294967295">
\t\t\t\t\t\t\t\t<textureSet>
\t\t\t\t\t\t\t\t\t<GFXMaterialTexturePathSet diffuse="World/MAPS/{name}/menuart/textures/{name}_cover_generic.tga" />
\t\t\t\t\t\t\t\t</textureSet>
\t\t\t\t\t\t\t</GFXMaterialSerializable>
\t\t\t\t\t\t</material>
\t\t\t\t\t\t<ENUM NAME="oldAnchor" SEL="1" />
\t\t\t\t\t</MaterialGraphicComponent>
\t\t\t\t</COMPONENTS>
\t\t\t</Actor>
\t\t</ACTORS>
\t\t<ACTORS NAME="Actor">
\t\t\t<Actor RELATIVEZ="0.000000" SCALE="0.300000 0.300000" xFLIPPED="0" USERFRIENDLY="{name}_cover_online" POS2D="-150.000000 0.000000" ANGLE="0.000000" INSTANCEDATAFILE="World/MAPS/{name}/menuart/actors/{name}_cover_online.act" LUA="enginedata/actortemplates/tpl_materialgraphiccomponent2d.tpl">
\t\t\t\t<COMPONENTS NAME="MaterialGraphicComponent">
\t\t\t\t\t<MaterialGraphicComponent colorComputerTagId="0" renderInTarget="0" disableLight="0" disableShadow="-1" AtlasIndex="0" customAnchor="0.000000 0.000000" SinusAmplitude="0.000000 0.000000 0.000000" SinusSpeed="1.000000" AngleX="0.000000" AngleY="0.000000">
\t\t\t\t\t\t<PrimitiveParameters>
\t\t\t\t\t\t\t<GFXPrimitiveParam colorFactor="1.000000 1.000000 1.000000 1.000000" />
\t\t\t\t\t\t</PrimitiveParameters>
\t\t\t\t\t\t<ENUM NAME="anchor" SEL="1" />
\t\t\t\t\t\t<material>
\t\t\t\t\t\t\t<GFXMaterialSerializable ATL_Channel="0" shaderPath="World/_COMMON/MatShader/MultiTexture_1Layer.msh" stencilTest="0" alphaTest="4294967295" alphaRef="4294967295">
\t\t\t\t\t\t\t\t<textureSet>
\t\t\t\t\t\t\t\t\t<GFXMaterialTexturePathSet diffuse="World/MAPS/{name}/menuart/textures/{name}_cover_online.tga" />
\t\t\t\t\t\t\t\t</textureSet>
\t\t\t\t\t\t\t</GFXMaterialSerializable>
\t\t\t\t\t\t</material>
\t\t\t\t\t\t<ENUM NAME="oldAnchor" SEL="1" />
\t\t\t\t\t</MaterialGraphicComponent>
\t\t\t\t</COMPONENTS>
\t\t\t</Actor>
\t\t</ACTORS>
\t</Scene>
</root>''', encoding="utf-8")


def _write_main_scene_isc(target: Path, name: str, has_autodance: bool = True) -> None:
    """Write the root MAIN_SCENE.isc that ties all subsystems together."""
    autodance_block = ""
    if has_autodance:
        autodance_block = f'''
\t\t<ACTORS NAME="SubSceneActor">
\t\t\t<SubSceneActor RELATIVEZ="0.000000" SCALE="1.000000 1.000000" xFLIPPED="0" USERFRIENDLY="{name}_AUTODANCE" POS2D="0.000000 0.000000" ANGLE="0.000000" INSTANCEDATAFILE="" LUA="enginedata/actortemplates/subscene.tpl" RELATIVEPATH="World/MAPS/{name}/autodance/{name}_autodance.isc" EMBED_SCENE="0" IS_SINGLE_PIECE="0" ZFORCED="1" DIRECT_PICKING="1">
\t\t\t\t<ENUM NAME="viewType" SEL="2" />
\t\t\t</SubSceneActor>
\t\t</ACTORS>'''

    (target / f"{name}_MAIN_SCENE.isc").write_text(
        f'''<?xml version="1.0" encoding="ISO-8859-1"?>
<root>
\t<Scene ENGINE_VERSION="81615" GRIDUNIT="2.000000" DEPTH_SEPARATOR="0" NEAR_SEPARATOR="1.000000 0.000000 0.000000 0.000000, 0.000000 1.000000 0.000000 0.000000, 0.000000 0.000000 1.000000 0.000000, 0.000000 0.000000 0.000000 1.000000" FAR_SEPARATOR="1.000000 0.000000 0.000000 0.000000, 0.000000 1.000000 0.000000 0.000000, 0.000000 0.000000 1.000000 0.000000, 0.000000 0.000000 0.000000 1.000000">
\t\t<ACTORS NAME="SubSceneActor">
\t\t\t<SubSceneActor RELATIVEZ="0.000000" SCALE="1.000000 1.000000" xFLIPPED="0" USERFRIENDLY="{name}_AUDIO" POS2D="0.000000 0.000000" ANGLE="0.000000" INSTANCEDATAFILE="" LUA="enginedata/actortemplates/subscene.tpl" RELATIVEPATH="World/MAPS/{name}/audio/{name}_audio.isc" EMBED_SCENE="0" IS_SINGLE_PIECE="0" ZFORCED="1" DIRECT_PICKING="1">
\t\t\t\t<ENUM NAME="viewType" SEL="2" />
\t\t\t</SubSceneActor>
\t\t</ACTORS>
\t\t<ACTORS NAME="SubSceneActor">
\t\t\t<SubSceneActor RELATIVEZ="0.000000" SCALE="1.000000 1.000000" xFLIPPED="0" USERFRIENDLY="{name}_CINE" POS2D="0.000000 0.000000" ANGLE="0.000000" INSTANCEDATAFILE="" LUA="enginedata/actortemplates/subscene.tpl" RELATIVEPATH="World/MAPS/{name}/cinematics/{name}_cine.isc" EMBED_SCENE="0" IS_SINGLE_PIECE="0" ZFORCED="1" DIRECT_PICKING="1">
\t\t\t\t<ENUM NAME="viewType" SEL="2" />
\t\t\t</SubSceneActor>
\t\t</ACTORS>
\t\t<ACTORS NAME="SubSceneActor">
\t\t\t<SubSceneActor RELATIVEZ="0.000000" SCALE="1.000000 1.000000" xFLIPPED="0" USERFRIENDLY="{name}_TML" POS2D="0.000000 0.000000" ANGLE="0.000000" INSTANCEDATAFILE="" LUA="enginedata/actortemplates/subscene.tpl" RELATIVEPATH="World/MAPS/{name}/timeline/{name}_tml.isc" EMBED_SCENE="0" IS_SINGLE_PIECE="0" ZFORCED="1" DIRECT_PICKING="1">
\t\t\t\t<ENUM NAME="viewType" SEL="2" />
\t\t\t</SubSceneActor>
\t\t</ACTORS>{autodance_block}
\t\t<ACTORS NAME="SubSceneActor">
\t\t\t<SubSceneActor RELATIVEZ="0.000000" SCALE="1.000000 1.000000" xFLIPPED="0" USERFRIENDLY="{name}_VIDEO" POS2D="0.000000 0.000000" ANGLE="0.000000" INSTANCEDATAFILE="" LUA="enginedata/actortemplates/subscene.tpl" RELATIVEPATH="World/MAPS/{name}/videoscoach/{name}_video.isc" EMBED_SCENE="0" IS_SINGLE_PIECE="0" ZFORCED="1" DIRECT_PICKING="1">
\t\t\t\t<ENUM NAME="viewType" SEL="2" />
\t\t\t</SubSceneActor>
\t\t</ACTORS>
\t\t<ACTORS NAME="Actor">
\t\t\t<Actor RELATIVEZ="0.000000" SCALE="1.000000 1.000000" xFLIPPED="0" USERFRIENDLY="{name} Main" POS2D="0.000000 0.000000" ANGLE="0.000000" INSTANCEDATAFILE="World/MAPS/{name}/songdesc.act" LUA="World/MAPS/{name}/songdesc.tpl">
\t\t\t\t<COMPONENTS NAME="JD_SongDescComponent">
\t\t\t\t\t<JD_SongDescComponent />
\t\t\t\t</COMPONENTS>
\t\t\t</Actor>
\t\t</ACTORS>
\t\t<ACTORS NAME="SubSceneActor">
\t\t\t<SubSceneActor RELATIVEZ="0.000000" SCALE="1.000000 1.000000" xFLIPPED="0" USERFRIENDLY="{name}_menuart" POS2D="0.000000 0.000000" ANGLE="0.000000" INSTANCEDATAFILE="" LUA="enginedata/actortemplates/subscene.tpl" RELATIVEPATH="World/MAPS/{name}/menuart/{name}_menuart.isc" EMBED_SCENE="0" IS_SINGLE_PIECE="0" ZFORCED="1" DIRECT_PICKING="1">
\t\t\t\t<ENUM NAME="viewType" SEL="3" />
\t\t\t</SubSceneActor>
\t\t</ACTORS>
\t\t<sceneConfigs>
\t\t\t<SceneConfigs activeSceneConfig="0">
\t\t\t\t<sceneConfigs NAME="JD_MapSceneConfig">
\t\t\t\t\t<JD_MapSceneConfig hud="0" cursors="0">
\t\t\t\t\t\t<ENUM NAME="type" SEL="1" />
\t\t\t\t\t\t<ENUM NAME="musicscore" SEL="2" />
\t\t\t\t\t</JD_MapSceneConfig>
\t\t\t\t</sceneConfigs>
\t\t\t</SceneConfigs>
\t\t</sceneConfigs>
\t</Scene>
</root>''', encoding="utf-8")


def _write_autodance_stubs(target: Path, name: str, vst: float = 0.0) -> None:
    """Write Autodance ISC, TPL, and ACT stub files.

    Skips TPL write if it already contains real converted data (>1KB)
    from the tape conversion step, to avoid overwriting real data.
    """
    autodance_tpl_path = target / f"Autodance/{name}_autodance.tpl"
    if autodance_tpl_path.exists() and autodance_tpl_path.stat().st_size >= 1024:
        return

    # Non-zero placeholder values so the autodance recap screen doesn't crash
    # V1 Parity: song_pos must be in ticks (seconds * 1000 * 48)
    TICKS_PER_MS = 48
    # Ensure even negative VST results in a valid (minimum 24) tick value
    song_pos = max(int(vst * 1000.0 * TICKS_PER_MS + 24), 24)
    ad_duration = 16
    map_low = name.lower()

    (target / f"Autodance/{name}_autodance.isc").write_text(
f'''<?xml version="1.0" encoding="ISO-8859-1"?>
<root>
\t<Scene ENGINE_VERSION="81615" GRIDUNIT="0.500000" DEPTH_SEPARATOR="0" NEAR_SEPARATOR="1.000000 0.000000 0.000000 0.000000, 0.000000 1.000000 0.000000 0.000000, 0.000000 0.000000 1.000000 0.000000, 0.000000 0.000000 0.000000 1.000000" FAR_SEPARATOR="1.000000 0.000000 0.000000 0.000000, 0.000000 1.000000 0.000000 0.000000, 0.000000 0.000000 1.000000 0.000000, 0.000000 0.000000 0.000000 1.000000">
\t\t<ACTORS NAME="Actor">
\t\t\t<Actor RELATIVEZ="0.000000" SCALE="1.000000 1.000000" xFLIPPED="0" USERFRIENDLY="{name}_autodance" POS2D="0.000000 0.000000" ANGLE="0.000000" INSTANCEDATAFILE="World/MAPS/{name}/autodance/{name}_autodance.act" LUA="World/MAPS/{name}/autodance/{name}_autodance.tpl">
\t\t\t\t<COMPONENTS NAME="JD_AutodanceComponent">
\t\t\t\t\t<JD_AutodanceComponent />
\t\t\t\t</COMPONENTS>
\t\t\t</Actor>
\t\t</ACTORS>
\t\t<sceneConfigs>
\t\t\t<SceneConfigs activeSceneConfig="0" />
\t\t</sceneConfigs>
\t</Scene>
</root>''', encoding="utf-8")

    autodance_tpl_path.write_text(
        f'''params =
{{
\tNAME = "Actor_Template",
\tActor_Template =
\t{{
\t\tCOMPONENTS =
\t\t{{
\t\t\t{{
\t\t\t\tNAME = "JD_AutodanceComponent_Template",
\t\t\t\tJD_AutodanceComponent_Template =
\t\t\t\t{{
\t\t\t\t\tsong = "{name}",
\t\t\t\t\tautodanceData =
\t\t\t\t\t{{
\t\t\t\t\t\tJD_AutodanceData =
\t\t\t\t\t\t{{
\t\t\t\t\t\t\trecording_structure = {{
\t\t\t\t\t\t\t\tNAME = "JD_AutodanceRecordingStructure",
\t\t\t\t\t\t\t\tJD_AutodanceRecordingStructure =
\t\t\t\t\t\t\t\t{{
\t\t\t\t\t\t\t\t\trecords = {{
\t\t\t\t\t\t\t\t\t\t{{
\t\t\t\t\t\t\t\t\t\t\tNAME = "Record",
\t\t\t\t\t\t\t\t\t\t\tRecord =
\t\t\t\t\t\t\t\t\t\t\t{{
\t\t\t\t\t\t\t\t\t\t\t\tStart = {song_pos},
\t\t\t\t\t\t\t\t\t\t\t\tDuration = {ad_duration},
\t\t\t\t\t\t\t\t\t\t\t}},
\t\t\t\t\t\t\t\t\t\t}},
\t\t\t\t\t\t\t\t\t}},
\t\t\t\t\t\t\t\t}},
\t\t\t\t\t\t\t}},
\t\t\t\t\t\t\tvideo_structure = {{
\t\t\t\t\t\t\t\tNAME = "JD_AutodanceVideoStructure",
\t\t\t\t\t\t\t\tJD_AutodanceVideoStructure =
\t\t\t\t\t\t\t\t{{
\t\t\t\t\t\t\t\t\tSongStartPosition = {song_pos},
\t\t\t\t\t\t\t\t\tDuration = {ad_duration},
\t\t\t\t\t\t\t\t\tThumbnailTime = 0,
\t\t\t\t\t\t\t\t\tFadeOutDuration = 3,
\t\t\t\t\t\t\t\t\tGroundPlanePath = "invalid ",
\t\t\t\t\t\t\t\t\tFirstLayerTripleBackgroundPath = "invalid ",
\t\t\t\t\t\t\t\t\tSecondLayerTripleBackgroundPath = "invalid ",
\t\t\t\t\t\t\t\t\tThirdLayerTripleBackgroundPath = "invalid ",
\t\t\t\t\t\t\t\t\tplayback_events = {{
\t\t\t\t\t\t\t\t\t\t{{
\t\t\t\t\t\t\t\t\t\t\tNAME = "PlaybackEvent",
\t\t\t\t\t\t\t\t\t\t\tPlaybackEvent =
\t\t\t\t\t\t\t\t\t\t\t{{
\t\t\t\t\t\t\t\t\t\t\t\tClipNumber = 0,
\t\t\t\t\t\t\t\t\t\t\t\tStartClip = 0,
\t\t\t\t\t\t\t\t\t\t\t\tStartTime = 0,
\t\t\t\t\t\t\t\t\t\t\t\tDuration = {ad_duration},
\t\t\t\t\t\t\t\t\t\t\t\tSpeed = 1,
\t\t\t\t\t\t\t\t\t\t\t}},
\t\t\t\t\t\t\t\t\t\t}},
\t\t\t\t\t\t\t\t\t}},
\t\t\t\t\t\t\t\t}},
\t\t\t\t\t\t\t}},
\t\t\t\t\t\t\tautodanceSoundPath = ""
\t\t\t\t\t\t}}
\t\t\t\t\t}}
\t\t\t\t}}
\t\t\t}},
\t\t}}
\t}}
}}''', encoding="utf-8")

    (target / f"Autodance/{name}_autodance.act").write_text(
        f'''params =
{{
\tNAME = "Actor",
\tActor =
\t{{
\t\tLUA = "World/MAPS/{name}/autodance/{name}_autodance.tpl",
\t}}
}}''', encoding="utf-8")


def _write_cinematics_stubs(target: Path, name: str) -> None:
    """Write Cinematics tape, ISC, TPL, and ACT stubs."""
    (target / f"Cinematics/{name}_MainSequence.tape").write_text(
        f'''params =
{{
    NAME = "Tape",
    Tape =
    {{
        Clips = {{
        }},
        TapeClock = 0,
        TapeBarCount = 1,
        FreeResourcesAfterPlay = 0,
        MapName = "{name}",
        SoundwichEvent = ""
    }}
}}''', encoding="utf-8")

    (target / f"Cinematics/{name}_cine.isc").write_text(
        f'''<?xml version="1.0" encoding="ISO-8859-1"?>
<root>
\t<Scene ENGINE_VERSION="55299" GRIDUNIT="0.500000" DEPTH_SEPARATOR="0" NEAR_SEPARATOR="1.000000 0.000000 0.000000 0.000000, 0.000000 1.000000 0.000000 0.000000, 0.000000 0.000000 1.000000 0.000000, 0.000000 0.000000 0.000000 1.000000" FAR_SEPARATOR="1.000000 0.000000 0.000000 0.000000, 0.000000 1.000000 0.000000 0.000000, 0.000000 0.000000 1.000000 0.000000, 0.000000 0.000000 0.000000 1.000000">
\t\t<ACTORS NAME="Actor">
\t\t\t<Actor RELATIVEZ="0.000000" SCALE="1.000000 1.000000" xFLIPPED="0" USERFRIENDLY="{name}_MainSequence" POS2D="0.000000 0.000000" ANGLE="0.000000" INSTANCEDATAFILE="World/MAPS/{name}/cinematics/{name}_mainsequence.act" LUA="World/MAPS/{name}/cinematics/{name}_mainsequence.tpl">
\t\t\t\t<COMPONENTS NAME="MasterTape">
\t\t\t\t\t<MasterTape bankState="4294967295" />
\t\t\t\t</COMPONENTS>
\t\t\t</Actor>
\t\t</ACTORS>
\t\t<sceneConfigs>
\t\t\t<SceneConfigs activeSceneConfig="0" />
\t\t</sceneConfigs>
\t</Scene>
</root>''', encoding="utf-8")

    (target / f"Cinematics/{name}_mainsequence.tpl").write_text(
        f'''params =
{{
    NAME = "Actor_Template",
    Actor_Template =
    {{
        COMPONENTS =
        {{
            {{
                NAME = "MasterTape_Template",
                MasterTape_Template =
                {{
                    TapePath = "World/MAPS/{name.lower()}/cinematics/{name}_MainSequence.tape"
                }}
            }}
        }}
    }}
}}''', encoding="utf-8")

    (target / f"Cinematics/{name}_mainsequence.act").write_text(
        f'''params =
{{
    NAME = "Actor",
    Actor =
    {{
        LUA = "World/MAPS/{name.lower()}/cinematics/{name}_mainsequence.tpl",
        COMPONENTS =
        {{
            {{
                NAME = "MasterTape"
            }}
        }}
    }}
}}''', encoding="utf-8")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def write_game_files(
    map_data: NormalizedMapData,
    target_dir: str | Path,
    config: Optional[AppConfig] = None,
) -> float:
    """Generate all UbiArt engine files for a map.

    Args:
        map_data:   Normalized map data.
        target_dir: Output directory (the map's World/MAPS/<codename>/ folder).
        config:     App configuration.

    Returns:
        The effective videoStartTime used for the installation.

    Raises:
        GameWriterError: If file generation fails.
    """
    config = config or AppConfig()
    target = Path(target_dir)
    name = map_data.codename
    mt = map_data.music_track
    sd = map_data.song_desc
    vst = map_data.effective_video_start_time

    # Check if source has a valid non-zero videoStartTime (e.g., JDNext/IPK maps).
    # Only synthesize as a last-resort fallback for Xbox 360 rips with 0.0.
    # This preserves the original sync for maps like JDNext that have already-correct offsets.
    if vst == 0.0 and map_data.source_dir and map_data.source_dir.exists():
        try:
            audio_dir = map_data.source_dir / "Audio"
            if audio_dir.exists():
                # Find .trk files for this map (case-insensitive search)
                trk_found = False
                for trk_path in audio_dir.glob("*.trk"):
                    if name.lower() in trk_path.stem.lower() or trk_path.stem.lower() == name.lower():
                        try:
                            content = trk_path.read_text(encoding="utf-8")
                            match = re.search(r"videoStartTime\s*=\s*([-+]?\d*\.?\d+)", content)
                            if match:
                                source_vst = float(match.group(1))
                                # Auto-fix if ticks were accidentally written previously
                                if abs(source_vst) > 1000:
                                    source_vst /= 48000.0
                                # If source has a valid non-zero value, preserve it
                                if abs(source_vst) > 0.0001:
                                    vst = source_vst
                                    logger.info(
                                        "Source .trk contains valid videoStartTime=%.6f; "
                                        "preserving for JDNext/IPK map '%s' (will not synthesize)",
                                        vst, name
                                    )
                                    trk_found = True
                                    break
                        except (OSError, ValueError):
                            continue
        except Exception as e:
            logger.debug("Failed to check source .trk for videoStartTime: %s", e)

    # Safety check from V1: if videoStartTime is 0.0 but startBeat < 0,
    # the game engine will assert "adding a brick in the past".
    # Auto-synthesize from markers as a last-resort fallback.
    # This only applies when the source doesn't have a valid offset (e.g., Xbox 360 rips).
    if vst == 0.0 and mt.start_beat < 0:
        idx = abs(mt.start_beat)
        if mt.markers and idx < len(mt.markers):
            vst = -(mt.markers[idx] / 48.0 / 1000.0)
            logger.warning(
                "videoStartTime was 0.0 with startBeat=%d; "
                "auto-synthesized %.5f from markers (source had no valid offset)",
                mt.start_beat, vst,
            )
        else:
            logger.warning(
                "videoStartTime is 0.0 with startBeat=%d. "
                "The game may assert 'adding a brick in the past'. "
                "Use the sync panel to set a negative offset.",
                mt.start_beat,
            )

    try:
        setup_dirs(target)

        # Determine coach count.
        # JD2021 gameplay uses 4 player slots; maps with >4 coaches still map into 4 slots.
        num_coach = sd.num_coach
        if num_coach < 1:
            textures_dir = target / "MenuArt/textures"
            if textures_dir.exists():
                coach_imgs = [
                    f for f in textures_dir.iterdir()
                    if "coach_" in f.name.lower() and f.suffix in (".png", ".tga")
                ]
                num_coach = len(coach_imgs) if coach_imgs else 1
            else:
                num_coach = 1

        if num_coach > 4:
            logger.debug("Clamping NumCoach from %d to 4 (JD2021 player slot limit)", num_coach)
            num_coach = 4

        media = map_data.media
        optional_arts: List[str] = []
        if media.cover_albumbkg_path:
            optional_arts.append("cover_albumbkg")
        if media.cover_albumcoach_path:
            optional_arts.append("cover_albumcoach")
        if media.banner_bkg_path:
            optional_arts.append("banner_bkg")
        if media.map_bkg_path:
            optional_arts.append("map_bkg")

        # Audio files (already existed in V2)
        _write_musictrack_trk(target, name, mt, vst)
        _write_songdesc(target, name, sd, num_coach, vst, config, map_data.has_autodance)
        _write_audio_isc(target, name)

        # Timeline (Dance + Karaoke TapeCase files)
        _write_timeline_files(target, name)

        # VideosCoach (MPD manifests + video player actors)
        _write_videoscoach_files(target, name)

        # MenuArt (texture actor ACTs + menuart.isc)
        _write_menuart_files(target, name, num_coach, optional_arts)

        # Root scene ISC (ties all subsystems together)
        _write_main_scene_isc(target, name, map_data.has_autodance)

        # Autodance stubs (placeholder so recap screen doesn't crash)
        if map_data.has_autodance:
            _write_autodance_stubs(target, name, vst)

        # Cinematics stubs (MainSequence tape + ISC)
        _write_cinematics_stubs(target, name)

        logger.debug("Game files written for '%s' to %s", name, target)
        return vst

    except Exception as exc:
        raise GameWriterError(f"Failed to write game files for '{name}': {exc}") from exc
