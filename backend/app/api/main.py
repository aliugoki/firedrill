"""ASGI entry point. `uvicorn app.api.main:app`.

Deliberately thin, and deliberately explicit about what is not wired yet. The
roster provider is the only thing this file decides, and it decides it by
reading the environment rather than by guessing, because a drill created against
the wrong roster is worse than a drill that refuses to be created.
"""

from __future__ import annotations

import os

from app.api.app import create_app
from app.core.roster import RosterSnapshot


def _assembly_zones() -> frozenset:
    raw = os.getenv("EVAC_ASSEMBLY_ZONES", "")
    return frozenset(z.strip() for z in raw.split(",") if z.strip())


def _roster_provider():
    """Where the expected set comes from.

    Returns None when no source is configured, which makes drill creation fail
    with an explanation rather than succeed with an empty roster. An empty
    roster produces a board that reads "0 of 0 accounted", which looks like
    success and is the most dangerous screen this system could show.

    The FaceTrack HTTP client lands in Phase 5 with the deployment shell. Until
    then a JSON file is supported so a site can run a drill from an exported
    roster with no network at all, which is also the fallback when FaceTrack is
    unreachable at drill start.
    """
    path = os.getenv("EVAC_ROSTER_FILE")
    if not path:
        return None

    def provider(site_id: str) -> RosterSnapshot:
        import json
        import time

        from app.core.roster import from_facetrack

        with open(path) as handle:
            payload = json.load(handle)
        rows = payload.get("employees", payload if isinstance(payload, list) else [])
        local = payload.get("local_fields", {}) if isinstance(payload, dict) else {}
        roster = from_facetrack(rows, local_fields=local)
        return roster.snapshot(
            int(time.time() * 1000),
            source_reachable=True,
            source_note=f"loaded from {path}")

    return provider


app = create_app(roster_provider=_roster_provider(),
                 assembly_zones=_assembly_zones())
