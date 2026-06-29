# JD2021 Map Installer v2

> **Extract, build, and install Just Dance maps into Just Dance 2021 PC — from any source.**

![Screenshot](./assets/images/tool-screenshot.png)

A Windows-first desktop application built on **PyQt6** that turns raw map assets — whether scraped from the web, unpacked from IPK archives, or extracted from **Just Dance Next** Unity bundles — into fully playable JD2021 PC maps.  
No manual file wrangling required.

---

## ✨ Headline Features

| | |
|---|---|
| 🎨 **Modern Interface** | A dark-themed GUI that provides a real-time progress checklist and log output while keeping the application responsive during downloads and extraction. |
| 🌍 **Multi-Source Support** | Install maps from Just Dance Unlimited, Just Dance Next, JDLO, or IPK archives, converting them to a compatible format for JD2021 PC. |
| 🎬 **Media Processing** | Automatically transcodes videos, converts image formats, generates menu previews, and applies audio gain adjustments to match the base game's volume. |
| 📝 **Localization** | Updates the game's `.loc8` files so that song titles, artists, and coach names display correctly in the menus. |
| 📦 **Batch Processing** | Select a directory of files or maps to process and install multiple songs in sequence. |

---

## 🎮 Supported Modes

| Mode | What you provide | What the app does |
|------|------------------|-------------------|
| **Fetch (Codename)** | A map codename | Downloads and installs the map from Just Dance Unlimited. |
| **Fetch JDNext** | A map codename | Downloads the map from Just Dance Next and converts it to UbiArt format. |
| **Fetch JDLO** | A map codename | Downloads and installs the map from the Just Dance Legacy Online (JDLO) servers. |
| **HTML Files** | Saved `.html` pages | Map source files that are already downloaded from Fetch Modes. Allows you to install maps without having to Fetch again. |
| **IPK Archive** | An `.ipk` file | Extracts and installs a map from an IPK archive. |
| **Batch (Directory)** | A folder of maps | Processes and installs multiple maps or map folders found in a single directory. |
| **Manual (Directory)** | Extracted files | Allows you to manually select specific audio, video, and data files to build a map. |

---

## 📋 Prerequisites

- **Windows 10/11** (64-bit)
- **Python 3.12+** with `pip` (current tested stack: Python 3.14.0)
- **Internet connection** (for Fetch modes and first-time tool downloads)
- **FFmpeg / FFprobe** on system `PATH` (required for all media processing)

> `setup.bat` handles all other dependencies automatically: it provisions a portable **Python 3.14.0** runtime from NuGet if no supported interpreter is found, portable **MinGit** if no Git is found, and downloads prebuilt **vgmstream** and **AssetStudioModCLI** binaries directly into `tools/`. Playwright Chromium is also installed automatically. See [Third-Party Tools](docs/04_reference/THIRD_PARTY_TOOLS.md) for details.

---

## 🚀 Quick Start

```bash
# 1. Clone the repository
git clone https://github.com/VenB304/jd2021-map-installer.git
cd jd2021-map-installer

# 2. First-time setup — installs Python deps, Playwright, and third-party tools
setup.bat

# 3. Launch the installer
RUN.bat
```

That's it. The GUI opens, pick a mode, and start installing maps.

> For a detailed walkthrough (manual Python setup, configuration, and advanced usage), see **[Getting Started](docs/01_getting_started/GETTING_STARTED.md)**.

---

## 🏗️ Architecture at a Glance

```
                ┌──────────────┐
  User Input ──►│  Extractor   │  WebPlaywright / ArchiveIPK / JDNext / Manual
                └──────┬───────┘
                       │  raw files + metadata
                ┌──────▼───────┐
                │  Normalizer  │  Parses CKDs (binary) → NormalizedMapData
                └──────┬───────┘
                       │  canonical dataclass
                ┌──────▼───────┐
                │  Installer   │  GameWriter (UbiArt scene gen) + MediaProcessor
                └──────┬───────┘
                       │
                 JD2021 PC Maps/
```

| Package | Role |
|---------|------|
| `core/` | Data models (`NormalizedMapData`, tapes, clips), Pydantic `AppConfig`, theming, and typed exceptions |
| `extractors/` | `BaseExtractor` ABC → `WebPlaywrightExtractor` (JDU/JDNext/JDLO, dual-provider Discord bot), `ArchiveIPKExtractor`, `JDNextBundleStrategy`, `ManualExtractor` |
| `parsers/` | `normalizer` (raw → `NormalizedMapData`), `binary_ckd` (stateless binary CKD parser) |
| `installers/` | `game_writer` (UbiArt `.trk/.tpl/.act/.isc` generation), `media_processor` (FFmpeg/Pillow/vgmstream), `gesture_compiler` + `biomechanics` + `hmm_generator` (JDNext→Durango Kinect gesture pipeline) |
| `ui/` | `MainWindow`, modular widgets (including `albumcoach_dialog` for multi-coach compositing), `QThread`-based pipeline workers |

> For the full architectural deep-dive, see **[Architecture](docs/02_core/ARCHITECTURE.md)** and **[Pipeline Reference](docs/02_core/PIPELINE_REFERENCE.md)**.

---

## 📖 Documentation

All documentation lives in the [`docs/`](docs/README.md) folder:

### Getting Started
- **[Getting Started](docs/01_getting_started/GETTING_STARTED.md)** — Dependencies, setup, and first run
- **[Usage Guide](docs/01_getting_started/USAGE_GUIDE.md)** — Beginner-friendly walkthrough of the GUI, settings, and all modes
- **[Modes Guide](docs/01_getting_started/MODES_GUIDE.md)** — In-depth instructions for every mode
- **[GUI Reference](docs/01_getting_started/GUI_REFERENCE.md)** — Window layout, controls, and thread lifecycle
- **[Troubleshooting](docs/01_getting_started/TROUBLESHOOTING.md)** — Common errors and solutions

### Architecture & Internals
- **[Architecture](docs/02_core/ARCHITECTURE.md)** — Component map, concurrency model, and data flow
- **[Pipeline Reference](docs/02_core/PIPELINE_REFERENCE.md)** — Extract → Normalize → Install phases
- **[Data Formats](docs/02_core/DATA_FORMATS.md)** — Binary CKD, IPK, ISC, TRK, TPL file formats
- **[Data Mapping](docs/02_core/DATA_MAPPING.md)** — JDU JSON ↔ JD2021 field mapping

### Media & Timing
- **[Audio Timing & Pre-Roll](docs/03_media/AUDIO_TIMING.md)** — `videoStartTime` synchronization model
- **[Video Reference](docs/03_media/VIDEO.md)** — Quality tiers, fallback behavior, and download
- **[Asset HTML Files](docs/03_media/ASSETS.md)** — Format of `assets.html` and `nohud.html`

### Reference & Guides
- **[Manual JDU Porting](docs/05_guides/MANUAL_JDU_PORTING_GUIDE.md)** — Step-by-step manual JDU map porting
- **[Manual IPK Porting](docs/05_guides/MANUAL_IPK_PORTING_GUIDE.md)** — Step-by-step manual IPK map porting
- **[Third-Party Tools](docs/04_reference/THIRD_PARTY_TOOLS.md)** — External dependencies and community tools

---

## 🙏 Credits

This project builds on the work of the Just Dance modding community:

- **[JustDanceTools](https://github.com/WodsonKun/JustDanceTools)** — Binary CKD format reference and audio crop formula validation
- **[XTX-Extractor](https://github.com/aboood40091/XTX-Extractor)** — Switch XTX texture extraction
- **[ubiart-archive-tools](https://github.com/PartyService/ubiart-archive-tools)** — IPK archive format reference
- **JDTools by BLDS** — Tape processing analysis, vgmstream for XMA2 audio decoding
- **[ferris_dancing](https://github.com/Kriskras99/ferris_dancing)** — Rust CKD parser used as field-order validation reference
- **[UBIART-AMB-CUTTER](https://github.com/RN-JK/UBIART-AMB-CUTTER)** — AMB extraction algorithm reference
- **Just Dance Helper** — JDU asset and NOHUD video provider via Discord, built by [rama0dev](https://github.com/rama0dev)
- **Sev4nty** — Primary JDU/JDNext Discord bot provider for asset extraction
- **[AssetStudioMod](https://github.com/aelurum/AssetStudio)** / **AssetStudioModCLI** — Unity bundle extraction for JDNext maps (auto-downloaded by `setup.bat`)
- **[Unity2UbiArt](https://github.com/Itaybl14/Unity2UbiArt)** — Unity-to-UbiArt conversion workflow (reference source)
- **[UnityPy](https://github.com/K0lb3/UnityPy)** — Python Unity asset parsing for JDNext bundle inspection (fallback extractor, pinned in `requirements.txt`)
- **[JDLO](https://jdlo.ovosimpatico.com/)** — This installer pulls maps from the Just Dance Legacy Online (JDLO) CDN. Special Thanks to Ovo and the JDLO Team for making this integration possible.
- **[OpenParty](https://github.com/ibratabian17/openparty)** — Community-driven independent Just Dance Unlimited server alternative.
- **[ubiart-loc8-converter](https://github.com/wukko/ubiart-loc8-converter)** — UbiArt localization file converter used for decompressing and patching `.loc8` files.

Special thanks to the authors and contributors of these tools for making Just Dance modding possible.

---

## 🤖 AI Acknowledgement

This project was built with significant assistance from AI coding tools — primarily **Google Gemini**, **Claude** (Anthropic), and **Codex** (OpenAI) — used throughout development for architecture design, code generation, debugging, and documentation. Human direction, domain knowledge, and creative decisions drove the project; AI accelerated the implementation.

Transparency matters. If you're exploring the codebase, know that vibe coding helped shape it. ✌️

---

<p align="center">
  <sub>Made with 💜 (and a little 🤖) for the Just Dance modding community</sub>
</p>
