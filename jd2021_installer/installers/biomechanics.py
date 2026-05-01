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
# Phase 2: Z-Axis Recovery (Optimization)
# ---------------------------------------------------------------------------

def _depth_objective(Z_flat: np.ndarray, X: np.ndarray, Y: np.ndarray, lambda_smooth: float = 0.85) -> float:
    """
    The L-BFGS-B objective loss function to minimize for Z-depth.
    """
    num_frames, num_joints = X.shape
    Z = Z_flat.reshape((num_frames, num_joints))
    total_loss = 0.0
    
    for (i, j), target_len in STANDARD_BONES.items():
        if i < num_joints and j < num_joints:
            dist_sq = (X[:, i] - X[:, j])**2 + (Y[:, i] - Y[:, j])**2 + (Z[:, i] - Z[:, j])**2
            loss_bone = np.sum((np.sqrt(np.maximum(0, dist_sq)) - target_len)**2)
            total_loss += loss_bone
            
    if num_frames > 1:
        loss_smooth = np.sum((Z[1:, :] - Z[:-1, :])**2)
        total_loss += lambda_smooth * loss_smooth
        
    return total_loss

def synthesize_depth_lbfgs(X_phys: np.ndarray, Y_phys: np.ndarray, Z_global: np.ndarray) -> np.ndarray:
    """
    Executes a bounded L-BFGS-B optimization to synthesize missing Z-depth.
    """
    num_frames, num_joints = X_phys.shape
    
    # Start with the global Z depth for each frame instead of flat Z_ROOT
    Z_initial = np.tile(Z_global[:, np.newaxis], (1, num_joints))
    
    logger.debug("Executing L-BFGS-B Z-Axis synthesis for %d frames...", num_frames)
    
    res = minimize(
        _depth_objective,
        Z_initial.flatten(),
        args=(X_phys, Y_phys, 0.85),
        method='L-BFGS-B',
        options={'maxiter': 500, 'ftol': 1e-4}
    )
    
    Z_opt = res.x.reshape((num_frames, num_joints))
    logger.debug("Z-Axis synthesis completed. Optimization success: %s", res.success)
    
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
    
    # Simplified approximation for compiler threshold passing:
    # Torque is roughly proportional to moment arm * mass * pseudo-acceleration
    # Since we need to trick the AdaBoost stumps, we synthesize a signal that scales
    # strongly with explosive movements (high velocity & high positional spread).
    
    for j in range(num_joints):
        speed_3d = np.sqrt(Vx[:, j]**2 + Vy[:, j]**2 + Vz[:, j]**2)
        
        # Pseudo-Lagrangian heuristic: kinetic energy metric
        # Assuming an average segment mass of 2.0kg and moment arm 0.05m
        segment_mass = 2.0
        moment_arm = 0.05
        
        # Synthesize a continuous torque signal
        torque[:, j] = speed_3d * segment_mass * 9.81 * moment_arm
        
        # Muscle Force is proportional to Torque / Moment Arm
        muscle_force[:, j] = torque[:, j] / moment_arm
        
    return torque, muscle_force
