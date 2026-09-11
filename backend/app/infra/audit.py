"""The audit log: who did what to the system.

Distinct from the evidence ledger, and the distinction is worth holding onto.
The ledger is about **a person being evacuated** — what was observed, and why
they were marked safe. The audit log is about **a person operating the system** —
who started the drill, who overrode a decision, who exported a report full of
other people's movements.

They answer different questions after an incident. "Why was EMP-482 marked
accounted?" is the ledger. "Who decided to declare all clear at 10:44, and what
did the board say at that moment?" is this.

Append-only, and every entry carries what the system believed at the time. An
override recorded without the state it overrode is unreviewable: nobody can tell
afterwards whether it was a correction or a mistake.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum


class AuditAction(str, Enum):
    """Things worth being able to ask about afterwards."""

    DRILL_CREATED = "DRILL_CREATED"
    DRILL_STARTED = "DRILL_STARTED"
    DRILL_COMPLETED = "DRILL_COMPLETED"

    WARDEN_ACTION = "WARDEN_ACTION"
    HEADCOUNT_RECORDED = "HEADCOUNT_RECORDED"
    SWEEP_COMPLETED = "SWEEP_COMPLETED"
    ESCALATED = "ESCALATED"

    MANUAL_OVERRIDE = "MANUAL_OVERRIDE"
    """An operator changed a decision the system had made. The highest-scrutiny
    entry in the log, and the one that must carry before-and-after state."""

    ALL_CLEAR_DECLARED = "ALL_CLEAR_DECLARED"
    THRESHOLD_CHANGED = "THRESHOLD_CHANGED"
    ROSTER_OVERRIDDEN = "ROSTER_OVERRIDDEN"

    REPORT_EXPORTED = "REPORT_EXPORTED"
    """Personal data leaving the building. Logged because it is a disclosure."""

    RETENTION_PURGE = "RETENTION_PURGE"
    ACCESS_DENIED = "ACCESS_DENIED"


#: Actions that change what the system believes, rather than recording an
#: observation. These require `before` and `after` or they cannot be reviewed.
STATE_CHANGING: frozenset[AuditAction] = frozenset({
    AuditAction.MANUAL_OVERRIDE, AuditAction.THRESHOLD_CHANGED,
    AuditAction.ROSTER_OVERRIDDEN,
})

#: Actions that disclose personal data outside the system.
DISCLOSING: frozenset[AuditAction] = frozenset({AuditAction.REPORT_EXPORTED})

#: Actions for operations this system does not offer yet.
#:
#: Every other member has a call site. These four name features that do not
#: exist -- no endpoint lets an operator change a decision, alter a threshold,
#: override the roster, or declare all clear (the board derives that from the
#: counts and the sweeps, and nobody declares it). They are kept because the
#: vocabulary is the design, and separated because "defined and never recorded"
#: otherwise reads as an oversight rather than as a feature nobody has built.
#:
#: `tests/infra/test_audit_and_retention.py` checks the split, so an action
#: added without a call site is noticed rather than assumed.
NOT_YET_REACHABLE: frozenset[AuditAction] = frozenset({
    AuditAction.MANUAL_OVERRIDE, AuditAction.ALL_CLEAR_DECLARED,
    AuditAction.THRESHOLD_CHANGED, AuditAction.ROSTER_OVERRIDDEN,
})


class AuditError(ValueError):
    """An entry that would not be reviewable."""


@dataclass(frozen=True, slots=True)
class AuditEntry:
    action: AuditAction
    actor_id: str
    ts_ms: int
    drill_id: str | None = None
    subject: str | None = None
    summary: str = ""
    before: dict | None = None
    after: dict | None = None
    context: dict = field(default_factory=dict)
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def is_override(self) -> bool:
        return self.action in STATE_CHANGING

    @property
    def is_disclosure(self) -> bool:
        return self.action in DISCLOSING

    def describe(self) -> str:
        who = f"{self.actor_id}"
        what = self.action.value.lower().replace("_", " ")
        about = f" on {self.subject}" if self.subject else ""
        return f"{who} {what}{about}: {self.summary}"


@dataclass
class AuditLog:
    """Append-only. Nothing here is edited or deleted by the application.

    Retention removes personal data (§ `retention.py`), and when it does it
    records that it did — as an entry in this same log. A log that can be
    quietly trimmed proves nothing.
    """

    entries: list = field(default_factory=list)

    #: Optional durable backing. Without one the log lives in this process and
    #: dies with it, which for a record read after an incident is close to not
    #: having it: an incident is when things get restarted.
    store: object | None = None

    #: Entries the store would not take. Counted rather than raised -- an audit
    #: write must not interrupt an evacuation -- and surfaced, because an audit
    #: log that silently stops persisting is worse than one that was never
    #: claimed to persist at all.
    unpersisted: int = 0
    last_store_error: str | None = None

    def record(
        self, *, action: AuditAction, actor_id: str, ts_ms: int,
        drill_id: str | None = None, subject: str | None = None,
        summary: str = "", before: dict | None = None, after: dict | None = None,
        **context,
    ) -> AuditEntry:
        if not actor_id or not actor_id.strip():
            raise AuditError(
                "an audit entry with no actor is not an audit entry")

        if action in STATE_CHANGING and (before is None or after is None):
            # An override recorded without the state it overrode is
            # unreviewable: nobody can tell afterwards whether it was a
            # correction or a mistake.
            raise AuditError(
                f"{action.value} must record both the state before and after, "
                "or the override cannot be reviewed")

        entry = AuditEntry(
            action=action, actor_id=actor_id, ts_ms=ts_ms, drill_id=drill_id,
            subject=subject, summary=summary, before=before, after=after,
            context=dict(context))
        self.entries.append(entry)
        if self.store is not None and not self.store.append(entry):
            self.unpersisted += 1
            self.last_store_error = getattr(self.store, "last_error", None)
        return entry

    # -- reading ---------------------------------------------------------------

    def for_drill(self, drill_id: str) -> list:
        return [e for e in self.entries if e.drill_id == drill_id]

    def by_actor(self, actor_id: str) -> list:
        return [e for e in self.entries if e.actor_id == actor_id]

    def overrides(self, drill_id: str | None = None) -> list:
        """Every time a human changed what the system believed.

        The first thing anyone reviewing a drill should read, because it is the
        list of places where the recorded outcome is not the one the evidence
        produced.
        """
        return [e for e in self.entries
                if e.is_override and (drill_id is None or e.drill_id == drill_id)]

    def disclosures(self, drill_id: str | None = None) -> list:
        return [e for e in self.entries
                if e.is_disclosure and (drill_id is None or e.drill_id == drill_id)]

    def between(self, start_ms: int, end_ms: int) -> list:
        return [e for e in self.entries if start_ms <= e.ts_ms < end_ms]

    def at(self, ts_ms: int, window_ms: int = 60_000) -> list:
        """What was happening around a moment. For reconstructing a decision."""
        return self.between(ts_ms - window_ms, ts_ms + window_ms)

    def __len__(self) -> int:
        return len(self.entries)

    def summarise(self, drill_id: str | None = None) -> dict:
        entries = self.for_drill(drill_id) if drill_id else self.entries
        counts: dict[str, int] = {}
        for entry in entries:
            counts[entry.action.value] = counts.get(entry.action.value, 0) + 1
        return {
            "total": len(entries),
            "actors": len({e.actor_id for e in entries}),
            "overrides": sum(1 for e in entries if e.is_override),
            "disclosures": sum(1 for e in entries if e.is_disclosure),
            "by_action": counts,
        }
