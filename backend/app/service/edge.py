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
            "configuration_gaps": list(self.gaps),
            "background": self.supervisor.health(now_ms),
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

    if not env.get("EVAC_ROSTER_FILE") and not env.get("EVAC_FACETRACK_URL"):
        gaps.append("no roster source is configured; a drill has no "
                    "denominator and cannot be created")

    supervisor = build_supervisor(
        ingestor=ingestor, consumer=consumer, replicator=replicator,
        sync_runner=sync_runner)

    return EdgeNode(
        site_id=site_id, tenant_id=tenant_id, ingestor=ingestor,
        supervisor=supervisor, consumer=consumer, replicator=replicator,
        geometry=geometry, gaps=tuple(gaps))


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
