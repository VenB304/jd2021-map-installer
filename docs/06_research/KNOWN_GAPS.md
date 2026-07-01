# Known Gaps and Remaining Work

**Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This document describes known limitations, unresolved issues, and potential improvements identified through code analysis.

---

## Active Gaps

### 1. Mid-Song AMB Sounds Remain Silent (Moved from Active Gap #4)

**Status:** By design; source data unavailable.

AMB sounds with `SoundSetClip StartTime > 0` are kept as silent WAV placeholders because mid-song AMB audio is hosted remotely by Ubisoft CDN and cannot be fetched by the installer.

> [!NOTE]
> **Intro AMB is now resolved.** The `INTRO_AMB_ATTEMPT_ENABLED` flag is `True` by default and the pipeline generates active intro AMB audio for all maps with `videoStartTime < 0`. See [AUDIO_TIMING.md](../03_media/AUDIO_TIMING.md) for full details. This was previously documented as a temporary mitigation but has since been fully implemented.

### 2. IPK Video Offset Is Approximate

**Status:** Inherent limitation; cannot be fully resolved from available source metadata.

Xbox 360 binary CKDs store `videoStartTime = 0.0`. The pipeline synthesizes a default from musictrack markers: `-(markers[abs(startBeat)] / 48.0 / 1000.0)`. This accounts for audio preroll but not video lead-in (extra video frames before audio starts).

Video lead-in varies per map (0s for TGIF, about 1.7s for Koi, about 1.2s for MrBlueSky) and is not encoded in binary metadata.

**Mitigation:** The GUI enables VIDEO_OFFSET handling for IPK workflows and warns that manual adjustment is expected. The marker-based formula gets users close, then manual tuning finalizes sync.

### 3. NX Platform Mode for Joycons Is Incomplete

**Status:** Partially implemented infrastructure, needs completion.

**What exists:**
- NX scene archives are downloaded and extracted.
- NX platform move folders are copied to `Timeline/Moves/NX/`.
- Scene archive selection includes NX in preference order.

**What is missing:**
- NX `.gesture` files are extracted but not merged into a usable final location.
- Maps are registered in `SkuScene_Maps_PC_All.isc`, not in `SkuScene_Maps_NX_All.isc`.
- No user-facing preference/flag to explicitly opt into NX mode.

### 4. No Multi-Audio-Track Support

**Status:** Known limitation.

Maps with more than one audio stream (for example alternate language tracks) are not supported. The pipeline assumes one primary OGG/WAV stream per map.

### 5. ORBIS Gesture Format Incompatibility

**Status:** Mitigated by substitution, not fully resolved.

PlayStation (ORBIS) `.gesture` files use a binary format incompatible with the PC Kinect adapter. The pipeline substitutes ORBIS-exclusive gesture variants by stripping trailing digits and copying the base Kinect gesture. This works in many cases but may be inaccurate for ORBIS-exclusive choreography variants.

### 6. mapsObjectives.ilu Is Not Patched

**Status:** Not required for core playability, but may affect UI.

`mapsObjectives.ilu` ties maps to unlock conditions. The `Status = 3` override is sufficient to make maps playable, but objective-related UI behaviors may still appear for maps referenced there.

### 7. Runtime Dependency Fragility

**Status:** Operational risk.

The V2 pipeline depends on local external tooling and runtime bundles, especially:
- FFmpeg/FFprobe for media conversion/probing.
- vgmstream runtime for X360/XMA2 audio decode paths.
- Playwright Chromium runtime for Fetch workflows.

Missing or partially installed dependencies cause degraded paths, fallback behavior, or outright feature failure (especially Fetch, preview/decode, and some conversion steps).

### 8. JDNext Gesture Compilation Quality

**Status:** Experimental; branch abandoned.

A JDNext→Durango Kinect gesture compilation pipeline exists across `gesture_compiler.py`, `biomechanics.py`, and `hmm_generator.py`, but the dedicated `feat/jdnext-gesture` branch was abandoned without being merged. Output quality is known to be inconsistent across maps, so the feature is disabled by default (`convert_jdnext_gestures = False`).

**Workaround (shipped):** `tape_converter.py` automatically mirrors the `.msm` controller track to the `.gesture` track 1:1 when native Kinect gesture data is absent, resolving the `0/0` gold moves grading issue on PC/Switch.

See [CAMERA_GESTURE_REMEDIATION_PLAN.md](CAMERA_GESTURE_REMEDIATION_PLAN.md) for the full research log.

---

## Potential Improvements

These are not bugs or active blockers, but improvements identified through code analysis and operations feedback:

### Parallel Downloads
The current downloader is intentionally conservative (sequential with inter-request delay). Controlled parallelism could reduce batch install time while keeping CDN-safe retry/backoff behavior.

### Progress Reporting for Downloads
Large media downloads can take several minutes without byte-level progress feedback. Adding streamed progress reporting would improve operator confidence.

### Cleanup Automation Parity
The GUI offers cleanup prompts after "Apply & Finish". Equivalent cleanup options in CLI-oriented flows would improve consistency.

### Video Lead-In Estimation for IPK Maps
Investigate ffprobe/keyframe or scene-analysis heuristics to estimate video lead-in automatically and reduce manual VIDEO_OFFSET tuning.

---

## Resolved Items

The following items from the original findings have been implemented in the codebase:

### Status Override for JD2021 Maps (Resolved)

**Original issue:** Maps originally from JD2021 appeared locked (`Status = 12`, ObjectiveLocked).

**Resolution:** `installers/game_writer.py` now overrides `Status = 12` to `Status = 3` (Available) during SongDesc generation.

### Download Throttling / Rate Limiting (Resolved)

**Original issue:** No protection against CDN throttling.

**Resolution:** `extractors/web_playwright.py` now implements browser-like User-Agent, retry logic with exponential backoff, HTTP 429 handling, and inter-request delay.

### Binary CKD Parsing (Resolved)

**Original issue:** Legacy Xbox 360 and older UbiArt CKD files used a cooked binary format unreadable by the JSON-based pipeline.

**Resolution:** `binary_ckd_parser.py` parses binary (big-endian) CKD files for musictracks, songdescs, choreography/karaoke tapes, cinematic tapes, autodance templates, and sound components. `helpers.load_ckd_json()` falls back to this parser when JSON parsing fails, making binary CKD support transparent throughout the pipeline.

### X360 Texture Decoding (Resolved)

**Original issue:** Xbox 360 textures used tiled memory layout and byte-swapped pixels, appearing garbled when extracted.

**Resolution:** The CKD texture decoder now detects X360 texture payloads (52-byte GPU descriptor), performs 16-bit word byte-swap, and applies Xenia-derived tiled-to-linear conversion (Tiled2D algorithm) for DXT1/DXT3/DXT5 block-compressed formats.

### Intro AMB Generation (Resolved)

**Original issue:** Maps with a pre-roll silence window (`videoStartTime < 0`) played silent audio from `t=0` until the WAV started. No mechanism existed to fill the gap.

**Resolution:** `installers/ambient_processor.py` and `installers/media_processor.py` now generate active intro AMB audio using FFmpeg. The `INTRO_AMB_ATTEMPT_ENABLED` flag is `True` by default. Audio content is extracted from the map's OGG source, trimmed and delayed to align with the video pre-roll window, and written to `Audio/AMB/amb_{mapname}_intro.wav` with matching `.ilu` / `.tpl` wrappers. See [AUDIO_TIMING.md](../03_media/AUDIO_TIMING.md) for the full timing model.

### Orphan AMB WAV CKD Handling (Resolved)

**Original issue:** Some IPK maps (e.g., Koi) contained `amb_*_intro.wav.ckd` files without matching `amb_*_intro.tpl.ckd` templates, causing AMB audio to be silently skipped.

**Resolution:** Step 09 (`step_09_process_amb`) detects orphan WAV CKDs and generates synthetic TPL and ILU wrappers.

### Karaoke Binary Field Order (Resolved)

**Original issue:** Binary CKD karaoke entries (entry class 80) had fields read in the wrong order, causing each syllable to appear on its own line.

**Resolution:** `binary_ckd_parser.py` now reads class 80 fields correctly: `IsEndOfLine`, `ContentType`, `StartTimeTolerance`, `EndTimeTolerance`, `SemitoneTolerance`.

### Autodance Video Structure (Resolved)

**Original issue:** Autodance stub wrote empty `video_structure = {}`, causing game assertion: "no valid video structure for song".

**Resolution:** `installers/game_writer.py` now generates a minimal valid `JD_AutodanceVideoStructure` with all required fields.

### MSM-to-Gesture Mirroring (Resolved)

**Original issue:** JDNext maps ported without native Kinect `.gesture` files scored `0/0` gold moves on PC/Switch because the gesture track was empty.

**Resolution:** `installers/tape_converter.py` (`_mirror_msm_to_gesture_clips`) automatically mirrors the right-hand `.msm` controller track to the `.gesture` track 1:1 when native Kinect gesture data is absent. This preserves original controller choreography, aligns scorable zones with scrolling pictograms, and resolves the `0/0` grading issue.

See gap #8 for the remaining gesture *compilation quality* issue (experimental JDNext→Kinect conversion).
