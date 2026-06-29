# Troubleshooting Guide - JD2021 Map Installer v2

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This guide covers common errors and their solutions, derived from the error handling and logging throughout the V2 codebase.

> **Intro AMB generation is active and enabled by default.**
> The pipeline generates intro AMB audio for all maps with `videoStartTime < 0`. Silent intro audio is now the exception (unusual source layouts) rather than expected behavior. See [AUDIO_TIMING.md](../03_media/AUDIO_TIMING.md) for details.

---

## Download Errors

- **HTTP 403/404 during download**: CDN links expired. Solution: get fresh HTML files from JDHelper. Links expire ~30 minutes after bot response.
- **HTTP 429 (Too Many Requests)**: CDN rate limiting. Pipeline has built-in retry with `Retry-After` header and exponential backoff. If persistent, wait a few minutes.
- **"Full Audio missing"**: OGG not found after download. Usually means NOHUD HTML links expired. Get fresh links.
- **"Full Video missing"**: WebM not found. Same cause as above.
- **Network timeouts**: `DOWNLOAD_TIMEOUT_S = 60` seconds. If on slow connection, downloads may fail. Retry.
- **SSL errors**: SSL verification is disabled globally (`ssl._create_unverified_context`) for CDN compatibility. If still seeing SSL errors, check network/proxy.
- **Existing video of different quality**: In CLI, prompted to Reuse/Download/Stop. In GUI/batch, existing video reused silently.

## Preflight Check Failures

- **"ffmpeg not found in PATH"**: Install ffmpeg or let the installer auto-install to `tools/ffmpeg/`.
- **"JD2021 game data not found"**: Game path resolution failed. Checked: `search_root/jd21/`, `search_root` itself, `SCRIPT_DIR/jd21/`, recursive scan. Solutions: use shorter path without spaces/accents, avoid Program Files, point directly at `jd21/` folder.
- **"SkuScene_Maps_PC_All.isc not found"**: Game data incomplete or wrong directory.
- **"Cannot write to game directory"**: Permission error. Try running as admin or moving game to non-protected path.
- **"`extractors/archive_ipk.py` not found"**: Project scripts not in correct directory.
- **"Pillow not installed"**: Run `pip install Pillow`.
- **"xtx_extractor/ package not found"**: Missing bundled dependency — re-clone the repository.
- **ffplay not found (warning)**: Sync preview unavailable but installation works.
- **ffprobe not found (warning)**: Duration calculations unavailable.
- **vgmstream not found (warning)**: IPK/XMA2 decode may fail or use reduced-fidelity fallback paths. Ensure `vgmstream-cli.exe` is available via setup/runtime tools.

## Game Path Discovery Issues

- **Clear Path Cache button / `clear_paths_cache()`**: Deletes `installer_paths.json`, forces re-scan on next run.
- **Path with spaces**: May cause issues with some tools. Use short path like `D:\jd2021`.
- **Non-ASCII characters in path**: May cause encoding errors. Use ASCII-only paths.
- **Path in Program Files**: May need admin privileges for write access.

## Texture Decoding Issues

- **"Not a CKD file: missing magic bytes"**: File is not a valid CKD texture.
- **"Not a texture CKD: missing TEX marker"**: CKD is not a texture file (it's JSON data or a different format).
- **"Unknown texture format after CKD header"**: Neither XTX, DDS, nor X360 GPU descriptor found in payload.
- **"Failed to deswizzle XTX texture data"**: xtx_extractor couldn't process the Nintendo Switch texture. Raw XTX saved as fallback.
- **"Pillow can't decode this DDS format"**: Uncommon DDS format. Raw DDS saved for manual conversion with ImageMagick.
- **Missing cover TGA files**: Step 05b logs `[MISS]` for each. Map loads but with blank cover art. If `cover_generic` exists but `cover_online` doesn't (or vice versa), Step 05b auto-copies.
- **X360 tiled textures appear garbled**: `the CKD texture decoder` detects X360 GPU descriptors (52-byte header) and applies tiled-to-linear conversion for DXT1/DXT3/DXT5. If the texture still appears garbled, the format may be unsupported (e.g., non-block-compressed).

## Audio/Sync Issues

- **Silence at map start**: Intro AMB is now generated automatically. If intro audio is still silent, check that the map's OGG source was downloaded successfully and that `videoStartTime < 0` in the generated `.trk`. Unusual source layouts may fall back to silent placeholders. See [AUDIO_TIMING.md](../03_media/AUDIO_TIMING.md).
- **Progressive audio desync**: Wrong sample rate. WAV must be 48kHz (matches markers). Re-run with correct settings.
- **Audio too early or too late**: `a_offset` value incorrect. Use sync refinement to adjust. Marker-based default is usually correct for HTML/fetch maps.
- **Pictos/karaoke appear too early**: `videoStartTime` incorrectly set to 0 on a pre-roll map. Restore original negative value.
- **Unexpected intro layering/double audio**: Usually indicates stale AMB actor state from older installs or manual edits. Reinstall the map cleanly and verify only expected audio actors remain in the generated ISC.
- **"adding a brick in the past" assertion in-game**: The engine received `videoStartTime = 0.0` for a map that actually has pre-roll. For IPK maps, the pipeline synthesizes `v_override` from markers, but if this synthesis fails, the raw `0.0` passes through. Check pipeline output for "Synthesized v_override from markers" message.

### IPK Audio Sync

IPK maps have unique sync characteristics:

- **Video offset is always approximate** — X360 binary CKDs store `videoStartTime = 0.0`. The pipeline synthesizes a default from musictrack markers: `-(markers[abs(startBeat)] / 48.0 / 1000.0)`. This accounts for audio preroll but NOT video lead-in (extra video frames before audio starts).
- **Video lead-in varies per map** — 0s for TGIF, ~1.7s for Koi, ~1.2s for MrBlueSky. No binary metadata encodes this.
- **Manual VIDEO_OFFSET adjustment is expected** — The GUI auto-enables VIDEO_OFFSET after IPK installation and shows a warning. Use the preview to fine-tune until the video matches the beat.
- **Audio offset should stay near 0.0** — IPK audio (decoded from XMA2 via vgmstream) already contains the full preroll from `startBeat` to `endBeat`. The markers map beat indices to positions in this untrimmed audio. Trimming via `a_offset` would break marker-to-audio alignment.

## Binary CKD Parsing Issues

- **"Not a binary CKD / wrong magic"**: File does not start with the expected UbiArt cooked header bytes. May be a JSON CKD instead — the pipeline falls back to JSON parsing automatically via `helpers.load_ckd_json()`.
- **Missing musictrack fields**: Binary CKD parser expects markers, startBeat, endBeat, videoStartTime in a specific field order. If the CKD is from an unsupported game version, fields may be at unexpected offsets.
- **Karaoke lyrics on separate lines**: Previously caused by reading class 80 fields in the wrong order. Fixed — `binary_ckd_parser.py` now reads `IsEndOfLine` first, then `ContentType`, `StartTimeTolerance`, `EndTimeTolerance`, `SemitoneTolerance`.
- **Autodance assertion "no valid video structure for song"**: Previously caused by empty `video_structure = {}`. Fixed — ``installers/game_writer.py`` now generates a minimal valid `JD_AutodanceVideoStructure`.

## IPK-Specific Issues

- **"IPK extraction issue"**: Logged as warning, continues. Some IPK entries may use unsupported compression.
- **Path traversal detected**: IPK contains a path with `..`. Entry skipped for security.
- **Stale audio/video from previous map** — If installing a second IPK map without resetting, the first map's audio/video paths could carry over. Fixed in current version: browsing a new IPK file clears `_source_spec` and hidden audio/video entries. Use **Reset State** if issues persist.
- **IPK re-extraction not happening** — If the extracted folder already exists, the pipeline may skip re-extraction. With `manual_ipk_file` set, re-extraction is now forced. If you change the IPK file, click **Reset State** first, then re-analyze.
- **vgmstream decode failure** — XMA2 audio inside the IPK requires `vgmstream-cli.exe` (installed to `tools/vgmstream/` by setup). If the tool is missing or the WAV CKD uses an unsupported codec, audio decode falls back to CKD header stripping. Check that `vgmstream-cli.exe` exists in the expected path.
- **JDNext mapPackage extraction fails with `AssetStudioModCLI.exe not found`** — Re-run `setup.bat` to auto-download the CLI binary into `tools/AssetStudioModCLI/`, or point `assetstudio_cli_path` in Settings → Advanced to the executable directly. UnityPy (installed via `requirements.txt`) will serve as automatic fallback if the CLI binary is absent.
- **Orphan AMB WAV CKDs** — Some IPK maps (e.g., Koi) contain `amb_*_intro.wav.ckd` files without matching `amb_*_intro.tpl.ckd` templates. Step 09 synthesizes wrappers for compatibility and active intro AMB audio is still generated from the OGG source.

## Config Generation Issues

- **"Could not fetch video start time"**: `musictrack.tpl.ckd` not found or has no `videoStartTime` field. Critical error for HTML/fetch maps. For IPK maps, this is expected (binary CKDs store `videoStartTime = 0.0`) and the pipeline synthesizes a value from markers.
- **Non-ASCII metadata**: Pipeline auto-strips in non-interactive mode, prompts in CLI, shows dialog in GUI.

## SkuScene Registration Issues

- **"Could not find sceneConfigs insertion point"**: SkuScene ISC is malformed or modified by another tool.
- **Map already registered**: Skipped (safe to re-run installer).
- **Map not appearing in game menu**: Check both Actor and CoverflowSong blocks in SkuScene ISC. Both are needed.

## GUI-Specific Issues

- **Log/output not updating**: TextWidgetHandler polls queue every 50ms. Check that pipeline thread is running.
- **Preview not working**: Requires ffplay. Check that ffplay is installed and in PATH.
- **"No Preview" shown**: Video path not set or video file missing.
- **Preview frozen**: May occur if ffmpeg process crashes. Stop and relaunch preview.
- **GUI hangs**: Pipeline runs in background thread. If thread crashes, GUI remains responsive but pipeline stops.
- **Quality dropdown still visible in IPK mode**: Should auto-hide when IPK mode is selected. If visible, try switching modes back and forth, or click **Reset State**.
- **IPK sync warning not appearing**: The warning only appears after the pipeline completes for an IPK map. It checks `state.source_type == 'ipk_file'`.
- **VIDEO_OFFSET not auto-enabled for IPK**: Same trigger as the warning — requires pipeline completion with an IPK source type.

## Batch Installation Issues

- **"No valid map folders found"**: In batch mode, valid candidates are either IPK files or map folders containing both `assets.html` and `nohud.html`.
- **Link expiration with many maps**: The two-phase batch mode downloads all maps first (Phase 1) before processing (Phase 2). If you still get 403s, reduce number of maps per batch.
- **Map skipped as "already installed"**: Use without `--skip-existing` flag, or delete the `MAPS/{map_name}/` directory first.

## General Tips

- Always get fresh HTML links immediately before running the installer (links expire in ~30 minutes).
- Use video quality tier that matches what's already downloaded to avoid re-downloading.
- For IPK maps, expect to spend time fine-tuning the VIDEO_OFFSET in the Sync Refinement panel. Use the Preview button to test.
- Run setup and dependency checks before troubleshooting map-specific issues (Playwright runtime, FFmpeg/FFprobe, and vgmstream for IPK-heavy maps).
- Intro AMB audio is generated automatically for pre-roll maps. Silence at map start (for `videoStartTime < 0` maps) indicates a pipeline issue, not expected behavior — check the log for AMB generation errors.
- Global installer settings are saved to `installer_settings.json` and auto-loaded on reinstall.
- Per-map sync configs are saved automatically and reloaded when reinstalling the same map.
- Log files are written to `logs/install_{map_name}_{timestamp}.log`.
- Ctrl+C gracefully stops after the current pipeline step completes (CLI only).
