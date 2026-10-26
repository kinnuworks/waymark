"""Map data: keyframes, 3D points and their observations."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..vision.features import Features
from ..vision.geometry import center, yaw_deg


@dataclass
class Keyframe:
    id: int
    walker: str
    R: np.ndarray
    t: np.ndarray
    K: np.ndarray
    feats: Features
    pid: np.ndarray                      # point id per keypoint, -1 if none
    t_ms: int = 0
    board: tuple[np.ndarray, np.ndarray] | None = None  # (corner pixels, corner world points)
    heading_deg: float | None = None
    anchor: tuple[np.ndarray, np.ndarray] | None = None  # known pose (R, t), used by evaluation

    @property
    def c(self) -> np.ndarray:
        return center(self.R, self.t)

    @property
    def yaw(self) -> float:
        return yaw_deg(self.R)


@dataclass
class Poi:
    id: int
    label: str
    kind: str
    x: float
    z: float
    walker: str
    kf: int


@dataclass
class MapData:
    keyframes: list[Keyframe] = field(default_factory=list)
    xyz: dict[int, np.ndarray] = field(default_factory=dict)
    desc: dict[int, np.ndarray] = field(default_factory=dict)
    obs: dict[int, list[tuple[int, int]]] = field(default_factory=dict)
    pois: list[Poi] = field(default_factory=list)
    next_point: int = 0
    next_kf: int = 0

    def kf_by_id(self) -> dict[int, Keyframe]:
        return {k.id: k for k in self.keyframes}

    def add_point(self, X: np.ndarray, d: np.ndarray, observations: list[tuple[int, int]]) -> int:
        pid = self.next_point
        self.next_point += 1
        self.xyz[pid] = np.asarray(X, np.float64)
        self.desc[pid] = np.asarray(d, np.float32)
        self.obs[pid] = list(observations)
        return pid

    def remove_point(self, pid: int) -> None:
        kfs = self.kf_by_id()
        for kf_id, kp in self.obs.get(pid, []):
            kf = kfs.get(kf_id)
            if kf is not None and kp < len(kf.pid) and kf.pid[kp] == pid:
                kf.pid[kp] = -1
        self.xyz.pop(pid, None)
        self.desc.pop(pid, None)
        self.obs.pop(pid, None)

    def points_of(self, kfs: list[Keyframe]) -> np.ndarray:
        ids = set()
        for kf in kfs:
            ids.update(int(p) for p in kf.pid[kf.pid >= 0])
        return np.array(sorted(i for i in ids if i in self.xyz), int)

    def arrays(self, ids: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        if len(ids) == 0:
            return np.zeros((0, 3)), np.zeros((0, 128), np.float32)
        return (np.array([self.xyz[i] for i in ids]),
                np.array([self.desc[i] for i in ids], np.float32))

    def trajectory(self, walker: str | None = None) -> np.ndarray:
        cs = [kf.c for kf in self.keyframes if walker is None or kf.walker == walker]
        return np.array(cs) if cs else np.zeros((0, 3))
