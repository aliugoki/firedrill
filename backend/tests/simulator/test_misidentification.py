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
from app.simulator.injections import NONE, Injections
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


#: Mixes that can put an employee's name on the wrong body, with a seed at
#: which each one does. Named and fixed rather than drawn at random: a property
#: test that picks its own inputs is one that fails on a machine nobody can
#: reproduce. The last entry is the mix and seed that first produced a
#: camera-only false accept during a sweep of sixty random combinations, kept
#: because a case found is worth more than a case designed.
FOOLS_THE_CAMERAS: tuple[tuple[str, int, Injections], ...] = (
    ("wrong identity alone", 1,
     Injections(wrong_identity_rate=0.15)),
    ("wrong identity under fragmentation", 1,
     Injections(wrong_identity_rate=0.15, track_fragmentation_rate=0.2)),
    ("everything at once", 32795,
     Injections(face_loss_rate=0.2, poor_quality_rate=0.3,
                lookalike_confusion_rate=0.4, wrong_identity_rate=0.15,
                misassociation_rate=0.3, track_fragmentation_rate=0.2,
                id_switch_rate=0.1, duplicate_rate=0.1, reorder_rate=0.2,
                delay_rate=0.2)),
)

#: Mixes the design defends against structurally, which therefore must **never**
#: produce one. A look-alike close enough to test the margin gate is a
#: `CONFLICT`, not a coin-flip winner (invariant 3); a correctly matched face
#: pinned to the wrong body is refused by `identity_fsm.gate` on the structure
#: of the association rather than on its score.
#:
#: Neither produced a camera-only false accept in 119 seeds when this was
#: written. The sweep below runs a smaller range on every suite run.
DEFENDED: tuple[tuple[str, Injections], ...] = (
    ("look-alikes and switches",
     Injections(lookalike_confusion_rate=0.4, id_switch_rate=0.1)),
    ("misassociation with a lossy face pipeline",
     Injections(face_loss_rate=0.3, misassociation_rate=0.3)),
)


class TestTheRollCallCatchesWhatTheCamerasCannot:
    """The claim at the top of this file, held to across a sweep rather than
    one hand-built pair.

    A stable misidentification leaves no camera evidence: the identity machine
    has nothing to conflict with, the presence machine watches a real person
    walk to the muster point, and the board reports the *other* employee safe.
    Nothing in `core/` can see it. The docstring says the manual roll-call is
    what stops it, and until now that rested on one deliberately constructed
    scenario.

    These runs construct nothing. They turn the injection knobs up and ask
    ground truth who actually arrived.
    """

    @staticmethod
    def _camera_only_false_accepts(agents, result) -> set:
        """Accounted by the cameras, and never at an assembly point in fact."""
        truth = {a.person_ref for a in agents if a.reached_assembly}
        return result.accounted_refs() - truth

    def _plan(self, site, seed, injections) -> DrillPlan:
        return DrillPlan(site=site, agents=population(site, 80, seed),
                         alarm_ms=ALARM_MS, injections=injections, seed=seed,
                         horizon_ms=HORIZON_MS)

    @pytest.mark.parametrize("label,seed,injections", FOOLS_THE_CAMERAS,
                             ids=[m[0] for m in FOOLS_THE_CAMERAS])
    def test_a_completed_roll_call_catches_every_one(
            self, site, label, seed, injections):
        plan = self._plan(site, seed, injections)
        agents = plan.agents
        fooled = self._camera_only_false_accepts(agents, run_drill(plan))
        assert fooled, (
            f"{label} is listed as a mix that fools the cameras and did not, "
            "so this case is no longer testing anything")

        report = _drill_report(plan)
        caught = {d.person_ref for d in report.false_accounted}
        assert fooled <= caught, (
            f"{label}: the cameras accounted for {sorted(fooled - caught)} who "
            "never reached an assembly point, and a finished roll-call did not "
            "catch them")
        assert report.is_safe_result is False
        assert report.validation.outcome is Outcome.FAIL

    @pytest.mark.parametrize("label,injections", DEFENDED,
                             ids=[m[0] for m in DEFENDED])
    def test_the_defended_failures_never_produce_one(self, site, label,
                                                     injections):
        """The other direction, and the more valuable one.

        These are the two failure modes `core/` is built to refuse outright. If
        either ever starts putting a name on the wrong body, the roll-call
        becomes the only defence against a failure the design claims to handle
        itself -- and nothing would say so.
        """
        for seed in range(1, 21):
            plan = self._plan(site, seed, injections)
            fooled = self._camera_only_false_accepts(plan.agents,
                                                     run_drill(plan))
            assert not fooled, (
                f"{label} at seed {seed} accounted for {sorted(fooled)}, who "
                "never reached an assembly point. This mix is supposed to be "
                "refused structurally, not caught downstream")

    def test_the_cameras_alone_never_report_it(self, site):
        """And the other half of the claim: `core/` cannot see this.

        Asserted so that if a future change *does* let the fold detect a stable
        misidentification, somebody has to come here and say so -- the file's
        opening paragraph would then be wrong.
        """
        label, seed, injections = FOOLS_THE_CAMERAS[-1]
        result = run_drill(self._plan(site, seed, injections))
        agents = population(site, 80, seed)
        fooled = self._camera_only_false_accepts(agents, result)
        assert fooled, "this mix is pinned because it fools the cameras"
        for ref in fooled:
            decision = result.decisions[ref]
            assert decision.state is AccountabilityState.ACCOUNTED
            # No conflict, no doubt, nothing for an operator to notice.
            assert not decision.blockers, (
                f"{ref} carries {decision.blockers}: the fold saw something "
                "after all, and this file's opening claim needs revisiting")
