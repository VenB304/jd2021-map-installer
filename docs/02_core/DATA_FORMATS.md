# Data Formats Reference

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

Binary, text, and Unity data formats consumed or produced by the JD2021 Map Installer pipeline.

---

## Table of Contents

- [CKD (Cooked Data)](#ckd-cooked-data)
- [XTX (NX/Switch Texture)](#xtx-nxswitch-texture)
- [Xbox 360 Texture CKD](#xbox-360-texture-ckd)
- [IPK (UbiArt Archive)](#ipk-ubiart-archive)
- [Unity AssetBundle (JDNext)](#unity-assetbundle-jdnext)
- [JDNext mapPackage JSON](#jdnext-mappackage-json)
- [ISC (Scene Graph)](#isc-scene-graph)
- [TPL (Template)](#tpl-template)
- [ACT (Actor Instance)](#act-actor-instance)
- [TRK (Music Track Timing)](#trk-music-track-timing)
- [DTAPE / KTAPE (Choreography Tapes)](#dtape--ktape-choreography-tapes)
- [BTAPE (Beats Tape)](#btape-beats-tape)
- [TAPE (Cinematic Tape)](#tape-cinematic-tape)
- [STAPE (Sequence Tape)](#stape-sequence-tape)
- [ILU (Sound Descriptor Include)](#ilu-sound-descriptor-include)
- [SFI (Sound Format Info)](#sfi-sound-format-info)
- [MPD (DASH Manifest)](#mpd-dash-manifest)
- [WAV Audio Requirements](#wav-audio-requirements)
- [Installer Settings JSON](#installer-settings-json)
- [installer_paths.json](#installer_pathsjson)

---

## Binary Formats

### CKD (Cooked Data)

CKD is UbiArt's cooked asset container format. Its internal payload varies
depending on the asset type.

**Header layout (44 bytes):**

| Offset | Size | Description |
|--------|------|-------------|
| 0-3 | 4 bytes | Magic: `\x00\x00\x00\x09` |
| 4-7 | 4 bytes | `TEX` marker (present for texture assets) |
| 8-43 | 36 bytes | Remaining header data |

**Constants:**

```
CKD_HEADER_SIZE = 44
CKD_MAGIC       = b'\x00\x00\x00\x09'
TEX_MAGIC       = b'TEX'
NVFD_MAGIC      = b'\x44\x46\x76\x4E'
```

**Texture CKD files** contain one of three payloads after the 44-byte header:

- **Switch textures (XTX):** identified by the NvFD magic `\x44\x46\x76\x4E`
  at the start of the payload. See [XTX section](#xtx-nxswitch-texture).
- **PC textures (DDS):** identified by the `DDS ` magic at the start of the
  payload. Decoded via Pillow after stripping the header.
- **Xbox 360 textures (DXT):** identified by GPU format codes (`0x52`=DXT1,
  `0x53`=DXT3, `0x54`=DXT5) at offset 32 of the payload. Requires byte-swap,
  untiling, and DDS reconstruction. See [Xbox 360 section](#xbox-360-texture-ckd).

**Non-texture CKD files** (`.tpl.ckd`, `.tape.ckd`, etc.) are plaintext JSON
with null-byte padding. They are loaded via `load_ckd()`, which strips
`\x00` bytes and leading/trailing whitespace, then parses the result as UTF-8
JSON. If JSON parsing fails, the binary CKD parser is invoked as a fallback.

**Binary CKD dispatch** (`binary_ckd.py`) uses two strategies:

1. **Filename-based dispatch:** Tape types are inferred from filename tokens
   (`dtape`, `ktape`, `btape`, `.tape.ckd`, `.stape.ckd`, `.adtape.ckd`,
   `.adrecording.ckd`, `.advideo.ckd`).
2. **Actor header CRC dispatch:** For `.tpl.ckd` files, a 48-byte Actor
   header is consumed and the template class CRC determines the parser:

| Template Class | CRC | Parser |
|----------------|-----|--------|
| `MusicTrackComponent_Template` | `0x02883A7E` | `_parse_musictrack_from_reader` |
| `JD_SongDescTemplate` | `0x8AC2B5C6` | `_parse_songdesc_from_reader` |
| `Actor_Template` | `0x1B857BCE` | (header-only, required prefix) |
| `AutodanceComponent_Template` | `0x51EA2CD0` | simple dict extraction |
| `SoundComponent_Template` | `0xD94D6C53` | simple dict extraction |
| `Tape` | `0x2AFED161` | `parse_btape` |

**BinaryReader** provides sequential big-endian reads: `u32`, `i32`, `f32`,
`u16`, `len_string` (length-prefixed UTF-8), `interned_string` (CRC32),
`split_path` (filename + path + path_id + padding).

---

### XTX (NX/Switch Texture)

XTX is the Nintendo Switch (NX) native texture format embedded inside
Switch-platform CKD files. The pipeline decodes XTX through the integrated
`xtx_extractor` module (ported from [XTX-Extractor](https://github.com/aboood40091/XTX-Extractor)).

**Decode pipeline:**

```
CKD (44-byte header strip) → XTX payload → readNv() → deswizzle → DDS → Pillow → TGA/PNG
```

**NvFD magic:** `0x44 0x46 0x76 0x4E` ("DFvN" in little-endian).

The deswizzle step reassembles tiled GPU blocks into row-linear DDS data. If
`xtx_extractor` is unavailable, the raw XTX payload is saved with a `.xtx`
extension for manual conversion.

---

### Xbox 360 Texture CKD

Xbox 360 CKD textures use a proprietary GPU descriptor + tiled DXT layout.

**GPU descriptor (52 bytes):**

| Offset | Field | Description |
|--------|-------|-------------|
| 32 | `fmt_code` (u32 BE) | DXT format: `0x52`=DXT1, `0x53`=DXT3, `0x54`=DXT5 |
| 36 | `size_word` (u32 BE) | Packed: `width = (word & 0x1FFF) + 1`, `height = ((word >> 13) & 0x1FFF) + 1` |

**Decode pipeline:**

```
CKD (44-byte header strip) → GPU descriptor parse → byte-swap (16-bit) → untile (Xenia-derived) → DDS build → Pillow → TGA/PNG
```

The untiling algorithm is ported from the Xenia Xbox 360 emulator and
handles bank/pipe/y-LSB interleaving for DXT block textures.

---

### IPK (UbiArt Archive)

IPK is UbiArt's package archive format. All multi-byte integers are
**big-endian**.

**Magic bytes:** `b'\x50\xEC\x12\xBA'`

**Header structure:**

| Field | Type | Description |
|-------|------|-------------|
| magic | uint32 | `0x50EC12BA` |
| version | uint32 | Archive version |
| platformsupported | uint32 | Platform bitmask |
| base_offset | uint32 | Global data offset added to per-file offsets |
| num_files | uint32 | Number of entries in the archive |
| compressed | uint32 | Compression flag |
| binaryscene | uint32 | Binary scene flag |
| binarylogic | uint32 | Binary logic flag |
| datasignature | uint32 | Data signature |
| enginesignature | uint32 | Engine signature |
| engineversion | uint32 | Engine version |
| num_files2 | uint32 | Secondary file count |

**Entry structure (per file):**

| Field | Type | Description |
|-------|------|-------------|
| numOffset | uint32 | Entry offset index |
| size | uint32 | Uncompressed size |
| compressed_size | uint32 | Compressed size |
| time_stamp | uint64 | File timestamp |
| offset | uint64 | Byte offset of file data within the archive |
| name_size | uint32 | Length of the filename string |
| file_name | bytes | Filename (length determined by name_size) |
| path_size | uint32 | Length of the path string |
| path_name | bytes | Path (length determined by path_size) |
| checksum | uint32 | CRC checksum |
| flag | uint32 | Entry flags |

**Decompression strategy:** For each compressed entry the pipeline attempts
decompression in the following order:

1. **zlib** -- try first.
2. **lzma** -- fall back if zlib raises an error.
3. **raw copy** -- treat data as uncompressed if both fail.

**Security:** Path traversal protection is enforced. Any entry whose resolved
path escapes the output directory is rejected.

**Multi-map bundles:** The extractor supports multi-map IPK bundles using
both standard (`world/maps/<codename>`) and legacy (`world/jd20XX/<codename>`)
directory structures. Codename inference uses filesystem scanning post-
extraction with header-scan corroboration. When multiple maps are detected,
selection is resolved by: explicit user request → filename stem matching →
first candidate fallback.

---

### Unity AssetBundle (JDNext)

JDNext maps ship as Unity AssetBundle (`.bundle`) files. The pipeline
supports two extraction backends in a configurable strategy order:

1. **AssetStudioModCLI** (preferred): External .NET tool that exports
   `Texture2D`, `TextAsset`, `MonoBehaviour`, `Sprite` directories.
2. **UnityPy** (fallback): Python-native library for in-process extraction.

**`jdnext_unitypy.py`** implements the UnityPy extraction pass:

| Object Type | Handler | Output |
|-------------|---------|--------|
| `Texture2D` | `parse_as_object()` → `image.save()` | `textures/*.png` |
| `AudioClip` | `parse_as_object()` → sample data write | `audio/*.<ext>` |
| `VideoClip` | `parse_as_object()` → `m_VideoData` write | `video/*.bin` |
| `TextAsset` | `parse_as_object()` → `m_Script` text write | `text/*.txt` |
| `MonoBehaviour` | `parse_as_dict()` → JSON dump | `typetree/*_monobehaviour.json` |
| Unknown types | `parse_as_dict()` → JSON dump | `typetree/*_<type>.json` |

**Encryption detection:** If UnityPy raises an "encrypted / no key provided"
error, the pipeline extracts `key_sig` and `data_sig` from the error message
for diagnostic reporting.

**UnityPy loading:** The module first attempts `import UnityPy`, then falls
back to `tools/UnityPy/` or a configured `third_party_tools_root`. A
fallback Unity version (`2021.3.0f1`) is set via `UnityPy.config.FALLBACK_UNITY_VERSION`.

**Strategy dispatch** (`jdnext_bundle_strategy.py`):

- `assetstudio_first`: Try AssetStudioModCLI, fall back to UnityPy.
- `unitypy_first`: Try UnityPy, fall back to AssetStudioModCLI.

The winning backend's output is post-processed by `map_assetstudio_output()`
which maps raw exports into a pipeline-compatible `mapped/` directory structure.

**Output artifacts:**

| Output File | Description |
|-------------|-------------|
| `summary.json` | Object counts and paths |
| `objects_index.json` | Per-object `path_id`, `type`, `name_hint`, `exported` |
| `strategy_summary.json` | Which backend won, error details |
| `mapping_summary.json` | Post-mapping file counts |

---

### JDNext mapPackage JSON

JDNext maps expose their metadata through a `map.json` file (extracted as a
`MonoBehaviour`). This JSON contains the same logical data as UbiArt CKDs
but in a different schema.

**Key top-level keys and their pipeline mappings:**

| mapPackage Key | Pipeline Target |
|----------------|-----------------|
| `SongDesc` (dict) | `SongDescription` dataclass |
| `SongDesc.MapName` | `codename` |
| `SongDesc.Title` / `Artist` | Song metadata |
| `SongDesc.NumCoach` / `MainCoach` | Coach configuration |
| `DanceData.MotionClips[]` | Synthesized `dtape.ckd` |
| `DanceData.PictoClips[]` | Synthesized `dtape.ckd` (PictogramClip entries) |
| `DanceData.GoldEffectClips[]` | Synthesized `dtape.ckd` (GoldEffectClip entries) |
| `KaraokeData.Clips[]` | Synthesized `ktape.ckd` |

**MusicTrack format** (`MusicTrack.json` MonoBehaviour):

The JDNext music track stores the same `MusicTrackStructure` under
`m_structure.MusicTrackStructure`. Markers use the `{VAL: N}` wrapper
format. The pipeline synthesizes a standard `musictrack.tpl.ckd` JSON
from this structure via `_synthesize_musictrack_tpl_ckd()`.

---

## Text / Structured Formats

### ISC (Scene Graph)

ISC files are XML documents that describe a UbiArt scene graph.

**Key requirements for JD2021:**

- `ENGINE_VERSION` attribute must be `"280000"`.
- Contains `ACTORS` elements that link to `.tpl` (template) and `.act` (actor
  instance) files.
- `sceneConfigs` elements hold scene-level configuration.

**Common scene roles:**

| Role | Description |
|------|-------------|
| MAIN_SCENE | Top-level map scene |
| audio | Audio scene graph |
| timeline | Timeline / sequencing |
| cinematics | Cinematic sequences |
| menuart | Menu artwork and UI |
| video | Video playback |
| autodance | Autodance mode |

---

### TPL (Template)

TPL files use **Lua table syntax**.

**Structure:**

```lua
params = {
    Actor_Template = {
        COMPONENTS = {
            -- component definitions
        }
    }
}
```

Templates define the reusable component layout for an actor. Common template
types include SongDesc, MusicTrack, Dance/Karaoke tapes, AMB sounds, and
autodance descriptors.

---

### ACT (Actor Instance)

ACT files use **Lua table syntax**.

**Structure:**

```lua
params = {
    Actor = {
        COMPONENTS = {
            -- instance-specific component data
        }
        -- references the parent TPL file
    }
}
```

An ACT file instantiates a TPL template with concrete data (paths, overrides,
runtime values).

---

### TRK (Music Track Timing)

TRK files use **Lua table syntax** and define the rhythmic structure of a song.

**Structure:**

```lua
structure = {
    MusicTrackStructure = {
        markers = {
            { VAL = <sample_position> },
            -- ...
        },
        startBeat = <int>,       -- typically negative, e.g. -5
        endBeat = <int>,
        videoStartTime = <float>, -- seconds, typically negative
        previewEntry = <float>,   -- beat index for song preview
    }
}
```

**Important:** All `markers` positions are in samples at **48 kHz**. The
`startBeat` value is usually negative to allow a pre-roll before beat zero.

---

### DTAPE / KTAPE (Choreography Tapes)

DTAPE (dance tape) and KTAPE (karaoke tape) files use **Lua table syntax**.

**Structure:**

```lua
params = {
    Clips = {
        -- array of timed clip events
    }
}
```

**Dance tape (.dtape) clip types:**

- `MotionClips` -- choreography move definitions.
- `PictogramClips` -- pictogram display triggers.
- `GoldEffectClips` -- gold move highlight effects.

**Karaoke tape (.ktape) clip types:**

- `KaraokeClips` -- lyric display events with timing.

**Timing resolution:** 24 ticks per beat.

**Binary dtape entry class IDs:**

| Class ID | Type |
|----------|------|
| 108, 112 | MotionClip |
| 56 | PictogramClip |
| 28 | GoldEffectClip |

**Binary ktape entry class IDs:**

| Class ID | Type |
|----------|------|
| 32, 80 | KaraokeClip (80 = extended with tolerances) |

---

### BTAPE (Beats Tape)

BTAPE files contain beat-level timing clips. The binary parser handles both
raw tape payloads and Actor-wrapped variants (identified by `\x00\x00\x00\x01`
prefix and validated against the `Tape` CRC `0x2AFED161`).

**BeatClip fields:** `id`, `track_id`, `is_active`, `start_time`, `duration`,
`beat_type`.

---

### TAPE (Cinematic Tape)

TAPE files use **Lua table syntax** and are structurally similar to dtape/ktape
but target animation and visual properties rather than gameplay events.

**Common clip types:**

| Clip Type | Description |
|-----------|-------------|
| AlphaClip | Opacity animation |
| RotationClip | Rotation animation |
| TranslationClip | Position animation |
| SizeClip | Scale animation |

**Curve keys** use the `vector2dNew(x, y)` format for keyframe values.

**Actor resolution:** `ActorPaths` are resolved from `ActorIndices` in the
containing scene graph.

**Binary cinematic tape entry class IDs:**

| Class ID | Type |
|----------|------|
| 136 | SoundSetClip |
| 160 | TapeReferenceClip |

---

### STAPE (Sequence Tape)

STAPE files contain BPM and time-signature data for each section of a song.

These files are converted from CKD-wrapped JSON via `the CKD-to-Lua converter` into Lua
table syntax.

---

### ILU (Sound Descriptor Include)

ILU files use **Lua table syntax** and define ambient sound descriptors.

**Structure:**

```lua
appendTable({
    SoundDescriptor_Template = {
        name = "<descriptor_name>",
        volume = <float>,
        category = "<category>",
        files = {
            -- list of audio file paths
        }
    }
})
```

Used for AMB (ambient) sounds within a map.

---

## XML Metadata Formats

### SFI (Sound Format Info)

SFI files declare audio encoding metadata per platform. JD2021 uses a
multi-target format:

**Example:**

```xml
<root>
  <SoundConfiguration TargetName="PC" Format="PCM" IsStreamed="1" IsMusic="1"/>
  <SoundConfiguration TargetName="Durango" Format="PCM" IsStreamed="1" IsMusic="1"/>
  <SoundConfiguration TargetName="NX" Format="OPUS" IsStreamed="1" IsMusic="1"/>
  <SoundConfiguration TargetName="ORBIS" Format="ADPCM" IsStreamed="1" IsMusic="1"/>
</root>
```

---

### MPD (DASH Manifest)

MPD files follow the MPEG-DASH manifest schema, adapted for UbiArt local
playback.

**Namespace (must be capitalized exactly as shown):**

```
urn:mpeg:DASH:schema:MPD:2011
```

**Profile:**

```
urn:webm:dash:profile:webm-on-demand:2012
```

**BaseURL format:**

```
jmcs://jd-contents/[MapName]/[MapName].webm
```

The `jmcs://` scheme forces the engine to use a local content fallback instead
of fetching from a remote server.

---

## Audio Requirements

### WAV Audio

All WAV audio files used by the pipeline must meet the following requirements:

| Property | Required Value |
|----------|---------------|
| Sample rate | 48000 Hz (48 kHz) |
| Format | PCM |
| Channels | Stereo (2 channels) |

The 48 kHz sample rate is mandatory because TRK marker positions are expressed
in samples at that rate.

**Trimming:** The WAV is trimmed by `abs(a_offset)` seconds from the start of
the source OGG file during conversion.

**JDNext audio:** JDNext sources may provide audio as `.opus` files instead of
`.ogg`. The normalizer handles both formats transparently (priority: `.ogg` >
`.opus` > `.wav` > `.wav.ckd`).

---

## Configuration Files

### Installer Settings JSON

Located at `installer_settings.json` in the project root. See [MAP_CONFIG_FORMAT.md](../04_reference/MAP_CONFIG_FORMAT.md) for full documentation.

**Fields:**

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| skip_preflight | bool | false | Skip pre-flight checks on startup |
| suppress_offset_notification | bool | false | Don't show offset refinement popup after install |
| auto_cleanup_downloads | bool | false | Auto-delete intermediates after Apply & Finish |
| default_quality | string | "ultra_hd" | Default video quality tier |

---

### installer_paths.json

Caches game-path discovery results so the installer does not need to re-scan on
every launch.

**Fields:**

| Field | Type | Description |
|-------|------|-------------|
| jd21_dir | string | Root directory of the JD2021 installation |
| sku_scene | string | Full path to `SkuScene_Maps_PC_All.isc` |

**Validation on load:** The `sku_scene` file must still exist on disk. If the
file is missing the cached paths are discarded and discovery runs again.
