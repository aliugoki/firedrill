"""Transforming VisionTrack floor plans and cameras into an EVAC-120 site.

The shapes below are the real ones, read from a live VisionTrack instance rather
than inferred from its models:

    zone    {id, name, color, rules[], polygon: [{x, y}]}
    marker  {id, x, y, camera_id, label, cone_range, cone_angle_deg, ...}
    camera  calibration {homography[9], src_points, dst_points, image_ref,
                         floor_plan_id, calibrated_at}

Coordinates are fractional 0..1 against the floor-plan image, which is the
convention the vendored geometry code already uses, so no rescaling is needed.

Three things are checked on the way in, and each of them has bitten a system
somewhere:

**A homography must be nine numbers.** A camera whose calibration is half-saved
projects every person to the same place, and that place is inside whichever zone
contains it. The sync rejects it rather than importing a camera that will
confidently put the whole building in one room.

**A polygon needs three vertices.** A zone somebody started drawing and never
closed is not a small zone, it is a shape with no inside.

**A camera on no floor plan cannot resolve zones.** It still sees people, so it
is imported and flagged rather than dropped: losing a camera silently is worse
than importing one that only contributes presence.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.presence_fsm import ZoneKind
from app.sync.zone_kinds import Proposal, TaggingReport, propose, review


@dataclass(frozen=True, slots=True)
class SyncedZone:
    zone_id: str
    name: str
    kind: ZoneKind
    floor_plan_id: str
    polygon: tuple

    @property
    def is_assembly(self) -> bool:
        return self.kind is ZoneKind.ASSEMBLY


@dataclass(frozen=True, slots=True)
class SyncedCamera:
    camera_id: str
    floor_plan_id: str | None
    homography: tuple | None
    marker: tuple | None = None

    @property
    def can_resolve_zones(self) -> bool:
        """Whether this camera can say *where* someone is, not just that they are.

        Without a homography a detection is a box in a frame, not a point on a
        floor. Such a camera still contributes presence and identity; it cannot
        contribute a zone, and therefore cannot tell anyone somebody reached the
        assembly point.
        """
        return self.homography is not None and self.floor_plan_id is not None


@dataclass(frozen=True, slots=True)
class SyncedFloorPlan:
    floor_plan_id: str
    name: str
    site_id: str
    width_px: int
    height_px: int


@dataclass(frozen=True, slots=True)
class SyncProblem:
    """Something the sync would not import, and why."""

    kind: str
    subject: str
    detail: str

    fatal: bool = False
    """Whether this alone stops a drill.

    Nothing in `sync` currently sets it, and that is the policy rather than an
    oversight: every geometry problem it can find is recoverable. A camera with
    a broken homography is imported anyway, because losing a camera silently is
    worse than keeping one that only contributes presence, and a zone with a
    shape that has no inside is skipped -- if it was the only assembly zone,
    the tagging report says so in words an operator can act on.

    The mechanism stays because `blocking` and `ready_for_a_drill` consult it,
    and a future importer that finds something genuinely unrecoverable should
    not have to invent the concept. `tests/sync/test_geometry_sync.py` exercises
    the branch so it is known to work rather than merely present.
    """


@dataclass(frozen=True, slots=True)
class SyncResult:
    floor_plans: tuple = ()
    zones: tuple = ()
    cameras: tuple = ()
    problems: tuple = ()
    tagging: TaggingReport | None = None

    @property
    def fatal_problems(self) -> tuple:
        return tuple(p for p in self.problems if p.fatal)

    @property
    def cameras_without_geometry(self) -> tuple:
        return tuple(c for c in self.cameras if not c.can_resolve_zones)

    @property
    def assembly_zones(self) -> tuple:
        return tuple(z for z in self.zones if z.is_assembly)

    @property
    def ready_for_a_drill(self) -> bool:
        """Whether a drill can run on this geometry.

        Deliberately strict. Everything below would produce a drill that runs
        and reports numbers nobody should act on, which is worse than a drill
        that refuses to start.
        """
        return (not self.fatal_problems
                and self.tagging is not None
                and self.tagging.ready
                and bool(self.assembly_zones)
                and any(c.can_resolve_zones for c in self.cameras))

    def blocking(self) -> list[str]:
        """Every reason a drill cannot run on this geometry."""
        reasons: list[str] = []
        for problem in self.fatal_problems:
            reasons.append(f"{problem.subject}: {problem.detail}")
        if self.tagging is not None:
            reasons.extend(self.tagging.blockers)
        if not self.cameras:
            reasons.append("no cameras were imported")
        elif not any(c.can_resolve_zones for c in self.cameras):
            reasons.append(
                "no camera has both a homography and a floor plan, so nothing "
                "can place a person in a zone and nobody can reach an assembly "
                "point")
        return reasons

    def describe(self) -> list[str]:
        lines = [
            "VisionTrack geometry sync",
            f"  floor plans  {len(self.floor_plans)}",
            f"  zones        {len(self.zones)} "
            f"({len(self.assembly_zones)} assembly)",
            f"  cameras      {len(self.cameras)} "
            f"({len(self.cameras_without_geometry)} without geometry)",
            "",
            "READY" if self.ready_for_a_drill else "NOT READY",
        ]
        for reason in self.blocking():
            lines.append(f"  - {reason}")
        if self.problems:
            lines += ["", "Problems:"]
            for problem in self.problems:
                mark = "  FATAL " if problem.fatal else "  warn  "
                lines.append(f"{mark}{problem.subject}: {problem.detail}")
        return lines


def sync(
    *,
    floor_plans: list,
    cameras: list,
    site_id: str,
    tagged_zones: dict | None = None,
) -> SyncResult:
    """Transform VisionTrack rows into EVAC-120 geometry.

    `floor_plans` are rows with `id`, `name`, `width_px`, `height_px`, `zones`
    and `markers`. `cameras` are rows with `id` and `calibration`. Both match
    what VisionTrack's tables actually hold.
    """
    plans: list[SyncedFloorPlan] = []
    zones: list[SyncedZone] = []
    proposals: list[Proposal] = []
    problems: list[SyncProblem] = []
    marker_by_camera: dict[str, tuple] = {}
    plan_by_camera: dict[str, str] = {}

    for row in floor_plans:
        plan_id = str(row["id"])
        plans.append(SyncedFloorPlan(
            floor_plan_id=plan_id, name=row.get("name") or plan_id,
            site_id=site_id, width_px=int(row.get("width_px") or 0),
            height_px=int(row.get("height_px") or 0)))

        for marker in row.get("markers") or []:
            camera_id = marker.get("camera_id")
            if not camera_id:
                continue
            marker_by_camera[str(camera_id)] = (
                float(marker.get("x", 0.0)), float(marker.get("y", 0.0)))
            plan_by_camera[str(camera_id)] = plan_id

        for zone in row.get("zones") or []:
            zone_id = str(zone.get("id"))
            name = zone.get("name") or zone_id
            polygon = tuple(
                (float(point["x"]), float(point["y"]))
                for point in (zone.get("polygon") or [])
                if "x" in point and "y" in point)

            if len(polygon) < 3:
                # Not a small zone. A shape with no inside.
                problems.append(SyncProblem(
                    kind="polygon", subject=name,
                    detail=f"only {len(polygon)} vertices; a zone needs at "
                           "least 3 to contain anybody",
                    fatal=False))
                continue

            proposal = propose(zone_id, name, tagged_zones)
            proposals.append(proposal)
            if proposal.is_usable:
                zones.append(SyncedZone(
                    zone_id=zone_id, name=name, kind=proposal.kind,
                    floor_plan_id=plan_id, polygon=polygon))

    synced_cameras: list[SyncedCamera] = []
    for row in cameras:
        camera_id = str(row["id"])
        calibration = row.get("calibration") or {}
        raw = calibration.get("homography")
        homography = None

        if raw is not None:
            if not isinstance(raw, (list, tuple)) or len(raw) != 9:
                # A half-saved calibration projects every person to the same
                # place, and that place is inside whichever zone contains it.
                problems.append(SyncProblem(
                    kind="homography", subject=camera_id,
                    detail=f"homography has {len(raw) if hasattr(raw, '__len__') else '?'} "
                           "values, expected 9; importing it would put the "
                           "whole building in one room",
                    fatal=False))
            else:
                try:
                    homography = tuple(float(v) for v in raw)
                except (TypeError, ValueError):
                    problems.append(SyncProblem(
                        kind="homography", subject=camera_id,
                        detail="homography is not numeric", fatal=False))

        plan_id = (calibration.get("floor_plan_id")
                   or plan_by_camera.get(camera_id))
        camera = SyncedCamera(
            camera_id=camera_id,
            floor_plan_id=str(plan_id) if plan_id else None,
            homography=homography,
            marker=marker_by_camera.get(camera_id))
        synced_cameras.append(camera)

        if not camera.can_resolve_zones:
            # Imported anyway. Losing a camera silently is worse than importing
            # one that only contributes presence.
            problems.append(SyncProblem(
                kind="geometry", subject=camera_id,
                detail="no usable homography or no floor plan; it can see "
                       "people but cannot place them in a zone",
                fatal=False))

    return SyncResult(
        floor_plans=tuple(plans), zones=tuple(zones),
        cameras=tuple(synced_cameras), problems=tuple(problems),
        tagging=review(proposals))
