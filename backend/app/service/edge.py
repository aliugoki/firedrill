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
from app.ingest.replication import Outbox, Replicator, measure_rpo
from app.service.supervisor import (
    DEFAULT_REPLICATE_INTERVAL_MS,
    Supervisor,
    build_supervisor,
)
from app.infra.audit import AuditLog
from app.infra.config import assembly_zones, database_url
from app.infra.retention import RetentionLedger
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
    #: How often the supervisor flushes to central. The producer half of the
    #: recovery point objective: events made in this window exist only in
    #: memory, so the number belongs beside the buffer's own counters rather
    #: than in a paragraph somewhere describing them.
    replicate_interval_ms: int = DEFAULT_REPLICATE_INTERVAL_MS
    geometry: GeometryStore = field(default_factory=GeometryStore)
    events_store: object | None = None
    drill_store: object | None = None
    registry: object | None = None
    recovered_drills: tuple = ()
    gaps: tuple = ()

    audit: AuditLog = field(default_factory=AuditLog)
    retention: RetentionLedger = field(default_factory=RetentionLedger)
    #: The last compliance check. `retention.py` says a policy nobody checks is
    #: a promise rather than a control, and until this ran on a schedule that
    #: is what it was: nothing outside its own tests ever called `verify`.
    retention_report: dict = field(default_factory=dict)
    #: What deletes the bytes. None until a producer of biometric material
    #: exists, which is blocked behind the P2.3b segfault. `purge` refuses to
    #: mark anything removed without one rather than recording a deletion
    #: nobody performed, so the day the first face crop is tracked the node
    #: reports it as unremovable instead of quietly keeping it forever.
    retention_remover: object | None = None

    @property
    def is_complete(self) -> bool:
        return not self.gaps

    def _rpo(self, now_ms: int) -> dict | None:
        """What a power cut or a dead disk would cost right now."""
        if self.replicator is None:
            return None
        measured = measure_rpo(self.replicator, now_ms=now_ms,
                               flush_interval_ms=self.replicate_interval_ms)
        return {
            "unsent_events": measured.unsent_events,
            "oldest_unsent_age_ms": measured.oldest_unsent_age_ms,
            "loss_on_power_failure": measured.durable_loss_on_power_failure,
            "loss_on_disk_failure": measured.loss_on_disk_failure,
            "producer_exposure_ms": measured.exposure_window_ms,
            "head_attempts": measured.head_attempts,
            "stalled": measured.is_stalled,
        }

    def health(self, now_ms: int) -> dict:
        state = self.ingestor.state
        report = {
            "site_id": self.site_id,
            # The event store is folded in here, not only into the `store`
            # block below. A monitor alerts on this key; a node buffering
            # 40,000 events into memory with a full disk behind it reported
            # `degraded: false` at the top and the truth three levels down.
            "degraded": (state.health.is_degraded or bool(self.gaps)
                         or (self.events_store is not None
                             and self.events_store.is_degraded)
                         # Blocked replication is a node that has sent central
                         # nothing since it booted and will not until somebody
                         # acts. It had its own key and was absent from the one
                         # a monitor alerts on.
                         or (self.replicator is not None
                             and (self.replicator.is_blocked
                                  # A queue whose head central keeps refusing
                                  # never drains either, and it reaches none of
                                  # the flags above: not blocked, not an
                                  # outage, just a backlog that grows forever
                                  # while every number here says "retrying".
                                  or self.replicator.is_stalled))),
            "blind": state.health.is_blind,
            "open_outages": len(state.health.open_now()),
            "events_accepted": state.accepted,
            "duplicates_dropped": state.duplicates_dropped,
            # A producer emitting rubbish used to look exactly like one
            # emitting nothing: `accepted` climbed and the state stayed empty.
            "malformed_rejected": state.rejected,
            "outstanding_gaps": len(state.tracker.outstanding_gaps()),
            "replication_backlog": (self.replicator.backlog
                                    if self.replicator else 0),
            # Surfaced separately from the backlog. A growing backlog with a
            # reachable central is an outage that will resolve; a blocked one
            # never will, and the two look identical from the depth alone.
            "replication_blocked": (self.replicator.blocked_reason
                                    if self.replicator else None),
            # What to do about it, beside the fact of it. The token is read
            # from the environment at startup and this process has no HTTP
            # surface, so there is no button anywhere that resolves this and an
            # operator staring at a reason from central needs to be told.
            "replication_blocked_action": (
                "central refused this node's credential. Correct "
                "EVAC_CENTRAL_TOKEN and restart the edge process; nothing is "
                "lost in the meantime, the events stay in the outbox."
                if self.replicator is not None and self.replicator.is_blocked
                else None),
            # Distinct from blocked, and from an ordinary outage. `attempts`
            # was incremented on every failed flush, carried through schema
            # migrations, and read by nothing -- so a node retrying one record
            # central will never accept looked exactly like one waiting for a
            # link to come back, and only one of those resolves itself.
            "replication_stalled_action": (
                f"the oldest unsent event has been refused "
                f"{self.replicator.head_attempts} times. This is not an "
                f"outage waiting to clear: central is rejecting that record "
                f"and the backlog behind it cannot drain until somebody looks "
                f"at it. Nothing is lost; nothing is being dropped either."
                if self.replicator is not None and self.replicator.is_stalled
                else None),
            # The recovery point objective, from live counters. The module's
            # own docstring says it is reported "from real counters rather than
            # from this paragraph", and `measure_rpo` was called by nothing --
            # so the paragraph was all there was. These are the numbers a site
            # needs before it decides how much it trusts the buffer.
            "rpo": self._rpo(now_ms),
            "geometry_ready": self.geometry.ready_for_a_drill(),
            # Reported even while it is trivially true. The producers of
            # biometric material are blocked behind the P2.3b segfault, so the
            # honest reading today is "nothing held, nothing overdue" -- and
            # the day the first face crop exists, the control is already
            # running rather than being remembered.
            "retention_compliant": self.retention_report.get("compliant", True),
            "retention_overdue": self.retention_report.get("overdue", 0),
            "retention_held": len(self.retention.held()),
            # Items that were due and could not be deleted because no remover
            # is configured. Zero today because nothing produces biometric
            # material yet, and the number that says so the moment one does.
            "retention_unremovable": len(
                self.retention_report.get("unremovable", ())),
            # The worst case a data-protection review asks about. Computed by
            # the policy since it was written and reported nowhere.
            "retention_longest_biometric_life_s": (
                self.retention.policy.longest_biometric_life_ms(
                    self.ingestor.state.elapsed_ms(now_ms)) / 1000),
            # Not a configuration gap: nothing an operator can set fixes it,
            # and a permanently-degraded node is a degraded signal that means
            # nothing. Reported as its own fact, and recorded as a known limit
            # in EVAC120_SECURITY.md §6.
            "retention_reviewed": self.retention.policy.calibrated,
            # An audit log that silently stops persisting is worse than one
            # that never claimed to: the entries are still being written, and
            # only this says they are going nowhere durable.
            "audit_durable": self.audit.store is not None,
            "audit_unpersisted": self.audit.unpersisted,
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


def _drill_end_times(node) -> dict:
    """When each drill this node knows about finished, for the DRILL_END rule.

    Without it every `DRILL_END` item reads as belonging to a drill still in
    progress and is never due, so the trigger the whole policy leans on --
    "thumbnails only, purged at drill end" -- would never fire.
    """
    registry = getattr(node, "registry", None) if node is not None else None
    if registry is None:
        return {}
    return {drill.drill_id: drill.completed_ms
            for drill in registry.list()
            if drill.completed_ms is not None}


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

    # --- replication to central ----------------------------------------------
    replicator = None
    central_url = env.get("EVAC_CENTRAL_URL", "")
    central_token = env.get("EVAC_CENTRAL_TOKEN", "")
    if central_url and central_token and site_id:
        from app.ingest.http_transport import HttpTransport

        outbox_dir = env.get("EVAC_OUTBOX_DIR", "./outbox")
        replicator = Replicator(
            outbox=Outbox(f"{outbox_dir}/{site_id}.db"),
            transport=HttpTransport(url=central_url.rstrip("/")
                                    + "/api/evac/replication/events",
                                    token=central_token, site_id=site_id))
    elif central_url:
        gaps.append("EVAC_CENTRAL_URL is set but EVAC_CENTRAL_TOKEN is not; "
                    "central refuses unauthenticated batches, so nothing would "
                    "be delivered")

    # --- the event stream -----------------------------------------------------
    consumer = None
    redis_url = env.get("EVAC_REDIS_URL", "")
    if redis_url and tenant_id:
        prefix = env.get("EVAC_EVENT_STREAM_PREFIX", "vt:evac:events")
        consumer = EventConsumer(
            ingestor=ingestor,
            client=RedisStreamClient(redis_url),
            stream=f"{prefix}:{tenant_id}",
            consumer_name=env.get("EVAC_CONSUMER_NAME", f"edge-{site_id or '1'}"),
            # Built after the replicator so every event this node folds is also
            # queued for central. Nothing held one before, so the outbox stayed
            # empty for the life of the node.
            replicator=replicator)
        # Set once the registry exists, further down: a camera observation has
        # to reach the drill it is about, and until now it reached the node's
        # ingestor and stopped there.
        consumer.ensure_group()
    else:
        gaps.append("no event stream is configured, so no camera observation "
                    "can reach this node")

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
        except ValueError as exc:
            # Cannot fire today: the only thing the adapter refuses is an empty
            # DSN, and the `elif` above has already excluded that. Kept as a
            # guard for a stricter check later, and made to speak, because
            # setting `source = None` silently would leave a site with a
            # configured geometry source, no geometry, and no explanation.
            source = None
            gaps.append(f"EVAC_VISIONTRACK_DSN was refused: {exc}")

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
    # Built before the database block, which hands it a store. It used to be
    # constructed afterwards, so that line raised `UnboundLocalError` -- and
    # the generic handler below reported it as "the database could not be
    # opened", which is a configuration problem an operator would have gone
    # looking for in the wrong place.
    audit = AuditLog()

    url = database_url(env)

    if url:
        try:
            import sqlalchemy as sa

            from app.drill import DrillRegistry, RecoveryUnavailable
            from app.store.audit import AuditStore
            from app.store.drills import DrillStore
            from app.store.events import EventStore

            engine = sa.create_engine(url, pool_pre_ping=True)
            events_store = EventStore(engine=engine, health=ingestor.state.health)
            audit.store = AuditStore(engine=engine)
            drill_store = DrillStore(engine=engine)
            registry = DrillRegistry()

            if site_id:
                # A node that restarts mid-evacuation must come back with the
                # same board rather than an empty one. This happens at startup
                # rather than on the first request, because the first request
                # may be an operator looking for people.
                for drill in registry.recover(
                        drill_store=drill_store, events_store=events_store,
                        replicator=replicator, site_id=site_id, now_ms=now_ms,
                        assembly_zones=assembly_zones(env)):
                    recovered.append(drill.drill_id)
            if consumer is not None:
                consumer.registry = registry
        except RecoveryUnavailable as exc:
            # The database opened; the recovery query failed. Persistence stays
            # on, because the next write may well succeed, but the board must
            # not come up looking like a building with no drill in it.
            gaps.append(
                f"{exc}. A drill that was running when this node stopped has "
                "not been reloaded, and the board is empty for that reason "
                "rather than because the building is")
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

    retention = RetentionLedger()
    node_holder: dict = {}

    def retention_check(when_ms: int) -> dict:
        """Purge what is due, then check that nothing outlived its class.

        Both, and in that order. The job used to verify only, which is the
        audit of a control without the control: `purge` is what actually
        removes the material, and running the check alone means the day a
        producer of face crops appears the finding arrives and nothing ever
        clears it.

        Verification after the purge rather than before, so what it reports is
        what survived a genuine attempt to remove it. Anything still overdue
        then is a failure of the remover, not a purge that had not run yet.
        """
        node = node_holder.get("node")
        drill_ended = _drill_end_times(node)
        purged = retention.purge(when_ms, audit=audit,
                                 drill_ended_ms=drill_ended,
                                 remover=node.retention_remover
                                 if node is not None else None)
        report = retention.verify(when_ms, drill_ended_ms=drill_ended)
        report["purged"] = purged
        # Lifted to the top level because it is the finding, not a detail of
        # the purge: an item that was due and could not be deleted is the one
        # state this policy exists to prevent.
        report["unremovable"] = purged["unremovable"]
        if node is not None:
            node.retention_report = report
        return report

    supervisor = build_supervisor(
        ingestor=ingestor, consumer=consumer, replicator=replicator,
        sync_runner=sync_runner, retention_check=retention_check)

    if events_store is not None:
        from app.service.supervisor import Job

        # Otherwise an event buffered during a database outage waits for the
        # next write to be retried, and a quiet drill never produces one.
        supervisor.add(Job(name="persist", interval_ms=5_000,
                           run=events_store.flush))

    node = EdgeNode(
        site_id=site_id, tenant_id=tenant_id, ingestor=ingestor,
        supervisor=supervisor, consumer=consumer, replicator=replicator,
        geometry=geometry, events_store=events_store, drill_store=drill_store,
        registry=registry, recovered_drills=tuple(recovered),
        gaps=tuple(gaps), retention=retention, audit=audit)
    # The job closes over this so its report reaches `health()`. The node
    # cannot be built before the supervisor it holds, and the supervisor needs
    # the job, so one of the two has to be handed over afterwards.
    node_holder["node"] = node
    return node


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
