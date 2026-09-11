"""Evacuation timing and percentile honesty."""

import pytest

from app.core.timing import (
    MIN_SAMPLES_FOR_P95,
    DrillTiming,
    ExclusionReason,
    measure,
    percentile,
    summarise,
    summarise_by,
)

T0 = 1_788_000_000_000


def timing_of(person_id: str, seconds: float | None, **overrides):
    return measure(
        person_id=person_id,
        drill_started_ms=T0,
        assembly_arrival_ms=None if seconds is None else T0 + int(seconds * 1000),
        **overrides,
    )


def population(seconds: list[float], prefix="EMP"):
    return [timing_of(f"{prefix}-{i}", s) for i, s in enumerate(seconds)]


class TestPercentile:
    def test_it_returns_an_observed_value_not_an_interpolation(self):
        # For a life-safety number, an interpolated 118.4 s that nobody took is
        # worse than the real 121 s that somebody did.
        values = [10.0, 20.0, 30.0, 40.0]
        for p in (1, 25, 50, 75, 99, 100):
            assert percentile(values, p) in values

    def test_nearest_rank_boundaries(self):
        values = [float(i) for i in range(1, 101)]
        assert percentile(values, 50) == 50.0
        assert percentile(values, 95) == 95.0
        assert percentile(values, 100) == 100.0

    def test_an_empty_sample_is_none_not_zero(self):
        # "Nobody evacuated in 0 seconds" and "we have no measurements" must not
        # look alike on a dashboard.
        assert percentile([], 95) is None

    def test_a_single_sample_is_itself(self):
        assert percentile([42.0], 95) == 42.0

    def test_out_of_range_percentiles_are_refused(self):
        with pytest.raises(ValueError):
            percentile([1.0], 0)
        with pytest.raises(ValueError):
            percentile([1.0], 101)


class TestMeasure:
    def test_a_normal_evacuation_is_timed(self):
        t = timing_of("EMP-1", 95.0)
        assert t.duration_s == 95.0
        assert t.excluded_because is None

    def test_someone_never_observed_has_no_time(self):
        t = measure(person_id="EMP-1", drill_started_ms=T0,
                    assembly_arrival_ms=None, was_observed=False)
        assert t.duration_s is None
        assert t.excluded_because is ExclusionReason.NEVER_OBSERVED

    def test_someone_who_never_arrived_has_no_time(self):
        t = timing_of("EMP-1", None)
        assert t.excluded_because is ExclusionReason.NEVER_REACHED_ASSEMBLY

    def test_someone_already_at_the_muster_point_is_excluded(self):
        # Real, and not an evacuation. Timing it would flatter the distribution.
        t = measure(person_id="EMP-1", drill_started_ms=T0,
                    assembly_arrival_ms=T0 - 5_000)
        assert t.excluded_because is ExclusionReason.ARRIVED_BEFORE_START
        assert t.duration_s is None

    def test_without_a_drill_start_nothing_is_timed(self):
        t = measure(person_id="EMP-1", drill_started_ms=None,
                    assembly_arrival_ms=T0 + 1_000)
        assert t.excluded_because is ExclusionReason.NO_DRILL_START


class TestSummaryHonesty:
    def test_a_small_sample_is_marked_unreliable(self):
        summary = summarise(population([float(i) for i in range(10)]))
        assert summary.is_reliable is False
        assert any("not a distribution" in c for c in summary.caveats())

    def test_a_large_sample_stands_on_its_own(self):
        summary = summarise(population([90.0 + i * 0.1 for i in range(200)]))
        assert summary.is_reliable is True
        assert summary.caveats() == ()

    def test_the_reliability_threshold_is_where_p95_stops_being_the_max(self):
        small = [float(i) for i in range(MIN_SAMPLES_FOR_P95 - 1)]
        assert percentile(small, 95) == max(small)
        big = [float(i) for i in range(MIN_SAMPLES_FOR_P95)]
        assert percentile(big, 95) < max(big)

    def test_no_measurements_says_so_rather_than_reporting_zero(self):
        summary = summarise([timing_of("EMP-1", None)])
        assert summary.p95 is None
        assert summary.sample_size == 0
        assert any("No measurements" in c for c in summary.caveats())

    def test_heavy_exclusion_is_surfaced_beside_the_number(self):
        # A good P95 achieved by losing track of the slow people is the most
        # dangerous way this metric could be gamed, so low coverage is loud.
        timed = population([90.0 + i * 0.1 for i in range(30)])
        untracked = [timing_of(f"LOST-{i}", None) for i in range(30)]
        summary = summarise(timed + untracked)
        assert summary.coverage == pytest.approx(0.5)
        assert any("excluded" in c for c in summary.caveats())

    def test_both_problems_are_said_when_both_apply(self):
        """A small sample and a low coverage are different problems.

        One says the percentile is weak; the other says it describes almost
        nobody and may have improved by losing people. Returning the first
        meant a drill where 92 of 100 produced no timing printed "Only 8
        measurements" and never mentioned the 92.
        """
        timed = population([90.0 + i for i in range(8)])
        untracked = [timing_of(f"LOST-{i}", None) for i in range(92)]
        summary = summarise(timed + untracked)

        notes = summary.caveats()
        assert len(notes) == 2
        assert any("not a distribution" in c for c in notes)
        assert any("92% excluded" in c for c in notes)

    def test_exclusions_are_broken_down_by_reason(self):
        people = (
            population([90.0])
            + [timing_of("A", None)]
            + [measure(person_id="B", drill_started_ms=T0,
                       assembly_arrival_ms=None, was_observed=False)]
        )
        summary = summarise(people)
        assert summary.exclusion_reasons == {
            ExclusionReason.NEVER_REACHED_ASSEMBLY.value: 1,
            ExclusionReason.NEVER_OBSERVED.value: 1,
        }

    def test_excluded_people_are_counted_not_hidden(self):
        summary = summarise(population([90.0, 100.0]) + [timing_of("X", None)])
        assert summary.sample_size == 2
        assert summary.excluded == 1


class TestGrouping:
    def test_a_building_average_hides_the_problem_floor(self):
        fast = [timing_of(f"F1-{i}", 60.0, floor_id="floor-1") for i in range(30)]
        slow = [timing_of(f"F5-{i}", 200.0, floor_id="floor-5") for i in range(30)]
        building = summarise(fast + slow)
        by_floor = summarise_by(fast + slow, "floor_id")
        assert building.p50 == 60.0          # the building looks fine
        assert by_floor["floor-5"].p50 == 200.0   # floor 5 does not
        assert by_floor["floor-1"].p95 == 60.0

    def test_grouping_by_zone(self):
        people = ([timing_of(f"A-{i}", 80.0, zone_id="assembly-north") for i in range(5)]
                  + [timing_of(f"B-{i}", 140.0, zone_id="assembly-south") for i in range(5)])
        by_zone = summarise_by(people, "zone_id")
        assert set(by_zone) == {"assembly-north", "assembly-south"}
        assert by_zone["assembly-south"].maximum == 140.0

    def test_an_unknown_grouping_key_is_refused(self):
        with pytest.raises(ValueError):
            summarise_by([], "department")


class TestDrillTiming:
    def _drill(self, seconds, completed_ms=None, target=120.0):
        people = population(seconds)
        return DrillTiming(
            building=summarise(people),
            by_floor=summarise_by(people, "floor_id"),
            by_zone=summarise_by(people, "zone_id"),
            accountability_completed_ms=completed_ms,
            target_p95_s=target,
        )

    def test_a_met_target_reads_as_met(self):
        drill = self._drill([90.0 + i * 0.1 for i in range(50)])
        assert drill.meets_target is True

    def test_a_missed_target_reads_as_missed(self):
        drill = self._drill([150.0 + i * 0.1 for i in range(50)])
        assert drill.meets_target is False

    def test_too_little_data_is_neither_pass_nor_fail(self):
        # None is a real answer. It means the drill could not say, which is
        # different from failing and very different from passing.
        drill = self._drill([90.0, 95.0, 100.0])
        assert drill.meets_target is None

    def test_accountability_completion_is_separate_from_evacuation(self):
        # The building empties in two minutes; establishing that nobody is left
        # takes as long as the last uncertain person takes to resolve.
        drill = self._drill([90.0 + i * 0.1 for i in range(50)],
                            completed_ms=480_000)
        assert drill.building.p95 < 100
        assert drill.accountability_completion_s == 480.0

    def test_completion_is_none_when_the_drill_never_settled(self):
        drill = self._drill([90.0 + i * 0.1 for i in range(50)])
        assert drill.accountability_completion_s is None
