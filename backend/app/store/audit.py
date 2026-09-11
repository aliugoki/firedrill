"""Persisting the audit log.

The table has existed since the first migration and nothing wrote to it, so the
log lived in one process's memory and died with it. For a record whose purpose
is answering questions after an incident that is close to not having one: an
incident is exactly when somebody restarts things.

The contract matches `DrillStore`, and for the same reasons. A write that cannot
land reports failure and the drill carries on, because an evacuation in progress
outranks the bookkeeping. A read that cannot be answered raises, because every
falsy thing a read could return is also a legitimate answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import sqlalchemy as sa

from app.infra.audit import AuditAction, AuditEntry
from app.store.errors import StoreUnavailable
from app.store.schema import audit_entries


@dataclass
class AuditStore:
    """Append-only in the database as well as in memory."""

    engine: sa.Engine
    last_error: str | None = None

    def append(self, entry: AuditEntry) -> bool:
        """Persist one entry. Returns False if it could not be written."""
        try:
            with self.engine.begin() as connection:
                connection.execute(sa.insert(audit_entries).values(
                    entry_id=entry.entry_id,
                    drill_id=entry.drill_id,
                    action=entry.action.value,
                    actor_id=entry.actor_id,
                    ts_ms=entry.ts_ms,
                    subject=entry.subject,
                    summary=entry.summary,
                    before=entry.before,
                    after=entry.after,
                    context=json.loads(json.dumps(entry.context, default=str)),
                ))
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        self.last_error = None
        return True

    def for_drill(self, drill_id: str) -> list[AuditEntry]:
        """Every entry for one drill, oldest first."""
        return self._read(
            sa.select(audit_entries)
            .where(audit_entries.c.drill_id == drill_id)
            .order_by(audit_entries.c.ts_ms, audit_entries.c.entry_id))

    def recent(self, limit: int = 200) -> list[AuditEntry]:
        """The newest entries, oldest first within the page."""
        newest = self._read(
            sa.select(audit_entries)
            .order_by(audit_entries.c.ts_ms.desc(),
                      audit_entries.c.entry_id.desc())
            .limit(limit))
        return list(reversed(newest))

    def _read(self, query) -> list[AuditEntry]:
        try:
            with self.engine.connect() as connection:
                rows = connection.execute(query).fetchall()
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            raise StoreUnavailable(self.last_error) from exc
        return [_from_row(dict(row._mapping)) for row in rows]


def _from_row(row: dict) -> AuditEntry:
    return AuditEntry(
        action=AuditAction(row["action"]), actor_id=row["actor_id"],
        ts_ms=row["ts_ms"], drill_id=row["drill_id"], subject=row["subject"],
        summary=row["summary"] or "", before=row["before"], after=row["after"],
        context=row["context"] or {}, entry_id=row["entry_id"])
