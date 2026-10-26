"""Persistence: local files always, S3 and DynamoDB when configured.

Every decision the agents make is appended to a JSONL trace on local disk and,
on AWS, written to DynamoDB so it survives the instance. Volunteer frames are
archived to S3 so any walk can be replayed through the pipeline later.
"""
from __future__ import annotations

import json
import queue
import threading
import time
from decimal import Decimal
from pathlib import Path

from .. import config

_q: queue.Queue = queue.Queue(maxsize=5000)
_started = False


def _worker():
    import boto3
    s3 = boto3.client("s3", region_name=config.REGION) if config.BUCKET else None
    table = boto3.resource("dynamodb", region_name=config.REGION).Table(config.TABLE) if config.TABLE else None
    while True:
        kind, args = _q.get()
        try:
            if kind == "s3" and s3 is not None:
                s3.put_object(Bucket=config.BUCKET, Key=args[0], Body=args[1])
            elif kind == "ddb" and table is not None:
                table.put_item(Item=args)
        except Exception as e:  # never let archiving break guidance
            print(f"store: {kind} failed: {e}", flush=True)


def _ensure():
    global _started
    if not _started and (config.BUCKET or config.TABLE):
        threading.Thread(target=_worker, daemon=True).start()
        _started = True


def _dec(o):
    if isinstance(o, float):
        return Decimal(str(round(o, 6)))
    if isinstance(o, dict):
        return {k: _dec(v) for k, v in o.items() if v is not None}
    if isinstance(o, list):
        return [_dec(v) for v in o]
    return o


_seq: dict[str, int] = {}
_lock = threading.Lock()


def trace(trace_id: str, record: dict) -> None:
    path = config.DATA / "traces" / f"{trace_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        seq = _seq.get(trace_id, 0)
        _seq[trace_id] = seq + 1
        with open(path, "a") as f:
            f.write(json.dumps(record, default=float) + "\n")
    # One structured log line per decision, for CloudWatch Logs Insights.
    print(f"trace id={trace_id} seq={seq} state={record.get('state')} decided={record.get('decided', '')!r}", flush=True)
    if config.TABLE:
        _ensure()
        item = {"id": trace_id, "seq": seq, "ts": Decimal(str(time.time())), "record": _dec(record)}
        try:
            _q.put_nowait(("ddb", item))
        except queue.Full:
            pass


def read_trace(trace_id: str, tail: int | None = None) -> list[dict]:
    path = config.DATA / "traces" / f"{trace_id}.jsonl"
    if not path.exists():
        return []
    lines = path.read_text().splitlines()
    if tail:
        lines = lines[-tail:]
    return [json.loads(x) for x in lines if x.strip()]


def archive(key: str, data: bytes) -> None:
    """Keep a local copy; mirror to S3 when a bucket is configured."""
    path = config.DATA / "archive" / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if config.BUCKET:
        _ensure()
        try:
            _q.put_nowait(("s3", (key, data)))
        except queue.Full:
            pass


def archived(prefix: str) -> list[Path]:
    root = config.DATA / "archive" / prefix
    return sorted(root.rglob("*")) if root.exists() else []


__all__ = ["trace", "read_trace", "archive", "archived", "Path"]
