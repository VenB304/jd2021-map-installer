# Third-Party Tools

> **Last Updated:** April 2026 | **Applies to:** JD2021 Map Installer v2

This document lists all external tools and libraries used by the JD2021 Map Installer v2, where they are used, and what they do.

## Current V2 Limitations (Important)

- **Intro AMB behavior is currently under temporary mitigation.** Intro AMB attempts are intentionally disabled in current V2 builds, and silent intro placeholders are expected until the AMB redesign/parity work is completed.
- **IPK video start timing is approximate by design.** Many IPK maps still require manual in-app video offset tuning after installation because binary metadata does not reliably preserve lead-in timing.
- **Runtime tools are mandatory for media-heavy workflows.** Missing FFmpeg/FFprobe or vgmstream can degrade conversion/preview behavior.

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

These tools were used as references during development. Their logic has been ported into the pipeline.

Setup bootstrap behavior for JDNext support:

1. `setup.bat` clones/upgrades source trees into `tools/AssetStudio`, `tools/UnityPy`, and `tools/Unity2UbiArt`.
2. `setup.bat` does not download the `AssetStudioModCLI` runtime bundle.
3. JDNext extraction resolves `AssetStudioModCLI.exe` from local `tools` paths only.

### AssetStudioMod / AssetStudioModCLI

**Source:** [github.com/aelurum/AssetStudio](https://github.com/aelurum/AssetStudio)

Used for JDNext `mapPackage` bundle extraction and asset export. The CLI binary is staged locally under `tools/Unity2UbiArt/bin/AssetStudioModCLI/` on machines that have the extracted toolchain.

### Unity2UbiArt

**Source:** [github.com/Itaybl14/Unity2UbiArt](https://github.com/Itaybl14/Unity2UbiArt)

Used as the local conversion toolchain that hosts the AssetStudioModCLI runtime bundle.

### UnityPy

**Source:** [github.com/K0lb3/UnityPy](https://github.com/K0lb3/UnityPy)

Used as the Python fallback / inspection path for JDNext bundle parsing and extraction.

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

Note for current V2 behavior: while this reference informs the AMB pipeline implementation, intro AMB playback remains temporarily disabled in active builds (see limitations section above).

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

### JDHelper Discord Bot

**Author:** [rama0dev](https://github.com/rama0dev)

Not a code dependency, but the primary source of JDU asset data. The bot provides two HTML exports per map:
- **Asset HTML:** URLs for CKD textures, IPK archives, OGG audio, and scene ZIPs
- **NOHUD HTML:** URL for the gameplay WebM video

Links expire approximately 30 minutes after the bot responds.

### Ubisoft CDN

Asset files are hosted on Ubisoft's CDN (`jd-s3.cdn.ubi.com`). SSL certificate verification is disabled in `extractors/web_playwright.py` for compatibility with some systems.
