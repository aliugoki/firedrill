"""Persisting drill lifecycle, and recovering a drill that was interrupted.

The events are the source of truth; this table holds the little that cannot be
derived from them — the name somebody typed, and the roster as it stood when the
drill began.

**The roster is stored, not re-fetched.** A drill recovered after a restart must
see the same expected set it started with. Re-fetching would silently change the
denominator mid-drill: somebody who left for the day between the alarm and the
crash would vanish from the board, and on screen that is indistinguishable from
a person who disappeared in a stairwell.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa

from app.core.roster import (
    ExpectationReason,
    Population,
    RosterEntry,
    RosterSnapshot,
)
from app.store.errors import StoreUnavailable
from app.store.schema import drills as drills_table


@dataclass
class DrillStore:
    """Reads and writes drill rows.

    A drill that cannot be persisted still runs: the operator has an
    evacuation in progress, and refusing to start one because a replica is
    failing over would be the software choosing its own bookkeeping over the
    thing it exists for. So `save` reports failure and returns False.

    Reads raise `StoreUnavailable` instead. See its docstring for why.
    """

    engine: sa.Engine
    last_error: str | None = None

    def save(self, drill) -> bool:
        row = {
            "drill_id": drill.drill_id,
            "tenant_id": drill.tenant_id,
            "site_id": drill.site_id,
            "name": drill.name,
            "status": drill.status.value,
            "created_ms": drill.created_ms,
            "started_ms": drill.started_ms,
            "completed_ms": drill.completed_ms,
            "roster_snapshot": _roster_to_json(drill.roster),
        }
        try:
            with self.engine.begin() as connection:
                existing = connection.execute(
                    sa.select(drills_table.c.drill_id)
                    .where(drills_table.c.drill_id == drill.drill_id)).first()
                if existing:
                    connection.execute(
                        sa.update(drills_table)
                        .where(drills_table.c.drill_id == drill.drill_id)
                        .values(**row))
                else:
                    connection.execute(sa.insert(drills_table).values(**row))
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        self.last_error = None
        return True

    def unfinished(self, site_id: str) -> list:
        """Drills that were running when the process stopped.

        These are what recovery cares about. A completed drill is history; a
        running one is a building that may still have people in it.
        """
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(
                    sa.select(drills_table)
                    .where(drills_table.c.site_id == site_id)
                    .where(drills_table.c.status == "RUNNING")
                    .order_by(drills_table.c.created_ms))
                return [dict(row._mapping) for row in rows]
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise StoreUnavailable(self.last_error) from exc


def _roster_to_json(roster: RosterSnapshot) -> dict:
    return {
        "taken_at_ms": roster.taken_at_ms,
        "source_reachable": roster.source_reachable,
        "source_note": roster.source_note,
        "entries": [
            {field: _plain(getattr(entry, field)) for field in entry.__slots__}
            for entry in roster.entries
        ],
    }


def roster_from_json(payload: dict) -> RosterSnapshot:
    """Rebuild the frozen roster. The same expected set, not a fresh one."""
    entries = []
    for row in payload.get("entries", []):
        data = dict(row)
        data["population"] = Population(data["population"])
        data["reason"] = ExpectationReason(data["reason"])
        entries.append(RosterEntry(**data))
    return RosterSnapshot(
        taken_at_ms=payload.get("taken_at_ms", 0),
        entries=tuple(entries),
        source_reachable=payload.get("source_reachable", True),
        source_note=payload.get("source_note"))


def _plain(value):
    return value.value if hasattr(value, "value") else value
