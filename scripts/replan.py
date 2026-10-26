"""Rebuild floor plans and route graphs of saved buildings after a floor-plan change.

Run with the service stopped: python scripts/replan.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from waymark.mapping import floorplan  # noqa: E402
from waymark.service import Registry  # noqa: E402

reg = Registry()
for b in reg.buildings.values():
    if b.merged is None:
        continue
    b.plan = floorplan.build(b.merged)
    b._activate()
    for v in b.visits.values():
        v.route = []
    b.save()
    print(b.id, len(b.plan.segments), "wall segments,", len(b.plan.nodes), "route nodes")
