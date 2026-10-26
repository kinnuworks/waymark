"""Safety and routing rules of the guidance agent, on a synthetic corridor."""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from waymark.agent.guide import GuideAgent, Visit  # noqa: E402
from waymark.mapping import floorplan  # noqa: E402
from waymark.mapping.model import Keyframe, MapData, Poi  # noqa: E402
from waymark.vision.features import Features  # noqa: E402

K = np.array([[500, 0, 320], [0, 500, 240], [0, 0, 1.0]])


def pose(x, z, yaw_deg):
    """World-to-camera pose for a camera at (x, 1.4, z) facing yaw (0 = +z)."""
    # Columns of Rwc: camera x (right), y (down), z (forward) in world.
    f = np.array([np.sin(np.radians(yaw_deg)), 0, np.cos(np.radians(yaw_deg))])
    down = np.array([0, -1.0, 0])
    right = np.cross(down, f)
    Rwc = np.stack([right, down, f], axis=1)
    R = Rwc.T
    return R, -R @ np.array([x, 1.4, z])


class FakeFix:
    def __init__(self, x, z, yaw, conf):
        self.R, self.t = pose(x, z, yaw)
        self.confidence = conf
        self.inliers = int(conf * 100)

    @property
    def c(self):
        return -self.R.T @ self.t

    @property
    def yaw(self):
        f = self.R.T @ np.array([0, 0, 1.0])
        return float(np.degrees(np.arctan2(f[0], f[2])))

    def summary(self):
        return {"x": float(self.c[0]), "z": float(self.c[2]), "confidence": self.confidence}


def corridor():
    """A walker went 10 m straight into the building, then 6 m to the right."""
    d = MapData()
    empty = Features(np.zeros((0, 2), np.float32), np.zeros((0, 128), np.float32), np.zeros(0, np.float32), (640, 480))
    path = [(0, z) for z in np.arange(0.5, 10.01, 0.5)] + [(x, 10) for x in np.arange(0.5, 6.01, 0.5)]
    for i, (x, z) in enumerate(path):
        yaw = 0 if x == 0 else 90
        R, t = pose(x, z, yaw)
        d.keyframes.append(Keyframe(i, "w1", R, t, K, empty, np.zeros(0, int)))
    d.pois = [Poi(0, "Room 104", "room", 6.0, 10.4, "w1", len(path) - 1),
              Poi(1, "Stairs", "stairs", 0.0, 7.0, "w1", 13)]
    return d, floorplan.build(d)


def test_routes_and_arrives():
    d, plan = corridor()
    g = GuideAgent(d, plan)
    v = Visit("v", "b")
    g.set_destination(v, 0, (0.0, 1.0))
    v.stairs_ack = True  # stairs tested separately
    out = g.step(v, {"ok": True}, FakeFix(0, 1.0, 0, 0.9))
    assert out["state"] == "guiding" and "metres" in out["say"], out
    out = g.step(v, {"ok": True}, FakeFix(5.8, 10.0, 90, 0.9))
    assert out["state"] == "arrived" and "Room 104" in out["say"], out


def test_stairs_pause_when_unsure():
    d, plan = corridor()
    g = GuideAgent(d, plan)
    v = Visit("v", "b")
    g.set_destination(v, 0, (0.0, 4.5))
    out = g.step(v, {"ok": True}, FakeFix(0, 4.5, 0, 0.6))
    assert out["state"] == "stairs_caution" and out["haptic"] == "stop", out


def test_hands_over_after_repeated_failures():
    d, plan = corridor()
    g = GuideAgent(d, plan)
    v = Visit("v", "b")
    states = [g.step(v, {"ok": True}, None)["state"] for _ in range(7)]
    assert states[0] == "low_confidence"
    assert states[-1] == "helper" and v.help_requested


def test_off_route_reroutes():
    d, plan = corridor()
    g = GuideAgent(d, plan)
    v = Visit("v", "b")
    g.set_destination(v, 0, (0.0, 1.0))
    v.stairs_ack = True
    out = g.step(v, {"ok": True}, FakeFix(3.5, 2.0, 0, 0.9))
    assert out["state"] == "off_route", out


def test_never_directs_without_confident_fix():
    d, plan = corridor()
    g = GuideAgent(d, plan)
    v = Visit("v", "b")
    g.set_destination(v, 0, (0.0, 1.0))
    out = g.step(v, {"ok": True}, FakeFix(0, 1.0, 0, 0.2))
    assert out["state"] == "low_confidence"
    assert "metres" not in out["say"] and "turn" not in out["say"].lower()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
