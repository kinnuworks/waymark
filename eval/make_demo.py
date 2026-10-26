"""Create the public sample building from Microsoft 7-Scenes and replay a visit through it.

The sample lets judges open the helper console and see a merged two-walker map,
named places, a route and a full decision trace without visiting a building.
It is labelled in the app as a replay of a public dataset. Each 7-Scenes
sequence plays one walker; their first and last frames get the dataset's
reference pose in place of the printed board. The visit replays a third pass
through the room with no reference poses at all.

Usage: python eval/make_demo.py [SCENE] [--step N]
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "eval"))

from sevenscenes import K, seq_frames  # noqa: E402
from waymark.service import Registry  # noqa: E402

PLACES = {  # frame index (after stepping) at which a volunteer would mark a place
    "heads": [(12, "Reading desk", "desk"), (40, "Bookshelf", "other"), (70, "Door to the corridor", "door")],
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scene", nargs="?", default="heads")
    ap.add_argument("--step", type=int, default=10)
    args = ap.parse_args()
    reg = Registry()
    b = reg.create(f"Sample room (Microsoft 7-Scenes '{args.scene}', replayed)", 1.2)
    seqs = sorted(int(p.name.split("-")[1]) for p in (ROOT / "eval/data/7scenes" / args.scene).glob("seq-*") if p.is_dir())
    walkers = seqs[:2]
    for k, s in enumerate(walkers):
        w = b.add_walker(f"Volunteer {'AB'[k]}")
        frames = seq_frames(args.scene, s)[::args.step]
        n = len(frames)
        for i, (f, R, t) in enumerate(frames):
            img = cv2.imread(str(f))
            anchor = (R, t) if (i < 10 or i >= n - 10) else None
            w.tracker.process(img, t_ms=i, anchor=anchor, K=K)
            if k == 0:
                for idx, label, kind in PLACES.get(args.scene, []):
                    if i == idx:
                        w.tracker.mark(label, kind)
        print(f"walker {w.id}: {len(w.tracker.data.keyframes)} keyframes, {len(w.tracker.data.xyz)} points", flush=True)
    stats = b.build()["stats"]
    print("build", {k: v for k, v in stats.items() if k != "ba"}, flush=True)

    # Replay a visit: the second sequence again, offset so frames differ from the mapped ones.
    v = b.new_visit("replay")
    v.device = "7-Scenes replay"
    v.focal_ratio = K[0, 0] / 640.0
    target = b.merged.pois[-1].id if b.merged.pois else None
    frames = seq_frames(args.scene, walkers[-1])[args.step // 2::args.step]
    t0 = time.time()
    for i, (f, _, _) in enumerate(frames):
        img = cv2.imread(str(f))
        if i == 3 and target is not None and v.last_fix is not None:
            b.guide.set_destination(v, target, (float(v.last_fix.c[0]), float(v.last_fix.c[2])))
        b.visit_frame(v, img, i * 1000, None)
    states = {}
    for r in v.trace:
        states[r["state"]] = states.get(r["state"], 0) + 1
    print("visit", v.id, states, f"{(time.time() - t0) / max(1, len(frames)) * 1000:.0f} ms/frame", flush=True)
    b.save()
    print("building", b.id)


if __name__ == "__main__":
    main()
