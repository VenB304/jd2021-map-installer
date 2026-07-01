# JDU Unused Data: Improvement Opportunities

> **Last Updated:** June 2026 | **Applies to:** JD2021 Map Installer v2

This document catalogs data available in JDU (Just Dance Unlimited) server payloads that the
V2 installer pipeline either already consumes, intentionally ignores, or only partially uses.
Each item is ranked by potential quality impact.

This page was originally authored during V1/early-V2 transition and is now normalized to current
V2 behavior so status labels are actionable.

> **Related docs:**
> - `docs/02_core/DATA_MAPPING.md` -- field-level mapping of what is consumed today
> - `docs/06_research/KNOWN_GAPS.md` -- runtime limitations and known operational caveats

---

## V2 Operational Caveats (Read Before Prioritizing)

These constraints currently have higher practical impact than most remaining "unused data" items:

1. **Intro AMB behavior is temporarily constrained.** Intro ambient handling has active mitigation
   logic in V2; deterministic intro AMB parity is still being stabilized.
2. **IPK video timing is still approximate by design.** Binary source metadata does not always
   encode reliable lead-in, so **manual video offset tuning remains expected** for many IPK maps.
3. **External tools are mandatory for full-quality installs.** FFmpeg/FFprobe and vgmstream are
   required for complete decode/convert coverage; missing tools trigger fallbacks or degraded
   behavior.

Because of the above, implementation order below should be read as "data fidelity opportunities"
and not as a replacement for current operational reliability work.

---

## Tier 1 -- High Impact

### 1.1 Stape BPM / Signature Data (SlotClips) -- IMPLEMENTED

> **Implemented in V2:** The installer converts `.stape.ckd` through the CKD-to-Lua conversion
> path and writes full SlotClip timing/BPM/signature data when available. It falls back to the
> minimal stape only when source stape data is missing or conversion fails.

**What JDU provides:**
Each `.stape.ckd` contains an array of `SlotClip` entries with per-section timing data:

```json
{
  "__class": "SlotClip",
  "Id": 886920941,
  "TrackId": 0,
  "IsActive": 1,
  "StartTime": 912,
  "Duration": 384,
  "Bpm": 118.812500,
  "Signature": "0101010101010101030303030300",
  "Guid": "003d0112-fa0b-4f77-8a32-2c93700f8rt6"
}
```

A typical map has 19-40+ SlotClips covering the song, each with:
- `Bpm` -- beats per minute for that section
- `Signature` -- encoded beat pattern
- `StartTime` / `Duration` -- section boundaries in ticks
- `Guid` -- unique identifier per section

**Current V2 behavior:**
- Preferred path: pass through converted SlotClip payload.
- Fallback path: emit minimal stape with `TapeClock` and `MapName` for compatibility.

**Where in code:** `installers/game_writer.py` (`generate_text_files()` stape block) plus CKD conversion pipeline.

---

### 1.2 Tags (Song Categorization) -- IMPLEMENTED

> **Implemented in V2:** Tags are extracted from `songdesc.tpl.ckd` and passed through to
> generated `SongDesc.tpl`. If absent, V2 falls back to `["Main"]`.

**What JDU provides:**
Real categorization tags per song in `songdesc.tpl.ckd`:

```json
"Tags": ["Extreme", "Main"]
```

Common values include `"Main"`, `"Extreme"`, `"Cool"`, `"Happy"`, `"Calm"`, `"Classic"`,
`"Pop"`, `"Rock"`, etc.

**Current V2 behavior:**
Tags are emitted as Lua `VAL` entries from CKD source when available.

**Where in code:** `installers/game_writer.py` (`generate_text_files()` SongDesc generation).

---

### 1.3 MenuArt Shaders and Masks

**What JDU provides (ISC scene data):**
Menu art may use `alpha_mul_b.msh` with an additional mask texture channel for rounded corners.

```text
shaderPath = "World/_COMMON/MatShader/alpha_mul_b.msh"
```

**Current V2 output:**
V2 generates single-layer menuart shader configuration:

```lua
shaderPath = "World/_COMMON/MatShader/MultiTexture_1Layer.msh"
```

**Reference behavior:**
The GetGetDown on-disk reference also uses `MultiTexture_1Layer.msh`, so current V2 output is
reference-aligned. The visible difference is subtle (rounded vs sharp corners).

**Where in code:** `installers/game_writer.py` (`generate_text_files()` menuart shader assignment).

**Effort / priority:** Medium effort, low urgency while parity/reliability work remains active.

---

## Tier 2 -- Medium Impact

### 2.1 MotionPlatformSpecifics for PC -- RESOLVED (N/A)

> **Resolved as not applicable:** JDU data does not provide a `"PC"` key because JD PC scoring
> uses phone input mode assumptions. V2 correctly passes platform dictionaries as provided.

**What JDU provides:**
`MotionPlatformSpecifics` dictionaries keyed by console platform identifiers.

**Current V2 behavior:**
`parsers/binary_ckd.py` passes through `MotionPlatformSpecifics` without fabricating a PC profile.

---

### 2.2 Autodance FX Parameters -- RESOLVED

> **Resolved in V2:** `autodance/*.tpl.ckd` is converted and the full `AutoDanceFxDesc` payload
> is preserved. Guard logic prevents sync "Apply" flows from clobbering converted TPL data with
> empty stubs.

**What JDU provides:**
Rich `AutoDanceFxDesc` parameter sets (toon, halftone, slime, refraction, floor plane, etc.).

**Current V2 behavior:**
Converted CKD data overwrites fallback templates; no additional action required.

**Where in code:** `installers/game_writer.py` and extraction/conversion pipeline (`step_11_extract_moves`).

---

### 2.3 PhoneImages Path Source -- IMPLEMENTED

> **Implemented in V2:** `PhoneImages` paths are read from `songdesc.tpl.ckd` when present
> (cover + coach images). Convention-based reconstruction remains fallback-only.

**What JDU provides:**
Pre-computed phone image paths:

```json
"PhoneImages": {
    "Cover": "world/maps/badromance/menuart/textures/badromance_cover_phone.jpg",
    "coach3": "world/maps/badromance/menuart/textures/badromance_coach_3_phone.png",
    "coach2": "world/maps/badromance/menuart/textures/badromance_coach_2_phone.png",
    "coach1": "world/maps/badromance/menuart/textures/badromance_coach_1_phone.png"
}
```

**Current V2 behavior:**
Use CKD paths first; fall back to convention-derived paths only when missing in source payload.

**Where in code:** `installers/game_writer.py` (`generate_text_files()` PhoneImages section).

---

## Tier 3 -- Low Impact

### 3.1 DoubleScoringType

**What JDU may provide:** Integer songdesc field for duo-scoring mode.

**Current status in V2:** Not prioritized; engine defaults are acceptable for current workflows.

### 3.2 SubArtist / SubTitle / SubCredits

**What JDU may provide:** Additional metadata fields for featured artists/subtitles/extended credits.

**Current status in V2:** Not consistently available in observed payloads and not required for
install correctness.

### 3.3 AudioPreviewFadeTime

**What JDU may provide:** Float for preview fade timing when leaving song select.

**Current status in V2:** Not extracted; engine default fade remains acceptable.

---

## Priority Implementation Table

| # | Item | Effort | Quality Improvement | Status |
|---|------|--------|---------------------|--------|
| 1 | ~~Pass through Tags from CKD~~ | ~~Trivial~~ | ~~Song filter/categorization~~ | **DONE** |
| 2 | ~~Use CKD PhoneImages paths~~ | ~~Trivial~~ | ~~Path reliability~~ | **DONE** |
| 3 | ~~Use real stape SlotClip data~~ | ~~Medium~~ | ~~Beat sync (tempo changes)~~ | **DONE** |
| 4 | ~~Investigate PC MotionPlatformSpecifics~~ | ~~Investigation~~ | ~~Score accuracy~~ | **N/A** (phone scoring model) |
| 5 | MenuArt shader/mask switch | Medium | Rounded album art corners | Open (reference-aligned today) |
| 6 | ~~Autodance FX parameters~~ | ~~Complex~~ | ~~Autodance visual effects~~ | **DONE** |
| 7 | SubArtist/SubTitle metadata | Trivial | Richer song info | Open (low practical impact) |

Items marked **DONE** are implemented in current V2 behavior. Items marked **N/A** were
investigated and intentionally closed. Remaining open items are quality enhancements, not blockers.

---

## Relevance Assessment

This document remains relevant for V2 as a **data-fidelity backlog**, but it should not be used as
a primary operations guide. For installer behavior and user expectations, prioritize the current
limitations/operations documentation and troubleshooting runbooks.

---

*Baseline analysis sources: GetGetDown reference map and sampled JDU payload sets. This page is
status-normalized for V2 as of June 2026.*
