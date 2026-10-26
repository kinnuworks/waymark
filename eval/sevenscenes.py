"""Benchmarks on Microsoft 7-Scenes (Shotton et al., CVPR 2013).

Three experiments, all using the same code as the live service:

  walk     One sequence plays the part of a volunteer walk. Only the first and
           last few frames get their true pose (standing in for the entrance
           board at the start and end of a walk). Everything in between is
           tracked. Reports trajectory error before and after bundle adjustment.
  merge    Two sequences play two walkers in the same room. Reports how far
           apart their maps were before and after the merge.
  reloc    Standard relocalisation protocol: a map triangulated from the
           training sequences with their reference poses, then every test frame
           localised. Reports median translation and rotation error and the
           share within 5 cm and 5 degrees, comparable to published HLoc results.

Usage: python eval/sevenscenes.py SCENE [walk|merge|reloc|all] [--step N]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from waymark.localize.localizer import Localizer  # noqa: E402
from waymark.mapping import ba, merge  # noqa: E402
from waymark.mapping.model import Keyframe, MapData  # noqa: E402
from waymark.mapping.tracker import WalkerTracker  # noqa: E402
from waymark.vision import geometry  # noqa: E402
from waymark.vision.board import BoardSpec  # noqa: E402
from waymark.vision.features import extractor, strong_matcher  # noqa: E402

DATA = ROOT / "eval" / "data" / "7scenes"
OUT = ROOT / "eval" / "out"
# 7-Scenes RGB intrinsics as used by DSAC*, HLoc and most relocalisation work.
K = np.array([[525.0, 0, 320.0], [0, 525.0, 240.0], [0, 0, 1]])


_ALIGN: dict[str, tuple[np.ndarray, np.ndarray]] = {}


def _raw(scene: str, seq: int) -> list[tuple[Path, np.ndarray]]:
    d = DATA / scene / f"seq-{seq:02d}"
    return [(f, np.loadtxt(str(f).replace(".color.png", ".pose.txt")))  # camera-to-world
            for f in sorted(d.glob("frame-*.color.png"))]


def alignment(scene: str) -> tuple[np.ndarray, np.ndarray]:
    """Rotate the dataset frame so y is up and shift so cameras sit 1.4 m above the floor.

    7-Scenes poses have no gravity direction. People hold the camera roughly
    upright, so the mean camera "down" axis is a good estimate of gravity.
    Errors are measured between poses in the same frame, so this changes no
    metric; it only lets the floor plan treat y as height.
    """
    if scene in _ALIGN:
        return _ALIGN[scene]
    seqs = sorted(int(p.name.split("-")[1]) for p in (DATA / scene).glob("seq-*") if p.is_dir())
    Ts = [T for s in seqs for _, T in _raw(scene, s)[::10]]
    down = np.mean([T[:3, 1] for T in Ts], axis=0)  # camera +y (image down) in world
    down /= np.linalg.norm(down)
    up = -down
    # Rotation taking `up` to +y.
    v = np.cross(up, [0, 1.0, 0])
    c = float(np.dot(up, [0, 1.0, 0]))
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    A = np.eye(3) + vx + vx @ vx / (1 + c)
    centers = np.array([A @ T[:3, 3] for T in Ts])
    b = np.array([-centers[0, 0], 1.4 - centers[:, 1].mean(), -centers[0, 2]])
    _ALIGN[scene] = (A, b)
    return A, b


def seq_frames(scene: str, seq: int) -> list[tuple[Path, np.ndarray, np.ndarray]]:
    """Frames with world-to-camera poses in the gravity-aligned frame."""
    A, b = alignment(scene)
    out = []
    for f, T in _raw(scene, seq):
        Rcw = T[:3, :3].T @ A.T
        C = A @ T[:3, 3] + b
        out.append((f, Rcw, -Rcw @ C))
    return out


def split(scene: str, name: str) -> list[int]:
    txt = (DATA / scene / name).read_text().split()
    return [int(x.replace("sequence", "")) for x in txt]


def ate(est: np.ndarray, gt: np.ndarray) -> dict:
    e = np.linalg.norm(est - gt, axis=1)
    return {"mean_m": round(float(e.mean()), 4), "median_m": round(float(np.median(e)), 4),
            "rmse_m": round(float(np.sqrt((e ** 2).mean())), 4), "max_m": round(float(e.max()), 4)}


def run_walk(scene: str, seq: int, step: int, anchor_n: int = 10, walker: str = "w1") -> tuple[MapData, dict]:
    frames = seq_frames(scene, seq)[::step]
    tr = WalkerTracker(walker, BoardSpec())
    n = len(frames)
    states = {}
    track_est, track_gt = [], []
    t0 = time.perf_counter()
    for i, (f, R, t) in enumerate(frames):
        img = cv2.imread(str(f))
        anchor = (R, t) if (i < anchor_n or i >= n - anchor_n) else None
        res = tr.process(img, t_ms=i, anchor=anchor, K=K)
        states[res["state"]] = states.get(res["state"], 0) + 1
        if res["state"] == "tracking" and anchor is None and tr.last_pose is not None:
            track_est.append(geometry.center(*tr.last_pose))
            track_gt.append(geometry.center(R, t))
    secs = time.perf_counter() - t0
    data = tr.data
    gt_by_t = {i: frames[i] for i in range(n)}
    kf_gt = np.array([geometry.center(gt_by_t[kf.t_ms][1], gt_by_t[kf.t_ms][2]) for kf in data.keyframes])
    before = ate(np.array([kf.c for kf in data.keyframes]), kf_gt)
    ba_stats = ba.adjust(data)
    after = ate(np.array([kf.c for kf in data.keyframes]), kf_gt)
    return data, {"scene": scene, "seq": seq, "frames": n, "step": step, "anchored_frames": 2 * anchor_n,
                  "states": states, "keyframes": len(data.keyframes), "points": len(data.xyz),
                  "tracked_frames": len(track_est),
                  "live_tracking_error": ate(np.array(track_est), np.array(track_gt)) if track_est else None,
                  "keyframe_error_before_ba": before, "keyframe_error_after_ba": after, "ba": ba_stats,
                  "ms_per_frame": round(secs / n * 1000, 1)}


def run_merge(scene: str, seqs: list[int], step: int) -> dict:
    parts, walks = [], []
    for k, s in enumerate(seqs):
        data, st = run_walk(scene, s, step, walker=f"w{k + 1}")
        parts.append(data)
        walks.append(st)
    data = merge.combine(parts)
    stats = merge.merge(data)
    gts = {}
    for k, s in enumerate(seqs):
        fr = seq_frames(scene, s)[::step]
        for kf in [kf for kf in data.keyframes if kf.walker == f"w{k + 1}"]:
            gts[kf.id] = geometry.center(fr[kf.t_ms][1], fr[kf.t_ms][2])
    err = ate(np.array([kf.c for kf in data.keyframes]), np.array([gts[kf.id] for kf in data.keyframes]))
    return {"scene": scene, "walkers": seqs, "walks": walks, "merge": stats, "merged_keyframe_error": err}


def build_reference_map(scene: str, seqs: list[int], step: int) -> MapData:
    """Triangulate a map from reference poses, the same protocol HLoc uses for 7-Scenes."""
    data = MapData()
    ex, sm = extractor(), strong_matcher()
    for s in seqs:
        prev: list[Keyframe] = []
        for i, (f, R, t) in enumerate(seq_frames(scene, s)[::step]):
            img = cv2.imread(str(f))
            feats = ex.extract(img)
            kf = Keyframe(id=data.next_kf, walker=f"s{s}", R=R, t=t, K=K, feats=feats,
                          pid=np.full(len(feats), -1, int), t_ms=i, anchor=(R, t))
            data.next_kf += 1
            for p in prev[-2:][::-1]:
                ia, ib = sm.match(p.feats, kf.feats)
                for a_, b_ in zip(ia, ib):
                    pa = p.pid[a_]
                    if pa >= 0 and kf.pid[b_] < 0:
                        kf.pid[b_] = pa
                        data.obs[pa].append((kf.id, int(b_)))
                free = (p.pid[ia] < 0) & (kf.pid[ib] < 0)
                ia, ib = ia[free], ib[free]
                X, keep = geometry.triangulate(K, p.R, p.t, R, t, p.feats.kps[ia], feats.kps[ib])
                for X_, a_, b_ in zip(X[keep], ia[keep], ib[keep]):
                    pid = data.add_point(X_, feats.desc[b_], [(p.id, int(a_)), (kf.id, int(b_))])
                    p.pid[a_] = pid
                    kf.pid[b_] = pid
            data.keyframes.append(kf)
            prev.append(kf)
    return data


def run_reloc(scene: str, map_step: int = 10, test_step: int = 10) -> dict:
    t0 = time.perf_counter()
    data = build_reference_map(scene, split(scene, "TrainSplit.txt"), map_step)
    ba_stats = ba.adjust(data, max_nfev=20)
    build_s = time.perf_counter() - t0
    loc = Localizer(data, check_height=False)
    terr, rerr, times, fails = [], [], [], 0
    for s in split(scene, "TestSplit.txt"):
        for f, R, t in seq_frames(scene, s)[::test_step]:
            img = cv2.imread(str(f))
            t1 = time.perf_counter()
            feats = extractor().extract(img)
            fix = loc.localize(feats, 0.82, K=K)
            times.append((time.perf_counter() - t1) * 1000)
            if fix is None:
                fails += 1
                terr.append(np.inf)
                rerr.append(np.inf)
                continue
            terr.append(float(np.linalg.norm(fix.c - geometry.center(R, t))))
            rerr.append(geometry.angle_between(fix.R, R))
    terr, rerr = np.array(terr), np.array(rerr)
    return {"scene": scene, "map_keyframes": len(data.keyframes), "map_points": len(data.xyz),
            "map_build_s": round(build_s, 1), "map_ba": ba_stats, "queries": int(len(terr)), "failures": fails,
            "median_cm": round(float(np.median(terr) * 100), 2), "median_deg": round(float(np.median(rerr)), 2),
            "within_5cm_5deg_pct": round(float(np.mean((terr < 0.05) & (rerr < 5)) * 100), 1),
            "within_25cm_10deg_pct": round(float(np.mean((terr < 0.25) & (rerr < 10)) * 100), 1),
            "ms_median": round(float(np.median(times)), 1), "ms_p95": round(float(np.percentile(times, 95)), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scene")
    ap.add_argument("what", default="all", nargs="?")
    ap.add_argument("--step", type=int, default=5)
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    out = {}
    if args.what in ("walk", "all"):
        _, out["walk"] = run_walk(args.scene, split(args.scene, "TrainSplit.txt")[0], args.step)
        print(json.dumps(out["walk"], indent=1), flush=True)
    if args.what in ("merge", "all"):
        tr = split(args.scene, "TrainSplit.txt") + split(args.scene, "TestSplit.txt")
        if len(tr) >= 2:
            out["merge"] = run_merge(args.scene, tr[:2], args.step)
            print(json.dumps({k: v for k, v in out["merge"].items() if k != "walks"}, indent=1), flush=True)
    if args.what in ("reloc", "all"):
        out["reloc"] = run_reloc(args.scene)
        print(json.dumps(out["reloc"], indent=1), flush=True)
    (OUT / f"7scenes_{args.scene}_{args.what}.json").write_text(json.dumps(out, indent=1, default=float))


if __name__ == "__main__":
    main()
