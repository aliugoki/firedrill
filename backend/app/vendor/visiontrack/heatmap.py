# ---------------------------------------------------------------------------
# VENDORED from VisionTrack @ 3592c72
#   backend/app/modules/analytics/heatmap.py
# Copied 2026-09-10 for EVAC-120. Pure stdlib.
# Do NOT edit to fix an upstream bug -- fix it upstream and re-vendor.
# Local changes, if any, are listed in docs/EVAC120_PROVENANCE.md.
# ---------------------------------------------------------------------------
"""Hour-of-day occupancy heatmap derived from zone presence.

For each zone, buckets presence into the 24 hours of the day (in the tenant's
timezone, aggregated across every day in the range) so operators can see *when*
each zone is busy — e.g. "Sales peaks 09:00–11:00". Each cell carries both
``seconds`` (person-time, the heat) and ``people`` (distinct employees that hour).

Pure (no DB/async) so it can be unit-tested; the async wrapper is
``service.get_occupancy_heatmap``. Presence intervals are split at local hour
boundaries — computed by finding the next local-hour boundary and converting back
to UTC — so :30/:45 offsets and DST are handled correctly.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any, Iterable


def hour_of_day_heatmap(
    presence: Iterable[dict[str, Any]],
    tz: tzinfo,
) -> list[dict[str, Any]]:
    """Bucket presence segments into per-zone, per-hour occupancy.

    ``presence`` items: ``{"zone_id", "zone_name", "floor_plan_id",
    "floor_plan_name", "emp_id", "start": datetime, "end": datetime}`` (UTC
    datetimes). Returns one row per zone (busiest first) with a 24-element
    ``cells`` list ``[{"hour", "seconds", "people"}]`` and ``total_seconds``.
    """
    zones: dict[str, dict[str, Any]] = {}
    for p in presence:
        start: datetime = p["start"]
        end: datetime = p["end"]
        if end <= start:
            continue
        zid = p["zone_id"]
        z = zones.get(zid)
        if z is None:
            z = zones[zid] = {
                "zone_id": zid,
                "zone_name": p.get("zone_name", "Zone"),
                "floor_plan_id": p.get("floor_plan_id"),
                "floor_plan_name": p.get("floor_plan_name"),
                "seconds": [0.0] * 24,
                "people": [set() for _ in range(24)],
            }
        emp_id = p.get("emp_id")

        cur = start
        # Walk the interval hour local-bucket by local-bucket.
        while cur < end:
            local = cur.astimezone(tz)
            next_local = local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            next_utc = next_local.astimezone(timezone.utc)
            seg_end = min(end, next_utc)
            if seg_end <= cur:  # safety against pathological tz math
                break
            hour = local.hour
            z["seconds"][hour] += (seg_end - cur).total_seconds()
            if emp_id:
                z["people"][hour].add(emp_id)
            cur = seg_end

    out: list[dict[str, Any]] = []
    for z in zones.values():
        out.append({
            "zone_id": z["zone_id"],
            "zone_name": z["zone_name"],
            "floor_plan_id": z["floor_plan_id"],
            "floor_plan_name": z["floor_plan_name"],
            "cells": [
                {"hour": h, "seconds": z["seconds"][h], "people": len(z["people"][h])}
                for h in range(24)
            ],
            "total_seconds": sum(z["seconds"]),
        })
    return sorted(out, key=lambda x: x["total_seconds"], reverse=True)
