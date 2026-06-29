# Asset HTML Files & Media Pipeline

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This page documents the **HTML input workflow** used by JD2021 Map Installer v2 and explains how discovered assets flow through the **media processing pipeline** (`media_processor.py`). V2 also supports Fetch by codename, IPK archive mode, batch directory mode, and manual source folder installs; those modes are documented separately.

---

## Two Domains of Asset Handling

The installer processes assets in two fundamentally different ways. Understanding this distinction is essential:

| Domain | What it does | Key modules | Tools used |
|---|---|---|---|
| **Text-based asset parsing** | Reads HTML files as UTF-8 text, extracts `href` URLs via regex, categorizes links by filename pattern. No binary interpretation. | `extract_urls()`, `download_files()` | None (pure Python string/regex) |
| **Binary media encoding** | Copies, transcodes, decodes, and converts downloaded A/V and image files into formats the UbiArt engine expects. | `media_processor.py`, `texture_decoder.py`, `tape_converter.py` | FFmpeg, FFprobe, vgmstream, Pillow |

> [!IMPORTANT]
> **HTML parsing is text-only.** The parser never interprets binary content. It reads `href` attributes from Discord embed HTML, strips proxy URLs, and returns a flat list of CDN links. All binary media work happens downstream in `media_processor.py` and its companion modules.

---

## Dependency Setup

External tools required for binary media operations are configured automatically by `setup.bat` before the first run:

| Tool | Purpose | How it's provisioned |
|---|---|---|
| **FFmpeg / FFprobe** | Video transcoding (VP9→VP8), audio format conversion (OGG→WAV), probing durations, generating previews, applying gain/trim/pad | Expected on the system `PATH`. The installer defaults to `ffmpeg` / `ffprobe` commands. Install via [ffmpeg.org](https://ffmpeg.org) or a package manager. |
| **vgmstream** | Decoding Xbox 360 XMA2 audio from IPK-derived `.wav.ckd` files | Auto-installed by `setup.bat` step 7/7 into `tools/vgmstream/`. Downloaded from GitHub nightly releases. |
| **Pillow** | Image format conversion (DDS/TGA/PNG), cover art processing, pictogram canvas compositing | Installed via `pip install -r requirements.txt` (setup.bat step 1/7). |
| **Playwright Chromium** | Fetch mode only — automated browser for Discord bot interaction | Installed by `setup.bat` step 2/7 (`python -m playwright install chromium`). |

> [!NOTE]
> FFmpeg and FFprobe are **not** bundled or auto-downloaded by `setup.bat`. They must be available on the system `PATH` before running the installer. All other media tools are provisioned automatically.

---

## assets.html

### Origin

Saved from the JDHelper bot's "JDU assets" embed response for a specific map. The HTML is a raw Discord embed page — CSS class names like `embedField__623de` are from Discord's stylesheet and are not semantically meaningful; the parser ignores them and only reads `href` attributes.

### What it contains

The embed groups assets under several named sections:

| Section | Assets |
|---|---|
| **Coach portraits** | Coach 1–4 (`.tga.ckd`), Phone Coach 1–4 (`.png`) |
| **Cover images** | `coverImageUrl`, `cover_1024ImageUrl`, `cover_smallImageUrl`, `expandBkgImageUrl`, `expandCoachImageUrl`, `phoneCoverImageUrl`, `map_bkgImageUrl`, `banner_bkgImageUrl` |
| **Video Preview** | `AudioPreview.ogg`, multi-quality preview WebMs (`HIGH.vp8`, `HIGH.vp9`, `MID.vp8`, `MID.vp9`, `LOW.vp8`, `LOW.vp9`, `ULTRA.vp8`, `ULTRA.vp9`) |
| **Main Scene** | Per-platform ZIPs: `PC`, `Nintendo Switch` (`NX`), `Xbox One` (`DURANGO`), `Xbox SX` (`SCARLETT`), `PlayStation 4` (`ORBIS`), `PlayStation 5` (`PROSPERO`), `Google Stadia` (`GGP`), `Nintendo WiiU` |

### CDN URL structure (public assets)

```
https://jd-s3.cdn.ubi.com/public/map/{MapName}/{platform}/{Filename}/{hash}.{ext}
```

- `{MapName}` — the map codename (e.g. `Starships`). The installer extracts this automatically from discovered URLs.
- `{platform}` — subdirectory indicating the target platform (`pc/`, `nx/`, `ps4/`, `x1/`, `ggp/`, `wiiu/`). Absent for platform-agnostic files (cover images, phone textures, audio preview).
- `{hash}` — MD5 content hash used by the CDN for cache-busting. Ignored by the installer.

### What the pipeline downloads from assets.html

| Asset type | Action |
|---|---|
| Main Scene ZIP | Selected by platform preference: **DURANGO** → NX → SCARLETT → any available. Extracted and installed into the game directory. |
| Coach textures (`.ckd`) | Downloaded to the map's download directory and installed. |
| Cover/background images (`.ckd`, `.jpg`, `.png`) | Downloaded and installed. |
| Video/audio preview files | Partially used. Preview WebM may be copied as optional media when discovered in source files, but current generated runtime preview config still targets main NOHUD media. `AudioPreview.ogg` is not used as install audio. |

The parser collects all `href` URLs from the file, filters out Discord CDN proxy URLs (`discordapp.net`), then categorizes them by extension and filename pattern.

### Preview Integration Status (Current v2)

Dedicated preview assets in `assets.html` are **not required** for functional installs.

- Main gameplay still uses NOHUD video + NOHUD audio.
- In-game preview timing is primarily driven by `.trk` preview fields (`previewEntry`, `previewLoopStart`, `previewLoopEnd`) over main media.
- `AudioPreview.ogg` is not selected as gameplay audio.

#### Installed-output difference today

| Scenario | Installed files/config effect |
|---|---|
| No dedicated preview assets | Install is still valid. Preview uses main media + `.trk` markers. |
| Dedicated preview video exists | `<codename>_MapPreview.webm` may be copied into `VideosCoach/` as optional payload. |
| Dedicated preview audio exists | No install-path change in current v2; it is not wired into generated game config. |

#### Important implementation note

Current generated preview actor config (`video_player_map_preview.act`) still references main video/MPD paths by default. This means dedicated preview payloads are optional in practice unless future runtime wiring is added.

---

## nohud.html

### Origin

Saved from the JDHelper bot's NOHUD video embed response. Unlike `assets.html`, this embed is compact — it contains only video and audio download links with no section headers.

### What it contains

| Field label | File |
|---|---|
| `Ultra:` | `{Codename}_ULTRA.webm` |
| `Ultra HD:` | `{Codename}_ULTRA.hd.webm` |
| `High:` | `{Codename}_HIGH.webm` |
| `High HD:` | `{Codename}_HIGH.hd.webm` |
| `Mid:` | `{Codename}_MID.webm` |
| `Mid HD:` | `{Codename}_MID.hd.webm` |
| `Low:` | `{Codename}_LOW.webm` |
| `Low HD:` | `{Codename}_LOW.hd.webm` |
| `Audio:` | `{Codename}.ogg` |

### CDN URL structure (private, signed)

```
https://jdcn-switch.cdn.ubisoft.cn/private/map/{MapName}/{Filename}/{hash}.{ext}
    ?auth=exp={unix_timestamp}~acl=/private/map/{MapName}/*~hmac={signature}
```

- `exp=` — Unix timestamp after which the link is invalid.
- `acl=` — Access control scope (wildcard covers all files for this map).
- `hmac=` — HMAC signature. Altering any part of the URL invalidates the signature and results in a 403.

All 8 video tiers and the audio track share the same `auth` token in a single bot response.

### What the pipeline downloads from nohud.html

| Asset | Action |
|---|---|
| One NOHUD WebM (selected quality) | Downloaded and installed as the map's coach video. See [VIDEO.md](VIDEO.md) for quality selection and fallback logic. |
| `{Codename}.ogg` | Downloaded as the map's game audio track. |

The preferred video quality is set by the `--quality` flag (CLI) or the Video Quality dropdown (GUI). If the requested tier is not present in the HTML, the pipeline falls back through lower tiers automatically.

> **Timing note:** Even with correctly downloaded NOHUD assets, final in-game sync can vary by map/source. Use the installer's Sync/Readjust workflow for per-map correction when needed.

---

## How the parser works (text-based)

Both files are processed identically by `extract_urls()`:

1. Opens the HTML as UTF-8 text.
2. Extracts all `href="..."` values via regex.
3. Strips Discord proxy URLs (`discordapp.net`) and decodes HTML entities (`&amp;` → `&`).
4. Returns a flat list of CDN URLs.

The distinction between asset and NOHUD content is made downstream in `download_files()` by filename pattern matching: `.ogg` without `AudioPreview` = audio track, `_ULTRA.webm` / `_HIGH.hd.webm` etc. = video, `MAIN_SCENE_*.zip` = main scene, `.ckd` / `.jpg` / `.png` = textures.

> [!NOTE]
> This entire parsing stage operates on **text only**. No binary data is read, no media tools are invoked, and no format conversion occurs. The parser produces a list of URLs; all binary media work happens in the next stage.

---

## Media Processing Pipeline (binary)

After assets are downloaded, `media_processor.py` handles all binary media conversion. This module is the central hub for A/V and image operations.

### Video processing

| Function | What it does | Tools used |
|---|---|---|
| `copy_video()` | Copies a WebM to the install target. If source is VP9 and `vp9_handling_mode` is `reencode_to_vp8`, transcodes VP9→VP8 via FFmpeg with optimized encoding params (`libvpx`, `deadline=good`, `cpu-used=2`, row-mt). Otherwise, performs a byte-for-byte `shutil.copy2`. | FFmpeg (transcode path only) |
| `generate_map_preview()` | Extracts a lower-quality excerpt clip from the main video for the map selection screen. | FFmpeg |

### Audio processing

| Function | What it does | Tools used |
|---|---|---|
| `copy_audio()` | Copies or transcodes audio to the install target. OGG→WAV transcoding uses FFmpeg (`pcm_s16le`, 48 kHz). Same-format copies use `shutil.copy2`. | FFmpeg (transcode path only) |
| `convert_audio()` | Full audio conversion pipeline: extracts CKD payloads, generates menu preview OGG, produces engine-compatible 48 kHz WAV with offset trimming/padding via `a_offset`. | FFmpeg, vgmstream (for CKD/XMA2) |
| `generate_audio_preview()` | Creates a trimmed audio preview with configurable fade-out. | FFmpeg |
| `apply_audio_gain()` | Applies dB gain adjustment to an audio file in-place using atomic temp-file replacement. | FFmpeg |
| `generate_intro_amb()` | Generates intro ambient WAV for pre-roll silence coverage (currently writes silent placeholders under temporary mitigation). | FFmpeg |
| `extract_amb_clips()` | Extracts SoundSetClip audio segments from cinematic tapes with timeline-based trimming and fade-out. | FFmpeg |
| `extract_ckd_audio_v1()` | Strips 44-byte CKD header, detects RIFF/OggS magic, falls back to vgmstream for XMA2 payloads. | vgmstream, FFmpeg (fallback) |
| `decode_xma2_audio()` | Decodes Xbox 360 XMA2 audio to WAV via `vgmstream-cli`. | vgmstream |

### Image processing

| Function | What it does | Tools used |
|---|---|---|
| `convert_image()` | Converts between image formats with optional resize (Pillow LANCZOS). | Pillow |
| `generate_cover_tga()` | Converts cover art to uncompressed RGBA TGA for the game engine. | Pillow |
| `process_menu_art()` | Validates, heals, and duplicates MenuArt textures — ensures `cover_generic`/`cover_online` parity, re-saves as 32-bit RGBA TGA. | Pillow |

### CKD Audio Decode Pipeline (extract_ckd_audio_v1)

The CKD audio extraction follows a multi-stage fallback chain:

```
┌─────────────────────────────────────────────────────────────────┐
│  Input: *.wav.ckd or *.ogg.ckd                                 │
├─────────────────────────────────────────────────────────────────┤
│  1. Try vgmstream on raw CKD (for .wav.ckd only)               │
│     ├─ Success → validate 48kHz stereo → return                 │
│     └─ Wrong format → FFmpeg transcode to 48kHz stereo          │
├─────────────────────────────────────────────────────────────────┤
│  2. Strip 44-byte CKD header                                    │
│     ├─ OggS magic found → write .ogg → return                   │
│     ├─ RIFF magic found → write .wav → return                   │
│     └─ No magic → scan wider window for RIFF/OggS               │
├─────────────────────────────────────────────────────────────────┤
│  3. Proprietary payload (XMA etc.)                              │
│     ├─ vgmstream decode → validate → return                     │
│     └─ FFmpeg unknown-payload fallback → return                  │
├─────────────────────────────────────────────────────────────────┤
│  4. All decoders failed → return None (silent fallback upstream) │
└─────────────────────────────────────────────────────────────────┘
```

---

## File naming and placement

For single-map installs the filenames are arbitrary - pass them via `--asset-html` and `--nohud-html`. For **batch installs**, the installer expects this exact layout:

```
MapDownloads/
  {MapName}/
    assets.html    ← asset HTML for this map
    nohud.html     ← NOHUD HTML for this map
```

The map name is derived from the folder name if URL-based detection fails.
