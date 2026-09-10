# ---------------------------------------------------------------------------
# VENDORED from VisionTrack @ 3592c72
#   backend/app/modules/analytics/occupancy.py
# Copied 2026-09-10 for EVAC-120. Imports rewritten to vendor paths.
# Do NOT edit to fix an upstream bug -- fix it upstream and re-vendor.
# Local changes, if any, are listed in docs/EVAC120_PROVENANCE.md.
# ---------------------------------------------------------------------------
"""Zone occupancy — count people currently in each zone, split known / unknown.

This is the pure computation, kept free of DB/async so it can be unit-tested in
isolation. The async DB wrapper lives in ``service.get_zone_occupancy`` and feeds
these functions plain dicts.

Model (mirrors the alert evaluator, ``workers/alerts_tasks.py``):
  * A **zone** is a polygon (fractional 0..1 coords) stored on a floor plan.
  * A **camera** has a marker point on the floor plan.
  * A camera belongs to a zone when its marker lies inside the zone polygon.
  * Zone occupancy = the active tracks on all cameras in that zone.

"Known" = the track has a face identity (an ``emp_id`` from the FaceTrack feed,
correlated by ``persons.face_identity_consumer``). "Unknown" = a tracked person
with no identity yet.
"""
from __future__ import annotations

from typing import Any, Iterable

from app.vendor.visiontrack.zones_math import point_in_polygon


def bucket_tracks_by_camera(active_tracks: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Group active tracks by camera into {total, known, unknown, people}.

    Each track dict: ``{"track_id", "camera_id", "emp_id" | None, "name" | None}``.
    A track is *known* iff it has a truthy ``emp_id``.
    """
    per: dict[str, dict[str, Any]] = {}
    for t in active_tracks:
        cam = str(t["camera_id"])
        b = per.setdefault(cam, {"total": 0, "known": 0, "unknown": 0, "people": []})
        b["total"] += 1
        if t.get("emp_id"):
            b["known"] += 1
            b["people"].append({"emp_id": str(t["emp_id"]), "name": t.get("name")})
        else:
            b["unknown"] += 1
    return per


def zones_occupancy(
    floor_plans: Iterable[dict[str, Any]],
    per_camera: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """For every zone on every floor plan, sum the occupancy of the cameras whose
    markers fall inside the zone polygon. Returns one row per zone.

    ``floor_plans`` items: ``{"id", "name", "zones": [{"id","name","polygon":[{"x","y"}]}],
    "markers": [{"camera_id","x","y"}]}``.
    """
    out: list[dict[str, Any]] = []
    for fp in floor_plans:
        markers: dict[str, tuple[float, float]] = {}
        for m in fp.get("markers") or []:
            cam_id = m.get("camera_id")
            if cam_id is not None:
                markers[str(cam_id)] = (float(m.get("x", 0.0)), float(m.get("y", 0.0)))

        for z in fp.get("zones") or []:
            polygon = [(float(p["x"]), float(p["y"])) for p in (z.get("polygon") or [])]
            if len(polygon) < 3:
                continue  # not a valid area
            cams_in_zone = [cam for cam, pt in markers.items() if point_in_polygon(pt, polygon)]

            total = known = unknown = 0
            people: list[dict[str, Any]] = []
            for cam in cams_in_zone:
                b = per_camera.get(cam)
                if not b:
                    continue
                total += b["total"]
                known += b["known"]
                unknown += b["unknown"]
                people += b["people"]

            out.append({
                "floor_plan_id": str(fp["id"]),
                "floor_plan_name": fp.get("name"),
                "zone_id": str(z.get("id")),
                "zone_name": z.get("name", "Zone"),
                "camera_ids": cams_in_zone,
                "total": total,
                "known": known,
                "unknown": unknown,
                "known_people": people,
            })
    return out


def zones_occupancy_from_tracks(
    floor_plans: Iterable[dict[str, Any]],
    tracks: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Per-zone headcount (known/unknown) from tracks that already carry their
    resolved zone refs — the position-aware (desk-level) counterpart to
    ``zones_occupancy``.

    Each track: ``{"camera_id", "emp_id" | None, "name" | None,
    "zones": [{floor_plan_id, floor_plan_name, zone_id, zone_name}]}``. Zone
    membership comes from ``zone_resolve.resolve_track_zone_refs`` (foot-point in
    polygon for calibrated cameras, camera-marker otherwise), so several desks in
    one camera view are counted separately.

    Every zone on every floor plan is returned (headcount 0 when empty) so the UI
    shows the full set of zones, matching ``zones_occupancy``.
    """
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for fp in floor_plans:
        for z in fp.get("zones") or []:
            polygon = z.get("polygon") or []
            if len(polygon) < 3:
                continue
            key = (str(fp["id"]), str(z.get("id")))
            rows[key] = {
                "floor_plan_id": str(fp["id"]),
                "floor_plan_name": fp.get("name"),
                "zone_id": str(z.get("id")),
                "zone_name": z.get("name", "Zone"),
                "_cameras": set(),
                "total": 0, "known": 0, "unknown": 0, "known_people": [],
            }

    for t in tracks:
        for ref in t.get("zones") or []:
            r = rows.get((ref["floor_plan_id"], ref["zone_id"]))
            if r is None:
                continue
            r["total"] += 1
            if t.get("emp_id"):
                r["known"] += 1
                r["known_people"].append({"emp_id": str(t["emp_id"]), "name": t.get("name")})
            else:
                r["unknown"] += 1
            if t.get("camera_id") is not None:
                r["_cameras"].add(str(t["camera_id"]))

    out: list[dict[str, Any]] = []
    for r in rows.values():
        r["camera_ids"] = sorted(r.pop("_cameras"))
        out.append(r)
    return out
