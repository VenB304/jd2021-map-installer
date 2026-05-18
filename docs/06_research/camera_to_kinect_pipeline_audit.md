# Pipeline Audit: Camera-to-Kinect Gesture Conversion

This document presents a technical audit and evaluation of the **Just Dance Camera-to-Kinect Gesture Conversion Pipeline** implemented in the `jd2021-map-installer` repository. It outlines which aspects of the architecture are correct and must remain the same, and details critical bugs, structural gaps, and legacy dead code that must be changed or adapted to achieve natural, arcade-perfect scoring on Kinect.

---

## 1. What Must Remain the Same (The Strong Pillars)

These elements are mathematically sound, highly performant, and structurally required. They form the correct foundation of the translation pipeline and should be preserved.

### 1.1 The 15-Joint Downsampling Schema
*   **Evaluation:** The mapping between MediaPipe BlazePose's 33 landmarks and the standard 15 Kinect V1 joints (as defined in `_JDNEXT_TO_DURANGO_JOINT_MAP` in [gesture_compiler.py](file:///c:/Github/jd2021-map-installer/jd2021_installer/installers/gesture_compiler.py#L174)) is correct.
*   **Justification:** This mapping matches the native, decompiled C# classes of the Ubisoft Controller App (`com.ubisoft.dance.justdancecontroller2023`). Any modification to this map would cause structural joint mismatches in Durango's state machine, leading to immediate matching failures.

### 1.2 Analytical 3D Depth Synthesis
*   **Evaluation:** The analytical Forward Kinematics approach used in [biomechanics.py](file:///c:/Github/jd2021-map-installer/jd2021_installer/installers/biomechanics.py#L119) (`synthesize_depth_analytical`) is highly accurate and extremely efficient.
*   **Justification:** Instead of running a slow numerical optimizer (like L-BFGS-B), this algorithm reconstructs missing Z-depth analytically by resolving foreshortening against rigid anthropometric limb length ratios (derived from a standard $1.7\text{m}$ adult height). It executes ~2000x faster than legacy optimization steps and correctly dampens coordinates with a Savitzky-Golay noise filter, preventing "velocity explosions."

### 1.3 Kinematic Derivative Computation
*   **Evaluation:** Calculating joint velocity, acceleration, torque, and muscle force trajectories using Savitzky-Golay filters is correct and necessary.
*   **Justification:** The legacy Kinect classifier stumps (ETypes) evaluate physical speed and acceleration. Deriving smooth derivatives from raw 2D input curves allows the hybrid compiler to feed realistic biomechanical metrics to the matching engine, simulating a physical performer.

### 1.4 Hybrid Bootstrapping (Preserving the Template Shell)
*   **Evaluation:** Re-writing an existing template's edge-table (`durango_template.gesture` or `discorope.gesture`) rather than compiling the binary from scratch is the only reliable deployment method.
*   **Justification:** The UbiArt engine's AdaBoost matching engine enforces complex, non-obvious binary layout rules. Generating the state table from scratch (`hmm_generator.py`) frequently results in subtle packing mismatches, causing the PC client to crash or instantly reject moves. Hybrid compilation safely preserves the original donor shell, ensuring 100% engine compatibility.

---

## 2. What Must Be Changed / Adapted (The Critical Gaps)

These sections represent active bugs, code redundancies, and structural oversights causing converted maps to oscillate between **"always perfect" (exploit-heavy)** and **"impossible to score" (miss-heavy)** behaviors.

### 2.1 🔴 The Gating Padding Omission Bug (Causes "Miss-Heavy" Behavior)
*   **The Issue:** Converted maps often suffer from severe "miss-heavy" tracking where natural, correct dancing results in failing scores.
*   **Root Cause:** In [gesture_compiler.py](file:///c:/Github/jd2021-map-installer/jd2021_installer/installers/gesture_compiler.py#L1011), the compiler doubles threshold padding for gating edges:
    ```python
    if is_gating:
        padding *= 2.0
    ```
    However, `is_gating` is evaluated from the *original* template's coefficient *before* the synthesis loop runs (based on `e_info['is_gating']` at line 916, which checks if the donor template's $t_a > 10.0$).
    When a non-gating scoring edge is *promoted* to a synthetic gating edge because it belongs to a structural joint (line 1039: `ta_val = 15.0`), **it does NOT receive the doubled padding!**
*   **Consequence:** The edge is given an extremely high gating weight (`|ta| = 15.0`, making it a hard pass/fail veto) but is left with a very narrow, tight scoring padding. The player must match the physical trajectory down to fractions of a centimeter, resulting in immediate, unfair veto failures (Misses).
*   **Fix:** Set `is_gating = True` (or explicitly double the padding) if `jdnext_joint` belongs to the `structural_joints` pool *before* calculating `tb_val`.

---

### 2.2 🟡 Static Variance Thresholds (Slower/Graceful Song Failures)
*   **The Issue:** Slower, graceful choreographies (such as contemporary or slow lyrical tracks) often fail conversion or lose all scoring accuracy, while very fast energetic tracks have too many structural gates.
*   **Root Cause:** The structural joint classifier is completely static:
    ```python
    var > (mean_variance + 0.5 * std_variance)
    ```
    On a slow song, the variance across all joints is extremely low. The standard deviation `std_variance` shrinks toward zero, resulting in noisy joint classification where subtle, insignificant movements are promoted to strict gating limits. On fast, erratic songs, almost all joints exceed the threshold, making the entire choreography overly restrictive.
*   **Fix:** Make the variance coefficient adaptive. Add a global choreographic activity coefficient:
    $$\text{Threshold} = \text{mean\_variance} + k \times \text{std\_variance}$$
    Where $k$ dynamically scales based on the median speed/velocity of the overall choreography.

---

### 2.3 🟡 Fixed Dead-Zone Hard Limit
*   **The Issue:** In [gesture_compiler.py](file:///c:/Github/jd2021-map-installer/jd2021_installer/installers/gesture_compiler.py#L94), `_DEAD_ZONE_MAX` is hardcoded to `0.14`.
*   **Root Cause:** This dead zone is intended to filter out flat padding/neutral coordinates. However, for moves that require extremely fine hand/wrist positioning close to the torso center, `0.14` is too aggressive, filtering out the active movement curve entirely and triggering wrist fallbacks or sparse joint arrays.
*   **Fix:** Scale the dead zone adaptively based on the individual joint's overall movement range (minimum/maximum coordinate spread) rather than using a flat, hard cutoff across all joints and songs.

---

### 2.4 🟢 Scale Factor Disparity & Consolidation
*   **The Issue:** Two competing scale constants exist in the script: `_JDNEXT_TO_DURANGO_SCALE = 1.97` (used in hybrid calibration) and `_CAM_TO_KINECT_SCALE = 2.28` (found in `_compile_with_donor`).
*   **Justification:** Forensic analysis of raw console telemetry establishes $2.28\times$ as the accurate representation for direct position coordinate expansion (std-deviation ratio). Using `1.97` compresses coordinates slightly too much, pushing thresholds closer to the dead zone.
*   **Fix:** Centralize and standardize the scale factor to $2.28\times$ for direct position constraints mapping, keeping it separated from the raw physical meter scaling inside `biomechanics.py`.

---

### 2.5 🧹 Dead Code & Pipeline Pruning
*   **The Issue:** The file contains complex, redundant paths that confuse maintenance.
*   **Identified Dead Code:**
    1.  `_compile_with_donor()` (lines 1134-1287) is entirely unused. It is legacy development code that attempt direct coordinate replacement but lacks the biomechanical kinematics and gating calculations of `compile_hybrid_gesture`.
    2.  `_decompile_jdnext_fallback()` is a useful heuristic but is rarely hit; ensure it is logged clearly as a fallback warning.
    3.  `_build_edge_table()` is unused since from-scratch HMM generation is disabled.
*   **Fix:** Prune `_compile_with_donor()` and `_build_edge_table()` from the script. Consolidate around the robust `compile_hybrid_gesture` path.

---

## 3. Comparative Audit Summary

| Component | Status | Code Action | Operational Impact |
|:---|:---:|:---|:---|
| **Joint Schema** | ✅ Preserve | None. Keep `_JDNEXT_TO_DURANGO_JOINT_MAP` as-is. | Essential for joint integrity. |
| **Biomechanical Engine** | ✅ Preserve | Keep `biomechanics.py` analytical model. | Delivers continuous kinematic values. |
| **Gating Synthesis** | ⚠️ Adapt | **Fix Gating Padding Bug!** Match padding to the *synthesized* gating status. | Resolves the "miss-heavy" tracking error. |
| **Variance Heuristics** | ⚠️ Adapt | Dynamic variance multiplier ($k$) based on overall movement speed. | Restores scoring on slow, lyrical tracks. |
| **Dead Zone** | ⚠️ Adapt | Adaptive per-joint noise thresholding instead of flat `0.14`. | Captures subtle hand placements. |
| **Scale Factors** | ⚠️ Adapt | Consolidate competing constants; default to forensic $2.28\times$. | Aligns positional bounds with Kinect expectation. |
| **Legacy Code** | 🧹 Clean | Remove `_compile_with_donor()` and unused helpers. | Reduces code bloating and maintenance risk. |
