"""A synthetic building to evacuate.

Deliberately awkward, because a building that is easy to cover proves nothing.
`default_site` includes the things that actually break accountability:

  * a **basement** whose only route out passes through a stairwell nobody has a
    camera in;
  * a **blind stairwell** on every floor, so the expected behaviour is losing
    people for tens of seconds at a time;
  * **two assembly points**, so a person can be at the wrong one and a warden
    can be looking at a list that does not contain them;
  * an **exit with no camera**, so some people reach assembly having never been
    observed leaving;
  * **overlapping camera coverage** at the main exit, so the same person is
    seen twice at once and must not be counted twice.

Coordinates are fractional 0..1 within a floor plan, matching the convention the
vendored geometry code uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.presence_fsm import ZoneKind

Point = tuple[float, float]
Polygon = list[Point]


@dataclass(frozen=True, slots=True)
class Zone:
    zone_id: str
    name: str
    kind: ZoneKind
    floor_id: str
    polygon: Polygon
    capacity: int | None = None


@dataclass(frozen=True, slots=True)
class Camera:
    camera_id: str
    floor_id: str
    coverage: Polygon
    #: The zones this camera is meant to watch. Not what `sees` consults --
    #: that is the coverage polygon, and it is the only thing the simulator
    #: acts on. This is the intent, and `tests/simulator/test_site.py` checks
    #: the geometry still matches it, which is what catches a polygon edited
    #: until it no longer covers the zone it was drawn for.
    covers_zones: tuple[str, ...] = ()

    def sees(self, point: Point) -> bool:
        from app.vendor.visiontrack.zones_math import point_in_polygon

        if len(self.coverage) < 3:
            return False
        return point_in_polygon(point, self.coverage)


@dataclass(frozen=True, slots=True)
class Floor:
    floor_id: str
    name: str
    level: int


@dataclass
class Site:
    site_id: str
    name: str
    floors: list[Floor] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    cameras: list[Camera] = field(default_factory=list)

    def zone(self, zone_id: str) -> Zone | None:
        return next((z for z in self.zones if z.zone_id == zone_id), None)

    def zones_of_kind(self, kind: ZoneKind) -> list[Zone]:
        return [z for z in self.zones if z.kind is kind]

    def zones_on(self, floor_id: str) -> list[Zone]:
        return [z for z in self.zones if z.floor_id == floor_id]

    def assembly_zones(self) -> list[Zone]:
        return self.zones_of_kind(ZoneKind.ASSEMBLY)

    def cameras_seeing(self, floor_id: str, point: Point) -> list[Camera]:
        """Every camera with the point in its coverage.

        More than one is normal at a main exit, and is exactly the case that
        must not produce two people.
        """
        return [c for c in self.cameras if c.floor_id == floor_id and c.sees(point)]

    def is_blind(self, floor_id: str, point: Point) -> bool:
        return not self.cameras_seeing(floor_id, point)

    def locate(self, floor_id: str, point: Point) -> Zone | None:
        """Which zone a point falls in. First match wins; zones do not overlap."""
        from app.vendor.visiontrack.zones_math import point_in_polygon

        for zone in self.zones_on(floor_id):
            if len(zone.polygon) >= 3 and point_in_polygon(point, zone.polygon):
                return zone
        return None


def _box(x1: float, y1: float, x2: float, y2: float) -> Polygon:
    return [(x1, y1), (x2, y1), (x2, y2), (x1, y2)]


def default_site() -> Site:
    """A five-storey office with a basement, two assembly points, and holes."""
    floors = [
        Floor("basement", "Basement — Plant & Server Room", -1),
        Floor("floor-1", "Ground — Lobby & Reception", 1),
        Floor("floor-2", "Level 2 — Engineering", 2),
        Floor("floor-3", "Level 3 — Finance & HR", 3),
        Floor("floor-4", "Level 4 — Operations", 4),
    ]

    zones: list[Zone] = []
    cameras: list[Camera] = []

    for floor in floors:
        fid = floor.floor_id
        zones.append(Zone(f"{fid}-open", f"{floor.name} open plan", ZoneKind.FLOOR,
                          fid, _box(0.0, 0.0, 0.6, 1.0), capacity=90))
        zones.append(Zone(f"{fid}-corridor", f"{floor.name} corridor", ZoneKind.FLOOR,
                          fid, _box(0.6, 0.0, 0.8, 1.0), capacity=30))
        # Every floor has a stairwell nobody covers. Losing a track here is the
        # expected outcome, not an anomaly.
        zones.append(Zone(f"{fid}-stair", f"{floor.name} stairwell", ZoneKind.BLIND,
                          fid, _box(0.8, 0.0, 1.0, 1.0), capacity=20))

        cameras.append(Camera(f"cam-{fid}-open", fid, _box(0.0, 0.0, 0.62, 1.0),
                              (f"{fid}-open",)))
        cameras.append(Camera(f"cam-{fid}-corridor", fid, _box(0.58, 0.0, 0.8, 1.0),
                              (f"{fid}-corridor",)))
        # Note: no camera on `{fid}-stair`.

    # Ground floor exits. The main exit is double-covered; the fire exit is not
    # covered at all, so some people leave unobserved.
    zones.append(Zone("exit-main", "Main entrance", ZoneKind.EXIT, "floor-1",
                      _box(0.0, 1.0, 0.5, 1.2), capacity=40))
    zones.append(Zone("exit-fire", "Fire exit (uncovered)", ZoneKind.EXIT, "floor-1",
                      _box(0.5, 1.0, 1.0, 1.2), capacity=40))
    cameras.append(Camera("cam-exit-main-a", "floor-1", _box(0.0, 0.95, 0.5, 1.2),
                          ("exit-main",)))
    cameras.append(Camera("cam-exit-main-b", "floor-1", _box(0.1, 0.98, 0.5, 1.25),
                          ("exit-main",)))

    # Two assembly points on their own plan.
    zones.append(Zone("assembly-north", "North Car Park", ZoneKind.ASSEMBLY,
                      "outside", _box(0.0, 0.0, 0.5, 1.0), capacity=250))
    zones.append(Zone("assembly-south", "South Garden", ZoneKind.ASSEMBLY,
                      "outside", _box(0.5, 0.0, 1.0, 1.0), capacity=180))
    cameras.append(Camera("cam-assembly-north", "outside", _box(0.0, 0.0, 0.5, 1.0),
                          ("assembly-north",)))
    cameras.append(Camera("cam-assembly-south", "outside", _box(0.5, 0.0, 1.0, 1.0),
                          ("assembly-south",)))
    floors.append(Floor("outside", "Assembly area", 0))

    return Site(site_id="site-sialkot-office", name="Sialkot Office",
                floors=floors, zones=zones, cameras=cameras)
