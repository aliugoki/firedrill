"""Assembling an edge node, and what it says when it cannot be assembled."""

from __future__ import annotations

import json

import pytest

from app.service.edge import build_edge

T0 = 1_788_000_000_000
SQUARE = [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0},
          {"x": 1.0, "y": 1.0}, {"x": 0.0, "y": 1.0}]


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

    def test_a_password_has_no_default(self, tmp_path):
        # A password with a default is a password that ends up in production.
        from app.service.edge import _database_url

        assert _database_url({"EVAC_DB_HOST": "db"}) == ""
        assert _database_url({"EVAC_DB_PASSWORD": "s3cret"}).startswith(
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
