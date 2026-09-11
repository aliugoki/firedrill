"""The post-drill report over HTTP, and the log of who did what.

Both were built and unreachable. `build_report` is the deliverable of Phase 5
and lived in a function two tests and a script called. `AuditLog` had twelve
action kinds and nothing in `app/` had ever constructed one, so "who ended the
drill at 10:44, and what did the board say then" had no answer anywhere.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.core.roster import ExpectationReason, Roster
from app.drill import DrillRegistry
from app.infra.audit import AuditAction, AuditLog
from app.infra.auth import AuthSettings
from app.infra.permissions import EVAC_OPERATE, EVAC_READ

T0 = 1_788_000_000_000
GATEWAY = AuthSettings(trust_headers=True)
OPERATOR = {"X-User-Id": "commander-1",
            "X-Permissions": f"{EVAC_READ},{EVAC_OPERATE}"}
VIEWER = {"X-User-Id": "viewer-9", "X-Permissions": EVAC_READ}


def roster_provider(site_id: str):
    roster = Roster()
    for i in range(4):
        roster.add_employee(
            emp_id=f"EMP-{i:03d}", display_name=f"Person {i}",
            has_gallery_entry=True, reason=ExpectationReason.ON_SHIFT,
            assigned_assembly_zone="assembly-north")
    return roster.snapshot(T0)


@pytest.fixture
def audit() -> AuditLog:
    return AuditLog()


@pytest.fixture
def client(audit) -> TestClient:
    return TestClient(create_app(
        registry=DrillRegistry(), roster_provider=roster_provider,
        assembly_zones=frozenset({"assembly-north"}), auth=GATEWAY,
        audit=audit))


def a_running_drill(client: TestClient) -> str:
    made = client.post("/api/evac/drills", headers=OPERATOR,
                       json={"tenant_id": "t", "site_id": "site-1",
                             "name": "Q3 drill"})
    drill_id = made.json()["drill_id"]
    client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
    return drill_id


class TestTheReportIsReachable:

    def test_it_carries_the_verdict_and_the_text(self, client):
        drill_id = a_running_drill(client)
        body = client.get(f"/api/evac/drills/{drill_id}/report",
                          headers=OPERATOR).json()

        assert body["drill_id"] == drill_id
        assert body["outcome"] in ("PASS", "FAIL", "INCONCLUSIVE")
        assert any("EVAC-120 drill report" in line for line in body["rendered"])

    def test_it_is_available_before_the_drill_ends(self, client):
        # A commander wanting to know where they stand at minute six should not
        # have to end the drill to find out.
        drill_id = a_running_drill(client)
        assert client.get(f"/api/evac/drills/{drill_id}/report",
                          headers=OPERATOR).status_code == 200

    def test_a_drill_with_nobody_confirmed_is_not_a_safe_result(self, client):
        drill_id = a_running_drill(client)
        body = client.get(f"/api/evac/drills/{drill_id}/report",
                          headers=OPERATOR).json()
        assert body["outcome"] != "PASS"


class TestTheAuditLogAnswersWhoDidWhat:

    def test_creating_starting_and_ending_are_all_recorded(self, client, audit):
        drill_id = a_running_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/complete", headers=OPERATOR)

        actions = [entry.action for entry in audit.for_drill(drill_id)]
        assert AuditAction.DRILL_CREATED in actions
        assert AuditAction.DRILL_STARTED in actions
        assert AuditAction.DRILL_COMPLETED in actions

    def test_ending_records_what_the_board_said_at_that_moment(
            self, client, audit):
        """The audit log's own docstring asks for this.

        An entry saying somebody ended the drill, without what the board showed
        when they did, cannot answer whether the decision was sound.
        """
        drill_id = a_running_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/complete", headers=OPERATOR)

        ended = [e for e in audit.for_drill(drill_id)
                 if e.action is AuditAction.DRILL_COMPLETED][0]
        assert ended.context["all_clear"] is False
        assert ended.context["expected"] == 4
        assert ended.context["accounted"] == 0
        assert ended.context["blocking"]

    def test_it_names_who_did_it(self, client, audit):
        drill_id = a_running_drill(client)
        assert all(entry.actor_id == "commander-1"
                   for entry in audit.for_drill(drill_id))

    def test_reading_the_report_is_logged_as_a_disclosure(self, client, audit):
        # It names people and where they were seen. Personal data leaving the
        # building is a disclosure whoever asks for it should be named against.
        drill_id = a_running_drill(client)
        client.get(f"/api/evac/drills/{drill_id}/report", headers=VIEWER)

        exports = [e for e in audit.for_drill(drill_id)
                   if e.action is AuditAction.REPORT_EXPORTED]
        assert len(exports) == 1
        assert exports[0].actor_id == "viewer-9"
        assert exports[0].is_disclosure is True
        assert exports[0].context["while_running"] is True

    def test_nothing_else_is_recorded_by_merely_looking(self, client, audit):
        drill_id = a_running_drill(client)
        before = len(audit.entries)
        client.get(f"/api/evac/drills/{drill_id}/board", headers=VIEWER)
        client.get(f"/api/evac/drills/{drill_id}/timing", headers=VIEWER)
        assert len(audit.entries) == before


class TestAnInjectedLogIsTheOneUsed:
    """An empty `AuditLog` is falsy: it defines `__len__`.

    So `audit or AuditLog()` built a second one and wrote to that, leaving the
    caller holding an empty log and no way to tell. Every injected dependency
    in `create_app` is checked with `is None` now, including the two that are
    truthy today and would do the same the day somebody gives them a `__len__`.
    """

    def test_an_empty_log_is_falsy(self):
        assert not AuditLog()

    def test_and_is_still_the_one_the_app_writes_to(self, client, audit):
        a_running_drill(client)
        assert len(audit.entries) == 2

    def test_an_injected_registry_is_the_one_used(self):
        registry = DrillRegistry()
        app = create_app(registry=registry, roster_provider=roster_provider,
                         assembly_zones=frozenset(), auth=GATEWAY)
        assert app.state.registry is registry

    def test_injected_settings_are_the_ones_used(self):
        settings = AuthSettings(secret="x")
        app = create_app(registry=DrillRegistry(), auth=settings)
        assert app.state.auth is settings
