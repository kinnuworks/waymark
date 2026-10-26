"""Pose helpers, robust PnP and triangulation.

Poses are stored world-to-camera: x_cam = R @ x_world + t.
"""
from __future__ import annotations

import cv2
import numpy as np


def center(R: np.ndarray, t: np.ndarray) -> np.ndarray:
    return -R.T @ t


def forward(R: np.ndarray) -> np.ndarray:
    return R.T @ np.array([0.0, 0.0, 1.0])


def yaw_deg(R: np.ndarray) -> float:
    """Heading in the floor plane, 0 = facing +z (into the building), positive towards +x."""
    f = forward(R)
    return float(np.degrees(np.arctan2(f[0], f[2])))


def angle_between(R1: np.ndarray, R2: np.ndarray) -> float:
    c = (np.trace(R1 @ R2.T) - 1) / 2
    return float(np.degrees(np.arccos(np.clip(c, -1, 1))))


def project(K: np.ndarray, R: np.ndarray, t: np.ndarray, X: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    Xc = (R @ X.T).T + t
    z = Xc[:, 2]
    uv = (K @ Xc.T).T
    uv = uv[:, :2] / np.maximum(uv[:, 2:3], 1e-9)
    return uv, z


def _usac_params(threshold: float) -> cv2.UsacParams:
    p = cv2.UsacParams()
    p.confidence = 0.999
    p.maxIterations = 2000
    p.threshold = threshold
    p.sampler = cv2.SAMPLING_UNIFORM
    p.score = cv2.SCORE_METHOD_MAGSAC
    p.loMethod = cv2.LOCAL_OPTIM_SIGMA
    p.loIterations = 10
    p.loSampleSize = 20
    p.final_polisher = cv2.MAGSAC
    p.final_polisher_iterations = 10
    return p


def pnp(obj: np.ndarray, px: np.ndarray, K: np.ndarray, threshold: float = 4.0, min_inliers: int = 12):
    """MAGSAC++ PnP followed by Levenberg-Marquardt refinement on the inliers.

    Returns (R, t, inlier_mask, rms_px) or None.
    """
    if len(obj) < 6:
        return None
    obj = np.ascontiguousarray(obj, np.float64).reshape(-1, 1, 3)
    px = np.ascontiguousarray(px, np.float64).reshape(-1, 1, 2)
    try:
        ok, _, rvec, tvec, inl = cv2.solvePnPRansac(obj, px, K, np.zeros(5), params=_usac_params(threshold))
    except cv2.error:
        ok = False
    if not ok or inl is None or len(inl) < min_inliers:
        return None
    idx = inl.reshape(-1)
    rvec, tvec = cv2.solvePnPRefineLM(obj[idx], px[idx], K, np.zeros(5), rvec, tvec)
    R, _ = cv2.Rodrigues(rvec)
    t = tvec.reshape(3)
    uv, z = project(K, R, t, obj.reshape(-1, 3))
    err = np.linalg.norm(uv - px.reshape(-1, 2), axis=1)
    mask = (err < threshold * 1.5) & (z > 0)
    if mask.sum() < min_inliers:
        return None
    rms = float(np.sqrt(np.mean(err[mask] ** 2)))
    return R, t, mask, rms


def triangulate(K: np.ndarray, R1, t1, R2, t2, p1: np.ndarray, p2: np.ndarray,
                max_err: float = 3.0, min_parallax_deg: float = 1.5, max_depth: float = 40.0):
    """Linear triangulation with cheirality, reprojection and parallax checks.

    Returns (X (M,3), keep mask over the inputs).
    """
    if len(p1) == 0:
        return np.zeros((0, 3)), np.zeros(0, bool)
    P1 = K @ np.hstack([R1, t1.reshape(3, 1)])
    P2 = K @ np.hstack([R2, t2.reshape(3, 1)])
    Xh = cv2.triangulatePoints(P1, P2, p1.T.astype(np.float64), p2.T.astype(np.float64))
    X = (Xh[:3] / Xh[3]).T
    uv1, z1 = project(K, R1, t1, X)
    uv2, z2 = project(K, R2, t2, X)
    e1 = np.linalg.norm(uv1 - p1, axis=1)
    e2 = np.linalg.norm(uv2 - p2, axis=1)
    c1, c2 = center(R1, t1), center(R2, t2)
    v1 = X - c1
    v2 = X - c2
    cosang = np.sum(v1 * v2, axis=1) / (np.linalg.norm(v1, axis=1) * np.linalg.norm(v2, axis=1) + 1e-12)
    par = np.degrees(np.arccos(np.clip(cosang, -1, 1)))
    keep = ((z1 > 0.1) & (z2 > 0.1) & (z1 < max_depth) & (z2 < max_depth)
            & (e1 < max_err) & (e2 < max_err) & (par > min_parallax_deg) & np.isfinite(X).all(axis=1))
    return X, keep
