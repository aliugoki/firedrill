"""Reading VisionTrack's Postgres and producing an EVAC-120 site.

The queries below are against tables this was written against directly:
`floor_plans` with JSONB `zones` and `markers`, and `cameras` with JSONB
`calibration`. They are read-only, and that is a rule rather than a coincidence.

**EVAC-120 never writes to VisionTrack.** A bug here cannot corrupt a running
CCTV deployment, and the connection is opened with a read-only transaction so
the guarantee survives someone adding an UPDATE in a hurry.

**A sync failure is not a drill failure.** A site that has synced once can run
drills from its stored geometry with VisionTrack switched off entirely. That is
the zero-Internet requirement applied to configuration as well as to the
accountability path: `SyncOutcome.stale` says how old the geometry is, so an
operator sees a working drill built on last week's floor plan rather than
assuming it is current.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.sync.geometry import SyncResult, sync


class GeometrySource(Protocol):
    """Where floor plans and cameras come from. Injected so the transformation
    can be tested without a database, and so a JSON export is a first-class
    source rather than a workaround."""

    def floor_plans(self, site_id: str) -> list: ...
    def cameras(self, site_id: str) -> list: ...


class SourceUnavailable(Exception):
    """VisionTrack could not be read. Not fatal to a site that has synced once."""


@dataclass
class SyncOutcome:
    result: SyncResult | None
    synced_at_ms: int | None
    source: str
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.result is not None and self.error is None

    def stale_ms(self, now_ms: int) -> int | None:
        if self.synced_at_ms is None:
            return None
        return max(0, now_ms - self.synced_at_ms)

    def describe(self, now_ms: int) -> list[str]:
        if not self.succeeded:
            return [f"Geometry sync failed: {self.error}",
                    "  Stored geometry, if any, is still usable."]
        stale = self.stale_ms(now_ms) or 0
        lines = [f"Geometry synced from {self.source}"]
        if stale > 3_600_000:
            lines.append(
                f"  WARNING: this geometry is {stale / 3_600_000:.0f} hours old. "
                "A drill will run on it, but a zone moved since then has moved "
                "only in VisionTrack.")
        lines += self.result.describe()
        return lines


@dataclass
class GeometryStore:
    """The last successful sync, held so a drill can run without VisionTrack.

    In memory for now. Persisting it is the same Phase-3 gap as the projections,
    and the consequence is the same: a restart re-syncs rather than reads back.
    """

    outcome: SyncOutcome | None = None

    def put(self, outcome: SyncOutcome) -> SyncOutcome:
        # A failed sync never replaces a good one. Losing working geometry
        # because VisionTrack was briefly down would turn an inconvenience into
        # a site that cannot run a drill.
        if outcome.succeeded:
            self.outcome = outcome
        return outcome

    @property
    def geometry(self) -> SyncResult | None:
        return self.outcome.result if self.outcome else None

    @property
    def has_geometry(self) -> bool:
        return self.geometry is not None

    def ready_for_a_drill(self) -> bool:
        return self.has_geometry and self.geometry.ready_for_a_drill

    def blocking(self) -> list[str]:
        if not self.has_geometry:
            return ["no geometry has been synced from VisionTrack"]
        return self.geometry.blocking()


def run_sync(
    source: GeometrySource, *, site_id: str, now_ms: int,
    tagged_zones: dict | None = None, store: GeometryStore | None = None,
) -> SyncOutcome:
    """Pull geometry once. Never raises: a failure is reported, not thrown."""
    try:
        plans = source.floor_plans(site_id)
        cameras = source.cameras(site_id)
    except SourceUnavailable as exc:
        outcome = SyncOutcome(result=None, synced_at_ms=None,
                              source=type(source).__name__, error=str(exc))
        return store.put(outcome) if store else outcome
    except Exception as exc:
        outcome = SyncOutcome(result=None, synced_at_ms=None,
                              source=type(source).__name__,
                              error=f"{type(exc).__name__}: {exc}")
        return store.put(outcome) if store else outcome

    result = sync(floor_plans=plans, cameras=cameras, site_id=site_id,
                  tagged_zones=tagged_zones)
    outcome = SyncOutcome(result=result, synced_at_ms=now_ms,
                          source=type(source).__name__)
    return store.put(outcome) if store else outcome


FLOOR_PLAN_QUERY = """
    SELECT id, name, width_px, height_px, zones, markers
    FROM floor_plans
    WHERE site_id = %(site_id)s
    ORDER BY name
"""

CAMERA_QUERY = """
    SELECT id, calibration
    FROM cameras
    WHERE site_id = %(site_id)s
    ORDER BY name
"""


class VisionTrackDatabase:
    """Reads VisionTrack's Postgres directly. Read-only, enforced.

    `psycopg2` is imported lazily so an edge node syncing from a JSON export
    does not need a Postgres driver installed to start.
    """

    def __init__(self, dsn: str) -> None:
        if not dsn:
            raise ValueError(
                "a DSN is required; a geometry source with no address would "
                "fail at the first drill rather than at configuration time")
        self.dsn = dsn

    def _query(self, sql: str, site_id: str) -> list:
        try:
            import psycopg2
            import psycopg2.extras
        except ImportError as exc:  # pragma: no cover - environment
            raise SourceUnavailable(
                "psycopg2 is not installed; use a JSON export instead") from exc

        try:
            with psycopg2.connect(self.dsn, connect_timeout=5) as connection:
                # The read-only guarantee, enforced by the server rather than by
                # everyone remembering. A bug here cannot corrupt a running CCTV
                # deployment.
                connection.set_session(readonly=True, autocommit=True)
                with connection.cursor(
                        cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                    cursor.execute(sql, {"site_id": site_id})
                    return [dict(row) for row in cursor.fetchall()]
        except SourceUnavailable:
            raise
        except Exception as exc:
            raise SourceUnavailable(f"{type(exc).__name__}: {exc}") from exc

    def floor_plans(self, site_id: str) -> list:
        return self._query(FLOOR_PLAN_QUERY, site_id)

    def cameras(self, site_id: str) -> list:
        return self._query(CAMERA_QUERY, site_id)


@dataclass
class JsonExport:
    """Geometry from a file. Not a fallback: the offline path.

    A site with no route to VisionTrack should be able to run a drill from an
    export, and treating that as a degraded mode rather than a supported one is
    how a system ends up unusable in the conditions it was built for.
    """

    path: str

    def _load(self) -> dict:
        import json

        try:
            with open(self.path) as handle:
                return json.load(handle)
        except FileNotFoundError as exc:
            raise SourceUnavailable(f"no geometry export at {self.path}") from exc
        except json.JSONDecodeError as exc:
            raise SourceUnavailable(
                f"the geometry export at {self.path} is not valid JSON: {exc}"
            ) from exc

    def floor_plans(self, site_id: str) -> list:
        return self._load().get("floor_plans", [])

    def cameras(self, site_id: str) -> list:
        return self._load().get("cameras", [])
