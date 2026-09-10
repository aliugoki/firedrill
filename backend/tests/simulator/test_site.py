"""The building is the one the module docstring describes.

`default_site` is deliberately awkward -- a blind stairwell per floor, an
uncovered fire exit, a double-covered main entrance -- and every property proved
against the simulator is really a property proved against those holes. If a
polygon is edited until a hole closes, the properties keep passing and stop
meaning what they say. These check the holes are still there.
"""

from __future__ import annotations

import pytest

from app.core.presence_fsm import ZoneKind
from app.simulator.site import default_site
from app.vendor.visiontrack.zones_math import point_in_polygon


@pytest.fixture(scope="module")
def built():
    return default_site()


def sample(polygon, steps: int = 9):
    """A grid of points inside a polygon's bounding box, keeping the inside."""
    xs = [point[0] for point in polygon]
    ys = [point[1] for point in polygon]
    for i in range(1, steps):
        for j in range(1, steps):
            point = (xs[0] + (max(xs) - min(xs)) * i / steps,
                     ys[0] + (max(ys) - min(ys)) * j / steps)
            if point_in_polygon(point, polygon):
                yield point


class TestDeclaredCoverageMatchesGeometry:
    """`covers_zones` is intent; the coverage polygon is what the simulator
    acts on. When the two disagree, the drawing is wrong and every run since is
    measuring a different building than the one anybody described."""

    def test_every_declared_zone_exists_on_the_camera_s_floor(self, built):
        for camera in built.cameras:
            for zone_id in camera.covers_zones:
                zone = built.zone(zone_id)
                assert zone is not None, f"{camera.camera_id} names {zone_id}"
                assert zone.floor_id == camera.floor_id

    def test_a_camera_can_actually_see_most_of_what_it_claims(self, built):
        for camera in built.cameras:
            for zone_id in camera.covers_zones:
                points = list(sample(built.zone(zone_id).polygon))
                assert points
                seen = sum(1 for point in points if camera.sees(point))
                assert seen / len(points) >= 0.8, (
                    f"{camera.camera_id} claims {zone_id} but sees "
                    f"{seen}/{len(points)} of it")

    def test_nothing_claims_a_zone_it_cannot_see_at_all(self, built):
        declared = {zone_id for camera in built.cameras
                    for zone_id in camera.covers_zones}
        for zone_id in declared:
            zone = built.zone(zone_id)
            assert any(camera.sees(point)
                       for camera in built.cameras
                       if camera.floor_id == zone.floor_id
                       for point in sample(zone.polygon))


class TestTheHolesAreStillThere:

    def test_every_floor_has_a_stairwell_nobody_covers(self, built):
        stairwells = [zone for zone in built.zones
                      if zone.kind is ZoneKind.BLIND]
        assert len(stairwells) == 5
        for zone in stairwells:
            for point in sample(zone.polygon):
                assert not built.cameras_seeing(zone.floor_id, point), (
                    f"{zone.zone_id} is no longer blind")

    def test_the_fire_exit_is_uncovered(self, built):
        zone = built.zone("exit-fire")
        assert all(not built.cameras_seeing(zone.floor_id, point)
                   for point in sample(zone.polygon))

    def test_the_main_exit_is_seen_by_two_cameras_at_once(self, built):
        zone = built.zone("exit-main")
        doubled = [point for point in sample(zone.polygon)
                   if len(built.cameras_seeing(zone.floor_id, point)) >= 2]
        assert doubled, "the overlapping-coverage case has gone"

    def test_both_assembly_points_are_covered(self, built):
        for zone in built.assembly_zones():
            points = list(sample(zone.polygon))
            seen = sum(1 for point in points
                       if built.cameras_seeing(zone.floor_id, point))
            assert seen == len(points)
