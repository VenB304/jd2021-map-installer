# Third-Party Tools

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This document lists all external tools and libraries used by the JD2021 Map Installer v2, where they are used, and what they do.

## Current V2 Notes (Important)

- **Intro AMB generation is active and enabled by default.** The pipeline generates intro AMB audio for all maps with a negative `videoStartTime`. Edge cases with unusual source layouts may produce silent placeholders, but this is the exception, not the rule. See [AUDIO_TIMING.md](../03_media/AUDIO_TIMING.md) for full details.
- **IPK video start timing is approximate by design.** Many IPK maps still require manual in-app video offset tuning after installation because binary metadata does not reliably preserve lead-in timing.
- **Runtime tools are mandatory for media-heavy workflows.** Missing FFmpeg/FFprobe or vgmstream can degrade conversion/preview behavior.
- **FFmpeg is NOT auto-installed by `setup.bat`** — it must be available on the system `PATH` separately.

---

## Python Dependencies

All Python dependencies are listed in `requirements.txt` and installed via `pip install -r requirements.txt`.

Supported runtime: Python 3.12 or newer. The current pinned and tested stack is based on Python 3.14.0.

### PyQt6

**Purpose:** GUI framework. Provides the main window, widgets, layout managers, and the `QThread` / `QObject` concurrency model used for background processing.

| Where Used | Purpose |
|------------|---------|
| `ui/main_window.py` | `QMainWindow`, `QSplitter`, `QTextEdit`, `QProgressBar`, `QStatusBar` |
| `ui/workers/pipeline_workers.py` | `QObject` workers with `pyqtSignal` for progress/status/error/finished |
| `main.py` | `QApplication` creation and event loop |

### Playwright for Python

**Purpose:** Headless browser automation. Replaces the legacy Node.js scraper for fetching JDU asset pages.

| Where Used | Purpose |
|------------|---------|
| `extractors/web_playwright.py` (`scrape_live()`) | Launches headless Chromium, navigates to JDU asset pages, extracts URLs |

Requires a one-time setup: `playwright install chromium`.

### Pydantic

**Purpose:** Data validation and settings management.

| Where Used | Purpose |
|------------|---------|
| `core/config.py` (`AppConfig`) | Validates configuration fields (paths, quality tiers, timeouts, engine constants). Supports environment variables via `env_prefix = "JD2021_"`. |

### Pillow (PIL)

**Purpose:** Image format conversion and processing.

| Where Used | Purpose |
|------------|---------|
| `installers/media_processor.py` (`convert_image()`, `generate_cover_tga()`) | Image resizing, format conversion (DDS/TGA/PNG/JPG) |

### SciPy

**Purpose:** Scientific computing for gesture biomechanics.

| Where Used | Purpose |
|------------|---------|
| `installers/biomechanics.py` | Savitzky-Golay filtering of skeleton joint trajectories (velocity/acceleration smoothing for gesture compilation) |

### pytest / pytest-qt

**Purpose:** Testing framework (development only).

| Where Used | Purpose |
|------------|---------|
| `tests/` | Unit tests for normalizer, models, and pipeline logic |
| `conftest.py` | Qt application fixture, sample data factories |

---

## System Dependencies

### FFmpeg

**Purpose:** Audio and video processing (conversion, trimming, preview generation).

| Where Used | Purpose |
|------------|---------|
| `installers/media_processor.py` (`run_ffmpeg()`) | OGG → WAV conversion, audio preview with fade-out, video preview clip |
| `installers/media_processor.py` (`copy_video()`) | Video file management |

### FFprobe

**Purpose:** Media duration detection.

| Where Used | Purpose |
|------------|---------|
| `installers/media_processor.py` (`get_video_duration()`) | Determines video duration for preview generation |

### vgmstream

**Purpose:** Decoding support for console-oriented audio formats (notably X360/XMA2 paths) used by some map sources.

| Where Used | Purpose |
|------------|---------|
| `installers/media_processor.py` | Fallback/decode path for non-OGG source audio where FFmpeg alone is insufficient |
| `setup.bat` / `tools/vgmstream/` | Runtime acquisition and local tool placement |

---

## Referenced Tools (Not Bundled)

These tools are community references. Their logic has been ported into the pipeline or they are auto-provisioned by `setup.bat`.

**`setup.bat` third-party tool provisioning:**

1. Downloads the latest `AssetStudioModCLI` release binary directly from [aelurum/AssetStudio](https://github.com/aelurum/AssetStudio) GitHub releases into **`tools/AssetStudioModCLI/`**. No git clone is performed.
2. Downloads the latest nightly `vgmstream` release from [vgmstream/vgmstream-releases](https://github.com/vgmstream/vgmstream-releases) into **`tools/vgmstream/`**.
3. Provisions portable **MinGit** (Portable Git for Windows) into `tools/git/` if no system Git is found.
4. Provisions portable **Python 3.14.0** from NuGet into `tools/python/` if no Python 3.12+ is found.

### AssetStudioMod / AssetStudioModCLI

**Source:** [github.com/aelurum/AssetStudio](https://github.com/aelurum/AssetStudio)

Used for JDNext `mapPackage` bundle extraction and asset export. `setup.bat` downloads the CLI binary automatically into **`tools/AssetStudioModCLI/`**. The path can be overridden via `assetstudio_cli_path` in Settings → Advanced.

The installer resolves `AssetStudioModCLI.exe` in this order:
1. `assetstudio_cli_path` config field
2. `tools/AssetStudioModCLI/...`
3. `tools/AssetStudio/...` (legacy fallback)

### Unity2UbiArt

**Source:** [github.com/Itaybl14/Unity2UbiArt](https://github.com/Itaybl14/Unity2UbiArt)

Used as a reference for the Unity-to-UbiArt conversion workflow and asset mapping conventions. Not cloned or used as a local toolchain host.

### UnityPy

**Source:** [github.com/K0lb3/UnityPy](https://github.com/K0lb3/UnityPy)

Pinned in `requirements.txt` (v1.25.0). Used as the Python fallback / inspection path for JDNext bundle parsing and extraction when AssetStudioModCLI is unavailable.

### JDTools by BLDS

Tape processing logic was analyzed and ported into the binary CKD parser. Contributions include:
- Cinematic curve handling
- MotionClip color conversion (`[a,r,g,b]` floats to `0xRRGGBBAA` hex)
- Ambient sound template processing

### UBIART-AMB-CUTTER by RN-JK

**Source:** [github.com/RN-JK/UBIART-AMB-CUTTER](https://github.com/RN-JK/UBIART-AMB-CUTTER)

AMB extraction algorithm used as a reference:
- Marker tick-to-millisecond formula (`markers[idx] / 48.0`)
- SoundSetClip splitting logic

This reference informs the AMB pipeline implementation in `installers/ambient_processor.py` and `installers/media_processor.py`. Intro AMB generation is now active by default.

### JustDanceTools

**Source:** [github.com/WodsonKun/JustDanceTools](https://github.com/WodsonKun/JustDanceTools)

Used for UbiArt and Just Dance specific file format understanding.

### ferris_dancing

**Source:** [github.com/Kriskras99/ferris_dancing](https://github.com/Kriskras99/ferris_dancing)

Rust-based binary CKD parser used as a reference for field order validation and format verification.

### ubiart-archive-tools

**Source:** [github.com/PartyService/ubiart-archive-tools](https://github.com/PartyService/ubiart-archive-tools)

IPK archive format reference. The extraction logic is integrated directly into `extractors/archive_ipk.py`.

---

## External Services

### Discord Bot Providers

The installer supports two configurable Discord bot providers for JDU and JDNext asset fetching. The active provider is selected via `discord_bot_provider` in `AppConfig` (`"sev4nty"` or `"rama"`; default: `"sev4nty"`).

| Provider | Setting Value | Bot |
|----------|---------------|-----|
| **Sev4nty** | `"sev4nty"` (default) | Primary provider |
| **JDHelper** | `"rama"` | [rama0dev](https://github.com/rama0dev) |

Both bots respond to Discord slash commands and return HTML exports or embedded CDN asset URLs per map:
- **Asset HTML:** URLs for CKD textures, IPK archives, OGG audio, and scene ZIPs
- **NOHUD HTML / embed:** URL for the gameplay WebM video

Links expire approximately 30 minutes after the bot responds. The active provider is configured in Settings → Integrations → Discord channel URL and bot provider selector.

### Ubisoft CDN

Asset files are hosted on Ubisoft's CDN (`jd-s3.cdn.ubi.com`). SSL certificate verification is disabled in `extractors/web_playwright.py` for compatibility with some systems.
