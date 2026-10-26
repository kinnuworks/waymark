"""Buildings, walkers and visits: the state behind the HTTP API."""
from __future__ import annotations

import json
import math
import pickle
import threading
import time
import uuid
from dataclasses import dataclass, field

import cv2
import numpy as np
from scipy.optimize import linear_sum_assignment

from . import config, store
from .agent.guide import GuideAgent, Visit, turn_words, wrap
from .localize.localizer import FOCAL_CANDIDATES, Localizer
from .mapping import floorplan, merge
from .mapping.model import MapData
from .mapping.tracker import WalkerTracker
from .vision import camera
from .vision.board import BoardSpec, Calibrator
from .vision.features import extractor

COLOURS = ["#C2410C", "#1D4ED8", "#15803D", "#7E22CE"]

VOLUNTEER_SAY = {
    "need_board": "Point the camera at the entrance board.",
    "bootstrap": "Keep the board in view and take two slow steps sideways.",
    "board_start": "Board found. Walk slowly into the building, holding the phone at chest height.",
    "blurry": "Too blurry. Walk a little slower.",
    "too_dark": "Too dark here. Point the camera at something lit.",
    "too_bright": "Facing a bright light. Turn the camera slightly away.",
    "lost": "I've lost track. Walk back to the last doorway you passed and point the camera at something with detail, like a door or a noticeboard.",
}


@dataclass
class Walker:
    id: str
    name: str
    colour: str
    tracker: WalkerTracker
    calib: Calibrator
    finished: bool = False
    last_say: str = ""
    last_say_t: float = 0.0
    last_assign_t: float = 0.0


@dataclass
class Building:
    id: str
    name: str
    spec: BoardSpec
    created: float = field(default_factory=time.time)
    walkers: dict = field(default_factory=dict)
    visits: dict = field(default_factory=dict)
    merged: MapData | None = None
    plan: object = None
    build_stats: dict = field(default_factory=dict)
    _preview: tuple = (0.0, None, None)

    def __post_init__(self):
        self.lock = threading.RLock()
        self.localizer: Localizer | None = None
        self.guide: GuideAgent | None = None

    def __getstate__(self):
        s = dict(self.__dict__)
        for k in ("lock", "localizer", "guide", "_preview"):
            s.pop(k, None)
        return s

    def __setstate__(self, s):
        self.__dict__.update(s)
        self.lock = threading.RLock()
        self._preview = (0.0, None, None)
        self.localizer, self.guide = None, None
        if self.merged is not None and self.plan is not None:
            self._activate()

    # ---- persistence --------------------------------------------------------------------
    @property
    def dir(self):
        return config.DATA / "buildings" / self.id

    def save(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / "state.pkl.tmp"
        with self.lock:
            tmp.write_bytes(pickle.dumps(self))
        tmp.replace(self.dir / "state.pkl")

    def summary(self) -> dict:
        n_pois = sum(len(w.tracker.data.pois) for w in self.walkers.values())
        return {"id": self.id, "name": self.name, "created": self.created,
                "status": "ready" if self.merged is not None else ("mapping" if self.walkers else "new"),
                "n_walkers": len(self.walkers), "n_pois": n_pois, "has_map": self.merged is not None,
                "board_height_m": self.spec.height_m, "square_mm": round(self.spec.square_m * 1000, 1)}

    # ---- mapping ------------------------------------------------------------------------
    def add_walker(self, name: str) -> Walker:
        with self.lock:
            wid = f"w{len(self.walkers) + 1}"
            w = Walker(wid, name or f"Walker {len(self.walkers) + 1}", COLOURS[len(self.walkers) % 4],
                       WalkerTracker(wid, self.spec), Calibrator(self.spec))
            self.walkers[wid] = w
        self.save()
        return w

    def calib_frame(self, wid: str, img: np.ndarray) -> dict:
        w = self.walkers[wid]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        out = w.calib.add(gray)
        if out["done"]:
            w.tracker.calib = w.calib.result
        return out

    def walker_frame(self, wid: str, img: np.ndarray, t_ms: int, heading) -> dict:
        w = self.walkers[wid]
        with self.lock:
            res = w.tracker.process(img, t_ms, heading)
        now = time.time()
        say = VOLUNTEER_SAY.get(res["code"], "")
        if res["state"] == "tracking" and res["code"] in ("track", "board"):
            say = ""
            if now - w.last_assign_t > 15:
                a = self.coordinator().get("assignments", {}).get(wid)
                if a:
                    say = a["say"]
                    w.last_assign_t = now
        speak = say and (say != w.last_say or now - w.last_say_t > 8)
        if speak:
            w.last_say, w.last_say_t = say, now
        res["say"] = say if speak else ""
        store.trace(f"{self.id}-{wid}", {"t": now, "state": res["state"], "code": res["code"],
                                          "decided": res["say"] or "(silent)", "pose": res["pose"],
                                          "quality": res["quality"], "keyframes": res["keyframes"]})
        if w.tracker.frames % 20 == 0:
            self.save()
        return res

    def mark(self, wid: str, label: str, kind: str):
        with self.lock:
            poi = self.walkers[wid].tracker.mark(label, kind)
        self.save()
        return poi

    def finish(self, wid: str) -> dict:
        self.walkers[wid].finished = True
        self.save()
        return {"ok": True}

    def _parts(self) -> list[MapData]:
        return [w.tracker.data for w in self.walkers.values() if w.tracker.data.keyframes]

    def preview(self):
        """Merged-but-unadjusted map for live views while walkers are still out."""
        t, data, plan = self._preview
        if time.time() - t < 3 and data is not None:
            return data, plan
        parts = self._parts()
        if not parts:
            return None, None
        with self.lock:
            data = merge.combine(parts)
        plan = floorplan.build(data)
        self._preview = (time.time(), data, plan)
        return data, plan

    def coordinator(self) -> dict:
        data, plan = self.preview()
        if data is None:
            return {"coverage_pct": 0, "frontiers": [], "assignments": {}}
        fr = floorplan.frontiers(plan)
        active = [w for w in self.walkers.values() if not w.finished and w.tracker.last_pose is not None]
        assignments = {}
        if fr and active:
            cost = np.zeros((len(active), len(fr)))
            for i, w in enumerate(active):
                c = w.tracker.last_pose
                pos = -c[0].T @ c[1]
                for j, f in enumerate(fr):
                    cost[i, j] = math.hypot(f["x"] - pos[0], f["z"] - pos[2]) - 0.2 * f["area_m2"]
            rows, cols = linear_sum_assignment(cost)
            for i, j in zip(rows, cols):
                w, f = active[i], fr[j]
                R, t = w.tracker.last_pose
                pos = -R.T @ t
                fwd = R.T @ np.array([0, 0, 1.0])
                yaw = math.degrees(math.atan2(fwd[0], fwd[2]))
                rel = wrap(math.degrees(math.atan2(f["x"] - pos[0], f["z"] - pos[2])) - yaw)
                d = math.hypot(f["x"] - pos[0], f["z"] - pos[2])
                direction = turn_words(rel).replace("turn ", "to your ")
                assignments[w.id] = {"target": {"x": f["x"], "z": f["z"]},
                                     "say": f"Next, head about {max(1, round(d))} metres {direction}. Nobody has walked that area yet."}
        for w in active:
            if w.id not in assignments:
                assignments[w.id] = {"target": None, "say": "Your area is covered. Walk back to the entrance board to finish."}
        return {"coverage_pct": round(floorplan.coverage(plan), 1), "frontiers": fr, "assignments": assignments}

    def build(self) -> dict:
        parts = self._parts()
        if not parts:
            return {"ok": False, "reason": "no walks yet"}
        t0 = time.time()
        with self.lock:
            data = merge.combine(parts)
            stats = merge.merge(data)
            plan = floorplan.build(data)
            self.merged, self.plan = data, plan
            stats["seconds"] = round(time.time() - t0, 1)
            stats["keyframes"] = len(data.keyframes)
            stats["points"] = len(data.xyz)
            stats["coverage_pct"] = round(floorplan.coverage(plan), 1)
            stats["route_nodes"] = int(len(plan.nodes))
            self.build_stats = stats
            self._activate()
        self.save()
        store.trace(f"{self.id}-build", {"t": time.time(), "state": "built", "decided": "map built",
                                         "stats": json.loads(json.dumps(stats, default=float))})
        return {"ok": True, "stats": stats}

    def _activate(self):
        self.localizer = Localizer(self.merged)
        self.guide = GuideAgent(self.merged, self.plan)

    def map_json(self) -> dict:
        if self.merged is not None:
            return floorplan.to_json(self.plan, self.merged, self.build_stats)
        data, plan = self.preview()
        if data is None:
            return {"bounds": [0, 0, 1, 1], "walls": [], "nodes": [], "edges": [], "pois": [],
                    "trajectories": {}, "coverage_pct": 0, "frontiers": [], "stats": {}}
        out = floorplan.to_json(plan, data, {"preview": True})
        out["walkers"] = {w.id: {"name": w.name, "colour": w.colour,
                                 "pose": None if w.tracker.last_pose is None else
                                 [round(float(x), 3) for x in (-w.tracker.last_pose[0].T @ w.tracker.last_pose[1])]}
                          for w in self.walkers.values()}
        return out

    def floorplan_png(self, visit: Visit | None = None) -> bytes:
        data, plan = (self.merged, self.plan) if self.merged is not None else self.preview()
        if data is None:
            ok, buf = cv2.imencode(".png", np.full((200, 300, 3), 250, np.uint8))
            return buf.tobytes()
        vis, path = None, None
        if visit is not None and visit.last_fix is not None:
            vis = (float(visit.last_fix.c[0]), float(visit.last_fix.c[2]), visit.last_fix.yaw)
            path = visit.route
        return floorplan.render(plan, data, path, vis)

    # ---- visits ------------------------------------------------------------------------------
    def new_visit(self, device: str) -> Visit:
        if self.guide is None:
            raise ValueError("This building has no finished map yet.")
        v = Visit(id=uuid.uuid4().hex[:10], building=self.id)
        self.visits[v.id] = v
        return v

    def visit_frame(self, v: Visit, img: np.ndarray, t_ms: int, heading) -> dict:
        t0 = time.perf_counter()
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        q = camera.quality(gray)
        feats = extractor().extract(img) if q["ok"] or q["reason"] == "blurry" else None
        fix = None
        if feats is not None and len(feats) > 30:
            now = time.time()
            dt = now - v.last_fix_t if v.last_fix_t else None
            if v.focal_ratio is None:
                f, fix = self.localizer.calibrate_focal(feats)
                if fix is not None and fix.confidence > 0.5:
                    v.focal_votes.append(f)
                    if len(v.focal_votes) >= 3:
                        v.focal_ratio = max(set(v.focal_votes), key=v.focal_votes.count)
            else:
                fix = self.localizer.localize(feats, v.focal_ratio, v.last_fix, dt)
        out = self.guide.step(v, q, fix)
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["focal_ratio"] = v.focal_ratio
        rec = dict(v.trace[-1])
        rec["ms"] = out["ms"]
        rec["heading_deg"] = heading
        store.trace(f"visit-{v.id}", rec)
        return out


class Registry:
    def __init__(self):
        self.buildings: dict[str, Building] = {}
        self.lock = threading.Lock()
        root = config.DATA / "buildings"
        if root.exists():
            for d in sorted(root.iterdir()):
                f = d / "state.pkl"
                if f.exists():
                    try:
                        b = pickle.loads(f.read_bytes())
                        self.buildings[b.id] = b
                    except Exception as e:
                        print(f"registry: could not load {d.name}: {e}", flush=True)

    def create(self, name: str, height_m: float, square_mm: float = 35.0) -> Building:
        bid = uuid.uuid4().hex[:8]
        b = Building(bid, name, BoardSpec(square_m=square_mm / 1000.0, height_m=height_m))
        with self.lock:
            self.buildings[bid] = b
        b.save()
        return b

    def visit(self, vid: str) -> tuple[Building, Visit]:
        for b in self.buildings.values():
            if vid in b.visits:
                return b, b.visits[vid]
        raise KeyError(vid)


__all__ = ["Registry", "Building", "Walker", "FOCAL_CANDIDATES"]
