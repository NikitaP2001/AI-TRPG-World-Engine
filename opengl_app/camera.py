"""Orbit camera for 3D viewport control.

Uses screen-space arcball: mouse movement on screen maps to a rotation
axis perpendicular to the drag direction. Point under cursor follows the
mouse naturally — no sinusoidal paths, no yaw/pitch decomposition.
"""

from __future__ import annotations
import math
import numpy as np


def _rot_vec(v: np.ndarray, axis: np.ndarray, angle: float) -> np.ndarray:
    """Rotate vector v around unit axis by angle (Rodrigues' formula)."""
    c = math.cos(angle)
    s = math.sin(angle)
    return v * c + np.cross(axis, v) * s + axis * np.dot(axis, v) * (1.0 - c)


class Camera:
    """Orbit camera with screen-space arcball and inertial damping.

    Controls:
        Left drag: orbit (arcball — point follows mouse naturally)
        Scroll: zoom
    """

    _PITCH_LIMIT = math.sin(math.radians(85.0))  # ~0.996

    @staticmethod
    def _clamp_pitch_static(d: np.ndarray) -> np.ndarray:
        """Clamp direction to pitch limit, preserving heading (x/z ratio)."""
        if abs(d[1]) <= Camera._PITCH_LIMIT:
            return d
        pl = Camera._PITCH_LIMIT
        sign = 1.0 if d[1] > 0 else -1.0
        xz = math.sqrt(max(0.0, d[0]*d[0] + d[2]*d[2]))
        xz_target = math.sqrt(max(0.0, 1.0 - pl*pl))
        if xz > 1e-8:
            s = xz_target / xz
            return np.array([d[0]*s, pl*sign, d[2]*s], dtype=np.float32)
        return np.array([0.0, pl*sign, 0.0], dtype=np.float32)

    def __init__(self, distance: float = 3.5, target=(0.0, 0.0, 0.0)):
        self.target = np.array(target, dtype=np.float32)
        self.distance = distance
        # Direction from target to camera (unit vector)
        # Initial: slightly above the equator looking down
        self._dir = np.array([0.0, 0.3, 1.0], dtype=np.float32)
        self._dir = self._dir / np.linalg.norm(self._dir)
        # Inertia state
        self._vel_yaw = 0.0           # yaw coast speed (around world Y)
        self._vel_pitch = 0.0         # pitch coast speed (around camera right)
        self._aspect = 1.0
        self._min_dist = 2.0   # 75% between old (1.6) and overdone (2.2)
        self._max_dist = 5.0   # planet stays large on screen
        self._fov = 45.0

        # Tuning
        self._drag_sensitivity = 0.0008  # radians per pixel of mouse movement
        self._damping = 0.90             # coast velocity decay per frame
        self._vel_threshold = 0.0001     # coast stops below this

    @property
    def fov(self) -> float:
        return self._fov

    @property
    def position(self) -> np.ndarray:
        """Camera position in world space."""
        return self.target + self._dir * self.distance

    @property
    def view_matrix(self) -> np.ndarray:
        """Standard OpenGL lookAt view matrix (row-major)."""
        pos = self.position
        center = self.target
        up = np.array([0.0, 1.0, 0.0], dtype=np.float32)

        forward = center - pos
        fnorm = np.linalg.norm(forward)
        if fnorm < 1e-8:
            return np.eye(4, dtype=np.float32)
        forward = forward / fnorm

        if abs(np.dot(forward, up)) > 0.999:
            up = np.array([0.0, 0.0, 1.0], dtype=np.float32)

        right = np.cross(forward, up)
        rnorm = np.linalg.norm(right)
        if rnorm < 1e-8:
            right = np.array([1.0, 0.0, 0.0], dtype=np.float32)
        else:
            right = right / rnorm
        up = np.cross(right, forward)

        return np.array([
            [right[0], right[1], right[2], -np.dot(right, pos)],
            [up[0], up[1], up[2], -np.dot(up, pos)],
            [-forward[0], -forward[1], -forward[2], np.dot(forward, pos)],
            [0.0, 0.0, 0.0, 1.0],
        ], dtype=np.float32)

    @property
    def projection_matrix(self) -> np.ndarray:
        """Standard OpenGL perspective projection (row-major)."""
        f = 1.0 / math.tan(math.radians(self._fov) / 2.0)
        n, ff = 0.1, 100.0
        return np.array([
            [f / self._aspect, 0.0, 0.0, 0.0],
            [0.0, f, 0.0, 0.0],
            [0.0, 0.0, -(ff + n) / (ff - n), -2.0 * ff * n / (ff - n)],
            [0.0, 0.0, -1.0, 0.0],
        ], dtype=np.float32)

    def set_aspect(self, aspect: float) -> None:
        """Update projection matrix aspect ratio."""
        self._aspect = aspect

    def orbit(self, dx: float, dy: float) -> None:
        """Orbit around target.

        Separates yaw (around world Y) from pitch (around camera right)
        so horizontal rotation feels consistent at any latitude —
        no "spinning top" effect at the poles.
        """
        dist_scale = max(0.15, min(3.0, self.distance / 3.5))
        sens = self._drag_sensitivity * dist_scale

        # Non-linear response: slow drags are extra precise
        mag = math.sqrt(dx*dx + dy*dy)
        slow_threshold = 6.0
        if mag < slow_threshold:
            sens *= mag / slow_threshold  # quadratic damping

        yaw_angle = -dx * sens
        pitch_angle = -dy * sens

        new_dir = self._dir.copy()

        # 1. Yaw — always around world Y axis (consistent at any latitude)
        if abs(yaw_angle) > self._vel_threshold:
            new_dir = _rot_vec(new_dir, np.array([0.0, 1.0, 0.0]), yaw_angle)

        # 2. Pitch — around camera right axis (from the yawed direction)
        if abs(pitch_angle) > self._vel_threshold:
            # Compute right axis from current (yawed) forward
            fwd = -new_dir / np.linalg.norm(new_dir)
            world_up = np.array([0.0, 1.0, 0.0])
            if abs(np.dot(fwd, world_up)) > 0.999:
                world_up = np.array([0.0, 0.0, 1.0])
            right = np.cross(fwd, world_up)
            rn = np.linalg.norm(right)
            if rn > 1e-8:
                new_dir = _rot_vec(new_dir, right / rn, pitch_angle)

        # Clamp pitch
        new_dir = Camera._clamp_pitch_static(new_dir)
        self._dir = new_dir

        # Store for coast
        self._vel_yaw = yaw_angle
        self._vel_pitch = pitch_angle

    def update(self, dt: float) -> None:
        """Apply inertial coast with damping."""
        new_dir = self._dir.copy()

        # Yaw coast
        if abs(self._vel_yaw) > self._vel_threshold:
            new_dir = _rot_vec(new_dir, np.array([0.0, 1.0, 0.0]), self._vel_yaw)

        # Pitch coast (using camera right from current direction)
        if abs(self._vel_pitch) > self._vel_threshold:
            fwd = -new_dir / np.linalg.norm(new_dir)
            world_up = np.array([0.0, 1.0, 0.0])
            if abs(np.dot(fwd, world_up)) > 0.999:
                world_up = np.array([0.0, 0.0, 1.0])
            right = np.cross(fwd, world_up)
            rn = np.linalg.norm(right)
            if rn > 1e-8:
                new_dir = _rot_vec(new_dir, right / rn, self._vel_pitch)

        new_dir = Camera._clamp_pitch_static(new_dir)
        self._dir = new_dir

        self._vel_yaw *= self._damping
        self._vel_pitch *= self._damping
        if abs(self._vel_yaw) < self._vel_threshold:
            self._vel_yaw = 0.0
        if abs(self._vel_pitch) < self._vel_threshold:
            self._vel_pitch = 0.0

    def zoom(self, delta: float) -> None:
        """Zoom by scroll delta."""
        self.distance *= (1.0 + delta * 0.002)
        self.distance = max(self._min_dist, min(self._max_dist, self.distance))

    def reset(self) -> None:
        """Reset to default view."""
        self.distance = 3.5
        self._dir = np.array([0.0, 0.3, 1.0], dtype=np.float32)
        self._dir = self._dir / np.linalg.norm(self._dir)
        self._vel_yaw = 0.0
        self._vel_pitch = 0.0
