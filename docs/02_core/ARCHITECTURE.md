# Architecture
**Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This document describes the internal architecture of the JD2021 Map Installer v2: how components relate to each other, how data flows through the pipeline, and the key design patterns used throughout.

---

## Current Limitations and Behavioral Notes (Read First)

1. Intro AMB generation is active and enabled by default.
   The pipeline generates intro AMB audio for all maps with `videoStartTime < 0`. See [AUDIO_TIMING.md](../03_media/AUDIO_TIMING.md) for details.
2. IPK video start timing remains approximate by design.
   Many IPK sources do not carry reliable lead-in metadata, so manual video offset tuning is expected after install.
3. Dependency health directly affects behavior.
   Missing FFmpeg/FFprobe or vgmstream can degrade conversion and preview paths; missing Playwright Chromium blocks Fetch workflows; missing UnityPy or AssetStudioModCLI limits JDNext extraction.
4. Parity work is ongoing.
   Core V2 pipeline is stable, but map-specific edge-case regressions are still possible in complex parity-sensitive paths.

---

## Component Map

```
                                 User Input
                                   │
                              main.py
                    QApplication + setup_logging()
                    + load_startup_config() + theme
                                   │
                          ui/main_window.py
                            MainWindow (PyQt6)
                                   │
          ┌────────────────────────┼────────────────────────┐
          │                        │                        │
    Source Ingestion         Install/Write           Readjust/Sync
    (mode-driven)             Pipeline               Workflow Layer
          │                        │                        │
    ui/workers/pipeline_workers.py and related worker modules
    ┌──────────────────────────────────────────────────────────────┐
    │ ExtractAndNormalizeWorker (QObject on QThread)              │
    │ InstallMapWorker (QObject on QThread)                       │
    │ Readjust-oriented workers (batch/apply/index-aware)         │
    └───────────────────────┬──────────────────────────────────────┘
                            │
          ┌─────────────────┼──────────────────┐
          │                 │                  │
      extractors/        parsers/         installers/
      - web/fetch        - normalizer     - game writer
      - archive_ipk      - binary_ckd     - media processor
      - manual                             - tape converter
      - jdnext_unitypy                     - texture decoder
      - jdnext_bundle                      - ambient processor
        _strategy                          - autodance processor
                                           - sku_scene registration
                            │
                      NormalizedMapData
                            │
                  UbiArt-ready game files
```

### Entry Point

| Module | Description |
|--------|-------------|
| `main.py` | Creates QApplication, configures logging via `setup_logging()`, loads startup config for theme/icon, and shows MainWindow. Entry via `python -m jd2021_installer.main`. |

### Core Modules

| Package | Module | Role |
|---------|--------|------|
| `core/` | `models.py` | Data models including `NormalizedMapData` and typed structures for song description, music track, tapes (dance, karaoke, cinematic, **beats**), clips (motion, pictogram, gold effect, karaoke, **sound set, tape reference, beat**), sections, signatures, colors, media, and **sync offsets**. |
| `core/` | `config.py` | `AppConfig` (Pydantic v2 BaseModel): paths, quality tiers, download behavior, engine constants, FFmpeg paths, runtime options, **Discord Fetch mode settings (channel URL, bot provider `sev4nty`/`rama`, browser profile, login/bot timeouts, background mode)**, **update checker settings**, **VP9 handling mode**, **preview video mode**, **video fallback behavior**, **gesture compilation flag**, **albumcoach/JDNext cover behavior**, **JDLO auth path**. Supports `JD2021_` environment overrides. |
| `core/` | `exceptions.py` | Typed exception hierarchy rooted at `JDInstallerError` for extraction, parsing, normalization, and installation failures. |
| `core/` | `logging_config.py` | **Four-tier logging detail system** (`quiet`, `user`, `detailed`, `developer`) with per-sink formatting (console, UI QtLogHandler, file). |
| `core/` | `theme.py` | **Theme stylesheet loading** with light/dark support, fallback resolution, and optional style debug overlay. |
| `core/` | `path_discovery.py` | **Game directory auto-detection** via fast heuristics and deep recursive scan with TTL caching. Codename inference from file/directory paths. |
| `core/` | `readjust_index.py` | **Persistent readjust index** for installed maps — stores source metadata, installed paths, and sync offsets. Supports upsert, prune, and offset update operations. |
| `core/` | `songdb_update.py` | **JDNext song database synthesizer** — imports raw JDNext songdb JSON into a compact lookup cache with normalized `mapName`, `parentMapName`, and `title` keys. Also provides JDU and JDNext codename extraction utilities. |
| `core/` | `install_summary.py` | **Post-install checklist builder** — evaluates required and optional file presence after installation and renders an actionable summary with status labels (`SUCCESS`, `PARTIAL/RISKY`, `FAILED`). |
| `core/` | `localization_update.py` | **ConsoleSave.json localization merger** — merges localization data into the game's translation index for 17 language codes. |
| `core/` | `clean_data.py` | **Game data reset** — removes custom maps, unregisters them from SkuScene, and cleans cooked cache while preserving baseline maps (currently `getgetdown`). |
| `extractors/` | `base.py` | `BaseExtractor` ABC contract: `extract()`, `get_codename()`, and `get_warnings()`. |
| `extractors/` | `web_playwright.py` | **Fetch, HTML, and Discord bot-driven ingestion** with URL extraction, codename detection (JDU and JDNext URL patterns), quality/platform/VP9-handling selection, retries with exponential backoff, HTTP 429 rate-limit handling, **PowerShell/curl DNS fallback downloaders**, EBML/FFmpeg webm integrity validation, and **JDNext mapPackage + auxiliary texture bundle extraction**. |
| `extractors/` | `archive_ipk.py` | IPK extraction with decompression (zlib/lzma/raw), path-traversal protection, `inspect_ipk()` fast header scan, and **multi-map bundle detection** supporting both standard (`world/maps/`) and legacy (`world/jd20XX/`) layouts. |
| `extractors/` | `manual_extractor.py` | **Manual source-folder extractor** — assembles extraction output from user-provided file/directory paths with IPK structure detection, HTML pair detection, codename-scoped media validation, and multi-map bundle support. |
| `extractors/` | `jdnext_unitypy.py` | **JDNext UnityPy extraction** — standalone bundle unpack using UnityPy for Texture2D, AudioClip, VideoClip, TextAsset, and MonoBehaviour objects. Produces a structured output with summary and objects index. Handles encrypted bundle detection. |
| `extractors/` | `jdnext_bundle_strategy.py` | **JDNext dual-strategy extraction** — orchestrates AssetStudioModCLI and UnityPy with configurable strategy (`assetstudio_first` or `unitypy_first`) and automatic fallback. Includes **CKD synthesis** from JDNext `MusicTrack.json` and `map.json`, **tape synthesis** (dance, karaoke) from JDNext `DanceData`/`KaraokeData`, and asset mapping (gestures, msm, pictos, menuart). |
| `extractors/` | `xtx_extractor/` | **Switch XTX texture extraction** — deswizzle and DDS conversion for NX platform textures. |
| `parsers/` | `normalizer.py` | Canonical normalize entrypoint. Loads CKD data (JSON-first, binary fallback), discovers media, validates, and emits `NormalizedMapData`. |
| `parsers/` | `binary_ckd.py` | Binary CKD parser for cooked structures (musictrack, songdesc, dance/karaoke/cinematic tapes, related payloads). |
| `installers/` | `game_writer.py` | Writes core UbiArt map outputs such as `.trk`, `.tpl`, `.act`, `.isc`, `.stape`, `.sfi` and related map assets. |
| `installers/` | `media_processor.py` | Media operations and wrappers around FFmpeg/FFprobe/Pillow for audio/video/image conversion, preview generation, cover outputs, and **intro AMB generation** (`generate_intro_amb()`). |
| `installers/` | `tape_converter.py` | **CKD JSON → UbiArt Lua tape converter.** Converts dance tapes (`.dtape.ckd`), karaoke tapes (`.ktape.ckd`), cinematic/mainsequence tapes (`.tape.ckd`), **beats tapes (`.btape.ckd`)**, and stape files. Supports JSON-first with binary CKD fallback parsing. Includes codename reference rewriting and path normalization. `auto_convert_tapes()` provides batch detection and conversion with loose tape fallback. |
| `installers/` | `texture_decoder.py` | **CKD texture decoder** supporting three platforms: PC (strip 44-byte header → DDS → Pillow), NX/Switch (XTX deswizzle → DDS), and **Xbox 360 (byte-swap + untile → DDS)**. Batch decoders for pictograms (with canvas sizing) and MenuArt textures. Handles loose PNG/TGA/JPG passthrough for already-decoded assets. |
| `installers/` | `ambient_processor.py` | **Ambient sound processor** — processes `amb_*.tpl.ckd` and `set_amb_*.tpl.ckd` into `.ilu` / `.tpl` Lua pairs. Includes intro AMB SoundSetClip injection/normalization in MainSequence tapes with timing derived from HideUserInterfaceClip, startBeat markers, or existing source clips. Supports synthetic AMB generation for orphaned `.wav.ckd` files. |
| `installers/` | `biomechanics.py` | **3D skeleton reconstruction for JDNext gestures** — reconstructs depth (Z-axis) from 2D camera landmarks via analytical forward kinematics (single-pass tree traversal from HipsCenter root). Computes velocity, acceleration, angular velocity, torque, and muscle force via Savitzky-Golay smoothing + inverse dynamics. Used by `gesture_compiler.py`. |
| `installers/` | `gesture_compiler.py` | **JDNext→Durango Kinect gesture compiler** — converts biomechanical feature vectors into Durango `.gesture` binaries. Implements AdaBoost ensemble re-weighting with gamma redistribution (pruning optical-flow stumps), adaptive per-joint dead-zones, structural gating edge detection, and threshold quantization matching real Kinect files. Opt-in: `convert_jdnext_gestures = True`. |
| `installers/` | `hmm_generator.py` | **Durango gesture state table generator** — dynamically builds the Zone A (implicit joint records) and Zone B (type-prefixed variable records) state tables written to `.gesture` files, with state counts derived from constraint density. |
| `installers/` | `autodance_processor.py` | **Autodance processor** — converts autodance CKDs (`.tpl.ckd`, `.adtape.ckd`, `.adrecording.ckd`, `.advideo.ckd`) and optional stape CKDs into game-ready Lua files. Includes loose payload fallback for pre-decoded maps. |
| `installers/` | `sku_scene.py` | **SkuScene ISC registration** — adds/removes maps from the game's `SkuScene_Maps_PC_All.isc` song list. Inserts both `SubSceneActor` and `CoverflowSkuSongs` XML entries. Idempotent registration with `is_registered()` check. |
| `ui/` | `main_window.py` | MainWindow (`QMainWindow`): orchestrates mode selection, worker lifecycle, progress/status/error handling, and post-install sync/readjust actions. |
| `ui/workers/` | `pipeline_workers.py` | Background QObject workers on QThread for extraction, normalization, installation, and readjust/apply operations. |
| `ui/workers/` | `media_workers.py` | **Media-specific workers** for preview generation and media operations. |
| `ui/widgets/` | (15 widget modules) | **Mode selector** (`mode_selector.py`), **config panel** (`config_panel.py`), **action panel** (`action_panel.py`), **preview widget** (`preview_widget.py`), **sync refinement** (`sync_refinement.py`), **log console** (`log_console.py`), **settings dialog** (`settings_dialog.py`), **albumcoach dialog** (`albumcoach_dialog.py` — live-preview multi-coach compositor with drag-and-drop ordering, overlap sliders, and PIL-based compositing), **bundle dialog** (`bundle_dialog.py`), **feedback panel** (`feedback_panel.py`), **FFmpeg dialog** (`ffmpeg_dialog.py`), **metadata dialog** (`metadata_dialog.py`), **installation summary dialog** (`installation_summary_dialog.py`), **quickstart dialog** (`quickstart_dialog.py`), **update dialog** (`update_dialog.py`). |
| `utils/` | `icon_gen.py` | Default icon generation/validation for the application window. |
| (root) | `updater.py` | **Standalone update checker and auto-updater** — supports git and zip-download update strategies with user data preservation. Branch selection and commit comparison via GitHub API. |

---

## Source Ingestion Modes

V2 supports multiple source modes in active UI flows:

1. **Fetch by codename (JDU)** — Uses Playwright to automate Discord slash commands and download JDU assets. Supports `sev4nty` and `rama` bot providers (`discord_bot_provider` config).
2. **Fetch by codename (JDNext)** — Downloads JDNext bundles via Discord bot and runs the JDNext dual-strategy extraction pipeline.
3. **Fetch JDLO** — Downloads maps from the Just Dance Legacy Online (JDLO) CDN (requires `jdlo_auth_path`).
4. **HTML export mode** — Processes pre-saved HTML files (assets.html + nohud.html) from Fetch modes.
5. **IPK archive mode** — Extracts maps from Xbox 360 `.ipk` archives.
6. **Batch directory mode** — Scans a directory for multiple maps (IPK files and/or compatible folders).
7. **Manual source-folder mode** — User provides individual file/directory paths for maximum control.

All ingestion paths normalize into the same canonical `NormalizedMapData` model so downstream writing and media processing remain consistent.

---

## Concurrency Model

The GUI remains responsive during heavy I/O (download, extraction, parsing, media conversion, write) through the QThread plus moveToThread pattern.

```
    Main Thread (Qt Event Loop)            Background QThread
    MainWindow                              Worker QObject
      │                                       │
      ├─ create worker + thread               │
      ├─ worker.moveToThread(thread)          │
      ├─ thread.started -> worker.run()    -> run executes
      │                                       │
      │ <- progress/status signals -----------┤
      │ <- error signal on exception ---------┤
      │ <- finished signal with result -------┘
      │
      ├─ thread.quit / cleanup
      ├─ re-enable UI actions
      └─ continue to next phase or readjust flow
```

Key rules:
1. Workers never update widgets directly.
2. UI and worker communication is signal-based only.
3. Exceptions are logged and surfaced through typed error/status signals.

---

## Data Flow

### Standard Install Flow (Single Map)

```
    1. User selects source mode and input
       - Fetch codename / HTML / IPK / manual source
    2. MainWindow resolves extractor and run context
    3. ExtractAndNormalizeWorker:
       - extraction/download/decompression
       - JDNext: bundle strategy (AssetStudio/UnityPy) + CKD synthesis
       - normalization to NormalizedMapData
    4. InstallMapWorker:
       - game file synthesis (trk/tpl/act/isc/stape/sfi)
       - tape conversion (dtape/ktape/btape/mainsequence) via tape_converter
       - texture decoding (pictograms, menuart) via texture_decoder
       - media conversion/copy via media_processor
       - ambient processing (ILU/TPL generation, intro AMB injection)
       - autodance processing
       - scene registration via sku_scene
       - install summary generation
    5. UI receives completion:
       - preview/sync controls enabled
       - optional offset apply
       - readjust index upserted
       - install summary dialog shown
```

### Batch Install Flow

```
    1. User selects batch directory.
    2. Worker scans valid candidates (IPK and/or compatible folders).
    3. For each map: extract -> normalize -> install -> register.
    4. UI returns installed map set for review and optional batch offset apply.
```

### Readjust Flow

```
    1. User opens readjust from index or source root.
    2. App reloads map context and media discovery.
    3. User edits offsets and applies per-map or batch.
    4. Readjust index is pruned/upserted for future discoverability.
```

---

## Exception Hierarchy

```
    JDInstallerError
    ├── ExtractionError
    │   ├── IPKExtractionError
    │   ├── WebExtractionError
    │   └── DownloadError (with url and http_code attributes)
    ├── ParseError
    │   └── BinaryCKDParseError
    ├── NormalizationError
    │   └── ValidationError
    └── InstallationError
        ├── MediaProcessingError
        ├── GameWriterError
        └── InsufficientDiskSpaceError
```

Typed exceptions keep stage failures explicit and improve user-facing diagnostics from worker error signals.

---

## Logging Architecture

Logger namespace is rooted at `jd2021` with child loggers per subsystem (UI, workers, extractors, parsers, installers). Root setup is initialized at startup via `setup_logging()` in `main.py`.

**The logging system supports four detail profiles:**

| Profile | Console | UI Panel | File | Use Case |
|---------|---------|----------|------|----------|
| `quiet` | WARNING+ | WARNING+ | INFO+ | End users who want minimal output |
| `user` | INFO+ | INFO+ | INFO+ | Default for normal operation |
| `detailed` | INFO+ | INFO+ | DEBUG+ | Troubleshooting without console noise |
| `developer` | DEBUG+ | DEBUG+ | DEBUG+ | Full diagnostic output including names/timestamps |

Profiles are configured via `AppConfig.log_detail_level` and applied at runtime by `logging_config.apply_log_detail()`. Worker tracebacks use `log_exception_for_profile()` to show concise errors for users and full traces for debug-capable profiles.

---

## Dependency and Runtime Model

1. Runtime is Windows-first local desktop execution.
2. FFmpeg and FFprobe are required for core media conversion and preview reliability.
3. vgmstream is required for specific decode paths (including X360/XMA2-oriented scenarios).
4. Playwright Chromium runtime is required for Fetch workflows.
5. **Pillow is required for texture decoding (CKD → DDS → TGA/PNG), image conversion, and cover generation.**
6. **UnityPy is used as a fallback JDNext bundle extraction strategy** when AssetStudioModCLI is unavailable.
7. **requests is used for HTTP downloads** (asset downloads, update checker).
8. `setup.bat` is the primary bootstrap path for dependencies and helper tool setup.
9. Missing dependencies trigger fallback/degraded behavior and should be treated as first-line troubleshooting.

---

## Key Design Decisions

### Canonical Normalization Contract
All source modes converge to `NormalizedMapData` before install. This keeps downstream generation deterministic and mode-agnostic.

### JSON-First CKD Loading
CKD parsing attempts JSON first (after null-padding cleanup) and falls back to binary parsing when needed, supporting mixed modern/legacy input sets. **This dual-parse strategy is applied consistently across normalizer, tape_converter, and ambient_processor.**

### Scene Registration and Idempotence
Map registration is integrated into install flow and designed to avoid duplicate registration side-effects across repeat installs. **Registration inserts both Actor (songdesc) and CoverflowSkuSongs entries into the SkuScene ISC file.**

### Status and Version Safety Mapping
Game-facing status/version fields are mapped for runtime compatibility while preserving meaningful source metadata where possible.

### Platform Asset Preference Strategy
Platform variant selection favors compatibility with JD2021 PC integration constraints. **Scene platform preference order is `DURANGO → NX → PC → ORBIS → SCARLETT → PROSPERO → WIIU → X360`**, with JDNext `MAP_PACKAGE` bundles also supported. VP9 handling mode (`reencode_to_vp8` or `fallback_compatible_down`) controls how JDNext VP9 video variants are handled.

### **Dual-Strategy JDNext Extraction**
JDNext extraction supports two strategies (`assetstudio_first` and `unitypy_first`) with automatic fallback. AssetStudio produces structured `TextAsset`/`MonoBehaviour`/`Texture2D` directories that are then mapped into installer-compatible CKD/tape/asset layouts. UnityPy provides a broader but less structured extraction pass as a fallback.

### **Tape Conversion Pipeline**
A dedicated `tape_converter` module handles CKD → UbiArt Lua conversion for all tape types (dance, karaoke, cinematic, beats). This includes codename reference rewriting, path normalization (lowercase conventions), and picto extension normalization. The module supports both CKD source files and pre-decoded loose tapes.

### **Multi-Platform Texture Decoding**
The `texture_decoder` module handles binary CKD texture payloads across three platforms (PC/DDS, NX/XTX, X360/tiled DXT) with a unified interface. Pictogram canvas sizing and transparent background compositing are performed during decode for game-engine compatibility.

### **Ambient Sound Processing with Timing Synthesis**
Intro AMB timing is derived from multiple sources in priority order: HideUserInterfaceClip timing → existing source SoundSetClip → startBeat-based fallback. The processor generates synthetic ILU/TPL pairs, injects SoundSetClip entries into MainSequence tapes, and manages intro/body AMB separation for scene file injection.

### **Install Summary and Checklist System**
Each install produces a structured summary (`InstallSummary`) with required and optional file checklists. This enables actionable user feedback about install completeness (e.g., missing pictograms, missing preview video, missing AMB artifacts).

### **Game Directory Auto-Discovery**
`path_discovery.py` uses fast heuristics (checking `jd21/` relative paths) with fallback to a deep recursive scan (with TTL caching) for the `SkuScene_Maps_PC_All.isc` sentinel file.

### **Persistent Readjust Index**
The readjust index (`map_readjust_index.json`) stores per-map source paths, installed locations, and sync offsets. Stale entries are automatically pruned based on source/installed media availability.

### **Standalone Auto-Updater**
The `updater.py` module is intentionally decoupled from the installer package (imports only stdlib + requests) so it can function even when the main codebase is broken. It supports git-based and zip-download update strategies with user data preservation.

---

## **UI Widget Architecture**

The `ui/widgets/` package contains 15 specialized widget modules organized by function:

| Widget | File | Purpose |
|--------|------|---------|
| Mode Selector | `mode_selector.py` | Source mode tabs and input configuration (Fetch, HTML, IPK, Batch, Manual) |
| Config Panel | `config_panel.py` | Video quality selection and runtime preferences |
| Action Panel | `action_panel.py` | Install/extract action buttons and flow control |
| Preview Widget | `preview_widget.py` | Map preview display with video/audio playback |
| Sync Refinement | `sync_refinement.py` | Post-install offset adjustment controls |
| Log Console | `log_console.py` | Real-time log output with Qt log handler |
| Settings Dialog | `settings_dialog.py` | Full application settings with tabs |
| Bundle Dialog | `bundle_dialog.py` | Multi-map IPK selection dialog |
| Feedback Panel | `feedback_panel.py` | User feedback and status messaging |
| FFmpeg Dialog | `ffmpeg_dialog.py` | FFmpeg path configuration and validation |
| Metadata Dialog | `metadata_dialog.py` | Map metadata editing |
| Install Summary | `installation_summary_dialog.py` | Post-install checklist display |
| Quickstart Dialog | `quickstart_dialog.py` | First-run guided setup |
| Update Dialog | `update_dialog.py` | Update checker and branch selection UI |

---

## Deprecated or Narrow Historical Narratives

The earlier architecture narrative that framed the app as mostly HTML plus IPK ingestion is now obsolete for operational documentation.
V2 production reality is multi-mode with integrated readjust workflows, broader post-install lifecycle handling, **JDNext extraction support, multi-platform texture decoding, tape conversion pipelines, and a full install summary system.**
Any legacy docs that imply a single narrow ingestion path should be treated as historical reference only.