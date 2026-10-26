"""Per-stage latency of the live pipeline on this machine.

Times every stage a visitor frame goes through (decode, quality check, ALIKED,
VLAD retrieval, matching, PnP) plus the keyframe-only stages (LightGlue,
triangulation) on real 7-Scenes frames, and records the OpenCV build so runs
on Graviton (stock wheel or COOL) and x86 can be compared like for like.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import time
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "eval"))

from sevenscenes import K, OUT, build_reference_map, seq_frames  # noqa: E402
from waymark.localize.localizer import Localizer  # noqa: E402
from waymark.vision import camera, geometry  # noqa: E402
from waymark.vision.features import extractor, match_mnn, strong_matcher  # noqa: E402


def timeit(fn, n=15, warm=2):
    for _ in range(warm):
        fn()
    ts = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t) * 1000)
    return {"median_ms": round(float(np.median(ts)), 2), "p95_ms": round(float(np.percentile(ts, 95)), 2)}


def main():
    label = os.environ.get("WAYMARK_BENCH_LABEL") or f"{platform.machine()}-{os.environ.get('WAYMARK_INSTANCE', 'local')}"
    frames = seq_frames("heads", 2)[::20]
    imgs = [cv2.imread(str(f)) for f, _, _ in frames[:6]]
    jpg = cv2.imencode(".jpg", imgs[0], [cv2.IMWRITE_JPEG_QUALITY, 80])[1].tobytes()
    gray = cv2.cvtColor(imgs[0], cv2.COLOR_BGR2GRAY)
    ex, sm = extractor(), strong_matcher()
    fa, fb = ex.extract(imgs[0]), ex.extract(imgs[1])
    data = build_reference_map("heads", [2], 25)
    loc = Localizer(data, check_height=False)
    q = seq_frames("heads", 1)[300]
    qimg = cv2.imread(str(q[0]))
    qf = ex.extract(qimg)
    ids = data.points_of(data.keyframes[:6])
    xyz, pdesc = data.arrays(ids)

    def pnp():
        ia, ib = match_mnn(qf.desc, pdesc)
        return geometry.pnp(xyz[ib], qf.kps[ia], K)

    stages = {
        "jpeg_decode": timeit(lambda: camera.decode(jpg)),
        "quality_check": timeit(lambda: camera.quality(gray)),
        "aliked_640": timeit(lambda: ex.extract(imgs[0]), n=8),
        "mnn_match_1000x1000": timeit(lambda: match_mnn(fa.desc, fb.desc)),
        "lightglue_600x600": timeit(lambda: sm.match(fa, fb), n=5, warm=1),
        "magsac_pnp": timeit(pnp),
        "vlad_retrieval": timeit(lambda: loc._shortlist(qf, None)),
        "localize_total_excl_aliked": timeit(lambda: loc.localize(qf, 0.82, K=K, allow_lightglue=False)),
        "visit_frame_total": timeit(lambda: loc.localize(ex.extract(qimg), 0.82, K=K, allow_lightglue=False), n=6),
    }
    info = cv2.getBuildInformation()
    hal = [line.strip() for line in info.splitlines() if "HAL" in line or "KleidiCV" in line or "kleidicv" in line]
    out = {"label": label, "arch": platform.machine(), "cpu_count": os.cpu_count(), "cv_threads": cv2.getNumThreads(),
           "opencv": cv2.__version__, "hal": hal[:6], "cool": bool(os.environ.get("WAYMARK_COOL")),
           "stages": stages}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"speed_{label}.json").write_text(json.dumps(out, indent=1))
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
