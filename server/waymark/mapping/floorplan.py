"""Floor plan, walkable route graph and coverage from a merged map.

Walls come from 3D points between knee and head height, rasterised at 5 cm
and cleaned with morphology; long straight walls are fitted with HoughLinesP.
The walkable graph is built only from places a volunteer actually walked, so
every route Waymark gives has been walked by a sighted person.
"""
from __future__ import annotations

import heapq
import math
from dataclasses import dataclass, field

import cv2
import numpy as np

from .model import MapData

CELL = 0.05
WALL_MIN_Y, WALL_MAX_Y = 0.3, 2.0
NODE_SPACING = 0.5
LINK_RADIUS = 1.2
MARGIN = 1.0


@dataclass
class Plan:
    origin: tuple[float, float]           # world (x, z) of pixel (0, 0)
    shape: tuple[int, int]                # (rows, cols)
    walls: np.ndarray                     # uint8 mask, 255 = wall
    seen: np.ndarray                      # uint8 mask, 255 = seen by some keyframe
    walked: np.ndarray                    # uint8 mask, 255 = within 0.6 m of a walk
    segments: list[list[float]]           # wall segments [x1, z1, x2, z2] in metres
    nodes: np.ndarray                     # (N, 2) node positions (x, z)
    edges: list[tuple[int, int, float]] = field(default_factory=list)
    node_walker: list[str] = field(default_factory=list)

    def to_px(self, x, z):
        return (np.asarray(x) - self.origin[0]) / CELL, (np.asarray(z) - self.origin[1]) / CELL

    def to_world(self, c, r):
        return self.origin[0] + np.asarray(c) * CELL, self.origin[1] + np.asarray(r) * CELL

    def bounds(self) -> list[float]:
        x1, z1 = self.to_world(self.shape[1], self.shape[0])
        return [self.origin[0], self.origin[1], float(x1), float(z1)]

    def blocked(self, a, b) -> bool:
        """True if the straight line a->b crosses a wall cell."""
        (c0, r0), (c1, r1) = self.to_px(*a), self.to_px(*b)
        n = int(max(abs(c1 - c0), abs(r1 - r0))) + 1
        cs = np.linspace(c0, c1, n).astype(int)
        rs = np.linspace(r0, r1, n).astype(int)
        ok = (rs >= 0) & (rs < self.shape[0]) & (cs >= 0) & (cs < self.shape[1])
        return bool((self.walls[rs[ok], cs[ok]] > 0).any())


def build(data: MapData) -> Plan:
    traj = np.array([[kf.c[0], kf.c[2]] for kf in data.keyframes])
    # Wall evidence: points seen from at least three keyframes, within 5 m of
    # where someone walked, between knee and head height. Far, thinly observed
    # points are mostly triangulation noise and draw streaks, not walls.
    good = [p for p, o in data.obs.items() if len(o) >= 3 and p in data.xyz]
    pts = np.array([data.xyz[p] for p in good]) if good else np.zeros((0, 3))
    if len(pts) and len(traj):
        d = np.min(np.linalg.norm(pts[:, None, [0, 2]] - traj[None, :, :], axis=2), axis=1)
        pts = pts[d < 5.0]
    band = pts[(pts[:, 1] > WALL_MIN_Y) & (pts[:, 1] < WALL_MAX_Y)] if len(pts) else pts
    # Ignore far outliers so a handful of bad points cannot blow up the canvas.
    ref = np.vstack([traj, band[:, [0, 2]]]) if len(band) else traj
    lo = np.percentile(ref, 1, axis=0) - MARGIN
    hi = np.percentile(ref, 99, axis=0) + MARGIN
    lo = np.minimum(lo, traj.min(axis=0) - MARGIN)
    hi = np.maximum(hi, traj.max(axis=0) + MARGIN)
    cols, rows = int((hi[0] - lo[0]) / CELL) + 1, int((hi[1] - lo[1]) / CELL) + 1
    plan = Plan(origin=(float(lo[0]), float(lo[1])), shape=(rows, cols),
                walls=np.zeros((rows, cols), np.uint8), seen=np.zeros((rows, cols), np.uint8),
                walked=np.zeros((rows, cols), np.uint8), segments=[], nodes=np.zeros((0, 2)))

    # Walls: point density in the knee-to-head band.
    if len(band):
        c, r = plan.to_px(band[:, 0], band[:, 2])
        ok = (r >= 0) & (r < rows) & (c >= 0) & (c < cols)
        dens = np.zeros((rows, cols), np.float32)
        np.add.at(dens, (r[ok].astype(int), c[ok].astype(int)), 1.0)
        dens = cv2.GaussianBlur(dens, (0, 0), 1.0)
        thr = max(1.2, float(np.percentile(dens[dens > 0], 75))) if (dens > 0).any() else 1.0
        walls = (dens >= thr).astype(np.uint8) * 255
        walls = cv2.morphologyEx(walls, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
        walls = cv2.morphologyEx(walls, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
        plan.walls = walls

    # Walked area: a 0.6 m corridor around every walk. Walls never cover it.
    walked = np.zeros((rows, cols), np.uint8)
    by_walker: dict[str, list] = {}
    for kf in data.keyframes:
        by_walker.setdefault(kf.walker, []).append(kf)
    for kfs in by_walker.values():
        c, r = plan.to_px([k.c[0] for k in kfs], [k.c[2] for k in kfs])
        poly = np.stack([c, r], axis=1).astype(np.int32)
        cv2.polylines(walked, [poly], False, 255, int(1.2 / CELL))
    plan.walked = walked
    plan.walls[walked > 0] = 0

    # Seen area: rays from every keyframe across a 60-degree cone, stopped by walls.
    seen = np.zeros((rows, cols), np.uint8)
    for kf in data.keyframes:
        c0, r0 = plan.to_px(kf.c[0], kf.c[2])
        yaw = math.radians(kf.yaw)
        for da in np.linspace(-0.52, 0.52, 13):
            dx, dz = math.sin(yaw + da), math.cos(yaw + da)
            for s in np.arange(0, 5.0, CELL * 2):
                cc, rr = int(c0 + dx * s / CELL), int(r0 + dz * s / CELL)
                if not (0 <= rr < rows and 0 <= cc < cols) or plan.walls[rr, cc]:
                    break
                seen[rr, cc] = 255
    plan.seen = cv2.dilate(seen, np.ones((3, 3), np.uint8))

    lines = cv2.HoughLinesP(plan.walls, 1, np.pi / 180, threshold=25, minLineLength=int(0.5 / CELL),
                            maxLineGap=int(0.25 / CELL))
    if lines is not None:
        for c1, r1, c2, r2 in lines.reshape(-1, 4):
            x1, z1 = plan.to_world(c1, r1)
            x2, z2 = plan.to_world(c2, r2)
            plan.segments.append([round(float(x1), 3), round(float(z1), 3), round(float(x2), 3), round(float(z2), 3)])

    _build_graph(plan, by_walker)
    return plan


def _build_graph(plan: Plan, by_walker: dict) -> None:
    nodes, owner, edges = [], [], []
    for walker, kfs in by_walker.items():
        path = np.array([[k.c[0], k.c[2]] for k in kfs])
        start = len(nodes)
        last = None
        for p in path:
            if last is None or np.linalg.norm(p - last) >= NODE_SPACING:
                nodes.append(p)
                owner.append(walker)
                last = p
        for i in range(start, len(nodes) - 1):
            edges.append((i, i + 1, float(np.linalg.norm(nodes[i + 1] - nodes[i]))))
    nodes = np.array(nodes) if nodes else np.zeros((0, 2))
    # Link nearby nodes, within one walk or across walkers, when no wall is in the way.
    for i in range(len(nodes)):
        d = np.linalg.norm(nodes - nodes[i], axis=1)
        for j in np.where((d < LINK_RADIUS) & (np.arange(len(nodes)) > i + 1))[0]:
            if not plan.blocked(nodes[i], nodes[j]):
                edges.append((i, int(j), float(d[j])))
    plan.nodes, plan.edges, plan.node_walker = nodes, edges, owner


def nearest_node(plan: Plan, x: float, z: float) -> int:
    return int(np.argmin(np.linalg.norm(plan.nodes - np.array([x, z]), axis=1)))


def route(plan: Plan, a: int, b: int, turn_penalty_m: float = 0.0) -> list[int]:
    """Shortest path; a turn penalty prefers routes with fewer turns."""
    adj: dict[int, list] = {}
    for i, j, w in plan.edges:
        adj.setdefault(i, []).append((j, w))
        adj.setdefault(j, []).append((i, w))
    # State is (node, previous node) so turns can be priced.
    start = (a, -1)
    dist = {start: 0.0}
    prev: dict = {}
    pq = [(0.0, a, -1)]
    best_end = None
    while pq:
        d, u, p = heapq.heappop(pq)
        if d > dist.get((u, p), math.inf):
            continue
        if u == b:
            best_end = (u, p)
            break
        for v, w in adj.get(u, []):
            cost = w
            if turn_penalty_m and p >= 0:
                v1 = plan.nodes[u] - plan.nodes[p]
                v2 = plan.nodes[v] - plan.nodes[u]
                cosang = float(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2) + 1e-9))
                if cosang < math.cos(math.radians(35)):
                    cost += turn_penalty_m
            nd = d + cost
            if nd < dist.get((v, u), math.inf):
                dist[(v, u)] = nd
                prev[(v, u)] = (u, p)
                heapq.heappush(pq, (nd, v, u))
    if best_end is None:
        return []
    path, s = [], best_end
    while s in prev or s == start:
        path.append(s[0])
        if s == start:
            break
        s = prev[s]
    return path[::-1]


def coverage(plan: Plan) -> float:
    """Share of the seen floor that someone has walked."""
    seen = plan.seen > 0
    walked = plan.walked > 0
    total = (seen | walked).sum()
    return float(walked.sum() / total * 100) if total else 0.0


def frontiers(plan: Plan, min_area_m2: float = 0.6) -> list[dict]:
    """Seen-but-unwalked floor, more than a metre from any walk, grouped into regions."""
    far = cv2.dilate(plan.walked, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (int(1.0 / CELL),) * 2))
    cand = ((plan.seen > 0) & (far == 0) & (plan.walls == 0)).astype(np.uint8)
    n, lab, st, cen = cv2.connectedComponentsWithStats(cand, 8)
    out = []
    for i in range(1, n):
        area = st[i, cv2.CC_STAT_AREA] * CELL * CELL
        if area < min_area_m2:
            continue
        x, z = plan.to_world(*cen[i])
        out.append({"x": round(float(x), 2), "z": round(float(z), 2), "area_m2": round(float(area), 2)})
    return sorted(out, key=lambda f: -f["area_m2"])


def render(plan: Plan, data: MapData, path_nodes: list[int] | None = None,
           visitor: tuple[float, float, float] | None = None, scale: int = 4) -> bytes:
    """A clean PNG of the floor plan for the helper console and the report."""
    # The image covers plan.bounds() exactly with +z pointing up, so the
    # entrance board sits at the bottom, as a person standing at it sees the building.
    rows, cols = plan.shape
    img = np.full((rows, cols, 3), 250, np.uint8)
    img[plan.seen > 0] = (238, 238, 232)
    img[plan.walked > 0] = (226, 240, 228)
    img = np.ascontiguousarray(np.flipud(img))
    img = cv2.resize(img, (cols * scale, rows * scale), interpolation=cv2.INTER_NEAREST)
    s = scale / CELL

    def P(x, z):
        c, r = plan.to_px(x, z)
        return int(c * scale), int((rows - 1 - r) * scale)

    wall_mask = cv2.resize(np.ascontiguousarray(np.flipud(plan.walls)), (cols * scale, rows * scale),
                           interpolation=cv2.INTER_NEAREST)
    img[wall_mask > 0] = (150, 150, 150)
    for x1, z1, x2, z2 in plan.segments:
        cv2.line(img, P(x1, z1), P(x2, z2), (40, 40, 40), max(2, scale // 2), cv2.LINE_AA)
    palette = [(200, 90, 30), (40, 120, 220), (60, 160, 60), (160, 60, 160)]
    walkers = sorted({kf.walker for kf in data.keyframes})
    for wi, w in enumerate(walkers):
        pts = np.array([P(k.c[0], k.c[2]) for k in data.keyframes if k.walker == w], np.int32)
        cv2.polylines(img, [pts], False, palette[wi % 4], 2, cv2.LINE_AA)
    if path_nodes:
        pts = np.array([P(*plan.nodes[i]) for i in path_nodes], np.int32)
        cv2.polylines(img, [pts], False, (20, 20, 200), max(3, scale), cv2.LINE_AA)
    for poi in data.pois:
        cv2.circle(img, P(poi.x, poi.z), max(5, scale + 2), (0, 110, 230), -1, cv2.LINE_AA)
        cv2.putText(img, poi.label[:24], (P(poi.x, poi.z)[0] + 8, P(poi.x, poi.z)[1] - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (20, 20, 20), 1, cv2.LINE_AA)
    # Entrance board at the origin.
    cv2.rectangle(img, P(-0.13, 0.05), P(0.13, -0.05), (0, 0, 0), -1)
    if visitor is not None:
        x, z, yaw = visitor
        p = P(x, z)
        tip = (int(p[0] + math.sin(math.radians(yaw)) * 0.7 * s), int(p[1] - math.cos(math.radians(yaw)) * 0.7 * s))
        cv2.arrowedLine(img, p, tip, (10, 10, 180), 3, cv2.LINE_AA, tipLength=0.35)
        cv2.circle(img, p, max(6, scale + 3), (10, 10, 180), -1, cv2.LINE_AA)
    # 1 m scale bar.
    cv2.line(img, (12, img.shape[0] - 14), (12 + int(s), img.shape[0] - 14), (0, 0, 0), 3)
    cv2.putText(img, "1 m", (16 + int(s), img.shape[0] - 9), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
    ok, buf = cv2.imencode(".png", img)
    return buf.tobytes()


def to_json(plan: Plan, data: MapData, stats: dict | None = None) -> dict:
    trajs: dict[str, list] = {}
    for kf in data.keyframes:
        trajs.setdefault(kf.walker, []).append([round(float(kf.c[0]), 3), round(float(kf.c[2]), 3)])
    return {
        "bounds": [round(v, 3) for v in plan.bounds()],
        "walls": plan.segments,
        "nodes": [{"id": i, "x": round(float(n[0]), 3), "z": round(float(n[1]), 3)} for i, n in enumerate(plan.nodes)],
        "edges": [[i, j] for i, j, _ in plan.edges],
        "pois": [{"id": p.id, "label": p.label, "kind": p.kind, "x": round(p.x, 3), "z": round(p.z, 3),
                  "walker": p.walker} for p in data.pois],
        "trajectories": trajs,
        "coverage_pct": round(coverage(plan), 1),
        "frontiers": frontiers(plan),
        "stats": stats or {},
    }


def yaw_to(a: np.ndarray, b: np.ndarray) -> float:
    d = b - a
    return math.degrees(math.atan2(d[0], d[1]))

