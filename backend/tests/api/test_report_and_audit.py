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


class TestTheWardenSideIsRecordedToo:
    """Sweeps and escalations by name, confirmations in bulk.

    A confirmation is evidence and it is already in the ledger; forty of them
    per sync would bury the two entries a review actually asks about.
    """

    WARDEN = {"X-User-Id": "warden-7", "X-Permissions": "evac:warden",
              "X-Zones": "assembly-north"}

    def sync(self, client, drill_id, *actions):
        return client.post(
            f"/api/evac/drills/{drill_id}/warden/sync", headers=self.WARDEN,
            json={"actions": [
                {"warden_id": "warden-7", "device_id": "tablet-3",
                 "zone_id": "assembly-north", "ts_ms": T0 + 60_000,
                 "device_seq": i + 1, **action}
                for i, action in enumerate(actions)]})

    def test_a_sweep_is_recorded_by_name(self, client, audit):
        drill_id = a_running_drill(client)
        assert self.sync(client, drill_id,
                         {"kind": "SWEEP_COMPLETE"}).json()["accepted"] == 1

        swept = [e for e in audit.for_drill(drill_id)
                 if e.action is AuditAction.SWEEP_COMPLETED]
        assert len(swept) == 1
        assert swept[0].actor_id == "warden-7"
        assert swept[0].context["zone_id"] == "assembly-north"

    def test_an_escalation_keeps_the_warden_s_words(self, client, audit):
        drill_id = a_running_drill(client)
        self.sync(client, drill_id,
                  {"kind": "ESCALATE", "note": "smoke in the west stairwell"})

        raised = [e for e in audit.for_drill(drill_id)
                  if e.action is AuditAction.ESCALATED]
        assert raised[0].summary == "smoke in the west stairwell"

    def test_confirmations_are_summarised_rather_than_listed(
            self, client, audit):
        drill_id = a_running_drill(client)
        self.sync(client, drill_id,
                  *[{"kind": "CONFIRM_PRESENT", "subject": f"emp:EMP-{i:03d}"}
                    for i in range(4)])

        batches = [e for e in audit.for_drill(drill_id)
                   if e.action is AuditAction.WARDEN_ACTION]
        assert len(batches) == 1
        assert batches[0].context["accepted"] == 4

    def test_a_refused_action_is_named_in_the_batch(self, client, audit):
        drill_id = a_running_drill(client)
        self.sync(client, drill_id, {"kind": "WRONG_PERSON",
                                     "subject": "emp:EMP-000"})

        batch = [e for e in audit.for_drill(drill_id)
                 if e.action is AuditAction.WARDEN_ACTION][0]
        assert batch.context["accepted"] == 0
        assert "name the identity" in batch.context["refused"][0]

    def test_a_headcount_is_recorded_with_both_numbers(self, client, audit):
        drill_id = a_running_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/warden/headcount",
                    headers=self.WARDEN,
                    json={"zone_id": "assembly-north", "warden_id": "warden-7",
                          "device_id": "tablet-3", "ts_ms": T0 + 120_000,
                          "physical_count": 3})

        counted = [e for e in audit.for_drill(drill_id)
                   if e.action is AuditAction.HEADCOUNT_RECORDED][0]
        assert counted.context["physical_count"] == 3
        assert counted.context["system_count"] == 0
        assert counted.context["tolerated_overcount"] == 0


class TestARefusalIsRecorded:
    """A refusal is a fact about who tried, and a run of them is the shape of
    somebody looking for a way in."""

    def test_a_caller_without_the_permission_is_logged(self, client, audit):
        response = client.get("/api/evac/drills",
                              headers={"X-User-Id": "nosy-1",
                                       "X-Permissions": "some:other"})
        assert response.status_code == 403

        denied = [e for e in audit.entries
                  if e.action is AuditAction.ACCESS_DENIED]
        assert len(denied) == 1
        assert denied[0].actor_id == "nosy-1"
        assert denied[0].context["path"] == "/api/evac/drills"
        assert denied[0].context["held"] == ["some:other"]

    def test_an_allowed_caller_is_not(self, client, audit):
        client.get("/api/evac/drills", headers=VIEWER)
        assert not any(e.action is AuditAction.ACCESS_DENIED
                       for e in audit.entries)
