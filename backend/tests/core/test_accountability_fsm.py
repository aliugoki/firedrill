"""Accountability derivation.

The central assertion of the whole system lives here: ACCOUNTED has exactly two
routes in, and no failure, no timeout, no confident-looking score opens a third.
"""

import pytest

from app.core.accountability_fsm import (
    PROVISIONAL_CONFIG,
    AccountabilityBoard,
    AccountabilityConfig,
    AccountabilityState,
    Context,
    WardenEvidence,
    derive,
)
from app.core.identity_fsm import (
    FaceObservation,
    IdentityConfig,
    PersonIdentity,
)
from app.core.presence_fsm import (
    PersonPresence,
    PresenceConfig,
    ZoneKind,
    ZoneSighting,
)

T0 = 1_788_000_000_000

IDENTITY_CFG = IdentityConfig(
    score_threshold=0.35, min_margin=0.05, min_votes=3, conflict_votes=3,
    min_face_quality=0.5, max_pose_deviation_deg=45.0,
    min_track_confidence=0.5, identity_expiry_ms=30_000,
)
PRESENCE_CFG = PresenceConfig(
    t_lost_ms=15_000, t_lost_blind_ms=45_000, assembly_dwell_ms=5_000)


def good(candidate="EMP-1", ts_ms=T0) -> FaceObservation:
    return FaceObservation(ts_ms=ts_ms, candidate_id=candidate, score=0.80,
                           margin=0.30, quality=0.9, pose_deviation_deg=5.0,
                           track_confidence=0.9)


def confirmed_identity(candidate="EMP-1") -> PersonIdentity:
    p = PersonIdentity(person_id="gp-1", config=IDENTITY_CFG)
    for i in range(3):
        p.observe(good(candidate, ts_ms=T0 + i * 100))
    return p


def conflicted_identity() -> PersonIdentity:
    p = PersonIdentity(person_id="gp-1", config=IDENTITY_CFG)
    for i in range(3):
        p.observe(good("EMP-1", ts_ms=T0 + i * 100))
    for i in range(3):
        p.observe(good("EMP-2", ts_ms=T0 + 1_000 + i * 100))
    return p


def at_assembly() -> PersonPresence:
    p = PersonPresence(person_id="gp-1", config=PRESENCE_CFG)
    p.observe(ZoneSighting(ts_ms=T0, zone_id="assembly-north",
                           zone_kind=ZoneKind.ASSEMBLY, camera_id="cam-9"))
    p.observe(ZoneSighting(ts_ms=T0 + 6_000, zone_id="assembly-north",
                           zone_kind=ZoneKind.ASSEMBLY, camera_id="cam-9"))
    return p


def inside(zone_kind=ZoneKind.FLOOR, zone_id="floor-3") -> PersonPresence:
    p = PersonPresence(person_id="gp-1", config=PRESENCE_CFG)
    p.observe(ZoneSighting(ts_ms=T0, zone_id=zone_id, zone_kind=zone_kind,
                           camera_id="cam-3"))
    return p


def ctx(**overrides) -> Context:
    base = dict(person_id="gp-1", drill_elapsed_ms=30_000, is_expected=True,
                config=PROVISIONAL_CONFIG)
    base.update(overrides)
    return Context(**base)


class TestTheOnlyTwoRoutesToAccounted:
    def test_assembly_presence_plus_confirmed_identity(self):
        d = derive(ctx(presence=at_assembly(), identity=confirmed_identity()))
        assert d.state is AccountabilityState.ACCOUNTED
        assert "assembly-zone presence" in d.qualifying_evidence

    def test_warden_confirmation_at_an_assembly_zone(self):
        d = derive(ctx(warden=WardenEvidence(
            confirmed=True, at_assembly_zone="assembly-north", warden_id="warden-7")))
        assert d.state is AccountabilityState.ACCOUNTED
        assert "warden-7" in d.reason

    def test_assembly_presence_alone_is_not_enough(self):
        # Somebody is at the muster point. Who, we do not know.
        d = derive(ctx(presence=at_assembly(), identity=None))
        assert d.state is AccountabilityState.UNCERTAIN

    def test_confirmed_identity_alone_is_not_enough(self):
        # We know exactly who they are, and they are still inside.
        d = derive(ctx(presence=inside(), identity=confirmed_identity()))
        assert d.state is not AccountabilityState.ACCOUNTED

    def test_a_warden_confirmation_away_from_assembly_is_not_enough(self):
        # Recognising a colleague in a corridor is identity evidence, not
        # evidence they got out.
        d = derive(ctx(presence=inside(), identity=confirmed_identity(),
                       warden=WardenEvidence(confirmed=True, warden_id="warden-7")))
        assert d.state is not AccountabilityState.ACCOUNTED

    def test_a_high_score_does_not_open_a_third_route(self):
        p = PersonPresence(person_id="gp-1", config=PRESENCE_CFG)
        p.observe(ZoneSighting(ts_ms=T0, zone_id="floor-3",
                               zone_kind=ZoneKind.FLOOR, camera_id="cam-3"))
        identity = PersonIdentity(person_id="gp-1", config=IDENTITY_CFG)
        for i in range(50):
            identity.observe(FaceObservation(
                ts_ms=T0 + i * 100, candidate_id="EMP-1", score=0.999,
                margin=0.9, quality=1.0, track_confidence=1.0))
        assert derive(ctx(presence=p, identity=identity)).state \
            is not AccountabilityState.ACCOUNTED


class TestIdentityStillCountsWhenTheFaceIsGone:
    def test_temporarily_unavailable_still_supports_accounted(self):
        # Invariant 1. They were identified at the muster point and then turned
        # away from the camera. That is not a reason to un-account them.
        identity = confirmed_identity()
        identity.tick(T0 + 120_000)
        d = derive(ctx(presence=at_assembly(), identity=identity))
        assert d.state is AccountabilityState.ACCOUNTED
        assert "face not currently visible" in d.reason

    def test_arrival_survives_the_track_being_lost_afterwards(self):
        presence = at_assembly()
        presence.track_lost(T0 + 30_000)
        presence.tick(T0 + 120_000)
        d = derive(ctx(presence=presence, identity=confirmed_identity()))
        assert d.state is AccountabilityState.ACCOUNTED


class TestConflictOutranksEverything:
    def test_a_conflicted_identity_at_assembly_is_not_accounted(self):
        # Invariant 3. Somebody is safe. Not necessarily this person.
        d = derive(ctx(presence=at_assembly(), identity=conflicted_identity()))
        assert d.state is AccountabilityState.MANUAL_VERIFICATION_REQUIRED
        assert "will not choose" in d.reason

    def test_the_contenders_are_named_in_the_reason(self):
        d = derive(ctx(presence=at_assembly(), identity=conflicted_identity()))
        assert "EMP-1" in d.reason and "EMP-2" in d.reason

    def test_a_warden_rejection_forces_verification(self):
        d = derive(ctx(presence=at_assembly(), identity=confirmed_identity(),
                       warden=WardenEvidence(rejected=True, warden_id="warden-7")))
        assert d.state is AccountabilityState.MANUAL_VERIFICATION_REQUIRED


class TestDegradation:
    """Invariant 8. Never a false ALL CLEAR, and never a confident guess."""

    def test_degradation_forces_manual_verification(self):
        d = derive(ctx(presence=inside(), identity=confirmed_identity(),
                       system_degraded=True, degraded_reason="cam-3 offline"))
        assert d.state is AccountabilityState.MANUAL_VERIFICATION_REQUIRED
        assert "cam-3 offline" in d.reason

    def test_a_persons_own_degraded_camera_is_enough(self):
        presence = inside()
        presence.mark_degraded(T0 + 1_000, "camera offline")
        d = derive(ctx(presence=presence, identity=confirmed_identity()))
        assert d.state is AccountabilityState.MANUAL_VERIFICATION_REQUIRED

    def test_degradation_never_produces_accounted(self):
        for degraded in (True, False):
            d = derive(ctx(presence=inside(), identity=None,
                           system_degraded=degraded, drill_elapsed_ms=600_000))
            assert d.state is not AccountabilityState.ACCOUNTED

    def test_a_warden_still_outranks_a_broken_camera(self):
        # A human's eyes do not stop working because a camera did.
        d = derive(ctx(system_degraded=True, degraded_reason="whole site offline",
                       warden=WardenEvidence(confirmed=True,
                                             at_assembly_zone="assembly-north",
                                             warden_id="warden-7")))
        assert d.state is AccountabilityState.ACCOUNTED

    def test_degradation_does_not_suppress_an_earlier_observation(self):
        # Someone confirmed at assembly before the outage stays accounted. The
        # outage makes future silence uninformative, not the past.
        presence = at_assembly()
        d = derive(ctx(presence=presence, identity=confirmed_identity(),
                       system_degraded=True))
        assert d.state is AccountabilityState.ACCOUNTED


class TestStillInside:
    def test_moving_through_an_exit_is_evacuating(self):
        d = derive(ctx(presence=inside(ZoneKind.EXIT, "exit-west"),
                       identity=confirmed_identity()))
        assert d.state is AccountabilityState.EVACUATING

    def test_inside_early_in_the_drill_is_not_evacuated(self):
        d = derive(ctx(presence=inside(), identity=confirmed_identity(),
                       drill_elapsed_ms=10_000))
        assert d.state is AccountabilityState.NOT_EVACUATED

    def test_inside_past_the_deadline_is_unaccounted(self):
        d = derive(ctx(presence=inside(), identity=confirmed_identity(),
                       drill_elapsed_ms=600_000))
        assert d.state is AccountabilityState.UNACCOUNTED

    def test_the_reason_says_where_to_go_and_look(self):
        d = derive(ctx(presence=inside(ZoneKind.FLOOR, "floor-3"),
                       identity=confirmed_identity(), drill_elapsed_ms=600_000))
        assert "floor-3" in d.reason
        assert "cam-3" in d.reason

    def test_a_stale_track_is_uncertain_not_unaccounted(self):
        # We stopped seeing them. That is a statement about the cameras.
        presence = inside()
        presence.track_lost(T0 + 1_000)
        presence.tick(T0 + 120_000)
        d = derive(ctx(presence=presence, identity=confirmed_identity(),
                       drill_elapsed_ms=600_000))
        assert d.state is AccountabilityState.UNCERTAIN

    def test_briefly_unobserved_is_still_evacuating(self):
        presence = inside(ZoneKind.EXIT, "exit-west")
        presence.track_lost(T0 + 1_000)
        d = derive(ctx(presence=presence, identity=confirmed_identity()))
        assert d.state is AccountabilityState.EVACUATING

    def test_never_observed_past_the_deadline_is_unaccounted(self):
        d = derive(ctx(presence=None, identity=None, drill_elapsed_ms=600_000))
        assert d.state is AccountabilityState.UNACCOUNTED
        assert "never observed" in d.reason

    def test_never_observed_early_is_not_yet_alarming(self):
        d = derive(ctx(presence=None, identity=None, drill_elapsed_ms=5_000))
        assert d.state is AccountabilityState.NOT_EVACUATED


class TestUnknownPeople:
    def test_someone_off_the_roster_is_uncertain_not_accounted(self):
        # Invariant 5. A visitor is tracked as a visitor, never folded onto an
        # employee record, and never silently dropped either.
        d = derive(ctx(is_expected=False, presence=inside(), identity=None,
                       drill_elapsed_ms=600_000))
        assert d.state is AccountabilityState.UNCERTAIN
        assert "not on the roster" in d.reason

    def test_an_unknown_person_at_assembly_is_still_uncertain(self):
        d = derive(ctx(is_expected=False, presence=at_assembly(), identity=None))
        assert d.state is AccountabilityState.UNCERTAIN


class TestWardenAbsence:
    def test_marked_absent_is_uncertain_not_safe(self):
        # "Not in the building today" is not the same as "evacuated safely",
        # and conflating them is how a genuinely absent person becomes a
        # false ALL CLEAR.
        d = derive(ctx(presence=None, identity=None, drill_elapsed_ms=600_000,
                       warden=WardenEvidence(marked_absent=True,
                                             warden_id="warden-7")))
        assert d.state is AccountabilityState.UNCERTAIN
        assert d.state is not AccountabilityState.ACCOUNTED
        assert "not on site today" in d.reason


class TestEveryDecisionHasAReason:
    @pytest.mark.parametrize("context", [
        ctx(presence=at_assembly(), identity=confirmed_identity()),
        ctx(presence=at_assembly(), identity=conflicted_identity()),
        ctx(presence=inside(), identity=confirmed_identity()),
        ctx(presence=inside(), identity=None, drill_elapsed_ms=600_000),
        ctx(presence=None, identity=None),
        ctx(system_degraded=True, presence=inside()),
        ctx(is_expected=False, presence=inside()),
        ctx(warden=WardenEvidence(confirmed=True, at_assembly_zone="a-1")),
        ctx(warden=WardenEvidence(rejected=True)),
        ctx(warden=WardenEvidence(marked_absent=True)),
    ])
    def test_no_branch_returns_a_bare_state(self, context):
        # Invariant 6. A state nobody can audit is not a state.
        d = derive(context)
        assert d.reason.strip()
        assert len(d.reason) > 15


class TestBoard:
    def test_it_reports_a_change_only_when_the_state_changes(self):
        board = AccountabilityBoard()
        _, changed = board.evaluate(ctx(presence=inside(), identity=confirmed_identity()))
        assert changed is True
        _, changed = board.evaluate(ctx(presence=inside(), identity=confirmed_identity()))
        assert changed is False

    def test_a_tally_covers_every_state(self):
        board = AccountabilityBoard()
        board.evaluate(ctx(person_id="gp-1", presence=at_assembly(),
                           identity=confirmed_identity()))
        board.evaluate(ctx(person_id="gp-2", presence=inside(),
                           identity=confirmed_identity(), drill_elapsed_ms=600_000))
        tally = board.tally()
        assert set(tally) == set(AccountabilityState)
        assert tally[AccountabilityState.ACCOUNTED] == 1
        assert tally[AccountabilityState.UNACCOUNTED] == 1

    def test_the_priority_list_is_everyone_a_human_must_look_at(self):
        board = AccountabilityBoard()
        board.evaluate(ctx(person_id="gp-1", presence=at_assembly(),
                           identity=confirmed_identity()))
        board.evaluate(ctx(person_id="gp-2", presence=at_assembly(),
                           identity=conflicted_identity()))
        board.evaluate(ctx(person_id="gp-3", presence=inside(),
                           identity=confirmed_identity(), drill_elapsed_ms=600_000))
        assert {d.person_id for d in board.needing_attention()} == {"gp-2", "gp-3"}
        assert [d.person_id for d in board.accounted()] == ["gp-1"]


class TestConfig:
    def test_the_provisional_config_is_marked_uncalibrated(self):
        assert PROVISIONAL_CONFIG.calibrated is False

    def test_a_nonsense_deadline_is_refused(self):
        with pytest.raises(ValueError):
            AccountabilityConfig(unaccounted_after_ms=0)


class TestEveryBranchCanBeTranslated:
    """A branch that returns no code renders as English on an Arabic tablet.

    `reason` is prose and stays that way for the report, which is a document.
    The warden PWA is not a document: the reason under a person's name is the
    sentence telling a warden what to do about them, and it is read in Arabic
    at an assembly point. So every branch carries a code and the values its
    wording needs, and the screen does the wording.
    """

    def test_the_enum_covers_every_branch(self):
        # Counted from the source rather than asserted as a number somebody
        # keeps up to date: a branch added without a code is the failure.
        import ast
        import inspect

        from app.core import accountability_fsm

        tree = ast.parse(inspect.getsource(accountability_fsm))
        without = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Call):
                continue
            called = node.value.func
            if getattr(called, "id", "") != "Decision":
                continue
            if not any(kw.arg == "code" for kw in node.value.keywords):
                without.append(node.lineno)
        assert without == [], f"Decision without a code at lines {without}"

    def test_every_code_is_reachable(self):
        """An enum member nothing returns is a string somebody translates for
        nothing, and a screen that will never show it."""
        import ast
        import inspect

        from app.core import accountability_fsm
        from app.core.accountability_fsm import ReasonCode

        source = inspect.getsource(accountability_fsm)
        tree = ast.parse(source)
        used = {
            node.attr for node in ast.walk(tree)
            if isinstance(node, ast.Attribute)
            and getattr(node.value, "id", "") == "ReasonCode"
        }
        assert {c.name for c in ReasonCode} == used

    def test_where_somebody_was_last_seen_travels_as_values(self):
        # "last seen in FLOOR zone floor-2 on cam-4" is a sentence with three
        # values in it, and a translation puts them in a different order.
        presence = PersonPresence(person_id="gp-1", config=PRESENCE_CFG)
        presence.observe(ZoneSighting(ts_ms=T0, zone_id="floor-2",
                                      zone_kind=ZoneKind.FLOOR,
                                      camera_id="cam-4"))
        decision = derive(Context(person_id="gp-1", drill_elapsed_ms=1_000,
                                  presence=presence))
        assert decision.detail["zone_id"] == "floor-2"
        assert decision.detail["zone_kind"] == "FLOOR"
        assert decision.detail["camera_id"] == "cam-4"

    def test_a_person_nobody_has_seen_carries_no_location(self):
        # An empty dict rather than "unknown": the screen decides what to say
        # about not knowing, and it says it in the reader's language.
        decision = derive(Context(person_id="gp-1", drill_elapsed_ms=1_000))
        assert "zone_id" not in decision.detail
