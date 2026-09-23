"""Assembling an edge node, and what it says when it cannot be assembled."""

from __future__ import annotations

import json

import pytest

from app.core.events import Event, EventType, SourceKind
from app.service.edge import build_edge

T0 = 1_788_000_000_000
SQUARE = [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0},
          {"x": 1.0, "y": 1.0}, {"x": 0.0, "y": 1.0}]


def _an_event() -> Event:
    return Event(tenant_id="t", site_id="site-1", drill_id="d1", source="cam-1",
                 source_kind=SourceKind.CAMERA, seq=1,
                 type=EventType.TRACK_UPDATED, ts_ms=T0, subject="gp-1",
                 payload={"zone_id": "floor-1", "zone_kind": "FLOOR"})


def geometry_file(tmp_path):
    payload = {
        "floor_plans": [{
            "id": "fp1", "name": "Ground", "width_px": 1920, "height_px": 1080,
            "zones": [
                {"id": "z-floor", "name": "Zone 1", "polygon": SQUARE},
                {"id": "z-assembly", "name": "Zone 2", "polygon": SQUARE},
            ],
            "markers": [{"camera_id": "cam1", "x": 0.5, "y": 0.5}],
        }],
        "cameras": [{"id": "cam1", "calibration": {
            "homography": [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0],
            "floor_plan_id": "fp1"}}],
    }
    path = tmp_path / "geometry.json"
    path.write_text(json.dumps(payload))
    return str(path)


def full_env(tmp_path, *, database=True):
    """A node with nothing missing.

    A database is part of that: a node that persists nowhere loses a drill in
    progress on any restart, and calling it fully configured would make the gap
    invisible.
    """
    import sqlalchemy as sa

    from app.store.schema import metadata

    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"employees": []}))
    env = {
        "EVAC_SITE_ID": "site-1",
        "EVAC_TENANT_ID": "tenant-1",
        "EVAC_REDIS_URL": "redis://localhost:6379/0",
        "EVAC_GEOMETRY_FILE": geometry_file(tmp_path),
        "EVAC_ROSTER_FILE": str(roster),
        "EVAC_ZONE_KINDS": "z-floor=FLOOR,z-assembly=ASSEMBLY",
        "EVAC_ASSEMBLY_ZONES": "assembly-north,assembly-south",
    }
    if database:
        url = f"sqlite:///{tmp_path / 'edge.db'}"
        metadata.create_all(sa.create_engine(url))
        env["EVAC_DATABASE_URL"] = url
    return env


class TestAMisconfiguredNodeStartsAndExplainsItself:
    """A process that exits because Redis is not configured gives an operator
    nothing to look at."""

    def test_an_empty_environment_still_produces_a_node(self):
        node = build_edge({}, now_ms=T0)
        assert node.ingestor is not None
        assert node.supervisor.jobs

    def test_every_missing_piece_is_named(self):
        gaps = "\n".join(build_edge({}, now_ms=T0).gaps)
        assert "EVAC_SITE_ID" in gaps
        assert "no event stream" in gaps
        assert "no geometry source" in gaps
        assert "no roster source" in gaps

    def test_the_gaps_say_what_the_consequence_is(self):
        # Not "REDIS_URL missing" but what an operator loses by it.
        gaps = build_edge({}, now_ms=T0).gaps
        assert any("no camera observation can reach this node" in g for g in gaps)
        assert any("nobody can reach an assembly point" in g for g in gaps)
        assert any("has no denominator" in g for g in gaps)

    def test_a_node_with_gaps_reports_itself_degraded(self):
        node = build_edge({}, now_ms=T0)
        assert node.is_complete is False
        assert node.health(T0)["degraded"] is True

    def test_the_tick_runs_even_on_a_bare_node(self):
        # The one job that must never stop, running even when nothing else is
        # configured.
        node = build_edge({}, now_ms=T0)
        assert node.supervisor.job("tick").critical is True


class TestAFullyConfiguredNode:
    def test_it_has_no_gaps(self, tmp_path):
        node = build_edge(full_env(tmp_path), now_ms=T0)
        assert node.gaps == (), f"unexpected gaps: {node.gaps}"
        assert node.is_complete is True

    def test_it_registers_every_background_job_it_can(self, tmp_path):
        node = build_edge(full_env(tmp_path), now_ms=T0)
        names = [job.name for job in node.supervisor.jobs]
        assert "tick" in names
        assert "consume" in names
        assert "sync" in names

    def test_the_geometry_is_synced_at_startup(self, tmp_path):
        # Not on the first scheduled run five minutes later: a node that has
        # just restarted mid-drill needs its zones now.
        node = build_edge(full_env(tmp_path), now_ms=T0)
        assert node.geometry.has_geometry is True
        assert node.geometry.ready_for_a_drill() is True

    def test_an_untagged_zone_leaves_the_geometry_unusable(self, tmp_path):
        env = full_env(tmp_path)
        del env["EVAC_ZONE_KINDS"]
        node = build_edge(env, now_ms=T0)
        assert node.geometry.has_geometry is True
        assert node.geometry.ready_for_a_drill() is False
        assert node.health(T0)["geometry_ready"] is False

    def test_the_stream_name_is_scoped_to_the_tenant(self, tmp_path):
        node = build_edge(full_env(tmp_path), now_ms=T0)
        assert node.consumer.stream == "vt:evac:events:tenant-1"


class TestZoneTagging:
    def test_kinds_are_read_from_configuration(self, tmp_path):
        # There is no safe default for a zone whose purpose nobody has stated.
        node = build_edge(full_env(tmp_path), now_ms=T0)
        kinds = {zone.zone_id: zone.kind.value
                 for zone in node.geometry.geometry.zones}
        assert kinds == {"z-floor": "FLOOR", "z-assembly": "ASSEMBLY"}

    def test_a_malformed_pair_is_ignored_rather_than_guessed(self, tmp_path):
        env = full_env(tmp_path)
        env["EVAC_ZONE_KINDS"] = "z-assembly=ASSEMBLY,nonsense,z-floor"
        node = build_edge(env, now_ms=T0)
        tagged = {z.zone_id for z in node.geometry.geometry.zones}
        assert tagged == {"z-assembly"}


class TestHealthReporting:
    def test_it_reports_the_background_jobs(self, tmp_path):
        node = build_edge(full_env(tmp_path), now_ms=T0)
        node.supervisor.start(T0)
        health = node.health(T0)
        assert "background" in health
        assert "tick" in health["background"]["jobs"]

    def test_it_reports_what_the_stream_could_not_understand(self, tmp_path):
        # A pipeline emitting a shape the consumer does not know produces a
        # stream that looks healthy and carries nothing.
        node = build_edge(full_env(tmp_path), now_ms=T0)
        assert "unparseable_fraction" in node.health(T0)["stream"]

    def test_a_complete_node_is_not_degraded_by_configuration(self, tmp_path):
        node = build_edge(full_env(tmp_path), now_ms=T0)
        assert node.health(T0)["configuration_gaps"] == []

    def test_replication_backlog_is_zero_without_a_replicator(self, tmp_path):
        node = build_edge(full_env(tmp_path), now_ms=T0)
        assert node.health(T0)["replication_backlog"] == 0


class TestGeometryFallback:
    def test_a_broken_geometry_file_does_not_stop_the_node(self, tmp_path):
        env = full_env(tmp_path)
        broken = tmp_path / "broken.json"
        broken.write_text("{not json")
        env["EVAC_GEOMETRY_FILE"] = str(broken)
        node = build_edge(env, now_ms=T0)
        assert node.geometry.has_geometry is False
        assert node.supervisor.jobs  # still running

    def test_a_missing_geometry_file_does_not_stop_the_node(self, tmp_path):
        env = full_env(tmp_path)
        env["EVAC_GEOMETRY_FILE"] = str(tmp_path / "absent.json")
        node = build_edge(env, now_ms=T0)
        assert node.geometry.ready_for_a_drill() is False
        assert node.health(T0)["geometry_ready"] is False


class TestDurability:
    """A node that persists nowhere is worse than one that says it has no
    database, so the gap is named rather than inferred."""

    def _with_database(self, tmp_path, migrated=True):
        if migrated:
            return full_env(tmp_path)
        env = full_env(tmp_path, database=False)
        env["EVAC_DATABASE_URL"] = f"sqlite:///{tmp_path / 'unmigrated.db'}"
        return env

    def test_no_database_is_a_named_gap(self, tmp_path):
        node = build_edge(full_env(tmp_path, database=False), now_ms=T0)
        assert any("no database is configured" in gap for gap in node.gaps)
        assert node.health(T0)["durable"] is False

    def test_a_configured_database_makes_the_node_durable(self, tmp_path):
        node = build_edge(self._with_database(tmp_path), now_ms=T0)
        assert node.events_store is not None
        assert node.drill_store is not None
        assert node.health(T0)["durable"] is True

    def test_the_store_is_flushed_on_a_schedule(self, tmp_path):
        # Otherwise an event buffered during an outage waits for the next write
        # to be retried, and a quiet drill never produces one.
        node = build_edge(self._with_database(tmp_path), now_ms=T0)
        assert "persist" in [job.name for job in node.supervisor.jobs]

    def test_the_stores_health_is_reported(self, tmp_path):
        node = build_edge(self._with_database(tmp_path), now_ms=T0)
        store = node.health(T0)["store"]
        assert store["degraded"] is False
        assert store["buffered"] == 0

    def test_a_buffering_store_degrades_the_node_at_the_top_level(
            self, tmp_path):
        # A monitor alerts on `degraded`, not on `store.degraded`. A node
        # holding 40,000 events in memory behind a full disk used to answer
        # `degraded: false` with the truth three levels down.
        import sqlalchemy as sa

        node = build_edge(self._with_database(tmp_path), now_ms=T0)
        # A freshly built node has not connected to Redis yet, so start from a
        # clean health log -- otherwise this passes on somebody else's outage.
        node.ingestor.state.health.close_all(T0)
        assert node.health(T0)["degraded"] is False

        node.events_store.engine = sa.create_engine(
            "postgresql+psycopg2://nobody@127.0.0.1:1/nothing")
        node.events_store.append(_an_event(), now_ms=T0)

        report = node.health(T0)
        assert report["degraded"] is True
        assert report["store"]["buffered"] == 1

    def test_a_password_has_no_default(self, tmp_path):
        # A password with a default is a password that ends up in production.
        from app.infra.config import database_url

        assert database_url({"EVAC_DB_HOST": "db"}) == ""
        assert database_url({"EVAC_DB_PASSWORD": "s3cret"}).startswith(
            "postgresql+psycopg2://")

    def test_an_unopenable_database_degrades_rather_than_crashes(self, tmp_path):
        env = full_env(tmp_path)
        env["EVAC_DATABASE_URL"] = "postgresql+psycopg2://nobody@127.0.0.1:1/x"
        node = build_edge(env, now_ms=T0)
        # The engine constructs lazily, so this node believes it has a store
        # until it tries. What matters is that the process started.
        assert node.supervisor.jobs

    def test_missing_tables_do_not_stop_startup(self, tmp_path):
        # Migrations are a deploy step. A node started before them must say so
        # rather than refusing to run.
        node = build_edge(self._with_database(tmp_path, migrated=False),
                          now_ms=T0)
        assert node.recovered_drills == ()
        assert node.supervisor.jobs


class TestRecoveringOnStartup:
    def test_a_running_drill_is_reloaded(self, tmp_path):
        import sqlalchemy as sa

        from app.core.roster import ExpectationReason, Roster
        from app.drill import Drill
        from app.store.drills import DrillStore
        from app.store.events import EventStore
        from app.store.schema import metadata

        url = f"sqlite:///{tmp_path / 'edge.db'}"
        engine = sa.create_engine(url)
        metadata.create_all(engine)

        roster = Roster()
        roster.add_employee(emp_id="EMP-1", display_name="A",
                            has_gallery_entry=True,
                            assigned_assembly_zone="assembly-north",
                            reason=ExpectationReason.ON_SHIFT)
        drill = Drill(drill_id="live", tenant_id="t", site_id="site-1",
                      name="interrupted", roster=roster.snapshot(T0),
                      created_ms=T0,
                      events_store=EventStore(engine=engine),
                      drill_store=DrillStore(engine=engine))
        drill.start(T0)

        env = full_env(tmp_path, database=False)
        env["EVAC_DATABASE_URL"] = url
        node = build_edge(env, now_ms=T0 + 600_000)

        assert node.recovered_drills == ("live",)
        assert node.registry.running() is not None
        assert node.health(T0)["recovered_drills"] == ["live"]

    def test_recovery_happens_at_startup_not_on_first_request(self, tmp_path):
        # The first request may be an operator looking for people.
        node = build_edge(self._env(tmp_path), now_ms=T0)
        assert node.registry is not None

    def _env(self, tmp_path):
        return full_env(tmp_path)


class TestRecoveryThatCouldNotRead:
    """An empty board and an unreadable database look identical to a caller
    that only counts what came back.

    This is the startup path for a node that may have restarted mid-evacuation,
    so the difference has to survive to the operator.
    """

    class Unreadable:
        def unfinished(self, site_id):
            from app.store.drills import StoreUnavailable

            raise StoreUnavailable("OperationalError: database is locked")

        def save(self, drill):
            return False

    class Empty:
        def unfinished(self, site_id):
            return []

        def save(self, drill):
            return True

    def test_it_is_raised_rather_than_read_as_nothing_to_do(self):
        from app.drill import DrillRegistry, RecoveryUnavailable

        with pytest.raises(RecoveryUnavailable, match="database is locked"):
            DrillRegistry().recover(
                drill_store=self.Unreadable(), events_store=None,
                site_id="site-1", now_ms=0)

    def test_a_genuinely_empty_store_is_not_an_error(self):
        from app.drill import DrillRegistry

        assert DrillRegistry().recover(
            drill_store=self.Empty(), events_store=None,
            site_id="site-1", now_ms=0) == []


class TestReplicationConfiguration:
    """Each branch here is a line an operator reads at boot and acts on.

    They only differ by which environment variables are set, which is the
    matrix that never shows up until a real deployment.
    """

    def test_a_url_and_a_token_and_a_site_gives_a_replicator(self, tmp_path):
        node = build_edge({"EVAC_SITE_ID": "site-1",
                           "EVAC_CENTRAL_URL": "https://central.example/",
                           "EVAC_CENTRAL_TOKEN": "a-secret",
                           "EVAC_OUTBOX_DIR": str(tmp_path)})
        assert node.replicator is not None
        assert node.replicator.transport.url.endswith(
            "/api/evac/replication/events")
        assert node.replicator.transport.site_id == "site-1"

    def test_a_url_without_a_token_is_a_named_gap(self):
        # Central refuses unauthenticated batches, so the node would buffer
        # forever while reporting itself healthy.
        node = build_edge({"EVAC_SITE_ID": "site-1",
                           "EVAC_CENTRAL_URL": "https://central.example/"})
        assert node.replicator is None
        assert any("EVAC_CENTRAL_TOKEN is not" in gap for gap in node.gaps)

    def test_no_central_at_all_is_silent_rather_than_a_gap(self):
        # An edge node with no central is a supported deployment, not a fault.
        node = build_edge({"EVAC_SITE_ID": "site-1"})
        assert node.replicator is None
        assert not any("CENTRAL" in gap for gap in node.gaps)


class TestGeometrySources:

    def test_a_visiontrack_dsn_gives_a_source(self):
        node = build_edge({"EVAC_SITE_ID": "site-1",
                           "EVAC_VISIONTRACK_DSN": "postgres://vt/visiontrack"})
        assert any(job.name == "sync" for job in node.supervisor.jobs)

    def test_a_file_export_wins_over_a_dsn(self, tmp_path):
        # The offline path is a supported deployment rather than a fallback, so
        # naming a file is taken as meaning it.
        export = tmp_path / "geometry.json"
        export.write_text('{"floor_plans": [], "cameras": []}')
        node = build_edge({"EVAC_SITE_ID": "site-1",
                           "EVAC_GEOMETRY_FILE": str(export),
                           "EVAC_VISIONTRACK_DSN": "postgres://vt/visiontrack"})
        assert any(job.name == "sync" for job in node.supervisor.jobs)

    def test_neither_leaves_the_node_saying_it_cannot_place_anybody(self):
        node = build_edge({"EVAC_SITE_ID": "site-1"})
        assert node.geometry.has_geometry is False
        assert not any(job.name == "sync" for job in node.supervisor.jobs)


class TestADatabaseThatWillNotOpen:
    """Persistence is optional; pretending it is present is not.

    A node whose database cannot be opened still runs drills -- an operator
    with an evacuation in progress needs the board more than the bookkeeping --
    but it has to say so, at startup, rather than at the first write.
    """

    def test_it_starts_anyway_and_names_the_gap(self):
        node = build_edge({"EVAC_SITE_ID": "site-1",
                           "EVAC_DATABASE_URL": "not-a-database-url"})
        assert node.registry is None
        assert node.drill_store is None
        assert any("database could not be opened" in gap for gap in node.gaps)
        assert any("a restart will lose them" in gap for gap in node.gaps)

    def test_a_working_database_leaves_no_gap_about_it(self, tmp_path):
        node = build_edge({"EVAC_SITE_ID": "site-1",
                           "EVAC_DATABASE_URL":
                               f"sqlite:///{tmp_path / 'evac.db'}"})
        assert node.registry is not None
        assert not any("database" in gap for gap in node.gaps)

    def test_no_database_configured_is_its_own_message(self):
        node = build_edge({"EVAC_SITE_ID": "site-1"})
        assert any("no database is configured" in gap for gap in node.gaps)


class TestRetentionIsAControlRatherThanAPromise:
    """`retention.py` says a policy nobody checks is a promise, and until this
    ran on a schedule that is exactly what it was.

    Nothing outside its own tests had ever constructed a `RetentionLedger`,
    called `purge`, or called `verify`. Three hundred lines governing the
    deletion of biometric material, wired to nothing.
    """

    def test_the_node_runs_a_retention_job(self):
        node = build_edge({"EVAC_SITE_ID": "site-1"})
        assert any(job.name == "retention" for job in node.supervisor.jobs)

    def test_the_check_reaches_the_health_report(self):
        node = build_edge({"EVAC_SITE_ID": "site-1"})
        node.supervisor.job("retention").execute(1_000)

        report = node.health(1_000)
        assert report["retention_compliant"] is True
        assert report["retention_overdue"] == 0
        assert report["retention_held"] == 0

    def test_something_overdue_makes_the_node_say_so(self):
        from app.infra.retention import DataClass, Item

        node = build_edge({"EVAC_SITE_ID": "site-1"})
        # A face crop from a drill that ended long ago. The producers of these
        # are blocked behind the P2.3b segfault, so this is the shape the first
        # real one will have rather than one the system can make today.
        node.retention.track(Item(item_id="crop-1",
                                  data_class=DataClass.FACE_CROP,
                                  created_ms=0, drill_id="d1"))
        node.supervisor.job("retention").execute(10_000_000_000)

        report = node.health(10_000_000_000)
        assert report["retention_compliant"] is False
        assert report["retention_overdue"] == 1
        assert report["retention_held"] == 1

    def test_an_unreviewed_policy_is_reported_but_is_not_a_gap(self):
        """The durations are a starting point for a data-protection review, and
        a node that does not say so invites them being taken as its outcome.

        Not a configuration gap, though: nothing an operator can set fixes it,
        and a node that is permanently degraded has a degraded signal that
        means nothing. It is recorded as a known limit in
        EVAC120_SECURITY.md §6 and reported as its own fact here.
        """
        node = build_edge({"EVAC_SITE_ID": "site-1"})
        assert node.health(1_000)["retention_reviewed"] is False
        assert not any("retention" in gap for gap in node.gaps)


class TestTheAuditLogIsDurableWhenItCanBe:
    """The table has existed since the first migration and nothing wrote to it.

    A log whose purpose is answering questions after an incident, living in one
    process's memory, is close to not having one: an incident is exactly when
    somebody restarts things.
    """

    def test_a_node_with_a_database_gets_a_store(self, tmp_path):
        node = build_edge({"EVAC_SITE_ID": "site-1",
                           "EVAC_DATABASE_URL":
                               f"sqlite:///{tmp_path / 'evac.db'}"})
        assert node.audit.store is not None
        assert node.health(0)["audit_durable"] is True

    def test_a_node_without_one_says_so_rather_than_implying_durability(self):
        node = build_edge({"EVAC_SITE_ID": "site-1"})
        assert node.audit.store is None
        assert node.health(0)["audit_durable"] is False

    def test_entries_that_did_not_persist_are_counted(self, tmp_path):
        # An audit log that silently stops persisting is worse than one that
        # never claimed to: the entries are still being written, and only this
        # says they are going nowhere durable.
        from app.infra.audit import AuditAction

        node = build_edge({"EVAC_SITE_ID": "site-1",
                           "EVAC_DATABASE_URL":
                               f"sqlite:///{tmp_path / 'evac.db'}"})
        # The tables are created by Alembic, which has not run here.
        node.audit.record(action=AuditAction.DRILL_STARTED,
                          actor_id="commander-1", ts_ms=0, drill_id="d1")

        assert node.health(0)["audit_unpersisted"] == 1
        assert len(node.audit.entries) == 1


class TestBlockedReplicationIsNotAHealthyJob:
    """A node that has sent central nothing since it booted, reporting fine.

    `Replicator.flush` returns zero when central refuses the credential, on
    purpose: it stops the retrying that would hide a problem waiting cannot
    fix. But zero is also what a quiet drill returns, so the supervisor's
    replicate job succeeded every fifteen seconds forever, `is_healthy` stayed
    true, and the only trace was one key three levels down in the health
    report.
    """

    class Blocked:
        blocked_reason = "401 Unauthorized: unknown site credential"
        backlog = 12
        # A blocked link is not a stalled queue: it has stopped trying on
        # purpose, so the head has not been hammered.
        head_attempts = 0
        is_stalled = False

        @property
        def is_blocked(self):
            return True

        def flush(self, now_ms=None):
            return 0

        def lag_ms(self, now_ms):
            return 90_000

        def __init__(self):
            from app.ingest.replication import ReplicationStats

            self.stats = ReplicationStats(buffered=12)

    def node(self, tmp_path):
        node = build_edge(full_env(tmp_path), now_ms=T0)
        node.ingestor.state.health.close_all(T0)
        node.replicator = self.Blocked()
        return node

    def test_the_job_reports_the_reason_rather_than_succeeding(self, tmp_path):
        from app.service.supervisor import build_supervisor

        supervisor = build_supervisor(ingestor=self.node(tmp_path).ingestor,
                                      replicator=self.Blocked())
        supervisor.start(T0)
        supervisor.run_due(T0 + 60_000)

        job = {j.name: j for j in supervisor.jobs}["replicate"]
        assert job.failures == 1
        assert "credential" in job.last_error
        assert job.last_ok_ms is None

    def test_the_node_says_it_is_degraded(self, tmp_path):
        # The key a monitor alerts on. The fact had its own key and was absent
        # from this one.
        assert self.node(tmp_path).health(T0)["degraded"] is True

    def test_and_says_what_to_do_about_it(self, tmp_path):
        # There is no button anywhere that resolves this: the token is read
        # from the environment at startup and this process has no HTTP surface.
        action = self.node(tmp_path).health(T0)["replication_blocked_action"]
        assert "EVAC_CENTRAL_TOKEN" in action
        assert "restart" in action
        assert "nothing is lost" in action

    def test_a_working_link_carries_no_instruction(self, tmp_path):
        node = build_edge(full_env(tmp_path), now_ms=T0)
        assert node.health(T0)["replication_blocked_action"] is None


class TestAStalledQueueReachesTheMonitor:
    """A backlog central will never accept reaches none of the flags a monitor
    watches: not blocked, not an outage, just a number that grows while every
    other field says "retrying"."""

    class Stalled:
        blocked_reason = None
        is_blocked = False
        backlog = 5
        head_attempts = 47
        is_stalled = True

        def __init__(self):
            from app.ingest.replication import ReplicationStats
            self.stats = ReplicationStats(buffered=12)

        def lag_ms(self, now_ms):
            return 600_000

    def node(self, tmp_path):
        node = build_edge(full_env(tmp_path), now_ms=T0)
        node.ingestor.state.health.close_all(T0)
        node.replicator = self.Stalled()
        return node

    def test_it_reaches_the_key_a_monitor_alerts_on(self, tmp_path):
        assert self.node(tmp_path).health(T0)["degraded"] is True

    def test_and_says_it_is_not_an_outage_waiting_to_clear(self, tmp_path):
        action = self.node(tmp_path).health(T0)["replication_stalled_action"]
        assert "47 times" in action
        assert "not an outage" in action
        assert "Nothing is lost" in action

    def test_the_rpo_block_carries_the_attempts(self, tmp_path):
        rpo = self.node(tmp_path).health(T0)["rpo"]
        assert rpo["head_attempts"] == 47
        assert rpo["stalled"] is True

    def test_a_healthy_link_carries_no_instruction(self, tmp_path):
        node = build_edge(full_env(tmp_path), now_ms=T0)
        health = node.health(T0)
        assert health["replication_stalled_action"] is None
        # `full_env` builds a node with no replicator at all, so there is no
        # RPO block to read -- which is itself the honest answer and is
        # asserted rather than worked around.
        assert health["rpo"] is None


class TestTheRecoveryPointObjectiveIsReported:
    """`replication.py` says `measure_rpo` reports the loss window "from real
    counters rather than from this paragraph". It was called by nothing, so the
    paragraph was all there was.

    The module is careful about this for a reason it states itself: "we buffer
    events" invites the belief that nothing can be lost, and something can. It
    is a bounded, measured amount, and the bound is worth knowing before
    somebody relies on it.
    """

    def test_a_node_with_a_link_reports_its_loss_window(self, tmp_path):
        env = full_env(tmp_path)
        env["EVAC_CENTRAL_URL"] = "https://central.example"
        env["EVAC_CENTRAL_TOKEN"] = "t0ken"
        env["EVAC_OUTBOX_DIR"] = str(tmp_path / "outbox")
        rpo = build_edge(env, now_ms=T0).health(T0)["rpo"]

        assert rpo["unsent_events"] == 0
        assert rpo["loss_on_power_failure"] == 0
        assert rpo["producer_exposure_ms"] > 0

    def test_the_exposure_is_the_interval_the_supervisor_actually_uses(self,
                                                                       tmp_path):
        # Two copies of the number would drift, and the one in the report is
        # the one a site plans against.
        from app.service.supervisor import DEFAULT_REPLICATE_INTERVAL_MS

        env = full_env(tmp_path)
        env["EVAC_CENTRAL_URL"] = "https://central.example"
        env["EVAC_CENTRAL_TOKEN"] = "t0ken"
        env["EVAC_OUTBOX_DIR"] = str(tmp_path / "outbox")
        node = build_edge(env, now_ms=T0)

        assert node.health(T0)["rpo"]["producer_exposure_ms"] == (
            DEFAULT_REPLICATE_INTERVAL_MS)

    def test_a_node_with_no_central_reports_none_rather_than_zero(self, tmp_path):
        # Zero would read as "nothing can be lost", which is the opposite of
        # what an edge node with nowhere to replicate to means.
        assert build_edge(full_env(tmp_path), now_ms=T0).health(T0)["rpo"] is None


class TestTheRetentionJobPurgesRatherThanOnlyChecking:
    """The job verified and never purged: the audit of a control, without the
    control. `verify` is what proves the policy held; `purge` is what makes it
    hold, and running the check alone means the day a producer of face crops
    appears the finding arrives and nothing ever clears it."""

    def node(self, remover=None):
        node = build_edge({"EVAC_SITE_ID": "site-1"})
        node.retention_remover = remover
        return node

    def a_crop(self, node, item_id="crop-1"):
        from app.infra.retention import DataClass, Item

        node.retention.track(Item(item_id=item_id,
                                  data_class=DataClass.FACE_CROP,
                                  created_ms=0, drill_id="d1"))

    def test_a_due_item_is_removed_when_something_can_remove_it(self):
        gone = []
        node = self.node(remover=gone.append)
        self.a_crop(node)
        node.supervisor.job("retention").execute(10_000_000_000)

        assert [i.item_id for i in gone] == ["crop-1"]
        assert node.health(10_000_000_000)["retention_held"] == 0

    def test_with_nothing_to_remove_it_the_node_says_so(self):
        # Not "compliant". A node holding biometric material it cannot delete
        # is the one thing this policy exists to prevent, and marking it purged
        # would make it verify clean while keeping the crop forever.
        node = self.node(remover=None)
        self.a_crop(node)
        node.supervisor.job("retention").execute(10_000_000_000)

        report = node.health(10_000_000_000)
        assert report["retention_compliant"] is False
        assert report["retention_unremovable"] == 1
        assert report["retention_held"] == 1

    def test_a_clean_node_reports_nothing_unremovable(self):
        node = self.node()
        node.supervisor.job("retention").execute(1_000)
        assert node.health(1_000)["retention_unremovable"] == 0

    def test_the_worst_case_a_review_asks_about_is_reported(self):
        # `longest_biometric_life_ms` calls itself exactly that and was
        # computed by the policy since it was written and reported nowhere.
        node = self.node()
        assert node.health(1_000)["retention_longest_biometric_life_s"] >= 0
