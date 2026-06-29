# GUI Reference

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This document describes every element of the graphical user interface, as implemented in the PyQt6 codebase under `jd2021_installer/ui/`.

---

## Overview

- **Main class:** `MainWindow` (subclass of `QMainWindow`)
- **Toolkit:** PyQt6
- **Default minimum window size:** 1000 × 920 (configurable in Settings → Window)
- **Window title:** "JD2021PC Map Installer"
- **Entry point:** Double-click `RUN.bat`, or run `python -m jd2021_installer.main`

---

## First-Time Setup

Before using the GUI for the first time:

1. **Run `setup.bat`** from the project root. This configures your Python environment and downloads dependencies like FFmpeg.
2. **Launch with `RUN.bat`**. The installer window will appear.
3. On first launch, a **Quick-Start Guide** dialog will walk you through the basics.

---

## Window Layout

The main window is split into two side-by-side columns inside a horizontal layout.

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        JD2021PC Map Installer                              │
├──────────────────────────┬──────────────────────────────────────────────────┤
│ Left Column (~40%)       │ Right Column (~60%)                              │
│                          │                                                  │
│ ┌──────────────────────┐ │ ┌──────────────────────────────────────────────┐ │
│ │ Mode Selector        │ │ │ Preview Widget                               │ │
│ │  - Mode dropdown     │ │ │  - Video canvas                              │ │
│ │  - Mode-specific     │ │ │  - Seek slider + time display                │ │
│ │    input fields      │ │ │  - Play/Stop, -5s, +5s controls              │ │
│ └──────────────────────┘ │ └──────────────────────────────────────────────┘ │
│ ┌──────────────────────┐ │ ┌──────────────────────────────────────────────┐ │
│ │ Configuration Panel  │ │ │ Sync Refinement Widget                       │ │
│ │  - Game Directory    │ │ │  - Audio/Video offset inputs                 │ │
│ │  - Video Quality     │ │ │  - Fine-tune buttons (±1/10/100/1000 ms)     │ │
│ └──────────────────────┘ │ │  - Preview, Apply Offset                     │ │
│ ┌──────────────────────┐ │ │  - Prev Map / Next Map navigation            │ │
│ │ Action Panel         │ │ └──────────────────────────────────────────────┘ │
│ │  - Install Map       │ │ ┌──────────────────────────────────────────────┐ │
│ │  - Uninstall a Map   │ │ │ Log Console                                  │ │
│ │  - Re-adjust Offset  │ │ │  - Color-coded, read-only log output         │ │
│ │  - Reset / Settings  │ │ │  - Auto-scrolls to latest entry              │ │
│ │  - Pre-flight Check  │ │ └──────────────────────────────────────────────┘ │
│ └──────────────────────┘ │                                                  │
│ ┌──────────────────────┐ │                                                  │
│ │ Progress Panel       │ │                                                  │
│ │  - Step checklist    │ │                                                  │
│ │  - Progress bar      │ │                                                  │
│ └──────────────────────┘ │                                                  │
├──────────────────────────┴──────────────────────────────────────────────────┤
│ Status Bar ("Ready", mode changes, install state, warnings)                │
└─────────────────────────────────────────────────────────────────────────────┘
```

### Layout Details

- **Left panel** minimum width: ~380 px.
- **Right panel** minimum width: ~500 px.
- Stretch ratio defaults to roughly **4 : 6** (left : right).
- The layout is proportional — no user-facing splitter handle.

---

## Mode Selector

**Widget:** `ModeSelectorWidget` — top of the left column.

The **Mode** dropdown lets you choose how map data is provided to the installer. When you switch modes, the input area below the dropdown updates to show mode-specific fields.

### Supported Modes (7 total)

| # | Mode Label | Input Type | Source |
|---|-----------|-----------|--------|
| 1 | **Fetch JDU** | Song codename(s) | JDU via Discord bot |
| 2 | **HTML JDU** | `assets.html` + `nohud.html` | JDU saved exports |
| 3 | **Fetch JDNext** | Song codename(s) | JDNext via Discord bot |
| 4 | **HTML JDNext** | `assets.html` | JDNext saved export |
| 5 | **IPK Archive** | `.ipk` file | Xbox 360 map archives |
| 6 | **Batch (Directory)** | Folder of map candidates | Mixed sources |
| 7 | **Manual (Directory)** | Pre-extracted folder | Advanced users |

> **Tip:** Each mode shows a brief description banner above its inputs. Read the banner for important notes (e.g., link expiration warnings for HTML modes).

For detailed step-by-step usage of each mode, see [MODES_GUIDE.md](MODES_GUIDE.md).

---

## Configuration Panel

**Widget:** `ConfigWidget` — below the Mode Selector in the left column.

| Control | What It Does |
|---------|-------------|
| **Game Directory** (read-only field) | Shows the path to your JD2021 installation |
| **Browse…** button | Open a folder picker to select the game root (must contain `data` and `engine`) |
| **Video Quality** dropdown | Choose output video quality: `ULTRA_HD`, `ULTRA`, `HIGH_HD`, `HIGH`, `MID_HD`, `MID`, `LOW_HD`, `LOW` |

Configuration is persisted in `installer_settings.json` and reloaded automatically on next launch.

---

## Action Panel

**Widget:** `ActionWidget` — below the Configuration Panel in the left column.

This panel groups all primary buttons:

### Primary Row

| Button | What It Does |
|--------|-------------|
| **Install Map** | Runs the full pipeline: Extract → Normalize → Install into your game |

### Secondary Row

| Button | What It Does |
|--------|-------------|
| **Uninstall a Map** | Opens a dialog to select and remove a previously installed custom map |
| **Re-adjust Offset** | Opens a map selection dialog to enter sync refinement for already-installed maps |

### Utility Row

| Button | What It Does |
|--------|-------------|
| **Reset State** | Clears current mode inputs and temporary installer state |
| **Settings** | Opens the full Installer Settings dialog (see [Settings Guide](#settings-dialog)) |
| **Pre-flight Check** | Validates paths, mode inputs, and tool dependencies before you install |

> **Important:** Always run **Pre-flight Check** before your first install to catch missing tools or configuration problems.

---

## Progress Panel

**Widget:** `ProgressLogWidget` — bottom of the left column.

Displays:

1. **Step Checklist** — each pipeline stage is listed with a status icon:
   - ⏳ `WAITING` — not yet started
   - 🔄 `IN_PROGRESS` — currently running
   - ✅ `DONE` — completed successfully
   - ❌ `ERROR` — failed

2. **Progress Bar** — fills from 0% to 100% as the pipeline executes.

Typical checklist stages include:

- Extracting map data
- Parsing CKDs and metadata
- Normalizing assets
- Decoding XMA2 audio
- Converting audio (pad/trim)
- Generating intro AMB
- Copying video files
- Converting dance/karaoke/cinematic tapes
- Processing ambient sounds
- Decoding textures and pictograms
- Integrating move data
- Registering in SkuScene
- Finalizing offsets

---

## Preview Widget

**Widget:** `PreviewWidget` — top of the right column.

| Control | What It Does |
|---------|-------------|
| **Video Canvas** | Embedded playback area for the installed map's video |
| **Seek Slider** | Drag to jump to any point; shows current / total time |
| **-5s / +5s** | Skip backward or forward by 5 seconds |
| **Play / Stop** | Toggle video+audio playback on or off |

Use the Preview Widget to visually inspect audio/video synchronization after install.

---

## Sync Refinement Widget

**Widget:** `SyncRefinementWidget` — below the Preview Widget in the right column.

This is where you fine-tune audio and video timing after installing a map.

| Control | What It Does |
|---------|-------------|
| **Audio Offset (ms)** | Set the audio timing offset in milliseconds |
| **Video Offset (ms)** checkbox + input | Enable and set a separate video offset (optional) |
| **±1 / ±10 / ±100 / ±1000** buttons | Quickly nudge audio or video offset values |
| **Preview** | Start/stop playback with current offset values |
| **Apply Offset** | Write the current offsets to the installed map files |
| **Prev Map / Next Map** | Navigate between maps when multiple maps are loaded (batch/readjust flows) |

> **Tip:** The workflow is: adjust offsets → Preview → listen/watch → adjust more → Apply Offset when satisfied.

---

## Log Console

**Widget:** `LogConsoleWidget` — bottom of the right column.

- **Read-only** scrolling text area showing live application logs.
- Logs are **color-coded** by severity:
  - 🟢 **Green** — `SUCCESS` messages
  - 🔵 **Default** — `INFO` messages
  - 🟠 **Orange** — `WARNING` messages
  - 🔴 **Red** — `ERROR` / `CRITICAL` messages
- Auto-scrolls to the latest log entry.
- Connected to a **thread-safe Qt logging handler** so background worker output appears in real-time.

Use the Log Console to:

1. Monitor progress during install.
2. Spot warnings about missing files or expired links.
3. Diagnose errors when something goes wrong.

---

## Metadata Correction Dialog

**Widget:** `MetadataCorrectionDialog` — pops up during install if needed.

If a map's metadata (song title, artist, etc.) contains non-ASCII characters that could crash the game engine, this dialog appears and lets you:

1. **See the original value** with problematic characters highlighted.
2. **Edit or accept** a sanitized replacement.
3. Choose **Keep Original** if you believe the characters are safe, or **Apply Replacement** to use the cleaned version.

---

## Settings Dialog

**Widget:** `SettingsDialog` — opened via the **Settings** button in the Action Panel.

The dialog is organized into **tabbed sections**:

### General Tab

| Setting | What It Controls |
|---------|-----------------|
| Skip startup pre-flight checks | Disable automatic checks on launch |
| Hide post-install offset reminder | Suppress the sync refinement popup after install |
| After install cleanup | `Ask` / `Always delete` / `Keep` temp files |
| Song unlock status | `Ask` / `Force to 3 (unlocked)` / `Keep original` |
| Show pre-flight success popup | Whether passing pre-flight shows a confirmation box |
| Show installation summary popup | Show a detailed checklist at end of install |
| Show quick-start help on launch | Show beginner guide on app start |
| Log detail level | `Quiet` / `Normal` / `Detailed` / `Developer` |
| Theme | `Light` / `Dark` |

### Window Tab

| Setting | What It Controls |
|---------|-----------------|
| Enforce minimum window size | Prevent resizing below configured minimum |
| Minimum window size | Width × Height in pixels |
| Show floating window size overlay | Display dimensions while resizing |
| Enable Style Debug Mode | Add colored outlines to help with theme development |

### Media Tab

| Setting | What It Controls |
|---------|-----------------|
| Default download quality | `ULTRA_HD` through `LOW` |
| FFmpeg acceleration | `auto` / `none` |
| VP9 handling | Re-encode to VP8 or fallback to compatible quality |
| Preview source | Low-res proxy (faster) or original file |

### Advanced Tab

| Setting | What It Controls |
|---------|-----------------|
| FFmpeg / FFprobe / vgmstream paths | Override auto-detected tool locations |
| 3rd-party tools root | Root directory for third-party tools (`tools/`) |
| AssetStudio CLI path | Override `tools/AssetStudioModCLI/` path for JDNext extraction |
| Download timeout / retries / delays | Network behavior tuning |
| Fetch login & bot timeouts | How long to wait for Discord login and bot response |
| Fetch background mode | Run Playwright browser headless (no visible browser window) |
| Preview video mode | `proxy_low` (fast low-res proxy) or `original` |
| VP9 handling mode | `reencode_to_vp8` or `fallback_compatible_down` |
| Video fallback behavior | `fallback_down` or `fallback_up` when quality tier unavailable |
| AlbumCoach behavior | Multi-coach compositing: `ask`, `always_customize`, `always_default` |
| JDNext cover behavior | Cover art synthesis: `ask`, `synthesized`, `original` |
| Convert JDNext gestures | Enable JDNext→Durango gesture compilation (experimental; output quality is inconsistent; disabled by default) |
| Audio preview fade | Fade duration for preview audio |

### Integrations Tab

| Setting | What It Controls |
|---------|-----------------|
| Discord channel URL | Required for Fetch modes — paste the Discord channel URL |
| Discord bot provider | Select `sev4nty` (default) or `rama` as the bot source |
| Update In-Game Localization | Import localization JSON into game data |
| Update Song Database | Import JDNext songdb JSON |
| Install All JDU Maps | Bulk-install every map from a JDU songdb |
| Install All JDNext Maps | Bulk-install every map from a JDNext songdb |
| Clean Game Data | Remove all custom maps and installer caches |
| Clear mapDownloads | Delete the downloaded source files folder |

---

## Status Bar

The bottom status bar shows a concise one-line summary of the current state.

- **Default:** `Ready`
- **During mode change:** `Mode: Fetch JDU`
- **During install:** Progress and pipeline stage updates
- **On completion:** Success or failure summary

---

## Thread Lifecycle

All heavy operations run on background **QThread** instances to keep the UI responsive.

```
User triggers action (Install / Apply / Readjust / Uninstall)
                  │
                  ▼
1. Build a worker QObject for the task
2. Create a QThread
3. worker.moveToThread(thread)
4. Connect signals:
        - thread.started  → worker.run
        - worker.progress → progress panel
        - worker.status   → status/log handlers
        - worker.error    → error/log handlers
        - worker.finished → completion handlers + thread.quit
5. Lock interactive UI areas while active
6. thread.start()
                  │
                  ▼
7. Worker runs task off the UI thread
8. Finished/error signals marshal back to the main thread
9. UI unlock + status refresh + thread cleanup
```

**Key guarantee:** Workers never update Qt widgets directly. All UI updates flow through Qt signal-slot connections.

---

## API Methods (MainWindow Compatibility Surface)

| Method | Purpose |
|--------|---------|
| `append_log(text: str)` | Append text to the GUI console through logger routing |
| `set_progress(value: int)` | Update progress bar value via progress panel |
| `set_status(text: str)` | Update status bar message |

---

## Related Docs

- [Usage Guide](USAGE_GUIDE.md) — Full beginner-to-advanced walkthrough
- [Modes Guide](MODES_GUIDE.md) — Detailed guide per mode
- [Troubleshooting](TROUBLESHOOTING.md)
- [Pipeline Reference](../02_core/PIPELINE_REFERENCE.md)
