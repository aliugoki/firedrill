"""The failure the cameras cannot see, and the process that catches it.

A stable misidentification -- one person's face confidently matched to another
employee's name, every frame, on one track, with nobody else claiming that name
-- leaves no camera evidence that anything is wrong. The identity machine has
nothing to conflict with, the presence machine watches one person walk to the
muster point, and the board reports that employee safe.

It is worth being exact about what stops this, because nothing in `core/` does.
The manual roll-call does. The report measures the system against what a warden
physically confirmed, and a person the system called safe whom no warden
confirmed is a false accounted, which fails validation outright. These tests
assert both halves: that the cameras are fooled, and that the process is not.
"""

from __future__ import annotations

import pytest

from app.core.accountability_fsm import AccountabilityState
from app.drill import Drill
from app.reporting.drill_report import build_report
from app.reporting.validation import Criterion, Outcome
from app.simulator.agents import Behaviour
from app.simulator.engine import DrillPlan, build_roster, observe, run_drill
from app.simulator.injections import NONE
from app.warden.actions import ActionKind, WardenAction
from tests.simulator.conftest import ALARM_MS, population

HORIZON_MS = 600_000


@pytest.fixture(scope="module")
def agents(site):
    return population(site, 120, 20260910)


def plan_for(site, agents, misidentify) -> DrillPlan:
    return DrillPlan(site=site, agents=agents, alarm_ms=ALARM_MS,
                     injections=NONE, seed=20260910, horizon_ms=HORIZON_MS,
                     misidentify=misidentify)


def one_of(agents, behaviour, *, reached=None):
    for agent in agents:
        if agent.behaviour is not behaviour or not agent.emp_id:
            continue
        if reached is not None and agent.reached_assembly is not reached:
            continue
        return agent
    pytest.skip(f"this population has no {behaviour.value} employee")


class TestTheCamerasCannotTell:

    def test_an_absent_employee_is_reported_safe(self, site, agents):
        """The purest form of the failure this system exists to prevent.

        Somebody who never came to work is marked ACCOUNTED, and the person
        actually standing at the muster point is reported UNACCOUNTED. A search
        team is sent for someone who is standing right there.
        """
        walked_out = one_of(agents, Behaviour.EVACUATES, reached=True)
        never_came_in = one_of(agents, Behaviour.ABSENT_FROM_SITE)

        result = run_drill(plan_for(
            site, agents, {walked_out.emp_id: never_came_in.emp_id}))

        assert never_came_in.reached_assembly is False
        assert result.state_of(never_came_in.person_ref) is (
            AccountabilityState.ACCOUNTED)
        assert result.state_of(walked_out.person_ref) is not (
            AccountabilityState.ACCOUNTED)

    def test_nothing_in_the_evidence_looks_wrong(self, site, agents):
        # No conflict, no rejection, no thin margin. Every observation of this
        # track says the same confident thing, which is why no threshold helps.
        walked_out = one_of(agents, Behaviour.EVACUATES, reached=True)
        never_came_in = one_of(agents, Behaviour.ABSENT_FROM_SITE)
        stream = observe(plan_for(
            site, agents, {walked_out.emp_id: never_came_in.emp_id}))

        # The face of the person who walked out, not every event that happens
        # to name the absent employee: an unenrolled stranger is matched against
        # the nearest gallery entry and can land on any id at a poor score.
        claims = [event.payload for event in stream.events
                  if stream.who(event.subject, event.ts_ms)
                  == walked_out.person_ref
                  and event.payload.get("candidate_id")]
        assert claims
        for payload in claims:
            assert payload["candidate_id"] == never_came_in.emp_id
            assert payload["score"] > 0.8
            assert payload["margin"] > 0.2
            assert payload["association_is_strong"] is True


class TestTheRollCallCatchesIt:

    def test_the_report_records_it_as_a_false_accounted(self, site, agents):
        walked_out = one_of(agents, Behaviour.EVACUATES, reached=True)
        never_came_in = one_of(agents, Behaviour.ABSENT_FROM_SITE)
        plan = plan_for(site, agents, {walked_out.emp_id: never_came_in.emp_id})
        report = _drill_report(plan)

        falsely = {item.person_ref for item in report.false_accounted}
        assert never_came_in.person_ref in falsely
        assert report.is_safe_result is False

    def test_validation_fails_on_the_criterion_that_matters(self, site, agents):
        walked_out = one_of(agents, Behaviour.EVACUATES, reached=True)
        never_came_in = one_of(agents, Behaviour.ABSENT_FROM_SITE)
        plan = plan_for(site, agents, {walked_out.emp_id: never_came_in.emp_id})
        report = _drill_report(plan)

        assert report.validation is not None
        assert report.validation.outcome is Outcome.FAIL
        failed = {check.criterion for check in report.validation.checks
                  if not check.passed}
        assert Criterion.NO_FALSE_ACCOUNTED in failed

    def test_a_clean_drill_does_not_fail_that_criterion(self, site, agents):
        # The guard for the two tests above: they must be failing because of the
        # misidentification, not because this harness fails everything.
        report = _drill_report(plan_for(site, agents, {}))
        failed = {check.criterion for check in report.validation.checks
                  if not check.passed}
        assert Criterion.NO_FALSE_ACCOUNTED not in failed


class TestAMisidentificationOntoSomebodyObserved:

    def test_it_is_refused_rather_than_believed(self, site, agents):
        """When the victim is in the building, their own track claims their own
        name, the two claims overlap in time, and neither is accounted for."""
        walked_out = one_of(agents, Behaviour.EVACUATES, reached=True)
        at_a_desk = one_of(agents, Behaviour.NEVER_LEAVES)

        result = run_drill(plan_for(
            site, agents, {walked_out.emp_id: at_a_desk.emp_id}))

        assert result.state_of(at_a_desk.person_ref) is not (
            AccountabilityState.ACCOUNTED)


def _drill_report(plan: DrillPlan):
    """Run a drill through the real Drill object with honest wardens."""
    stream = observe(plan)
    end_ms = plan.alarm_ms + plan.horizon_ms
    roster = build_roster(plan.agents, plan.alarm_ms)
    zones = frozenset(entry.assigned_assembly_zone for entry in roster.entries
                      if entry.assigned_assembly_zone)

    drill = Drill(drill_id="d-misid", tenant_id="t", site_id=plan.site.site_id,
                  name="misidentification", roster=roster,
                  created_ms=plan.alarm_ms - 1, assembly_zones=zones)
    drill.start(plan.alarm_ms)
    drill.feed([event for event in stream.events
                if stream.arrival_of(event) <= end_ms])
    drill.tick(end_ms)

    truly_at: dict[str, list[str]] = {}
    for agent in plan.agents:
        if agent.reached_assembly and agent.target_assembly:
            truly_at.setdefault(agent.target_assembly, []).append(agent.person_ref)

    sweep_ms = end_ms - 60_000
    for index, zone_id in enumerate(sorted(zones)):
        warden_id, device_id = f"warden-{index}", f"device-{index}"
        for person_ref in truly_at.get(zone_id, []):
            drill.record_warden_action(WardenAction(
                kind=ActionKind.CONFIRM_PRESENT, warden_id=warden_id,
                device_id=device_id, zone_id=zone_id, ts_ms=sweep_ms,
                subject=person_ref))
        drill.record_warden_action(WardenAction(
            kind=ActionKind.SWEEP_COMPLETE, warden_id=warden_id,
            device_id=device_id, zone_id=zone_id, ts_ms=sweep_ms + 30_000))

    drill.complete(end_ms)
    return build_report(drill, now_ms=end_ms)
