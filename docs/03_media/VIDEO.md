# Video Reference

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This document covers the NOHUD video quality system in JD2021 Map Installer v2: available tiers for both **JDU** and **JDNext** sources, selection/fallback behavior, VP9 compatibility handling, and how `media_processor.py` processes video files using FFmpeg. Empirical data for JDNext is drawn from a 46-file sample across 30+ maps.

---

## V2 Operational Notes (Read First)

These constraints affect video/audio behavior in current v2 builds:

1. **IPK video timing remains approximate by design.**
  X360/IPK source metadata often does not provide a reliable lead-in, so generated defaults can still require manual Video Offset tuning.
2. **Runtime dependencies are required for stable media workflows.**
  FFmpeg/FFprobe and vgmstream must be available for full decode/preview/convert coverage. FFmpeg is expected on the system `PATH`; vgmstream is auto-installed by `setup.bat` into `tools/vgmstream/`. Fetch mode also requires Playwright Chromium.
3. **Dedicated preview assets are optional in current runtime wiring.**
  Current installs remain playable and preview-capable without `AudioPreview` / `MapPreview` files; preview loop behavior is typically driven by `.trk` marker fields over main media. See [ASSETS.md](ASSETS.md) for exact with/without differences.

> [!IMPORTANT]
> **Text vs Binary distinction:** Video quality selection and tier mapping are **text-based** operations — the pipeline matches filename patterns against URL strings parsed from HTML. The actual binary video work (copy, transcode, re-encode) is handled by `media_processor.py` via FFmpeg subprocess calls. See [ASSETS.md](ASSETS.md) for the full media pipeline reference.

---

## Available Quality Tiers

The pipeline supports 8 logical quality tiers. The concrete filename variant differs by source family (JDU vs JDNext).

| Tier | Suffix Pattern | Description |
|------|---------------|-------------|
| `ULTRA_HD` | `_ULTRA.hd.webm` | Highest quality (HD variant of Ultra) |
| `ULTRA` | `_ULTRA.webm` | Ultra quality |
| `HIGH_HD` | `_HIGH.hd.webm` | High HD |
| `HIGH` | `_HIGH.webm` | High |
| `MID_HD` | `_MID.hd.webm` | Medium HD |
| `MID` | `_MID.webm` | Medium |
| `LOW_HD` | `_LOW.hd.webm` | Low HD |
| `LOW` | `_LOW.webm` | Lowest quality |

### JDNext Variant Mapping

JDNext uses explicit `hd`, `vp9`, and sometimes `vp8` variant filenames. The installer maps JDNext links into the same 8 logical tiers:

| Logical Tier | JDNext Preferred Variant | Typical JDNext URL pattern |
|---|---|---|
| `ULTRA_HD` | `hd` | `/video_ultra.hd.webm/...` |
| `ULTRA` | `vp9` | `/video_ultra.vp9.webm/...` |
| `HIGH_HD` | `hd` | `/video_high.hd.webm/...` |
| `HIGH` | `vp9` | `/video_high.vp9.webm/...` |
| `MID_HD` | `hd` | `/video_mid.hd.webm/...` |
| `MID` | `vp9` | `/video_mid.vp9.webm/...` |
| `LOW_HD` | `hd` | `/video_low.hd.webm/...` |
| `LOW` | `vp9` | `/video_low.vp9.webm/...` |

Notes:
- If both `hd` and legacy `vp8` exist for the same `*_HD` tier, `hd` is preferred (controlled by `_classify_urls()` in `web_playwright.py`).
- JDNext non-HD tiers are VP9 by design unless compatibility fallback mode is enabled.

### JDNext vs JDU Filename Conventions

| Convention | JDU Pattern | JDNext Pattern |
|---|---|---|
| Prefix | `{MapName}_` (e.g. `BadRomance_HIGH.hd.webm`) | `video_` (e.g. `video_HIGH.hd.webm`) |
| HD variant | `_TIER.hd.webm` | `video_TIER.hd.webm` |
| Non-HD variant | `_TIER.webm` | `video_TIER.vp9.webm` |
| Fallback format | N/A | `video_TIER.vp8.webm` (rare) |

Some JDU-origin maps that have been mirrored through JDNext CDN paths retain the `{MapName}_` prefix pattern. The quality classifier handles both patterns transparently.

---

## Quality Selection Behavior

### Fallback Chain

When the requested quality is not available on the CDN, the pipeline falls back through tiers in wrap-around order starting from the requested tier:

```
Requested: HIGH_HD
Search order: HIGH_HD -> HIGH -> MID_HD -> MID -> LOW_HD -> LOW -> ULTRA_HD -> ULTRA
```

The first available tier is selected. If it differs from the requested tier, a status message is logged.

> [!NOTE]
> Quality selection operates entirely on **text-based filename pattern matching** against URL strings. No video files are opened or probed during selection — that only happens downstream in `media_processor.py`.

### JDNext VP9 Handling Modes

For JDNext links, behavior is controlled by `vp9_handling_mode`:

| Mode | Behavior | Binary media impact |
|---|---|---|
| `reencode_to_vp8` | Keeps requested JDNext VP9 tier, then re-encodes VP9 → VP8 during install for compatibility. | `copy_video()` invokes FFmpeg with `libvpx` encoder (see encoding params below). |
| `fallback_compatible_down` | Avoids VP9 tiers and picks the next compatible `*_HD` tier down (no VP9 re-encode path). | `copy_video()` performs a byte-for-byte `shutil.copy2`. |

Compatibility-down examples:

```
Requested: ULTRA   -> search ULTRA_HD -> HIGH_HD -> MID_HD -> LOW_HD
Requested: HIGH    -> search MID_HD -> LOW_HD
Requested: MID_HD  -> search MID_HD -> LOW_HD
```

### VP9→VP8 Transcoding Details (media_processor.py)

When `vp9_handling_mode` is `reencode_to_vp8`, the `copy_video()` function in `media_processor.py` transcodes using these FFmpeg parameters:

```
ffmpeg -y -hwaccel auto -i <source> -an -c:v libvpx -pix_fmt yuv420p \
  -deadline good -cpu-used 2 -row-mt 1 -threads 0 \
  -b:v 8500k -maxrate 11000k -bufsize 22000k \
  -qmin 4 -qmax 32 -g 25 -keyint_min 25 -sc_threshold 0 \
  <output.webm>
```

Key encoding choices:
- **`-deadline good`** — balanced quality/speed (avoids `best` which is prohibitively slow).
- **`-cpu-used 2`** — sweet spot for quality vs encoding time.
- **`-row-mt 1`** — enables row-based multithreading for VP8.
- **`-threads 0`** — uses all available CPU cores.
- **`-hwaccel auto`** — auto-injected by `run_ffmpeg()` for decode acceleration.

### Existing Video Detection

Before downloading, the pipeline checks whether a video of a different quality already exists in the map download directory.

| Mode | Behavior |
|------|----------|
| **Interactive CLI path (where used)** | Prompts to reuse, redownload, or stop |
| **GUI / non-interactive workers (default v2 usage)** | Reuses existing video automatically |

Batch workflows additionally detect the quality of already-downloaded files and prefer reuse to avoid unnecessary redownloads.

### Post-Download Fallback

If download fails (commonly expired links / HTTP 403 / HTTP 404), the pipeline searches local `.webm` files using the same quality fallback chain and continues with the best available local match.

---

## Setting Quality

### GUI (Primary v2 path)

Select **Video Quality** in the Configuration section.

Available values:
- `ultra_hd`
- `ultra`
- `high_hd`
- `high`
- `mid_hd`
- `mid`
- `low_hd`
- `low`

### CLI / Scripted Invocation (Advanced)

The project is now GUI-first in normal operation (`RUN.bat` or `python -m jd2021_installer.main`).

If you run internal/scripted pipeline entry points, pass quality with the corresponding `--quality` option for that flow. Prefer documented GUI workflows unless you are maintaining or testing internals.

---

## Quality Persistence

Video preferences are persisted in `installer_settings.json` using:
- `video_quality`
- `vp9_handling_mode`

Example:

```json
{
  "video_quality": "ULTRA_HD",
  "vp9_handling_mode": "fallback_compatible_down",
  "v_override": -2.145,
  "a_offset": -2.060,
  "marker_preroll_ms": 2060.0
}
```

On later installs/reinstalls, these global settings are loaded automatically.

---

## File Size Considerations

Higher tiers produce much larger WebM files. File sizes vary by source:

| Tier | Typical JDU Size | Typical JDNext `.hd` Size | Typical JDNext `.vp9` Size |
|---|---|---|---|
| `ULTRA_HD` | ~214 MB | ~213 MB | N/A |
| `ULTRA` | ~192 MB | N/A | ~147 MB |
| `HIGH_HD` | ~93 MB | ~67–143 MB (varies) | N/A |
| `HIGH` | ~93 MB | N/A | ~74 MB |
| `MID_HD` | ~46 MB | ~40 MB | N/A |
| `MID` | ~46 MB | N/A | ~37 MB |
| `LOW_HD` | ~13 MB | ~10 MB | N/A |
| `LOW` | ~13 MB | N/A | ~19 MB* |

\* JDNext VP9 LOW is an anomaly — the VP9 file is nearly 2× the size of the VP8 HD variant at the same resolution, likely because VP9 at 480×270 uses a higher target bitrate than the VP8 LOW encode.

If disk space is constrained, `HIGH_HD` is the recommended default for both JDU and JDNext — it provides 720p VP8 output with no transcoding required.

In standard workflows, one quality tier is used per map install target. Switching tier usually requires redownloading unless multiple tier files are already present locally.

---

## How media_processor.py Handles Video Files

The video processing functions in `media_processor.py` are the **only** place where binary video data is read, copied, or transcoded:

| Function | Binary operation | When it runs |
|---|---|---|
| `copy_video()` | Copies or transcodes a WebM file to the install target directory. Decides at runtime whether to use `shutil.copy2` (same format, no VP9 issue) or FFmpeg (VP9→VP8 transcode, format conversion, or forced re-encode). | During install, after download/selection. |
| `generate_map_preview()` | Extracts a lower-quality excerpt clip from the main video using FFmpeg (`-ss`, `-t`, VP9 output). | During preview generation (optional). |
| `_get_video_codec()` | Probes the primary video codec via FFprobe (`-show_entries stream=codec_name`). Used to detect VP9 sources. | Inside `copy_video()` decision logic. |
| `get_video_duration()` | Probes video duration via FFprobe (`-show_entries format=duration`). | Timing calculations. |

### Copy-vs-transcode decision tree in copy_video()

```
Input: source WebM + config
  │
  ├─ force_reencode=True? → FFmpeg transcode (always)
  │
  ├─ Different output extension? → FFmpeg transcode
  │
  ├─ Source is VP9?
  │   ├─ vp9_handling_mode = "reencode_to_vp8" → FFmpeg VP9→VP8 transcode
  │   └─ vp9_handling_mode = "fallback_compatible_down" → shutil.copy2 (with warning)
  │
  └─ Same format, not VP9, no force → shutil.copy2 (byte-for-byte copy)
```

> [!TIP]
> **Performance note:** VP9→VP8 re-encoding is CPU-intensive and can take several minutes per map at ULTRA/ULTRA_HD quality. The `fallback_compatible_down` mode avoids this entirely by selecting HD-variant tiers that are already VP8.

---

## NOHUD Video File Analysis

These are **NOHUD (No Heads-Up Display) coach videos**: dance footage without gameplay overlays (score, arrows, coach UI, etc.). The 8 logical tiers represent **4 quality levels × 2 slots** (`*_HD` and non-`HD`) plus one shared audio stream.

For JDU, non-HD slots are typically VP8 (`_ULTRA.webm`, `_HIGH.webm`, etc.).
For JDNext, non-HD slots are typically VP9 (`video_ULTRA.vp9.webm`, etc.) unless compatibility-down mode is used.

---

### JDU File Reference (Example: single map sample)

**Common `.webm` properties:**

- Codec: VP8
- Pixel format: yuv420p
- Frame rate: 25 fps
- Duration: ~194.36s

| File | Resolution | Bitrate | Size | VP8 Profile |
|---|---|---|---|---|
| `ULTRA HD.webm` | 1920×1080 | 8,822 kbps | 214 MB | 0 |
| `ULTRA.webm` | 1216×720 | 7,889 kbps | 192 MB | **2** |
| `HIGH HD.webm` | 1280×720 | 3,834 kbps | 93.1 MB | 0 |
| `HIGH.webm` | 1216×720 | 3,850 kbps | 93.5 MB | **2** |
| `MID HD.webm` | 768×432 | 1,902 kbps | 46.2 MB | 0 |
| `MID.webm` | 768×432 | 1,902 kbps | 46.2 MB | 0 |
| `LOW HD.webm` | 480×270 | 533 kbps | 12.9 MB | 0 |
| `LOW.webm` | 480×270 | 529 kbps | 12.9 MB | 0 |

#### The Two Encoding Generations

The non-`HD` and `HD` files appear to represent two encoding targets: likely original extracted assets vs. re-encoded/corrected assets.

##### The 1216-Wide Non-Standard Width

`HIGH.webm` and `ULTRA.webm` are 1216 pixels wide, which is non-standard for typical web/video delivery (720p standard width is 1280).

This is consistent with source cropping (for example HUD-safe or letterbox-area removal). `HD` variants normalize this to standard frames (1280×720 and 1920×1080).

##### VP8 Profile Difference

- **Profile 0** (HD variants): broad compatibility and typical decode behavior.
- **Profile 2** (`ULTRA.webm`, `HIGH.webm`): more complex profile, commonly seen in legacy engine-generated encodes.

This again supports the interpretation that non-HD files are closer to original extracted game encodes, while HD files are compatibility-focused re-encodes.

#### MID and LOW: Minimal Practical Difference

For `MID` and `LOW`, `HD` and non-`HD` are effectively equivalent in practice (same resolution/profile and near-identical size/bitrate).

---

### JDNext File Reference (Empirical: 46 files / 30+ maps)

Data sourced from `jdnext_videos_ffprobe.csv` — real ffprobe output from downloaded JDNext WebM files.

**Common `.webm` properties (all JDNext samples):**

- Pixel format: yuv420p
- Frame rate: 25 fps (25/1)
- Duration range: 156s–296s (varies per song)

#### Complete 8-Tier Reference (BirdsOfAFeather)

BirdsOfAFeather is the only map in the sample with all 8 tiers downloaded, providing a direct comparison:

| File | Codec | Resolution | Bitrate | Size |
|---|---|---|---|---|
| `video_ULTRA.hd.webm` | VP8 | 1920×1080 | 7,879 kbps | 213 MB |
| `video_ULTRA.vp9.webm` | **VP9** | 1280×720 | 5,434 kbps | 147 MB |
| `video_HIGH.hd.webm` | VP8 | 1280×720 | 2,954 kbps | 80 MB |
| `video_HIGH.vp9.webm` | **VP9** | 1280×720 | 2,740 kbps | 74 MB |
| `video_MID.hd.webm` | VP8 | 768×432 | 1,486 kbps | 40 MB |
| `video_MID.vp9.webm` | **VP9** | 768×432 | 1,363 kbps | 37 MB |
| `video_LOW.hd.webm` | VP8 | 480×270 | 375 kbps | 10 MB |
| `video_LOW.vp9.webm` | **VP9** | 480×270 | 691 kbps | 19 MB |

> [!NOTE]
> A transcoded test file `video_ULTRA.vp9_to_vp8.webm` (147 MB, VP8 1280×720, 5,451 kbps) is also present — this confirms that VP9→VP8 re-encoding preserves resolution and produces near-identical file size to the VP9 source.

#### JDNext `HIGH_HD` Tier Statistics (27 maps)

Most JDNext maps in the sample were downloaded at `HIGH_HD` (the recommended default). Aggregate statistics:

| Metric | Value |
|---|---|
| Codec | VP8 (all) |
| Resolution | 1280×720 (all) |
| Frame rate | 25 fps (all) |
| Bitrate range | 2,601–4,123 kbps |
| Bitrate median | ~3,790 kbps |
| File size range | 66–143 MB |
| Duration range | 156–296s |

#### JDU-Origin Maps on JDNext CDN (ULTRA Tier)

Several maps (Balance, Chiwawa, Domino, Hangover, Koi, MamaMia, MrBlueSky) use the JDU `{MapName}_ULTRA.webm` naming convention despite being downloaded through JDNext workflows. These files share JDU encoding characteristics:

| Metric | Value |
|---|---|
| Codec | VP8 (all) |
| Resolution | 1216×720 (non-standard JDU width) |
| Bitrate range | 7,376–7,954 kbps |
| File size range | 165–242 MB |

This confirms these are JDU-origin encodes served through JDNext infrastructure, not native JDNext re-encodes.

#### Key JDNext Observations

1. **VP9 is more size-efficient** at HIGH/MID/ULTRA, achieving ~7–15% smaller files than the equivalent `.hd` (VP8) tier at the same resolution.
2. **VP9 LOW is the exception** — at 480×270, the VP9 variant is ~1.8× larger than the VP8 HD variant (19 MB vs 10 MB), likely due to a higher target bitrate setting in the JDNext encoding pipeline.
3. **JDNext `.hd` variants always use VP8** and match the exact resolution/codec of JDU HD files, making them drop-in compatible with JD2021 without transcoding.
4. **JDNext `.vp9` variants always use VP9**, even when the resolution is the same as the `.hd` counterpart (e.g., both HIGH variants are 1280×720).
5. **ULTRA resolution differs by variant**: `.hd` is 1920×1080, `.vp9` is 1280×720 — the VP9 non-HD tier is lower resolution.

---

### Audio Gap (Both Sources)

`AUDIO.ogg` (Vorbis, ~224 kbps) is typically shorter than the corresponding NOHUD video (roughly 5+ seconds in common sets).

Audio/video are intentionally not 1:1 duration-matched at source level. Final play sync is handled through installer timing controls (`v_override`, `a_offset`, marker preroll handling) plus user readjust when needed.

---

## Dependency Summary for Video Operations

| Tool | Required for | Provisioned by |
|---|---|---|
| **FFmpeg** | VP9→VP8 transcoding, preview generation, all video format conversion | Must be on system `PATH` (not auto-installed by `setup.bat`) |
| **FFprobe** | Codec detection, duration probing | Must be on system `PATH` (bundled with FFmpeg) |

> [!WARNING]
> Without FFmpeg on the system `PATH`, `copy_video()` will fall back to byte-for-byte copy even for VP9 sources, and `generate_map_preview()` will fail entirely. Install FFmpeg from [ffmpeg.org](https://ffmpeg.org) or via a package manager before running the installer.

---

## Quick-Reference Q&A

| Question | Answer |
|---|---|
| Which variants should I use? | For **JDU**: prefer `*_HD` for ULTRA/HIGH (standard aspect output, Profile 0). MID/LOW are interchangeable. For **JDNext**: prefer `*.hd.webm` variants — they are already VP8 and need no transcoding. |
| Why is ULTRA_HD much larger? | 1920×1080 contains about 2.3× the pixel count of 1216×720 (JDU) or 1280×720 (JDNext VP9), reflected in output size and bitrate. |
| Do JDNext `.hd` files need transcoding? | No — JDNext `.hd.webm` files are VP8 at standard resolutions and can be byte-copied to the install target without FFmpeg. |
| Why is JDNext VP9 LOW larger than VP8 LOW? | The JDNext encoding pipeline appears to use a higher target bitrate for VP9 LOW (~691 kbps) than VP8 LOW HD (~375 kbps) at the same 480×270 resolution. |
| How do I tell JDU-origin maps from true JDNext? | Check the filename prefix: `{MapName}_TIER.webm` = JDU origin; `video_TIER.{variant}.webm` = native JDNext. JDU-origin also shows the non-standard 1216px width at ULTRA tier. |
| Why is video sync still off on some IPK maps? | IPK `videoStartTime` metadata is often incomplete/approximate, so manual Video Offset tuning is expected for some maps. |
| Can I skip VP9→VP8 re-encoding? | Yes — set `vp9_handling_mode` to `fallback_compatible_down` in settings. The pipeline will select HD-variant tiers (VP8) instead. |
| Where does binary video processing happen? | Exclusively in `media_processor.py` functions: `copy_video()`, `generate_map_preview()`, `_get_video_codec()`, `get_video_duration()`. |
