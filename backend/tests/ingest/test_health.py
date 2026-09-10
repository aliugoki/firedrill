"""Health tracked as intervals, so a report can say what was true at the time."""

import pytest

from app.ingest.health import Component, HealthLog

T0 = 1_788_000_000_000


@pytest.fixture
def log() -> HealthLog:
    return HealthLog()


class TestRecording:
    def test_degrading_then_recovering_closes_the_interval(self, log):
        log.degrade(Component.CAMERA, "cam-1", T0, "offline")
        assert log.is_degraded is True
        log.recover(Component.CAMERA, "cam-1", T0 + 60_000)
        assert log.is_degraded is False
        assert log.degradations[0].duration_ms(T0 + 60_000) == 60_000

    def test_a_flapping_camera_is_one_outage_not_a_hundred(self, log):
        assert log.degrade(Component.CAMERA, "cam-1", T0, "offline") is not None
        for i in range(99):
            assert log.degrade(Component.CAMERA, "cam-1", T0 + i, "offline") is None
        assert len(log.degradations) == 1

    def test_recovering_something_that_was_never_down_does_nothing(self, log):
        assert log.recover(Component.CAMERA, "cam-1", T0) is None
        assert log.degradations == []

    def test_different_targets_are_separate_outages(self, log):
        log.degrade(Component.CAMERA, "cam-1", T0, "offline")
        log.degrade(Component.CAMERA, "cam-2", T0, "offline")
        assert len(log.open_now()) == 2

    def test_closing_a_drill_mid_failure_closes_everything(self, log):
        log.degrade(Component.CAMERA, "cam-1", T0, "offline")
        log.degrade(Component.EVENT_BUS, "*", T0, "redis down")
        closed = log.close_all(T0 + 100_000)
        assert len(closed) == 2
        assert log.is_degraded is False


class TestBlindness:
    def test_a_camera_failure_blinds_the_system(self, log):
        log.degrade(Component.CAMERA, "cam-1", T0, "offline")
        assert log.is_blind is True

    def test_a_database_failure_does_not(self, log):
        # The system can still see. It may not be able to remember, which is a
        # different problem with a different response.
        log.degrade(Component.DATABASE, "*", T0, "postgres down")
        assert log.is_degraded is True
        assert log.is_blind is False

    def test_a_broken_link_to_central_does_not_blind_the_edge(self, log):
        # The edge is the authority during a drill. Losing central costs
        # reporting, not sight.
        log.degrade(Component.CENTRAL_LINK, "*", T0, "wan down")
        assert log.is_blind is False


class TestHistoricalQuestions:
    """A boolean cannot answer these, which is why intervals are stored."""

    def test_it_answers_whether_the_system_was_blind_at_a_moment(self, log):
        log.degrade(Component.CAMERA, "cam-3", T0 + 60_000, "offline")
        log.recover(Component.CAMERA, "cam-3", T0 + 180_000)
        assert log.was_degraded_at(T0 + 30_000) is False
        assert log.was_degraded_at(T0 + 120_000) is True
        assert log.was_degraded_at(T0 + 200_000) is False

    def test_it_names_the_cause_in_words_a_warden_can_use(self, log):
        log.degrade(Component.CAMERA, "cam-3", T0, "camera offline")
        causes = log.causes_at(T0 + 1_000)
        assert causes == ["camera cam-3: camera offline"]

    def test_an_open_outage_covers_everything_after_it_started(self, log):
        log.degrade(Component.CAMERA, "cam-1", T0, "offline")
        assert log.was_degraded_at(T0 + 10_000_000) is True

    def test_it_reports_which_cameras_were_dark(self, log):
        log.degrade(Component.CAMERA, "cam-1", T0, "offline")
        log.degrade(Component.CAMERA, "cam-2", T0, "offline")
        log.degrade(Component.DATABASE, "*", T0, "down")
        assert log.blinded_targets_at(T0 + 1) == {"cam-1", "cam-2"}


class TestDegradedFraction:
    def test_a_clean_drill_is_zero(self, log):
        assert log.degraded_fraction(T0, T0 + 600_000) == 0.0

    def test_half_a_drill_is_half(self, log):
        log.degrade(Component.CAMERA, "cam-1", T0, "offline")
        log.recover(Component.CAMERA, "cam-1", T0 + 300_000)
        assert log.degraded_fraction(T0, T0 + 600_000) == pytest.approx(0.5)

    def test_overlapping_outages_are_unioned_not_summed(self):
        # Three cameras down at once is one blind period. Summing them could
        # report more downtime than the drill lasted.
        log = HealthLog()
        for cam in ("cam-1", "cam-2", "cam-3"):
            log.degrade(Component.CAMERA, cam, T0, "offline")
            log.recover(Component.CAMERA, cam, T0 + 300_000)
        assert log.degraded_fraction(T0, T0 + 600_000) == pytest.approx(0.5)

    def test_it_never_exceeds_one(self, log):
        for cam in range(10):
            log.degrade(Component.CAMERA, f"cam-{cam}", T0 - 100_000, "offline")
        assert log.degraded_fraction(T0, T0 + 600_000) <= 1.0

    def test_an_outage_outside_the_window_does_not_count(self, log):
        log.degrade(Component.CAMERA, "cam-1", T0 - 500_000, "offline")
        log.recover(Component.CAMERA, "cam-1", T0 - 400_000)
        assert log.degraded_fraction(T0, T0 + 600_000) == 0.0

    def test_a_non_blinding_outage_is_excluded_by_default(self, log):
        log.degrade(Component.DATABASE, "*", T0, "down")
        log.recover(Component.DATABASE, "*", T0 + 300_000)
        assert log.degraded_fraction(T0, T0 + 600_000) == 0.0
        assert log.degraded_fraction(T0, T0 + 600_000,
                                     blinding_only=False) == pytest.approx(0.5)


class TestSummaryCaveats:
    def test_a_clean_drill_needs_no_caveat(self, log):
        assert log.summary(T0, T0 + 600_000).caveat() is None

    def test_a_mostly_blind_drill_defers_to_the_manual_count(self, log):
        log.degrade(Component.CAMERA, "*", T0, "site power failure")
        log.recover(Component.CAMERA, "*", T0 + 400_000)
        caveat = log.summary(T0, T0 + 600_000).caveat()
        assert "manual roll-call is the authority" in caveat

    def test_a_briefly_blind_drill_warns_about_reading_gaps(self, log):
        log.degrade(Component.CAMERA, "cam-3", T0, "offline")
        log.recover(Component.CAMERA, "cam-3", T0 + 60_000)
        caveat = log.summary(T0, T0 + 600_000).caveat()
        assert "unobserved rather than as absence" in caveat

    def test_a_non_blinding_outage_says_the_system_kept_watching(self, log):
        log.degrade(Component.CENTRAL_LINK, "*", T0, "wan down")
        log.recover(Component.CENTRAL_LINK, "*", T0 + 60_000)
        caveat = log.summary(T0, T0 + 600_000).caveat()
        assert "kept" in caveat and "watching" in caveat

    def test_the_summary_counts_outages_by_component(self, log):
        log.degrade(Component.CAMERA, "cam-1", T0, "offline")
        log.degrade(Component.CAMERA, "cam-2", T0, "offline")
        log.degrade(Component.DATABASE, "*", T0, "down")
        by_component = log.summary(T0, T0 + 600_000).by_component
        assert by_component[Component.CAMERA] == 2
        assert by_component[Component.DATABASE] == 1
