"""Assembling the edge node from its environment, and refusing to lie about it.

`build_edge` reads configuration and wires whatever is actually available. What
it does **not** do is substitute a plausible default for a missing dependency: a
node with no roster source, or no event stream, is a node that will produce a
board reading "0 of 0 accounted", and that screen looks exactly like a building
that has been safely evacuated.

So each missing piece is recorded in `EdgeNode.gaps`, the health endpoint
reports them, and a drill cannot be created without a roster. The node still
starts — an operator needs a screen that explains what is wrong far more than
they need a process that exited.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from app.ingest.consumer import EventConsumer, RedisStreamClient
from app.ingest.ingestor import Ingestor
from app.ingest.replication import Outbox, Replicator
from app.service.supervisor import Supervisor, build_supervisor
from app.sync.runner import GeometryStore, JsonExport, VisionTrackDatabase, run_sync


@dataclass
class EdgeNode:
    """Everything the edge process holds, and everything it is missing."""

    site_id: str
    tenant_id: str
    ingestor: Ingestor
    supervisor: Supervisor
    consumer: EventConsumer | None = None
    replicator: Replicator | None = None
    geometry: GeometryStore = field(default_factory=GeometryStore)
    events_store: object | None = None
    drill_store: object | None = None
    registry: object | None = None
    recovered_drills: tuple = ()
    gaps: tuple = ()

    @property
    def is_complete(self) -> bool:
        return not self.gaps

    def health(self, now_ms: int) -> dict:
        state = self.ingestor.state
        report = {
            "site_id": self.site_id,
            "degraded": state.health.is_degraded or bool(self.gaps),
            "blind": state.health.is_blind,
            "open_outages": len(state.health.open_now()),
            "events_accepted": state.accepted,
            "duplicates_dropped": state.duplicates_dropped,
            "outstanding_gaps": len(state.tracker.outstanding_gaps()),
            "replication_backlog": (self.replicator.backlog
                                    if self.replicator else 0),
            "geometry_ready": self.geometry.ready_for_a_drill(),
            "durable": self.events_store is not None,
            "recovered_drills": list(self.recovered_drills),
            "configuration_gaps": list(self.gaps),
            "background": self.supervisor.health(now_ms),
        }
        if self.events_store is not None:
            report["store"] = {
                "written": self.events_store.stats.written,
                "duplicates": self.events_store.stats.duplicates,
                "buffered": self.events_store.pending,
                "dropped": self.events_store.stats.dropped,
                "degraded": self.events_store.is_degraded,
            }
        if self.consumer is not None:
            report["stream"] = {
                "messages_read": self.consumer.stats.messages_read,
                "malformed": self.consumer.stats.malformed,
                "unparseable_fraction": round(
                    self.consumer.stats.unparseable_fraction, 4),
                "claimed": self.consumer.stats.claimed,
                "outages": self.consumer.stats.outages,
            }
        return report


def build_edge(env: dict | None = None, *, now_ms: int = 0) -> EdgeNode:
    """Wire an edge node from the environment. Never raises on a missing piece.

    A process that exits because Redis is not configured gives an operator
    nothing to look at. A process that starts and says "there is no event
    stream" gives them the answer.
    """
    env = env if env is not None else dict(os.environ)
    gaps: list[str] = []

    site_id = env.get("EVAC_SITE_ID", "")
    tenant_id = env.get("EVAC_TENANT_ID", "")
    if not site_id:
        gaps.append("EVAC_SITE_ID is not set; this node does not know which "
                    "building it covers")
    if not tenant_id:
        gaps.append("EVAC_TENANT_ID is not set")

    ingestor = Ingestor()

    # --- the event stream -----------------------------------------------------
    consumer = None
    redis_url = env.get("EVAC_REDIS_URL", "")
    if redis_url and tenant_id:
        prefix = env.get("EVAC_EVENT_STREAM_PREFIX", "vt:evac:events")
        consumer = EventConsumer(
            ingestor=ingestor,
            client=RedisStreamClient(redis_url),
            stream=f"{prefix}:{tenant_id}",
            consumer_name=env.get("EVAC_CONSUMER_NAME", f"edge-{site_id or '1'}"))
        consumer.ensure_group()
    else:
        gaps.append("no event stream is configured, so no camera observation "
                    "can reach this node")

    # --- replication to central ----------------------------------------------
    replicator = None
    central_url = env.get("EVAC_CENTRAL_URL", "")
    if central_url:
        from app.ingest.replication import InMemoryTransport

        outbox_dir = env.get("EVAC_OUTBOX_DIR", "./outbox")
        # The HTTP transport lands with the central node's own API. Until then
        # the outbox still buffers durably, so nothing is lost by the gap.
        replicator = Replicator(
            outbox=Outbox(f"{outbox_dir}/{site_id or 'site'}.db"),
            transport=InMemoryTransport())
        gaps.append("central replication has no HTTP transport yet; events are "
                    "buffered durably but not delivered")

    # --- geometry -------------------------------------------------------------
    geometry = GeometryStore()
    sync_runner = None
    export_path = env.get("EVAC_GEOMETRY_FILE", "")
    visiontrack_dsn = env.get("EVAC_VISIONTRACK_DSN", "")

    source = None
    if export_path:
        source = JsonExport(export_path)
    elif visiontrack_dsn:
        try:
            source = VisionTrackDatabase(visiontrack_dsn)
        except ValueError:
            source = None

    if source is not None and site_id:
        def sync_runner(when_ms: int):
            return run_sync(source, site_id=site_id, now_ms=when_ms,
                            tagged_zones=_tagged_zones(env), store=geometry)

        sync_runner(now_ms)
    else:
        gaps.append("no geometry source is configured, so no zone can be "
                    "resolved and nobody can reach an assembly point")

    # --- durable storage ------------------------------------------------------
    events_store = drill_store = None
    registry = None
    recovered: list[str] = []
    database_url = _database_url(env)

    if database_url:
        try:
            import sqlalchemy as sa

            from app.drill import DrillRegistry
            from app.store.drills import DrillStore
            from app.store.events import EventStore

            engine = sa.create_engine(database_url, pool_pre_ping=True)
            events_store = EventStore(engine=engine, health=ingestor.state.health)
            drill_store = DrillStore(engine=engine)
            registry = DrillRegistry()

            if site_id:
                # A node that restarts mid-evacuation must come back with the
                # same board rather than an empty one. This happens at startup
                # rather than on the first request, because the first request
                # may be an operator looking for people.
                for drill in registry.recover(
                        drill_store=drill_store, events_store=events_store,
                        site_id=site_id, now_ms=now_ms,
                        assembly_zones=_assembly_zones(env)):
                    recovered.append(drill.drill_id)
        except Exception as exc:
            gaps.append(
                f"the database could not be opened ({type(exc).__name__}); "
                "drills will run but nothing will be recorded and a restart "
                "will lose them")
            events_store = drill_store = registry = None
    else:
        gaps.append("no database is configured; drills run but are not "
                    "recorded, and a restart loses one in progress")

    if not env.get("EVAC_ROSTER_FILE") and not env.get("EVAC_FACETRACK_URL"):
        gaps.append("no roster source is configured; a drill has no "
                    "denominator and cannot be created")

    supervisor = build_supervisor(
        ingestor=ingestor, consumer=consumer, replicator=replicator,
        sync_runner=sync_runner)

    if events_store is not None:
        from app.service.supervisor import Job

        # Otherwise an event buffered during a database outage waits for the
        # next write to be retried, and a quiet drill never produces one.
        supervisor.add(Job(name="persist", interval_ms=5_000,
                           run=events_store.flush))

    return EdgeNode(
        site_id=site_id, tenant_id=tenant_id, ingestor=ingestor,
        supervisor=supervisor, consumer=consumer, replicator=replicator,
        geometry=geometry, events_store=events_store, drill_store=drill_store,
        registry=registry, recovered_drills=tuple(recovered),
        gaps=tuple(gaps))


def _database_url(env: dict) -> str:
    explicit = env.get("EVAC_DATABASE_URL")
    if explicit:
        return explicit
    password = env.get("EVAC_DB_PASSWORD")
    if not password:
        # No default. A password with a default is a password that ends up in
        # production, and a node that silently persists nowhere is worse than
        # one that says it has no database.
        return ""
    host = env.get("EVAC_DB_HOST", "localhost")
    port = env.get("EVAC_DB_PORT", "5432")
    name = env.get("EVAC_DB_NAME", "firedrill")
    user = env.get("EVAC_DB_USER", "firedrill")
    return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{name}"


def _assembly_zones(env: dict) -> frozenset:
    raw = env.get("EVAC_ASSEMBLY_ZONES", "")
    return frozenset(z.strip() for z in raw.split(",") if z.strip())


def _tagged_zones(env: dict) -> dict:
    """Zone kinds for this site, as `zone_id=KIND` pairs.

    Read from configuration rather than inferred, because there is no safe
    default for a zone whose purpose nobody has stated. See
    `app/sync/zone_kinds.py`.
    """
    raw = env.get("EVAC_ZONE_KINDS", "")
    tagged: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" in pair:
            zone_id, kind = pair.split("=", 1)
            tagged[zone_id.strip()] = kind.strip().upper()
    return tagged
