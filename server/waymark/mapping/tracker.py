"""Incremental keyframe mapping for one volunteer walker.

Each walk starts at the entrance board, which gives the first poses directly
in the building frame. After that the camera is tracked against the walker's
own 3D points with PnP, new keyframes triangulate new points against the
previous ones, and the board is used again whenever it comes back into view
(including at the end of the walk, which closes the loop).
"""
from __future__ import annotations

import cv2
import numpy as np

from ..vision import camera, geometry
from ..vision.board import BoardDetector, BoardSpec
from ..vision.features import Features, extractor, match_mnn, strong_matcher
from ..vision.retrieval import Vocabulary, top_k
from .model import Keyframe, MapData, Poi

KF_DIST_M = 0.30
KF_ANGLE_DEG = 12.0
MIN_TRACK_INLIERS = 20


class WalkerTracker:
    def __init__(self, walker_id: str, spec: BoardSpec, data: MapData | None = None):
        self.walker = walker_id
        self.spec = spec
        self.board = BoardDetector(spec)
        self.data = data or MapData()
        self.calib: dict | None = None
        self.last_pose: tuple[np.ndarray, np.ndarray] | None = None
        self.lost_frames = 0
        self.frames = 0
        self.vocab: Vocabulary | None = None
        self.vlad: dict[int, np.ndarray] = {}
        self.finished = False

    # ---- intrinsics -------------------------------------------------------------
    def K_for(self, size: tuple[int, int]) -> np.ndarray:
        if self.calib is not None:
            return camera.scale_K(self.calib["K"], self.calib["size"], size)
        return camera.default_K(*size)

    # ---- main entry ---------------------------------------------------------------
    def process(self, img: np.ndarray, t_ms: int = 0, heading: float | None = None,
                anchor: tuple[np.ndarray, np.ndarray] | None = None, K: np.ndarray | None = None) -> dict:
        """Track one frame. `anchor` is a known pose standing in for the board (evaluation only)."""
        self.frames += 1
        gray = img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        q = camera.quality(gray)
        size = (gray.shape[1], gray.shape[0])
        K = self.K_for(size) if K is None else K
        board = self.board.pose(gray, K) if anchor is None else (anchor[0], anchor[1], 0.0, 99)

        if not self.data.keyframes:
            if board is None:
                return self._result("need_board", q, None, "need_board")
            R, t, rms, n = board
            feats = extractor().extract(img)
            kf = self._add_keyframe(R, t, K, feats, t_ms, heading, gray, board_used=True, tracked=None)
            if anchor is not None:
                kf.anchor = anchor
            self.last_pose = (R, t)
            return self._result("tracking", q, (R, t), "board_start")

        if not q["ok"] and board is None:
            return self._result("rejected", q, self.last_pose, q["reason"])

        feats = extractor().extract(img)
        pose, tracked, source = None, None, None
        if board is not None and board[2] < 2.5 and board[3] >= 10:
            pose, source = (board[0], board[1]), "board"
            tracked = self._track(feats, K, prior=pose)
            if tracked is not None:
                tracked = (pose[0], pose[1], tracked[2], tracked[3], tracked[4])
        else:
            tracked = self._track(feats, K, prior=self.last_pose)
            if tracked is None:
                tracked = self._relocalise(feats, K)
            if tracked is not None:
                pose, source = (tracked[0], tracked[1]), "track"

        if pose is None:
            self.lost_frames += 1
            if self._needs_bootstrap():
                return self._result("need_board", q, self.last_pose, "bootstrap")
            return self._result("lost", q, self.last_pose, "lost")

        self.lost_frames = 0
        self.last_pose = pose
        last = self.data.keyframes[-1]
        dist = float(np.linalg.norm(geometry.center(*pose) - last.c))
        ang = geometry.angle_between(pose[0], last.R)
        weak = tracked is not None and int(tracked[3].sum()) < 150
        # While the map is still tiny, take keyframes more often so the first
        # points get triangulated from the short sway in front of the board.
        boot = len(self.data.xyz) < 300
        kf_dist, kf_ang = (0.06, 3.0) if boot else (KF_DIST_M, KF_ANGLE_DEG)
        if dist > kf_dist or ang > kf_ang or (weak and dist > 0.1):
            kf = self._add_keyframe(pose[0], pose[1], K, feats, t_ms, heading, gray,
                                    board_used=(source == "board" and anchor is None), tracked=tracked)
            if anchor is not None:
                kf.anchor = anchor
        return self._result("tracking", q, pose, source)

    # ---- tracking -------------------------------------------------------------
    def _local_keyframes(self, prior) -> list[Keyframe]:
        kfs = self.data.keyframes
        chosen = {kf.id: kf for kf in kfs[-4:]}
        if prior is not None:
            c = geometry.center(*prior)
            near = sorted(kfs, key=lambda k: np.linalg.norm(k.c - c))[:6]
            for kf in near:
                chosen.setdefault(kf.id, kf)
        return list(chosen.values())

    def _track(self, feats: Features, K, prior):
        """PnP against points seen by nearby keyframes. Returns (R, t, ia, mask, ids) or None."""
        ids = self.data.points_of(self._local_keyframes(prior))
        if len(ids) < 30 or len(feats) < 30:
            return None
        xyz, pdesc = self.data.arrays(ids)
        ia, ib = match_mnn(feats.desc, pdesc, max_dist=1.0, ratio=0.92)
        res = geometry.pnp(xyz[ib], feats.kps[ia], K) if len(ia) >= 12 else None
        if res is None or res[2].sum() < MIN_TRACK_INLIERS:
            # Second chance: LightGlue against the most recent keyframe with points.
            last = next((k for k in reversed(self.data.keyframes) if (k.pid >= 0).sum() > 30), None)
            if last is None:
                return None
            ja, jb = strong_matcher().match(feats, last.feats)
            has = last.pid[jb] >= 0
            ja, jb = ja[has], jb[has]
            if len(ja) < 12:
                return None
            pts = np.array([self.data.xyz[int(p)] for p in last.pid[jb]])
            res = geometry.pnp(pts, feats.kps[ja], K)
            if res is None or res[2].sum() < MIN_TRACK_INLIERS:
                return None
            return res[0], res[1], ja, res[2], last.pid[jb]
        return res[0], res[1], ia, res[2], ids[ib]

    def _relocalise(self, feats: Features, K):
        if self.vocab is None or not self.vlad:
            return None
        q = self.vocab.vlad(feats.desc)
        kf_ids = np.array(list(self.vlad.keys()))
        db = np.array([self.vlad[i] for i in kf_ids])
        order, _ = top_k(db, q, 4)
        kfs = self.data.kf_by_id()
        return self._track_against([kfs[int(kf_ids[o])] for o in order], feats, K)

    def _track_against(self, kfs: list[Keyframe], feats: Features, K):
        ids = self.data.points_of(kfs)
        if len(ids) < 30:
            return None
        xyz, pdesc = self.data.arrays(ids)
        ia, ib = match_mnn(feats.desc, pdesc, max_dist=1.0, ratio=0.92)
        res = geometry.pnp(xyz[ib], feats.kps[ia], K) if len(ia) >= 12 else None
        if res is None or res[2].sum() < MIN_TRACK_INLIERS:
            return None
        return res[0], res[1], ia, res[2], ids[ib]

    def _needs_bootstrap(self) -> bool:
        return len(self.data.xyz) < 30

    # ---- keyframes --------------------------------------------------------------
    def _add_keyframe(self, R, t, K, feats: Features, t_ms, heading, gray, board_used: bool, tracked) -> Keyframe:
        d = self.data
        kf = Keyframe(id=d.next_kf, walker=self.walker, R=R, t=t, K=K, feats=feats,
                      pid=np.full(len(feats), -1, int), t_ms=t_ms, heading_deg=heading)
        d.next_kf += 1
        if board_used:
            found = self.board.detect(gray)
            if found is not None:
                kf.board = (found[0], self.board.object_points(found[1]))
        if tracked is not None:
            _, _, ia, mask, pids = tracked
            for kp, pid in zip(ia[mask], pids[mask]):
                pid = int(pid)
                if kf.pid[kp] < 0 and pid in d.xyz:
                    kf.pid[kp] = pid
                    d.obs[pid].append((kf.id, int(kp)))
        # Triangulate against the last two keyframes (the older one gives a wider baseline).
        # LightGlue is the expensive step (about 0.56 s per pair on Graviton4),
        # so steady-state keyframes match only the previous keyframe; the
        # bootstrap uses two more for a wider baseline.
        if len(d.xyz) < 300:
            partners = d.keyframes[-2:][::-1] + d.keyframes[:1] * (len(d.keyframes) > 2)
        else:
            partners = d.keyframes[-1:]
        for prev in partners:
            self._triangulate_pair(prev, kf)
        d.keyframes.append(kf)
        self._update_retrieval(kf)
        return kf

    def _triangulate_pair(self, a: Keyframe, b: Keyframe) -> int:
        d = self.data
        ia, ib = strong_matcher().match(a.feats, b.feats)
        if len(ia) == 0:
            return 0
        added = 0
        # Matches where the old keyframe already has a point: extend that track.
        for i, j in zip(ia, ib):
            pa = a.pid[i]
            if pa >= 0 and b.pid[j] < 0 and pa in d.xyz:
                uv, z = geometry.project(b.K, b.R, b.t, d.xyz[pa][None])
                if z[0] > 0 and np.linalg.norm(uv[0] - b.feats.kps[j]) < 4.0:
                    b.pid[j] = pa
                    d.obs[pa].append((b.id, int(j)))
        free = (a.pid[ia] < 0) & (b.pid[ib] < 0)
        ia, ib = ia[free], ib[free]
        if len(ia) == 0:
            return 0
        X, keep = geometry.triangulate(a.K, a.R, a.t, b.R, b.t, a.feats.kps[ia], b.feats.kps[ib])
        for X_, i, j in zip(X[keep], ia[keep], ib[keep]):
            pid = d.add_point(X_, b.feats.desc[j], [(a.id, int(i)), (b.id, int(j))])
            a.pid[i] = pid
            b.pid[j] = pid
            added += 1
        return added

    def _update_retrieval(self, kf: Keyframe) -> None:
        n = len(self.data.keyframes)
        if self.vocab is None and n >= 8 or (self.vocab is not None and n % 25 == 0):
            self.vocab = Vocabulary.train([k.feats.desc for k in self.data.keyframes])
            self.vlad = {k.id: self.vocab.vlad(k.feats.desc) for k in self.data.keyframes}
        elif self.vocab is not None:
            self.vlad[kf.id] = self.vocab.vlad(kf.feats.desc)

    # ---- places -----------------------------------------------------------------
    def mark(self, label: str, kind: str) -> Poi | None:
        if self.last_pose is None or not self.data.keyframes:
            return None
        c = geometry.center(*self.last_pose)
        # A place is marked where the volunteer stands, about half a metre in
        # front of the phone they are holding.
        f = geometry.forward(self.last_pose[0])
        f2 = np.array([f[0], 0, f[2]])
        f2 /= np.linalg.norm(f2) + 1e-9
        p = c + 0.5 * f2
        poi = Poi(id=len(self.data.pois), label=label, kind=kind, x=float(p[0]), z=float(p[2]),
                  walker=self.walker, kf=self.data.keyframes[-1].id)
        self.data.pois.append(poi)
        return poi

    # ---- output -------------------------------------------------------------------
    def _result(self, state: str, q: dict, pose, code: str) -> dict:
        out = {"state": state, "quality": q, "code": code,
               "keyframes": len(self.data.keyframes), "points": len(self.data.xyz), "pose": None}
        if pose is not None:
            c = geometry.center(*pose)
            out["pose"] = {"x": round(float(c[0]), 3), "y": round(float(c[1]), 3),
                           "z": round(float(c[2]), 3), "yaw_deg": round(geometry.yaw_deg(pose[0]), 1)}
        return out
