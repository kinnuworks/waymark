"""End-to-end system replay: real indoor video, a virtual entrance board, the real HTTP API.

The public 7-Scenes sequences have ground-truth camera poses but no printed
board. This script places a virtual ChArUco board on a wall of the room, renders
it into every frame where the real camera would see it (a homography from the
ground-truth pose), and then drives the actual HTTP API exactly as phones do:

  1. each volunteer calibrates from board views and walks (one sequence each);
  2. places are marked; the map is built (merge + board-anchored adjustment);
  3. a visitor replays a third pass with no board and no poses, asks for a
     place, and is guided.

Every pose the system reports is compared with the ground truth expressed in
the board's building frame. Nothing in the pipeline sees the ground truth.

Usage: python eval/system_replay.py [--scene heads] [--step 10] [--url URL]
Without --url it runs the app in-process; with --url it drives a deployed server.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "eval"))

from sevenscenes import K, OUT, seq_frames  # noqa: E402
from waymark.vision.board import BoardSpec  # noqa: E402

SPEC = BoardSpec(square_m=0.035, height_m=1.2)


def board_pose_in_world(R0: np.ndarray, t0: np.ndarray, dist: float = 0.7):
    """Put the board `dist` metres in front of the first camera, upright, facing it."""
    C = -R0.T @ t0
    f = R0.T @ np.array([0, 0, 1.0])
    f[1] = 0
    f /= np.linalg.norm(f)
    centre = C + dist * f
    centre[1] = C[1]  # board centre at camera height
    z_b = -f                                   # building z: out of the wall, towards the camera
    y_b = np.array([0, 1.0, 0])                # up
    x_b = np.cross(y_b, z_b)                   # right as seen facing the board
    Rbw = np.stack([x_b, y_b, z_b], axis=1)    # building axes in world
    height = float(centre[1])                  # world y is height above the floor (see sevenscenes.alignment)
    return Rbw, centre, height


def to_building(Rcw, tcw, Rbw, centre, height):
    """Ground-truth world-to-camera pose expressed in the building frame."""
    # X_w = Rbw @ (X_b - [0, h, 0]) + centre
    off = np.array([0, height, 0])
    R = Rcw @ Rbw
    t = Rcw @ (centre - Rbw @ off) + tcw
    return R, t


def render_board(img, Rcb, tcb, spec: BoardSpec, cache={}):
    """Draw the board into the image if the camera would see it. Returns (img, visible)."""
    key = spec.square_m
    if key not in cache:
        cache[key] = spec.board().generateImage((700, 500), marginSize=0)
    tex = cache[key]
    bw, bh = 7 * spec.square_m, 5 * spec.square_m
    # Board corners in the building frame: centre (0, h, 0), in the z = 0 wall plane.
    h = spec.height_m
    corners = np.array([[-bw / 2, h + bh / 2, 0], [bw / 2, h + bh / 2, 0],
                        [bw / 2, h - bh / 2, 0], [-bw / 2, h - bh / 2, 0]])
    Xc = (Rcb @ corners.T).T + tcb
    if (Xc[:, 2] < 0.15).any():
        return img, False
    uv = (K @ Xc.T).T
    uv = uv[:, :2] / uv[:, 2:3]
    hgt, wid = img.shape[:2]
    if (uv[:, 0] < 0).any() or (uv[:, 0] > wid).any() or (uv[:, 1] < 0).any() or (uv[:, 1] > hgt).any():
        return img, False
    # The board must face the camera.
    n = Rcb @ np.array([0, 0, 1.0])
    if np.dot(n, Xc.mean(axis=0)) > 0:
        return img, False
    H = cv2.getPerspectiveTransform(np.float32([[0, 0], [700, 0], [700, 500], [0, 500]]), uv.astype(np.float32))
    warped = cv2.warpPerspective(tex, H, (wid, hgt), flags=cv2.INTER_AREA, borderValue=0)
    mask = cv2.warpPerspective(np.full((500, 700), 255, np.uint8), H, (wid, hgt))
    out = img.copy()
    # Paper under room light: soften the pure black and white towards the frame's brightness.
    shade = float(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY).mean()) / 255.0
    paper = (warped.astype(np.float32) * (0.55 + 0.4 * shade) + 18).clip(0, 255).astype(np.uint8)
    m = mask > 0
    out[m] = cv2.cvtColor(paper, cv2.COLOR_GRAY2BGR)[m]
    return out, True


def calib_views(n=10, seed=0):
    """Synthetic board photos at varied poses, taken with the dataset's real intrinsics."""
    rng = np.random.default_rng(seed)
    views = []
    for i in range(n):
        yaw, pitch = rng.uniform(-35, 35), rng.uniform(-25, 25)
        R = cv2.Rodrigues(np.radians([pitch, yaw, rng.uniform(-10, 10)]))[0]
        Rwc = R @ np.diag([1.0, -1.0, -1.0])
        C = np.array([0, SPEC.height_m, 0]) + Rwc @ np.array([rng.uniform(-0.06, 0.06), rng.uniform(-0.05, 0.05), -rng.uniform(0.38, 0.6)])
        Rcb = Rwc.T
        tcb = -Rcb @ C
        base = np.full((480, 640, 3), 150, np.uint8)
        img, ok = render_board(base, Rcb, tcb, SPEC)
        if ok:
            views.append(img)
    return views


class Client:
    def __init__(self, url: str | None):
        if url:
            import urllib.request
            self.url = url.rstrip("/")
            self._req = urllib.request
            self.tc = None
        else:
            os.environ.setdefault("WAYMARK_DATA", tempfile.mkdtemp())
            from fastapi.testclient import TestClient
            from waymark.api import app
            self.tc = TestClient(app)

    def get(self, path):
        if self.tc:
            return self.tc.get(path).json()
        with self._req.urlopen(self.url + path, timeout=120) as r:
            return json.loads(r.read())

    def post(self, path, body=None, image=None, form=None):
        if self.tc:
            if image is not None:
                return self.tc.post(path, files={"image": ("f.jpg", image, "image/jpeg")}, data=form or {}).json()
            return self.tc.post(path, json=body or {}).json()
        import uuid
        if image is not None:
            b = uuid.uuid4().hex
            parts = [f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode() for k, v in (form or {}).items()]
            parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"f.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n".encode() + image + b"\r\n")
            data = b"".join(parts) + f"--{b}--\r\n".encode()
            req = self._req.Request(self.url + path, data=data, headers={"Content-Type": f"multipart/form-data; boundary={b}"})
        else:
            req = self._req.Request(self.url + path, data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json"})
        with self._req.urlopen(req, timeout=300) as r:
            return json.loads(r.read())


def jpg(img):
    return cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scene", default="heads")
    ap.add_argument("--step", type=int, default=10)
    ap.add_argument("--url", default=None)
    ap.add_argument("--name", default=None)
    ap.add_argument("--places", default=None, help='JSON list of [fraction, label, kind]')
    args = ap.parse_args()
    cl = Client(args.url)
    seqs = sorted(int(p.name.split("-")[1]) for p in (Path(os.environ.get("SEVENSCENES", ROOT / "eval/data/7scenes")) / args.scene).glob("seq-*") if p.is_dir())
    walks = {s: seq_frames(args.scene, s) for s in seqs[:2]}
    first = walks[seqs[0]][0]
    Rbw, centre, height = board_pose_in_world(first[1], first[2])
    spec = BoardSpec(square_m=SPEC.square_m, height_m=round(height, 3))
    SPEC.height_m = spec.height_m
    name = args.name or f"Sample room: Microsoft 7-Scenes '{args.scene}' replayed through the live system"
    bid = cl.post("/api/buildings", {"name": name, "board_height_m": spec.height_m, "square_mm": 35.0})["id"]
    report = {"scene": args.scene, "board_height_m": spec.height_m, "walkers": {}}
    places = json.loads(args.places) if args.places else [(0.3, "Reading desk", "desk"), (0.55, "Bookshelf", "other"), (0.8, "Door to the corridor", "door")]

    for k, s in enumerate(seqs[:2]):
        wid = cl.post(f"/api/buildings/{bid}/walkers", {"name": f"Volunteer {'AB'[k]}"})["walker_id"]
        cal = None
        for v in calib_views(12, seed=k):
            cal = cl.post(f"/api/buildings/{bid}/walkers/{wid}/calib", image=jpg(v))
            if cal.get("done"):
                break
        frames = walks[s][::args.step]
        # Walks start and end at the board: replay the first frames towards it and the last ones back.
        est, gt, states, board_frames, t0 = [], [], {}, 0, time.time()
        n = len(frames)
        for i, (f, Rcw, tcw) in enumerate(frames):
            Rcb, tcb = to_building(Rcw, tcw, Rbw, centre, height)
            img = cv2.imread(str(f))
            img, vis = render_board(img, Rcb, tcb, spec)
            board_frames += vis
            r = cl.post(f"/api/buildings/{bid}/walkers/{wid}/frame", image=jpg(img), form={"t_ms": str(i * 700)})
            states[r["state"]] = states.get(r["state"], 0) + 1
            if r.get("pose") and r["state"] == "tracking":
                c = -Rcb.T @ tcb
                est.append([r["pose"]["x"], r["pose"]["y"], r["pose"]["z"]])
                gt.append(c.tolist())
            if k == 0:
                for frac, label, kind in places:
                    if i == int(frac * n):
                        cl.post(f"/api/buildings/{bid}/walkers/{wid}/poi", {"label": label, "kind": kind})
        cl.post(f"/api/buildings/{bid}/walkers/{wid}/finish")
        e = np.linalg.norm(np.array(est) - np.array(gt), axis=1) if est else np.zeros(0)
        report["walkers"][wid] = {"sequence": s, "frames": n, "frames_with_board": int(board_frames), "states": states,
                                  "calibration": cal, "live_error_median_m": round(float(np.median(e)), 3) if len(e) else None,
                                  "seconds_per_frame": round((time.time() - t0) / n, 2)}
        print(json.dumps(report["walkers"][wid]), flush=True)

    t0 = time.time()
    build = cl.post(f"/api/buildings/{bid}/build")
    report["build"] = build.get("stats")
    report["build_seconds"] = round(time.time() - t0, 1)
    m = cl.get(f"/api/buildings/{bid}/map")
    report["map"] = {"pois": len(m["pois"]), "nodes": len(m["nodes"]), "walls": len(m["walls"]), "coverage_pct": m.get("coverage_pct")}
    print("build", json.dumps({k: v for k, v in (report["build"] or {}).items() if k != "ba"}), flush=True)

    # Visitor: the second sequence at frames the walkers never sent, no board drawn, no poses.
    vis = cl.post(f"/api/buildings/{bid}/visits", {"device": "7-Scenes replay"})
    vid = vis["visit_id"]
    dest = vis["pois"][-1]["id"] if vis.get("pois") else None
    vframes = walks[seqs[-1]][args.step // 2::args.step]
    errs, states, says, ms = [], {}, [], []
    for i, (f, Rcw, tcw) in enumerate(vframes):
        if i == 2 and dest is not None:
            cl.post(f"/api/visits/{vid}/destination", {"poi_id": dest})
        r = cl.post(f"/api/visits/{vid}/frame", image=jpg(cv2.imread(str(f))), form={"t_ms": str(i * 1000)})
        states[r["state"]] = states.get(r["state"], 0) + 1
        ms.append(r.get("ms", 0))
        if r.get("say"):
            says.append(r["say"])
        if r.get("fix") and r["fix"]["confidence"] >= 0.35:
            Rcb, tcb = to_building(Rcw, tcw, Rbw, centre, height)
            c = -Rcb.T @ tcb
            errs.append(float(np.hypot(r["fix"]["x"] - c[0], r["fix"]["z"] - c[2])))
    cl.post(f"/api/visits/{vid}/end", {"arrived": "arrived" in states, "notes": "system replay"})
    errs = np.array(errs)
    report["visit"] = {"visit_id": vid, "frames": len(vframes), "states": states,
                       "confident_fixes": int(len(errs)),
                       "floor_error_median_m": round(float(np.median(errs)), 3) if len(errs) else None,
                       "floor_error_p90_m": round(float(np.percentile(errs, 90)), 3) if len(errs) else None,
                       "server_ms_median": round(float(np.median(ms)), 1) if ms else None,
                       "spoken": says[:12]}
    report["building_id"] = bid
    print("visit", json.dumps(report["visit"]), flush=True)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"system_replay_{args.scene}.json").write_text(json.dumps(report, indent=1, default=float))


if __name__ == "__main__":
    main()
