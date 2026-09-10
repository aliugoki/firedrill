# ---------------------------------------------------------------------------
# VENDORED from VisionTrack @ 3592c72
#   backend/app/modules/floor_plans/zones_math.py
# Copied 2026-09-10 for EVAC-120. Pure stdlib.
# Do NOT edit to fix an upstream bug -- fix it upstream and re-vendor.
# Local changes, if any, are listed in docs/EVAC120_PROVENANCE.md.
# ---------------------------------------------------------------------------
"""Geometric primitives for zone-based alert evaluation.

The point-in-polygon test is used by the Celery alert-evaluation task
(Step 5 Batch C) to determine how many tracked people are currently
inside each zone, every evaluation tick.

We use the ray-casting algorithm (a.k.a. Jordan curve theorem method):
  - Cast a horizontal ray from the test point to the right
  - Count how many polygon edges it crosses
  - Odd count → inside; even count → outside

This works for any simple polygon, convex or concave, which matters
because operators draw L-shapes for aisles, U-shapes for floor wings,
etc. It does NOT work correctly for self-intersecting polygons — but
the editor prevents those (no edge-crossing during draw).

Coordinates are expected as fractional (0..1, relative to the floor
plan's width/height). The algorithm itself is unit-agnostic; we use
fractional throughout VisionTrack so this is automatic.

Performance:
  Per polygon: O(N) where N is the number of vertices.
  For a typical install (10 zones × 8 vertices each = 80 ops per person
  per evaluation tick), this is negligible compared to the DB and Redis
  round trips. No need for spatial indexes at this scale.
"""

from __future__ import annotations

from typing import Sequence


# Type alias: a polygon is a list of (x, y) tuples. Both fractional 0..1.
Point = tuple[float, float]
Polygon = Sequence[Point]


def point_in_polygon(point: Point, polygon: Polygon) -> bool:
    """Return True if `point` lies strictly inside `polygon`.

    Edge behavior: points exactly on a polygon edge are reported as
    'inside' in some cases and 'outside' in others, depending on the
    ray direction. For alert evaluation this is fine — a person whose
    centroid is on a zone edge is genuinely ambiguous, and the next
    frame's centroid will resolve them one way or the other.

    Raises ValueError if the polygon has fewer than 3 vertices.
    """
    n = len(polygon)
    if n < 3:
        raise ValueError(f"polygon must have at least 3 vertices, got {n}")

    px, py = point
    inside = False
    # Loop over each edge: (polygon[j], polygon[i]) where j precedes i.
    # We initialize j = n - 1 so the first iteration handles the wraparound
    # edge from the last vertex back to the first.
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        # Does the horizontal ray at y=py cross this edge?
        crosses_y_band = (yi > py) != (yj > py)
        if crosses_y_band:
            # Find the x where the edge crosses the horizontal ray.
            # If that x is to the right of the point, this edge counts.
            x_intersect = xi + (py - yi) * (xj - xi) / (yj - yi)
            if px < x_intersect:
                inside = not inside
        j = i
    return inside


def polygon_centroid(polygon: Polygon) -> Point:
    """Compute the centroid of a polygon.

    Used for placing a zone's name label at a sensible default position
    on the floor plan. Uses the standard signed-area weighted formula,
    which gives the true geometric centroid (not just the vertex average,
    which is biased for non-convex shapes).
    """
    n = len(polygon)
    if n < 3:
        raise ValueError(f"polygon must have at least 3 vertices, got {n}")

    # Signed area × 6, plus weighted x and y sums × 6.
    area6 = 0.0
    cx6 = 0.0
    cy6 = 0.0
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        cross = xj * yi - xi * yj
        area6 += cross
        cx6 += (xj + xi) * cross
        cy6 += (yj + yi) * cross
        j = i

    if abs(area6) < 1e-12:
        # Degenerate (collinear vertices) — fall back to vertex average
        return (
            sum(p[0] for p in polygon) / n,
            sum(p[1] for p in polygon) / n,
        )

    area = area6 / 2.0
    return (cx6 / (6.0 * area), cy6 / (6.0 * area))


def count_points_in_polygon(points: Sequence[Point], polygon: Polygon) -> int:
    """Count how many of `points` lie inside `polygon`.

    Convenience for the evaluator: pass it the centroids of all currently
    tracked people and one zone's polygon, get back the occupancy count
    for that zone in O(N × M).
    """
    if not points:
        return 0
    return sum(1 for p in points if point_in_polygon(p, polygon))
