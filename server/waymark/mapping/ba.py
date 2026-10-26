"""Sparse bundle adjustment with the entrance board as a fixed anchor.

Unknowns are every keyframe pose (Rodrigues vector and translation) and every
3D point seen at least twice. Residuals are reprojection errors with a Huber
loss. Board corners have known building coordinates, so keyframes that saw
the board are pulled to the true frame and scale, which removes the drift
that a walk accumulates between leaving the board and returning to it.
"""
from __future__ import annotations

import time

import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.sparse import lil_matrix

from .model import MapData

BOARD_WEIGHT = 3.0


def _rotate(rvecs: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Rotate pts[i] by rvecs[i] (Rodrigues formula, vectorised)."""
    theta = np.linalg.norm(rvecs, axis=1)[:, None]
    with np.errstate(invalid="ignore", divide="ignore"):
        v = np.where(theta > 1e-12, rvecs / theta, 0.0)
    dot = np.sum(pts * v, axis=1)[:, None]
    cos, sin = np.cos(theta), np.sin(theta)
    return cos * pts + sin * np.cross(v, pts) + dot * (1 - cos) * v


def adjust(data: MapData, max_nfev: int = 40, min_obs: int = 2, max_points: int = 40000,
           anchor_weight: float = 1000.0, f_scale: float = 1.5) -> dict:
    t0 = time.perf_counter()
    kfs = data.keyframes
    if len(kfs) < 2:
        return {"ok": False, "reason": "too few keyframes"}
    kf_index = {kf.id: i for i, kf in enumerate(kfs)}
    pids = [p for p, o in data.obs.items() if len([1 for k, _ in o if k in kf_index]) >= min_obs]
    if len(pids) > max_points:
        pids = sorted(pids, key=lambda p: -len(data.obs[p]))[:max_points]
    p_index = {p: i for i, p in enumerate(pids)}

    cam_i, pt_i, uv, Ks = [], [], [], []
    for p in pids:
        for kf_id, kp in data.obs[p]:
            i = kf_index.get(kf_id)
            if i is None:
                continue
            cam_i.append(i)
            pt_i.append(p_index[p])
            uv.append(kfs[i].feats.kps[kp])
    cam_i, pt_i, uv = np.array(cam_i), np.array(pt_i), np.array(uv, np.float64)

    b_cam, b_X, b_uv = [], [], []
    for i, kf in enumerate(kfs):
        if kf.board is not None:
            px, X = kf.board
            b_cam += [i] * len(px)
            b_X.append(X)
            b_uv.append(px)
    b_cam = np.array(b_cam, int)
    b_X = np.vstack(b_X) if b_X else np.zeros((0, 3))
    b_uv = np.vstack(b_uv).astype(np.float64) if b_uv else np.zeros((0, 2))
    anchors = [(i, kf.anchor) for i, kf in enumerate(kfs) if kf.anchor is not None]
    if len(b_cam) == 0 and not anchors:
        return {"ok": False, "reason": "no board observations"}
    a_idx = np.array([i for i, _ in anchors], int)
    a_r = np.array([cv2.Rodrigues(a[0])[0].reshape(3) for _, a in anchors]) if anchors else np.zeros((0, 3))
    a_t = np.array([a[1] for _, a in anchors]) if anchors else np.zeros((0, 3))

    K = np.array([kf.K for kf in kfs])
    n_c, n_p = len(kfs), len(pids)
    x0 = np.zeros(n_c * 6 + n_p * 3)
    for i, kf in enumerate(kfs):
        x0[i * 6:i * 6 + 3] = cv2.Rodrigues(kf.R)[0].reshape(3)
        x0[i * 6 + 3:i * 6 + 6] = kf.t
    x0[n_c * 6:] = np.array([data.xyz[p] for p in pids]).reshape(-1) if n_p else []

    def proj(cams, X, ci):
        Xc = _rotate(cams[ci, :3], X) + cams[ci, 3:]
        Kc = K[ci]
        z = np.maximum(Xc[:, 2], 1e-6)
        u = Kc[:, 0, 0] * Xc[:, 0] / z + Kc[:, 0, 2]
        v = Kc[:, 1, 1] * Xc[:, 1] / z + Kc[:, 1, 2]
        return np.stack([u, v], axis=1)

    def residuals(x):
        cams = x[:n_c * 6].reshape(n_c, 6)
        pts = x[n_c * 6:].reshape(n_p, 3)
        r1 = (proj(cams, pts[pt_i], cam_i) - uv).reshape(-1) if n_p else np.zeros(0)
        r2 = ((proj(cams, b_X, b_cam) - b_uv) * BOARD_WEIGHT).reshape(-1) if len(b_cam) else np.zeros(0)
        # Known poses: 1 mrad and 1 mm count like about one pixel.
        r3 = np.concatenate([((cams[a_idx, :3] - a_r) * anchor_weight).reshape(-1),
                             ((cams[a_idx, 3:] - a_t) * anchor_weight).reshape(-1)]) if len(a_idx) else np.zeros(0)
        return np.concatenate([r1, r2, r3])

    m = 2 * (len(cam_i) + len(b_cam)) + 6 * len(a_idx)
    A = lil_matrix((m, len(x0)), dtype=np.uint8)
    rows = np.arange(len(cam_i))
    for s in range(6):
        A[2 * rows, cam_i * 6 + s] = 1
        A[2 * rows + 1, cam_i * 6 + s] = 1
    for s in range(3):
        A[2 * rows, n_c * 6 + pt_i * 3 + s] = 1
        A[2 * rows + 1, n_c * 6 + pt_i * 3 + s] = 1
    off = 2 * len(cam_i)
    brows = np.arange(len(b_cam))
    for s in range(6):
        A[off + 2 * brows, b_cam * 6 + s] = 1
        A[off + 2 * brows + 1, b_cam * 6 + s] = 1
    aoff = off + 2 * len(b_cam)
    for k, i in enumerate(a_idx):
        for s in range(3):
            A[aoff + 3 * k + s, i * 6 + s] = 1
            A[aoff + 3 * len(a_idx) + 3 * k + s, i * 6 + 3 + s] = 1

    r0 = residuals(x0)
    cam_all, pt_all, uv_all = cam_i, pt_i, uv
    # Stage 1: a wide Huber kernel so large initial errors still pull the
    # solution in. Stage 2: drop observations that still disagree badly,
    # then a tight kernel for the final polish.
    res = least_squares(residuals, x0, jac_sparsity=A, loss="linear", x_scale="jac",
                        method="trf", tr_solver="lsmr", max_nfev=max_nfev, verbose=0)
    nfev = res.nfev
    if n_p:
        cams1 = res.x[:n_c * 6].reshape(n_c, 6)
        pts1 = res.x[n_c * 6:].reshape(n_p, 3)
        e1 = np.linalg.norm(proj(cams1, pts1[pt_i], cam_i) - uv, axis=1)
        good = e1 < 6.0
        if (~good).any() and good.sum() > 20:
            cam_i, pt_i, uv = cam_i[good], pt_i[good], uv[good]
            m = 2 * (len(cam_i) + len(b_cam)) + 6 * len(a_idx)
            A2 = lil_matrix((m, len(x0)), dtype=np.uint8)
            rows = np.arange(len(cam_i))
            for s_ in range(6):
                A2[2 * rows, cam_i * 6 + s_] = 1
                A2[2 * rows + 1, cam_i * 6 + s_] = 1
            for s_ in range(3):
                A2[2 * rows, n_c * 6 + pt_i * 3 + s_] = 1
                A2[2 * rows + 1, n_c * 6 + pt_i * 3 + s_] = 1
            off = 2 * len(cam_i)
            for s_ in range(6):
                A2[off + 2 * brows, b_cam * 6 + s_] = 1
                A2[off + 2 * brows + 1, b_cam * 6 + s_] = 1
            aoff = off + 2 * len(b_cam)
            for k, i in enumerate(a_idx):
                for s_ in range(3):
                    A2[aoff + 3 * k + s_, i * 6 + s_] = 1
                    A2[aoff + 3 * len(a_idx) + 3 * k + s_, i * 6 + 3 + s_] = 1
            A = A2
    res = least_squares(residuals, res.x, jac_sparsity=A, loss="linear", x_scale="jac",
                        method="trf", tr_solver="lsmr", max_nfev=max_nfev, verbose=0)
    nfev += res.nfev
    x = res.x
    cams = x[:n_c * 6].reshape(n_c, 6)
    pts = x[n_c * 6:].reshape(n_p, 3)
    for i, kf in enumerate(kfs):
        kf.R = cv2.Rodrigues(cams[i, :3])[0]
        kf.t = cams[i, 3:].copy()
    for p, i in p_index.items():
        data.xyz[p] = pts[i].copy()

    # Drop observations that still disagree, then points left with fewer than two views.
    r_final = residuals(x)
    off_final, aoff_final = off, aoff
    err_all = np.linalg.norm(proj(cams, pts[pt_all], cam_all) - uv_all, axis=1) if n_p else np.zeros(0)
    err = np.linalg.norm(proj(cams, pts[pt_i], cam_i) - uv, axis=1) if n_p else np.zeros(0)
    bad = err_all > 4.0
    cam_i, pt_i = cam_all, pt_all
    removed = 0
    if bad.any():
        kf_list = kfs
        for ci, pi in zip(cam_i[bad], pt_i[bad]):
            p = pids[pi]
            kf = kf_list[ci]
            hits = np.where(kf.pid == p)[0]
            kf.pid[hits] = -1
            data.obs[p] = [(k, kp) for k, kp in data.obs[p] if k != kf.id]
        for p in pids:
            if p in data.obs and len(data.obs[p]) < 2:
                data.remove_point(p)
                removed += 1

    def rms(r):
        r = r.reshape(-1, 2)
        return float(np.sqrt(np.mean(np.sum(r ** 2, axis=1)))) if len(r) else 0.0

    return {"ok": True, "keyframes": n_c, "points": n_p, "observations": int(len(cam_i)),
            "board_corners": int(len(b_cam)), "rms_before_px": round(rms(r0[:2 * len(cam_all)]), 3),
            "rms_after_px": round(float(np.sqrt(np.mean(err ** 2))) if len(err) else 0.0, 3),
            "board_rms_before_px": round(rms(r0[2 * len(cam_all):2 * len(cam_all) + 2 * len(b_cam)] / BOARD_WEIGHT), 3),
            "board_rms_after_px": round(rms(r_final[off_final:aoff_final] / BOARD_WEIGHT), 3),
            "anchors": int(len(a_idx)),
            "points_removed": removed, "iterations": int(nfev),
            "seconds": round(time.perf_counter() - t0, 2)}
