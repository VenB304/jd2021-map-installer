# Pipeline Reference

**Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This document describes the JD2021 Map Installer v2 data pipeline: how map data flows from source ingestion (Fetch JDU, HTML JDU, Fetch JDNext, HTML JDNext, IPK, Batch, Manual) through extraction, normalization, and installation into playable UbiArt engine files.

## Current Behavior Notes (June 2026)

> [NOTE]
> **IPK-derived video timing may require manual tuning.**
> For many IPK maps, `videoStartTime` is approximate by design due to source metadata limitations. Manual post-install offset adjustment is expected.

> [WARNING]
> **Runtime dependencies are required for full pipeline coverage.**
> Missing or misconfigured FFmpeg/FFprobe, vgmstream, or Playwright Chromium can cause degraded behavior, fallback processing, or workflow failures.

---

## Pipeline Overview

V2 replaces the legacy monolithic flow with a three-phase architecture:

```
┌─────────────────────┐      ┌───────────────────┐      ┌─────────────────┐
│     EXTRACTION      │ ───→ │  NORMALIZATION    │ ───→ │  INSTALLATION   │
│                     │      │                   │      │                 │
│ WebPlaywright       │      │  normalizer.py    │      │ game_writer.py  │
│ ArchiveIPK          │      │  binary_ckd.py    │      │ texture_decoder │
│ JDNextBundleStrategy│      │                   │      │ media_processor │
│ ManualExtractor     │      │                   │      │                 │
│                     │      │ NormalizedMapData │      │ UbiArt configs  │
│ Raw files in        │      │ (canonical model) │      │ .trk/.tpl/.act  │
│ output_dir          │      │                   │      │ .isc/.mpd/.sfi  │
└─────────────────────┘      └───────────────────┘      └─────────────────┘
```

Each phase is orchestrated by a `QObject` worker running on a `QThread`, communicating with the GUI through Qt signals.

Pipeline execution is launched from multiple UI modes (Fetch JDU, HTML JDU, Fetch JDNext, HTML JDNext, IPK, Batch, Manual). Source mode affects extraction strategy, but all successful paths converge into the same normalization and installation contracts.

---

## Extractor Architecture

All extractors implement the `BaseExtractor` abstract class:

```python
class BaseExtractor(ABC):
    @abstractmethod
    def extract(self, output_dir: Path) -> Path: ...

    @abstractmethod
    def get_codename(self) -> Optional[str]: ...

    def get_warnings(self) -> list[str]: ...
```

The `extract()` method populates a directory with raw source files. The
normalizer then processes this directory into a `NormalizedMapData`. The
contract guarantees that **source format details are fully encapsulated
within the extractor** — the normalizer receives a uniform filesystem
layout.

---

## Phase 1 — Extraction

**Worker:** `ExtractAndNormalizeWorker` (first half)

**Purpose:** Fetch raw map data from a source and place it into a temporary directory.

### Web Extraction (`WebPlaywrightExtractor`)

Handles HTML files exported from JDHelper, direct URL lists, fetch-driven URL discovery via Discord automation, and JDNext CDN downloads.

**Steps:**
1. Collect URLs from `asset_html`, `nohud_html`, and/or direct URL list.
2. Extract codename from URL pattern (`/public/map/{MapName}/...` or `/jdnext/maps/{id}/...`).
3. Classify URLs into video, audio, mainscene ZIP, mapPackage bundle, and other assets.
4. Select best video quality (falls back through 8 tiers: `ULTRA_HD` → `LOW`).
5. Select preferred mainscene platform (`MAP_PACKAGE` → `DURANGO` → `NX` → `SCARLETT` → any).
6. Download files with retry logic (exponential backoff, HTTP 429 handling, 0.5s inter-request delay).
7. Skip already-downloaded files (cache check with integrity validation for NOHUD videos).

**JDNext-specific steps:**
- **mapPackage detection:** URLs containing `mappackage` and `.bundle` are classified as `MAP_PACKAGE` scene sources.
- **Bundle extraction:** `run_jdnext_bundle_strategy()` is invoked on downloaded `.bundle` files, which dispatches to AssetStudioModCLI (primary) or UnityPy (fallback).
- **Post-extraction mapping:** `map_assetstudio_output()` reorganizes the raw AssetStudio export into a pipeline-compatible structure: `monobehaviour/map.json`, synthesized CKDs, `pictos/`, `menuart/`, `timeline/moves/`.
- **Auxiliary texture bundles:** `_extract_jdnext_aux_texture_bundles()` processes non-mapPackage bundles to recover Cover, Coach, and Title images.
- **Missing asset fallbacks:** `_try_jdnext_missing_fallbacks()` attempts to reuse locally cached mapPackage bundles from `temp/jdnext_downloads/` when private CDN links fail.

**JDNext video quality mapping:**

| Variant | `.hd` | `.vp9` | `.vp8` |
|---------|-------|--------|--------|
| `ultra` | `ULTRA_HD` | `ULTRA` | `ULTRA_HD` (fallback) |
| `high` | `HIGH_HD` | `HIGH` | `HIGH_HD` (fallback) |
| `mid` | `MID_HD` | `MID` | `MID_HD` (fallback) |
| `low` | `LOW_HD` | `LOW` | `LOW_HD` (fallback) |

**Download fallback chain:**
1. `requests.Session` (primary, with Referer header)
2. `curl.exe --resolve` (for DNS-sensitive hosts, bypasses local ISP DNS via Cloudflare `1.1.1.1`)
3. `Invoke-WebRequest` PowerShell (Windows-only fallback when Python DNS fails)

**Inputs:** HTML file paths or URL list, quality preference, `AppConfig`.

**Outputs:** Raw files in `output_dir` (CKDs, OGG/Opus, WebM, ZIPs, Unity bundles).

**Errors:** `WebExtractionError` if no URLs are provided. `DownloadError` for network failures (with `url` and `http_code` attributes).

### IPK Extraction (`ArchiveIPKExtractor`)

Handles Xbox 360 `.ipk` archive files, including multi-map bundles.

**Steps:**
1. Validate IPK magic bytes (`\x50\xEC\x12\xBA`).
2. Read big-endian file headers (12-field header, per-file entries with offset/size/compressed_size/paths).
3. Decompress each entry (tries zlib first, then lzma, then raw copy).
4. Apply path-traversal protection (rejects paths that escape `output_dir`).
5. Detect maps via filesystem scan (standard `world/maps/<codename>` and legacy `world/jd20XX/<codename>` layouts).
6. Corroborate with header-only fast scan (`inspect_ipk`).
7. Resolve codename: desired codename → filename stem matching → first candidate.

**Multi-map bundle handling:**
- `bundle_maps` list exposes all discovered codenames.
- IPK filename stems are stripped of platform suffixes (`_x360`, `_durango`, `_scarlett`, `_nx`, `_orbis`, `_prospero`, `_pc`) for matching.
- Internal folders like `cache`, `common`, `enginedata`, `audio`, `videoscoach`, `localization` are excluded from map detection.

**Inputs:** Path to `.ipk` file, optional desired codename.

**Outputs:** Extracted files in `output_dir`.

**Errors:** `IPKExtractionError` for invalid files or extraction failures.

### JDNext Unity Bundle Extraction

**Module:** `jdnext_bundle_strategy.py` + `jdnext_unitypy.py`

JDNext bundles undergo a two-stage process:

**Stage 1 — Raw extraction** (strategy-based):

| Strategy | Primary Backend | Fallback Backend |
|----------|----------------|-----------------|
| `assetstudio_first` | AssetStudioModCLI.exe | UnityPy (Python) |
| `unitypy_first` | UnityPy (Python) | AssetStudioModCLI.exe |

UnityPy extracts objects into typed subdirectories (`textures/`, `audio/`,
`video/`, `text/`, `typetree/`) with a `summary.json` and `objects_index.json`.

**Stage 2 — Asset mapping** (`map_assetstudio_output`):

```
AssetStudio raw export → mapped/ directory
  ├─ monobehaviour/map.json          (map metadata)
  ├─ <codename>_musictrack.tpl.ckd   (synthesized from MusicTrack.json)
  ├─ <codename>_tml_dance.dtape.ckd  (synthesized from DanceData)
  ├─ <codename>_tml_karaoke.ktape.ckd (synthesized from KaraokeData)
  ├─ pictos/*.png                    (matched by PictoPath names)
  ├─ menuart/*.png                   (remaining textures)
  └─ timeline/moves/wiiu/*.gesture|*.msm (motion classifier files)
```

**Synthesized CKD files** are standard JSON payloads that the normalizer
processes identically to JDU CKD files.

### Live Scraping (`scrape_live()`)

Live/experimental mode using Playwright to scrape JDU asset pages:

1. Launch headless Chromium via `playwright.async_api`.
2. Navigate to page URL and wait for `networkidle`.
3. Extract page content and parse URLs.
4. Return URL list for the standard download pipeline.

Runs in an `asyncio` event loop inside a `QThread` worker.

Operational caveat:
- Link expiry and remote endpoint behavior can break fetch sessions even when local code is healthy. Retry and fallback logic reduce, but do not eliminate, this fragility.

### Discord Automation (Fetch Modes)

The Fetch JDU and Fetch JDNext modes automate Discord slash commands:

1. Open browser to Discord channel URL.
2. Wait for login (`[role="textbox"]` detection, up to 5 min).
3. Send `/assets <codename>` slash command (choices: `jdu` or `jdnext`).
4. Wait for bot embed response (stability polling: 3 × 500ms).
5. Extract URLs from response HTML.
6. **For `/nohud` step:** Repeat with the NOHUD variant command.
7. Pass collected URLs to the standard download pipeline.

---

## Phase 2 — Normalization

**Worker:** `ExtractAndNormalizeWorker` (second half)

**Entry point:** `normalizer.normalize(directory, codename)`

**Purpose:** Transform raw extracted files into a single canonical `NormalizedMapData` dataclass, regardless of source format.

### Source Directory Resolution

The normalizer first resolves the map-local directory within the extraction
output using `_resolve_map_source_dir()`, which searches for a subdirectory
matching the codename in both `world/maps/` and `world/jd20XX/` hierarchies.

### JDNext Source Detection

The normalizer auto-detects JDNext-origin sources via `_is_jdnext_source()`:

| Signal | Priority |
|--------|----------|
| `jdnext_metadata.json` exists | Strongest |
| `monobehaviour/map.json` exists | Strong |
| HTML content contains `/jdnext/maps/` or `server:jdnext` | Medium |
| Video filename matches `video_<tier>.<variant>.webm` | Medium |
| Audio filename is `audio.opus` | Weak |

### CKD Loading Strategy

For each CKD type, the normalizer:
1. Searches the directory recursively using glob patterns (e.g., `*musictrack*.tpl.ckd`).
2. Filters by codename if provided (matches directory components or filename prefix).
3. Prefers non-legacy files over `main_legacy` variants.
4. Attempts **JSON parsing first** (strips null padding, decodes UTF-8).
5. Falls back to the **binary CKD parser** on JSON failure.

**CKD stem alias inference:** When source filenames don't match the codename (common with JDNext maps that use legacy internal stems), `_infer_ckd_stem_alias()` scans for the most frequent alternate stem across musictrack, dtape, ktape, and songdesc files.

**Bundle scoping:** In multi-map bundles, CKD discovery uses strict codename scoping. If codename filtering returns 0 results but multiple unscoped candidates exist, the normalizer returns empty rather than risk cross-map assignment.

### Data Extraction

| Component | Glob Pattern | Output Model |
|-----------|-------------|--------------| 
| Music Track | `*musictrack*.tpl.ckd` | `MusicTrackStructure` (markers, signatures, sections, timing) |
| Song Description | `*songdesc*.tpl.ckd` | `SongDescription` (title, artist, difficulty, colors, tags) |
| Dance Tape | `*dtape*ckd` | `DanceTape` (MotionClips, PictogramClips, GoldEffectClips) |
| Karaoke Tape | `*ktape*ckd` | `KaraokeTape` (KaraokeClips with lyrics, pitch, tolerances) |
| Cinematic Tape | `*mainsequence*tape.ckd` | `CinematicTape` (SoundSetClips, TapeReferenceClips) |
| Media Assets | `*.webm`, `*.ogg`, `*.opus`, `*.wav`, `*.jpg`, `*.png` | `MapMedia` (video, audio, cover, coaches, pictograms) |

### Normalization Overlays (JDNext)

After primary extraction, the normalizer applies JDNext-specific overlays
in order:

1. **JDNext metadata overlay** (`_apply_jdnext_metadata_songdesc_overrides`)
   — from `jdnext_metadata.json`
2. **JDNext songdb cache overlay** (`_apply_jdnext_songdb_cache_overrides`)
   — from synthesized songdb lookup
3. **Preview field merge** (`_merge_preview_fields_from_trk`) — from source `.trk` file

**All overlays are conditional** — they only modify fields that contain
placeholder or default values, never overwriting authoritative source data.

### Binary CKD Parser

The stateless parser (`binary_ckd.py`) handles legacy binary (cooked) UbiArt files:

- **Dispatch:** Filename-based for tapes (`dtape`, `ktape`, `btape`, `.tape.ckd`), Actor header CRC-based for TPL files.
- **Reader:** `BinaryReader` class for big-endian sequential reads (`u32`, `i32`, `f32`, `u16`, `len_string`, `interned_string`, `split_path`).
- **Known CRCs:** `MusicTrackComponent_Template` (0x02883A7E), `JD_SongDescTemplate` (0x8AC2B5C6), `Actor_Template` (0x1B857BCE), `AutodanceComponent_Template` (0x51EA2CD0), `SoundComponent_Template` (0xD94D6C53), `Tape` (0x2AFED161), `BeatClip` (0x364811D4).

**MusicTrack binary parsing:** The parser reads markers, signatures (with
per-entry fields), sections (with optional comment strings), timing fields
(`startBeat`, `endBeat`, `videoStartTime`, `volume`), and an optional 32-byte
trailing block for preview/fade values (sanity-checked against 0.0–10000.0
range). Some console binaries carry this data regardless of the version flag.

**SongDesc binary parsing:** Reads map_name, jd_version, original_jd_version,
related albums (skipped), artist, dancer_name, title, coach/difficulty fields,
tags (CRC-indexed, decoded as `["Main"]`), DefaultColors (CRC-keyed RGBA),
and path arrays (consumed but discarded).

### Media Discovery

The normalizer discovers media assets through priority-ordered filesystem scanning:

**Video:** `.webm` files, filtered by codename matching (filename then path),
with quality tier preference (`ULTRA_HD` → `LOW`). Preview videos are
separated into `map_preview_video`.

**Audio:** Priority: `.ogg` > `.opus` > `.wav` > `.wav.ckd`. Codename scoping
with exclusions for `amb/`, `autodance/`, `audiopreview`, `mappreview`.

**Images:** Cover, coach, banner, map background textures scanned via codename-prefixed filename patterns.

### Validation

After normalization, the following checks are applied:
- `MusicTrackStructure.markers` must not be empty → raises `ValidationError`.
- Preview fields (entry, loop start, loop end) are sanity-checked against marker count.

Source-shape caveats:
- Some IPK-derived maps rely on cache-like layouts (`cache/`, `itf_cooked`) for pictogram/texture discovery when canonical paths are incomplete.
- Path casing and folder-shape differences are handled with fallbacks, especially around audio/ambient-related assets.

**Output:** `NormalizedMapData` — the single canonical representation of a map.

---

## Phase 3 — Installation

**Worker:** `InstallMapWorker`

**Entry point:** `game_writer.write_game_files(map_data, target_dir, config)`

**Purpose:** Generate all UbiArt engine configuration files from `NormalizedMapData`.

### Directory Setup

Creates the standard map directory structure:
```
{target_dir}/
├── Audio/
├── Timeline/
│   ├── pictos/
│   └── Moves/
├── Cinematics/
├── VideosCoach/
├── MenuArt/
│   ├── Actors/
│   └── textures/
└── Autodance/
```

### Generated Files

| File | Format | Contents |
|------|--------|----------|
| `Audio/{MapName}.trk` | Lua | Beat timing markers, signatures, sections, videoStartTime |
| `SongDesc.tpl` | Lua | Full song metadata (title, artist, difficulty, colors, tags, phone images) |
| `SongDesc.act` | Lua | Song description actor instance |
| `Audio/{MapName}_musictrack.tpl` | Lua | MusicTrack template referencing `.trk` |
| `Audio/{MapName}_sequence.tpl` | Lua | Sequence template with TapeCase |
| `Audio/{MapName}.stape` | Lua | Sequence tape |
| `Audio/{MapName}_audio.isc` | XML | Audio scene with MusicTrack + TapeCase actors |
| `Audio/ConfigMusic.sfi` | XML | Sound format info (PC/Durango/NX/ORBIS) |
| `Timeline/{MapName}_TML_Dance.tpl` | Lua | Dance TapeCase template |
| `Timeline/{MapName}_TML_Dance.act` | Lua | Dance TapeCase actor |
| `Timeline/{MapName}_TML_Karaoke.tpl` | Lua | Karaoke TapeCase template |
| `Timeline/{MapName}_TML_Karaoke.act` | Lua | Karaoke TapeCase actor |
| `Timeline/{MapName}_tml.isc` | XML | Timeline scene |
| `VideosCoach/{MapName}.mpd` | XML | DASH manifest for gameplay video |
| `VideosCoach/{MapName}_MapPreview.mpd` | XML | DASH manifest for preview video |
| `VideosCoach/video_player_main.act` | Lua | Video player actor (PleoComponent) |
| `VideosCoach/video_player_map_preview.act` | Lua | Preview video player actor |
| `VideosCoach/{MapName}_video.isc` | XML | Video scene |
| `VideosCoach/{MapName}_video_map_preview.isc` | XML | Preview video scene |
| `MenuArt/Actors/{MapName}_*.act` | Lua | MenuArt texture actors |
| `MenuArt/{MapName}_menuart.isc` | XML | MenuArt scene |
| `{MapName}_MAIN_SCENE.isc` | XML | Root scene tying all subsystems |
| `Autodance/{MapName}_autodance.isc` | XML | Autodance scene |
| `Autodance/{MapName}_autodance.tpl` | Lua | Autodance template (stub or converted) |
| `Autodance/{MapName}_autodance.act` | Lua | Autodance actor |
| `Cinematics/{MapName}_MainSequence.tape` | Lua | Main sequence tape (stub or converted) |
| `Cinematics/{MapName}_cine.isc` | XML | Cinematics scene |
| `Cinematics/{MapName}_mainsequence.tpl` | Lua | MasterTape template |
| `Cinematics/{MapName}_mainsequence.act` | Lua | MasterTape actor |

### Key Transformations

- **Status override:** `Status = 12` (ObjectiveLocked) → `Status = 3` (Available).
- **JDVersion mapping:** Runtime `JDVersion` is normalized to a stable engine branch (`2016` or `2021` via `_select_playable_jd_version()`), while `OriginalJDVersion` preserves the map's numeric source year.
- **Coach count inference:** If `num_coach < 1`, counts `coach_*.png/.tga` files in `MenuArt/textures/`. Clamped to max 4 (JD2021 player slot limit).
- **Color conversion:** `[R,G,B,A]` float arrays → `0xRRGGBBAA` hex strings via `color_array_to_hex()`. Missing alpha padded to `0xFF`.
- **Lua long strings:** Handles nested brackets in metadata values via `lua_long_string()`.
- **videoStartTime preservation:** For JDNext/IPK maps, the writer checks the source `.trk` for an authoritative value before falling back to marker synthesis.
- **Preview loop enforcement:** Monotonic ordering is enforced (entry ≤ start ≤ end) with logged warnings when adjustments are made.
- **Autodance protection:** If a `_autodance.tpl` already exceeds 1KB (indicating real converted data), the stub writer is skipped.

Behavior caveats:
- **Intro ambient behavior:** Current V2 generation keeps intro ambient effectively silent by policy (temporary mitigation).
- **IPK video sync expectation:** `videoStartTime` is frequently a best-effort value for IPK sources; in-app readjustment is the intended follow-up step.

**Errors:** `GameWriterError` wraps any file generation failure.

### Texture Decoding

`texture_decoder.py` handles CKD texture conversion during installation:

| Function | Platform | Pipeline |
|----------|----------|----------|
| `decode_ckd_texture()` | Auto-detect | CKD → strip header → detect format → DDS → Pillow → TGA/PNG |
| `decode_pictograms()` | All | Batch CKD decode + loose PNG/TGA/JPG copy. Canvas compositing (512px pictos preserved). |
| `decode_menuart_textures()` | All | Batch CKD decode (excluding companion `.act.ckd`, `.tpl.ckd`). Loose PNG/TGA/JPG passthrough. |

### Media Processing

`media_processor.py` provides supporting operations:

| Function | Purpose |
|----------|---------| 
| `run_ffmpeg()` | Execute FFmpeg subprocess with error handling and timeout |
| `run_ffprobe()` | Execute FFprobe for media inspection |
| `get_video_duration()` | Probe video duration in seconds |
| `copy_video()` / `copy_audio()` | Copy media files with directory creation |
| `generate_map_preview()` | Create map preview video clip (VP9, 1Mbps) |
| `generate_audio_preview()` | Create audio preview with fade-out (Vorbis) |
| `convert_image()` | Pillow-based format conversion with optional resize |
| `generate_cover_tga()` | Convert cover to 720×720 TGA for game engine |

Dependency notes:
- `ffmpeg`/`ffprobe` must be available for preview generation, timing probes, and many media transforms.
- `vgmstream` is required for parts of the legacy/X360 audio decode path.
- Dependency gaps can trigger fallback behavior, reduced output quality, or hard failure depending on operation.

---

## Worker Orchestration

### ExtractAndNormalizeWorker

```python
progress.emit(10)    # Starting extraction
extractor.extract(output_dir)
progress.emit(50)    # Extraction complete, starting normalization
normalize(extracted_dir, codename)
progress.emit(100)   # Normalization complete
finished.emit(map_data)  # NormalizedMapData or None on error
```

### InstallMapWorker

```python
progress.emit(20)    # Starting installation
write_game_files(map_data, target_dir, config)
progress.emit(100)   # Installation complete
finished.emit(True)  # success=True, or False on error
```

### Error Handling

All workers catch exceptions in their `run()` method:
1. Log the full traceback via `logger.error()`.
2. Emit the error message via `error.emit(str(e))`.
3. Emit `finished.emit(None)` or `finished.emit(False)` to signal failure.

The `MainWindow` connects to these signals to display errors in the log panel and re-enable UI controls.

---

## NormalizedMapData Model

The canonical data model that bridges Normalizer → Installer:

```python
@dataclass
class NormalizedMapData:
    codename: str
    song_desc: SongDescription
    music_track: MusicTrackStructure
    dance_tape: Optional[DanceTape] = None
    karaoke_tape: Optional[KaraokeTape] = None
    cinematic_tape: Optional[CinematicTape] = None
    beats_tape: Optional[BeatsTape] = None
    media: MapMedia = field(default_factory=MapMedia)
    sync: MapSync = field(default_factory=MapSync)
    source_dir: Optional[Path] = None
    is_html_source: bool = False
    is_jdnext_source: bool = False
    video_start_time_override: Optional[float] = None
    has_autodance: bool = True

    @property
    def effective_video_start_time(self) -> float:
        """Return the override if set, otherwise the CKD value."""
```

**Key flags:**
- `is_jdnext_source`: Set by `_is_jdnext_source()` detection. Controls downstream behaviors like VP9 video handling.
- `video_start_time_override`: User-specified sync adjustment; takes precedence over all other videoStartTime sources.
- `has_autodance`: Controls whether autodance scene/actor stubs are generated.

---

## Source Mode → Extractor Mapping

| UI Mode | Extractor Class | Key Behavior |
|---------|----------------|-------------|
| **Fetch JDU** | `WebPlaywrightExtractor` | Discord `/assets jdu` → URL collection → download |
| **HTML JDU** | `WebPlaywrightExtractor` | Parse JDHelper HTML → URL extraction → download |
| **Fetch JDNext** | `WebPlaywrightExtractor` | Discord `/assets jdnext` → URL collection → download → `run_jdnext_bundle_strategy()` |
| **HTML JDNext** | `WebPlaywrightExtractor` | Parse JDHelper HTML (JDNext) → URL extraction → download → `run_jdnext_bundle_strategy()` |
| **IPK Archive** | `ArchiveIPKExtractor` | IPK unpack → codename inference |
| **Batch** | `ArchiveIPKExtractor` (per-IPK) | Multi-file IPK iteration → per-map extract+normalize+install |
| **Manual** | `ManualExtractor` | User-provided directory → filesystem copy into pipeline |

---

## Related Operational Flows

This reference focuses on the extraction-normalization-installation core. In current V2 usage, two adjacent flows are also important:

1. **Post-install sync/readjust:** Users commonly refine offsets after install (especially IPK maps).
2. **Batch install + batch apply:** Multi-map processing and subsequent offset updates are part of normal operator workflow.

These flows are orchestrated in `MainWindow` and reuse outputs from the core pipeline described above.
