"""The VisionTrack code vendored into app/vendor/visiontrack still works here.

Phase 0 gate. This is not a re-run of VisionTrack's own suite; it is a
copy-integrity check. It proves the files import under the rewritten vendor
paths and that the geometry and hold-time behaviour EVAC-120 depends on came
across intact, so Phase 1 can build zone logic on them without re-deriving
anything.

Note the coordinate convention these functions actually use: a polygon is a
sequence of (x, y) tuples, fractional 0..1 relative to the floor-plan image.
Floor-plan JSON stores {"x":, "y":} dicts, so every caller converts. Phase 1
must do the same at its boundary.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.vendor.visiontrack.dwell import union_seconds
from app.vendor.visiontrack.occupancy import bucket_tracks_by_camera, zones_occupancy
from app.vendor.visiontrack.rule_state import RuleState, evaluate_occupancy_rule
from app.vendor.visiontrack.zones_math import (
    count_points_in_polygon,
    point_in_polygon,
    polygon_centroid,
)

SQUARE = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]

# An L-shape, to catch a bounding-box implementation: (0.9, 0.9) is inside the
# bounding box but outside the polygon. Assembly zones drawn around a building
# corner have exactly this shape.
L_SHAPE = [(0.0, 0.0), (1.0, 0.0), (1.0, 0.5), (0.5, 0.5), (0.5, 1.0), (0.0, 1.0)]


def _clean_state() -> RuleState:
    return RuleState(condition_since_ms=None, armed=False, last_fire_ms=None)


class TestPointInPolygon:
    def test_interior_point_is_inside(self):
        assert point_in_polygon((0.5, 0.5), SQUARE) is True

    def test_exterior_point_is_outside(self):
        assert point_in_polygon((1.5, 0.5), SQUARE) is False

    def test_concave_notch_is_outside(self):
        assert point_in_polygon((0.9, 0.9), L_SHAPE) is False
        assert point_in_polygon((0.2, 0.9), L_SHAPE) is True

    def test_degenerate_polygon_raises(self):
        # A zone a user started drawing but never closed must not silently
        # swallow, or silently exclude, everyone on the floor. It raises, and
        # Phase 1 must decide explicitly what a malformed zone means.
        with pytest.raises(ValueError):
            point_in_polygon((0.5, 0.5), [(0.0, 0.0), (1.0, 0.0)])


class TestPolygonHelpers:
    def test_centroid_of_square_is_its_middle(self):
        cx, cy = polygon_centroid(SQUARE)
        assert abs(cx - 0.5) < 1e-9
        assert abs(cy - 0.5) < 1e-9

    def test_centroid_is_area_weighted_not_vertex_average(self):
        # The L-shape's vertex average and true centroid differ. Getting this
        # wrong puts an assembly-point label outside its own zone.
        cx, cy = polygon_centroid(L_SHAPE)
        vertex_avg_x = sum(p[0] for p in L_SHAPE) / len(L_SHAPE)
        assert abs(cx - vertex_avg_x) > 1e-6
        assert point_in_polygon((cx, cy), L_SHAPE) is True

    def test_count_points_counts_only_interior(self):
        pts = [(0.5, 0.5), (0.1, 0.1), (5.0, 5.0)]
        assert count_points_in_polygon(pts, SQUARE) == 2

    def test_count_of_no_points_is_zero(self):
        assert count_points_in_polygon([], SQUARE) == 0


class TestOccupancy:
    def test_known_and_unknown_are_counted_separately(self):
        # Invariant 5: an unknown person is tracked as unknown, never forced
        # onto an employee. The bucket keeps them apart at the lowest level.
        tracks = [
            {"track_id": "t1", "camera_id": "cam-1", "emp_id": "EMP-1", "name": "A"},
            {"track_id": "t2", "camera_id": "cam-1", "emp_id": None, "name": None},
            {"track_id": "t3", "camera_id": "cam-1", "emp_id": "EMP-2", "name": "B"},
        ]
        per_cam = bucket_tracks_by_camera(tracks)
        assert per_cam["cam-1"]["total"] == 3
        assert per_cam["cam-1"]["known"] == 2
        assert per_cam["cam-1"]["unknown"] == 1

    def test_zone_occupancy_sums_only_cameras_inside_the_zone(self):
        floor_plans = [{
            "id": "fp-1",
            "name": "Ground floor",
            "zones": [{"id": "assembly-north", "name": "North Car Park",
                       "polygon": [{"x": 0.0, "y": 0.0}, {"x": 0.5, "y": 0.0},
                                   {"x": 0.5, "y": 0.5}, {"x": 0.0, "y": 0.5}]}],
            "markers": [{"camera_id": "cam-in", "x": 0.25, "y": 0.25},
                        {"camera_id": "cam-out", "x": 0.9, "y": 0.9}],
        }]
        per_cam = {
            "cam-in": {"total": 4, "known": 3, "unknown": 1, "people": []},
            "cam-out": {"total": 7, "known": 7, "unknown": 0, "people": []},
        }
        rows = zones_occupancy(floor_plans, per_cam)
        assert len(rows) == 1
        assert rows[0]["zone_id"] == "assembly-north"
        assert rows[0]["camera_ids"] == ["cam-in"]
        assert rows[0]["total"] == 4
        assert rows[0]["unknown"] == 1

    def test_malformed_zone_is_skipped_not_counted(self):
        floor_plans = [{
            "id": "fp-1", "name": "Ground floor",
            "zones": [{"id": "half-drawn", "name": "Half drawn",
                       "polygon": [{"x": 0.0, "y": 0.0}, {"x": 0.5, "y": 0.0}]}],
            "markers": [{"camera_id": "cam-in", "x": 0.25, "y": 0.25}],
        }]
        per_cam = {"cam-in": {"total": 4, "known": 3, "unknown": 1, "people": []}}
        assert zones_occupancy(floor_plans, per_cam) == []


class TestDwellIntervals:
    def test_union_seconds_merges_overlap(self):
        t0 = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
        # Two cameras see the same person over overlapping windows. The union is
        # 90 s, not the 120 s a naive sum gives. That difference is a person
        # double-counted at an assembly point.
        intervals = [(t0, t0 + timedelta(seconds=60)),
                     (t0 + timedelta(seconds=30), t0 + timedelta(seconds=90))]
        assert union_seconds(intervals) == 90.0

    def test_union_seconds_keeps_disjoint_windows_separate(self):
        t0 = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)
        intervals = [(t0, t0 + timedelta(seconds=30)),
                     (t0 + timedelta(seconds=60), t0 + timedelta(seconds=90))]
        assert union_seconds(intervals) == 60.0

    def test_union_seconds_of_nothing_is_zero(self):
        assert union_seconds([]) == 0.0


# A realistic wall-clock millisecond timestamp. See TestZeroTimestampBug for why
# these tests must not start the clock at 0.
T0 = 1_788_000_000_000


class TestHoldTimeAndHysteresis:
    """The behaviour EVAC-120 reuses for presence, so it has to survive the copy
    exactly: a threshold crossing must persist for hold_time before it counts,
    which is what stops one noisy frame flipping a person's state."""

    RULE = {"kind": "occupancy_max", "threshold": 5, "hold_time_s": 30}

    def test_breach_does_not_fire_before_hold_time(self):
        out = evaluate_occupancy_rule(
            **self.RULE, current_count=9, state=_clean_state(), now_ms=T0)
        assert out.fire is False

    def test_breach_fires_once_hold_time_elapses(self):
        first = evaluate_occupancy_rule(
            **self.RULE, current_count=9, state=_clean_state(), now_ms=T0)
        later = evaluate_occupancy_rule(
            **self.RULE, current_count=9, state=first.new_state, now_ms=T0 + 31_000)
        assert later.fire is True

    def test_transient_spike_never_fires(self):
        spike = evaluate_occupancy_rule(
            **self.RULE, current_count=9, state=_clean_state(), now_ms=T0)
        recovered = evaluate_occupancy_rule(
            **self.RULE, current_count=1, state=spike.new_state, now_ms=T0 + 5_000)
        after = evaluate_occupancy_rule(
            **self.RULE, current_count=9, state=recovered.new_state, now_ms=T0 + 10_000)
        assert after.fire is False

    def test_armed_rule_does_not_refire_while_condition_persists(self):
        # Hysteresis: one sustained breach is one event, not one per tick.
        s = _clean_state()
        fired = 0
        for t in range(0, 120_000, 5_000):
            out = evaluate_occupancy_rule(
                **self.RULE, current_count=9, state=s, now_ms=T0 + t)
            fired += int(out.fire)
            s = out.new_state
        assert fired == 1


class TestZeroTimestampBug:
    """Upstream defect, found while vendoring. Recorded, not worked around.

    `evaluate_occupancy_rule` starts its hold timer with

        since = state.condition_since_ms or now_ms

    A `condition_since_ms` of exactly 0 is falsy, so the timer silently restarts
    on every tick and the rule can never fire. VisionTrack never hits this
    because it passes epoch milliseconds, which are never 0.

    EVAC-120 can hit it. Camera events carry `pts_ms`, the frame presentation
    timestamp, and PTS starts at 0 for the first frame of a stream. A drill that
    begins at the start of a recording would have its first ticks stamped 0.

    Phase 1 must therefore either normalise every timestamp to epoch
    milliseconds at the ingest boundary, or fix the falsy-zero check when this
    logic is reimplemented in `presence_fsm.py`. Filed against VisionTrack
    `backend/app/modules/alerts/evaluator.py`.
    """

    RULE = {"kind": "occupancy_max", "threshold": 5, "hold_time_s": 30}

    @pytest.mark.xfail(
        reason="upstream falsy-zero bug: condition_since_ms=0 restarts the "
               "hold timer, so the rule never fires. Flips to XPASS when fixed.",
        strict=True,
    )
    def test_hold_timer_survives_a_zero_start_timestamp(self):
        first = evaluate_occupancy_rule(
            **self.RULE, current_count=9, state=_clean_state(), now_ms=0)
        assert first.new_state.condition_since_ms == 0
        later = evaluate_occupancy_rule(
            **self.RULE, current_count=9, state=first.new_state, now_ms=31_000)
        assert later.fire is True

    def test_the_bug_is_exactly_a_falsy_zero_and_nothing_wider(self):
        # One millisecond later and the same sequence works, which pins the
        # cause to the falsy check rather than to anything about the hold logic.
        first = evaluate_occupancy_rule(
            **self.RULE, current_count=9, state=_clean_state(), now_ms=1)
        later = evaluate_occupancy_rule(
            **self.RULE, current_count=9, state=first.new_state, now_ms=31_001)
        assert later.fire is True


class TestHomographyProjection:
    """Turning a camera bounding box into a floor-plan point. Every exit and
    assembly-zone event in EVAC-120 is decided by this projection, so the
    failure modes matter more than the happy path."""

    # Identity homography: pixel coordinates pass straight through.
    IDENTITY = [1.0, 0.0, 0.0,
                0.0, 1.0, 0.0,
                0.0, 0.0, 1.0]

    def test_foot_point_is_the_bottom_centre_of_the_box(self):
        from app.vendor.visiontrack.zone_resolve import project_foot

        # A person's feet touch the floor at the bottom-centre of their box.
        # Using the centre instead puts them half a body-length off, which at an
        # exit line is the difference between inside and outside the building.
        assert project_foot(self.IDENTITY, [10.0, 20.0, 30.0, 60.0]) == (20.0, 60.0)

    def test_missing_homography_yields_no_point(self):
        from app.vendor.visiontrack.zone_resolve import project_foot

        # Invariant 1: an uncalibrated camera produces no position, not a
        # position of (0, 0), which would put everyone in whichever zone
        # contains the floor-plan origin.
        assert project_foot(None, [10.0, 20.0, 30.0, 60.0]) is None
        assert project_foot([1.0, 0.0, 0.0], [10.0, 20.0, 30.0, 60.0]) is None

    def test_unusable_bbox_yields_no_point(self):
        from app.vendor.visiontrack.zone_resolve import project_foot

        assert project_foot(self.IDENTITY, None) is None
        assert project_foot(self.IDENTITY, [10.0, 20.0]) is None

    def test_degenerate_projection_yields_no_point(self):
        from app.vendor.visiontrack.zone_resolve import project_foot

        # A homography whose bottom row zeroes out maps the point to infinity.
        degenerate = [1.0, 0.0, 0.0,
                      0.0, 1.0, 0.0,
                      0.0, 0.0, 0.0]
        assert project_foot(degenerate, [10.0, 20.0, 30.0, 60.0]) is None

    def test_a_camera_with_no_zones_is_not_indexed(self):
        from app.vendor.visiontrack.zone_resolve import camera_plan_zones

        plans = [{"id": "fp-1", "name": "Ground floor", "zones": [],
                  "markers": [{"camera_id": "cam-1", "x": 0.5, "y": 0.5}]}]
        assert camera_plan_zones(plans) == {}

    def test_camera_is_indexed_against_the_plan_it_is_placed_on(self):
        from app.vendor.visiontrack.zone_resolve import camera_plan_zones

        plans = [{
            "id": "fp-1", "name": "Ground floor",
            "zones": [{"id": "assembly-north", "name": "North Car Park",
                       "polygon": [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0},
                                   {"x": 1.0, "y": 1.0}, {"x": 0.0, "y": 1.0}]}],
            "markers": [{"camera_id": "cam-1", "x": 0.5, "y": 0.5}],
        }]
        index = camera_plan_zones(plans)
        assert list(index) == ["cam-1"]
        plan_id, plan_name, zones = index["cam-1"][0]
        assert plan_id == "fp-1"
        assert zones[0][0] == "assembly-north"
