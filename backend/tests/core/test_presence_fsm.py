"""Presence state machine."""

import pytest

from app.core.presence_fsm import (
    PROVISIONAL_CONFIG,
    PersonPresence,
    PresenceConfig,
    PresenceRegistry,
    PresenceState,
    ZoneKind,
    ZoneSighting,
)

T0 = 1_788_000_000_000


def config(**overrides) -> PresenceConfig:
    base = dict(t_lost_ms=15_000, t_lost_blind_ms=45_000, assembly_dwell_ms=5_000)
    base.update(overrides)
    return PresenceConfig(**base)


def person(**overrides) -> PersonPresence:
    return PersonPresence(person_id="gp-1", config=config(**overrides))


def seen(kind: ZoneKind, ts_ms: int, zone_id: str = "z-1", camera_id: str = "cam-1"):
    return ZoneSighting(ts_ms=ts_ms, zone_id=zone_id, zone_kind=kind,
                        camera_id=camera_id)


def settle_at_assembly(p: PersonPresence, start: int = T0) -> None:
    """Walk someone into an assembly zone and let them dwell past the gate."""
    p.observe(seen(ZoneKind.ASSEMBLY, start, "assembly-north"))
    p.observe(seen(ZoneKind.ASSEMBLY, start + p.config.assembly_dwell_ms + 1,
                   "assembly-north"))
    assert p.state is PresenceState.ASSEMBLY_PRESENT


class TestConfig:
    def test_the_provisional_config_is_marked_uncalibrated(self):
        assert PROVISIONAL_CONFIG.calibrated is False

    def test_a_blind_zone_cannot_be_less_forgiving_than_a_covered_one(self):
        # Inverting these would punish people for walking through an
        # architectural coverage hole.
        with pytest.raises(ValueError, match="t_lost_blind_ms"):
            config(t_lost_ms=30_000, t_lost_blind_ms=10_000)

    def test_nonsense_timings_are_refused(self):
        with pytest.raises(ValueError):
            config(t_lost_ms=0)
        with pytest.raises(ValueError):
            config(assembly_dwell_ms=-1)


class TestTheHappyPath:
    def test_floor_then_exit_then_assembly(self):
        p = person()
        assert p.state is PresenceState.NOT_OBSERVED
        assert p.observe(seen(ZoneKind.FLOOR, T0)).to_state is PresenceState.IN_BUILDING
        assert p.observe(seen(ZoneKind.EXIT, T0 + 10_000)).to_state is PresenceState.IN_TRANSIT
        settle_at_assembly(p, T0 + 20_000)

    def test_arriving_at_assembly_without_being_seen_at_an_exit(self):
        # Exit cameras are the ones most likely to be saturated during an
        # evacuation. Missing that sighting must not block the arrival.
        p = person()
        p.observe(seen(ZoneKind.FLOOR, T0))
        settle_at_assembly(p, T0 + 30_000)


class TestAssemblyHysteresis:
    def test_walking_through_the_corner_of_an_assembly_zone_is_not_arrival(self):
        p = person(assembly_dwell_ms=5_000)
        p.observe(seen(ZoneKind.FLOOR, T0))
        p.observe(seen(ZoneKind.ASSEMBLY, T0 + 1_000, "assembly-north"))
        p.observe(seen(ZoneKind.ASSEMBLY, T0 + 2_000, "assembly-north"))
        assert p.state is PresenceState.IN_BUILDING

    def test_settling_there_is(self):
        p = person(assembly_dwell_ms=5_000)
        p.observe(seen(ZoneKind.FLOOR, T0))
        p.observe(seen(ZoneKind.ASSEMBLY, T0 + 1_000, "assembly-north"))
        t = p.observe(seen(ZoneKind.ASSEMBLY, T0 + 7_000, "assembly-north"))
        assert t.to_state is PresenceState.ASSEMBLY_PRESENT

    def test_leaving_and_returning_restarts_the_dwell(self):
        p = person(assembly_dwell_ms=5_000)
        p.observe(seen(ZoneKind.ASSEMBLY, T0, "assembly-north"))
        p.observe(seen(ZoneKind.FLOOR, T0 + 1_000))
        p.observe(seen(ZoneKind.ASSEMBLY, T0 + 2_000, "assembly-north"))
        p.observe(seen(ZoneKind.ASSEMBLY, T0 + 4_000, "assembly-north"))
        assert p.state is PresenceState.IN_BUILDING


class TestLosingATrack:
    def test_a_dropped_track_is_temporarily_unobserved_first(self):
        p = person()
        p.observe(seen(ZoneKind.FLOOR, T0))
        t = p.track_lost(T0 + 1_000)
        assert t.to_state is PresenceState.TEMPORARILY_UNOBSERVED

    def test_it_ages_into_lost_only_after_the_grace_window(self):
        p = person(t_lost_ms=15_000)
        p.observe(seen(ZoneKind.FLOOR, T0))
        p.track_lost(T0 + 1_000)
        assert p.tick(T0 + 10_000) is None
        assert p.tick(T0 + 20_000).to_state is PresenceState.LOST

    def test_a_blind_zone_earns_a_longer_grace_window(self):
        # Not seeing someone in an uncovered corridor is the expected outcome
        # there, not evidence of anything.
        p = person(t_lost_ms=15_000, t_lost_blind_ms=45_000)
        p.observe(seen(ZoneKind.BLIND, T0, "stairwell-b"))
        p.track_lost(T0 + 1_000)
        assert p.tick(T0 + 20_000) is None
        assert p.tick(T0 + 50_000).to_state is PresenceState.LOST

    def test_reacquiring_the_track_restores_presence(self):
        p = person()
        p.observe(seen(ZoneKind.FLOOR, T0))
        p.track_lost(T0 + 1_000)
        p.tick(T0 + 60_000)
        assert p.state is PresenceState.LOST
        t = p.observe(seen(ZoneKind.EXIT, T0 + 70_000))
        assert t.to_state is PresenceState.IN_TRANSIT

    def test_a_person_never_seen_cannot_lose_a_track(self):
        assert person().track_lost(T0) is None

    def test_lost_says_nothing_about_the_person(self):
        # Invariant 1. LOST is a statement about the track's staleness. The
        # last-known evidence survives precisely so a human can go and look.
        p = person()
        p.observe(seen(ZoneKind.FLOOR, T0, "floor-3-east", "cam-7"))
        p.track_lost(T0 + 1_000)
        p.tick(T0 + 60_000)
        assert p.state is PresenceState.LOST
        assert p.last_known.zone_id == "floor-3-east"
        assert p.last_known.camera_id == "cam-7"
        assert p.last_known.ts_ms == T0


class TestDegradation:
    """Invariant 8. Our own outage is never evidence about a person."""

    def test_a_degraded_camera_suspends_the_grace_clock(self):
        p = person(t_lost_ms=15_000)
        p.observe(seen(ZoneKind.FLOOR, T0))
        p.track_lost(T0 + 1_000)
        p.mark_degraded(T0 + 2_000, "camera offline")
        assert p.tick(T0 + 600_000) is None
        assert p.state is PresenceState.TEMPORARILY_UNOBSERVED

    def test_recovery_credits_back_the_outage(self):
        # A ten-minute camera failure must not instantly age everyone it covered
        # into LOST the moment it comes back.
        p = person(t_lost_ms=15_000)
        p.observe(seen(ZoneKind.FLOOR, T0))
        p.track_lost(T0 + 1_000)
        p.mark_degraded(T0 + 2_000, "camera offline")
        p.mark_recovered(T0 + 602_000)
        assert p.tick(T0 + 603_000) is None
        assert p.tick(T0 + 620_000).to_state is PresenceState.LOST

    def test_being_seen_again_ends_the_degradation_by_itself(self):
        """A sighting disproves "we cannot currently see this person".

        The registry credits a recovery to whoever was last seen on the camera
        that failed. Someone who walked into a different camera's view during
        the outage is no longer on that list, so the recovery never reached
        them: `is_degraded` stayed true for the rest of the drill, the grace
        clock never ran, and the board showed them as briefly unobserved
        forever instead of eventually LOST.
        """
        registry = PresenceRegistry(config(t_lost_ms=10_000))
        registry.observe("gp-1", seen(ZoneKind.FLOOR, T0, camera_id="cam-a"))
        registry.mark_camera_degraded("cam-a", T0 + 1_000, "camera offline")
        registry.observe("gp-1", seen(ZoneKind.FLOOR, T0 + 2_000,
                                      camera_id="cam-b"))
        registry.mark_camera_recovered("cam-a", T0 + 30_000)

        walked_away = registry.get("gp-1")
        assert walked_away.is_degraded is False

        walked_away.track_lost(T0 + 31_000)
        assert [t.to_state for t in registry.tick(T0 + 90_000)] == [
            PresenceState.LOST]

    def test_a_sighting_still_credits_the_time_spent_blind(self):
        # The credit is the point of the mechanism, so ending the degradation
        # on a sighting must not throw it away and age the person early.
        blinded = person(t_lost_ms=15_000)
        blinded.observe(seen(ZoneKind.FLOOR, T0))
        blinded.mark_degraded(T0 + 1_000, "camera offline")
        blinded.observe(seen(ZoneKind.FLOOR, T0 + 601_000))
        blinded.track_lost(T0 + 601_500)
        assert blinded.tick(T0 + 621_000) is None

        # The same twenty seconds, without an outage behind them, is LOST.
        watched = person(t_lost_ms=15_000)
        watched.observe(seen(ZoneKind.FLOOR, T0 + 601_000))
        watched.track_lost(T0 + 601_500)
        assert watched.tick(T0 + 621_000).to_state is PresenceState.LOST

    def test_a_site_wide_camera_outage_covers_everyone_it_was_watching(self):
        """`"*"` is how a whole-site failure arrives from the ingest.

        Matching it against each person's last known camera id matched nobody,
        so the most severe outage was the one that credited nothing: everyone
        aged into LOST for a blindness that was entirely ours, while a single
        camera failing was handled correctly.
        """
        registry = PresenceRegistry(config(t_lost_ms=10_000))
        registry.observe("gp-1", seen(ZoneKind.FLOOR, T0, camera_id="cam-a"))
        registry.observe("gp-2", seen(ZoneKind.FLOOR, T0, camera_id="cam-b"))
        assert registry.mark_camera_degraded("*", T0 + 1_000, "all offline") == 2

        for person_id in ("gp-1", "gp-2"):
            registry.get(person_id).track_lost(T0 + 2_000)
        assert registry.tick(T0 + 200_000) == []

        registry.mark_camera_recovered("*", T0 + 200_000)
        assert registry.tick(T0 + 201_000) == []
        assert [t.to_state for t in registry.tick(T0 + 215_000)] == [
            PresenceState.LOST, PresenceState.LOST]

    def test_a_site_wide_outage_does_not_soften_someone_never_seen(self):
        # They have no grace clock to suspend, and marking them degraded would
        # turn "no observation at all" into "we had a camera problem".
        registry = PresenceRegistry(config())
        registry.get("gp-never-seen")
        assert registry.mark_camera_degraded("*", T0, "all offline") == 0
        assert registry.get("gp-never-seen").is_degraded is False

    def test_one_outage_ending_does_not_clear_another_still_open(self):
        """Outages overlap, and a single flag could not say so.

        A camera fails, then every camera fails. The first one comes back while
        the site-wide failure is still running, and with one slot that recovery
        cleared the degradation, restarted the grace clock, and aged people out
        while nobody could see them.
        """
        registry = PresenceRegistry(config(t_lost_ms=10_000))
        registry.observe("gp-1", seen(ZoneKind.FLOOR, T0, camera_id="cam-a"))
        registry.mark_camera_degraded("cam-a", T0 + 1_000, "one camera offline")
        registry.mark_camera_degraded("*", T0 + 2_000, "all cameras offline")

        lost_sight_of = registry.get("gp-1")
        lost_sight_of.track_lost(T0 + 3_000)

        registry.mark_camera_recovered("cam-a", T0 + 4_000)
        assert lost_sight_of.is_degraded is True
        assert registry.tick(T0 + 200_000) == []

        registry.mark_camera_recovered("*", T0 + 200_000)
        assert lost_sight_of.is_degraded is False
        assert registry.tick(T0 + 201_000) == []
        assert [t.to_state for t in registry.tick(T0 + 215_000)] == [
            PresenceState.LOST]

    def test_degradation_is_visible_rather_than_silent(self):
        p = person()
        p.observe(seen(ZoneKind.FLOOR, T0))
        p.mark_degraded(T0 + 1_000, "pipeline restart")
        assert p.is_degraded is True
        p.mark_recovered(T0 + 2_000)
        assert p.is_degraded is False


class TestReachingAssembly:
    def test_arrival_survives_the_track_being_lost_afterwards(self):
        # Someone who walked into the assembly point and then out of camera
        # view has not un-arrived.
        p = person()
        settle_at_assembly(p)
        p.track_lost(T0 + 30_000)
        p.tick(T0 + 90_000)
        assert p.state is PresenceState.LOST
        assert p.was_at_assembly is True

    def test_someone_who_never_arrived_did_not_arrive(self):
        p = person()
        p.observe(seen(ZoneKind.FLOOR, T0))
        p.track_lost(T0 + 1_000)
        p.tick(T0 + 60_000)
        assert p.was_at_assembly is False

    def test_walking_back_inside_ends_assembly_presence(self):
        p = person()
        settle_at_assembly(p)
        t = p.observe(seen(ZoneKind.FLOOR, T0 + 30_000))
        assert t.to_state is PresenceState.IN_BUILDING
        assert p.was_at_assembly is False


class TestBlindZones:
    def test_being_seen_at_a_blind_zone_means_still_inside(self):
        p = person()
        t = p.observe(seen(ZoneKind.BLIND, T0, "stairwell-b"))
        assert t.to_state is PresenceState.IN_BUILDING

    def test_a_blind_zone_does_not_undo_being_in_transit(self):
        # Walking from an exit corridor into an unmonitored stairwell is still
        # heading out, not a return into the building.
        p = person()
        p.observe(seen(ZoneKind.EXIT, T0))
        assert p.observe(seen(ZoneKind.BLIND, T0 + 1_000, "stairwell-b")) is None
        assert p.state is PresenceState.IN_TRANSIT


class TestRegistry:
    def test_people_are_independent(self):
        registry = PresenceRegistry(config())
        registry.observe("gp-1", seen(ZoneKind.FLOOR, T0))
        registry.observe("gp-2", seen(ZoneKind.EXIT, T0))
        assert registry.get("gp-1").state is PresenceState.IN_BUILDING
        assert registry.get("gp-2").state is PresenceState.IN_TRANSIT
        assert len(registry) == 2

    def test_a_camera_failure_covers_everyone_it_was_watching(self):
        registry = PresenceRegistry(config(t_lost_ms=15_000))
        for pid in ("gp-1", "gp-2", "gp-3"):
            registry.observe(pid, seen(ZoneKind.FLOOR, T0, camera_id="cam-1"))
            registry.get(pid).track_lost(T0 + 1_000)
        registry.observe("gp-4", seen(ZoneKind.FLOOR, T0, camera_id="cam-2"))
        registry.get("gp-4").track_lost(T0 + 1_000)

        assert registry.mark_camera_degraded("cam-1", T0 + 2_000, "offline") == 3
        lost = registry.tick(T0 + 600_000)
        # Only the person behind the healthy camera ages into LOST.
        assert [t.person_id for t in lost] == ["gp-4"]

    def test_still_inside_excludes_people_at_assembly(self):
        registry = PresenceRegistry(config())
        registry.observe("gp-1", seen(ZoneKind.FLOOR, T0))
        settle_at_assembly(registry.get("gp-2"))
        assert [p.person_id for p in registry.still_inside()] == ["gp-1"]
        assert [p.person_id for p in registry.at_assembly()] == ["gp-2"]
