# ---------------------------------------------------------------------------
# VENDORED from VisionTrack @ 3592c72
#   backend/app/modules/analytics/zone_resolve.py
# Copied 2026-09-10 for EVAC-120. Imports rewritten to vendor paths.
# Do NOT edit to fix an upstream bug -- fix it upstream and re-vendor.
# Local changes, if any, are listed in docs/EVAC120_PROVENANCE.md.
# ---------------------------------------------------------------------------
"""Per-person zone attribution — desk-level precision via homography.

The zone analytics (occupancy / dwell / timeline) need to decide *which zone a
tracked person is in*. There are two ways, and this module picks the best one
available per camera:

  1. **Position mode (precise)** — when a camera is BEV-calibrated (its
     ``calibration.homography`` is set), we project each person's foot-point
     (bottom-centre of the bbox) through that homography to floor-plan fractional
     coordinates and test it against each zone polygon. This distinguishes
     several desks inside a single camera's view — a person at desk 3 lands in
     desk 3's polygon, not desks 1 or 2.

  2. **Marker mode (coarse, fallback)** — when a camera has no homography, we
     fall back to the camera-marker model: the person is "in" whatever zone(s)
     the camera's marker point sits inside. This is the original behaviour, so
     deployments that haven't calibrated cameras are completely unaffected.

The projection mirrors ``tracks.consumer._project_foot`` and the frontend's
``bev/homography.ts`` exactly, so ingest, the live BEV view, and these analytics
all agree on where a person is. Pure/DB-free so it can be unit-tested.
"""
from __future__ import annotations

from typing import Any, Iterable

from app.vendor.visiontrack.dwell import camera_zone_index
from app.vendor.visiontrack.zones_math import point_in_polygon


def _bbox_xyxy(bbox: Any) -> tuple[float, float, float, float] | None:
    """Normalise a bbox (dict ``{x1,y1,x2,y2}`` or list ``[x1,y1,x2,y2]``)."""
    if isinstance(bbox, dict):
        try:
            return (float(bbox["x1"]), float(bbox["y1"]),
                    float(bbox["x2"]), float(bbox["y2"]))
        except (KeyError, TypeError, ValueError):
            return None
    if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        try:
            return (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        except (TypeError, ValueError):
            return None
    return None


def project_foot(homography: Any, bbox: Any) -> tuple[float, float] | None:
    """Project a bbox foot-point through a row-major 3x3 homography to floor-plan
    fractional coords. ``None`` if the homography is missing/degenerate, the bbox
    is unusable, or the point maps to infinity.

    Mirrors ``tracks.consumer._project_foot`` so attribution agrees with ingest.
    """
    if not isinstance(homography, (list, tuple)) or len(homography) != 9:
        return None
    xyxy = _bbox_xyxy(bbox)
    if xyxy is None:
        return None
    x1, _y1, x2, y2 = xyxy
    fx = (x1 + x2) / 2.0
    fy = y2  # bottom-centre = where the feet touch the floor
    h = [float(v) for v in homography]
    w = h[6] * fx + h[7] * fy + h[8]
    if abs(w) < 1e-9:
        return None
    return ((h[0] * fx + h[1] * fy + h[2]) / w,
            (h[3] * fx + h[4] * fy + h[5]) / w)


def camera_plan_zones(
    floor_plans: Iterable[dict[str, Any]],
) -> dict[str, list[tuple[str, Any, list[tuple[str, Any, list[tuple[float, float]]]]]]]:
    """Map camera id -> the floor plan(s) it is placed on, each with that plan's
    zone polygons (for position-mode point-in-polygon tests).

    Returns ``{camera_id: [(floor_plan_id, floor_plan_name,
    [(zone_id, zone_name, polygon)])]}``. A camera is associated with a plan when
    it has a marker there — that plan's coordinate space is the one its
    homography projects into.
    """
    out: dict[str, list] = {}
    for fp in floor_plans:
        zones: list[tuple[str, Any, list[tuple[float, float]]]] = []
        for z in fp.get("zones") or []:
            poly = [(float(p["x"]), float(p["y"])) for p in (z.get("polygon") or [])]
            if len(poly) >= 3:
                zones.append((str(z.get("id")), z.get("name", "Zone"), poly))
        if not zones:
            continue
        entry = (str(fp["id"]), fp.get("name"), zones)
        for m in fp.get("markers") or []:
            cam = m.get("camera_id")
            if cam is not None:
                out.setdefault(str(cam), []).append(entry)
    return out


def resolve_track_zone_refs(
    camera_id: Any,
    last_bbox: Any,
    homographies: dict[str, Any],
    marker_zones: dict[str, list[dict[str, Any]]],
    plan_zones: dict[str, list],
) -> list[dict[str, Any]]:
    """Return the zone refs a track belongs to, using position mode when the
    camera is calibrated and marker mode otherwise.

    A ref is ``{floor_plan_id, floor_plan_name, zone_id, zone_name}``.

    Position mode returns exactly the zones whose polygon contains the projected
    foot-point — possibly empty (the person is between desks / in an aisle), which
    is correct: they aren't at any desk. We only fall back to marker mode when
    there is no usable homography/projection, so a calibration error can't
    silently revert precise cameras to coarse counting.
    """
    cam = str(camera_id)
    H = homographies.get(cam)
    if H and last_bbox:
        pt = project_foot(H, last_bbox)
        if pt is not None and 0.0 <= pt[0] <= 1.0 and 0.0 <= pt[1] <= 1.0:
            refs: list[dict[str, Any]] = []
            for fp_id, fp_name, zones in plan_zones.get(cam, []):
                for z_id, z_name, poly in zones:
                    if point_in_polygon(pt, poly):
                        refs.append({
                            "floor_plan_id": fp_id, "floor_plan_name": fp_name,
                            "zone_id": z_id, "zone_name": z_name,
                        })
            return refs
        # No usable projection (missing/degenerate H, off-plan point) -> coarse.
    return marker_zones.get(cam, [])


def point_zone_refs(
    wx: float, wy: float, plan_zones_for_camera: list,
) -> list[dict[str, Any]]:
    """Zones containing a floor-plan point ``(wx, wy)`` for one camera.

    ``plan_zones_for_camera`` is ``camera_plan_zones(...)[camera_id]`` — the
    plan(s) the camera is on with their zone polygons. Used for per-track-point
    time-weighting, where ``track_points.world_x/world_y`` already hold the
    foot-point projected to floor fractions at ingest (so no homography needed
    here). Returns ``[]`` when the point is in no zone (aisle / between desks).
    """
    refs: list[dict[str, Any]] = []
    for fp_id, fp_name, zones in plan_zones_for_camera or []:
        for z_id, z_name, poly in zones:
            if point_in_polygon((wx, wy), poly):
                refs.append({
                    "floor_plan_id": fp_id, "floor_plan_name": fp_name,
                    "zone_id": z_id, "zone_name": z_name,
                })
    return refs


def homographies_by_camera(cameras: Iterable[tuple[Any, Any]]) -> dict[str, list[float]]:
    """Extract usable homographies from ``(camera_id, calibration)`` rows.

    ``calibration`` is the camera's JSONB; a homography is a list of 9 floats
    under the ``homography`` key. Cameras without one are simply absent (marker
    mode applies to them).
    """
    out: dict[str, list[float]] = {}
    for camera_id, calibration in cameras:
        if isinstance(calibration, dict):
            h = calibration.get("homography")
            if isinstance(h, list) and len(h) == 9:
                try:
                    out[str(camera_id)] = [float(v) for v in h]
                except (TypeError, ValueError):
                    pass
    return out


# Re-exported so callers have one import site for zone attribution.
__all__ = [
    "project_foot",
    "camera_plan_zones",
    "camera_zone_index",
    "resolve_track_zone_refs",
    "point_zone_refs",
    "homographies_by_camera",
]
