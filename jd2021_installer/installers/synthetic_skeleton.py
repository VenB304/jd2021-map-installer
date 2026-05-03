import math

# Kinect V2 Joint IDs
# 0: SpineBase, 1: SpineMid, 2: Neck, 3: Head
# 4: ShoulderLeft, 5: ElbowLeft, 6: WristLeft, 7: HandLeft
# 8: ShoulderRight, 9: ElbowRight, 10: WristRight, 11: HandRight
# 12: HipLeft, 13: KneeLeft, 14: AnkleLeft, 15: FootLeft
# 16: HipRight, 17: KneeRight, 18: AnkleRight, 19: FootRight
# 20: SpineShoulder, 21: HandTipLeft, 22: ThumbLeft, 23: HandTipRight, 24: ThumbRight

class SyntheticSkeleton:
    def __init__(self):
        # 25 joints, [x, y, z] in meters
        self.joints = [[0.0, 0.0, 0.0] for _ in range(25)]
        self._build_neutral_pose()
        
    def _build_neutral_pose(self):
        # Base translation to put the skeleton ~2 meters from the camera
        Z_DEPTH = 2.0
        
        # Core
        self.joints[0]  = [0.0, -0.1, Z_DEPTH]      # SpineBase
        self.joints[1]  = [0.0,  0.2, Z_DEPTH]      # SpineMid
        self.joints[20] = [0.0,  0.5, Z_DEPTH]      # SpineShoulder
        self.joints[2]  = [0.0,  0.6, Z_DEPTH]      # Neck
        self.joints[3]  = [0.0,  0.8, Z_DEPTH]      # Head
        
        # Left Arm (Neutral resting down)
        self.joints[4]  = [-0.2, 0.45, Z_DEPTH]     # ShoulderLeft
        self.joints[5]  = [-0.25, 0.15, Z_DEPTH]    # ElbowLeft
        self.joints[6]  = [-0.25, -0.15, Z_DEPTH]   # WristLeft
        self.joints[7]  = [-0.25, -0.25, Z_DEPTH]   # HandLeft
        self.joints[21] = [-0.25, -0.30, Z_DEPTH]   # HandTipLeft
        self.joints[22] = [-0.22, -0.22, Z_DEPTH]   # ThumbLeft
        
        # Right Arm (Will be animated)
        self.joints[8]  = [0.2, 0.45, Z_DEPTH]      # ShoulderRight
        self.joints[9]  = [0.25, 0.15, Z_DEPTH]     # ElbowRight
        self.joints[10] = [0.25, -0.15, Z_DEPTH]    # WristRight
        self.joints[11] = [0.25, -0.25, Z_DEPTH]    # HandRight
        self.joints[23] = [0.25, -0.30, Z_DEPTH]    # HandTipRight
        self.joints[24] = [0.22, -0.22, Z_DEPTH]    # ThumbRight
        
        # Left Leg
        self.joints[12] = [-0.1, -0.15, Z_DEPTH]    # HipLeft
        self.joints[13] = [-0.1, -0.55, Z_DEPTH]    # KneeLeft
        self.joints[14] = [-0.1, -0.95, Z_DEPTH]    # AnkleLeft
        self.joints[15] = [-0.1, -1.05, Z_DEPTH-0.1]# FootLeft
        
        # Right Leg
        self.joints[16] = [0.1, -0.15, Z_DEPTH]     # HipRight
        self.joints[17] = [0.1, -0.55, Z_DEPTH]     # KneeRight
        self.joints[18] = [0.1, -0.95, Z_DEPTH]     # AnkleRight
        self.joints[19] = [0.1, -1.05, Z_DEPTH-0.1] # FootRight

    def _dist(self, p1, p2):
        return math.sqrt(sum((a - b)**2 for a, b in zip(p1, p2)))

    def _normalize(self, v):
        mag = math.sqrt(sum(a**2 for a in v))
        if mag == 0: return [0,0,0]
        return [a / mag for a in v]

    def apply_ik_right_arm(self, cam_x: float, cam_y: float):
        """
        Applies a 2-bone IK solver to the Right Arm.
        cam_x, cam_y are normalized coordinates in [-1.0, 1.0]
        """
        # Map 2D camera coordinates to 3D space
        # Center of camera maps to upper chest roughly
        target_x = 0.0 + (cam_x * 0.8)  # Max reach 0.8m left/right
        target_y = 0.4 + (cam_y * 0.8)  # Max reach up/down
        target_z = 1.7  # Pushed slightly forward from the body (Z=2.0)
        
        target = [target_x, target_y, target_z]
        
        shoulder = self.joints[8]
        # Real human arm proportions (approx)
        upper_arm_len = 0.28  # Shoulder to Elbow
        lower_arm_len = 0.25  # Elbow to Wrist
        
        # Distance from shoulder to target
        d = self._dist(shoulder, target)
        max_reach = upper_arm_len + lower_arm_len
        
        # 1. If target is out of reach, clamp it
        if d > max_reach:
            vec = self._normalize([target[i] - shoulder[i] for i in range(3)])
            target = [shoulder[i] + vec[i] * (max_reach * 0.999) for i in range(3)]
            d = max_reach * 0.999
            
        # 2. Find elbow angle using Law of Cosines
        # a = lower_arm, b = upper_arm, c = d
        # cos(A) = (b^2 + c^2 - a^2) / (2bc)
        cos_angle = (upper_arm_len**2 + d**2 - lower_arm_len**2) / (2 * upper_arm_len * d)
        cos_angle = max(-1.0, min(1.0, cos_angle))
        angle = math.acos(cos_angle)
        
        # 3. Determine plane of rotation
        # Vector from shoulder to target
        D = [target[i] - shoulder[i] for i in range(3)]
        D_norm = self._normalize(D)
        
        # We need a normal vector to define the elbow plane. 
        # A good default is the cross product of D and the forward vector (0, 0, -1)
        # to make the elbow point downwards/outwards naturally.
        forward = [0.0, 0.0, -1.0]
        
        # Cross product of D and forward
        right = [
            D_norm[1]*forward[2] - D_norm[2]*forward[1],
            D_norm[2]*forward[0] - D_norm[0]*forward[2],
            D_norm[0]*forward[1] - D_norm[1]*forward[0]
        ]
        
        if self._dist(right, [0,0,0]) < 0.01:
            # If D is parallel to forward, use up vector
            up = [0.0, 1.0, 0.0]
            right = [
                D_norm[1]*up[2] - D_norm[2]*up[1],
                D_norm[2]*up[0] - D_norm[0]*up[2],
                D_norm[0]*up[1] - D_norm[1]*up[0]
            ]
            
        right = self._normalize(right)
        
        # Cross again to get the actual up vector for the elbow
        up = [
            right[1]*D_norm[2] - right[2]*D_norm[1],
            right[2]*D_norm[0] - right[0]*D_norm[2],
            right[0]*D_norm[1] - right[1]*D_norm[0]
        ]
        up = self._normalize(up)
        
        # 4. Calculate elbow position
        # Move along D by distance (upper_arm_len * cos(angle))
        dist_along_D = upper_arm_len * math.cos(angle)
        height_along_up = upper_arm_len * math.sin(angle)
        
        elbow = [
            shoulder[i] + (D_norm[i] * dist_along_D) - (up[i] * height_along_up)
            for i in range(3)
        ]
        
        # 5. Update joints
        self.joints[9] = elbow       # ElbowRight
        self.joints[10] = target     # WristRight
        
        # Hand/Thumb follow wrist
        vec_lower = self._normalize([target[i] - elbow[i] for i in range(3)])
        self.joints[11] = [target[i] + vec_lower[i] * 0.05 for i in range(3)] # HandRight
        self.joints[23] = [target[i] + vec_lower[i] * 0.10 for i in range(3)] # HandTipRight
        self.joints[24] = [target[i] + vec_lower[i] * 0.08 for i in range(3)] # ThumbRight

        return self.joints

if __name__ == "__main__":
    skel = SyntheticSkeleton()
    
    print("Testing IK Solver...")
    print("Shoulder:", [round(x, 3) for x in skel.joints[8]])
    
    # Target: High and to the right
    skel.apply_ik_right_arm(1.0, 1.0)
    print("\nIK (1.0, 1.0) - High Right")
    print("Elbow:", [round(x, 3) for x in skel.joints[9]])
    print("Wrist:", [round(x, 3) for x in skel.joints[10]])
    
    # Target: Low and across body to the left
    skel.apply_ik_right_arm(-0.8, -1.0)
    print("\nIK (-0.8, -1.0) - Low Left (Cross body)")
    print("Elbow:", [round(x, 3) for x in skel.joints[9]])
    print("Wrist:", [round(x, 3) for x in skel.joints[10]])
