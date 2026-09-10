"""Where the evacuation is slow, and refusing to invent the numbers that aren't there."""

from __future__ import annotations

import pytest

from app.core.presence_fsm import ZoneKind
from app.ingest.bottlenecks import BottleneckTracker

T0 = 1_788_000_000_000


def tracker(**capacities) -> BottleneckTracker:
    return BottleneckTracker(capacities=dict(capacities))


def walk_through(t, person, zone, entered, left=None):
    t.observe(person, zone, ZoneKind.EXIT, entered)
    if left is not None:
        t.observe(person, "assembly-north", ZoneKind.ASSEMBLY, left)


class TestMeasuringAnExit:
    def test_people_through_are_counted(self):
        t = tracker()
        for i in range(10):
            walk_through(t, f"gp-{i}", "exit-main", T0 + i * 1_000,
                         T0 + i * 1_000 + 5_000)
        [exit_main] = t.measure(T0 + 120_000, started_ms=T0).exits
        assert exit_main.completed == 10
        assert exit_main.queue == 0

    def test_people_still_in_the_zone_are_the_queue(self):
        t = tracker()
        for i in range(5):
            walk_through(t, f"gp-{i}", "exit-main", T0 + 10_000)
        [exit_main] = t.measure(T0 + 60_000, started_ms=T0).exits
        assert exit_main.queue == 5
        assert exit_main.completed == 0

    def test_throughput_is_per_minute(self):
        t = tracker()
        for i in range(30):
            walk_through(t, f"gp-{i}", "exit-main", T0 + i * 1_000,
                         T0 + i * 1_000 + 2_000)
        [exit_main] = t.measure(T0 + 60_000, started_ms=T0).exits
        assert exit_main.throughput_per_min == pytest.approx(30.0)

    def test_dwell_reports_the_middle_and_the_worst(self):
        t = tracker()
        for i, seconds in enumerate((2, 4, 6, 8, 30)):
            walk_through(t, f"gp-{i}", "exit-main", T0,
                         T0 + seconds * 1_000)
        [exit_main] = t.measure(T0 + 120_000, started_ms=T0).exits
        assert exit_main.median_dwell_s == 6.0
        assert exit_main.worst_dwell_s == 30.0

    def test_exits_are_measured_separately(self):
        t = tracker()
        walk_through(t, "gp-1", "exit-main", T0, T0 + 2_000)
        walk_through(t, "gp-2", "exit-fire", T0, T0 + 2_000)
        panel = t.measure(T0 + 120_000, started_ms=T0)
        assert {e.zone_id for e in panel.exits} == {"exit-main", "exit-fire"}


class TestDensityIsNotInvented:
    """A crowding figure against a guessed denominator is worse than none: it
    is the kind of number that ends up in a report."""

    def test_a_zone_with_no_capacity_reports_no_density(self):
        t = tracker()
        for i in range(20):
            walk_through(t, f"gp-{i}", "exit-fire", T0)
        [exit_fire] = t.measure(T0 + 60_000, started_ms=T0).exits
        assert exit_fire.density is None
        assert exit_fire.capacity is None

    def test_and_the_panel_says_why(self):
        t = tracker()
        walk_through(t, "gp-1", "exit-fire", T0)
        panel = t.measure(T0 + 60_000, started_ms=T0)
        assert any("No capacity recorded for exit-fire" in c
                   for c in panel.caveats)

    def test_a_zone_with_a_capacity_reports_density(self):
        t = tracker(**{"exit-main": 40})
        for i in range(10):
            walk_through(t, f"gp-{i}", "exit-main", T0)
        [exit_main] = t.measure(T0 + 60_000, started_ms=T0).exits
        assert exit_main.density == pytest.approx(0.25)


class TestFindingTheLimitingExit:
    """The question an incident commander actually has: where do I send someone."""

    def test_a_clearing_exit_is_not_congested(self):
        t = tracker()
        for i in range(20):
            walk_through(t, f"gp-{i}", "exit-main", T0 + i * 500,
                         T0 + i * 500 + 3_000)
        panel = t.measure(T0 + 120_000, started_ms=T0)
        assert panel.limiting is None

    def test_a_slow_queue_is_named(self):
        t = tracker()
        # Main exit clears quickly.
        for i in range(20):
            walk_through(t, f"main-{i}", "exit-main", T0 + i * 500,
                         T0 + i * 500 + 3_000)
        # Fire exit is slow and still holding people.
        for i in range(8):
            walk_through(t, f"fire-{i}", "exit-fire", T0,
                         T0 + 45_000)
        for i in range(5):
            walk_through(t, f"stuck-{i}", "exit-fire", T0 + 50_000)

        limiting = t.measure(T0 + 120_000, started_ms=T0).limiting
        assert limiting is not None
        assert limiting.zone_id == "exit-fire"

    def test_congestion_does_not_depend_on_a_capacity(self):
        # Most sites never record one, and a queue that is not clearing is
        # observable without it.
        t = tracker()
        for i in range(6):
            walk_through(t, f"gp-{i}", "exit-fire", T0, T0 + 40_000)
        for i in range(3):
            walk_through(t, f"waiting-{i}", "exit-fire", T0 + 45_000)
        [exit_fire] = t.measure(T0 + 120_000, started_ms=T0).exits
        assert exit_fire.capacity is None
        assert exit_fire.is_congested is True


class TestHonestyAboutTheWindow:
    def test_throughput_is_withheld_early_in_a_drill(self):
        # Ten people in five seconds extrapolates to 120 a minute, which is a
        # number about the sample rather than about the building.
        t = tracker()
        for i in range(10):
            walk_through(t, f"gp-{i}", "exit-main", T0, T0 + 1_000)
        panel = t.measure(T0 + 5_000, started_ms=T0)
        assert panel.exits[0].throughput_per_min is None
        assert any("not yet meaningful" in c for c in panel.caveats)

    def test_both_caveats_appear_when_both_are_true(self):
        # A single caveat slot would drop one of them, and a caveat is exactly
        # the thing that must not be dropped.
        t = tracker()
        walk_through(t, "gp-1", "exit-main", T0, T0 + 1_000)
        panel = t.measure(T0 + 5_000, started_ms=T0)
        assert len(panel.caveats) == 2

    def test_an_unobserved_building_says_so(self):
        panel = tracker().measure(T0 + 120_000, started_ms=T0)
        assert panel.exits == ()
        assert "nothing can be said" in "\n".join(panel.describe())

    def test_a_drill_that_has_not_started_produces_no_throughput(self):
        t = tracker()
        walk_through(t, "gp-1", "exit-main", T0, T0 + 1_000)
        assert t.measure(T0 + 60_000).exits[0].throughput_per_min is None


class TestOverlappingCameras:
    def test_one_person_seen_twice_is_one_passage(self):
        # The double-covered main exit. Counting them twice would inflate
        # throughput and halve the apparent dwell.
        t = tracker()
        t.observe("gp-1", "exit-main", ZoneKind.EXIT, T0)
        t.observe("gp-1", "exit-main", ZoneKind.EXIT, T0 + 1_000)
        t.observe("gp-1", "exit-main", ZoneKind.EXIT, T0 + 2_000)
        t.observe("gp-1", "assembly-north", ZoneKind.ASSEMBLY, T0 + 5_000)
        [exit_main] = t.measure(T0 + 60_000, started_ms=T0).exits
        assert exit_main.completed == 1

    def test_time_across_exits_is_unioned_not_summed(self):
        t = tracker()
        t.observe("gp-1", "exit-main", ZoneKind.EXIT, T0)
        t.observe("gp-1", "exit-fire", ZoneKind.EXIT, T0 + 2_000)
        t.observe("gp-1", "assembly-north", ZoneKind.ASSEMBLY, T0 + 6_000)
        assert t.total_exit_time_s("gp-1", T0 + 60_000) == 6.0

    def test_moving_between_exits_closes_the_first(self):
        t = tracker()
        t.observe("gp-1", "exit-main", ZoneKind.EXIT, T0)
        t.observe("gp-1", "exit-fire", ZoneKind.EXIT, T0 + 3_000)
        panel = t.measure(T0 + 60_000, started_ms=T0)
        by_zone = {e.zone_id: e for e in panel.exits}
        assert by_zone["exit-main"].completed == 1
        assert by_zone["exit-fire"].queue == 1


class TestOnlyExitsAreTracked:
    def test_floor_and_assembly_sightings_create_no_crossings(self):
        t = tracker()
        t.observe("gp-1", "floor-2", ZoneKind.FLOOR, T0)
        t.observe("gp-1", "assembly-north", ZoneKind.ASSEMBLY, T0 + 1_000)
        t.observe("gp-1", "stairwell", ZoneKind.BLIND, T0 + 2_000)
        assert t.measure(T0 + 60_000, started_ms=T0).exits == ()
