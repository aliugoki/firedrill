"""Invariant 8, swept across failure modes rather than argued about.

`CLAUDE.md`: "Any infrastructure failure degrades to `DEGRADED` or
`MANUAL_VERIFICATION_REQUIRED`, never to a false `ALL CLEAR`." Individual
failures are covered in their own files; what this adds is one place that runs
a full honest drill, breaks exactly one thing, and asks whether the verdict
moved.

**The clean case is asserted too, and that is the point.** A system that never
says ALL CLEAR passes "never falsely says ALL CLEAR" trivially, so every
negative case here is worthless without a positive one beside it proving the
verdict is reachable at all.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from app.drill import Drill
from app.simulator.engine import DrillPlan, build_roster, observe
from app.simulator.injections import Injections, Outage
from app.simulator.site import default_site
from app.warden.actions import ActionKind, WardenAction
from app.warden.headcount import Headcount
from tests.simulator.conftest import ALARM_MS, population

SEED = 11
NORTH = "assembly-north"


def _drill(site, *, injections=Injections(), roster_reachable=True,
           skip_sweep=None, skip_headcount=False, headcount_delta=0,
           correct_a_bad_count=False):
    """A complete, honest drill with exactly one thing changed.

    The roll-call confirms every person the roster expects rather than only
    those who really arrived: this file is about the all-clear rule, and a
    warden who has not finished is a different test.
    """
    agents = population(site, 40, SEED)
    plan = DrillPlan(site=site, agents=agents, alarm_ms=ALARM_MS,
                     injections=injections, seed=SEED)
    stream = observe(plan)
    end = ALARM_MS + plan.horizon_ms

    roster = build_roster(agents, ALARM_MS)
    if not roster_reachable:
        roster = replace(roster, source_reachable=False,
                         source_note="loaded from an exported file")
    zones = frozenset(e.assigned_assembly_zone for e in roster.entries
                      if e.assigned_assembly_zone)

    drill = Drill(drill_id="d-allclear", tenant_id="t", site_id=site.site_id,
                  name="all-clear sweep", roster=roster,
                  created_ms=ALARM_MS - 1, assembly_zones=zones)
    drill.start(ALARM_MS)
    drill.feed([e for e in stream.events if stream.arrival_of(e) <= end])
    drill.tick(end)

    sweep_ms = end - 60_000
    for index, zone in enumerate(sorted(zones)):
        if skip_sweep == zone:
            continue
        warden, device = f"warden-{index}", f"device-{index}"
        for entry in roster.expected:
            if entry.assigned_assembly_zone != zone:
                continue
            drill.record_warden_action(WardenAction(
                kind=ActionKind.CONFIRM_PRESENT, warden_id=warden,
                device_id=device, zone_id=zone, ts_ms=sweep_ms,
                subject=entry.person_ref))

        confirmed = {p["zone_id"]: p
                     for p in drill.zone_panels(end)}[zone]["confirmed"]
        if correct_a_bad_count:
            # The dangerous direction first -- the warden sees fewer heads than
            # the system believes are safe -- and then the right number.
            drill.record_headcount(Headcount(
                zone_id=zone, warden_id=warden, device_id=device,
                ts_ms=sweep_ms + 5_000, physical_count=confirmed - 2,
                system_count=confirmed))
        if not skip_headcount:
            drill.record_headcount(Headcount(
                zone_id=zone, warden_id=warden, device_id=device,
                ts_ms=sweep_ms + 10_000,
                physical_count=confirmed + headcount_delta,
                system_count=confirmed))
        drill.record_warden_action(WardenAction(
            kind=ActionKind.SWEEP_COMPLETE, warden_id=warden,
            device_id=device, zone_id=zone, ts_ms=sweep_ms + 30_000))

    return drill, end


class TestTheVerdictIsReachableAtAll:
    """Without this the whole file is vacuous."""

    def test_an_honest_complete_drill_reaches_all_clear(self, site):
        drill, end = _drill(site)
        assert drill.all_clear(end) is True, drill.blocking_all_clear(end)
        assert drill.blocking_all_clear(end) == []


#: One broken thing each, and the reason it must block. The roll-call is
#: complete and honest in every one of them, so the only difference from the
#: case above is the named failure.
BLOCKED = (
    ("a zone nobody swept", dict(skip_sweep=NORTH)),
    ("a zone with no physical count", dict(skip_headcount=True)),
    ("a physical count that disagrees", dict(headcount_delta=-1)),
    ("an outage still open at the end",
     dict(injections=Injections(
         camera_outages=(Outage(30_000, 10_000_000, "*"),)))),
    ("a roster that could not be verified", dict(roster_reachable=False)),
)


class TestNoFailurePathReachesAllClear:

    @pytest.mark.parametrize("label,change", BLOCKED,
                             ids=[case[0] for case in BLOCKED])
    def test_it_is_blocked_and_says_why(self, site, label, change):
        drill, end = _drill(site, **change)
        assert drill.all_clear(end) is False, f"{label} did not block"
        # Never empty when it cannot clear: a refusal with no reason is a
        # screen an operator cannot act on.
        assert drill.blocking_all_clear(end), label


class TestWhatDoesNotBlockIt:
    """Two cases that look like failures and are not, asserted so that making
    them block later is a deliberate decision rather than a drift."""

    def test_an_outage_that_closed_does_not(self, site):
        # Invariant 9: a warden's eyes are the final authority. If every
        # expected person was confirmed in person and the counts agree, the
        # drill is clear whatever the cameras missed earlier. What the blind
        # period costs is the *measurement*, which the health caveat carries.
        drill, end = _drill(site, injections=Injections(
            camera_outages=(Outage(30_000, 90_000, "*"),)))
        assert drill.all_clear(end) is True, drill.blocking_all_clear(end)

    def test_nor_does_a_count_that_was_wrong_and_then_corrected(self, site):
        drill, end = _drill(site, correct_a_bad_count=True)
        assert drill.all_clear(end) is True, drill.blocking_all_clear(end)

    def test_but_the_moment_of_disagreement_is_still_on_the_record(self, site):
        """`EVAC120_EXPLAINED.md`: "Even if the count is later corrected and
        agrees, the report still records that there was a moment when it did
        not."

        `mismatches()` answers about *now* and is empty here, which is correct
        and is also how this looks clean from the outside. `count_history()` is
        the memory, and it is what the report reads.
        """
        drill, end = _drill(site, correct_a_bad_count=True)
        assert drill.warden.mismatches() == []

        history = drill.warden.count_history()
        assert history, "the disagreement left no trace at all"
        for zone in history:
            assert zone["ever_escalated"] is True
            assert zone["settled_now"] is True
            assert len(zone["physical_counts"]) == 2, zone

    def test_a_drill_that_never_disagreed_has_no_history(self, site):
        # The list is exceptions only; padding it with quiet zones buries them.
        drill, end = _drill(site)
        assert drill.warden.count_history() == []
