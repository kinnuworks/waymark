"""Runtime settings, read once from the environment."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODELS = Path(os.environ.get("WAYMARK_MODELS", ROOT / "models"))
DATA = Path(os.environ.get("WAYMARK_DATA", ROOT / "data"))
WEB = Path(os.environ.get("WAYMARK_WEB", ROOT / "web"))
EVAL_OUT = Path(os.environ.get("WAYMARK_EVAL", ROOT / "eval" / "out"))
BUCKET = os.environ.get("WAYMARK_BUCKET", "")
TABLE = os.environ.get("WAYMARK_TABLE", "")
INSTANCE = os.environ.get("WAYMARK_INSTANCE", "local")
REGION = os.environ.get("AWS_REGION", "us-east-1")
# Matching backend for keyframe and loop-closure matching: "lightglue" or "mnn".
STRONG_MATCHER = os.environ.get("WAYMARK_STRONG_MATCHER", "lightglue")

ALIKED_ONNX = MODELS / "aliked-n16rot-top1k-640.onnx"
LIGHTGLUE_ONNX = MODELS / "aliked_lightglue.onnx"
