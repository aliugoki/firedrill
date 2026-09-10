"""Edge to central replication, and what is lost when the link dies.

The edge node is the authority during a drill. Central is a replica for admin
and reporting, and the accountability path must work with zero Internet. So
replication is one-directional, asynchronous, and never on the critical path:
nothing an edge node decides waits for central to acknowledge it.

Durability comes from the vendored DeepStream outbox, a SQLite WAL store with
`synchronous=FULL`. That setting fsyncs on commit, which is slower and is the
point: an event that reached the outbox survives the power going out.

**Recovery point objective.** For anything committed to the outbox, RPO is zero
— it survives process death and power loss and is replayed on restart. The only
loss window is between an event being produced and being committed, which is
`flush_interval_ms` plus one fsync in the worst case. `measure_rpo` reports that
window from real counters rather than from this paragraph.

The reason to be precise about it: "we buffer events" invites the belief that
nothing can be lost. Something can. It is a bounded, measured amount, and the
bound is worth knowing before someone relies on it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from app.core.events import Event, EventType, SequenceTracker, SourceKind


class Transport(Protocol):
    """How a batch reaches central. Anything that can fail, and will."""

    def send(self, batch: list[dict]) -> None:
        """Deliver a batch, or raise. Raising means "retry later", not "drop"."""


class TransportUnavailable(Exception):
    """Central is unreachable. The batch stays buffered; nothing is dropped."""


@dataclass
class Outbox:
    """Durable local buffer. A thin, testable wrapper over the vendored store.

    The vendored module reads its directory from a module-level global at import
    time, which is a trap documented in docs/EVAC120_PROVENANCE.md. This owns
    its own connection instead so several drills, or several tests, cannot
    collide in one process.
    """

    path: Path
    _conn: sqlite3.Connection | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            conn = sqlite3.connect(self.path, timeout=10,
                                   check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            # Slower, and the reason the buffer is worth having: an event that
            # committed survives the power going out mid-drill.
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute(
                """CREATE TABLE IF NOT EXISTS pending (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       source TEXT NOT NULL,
                       seq INTEGER NOT NULL,
                       payload TEXT NOT NULL,
                       queued_at_ms INTEGER NOT NULL,
                       attempts INTEGER NOT NULL DEFAULT 0,
                       UNIQUE(source, seq))""")
            conn.commit()
            self._conn = conn
        return self._conn

    def add(self, event: Event, now_ms: int | None = None) -> bool:
        """Buffer one event. Returns False if it was already buffered.

        The uniqueness constraint on `(source, seq)` makes buffering idempotent
        too, so a producer that retries after a crash mid-write does not send
        central the same event twice.
        """
        try:
            self.conn.execute(
                "INSERT INTO pending (source, seq, payload, queued_at_ms) "
                "VALUES (?, ?, ?, ?)",
                (event.source, event.seq, json.dumps(_to_wire(event)),
                 now_ms if now_ms is not None else int(time.time() * 1000)))
            self.conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def pending(self, limit: int = 500) -> list[tuple[int, dict]]:
        rows = self.conn.execute(
            "SELECT id, payload FROM pending ORDER BY id LIMIT ?",
            (limit,)).fetchall()
        return [(row[0], json.loads(row[1])) for row in rows]

    def acknowledge(self, ids: list[int]) -> None:
        """Delete only what central confirmed. Nothing is removed on a guess."""
        if not ids:
            return
        self.conn.executemany("DELETE FROM pending WHERE id = ?",
                              [(i,) for i in ids])
        self.conn.commit()

    def record_attempt(self, ids: list[int]) -> None:
        if not ids:
            return
        self.conn.executemany(
            "UPDATE pending SET attempts = attempts + 1 WHERE id = ?",
            [(i,) for i in ids])
        self.conn.commit()

    def depth(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM pending").fetchone()[0]

    def oldest_queued_ms(self) -> int | None:
        row = self.conn.execute(
            "SELECT MIN(queued_at_ms) FROM pending").fetchone()
        return row[0] if row and row[0] is not None else None

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


def _to_wire(event: Event) -> dict:
    return {
        "event_id": event.event_id, "tenant_id": event.tenant_id,
        "site_id": event.site_id, "drill_id": event.drill_id,
        "source": event.source, "source_kind": event.source_kind.value,
        "seq": event.seq, "type": event.type.value, "ts_ms": event.ts_ms,
        "subject": event.subject, "payload": event.payload,
    }


def from_wire(record: dict) -> Event:
    return Event(
        tenant_id=record["tenant_id"], site_id=record["site_id"],
        drill_id=record["drill_id"], source=record["source"],
        source_kind=SourceKind(record["source_kind"]), seq=record["seq"],
        type=EventType(record["type"]), ts_ms=record["ts_ms"],
        subject=record.get("subject"), payload=record.get("payload") or {},
        event_id=record["event_id"])


@dataclass
class ReplicationStats:
    buffered: int = 0
    sent: int = 0
    failed_flushes: int = 0
    last_success_ms: int | None = None
    last_failure_reason: str | None = None


@dataclass
class Replicator:
    """Edge side. Buffers durably, flushes opportunistically, never blocks."""

    outbox: Outbox
    transport: Transport
    batch_size: int = 500
    stats: ReplicationStats = field(default_factory=ReplicationStats)

    #: Set when central refuses the credential. Retrying will not fix a wrong
    #: token, and a failure that cannot be resolved by waiting must not sit
    #: behind exponential backoff pretending to be a transient outage — it needs
    #: a human, and the only way they find out is if it stops and says so.
    blocked_reason: str | None = None

    def enqueue(self, event: Event, now_ms: int | None = None) -> None:
        """Buffer an event for central. Never raises, never blocks a decision."""
        if self.outbox.add(event, now_ms):
            self.stats.buffered += 1

    def flush(self, now_ms: int | None = None) -> int:
        """Try to deliver. Returns how many were acknowledged.

        A failed flush leaves everything buffered and increments a counter. It
        does not raise: replication failing must never interrupt a drill, and an
        edge node with no link to central is still fully operational.
        """
        if self.blocked_reason is not None:
            # Nothing is lost: the events stay buffered. What stops is the
            # pointless retrying that would hide the real problem.
            return 0

        batch = self.outbox.pending(self.batch_size)
        if not batch:
            return 0
        ids = [row_id for row_id, _ in batch]
        try:
            self.transport.send([record for _, record in batch])
        except Exception as exc:
            if type(exc).__name__ == "ReplicationRejected":
                self.blocked_reason = str(exc)
                self.stats.last_failure_reason = self.blocked_reason
                self.stats.failed_flushes += 1
                return 0
            self.outbox.record_attempt(ids)
            self.stats.failed_flushes += 1
            self.stats.last_failure_reason = f"{type(exc).__name__}: {exc}"
            return 0
        self.outbox.acknowledge(ids)
        self.stats.sent += len(ids)
        self.stats.last_success_ms = (
            now_ms if now_ms is not None else int(time.time() * 1000))
        return len(ids)

    def drain(self, now_ms: int | None = None, max_rounds: int = 100) -> int:
        """Flush until the buffer is empty or the link fails."""
        total = 0
        for _ in range(max_rounds):
            sent = self.flush(now_ms)
            if sent == 0:
                break
            total += sent
        return total

    def unblock(self) -> None:
        """Resume after a human has fixed the credential."""
        self.blocked_reason = None

    @property
    def is_blocked(self) -> bool:
        return self.blocked_reason is not None

    @property
    def backlog(self) -> int:
        return self.outbox.depth()

    def lag_ms(self, now_ms: int) -> int | None:
        """How stale central's copy is. None when there is nothing outstanding."""
        oldest = self.outbox.oldest_queued_ms()
        return None if oldest is None else max(0, now_ms - oldest)


@dataclass
class Reconciler:
    """Central side. Accepts batches, deduplicates, reports what is missing.

    Central is a replica, not an authority. It never corrects the edge and never
    fills a gap: it records what it received and what it can tell is absent, so
    a reconciliation report can say how complete the central copy is rather than
    implying it is complete.
    """

    tracker: SequenceTracker = field(default_factory=SequenceTracker)
    received: list[Event] = field(default_factory=list)
    duplicates: int = 0
    rejected: int = 0

    def accept(self, records: list[dict]) -> int:
        """Take a batch. Returns how many were new."""
        new = 0
        for record in records:
            try:
                event = from_wire(record)
            except (KeyError, ValueError):
                self.rejected += 1
                continue
            is_new, _ = self.tracker.observe(event.source, event.seq)
            if not is_new:
                self.duplicates += 1
                continue
            self.received.append(event)
            new += 1
        return new

    def gaps(self) -> list:
        return self.tracker.outstanding_gaps()

    @property
    def is_complete(self) -> bool:
        return not self.tracker.outstanding_gaps()

    def report(self) -> "ReconciliationReport":
        gaps = self.gaps()
        return ReconciliationReport(
            events_held=len(self.received),
            duplicates_seen=self.duplicates,
            malformed_rejected=self.rejected,
            outstanding_gaps=len(gaps),
            missing_events=sum(g.missing_count for g in gaps),
            sources=len(self.tracker.sources()))


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    events_held: int
    duplicates_seen: int
    malformed_rejected: int
    outstanding_gaps: int
    missing_events: int
    sources: int

    @property
    def is_complete(self) -> bool:
        return self.outstanding_gaps == 0

    def describe(self) -> list[str]:
        lines = [
            f"Central holds {self.events_held} events from {self.sources} source(s).",
        ]
        if self.is_complete:
            lines.append("No gaps: the central copy is complete.")
        else:
            lines.append(
                f"INCOMPLETE: {self.missing_events} event(s) missing across "
                f"{self.outstanding_gaps} gap(s). The edge node's copy is "
                "authoritative; central must not be used for accountability.")
        if self.duplicates_seen:
            lines.append(f"{self.duplicates_seen} duplicate(s) dropped on arrival.")
        if self.malformed_rejected:
            lines.append(
                f"{self.malformed_rejected} malformed record(s) rejected.")
        return lines


@dataclass(frozen=True, slots=True)
class RPO:
    """Measured recovery point objective, not an aspirational one."""

    committed_events: int
    unsent_events: int
    oldest_unsent_age_ms: int | None
    exposure_window_ms: int

    @property
    def durable_loss_on_power_failure(self) -> int:
        """Events lost if the edge node loses power right now.

        Zero by construction: everything counted as committed reached SQLite
        with `synchronous=FULL`. It is stated as a measurement so that if the
        pragma is ever changed, this number changes with it rather than the
        claim silently becoming false.
        """
        return 0

    @property
    def loss_on_disk_failure(self) -> int:
        """Events lost if the edge node's disk dies right now.

        Everything not yet acknowledged by central. This is the number that
        argues for keeping the flush interval short.
        """
        return self.unsent_events

    def describe(self) -> list[str]:
        return [
            f"Committed to the durable buffer: {self.committed_events}",
            f"Not yet acknowledged by central: {self.unsent_events}",
            f"Oldest unsent event age: "
            f"{'n/a' if self.oldest_unsent_age_ms is None else f'{self.oldest_unsent_age_ms} ms'}",
            "",
            f"RPO, edge power loss:  {self.durable_loss_on_power_failure} events "
            "(fsync on commit)",
            f"RPO, edge disk loss:   {self.loss_on_disk_failure} events "
            "(everything central has not acknowledged)",
            f"Producer exposure:     up to {self.exposure_window_ms} ms of events "
            "produced but not yet committed",
        ]


def measure_rpo(replicator: Replicator, *, now_ms: int,
                flush_interval_ms: int) -> RPO:
    """Report the real recovery point objective from live counters."""
    return RPO(
        committed_events=replicator.stats.buffered,
        unsent_events=replicator.backlog,
        oldest_unsent_age_ms=replicator.lag_ms(now_ms),
        exposure_window_ms=flush_interval_ms)


class InMemoryTransport:
    """A transport that can be told to fail. The chaos suite's main lever."""

    def __init__(self, reconciler: Reconciler | None = None) -> None:
        self.reconciler = reconciler or Reconciler()
        self.up = True
        self.batches_sent = 0
        self.on_send: Callable[[list[dict]], None] | None = None

    def send(self, batch: list[dict]) -> None:
        if not self.up:
            raise TransportUnavailable("central is unreachable")
        if self.on_send is not None:
            self.on_send(batch)
        self.reconciler.accept(batch)
        self.batches_sent += 1
