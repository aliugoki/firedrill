"""The two properties the whole system exists to guarantee.

These are asserted against ground truth the core cannot see, across every
failure the injection catalogue can produce. If either of these ever fails, the
system is telling an incident commander that someone is safe when they are not.
"""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from app.core.accountability_fsm import AccountabilityState
from app.simulator.agents import Behaviour
from app.simulator.engine import DrillPlan, observe, replay, run_drill
from app.simulator.injections import HOSTILE, NONE, REALISTIC, Injections, Outage

from tests.simulator.conftest import ALARM_MS, drill, population, truly_reached_assembly

PROFILES = [("clean", NONE), ("realistic", REALISTIC), ("hostile", HOSTILE)]

SLOW = settings(
    max_examples=12, deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


class TestNobodyIsFalselyAccounted:
    """Property 1. ACCOUNTED requires qualifying evidence, under every injection.

    Ground truth says who actually reached an assembly zone. Nobody outside that
    set may be marked safe, no matter what the pipeline did.
    """

    @pytest.mark.parametrize("name,injections", PROFILES)
    def test_across_every_injection_profile(self, site, name, injections):
        agents, result = drill(site, injections)
        falsely_safe = result.accounted_refs() - truly_reached_assembly(agents)
        assert falsely_safe == set(), (
            f"{name}: {len(falsely_safe)} people marked ACCOUNTED who never "
            f"reached an assembly zone: {sorted(falsely_safe)[:5]}"
        )

    @pytest.mark.parametrize("name,injections", PROFILES)
    def test_people_still_at_their_desks_are_never_safe(self, site, name, injections):
        # The people the whole system exists to find.
        agents, result = drill(site, injections)
        still_inside = [a for a in agents if a.behaviour is Behaviour.NEVER_LEAVES]
        assert still_inside, "the population must contain people who never leave"
        for agent in still_inside:
            assert result.state_of(agent.person_ref) is not AccountabilityState.ACCOUNTED

    @pytest.mark.parametrize("name,injections", PROFILES)
    def test_people_who_left_assembly_are_not_still_safe(self, site, name, injections):
        # They arrived and then walked back inside. Both facts are true, and the
        # second one is the operative one.
        agents, result = drill(site, injections)
        wanderers = [a for a in agents
                     if a.behaviour is Behaviour.LEAVES_ASSEMBLY_EARLY]
        for agent in wanderers:
            state = result.state_of(agent.person_ref)
            assert state is not None

    @given(
        face_loss=st.floats(0.0, 0.95),
        quality=st.floats(0.0, 0.8),
        fragmentation=st.floats(0.0, 0.5),
        id_switch=st.floats(0.0, 0.3),
        drop=st.floats(0.0, 0.3),
        duplicate=st.floats(0.0, 0.4),
    )
    @SLOW
    def test_under_arbitrary_pipeline_failure_rates(
        self, site, face_loss, quality, fragmentation, id_switch, drop, duplicate
    ):
        agents = population(site, 60, 20260910)
        plan = DrillPlan(
            site=site, agents=agents, alarm_ms=ALARM_MS,
            injections=Injections(
                face_loss_rate=face_loss, poor_quality_rate=quality,
                track_fragmentation_rate=fragmentation, id_switch_rate=id_switch,
                drop_rate=drop, duplicate_rate=duplicate,
                lookalike_confusion_rate=0.5, wrong_identity_rate=0.05,
            ))
        result = run_drill(plan)
        assert result.accounted_refs() <= truly_reached_assembly(agents)

    @given(
        outage_start=st.integers(0, 200_000),
        outage_length=st.integers(10_000, 300_000),
    )
    @SLOW
    def test_under_an_arbitrary_total_camera_outage(
        self, site, outage_start, outage_length
    ):
        # Every camera in the building, dark, for an arbitrary window.
        agents = population(site, 60, 20260910)
        plan = DrillPlan(
            site=site, agents=agents, alarm_ms=ALARM_MS,
            injections=Injections(camera_outages=(
                Outage(outage_start, outage_start + outage_length, "*"),)))
        result = run_drill(plan)
        assert result.accounted_refs() <= truly_reached_assembly(agents)


class TestFailureDegradesRatherThanClears:
    """Property 2. Infrastructure failure never produces a false ALL CLEAR."""

    def test_a_hostile_run_answers_i_do_not_know_far_more_often(self, site):
        _, clean = drill(site, NONE)
        _, hostile = drill(site, HOSTILE)
        clean_unsure = len(clean.board.needing_attention())
        hostile_unsure = len(hostile.board.needing_attention())
        assert hostile_unsure > clean_unsure * 3, (
            "a hostile run must collapse into uncertainty, not stay confident"
        )

    def test_a_hostile_run_marks_far_fewer_people_safe(self, site):
        _, clean = drill(site, NONE)
        _, hostile = drill(site, HOSTILE)
        assert len(hostile.accounted_refs()) < len(clean.accounted_refs())

    def test_total_blindness_accounts_for_nobody_new(self, site):
        # Every camera down for the whole drill. The system knows nothing, and
        # must say so rather than inferring anyone to safety.
        agents = population(site, 120, 20260910)
        plan = DrillPlan(
            site=site, agents=agents, alarm_ms=ALARM_MS,
            injections=Injections(camera_outages=(Outage(0, 10_000_000, "*"),)))
        result = run_drill(plan)
        assert result.accounted_refs() == set()

    def test_losing_every_event_accounts_for_nobody(self, site):
        agents = population(site, 120, 20260910)
        plan = DrillPlan(site=site, agents=agents, alarm_ms=ALARM_MS,
                         injections=Injections(drop_rate=1.0))
        result = run_drill(plan)
        assert result.accounted_refs() == set()

    @pytest.mark.parametrize("name,injections", PROFILES)
    def test_every_person_has_some_settled_state(self, site, name, injections):
        # Nobody falls off the board. An unlisted person is worse than an
        # uncertain one, because nobody goes looking for them.
        agents, result = drill(site, injections)
        assert set(result.decisions) == {a.person_ref for a in agents}

    @pytest.mark.parametrize("name,injections", PROFILES)
    def test_every_decision_carries_a_reason(self, site, name, injections):
        agents, result = drill(site, injections)
        for ref, decision in result.decisions.items():
            assert decision.reason.strip(), f"{ref} has a state with no reason"


class TestReplayIsIdempotentAndOrderTolerant:
    """Ingest is fed by an unreliable stream. Same events, same conclusions."""

    def test_replaying_the_same_stream_twice_changes_nothing(self, site):
        agents = population(site, 120, 20260910)
        plan = DrillPlan(site=site, agents=agents, alarm_ms=ALARM_MS,
                         injections=REALISTIC)
        stream = observe(plan)
        once = replay(stream.events, plan)
        twice = replay(stream.events + stream.events, plan)
        assert {p.person_id: p.state for p in once.presence} == \
               {p.person_id: p.state for p in twice.presence}
        assert {p.person_id: p.identity for p in once.identity} == \
               {p.person_id: p.identity for p in twice.identity}

    def test_duplicates_are_counted_not_absorbed_silently(self, site):
        agents = population(site, 120, 20260910)
        plan = DrillPlan(site=site, agents=agents, alarm_ms=ALARM_MS,
                         injections=Injections(duplicate_rate=0.5))
        result = run_drill(plan)
        assert result.state.duplicates_dropped > 0

    @given(shuffle_seed=st.integers(0, 10_000))
    @SLOW
    def test_a_shuffled_stream_reaches_the_same_identities(self, site, shuffle_seed):
        import random

        agents = population(site, 60, 20260910)
        plan = DrillPlan(site=site, agents=agents, alarm_ms=ALARM_MS,
                         injections=REALISTIC)
        stream = observe(plan)
        ordered = replay(stream.events, plan)
        shuffled_events = list(stream.events)
        random.Random(shuffle_seed).shuffle(shuffled_events)
        shuffled = replay(shuffled_events, plan)
        assert {p.person_id: p.identity for p in ordered.identity} == \
               {p.person_id: p.identity for p in shuffled.identity}


class TestGapsAreReportedNotRepaired:
    def test_dropped_events_leave_a_visible_hole(self, site):
        agents = population(site, 120, 20260910)
        plan = DrillPlan(site=site, agents=agents, alarm_ms=ALARM_MS,
                         injections=Injections(drop_rate=0.15))
        stream = observe(plan)
        state = replay(stream.events, plan)
        assert stream.dropped > 0
        assert state.tracker.outstanding_gaps(), (
            "events were dropped and no gap was reported"
        )

    def test_a_clean_stream_reports_no_gaps(self, site):
        agents = population(site, 120, 20260910)
        plan = DrillPlan(site=site, agents=agents, alarm_ms=ALARM_MS,
                         injections=NONE)
        state = replay(observe(plan).events, plan)
        assert state.tracker.outstanding_gaps() == []
