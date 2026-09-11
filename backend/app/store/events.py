"""Persisting and replaying the event log.

Two properties matter, and the second is easy to get wrong.

**Appending is idempotent**, enforced by a unique constraint rather than by the
application checking first. A check-then-insert races with itself under
concurrent consumers; the constraint does not.

**Losing the database does not stop the drill.** Phase 3 established that a
Postgres outage is *not* blinding: the core holds its state in memory and the
projections are recomputable. So `append` reports a failure and returns rather
than raising, the health log records a non-blinding outage, and events that
could not be written are buffered in memory until it comes back.

That buffer is the honest part. It is bounded, and when it fills the store says
so instead of quietly discarding the oldest — a full buffer is a real risk to
the record, and a silent one would be worse.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import sqlalchemy as sa

from app.core.events import Event, EventType, SourceKind
from app.ingest.health import Component, HealthLog
from app.store.schema import evac_events


#: How many events to hold in memory while the database is unreachable. Sized so
#: a long outage during a large drill still fits: a 500-person drill produces
#: tens of thousands of events, and losing the tail of the record is the thing
#: this exists to prevent.
DEFAULT_BUFFER_LIMIT = 100_000


@dataclass
class StoreStats:
    written: int = 0
    duplicates: int = 0
    buffered: int = 0
    dropped: int = 0
    flushes: int = 0
    failures: int = 0
    last_error: str | None = None

    @property
    def buffer_overflowed(self) -> bool:
        return self.dropped > 0


@dataclass
class EventStore:
    """Writes events to Postgres, and survives Postgres not being there."""

    engine: sa.Engine
    health: HealthLog | None = None
    buffer_limit: int = DEFAULT_BUFFER_LIMIT
    stats: StoreStats = field(default_factory=StoreStats)
    _pending: list = field(default_factory=list)
    _degraded: bool = False

    # -- writing ---------------------------------------------------------------

    def append(self, event: Event, now_ms: int | None = None) -> bool:
        """Persist one event. Returns False if it was already stored.

        Never raises on a database failure. The event is buffered and the caller
        continues, because the alternative is a drill that stops because a
        replica failed over.
        """
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        row = _to_row(event, now_ms)

        try:
            with self.engine.begin() as connection:
                connection.execute(sa.insert(evac_events).values(**row))
        except sa.exc.IntegrityError as exc:
            if _is_duplicate(exc):
                # The unique constraint did its job. A redelivered event is not
                # a problem, it is the design working.
                self.stats.duplicates += 1
                return False
            # Any other integrity failure is a schema or data problem, and
            # counting it as a duplicate would discard the event silently. It is
            # buffered like any other write failure so nothing is lost while
            # somebody works out what is wrong.
            self._buffer(row, exc, now_ms)
            return True
        except Exception as exc:
            self._buffer(row, exc, now_ms)
            return True

        self._recovered(now_ms)
        self.stats.written += 1
        return True

    def append_many(self, events: list, now_ms: int | None = None) -> int:
        return sum(1 for event in events if self.append(event, now_ms))

    def _buffer(self, row: dict, exc: Exception, now_ms: int) -> None:
        self.stats.failures += 1
        self.stats.last_error = f"{type(exc).__name__}: {exc}"
        self._degrade(now_ms)

        if len(self._pending) >= self.buffer_limit:
            # Say so rather than quietly discarding the oldest. A full buffer is
            # a real risk to the record, and a silent one would be worse.
            self.stats.dropped += 1
            return
        self._pending.append(row)
        self.stats.buffered = len(self._pending)

    def flush(self, now_ms: int | None = None) -> int:
        """Try to write what the outage held back. Returns how many landed."""
        if not self._pending:
            self._recovered(now_ms or 0)
            return 0

        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        written = 0
        remaining = []

        for row in self._pending:
            try:
                with self.engine.begin() as connection:
                    connection.execute(sa.insert(evac_events).values(**row))
            except sa.exc.IntegrityError as exc:
                if _is_duplicate(exc):
                    self.stats.duplicates += 1
                    continue
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                remaining.append(row)
                continue
            except Exception as exc:
                self.stats.last_error = f"{type(exc).__name__}: {exc}"
                # Keep it and try the next. Physical insert order does not
                # matter -- replay reads a drill ordered by `ts_ms` and `id`,
                # not by arrival -- so a row that fails while a later one
                # succeeds costs nothing but a retry.
                remaining.append(row)
                continue
            written += 1
            self.stats.written += 1

        self._pending = remaining
        self.stats.buffered = len(self._pending)
        self.stats.flushes += 1
        if not remaining:
            self._recovered(now_ms)
        return written

    # -- reading ---------------------------------------------------------------

    def replay(self, drill_id: str, *, after_id: int = 0,
               limit: int | None = None) -> list:
        """Read a drill's events back, in the order they happened.

        Ordered by `(ts_ms, id)` rather than by insertion. Late arrivals are
        written after earlier events but happened before them, and replaying in
        insertion order would let a later sighting be overwritten by an earlier
        one — the same reason `feed_batch` sorts.
        """
        query = (sa.select(evac_events)
                 .where(evac_events.c.drill_id == drill_id)
                 .where(evac_events.c.id > after_id)
                 .order_by(evac_events.c.ts_ms, evac_events.c.id))
        if limit:
            query = query.limit(limit)

        with self.engine.connect() as connection:
            return [_from_row(row._mapping)
                    for row in connection.execute(query)]

    def count(self, drill_id: str) -> int:
        query = (sa.select(sa.func.count())
                 .select_from(evac_events)
                 .where(evac_events.c.drill_id == drill_id))
        with self.engine.connect() as connection:
            return connection.execute(query).scalar_one()

    def highest_seq(self, drill_id: str, source: str) -> int | None:
        """The last sequence number stored for a source.

        Lets a restarted consumer tell a genuine gap from events it already has,
        without replaying the whole drill to find out.
        """
        query = (sa.select(sa.func.max(evac_events.c.seq))
                 .where(evac_events.c.drill_id == drill_id)
                 .where(evac_events.c.source == source))
        with self.engine.connect() as connection:
            return connection.execute(query).scalar()

    # -- health ----------------------------------------------------------------

    @property
    def is_degraded(self) -> bool:
        return self._degraded

    @property
    def pending(self) -> int:
        return len(self._pending)

    def _degrade(self, now_ms: int) -> None:
        if self._degraded:
            return
        self._degraded = True
        if self.health is not None:
            # DATABASE, not PIPELINE. Losing storage costs durability, not
            # sight, and reporting it as blinding would make the system blind
            # itself over something it does not need to see through.
            self.health.degrade(Component.DATABASE, "events", now_ms,
                                self.stats.last_error or "unreachable")

    def _recovered(self, now_ms: int) -> None:
        if not self._degraded:
            return
        self._degraded = False
        if self.health is not None:
            self.health.recover(Component.DATABASE, "events", now_ms)


def _is_duplicate(exc: sa.exc.IntegrityError) -> bool:
    """Whether an integrity failure is the uniqueness constraint doing its job.

    Worth distinguishing. Treating every integrity error as a duplicate means a
    NOT NULL violation or a type mismatch is counted as a redelivery and the
    event vanishes — which is exactly how a schema bug becomes missing evidence
    that nobody notices.
    """
    text = str(exc.orig if exc.orig is not None else exc).lower()
    return ("unique" in text or "duplicate key" in text)


def _to_row(event: Event, now_ms: int) -> dict:
    return {
        "event_id": event.event_id,
        "tenant_id": event.tenant_id,
        "site_id": event.site_id,
        "drill_id": event.drill_id,
        "source": event.source,
        "source_kind": event.source_kind.value,
        "seq": event.seq,
        "type": event.type.value,
        "ts_ms": event.ts_ms,
        "subject": event.subject,
        "payload": event.payload,
        "ingested_at_ms": now_ms,
    }


def _from_row(row) -> Event:
    return Event(
        tenant_id=row["tenant_id"], site_id=row["site_id"],
        drill_id=row["drill_id"], source=row["source"],
        source_kind=SourceKind(row["source_kind"]), seq=row["seq"],
        type=EventType(row["type"]), ts_ms=row["ts_ms"],
        subject=row["subject"], payload=row["payload"] or {},
        event_id=row["event_id"], ingested_at_ms=row["ingested_at_ms"])


def rebuild(store: EventStore, drill_id: str, ingestor) -> int:
    """Reconstruct a drill's state from the stored log.

    This is what makes the rest of the system's in-memory state acceptable: a
    node that restarts mid-drill reads the events back and arrives at exactly
    the state it had, because the fold is deterministic and idempotent.
    """
    events = store.replay(drill_id)
    return ingestor.feed_batch(events)
