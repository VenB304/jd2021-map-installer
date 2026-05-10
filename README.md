# JD2021 Map Installer v2

> **Extract, build, and install Just Dance maps into Just Dance 2021 PC — from any source.**

![Screenshot](./assets/images/tool-screenshot.png)

A Windows-first desktop application built on **PyQt6** that turns raw map assets — whether scraped from the web, unpacked from Xbox 360 IPK archives, or extracted from **Just Dance Next** Unity bundles — into fully playable JD2021 PC maps.  
No manual file wrangling required.

---

## ✨ Headline Features

| | |
|---|---|
| 🎨 **Modern PyQt6 Interface** | Dark-themed, split-panel GUI with live log output, a granular progress checklist, real-time preview, and sync-refinement tools — all on background threads so the UI never freezes. |
| 🆕 **JDNext Support** | Full pipeline for Just Dance Next maps: fetch asset pages, extract Unity bundles via AssetStudioMod, parse `.btape` text files, and convert everything to UbiArt format. |
| 🔄 **Unified Pipeline** | Every source flows through the same **Extract → Normalize → Install** pipeline, producing a canonical `NormalizedMapData` regardless of origin. |
| 📦 **Multi-Mode Ingestion** | Seven distinct input modes cover every acquisition workflow — from one-click codename fetch to granular manual file selection. |
| 🎬 **Media Processing** | Automatic video transcoding (8 quality tiers with fallback), audio conversion, XMA2 decode via vgmstream, preview generation, and image format conversion powered by FFmpeg, FFprobe, and Pillow. |

---

## 🎮 Supported Modes

| Mode | Source | What It Does |
|------|--------|-------------|
| **Fetch (Codename)** | JDU via Playwright | Enter codenames → headless Chromium scrapes JDHelper for asset & NOHUD HTML, downloads everything, and installs automatically. |
| **HTML Files** | Saved `.html` exports | Load pre-saved asset + NOHUD HTML files from JDHelper. Useful when files are already downloaded. |
| **IPK Archive** | `.ipk` file | Extracts maps from Xbox 360 IPK archives with zlib/LZMA decompression and binary CKD parsing. |
| **Batch (Directory)** | Folder of maps | Point to a directory containing any mix of IPK files, HTML exports, or pre-extracted map folders — processes them all in sequence. |
| **Manual (Directory)** | Pre-extracted files | Full granular control: pick individual audio, video, tape, and asset files by hand. Supports JDU, IPK, and mixed source layouts. |
| **Fetch JDNext** | JDNext via Playwright | Enter codenames → fetches JDNext asset pages, extracts Unity bundles,  and converts to UbiArt. |
| **HTML Files JDNext** | Saved JDNext `.html` | Same as Fetch JDNext but from pre-saved HTML. |

---

## 📋 Prerequisites

- **Windows 10/11** (64-bit)
- **Python 3.10+** with `pip`
- **Git** (for first-time setup dependency cloning)
- **Internet connection** (for Fetch modes and first-time tool downloads)

> FFmpeg, vgmstream, Playwright Chromium, and AssetStudioModCLI are all installed automatically by `setup.bat`. You can also install them manually — see [Third-Party Tools](docs/04_reference/THIRD_PARTY_TOOLS.md).

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
| `extractors/` | `BaseExtractor` ABC → `WebPlaywrightExtractor`, `ArchiveIPKExtractor`, `JDNextBundleStrategy`, `ManualExtractor` |
| `parsers/` | `normalizer` (raw → `NormalizedMapData`), `binary_ckd` (stateless binary CKD parser) |
| `installers/` | `game_writer` (UbiArt `.trk/.tpl/.act/.isc` generation), `media_processor` (FFmpeg/Pillow/vgmstream) |
| `ui/` | `MainWindow`, modular widgets, `QThread`-based pipeline workers |

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

## ⚠️ Known Limitations

- **JD2021 PC only** — installed maps target the PC development build and are not compatible with console versions.
- **IPK video offset is approximate** — Xbox 360 binary CKDs store `videoStartTime = 0.0`; the pipeline synthesizes a default from musictrack markers, but manual sync tuning may be needed.
- **JDNext extraction relies on third-party staging** — requires AssetStudioModCLI under `tools/`; `setup.bat` handles this automatically.
- **JDHelper links expire quickly** — HTML mode files must be used within ~30 minutes of export from the JDHelper Discord bot. If files are already downloaded, ignore this warning.
- **Toolchain completeness affects fidelity** — missing FFmpeg/FFprobe or vgmstream will degrade media conversion, previews, and fallback paths.

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
- **[AssetStudioMod](https://github.com/aelurum/AssetStudio)** / **AssetStudioModCLI** — Unity bundle extraction for JDNext maps
- **[Unity2UbiArt](https://github.com/Itaybl14/Unity2UbiArt)** — Unity-to-UbiArt conversion workflow
- **[UnityPy](https://github.com/K0lb3/UnityPy)** — Python Unity asset parsing for JDNext bundle inspection

Special thanks to the authors and contributors of these tools for making Just Dance modding possible.

---

## 🤖 AI Acknowledgement

This project was built with significant assistance from AI coding tools — primarily **Google Gemini**, **Claude** (Anthropic), and **Codex** (OpenAI) — used throughout development for architecture design, code generation, debugging, and documentation. Human direction, domain knowledge, and creative decisions drove the project; AI accelerated the implementation.

Transparency matters. If you're exploring the codebase, know that vibe coding helped shape it. ✌️

---

<p align="center">
  <sub>Made with 💜 (and a little 🤖) for the Just Dance modding community</sub>
</p>
