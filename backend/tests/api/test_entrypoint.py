"""The ASGI entry point: where the roster comes from, and what it claims.

Sixty lines of wiring that nothing imported. It decides the one thing the API
cannot decide for itself -- the expected set -- and a drill created against the
wrong roster is worse than a drill that refuses to be created.
"""

from __future__ import annotations

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.main import _roster_provider
from app.infra.config import assembly_zones
from app.drill import DrillRegistry
from app.infra.auth import AuthSettings
from app.infra.permissions import EVAC_OPERATE, EVAC_READ

GATEWAY = AuthSettings(trust_headers=True)
OPERATOR = {"X-User-Id": "commander-1",
            "X-Permissions": f"{EVAC_READ},{EVAC_OPERATE}"}


@pytest.fixture
def roster_file(tmp_path):
    path = tmp_path / "roster.json"
    path.write_text(json.dumps({"employees": [
        {"emp_id": "EMP-001", "name": "Ali", "has_face": True},
        {"emp_id": "EMP-002", "name": "Sara", "has_face": True},
    ]}))
    return path


def with_env(monkeypatch, **values):
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


class TestAssemblyZones:
    def test_absent_means_none_rather_than_one_empty_name(self, monkeypatch):
        with_env(monkeypatch, EVAC_ASSEMBLY_ZONES=None)
        assert assembly_zones(dict(os.environ)) == frozenset()

    def test_blanks_and_spacing_are_tolerated(self, monkeypatch):
        with_env(monkeypatch, EVAC_ASSEMBLY_ZONES=" north , , south ")
        assert assembly_zones(dict(os.environ)) == frozenset({"north", "south"})


class TestTheRosterSource:
    def test_nothing_configured_is_no_provider(self, monkeypatch):
        # Which makes drill creation refuse with an explanation rather than
        # succeed with an empty roster -- a board reading "0 of 0 accounted"
        # looks like success.
        with_env(monkeypatch, EVAC_ROSTER_FILE=None)
        assert _roster_provider() is None

    def test_a_file_loads_the_people_in_it(self, monkeypatch, roster_file):
        with_env(monkeypatch, EVAC_ROSTER_FILE=str(roster_file))
        snapshot = _roster_provider()("site-1")
        assert {e.emp_id for e in snapshot.entries} == {"EMP-001", "EMP-002"}

    def test_a_file_is_not_a_live_source(self, monkeypatch, roster_file):
        """This path is the fallback for FaceTrack being unreachable.

        Claiming `source_reachable` here meant the one branch that means "the
        authoritative source did not answer" declared that it had, and
        `is_trustworthy` gates the all-clear.
        """
        with_env(monkeypatch, EVAC_ROSTER_FILE=str(roster_file))
        snapshot = _roster_provider()("site-1")
        assert snapshot.source_reachable is False
        assert snapshot.is_trustworthy is False

    def test_the_note_says_how_old_the_export_is(self, monkeypatch, roster_file):
        # A file exported this morning and one exported in March are the same
        # file to everything else in the system.
        old = time.time() - 50 * 3600
        os.utime(roster_file, (old, old))
        with_env(monkeypatch, EVAC_ROSTER_FILE=str(roster_file))
        snapshot = _roster_provider()("site-1")
        assert "50.0 hours ago" in snapshot.source_note
        assert "FaceTrack was not consulted" in snapshot.source_note


class TestABrokenRosterSource:
    def test_it_refuses_the_drill_and_says_why(self):
        def explodes(site_id):
            raise FileNotFoundError("/etc/evac/roster.json")

        client = TestClient(create_app(
            registry=DrillRegistry(), roster_provider=explodes,
            assembly_zones=frozenset(), auth=GATEWAY))
        response = client.post("/api/evac/drills", headers=OPERATOR,
                               json={"tenant_id": "t", "site_id": "site-1",
                                     "name": "Q3"})
        assert response.status_code == 503
        assert "roster source could not be read" in response.json()["detail"]
        assert "FileNotFoundError" in response.json()["detail"]


def a_roster(site_id: str):
    from app.core.roster import ExpectationReason, Roster

    roster = Roster()
    for i in range(3):
        roster.add_employee(
            emp_id=f"EMP-{i:03d}", display_name=f"Person {i}",
            has_gallery_entry=True, reason=ExpectationReason.ON_SHIFT,
            assigned_assembly_zone="north")
    return roster.snapshot(0)


class TestADrillCreatedHereIsWrittenDown:
    """Every drill created through this API used to live in memory alone.

    No rows, no events, nothing for the edge node's recovery to find, and
    `Drill.is_durable` reporting it to nobody. A restart mid-evacuation lost
    the board.
    """

    def with_stores(self, tmp_path):
        import sqlalchemy as sa

        from app.infra.audit import AuditLog
        from app.store.audit import AuditStore
        from app.store.drills import DrillStore
        from app.store.events import EventStore
        from app.store.schema import metadata

        engine = sa.create_engine(f"sqlite:///{tmp_path / 'evac.db'}")
        metadata.create_all(engine)
        audit = AuditLog(store=AuditStore(engine=engine))
        return engine, audit, TestClient(create_app(
            registry=DrillRegistry(), roster_provider=a_roster,
            assembly_zones=frozenset({"north"}), auth=GATEWAY, audit=audit,
            events_store=EventStore(engine=engine),
            drill_store=DrillStore(engine=engine)))

    def test_the_drill_reaches_the_database(self, tmp_path):
        from app.store.drills import DrillStore

        engine, _, client = self.with_stores(tmp_path)
        made = client.post("/api/evac/drills", headers=OPERATOR,
                           json={"tenant_id": "t", "site_id": "site-1",
                                 "name": "Q3"})
        drill_id = made.json()["drill_id"]
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)

        running = DrillStore(engine=engine).unfinished("site-1")
        assert [row["drill_id"] for row in running] == [drill_id]

    def test_the_board_says_whether_it_is_durable(self, tmp_path):
        _, _, client = self.with_stores(tmp_path)
        made = client.post("/api/evac/drills", headers=OPERATOR,
                           json={"tenant_id": "t", "site_id": "site-1",
                                 "name": "Q3"})
        client.post(f"/api/evac/drills/{made.json()['drill_id']}/start",
                    headers=OPERATOR)

        assert client.get("/healthz").json()["durable"] is True

    def test_a_process_with_no_database_says_so(self):
        client = TestClient(create_app(
            registry=DrillRegistry(), roster_provider=a_roster,
            assembly_zones=frozenset(), auth=GATEWAY))
        made = client.post("/api/evac/drills", headers=OPERATOR,
                           json={"tenant_id": "t", "site_id": "site-1",
                                 "name": "Q3"})
        client.post(f"/api/evac/drills/{made.json()['drill_id']}/start",
                    headers=OPERATOR)

        report = client.get("/healthz").json()
        assert report["durable"] is False
        assert report["audit_durable"] is False

    def test_the_audit_log_reaches_the_database_too(self, tmp_path):
        from app.store.audit import AuditStore

        engine, _, client = self.with_stores(tmp_path)
        made = client.post("/api/evac/drills", headers=OPERATOR,
                           json={"tenant_id": "t", "site_id": "site-1",
                                 "name": "Q3"})
        drill_id = made.json()["drill_id"]

        persisted = AuditStore(engine=engine).for_drill(drill_id)
        assert [entry.action.value for entry in persisted] == ["DRILL_CREATED"]


class TestARestartComesBackWithTheDrill:
    """The edge node has recovered running drills since Phase 3 and the API
    never did, so the two halves of one node disagreed after a restart: the
    edge came back with the drill and the API came back with an empty board.

    The board is the half an operator is looking at.
    """

    def stores(self, tmp_path):
        import sqlalchemy as sa

        from app.store.drills import DrillStore
        from app.store.events import EventStore
        from app.store.schema import metadata

        engine = sa.create_engine(f"sqlite:///{tmp_path / 'evac.db'}")
        metadata.create_all(engine)
        return EventStore(engine=engine), DrillStore(engine=engine)

    def a_running_drill(self, tmp_path):
        from app.api.main import _recovered_registry

        events_store, drill_store = self.stores(tmp_path)
        client = TestClient(create_app(
            registry=DrillRegistry(), roster_provider=a_roster,
            assembly_zones=frozenset({"north"}), auth=GATEWAY,
            events_store=events_store, drill_store=drill_store))
        made = client.post("/api/evac/drills", headers=OPERATOR,
                           json={"tenant_id": "t", "site_id": "site-1",
                                 "name": "Q3"})
        drill_id = made.json()["drill_id"]
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        return drill_id, events_store, drill_store, _recovered_registry

    def test_the_running_drill_is_reloaded(self, tmp_path):
        drill_id, events, drills, recover = self.a_running_drill(tmp_path)

        registry, gaps = recover({"EVAC_SITE_ID": "site-1"}, drills, events)
        assert gaps == ()
        assert [d.drill_id for d in registry.list()] == [drill_id]
        assert registry.running() is not None

    def test_the_reloaded_board_is_servable(self, tmp_path):
        drill_id, events, drills, recover = self.a_running_drill(tmp_path)
        registry, _ = recover({"EVAC_SITE_ID": "site-1"}, drills, events)

        after_restart = TestClient(create_app(
            registry=registry, roster_provider=a_roster,
            assembly_zones=frozenset({"north"}), auth=GATEWAY,
            events_store=events, drill_store=drills))
        board = after_restart.get(f"/api/evac/drills/{drill_id}/board",
                                  headers=OPERATOR)
        assert board.status_code == 200
        assert board.json()["expected"] == 3

    def test_no_site_id_means_no_recovery_rather_than_a_crash(self, tmp_path):
        _, events, drills, recover = self.a_running_drill(tmp_path)
        registry, gaps = recover({}, drills, events)
        assert registry.list() == []
        assert gaps == ()

    def test_a_store_that_cannot_answer_is_a_startup_gap(self):
        import sqlalchemy as sa

        from app.api.main import _recovered_registry
        from app.store.drills import DrillStore

        unmigrated = DrillStore(engine=sa.create_engine("sqlite:///:memory:"))
        registry, gaps = _recovered_registry(
            {"EVAC_SITE_ID": "site-1"}, unmigrated, None)

        assert registry.list() == []
        assert len(gaps) == 1
        assert "the board is empty for that reason" in gaps[0]

    def test_a_startup_gap_reaches_healthz_and_degrades_the_node(self):
        client = TestClient(create_app(
            registry=DrillRegistry(), roster_provider=a_roster,
            auth=GATEWAY, startup_gaps=("the drill could not be reloaded",)))
        report = client.get("/healthz").json()

        assert report["degraded"] is True
        assert "the drill could not be reloaded" in report["configuration_gaps"]
