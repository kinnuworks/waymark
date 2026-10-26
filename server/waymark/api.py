"""HTTP API and static site. See docs/API.md."""
from __future__ import annotations

import json
import os
import platform
import re
import time

import cv2
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, store
from .service import Registry
from .vision import camera
from .vision.board import BoardSpec, render_png
from .vision.features import extractor, strong_matcher

app = FastAPI(title="Waymark", docs_url="/api/docs", openapi_url="/api/openapi.json")
reg = Registry()
POI_KINDS = {"room", "door", "stairs", "lift", "restroom", "exit", "desk", "other"}


def _building(bid: str):
    b = reg.buildings.get(bid)
    if b is None:
        raise HTTPException(404, "No such building")
    return b


async def _image(image: UploadFile):
    data = await image.read()
    if len(data) > 8_000_000:
        raise HTTPException(413, "Image too large")
    img = camera.decode(data)
    if img is None:
        raise HTTPException(400, "Could not read the image")
    return img, data


class NewBuilding(BaseModel):
    name: str
    board_height_m: float = 1.20
    square_mm: float = 35.0


class Named(BaseModel):
    name: str = ""


class PoiIn(BaseModel):
    label: str
    kind: str = "room"


class Dest(BaseModel):
    poi_id: int


class Text(BaseModel):
    text: str


class Checkpoint(BaseModel):
    checkpoint_id: str


class EndVisit(BaseModel):
    arrived: bool = False
    notes: str = ""


# ---- system -----------------------------------------------------------------------------
@app.get("/api/health")
def health():
    info = cv2.getBuildInformation()
    return {"ok": True, "opencv": cv2.__version__, "arch": platform.machine(), "instance": config.INSTANCE,
            "threads": cv2.getNumThreads(),
            "build": {"kleidicv": "kleidicv" in info.lower(), "cool": bool(os.environ.get("WAYMARK_COOL")),
                      "dnn_engine": "ENGINE_NEW"},
            "features": {"extractor": extractor().kind, "matcher": strong_matcher().kind},
            "buildings": len(reg.buildings)}


@app.get("/api/eval")
def eval_results():
    f = config.EVAL_OUT / "results.json"
    return json.loads(f.read_text()) if f.exists() else {"sections": []}


@app.get("/api/trace/{tid}")
def trace(tid: str, tail: int | None = None):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", tid):
        raise HTTPException(400, "Bad id")
    return store.read_trace(tid, tail)


@app.get("/api/board.png")
def board_png(square_mm: float = 35.0, height_m: float = 1.2):
    return Response(render_png(BoardSpec(square_mm / 1000, height_m)), media_type="image/png")


# ---- buildings and mapping -------------------------------------------------------------------
@app.get("/api/buildings")
def list_buildings():
    return [b.summary() for b in sorted(reg.buildings.values(), key=lambda b: -b.created)]


@app.post("/api/buildings")
def create_building(body: NewBuilding):
    if not body.name.strip():
        raise HTTPException(400, "Name required")
    if not 0.3 <= body.board_height_m <= 2.5:
        raise HTTPException(400, "Board height must be between 0.3 and 2.5 m")
    b = reg.create(body.name.strip()[:80], body.board_height_m, body.square_mm)
    return {"id": b.id}


@app.get("/api/buildings/{bid}")
def get_building(bid: str):
    b = _building(bid)
    out = b.summary()
    out["walkers"] = [{"id": w.id, "name": w.name, "colour": w.colour, "finished": w.finished,
                       "keyframes": len(w.tracker.data.keyframes)} for w in b.walkers.values()]
    out["build_stats"] = b.build_stats
    return out


@app.post("/api/buildings/{bid}/walkers")
def add_walker(bid: str, body: Named):
    w = _building(bid).add_walker(body.name.strip()[:40])
    return {"walker_id": w.id, "colour": w.colour}


@app.post("/api/buildings/{bid}/walkers/{wid}/calib")
async def calib(bid: str, wid: str, image: UploadFile = File(...)):
    b = _building(bid)
    if wid not in b.walkers:
        raise HTTPException(404, "No such walker")
    img, _ = await _image(image)
    return b.calib_frame(wid, img)


@app.post("/api/buildings/{bid}/walkers/{wid}/frame")
async def walker_frame(bid: str, wid: str, image: UploadFile = File(...), heading_deg: float | None = Form(None),
                       t_ms: int = Form(0)):
    b = _building(bid)
    if wid not in b.walkers:
        raise HTTPException(404, "No such walker")
    img, raw = await _image(image)
    n = b.walkers[wid].tracker.frames
    store.archive(f"walks/{bid}/{wid}/{n:05d}_{t_ms}.jpg", raw)
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(b.walker_frame, wid, img, t_ms, heading_deg)


@app.post("/api/buildings/{bid}/walkers/{wid}/poi")
def add_poi(bid: str, wid: str, body: PoiIn):
    b = _building(bid)
    kind = body.kind if body.kind in POI_KINDS else "other"
    poi = b.mark(wid, body.label.strip()[:60] or kind.capitalize(), kind)
    if poi is None:
        raise HTTPException(409, "Walk a few steps from the board first so I know where you are.")
    return {"poi_id": poi.id, "x": round(poi.x, 3), "z": round(poi.z, 3)}


@app.post("/api/buildings/{bid}/walkers/{wid}/finish")
def finish(bid: str, wid: str):
    return _building(bid).finish(wid)


class CheckpointsIn(BaseModel):
    checkpoints: dict[str, list[float]]  # id -> [x, z] in metres, tape-measured from the board


@app.get("/api/buildings/{bid}/checkpoints")
def get_checkpoints(bid: str):
    f = _building(bid).dir / "checkpoints.json"
    return json.loads(f.read_text()) if f.exists() else {"checkpoints": {}}


@app.post("/api/buildings/{bid}/checkpoints")
def set_checkpoints(bid: str, body: CheckpointsIn):
    b = _building(bid)
    b.dir.mkdir(parents=True, exist_ok=True)
    (b.dir / "checkpoints.json").write_text(json.dumps({"checkpoints": body.checkpoints}, indent=1))
    return {"ok": True, "n": len(body.checkpoints)}


@app.get("/api/buildings/{bid}/report")
def field_report(bid: str):
    from .fieldreport import report
    return report(_building(bid))


@app.get("/api/buildings/{bid}/coordinator")
def coordinator(bid: str):
    return _building(bid).coordinator()


_builds: dict[str, dict] = {}


@app.post("/api/buildings/{bid}/build")
def build(bid: str, wait: bool = False):
    """Start a map build in the background (a build takes longer than API Gateway's 30 s limit).

    Poll GET /api/buildings/{bid}/build for the result. `?wait=1` builds inline (tests, scripts).
    """
    b = _building(bid)
    if wait:
        return b.build()
    job = _builds.get(bid)
    if job and job["state"] == "building":
        return {"ok": True, "state": "building", "started": job["started"]}
    job = {"state": "building", "started": time.time(), "result": None}
    _builds[bid] = job

    def run():
        try:
            job["result"] = b.build()
            job["state"] = "done" if job["result"].get("ok") else "error"
        except Exception as e:  # report, don't crash the worker
            job["state"], job["result"] = "error", {"ok": False, "reason": str(e)}
        job["finished"] = time.time()

    import threading
    threading.Thread(target=run, daemon=True).start()
    return {"ok": True, "state": "building", "started": job["started"]}


@app.get("/api/buildings/{bid}/build")
def build_status(bid: str):
    _building(bid)
    job = _builds.get(bid)
    if job is None:
        return {"state": "idle"}
    out = {"state": job["state"], "started": job["started"], "finished": job.get("finished")}
    if job["result"] is not None:
        out.update(json.loads(json.dumps(job["result"], default=float)))
    return out


@app.get("/api/buildings/{bid}/map")
def get_map(bid: str):
    return _building(bid).map_json()


@app.get("/api/buildings/{bid}/floorplan.png")
def floorplan_png(bid: str, visit: str | None = None):
    b = _building(bid)
    v = b.visits.get(visit) if visit else None
    return Response(b.floorplan_png(v), media_type="image/png", headers={"Cache-Control": "no-store"})


# ---- visits -----------------------------------------------------------------------------------
@app.post("/api/buildings/{bid}/visits")
def new_visit(bid: str, body: dict | None = None):
    b = _building(bid)
    try:
        v = b.new_visit((body or {}).get("device", ""))
        v.device = (body or {}).get("device", "")[:120]
    except ValueError as e:
        raise HTTPException(409, str(e))
    store.trace(f"visit-{v.id}", {"t": time.time(), "state": "started", "decided": "visit started",
                                  "device": (body or {}).get("device", "")[:200]})
    return {"visit_id": v.id, "pois": [{"id": p.id, "label": p.label, "kind": p.kind} for p in b.merged.pois]}


@app.post("/api/visits/{vid}/destination")
def destination(vid: str, body: Dest):
    b, v = _visit(vid)
    if body.poi_id not in b.guide.pois:
        raise HTTPException(404, "No such place")
    xz = None if v.last_fix is None else (float(v.last_fix.c[0]), float(v.last_fix.c[2]))
    d = b.guide.set_destination(v, body.poi_id, xz)
    store.trace(f"visit-{vid}", {"t": time.time(), "state": "destination",
                                 "decided": f"destination set to {b.guide.pois[body.poi_id].label}"})
    return {"ok": True, "route_m": round(d, 1)}


def _visit(vid: str):
    try:
        return reg.visit(vid)
    except KeyError:
        raise HTTPException(404, "No such visit")


@app.post("/api/visits/{vid}/frame")
async def visit_frame(vid: str, image: UploadFile = File(...), heading_deg: float | None = Form(None),
                      t_ms: int = Form(0)):
    b, v = _visit(vid)
    img, _ = await _image(image)
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(b.visit_frame, v, img, t_ms, heading_deg)


@app.post("/api/visits/{vid}/ask")
def ask(vid: str, body: Text):
    b, v = _visit(vid)
    text = body.text.strip().lower()
    if any(w in text for w in ("where am i", "where are we", "what's around", "what is around")):
        say = b.guide.describe(v)
    elif any(w in text for w in ("help", "helper", "person", "someone")):
        v.help_requested = True
        say = "I've asked your helper to come. Please stay where you are."
    else:
        poi = b.guide.match_destination(text)
        if poi is None:
            names = ", ".join(p.label for p in b.merged.pois[:8])
            say = f"I didn't catch a place. You can go to: {names}." if names else "No places have been named yet."
        else:
            xz = None if v.last_fix is None else (float(v.last_fix.c[0]), float(v.last_fix.c[2]))
            d = b.guide.set_destination(v, poi.id, xz)
            say = f"Going to {poi.label}." + (f" It's about {round(d)} metres." if d else "")
    store.trace(f"visit-{vid}", {"t": time.time(), "state": "ask", "decided": say, "heard": body.text[:200]})
    return {"say": say}


@app.post("/api/visits/{vid}/help")
def help_(vid: str):
    b, v = _visit(vid)
    v.help_requested = True
    v.state = "helper"
    store.trace(f"visit-{vid}", {"t": time.time(), "state": "helper", "decided": "visitor asked for a helper"})
    return {"ok": True}


@app.post("/api/visits/{vid}/checkpoint")
def checkpoint(vid: str, body: Checkpoint):
    b, v = _visit(vid)
    fix = v.last_fix
    rec = {"t": time.time(), "state": "checkpoint", "decided": f"evaluator marked checkpoint {body.checkpoint_id}",
           "checkpoint": body.checkpoint_id, "fix": None if fix is None else fix.summary(),
           "fix_age_s": None if v.last_fix_t is None else round(time.time() - v.last_fix_t, 2)}
    store.trace(f"visit-{vid}", rec)
    return {"ok": True}


@app.post("/api/visits/{vid}/end")
def end(vid: str, body: EndVisit):
    b, v = _visit(vid)
    store.trace(f"visit-{vid}", {"t": time.time(), "state": "ended", "decided": "visit ended",
                                 "arrived": body.arrived, "notes": body.notes[:500],
                                 "duration_s": round(time.time() - v.started, 1)})
    b.save()
    return {"ok": True}


# ---- helper console ----------------------------------------------------------------------
@app.get("/api/visits")
def visits():
    out = []
    for b in reg.buildings.values():
        for v in b.visits.values():
            out.append({"visit_id": v.id, "id": v.id, "building": b.id, "building_id": b.id,
                        "building_name": b.name, "state": v.state, "device": getattr(v, "device", ""),
                        "help_requested": v.help_requested, "started": v.started,
                        "destination": None if v.destination is None else b.guide.pois[v.destination].label})
    return sorted(out, key=lambda x: -x["started"])


@app.get("/api/visits/{vid}/live")
def live(vid: str):
    b, v = _visit(vid)
    poly = [[round(float(x), 3), round(float(z), 3)] for x, z in b.plan.nodes[v.route]] if v.route else []
    return {"state": v.state, "building": b.id, "fix": None if v.last_fix is None else v.last_fix.summary(),
            "destination": None if v.destination is None else b.guide.pois[v.destination].label,
            "route_polyline": poly, "trace_tail": v.trace[-25:], "help_requested": v.help_requested,
            "focal_ratio": v.focal_ratio}


@app.post("/api/visits/{vid}/say")
def helper_say(vid: str, body: Text):
    b, v = _visit(vid)
    v.helper_text.append("Message from your helper: " + body.text.strip()[:300])
    if v.state == "helper":
        v.help_requested = False
        v.state = "locating"
    store.trace(f"visit-{vid}", {"t": time.time(), "state": "helper_message", "decided": body.text[:300]})
    return {"ok": True}


# ---- static site (mounted last so /api wins) ----------------------------------------------
if config.WEB.exists():
    app.mount("/", StaticFiles(directory=str(config.WEB), html=True), name="web")
else:
    @app.get("/")
    def root():
        return JSONResponse({"ok": True, "note": "web/ not found"})
