"""One scenario per failure the brief names, asserting what should happen.

The property tests prove nobody is falsely accounted under everything at once.
These prove each individual failure is handled *the way it should be* rather
than merely handled safely: a face loss must degrade identity and nothing else,
a camera outage must suspend the grace clock rather than declare people lost,
and a look-alike pair must produce a conflict rather than a coin-flip winner.
"""

from __future__ import annotations

import pytest

from app.core.accountability_fsm import AccountabilityState
from app.core.identity_fsm import IdentityState
from app.core.ledger import EvidenceKind
from app.core.presence_fsm import PresenceState
from app.simulator.agents import Behaviour, build_population, walk_all
from app.simulator.engine import DrillPlan, observe, replay, run_drill
from app.simulator.injections import Injections, NONE, Outage

from tests.simulator.conftest import (
    ALARM_MS, drill, falsely_accounted, population, truly_reached_assembly)


def plan_with(site, injections, *, count=120, seed=20260910) -> DrillPlan:
    return DrillPlan(site=site, agents=population(site, count, seed),
                     alarm_ms=ALARM_MS, injections=injections, seed=seed)


class TestBaseline:
    def test_a_clean_drill_finds_almost_everyone_it_can(self, site):
        """A clean run that cannot account for people proves nothing about the
        injected runs, so this is the floor everything else is measured from.

        The denominator is people the cameras could possibly account for:
        enrolled employees who actually reached an assembly zone. Visitors and
        never-enrolled employees have no face in the gallery and can only be
        accounted for by a warden, so counting them makes the threshold a
        statement about the roster's enrolment rate rather than about the
        pipeline.
        """
        agents, result = drill(site, NONE)
        reachable = {
            a.person_ref for a in agents
            if a.reached_assembly and a.has_gallery_entry and not a.is_visitor
        }
        assert reachable
        found = result.accounted_refs() & reachable
        ratio = len(found) / len(reachable)
        assert ratio >= 0.9, (
            f"a clean run accounted for only {ratio:.1%} of the people it had "
            f"every means to identify"
        )

    def test_nobody_with_a_gallery_entry_is_quietly_dropped(self, site):
        # The other half of the same statement. Someone enrolled who reached
        # assembly and was still not accounted for must at least be on the
        # board in a state that sends a human to check.
        agents, result = drill(site, NONE)
        by_ref = {a.person_ref: a for a in agents}
        for ref in truly_reached_assembly(agents) - result.accounted_refs():
            agent = by_ref[ref]
            if agent.has_gallery_entry and not agent.is_visitor:
                assert result.decisions[ref].needs_attention, (
                    f"{ref} was enrolled, reached assembly, was not accounted "
                    "for, and nothing asks anyone to look at them"
                )

    def test_a_clean_drill_produces_a_usable_p95(self, site):
        _, result = drill(site, NONE)
        assert result.timing.building.p95 is not None
        assert result.timing.building.is_reliable is True


class TestFaceLoss:
    """A face that was not detected says nothing about who the person is."""

    def test_total_face_loss_leaves_everyone_unidentified(self, site):
        result = run_drill(plan_with(site, Injections(face_loss_rate=1.0)))
        assert all(p.state is IdentityState.UNKNOWN for p in result.state.identity)

    def test_total_face_loss_accounts_for_nobody(self, site):
        result = run_drill(plan_with(site, Injections(face_loss_rate=1.0)))
        assert result.accounted_refs() == set()

    def test_people_are_still_tracked_without_a_face(self, site):
        # Presence is independent of identity. Somebody is at the muster point,
        # even if we cannot say who.
        result = run_drill(plan_with(site, Injections(face_loss_rate=1.0)))
        assert any(p.was_at_assembly for p in result.state.presence)

    def test_face_loss_is_recorded_as_unavailable_not_absence(self, site):
        result = run_drill(plan_with(site, Injections(face_loss_rate=1.0)))
        kinds = {e.kind for e in result.state.ledger}
        assert EvidenceKind.FACE_UNAVAILABLE in kinds

    def test_heavy_but_partial_face_loss_still_identifies_people(self, site):
        result = run_drill(plan_with(site, Injections(face_loss_rate=0.5)))
        confirmed = [p for p in result.state.identity
                     if p.state is IdentityState.CONFIRMED]
        assert confirmed, "half the faces is still enough to identify somebody"


class TestPoorQualityAndPose:
    def test_a_blurred_face_never_votes(self, site):
        result = run_drill(plan_with(site, Injections(poor_quality_rate=1.0)))
        assert all(p.state is IdentityState.UNKNOWN for p in result.state.identity)

    def test_a_turned_head_never_votes(self, site):
        result = run_drill(plan_with(site, Injections(bad_pose_rate=1.0)))
        assert all(p.state is IdentityState.UNKNOWN for p in result.state.identity)

    def test_neither_produces_a_false_accounting(self, site):
        for injections in (Injections(poor_quality_rate=1.0),
                           Injections(bad_pose_rate=1.0)):
            result = run_drill(plan_with(site, injections))
            assert result.accounted_refs() == set()


class TestTrackFragmentation:
    """One person becomes several global ids. Their evidence is genuinely split."""

    def test_fragmentation_creates_more_tracked_entities_than_people(self, site):
        result = run_drill(plan_with(site, Injections(track_fragmentation_rate=0.4)))
        assert len(result.state.presence) > len(result.decisions)

    def test_fragmentation_never_invents_a_safe_person(self, site):
        agents, _ = drill(site, NONE)
        result = run_drill(plan_with(site, Injections(track_fragmentation_rate=0.4)))
        assert falsely_accounted(result, agents) == set()

    def test_fragments_stay_visible_rather_than_being_merged_away(self, site):
        # Merging fragments back together would hide exactly the damage
        # fragmentation does to the evidence.
        result = run_drill(plan_with(site, Injections(track_fragmentation_rate=0.4)))
        fragment_ids = [p.person_id for p in result.state.presence
                        if "#" in p.person_id]
        assert fragment_ids


class TestIdentitySwitchAndMisidentification:
    def test_an_outright_wrong_match_does_not_account_for_the_wrong_person(self, site):
        agents, _ = drill(site, NONE)
        result = run_drill(plan_with(site, Injections(wrong_identity_rate=1.0)))
        assert falsely_accounted(result, agents) == set()

    def test_a_wrong_match_names_a_real_employee(self, site):
        """Otherwise it is not the failure it claims to be.

        This injection used to emit `EMP-9999`, a gallery id nobody holds, so a
        confident wrong match could never be mistaken for a person on the
        roster and the most dangerous entry in the catalogue could not be
        produced at all. What it costs, and what catches it, is
        `tests/simulator/test_misidentification.py`.
        """
        agents, _ = drill(site, NONE)
        on_the_roster = {a.emp_id for a in agents if a.emp_id}
        plan = plan_with(site, Injections(wrong_identity_rate=1.0))
        stream = observe(plan)

        assert stream.misidentified_as
        assert set(stream.misidentified_as.values()) <= on_the_roster
        # And nobody is confused with themselves, which would be no failure.
        assert all(wrong != right
                   for right, wrong in stream.misidentified_as.items())

    def test_the_confusion_is_stable_for_one_person(self, site):
        # A different wrong name each frame is noise the identity machine
        # dismisses in one line, because no wrong name ever reaches min_votes.
        # A real misidentification is the same wrong name every time.
        from app.core.events import EventType

        plan = plan_with(site, Injections(wrong_identity_rate=1.0))
        stream = observe(plan)
        assert stream.misidentified_as

        # Only the misidentified. An unenrolled person is matched against the
        # nearest gallery entry, which really does differ frame to frame, and
        # that is a separate and deliberate behaviour.
        wrong_name = {f"emp:{emp}": name
                      for emp, name in stream.misidentified_as.items()}
        claimed: dict[str, set] = {}
        for event in stream.events:
            if event.type is not EventType.FACE_OBSERVED:
                continue
            person = stream.who(event.subject, event.ts_ms)
            if person in wrong_name:
                claimed.setdefault(person, set()).add(
                    event.payload["candidate_id"])
        assert claimed
        for person, names in claimed.items():
            assert names == {wrong_name[person]}, (
                f"{person} was matched as {sorted(names)}")

    def test_an_unenrolled_face_is_still_matched_against_the_gallery(self, site):
        # A matcher does not know who is enrolled. It returns its nearest entry
        # at a poor score, and how often that poor score is admitted anyway is
        # the false-accept rate the calibration harness exists to measure.
        from app.core.events import EventType

        p = plan_with(site, NONE)
        stream = observe(p)
        unenrolled = [e for e in stream.events
                      if e.type is EventType.FACE_OBSERVED
                      and e.payload.get("enrolled") is False]
        assert unenrolled, "unenrolled people must still produce face matches"
        assert all(e.payload["candidate_id"] for e in unenrolled)

    def test_an_unenrolled_face_is_subject_to_the_same_detector_failures(self, site):
        # Detection failure, blur and pose do not care who is in the gallery.
        from app.core.events import EventType

        p = plan_with(site, Injections(face_loss_rate=1.0))
        stream = observe(p)
        assert not [e for e in stream.events if e.type is EventType.FACE_OBSERVED]


class TestLookAlikes:
    """Two people the gallery cannot separate. The answer is a conflict."""

    def test_confusion_produces_conflicts_not_coin_flips(self, site):
        result = run_drill(plan_with(
            site, Injections(lookalike_confusion_rate=1.0), count=120))
        conflicted = [p for p in result.state.identity
                      if p.state is IdentityState.CONFLICT]
        assert conflicted, "look-alike confusion must surface as CONFLICT"

    def test_a_conflicted_person_is_sent_to_a_human(self, site):
        result = run_drill(plan_with(
            site, Injections(lookalike_confusion_rate=1.0), count=120))
        conflicted = {p.person_id for p in result.state.identity
                      if p.state is IdentityState.CONFLICT}
        assert conflicted
        states = {d.state for ref, d in result.decisions.items()}
        assert AccountabilityState.MANUAL_VERIFICATION_REQUIRED in states

    def test_the_conflict_names_both_candidates(self, site):
        result = run_drill(plan_with(
            site, Injections(lookalike_confusion_rate=1.0), count=120))
        conflicted = [p for p in result.state.identity
                      if p.state is IdentityState.CONFLICT]
        assert all(len(p.conflict_with) >= 2 for p in conflicted)


class TestOcclusion:
    def test_being_hidden_behind_someone_is_not_being_absent(self, site):
        result = run_drill(plan_with(site, Injections(occlusion_rate=0.5)))
        agents, _ = drill(site, NONE)
        assert falsely_accounted(result, agents) == set()

    def test_total_occlusion_observes_nobody(self, site):
        result = run_drill(plan_with(site, Injections(occlusion_rate=1.0)))
        assert len(result.state.presence) == 0
        assert result.accounted_refs() == set()


class TestBlindZonesAndUncoveredExits:
    def test_the_stairwells_are_genuinely_uncovered(self, site):
        assert site.is_blind("floor-3", (0.9, 0.5)) is True

    def test_the_fire_exit_has_no_camera(self, site):
        assert site.cameras_seeing("floor-1", (0.75, 1.05)) == []

    def test_the_main_exit_is_double_covered(self, site):
        # The same person seen twice at once must not become two people.
        assert len(site.cameras_seeing("floor-1", (0.3, 1.05))) == 2

    def test_double_coverage_does_not_duplicate_people(self, site):
        agents = population(site, 120, 20260910)
        result = run_drill(plan_with(site, NONE))
        real_ids = {p.person_id for p in result.state.presence if "#" not in p.person_id}
        assert len(real_ids) <= len(agents)


class TestTransportFailures:
    def test_duplicates_are_dropped_and_counted(self, site):
        result = run_drill(plan_with(site, Injections(duplicate_rate=0.5)))
        assert result.state.duplicates_dropped > 0

    def test_duplicates_do_not_change_the_outcome(self, site):
        clean = run_drill(plan_with(site, NONE))
        duped = run_drill(plan_with(site, Injections(duplicate_rate=0.5)))
        assert duped.accounted_refs() == clean.accounted_refs()

    def test_reordering_does_not_change_identities(self, site):
        clean = run_drill(plan_with(site, NONE))
        shuffled = run_drill(plan_with(site, Injections(reorder_rate=0.4)))
        assert {p.person_id: p.identity for p in shuffled.state.identity} == \
               {p.person_id: p.identity for p in clean.state.identity}

    def test_delayed_events_still_arrive_and_count(self, site):
        delayed = run_drill(plan_with(site, Injections(delay_rate=0.4)))
        clean = run_drill(plan_with(site, NONE))
        assert delayed.accounted_refs() == clean.accounted_refs()

    def test_dropped_events_leave_a_reported_gap(self, site):
        p = plan_with(site, Injections(drop_rate=0.2))
        state = replay(observe(p).events, p)
        assert state.tracker.outstanding_gaps()

    def test_a_gap_becomes_evidence_against_certainty(self, site):
        p = plan_with(site, Injections(drop_rate=0.2))
        state = replay(observe(p).events, p)
        assert any(e.kind is EvidenceKind.SEQUENCE_GAP for e in state.ledger)


class TestCameraOutage:
    def test_one_camera_down_does_not_lose_the_people_it_watched(self, site):
        # Invariant 8: our own outage is not evidence about a person, so the
        # grace clock stops rather than ageing everyone into LOST.
        outage = Injections(camera_outages=(Outage(0, 600_000, "cam-floor-3-open"),))
        result = run_drill(plan_with(site, outage))
        agents, _ = drill(site, NONE)
        assert falsely_accounted(result, agents) == set()

    def test_every_camera_down_accounts_for_nobody(self, site):
        result = run_drill(plan_with(
            site, Injections(camera_outages=(Outage(0, 10_000_000, "*"),))))
        assert result.accounted_refs() == set()

    def test_a_camera_failure_is_recorded_as_an_event(self, site):
        p = plan_with(site, Injections(
            camera_outages=(Outage(10_000, 100_000, "cam-floor-2-open"),)))
        stream = observe(p)
        from app.core.events import EventType

        types = {e.type for e in stream.events}
        assert EventType.CAMERA_FAILURE in types
        assert EventType.CAMERA_RECOVERED in types


class TestInfrastructureOutage:
    """Not every outage is the same kind of outage.

    An earlier version of these tests assumed Redis, Postgres and the network
    all fail identically, and asserted all three force manual verification. Two
    of the three do not, and treating them alike was wrong in a way that
    mattered: it would make the system blind itself over a database it does not
    need to see through.
    """

    def test_losing_the_event_bus_blinds_the_system(self, site):
        # Redis carries the observations. Without it the system sees nothing.
        result = run_drill(plan_with(
            site, Injections(redis_outages=(Outage(0, 10_000_000),))))
        assert result.state.health.is_blind is True

    @pytest.mark.parametrize("field,component", [
        ("db_outages", "DATABASE"),
        ("network_partitions", "CENTRAL_LINK"),
    ])
    def test_losing_storage_or_central_does_not_blind_the_system(
        self, site, field, component
    ):
        # The core holds its state in memory and the projections are
        # recomputable from the ledger. Losing Postgres costs durability;
        # losing central costs reporting. Neither costs sight.
        result = run_drill(plan_with(
            site, Injections(**{field: (Outage(0, 10_000_000),)})))
        assert result.state.health.is_degraded is True
        assert result.state.health.is_blind is False

    @pytest.mark.parametrize("field", ["redis_outages", "db_outages",
                                       "network_partitions"])
    def test_every_outage_is_recorded_with_its_own_component(self, site, field):
        result = run_drill(plan_with(
            site, Injections(**{field: (Outage(30_000, 90_000),)})))
        assert len(result.state.health.degradations) == 1

    @pytest.mark.parametrize("field", ["redis_outages", "db_outages",
                                       "network_partitions"])
    def test_an_outage_never_produces_a_false_all_clear(self, site, field):
        agents, _ = drill(site, NONE)
        injections = Injections(**{field: (Outage(0, 10_000_000),)})
        result = run_drill(plan_with(site, injections))
        assert falsely_accounted(result, agents) == set()


class TestPeopleWhoAreNotEmployees:
    def test_visitors_are_never_identified_by_face(self, site):
        agents, result = drill(site, NONE)
        visitors = [a for a in agents if a.is_visitor]
        assert visitors
        for visitor in visitors:
            assert result.state_of(visitor.person_ref) is not \
                AccountabilityState.ACCOUNTED

    def test_an_employee_with_no_gallery_entry_needs_a_human(self, site):
        agents, result = drill(site, NONE)
        unenrolled = [a for a in agents
                      if not a.is_visitor and not a.has_gallery_entry]
        assert unenrolled
        for agent in unenrolled:
            assert result.state_of(agent.person_ref) is not \
                AccountabilityState.ACCOUNTED


class TestPeopleWhoWereNeverThere:
    def test_someone_absent_from_site_is_not_accounted(self, site):
        agents, result = drill(site, NONE)
        absent = [a for a in agents if a.behaviour is Behaviour.ABSENT_FROM_SITE]
        assert absent
        for agent in absent:
            assert result.state_of(agent.person_ref) is not \
                AccountabilityState.ACCOUNTED

    def test_someone_absent_from_site_is_not_hidden_either(self, site):
        # They must appear on the board so a human can reconcile them against
        # the roster, rather than silently disappearing from the count.
        agents, result = drill(site, NONE)
        absent = [a for a in agents if a.behaviour is Behaviour.ABSENT_FROM_SITE]
        for agent in absent:
            assert agent.person_ref in result.decisions

    def test_someone_still_at_their_desk_is_reported(self, site):
        agents, result = drill(site, NONE)
        inside = [a for a in agents if a.behaviour is Behaviour.NEVER_LEAVES]
        assert inside
        for agent in inside:
            decision = result.decisions[agent.person_ref]
            assert decision.state in (AccountabilityState.UNACCOUNTED,
                                      AccountabilityState.NOT_EVACUATED,
                                      AccountabilityState.UNCERTAIN,
                                      AccountabilityState.MANUAL_VERIFICATION_REQUIRED)

    def test_the_reason_says_where_to_look_for_them(self, site):
        agents, result = drill(site, NONE)
        inside = [a for a in agents if a.behaviour is Behaviour.NEVER_LEAVES]
        found_a_location = False
        for agent in inside:
            reason = result.decisions[agent.person_ref].reason
            if "last seen" in reason or "never observed" in reason:
                found_a_location = True
        assert found_a_location


class TestExplainability:
    def test_every_accounted_person_can_be_explained(self, site):
        # Invariant 6. "Why was this person marked safe?" must have an answer.
        _, result = drill(site, NONE)
        accounted = list(result.accounted_refs())[:20]
        assert accounted
        for ref in accounted:
            decision = result.decisions[ref]
            assert decision.qualifying_evidence, (
                f"{ref} is ACCOUNTED with no qualifying evidence listed"
            )

    def test_the_ledger_narrates_a_tracked_person(self, site):
        _, result = drill(site, NONE)
        tracked = [p.person_id for p in result.state.presence
                   if p.state is PresenceState.ASSEMBLY_PRESENT]
        assert tracked
        lines = result.state.ledger.explain(tracked[0]).narrate()
        assert len(lines) > 1
