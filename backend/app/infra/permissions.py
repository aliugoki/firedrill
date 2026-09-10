"""Permission registry.

Every protected action in EVAC-120 is named here as a string in the form
`resource:action`. Roles are stored as JSONB arrays of these strings. To add a
permission:

  1. Add the constant here.
  2. Add it to PERMISSION_CATALOG with a human-readable label.
  3. Use it on the route.

The catalog is exposed via the API so the frontend can render a permission
matrix.

The four core permissions are deliberately **role-shaped, not CRUD-shaped**,
because an evacuation has four kinds of actor and a warden must never be able to
start or stop a drill, nor an operator to sign off a physical headcount.
"""

from typing import Final


# -- The four EVAC-120 actors --------------------------------------------------
EVAC_READ: Final = "evac:read"
"""Command-center dashboards, drill history, reports. Read-only."""

EVAC_OPERATE: Final = "evac:operate"
"""Create, start and stop a drill; acknowledge system state."""

EVAC_WARDEN: Final = "evac:warden"
"""Warden mobile PWA: confirm present, not here, wrong person, sweep complete,
physical headcount entry. Scoped to the zones assigned to that warden."""

EVAC_ADMIN: Final = "evac:admin"
"""Configure zones, thresholds, retention, roster sources, warden assignments."""


# -- Supporting permissions ----------------------------------------------------
DRILL_EXPORT: Final = "drill:export"
"""Export a post-drill report. Separate from evac:read because reports leave
the building and carry personal data."""

AUDIT_VIEW: Final = "audit:view"
"""Read the audit log of warden actions and manual overrides."""

SYSTEM_ADMIN: Final = "system:admin"
"""Node administration: replication, retention jobs, tenant management. Not
granted to any tenant role."""


PERMISSION_CATALOG: list[dict[str, str]] = [
    {"key": EVAC_READ, "group": "Evacuation", "label": "View drills, live board, and reports"},
    {"key": EVAC_OPERATE, "group": "Evacuation", "label": "Create, start, and stop drills"},
    {"key": EVAC_WARDEN, "group": "Evacuation", "label": "Warden actions: confirm, reject, sweep, headcount"},
    {"key": EVAC_ADMIN, "group": "Evacuation", "label": "Configure zones, thresholds, and retention"},
    {"key": DRILL_EXPORT, "group": "Reporting", "label": "Export post-drill reports"},
    {"key": AUDIT_VIEW, "group": "Audit", "label": "View the audit log"},
]

ALL_PERMISSIONS: set[str] = {p["key"] for p in PERMISSION_CATALOG}

SYSTEM_PERMISSIONS: set[str] = {SYSTEM_ADMIN}


# -- Default seeded roles ------------------------------------------------------
# Separation of duty is the point of this table, not a side effect:
#
#   * An Incident Commander runs the drill but cannot sign off a physical
#     headcount -- that is the warden's evidence and must stay theirs.
#   * A Floor Warden confirms people but cannot start, stop, or reconfigure a
#     drill, and cannot see other wardens' zones.
#   * A Safety Officer configures the system but does not operate a live drill.
#
# No role holds every permission. Combining EVAC_OPERATE and EVAC_WARDEN in one
# role defeats the two-source evidence model the whole system rests on, so if a
# deployment needs it, that must be a deliberate custom role, never a default.
DEFAULT_ROLES: list[dict] = [
    {
        "name": "Incident Commander",
        "description": "Runs the drill from the command center",
        "is_system": True,
        "permissions": sorted([EVAC_READ, EVAC_OPERATE, DRILL_EXPORT, AUDIT_VIEW]),
    },
    {
        "name": "Floor Warden",
        "description": "Physically verifies people at an assigned assembly zone",
        "is_system": True,
        "permissions": sorted([EVAC_READ, EVAC_WARDEN]),
    },
    {
        "name": "Safety Officer",
        "description": "Configures zones, thresholds, retention, and warden assignments",
        "is_system": True,
        "permissions": sorted([EVAC_READ, EVAC_ADMIN, DRILL_EXPORT, AUDIT_VIEW]),
    },
    {
        "name": "Viewer",
        "description": "Read-only access to the live board and past drills",
        "is_system": True,
        "permissions": sorted([EVAC_READ]),
    },
]
