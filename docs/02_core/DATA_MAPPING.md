# Data Mapping Specification

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This document details how raw data from multiple source formats (JDU JSON
payloads, binary CKD files, IPK archives, and JDNext Unity bundles) is
mapped, transformed, or ignored when porting maps to the Just Dance 2021 PC
(UbiArt) engine.

---

## 1. Map Metadata (SongDesc)

### JDU / CKD JSON Sources

| JDU Property | JD2021 PC Property | Transformation / Note |
|--------------|-------------------|-----------------------|
| `title` | `Title` | Direct string copy. |
| `artist` | `Artist` | Direct string copy. |
| `difficulty` | `Difficulty` | 1-4 scale mapped to UbiArt enum. |
| `sweatDifficulty`| `SweatDifficulty` | 1-3 scale. |
| `lyricsType` | `LyricsType` | **CRITICAL**: 0=None, 1=Karaoke. Must be correct for UI rendering. |
| `backgroundType` | `backgroundType` | Typically 0 (2D Video). |
| `coachNumbers` | `NumCoach` | Number of dancers (1-4). |
| `mainCoach` | `MainCoach` | Index of the primary coach (0-3, or -1 for default). Extracted from CKD. |
| `jdVersion` | `OriginalJDVersion`| Year of original release (e.g., 2014, 2015). |
| `DancerName` | `DancerName` | Extracted from CKD, defaults to `"Unknown Dancer"`. |
| `Credits` | `Credits` | Full rights/credits string from CKD. |
| `Tags` | `Tags` | Extracted from CKD (e.g. `["Extreme", "Main"]`). Falls back to `["Main"]`. |
| `Energy` | `Energy` | Energy level indicator, defaults to 1. |
| `Status` | `Status` | Song status flag, defaults to 3. **Override: `12` (ObjectiveLocked) → `3` (Available).** |
| `LocaleID` | `LocaleID` | Locale identifier, defaults to 4294967295 (all locales). |
| `MojoValue` | `MojoValue` | Mojo reward value, defaults to 0. |
| `CountInProgression` | `CountInProgression` | Hardcoded to 0. |

### JDNext mapPackage Sources

JDNext maps use Unity `MonoBehaviour` JSON (`map.json`) exported from the
mapPackage bundle. The normalizer extracts SongDesc via
`_songdesc_from_map_json_fallback()`:

| map.json Path | JD2021 PC Property | Note |
|---------------|-------------------|------|
| `SongDesc.MapName` | `MapName` (codename) | Primary codename source for JDNext maps. |
| `SongDesc.Title` | `Title` | Direct copy. |
| `SongDesc.Artist` | `Artist` | Direct copy. |
| `SongDesc.NumCoach` | `NumCoach` | Int coercion with fallback to 1. |
| `SongDesc.MainCoach` | `MainCoach` | Int coercion with fallback to -1. |
| `SongDesc.Difficulty` | `Difficulty` | Int coercion with fallback to 2. |
| `SongDesc.SweatDifficulty` | `SweatDifficulty` | Int coercion with fallback to 1. |
| `SongDesc.JDVersion` | `JDVersion` | Int coercion with fallback to 2021. |
| `SongDesc.OriginalJDVersion` | `OriginalJDVersion` | Int coercion with fallback to 2021. |

### JDNext Metadata Overlay

When a `jdnext_metadata.json` file exists in the source directory, the
normalizer overlays additional fields via `_apply_jdnext_metadata_songdesc_overrides()`:

| Metadata Key | Target Field | Condition for Override |
|-------------|-------------|----------------------|
| `tags` | `Tags` | Applied when current tags are empty or `["Main"]`. |
| `credits` | `Credits` | Applied when current credits are effectively missing. |
| `other_info.difficulty` | `Difficulty` | Applied when current difficulty is 0 or 2 (default). Supports text labels (`"easy"`, `"hard"`, etc.). |
| `other_info.sweat_difficulty` | `SweatDifficulty` | Applied when current value is 0 or 1 (default). |
| `other_info.original_jd_version` | `OriginalJDVersion` | Applied when current value is 0, -1, or 2021 (default). |
| `other_info.coach_count` | `NumCoach` | Applied when greater than current value. |

### JDNext Songdb Cache Overlay

An optional synthesized songdb cache (`songdb_synth.json`) provides a
secondary metadata source. Lookup is by codename, map_name, or title.
Applied fields include: `tags`, `credits`, `title`, `artist`, `difficulty`,
`sweat_difficulty`, `coach_count`, `original_jd_version`, `preview_entry`,
`preview_loop_start`, `preview_loop_end`, and `video_start_time`. All
overrides are conditional — they only apply when the current value appears
to be a placeholder or default.

### SongDesc Fallback Chain

The normalizer resolves SongDesc through this priority chain:

1. `songdesc.tpl.ckd` (JSON or binary)
2. JDNext `map.json` MonoBehaviour (via `_songdesc_from_map_json_fallback`)
3. HTML embed title/artist extraction (via `_songdesc_from_html_fallback`)
4. Codename-derived defaults

**After primary resolution**, overlays are applied in order:
JDNext metadata → JDNext songdb cache → HTML title/artist gap-fill.

---

## 2. Audio & Synchronization (MusicTrack)

### .TRK Logic

The timing markers in the original `musictrack.tpl.ckd` are verbatim audio sample positions at **48kHz**.

| Field | Meaning |
|---|---|
| `markers` | Array of sample positions for every beat. Marker 0 always = sample 0 of the WAV. |
| `videoStartTime` | Seconds before/after beat 0 where the video starts. Negative = pre-roll intro. |
| `startBeat` | Beat index of marker 0 (e.g. `-5` = marker 0 is beat -5, five beats before scoring begins). |
| `previewEntry` | Beat index where the song select preview begins. |
| `volume` | dB adjustment (usually 0). |

**DANGER**: Do not calculate `videoStartTime` synthetically when authoritative JDU metadata is available. Even a small error causes permanent audio/video desync.

**V2 IPK Limitation (Important):** In IPK-only workflows, source metadata is not always sufficient to reconstruct exact lead-in timing, so `videoStartTime` may remain approximate. Manual sync tuning in the installer is expected for many IPK maps.

### videoStartTime Auto-Detection

The normalizer detects whether `videoStartTime` is in ticks or seconds:

```python
if abs(vst) > 1000:
    vst /= 48000.0  # Convert from ticks to seconds
```

### videoStartTime Synthesis (Last-Resort)

When `videoStartTime` is `0.0` but `startBeat < 0`, the game engine will
assert "adding a brick in the past." The pipeline auto-synthesizes:

```python
vst = -(markers[abs(start_beat)] / 48.0 / 1000.0)
```

This only activates when no authoritative source value exists (e.g., Xbox 360
rips). JDNext and Fetch maps with valid offsets are preserved.

### The videoStartTime Coupling

`videoStartTime` controls two inseparable engine behaviors simultaneously:

1. The video player seeks to `videoStartTime` seconds at map start (negative = shows pre-roll frames before beat 0).
2. The WAV audio is delayed by exactly `abs(videoStartTime)` seconds from game start.

This means any map with `videoStartTime < 0` will have `abs(videoStartTime)` seconds of silence at the start, as the video plays its intro but the WAV hasn't started yet. See **[AUDIO_TIMING.md](../03_media/AUDIO_TIMING.md)** for the full explanation.

**Current V2 Runtime Status:** Intro AMB compensation is currently under temporary mitigation and should not be assumed to reliably fill this pre-roll silence in all cases.

### Preview Loop Monotonic Enforcement

The game writer enforces monotonic ordering on preview fields:

- If `previewLoopStart < previewEntry`, it is clamped to `previewEntry`.
- If `previewLoopEnd <= previewLoopStart`, it is set to `endBeat` or
  `previewLoopStart + 1.0` as a last resort.
- When all three preview fields are zero and `endBeat > 0`, a midpoint
  preview is synthesized: `entry = endBeat/2`, `start = endBeat/2`,
  `end = endBeat`.

### MusicTrack from JDNext mapPackage

JDNext `MusicTrack.json` (MonoBehaviour) stores the MusicTrackStructure
under `m_structure.MusicTrackStructure` with markers in `{VAL: N}` wrapper
format. The bundle strategy synthesizes a standard `_musictrack.tpl.ckd`
JSON via `_synthesize_musictrack_tpl_ckd()`, unwrapping the VAL containers
and preserving all timing fields.

### SFI (Sound Format Info)

JD2021 PC requires an explicit XML declaration of the sound format:
```xml
<root>
  <SoundConfiguration TargetName="PC" Format="PCM" IsStreamed="1" IsMusic="1"/>
  <SoundConfiguration TargetName="Durango" Format="PCM" IsStreamed="1" IsMusic="1"/>
  <SoundConfiguration TargetName="NX" Format="OPUS" IsStreamed="1" IsMusic="1"/>
  <SoundConfiguration TargetName="ORBIS" Format="ADPCM" IsStreamed="1" IsMusic="1"/>
</root>
```

**Dependency Note:** End-to-end audio processing in V2 depends on local FFmpeg/FFprobe availability, and some legacy decode paths depend on `vgmstream`.

---

## 3. Choreography Tapes (.dtape / .ktape)

### The Primitive Array Crash
The UbiArt engine (v280000) will crash if a tape contains raw primitive arrays. They MUST be wrapped in `VAL` tables.

- **JDU Source**: `"PartsScale": [1, 1, 1]`
- **JD2021 Target**: `PartsScale = { {VAL = 1.0}, {VAL = 1.0}, {VAL = 1.0} }`

### Tick Conversion
- **Resolution**: 24 ticks per beat.
- **Clip Timing**: `StartTime` and `Duration` values in JDU CKD data are already in engine tick units. The pipeline passes them through as-is with no conversion needed.

### MotionClip Processing (handled by `parsers/binary_ckd.py`)

| JDU Data | JD2021 Lua Output | Note |
|----------|-------------------|------|
| `Color: [a, r, g, b]` (floats 0-1) | `Color = "0xRRGGBBAA"` | Hex string with channels reordered. Default `"0xffffffff"` if missing. |
| `MotionPlatformSpecifics: {platform: data}` | `MotionPlatformSpecifics = { {KEY="X360", VAL={...}}, ... }` | Dict converted to KEY/VAL array. Inner data runs through `remove_class`. |
| `TrackId: N` (per clip) | `Tracks = { {TapeTrack = {id=N}}, ... }` | Unique TrackIds collected across all clips. `Tracks` array built and inserted at tape level. |

### JDNext Tape Synthesis

**JDNext maps do not ship `.dtape.ckd` / `.ktape.ckd` files.** Instead,
the bundle strategy synthesizes them from `map.json`:

- `DanceData.MotionClips[]` → `dtape.ckd` MotionClip entries
- `DanceData.PictoClips[]` → `dtape.ckd` PictogramClip entries
- `DanceData.GoldEffectClips[]` → `dtape.ckd` GoldEffectClip entries
- `KaraokeData.Clips[]` → `ktape.ckd` KaraokeClip entries

**JDNext MotionClip mapping:**

| JDNext map.json Field | Output CKD Field | Note |
|----------------------|------------------|------|
| `MoveName` | `ClassifierPath` | Normalized to `world/maps/<codename>/timeline/moves/<name>.<ext>`. Extension is `.gesture` for MoveType=1, `.msm` otherwise. |
| `Color` | `Color` | **Assumption: JDNext hex colors (e.g. `0xRRGGBBAA`) are normalized to `[a, r, g, b]` float arrays by `_normalize_color()`.** |
| `GoldMove` | `GoldMove` | Direct int copy. |
| `CoachId` | `CoachId` | Direct int copy. |
| `MoveType` | `MoveType` | Direct int copy. |

**JDNext PictoClip mapping:** `PictoPath` field is prefixed with
`world/maps/<codename>/timeline/pictos/` and suffixed with `.png`.

### Degenerate TrackId Normalization
When every clip in a tape has a unique TrackId (a sign of bad source data), the pipeline groups clips by `__class` and assigns a deterministic shared ID per class (using `hash(class_name) & 0xFFFFFFFF`). This prevents the engine from creating hundreds of individual tracks.

### Falsy Value Removal
After conversion, keys with default/empty values (`""`, `0`, `0.0`, `False`, `{}`, `[]`, `None`) are stripped from the output to produce cleaner, smaller Lua files.

---

## 3.5. Cinematic Tapes (.tape in `cinematics/`)

Cinematic tapes contain animation clips that require additional processing beyond standard choreography tapes:

### Curve Data
Clip types like `AlphaClip`, `RotationClip`, `TranslationClip`, `SizeClip`, `ScaleClip`, `ColorClip`, `MaterialGraphicDiffuseAlphaClip`, `MaterialGraphicDiffuseColorClip`, `MaterialGraphicUVTranslationClip`, and `ProportionClip` contain nested `Curve.Keys` arrays where each keyframe value is a `[x, y]` pair. These must be emitted as `vector2dNew(x, y)` in the Lua output for the engine to interpret them as 2D vectors.

### ActorIndices Resolution
Cinematic clips reference actors via `ActorIndices` (integer array). The tape's top-level `ActorPaths` array maps these indices to actual actor path strings. During conversion:
1. Each clip's `ActorIndices` is dereferenced against `ActorPaths`
2. Resolved paths are written as `ActorPaths = { {VAL = "path/to/actor"}, ... }` on the clip
3. The top-level `ActorPaths` array is removed from the tape

### Binary Cinematic Tape Clips

The binary parser (`parse_cinematic_tape`) recognizes two entry classes:

| Class ID | Clip Type | Key Fields |
|----------|-----------|------------|
| 136 | `SoundSetClip` | `sound_set_path` (SplitPath: path + filename, `jd2015` → `maps` rewrite) |
| 160 | `TapeReferenceClip` | `path` (SplitPath), `loop` flag |

### Fallback
If no cinematic `.tape.ckd` files exist in the extracted IPK, the pipeline keeps the empty fallback tape generated by `installers/game_writer.py`.

---

## 3.6. Ambient Sounds (`audio/amb/`)

### IPK-Sourced AMB

Maps that include ambient sound templates in their IPK (`audio/amb/*.tpl.ckd`) are processed into two files:

| Output File | Contents |
|------------|----------|
| `.ilu` (descriptor) | Lua sound list data + `appendTable(component.SoundComponent_Template.soundList, DESCRIPTOR)` call |
| `.tpl` (template) | Actor_Template wrapper with `includeReference` to the SoundComponent and the `.ilu` file |

These are placed in `Audio/AMB/` and injected as SoundComponent actors into the audio ISC. The WAV files they reference are initially created as silent placeholders, since JDU-hosted AMB audio is not directly downloadable.

For SoundSetClip AMBs with `StartTime <= 0` referenced in the mainsequence tape, the intended behavior is to overwrite these placeholders with real audio extracted from the OGG pre-roll. AMBs with `StartTime > 0` (mid-song background sounds) remain as silent placeholders.

**Current V2 Limitation (Prominent):** Intro AMB extraction/compensation is currently in a temporary mitigation state. In practical terms, pre-roll intro AMB coverage may be disabled, and silent placeholders may be retained.

### Synthetic Intro AMB (Pre-Roll Coverage)

This section describes the intended V2 design behavior, not guaranteed current runtime behavior.

Design intent: regardless of whether the map has AMB data in its IPK, the pipeline can generate a real-content intro AMB whenever `videoStartTime < 0`. This covers the silence caused by the engine's WAV scheduling delay. See **[AUDIO_TIMING.md](../03_media/AUDIO_TIMING.md)** for full technical details.

The intro AMB:
- Sources audio from the same OGG as the main track (making any overlap inaudible)
- Duration: marker-based (primary) or `abs(videoStartTime) + 1.355s` (fallback) — see AUDIO_TIMING.md Section 5
- 200ms linear fade-out at the end
- Automatically regenerated when audio timing is adjusted in the sync loop

**Current V2 Limitation (Authoritative):** The temporary mitigation can bypass this synthetic intro AMB path and keep silent intro behavior. Treat manual sync refinement as the reliable operator workflow until the mitigation is lifted.

---

## 4. Video & DASH Manifests

The engine uses DASH for video quality fallback. To work locally, the manifest must follow these strict technical rules:

| XML Element | Requirement | Note |
|-------------|-------------|------|
| **Namespace** | `urn:mpeg:DASH:schema:MPD:2011` | **MUST** be capitalized. |
| **Profile** | `urn:webm:dash:profile:webm-on-demand:2012` | Required for WebM support. |
| **BaseURL** | `jmcs://jd-contents/[MapName]/[MapName].webm` | Forces engine to local file fallback. |

### JDNext Video Quality Tiers

JDNext maps expose video variants with a different naming convention:

| URL Pattern | Quality Tier | Variant |
|-------------|-------------|---------|
| `video_ultra.hd.webm` | `ULTRA_HD` | `.hd` |
| `video_ultra.vp9.webm` | `ULTRA` | `.vp9` |
| `video_high.hd.webm` | `HIGH_HD` | `.hd` |
| `video_high.vp9.webm` | `HIGH` | `.vp9` |
| `video_mid.hd.webm` | `MID_HD` | `.hd` |
| `video_low.vp9.webm` | `LOW` | `.vp9` |

**VP9 handling:** The `vp9_handling_mode` config option controls behavior:
- `reencode_to_vp8` (default): Accept VP9 tier, re-encode later.
- `fallback_compatible_down`: Skip VP9 tiers entirely, pick next `_HD` tier.

---

## 5. Autodance & Recording

The engine looks for a specific actor chain to enable recaps:
- **Template**: `JD_AutodanceComponent_Template` — populated from `autodance/*.tpl.ckd` via `the CKD-to-Lua converter` during step 11. Contains the full `recording_structure`, `video_structure`, and `autodanceSoundPath`. An empty stub is created in step 6 as a placeholder and is only kept if no CKD data exists.
- **Actor Component**: `JD_AutodanceComponent`
- **Tapes**:
  - `.adtape`: Scoring/Title/Picto timings for the recap.
  - `.advideo`: Video encoding parameters for the exporter.
  - `.adrecording`: Raw controller movement data (6-axis/IMU).

The pipeline protects converted autodance data from being overwritten by the empty stub during sync refinement ("Apply & Finish").

---

## 6. Ignored / Discarded Data

The following JDU properties are safely ignored:
- **`assets`**: JDU menu icons are replaced by native PC MenuArt actors.
- **`urls`**: Only used for initial acquisition.
- **`skuConfigs`**: Licensing logic is bypassed.
- **`platformSpecifics`**: Metadata for older consoles (WiiU/PS3) is discarded.
- **`previewAudio`**: The separate preview audio file is not used. Instead, the engine plays the main audio file starting from the `previewEntry` beat marker, with `AudioPreviewFadeTime` controlling the fade-out (set to 2.0s when `previewEntry > 0`, otherwise 0.0).

### JDNext-Specific Ignores

- **UUID-like map IDs**: JDNext URLs use opaque UUID identifiers under
  `/jdnext/maps/<uuid>/`. These are explicitly rejected as codename
  candidates (matched against `_UUID_LIKE_RE`).
- **`audiopreview` / `videopreview` / `mappreview`**: Preview assets from
  JDNext CDN URLs are filtered out during URL classification.

---

## 7. Default Colors

JDU exposes a `DefaultColors` block in the `songdesc.tpl.ckd` payload containing song-specific theme colors. The automation extracts **all** color keys (`lyrics`, `theme`, `songcolor_1a`, `songcolor_1b`, `songcolor_2a`, `songcolor_2b`, and any extras) and injects them into the generated `SongDesc.tpl`.

### Key matching

CKD color keys may use different casing than the hardcoded fallbacks (e.g., CKD has `songcolor_1a` while fallback is `songColor_1A`). The pipeline performs **case-insensitive matching** to avoid duplicates. CKD values and key names take priority; fallback hex values are used only when a key is absent from the CKD entirely.

**CRITICAL:** The merge explicitly excludes any key named `"DefaultColors"` (case-insensitive) to prevent a recursive `DefaultColors` block inside the map, which would crash the engine's `zserializerobjectcontainers.h` deserialization.

### Conversion rules

- **Source format**: `[component, component, component, component]` where each component is a float in 0.0-1.0 range.
  - For `installers/game_writer.py` (`color_array_to_hex`): Components are taken in array order `[R,G,B,A]` and concatenated as `0xRRGGBBAA` hex. Missing alpha is padded with `0xFF`.
  - For `parsers/binary_ckd.py` (`argb_hex`, used in MotionClip Colors): Input is `[a, r, g, b]`, output is `0xRRGGBBAA` (channels reordered).
- **Hex strings**: If the CKD already contains a hex string (e.g., `0xFFB8113B`), it is passed through unchanged.

### Binary CKD Color Parsing

Binary SongDesc CKDs store `DefaultColors` as CRC-keyed entries:

| Color Name | CRC32 (uppercase ASCII) |
|-----------|------------------------|
| `theme` | `_string_id("theme")` |
| `lyrics` | `_string_id("lyrics")` |
| (others) | Stored as `0xNNNNNNNN` hex CRC keys in `extra` dict |

### Fallback values

| Key | Fallback Hex |
|-----|-------------|
| `lyrics` | `0xFF1B34AA` |
| `theme` | `0xFFFFFFFF` |
| `songColor_1A` | `0x00D1D0D0` |
| `songColor_1B` | `0xF50005D0` |
| `songColor_2A` | `0x00D1D0D0` |
| `songColor_2B` | `0xF50005D0` |

- Notes:
  - Some `.tpl.ckd` files are plaintext JSON despite the `.ckd` extension; the pipeline attempts to detect and parse JSON first before applying CKD-specific decoding.
  - Extra color keys from the CKD that are not in the fallback table are included in the output with no fallback (conversion only).

---

## 8. Texture Asset Mapping

### CKD Texture Decode Pipeline

The `texture_decoder.py` module decodes CKD textures from three platforms:

| Platform | Pipeline |
|----------|----------|
| PC | CKD → strip 44-byte header → DDS → Pillow → TGA/PNG |
| NX (Switch) | CKD → strip header → XTX → deswizzle → DDS → TGA/PNG |
| X360 | CKD → strip header → GPU descriptor parse → byte-swap → untile → DDS build → TGA/PNG |

### Batch Operations

| Function | Input | Output |
|----------|-------|--------|
| `decode_pictograms()` | `*picto*.ckd` in picto_dir | PNG files in output_dir; optionally composited on a transparent canvas (bottom-center placement, no upscale). **JDNext 512px-wide pictos are preserved as-is.** |
| `decode_menuart_textures()` | `*.ckd` in menuart_dir (excluding `.act.ckd`, `.tpl.ckd`, etc.) | TGA files in output_dir |

### JDNext Texture Sources

JDNext bundles provide textures as already-decoded PNG files from `Texture2D`
and `Sprite` Unity object types. These bypass the CKD decode pipeline and
are copied directly into `menuart/` or `pictos/` based on picto name matching
from the synthesized dtape data.

---

## 9. Codename Resolution

The normalizer resolves the authoritative `codename` through a multi-source
priority chain:

| Priority | Source | Method |
|----------|--------|--------|
| 1 | User-specified codename | Direct pass-through |
| 2 | JDNext `map.json` → `SongDesc.MapName` | `_infer_codename_from_source_files()` |
| 3 | JDU URL path segment | `extract_codename_from_urls()`: `/public/map/<codename>/...` |
| 4 | `songdesc.tpl.ckd` → `MapName` | Via CKD JSON parsing |
| 5 | `*_MAIN_SCENE.isc` filename stem | Stem before `_MAIN_SCENE` |
| 6 | Music track / `.trk` filename stem | Stem before `_musictrack` |
| 7 | IPK filename stem | Platform suffix stripped (e.g. `_x360`, `_nx`) |
| 8 | Timeline move prefix heuristic | Most-frequent `.gesture`/`.msm` prefix |
| 9 | HTML embed title | Extracted from Discord embed `<div class="embedTitle">` |

**JDNext UUID rejection:** URL segments matching the UUID pattern
(`^[0-9a-f]{8}-...$`) are explicitly excluded. The codename falls through
to `map.json` or user-specified sources.

---

*Reference: This mapping is derived from analysis of GetGetDown (reference map), BadRomance, Albatraoz JDU payloads, and JDNext mapPackage bundles.*
