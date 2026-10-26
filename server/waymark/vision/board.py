"""The printed entrance board: a ChArUco board that fixes the building frame.

Every walker starts and ends at this board, so every map shares one origin and
one metric scale without any cross-device matching. The building frame is
right-handed with metres as units:

    x  to the right along the wall, as seen when facing the board
    y  up, height above the floor
    z  away from the wall, into the building

The board centre sits at (0, board_height, 0).
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

SQUARES_X, SQUARES_Y = 7, 5
DICT = cv2.aruco.DICT_5X5_100
MARKER_RATIO = 0.75


@dataclass
class BoardSpec:
    square_m: float = 0.035
    height_m: float = 1.20  # board centre above the floor

    def board(self) -> cv2.aruco.CharucoBoard:
        d = cv2.aruco.getPredefinedDictionary(DICT)
        return cv2.aruco.CharucoBoard((SQUARES_X, SQUARES_Y), self.square_m,
                                      self.square_m * MARKER_RATIO, d)

    def board_to_world(self) -> tuple[np.ndarray, np.ndarray]:
        """Rotation and translation that take board coordinates to the building frame.

        The board frame has x right, y down the board, z into the wall.
        """
        w, h = SQUARES_X * self.square_m, SQUARES_Y * self.square_m
        R = np.diag([1.0, -1.0, -1.0])
        t = np.array([-w / 2, h / 2 + self.height_m, 0.0])
        return R, t


def render_png(spec: BoardSpec, dpi: int = 300) -> bytes:
    """A4 landscape page with the board at true size and a ruler for checking the print."""
    px_per_m = dpi / 0.0254
    page_w, page_h = int(0.297 * px_per_m), int(0.210 * px_per_m)
    bw = int(SQUARES_X * spec.square_m * px_per_m)
    bh = int(SQUARES_Y * spec.square_m * px_per_m)
    img = spec.board().generateImage((bw, bh), marginSize=0, borderBits=1)
    page = np.full((page_h, page_w), 255, np.uint8)
    x0, y0 = (page_w - bw) // 2, int(0.012 * px_per_m)
    page[y0:y0 + bh, x0:x0 + bw] = img
    # 10 cm ruler under the board so the print scale can be checked by hand.
    ry = y0 + bh + int(0.008 * px_per_m)
    rl = int(0.10 * px_per_m)
    cv2.line(page, (x0, ry), (x0 + rl, ry), 0, 3)
    for i in range(11):
        tick = 18 if i % 5 == 0 else 10
        x = x0 + int(i * rl / 10)
        cv2.line(page, (x, ry - tick), (x, ry + tick), 0, 2)
    txt = (f"Waymark entrance board. Each square should measure {spec.square_m * 1000:.0f} mm. "
           f"Mount it upright with its centre {spec.height_m:.2f} m above the floor.")
    cv2.putText(page, txt, (x0, ry + int(0.012 * px_per_m)), cv2.FONT_HERSHEY_SIMPLEX, 1.1, 0, 2, cv2.LINE_AA)
    ok, buf = cv2.imencode(".png", page)
    return buf.tobytes()


class BoardDetector:
    def __init__(self, spec: BoardSpec):
        self.spec = spec
        self.board = spec.board()
        params = cv2.aruco.CharucoParameters()
        dparams = cv2.aruco.DetectorParameters()
        dparams.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        self.det = cv2.aruco.CharucoDetector(self.board, params, dparams)
        self.R_bw, self.t_bw = spec.board_to_world()

    def detect(self, gray: np.ndarray):
        """Return (corner_pixels (N,2), corner_ids (N,)) or None."""
        corners, ids, _, _ = self.det.detectBoard(gray)
        if ids is None or len(ids) < 6:
            return None
        return corners.reshape(-1, 2).astype(np.float32), ids.reshape(-1)

    def object_points(self, ids: np.ndarray) -> np.ndarray:
        """Board corners for these ids, in building coordinates."""
        pb = self.board.getChessboardCorners()[ids].astype(np.float64)
        return (self.R_bw @ pb.T).T + self.t_bw

    def pose(self, gray: np.ndarray, K: np.ndarray, dist: np.ndarray | None = None):
        """Camera pose from the board: (R_cw, t_cw, rms_px, n_corners) or None."""
        found = self.detect(gray)
        if found is None:
            return None
        px, ids = found
        obj = self.object_points(ids)
        dist = np.zeros(5) if dist is None else dist
        ok, rvec, tvec = cv2.solvePnP(obj, px, K, dist, flags=cv2.SOLVEPNP_SQPNP)
        if not ok:
            return None
        rvec, tvec = cv2.solvePnPRefineLM(obj, px, K, dist, rvec, tvec)
        proj, _ = cv2.projectPoints(obj, rvec, tvec, K, dist)
        rms = float(np.sqrt(np.mean(np.sum((proj.reshape(-1, 2) - px) ** 2, axis=1))))
        R, _ = cv2.Rodrigues(rvec)
        return R, tvec.reshape(3), rms, len(ids)


class Calibrator:
    """Collects board views and solves for the camera intrinsics."""

    def __init__(self, spec: BoardSpec, needed: int = 8):
        self.det = BoardDetector(spec)
        self.needed = needed
        self.obj: list[np.ndarray] = []
        self.img: list[np.ndarray] = []
        self.size: tuple[int, int] | None = None
        self.result: dict | None = None

    def add(self, gray: np.ndarray) -> dict:
        found = self.det.detect(gray)
        self.size = (gray.shape[1], gray.shape[0])
        if found is not None and len(found[1]) >= 10:
            px, ids = found
            self.obj.append(self.det.board.getChessboardCorners()[ids].astype(np.float32))
            self.img.append(px)
        if len(self.obj) >= self.needed and self.result is None:
            self.solve()
        return {"board_found": found is not None, "views": len(self.obj), "needed": self.needed,
                "done": self.result is not None,
                "rms_px": None if self.result is None else round(self.result["rms"], 3)}

    def solve(self) -> None:
        rms, K, dist, _, _ = cv2.calibrateCamera(self.obj, self.img, self.size, None, None,
                                                 flags=cv2.CALIB_FIX_K3)
        self.result = {"K": K, "dist": dist.reshape(-1), "rms": float(rms), "size": self.size}
