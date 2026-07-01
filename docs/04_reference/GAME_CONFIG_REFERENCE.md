# Game Configuration Reference

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This document maps the configuration files inside the JD2021 PC game data directory and the V2 installer's own configuration surfaces. It is a reference for understanding what each file controls and which files are relevant to modding.

This replaces the previous `JD21_Configuration_Map.md`.

Scope note for V2: this document covers game/runtime configuration surfaces, installer-generated config artifacts, and the `AppConfig` parameters that control pipeline behavior across all modes (Fetch JDU, Fetch JDNext, HTML JDU, HTML JDNext, IPK, Batch, Manual).

---

## 1. Top-Level Configuration

| File | Format | Purpose |
|------|--------|---------| 
| `jd21/data/config.xml` | XML | Main game config: resolution (1280x720), fullscreen, safe frame, XInput |
| `jd21/engine/RUNNER.bat` | BAT | Startup parameters: resolution override (1920x1080), language, debug flags |
| `jd21/engine/projectinfo.xml` | XML | Project ID (JD2021_TU1), branch info |
| `jd21/engine/bloomberg_settings.ini` | INI | Crash reporting servers, logging level, build numbers |

---

## 2. Installer-Generated Configuration

These files are created by the installer pipeline, not part of the base game.

| File | Format | Purpose |
|------|--------|---------| 
| `installer_paths.json` | JSON | Cached game-data directory paths and SkuScene reference |
| `installer_settings.json` | JSON | Global installer settings: default_quality, a_offset, v_override, marker_preroll_ms |
| `map_readjust_index.json` | JSON | Persistent readjust index linking installed maps to source roots for post-install offset workflows |

---

## 3. V2 AppConfig Parameters (`core/config.py`)

The `AppConfig` class (Pydantic `BaseModel`) defines all runtime parameters for the installer. Environment variables with the `JD2021_` prefix or a `.env` file can override defaults.

### Paths

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `game_directory` | `Path?` | `None` | JD2021 PC game data root (set by user or auto-detected) |
| `download_root` | `Path` | `./mapDownloads` | Root for downloaded/cached map data |
| `cache_directory` | `Path` | `./cache` | General cache directory |
| `temp_directory` | `Path` | `./temp` | Temporary working directory for extraction |
| `app_icon_path` | `Path` | `./assets/app_icon.jpg` | Application icon for UI window |
| `gesture_template_path` | `Path` | `./assets/gesture_templates/durango_template.gesture` | Durango Kinect gesture template used as structural donor for JDNext gesture compilation |

### Video Quality

| Parameter | Type | Default | Allowed Values |
|-----------|------|---------|----------------|
| `video_quality` | `str` | `ULTRA_HD` | `ULTRA_HD`, `ULTRA`, `HIGH_HD`, `HIGH`, `MID_HD`, `MID`, `LOW_HD`, `LOW` |

Quality is resolved by descending fallback — if `ULTRA_HD` is unavailable, the pipeline tries `ULTRA`, then `HIGH_HD`, and so on. Quality patterns map to filename suffixes:

| Tier | Filename Suffix |
|------|-----------------|
| `ULTRA_HD` | `_ULTRA.hd.webm` |
| `ULTRA` | `_ULTRA.webm` |
| `HIGH_HD` | `_HIGH.hd.webm` |
| `HIGH` | `_HIGH.webm` |
| `MID_HD` | `_MID.hd.webm` |
| `MID` | `_MID.webm` |
| `LOW_HD` | `_LOW.hd.webm` |
| `LOW` | `_LOW.webm` |

### UI/UX Settings

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `skip_preflight` | `bool` | `false` | Skip pre-install validation checks |
| `suppress_offset_notification` | `bool` | `false` | Suppress audio/video offset notifications |
| `cleanup_behavior` | `str` | `ask` | Post-install cleanup: `ask`, `delete`, `keep`, or `aggressive` |
| `locked_status_behavior` | `str` | `ask` | Locked map status handling: `ask`, `force3`, or `keep` |
| `show_preflight_success_popup` | `bool` | `true` | Show popup on successful preflight |
| `show_install_summary_popup` | `bool` | `true` | Show summary dialog after install |
| `show_quickstart_on_launch` | `bool` | `true` | Show quickstart dialog on first launch |
| `skip_quickstart` | `bool` | `false` | Permanently skip quickstart |
| `log_detail_level` | `str` | `user` | Log verbosity: `quiet`, `user`, `detailed`, `developer` |
| `theme` | `str` | `light` | UI theme: `light` or `dark` |
| `enforce_min_window_size` | `bool` | `true` | Enforce minimum window dimensions |
| `min_window_width` | `int` | `1000` | Minimum window width in pixels |
| `min_window_height` | `int` | `920` | Minimum window height in pixels |
| `show_window_size_overlay` | `bool` | `false` | Show debug overlay with window dimensions |
| `style_debug_mode` | `bool` | `false` | Enable CSS debug borders in UI |
| `window_size_overlay_timeout_ms` | `int` | `1100` | Overlay auto-hide timeout |
| `enable_legacy_sync_refinement` | `bool` | `false` | Enable legacy sync refinement UI mode |
| `convert_jdnext_gestures` | `bool` | `false` | Enable JDNext→Durango gesture compilation (experimental; output quality is inconsistent; requires `gesture_template_path`) |
| `albumcoach_behavior` | `str` | `ask` | Multi-coach compositing behavior: `ask`, `always_customize`, or `always_default` |
| `jdnext_cover_behavior` | `str` | `ask` | JDNext cover art synthesis behavior: `ask`, `synthesized`, or `original` |

### Download Settings

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `download_timeout_s` | `int` | `600` | Maximum download time per file (seconds) |
| `max_retries` | `int` | `3` | Maximum retry count for failed downloads |
| `retry_base_delay_s` | `int` | `2` | Base delay between retries (exponential backoff) |
| `inter_request_delay_s` | `float` | `1.5` | Delay between sequential HTTP requests (rate limiting) |
| `user_agent` | `str` | Chrome 131 UA | User-Agent header for web requests |

### UbiArt Engine Constants

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `ticks_per_ms` | `int` | `48` | Engine tick rate (ticks per millisecond) |
| `max_jd_version` | `int` | `2021` | Maximum supported Just Dance version |
| `min_jd_version` | `int` | `2014` | Minimum supported Just Dance version |
| `preview_fps` | `int` | `25` | Frame rate for preview rendering |
| `preview_only_audio_offset_ms` | `float` | `0.0` | Audio offset applied in preview-only mode (ms) |
| `audio_preview_fade_s` | `float` | `2.0` | Fade duration for audio preview transitions |

### Discord Fetch Mode

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `discord_channel_url` | `str` | `""` | Discord channel URL for Fetch mode bot interaction |
| `discord_bot_provider` | `str` | `"sev4nty"` | Bot provider for Fetch mode: `"sev4nty"` or `"rama"` |
| `browser_profile_dir` | `Path` | `./.browser-profile` | Persistent browser profile for Playwright sessions |
| `fetch_login_timeout_s` | `int` | `300` | Maximum time to wait for Discord login (seconds) |
| `fetch_bot_response_timeout_s` | `int` | `60` | Maximum time to wait for bot response (seconds) |
| `fetch_background_mode` | `bool` | `false` | Run Playwright browser headless (no visible browser window) |

### Update Checker

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `check_updates_on_launch` | `bool` | `true` | Silently check for updates on application start |
| `update_branch` | `str` | `""` | Branch to track for updates (empty = auto-detect from git or state file) |

### External Tool Paths

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `ffmpeg_path` | `str` | `ffmpeg` | Path to FFmpeg executable |
| `ffprobe_path` | `str` | `ffprobe` | Path to FFprobe executable |
| `vgmstream_path` | `str?` | `None` | Path to vgmstream CLI (optional; for console audio decode) |
| `assetstudio_cli_path` | `str?` | `None` | Path to AssetStudioModCLI.exe for JDNext bundle extraction |
| `third_party_tools_root` | `Path?` | `./tools` | Root directory for third-party tools (AssetStudio, UnityPy, etc.) |
| `ffmpeg_hwaccel` | `str` | `auto` | Hardware acceleration mode: `auto` or `none` |
| `vp9_handling_mode` | `str` | `fallback_compatible_down` | VP9 video handling: `reencode_to_vp8` or `fallback_compatible_down` |
| `preview_video_mode` | `str` | `original` | Preview video source: `proxy_low` or `original` |
| `video_fallback_behavior` | `str` | `fallback_down` | How missing/incompatible quality tiers fall back: `fallback_down` or `fallback_up` |
| `jdlo_auth_path` | `Path?` | `None` | Path to JDLO authentication token file (required for Fetch JDLO mode) |

### Scene Platform Preference

The `SCENE_PLATFORM_PREFERENCE` constant defines the priority order for resolving platform-specific scene files:

```python
SCENE_PLATFORM_PREFERENCE = ["DURANGO", "NX", "PC", "ORBIS", "SCARLETT", "PROSPERO", "WIIU", "X360"]
```

---

## 4. EngineData/GameConfig (Lua Files)

All `.isg` and `.ilu` files are plain-text Lua table syntax, directly editable with any text editor. `gameconfig.isg` is the master hub that includes all subsystem configs.

### Core Gameplay (.isg files)

| File | Controls |
|------|----------|
| `gameconfig.isg` | Master config: includes all subsystem configs |
| `quickplayrules.isg` | Difficulty sequencing, song selection rules, coach matching |
| `achievements.isg` | 35 achievements across all platforms |
| `soundconfig.isg` | Full audio bus system (25+ buses), ducking, limiters |
| `vibration.isg` | 28 HD Rumble effects for controllers |
| `padrumbleconfig.isg` | Light/Heavy/Medium rumble presets with durations |
| `camerashakeconfig.isg` | Camera shake effects |
| `objectives.isg` | Progression objective definitions |
| `scheduledquests.isg` | Daily/weekly quest system |
| `ftuesteps.isg` | First-time user experience / onboarding |
| `carousel.isg` | 300+ UI carousel item definitions |
| `gachacontent.isg` | Gift machine / loot system content |
| `fonteffectpreset.isg` | Font rendering effects |
| `portrait_borders.isg` | Player portrait border customization |
| `localisationTRC.isg` | Localization and content rating compliance |
| `zinput.isg` | Input/controller configuration |

### Feature Configs (.ilu files)

#### Scoring and Progression

| File | Controls |
|------|----------|
| `gc_scoring.ilu` | Scoring parameters: max 13,333 pts, on-fire multiplier, kids mode |
| `gc_scoring_camera.ilu` | Camera-based scoring mechanics |
| `gc_scoring_movespace.ilu` | Movement space scoring |
| `gc_jdrank_data.ilu` | 200-level rank system with XP thresholds per level |
| `gc_coop.ilu` | Coop scoring diamond multipliers (0.187 - 3.0) |
| `gc_rating.ilu` | Age rating screens (ESRB, PEGI) |
| `gacha_config.ilu` | Gift machine: 100 pt cost, rarity, thresholds |
| `aliasesObjectives.ilu` | Alias/title unlock conditions |
| `mapsObjectives.ilu` | Map unlock objectives (relevant to Status override) |

#### UI and Menus

| File | Controls |
|------|----------|
| `gc_hud_ui.ilu` | In-game HUD: players, raceline, pictoline, lyrics, gold moves |
| `gc_common_ui.ilu` | Common UI screen layouts |
| `gc_menuassets.ilu` | Menu asset database |
| `gc_menumusics.ilu` | Menu background music |
| `cr_core_navigation.ilu` | Core menu navigation structure |
| `cr_game_mode_selection.ilu` | Game mode selector |
| `cr_lobby.ilu` | Lobby screen |
| `cr_device_selection.ilu` | Input device selection (phone, camera, motion) |
| `cr_dancer_card_edition.ilu` | Dancer card editor |
| `cr_search_symbol.ilu` | Search UI symbols |
| `popups.ilu` | Popup window configs |
| `on_fly_notifications.ilu` | In-game notification system |
| `home_data_config.ilu` | Home screen content layout |
| `home_tips_config.ilu` | Tips/help system |

#### Game Modes

| File | Controls |
|------|----------|
| `cr_kids.ilu` | Kids mode settings |
| `cr_wdf.ilu` | World Dance Floor (online) config |
| `gc_wdf_boss.ilu` | WDF boss challenge data |
| `gr_pause.ilu` | Pause menu options |
| `recap.ilu` | Score recap screen |
| `cr_recap_autodance.ilu` | AutoDance recap |
| `gc_autodance_effects.ilu` | AutoDance visual effects |
| `gc_tutorials.ilu` | Tutorial definitions |

#### Collectibles and Customization

| File | Controls |
|------|----------|
| `collectible_album.ilu` | Sticker album (6 pages, 60+ stickers) |
| `collectible_sticker_items.ilu` | Individual sticker definitions |
| `gc_customizable_item_config.ilu` | Customizable items |
| `gc_item_color_lookup.ilu` | Color palette for items |
| `gc_uplay_rewards.ilu` | Ubisoft account rewards |
| `gc_unlimited_upsell_songlist.ilu` | JD Unlimited song list |
| `gc_redeem_maps.ilu` | Redeemable map content |

---

## 5. JSON Configuration Files

| File | Controls |
|------|----------|
| `alias_db.json` | 150+ player titles/aliases with difficulty colors |
| `playlists.json` | 15 offline playlists with song assignments |
| `gc_carousel_rules.json` | Menu carousel behavior, platform-specific rules, filters |
| `wdf_linear_rewards.json` | 16 WDF online reward tiers |

---

## 6. Other EngineData Directories

| Path | Format | Purpose |
|------|--------|---------| 
| `Achievements/RewardsConfig.xml` | XML | Trophy config (48 trophies, 18 languages) |
| `Shaders/Unified/ShaderConfig.xml` | XML | Cross-platform shader compilation |
| `Localisation/Saves/ConsoleSave.json` | JSON | 17-language localization string database |
| `UserConfig/screencapture.xml` | XML | Screenshot capture settings |
| `GameToolPackageTask/*.xml` | XML | Asset packaging and build order |

---

## 7. World/Content Data

| Path | Format | Purpose |
|------|--------|---------| 
| `World/SkuScenes/SkuScene_Maps_PC_All.isc` | ISC/XML | Song database: Actor entries for each map + CoverflowSong entries |
| `World/SkuScenes/SkuScene_Maps_NX_All.isc` | ISC/XML | NX platform song database (exists but not used by pipeline) |
| `World/ui/textures/quests/unlimited/*/quest_metadata.json` | JSON | Quest definitions |

---

## 8. Key Gameplay Parameters

### Scoring
- Max song score: 13,333 points
- Star progression: 0-7 stars (Superstar at 11,000, Megastar at 12,000)
- Kids mode has special scoring ratios
- On-fire multiplier for sustained combos

### Rank Progression
- 200 levels with exponential XP scaling
- 7 visual tiers based on level brackets
- Level 1 requires 40 XP, Level 200 requires ~97,436 XP

### Audio
- 25+ audio buses
- Dynamic volume ducking during video/narration
- All game audio at 48kHz sample rate

---

## 9. Modding Notes

- All `.isg` and `.ilu` files are plain-text Lua, directly editable
- JSON and XML files are standard formats, fully editable
- Scoring parameters, rank XP curves, achievement conditions, and playlists can all be modified
- The game references PS4/PS5/Xbox/Switch/Stadia platforms across configs; only PC paths are active
- Resolution can be changed via `config.xml` or `RUNNER.bat`
- `mapsObjectives.ilu` controls map unlock conditions; the pipeline's Status override (`Status = 3`) bypasses these

---

## 10. V2 Installer Behavior and Limitations (Critical)

These points are current V2 behavior and should be treated as operational constraints when using this reference with mod installs.

### Ambient (AMB)
- Intro AMB generation is active and enabled by default (`INTRO_AMB_ATTEMPT_ENABLED = True`).
- The pipeline generates intro AMB audio for all maps with `videoStartTime < 0`. See [AUDIO_TIMING.md](../03_media/AUDIO_TIMING.md).
- AMB path resolution remains sensitive to source folder shape and casing conventions; mixed-case layouts use case-insensitive fallback resolution.

### Audio/Video Timing
- IPK-derived `videoStartTime` remains approximate by design because source metadata often does not reliably encode lead-in.
- Manual video offset tuning is expected for many IPK maps after installation.
- `installer_settings.json` timing controls (`a_offset`, `v_override`, `marker_preroll_ms`) are still the main global adjustment surface.

### Runtime Dependencies (Required for Full Functionality)
- FFmpeg/FFprobe are required for media conversion/probing paths.
- vgmstream runtime is required for X360/XMA2 decode paths.
- AssetStudioModCLI or UnityPy are required for JDNext bundle extraction.
- Missing or partial tool availability can produce degraded installs, preview limitations, or fallback behavior.
- `setup.bat` configures all mandatory dependencies automatically.

### JDNext-Specific Constraints
- JDNext bundle extraction uses a dual-strategy approach (AssetStudio → UnityPy or vice versa).
- Encrypted bundles are detected but not automatically decrypted.
- Musictrack and tape data are synthesized from Unity MonoBehaviour JSON, not read from pre-existing CKD files.
- AssetStudioModCLI resolution order: `assetstudio_cli_path` config → `tools/AssetStudioModCLI/...` → `tools/AssetStudio/...` (legacy fallback).
- Default Unity version assumption for AssetStudio: `2021.3.9f1`; UnityPy fallback version: `2021.3.0f1`.

### Pipeline Context
- V2 supports seven install input modes: Fetch JDU, HTML JDU, Fetch JDNext, HTML JDNext, IPK Archive, Batch Directory, and Manual source folder.
- Manual mode offers three source-type presets (JDU, IPK, Mixed) that control which UI fields and validation rules are active.
- This file remains valid as a config map across all modes, but mode-specific troubleshooting should be read alongside current operator/troubleshooting docs.
