"""Image quality checks and camera intrinsics."""
from __future__ import annotations

import cv2
import numpy as np

# Phone main cameras are close to a 26 mm full-frame equivalent. On a 4:3
# sensor that gives a focal length of about 0.75 x the long image side.
DEFAULT_FOCAL_RATIO = 0.75


def decode(data: bytes, max_side: int = 960) -> np.ndarray | None:
    img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    h, w = img.shape[:2]
    s = max_side / max(h, w)
    if s < 1:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    return img


def quality(gray: np.ndarray) -> dict:
    """Sharpness (variance of the Laplacian on a fixed-size copy) and brightness."""
    small = cv2.resize(gray, (480, int(480 * gray.shape[0] / gray.shape[1])), interpolation=cv2.INTER_AREA)
    sharp = float(cv2.Laplacian(small, cv2.CV_32F).var())
    bright = float(small.mean())
    ok = sharp > 35 and 35 < bright < 225
    reason = "ok"
    if bright <= 35:
        reason = "too_dark"
    elif bright >= 225:
        reason = "too_bright"
    elif sharp <= 35:
        reason = "blurry"
    return {"sharpness": round(sharp, 1), "brightness": round(bright, 1), "ok": ok, "reason": reason}


def default_K(width: int, height: int, focal_ratio: float = DEFAULT_FOCAL_RATIO) -> np.ndarray:
    f = focal_ratio * max(width, height)
    return np.array([[f, 0, width / 2], [0, f, height / 2], [0, 0, 1]], np.float64)


def scale_K(K: np.ndarray, from_size: tuple[int, int], to_size: tuple[int, int]) -> np.ndarray:
    sx, sy = to_size[0] / from_size[0], to_size[1] / from_size[1]
    K2 = K.copy()
    K2[0] *= sx
    K2[1] *= sy
    return K2
