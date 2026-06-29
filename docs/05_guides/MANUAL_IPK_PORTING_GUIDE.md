# Manual IPK Porting Guide (Just Dance 2021 PC)

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This guide is a practical, IPK-specific manual workflow for porting maps into JD2021 PC (UbiArt). It covers both the **IPK Archive mode** (automated extraction from `.ipk` files) and the **Manual mode with IPK source type** (pointing at an already-unpacked IPK tree). Use it for debugging, recovery, parity checks, and advanced manual installs when automation is not enough.

## Current V2 Behavior Notice (Read First)

1. **IPK video timing is often approximate.**
   - Many IPK musictracks provide incomplete lead-in metadata.
   - Manual post-install video offset tuning is often required.
2. **Dependencies are required for full-fidelity output.**
   - FFmpeg and FFprobe for conversion/probing.
   - vgmstream for some console audio decode paths.

---

## Table of Contents

1. [What Makes IPK Porting Different](#1-what-makes-ipk-porting-different)
2. [Prerequisites](#2-prerequisites)
3. [Step 1: Extract the IPK Safely](#3-step-1-extract-the-ipk-safely)
4. [Step 2: Identify Codename and Core Assets](#4-step-2-identify-codename-and-core-assets)
5. [Step 3: Parse MusicTrack and Build Timing](#5-step-3-parse-musictrack-and-build-timing)
6. [Step 4: Convert Audio and Video Assets](#6-step-4-convert-audio-and-video-assets)
7. [Step 5: Convert Textures, Pictos, and Tapes](#7-step-5-convert-textures-pictos-and-tapes)
8. [Step 6: Generate JD2021 Game Files](#8-step-6-generate-jd2021-game-files)
9. [Step 7: Register in SkuScene](#9-step-7-register-in-skuscene)
10. [Step 8: Validate and Readjust](#10-step-8-validate-and-readjust)
11. [Troubleshooting](#11-troubleshooting)
12. [Manual Checklist](#12-manual-checklist)
13. [Appendix A: How V2 IPK Extraction Works](#13-appendix-a-how-v2-ipk-extraction-works)
14. [Appendix B: Manual Mode Source Types](#14-appendix-b-manual-mode-source-types)

---

## 1. What Makes IPK Porting Different

Compared with Fetch or HTML workflows, IPK installs are more likely to require recovery logic:

- Binary CKD parsing is common (musictrack, songdesc, tapes).
- `videoStartTime` may be missing or zero in legacy console data.
- Textures/pictos can be split between normal map folders and cooked cache paths.
- Audio may arrive as `.wav.ckd` and need decode fallback.
- IPK archives may contain **multiple maps** in a single bundle (the V2 `ArchiveIPKExtractor` discovers all of them and lets you pick one).
- Both `world/maps/<codename>/` (standard) and `world/jd20XX/<codename>/` (legacy) layouts must be handled.

Result: an IPK map may install successfully but still need manual sync refinement.

---

## 2. Prerequisites

Required tools:

- FFmpeg
- FFprobe
- vgmstream (recommended for broader audio compatibility)

All three can be configured automatically via `setup.bat` in the repository root.

Required output target:

- JD2021 game directory with write access (typically `jd21/data/World/MAPS/`).

Recommended workspace prep:

1. Keep one working folder per map (`temp/<codename>/`).
2. Keep original extracted IPK files unchanged.
3. Record every offset you test during sync tuning.

---

## 3. Step 1: Extract the IPK Safely

### Automated (IPK Archive mode)

The V2 `ArchiveIPKExtractor` (`archive_ipk.py`) handles this end-to-end:

1. **Validates IPK magic bytes** (`\x50\xEC\x12\xBA`) before any decompression.
2. **Reads the big-endian header** to determine file count and base offset.
3. **Decompresses each entry** using a zlib-first, lzma-fallback chain; raw data is kept if neither succeeds.
4. **Rejects unsafe paths** — any entry with absolute paths, parent traversal (`..`), or output escaping the extraction root is silently skipped.
5. **Preserves archive folder structure** under the output directory.
6. **Discovers codenames** by scanning both extracted filesystem structure _and_ archive headers (combining both for robustness).

After extraction, the extractor populates `bundle_maps` with all discovered map codenames and auto-selects the best match (by requested codename → IPK filename stem → first candidate).

### Manual extraction

If extracting by hand, follow the same safety expectations as V2:

1. Validate IPK magic/header before processing.
2. Decompress entries (zlib first, then lzma fallback where needed).
3. Reject unsafe paths:
   - absolute paths,
   - parent traversal (`..`),
   - any output that escapes the chosen extraction root.
4. Preserve folder structure from the archive.

Expected result: a local extracted tree containing map data, audio/video assets, and CKD files.

---

## 4. Step 2: Identify Codename and Core Assets

Find codename first, then scope every search to that codename.

### V2 codename discovery

The V2 extractor discovers codenames from two sources:

| Source | Path pattern | Priority |
|--------|-------------|----------|
| Standard layout | `world/maps/<codename>/` | Primary |
| Legacy layout | `world/jd20XX/<codename>/` (e.g. `world/jd2018/badromance/`) | Secondary |

Internal folders (`cache`, `common`, `enginedata`, `audio`, `videoscoach`, `localization`) are automatically excluded from candidates.

### Manual codename inference

When using **Manual mode** with `source_type = ipk`, the `ManualExtractor` performs the same discovery via `_validate_ipk_root()`:

1. Scans for `world/maps/` and `world/jd20XX/` directories under the root.
2. If a single map is found, auto-selects it.
3. If multiple maps exist, logs a warning and selects the first alphabetically (or the one matching the user-specified codename).

### Core assets to collect

- musictrack CKD/TPL (search pattern: `*musictrack*.tpl.ckd`, `*musictrack*.trk`, `*.trk`)
- songdesc CKD/TPL
- dance/karaoke tape sources (`dtape`, `ktape`, or tape CKD variants)
- main video (`.webm`)
- primary audio (`.ogg`, `.wav`, or `.wav.ckd` — excluding `audiopreview`, `amb_*`, and `autodance` files)
- menuart covers/coaches and timeline pictos
- moves assets for platform folders

If multiple maps are bundled, do not mix assets across codenames.

---

## 5. Step 3: Parse MusicTrack and Build Timing

This is the most important IPK step.

Extract or derive:

- `markers` (sample positions at 48kHz)
- `signatures` (time signature changes: `beats` + `marker` position)
- `sections` (section boundaries: `sectionType` + `marker` position)
- `startBeat` and `endBeat`
- `videoStartTime`
- preview fields (`previewEntry`, `previewLoopStart`, `previewLoopEnd`)
- volume/fade parameters (`volume`, `fadeInDuration`, `fadeInType`, `fadeOutDuration`, `fadeOutType`)

Timing rules for IPK manual work:

1. If `videoStartTime` is valid and non-zero, use it.
2. If it is zero/missing and `startBeat` is negative, derive pre-roll from markers as an approximation.
3. Keep a note that derived IPK video offsets are often close, not final.

Important: do not invent synthetic timing that ignores source marker structure.

---

## 6. Step 4: Convert Audio and Video Assets

### Audio

1. Prefer source OGG when available.
2. If only `.wav.ckd` exists, decode it first.
3. Produce map WAV at 48kHz PCM.
4. Preserve the original OGG for selection/preview usage when possible.
5. Exclude files matching `amb_*`, `audiopreview*`, or those inside `autodance/` directories.

Example conversion:

```bash
ffmpeg -i input.ogg -ar 48000 output.wav
```

If you need trim-based alignment, use the offset derived from your timing step and keep logs of exact values tested.

### Video

1. Keep source `.webm` as primary gameplay video.
2. Exclude `mappreview` and `videopreview` files (these are not gameplay video).
3. Ensure MPD/manifest references remain valid if present.
4. The V2 installer selects video by quality tier priority: `ULTRA_HD → ULTRA → HIGH_HD → HIGH → MID_HD → MID → LOW_HD → LOW`.
5. Expect to tune video sync after first in-game validation on many IPK maps.

---

## 7. Step 5: Convert Textures, Pictos, and Tapes

### Textures and pictos

1. Strip CKD wrappers where needed.
2. Decode DDS/XTX payloads to usable texture outputs (usually TGA/PNG).
3. Check both standard map paths and cooked cache-like layouts if direct paths are empty.
4. Place final outputs under expected map folders (`MenuArt/textures`, `Timeline/pictos`).

### Tapes

1. Convert JSON-style data to Lua tables for JD2021 consumption.
2. For binary tape formats, parse clips and rebuild valid Lua structures.
3. Validate clip timing/ticks and track references before install.
4. Handle dance tape special processing:
   - **MotionClip Color**: Convert `[a, r, g, b]` float arrays to hex strings (e.g., `"0x0e8cd3ff"`)
   - **MotionPlatformSpecifics**: Convert platform dict to KEY/VAL array format
   - **Tracks array**: Build `Tracks = { {TapeTrack = {id = X}}, ... }` from unique TrackIds
   - **Degenerate TrackId normalization**: When every clip has a unique ID, group by class and assign shared IDs
   - **Primitive arrays**: Must be wrapped as `{ {VAL = 1}, {VAL = 2} }` to prevent engine crashes

### Cinematic tapes

If the extracted IPK contains files in a `cinematics/` folder:

- **Curve data**: `[x, y]` keyframe values must be emitted as `vector2dNew(x, y)`
- **ActorIndices resolution**: Integer actor references must be resolved against the tape's `ActorPaths` array

### Ambient sounds from IPK

If the extracted IPK has files in an `audio/amb/` folder (e.g., `amb_mapname_intro.tpl.ckd`), process each into:

- `.ilu` (sound descriptor): Lua sound list + `appendTable` call
- `.tpl` (actor template): Wrapper referencing SoundComponent and the `.ilu`

Then inject a SoundComponent actor into the audio `.isc` for each AMB file.

---

## 8. Step 6: Generate JD2021 Game Files

Create the standard map structure under `World/MAPS/<MapName>/` and generate:

- main scene ISC
- `SongDesc.tpl` and `SongDesc.act`
- Audio chain (`.trk`, musictrack tpl, sequence tpl, `.stape`, audio ISC, config sfi)
- Timeline dance/karaoke assets
- Cinematics chain
- VideosCoach files
- MenuArt actors/textures
- Autodance assets

Critical IPK notes:

1. Ensure `.trk` markers stay sample-accurate with 48kHz assumptions.
2. Keep `videoStartTime` in `.trk` aligned with your best known offset.
3. If AMB assets are missing, do not treat that as immediate install failure under current V2 mitigation state.

---

## 9. Step 7: Register in SkuScene

Register the map in `SkuScene_Maps_PC_All.isc`:

1. Add actor entry with the map codename and songdesc template path.
2. Ensure registration is idempotent (avoid duplicate entries).
3. Confirm map title and covers resolve in song select.

---

## 10. Step 8: Validate and Readjust

After first launch:

1. Confirm map loads and appears in song list.
2. Check coach select, timeline cues, and video playback.
3. Test audio/video start alignment on first beats.
4. Apply sync refinement iteratively until visually correct.

Recommended readjust loop:

1. Change only one offset axis at a time.
2. Retest from map start each pass.
3. Persist final values in your tracking/index workflow.

---

## 11. Troubleshooting

| Issue | Likely Cause | Manual Fix |
|------|------|------|
| Start silence on automated V2 install | Intro AMB mitigation currently active | Expected in current builds; continue sync tuning and watch AMB redesign updates |
| Video still off after install | IPK lead-in metadata incomplete/approximate | Manually tune video offset in readjust workflow |
| Progressive desync | Wrong WAV sample rate | Rebuild WAV at exactly 48kHz |
| Audio decode failure from CKD | Missing/unsupported decode path | Verify vgmstream availability and fallback source assets |
| Missing pictos/menuart | Assets only present in cooked/cache-style paths | Scan fallback cooked paths and re-place outputs manually |
| Coach select crash | Incomplete cinematics/timeline chain | Rebuild required ISC/TPL/ACT/TAPE references |
| Missing map title | SkuScene or SongDesc reference mismatch | Recheck codename and songdesc paths in SkuScene |
| Bundle contains multiple maps | Multi-map IPK with wrong codename selection | Use `inspect_ipk()` to list all maps, or set `desired_codename` in `ArchiveIPKExtractor` |
| Extraction produced no files | All entries had path-traversal violations or bad compression | Re-validate IPK integrity; check for non-standard archive variants |
| Kinect gesture load failure / coach-select freeze | Non-Kinect or newer-schema `.gesture` files copied to PC/ | Only use `.gesture` files from X360/DURANGO (Kinect v1/v2 format) |
| Preview/decode failures for some maps | Missing FFmpeg/FFprobe or missing vgmstream runtime | Re-run `setup.bat` and confirm both toolchains are available before reinstall/readjust |

---

## 12. Manual Checklist

Use this before finalizing an IPK port:

1. IPK extracted with safe paths and no traversal issues.
2. Codename-scoped assets collected (no cross-map contamination).
3. MusicTrack markers/startBeat/endBeat/videoStartTime validated.
4. Signatures and sections parsed (if present in source data).
5. WAV exported at 48kHz PCM.
6. Video asset and manifest verified.
7. Tapes converted to valid Lua output (including MotionClip color, platform specifics, and primitive array wrapping).
8. MenuArt/pictos resolved, including fallback scans when needed.
9. Full map folder generated under `World/MAPS/<MapName>/`.
10. SkuScene registration added once (no duplicates).
11. In-game validation and manual readjust completed.

If all eleven checks pass, the IPK map is in a stable manual-port state for V2.

---

## 13. Appendix A: How V2 IPK Extraction Works

The `ArchiveIPKExtractor` class in `extractors/archive_ipk.py` drives automated IPK installs:

```
IPK File → validate_ipk_magic() → extract_ipk()
                                      ├─ Read big-endian header
                                      ├─ Parse file entries (offset, size, path)
                                      ├─ Decompress: zlib → lzma → raw fallback
                                      └─ Path-traversal protection (skip unsafe)
                                   → _detect_maps_in_dir() + inspect_ipk()
                                      ├─ Filesystem scan: world/maps/ + world/jd20XX/
                                      ├─ Header scan: path_name fields in archive
                                      └─ Filter: exclude cache/common/enginedata/etc.
                                   → Codename selection
                                      ├─ Match desired_codename (if provided)
                                      ├─ Match IPK filename stem (strip platform suffix)
                                      └─ Fallback: first discovered candidate
```

The `inspect_ipk()` function provides a **fast, header-only** scan (no decompression) for discovering map codenames before committing to a full extraction.

---

## 14. Appendix B: Manual Mode Source Types

When using the GUI **Manual mode**, the Source Type selector controls which UI fields and validation rules are active:

| Source Type | Value | Shows | Validation |
|-------------|-------|-------|------------|
| **JDU** | `jdu` | Required files + JDU MenuArt fields | Expects `assets.html` + `nohud.html` pair or equivalent |
| **IPK** | `ipk` | Required files + Tapes & Config + AMB/MenuArt dirs | Expects `world/maps/` or `world/jd20XX/` structure |
| **Mixed** | `mixed` | All fields (JDU + IPK combined) | Accepts either IPK structure or HTML pair |

Default is **Mixed** (index 2), which is the most permissive and suitable for unusual source layouts.

The `ManualExtractor` auto-detects source structure when `source_type = "auto"`:
- If the root contains `world/maps/` or `world/jd20XX/<codename>/`, it treats the source as IPK.
- Otherwise, it looks for HTML pairs or uses explicit file inputs.

Auto-fill behavior scans recursively for all source types and populates:
- Musictrack (priority: `*musictrack*.tpl.ckd` → `*musictrack*.trk` → `*.trk`)
- Dance/karaoke tapes (priority: `*_tml_dance.dtape` variants → broad `*dance*.dtape` fallback)
- Audio (priority: `.ogg` → `.wav` → `.wav.ckd`, excluding `audiopreview`, `amb_*`, `autodance`)
- Video (`.webm`, excluding `mappreview`/`videopreview`, quality-tier preference)
- Asset folders (moves, pictos, menuart/textures, amb)
