# ---------------------------------------------------------------------------
# VENDORED from VisionTrack @ 3592c72
#   backend/app/modules/analytics/dwell.py
# Copied 2026-09-10 for EVAC-120. Imports rewritten to vendor paths.
# Do NOT edit to fix an upstream bug -- fix it upstream and re-vendor.
# Local changes, if any, are listed in docs/EVAC120_PROVENANCE.md.
# ---------------------------------------------------------------------------
"""Per-person zone dwell — how long each named person spends in each zone.

This is the "indoor geofencing" computation: for a time window, attribute every
tracked person's presence to the zones they were in and sum the time. It is the
temporal companion to ``occupancy.py`` (which is a live headcount snapshot).

Kept free of DB/async so it can be unit-tested in isolation; the async DB wrapper
is ``service.get_zone_dwell``.

Model (identical zone↔camera binding to occupancy, so the two agree):
  * A **zone** is a polygon (fractional 0..1) on a floor plan.
  * A **camera** has a marker point on the floor plan; it "belongs to" a zone
    when its marker lies inside the zone polygon.
  * A **track** is one person on one camera for ``[started_at, ended_at]``. That
    interval IS the person's dwell in every zone the camera sits in.

"Named" person = a track carrying a face identity (``emp_id`` from the FaceTrack
feed). Anonymous tracks have no ``emp_id`` and are ignored here — dwell answers
"which *employee* was where, and for how long"; anonymous headcount is the
occupancy panel's job.

A person seen by two cameras that both sit in the same zone, or re-acquired under
a new tracker id, contributes multiple tracks to that (person, zone) pair — they
are summed as ``seconds`` with ``sessions`` counting the fragments. Overlapping
fragments (same instant, two cameras) can marginally over-count; gaps where the
tracker briefly loses the person under-count. This is the same honest
approximation occupancy makes, stated plainly rather than hidden.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from app.vendor.visiontrack.zones_math import point_in_polygon


def camera_zone_index(
    floor_plans: Iterable[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Map each camera id -> the list of zones its marker falls inside.

    ``floor_plans`` items: ``{"id", "name", "zones": [{"id","name","polygon":
    [{"x","y"}]}], "markers": [{"camera_id","x","y"}]}`` — same shape occupancy
    consumes. A camera can belong to several zones (overlapping polygons); each
    zone ref is ``{floor_plan_id, floor_plan_name, zone_id, zone_name}``.
    """
    index: dict[str, list[dict[str, Any]]] = {}
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
            ref = {
                "floor_plan_id": str(fp["id"]),
                "floor_plan_name": fp.get("name"),
                "zone_id": str(z.get("id")),
                "zone_name": z.get("name", "Zone"),
            }
            for cam, pt in markers.items():
                if point_in_polygon(pt, polygon):
                    index.setdefault(cam, []).append(ref)
    return index


def clip_interval(
    start: datetime, end: datetime, lo: datetime, hi: datetime
) -> tuple[datetime, datetime] | None:
    """Intersect ``[start, end]`` with the window ``[lo, hi]``.

    Returns the clipped ``(start, end)`` or ``None`` if they don't overlap (or the
    overlap is empty). ``end`` before ``start`` (bad data) yields ``None``.
    """
    s = max(start, lo)
    e = min(end, hi)
    if e <= s:
        return None
    return (s, e)


def accumulate_dwell(
    tracks: Iterable[dict[str, Any]],
    cam_zones: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Sum per-(person, zone) dwell over the given tracks.

    Each track dict: ``{"camera_id", "emp_id", "name", "start": datetime,
    "end": datetime, "present": bool}`` where ``start``/``end`` are already
    clipped to the query window and ``present`` marks a still-active track.

    Zone attribution, in order of precedence per track:
      * ``"zone_durations"`` — a list of ``{"ref", "seconds", "first", "last"}``
        from per-track-point time-weighting (``zone_durations_from_intervals``),
        where a track that *moved* between zones splits its time across them.
      * ``"zones"`` — a pre-resolved ref list (position/desk-level or marker); the
        whole clipped ``[start, end]`` duration is attributed to each.
      * ``cam_zones[camera_id]`` — the coarse camera-marker fallback.

    Tracks without an ``emp_id`` (anonymous) or in no zone are skipped. A track
    counts as one ``session`` per zone it was in (not per point).

    Returns one row per (emp_id, zone) with ``seconds`` (float), ``sessions``,
    ``first_seen``, ``last_seen``, ``present`` (any fragment still active), and
    the zone/floor-plan labels. Rows are sorted by ``seconds`` descending.
    """
    agg: dict[tuple[str, str], dict[str, Any]] = {}
    for t in tracks:
        emp_id = t.get("emp_id")
        if not emp_id:
            continue  # anonymous — not a named employee
        present = bool(t.get("present"))

        # Normalise this track into (ref, seconds, first, last) contributions.
        contributions: list[tuple[dict[str, Any], float, datetime, datetime]] = []
        zone_durations = t.get("zone_durations")
        if zone_durations is not None:
            for zd in zone_durations:
                secs = max(0.0, float(zd["seconds"]))
                contributions.append((zd["ref"], secs, zd["first"], zd["last"]))
        else:
            zones = t.get("zones")
            if zones is None:
                zones = (cam_zones or {}).get(str(t["camera_id"]))
            if not zones:
                continue  # in no zone
            start = t["start"]
            end = t["end"]
            secs = max(0.0, (end - start).total_seconds())
            for ref in zones:
                contributions.append((ref, secs, start, end))

        for ref, secs, first, last in contributions:
            key = (str(emp_id), ref["zone_id"])
            row = agg.get(key)
            if row is None:
                row = {
                    "emp_id": str(emp_id),
                    "name": t.get("name"),
                    "floor_plan_id": ref["floor_plan_id"],
                    "floor_plan_name": ref["floor_plan_name"],
                    "zone_id": ref["zone_id"],
                    "zone_name": ref["zone_name"],
                    "seconds": 0.0,
                    "sessions": 0,
                    "first_seen": first,
                    "last_seen": last,
                    "present": False,
                }
                agg[key] = row
            row["seconds"] += secs
            row["sessions"] += 1
            row["first_seen"] = min(row["first_seen"], first)
            row["last_seen"] = max(row["last_seen"], last)
            row["present"] = row["present"] or present
            if t.get("name") and not row.get("name"):
                row["name"] = t["name"]

    return sorted(agg.values(), key=lambda r: r["seconds"], reverse=True)


def track_zone_intervals(
    points: list[tuple[datetime, list[dict[str, Any]]]],
    seg_start: datetime,
    seg_end: datetime,
) -> list[dict[str, Any]]:
    """Turn one track's timestamped zone samples into time-ordered zone visits.

    ``points`` is a list of ``(ts, [zone_refs])`` sorted ascending by ``ts`` —
    each the zones a track's foot-point fell in at that moment (from
    ``track_points.world_x/world_y``). Using forward-fill, each sample's zone(s)
    hold until the next sample; the first sample extends back to ``seg_start`` and
    the last forward to ``seg_end`` (both the track's window-clipped bounds).
    Consecutive samples in the same zone merge into one contiguous interval, so a
    person who walks Desk A -> Desk B produces ``[DeskA:.., DeskB:..]`` rather
    than one lumped visit. Samples in no zone contribute nothing (aisle/between
    desks). Returns interval dicts ``{zone_id, zone_name, floor_plan_id,
    floor_plan_name, start, end}`` ordered by ``start``.
    """
    n = len(points)
    if n == 0:
        return []

    # Raw per-sample sub-intervals, grouped by zone.
    by_zone: dict[str, list[tuple[dict[str, Any], datetime, datetime]]] = {}
    for i, (ts, refs) in enumerate(points):
        cur = seg_start if i == 0 else ts
        nxt = seg_end if i == n - 1 else points[i + 1][0]
        lo = max(cur, seg_start)
        hi = min(nxt, seg_end)
        if hi <= lo:
            continue
        for ref in refs:
            by_zone.setdefault(ref["zone_id"], []).append((ref, lo, hi))

    intervals: list[dict[str, Any]] = []
    for items in by_zone.values():
        items.sort(key=lambda x: x[1])
        cref, clo, chi = items[0]
        for ref, lo, hi in items[1:]:
            if lo <= chi:  # contiguous (forward-fill) or overlapping -> merge
                chi = max(chi, hi)
            else:
                intervals.append(_interval(cref, clo, chi))
                cref, clo, chi = ref, lo, hi
        intervals.append(_interval(cref, clo, chi))

    return sorted(intervals, key=lambda s: s["start"])


def _interval(ref: dict[str, Any], start: datetime, end: datetime) -> dict[str, Any]:
    return {
        "zone_id": ref["zone_id"],
        "zone_name": ref["zone_name"],
        "floor_plan_id": ref["floor_plan_id"],
        "floor_plan_name": ref.get("floor_plan_name"),
        "start": start,
        "end": end,
    }


def union_seconds(intervals: Iterable[tuple[datetime, datetime]]) -> float:
    """Total seconds covered by a set of ``(start, end)`` intervals, overlaps
    counted once. Used for aisle/idle time — a track's in-zone coverage (union of
    its zone intervals) vs its span reveals time tracked but in no zone."""
    ivs = sorted(((s, e) for s, e in intervals if e > s), key=lambda x: x[0])
    if not ivs:
        return 0.0
    total = 0.0
    cur_s, cur_e = ivs[0]
    for s, e in ivs[1:]:
        if s <= cur_e:
            if e > cur_e:
                cur_e = e
        else:
            total += (cur_e - cur_s).total_seconds()
            cur_s, cur_e = s, e
    total += (cur_e - cur_s).total_seconds()
    return total


def zone_durations_from_intervals(
    intervals: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse a track's zone intervals into per-zone totals for dwell.

    Returns ``[{"ref", "seconds", "first", "last"}]`` — one entry per distinct
    zone the track visited, ``seconds`` summed across its intervals. Feeds
    ``accumulate_dwell`` via a track's ``"zone_durations"``.
    """
    by: dict[str, dict[str, Any]] = {}
    for s in intervals:
        secs = max(0.0, (s["end"] - s["start"]).total_seconds())
        e = by.get(s["zone_id"])
        if e is None:
            by[s["zone_id"]] = {
                "ref": {
                    "floor_plan_id": s["floor_plan_id"],
                    "floor_plan_name": s.get("floor_plan_name"),
                    "zone_id": s["zone_id"],
                    "zone_name": s["zone_name"],
                },
                "seconds": secs,
                "first": s["start"],
                "last": s["end"],
            }
        else:
            e["seconds"] += secs
            e["first"] = min(e["first"], s["start"])
            e["last"] = max(e["last"], s["end"])
    return list(by.values())


def build_person_timeline(
    segments: Iterable[dict[str, Any]],
    merge_gap_seconds: float = 60.0,
) -> list[dict[str, Any]]:
    """Collapse one person's per-zone presence segments into a chronological
    "where were they" visit list — the timeline behind "where was X today".

    Each input segment: ``{"zone_id", "zone_name", "floor_plan_id",
    "floor_plan_name", "start": datetime, "end": datetime, "present": bool}`` —
    one per (track, zone), already clipped to the query window.

    Two segments in the **same zone** are merged into one visit when the later
    one starts within ``merge_gap_seconds`` of the running visit's end. This
    bridges brief tracker drops / camera hand-offs so a continuous stay reads as
    one visit rather than a dozen fragments, while a genuine departure and return
    to the same zone (a gap larger than the tolerance) stays two visits.

    Merging is per-zone, so moving Sales -> Production -> Sales yields three
    visits (Sales appears twice). Returns visits sorted by ``start``; each carries
    ``seconds`` (end-start), ``sessions`` (fragments merged), and ``present``.
    """
    by_zone: dict[str, list[dict[str, Any]]] = {}
    for s in segments:
        by_zone.setdefault(s["zone_id"], []).append(s)

    visits: list[dict[str, Any]] = []
    gap = timedelta(seconds=merge_gap_seconds)
    for zone_segs in by_zone.values():
        zone_segs.sort(key=lambda s: s["start"])
        cur: dict[str, Any] | None = None
        for s in zone_segs:
            if cur is not None and s["start"] <= cur["end"] + gap:
                cur["end"] = max(cur["end"], s["end"])
                cur["sessions"] += 1
                cur["present"] = cur["present"] or bool(s.get("present"))
            else:
                if cur is not None:
                    visits.append(cur)
                cur = {
                    "zone_id": s["zone_id"],
                    "zone_name": s["zone_name"],
                    "floor_plan_id": s["floor_plan_id"],
                    "floor_plan_name": s.get("floor_plan_name"),
                    "start": s["start"],
                    "end": s["end"],
                    "sessions": 1,
                    "present": bool(s.get("present")),
                }
        if cur is not None:
            visits.append(cur)

    for v in visits:
        v["seconds"] = (v["end"] - v["start"]).total_seconds()
    return sorted(visits, key=lambda v: v["start"])
