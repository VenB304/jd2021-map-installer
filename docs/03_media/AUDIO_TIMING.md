# Audio Timing, Pre-Roll Silence, and AMB Status (V2)

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This document explains the UbiArt engine's audio/video synchronization model, the pre-roll silence problem that affects most ported maps, the AMB-based strategy, and IPK-specific audio handling.

> [!IMPORTANT]
> **Text vs Binary distinction:** This document describes **runtime timing semantics** — how the engine interprets numeric fields parsed from text-based `.trk` / `.tpl` files. The actual binary media encoding (WAV generation, OGG trimming, AMB audio extraction) is performed by `media_processor.py` using FFmpeg and vgmstream. See [ASSETS.md](ASSETS.md) for the full media pipeline reference.

> [!NOTE]
> **Intro AMB generation is now enabled by default.** The `INTRO_AMB_ATTEMPT_ENABLED` flag in `media_processor.py` is set to `True`. The installer generates active intro AMB audio for maps with negative `videoStartTime`, covering the pre-roll silence window.

---

## 1. How the Engine Synchronizes Audio and Video

Every map has a `.trk` (MusicTrack) file that defines the beat grid. The two most critical fields are:

| Field | Type | Meaning |
|---|---|---|
| `videoStartTime` | float (seconds) | Where in the video file beat 0 appears. Negative = video has a pre-roll intro before beat 0. |
| `startBeat` | int | The beat index of the first marker (e.g. `-5` = the first marker is beat -5). |
| `markers` | array (samples @ 48 kHz) | Sample position for each beat marker. For maps with negative `startBeat`, beat 0 is typically at `markers[abs(startBeat)]`. |

The engine couples two behaviors to `videoStartTime`:

1. **Video alignment**: The video player seeks to `videoStartTime` seconds when the map starts. For a negative value like `-2.145`, the video starts 2.145 seconds before beat 0, showing the pre-roll intro frames.

2. **WAV delay**: The engine delays WAV playback by exactly `abs(videoStartTime)` seconds. With `videoStartTime = -2.145`, the WAV does not start playing until `t = 2.145s` from game start.

These two behaviors are **inseparably coupled** through a single parameter. There is no mechanism to set them independently.

This timing model applies across active V2 install/update flows (Fetch, HTML, IPK, Batch, Manual, and readjust) whenever MusicTrack timing is generated, preserved, or re-applied.

> [!NOTE]
> **Parsing context:** These timing fields are read from **text-based** CKD JSON or Lua config files. The parser (`tape_converter.py`, MusicTrack normalizer) reads them as structured text. No binary audio data is touched during timing field extraction — that happens later in `media_processor.py`.

---

## 2. The Pre-Roll Silence Problem

For any map where `videoStartTime < 0`, the following happens:

- `t = 0.000s` - Game starts. Video begins playing from the intro frames. **No audio.**
- `t = 2.145s` - WAV begins playing (sample 0 = beat -5 in the song).
- `t ≈ 4.489s` - Beat 0 of the song. Choreography and scoring begin.

The interval from `t=0` to `t=abs(vst)` is inherently silent. The video shows the coach's intro animation but the audio has not started yet. This produces a noticeable silence of typically 1-4 seconds at the start of many maps.

### Why vst=0 Does Not Fix This

Setting `videoStartTime = 0.000000` eliminates the WAV delay and plays audio immediately, but it also eliminates the video pre-roll: the video starts at beat-0 frame rather than at the intro frame. This means:

- The coach's pre-roll animation is skipped (visual desync)
- Pictos and karaoke, which are driven by the video timeline, appear too early relative to the audio
- The only JD2021 map that legitimately uses `vst=0` is JustDance, which also has `startBeat=0` (no pre-roll)

For maps with `startBeat < 0`, forcing `vst=0` breaks the visual timeline. The negative `videoStartTime` value from source metadata should generally be preserved.

---

## 3. AMB Strategy and Current Runtime Policy

The engine loads `SoundComponent` actors from the audio `.isc` at `t=0`, before the WAV delay kicks in. In principle, an ambient actor can play immediately at game start and cover the silence window.

### Intended strategy (design behavior)

The intended intro AMB strategy is:
- Play from `t=0`, covering the pre-roll silence window
- Source audio from the same song asset path used by the main track
- Fade out near the handoff to avoid hard cuts

### Current V2 runtime behavior

- **Intro AMB generation is enabled by default** (`INTRO_AMB_ATTEMPT_ENABLED = True` in `media_processor.py`).
- The `generate_intro_amb()` function produces active intro AMB audio using FFmpeg:
  - Extracts a segment from the map's OGG source, trimmed and delayed to align with the video pre-roll window.
  - Applies marker-based or video-aligned duration calculation.
  - Falls back to silent placeholders only when `attempt_enabled=False` is explicitly passed or when no pre-roll silence exists (`a_offset >= 0` and `v_override >= 0`).

### Why same-source overlap works

When the AMB and main WAV overlap, two phase-coherent sources reading the same audio segment overlap as a louder single source rather than echo/doubling. The design ensures AMB content is extracted from the same OGG source used for the main track.

### How media_processor.py generates AMB audio

The `generate_intro_amb()` function in `media_processor.py` performs these **binary media operations** using FFmpeg:

1. **Resolves the AMB directory** with case-insensitive fallback (`Audio/AMB`, `audio/amb`, etc.).
2. **Computes timing** from `v_override`, `a_offset`, and `marker_preroll_ms` (text-parsed values).
3. **Extracts audio** from the OGG source using FFmpeg with `-ss` (front trim) and `-t` (duration).
4. **Applies delay padding** via FFmpeg `adelay` filter when `audio_delay > 0`.
5. **Writes the intro WAV** at 48 kHz to the AMB directory.
6. **Generates `.ilu` and `.tpl` files** (text-based UbiArt actor descriptors) when no existing wrappers are found.

> [!TIP]
> The AMB generation pipeline cleanly illustrates the text/binary boundary: timing values are parsed from text config files upstream, then passed as numeric parameters to `generate_intro_amb()`, which performs all binary audio work via FFmpeg subprocess calls.

---

## 4. Two Sync Variables

The pipeline tracks two independent timing values:

| Variable | Source | Meaning |
|---|---|---|
| `v_override` | `videoStartTime` from `musictrack.tpl.ckd` (or synthesized for IPK) | How far before beat 0 the video file starts. Usually negative. |
| `a_offset` | Marker-based calculation (preferred), `0.0` for IPK maps, or fallback behavior depending on source | How far before beat 0 the audio file starts. Controls audio trimming/padding. |

### HTML/Fetch Maps

For server-fetched maps, both `v_override` and `a_offset` are typically negative. The OGG audio contains the full song and `a_offset` trims it to start at the expected beat alignment.

Marker-based formula for `a_offset`:
```
idx               = abs(startBeat)
marker_preroll_ms = markers[idx] / 48.0 + 85.0   # markers are 48 kHz samples; 85ms = OGG decode latency calibration
a_offset          = -(marker_preroll_ms / 1000.0)
```

`markers[idx]` is the sample position of beat 0 in the audio. Dividing by 48 converts 48 kHz samples to milliseconds.

**How `a_offset` drives binary audio conversion:** The `convert_audio()` function in `media_processor.py` uses `a_offset` to control FFmpeg:
- `a_offset == 0` → straight conversion to 48 kHz WAV (`-ar 48000`)
- `a_offset < 0` → FFmpeg trims the first `abs(a_offset)` seconds (`-ss`)
- `a_offset > 0` → FFmpeg pads silence via `adelay` filter

### IPK Maps (Xbox 360 Binary Mode)

For IPK maps extracted from Xbox 360 archives, handling differs:

1. **`a_offset` is `0.0`** in the current path - decoded XMA2-derived audio is treated as already containing preroll.

2. **`v_override` is synthesized from markers** because many binary CKDs carry `videoStartTime = 0.0`:
   ```
   v_override = -(markers[abs(startBeat)] / 48.0 / 1000.0)
   ```
   No `+85ms` compensation is added here; that calibration applies to OGG trimming logic, not direct video timing.

3. **Video lead-in is not encoded in binary metadata** - synthesized `v_override` only accounts for marker-derived preroll timing. Additional per-map video lead-in must be tuned manually.

4. **XMA2 audio decoding** is handled by `decode_xma2_audio()` in `media_processor.py`, which invokes `vgmstream-cli` as a subprocess. The resulting WAV is validated for 48 kHz stereo format; mismatches are transcoded via FFmpeg.

### Community Tool Validation

The marker formula `markers[abs(startBeat)] / 48` (milliseconds) is validated by multiple community tools:
- **MediaTool** (JustDanceTools) - uses `markers[abs(startBeat)] / 48` for FFmpeg `-ss`
- **Unity2UbiArt** - uses `markers[abs(startBeat)] / 48` for audio cutting
- **ferris_dancing** - binary CKD parser confirms markers/startBeat/endBeat/videoStartTime structure
- **UBIART-AMB-CUTTER** - adds `+85ms` in its trimming approach (calibration reference)

---

## 5. Timing Formula (AMB Mode)

Timing is computed in two steps:

**Step 1: Determine total intro window length and any audio delay.**
```
intro_dur   = abs(v_override)      # total silence window = video pre-roll length
audio_delay = max(0, intro_dur - abs(a_offset))
              # if v_override has more pre-roll than a_offset, prepend silence
              # so audio content starts later and aligns with the OGG start
```

**Step 2: Determine audio content duration and fade point.**

*Primary path - marker data available:*
```
audio_content_dur = marker_preroll_ms / 1000.0
fade_start        = audio_delay + audio_content_dur - 0.200
amb_duration      = audio_delay + audio_content_dur
```

*Fallback path - no marker data (or `marker_preroll_ms` is `None`):*
```
audio_content_dur = abs(a_offset) + 1.355
fade_start        = audio_delay + abs(a_offset) + 1.155
amb_duration      = audio_delay + audio_content_dur
```

The `1.355s` fallback tail is intended to absorb WAV scheduling jitter (buffer load + system audio latency).

Example (`v_override = -2.145`, `marker_preroll_ms = 2060`):
```
intro_dur          = 2.145s
a_offset           = -(2060/1000) = -2.060s
audio_delay        = max(0, 2.145 - 2.060) = 0.085s
audio_content_dur  = 2.060s
amb_duration       = 0.085 + 2.060 = 2.145s
fade_start         = 0.085 + 2.060 - 0.200 = 1.945s
```

A `200ms` linear fade-out is applied at `fade_start` to avoid hard-cut handoff artifacts.

**Binary implementation:** These computed values are passed to FFmpeg in `generate_intro_amb()`:
- `trim_front_s` → FFmpeg `-ss` (input seek)
- `audio_content_dur` → FFmpeg `-t` (duration limit)
- `audio_delay` → FFmpeg `adelay` filter (silence padding)
- Output is always 48 kHz WAV written to `Audio/AMB/`.

---

## 6. AMB File Requirements

Each intro AMB uses three files under `Audio/AMB/`:

| File | Contents | Domain |
|---|---|---|
| `amb_{mapname}_intro.wav` | PCM 48 kHz stereo; generated by FFmpeg from OGG source | **Binary** (media_processor.py) |
| `amb_{mapname}_intro.ilu` | Sound descriptor (`category="amb"`, `playMode=1`, `loop=0`) | **Text** (generated Lua-like config) |
| `amb_{mapname}_intro.tpl` | Actor template that references SoundComponent + `.ilu` | **Text** (generated Lua-like config) |

The actor is referenced from `{MapName}_audio.isc` as a `SoundComponent` actor.

The `.ilu` `volume = 0` field is a dB offset (`0 = unity`), not a mute flag.

> [!NOTE]
> The `.ilu` and `.tpl` files are **pure text** — generated directly by `generate_intro_amb()` using Python string formatting. Only the `.wav` file is a binary artifact produced by FFmpeg.

---

## 7. Pipeline Integration

### Audio conversion flow (convert_audio)

The `convert_audio()` function in `media_processor.py` orchestrates the full audio pipeline for a map:

```
Input: source audio (.ogg, .wav, or .ckd)
  │
  ├─ CKD? → extract_ckd_audio_v1()  [vgmstream / header strip]
  │         └─ Failed? → write silent WAV + silent OGG fallback
  │
  ├─ Generate menu preview OGG
  │   ├─ Source is .ogg → shutil.copy2
  │   └─ Other format → FFmpeg transcode
  │
  └─ Generate engine WAV (48 kHz)
      ├─ a_offset == 0  → FFmpeg straight convert (-ar 48000)
      ├─ a_offset < 0   → FFmpeg trim front (-ss)
      └─ a_offset > 0   → FFmpeg pad silence (adelay filter)
```

### Cinematic AMB clip extraction

The `extract_amb_clips()` function scans the cinematic tape for `SoundSetClip` entries and extracts audio segments:
- Start time and duration are read from the **text-based** tape structure.
- Audio extraction is performed by **FFmpeg** with `-ss`, `-t`, and a 200ms fade-out filter.
- Existing clips >100KB are not overwritten.

### Intro AMB generation

When `INTRO_AMB_ATTEMPT_ENABLED` is `True` (default):

1. Reuses existing IPK intro AMB wrappers (`*_intro.tpl.ckd`) and replaces placeholder WAV content.
2. Generates missing intro AMB wrappers (`.tpl`/`.ilu`) and injects actor references when absent.
3. Populates eligible pre-roll AMB audio from source preroll audio where valid.
4. Continues orphan WAV CKD recovery (synthetic wrappers) before audio population.

Implementation detail: AMB path resolution and folder-case differences (`Audio/AMB` vs `audio/amb`) are handled defensively to avoid broken actor references on mixed-source layouts.

---

## 8. Limitations and Operator Expectations

1. **IPK video offset remains approximate by design:** marker-derived `v_override` cannot encode per-map video lead-in; manual VIDEO_OFFSET tuning is expected.
2. **Dependency-sensitive behavior:** Missing FFmpeg/FFprobe or vgmstream reduces decode/preview/install reliability, especially for IPK/XMA2 audio paths.
3. **Source-layout variability:** Mixed casing/path layouts and cache-only assets require fallback resolution logic; map-specific edge cases remain possible.

---

## 9. Runtime Dependencies Relevant to Timing

Audio timing and preview/install quality depend on the local toolchain:

| Dependency | Role in timing pipeline | Provisioned by |
|---|---|---|
| **FFmpeg / FFprobe** | Probing durations, transcoding OGG→WAV with offset trim/pad, generating AMB audio, applying gain | Must be on system `PATH` |
| **vgmstream** | Xbox 360 XMA2 decode for IPK maps (`decode_xma2_audio()` in `media_processor.py`) | `setup.bat` step 7/7 → `tools/vgmstream/` |
| **Playwright Chromium** | Fetch mode only — web-fetch workflows, not for pure local/IPK timing math | `setup.bat` step 2/7 |

If any dependency is missing or partially installed, expected behavior includes decode fallback paths, reduced preview confidence, and higher chance of manual offset correction.

> [!WARNING]
> FFmpeg is **not** auto-installed by `setup.bat`. It must be available on the system `PATH` before running the installer. Without FFmpeg, all audio conversion, AMB generation, and VP9→VP8 video transcoding will fail.
