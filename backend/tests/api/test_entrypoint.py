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
from app.api.main import _assembly_zones, _roster_provider
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
        assert _assembly_zones() == frozenset()

    def test_blanks_and_spacing_are_tolerated(self, monkeypatch):
        with_env(monkeypatch, EVAC_ASSEMBLY_ZONES=" north , , south ")
        assert _assembly_zones() == frozenset({"north", "south"})


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
