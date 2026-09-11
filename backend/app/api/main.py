"""ASGI entry point. `uvicorn app.api.main:app`.

Deliberately thin, and deliberately explicit about what is not wired yet. What
this file decides is where the roster comes from and where the drill is written
down, and it decides both by reading the environment rather than by guessing:
a drill created against the wrong roster is worse than a drill that refuses to
be created, and a drill written nowhere is one a restart loses.
"""

from __future__ import annotations

import os
import time

from app.api.app import create_app
from app.core.roster import RosterSnapshot
from app.infra.audit import AuditLog
from app.infra.auth import settings_from_env
from app.infra.config import assembly_zones, database_url


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

        # Not a live source, and it used to say it was. This path exists as the
        # fallback for when FaceTrack cannot be reached, so the one code branch
        # that means "the authoritative source did not answer" was declaring
        # that it had. `is_trustworthy` gates the all-clear, so claiming it here
        # removed one of the four things that hold that verdict back.
        #
        # The age is in the note because it is the fact an operator can act on:
        # a file exported this morning and one exported in March are the same
        # file to everything else in the system.
        age_s = max(0.0, time.time() - os.path.getmtime(path))
        return roster.snapshot(
            int(time.time() * 1000),
            source_reachable=False,
            source_note=(f"loaded from the exported file {path}, written "
                         f"{age_s / 3600:.1f} hours ago; FaceTrack was not "
                         "consulted"))

    return provider


def _stores(env: dict):
    """The drill store, the event store, and an audit log that outlives us.

    All three or none: they share one engine, and a process holding some of
    them would persist part of a drill. Nothing here was wired before, so every
    drill created through this API lived in memory, produced no rows for the
    edge node's recovery to find, and reported `is_durable` to nobody.
    """
    url = database_url(env)
    audit = AuditLog()
    if not url:
        return None, None, audit

    try:
        import sqlalchemy as sa

        from app.store.audit import AuditStore
        from app.store.drills import DrillStore
        from app.store.events import EventStore

        engine = sa.create_engine(url, pool_pre_ping=True)
        audit.store = AuditStore(engine=engine)
        return (EventStore(engine=engine), DrillStore(engine=engine), audit)
    except Exception:
        # A drill still runs without a database: an operator with an evacuation
        # in progress needs the board more than the bookkeeping. `/healthz`
        # reports `durable: false` so they find out from a dashboard rather
        # than from a restart.
        return None, None, audit


def _recovered_registry(env: dict, drill_store, events_store):
    """A registry holding whatever was running when this process last stopped.

    The edge node has done this since Phase 3 and the API never did, so the two
    halves of one node disagreed after a restart: the edge came back with the
    drill and the API came back with an empty board. The board is the half an
    operator is looking at.

    A failure here is reported, not raised. A process that refuses to start
    gives an operator nothing to look at; one that starts and says what is
    missing gives them the answer.
    """
    from app.drill import DrillRegistry, RecoveryUnavailable

    registry = DrillRegistry()
    site_id = env.get("EVAC_SITE_ID", "")
    if drill_store is None or not site_id:
        return registry, ()

    try:
        registry.recover(
            drill_store=drill_store, events_store=events_store,
            site_id=site_id, now_ms=int(time.time() * 1000),
            assembly_zones=assembly_zones(env))
    except RecoveryUnavailable as exc:
        return registry, (
            f"{exc}. A drill that was running when this process stopped has "
            "not been reloaded, and the board is empty for that reason rather "
            "than because the building is",)
    return registry, ()


_env = dict(os.environ)
events_store, drill_store, audit = _stores(_env)
registry, startup_gaps = _recovered_registry(_env, drill_store, events_store)

app = create_app(registry=registry,
                 roster_provider=_roster_provider(),
                 assembly_zones=assembly_zones(_env),
                 auth=settings_from_env(_env),
                 audit=audit,
                 events_store=events_store,
                 drill_store=drill_store,
                 startup_gaps=startup_gaps)
