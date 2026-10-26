"""Where is the visitor? Relocalisation of one phone frame against a merged map.

1. Shortlist keyframes: VLAD retrieval, plus keyframes near the last fix.
2. Match the frame's ALIKED descriptors to the shortlisted 3D points (mutual
   nearest neighbour), then MAGSAC++ PnP and LM refinement.
3. If that is weak, verify with LightGlue against the best keyframe.
4. Turn inliers, reprojection error and motion plausibility into a
   confidence the guidance agent can act on.

Visitors never calibrate their phone, so the focal length is estimated on the
first fixes by trying a few plausible values and keeping the one with the most
inliers.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass

import numpy as np

from ..mapping.model import MapData
from ..vision import camera, geometry
from ..vision.features import Features, match_mnn, strong_matcher
from ..vision.retrieval import Vocabulary, top_k

FOCAL_CANDIDATES = (0.62, 0.68, 0.75, 0.82, 0.90)


@dataclass
class Fix:
    R: np.ndarray
    t: np.ndarray
    inliers: int
    matches: int
    rms_px: float
    confidence: float
    kf_ids: list[int]
    method: str
    ms: float

    @property
    def c(self) -> np.ndarray:
        return geometry.center(self.R, self.t)

    @property
    def yaw(self) -> float:
        return geometry.yaw_deg(self.R)

    def summary(self) -> dict:
        c = self.c
        return {"x": round(float(c[0]), 3), "y": round(float(c[1]), 3), "z": round(float(c[2]), 3),
                "yaw_deg": round(self.yaw, 1), "confidence": round(self.confidence, 3),
                "inliers": self.inliers, "rms_px": round(self.rms_px, 2), "method": self.method,
                "ms": round(self.ms, 1)}


def confidence(inliers: int, rms: float, jump_m: float | None, dt_s: float | None) -> float:
    c = 1 - math.exp(-max(inliers - 8, 0) / 30.0)
    if rms > 2.5:
        c *= max(0.4, 1 - (rms - 2.5) / 5)
    if jump_m is not None and dt_s is not None:
        # People walk at up to about 1.5 m/s; allow generous slack for timing noise.
        plausible = 1.5 * max(dt_s, 1.0) + 1.0
        if jump_m > plausible:
            c *= 0.35
    return float(np.clip(c, 0, 1))


class Localizer:
    def __init__(self, data: MapData, check_height: bool = True):
        self.data = data
        self.check_height = check_height
        self.vocab = Vocabulary.train([kf.feats.desc for kf in data.keyframes])
        self.kf_ids = np.array([kf.id for kf in data.keyframes])
        self.vlad = np.array([self.vocab.vlad(kf.feats.desc) for kf in data.keyframes])
        self.kf_xyz = np.array([kf.c for kf in data.keyframes])
        self.kfs = data.kf_by_id()

    def _shortlist(self, feats: Features, prior_c: np.ndarray | None, k: int = 5) -> list[int]:
        q = self.vocab.vlad(feats.desc)
        order, _ = top_k(self.vlad, q, k)
        ids = [int(self.kf_ids[o]) for o in order]
        if prior_c is not None:
            near = np.argsort(np.linalg.norm(self.kf_xyz - prior_c, axis=1))[:4]
            ids += [int(self.kf_ids[i]) for i in near if int(self.kf_ids[i]) not in ids]
        return ids

    def _pnp_points(self, feats: Features, kf_ids: list[int], K: np.ndarray):
        ids = self.data.points_of([self.kfs[i] for i in kf_ids])
        if len(ids) < 20:
            return None, 0
        xyz, pdesc = self.data.arrays(ids)
        ia, ib = match_mnn(feats.desc, pdesc, max_dist=1.0, ratio=0.92)
        if len(ia) < 12:
            return None, len(ia)
        return geometry.pnp(xyz[ib], feats.kps[ia], K, threshold=5.0), len(ia)

    def _pnp_lightglue(self, feats: Features, kf_id: int, K: np.ndarray):
        kf = self.kfs[kf_id]
        ia, ib = strong_matcher().match(feats, kf.feats)
        has = kf.pid[ib] >= 0 if len(ib) else np.zeros(0, bool)
        if has.sum() < 12:
            return None, int(has.sum())
        pts = np.array([self.data.xyz[int(p)] for p in kf.pid[ib][has]])
        return geometry.pnp(pts, feats.kps[ia][has], K, threshold=5.0), int(has.sum())

    def localize(self, feats: Features, focal_ratio: float, prior: Fix | None = None,
                 dt_s: float | None = None, allow_lightglue: bool = True, K: np.ndarray | None = None) -> Fix | None:
        t0 = time.perf_counter()
        K = camera.default_K(*feats.size, focal_ratio) if K is None else K
        prior_c = prior.c if prior is not None and prior.confidence > 0.4 else None
        cand = self._shortlist(feats, prior_c)
        res, n_match = self._pnp_points(feats, cand, K)
        method = "points"
        if (res is None or res[2].sum() < 30) and allow_lightglue and cand:
            res2, n2 = self._pnp_lightglue(feats, cand[0], K)
            if res2 is not None and (res is None or res2[2].sum() > res[2].sum()):
                res, n_match, method = res2, n2, "lightglue"
        if res is None:
            return None
        R, t, mask, rms = res
        inl = int(mask.sum())
        c = geometry.center(R, t)
        jump = float(np.linalg.norm(c - prior.c)) if prior is not None else None
        conf = confidence(inl, rms, jump, dt_s)
        # Points far below the floor or above the ceiling mean a wrong solution.
        if self.check_height and not (-0.5 < c[1] < 3.0):
            conf *= 0.3
        return Fix(R, t, inl, n_match, rms, conf, cand, method, (time.perf_counter() - t0) * 1000)

    def calibrate_focal(self, feats: Features) -> tuple[float, Fix | None]:
        """Try each plausible focal ratio and keep the one with the most inliers."""
        best = (FOCAL_CANDIDATES[2], None)
        for f in FOCAL_CANDIDATES:
            fix = self.localize(feats, f, allow_lightglue=False)
            if fix is not None and (best[1] is None or fix.inliers > best[1].inliers):
                best = (f, fix)
        return best
