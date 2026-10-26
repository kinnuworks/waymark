"""Field evaluation: what happened in a real building, computed from the traces.

* Localisation accuracy: an evaluator taps "checkpoint N" when the visitor's
  phone is over a tape-measured floor mark; the latest fix is compared with
  the mark.
* Route outcomes: arrivals, time, off-route events, low-confidence share and
  hand-offs to a human helper, per visit.
* Map quality: the build statistics, including how far the two walkers
  disagreed before and after the merge.
"""
from __future__ import annotations

import json

import numpy as np

from . import store


def report(b) -> dict:
    cp_file = b.dir / "checkpoints.json"
    marks = json.loads(cp_file.read_text())["checkpoints"] if cp_file.exists() else {}
    cp_err, visits = [], []
    for v in b.visits.values():
        tr = store.read_trace(f"visit-{v.id}")
        states = [r.get("state") for r in tr]
        frames = [r for r in tr if "saw" in r]
        n = max(1, len(frames))
        conf_ok = sum(1 for r in frames if (r.get("saw") or {}).get("fix") and r["saw"]["fix"]["confidence"] >= 0.35)
        ms = [r["ms"] for r in frames if "ms" in r]
        end = next((r for r in reversed(tr) if r.get("state") == "ended"), None)
        visits.append({
            "visit": v.id, "device": getattr(v, "device", ""), "frames": len(frames),
            "arrived": bool(end and end.get("arrived")) or ("arrived" in states),
            "evaluator_says_arrived": None if end is None else bool(end.get("arrived")),
            "duration_s": None if end is None else end.get("duration_s"),
            "located_pct": round(conf_ok / n * 100, 1),
            "off_route_events": states.count("off_route"),
            "stairs_pauses": states.count("stairs_caution"),
            "helper_handoffs": states.count("helper"),
            "ms_median": round(float(np.median(ms)), 1) if ms else None,
            "notes": None if end is None else end.get("notes"),
        })
        for r in tr:
            if r.get("state") == "checkpoint" and r.get("fix") and r["checkpoint"] in marks:
                gx, gz = marks[r["checkpoint"]]
                e = float(np.hypot(r["fix"]["x"] - gx, r["fix"]["z"] - gz))
                cp_err.append({"visit": v.id, "checkpoint": r["checkpoint"], "error_m": round(e, 3),
                               "fix_age_s": r.get("fix_age_s"), "confidence": r["fix"]["confidence"]})
    errs = np.array([c["error_m"] for c in cp_err]) if cp_err else np.zeros(0)
    return {
        "building": b.summary(),
        "map": b.build_stats,
        "checkpoints": {
            "n": int(len(errs)),
            "median_m": round(float(np.median(errs)), 3) if len(errs) else None,
            "p90_m": round(float(np.percentile(errs, 90)), 3) if len(errs) else None,
            "within_0_5m_pct": round(float((errs < 0.5).mean() * 100), 1) if len(errs) else None,
            "within_1m_pct": round(float((errs < 1.0).mean() * 100), 1) if len(errs) else None,
            "all": cp_err,
        },
        "visits": visits,
        "arrival_rate_pct": round(100 * sum(v["arrived"] for v in visits) / len(visits), 1) if visits else None,
    }
