"""Merging several volunteers' walks into one map.

Every walk starts at the entrance board, so all walkers already share one
building frame. What they do not share are 3D points: two people who filmed
the same corridor each triangulated their own copy of it, and drift makes the
copies disagree. The merge finds the places that more than one walker saw
(retrieval + LightGlue + PnP), measures how far apart the walkers' estimates
were, fuses the matching points into shared tracks, and lets a joint bundle
adjustment pull everyone into agreement.
"""
from __future__ import annotations

import numpy as np

from ..vision import geometry
from ..vision.features import strong_matcher
from ..vision.retrieval import Vocabulary, top_k
from . import ba
from .model import MapData


def combine(parts: list[MapData]) -> MapData:
    """Concatenate per-walker maps, renumbering keyframes and points."""
    out = MapData()
    for part in parts:
        kf_map = {}
        for kf in part.keyframes:
            kf_map[kf.id] = out.next_kf
            out.next_kf += 1
        p_map = {}
        for p in part.xyz:
            p_map[p] = out.next_point
            out.next_point += 1
        for kf in part.keyframes:
            new = type(kf)(**{**kf.__dict__})
            new.id = kf_map[kf.id]
            new.pid = np.array([p_map.get(int(p), -1) if p >= 0 else -1 for p in kf.pid], int)
            out.keyframes.append(new)
        for p, X in part.xyz.items():
            q = p_map[p]
            out.xyz[q] = X.copy()
            out.desc[q] = part.desc[p]
            out.obs[q] = [(kf_map[k], kp) for k, kp in part.obs[p] if k in kf_map]
        for poi in part.pois:
            poi2 = type(poi)(**{**poi.__dict__})
            poi2.id = len(out.pois)
            poi2.kf = kf_map.get(poi.kf, -1)
            out.pois.append(poi2)
    return out


def _fuse(data: MapData, keep: int, drop: int) -> None:
    if keep == drop or drop not in data.xyz or keep not in data.xyz:
        return
    kfs = data.kf_by_id()
    for kf_id, kp in data.obs[drop]:
        kf = kfs.get(kf_id)
        if kf is None:
            continue
        if kf.pid[kp] == drop:
            kf.pid[kp] = keep
            data.obs[keep].append((kf_id, kp))
    data.xyz.pop(drop, None)
    data.desc.pop(drop, None)
    data.obs.pop(drop, None)


def merge(data: MapData, per_kf_candidates: int = 3, max_pairs: int = 400) -> dict:
    """Find cross-walker overlaps, fuse shared points, and re-adjust jointly."""
    walkers = sorted({kf.walker for kf in data.keyframes})
    stats = {"walkers": walkers, "pairs_tested": 0, "pairs_verified": 0, "points_fused": 0,
             "disagreement_before_m": None, "disagreement_after_m": None}
    if len(walkers) < 2:
        stats["ba"] = ba.adjust(data)
        return stats
    vocab = Vocabulary.train([kf.feats.desc for kf in data.keyframes])
    vl = np.array([vocab.vlad(kf.feats.desc) for kf in data.keyframes])
    kfs = data.keyframes
    pairs = []
    for i, kf in enumerate(kfs):
        others = np.array([j for j, k in enumerate(kfs) if k.walker != kf.walker])
        if len(others) == 0:
            continue
        order, sims = top_k(vl[others], vl[i], per_kf_candidates)
        for o, s in zip(order, sims):
            j = int(others[o])
            if i < j:
                pairs.append((float(s), i, j))
    pairs.sort(reverse=True)
    pairs = pairs[:max_pairs]

    gaps, fused = [], 0
    for _, i, j in pairs:
        a, b = kfs[i], kfs[j]
        stats["pairs_tested"] += 1
        ia, ib = strong_matcher().match(a.feats, b.feats)
        if len(ia) < 25:
            continue
        # Localise keyframe a against b's points: the pose gap is the walkers' disagreement.
        has = b.pid[ib] >= 0
        if has.sum() < 15:
            continue
        pts = np.array([data.xyz[int(p)] for p in b.pid[ib][has]])
        res = geometry.pnp(pts, a.feats.kps[ia][has], a.K)
        if res is None or res[2].sum() < 25:
            continue
        stats["pairs_verified"] += 1
        gaps.append(float(np.linalg.norm(geometry.center(res[0], res[1]) - a.c)))
        for x, y in zip(ia, ib):
            pa, pb = int(a.pid[x]), int(b.pid[y])
            if pa >= 0 and pb >= 0 and pa != pb and pa in data.xyz and pb in data.xyz:
                _fuse(data, pa, pb)
                fused += 1
            elif pa >= 0 and pb < 0 and pa in data.xyz:
                b.pid[y] = pa
                data.obs[pa].append((b.id, int(y)))
            elif pb >= 0 and pa < 0 and pb in data.xyz:
                a.pid[x] = pb
                data.obs[pb].append((a.id, int(x)))
    stats["points_fused"] = fused
    if gaps:
        stats["disagreement_before_m"] = round(float(np.median(gaps)), 3)
    stats["ba"] = ba.adjust(data)

    # Re-measure the same overlaps after the joint adjustment.
    after = []
    for _, i, j in pairs[:80]:
        a, b = kfs[i], kfs[j]
        ia, ib = strong_matcher().match(a.feats, b.feats)
        has = b.pid[ib] >= 0 if len(ib) else np.zeros(0, bool)
        if has.sum() < 15:
            continue
        pts = np.array([data.xyz[int(p)] for p in b.pid[ib][has] if int(p) in data.xyz])
        if len(pts) != has.sum():
            continue
        res = geometry.pnp(pts, a.feats.kps[ia][has], a.K)
        if res is not None and res[2].sum() >= 25:
            after.append(float(np.linalg.norm(geometry.center(res[0], res[1]) - a.c)))
    if after:
        stats["disagreement_after_m"] = round(float(np.median(after)), 3)
    return stats
