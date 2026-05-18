# Gesture Compiler Audit Report - Confirmation & Fixes
**Date:** May 13, 2026  
**Auditor:** GitHub Copilot  
**Target:** `jd2021_installer/installers/gesture_compiler.py`

---

## Executive Summary

All four critical issues identified in the pipeline audit have been **CONFIRMED** through direct source code inspection:

✅ **Issue 1 (🔴 Critical):** Gating Padding Omission Bug — **CONFIRMED & UNFIXED**  
✅ **Issue 2 (🟡 High):** Static Variance Thresholding — **CONFIRMED & UNFIXED**  
✅ **Issue 3 (🟡 High):** Flat Dead-Zone Filtering — **CONFIRMED & UNFIXABLE AS-IS**  
✅ **Issue 4 (🟢 Medium):** Scale Factor Disparity — **CONFIRMED & UNFIXED**  
✅ **Issue 5 (🧹 Cleanup):** Dead Code Paths — **CONFIRMED & REMOVABLE**  

---

## 1. 🔴 CRITICAL: Gating Padding Omission Bug (Miss-Heavy Behavior)

### Location
[gesture_compiler.py](gesture_compiler.py#L915-L1043)

### Problem Statement
When a non-gating scoring edge (detected from the template as `is_gating = False`) is later **promoted** to a structural gating edge (because `jdnext_joint in structural_joints`), the padding calculation is stale.

### Proof of Bug

**Lines 1001-1012** (Padding calculation):
```python
# Dynamically pad the threshold based on AdaBoost evaluation direction
padding = max(abs(expected) * 0.4, 0.25)

# If this is a gating/veto edge (ta > 10.0), double the padding.
# Gating edges are pass/fail boundaries.
if is_gating:
    padding *= 2.0
```

**Lines 1038-1043** (Promotion logic):
```python
if jdnext_joint in structural_joints:
    # Structural edge: make it a gating edge (high |ta| for strict checking)
    ta_val = 15.0 if ta > 0 else -15.0
    logger.debug("  state_id=%d (joint=%d): synthetic gating edge", sid, jdnext_joint)
```

### The Logic Error

1. **Pass 1 (Line 916):** `is_gating = (abs(ta) > 10.0)` evaluates to `False` for scoring edges in the template.
2. **Pass 2 (Line 1001-1007):** Padding is calculated using the scoring-edge formula. Since `is_gating = False`, **padding is NOT doubled**.
3. **Pass 2 (Line 1039):** The edge is promoted to `ta_val = ±15.0` (making it a strict veto gate), but the narrow padding remains.

**Result:** The classifier has extremely tight bounds (narrow padding) despite being a hard veto gate. Natural movement variance causes immediate "Miss" events, creating the miss-heavy behavior.

### Correct Behavior

Synthetic gating edges should have **doubled padding** to allow natural human variance while still rejecting truly bad input. The fix is to evaluate `is_gating` **after** the promotion decision.

### Recommended Fix

Replace the padding logic to account for synthesized gating:

```python
# Determine if edge will be gating (either original or synthesized)
is_gating_final = is_gating or (
    not is_gating and jdnext_joint in structural_joints
)

# Dynamically pad based on final gating status
padding = max(abs(expected) * 0.4, 0.25)
if is_gating_final:
    padding *= 2.0  # Now applies to BOTH original AND synthetic gates!
```

---

## 2. 🟡 HIGH: Static Variance Thresholding (Slow Song Failures)

### Location
[gesture_compiler.py](gesture_compiler.py#L882-L892)

### Problem Statement
The structural joint classifier uses a **flat, fixed multiplier** of `0.5` for the standard deviation threshold, which does not adapt to choreography speed/tempo.

### Proof of Issue

```python
# Lines 882-892
joint_variances = {}
for jid, vals in per_joint.items():
    if len(vals) > 1:
        joint_variances[jid] = statistics.stdev(vals)
    else:
        joint_variances[jid] = 0.0

mean_variance = statistics.mean(joint_variances.values()) if joint_variances else 0.0
std_variance = statistics.stdev(joint_variances.values()) if len(joint_variances) > 1 else 0.1

# Joints with variance > mean+0.5*std are structural (high discriminative power)
structural_joints = {
    jid for jid, var in joint_variances.items() 
    if var > (mean_variance + 0.5 * std_variance)  # <-- FLAT 0.5 MULTIPLIER
}
```

### The Problem

- **Slow/Lyrical Songs:** Variance across all joints is naturally low. `std_variance` shrinks. The threshold becomes very tight. Minor noise in joint trajectories gets promoted to structural gates, creating false positives.
- **Fast/Energetic Songs:** Variance is high. Almost all joints exceed the threshold, making the entire choreography overly restrictive with too many veto gates.

### Recommended Fix

Implement an **adaptive coefficient** $k$ that scales based on the median velocity/speed of the entire choreography:

```python
# Compute global choreography activity level
all_speeds = []
for f in range(num_frames):
    frame_speeds = []
    for jid in range(15):
        vx, vy, vz = Vx[f, jid], Vy[f, jid], Vz[f, jid]
        speed = (vx**2 + vy**2 + vz**2) ** 0.5
        frame_speeds.append(speed)
    all_speeds.append(statistics.mean(frame_speeds) if frame_speeds else 0.0)

median_speed = statistics.median(all_speeds) if all_speeds else 0.5
# Map speed to activity coefficient: slow songs (0.2) → k=0.3, fast songs (1.0+) → k=0.7
speed_normalized = min(median_speed / 1.0, 1.0)  # Normalize to [0, 1]
k = 0.3 + (speed_normalized * 0.4)  # Scale from 0.3 to 0.7

# Apply adaptive threshold
structural_joints = {
    jid for jid, var in joint_variances.items() 
    if var > (mean_variance + k * std_variance)
}

logger.debug(
    "Adaptive gating: median_speed=%.3f, k=%.3f, %d structural joints",
    median_speed, k, len(structural_joints)
)
```

---

## 3. 🟡 HIGH: Flat Dead-Zone Filtering (Subtle Movement Loss)

### Location
[gesture_compiler.py](gesture_compiler.py#L607-L628)

### Problem Statement
A **hardcoded, fixed dead-zone threshold** of `0.14` is applied uniformly across all joints and all songs, filtering out subtle hand/wrist placements near the body center.

### Proof of Issue

```python
# Lines 75 & 607
_DEAD_ZONE_MAX = 0.14  # <-- FLAT CONSTANT

# Lines 607-628
dead_zone = _DEAD_ZONE_MAX
filtered = [(jid, v) for jid, v in joint_constraints if abs(v) > dead_zone]
```

### The Problem

- **Fine Hand Movements:** Wrist or hand placements close to the torso center (within `±0.14` normalized units) are completely filtered out, losing active choreographic gestures.
- **Per-Joint Variance:** Different joints have naturally different movement ranges. Wrists move much more than hips. A single dead-zone value is inappropriate.

### Recommended Fix

Implement **per-joint adaptive dead-zoning** based on each joint's movement range:

```python
# Compute per-joint min/max to establish activity range
per_joint_range = {}
for jid, vals in per_joint.items():
    if vals:
        min_v = min(vals)
        max_v = max(vals)
        range_v = max_v - min_v
        per_joint_range[jid] = range_v
    else:
        per_joint_range[jid] = 0.0

# Adaptive dead-zone: joints with tiny ranges get tiny dead-zones
# Joints with large ranges get proportionally larger dead-zones
filtered = []
for jid, v in joint_constraints:
    joint_range = per_joint_range.get(jid, 0.5)
    # Dead zone = min(10% of joint's own range, global max 0.14)
    adaptive_dead_zone = min(joint_range * 0.1, 0.14)
    
    if abs(v) > adaptive_dead_zone:
        filtered.append((jid, v))

logger.debug(
    "Adaptive dead-zone: %d/%d constraints kept; "
    "per-joint zones ranged from %.3f to %.3f",
    len(filtered), len(joint_constraints),
    min(per_joint_range.values()) if per_joint_range else 0.0,
    max(per_joint_range.values()) if per_joint_range else 0.0
)
```

---

## 4. 🟢 MEDIUM: Scale Factor Disparity (2.28 vs 1.97)

### Location
Two conflicting constants in [gesture_compiler.py](gesture_compiler.py#L75, L1204)

### Problem Statement
Two different scale factors are used inconsistently:
- **Line 101:** `_JDNEXT_TO_DURANGO_SCALE = 1.97` (used in hybrid compilation)
- **Line 1204:** `_CAM_TO_KINECT_SCALE = 2.28` (used in `_compile_with_donor`)

### Justification for 2.28

Forensic standard-deviation analysis (MakeItJingle cross-format study):
- Camera constraint std = 0.434
- Kinect threshold std = 0.990
- **True scale = 0.990 / 0.434 = 2.28×**

The `1.97` constant is an approximation that underscales by ~13.6%, compressing coordinates and pushing thresholds toward the dead zone.

### Recommended Fix

Consolidate to `2.28` as the canonical constant and apply it consistently:

```python
# Single authoritative scale factor (forensically justified)
_JDNEXT_TO_KINECT_SCALE = 2.28
```

Update all usages to reference this single constant instead of the two conflicting values.

---

## 5. 🧹 CLEANUP: Dead Code Paths

### Location
[gesture_compiler.py](gesture_compiler.py#L1150-L1294)

### Issue

The function `_compile_with_donor()` (165 lines) is **completely unused**:
- Never called from `compile_gesture_from_scratch()` (which now uses `compile_hybrid_gesture()`)
- Never called from public API
- Duplicates logic from the working hybrid path but lacks biomechanical kinematics and proper gating synthesis

### Recommended Action

**Remove entirely:**
- `_compile_with_donor()` (lines 1150-1294)
- `_build_edge_table()` (lines 1392-1499) — only called by `_compile_with_donor()`

Keep:
- `_find_donor_gesture()` — still used by hybrid compiler
- `compile_hybrid_gesture()` — the canonical, working path
- `_compile_with_donor()`'s internal scale factor logic can be inlined if needed elsewhere

---

## Impact Assessment

| Fix | Severity | User Impact | Test Coverage Needed |
|:---|:---|:---|:---|
| **#1: Gating Padding** | Critical | Eliminates miss-heavy maps | Unit test: synthetic gating edge padding verification |
| **#2: Adaptive Variance** | High | Fixes slow/lyrical song recognition | Integration test: verify structural joint counts vs tempo |
| **#3: Adaptive Dead-Zone** | High | Preserves subtle hand/wrist moves | Unit test: per-joint range measurement & filtering |
| **#4: Scale Factor** | Medium | Aligns coordinates with Kinect expectation | Forensic validation: variance ratio verification |
| **#5: Code Cleanup** | Low | Reduces maintenance burden | Smoke test: ensure hybrid path still works |

---

## Verification Checklist

- [ ] Fix #1 (Gating Padding): Deploy and test on previously miss-heavy maps
- [ ] Fix #2 (Adaptive Variance): Verify slow songs now have appropriate gating density
- [ ] Fix #3 (Dead-Zone): Manually inspect wrist-heavy choreographies for recovery
- [ ] Fix #4 (Scale): Validate coordinate standard deviations match Kinect expectation
- [ ] Fix #5 (Cleanup): Run full test suite; ensure no regressions

---

**Next Steps:**
1. Implement all fixes in `gesture_compiler.py`
2. Add unit tests for adaptive thresholding
3. Validate on regression test set (10+ varied songs)
4. Deploy to production
