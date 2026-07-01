# Camera Gesture Remediation: Research Log

Last updated: 2026-06-29

> [!WARNING]
> **Branch abandoned — experimental status.** The dedicated `feat/jdnext-gesture` branch where this work was primarily developed was abandoned without being merged to master. The gesture compiler modules (`gesture_compiler.py`, `biomechanics.py`, `hmm_generator.py`) are present in master as an **opt-in experimental feature** (`convert_jdnext_gestures = False` by default). Output quality is known to be imperfect — compiled `.gesture` binaries are structurally valid but scoring consistency varies significantly by map.
>
> The production-ready workaround for JDNext maps without native gestures is **MSM mirroring**: the pipeline automatically copies the right-hand controller (`.msm`) track to the gesture track (`.gesture`) 1:1 when native Kinect gesture data is absent. This preserves original controller choreography and resolves the `0/0` gold moves grading issue on PC/Switch.

---

## Problem Statement (Original)

The JDNext camera/phone gesture conversion pipeline produced two visible failure modes:

1. **Over-forgiving:** Some converted maps behaved like generic-perfect maps — the scoring engine mostly returned Perfect results regardless of choreography execution.
2. **Under-forgiving:** Other maps produced miss/ok-heavy results, where the same style of choreography yielded weak or inconsistent recognition.

The root cause was not a single broken file but a systematic mismatch between:
- the Durango (Xbox One) Kinect gesture model,
- the 2D joint data extracted from JDNext camera bundles,
- and the installer-side assumptions used when building Durango `.gesture` binaries.

---

## Console-Side Gesture Engine (Research Summary)

The Durango `.gesture` file is an **AdaBoost ensemble classifier** — not a Hidden Markov Model or a plain joint coordinate timeline. It contains:

- A set of **decision stumps**, each testing a single feature (joint velocity, acceleration, bone angle, joint torque, muscle force, or optical flow magnitude) against a threshold pair (`ta`, `tb`).
- Each stump carries a **weight** (`ta` sign encodes pass/fail direction; `tb` = secondary threshold).
- The ensemble vote determines the scoring output (Perfect / OK / Miss).

**36 feature types** are addressed by stumps. Types 18-23 (infrared optical flow, `TimeSpaceAngles`) depend on hardware not available in the PC/camera scoring path and must be pruned.

**Phone/camera scoring bypass:** When phone or camera scoring is active (`JD_PhoneScoringData`), the engine **completely bypasses the `.gesture` file** and reads directly from 3D skeletal coordinates serialized in `.msm` files. This means:
- Modifying gesture thresholds has **zero effect** on camera scoring behavior.
- The `.gesture` file is only consumed by the Kinect/controller scoring path.
- JDNext gesture conversion targets the **Kinect adapter path**, not camera/phone scoring.

The console-side scoring stack for camera/phone:
- `PlayerModel`
- `PlayerScoringData` / `PlayerScoringDebugConfig`
- `PhoneCameraScoringSkeletonExtractModel`
- `BlazePoseModelRsc` / `PoseNetModelRsc`
- `ImageGestureRecognizer` → `GestureRecognizer`

---

## Implemented Fixes

### Phase 1: Canonical Model Documentation

The joint model, scale factors, and edge classification rules are now documented in `gesture_compiler.py` module-level constants:

```python
_JDNEXT_TO_DURANGO_JOINT_MAP = {
    1: 20,   # ShouldersCenter → SpineShoulder
    2: 4,    # ShoulderLeft
    3: 8,    # ShoulderRight
    4: 5, 5: 9,      # Elbows
    6: 6, 7: 10,     # Wrists
    8: 0,    # HipsCenter → SpineBase
    9: 12, 10: 16,   # Hips
    11: 13, 12: 17,  # Knees
    13: 14, 14: 18,  # Ankles
}

# JDNext [-1, +1] → Durango [-3, +3]; scale derived from MakeItJingle Rosetta Stone
_JDNEXT_SCALE_FACTOR = 2.28
```

The 13-float Kinect parameter block is calibrated from real Kinect gesture files:
```python
_KINECT_MEAN_PARAMS = (
    739.969, 0.049, 0.000,
    0.332, 0.459, 0.360, 0.030,
    -0.653, -0.333, -0.475, 0.060,
    144.714, 58.004,
)
```

### Phase 2: Z-Axis Recovery (biomechanics.py)

JDNext camera data is 2D. Full 3D reconstruction is performed in `installers/biomechanics.py` using analytical forward kinematics:

- **Root placement:** Joint 8 (HipsCenter) depth estimated from torso-to-viewport ratio and Kinect view range (1.0–4.0 m).
- **Single-pass tree traversal:** Each child joint's Z coordinate is recovered from the known parent Z + anthropometric bone length (`STANDARD_BONES` table, referenced to H_REF = 1.70 m) using foreshortening: `missing_sq = target_len² − (Δx² + Δy²)`.
- **Symmetry handling:** Forward/backward-facing direction is detected from shoulder-to-hip vector sign.
- **Smoothing:** Savitzky-Golay filter (window=7, polyorder=2) applied to all joint trajectories before derivative computation.

This replaces the originally planned L-BFGS-B global optimization approach, achieving equivalent Z recovery approximately 2000× faster.

### Phase 3: Simulated Kinetics (biomechanics.py)

`installers/biomechanics.py` computes the full kinematic feature set required by Durango stumps:

- **Velocity / Acceleration:** First and second derivatives of Savitzky-Golay-smoothed coordinates.
- **Angular velocity:** Cross product of sequential limb direction vectors.
- **Angular acceleration:** Temporal gradient of angular velocity.
- **Torque:** `I * α` where `I = m * r²` (moment of inertia for each limb segment).
- **Muscle force:** `Torque / moment_arm` (moment arm = 0.05 m).
- **DiffMuscleForce:** Temporal difference of MuscleForce.

### Phase 4: Ensemble Re-weighting and Edge Classification (gesture_compiler.py)

**Pruned stumps (Pass 1 — weight redistribution):**
- Types 18-23 (optical flow, TimeSpaceAngles) depend on unavailable Kinect IR hardware.
- Pruned stumps receive `ta = 0.0`, removing their vote.
- A gamma multiplier `γ = total_original_weight / total_surviving_weight` is applied to surviving kinematic/torque stumps to preserve ensemble calibration.

**Edge classification:**
- **Scoring edges:** `|ta| ≤ 1.0` — quantized to 0.1 steps using `_QUANT_WEIGHTS` distribution (bell-curve matching real Kinect files).
- **Gating edges:** `|ta| > 10.0` — structural veto gates; kept immutable from donor template.
- **Boundary edges:** `ta ∈ [1, 10]` — treated as scoring edges with moderate weight.

**Adaptive dead-zone per joint:**
- Zone = `min(10% of joint range, 0.14)`, floor = 0.03.
- Filters micro-jitter and padding/null constraints.
- Configurable by gesture type; constraint counts are logged for diagnostics.

**Structural gating detection:**
- Activity coefficient `k` scales from 0.3 (slow tempo) to 0.7 (fast tempo) based on median velocity.
- Variance threshold = `mean_variance + k * std_variance`.
- High-variance joints (structurally significant motion) → synthetic gating edges (`|ta| = ±15.0`).
- Low-variance joints → scored edges with gamma-scaled thresholds.

**Double padding on gating edges:**
- Native gating edges from the donor template receive `2×` padding on both edge ends.
- Synthetic gating edges (promoted from active scoring joints) also receive `2×` padding.
- Prevents random miss penalties at gate boundaries while maintaining structural tolerance.

### Phase 5: Validation (Inconclusive)

Validation against reference maps showed partial improvement over the original failure modes, but results remained inconsistent across songs. Some maps produced more appropriate scoring variation; others still showed generic-perfect or miss-heavy behavior. The branch was abandoned at this point — the compiled `.gesture` files are structurally valid but the scoring model does not generalize reliably across the variety of JDNext choreographies.

The diagnostic output (joint counts before/after filtering, per-joint constraint distribution, scoring vs. gating edge counts, parameter block values) is logged at `detailed` level during compilation and can be used as a starting point if the effort is resumed.

---

## Configuration

Gesture compilation is **opt-in** (`convert_jdnext_gestures = False` by default in `AppConfig`). To enable:

1. Set `convert_jdnext_gestures = true` in Settings → Advanced.
2. Ensure `gesture_template_path` points to a valid Durango donor template (default: `./assets/gesture_templates/durango_template.gesture`).

> [!IMPORTANT]
> JDNext camera/phone scoring uses `.msm` files via `JD_PhoneScoringData` and **bypasses the `.gesture` file entirely**. Gesture compilation only affects the Kinect adapter scoring path. For maps played with phone or camera input, `.gesture` quality has no bearing on scoring behavior.

---

## Residual Limitations

| Limitation | Status |
|------------|--------|
| Scoring consistency varies significantly by map; output quality is imperfect | Branch abandoned — known issue |
| Camera/phone scoring path is not affected by `.gesture` quality | By design — console-side bypass via `.msm` / `JD_PhoneScoringData` |
| ORBIS `.gesture` files remain substituted (digit-stripping workaround) | Active gap — see [KNOWN_GAPS.md](KNOWN_GAPS.md) |
| Optical flow features (types 18-23) cannot be recovered without IR hardware | By design — pruned and weight-redistributed |
| JDNext maps without skeleton data fall back to donor template only | Expected — sparse gesture data handled by structural gating |
