"""Separation of duty in the seeded roles is a safety property, not a default.

The whole accountability model rests on two independent sources of evidence: what
the system observed, and what a human warden physically confirmed. A role that
holds both `evac:operate` and `evac:warden` can run a drill and sign off its own
headcount, which collapses those two sources into one. These tests exist so that
collapse cannot happen by accident in a later edit.
"""

import pytest

from app.infra.permissions import (
    ALL_PERMISSIONS,
    AUDIT_VIEW,
    DEFAULT_ROLES,
    DRILL_EXPORT,
    EVAC_ADMIN,
    EVAC_OPERATE,
    EVAC_READ,
    EVAC_WARDEN,
    PERMISSION_CATALOG,
    SYSTEM_ADMIN,
    SYSTEM_PERMISSIONS,
)

ROLES_BY_NAME = {r["name"]: set(r["permissions"]) for r in DEFAULT_ROLES}


class TestCatalog:
    def test_keys_are_unique(self):
        keys = [p["key"] for p in PERMISSION_CATALOG]
        assert len(keys) == len(set(keys))

    def test_every_key_is_resource_colon_action(self):
        for p in PERMISSION_CATALOG:
            resource, _, action = p["key"].partition(":")
            assert resource and action, p["key"]

    def test_every_entry_has_a_group_and_a_label(self):
        for p in PERMISSION_CATALOG:
            assert p["group"].strip()
            assert p["label"].strip()

    def test_all_permissions_matches_the_catalog(self):
        assert ALL_PERMISSIONS == {p["key"] for p in PERMISSION_CATALOG}

    def test_system_permissions_are_not_grantable_to_a_tenant(self):
        assert SYSTEM_ADMIN in SYSTEM_PERMISSIONS
        assert not (SYSTEM_PERMISSIONS & ALL_PERMISSIONS)


class TestSeededRoles:
    def test_every_granted_permission_exists(self):
        for name, perms in ROLES_BY_NAME.items():
            unknown = perms - ALL_PERMISSIONS
            assert not unknown, f"{name} grants unknown permissions: {unknown}"

    def test_no_role_holds_a_system_permission(self):
        for name, perms in ROLES_BY_NAME.items():
            assert not (perms & SYSTEM_PERMISSIONS), name

    def test_no_default_role_holds_everything(self):
        for name, perms in ROLES_BY_NAME.items():
            assert perms != ALL_PERMISSIONS, f"{name} is a superuser role"

    @pytest.mark.parametrize("name", ROLES_BY_NAME)
    def test_every_role_can_at_least_read(self, name):
        # A role that cannot see the board cannot act on it safely.
        assert EVAC_READ in ROLES_BY_NAME[name]


class TestSeparationOfDuty:
    @pytest.mark.parametrize("name", ROLES_BY_NAME)
    def test_no_role_both_runs_a_drill_and_confirms_a_headcount(self, name):
        perms = ROLES_BY_NAME[name]
        assert not (EVAC_OPERATE in perms and EVAC_WARDEN in perms), (
            f"{name} can both operate a drill and sign off its headcount, "
            "which collapses the two-source evidence model"
        )

    def test_the_warden_cannot_start_stop_or_reconfigure_a_drill(self):
        warden = ROLES_BY_NAME["Floor Warden"]
        assert EVAC_OPERATE not in warden
        assert EVAC_ADMIN not in warden

    def test_the_commander_cannot_confirm_people_or_reconfigure(self):
        commander = ROLES_BY_NAME["Incident Commander"]
        assert EVAC_WARDEN not in commander
        assert EVAC_ADMIN not in commander

    def test_the_safety_officer_does_not_operate_a_live_drill(self):
        officer = ROLES_BY_NAME["Safety Officer"]
        assert EVAC_ADMIN in officer
        assert EVAC_OPERATE not in officer
        assert EVAC_WARDEN not in officer

    def test_the_viewer_can_only_read(self):
        assert ROLES_BY_NAME["Viewer"] == {EVAC_READ}

    def test_exporting_a_report_is_separate_from_reading_the_board(self):
        # Reports carry personal data out of the building, so the warden, who
        # holds the most personal data on a device, cannot export them.
        warden = ROLES_BY_NAME["Floor Warden"]
        assert DRILL_EXPORT not in warden
        assert AUDIT_VIEW not in warden

    def test_someone_can_read_the_audit_log(self):
        # Invariant 6 is worthless if no seeded role can inspect the record.
        assert any(AUDIT_VIEW in perms for perms in ROLES_BY_NAME.values())
