# Manual JDNext Porting Guide (Just Dance 2021 PC)

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This guide provides an end-to-end walkthrough for porting **Just Dance Next (JDNext)** maps into the JD2021 PC engine (UbiArt). JDNext maps use **Unity asset bundles** instead of UbiArt IPK archives, requiring a fundamentally different extraction and synthesis workflow compared to JDU or IPK porting.

The automated V2 installer handles JDNext maps through **Fetch JDNext** and **HTML JDNext** modes. This guide is for manual extraction, debugging, parity checks, and understanding the internals.

---

## Table of Contents

1. [How JDNext Differs from JDU/IPK](#1-how-jdnext-differs-from-jduipk)
2. [Prerequisites](#2-prerequisites)
3. [Step 1: Obtain the Bundle File](#3-step-1-obtain-the-bundle-file)
4. [Step 2: Extract the Unity Bundle](#4-step-2-extract-the-unity-bundle)
5. [Step 3: Map Extracted Assets to JD2021 Format](#5-step-3-map-extracted-assets-to-jd2021-format)
6. [Step 4: Synthesize MusicTrack CKD](#6-step-4-synthesize-musictrack-ckd)
7. [Step 5: Synthesize Dance and Karaoke Tapes](#7-step-5-synthesize-dance-and-karaoke-tapes)
8. [Step 6: Resolve Song Metadata](#8-step-6-resolve-song-metadata)
9. [Step 7: Process Audio and Video](#9-step-7-process-audio-and-video)
10. [Step 8: Classify and Place Textures](#10-step-8-classify-and-place-textures)
11. [Step 9: Generate JD2021 Game Files](#11-step-9-generate-jd2021-game-files)
12. [Step 10: Register in SkuScene](#12-step-10-register-in-skuscene)
13. [Step 11: Validate and Readjust](#13-step-11-validate-and-readjust)
14. [Troubleshooting](#14-troubleshooting)
15. [Manual Checklist](#15-manual-checklist)
16. [Appendix A: Extraction Strategy Architecture](#appendix-a-extraction-strategy-architecture)
17. [Appendix B: JDNext Source Detection Heuristics](#appendix-b-jdnext-source-detection-heuristics)
18. [Appendix C: Song Database Cache System](#appendix-c-song-database-cache-system)

---

## 1. How JDNext Differs from JDU/IPK

| Aspect | JDU / IPK | JDNext |
|--------|-----------|--------|
| **Archive format** | UbiArt `.ipk` (zlib/lzma) | Unity `.bundle` (asset bundles) |
| **Data format** | CKD files (JSON or binary) | MonoBehaviour JSON, TextAsset, Texture2D |
| **Musictrack** | `*_musictrack.tpl.ckd` | `MusicTrack.json` (MonoBehaviour); must be synthesized into CKD |
| **Dance/Karaoke data** | Separate `.dtape` / `.ktape` files | Embedded inside `map.json` (`DanceData`, `KaraokeData`); must be synthesized |
| **Move files** | Platform-specific folders (PC/DURANGO/X360/WIIU) | `TextAsset/*.gesture` and `*.msm` (placed into `wiiu/`) |
| **Textures** | DDS/XTX inside CKD wrappers | `Texture2D/*.png` and `Sprite/*.png` (already decoded) |
| **Audio** | `.ogg` or `.wav.ckd` | `.ogg`, `.opus`, or AudioClip samples |
| **Video** | `.webm` with quality tiers | `.webm` with JDNext-specific naming (`video_ultra.hd.webm`) |
| **Metadata** | `songdesc.tpl.ckd` | `SongDesc` embedded in `map.json`, plus optional `jdnext_metadata.json` and songdb cache |
| **Extraction tools** | None (standard archive) | AssetStudioModCLI or UnityPy |

---

## 2. Prerequisites

### Required Tools

| Tool | Purpose | Install |
|------|---------|---------|
| **AssetStudioModCLI** | Primary Unity bundle extraction | Auto-downloaded by `setup.bat` to `tools/AssetStudioModCLI/`; or set `assetstudio_cli_path` in config |
| **UnityPy** (Python) | Fallback Unity bundle extraction | Installed via `requirements.txt` (pinned v1.25.0); automatic fallback when CLI is absent |
| **FFmpeg / FFprobe** | Audio conversion, probing | Configured via `setup.bat` |
| **vgmstream** | Console audio decode (optional) | Configured via `setup.bat` |

> **Note:** You need at least ONE of AssetStudioModCLI or UnityPy. The V2 pipeline tries both with fallback logic. For manual work, AssetStudioModCLI generally produces the cleanest output.

### Tool Resolution Order

The installer resolves AssetStudioModCLI from these locations, in order:

1. `assetstudio_cli_path` in `AppConfig` (explicit override)
2. `<third_party_tools_root>/AssetStudioModCLI/AssetStudioModCLI.exe` (default after `setup.bat`)
3. `<third_party_tools_root>/AssetStudio/AssetStudioModCLI.exe` (legacy fallback)

UnityPy resolution:
1. `import UnityPy` from Python environment (primary — pinned in `requirements.txt`)
2. `<third_party_tools_root>/UnityPy/` (added to `sys.path` as fallback)

### Required Files

- JDNext `.bundle` file for the target map
- Codename for the map (optional; can be inferred from bundle contents)

---

## 3. Step 1: Obtain the Bundle File

JDNext map data is distributed as Unity asset bundle files. These contain all gameplay assets (moves, timings, textures) but typically **not** audio and video — those are fetched separately.

In automated V2 workflows:
- **Fetch JDNext** mode downloads the bundle from a Discord bot channel using Playwright automation.
- **HTML JDNext** mode parses asset URLs from saved HTML exports.

For manual work, obtain the `.bundle` file from your source and ensure it's a single file (not a directory).

---

## 4. Step 2: Extract the Unity Bundle

### Option A: AssetStudioModCLI (Recommended)

```bash
AssetStudioModCLI.exe <bundle_path> -m export -o <output_dir> -g type --unity-version 2021.3.9f1
```

This produces a type-grouped output structure:

```
output_dir/
├── TextAsset/          # .gesture, .msm, miscellaneous text files
├── MonoBehaviour/      # map JSON, musictrack JSON
├── Texture2D/          # Decoded PNG textures
└── Sprite/             # Decoded PNG sprites
```

### Option B: UnityPy (Python)

```python
from jd2021_installer.extractors.jdnext_unitypy import unpack_jdnext_bundle_with_unitypy

summary = unpack_jdnext_bundle_with_unitypy(
    bundle_path="path/to/map.bundle",
    output_dir="path/to/output",
)
print(f"Exported {summary.exported_objects}/{summary.total_objects} objects")
```

This produces a different structure:

```
output_dir/
├── textures/           # Decoded PNG textures
├── audio/              # AudioClip samples (OGG/WAV/BIN)
├── video/              # VideoClip raw data
├── text/               # TextAsset files (.txt)
└── typetree/           # MonoBehaviour and unknown objects as JSON
```

### Option C: V2 Dual-Strategy Pipeline

The `run_jdnext_bundle_strategy()` function automates the fallback:

```python
from jd2021_installer.extractors.jdnext_bundle_strategy import run_jdnext_bundle_strategy

summary = run_jdnext_bundle_strategy(
    bundle_path="path/to/map.bundle",
    output_dir="path/to/output",
    strategy="assetstudio_first",       # or "unitypy_first"
    codename="MapCodename",             # optional
    unity_version="2021.3.9f1",         # default
)
print(f"Winner: {summary.winner}")       # "assetstudio" or "unitypy"
```

### Encrypted Bundles

Some JDNext bundles are encrypted. The UnityPy path detects this and reports `key_sig` and `data_sig` from the error:

```
JDNext bundle appears encrypted and UnityPy has no decrypt key.
key_sig=<signature> data_sig=<signature>
```

**There is currently no automated decrypt path.** Encrypted bundles must be decrypted externally before extraction.

---

## 5. Step 3: Map Extracted Assets to JD2021 Format

After raw extraction, the assets must be reorganized from Unity's type-grouped layout into the format the JD2021 normalizer expects. The V2 pipeline does this via `map_assetstudio_output()`.

### Asset Mapping Table

| Unity Source | Mapped Destination | Processing |
|--------------|--------------------|------------|
| `MonoBehaviour/<codename>.json` | `monobehaviour/map.json` | Copied; if not found, first non-musictrack JSON is used |
| `MonoBehaviour/MusicTrack.json` | `monobehaviour/musictrack.json` | Copied; also synthesized into `<codename>_musictrack.tpl.ckd` |
| `TextAsset/*.gesture` | `timeline/moves/wiiu/<name>.gesture` | Lowercased filenames |
| `TextAsset/*.msm` | `timeline/moves/wiiu/<name>.msm` | Lowercased filenames |
| `TextAsset/*.txt` | `textasset/<name>.txt` | Copied as-is |
| `Texture2D/*.png` + `Sprite/*.png` | `pictos/` or `menuart/` | Classified by picto name matching (see Step 8) |

### Manual Mapping

If working by hand, create the following directory structure from your extracted output:

```
mapped/
├── monobehaviour/
│   ├── map.json                        # From MonoBehaviour/<codename>.json
│   └── musictrack.json                 # From MonoBehaviour/MusicTrack.json
├── <codename>_musictrack.tpl.ckd       # Synthesized from musictrack.json (see Step 4)
├── <codename>_tml_dance.dtape.ckd      # Synthesized from map.json DanceData (see Step 5)
├── <codename>_tml_karaoke.ktape.ckd    # Synthesized from map.json KaraokeData (see Step 5)
├── timeline/
│   └── moves/
│       └── wiiu/                        # All .gesture and .msm files (lowercased)
├── pictos/                              # PNG files classified as pictograms
├── menuart/                             # PNG files classified as menu art
└── mapping_summary.json                 # Optional diagnostic output
```

---

## 6. Step 4: Synthesize MusicTrack CKD

JDNext stores musictrack data as a Unity MonoBehaviour JSON (`MusicTrack.json`), not as a UbiArt CKD. You must synthesize the CKD format that the normalizer expects.

### Input Format: Unity MusicTrack JSON

```json
{
  "m_structure": {
    "MusicTrackStructure": {
      "markers": [{"VAL": 0}, {"VAL": 23040}, ...],
      "signatures": [{"MusicSignature": {"beats": 4, "marker": 0}}],
      "sections": [{"MusicSection": {"sectionType": 0, "marker": 0}}],
      "startBeat": -5,
      "endBeat": 333,
      "videoStartTime": -2.145,
      "previewEntry": 84.0,
      "previewLoopStart": 84.0,
      "previewLoopEnd": 172.0,
      "volume": 0.0,
      "fadeInDuration": 0.0,
      "fadeInType": 0,
      "fadeOutDuration": 0.0,
      "fadeOutType": 0
    }
  }
}
```

### Output Format: Synthesized CKD

```json
{
  "COMPONENTS": [{
    "trackData": {
      "structure": {
        "markers": [0, 23040, ...],
        "signatures": [{"beats": 4, "marker": 0}],
        "sections": [{"sectionType": 0, "marker": 0}],
        "startBeat": -5,
        "endBeat": 333,
        "videoStartTime": -2.145,
        "previewEntry": 84.0,
        "previewLoopStart": 84.0,
        "previewLoopEnd": 172.0,
        "volume": 0.0,
        "fadeInDuration": 0.0,
        "fadeInType": 0,
        "fadeOutDuration": 0.0,
        "fadeOutType": 0
      }
    }
  }]
}
```

### Key Processing Rules

1. **Markers**: Extract integer values from `{VAL: n}` or `{val: n}` wrappers.
2. **Signatures**: Extract `{beats, marker}` pairs from `MusicSignature` wrappers.
3. **Sections**: Extract `{sectionType, marker}` pairs from `MusicSection` wrappers.
4. **Numeric defaults**: All fields default to `0` or `0.0` if missing or null.
5. **videoStartTime**: If `0.0` but `startBeat < 0`, the normalizer will synthesize it from markers downstream (same as IPK maps).

---

## 7. Step 5: Synthesize Dance and Karaoke Tapes

JDNext embeds dance choreography and karaoke lyrics inside the map JSON (`map.json`) rather than shipping separate `.dtape` and `.ktape` files. You must synthesize standard CKD tapes from this data.

### Dance Tape Synthesis

The `DanceData` object in `map.json` contains three clip arrays:

#### MotionClips → `MotionClip`

| Unity Field | CKD Field | Processing |
|-------------|-----------|------------|
| `StartTime` | `StartTime` | Integer (ticks) |
| `Duration` | `Duration` | Integer (ticks) |
| `Id` | `Id` | Integer |
| `TrackId` | `TrackId` | Integer |
| `IsActive` | `IsActive` | Integer (0 or 1) |
| `MoveName` | `ClassifierPath` | Normalized (see below) |
| `GoldMove` | `GoldMove` | Integer (0 or 1) |
| `CoachId` | `CoachId` | Integer |
| `MoveType` | `MoveType` | Integer: 0 = msm, 1 = gesture |
| `Color` | `Color` | Color normalization (see below) |

**Move name normalization** (`_normalize_move_name`):
1. Replace backslashes with forward slashes.
2. If the name contains `/`, take only the last segment (strip path components).
3. Remove `.gesture` or `.msm` extension if present.
4. Re-apply extension based on `MoveType`: 1 → `.gesture`, 0 → `.msm`.
5. Build full classifier path: `world/maps/<codename>/timeline/moves/<name>.<ext>`

**Color normalization** (`_normalize_color`):
- If the color is a hex string like `"0x0e8cd3ff"`:
  - Parse as `[r, g, b, a]` byte values from positions `[2:4]`, `[4:6]`, `[6:8]`, `[8:10]`.
  - Convert to float array `[a/255, r/255, g/255, b/255]`.
- If parsing fails, use the default yellow: `[1.0, 0.968, 0.164, 0.552]`.

#### PictoClips → `PictogramClip`

| Unity Field | CKD Field | Processing |
|-------------|-----------|------------|
| `PictoPath` | `PictoPath` | Generates: `world/maps/<codename>/timeline/pictos/<name>.png` |
| `CoachCount` | `CoachCount` | Integer |

Picto names from these clips are collected and used later for texture classification (Step 8).

#### GoldEffectClips → `GoldEffectClip`

| Unity Field | CKD Field | Processing |
|-------------|-----------|------------|
| `EffectType` | `EffectType` | Integer (default: 1) |

### Karaoke Tape Synthesis

The `KaraokeData` object contains a `Clips` array. Each entry may have a `KaraokeClip` wrapper (unwrapped automatically):

| Unity Field | CKD Field | Processing |
|-------------|-----------|------------|
| `Lyrics` | `Lyrics` | String |
| `Pitch` | `Pitch` | Float |
| `IsEndOfLine` | `IsEndOfLine` | Integer (0 or 1) |
| `ContentType` | `ContentType` | Integer |
| `SemitoneTolerance` | `SemitoneTolerance` | Float (default: 5.0) |
| `StartTimeTolerance` | `StartTimeTolerance` | Integer (default: 4) |
| `EndTimeTolerance` | `EndTimeTolerance` | Integer (default: 4) |

### Output CKD Structure

Both tapes are written as JSON with this schema:

```json
{
  "__class": "Tape",
  "Clips": [...],
  "TapeClock": 0,
  "TapeBarCount": 1,
  "FreeResourcesAfterPlay": 0,
  "MapName": "<codename>",
  "SoundwichEvent": ""
}
```

Output files:
- `<codename>_tml_dance.dtape.ckd`
- `<codename>_tml_karaoke.ktape.ckd`

---

## 8. Step 6: Resolve Song Metadata

JDNext maps get song metadata (title, artist, difficulty, etc.) from multiple cascading sources:

### Metadata Resolution Priority

```
1. songdesc.tpl.ckd              (if present — rare for JDNext)
   ↓ (fallback)
2. map.json → SongDesc            (embedded in MonoBehaviour map JSON)
   ↓ (fallback)
3. jdnext_metadata.json           (from Fetch mode download metadata)
   ↓ (overlay)
4. jdnext_songdb_synth.json       (optional local songdb cache)
   ↓ (overlay)
5. assets.html scraping            (title/artist from embed HTML)
```

### map.json SongDesc Fields

When `songdesc.tpl.ckd` is missing (the common case), the normalizer extracts these from `map.json`:

| Field | Source Key | Default |
|-------|-----------|---------|
| `map_name` | `SongDesc.MapName` or `MapName` | codename |
| `title` | `SongDesc.Title` | codename |
| `artist` | `SongDesc.Artist` | `"Unknown Artist"` |
| `dancer_name` | `SongDesc.DancerName` | `"Unknown Dancer"` |
| `credits` | `SongDesc.Credits` | `""` |
| `num_coach` | `SongDesc.NumCoach` | `1` |
| `difficulty` | `SongDesc.Difficulty` | `2` |
| `sweat_difficulty` | `SongDesc.SweatDifficulty` | `1` |
| `jd_version` | `SongDesc.JDVersion` | `2021` |
| `original_jd_version` | `SongDesc.OriginalJDVersion` | `2021` |

### jdnext_metadata.json Overrides

If a `jdnext_metadata.json` file exists in the source (deposited by the Fetch mode), these fields are overlaid:

| Override | Condition |
|----------|-----------|
| `tags` | Only if SongDesc has empty/default tags |
| `credits` | Only if SongDesc credits are effectively missing |
| `difficulty` | Only if current difficulty is 0 or 2 (default) |
| `sweat_difficulty` | Only if current is 0 or 1 (default) |
| `original_jd_version` | Only if current is 0, -1, or 2021 |
| `coach_count` | Only if metadata value is larger than current |

Difficulty values support word-to-int mapping: `easy=1`, `medium/normal=2`, `hard=3`, `extreme=4`.

### Song Database Cache Overrides

The optional `jdnext_songdb_synth.json` cache provides a broader metadata overlay. It is generated by importing a JDNext song database JSON via `synthesize_jdnext_songdb()`. Lookup is by normalized codename → mapName → title key.

Fields overlaid from songdb cache: `tags`, `credits`, `title`, `artist`, `difficulty`, `sweat_difficulty`, `coach_count`, `original_jd_version`, `preview_entry`, `preview_loop_start`, `preview_loop_end`, `video_start_time`.

---

## 9. Step 7: Process Audio and Video

### Audio

JDNext maps may provide audio in several formats:

| Format | Priority | Notes |
|--------|----------|-------|
| `.ogg` | 1st | Standard compressed audio |
| `.opus` | 2nd | JDNext-native format; sometimes named `audio.opus` |
| `.wav` | 3rd | Uncompressed PCM |
| `.wav.ckd` | 4th | CKD-wrapped WAV |

**Exclusion rules** (same as JDU/IPK):
- Files in `/amb/`, `/autodance/` directories
- Files starting with `amb_` or `ad_`
- Files containing `audiopreview` or `mappreview` in their name

**Conversion**:
```bash
ffmpeg -i input.ogg -ar 48000 output.wav
# or for .opus:
ffmpeg -i audio.opus -ar 48000 output.wav
```

### Video

JDNext video files use a different naming convention than JDU:

| JDNext Pattern | Example |
|----------------|---------|
| `video_<quality>.<codec>.webm` | `video_ultra.hd.webm`, `video_high.vp8.webm` |

The V2 heuristic detects JDNext-origin video filenames with this pattern:
```
^video_(ultra|high|mid|low)\.(hd|vp8|vp9)\.webm$
```

Quality selection follows the same descending tier priority as JDU:
`ULTRA_HD → ULTRA → HIGH_HD → HIGH → MID_HD → MID → LOW_HD → LOW`

---

## 10. Step 8: Classify and Place Textures

JDNext bundles export all textures as decoded PNG files (no CKD wrappers). They must be classified into **pictos** (timeline pictograms) vs. **menuart** (covers, backgrounds, coach art).

### Classification Logic

The V2 pipeline classifies textures using picto names collected during tape synthesis (Step 5):

1. Collect all `PictoPath` values from `PictoClips` in the dance tape → lowercase set of picto names.
2. For each PNG from `Texture2D/` and `Sprite/`:
   - If the lowercase stem matches a picto name OR contains `"picto"` → classify as **picto** → place in `pictos/`.
   - Otherwise → classify as **menuart** → place in `menuart/`.

### Manual Classification

If working by hand, classify textures by filename patterns:

| Pattern | Classification | Destination |
|---------|---------------|-------------|
| `picto_*`, `*picto*`, known picto names | Pictogram | `pictos/` |
| `cover_*`, `banner_*`, `coach_*`, `map_bkg_*` | Menu art | `menuart/` |
| Everything else | Menu art (default) | `menuart/` |

---

## 11. Step 9: Generate JD2021 Game Files

From this point, the workflow is **identical to JDU/IPK porting**. The synthesized CKD files feed into the standard normalizer pipeline.

Create the standard map structure under `World/MAPS/<MapName>/` and generate:

- Main scene ISC (`ENGINE_VERSION="280000"`)
- `SongDesc.tpl` and `SongDesc.act`
- Audio chain (`.trk`, musictrack tpl, sequence tpl, `.stape`, audio ISC, config sfi)
- Timeline dance/karaoke assets (converted from synthesized CKD tapes to Lua)
- Cinematics chain
- VideosCoach files
- MenuArt actors/textures
- Autodance assets

### JDNext-Specific Notes

1. **Gesture files are placed under `wiiu/`**: The V2 pipeline copies all JDNext `.gesture` and `.msm` files to `timeline/moves/wiiu/` with lowercased filenames. The installer then copies these to the appropriate platform directories during game file generation.
2. **CKD stem aliasing**: Some JDNext maps have a different internal codename (e.g., the bundle contains `pigstep_musictrack.tpl.ckd` but the map codename is `Jukebox`). The normalizer detects this via `_infer_ckd_stem_alias()` and handles the mismatch.
3. **videoStartTime units**: JDNext musictracks sometimes express `videoStartTime` in ticks (values > 1000) instead of seconds. The normalizer auto-detects this and divides by 48000.

---

## 12. Step 10: Register in SkuScene

Identical to the JDU guide:

1. Add actor entry in `SkuScene_Maps_PC_All.isc` with the map codename.
2. Ensure registration is idempotent (no duplicate entries).
3. Confirm map title and covers resolve in song select.

---

## 13. Step 11: Validate and Readjust

After first launch:

1. Confirm map loads and appears in song list.
2. Check coach select, timeline cues, and video playback.
3. Test audio/video start alignment on first beats.
4. Apply sync refinement iteratively until visually correct.

### JDNext Timing Notes

- JDNext maps typically have cleaner metadata than legacy IPK maps, so video sync is often closer on first install.
- The songdb cache can supply `videoStartTime` when the extracted musictrack lacks it — check `jdnext_songdb_synth.json` if timing is zero.
- The normalizer applies JDNext-specific sync logging (`JDNext sync (metadata-preserved)`, `JDNext sync (synthesized video)`, `JDNext sync (fallback)`) which can be found in installer logs at `detailed` or `developer` log level.

---

## 14. Troubleshooting

| Issue | Likely Cause | Fix |
|-------|-------------|-----|
| **AssetStudioModCLI not found** | Tool not installed or path misconfigured | Re-run `setup.bat` (downloads to `tools/AssetStudioModCLI/`) or set `assetstudio_cli_path` in config |
| **UnityPy import error** | Missing Python package | Run `pip install -r requirements.txt` to install UnityPy v1.25.0 |
| **Both extraction tools fail** | Corrupted or encrypted bundle | Check for encryption errors; decrypt externally first |
| **Encrypted bundle detected** | JDNext DRM on bundle | Currently no automated decrypt; `key_sig` and `data_sig` are reported for manual triage |
| **`map.json` not found after extraction** | AssetStudio exported with unexpected codename | Check `MonoBehaviour/` for any `.json` files; the first non-musictrack JSON is used as fallback |
| **Missing musictrack** | `MusicTrack.json` not in MonoBehaviour output | Verify bundle contains the musictrack asset; check `typetree/` in UnityPy output |
| **Empty or missing pictos** | Picto names in DanceData don't match texture names | Manually inspect `PictoClips` → `PictoPath` values and cross-reference with extracted textures |
| **Codename mismatch / CKD stem aliasing** | Internal map name differs from expected codename | Normalizer handles this via `_infer_ckd_stem_alias()`; for manual work, check the `MapName` field in `map.json` |
| **videoStartTime is 0 but map has pre-roll** | JDNext musictrack doesn't always populate this | Check songdb cache for override; synthesize from markers if `startBeat < 0` |
| **Audio is `.opus` format** | JDNext-native audio format | FFmpeg handles `.opus` → 48kHz WAV conversion natively |
| **Missing title/artist metadata** | SongDesc fallback chain didn't find values | Import a JDNext songdb JSON via Settings to populate the cache, then reinstall |
| **Gesture/MSM files not loading** | Filenames not lowercased | Ensure all move files in `wiiu/` have lowercase filenames |
| **Progressive desync** | Wrong WAV sample rate | Rebuild WAV at exactly 48kHz (`ffmpeg -ar 48000`) |

---

## 15. Manual Checklist

Use this before finalizing a JDNext port:

1. [ ] Bundle file extracted successfully (AssetStudioModCLI or UnityPy).
2. [ ] `map.json` identified and contains `DanceData`, `KaraokeData`, and `SongDesc`.
3. [ ] `musictrack.json` identified and synthesized into `*_musictrack.tpl.ckd`.
4. [ ] Markers, signatures, and sections extracted from musictrack JSON.
5. [ ] Dance tape synthesized with MotionClips, PictoClips, and GoldEffectClips.
6. [ ] Karaoke tape synthesized with KaraokeClips.
7. [ ] Move name normalization applied (path-stripped, extension-corrected, lowercased).
8. [ ] Color normalization applied to MotionClip colors.
9. [ ] Audio converted to 48kHz WAV (from `.ogg`, `.opus`, or AudioClip samples).
10. [ ] Video file placed and quality tier selected.
11. [ ] Textures classified into pictos vs. menuart using PictoClip names.
12. [ ] Song metadata resolved (map.json → metadata.json → songdb cache → HTML).
13. [ ] Full map folder generated under `World/MAPS/<MapName>/`.
14. [ ] SkuScene registration added (no duplicates).
15. [ ] In-game validation and readjust completed.

---

## Appendix A: Extraction Strategy Architecture

The V2 pipeline uses a dual-strategy approach defined in `jdnext_bundle_strategy.py`:

```
┌─────────────────────┐
│ run_jdnext_bundle_  │
│ strategy()          │
└─────────┬───────────┘
          │
          ├── strategy = "assetstudio_first" (default)
          │   ├── Try: AssetStudioModCLI → assetstudio_raw/
          │   │   └── Success → map_assetstudio_output() → mapped/
          │   └── Fallback: UnityPy → unitypy_raw/
          │
          └── strategy = "unitypy_first"
              ├── Try: UnityPy → unitypy_raw/
              └── Fallback: AssetStudioModCLI → assetstudio_raw/
                  └── Success → map_assetstudio_output() → mapped/
```

Output directory structure:
```
output_dir/
├── assetstudio_raw/           # Raw AssetStudio export (type-grouped)
├── unitypy_raw/               # Raw UnityPy export
├── mapped/                    # Reorganized for JD2021 normalizer
│   ├── monobehaviour/
│   ├── timeline/moves/wiiu/
│   ├── pictos/
│   ├── menuart/
│   └── mapping_summary.json
└── strategy_summary.json      # Diagnostic: strategy used, winner, counts
```

---

## Appendix B: JDNext Source Detection Heuristics

The normalizer automatically detects JDNext-origin sources via `_is_jdnext_source()`:

| Heuristic | Check |
|-----------|-------|
| Explicit metadata | `jdnext_metadata.json` exists in source directory |
| Map JSON | `monobehaviour/map.json` exists in source directory |
| HTML content | `assets.html` contains `/jdnext/maps/` or `server:jdnext` |
| Video naming | Video filename matches `^video_(ultra\|high\|mid\|low)\.(hd\|vp8\|vp9)\.webm$` |
| Audio naming | Audio filename is exactly `audio.opus` |

When a JDNext source is detected, the normalizer applies:
- Song database cache overrides (`_apply_jdnext_songdb_cache_overrides`)
- JDNext-specific sync logging
- JDNext metadata overlay from `jdnext_metadata.json`

---

## Appendix C: Song Database Cache System

The JDNext song database cache (`jdnext_songdb_synth.json`) is an optional local lookup file that enriches map metadata during installation. It is especially useful when `map.json` SongDesc data is incomplete.

### Importing a Song Database

1. Obtain a raw JDNext song database JSON (keyed by map UUID).
2. In the installer, go to **Settings → Import JDNext Song DB**.
3. The installer synthesizes a compact lookup cache (`jdnext_songdb_synth.json`).

### Cache Structure

```json
{
  "schema_version": 1,
  "source_kind": "jdnext_songdb",
  "generated_at": "2026-04-12T22:00:00",
  "source_entries": 500,
  "usable_entries": 480,
  "index": {
    "<normalized_key>": {
      "entry_id": "<uuid>",
      "map_name": "MapCodename",
      "parent_map_name": "ParentCodename",
      "title": "Song Title",
      "artist": "Artist Name",
      "credits": "Credits Text",
      "tags": ["Main", "SoloMode"],
      "difficulty": 3,
      "sweat_difficulty": 2,
      "coach_count": 1,
      "original_jd_version": 2024,
      "lyrics_color": "#FFFFFF",
      "preview_entry": 84.0,
      "preview_loop_start": 84.0,
      "preview_loop_end": 172.0,
      "video_start_time": -2.145
    }
  }
}
```

### Lookup Resolution

The cache indexes entries by three keys (all normalized to lowercase alphanumeric):
1. `mapName` (primary)
2. `parentMapName` (for alternate/variant maps)
3. `title` (for title-based search)

When multiple entries collide on the same key, the entry with the higher metadata completeness score wins (more non-null fields = higher score). Title collisions additionally prefer entries whose `mapName` matches the lookup key.

### Preview Timing Derivation

The songdb cache extracts preview timing from `assetsMetadata.audioPreviewTrk`:
- `PreviewEntry`, `PreviewLoopStart`, `PreviewLoopEnd` are beat indices.
- `PreviewDuration` is in seconds; if `PreviewLoopEnd` is missing, it's derived by converting duration back to a beat index using marker tick positions.
- `VideoStartTime` is preserved as-is.
