"""Image retrieval with VLAD over ALIKED descriptors.

Relocalisation first needs a shortlist of keyframes that look like the query.
VLAD (k-means residuals, power- and L2-normalised) is cheap and works on any
descriptor, so the vocabulary is trained per building from its own keyframes
with cv2.kmeans.
"""
from __future__ import annotations

import cv2
import numpy as np


class Vocabulary:
    def __getstate__(self):
        return {"centers": self.centers}

    def __setstate__(self, state):
        self.__init__(state["centers"])

    def __init__(self, centers: np.ndarray):
        self.centers = centers.astype(np.float32)
        self._matcher = cv2.BFMatcher(cv2.NORM_L2)

    @classmethod
    def train(cls, descs: list[np.ndarray], k: int = 32, sample: int = 60000, seed: int = 0) -> "Vocabulary":
        allx = np.vstack([d for d in descs if len(d)]).astype(np.float32)
        rng = np.random.default_rng(seed)
        if len(allx) > sample:
            allx = allx[rng.choice(len(allx), sample, replace=False)]
        k = max(2, min(k, len(allx) // 20))
        crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 1e-4)
        cv2.setRNGSeed(seed)
        _, _, centers = cv2.kmeans(allx, k, None, crit, 2, cv2.KMEANS_PP_CENTERS)
        return cls(centers)

    def vlad(self, desc: np.ndarray) -> np.ndarray:
        k, d = self.centers.shape
        v = np.zeros((k, d), np.float32)
        if len(desc):
            nn = np.array([m.trainIdx for m in self._matcher.match(desc.astype(np.float32), self.centers)])
            for c in range(k):
                sel = desc[nn == c]
                if len(sel):
                    r = (sel - self.centers[c]).sum(axis=0)
                    v[c] = r / (np.linalg.norm(r) + 1e-12)  # intra-normalisation
        v = v.reshape(-1)
        v = np.sign(v) * np.sqrt(np.abs(v))
        return v / (np.linalg.norm(v) + 1e-12)


def top_k(db: np.ndarray, q: np.ndarray, k: int) -> tuple[np.ndarray, np.ndarray]:
    if len(db) == 0:
        return np.zeros(0, int), np.zeros(0)
    sims = db @ q
    order = np.argsort(-sims)[:k]
    return order, sims[order]
