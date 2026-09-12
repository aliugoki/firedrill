"""A populated drill, served, for looking at the UI and for the smoke test.

    cd backend && ../.venv/bin/python scripts/serve_demo.py [--port 8811]

The screens are the deliverable of Phase 4 and the only way to evaluate them is
to open them against something that has happened. A drill with no events shows
every panel's empty state, which is the one thing already covered by unit
tests.

Anchored to the wall clock so the board reads as an evacuation in progress
rather than one dated two weeks out, and only one of the two assembly points is
swept, because a screen where everything is done shows none of the states it
exists to show.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.api.app import create_app                                # noqa: E402
from app.drill import Drill, DrillRegistry                        # noqa: E402
from app.infra.audit import AuditLog                              # noqa: E402
from app.infra.auth import AuthSettings                           # noqa: E402
from app.simulator.agents import build_population, walk_all       # noqa: E402
from app.simulator.engine import DrillPlan, build_roster, observe  # noqa: E402
from app.simulator.injections import REALISTIC                    # noqa: E402
from app.simulator.site import default_site                       # noqa: E402
from app.warden.actions import ActionKind, WardenAction           # noqa: E402
from app.warden.headcount import Headcount                        # noqa: E402

SEED = 20260910
PEOPLE = 200
ELAPSED_MS = 300_000
HORIZON_MS = 600_000
DRILL_ID = "demo"


def build(now_ms: int | None = None):
    """A running drill five minutes in, and the app serving it."""
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    alarm_ms = now_ms - ELAPSED_MS

    site = default_site()
    agents = build_population(site, count=PEOPLE, seed=SEED)
    walk_all(agents, site, alarm_ms=alarm_ms, seed=SEED)
    plan = DrillPlan(site=site, agents=agents, alarm_ms=alarm_ms,
                     injections=REALISTIC, seed=SEED, horizon_ms=HORIZON_MS)
    stream = observe(plan)

    roster = build_roster(agents, alarm_ms)
    zones = frozenset(e.assigned_assembly_zone for e in roster.entries
                      if e.assigned_assembly_zone)
    drill = Drill(drill_id=DRILL_ID, tenant_id="tenant-sim",
                  site_id=site.site_id, name="Quarterly drill", roster=roster,
                  created_ms=alarm_ms - 1, assembly_zones=zones)
    drill.start(alarm_ms)
    drill.feed([e for e in stream.events if stream.arrival_of(e) <= now_ms])
    drill.tick(now_ms)

    truly_at: dict = {}
    for agent in agents:
        if agent.reached_assembly and agent.target_assembly:
            truly_at.setdefault(agent.target_assembly, []).append(agent.person_ref)

    # One zone swept, one still being walked. A warden who has counted one head
    # more than the system has, and a contractor nobody had on a list.
    swept = sorted(zones)[0]
    for person_ref in truly_at.get(swept, []):
        drill.record_warden_action(WardenAction(
            kind=ActionKind.CONFIRM_PRESENT, warden_id="warden-1",
            device_id="device-1", zone_id=swept, ts_ms=now_ms - 90_000,
            subject=person_ref))
    panel = {p["zone_id"]: p for p in drill.zone_panels(now_ms)}[swept]
    drill.record_headcount(Headcount(
        zone_id=swept, warden_id="warden-1", device_id="device-1",
        ts_ms=now_ms - 60_000,
        physical_count=len(truly_at.get(swept, [])) + 1,
        system_count=panel.get("confirmed", 0)))
    drill.record_warden_action(WardenAction(
        kind=ActionKind.TAG_UNKNOWN, warden_id="warden-1", device_id="device-1",
        zone_id=swept, ts_ms=now_ms - 55_000, note="contractor, lift engineer"))
    # Heard from a while ago, so the assembly panel shows a tablet going quiet.
    drill.warden.heard_from("device-1", "warden-1", now_ms - 400_000, swept)

    registry = DrillRegistry()
    registry.add(drill)
    app = create_app(registry=registry, auth=AuthSettings(trust_headers=True),
                     assembly_zones=zones, audit=AuditLog())
    return app, drill, swept


def main(argv: list | None = None) -> int:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8811)
    args = parser.parse_args(argv)

    app, _, swept = build()
    print(f"drill={DRILL_ID} swept={swept} "
          f"http://127.0.0.1:{args.port}/?drill={DRILL_ID}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
