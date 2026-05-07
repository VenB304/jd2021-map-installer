# Camera Gesture Remediation Plan

Last updated: 2026-05-05

This document turns the console-side camera gesture research into an implementation plan for the JD2021 Map Installer. The goal is not just to understand the mismatch, but to describe how the installer pipeline should be corrected so JDNext camera gestures convert into Kinect gestures with stable scoring behavior.

## Problem Statement

The current JDNext conversion pipeline produces two visible failure modes:

1. Some converted maps behave like generic-perfect maps, where the scoring engine mostly returns Perfect results and the gesture is too forgiving.
2. Other converted maps behave like miss/ok-heavy maps, where the same style of choreography produces weak or inconsistent recognition.

The likely issue is not a single broken file. It is more likely a mismatch between:

- the console-side camera gesture model,
- the JDNext joint data that we extract,
- and the installer-side assumptions used when building Durango/Kinect gesture binaries.

## Evidence To Anchor The Fix

The console-side dump suggests the relevant camera scoring stack is centered around:

- `PlayerModel`
- `PlayerScoringData`
- `PlayerScoringDebugConfig`
- `PhoneCameraScoringSkeletonExtractModel`
- `BlazePoseModelRsc`
- `PoseNetModelRsc`
- `ImageGestureRecognizer`
- `GestureRecognizer`

The installer-side pipeline currently makes several strong assumptions in `gesture_compiler.py`:

- JDNext camera constraints are converted into Kinect-style edge thresholds.
- Near-zero constraints are filtered through a dead-zone.
- A template donor is used for the structural gesture shell.
- Scoring edges, gating edges, and parameter blocks are handled separately.

The remediation plan should treat these as the main surfaces to validate and, if needed, revise.

## Likely Mismatch Categories

### 1. Joint Mapping Mismatch

The first thing to verify is whether the installer maps JDNext joints to Kinect joints the same way the console does.

What to check:

- whether the console uses the same 14 practical joints the installer assumes,
- whether center joints are interpolated the same way,
- whether any joints are excluded or remapped during skeleton extraction,
- whether the installer is placing constraints on the same Kinect joint IDs that the scoring engine expects.

Implementation impact if confirmed:

- update the joint map used by gesture compilation,
- keep the mapping explicit and data-driven,
- and add a report that shows how many constraints land on each joint.

### 2. Scaling Mismatch

The installer currently relies on a fixed camera-to-Kinect scaling assumption. If that scale is wrong, the converted thresholds will drift toward either overly permissive or overly strict matching.

What to check:

- whether the console-side conversion from camera landmarks to scoring space uses a consistent scale,
- whether that scale differs for X and Y coordinates,
- whether the installer should use one global scale, separate axis scales, or no scale at all for specific edge classes.

Implementation impact if confirmed:

- centralize the scale factor in one place,
- remove competing scale constants,
- and validate the resulting threshold distribution against known-good gesture files.

### 3. Dead-Zone Mismatch

The current pipeline filters near-zero camera constraints. That can be correct if those values are padding or neutral noise, but it is risky if the console scoring model still depends on low-amplitude movement for intermediate judgments.

What to check:

- whether the console-side stack discards or retains low-value movement cues,
- whether some JDNext gestures carry meaningful low-amplitude pose data,
- whether the dead-zone is too aggressive for sparse gestures.

Implementation impact if confirmed:

- make dead-zone behavior configurable by gesture type or strictness,
- preserve a diagnostic count of filtered constraints,
- and avoid using a single hard cutoff without visibility.

### 4. Structural Gate Mismatch

The installer preserves gating edges in the donor template, which is likely correct in principle. The remaining question is whether the selected donor structure is close enough to the source map, and whether the state-to-joint relationships are being preserved consistently.

What to check:

- whether the donor template structure matches the target map’s gesture shape,
- whether state IDs and edge groups are preserved correctly,
- whether gating edges are ever being altered by side effects in later steps.

Implementation impact if confirmed:

- keep gating edges immutable,
- document which edge classes are donor-derived versus JDNext-derived,
- and add a structural integrity check after compilation.

### 5. Parameter Block Mismatch

The 13-float gesture parameter block should reflect the source gesture’s timing and complexity, not just a copied donor baseline.

What to check:

- whether the console-side model suggests a stable parameter range,
- whether the installer’s synthesized parameters fall outside that range,
- whether timing-derived values correlate with scoring quality.

Implementation impact if confirmed:

- normalize parameter synthesis around a single source model,
- keep timing-derived fields separate from spatial-threshold fields,
- and test the parameter block independently from the edge table.

## Implementation Plan

### Phase 1: Document the Canonical Model

Produce a short internal reference that states:

- which console-side symbols are treated as authoritative,
- which installer assumptions are currently inferred,
- and which values need direct validation before code changes.

Deliverable:

- a compact gesture model reference that the installer code can be checked against.

### Phase 2: Add Conversion Diagnostics

Before changing behavior, add diagnostics to the compiler path so each conversion can report:

- joint counts before and after filtering,
- per-joint distribution of constraints,
- threshold value ranges after scaling,
- number of scoring edges versus gating edges,
- parameter block values written to the donor/template.

Deliverable:

- a debug summary that can be compared across good and bad maps.

### Phase 3: Consolidate Conversion Rules

Reduce the chance of mixed behavior by ensuring the pipeline uses one documented conversion rule set for:

- joint mapping,
- threshold scaling,
- dead-zone filtering,
- donor selection,
- and parameter synthesis.

Deliverable:

- one clearly defined gesture compilation path, with fallback logic documented separately.

### Phase 4: Validate Against Representative Maps

Test the revised pipeline against at least two reference cases:

- one map that currently scores mostly Perfect,
- one map that currently trends toward Miss/OK results.

Compare:

- threshold distributions,
- filtered joint counts,
- edge class balance,
- and post-install scoring behavior.

Deliverable:

- a comparison table that shows which change affected the scoring outcome.

### Phase 5: Lock the Behavior Into Tests And Docs

Once the mismatch is resolved, add regression coverage and document the final rule set.

Deliverable:

- tests for the conversion path,
- a short operator note describing the expected scoring profile,
- and a follow-up note for any map classes that still need special handling.

## Acceptance Criteria

The remediation is complete when:

- JDNext camera gestures compile through one documented conversion path.
- The console-side joint/scoring assumptions and the installer-side mapping rules agree on the same joint model.
- The resulting converted maps no longer split into generic-perfect-only versus miss/ok-heavy behavior without an explicit map-specific reason.
- The installer can report why a gesture was filtered, scaled, or classified in a way that is traceable for debugging.

## Recommended Output For The Final Implementation Document

When this work is finished, the final documentation should include:

- the console-side gesture model summary,
- the exact installer-side mismatch that was corrected,
- the code paths that were changed,
- the validation maps that were used,
- and the residual limitations, if any.

That final document should read as an implementation plan and remediation record, not as a general research note.