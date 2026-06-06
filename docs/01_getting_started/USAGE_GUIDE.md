# Usage Guide

> **Last Updated:** April 2026 | **Applies to:** JD2021 Map Installer v2

This is the primary reference for new users. It combines setup, GUI orientation, mode selection, settings, and troubleshooting into a single walkthrough.

---

## Table of Contents

- [1. Getting Started](#1-getting-started)
- [2. Main Screen Guide](#2-main-screen-guide)
- [3. Install Workflow (Recommended)](#3-install-workflow-recommended)
- [4. Modes Guide (Which mode to use)](#4-modes-guide-which-mode-to-use)
- [5. Settings Guide](#5-settings-guide)
- [6. Quick Troubleshooting](#6-quick-troubleshooting)
- [7. Suggested First Session](#7-suggested-first-session)
- [8. Related Docs](#8-related-docs)

---

## 1. Getting Started

### 1.1 First-time setup (Windows)

1. Open the project folder in File Explorer.
2. **Double-click `setup.bat`**. This will:
   - Create a Python virtual environment
   - Install required Python packages
   - Download and configure FFmpeg if not already present
   - Provision a portable Python 3.14.0 runtime if no supported Python 3.12+ interpreter is available
3. **Double-click `RUN.bat`** to start the installer.

If you prefer a terminal:

```bat
setup.bat
RUN.bat
```

Manual launch (advanced):

```bash
python -m jd2021_installer.main
```

### 1.2 Required external tools

`setup.bat` handles most of this automatically. For full media support, these tools must be available:

| Tool | Purpose | How to get it |
|------|---------|---------------|
| `ffmpeg` | Audio/video transcoding | Auto-installed by `setup.bat`, or [download manually](https://ffmpeg.org) |
| `ffprobe` | Media analysis | Included with FFmpeg |
| `vgmstream-cli` | XMA2 audio decode (Xbox 360 sources) | Place in `tools/vgmstream/` |

**Fetch modes** also require Playwright Chromium:

```bash
python -m playwright install chromium
```

**JDNext modes** require additional Unity tools under `tools/`:

| Tool | Path |
|------|------|
| AssetStudio | `tools/AssetStudio` |
| UnityPy | `tools/UnityPy` |
| AssetStudioModCLI | `tools/Unity2UbiArt/bin/AssetStudioModCLI/AssetStudioModCLI.exe` |

### 1.3 What happens on first launch

When you start the app for the first time:

1. A **Quick-Start Guide** dialog appears with the basics.
2. The app runs a **dependency health check** and offers to auto-install anything missing.
3. If `check_updates_on_launch` is enabled, a silent update check runs in the background.

### 1.4 First launch checklist

Before your first install:

1. ✅ Set **Game Directory** in the Configuration panel (left column).
2. ✅ Choose a **Mode** from the Mode dropdown.
3. ✅ Fill in mode-specific inputs (codename, file path, folder, etc.).
4. ✅ Click **Pre-flight Check** in the Action Panel.
5. ✅ Click **Install Map**.

---

## 2. Main Screen Guide

The main window has two columns and a status bar at the bottom.

### Left Column (Where you set up and launch)

#### 2.1 Mode Selector (top of left column)

The **Mode** dropdown determines how you'll provide map data to the installer. The 7 available modes are:

| Mode | What it expects |
|------|-----------------|
| **Fetch JDU** | JDU song codename(s), comma-separated |
| **HTML JDU** | JDU `assets.html` + `nohud.html` pair |
| **Fetch JDNext** | JDNext song codename(s) |
| **HTML JDNext** | JDNext `assets.html` file |
| **IPK Archive** | Xbox 360 `.ipk` file |
| **Batch (Directory)** | Folder containing multiple map candidates |
| **Manual (Directory)** | Pre-extracted folder with individual file fields |

When you change modes, the input area below the dropdown updates to show mode-specific fields. Each mode has a colored info banner describing what it does and any requirements.

#### 2.2 Configuration Panel

| Control | Purpose |
|---------|---------|
| **Game Directory** field | Shows your JD2021 installation path (read-only) |
| **Browse…** button | Open a folder picker to select the game root |
| **Video Quality** dropdown | Select from `ULTRA_HD` down to `LOW` |

> **Tip:** The game directory must be the folder that contains both `data` and `engine` subdirectories.

#### 2.3 Action Panel

Buttons are grouped into three rows:

| Button | What it does |
|--------|-------------|
| **Install Map** | Run the full extract → normalize → install pipeline |
| **Uninstall a Map** | Remove a previously installed custom map |
| **Re-adjust Offset** | Open sync refinement for installed maps |
| **Reset State** | Clear current mode inputs and temporary state |
| **Settings** | Open the Settings dialog |
| **Pre-flight Check** | Validate everything before install |

#### 2.4 Progress Panel (bottom of left column)

| Element | Purpose |
|---------|---------|
| **Step checklist** | Shows each pipeline step with status icons (⏳ waiting, 🔄 running, ✅ done, ❌ error) |
| **Progress bar** | Visual 0–100% progress indicator |

Typical checklist entries include: extract, parse, normalize, decode/convert, tape conversion, registration, and finalize offsets.

---

### Right Column (Where you monitor and refine)

#### 2.5 Preview Widget (top of right column)

| Control | Purpose |
|---------|---------|
| **Video canvas** | Shows the map's video playback |
| **Seek slider** | Drag to jump to any time; shows current/total time |
| **-5s** / **+5s** buttons | Skip backward or forward |
| **Play/Stop** button | Toggle playback |

Use this panel to visually inspect audio/video sync.

#### 2.6 Sync Refinement Widget

| Control | Purpose |
|---------|---------|
| **Audio Offset (ms)** | Adjust audio timing |
| **Video Offset (ms)** | Optional separate video timing adjustment (checkbox to enable) |
| **±1 / ±10 / ±100 / ±1000 buttons** | Quick-nudge offset values |
| **Preview** | Play with current offsets |
| **Apply Offset** | Save offset changes to installed files |
| **Prev Map / Next Map** | Navigate between maps in multi-map flows |

#### 2.7 Log Console (bottom of right column)

Read-only live log output with **color-coded** severity:

- 🟢 Green — Success
- Default — Info
- 🟠 Orange — Warning
- 🔴 Red — Error / Critical

Use the Log Console for:

1. Tracking progress details during install
2. Catching warnings about missing files or expired links
3. Diagnosing dependency or input failures

#### 2.8 Status Bar (bottom of window)

One-line status message. Default: `Ready`. Updates during mode changes, installs, and errors.

---

## 3. Install Workflow (Recommended)

For the best results, follow this order:

1. **Set Game Directory** in the Configuration panel.
2. **Select Mode** from the dropdown.
3. **Fill all required inputs** for that mode.
4. **Run Pre-flight Check** — fix any issues it reports in the Log Console.
5. **Click Install Map**.
6. **Watch Progress Panel** (left) and **Log Console** (right).
7. **Test in game.**
8. If timing is off, click **Re-adjust Offset** and use Sync Refinement.

---

## 4. Modes Guide (Which mode to use)

### 4.1 Fetch JDU

- **Use when:** You know the JDU codename and want fully automated install.
- **Input:** One or more codenames, comma-separated (e.g., `RainOnMe, DontStartNow`).
- **Requires:** Internet + Playwright Chromium + Discord Channel URL in Settings.

### 4.2 HTML JDU

- **Use when:** You already have `assets.html` and `nohud.html` from a JDU bot export.
- **Input:** Browse for Asset HTML, then NOHUD HTML (auto-detection tries to find the pair).
- **Warning:** Bot links expire after ~30 minutes. If downloads fail, re-export.

### 4.3 Fetch JDNext

- **Use when:** You know the JDNext codename and want automated install.
- **Input:** One or more JDNext codenames (e.g., `TelephoneALT`).
- **Requires:** Internet + Playwright Chromium + Discord Channel URL + Unity tools under `tools/`.

### 4.4 HTML JDNext

- **Use when:** You have a saved JDNext bot HTML export.
- **Input:** Browse for the Asset HTML file (only one file needed — no NOHUD).
- **Requires:** Unity tools under `tools/`.
- **Warning:** Asset links expire after ~30 minutes.

### 4.5 IPK Archive

- **Use when:** You have an Xbox 360 `.ipk` archive.
- **Input:** Browse and select the `.ipk` file.
- **Special:** Bundle IPKs with multiple maps open a selection dialog.
- **Note:** Video timing may need manual refinement — this is expected.

### 4.6 Batch (Directory)

- **Use when:** Processing many candidates at once.
- **Input:** Browse and select the root folder with map subfolders.
- **Accepted sources:** `.ipk` files, folders with HTML pairs, extracted map folders.
- **Tip:** Retry failed maps individually using the most suitable mode.

### 4.7 Manual (Directory)

- **Use when:** You have pre-extracted files and want full manual control.
- **Input:** Root folder, plus individual file fields for audio, video, musictrack, tapes, asset folders, and MenuArt.

Top controls:

| Field | Purpose |
|-------|---------|
| Source Type | `JDU` / `IPK` / `Mixed` — controls which field groups are shown |
| Root Folder | Base directory for the map source |
| Scan | Re-scan folder to auto-detect files |
| Codename | Auto-detected or manually entered |

Required files: Audio, Video, Musictrack. Optional: Songdesc, Dance Tape, Karaoke Tape, Mainseq Tape, asset folders, and JDU MenuArt images.

---

## 5. Settings Guide

Open using the **Settings** button in the Action Panel. The dialog has five tabs:

### 5.1 General Tab

| Setting | Options |
|---------|---------|
| Skip startup pre-flight checks | On / Off |
| Hide post-install offset reminder | On / Off |
| After install cleanup | Ask / Always delete / Keep |
| Song unlock status | Ask / Force to 3 (unlocked) / Keep |
| Show pre-flight success popup | On / Off |
| Show installation summary popup | On / Off |
| Show quick-start help on launch | On / Off |
| Log detail level | Quiet / Normal / Detailed / Developer |
| Theme | Light / Dark |

### 5.2 Window Tab

| Setting | Options |
|---------|---------|
| Enforce minimum window size | On / Off |
| Minimum window size | Width × Height in px |
| Show floating window size overlay | On / Off |
| Enable Style Debug Mode | On / Off (for theme developers) |

### 5.3 Media Tab

| Setting | Options |
|---------|---------|
| Default download quality | ULTRA_HD through LOW |
| FFmpeg acceleration | auto / none |
| VP9 handling | Re-encode to VP8 / Use next compatible quality |
| Preview source | Low-res proxy / Original file |

### 5.4 Advanced Tab

| Setting | What it controls |
|---------|-----------------|
| FFmpeg executable | Path override for ffmpeg |
| FFprobe executable | Path override for ffprobe |
| vgmstream executable | Path override for vgmstream-cli |
| 3rd-party tools root | Root dir for JDNext tools |
| AssetStudio CLI | Path override for AssetStudioModCLI |
| Download timeout / retries / delays | Network behavior |
| Fetch login timeout | Discord login wait time |
| Fetch bot response timeout | Bot link wait time |
| Window size overlay timeout | Overlay display duration |
| Preview FPS / startup compensation | Preview playback tuning |
| Audio-only preview offset | Nudge for audio-only preview |
| Audio preview fade | Fade-out duration |

### 5.5 Integrations Tab

| Setting | Purpose |
|---------|---------|
| Discord Channel URL | Required for Fetch modes (paste from browser) |
| Update In-Game Localization | Import a JSON file to update game locale |
| Update Song Database | Import JDNext songdb JSON |
| Install All JDU Maps | Bulk-install from JDU songdb JSON |
| Install All JDNext Maps | Bulk-install from JDNext songdb JSON |
| Clean Game Data | Remove all custom maps and caches |
| Clear mapDownloads | Delete downloaded source files |

---

## 6. Quick Troubleshooting

| Symptom | What to check |
|---------|---------------|
| Install button stays disabled | Run Pre-flight Check and review errors in Log Console |
| Fetch mode fails | Confirm Discord Channel URL in Settings and Playwright Chromium install |
| Audio/video out of sync | Use Re-adjust Offset → Preview → Apply Offset |
| Missing media output | Verify ffmpeg, ffprobe, vgmstream-cli availability |
| Batch failures | Re-run failed maps individually in best-fit mode |
| JDNext mode fails | Confirm Unity tools exist under `tools/` |
| Metadata encoding dialog appears | Decide whether to keep or replace non-ASCII characters |
| App won't start | Re-run `setup.bat`, then `RUN.bat` |

---

## 7. Suggested First Session

If you've never used this tool before:

1. **Start with one map** you know works.
2. Use **Fetch JDU** or **Fetch JDNext** mode — these are the simplest.
3. Run **Pre-flight Check** before every install.
4. Complete one successful install end-to-end and test it in-game.
5. Once comfortable, try **Batch**, **Manual**, or **IPK Archive** modes.

---

## 8. Related Docs

1. [Modes Guide](MODES_GUIDE.md) — Detailed per-mode documentation
2. [GUI Reference](GUI_REFERENCE.md) — Technical widget reference
3. [Troubleshooting](TROUBLESHOOTING.md)
4. [Audio Timing](../03_media/AUDIO_TIMING.md)
5. [Pipeline Reference](../02_core/PIPELINE_REFERENCE.md)
