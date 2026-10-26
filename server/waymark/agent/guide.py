"""The guidance agent: what to say and do next, given what the camera saw.

Each phone frame produces an observation (image quality, localisation fix and
confidence, position on the route). The agent picks one action and records
what it saw, what it decided and why. Safety rules live here in code: the
agent never gives a direction it cannot back with a confident fix, it stops
before stairs unless the fix is strong, and after repeated failures it hands
the visitor to a person instead of guessing.

Spoken instructions follow the spirit of the Wayfindr open standard for audio
navigation: short, one action at a time, distances in metres, landmarks named
when available, and a clear arrival message that says on which side the
destination is.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field

import numpy as np

from ..mapping import floorplan
from ..mapping.model import MapData

ARRIVE_M = 1.2
OFF_ROUTE_M = 1.6
STAIRS_WARN_M = 3.0
LOOKAHEAD_M = 1.5
REPEAT_S = 7.0


def turn_words(deg: float) -> str:
    a = abs(deg)
    side = "right" if deg > 0 else "left"
    if a < 20:
        return "straight ahead"
    if a < 50:
        return f"slightly {side}"
    if a < 135:
        return f"turn {side}"
    return "turn around"


def wrap(deg: float) -> float:
    return (deg + 180) % 360 - 180


@dataclass
class Visit:
    id: str
    building: str
    destination: int | None = None       # poi id
    route: list[int] = field(default_factory=list)
    turn_penalty: float = 0.0
    reroutes: int = 0
    low_streak: int = 0
    stale_streak: int = 0
    last_fix: object = None
    last_fix_t: float | None = None
    last_say: str = ""
    last_say_t: float = 0.0
    state: str = "locating"
    focal_ratio: float | None = None
    focal_votes: list = field(default_factory=list)
    stairs_ack: bool = False
    help_requested: bool = False
    helper_text: list = field(default_factory=list)
    arrived: bool = False
    trace: list = field(default_factory=list)
    started: float = field(default_factory=time.time)


class GuideAgent:
    def __init__(self, data: MapData, plan: floorplan.Plan):
        self.data = data
        self.plan = plan
        self.pois = {p.id: p for p in data.pois}

    # ---- routing -------------------------------------------------------------------
    def set_destination(self, v: Visit, poi_id: int, from_xz=None) -> float:
        v.destination = poi_id
        v.arrived = False
        v.stairs_ack = False
        if from_xz is not None:
            self._plan_route(v, from_xz)
        return self._route_len(v)

    def _plan_route(self, v: Visit, xz) -> None:
        poi = self.pois[v.destination]
        a = floorplan.nearest_node(self.plan, *xz)
        b = floorplan.nearest_node(self.plan, poi.x, poi.z)
        v.route = floorplan.route(self.plan, a, b, v.turn_penalty)

    def _route_len(self, v: Visit) -> float:
        if len(v.route) < 2:
            return 0.0
        pts = self.plan.nodes[v.route]
        return float(np.sum(np.linalg.norm(np.diff(pts, axis=0), axis=1)))

    # ---- one step ------------------------------------------------------------------
    def step(self, v: Visit, quality: dict, fix, now: float | None = None) -> dict:
        now = now or time.time()
        obs = {"quality": quality, "fix": fix.summary() if fix is not None else None}
        state, say, haptic, why, route_info = self._decide(v, quality, fix, now)
        helper = ""
        if v.helper_text:
            helper = v.helper_text.pop(0)
            say = helper + (" " + say if say else "")
            why += "; relayed helper message"
        speak = say and (say != v.last_say or now - v.last_say_t > REPEAT_S)
        v.state = state
        if fix is not None and fix.confidence >= 0.35:
            v.last_fix, v.last_fix_t = fix, now
        rec = {"t": round(now, 3), "state": state, "saw": obs, "decided": say or "(silent)",
               "haptic": haptic, "why": why}
        v.trace.append(rec)
        if speak:
            v.last_say, v.last_say_t = say, now
        return {"state": state, "fix": obs["fix"], "say": say if speak else "", "haptic": haptic,
                "route": route_info, "why": why, "helper_text": helper}

    def _decide(self, v: Visit, q: dict, fix, now: float):
        conf = fix.confidence if fix is not None else 0.0
        if v.help_requested and v.state == "helper":
            return "helper", "", "none", "waiting for the human helper", None
        if not q.get("ok", True) and fix is None:
            v.low_streak += 1
            msg = {"too_dark": "It's too dark for the camera. Point it towards a lit area.",
                   "too_bright": "The camera is facing a bright light. Turn it slightly away.",
                   "blurry": "Hold the phone a little steadier."}.get(q.get("reason"), "Hold the phone steady.")
            return self._escalate(v, "low_confidence", msg, f"image unusable ({q.get('reason')})")
        if fix is None or conf < 0.35:
            v.low_streak += 1
            if fix is None:
                v.stale_streak += 1 if q.get("ok") else 0
            if v.low_streak <= 2:
                return "low_confidence", "Slowly pan the phone left and right.", "none", \
                    f"no confident fix (confidence {conf:.2f}); asking for a slow pan to give the camera more to match", None
            return self._escalate(v, "low_confidence", "Please stop walking for a moment while I find where you are.",
                                  f"{v.low_streak} frames without a confident fix")
        # Confident fix from here on.
        v.low_streak = 0
        v.stale_streak = 0
        if v.state in ("lost", "helper") and not v.help_requested:
            pass
        xz = np.array([fix.c[0], fix.c[2]])
        yaw = fix.yaw
        if v.destination is None:
            near = self._nearest_poi(xz)
            msg = f"Located. You are near {near.label}." if near else "Located."
            return "guiding", msg, "none", "fix confident; no destination chosen yet", None
        poi = self.pois[v.destination]
        dist_goal = float(np.linalg.norm(xz - np.array([poi.x, poi.z])))
        if v.arrived and dist_goal < ARRIVE_M * 1.6:
            return "arrived", "", "none", f"already arrived; {dist_goal:.1f} m from the destination, staying quiet", \
                {"remaining_m": round(dist_goal, 1), "next_turn_deg": 0, "next_label": poi.label}
        if dist_goal < ARRIVE_M:
            v.arrived = True
            side = turn_words(wrap(floorplan.yaw_to(xz, np.array([poi.x, poi.z])) - yaw))
            where = "ahead of you" if side == "straight ahead" else ("on your " + side.split()[-1]) if "turn" in side or "slightly" in side else side
            return "arrived", f"You have arrived. {poi.label} is {where}.", "arrived", \
                f"within {dist_goal:.1f} m of the destination", {"remaining_m": round(dist_goal, 1), "next_turn_deg": 0, "next_label": poi.label}
        if not v.route:
            self._plan_route(v, xz)
        pts = self.plan.nodes[v.route] if v.route else np.zeros((0, 2))
        if len(pts) == 0:
            return self._escalate(v, "lost", "I can't find a walked route from here.", "no route in the graph")
        # Distance from the route polyline.
        seg_d, seg_i, proj = self._project(pts, xz)
        if seg_d > OFF_ROUTE_M:
            v.reroutes += 1
            if v.reroutes >= 3:
                v.turn_penalty = 4.0
            self._plan_route(v, xz)
            pts = self.plan.nodes[v.route]
            seg_d, seg_i, proj = self._project(pts, xz)
            tag = " with fewer turns" if v.turn_penalty else ""
            say = f"You've left the route. Finding a new one{tag}."
            return "off_route", say, "stop", f"{seg_d:.1f} m from the route; rerouted (#{v.reroutes})", None
        # Stairs ahead?
        stairs = [p for p in self.data.pois if p.kind == "stairs"]
        for s in stairs:
            ds = float(np.linalg.norm(xz - np.array([s.x, s.z])))
            ahead = abs(wrap(floorplan.yaw_to(xz, np.array([s.x, s.z])) - yaw)) < 60
            if ds < STAIRS_WARN_M and ahead and not v.stairs_ack:
                if conf < 0.8:
                    return "stairs_caution", f"Stairs ahead, about {ds:.0f} metres. Stop and find the handrail with your cane.", \
                        "stop", f"stairs {ds:.1f} m ahead and confidence {conf:.2f} below 0.8: pausing guidance", None
                v.stairs_ack = True
                return "guiding", f"Stairs ahead in about {ds:.0f} metres. The handrail is the safest guide.", "stop", \
                    f"stairs {ds:.1f} m ahead, confident fix", None
        # Next instruction: heading to a lookahead point, and the next real turn.
        target = self._lookahead(pts, seg_i, proj)
        heading_err = wrap(floorplan.yaw_to(xz, target) - yaw)
        remaining = self._remaining(pts, seg_i, proj)
        next_turn_d, next_turn_deg = self._next_turn(pts, seg_i, proj)
        if abs(heading_err) > 25:
            say = turn_words(heading_err).capitalize() + "."
            haptic = "right" if heading_err > 0 else "left"
            why = f"facing {heading_err:+.0f} degrees off the route"
        elif next_turn_d is not None and next_turn_d < 8:
            say = f"Walk straight for {max(1, round(next_turn_d))} metres, then {turn_words(next_turn_deg)}."
            haptic = "straight"
            why = f"on route; next turn {next_turn_deg:+.0f} degrees in {next_turn_d:.1f} m"
        else:
            say = f"Walk straight. {poi.label} is {round(remaining)} metres away."
            haptic = "straight"
            why = f"on route; {remaining:.1f} m to go"
        if conf < 0.6:
            say = "Slowly. " + say
            why += f"; confidence {conf:.2f}, asking for a slower pace"
        return "guiding", say, haptic, why, {"remaining_m": round(remaining, 1),
                                             "next_turn_deg": round(heading_err, 1), "next_label": poi.label}

    def _escalate(self, v: Visit, state: str, msg: str, why: str):
        if v.low_streak >= 6:
            v.help_requested = True
            return "helper", "I can't place you reliably. Please stay where you are. I'm calling your helper.", \
                "stop", why + "; six frames without a fix, handing over to a person", None
        if v.stale_streak >= 4:
            why += "; the images are clear but don't match the map here, so this area may have changed and is flagged for re-mapping"
        return state, msg, "stop" if v.low_streak >= 3 else "none", why, None

    # ---- geometry on the route ---------------------------------------------------------
    @staticmethod
    def _project(pts, xz):
        best = (math.inf, 0, pts[0])
        for i in range(len(pts) - 1):
            a, b = pts[i], pts[i + 1]
            ab = b - a
            t = float(np.clip(np.dot(xz - a, ab) / (np.dot(ab, ab) + 1e-9), 0, 1))
            p = a + t * ab
            d = float(np.linalg.norm(xz - p))
            if d < best[0]:
                best = (d, i, p)
        if len(pts) == 1:
            best = (float(np.linalg.norm(xz - pts[0])), 0, pts[0])
        return best

    @staticmethod
    def _lookahead(pts, i, p):
        left = LOOKAHEAD_M
        cur = p
        for j in range(i + 1, len(pts)):
            d = float(np.linalg.norm(pts[j] - cur))
            if d >= left:
                return cur + (pts[j] - cur) * (left / d)
            left -= d
            cur = pts[j]
        return pts[-1]

    @staticmethod
    def _remaining(pts, i, p):
        if i + 1 >= len(pts):
            return 0.0
        return float(np.linalg.norm(pts[i + 1] - p) + np.sum(np.linalg.norm(np.diff(pts[i + 1:], axis=0), axis=1)))

    @staticmethod
    def _next_turn(pts, i, p):
        dist = float(np.linalg.norm(pts[i + 1] - p)) if i + 1 < len(pts) else 0.0
        for j in range(i + 1, len(pts) - 1):
            v1, v2 = pts[j] - pts[j - 1], pts[j + 1] - pts[j]
            if np.linalg.norm(v1) < 1e-6 or np.linalg.norm(v2) < 1e-6:
                continue
            ang = wrap(math.degrees(math.atan2(v2[0], v2[1]) - math.atan2(v1[0], v1[1])))
            if abs(ang) > 35:
                return dist, ang
            dist += float(np.linalg.norm(v2))
        return None, 0.0

    def _nearest_poi(self, xz):
        if not self.data.pois:
            return None
        return min(self.data.pois, key=lambda p: np.linalg.norm(xz - np.array([p.x, p.z])))

    # ---- questions -----------------------------------------------------------------------
    def describe(self, v: Visit) -> str:
        fix = v.last_fix
        if fix is None:
            return "I don't know where you are yet. Hold the phone up and slowly pan it."
        xz = np.array([fix.c[0], fix.c[2]])
        near = sorted(self.data.pois, key=lambda p: np.linalg.norm(xz - np.array([p.x, p.z])))[:2]
        if not near:
            return "You're inside the mapped area, but no places have been named yet."
        parts = []
        for p in near:
            d = float(np.linalg.norm(xz - np.array([p.x, p.z])))
            rel = turn_words(wrap(floorplan.yaw_to(xz, np.array([p.x, p.z])) - fix.yaw))
            rel = rel.replace("turn ", "to your ").replace("slightly ", "ahead and slightly to the ")
            parts.append(f"{p.label} is about {max(1, round(d))} metres {rel}")
        return ". ".join(parts) + "."

    def match_destination(self, text: str):
        t = text.lower()
        best, score = None, 0
        for p in self.data.pois:
            words = [w for w in p.label.lower().split() if len(w) > 1] + [p.kind]
            s = sum(1 for w in words if w in t)
            if s > score:
                best, score = p, s
        return best
