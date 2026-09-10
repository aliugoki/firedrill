"""Regenerate the worked example in docs/EVAC120_VALIDATION.md.

The report in that document quotes measured numbers. A measured number nobody
can reproduce is a claim, so this is the thing that produced it: one 200-person
drill under the realistic injection profile, driven through the real `Drill`
object rather than a test harness, with wardens who sweep and count.

    cd backend && ../.venv/bin/python scripts/worked_example.py

The wardens here are honest and thorough: each confirms exactly the people who
physically reached their zone, and counts exactly the people who are standing
there. That is deliberate. It makes the manual roll-call a clean reference, so
every disagreement the report prints is the system's, not a simulated human's.
A real drill will not be this tidy, which is one more reason the numbers below
are an illustration of the report and not a measurement of a building.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.drill import Drill                                       # noqa: E402
from app.infra.audit import AuditLog                              # noqa: E402
from app.reporting.drill_report import build_report               # noqa: E402
from app.simulator.agents import build_population, walk_all       # noqa: E402
from app.simulator.engine import DrillPlan, build_roster, observe  # noqa: E402
from app.simulator.injections import REALISTIC                    # noqa: E402
from app.simulator.site import default_site                       # noqa: E402
from app.warden.actions import ActionKind, WardenAction           # noqa: E402
from app.warden.headcount import Headcount                        # noqa: E402

ALARM_MS = 1_788_000_000_000
HORIZON_MS = 600_000
SEED = 20260910
PEOPLE = 200


def main() -> int:
    site = default_site()
    agents = build_population(site, count=PEOPLE, seed=SEED)
    walk_all(agents, site, alarm_ms=ALARM_MS, seed=SEED)

    plan = DrillPlan(site=site, agents=agents, alarm_ms=ALARM_MS,
                     injections=REALISTIC, seed=SEED, horizon_ms=HORIZON_MS)
    stream = observe(plan)
    end_ms = ALARM_MS + HORIZON_MS

    roster = build_roster(agents, ALARM_MS)
    zones = frozenset(entry.assigned_assembly_zone
                      for entry in roster.entries
                      if entry.assigned_assembly_zone)
    drill = Drill(drill_id="drill-worked-example", tenant_id="tenant-sim",
                  site_id=site.site_id, name="Worked example",
                  roster=roster, created_ms=ALARM_MS - 1, assembly_zones=zones)
    drill.start(ALARM_MS)
    drill.feed([event for event in stream.events
                if stream.arrival_of(event) <= end_ms])
    drill.tick(end_ms)

    # The manual roll-call. Ground truth, which is what a warden's own eyes are.
    truly_at: dict[str, list[str]] = {}
    for agent in agents:
        if agent.reached_assembly and agent.target_assembly:
            truly_at.setdefault(agent.target_assembly, []).append(agent.person_ref)

    sweep_ms = end_ms - 60_000
    for index, zone_id in enumerate(sorted(zones)):
        warden_id = f"warden-{index + 1}"
        device_id = f"device-{index + 1}"
        present = truly_at.get(zone_id, [])
        for person_ref in present:
            drill.record_warden_action(WardenAction(
                kind=ActionKind.CONFIRM_PRESENT, warden_id=warden_id,
                device_id=device_id, zone_id=zone_id, ts_ms=sweep_ms,
                subject=person_ref))
        expected = {entry.person_ref for entry in roster.entries
                    if entry.assigned_assembly_zone == zone_id}
        for person_ref in sorted(expected - set(present)):
            drill.record_warden_action(WardenAction(
                kind=ActionKind.NOT_HERE, warden_id=warden_id,
                device_id=device_id, zone_id=zone_id, ts_ms=sweep_ms,
                subject=person_ref))
        panel = {p["zone_id"]: p for p in drill.zone_panels()}[zone_id]
        drill.record_headcount(Headcount(
            zone_id=zone_id, warden_id=warden_id, device_id=device_id,
            ts_ms=sweep_ms, physical_count=len(present),
            system_count=panel.get("confirmed", len(present))))
        drill.record_warden_action(WardenAction(
            kind=ActionKind.SWEEP_COMPLETE, warden_id=warden_id,
            device_id=device_id, zone_id=zone_id, ts_ms=sweep_ms + 30_000))

    drill.complete(end_ms)
    report = build_report(drill, now_ms=end_ms, audit=AuditLog())
    print("\n".join(report.render()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
