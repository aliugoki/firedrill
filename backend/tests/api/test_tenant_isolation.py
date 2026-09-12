"""Every drill-scoped route, swept for cross-tenant access.

CLAUDE.md says to tenant-scope every query, and until this week not one
endpoint compared the caller's tenant with the drill's. The fix was per-route,
which means the rule decays the moment somebody adds a route and forgets.

So this enumerates the routes from the application itself rather than listing
them. A new drill-scoped endpoint is covered the day it is added, and the only
way to escape the sweep is to say out loud that it is exempt.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.core.roster import ExpectationReason, Roster
from app.drill import DrillRegistry
from app.infra.auth import AuthSettings, issue
from app.infra.permissions import (
    AUDIT_VIEW,
    EVAC_ADMIN,
    EVAC_OPERATE,
    EVAC_READ,
    EVAC_WARDEN,
)

T0 = 1_788_000_000_000
JWT = AuthSettings(secret="a-test-signing-secret")
ASSEMBLY = frozenset({"assembly-north"})

#: Routes that are deliberately not drill-scoped, with the reason. Anything
#: else carrying a `drill_id` has to pass the sweep.
EXEMPT: dict[str, str] = {}


def roster_provider(site_id: str):
    roster = Roster()
    for i in range(3):
        roster.add_employee(
            emp_id=f"EMP-{i:03d}", display_name=f"Person {i}",
            has_gallery_entry=True, reason=ExpectationReason.ON_SHIFT,
            assigned_assembly_zone="assembly-north")
    return roster.snapshot(T0)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app(registry=DrillRegistry(),
                                 roster_provider=roster_provider,
                                 assembly_zones=ASSEMBLY, auth=JWT))


def headers_for(tenant: str) -> dict:
    """A caller who holds everything, so a 403 here means a tenant check fired.

    Every permission the drill-scoped routes require, `AUDIT_VIEW` included: a
    sweep whose caller is short one permission stops testing tenant isolation
    on that route and starts testing the permission, which passes for the wrong
    reason and hides whatever the tenant check does.
    """
    token = issue(f"user-of-{tenant}", JWT, tenant_id=tenant,
                  zones=["assembly-north"],
                  permissions=[EVAC_READ, EVAC_OPERATE, EVAC_WARDEN,
                               EVAC_ADMIN, AUDIT_VIEW])
    return {"Authorization": f"Bearer {token}"}


def a_drill_belonging_to(client: TestClient, tenant: str) -> str:
    response = client.post("/api/evac/drills", headers=headers_for(tenant),
                           json={"tenant_id": tenant, "site_id": "site-1",
                                 "name": "theirs"})
    assert response.status_code == 201, response.text
    drill_id = response.json()["drill_id"]
    client.post(f"/api/evac/drills/{drill_id}/start",
                headers=headers_for(tenant))
    return drill_id


def drill_scoped_routes(client: TestClient):
    """(method, path template) for everything addressed by a drill id."""
    seen = []
    for route in client.app.routes:
        path = getattr(route, "path", "")
        if "{drill_id}" not in path or path in EXEMPT:
            continue
        for method in sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}):
            seen.append((method, path))
    return sorted(seen)


#: Values for the path parameters that are not the drill id. None of them has
#: to exist: the tenant check happens before the drill is looked into.
FILLERS = {
    "zone_id": "assembly-north",
    "person_ref": "emp:EMP-000",
}

#: The smallest body each write accepts, so a refusal is about the tenant
#: rather than about a malformed request.
BODIES = {
    "/api/evac/drills/{drill_id}/warden/sync": {"actions": []},
    "/api/evac/drills/{drill_id}/warden/headcount": {
        "zone_id": "assembly-north", "warden_id": "w", "device_id": "d",
        "ts_ms": T0, "physical_count": 3},
}


def fill(path: str, drill_id: str) -> str:
    filled = path.replace("{drill_id}", drill_id)
    for name, value in FILLERS.items():
        filled = re.sub(rf"\{{{name}(:[^}}]+)?\}}", value, filled)
    return filled


class TestNoRouteLeaksAcrossTenants:

    def test_the_sweep_found_the_routes(self, client):
        # Guards the guard: an empty sweep would pass every assertion below.
        assert len(drill_scoped_routes(client)) >= 10

    def test_every_drill_scoped_route_refuses_another_tenant(self, client):
        theirs = a_drill_belonging_to(client, "tenant-a")
        intruder = headers_for("tenant-b")

        leaked = []
        for method, path in drill_scoped_routes(client):
            response = client.request(
                method, fill(path, theirs), headers=intruder,
                json=BODIES.get(path, {}) if method == "POST" else None)
            if response.status_code not in (403, 404):
                leaked.append(f"{method} {path} -> {response.status_code}")
        assert leaked == []

    def test_the_owner_is_not_refused_by_the_same_check(self, client):
        """The other half: a check that refuses everybody would pass above.

        Only the reads are asserted, because the writes change the drill's
        lifecycle and several of them legitimately refuse a second time.
        """
        mine = a_drill_belonging_to(client, "tenant-a")
        owner = headers_for("tenant-a")

        refused = []
        for method, path in drill_scoped_routes(client):
            if method != "GET":
                continue
            response = client.request(method, fill(path, mine), headers=owner)
            if response.status_code != 200:
                refused.append(f"{method} {path} -> {response.status_code}")
        assert refused == []

    def test_a_drill_that_does_not_exist_answers_the_same_way(self, client):
        # A different answer for "not yours" and "not there" turns the id space
        # into a directory of other sites' drills.
        a_drill_belonging_to(client, "tenant-a")
        intruder = headers_for("tenant-b")

        for method, path in drill_scoped_routes(client):
            if method != "GET":
                continue
            missing = client.request(
                method, fill(path, "00000000-0000-0000-0000-000000000000"),
                headers=intruder)
            assert missing.status_code == 404, path
