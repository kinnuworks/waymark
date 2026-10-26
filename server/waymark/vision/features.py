"""Local features and matching on the OpenCV 5 DNN engine.

ALIKED gives keypoints and 128-d descriptors for every image. Matching has two
speeds: mutual nearest neighbour (a few ms) for frame-to-frame tracking, and
LightGlue (hundreds of ms) where a wrong match is expensive: new keyframes,
loop closures and the cross-walker merge. SIFT is the fallback when the ONNX
models are missing, so the pipeline still runs on a bare OpenCV install.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass

import cv2
import numpy as np

from .. import config

# ALIKED's exported graph has a fixed 640x640 input; keypoints come back in
# the coordinates of the image we pass in.
_ALIKED_SIDE = 640


@dataclass
class Features:
    kps: np.ndarray        # (N, 2) float32 pixel coordinates
    desc: np.ndarray       # (N, D) float32, L2-normalised
    scores: np.ndarray     # (N,) float32 detector response
    size: tuple[int, int]  # (width, height) of the image the points refer to

    def __len__(self) -> int:
        return len(self.kps)

    def subset(self, idx: np.ndarray) -> "Features":
        return Features(self.kps[idx], self.desc[idx], self.scores[idx], self.size)


class Extractor:
    """Thread-safe ALIKED wrapper (one network instance per thread)."""

    def __init__(self, max_kps: int = 1000):
        self.max_kps = max_kps
        self.kind = "aliked" if config.ALIKED_ONNX.exists() else "sift"
        self._local = threading.local()

    def _net(self):
        net = getattr(self._local, "net", None)
        if net is None:
            if self.kind == "aliked":
                params = cv2.ALIKED_Params()
                params.inputSize = (_ALIKED_SIDE, _ALIKED_SIDE)
                net = cv2.ALIKED_create(str(config.ALIKED_ONNX), params)
            else:
                net = cv2.SIFT_create(nfeatures=self.max_kps)
            self._local.net = net
        return net

    def extract(self, img: np.ndarray) -> Features:
        if img.ndim == 2:
            img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
        kps, desc = self._net().detectAndCompute(img, None)
        h, w = img.shape[:2]
        if desc is None or len(kps) == 0:
            return Features(np.zeros((0, 2), np.float32), np.zeros((0, 128), np.float32),
                            np.zeros(0, np.float32), (w, h))
        pts = np.float32([k.pt for k in kps])
        scores = np.float32([k.response for k in kps])
        desc = np.asarray(desc, np.float32)
        desc /= np.linalg.norm(desc, axis=1, keepdims=True) + 1e-9
        if len(pts) > self.max_kps:
            keep = np.argsort(-scores)[: self.max_kps]
            pts, desc, scores = pts[keep], desc[keep], scores[keep]
        return Features(pts, desc, scores, (w, h))


def match_mnn(desc_a: np.ndarray, desc_b: np.ndarray, max_dist: float = 0.9,
              ratio: float = 0.95) -> tuple[np.ndarray, np.ndarray]:
    """Mutual nearest neighbours with an absolute and a ratio test.

    Descriptors are unit vectors, so L2 distance lies in [0, 2].
    """
    if len(desc_a) < 2 or len(desc_b) < 2:
        return np.zeros(0, int), np.zeros(0, int)
    bf = cv2.BFMatcher(cv2.NORM_L2)
    ab = bf.knnMatch(desc_a, desc_b, k=2)
    ba = bf.match(desc_b, desc_a)
    back = np.full(len(desc_b), -1)
    for m in ba:
        back[m.queryIdx] = m.trainIdx
    ia, ib = [], []
    for pair in ab:
        if len(pair) < 2:
            continue
        m, n = pair
        if m.distance > max_dist or m.distance > ratio * n.distance:
            continue
        if back[m.trainIdx] == m.queryIdx:
            ia.append(m.queryIdx)
            ib.append(m.trainIdx)
    return np.array(ia, int), np.array(ib, int)


class StrongMatcher:
    """LightGlue on ALIKED descriptors, falling back to MNN."""

    def __init__(self):
        self.kind = ("lightglue" if config.STRONG_MATCHER == "lightglue"
                     and config.LIGHTGLUE_ONNX.exists() and config.ALIKED_ONNX.exists() else "mnn")
        self._local = threading.local()

    def _net(self):
        net = getattr(self._local, "net", None)
        if net is None:
            net = cv2.LightGlueMatcher_create(str(config.LIGHTGLUE_ONNX), 0.0)
            self._local.net = net
        return net

    def match(self, a: Features, b: Features, min_score: float = 0.2,
              max_kps: int = 600) -> tuple[np.ndarray, np.ndarray]:
        """LightGlue on the strongest `max_kps` points of each image.

        Attention cost grows with the square of the point count; 600 points
        keep most of the accuracy at about a third of the time of 1000.
        """
        if self.kind != "lightglue" or len(a) < 8 or len(b) < 8:
            return match_mnn(a.desc, b.desc)
        sa = np.argsort(-a.scores)[:max_kps]
        sb = np.argsort(-b.scores)[:max_kps]
        net = self._net()
        net.setPairInfo(np.ascontiguousarray(a.kps[sa]), np.ascontiguousarray(b.kps[sb]), a.size, b.size)
        ms = net.match(np.ascontiguousarray(a.desc[sa]), np.ascontiguousarray(b.desc[sb]))
        # LightGlue reports 1 - confidence in DMatch.distance.
        good = [m for m in ms if 1.0 - m.distance >= min_score]
        ia = sa[np.array([m.queryIdx for m in good], int)] if good else np.zeros(0, int)
        ib = sb[np.array([m.trainIdx for m in good], int)] if good else np.zeros(0, int)
        return ia, ib


_extractor: Extractor | None = None
_strong: StrongMatcher | None = None


def extractor() -> Extractor:
    global _extractor
    if _extractor is None:
        _extractor = Extractor()
    return _extractor


def strong_matcher() -> StrongMatcher:
    global _strong
    if _strong is None:
        _strong = StrongMatcher()
    return _strong
