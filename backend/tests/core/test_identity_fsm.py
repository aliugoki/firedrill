"""Identity state machine.

Most of these are invariant tests. They exist because the failure they prevent
is someone being told a stranger is their colleague, or a colleague being
declared missing because a camera looked away.
"""

import pytest

from app.core.identity_fsm import (
    PROVISIONAL_CONFIG,
    FaceObservation,
    IdentityConfig,
    IdentityRegistry,
    IdentityState,
    PersonIdentity,
    RejectionReason,
    gate,
)

T0 = 1_788_000_000_000


def config(**overrides) -> IdentityConfig:
    base = dict(
        score_threshold=0.35, min_margin=0.05, min_votes=3, conflict_votes=3,
        min_face_quality=0.5, max_pose_deviation_deg=45.0,
        min_track_confidence=0.5, identity_expiry_ms=30_000,
    )
    base.update(overrides)
    return IdentityConfig(**base)


def person(**overrides) -> PersonIdentity:
    return PersonIdentity(person_id="gp-1", config=config(**overrides))


def good(candidate="EMP-1", ts_ms=T0, **overrides) -> FaceObservation:
    base = dict(ts_ms=ts_ms, candidate_id=candidate, score=0.80, margin=0.30,
                quality=0.9, pose_deviation_deg=5.0, track_confidence=0.9)
    base.update(overrides)
    return FaceObservation(**base)


def confirm(p: PersonIdentity, candidate="EMP-1", start=T0) -> None:
    for i in range(p.config.min_votes):
        p.observe(good(candidate, ts_ms=start + i * 100))
    assert p.state is IdentityState.CONFIRMED


class TestConfig:
    def test_the_provisional_config_is_marked_uncalibrated(self):
        # Invariant 7. If this ever reads True without a calibration report
        # behind it, the system is presenting invented numbers as validated.
        assert PROVISIONAL_CONFIG.calibrated is False
        assert "unvalidated" in PROVISIONAL_CONFIG.source

    def test_nonsense_configs_are_refused(self):
        with pytest.raises(ValueError):
            config(min_votes=0)
        with pytest.raises(ValueError):
            config(identity_expiry_ms=0)
        with pytest.raises(ValueError):
            config(min_face_quality=1.5)


class TestGates:
    def test_a_clean_observation_is_accepted(self):
        assert gate(good(), config()) is RejectionReason.ACCEPTED

    @pytest.mark.parametrize("override,expected", [
        ({"candidate_id": None}, RejectionReason.NO_FACE),
        ({"quality": 0.1}, RejectionReason.LOW_QUALITY),
        ({"pose_deviation_deg": 80.0}, RejectionReason.BAD_POSE),
        ({"track_confidence": 0.1}, RejectionReason.LOW_TRACK_CONFIDENCE),
        ({"score": 0.20}, RejectionReason.BELOW_SCORE_THRESHOLD),
        ({"margin": 0.01}, RejectionReason.BELOW_MARGIN),
    ])
    def test_each_gate_reports_its_own_reason(self, override, expected):
        # The reason is recorded, not just the rejection. "Why was this person
        # not identified?" has to have an answer (invariant 6).
        assert gate(good(**override), config()) is expected


class TestPromotion:
    def test_one_observation_makes_a_candidate_not_a_confirmation(self):
        p = person()
        t = p.observe(good())
        assert t.to_state is IdentityState.CANDIDATE
        assert p.state is IdentityState.CANDIDATE

    def test_enough_votes_confirms(self):
        p = person()
        p.observe(good())
        p.observe(good(ts_ms=T0 + 100))
        t = p.observe(good(ts_ms=T0 + 200))
        assert t.to_state is IdentityState.CONFIRMED
        assert p.identity == "EMP-1"

    def test_inadmissible_observations_never_promote(self):
        p = person()
        for i in range(20):
            p.observe(good(score=0.10, ts_ms=T0 + i * 100))
        assert p.state is IdentityState.UNKNOWN

    def test_an_absent_face_never_promotes(self):
        # Invariant 1: no face is not evidence about identity in either
        # direction.
        p = person()
        for i in range(20):
            p.observe(FaceObservation(ts_ms=T0 + i * 100, candidate_id=None))
        assert p.state is IdentityState.UNKNOWN
        assert p.identity is None


class TestStickiness:
    """Invariant 2. A confirmed identity survives everything short of a rival
    with real support, or a human."""

    def test_a_blank_face_does_not_dislodge_a_confirmed_identity(self):
        p = person()
        confirm(p)
        for i in range(5):
            p.observe(FaceObservation(ts_ms=T0 + 1_000 + i * 100, candidate_id=None))
        assert p.state is IdentityState.CONFIRMED
        assert p.identity == "EMP-1"

    def test_a_low_quality_glimpse_of_someone_else_does_not_dislodge(self):
        p = person()
        confirm(p)
        for i in range(10):
            p.observe(good("EMP-2", ts_ms=T0 + 1_000 + i * 100, quality=0.2))
        assert p.state is IdentityState.CONFIRMED
        assert p.identity == "EMP-1"

    def test_a_single_clean_look_at_a_rival_does_not_dislodge(self):
        p = person(conflict_votes=3)
        confirm(p)
        p.observe(good("EMP-2", ts_ms=T0 + 1_000))
        assert p.state is IdentityState.CONFIRMED
        assert p.identity == "EMP-1"

    def test_reconfirmation_refreshes_without_changing_state(self):
        p = person()
        confirm(p)
        assert p.observe(good(ts_ms=T0 + 1_000)) is None
        assert p.state is IdentityState.CONFIRMED


class TestTemporarilyUnavailable:
    """A face going away is not a person going away."""

    def test_expiry_moves_confirmed_to_temporarily_unavailable(self):
        p = person(identity_expiry_ms=30_000)
        confirm(p)
        t = p.tick(T0 + 40_000)
        assert t.to_state is IdentityState.TEMPORARILY_UNAVAILABLE
        assert p.identity == "EMP-1"

    def test_it_never_falls_back_to_unknown(self):
        # UNKNOWN would mean "we have no idea who this is", which is false: we
        # identified them and nothing contradicted it.
        p = person(identity_expiry_ms=10_000)
        confirm(p)
        for i in range(1, 20):
            p.tick(T0 + i * 10_000)
        assert p.state is IdentityState.TEMPORARILY_UNAVAILABLE
        assert p.identity == "EMP-1"

    def test_seeing_the_face_again_restores_confirmed(self):
        p = person(identity_expiry_ms=10_000)
        confirm(p)
        p.tick(T0 + 20_000)
        t = p.observe(good(ts_ms=T0 + 21_000))
        assert t.to_state is IdentityState.CONFIRMED

    def test_it_still_supports_accountability(self):
        p = person(identity_expiry_ms=10_000)
        confirm(p)
        p.tick(T0 + 20_000)
        assert p.is_usable_for_accountability is True

    def test_expiry_does_not_fire_before_its_time(self):
        p = person(identity_expiry_ms=30_000)
        confirm(p)
        assert p.tick(T0 + 1_000) is None


class TestConflict:
    """Invariant 3. Never resolved silently, never resolved by vote count."""

    def test_two_supported_identities_raise_conflict(self):
        p = person(min_votes=3, conflict_votes=3)
        for i in range(3):
            p.observe(good("EMP-1", ts_ms=T0 + i * 100))
        for i in range(3):
            p.observe(good("EMP-2", ts_ms=T0 + 1_000 + i * 100))
        assert p.state is IdentityState.CONFLICT

    def test_a_losing_rival_still_raises_conflict(self):
        # 9 votes to 3 is still a conflict. Three independent clean looks at a
        # different person is real evidence, and outvoting it is what
        # invariant 3 forbids.
        p = person(min_votes=3, conflict_votes=3)
        for i in range(9):
            p.observe(good("EMP-1", ts_ms=T0 + i * 100))
        assert p.state is IdentityState.CONFIRMED
        for i in range(3):
            p.observe(good("EMP-2", ts_ms=T0 + 5_000 + i * 100))
        assert p.state is IdentityState.CONFLICT
        assert set(p.conflict_with) == {"EMP-1", "EMP-2"}

    def test_conflict_drops_the_identity_claim(self):
        p = person(min_votes=3, conflict_votes=3)
        for i in range(3):
            p.observe(good("EMP-1", ts_ms=T0 + i * 100))
        for i in range(3):
            p.observe(good("EMP-2", ts_ms=T0 + 1_000 + i * 100))
        assert p.identity is None
        assert p.is_usable_for_accountability is False
        assert p.needs_human is True

    def test_more_camera_evidence_cannot_break_a_conflict(self):
        p = person(min_votes=3, conflict_votes=3)
        for i in range(3):
            p.observe(good("EMP-1", ts_ms=T0 + i * 100))
        for i in range(3):
            p.observe(good("EMP-2", ts_ms=T0 + 1_000 + i * 100))
        for i in range(100):
            p.observe(good("EMP-1", ts_ms=T0 + 5_000 + i * 100))
        assert p.state is IdentityState.CONFLICT


class TestWardenAuthority:
    """Invariant 9. The human is the final authority in both directions."""

    def test_a_warden_can_resolve_a_conflict(self):
        p = person(min_votes=3, conflict_votes=3)
        for i in range(3):
            p.observe(good("EMP-1", ts_ms=T0 + i * 100))
        for i in range(3):
            p.observe(good("EMP-2", ts_ms=T0 + 1_000 + i * 100))
        t = p.warden_confirms("EMP-2", T0 + 9_000, warden_id="warden-7")
        assert t.to_state is IdentityState.CONFIRMED
        assert p.identity == "EMP-2"
        assert t.detail["human"] is True

    def test_a_warden_rejection_sticks_against_all_later_cameras(self):
        p = person(min_votes=3)
        confirm(p)
        p.warden_rejects("EMP-1", T0 + 5_000, warden_id="warden-7")
        assert p.state is IdentityState.REJECTED
        assert p.identity is None
        for i in range(50):
            p.observe(good("EMP-1", ts_ms=T0 + 10_000 + i * 100))
        assert p.state is IdentityState.REJECTED

    def test_a_rejected_person_is_not_declared_missing(self):
        # REJECTED means the system's answer was wrong, not that the person is
        # gone. They still need identifying, which is why they need a human.
        p = person()
        confirm(p)
        p.warden_rejects("EMP-1", T0 + 5_000, warden_id="warden-7")
        assert p.needs_human is True
        assert p.is_usable_for_accountability is False

    def test_a_warden_can_reinstate_an_identity_they_rejected(self):
        p = person()
        confirm(p)
        p.warden_rejects("EMP-1", T0 + 5_000, warden_id="warden-7")
        t = p.warden_confirms("EMP-1", T0 + 6_000, warden_id="warden-7")
        assert t.to_state is IdentityState.CONFIRMED
        assert p.identity == "EMP-1"


class TestRegistry:
    def test_people_are_tracked_independently(self):
        registry = IdentityRegistry(config())
        registry.observe("gp-1", good("EMP-1"))
        registry.observe("gp-2", good("EMP-2"))
        assert len(registry) == 2
        assert registry.get("gp-1").leader()[0] == "EMP-1"
        assert registry.get("gp-2").leader()[0] == "EMP-2"

    def test_tick_ages_everyone_at_once(self):
        registry = IdentityRegistry(config(identity_expiry_ms=10_000))
        for pid in ("gp-1", "gp-2"):
            for i in range(3):
                registry.observe(pid, good("EMP-1", ts_ms=T0 + i * 100))
        transitions = registry.tick(T0 + 60_000)
        assert len(transitions) == 2
        assert all(t.to_state is IdentityState.TEMPORARILY_UNAVAILABLE
                   for t in transitions)

    def test_the_registry_can_list_who_needs_a_human(self):
        registry = IdentityRegistry(config(min_votes=3, conflict_votes=3))
        for i in range(3):
            registry.observe("gp-1", good("EMP-1", ts_ms=T0 + i * 100))
        for i in range(3):
            registry.observe("gp-1", good("EMP-2", ts_ms=T0 + 1_000 + i * 100))
        registry.observe("gp-2", good("EMP-3"))
        needing = registry.needing_human()
        assert [p.person_id for p in needing] == ["gp-1"]
