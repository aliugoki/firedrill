"""The HTTP surface, and the separation of duty it enforces."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.core.roster import ExpectationReason, Roster
from app.infra.auth import AuthSettings, issue
from app.drill import DrillRegistry
from app.infra.permissions import EVAC_ADMIN, EVAC_OPERATE, EVAC_READ, EVAC_WARDEN

T0 = 1_788_000_000_000
ASSEMBLY = frozenset({"assembly-north", "assembly-south"})


def roster_provider(site_id: str):
    roster = Roster()
    for i in range(6):
        roster.add_employee(
            emp_id=f"EMP-{i:03d}", display_name=f"Person {i}",
            has_gallery_entry=True, department="Engineering",
            home_floor_id="floor-2",
            assigned_assembly_zone="assembly-north" if i < 4 else "assembly-south",
            reason=ExpectationReason.ON_SHIFT)
    return roster.snapshot(T0)


#: A gateway-fronted deployment. Chosen for most of these tests because they
#: are about authorisation, not authentication, and a signed token per request
#: would obscure what each one is checking. `TestAuthentication` covers the
#: default, where headers are refused.
GATEWAY = AuthSettings(trust_headers=True)

#: The signed-token deployment, which is the only one that can carry a tenant.
JWT = AuthSettings(secret="a-test-signing-secret")


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(registry=DrillRegistry(),
                                 roster_provider=roster_provider,
                                 assembly_zones=ASSEMBLY, auth=GATEWAY))


@pytest.fixture
def jwt_client() -> TestClient:
    return TestClient(create_app(registry=DrillRegistry(),
                                 roster_provider=roster_provider,
                                 assembly_zones=ASSEMBLY, auth=JWT))


def headers(*permissions, user="user-1", zones=""):
    return {"X-User-Id": user, "X-Permissions": ",".join(permissions),
            "X-Zones": zones}


OPERATOR = headers(EVAC_READ, EVAC_OPERATE, user="commander-1")
VIEWER = headers(EVAC_READ, user="viewer-1")
WARDEN = headers(EVAC_READ, EVAC_WARDEN, user="warden-7", zones="assembly-north")
ADMIN = headers(EVAC_READ, EVAC_ADMIN, user="officer-1")


def make_drill(client) -> str:
    response = client.post("/api/evac/drills", headers=OPERATOR, json={
        "name": "Q3 drill", "site_id": "site-1", "tenant_id": "tenant-1"})
    assert response.status_code == 201
    return response.json()["drill_id"]


class TestAuthentication:
    def test_an_anonymous_caller_is_refused(self, client):
        assert client.get("/api/evac/drills").status_code == 401

    def test_a_caller_without_the_permission_is_refused(self, client):
        response = client.get("/api/evac/drills", headers=headers("some:other"))
        assert response.status_code == 403
        assert EVAC_READ in response.json()["detail"]


class TestHeadersAreNotTrustedByDefault:
    """Before this was enforced, anyone who could reach the service could name
    their own permissions. The insecure mode is still supported; it now has a
    name and a switch."""

    def _secured(self):
        settings = AuthSettings(secret="a-test-signing-secret")
        return settings, TestClient(create_app(
            registry=DrillRegistry(), roster_provider=roster_provider,
            assembly_zones=ASSEMBLY, auth=settings))

    def test_forged_identity_headers_are_refused(self):
        _, secured = self._secured()
        response = secured.get(
            "/api/evac/drills",
            headers={"X-User-Id": "attacker", "X-Permissions": "evac:admin"})
        assert response.status_code == 401
        assert "not trusted" in response.json()["detail"]

    def test_the_refusal_names_the_setting(self):
        # The alternative is somebody spending an afternoon on a 401 that is
        # really a deployment decision nobody made.
        _, secured = self._secured()
        detail = secured.get("/api/evac/drills",
                             headers={"X-User-Id": "x"}).json()["detail"]
        assert "EVAC_TRUST_IDENTITY_HEADERS" in detail

    def test_a_signed_token_is_accepted(self):
        settings, secured = self._secured()
        token = issue("viewer-1", settings, permissions=["evac:read"])
        assert secured.get("/api/evac/drills",
                           headers={"Authorization": f"Bearer {token}"}
                           ).status_code == 200

    def test_permissions_come_from_the_token_not_the_request(self):
        # A warden cannot widen their own scope by editing a header.
        settings, secured = self._secured()
        token = issue("warden-7", settings, permissions=["evac:read"])
        response = secured.post(
            "/api/evac/drills",
            headers={"Authorization": f"Bearer {token}",
                     "X-Permissions": "evac:operate"},
            json={"name": "x", "site_id": "s", "tenant_id": "t"})
        assert response.status_code == 403

    def test_zone_scope_comes_from_the_token(self):
        settings, secured = self._secured()
        commander = issue("commander-1", settings,
                          permissions=["evac:read", "evac:operate"])
        drill_id = secured.post(
            "/api/evac/drills",
            headers={"Authorization": f"Bearer {commander}"},
            json={"name": "x", "site_id": "site-1",
                  "tenant_id": "tenant-1"}).json()["drill_id"]

        warden = issue("warden-7", settings,
                       permissions=["evac:read", "evac:warden"],
                       zones=["assembly-north"])
        auth = {"Authorization": f"Bearer {warden}"}
        assert secured.get(f"/api/evac/drills/{drill_id}/warden/assembly-north",
                           headers=auth).status_code == 200
        refused = secured.get(
            f"/api/evac/drills/{drill_id}/warden/assembly-south", headers=auth)
        assert refused.status_code == 403
        assert "not assigned" in refused.json()["detail"]

    def test_an_unconfigured_service_refuses_everything(self):
        # No secret and no trusted headers. The safe default.
        bare = TestClient(create_app(registry=DrillRegistry(),
                                     roster_provider=roster_provider))
        assert bare.get("/api/evac/drills",
                        headers={"X-User-Id": "x"}).status_code == 401

    def test_and_says_so_in_health_rather_than_only_in_401s(self):
        bare = TestClient(create_app(registry=DrillRegistry()))
        body = bare.get("/healthz").json()
        assert body["degraded"] is True
        assert any("EVAC_JWT_SECRET" in gap
                   for gap in body["configuration_gaps"])


class TestSeparationOfDuty:
    """Not decoration. Collapsing these roles defeats the two-source evidence
    model the whole system rests on."""

    def test_a_warden_cannot_start_a_drill(self, client):
        drill_id = make_drill(client)
        response = client.post(f"/api/evac/drills/{drill_id}/start", headers=WARDEN)
        assert response.status_code == 403
        assert EVAC_OPERATE in response.json()["detail"]

    def test_a_warden_cannot_stop_one(self, client):
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        assert client.post(f"/api/evac/drills/{drill_id}/complete",
                           headers=WARDEN).status_code == 403

    def test_an_operator_cannot_sign_off_a_headcount(self, client):
        # The warden's physical count is their evidence and stays theirs.
        drill_id = make_drill(client)
        response = client.post(
            f"/api/evac/drills/{drill_id}/warden/headcount", headers=OPERATOR,
            json={"zone_id": "assembly-north", "warden_id": "commander-1",
                  "device_id": "desktop", "ts_ms": T0, "physical_count": 4})
        assert response.status_code == 403
        assert EVAC_WARDEN in response.json()["detail"]

    def test_an_operator_cannot_confirm_people(self, client):
        drill_id = make_drill(client)
        response = client.post(
            f"/api/evac/drills/{drill_id}/warden/sync", headers=OPERATOR,
            json={"actions": []})
        assert response.status_code == 403

    def test_a_safety_officer_does_not_operate_a_live_drill(self, client):
        drill_id = make_drill(client)
        assert client.post(f"/api/evac/drills/{drill_id}/start",
                           headers=ADMIN).status_code == 403

    def test_a_viewer_can_only_read(self, client):
        drill_id = make_drill(client)
        assert client.get(f"/api/evac/drills/{drill_id}/board",
                          headers=VIEWER).status_code == 200
        assert client.post(f"/api/evac/drills/{drill_id}/start",
                           headers=VIEWER).status_code == 403


class TestWardenZoneScoping:
    """A warden confirming people at a zone they are not standing in is making
    the one claim the system trusts above its own cameras, about a place they
    cannot see."""

    def test_a_warden_cannot_read_another_zone(self, client):
        drill_id = make_drill(client)
        response = client.get(
            f"/api/evac/drills/{drill_id}/warden/assembly-south", headers=WARDEN)
        assert response.status_code == 403
        assert "not assigned" in response.json()["detail"]

    def test_a_warden_cannot_count_another_zone(self, client):
        drill_id = make_drill(client)
        response = client.post(
            f"/api/evac/drills/{drill_id}/warden/headcount", headers=WARDEN,
            json={"zone_id": "assembly-south", "warden_id": "warden-7",
                  "device_id": "tablet-3", "ts_ms": T0, "physical_count": 2})
        assert response.status_code == 403

    def test_actions_for_another_zone_are_rejected_individually(self, client):
        # A device syncing a mixed batch must be told exactly what was refused.
        drill_id = make_drill(client)
        response = client.post(
            f"/api/evac/drills/{drill_id}/warden/sync", headers=WARDEN,
            json={"actions": [
                {"kind": "CONFIRM_PRESENT", "warden_id": "warden-7",
                 "device_id": "tablet-3", "zone_id": "assembly-north",
                 "ts_ms": T0, "device_seq": 1, "subject": "emp:EMP-000"},
                {"kind": "CONFIRM_PRESENT", "warden_id": "warden-7",
                 "device_id": "tablet-3", "zone_id": "assembly-south",
                 "ts_ms": T0, "device_seq": 2, "subject": "emp:EMP-004"}]})
        body = response.json()
        assert body["accepted"] == 1
        assert len(body["rejected"]) == 1
        assert "assembly-south" in body["rejected"][0]

    def test_a_warden_with_no_assigned_zones_is_unrestricted(self, client):
        # A roving supervisor. Explicit, not a default that erodes.
        drill_id = make_drill(client)
        roving = headers(EVAC_READ, EVAC_WARDEN, user="warden-9")
        assert client.get(f"/api/evac/drills/{drill_id}/warden/assembly-south",
                          headers=roving).status_code == 200


class TestDrillLifecycle:
    def test_a_drill_can_be_created_started_and_completed(self, client):
        drill_id = make_drill(client)
        assert client.post(f"/api/evac/drills/{drill_id}/start",
                           headers=OPERATOR).json()["status"] == "RUNNING"
        assert client.post(f"/api/evac/drills/{drill_id}/complete",
                           headers=OPERATOR).json()["status"] == "COMPLETE"

    def test_a_drill_cannot_be_started_twice(self, client):
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        assert client.post(f"/api/evac/drills/{drill_id}/start",
                           headers=OPERATOR).status_code == 409

    def test_two_drills_cannot_run_on_one_site(self, client):
        # Two simultaneous evacuations of one building would split the roster.
        first = make_drill(client)
        second = make_drill(client)
        client.post(f"/api/evac/drills/{first}/start", headers=OPERATOR)
        response = client.post(f"/api/evac/drills/{second}/start", headers=OPERATOR)
        assert response.status_code == 409
        assert first in response.json()["detail"]

    def test_a_drill_cannot_be_completed_before_it_starts(self, client):
        drill_id = make_drill(client)
        assert client.post(f"/api/evac/drills/{drill_id}/complete",
                           headers=OPERATOR).status_code == 409

    def test_an_unknown_drill_is_a_404(self, client):
        assert client.get("/api/evac/drills/nope", headers=VIEWER).status_code == 404

    def test_a_drill_without_a_roster_source_is_refused(self):
        # A drill with no roster has no denominator and cannot account for
        # anyone. Better to refuse than to show a confident zero.
        bare = TestClient(create_app(registry=DrillRegistry(), auth=GATEWAY))
        response = bare.post("/api/evac/drills", headers=OPERATOR, json={
            "name": "x", "site_id": "s", "tenant_id": "t"})
        assert response.status_code == 503
        assert "no denominator" in response.json()["detail"]


class TestTheBoard:
    def test_the_board_reports_the_roster_as_the_denominator(self, client):
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        board = client.get(f"/api/evac/drills/{drill_id}/board",
                           headers=VIEWER).json()
        assert board["expected"] == 6
        assert board["accounted"] == 0
        assert len(board["rows"]) == 6

    def test_no_raw_ai_metrics_reach_the_operator(self, client):
        # Scores and margins are real and are in the explain drawer. Putting
        # them on the live board invites second-guessing a threshold mid-drill.
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        board = client.get(f"/api/evac/drills/{drill_id}/board",
                           headers=VIEWER).json()
        blob = str(board).lower()
        for forbidden in ("score", "margin", "embedding", "cosine", "votes"):
            assert forbidden not in blob

    def test_every_row_carries_a_state_a_colour_and_a_reason(self, client):
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        rows = client.get(f"/api/evac/drills/{drill_id}/board",
                          headers=VIEWER).json()["rows"]
        for row in rows:
            assert row["colour"] in {"GREEN", "YELLOW", "ORANGE", "RED"}
            assert row["reason"].strip()

    def test_the_all_clear_is_blocked_and_says_why(self, client):
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        board = client.get(f"/api/evac/drills/{drill_id}/board",
                           headers=VIEWER).json()
        assert board["all_clear"] is False
        assert board["blocking_all_clear"]

    def test_an_unstarted_drill_says_so_in_the_blockers(self, client):
        drill_id = make_drill(client)
        board = client.get(f"/api/evac/drills/{drill_id}/board",
                           headers=VIEWER).json()
        assert "the drill has not been started" in board["blocking_all_clear"]

    def test_the_priority_list_is_available_and_capped(self, client):
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        rows = client.get(f"/api/evac/drills/{drill_id}/priority?limit=3",
                          headers=VIEWER).json()
        assert len(rows) <= 3

    def test_zone_panels_cover_every_assembly_zone(self, client):
        drill_id = make_drill(client)
        panels = client.get(f"/api/evac/drills/{drill_id}/zones",
                            headers=VIEWER).json()
        assert {p["zone_id"] for p in panels} == {"assembly-north", "assembly-south"}


class TestTiming:
    def test_an_empty_drill_reports_no_percentiles_rather_than_zero(self, client):
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        timing = client.get(f"/api/evac/drills/{drill_id}/timing",
                            headers=VIEWER).json()
        assert timing["building"]["p95"] is None
        assert timing["meets_target"] is None
        assert any("No measurements" in c
                   for c in timing["building"]["caveats"])

    def test_the_target_is_reported_alongside_the_result(self, client):
        drill_id = make_drill(client)
        timing = client.get(f"/api/evac/drills/{drill_id}/timing",
                            headers=VIEWER).json()
        assert timing["target_p95_s"] == 120.0


class TestWardenFlow:
    def _running(self, client) -> str:
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        return drill_id

    def test_a_warden_gets_their_zone_roster_and_the_system_health(self, client):
        # Health on the warden's screen too, so they know when to rely on their
        # own count rather than the list in front of them.
        drill_id = self._running(client)
        body = client.get(f"/api/evac/drills/{drill_id}/warden/assembly-north",
                          headers=WARDEN).json()
        assert len(body["roster"]) == 4
        assert body["system_health"]["degraded"] is False
        assert body["panel"]["zone_id"] == "assembly-north"

    def test_confirming_people_accounts_for_them(self, client):
        drill_id = self._running(client)
        actions = [
            {"kind": "CONFIRM_PRESENT", "warden_id": "warden-7",
             "device_id": "tablet-3", "zone_id": "assembly-north",
             "ts_ms": T0 + i, "device_seq": i + 1, "subject": f"emp:EMP-{i:03d}"}
            for i in range(4)]
        response = client.post(f"/api/evac/drills/{drill_id}/warden/sync",
                               headers=WARDEN, json={"actions": actions})
        assert response.json()["accepted"] == 4
        board = client.get(f"/api/evac/drills/{drill_id}/board",
                           headers=VIEWER).json()
        assert board["accounted"] == 4

    def test_a_redelivered_batch_is_deduplicated(self, client):
        # Offline devices retry. A warden must not confirm someone twice.
        drill_id = self._running(client)
        actions = [{"kind": "CONFIRM_PRESENT", "warden_id": "warden-7",
                    "device_id": "tablet-3", "zone_id": "assembly-north",
                    "ts_ms": T0, "device_seq": 1, "subject": "emp:EMP-000"}]
        client.post(f"/api/evac/drills/{drill_id}/warden/sync", headers=WARDEN,
                    json={"actions": actions})
        again = client.post(f"/api/evac/drills/{drill_id}/warden/sync",
                            headers=WARDEN, json={"actions": actions}).json()
        assert again["accepted"] == 0
        assert again["duplicates"] == 1

    def test_an_offline_action_is_accepted_and_flagged(self, client):
        drill_id = self._running(client)
        response = client.post(
            f"/api/evac/drills/{drill_id}/warden/sync", headers=WARDEN,
            json={"actions": [{
                "kind": "CONFIRM_PRESENT", "warden_id": "warden-7",
                "device_id": "tablet-3", "zone_id": "assembly-north",
                "ts_ms": T0, "device_seq": 1, "subject": "emp:EMP-000",
                "queued_offline": True}]})
        assert response.json()["accepted"] == 1

    def test_a_malformed_action_is_rejected_with_its_reason(self, client):
        drill_id = self._running(client)
        response = client.post(
            f"/api/evac/drills/{drill_id}/warden/sync", headers=WARDEN,
            json={"actions": [{
                "kind": "WRONG_PERSON", "warden_id": "warden-7",
                "device_id": "tablet-3", "zone_id": "assembly-north",
                "ts_ms": T0, "device_seq": 1, "subject": "emp:EMP-000"}]})
        body = response.json()
        assert body["accepted"] == 0
        assert "name the identity" in body["rejected"][0]

    def test_an_unknown_action_kind_is_rejected_not_crashed_on(self, client):
        drill_id = self._running(client)
        response = client.post(
            f"/api/evac/drills/{drill_id}/warden/sync", headers=WARDEN,
            json={"actions": [{
                "kind": "DELETE_EVERYTHING", "warden_id": "warden-7",
                "device_id": "tablet-3", "zone_id": "assembly-north",
                "ts_ms": T0, "device_seq": 1}]})
        assert "unknown action" in response.json()["rejected"][0]


class TestHeadcount:
    def _running(self, client) -> str:
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        return drill_id

    def test_a_matching_count_asks_for_nothing(self, client):
        drill_id = self._running(client)
        response = client.post(
            f"/api/evac/drills/{drill_id}/warden/headcount", headers=WARDEN,
            json={"zone_id": "assembly-north", "warden_id": "warden-7",
                  "device_id": "tablet-3", "ts_ms": T0, "physical_count": 0})
        body = response.json()
        assert body["kind"] == "MATCH"
        assert "No action" in body["recommended_action"]

    def test_the_system_counting_more_escalates(self, client):
        drill_id = self._running(client)
        actions = [{"kind": "CONFIRM_PRESENT", "warden_id": "warden-7",
                    "device_id": "tablet-3", "zone_id": "assembly-north",
                    "ts_ms": T0 + i, "device_seq": i + 1,
                    "subject": f"emp:EMP-{i:03d}"} for i in range(4)]
        client.post(f"/api/evac/drills/{drill_id}/warden/sync", headers=WARDEN,
                    json={"actions": actions})
        response = client.post(
            f"/api/evac/drills/{drill_id}/warden/headcount", headers=WARDEN,
            json={"zone_id": "assembly-north", "warden_id": "warden-7",
                  "device_id": "tablet-3", "ts_ms": T0 + 60_000,
                  "physical_count": 3})
        body = response.json()
        assert body["severity"] == "ESCALATE"
        assert body["missing_from_the_muster_point"] == 1
        assert "Do not declare all clear" in body["recommended_action"]


class TestExplain:
    def test_a_person_with_no_evidence_explains_honestly(self, client):
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        body = client.get(
            f"/api/evac/drills/{drill_id}/people/emp:EMP-000/explain",
            headers=VIEWER).json()
        narrative = "\n".join(body["narrative"])
        assert "Absence of evidence is not evidence of absence" in narrative

    def test_someone_not_on_the_roster_is_a_404(self, client):
        drill_id = make_drill(client)
        assert client.get(
            f"/api/evac/drills/{drill_id}/people/emp:NOBODY/explain",
            headers=VIEWER).status_code == 404


class TestHealthEndpoint:
    def test_it_answers_with_no_drill_running(self, client):
        body = client.get("/healthz").json()
        assert body["status"] == "ok"
        assert body["degraded"] is False

    def test_it_reports_the_running_drill(self, client):
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        body = client.get("/healthz").json()
        assert body["drill"] == drill_id
        assert body["degraded"] is False

    def test_it_needs_no_authentication(self, client):
        # The chaos script polls it from outside, and a health endpoint that
        # needs a token cannot tell you the token service is down.
        assert client.get("/healthz").status_code == 200


class TestOpenApi:
    def test_the_spec_is_generated(self, client):
        spec = client.get("/openapi.json").json()
        assert spec["info"]["title"] == "EVAC-120"
        paths = spec["paths"]
        assert "/api/evac/drills" in paths
        assert "/api/evac/drills/{drill_id}/board" in paths

    def test_the_safety_notice_is_in_the_spec(self, client):
        spec = client.get("/openapi.json").json()
        description = spec["info"]["description"]
        assert "never replaces, certified fire" in description
        assert "final authority" in description


class TestServingTheFrontEnds:
    """Same origin, because a service worker can only control its own origin
    and a warden's tablet has to start from cache with no network at all."""

    def test_the_command_centre_is_served(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "EVAC-120" in response.text

    def test_the_warden_pwa_is_served_at_its_own_path(self, client):
        response = client.get("/evac/warden")
        assert response.status_code == 200
        assert "manifest.webmanifest" in response.text

    def test_the_service_worker_is_allowed_a_root_scope(self, client):
        # Without this header the browser refuses a worker served from /static/
        # the scope /evac/, and the PWA silently loses its ability to start
        # offline — failing in the one way nobody notices until a real drill.
        response = client.get("/static/sw.js")
        assert response.status_code == 200
        assert response.headers.get("service-worker-allowed") == "/"

    def test_the_manifest_declares_an_installable_app(self, client):
        manifest = client.get("/static/manifest.webmanifest").json()
        assert manifest["display"] == "standalone"
        assert manifest["start_url"] == "/evac/warden"
        assert any(icon["sizes"] == "512x512" for icon in manifest["icons"])
        assert any(icon["purpose"] == "maskable" for icon in manifest["icons"])

    def test_every_file_the_worker_caches_actually_exists(self, client):
        # A service worker whose install list contains a 404 fails to install
        # entirely, and the app then never works offline at all.
        import re

        source = client.get("/static/sw.js").text
        shell = re.search(r"const SHELL = \[(.*?)\];", source, re.S).group(1)
        paths = re.findall(r"'([^']+)'", shell)
        assert paths
        for path in paths:
            assert client.get(path).status_code == 200, f"{path} is in SHELL but 404s"

    def test_the_static_assets_are_served(self, client):
        for path in ("/static/css/app.css", "/static/js/warden.js",
                     "/static/js/queue.js", "/static/js/render.js",
                     "/static/icons/icon-192.png"):
            assert client.get(path).status_code == 200, path


class TestHealthReportsConfigurationGaps:
    """A node with no roster is not healthy just because nothing has crashed."""

    def test_a_bare_edge_node_makes_the_service_degraded(self, client):
        from app.service.edge import build_edge

        client.app.state.edge = build_edge({}, now_ms=T0)
        body = client.get("/healthz").json()
        assert body["degraded"] is True
        assert body["configuration_gaps"]

    def test_the_gaps_are_named_so_a_chaos_run_can_read_them(self, client):
        from app.service.edge import build_edge

        client.app.state.edge = build_edge({}, now_ms=T0)
        gaps = "\n".join(client.get("/healthz").json()["configuration_gaps"])
        assert "no roster source" in gaps

    def test_without_an_edge_node_it_still_answers(self, client):
        # The API can run alone, and a health endpoint that 500s when a
        # collaborator is absent is worse than one that says so.
        assert client.get("/healthz").status_code == 200


class TestTenantScope:
    """CLAUDE.md: tenant-scope every query.

    A drill id is a UUID, so this is not the easiest hole to walk through, but
    "hard to guess" is not an access control. Two sites on one central replica
    is the whole reason `tenant_id` exists.
    """

    def _drill_for(self, client, tenant: str) -> str:
        token = issue("commander-1", JWT,
                      permissions=[EVAC_READ, EVAC_OPERATE], tenant_id=tenant)
        response = client.post(
            "/api/evac/drills", headers={"Authorization": f"Bearer {token}"},
            json={"tenant_id": tenant, "site_id": "site-1", "name": "theirs"})
        assert response.status_code == 201, response.text
        return response.json()["drill_id"]

    def _as(self, tenant: str) -> dict:
        token = issue("commander-2", JWT,
                      permissions=[EVAC_READ, EVAC_OPERATE], tenant_id=tenant)
        return {"Authorization": f"Bearer {token}"}

    def test_another_tenants_drill_is_not_readable(self, jwt_client):
        drill_id = self._drill_for(jwt_client, "tenant-a")
        response = jwt_client.get(f"/api/evac/drills/{drill_id}",
                                  headers=self._as("tenant-b"))
        assert response.status_code == 404

    def test_another_tenants_board_is_not_readable(self, jwt_client):
        drill_id = self._drill_for(jwt_client, "tenant-a")
        response = jwt_client.get(f"/api/evac/drills/{drill_id}/board",
                                  headers=self._as("tenant-b"))
        assert response.status_code == 404

    def test_another_tenants_drill_cannot_be_started(self, jwt_client):
        drill_id = self._drill_for(jwt_client, "tenant-a")
        response = jwt_client.post(f"/api/evac/drills/{drill_id}/start",
                                   headers=self._as("tenant-b"))
        assert response.status_code == 404

    def test_the_listing_shows_only_your_own(self, jwt_client):
        self._drill_for(jwt_client, "tenant-a")
        response = jwt_client.get("/api/evac/drills", headers=self._as("tenant-b"))
        assert response.status_code == 200
        assert response.json() == []

    def test_your_own_drill_is_still_readable(self, jwt_client):
        # The guard for the four above: they must be failing on the tenant, not
        # on something that refuses everybody.
        drill_id = self._drill_for(jwt_client, "tenant-a")
        assert jwt_client.get(f"/api/evac/drills/{drill_id}",
                              headers=self._as("tenant-a")).status_code == 200
        assert len(jwt_client.get("/api/evac/drills",
                                  headers=self._as("tenant-a")).json()) == 1

    def test_a_drill_cannot_be_created_under_somebody_elses_tenant(
            self, jwt_client):
        token = issue("commander-1", JWT,
                      permissions=[EVAC_READ, EVAC_OPERATE], tenant_id="tenant-a")
        response = jwt_client.post(
            "/api/evac/drills", headers={"Authorization": f"Bearer {token}"},
            json={"tenant_id": "tenant-b", "site_id": "site-1", "name": "theirs"})
        assert response.status_code == 403


class TestAGatewayCanSayWhichTenant:
    """Until it could, every gateway-fronted caller was tenantless, so every
    tenant check passed and §1.3 of the security document was aspirational."""

    def test_the_header_scopes_the_listing(self, client):
        made = client.post("/api/evac/drills", headers=OPERATOR,
                           json={"tenant_id": "tenant-a", "site_id": "site-1",
                                 "name": "theirs"})
        assert made.status_code == 201

        theirs = dict(headers(EVAC_READ, user="viewer-2"))
        theirs["X-Tenant-Id"] = "tenant-b"
        assert client.get("/api/evac/drills", headers=theirs).json() == []

        ours = dict(headers(EVAC_READ, user="viewer-3"))
        ours["X-Tenant-Id"] = "tenant-a"
        assert len(client.get("/api/evac/drills", headers=ours).json()) == 1

    def test_no_header_still_sees_everything(self, client):
        # Documented in EVAC120_SECURITY.md §1.3 as a finding, not a feature.
        # Asserted so that changing it is a deliberate act with a failing test
        # rather than a silent shift in who can read what.
        client.post("/api/evac/drills", headers=OPERATOR,
                    json={"tenant_id": "tenant-a", "site_id": "site-1",
                          "name": "theirs"})
        assert len(client.get("/api/evac/drills", headers=VIEWER).json()) == 1


class TestTheBlindBannerIsSpentOnBlindness:
    """`blind` is the operator screen's most serious state.

    It means nothing on the board can be trusted over a warden's own eyes. It
    used to be reconstructed as "some blindness has happened at some point and
    some outage is open now", so a camera that dropped for ten seconds early in
    the drill plus a slow database raised it. A banner that cries wolf over
    storage is a banner that gets ignored on the day a camera really is down.
    """

    def _running(self, client) -> str:
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        return drill_id

    def _board_health(self, client, drill_id) -> dict:
        return client.get(f"/api/evac/drills/{drill_id}/board",
                          headers=OPERATOR).json()["health"]

    def test_an_old_camera_outage_plus_a_live_database_one_is_not_blind(
            self, client):
        from app.ingest.health import Component

        drill_id = self._running(client)
        drill = client.app.state.registry.get(drill_id)
        health = drill.ingestor.state.health
        # Anchored to the drill's own start: the board measures the fraction
        # over [started_ms, now], and an outage stamped outside that window
        # contributes nothing.
        began = drill.started_ms
        health.degrade(Component.CAMERA, "cam-1", began, "offline")
        health.recover(Component.CAMERA, "cam-1", began + 10_000)
        health.degrade(Component.DATABASE, "events", began + 20_000,
                       "unreachable")

        body = self._board_health(client, drill_id)
        assert body["degraded"] is True
        assert body["blind_fraction"] > 0
        assert body["blind"] is False

    def test_a_camera_that_is_down_now_is_blind(self, client):
        from app.ingest.health import Component

        drill_id = self._running(client)
        drill = client.app.state.registry.get(drill_id)
        drill.ingestor.state.health.degrade(
            Component.CAMERA, "cam-1", drill.started_ms, "offline")
        assert self._board_health(client, drill_id)["blind"] is True


class TestAWardenTabletThatGoesQuiet:
    """A zone whose warden has walked out of range looks swept-in-progress.

    `WardenState.stale_devices` said the command centre needs this before it
    trusts a zone as settled, and nothing called it. It could not have helped:
    it reads the device's own pending queue, and on the server actions arrive
    already synced, so it reports nothing whatever happens.

    What the server can observe is when it last heard from a tablet. It was not
    recording that either. A warden with nothing new to report does not post a
    sync, so the PWA's five-second zone refresh is the heartbeat that separates
    a quiet warden from a departed one -- and it did not carry a device id.
    """

    def _running(self, client) -> str:
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        return drill_id

    def _panels(self, client, drill_id):
        return {p["zone_id"]: p for p in client.get(
            f"/api/evac/drills/{drill_id}/zones", headers=OPERATOR).json()}

    def test_a_zone_nobody_has_connected_to_says_so(self, client):
        drill_id = self._running(client)
        panel = self._panels(client, drill_id)["assembly-north"]
        assert panel["warden_silent_ms"] is None

    def test_a_zone_refresh_counts_as_contact(self, client):
        drill_id = self._running(client)
        client.get(f"/api/evac/drills/{drill_id}/warden/assembly-north"
                   "?device_id=tablet-3", headers=WARDEN)

        panel = self._panels(client, drill_id)["assembly-north"]
        assert panel["warden_silent_ms"] is not None
        assert panel["warden_silent_ms"] < 5_000

    def test_a_refresh_without_a_device_is_not_contact(self, client):
        # An operator reading a warden's zone from the command centre is not
        # evidence that the tablet is still there.
        drill_id = self._running(client)
        client.get(f"/api/evac/drills/{drill_id}/warden/assembly-north",
                   headers=WARDEN)
        assert self._panels(client, drill_id)["assembly-north"][
            "warden_silent_ms"] is None

    def test_a_sync_counts_as_contact_too(self, client):
        drill_id = self._running(client)
        client.post(f"/api/evac/drills/{drill_id}/warden/sync", headers=WARDEN,
                    json={"actions": [{
                        "kind": "CONFIRM_PRESENT", "warden_id": "warden-7",
                        "device_id": "tablet-3", "zone_id": "assembly-north",
                        "ts_ms": T0, "device_seq": 1,
                        "subject": "emp:EMP-000"}]})
        assert self._panels(client, drill_id)["assembly-north"][
            "warden_silent_ms"] is not None

    def test_the_quietest_device_covering_a_zone_is_the_one_reported(self,
                                                                     client):
        # Two tablets on one zone and one has gone away: that is the fact worth
        # surfacing, and taking the most recent would hide it.
        drill_id = self._running(client)
        drill = client.app.state.registry.get(drill_id)
        drill.warden.heard_from("tablet-3", "warden-7", T0, "assembly-north")
        drill.warden.heard_from("tablet-4", "warden-9", T0 + 600_000,
                                "assembly-north")

        silence = drill.warden.silence_for_zone(
            "assembly-north", T0 + 900_000)
        assert silence == 900_000


class TestTheExplainDrawerNamesTheDisagreement:
    """`is_disputed` was a boolean, and a boolean is not a report.

    Invariant 3 says a conflict is reported and never adjudicated. The drawer
    could say the system cannot settle this person's identity without saying
    between whom, which leaves an operator to work it out from thirty lines of
    observations. Blindness had the same problem from the other direction: it
    was inside `context`, indistinguishable from an ordinary sighting, and it
    is the answer to the question a warden actually asks.
    """

    def _running(self, client) -> str:
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        return drill_id

    def _explain(self, client, drill_id, person="emp:EMP-000"):
        return client.get(
            f"/api/evac/drills/{drill_id}/people/{person}/explain",
            headers=OPERATOR).json()

    def test_a_disputed_person_carries_the_competing_claims(self, client):
        from app.core.ledger import EvidenceKind, Stance

        drill_id = self._running(client)
        ledger = client.app.state.registry.get(drill_id).ingestor.state.ledger
        for identity in ("EMP-000", "EMP-001"):
            for i in range(3):
                ledger.record(subject="emp:EMP-000",
                              kind=EvidenceKind.IDENTITY_CONFIRMED,
                              ts_ms=T0 + i * 100, stance=Stance.SUPPORTS,
                              identity=identity, source="cam-9",
                              summary=f"claimed {identity}")

        body = self._explain(client, drill_id)
        assert body["is_disputed"] is True
        assert body["disputes"]
        assert set(body["disputes"][0]["identities"]) == {"EMP-000", "EMP-001"}

    def test_blindness_is_lifted_out_of_the_context_pile(self, client):
        from app.core.ledger import EvidenceKind, Stance

        drill_id = self._running(client)
        ledger = client.app.state.registry.get(drill_id).ingestor.state.ledger
        ledger.record(subject="emp:EMP-000", kind=EvidenceKind.CAMERA_DEGRADED,
                      ts_ms=T0, stance=Stance.CONTEXT, source="cam-9",
                      summary="cam-9 offline")
        ledger.record(subject="emp:EMP-000", kind=EvidenceKind.ZONE_SIGHTING,
                      ts_ms=T0 + 100, stance=Stance.CONTEXT, source="cam-9",
                      summary="seen on floor 2")

        body = self._explain(client, drill_id)
        assert [e["summary"] for e in body["blindness"]] == ["cam-9 offline"]
        # Still in context too: this is a second view of the same evidence,
        # not a move, and the narrative must stay complete.
        assert any(e["summary"] == "cam-9 offline" for e in body["context"])

    def test_an_ordinary_person_carries_neither(self, client):
        body = self._explain(client, self._running(client))
        assert body["disputes"] == []
        assert body["blindness"] == []


class TestTheRecordOfWhoDidWhat:
    """Written in nine places, persisted, and readable by nobody.

    `AUDIT_VIEW` is defined, is in the safety officer's role, and no route used
    it. A permission that unlocks nothing and a record nobody can consult are
    the same problem seen from two sides: "who declared the drill over at
    10:44" had an answer in the database and no way to ask it.
    """

    def _running(self, client) -> str:
        drill_id = make_drill(client)
        client.post(f"/api/evac/drills/{drill_id}/start", headers=OPERATOR)
        return drill_id

    def _auditor(self):
        from app.infra.permissions import AUDIT_VIEW

        return headers(EVAC_READ, AUDIT_VIEW, user="safety-officer-1")

    def test_the_drills_entries_can_be_read(self, client):
        drill_id = self._running(client)
        body = client.get(f"/api/evac/drills/{drill_id}/audit",
                          headers=self._auditor()).json()

        actions = [e["action"] for e in body["entries"]]
        assert "DRILL_CREATED" in actions
        assert "DRILL_STARTED" in actions

    def test_it_says_who(self, client):
        drill_id = self._running(client)
        body = client.get(f"/api/evac/drills/{drill_id}/audit",
                          headers=self._auditor()).json()
        assert all(e["actor_id"] == "commander-1" for e in body["entries"])

    def test_a_reader_without_the_permission_is_refused(self, client):
        drill_id = self._running(client)
        assert client.get(f"/api/evac/drills/{drill_id}/audit",
                          headers=OPERATOR).status_code == 403

    def test_it_says_whether_the_answer_is_durable(self, client):
        # An in-memory log holds only what this process did and is gone on a
        # restart, which changes what the list can be used to prove.
        drill_id = self._running(client)
        body = client.get(f"/api/evac/drills/{drill_id}/audit",
                          headers=self._auditor()).json()
        assert body["durable"] is False

    def test_another_tenants_drill_is_not_readable(self, client):
        from app.infra.permissions import AUDIT_VIEW

        drill_id = self._running(client)
        outsider = headers(EVAC_READ, AUDIT_VIEW, user="nosy-1")
        outsider["X-Tenant-Id"] = "tenant-elsewhere"
        assert client.get(f"/api/evac/drills/{drill_id}/audit",
                          headers=outsider).status_code == 404


class TestARosterWithHolesIsFlaggedAtCreation:
    """The last moment an operator can do anything about it.

    `coverage_gaps` was computed by the roster and read by nobody, so a site
    whose people have no assembly zone set discovered it when the first sweep
    found nobody to sweep.
    """

    def test_a_complete_roster_reports_no_gaps(self, client):
        body = client.post("/api/evac/drills", headers=OPERATOR,
                           json={"tenant_id": "t", "site_id": "site-1",
                                 "name": "Q3"}).json()
        assert body["roster_gaps"] == {}
        assert body["roster_trustworthy"] is True

    def test_missing_assembly_zones_are_named_in_the_response(self):
        from app.core.roster import ExpectationReason, Roster

        def zoneless(site_id):
            roster = Roster()
            for i in range(3):
                roster.add_employee(emp_id=f"EMP-{i:03d}", display_name=f"P{i}",
                                    has_gallery_entry=True,
                                    reason=ExpectationReason.ON_SHIFT)
            return roster.snapshot(T0)

        thin = TestClient(create_app(registry=DrillRegistry(),
                                     roster_provider=zoneless,
                                     assembly_zones=ASSEMBLY, auth=GATEWAY))
        body = thin.post("/api/evac/drills", headers=OPERATOR,
                         json={"tenant_id": "t", "site_id": "site-1",
                               "name": "Q3"}).json()
        assert body["roster_gaps"]["no_assembly_zone"] == 3
