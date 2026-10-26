"""End-to-end smoke test of the HTTP API with synthetic photos of the entrance board."""
import os
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
os.environ["WAYMARK_DATA"] = tempfile.mkdtemp()

from fastapi.testclient import TestClient  # noqa: E402

from waymark.api import app  # noqa: E402
from waymark.vision.board import BoardSpec  # noqa: E402
from waymark.vision.camera import default_K  # noqa: E402

client = TestClient(app)


def board_photo(x: float, z: float, yaw_deg: float = 0.0) -> bytes:
    """Render the board as a camera at (x, 1.2, z) facing the wall would see it."""
    spec = BoardSpec()
    img = spec.board().generateImage((700, 500), marginSize=0)
    w, h = 960, 720
    K = default_K(w, h)
    # Board corners in the building frame (board centre at height 1.2, on the z=0 wall).
    bw, bh = 7 * spec.square_m, 5 * spec.square_m
    corners = np.array([[-bw / 2, 1.2 + bh / 2, 0], [bw / 2, 1.2 + bh / 2, 0],
                        [bw / 2, 1.2 - bh / 2, 0], [-bw / 2, 1.2 - bh / 2, 0]])
    # Facing the wall (towards -z) with image y down means camera axes
    # x, y, z = world +x, -y, -z; then turn by yaw about the vertical.
    Rwc = cv2.Rodrigues(np.array([0, np.radians(yaw_deg), 0.0]))[0] @ np.diag([1.0, -1.0, -1.0])
    R = Rwc.T
    t = -R @ np.array([x, 1.2, z])
    uv, _ = cv2.projectPoints(corners, cv2.Rodrigues(R)[0], t, K, None)
    src = np.float32([[0, 0], [700, 0], [700, 500], [0, 500]])
    H = cv2.getPerspectiveTransform(src, uv.reshape(4, 2).astype(np.float32))
    bg = np.full((h, w), 180, np.uint8)
    noise = np.random.default_rng(0).integers(0, 60, (h, w)).astype(np.uint8)
    bg = cv2.add(bg, cv2.GaussianBlur(noise, (0, 0), 2))
    warped = cv2.warpPerspective(img, H, (w, h), borderValue=0)
    mask = cv2.warpPerspective(np.full((500, 700), 255, np.uint8), H, (w, h))
    bg[mask > 0] = warped[mask > 0]
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(bg, cv2.COLOR_GRAY2BGR))
    return buf.tobytes()


def test_flow():
    assert client.get("/api/health").json()["ok"]
    assert client.get("/api/board.png").status_code == 200
    bid = client.post("/api/buildings", json={"name": "Test hall", "board_height_m": 1.2}).json()["id"]
    wid = client.post(f"/api/buildings/{bid}/walkers", json={"name": "A"}).json()["walker_id"]

    r = client.post(f"/api/buildings/{bid}/walkers/{wid}/frame",
                    files={"image": ("f.jpg", board_photo(0, 0.7), "image/jpeg")}, data={"t_ms": "0"}).json()
    assert r["state"] == "tracking", r
    assert abs(r["pose"]["z"] - 0.7) < 0.05 and abs(r["pose"]["x"]) < 0.05, r["pose"]

    r = client.post(f"/api/buildings/{bid}/walkers/{wid}/frame",
                    files={"image": ("f.jpg", board_photo(0.15, 0.75, 6), "image/jpeg")}, data={"t_ms": "700"}).json()
    assert r["pose"] is not None and abs(r["pose"]["x"] - 0.15) < 0.05, r

    poi = client.post(f"/api/buildings/{bid}/walkers/{wid}/poi", json={"label": "Room 1", "kind": "room"})
    assert poi.status_code == 200
    assert client.get(f"/api/buildings/{bid}/map").status_code == 200
    assert client.get(f"/api/buildings/{bid}/coordinator").status_code == 200
    assert client.get(f"/api/buildings/{bid}/floorplan.png").status_code == 200
    # No finished map yet, so a visit is refused cleanly.
    assert client.post(f"/api/buildings/{bid}/visits", json={}).status_code == 409


if __name__ == "__main__":
    test_flow()
    print("ok")
