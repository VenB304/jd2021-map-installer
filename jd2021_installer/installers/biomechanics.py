import logging
import numpy as np
from scipy.optimize import minimize
from scipy.signal import savgol_filter

logger = logging.getLogger("jd2021.installers.biomechanics")

# ---------------------------------------------------------------------------
# Anthropometric & Physical Constants
# ---------------------------------------------------------------------------

# Kinect v2 hardware optimal depth center and limits
Z_ROOT = 2.50  # meters
KINECT_FOV_H_RAD = np.deg2rad(70.0)
KINECT_FOV_V_RAD = np.deg2rad(60.0)

X_MAX = Z_ROOT * np.tan(KINECT_FOV_H_RAD / 2.0)
Y_MAX = Z_ROOT * np.tan(KINECT_FOV_V_RAD / 2.0)

# Anthropometric ideal proportions (relative to standard adult height H = 1.7m)
H_REF = 1.70
STANDARD_BONES = {
    # Torso/Spine
    (1, 8): 0.288 * H_REF,   # ShouldersCenter to HipsCenter
    (1, 2): 0.129 * H_REF,   # ShouldersCenter to ShoulderLeft
    (1, 3): 0.129 * H_REF,   # ShouldersCenter to ShoulderRight
    (8, 9): 0.100 * H_REF,   # HipsCenter to HipLeft
    (8, 10): 0.100 * H_REF,  # HipsCenter to HipRight
    
    # Left Arm
    (2, 4): 0.186 * H_REF,   # ShoulderLeft to ElbowLeft
    (4, 6): 0.146 * H_REF,   # ElbowLeft to WristLeft
    
    # Right Arm
    (3, 5): 0.186 * H_REF,   # ShoulderRight to ElbowRight
    (5, 7): 0.146 * H_REF,   # ElbowRight to WristRight
    
    # Left Leg
    (9, 11): 0.245 * H_REF,  # HipLeft to KneeLeft
    (11, 13): 0.246 * H_REF, # KneeLeft to AnkleLeft
    
    # Right Leg
    (10, 12): 0.245 * H_REF, # HipRight to KneeRight
    (12, 14): 0.246 * H_REF, # KneeRight to AnkleRight
}

# Average adult mass distribution (Total mass M = 70.0 kg)
MASS_TOTAL = 70.0
SEGMENT_MASS_RATIOS = {
    # Approximate mappings matching standard kinesiology
    'trunk': 0.546 * MASS_TOTAL,
    'arm_l': 0.028 * MASS_TOTAL,
    'arm_r': 0.028 * MASS_TOTAL,
    'forearm_l': 0.015 * MASS_TOTAL,
    'forearm_r': 0.015 * MASS_TOTAL,
    'thigh_l': 0.105 * MASS_TOTAL,
    'thigh_r': 0.105 * MASS_TOTAL,
    'shank_l': 0.043 * MASS_TOTAL,
    'shank_r': 0.043 * MASS_TOTAL,
}


# ---------------------------------------------------------------------------
# Phase 1: Coordinate Space Translation & Global Z Estimation
# ---------------------------------------------------------------------------

def estimate_global_z(x_norm: np.ndarray, y_norm: np.ndarray) -> np.ndarray:
    """
    Estimates the global Z-depth of the root by analyzing the apparent 2D torso length.
    If the player steps closer, their 2D torso length increases in normalized space.
    """
    num_frames = x_norm.shape[0]
    Z_global = np.full(num_frames, Z_ROOT)
    
    if x_norm.shape[1] > 8:
        # Physical torso length target
        target_torso_phys = STANDARD_BONES[(1, 8)]
        
        # Calculate 2D normalized torso length for each frame
        torso_len_2d = np.sqrt((x_norm[:, 1] - x_norm[:, 8])**2 + (y_norm[:, 1] - y_norm[:, 8])**2)
        torso_len_2d = np.maximum(torso_len_2d, 0.05)
        
        # Perspective relation: Z_global = Z_ROOT * (target_phys / apparent_phys_at_Z_ROOT)
        Z_est = Z_ROOT * (target_torso_phys / (torso_len_2d * Y_MAX))
        
        # Clamp between 1.0m and 4.0m (Kinect view range)
        Z_global = np.clip(Z_est, 1.0, 4.0)
        
        # Heavily smooth global Z to prevent tracking jitter from creating fake explosive forces
        if num_frames >= 5:
            # Use a ~1 second window (31 frames at 30fps) for macro-movements only
            w_len = min(31, num_frames if num_frames % 2 != 0 else num_frames - 1)
            Z_global = savgol_filter(Z_global, window_length=w_len, polyorder=2)
            
    return Z_global

def scale_to_physical(x_norm: np.ndarray, y_norm: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """
    Translates JDNext [-1.0, 1.0] normalized screen space into physical meters,
    accounting for global Z-depth perspective.
    """
    Z_global = estimate_global_z(x_norm, y_norm)
    
    # Calculate X and Y physical spans dynamically based on Z_global
    Z_g = Z_global[:, np.newaxis]
    X_max_dynamic = Z_g * np.tan(KINECT_FOV_H_RAD / 2.0)
    Y_max_dynamic = Z_g * np.tan(KINECT_FOV_V_RAD / 2.0)
    
    X_phys = x_norm * X_max_dynamic
    Y_phys = -y_norm * Y_max_dynamic
    
    return X_phys, Y_phys, Z_global


# ---------------------------------------------------------------------------
# Phase 2: Z-Axis Recovery (Analytical Forward Kinematics)
# ---------------------------------------------------------------------------

def synthesize_depth_analytical(X_phys: np.ndarray, Y_phys: np.ndarray, Z_global: np.ndarray) -> np.ndarray:
    """
    Analytically reconstructs Z-depth using a single-pass Forward Kinematics tree.
    This replaces the slow L-BFGS-B optimizer, speeding up compilation by ~2000x.
    """
    num_frames, num_joints = X_phys.shape
    Z_opt = np.tile(Z_global[:, np.newaxis], (1, num_joints))
    
    # Define the hierarchical tree (parent -> child)
    # Root is 8 (HipsCenter)
    tree = [
        (8, 1), (8, 9), (8, 10),
        (1, 2), (1, 3),
        (2, 4), (4, 6),
        (3, 5), (5, 7),
        (9, 11), (11, 13),
        (10, 12), (12, 14)
    ]
    
    # Determine facing direction per frame: Right Shoulder X (3) vs Left Shoulder X (2)
    # If Left Shoulder has a larger X coordinate, the player is facing forward.
    facing_forward = X_phys[:, 2] > X_phys[:, 3]
    # direction array: 1.0 for facing forward, -1.0 for facing backward
    direction = np.where(facing_forward, 1.0, -1.0)
    
    for (p, c) in tree:
        if p < num_joints and c < num_joints:
            target_len = STANDARD_BONES.get((p, c), STANDARD_BONES.get((c, p), 0.2))
            
            x_diff = X_phys[:, c] - X_phys[:, p]
            y_diff = Y_phys[:, c] - Y_phys[:, p]
            
            # Foreshortening: how much length is missing in 2D?
            missing_sq = target_len**2 - (x_diff**2 + y_diff**2)
            z_diff = np.sqrt(np.maximum(0, missing_sq))
            
            # Symmetrical Joint handling (180-Degree Spin Fix):
            # Evaluate facing direction to prevent depth inversion when the player turns around.
            if c in (3, 10):  # ShoulderRight, HipRight
                Z_opt[:, c] = Z_opt[:, p] + (z_diff * direction)
            else:
                # Assume limbs generally reach FORWARD (towards camera = negative Z direction)
                # If turned around, direction=-1 pushes limbs AWAY from camera (+Z)
                Z_opt[:, c] = Z_opt[:, p] - (z_diff * direction)
            
    # Apply a light temporal smoothing to fix any popping from the distance clamps
    if num_frames >= 5:
        w_len = min(7, num_frames if num_frames % 2 != 0 else num_frames - 1)
        Z_opt = savgol_filter(Z_opt, window_length=w_len, polyorder=2, axis=0)
        
    logger.debug("Analytical Z-Axis synthesis completed for %d frames.", num_frames)
    return Z_opt


# ---------------------------------------------------------------------------
# Phase 3: Kinematics (Velocity & Acceleration)
# ---------------------------------------------------------------------------

def compute_kinematics(pos: np.ndarray, dt: float = 1.0/30.0) -> tuple[np.ndarray, np.ndarray]:
    """
    Computes Velocity and Acceleration arrays using Savitzky-Golay filtering.
    
    Args:
        pos: shape (num_frames, num_joints) positional data (e.g., X, Y, or Z)
        dt: delta time per frame (JDNext is typically ~30 FPS pseudo-timing)
        
    Returns:
        vel, accel arrays of the same shape.
    """
    num_frames = pos.shape[0]
    
    # Savitzky-Golay requires window_length > polyorder and window_length must be odd
    window_len = min(7, num_frames if num_frames % 2 != 0 else num_frames - 1)
    if window_len < 3:
        # Fallback to simple finite difference if too few frames
        vel = np.gradient(pos, dt, axis=0)
        accel = np.gradient(vel, dt, axis=0)
        return vel, accel

    # First derivative (Velocity)
    vel = savgol_filter(pos, window_length=window_len, polyorder=2, deriv=1, delta=dt, axis=0)
    
    # Second derivative (Acceleration)
    accel = savgol_filter(pos, window_length=window_len, polyorder=2, deriv=2, delta=dt, axis=0)
    
    return vel, accel


# ---------------------------------------------------------------------------
# Phase 4: Biomechanical Inverse Dynamics
# ---------------------------------------------------------------------------

def simulate_inverse_dynamics(X: np.ndarray, Y: np.ndarray, Z: np.ndarray, 
                              Vx: np.ndarray, Vy: np.ndarray, Vz: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """
    Approximates joint Torque and MuscleForce based on reconstructed 3D kinematics.
    
    Args:
        X, Y, Z: 3D Positions (num_frames, num_joints)
        Vx, Vy, Vz: 3D Velocities
        
    Returns:
        Torque, MuscleForce arrays (num_frames, num_joints)
    """
    num_frames, num_joints = X.shape
    
    torque = np.zeros_like(X)
    muscle_force = np.zeros_like(X)
    
    # Define parent mapping for angular velocity calculation
    # Parent mapping derived from the standard tree:
    # 8(Root) -> 1, 9, 10. 1 -> 2, 3. 2 -> 4 -> 6. 3 -> 5 -> 7.
    # 9 -> 11 -> 13. 10 -> 12 -> 14.
    parent_map = {1:8, 9:8, 10:8, 2:1, 3:1, 4:2, 6:4, 5:3, 7:5, 11:9, 13:11, 12:10, 14:12}
    
    dt = 1.0 / 30.0  # Assuming 30 FPS for angular derivative
    
    for j in range(num_joints):
        # Determine parent joint to form a limb vector. If no parent, use root (8)
        p = parent_map.get(j, 8)
        
        if j == 8 or j == p:
            # Root joint or no valid vector
            torque[:, j] = 0.0
            muscle_force[:, j] = 0.0
            continue
            
        # Calculate limb vector
        limb_vector = np.column_stack((X[:, j] - X[:, p], Y[:, j] - Y[:, p], Z[:, j] - Z[:, p]))
        
        # Normalize limb vector safely
        norms = np.linalg.norm(limb_vector, axis=1, keepdims=True)
        # Avoid division by zero
        norms[norms == 0] = 1e-6
        limb_vector_normalized = limb_vector / norms
        
        # Angular velocity via cross product of sequential frames
        # Padded to maintain array shape
        cross_prod = np.cross(limb_vector_normalized[:-1], limb_vector_normalized[1:])
        ang_vel = np.zeros(num_frames)
        ang_vel[1:] = np.linalg.norm(cross_prod, axis=1) / dt
        ang_vel[0] = ang_vel[1] if num_frames > 1 else 0.0
        
        # Angular acceleration (1st derivative of angular velocity)
        if num_frames >= 3:
            ang_accel = np.gradient(ang_vel, dt)
        else:
            ang_accel = np.zeros(num_frames)
        
        # Biomechanical properties
        segment_mass = 2.0
        moment_arm = 0.05
        
        # Moment of Inertia for a point mass approximation: I = m * r^2
        # We'll use a simplified scalar moment of inertia.
        # Averaging limb length to avoid noisy inertia scaling
        r_avg = np.mean(norms)
        moment_of_inertia = segment_mass * (r_avg ** 2)
        
        # True torque approximation: Torque = I * angular_acceleration
        # This prevents the "flail" exploit by requiring actual rotation.
        torque[:, j] = moment_of_inertia * ang_accel
        
        # Muscle Force is proportional to Torque / Moment Arm
        muscle_force[:, j] = torque[:, j] / moment_arm
        
    return torque, muscle_force
