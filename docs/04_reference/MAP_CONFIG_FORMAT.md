# Installer Settings Format

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This document describes the persistent user settings stored in `installer_settings.json`.

---

## Overview

The installer uses a shared JSON settings file backed by the Pydantic `AppConfig` model (`jd2021_installer/core/config.py`).

Settings are loaded at startup and can be edited from the PyQt6 **Installer Settings** dialog (`jd2021_installer/ui/widgets/settings_dialog.py`) or by editing the JSON directly.

Important scope note:
- This file name is historical (`MAP_CONFIG_FORMAT.md`) and now documents installer runtime/user settings.
- V2 is GUI-first. Earlier docs that referenced a primary CLI settings workflow are now outdated.

---

## File Location

```text
project_root/
└── installer_settings.json
```

Created automatically the first time settings are saved from the GUI.

---

## Schema

`installer_settings.json` follows `AppConfig` defaults plus user overrides.

Example (trimmed to commonly user-edited fields):

```json
{
  "game_directory": "D:/jd2021pc/jd21",
  "download_root": "mapDownloads",
  "cache_directory": "cache",
  "temp_directory": "temp",
  "video_quality": "ULTRA_HD",
  "skip_preflight": false,
  "suppress_offset_notification": false,
  "cleanup_behavior": "ask",
  "locked_status_behavior": "ask",
  "show_preflight_success_popup": true,
  "show_quickstart_on_launch": true,
  "log_detail_level": "user",
  "theme": "light",
  "enforce_min_window_size": true,
  "min_window_width": 1000,
  "min_window_height": 920,
  "show_window_size_overlay": false,
  "discord_channel_url": "",
  "ffmpeg_path": "ffmpeg",
  "ffprobe_path": "ffprobe"
}
```

### Fields

#### Core install behavior

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `game_directory` | path\|null | `null` | Target JD2021 root directory. |
| `video_quality` | string | `"ULTRA_HD"` | Default quality tier. Valid: `ULTRA_HD`, `ULTRA`, `HIGH_HD`, `HIGH`, `MID_HD`, `MID`, `LOW_HD`, `LOW`. |
| `skip_preflight` | bool | `false` | Skip pre-flight checks and allow install flow immediately. |
| `suppress_offset_notification` | bool | `false` | Suppress post-install offset refinement reminder popup. |
| `cleanup_behavior` | string | `"ask"` | Post-apply cleanup policy. Valid: `ask`, `delete`, `keep`. |
| `locked_status_behavior` | string | `"ask"` | Handling for non-3 song status during install. Valid: `ask`, `force3`, `keep`. |

#### UX and window behavior

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `show_preflight_success_popup` | bool | `true` | Show popup when pre-flight succeeds. |
| `show_quickstart_on_launch` | bool | `true` | Show quickstart guide on launch. |
| `skip_quickstart` | bool | `false` | Legacy/compat flag used by quickstart flow. |
| `log_detail_level` | string | `"user"` | Logging verbosity. Valid: `quiet`, `user`, `detailed`, `developer`. |
| `theme` | string | `"light"` | App theme. Valid: `light`, `dark`. |
| `enforce_min_window_size` | bool | `true` | Enforce configured minimum window size in main window. |
| `min_window_width` | int | `1000` | Minimum width (px) when enforcement is enabled. |
| `min_window_height` | int | `920` | Minimum height (px) when enforcement is enabled. |
| `show_window_size_overlay` | bool | `false` | Show floating size overlay while resizing. |
| `window_size_overlay_timeout_ms` | int | `1100` | Overlay hide timeout in milliseconds. |

#### Download/fetch/runtime controls

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `download_root` | path | `"./mapDownloads"` | Root folder for downloaded source media. |
| `cache_directory` | path | `"./cache"` | Cache directory used by extraction/processing paths. |
| `temp_directory` | path | `"./temp"` | Temp working directory. |
| `download_timeout_s` | int | `600` | Download timeout (seconds). |
| `max_retries` | int | `3` | Retry attempts for supported network operations. |
| `retry_base_delay_s` | int | `2` | Base retry delay (seconds). |
| `inter_request_delay_s` | float | `1.5` | Delay between selected requests. |
| `discord_channel_url` | string | `""` | Discord channel URL used by Fetch mode. |
| `browser_profile_dir` | path | `"./.browser-profile"` | Browser profile directory for Playwright login state. |
| `fetch_login_timeout_s` | int | `300` | Login wait timeout for Fetch mode. |
| `fetch_bot_response_timeout_s` | int | `60` | Bot-response timeout for Fetch mode. |
| `ffmpeg_path` | string | `"ffmpeg"` | FFmpeg executable path. |
| `ffprobe_path` | string | `"ffprobe"` | FFprobe executable path. |

---

## Where Settings Are Applied

### GUI (`jd2021_installer/ui/main_window.py` + `jd2021_installer/ui/widgets/settings_dialog.py`)
- **Startup**: Loads `installer_settings.json` into `AppConfig`, applies theme and quality defaults.
- **Pre-flight + Install UX**: `skip_preflight` and `show_preflight_success_popup` alter startup/install flow.
- **Pipeline complete**: `suppress_offset_notification` controls post-install prompt behavior.
- **Post-apply cleanup**: `cleanup_behavior` controls ask/delete/keep handling.
- **Batch status policy**: `locked_status_behavior` controls non-3 status behavior.
- **General UI**: theme, log detail, quickstart, and window sizing fields are all consumed by UI runtime.

### Batch mode (inside GUI)
- Batch is part of the same PyQt6 app mode selector and shares `AppConfig` values.
- No separate settings file exists for batch mode.

### Deprecated / outdated references
- Older docs that mention Tk/Toplevel settings UI, `default_quality`, or `auto_cleanup_downloads` are obsolete in V2.
- Older docs that describe a primary CLI settings path (for example `--quality` defaults from this file) should be considered historical unless reintroduced.

---

## Settings Persistence API

There is no standalone `load_settings()` / `save_settings()` helper module as documented in early drafts.

Current persistence is handled in `MainWindow`:

| Method | Location | Description |
|--------|----------|-------------|
| `_load_settings()` | `jd2021_installer/ui/main_window.py` | Reads `installer_settings.json`, validates through `AppConfig`, returns defaults on failure. |
| `_save_settings()` | `jd2021_installer/ui/main_window.py` | Serializes current `AppConfig` to `installer_settings.json`. |

---

## Offset Readjustment

V2 supports offset readjustment for already installed maps through the GUI readjust flow and persistent index (`map_readjust_index.json`).

### GUI flow
1. Click **Re-adjust Offset**.
2. Select maps from index or pick a source folder manually.
3. Adjust offset(s), then apply updates (single-map or batch readjust apply).

### Source requirements
Readjust generally requires source media to still exist (audio/video and/or recoverable metadata). If source files were removed after install, readjust may be unavailable for that map.

### Important behavior by source type
- Fetch/HTML-origin maps typically allow audio-focused readjust behavior.
- IPK-origin maps rely more heavily on installed/map metadata and may apply video-focused readjust behavior.

---

## Critical Runtime Notes (V2)

1. **Intro AMB generation is active and enabled by default**
  The pipeline generates active intro AMB audio for all maps with `videoStartTime < 0`. Silent intro audio is the exception (unusual source layouts), not the expected behavior. See [AUDIO_TIMING.md](../03_media/AUDIO_TIMING.md).

2. **IPK video timing often needs manual tuning**
  `videoStartTime` from IPK-derived content remains approximate in many maps; manual sync refinement is expected.

3. **Dependencies are required for full functionality**
  FFmpeg/FFprobe are required for media processing and previews, and vgmstream is required for specific decode paths (notably X360/XMA2-related flows). Missing tools can degrade behavior or block parts of the pipeline.
