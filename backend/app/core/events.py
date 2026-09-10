"""Event schema, validation, and sequence tracking.

Everything EVAC-120 knows arrives as an event, and every read model is a
projection over the resulting stream. An event is immutable, carries a
monotonic per-source sequence number, and is idempotent on ``(source, seq)``:
replaying a stream from any point converges to the same state.

Two rules shape the design.

**Invariant 6 — every decision is reconstructable.** Nothing is stored as a
computed conclusion without the observations behind it. `explain(subject)` in
`ledger.py` walks this stream.

**Invariant 1 — absence of evidence is not evidence of absence.** There is no
event type meaning "this person is missing". There are events meaning a face was
unavailable, a camera failed, a track was lost, and a person is unaccounted
*for*, which is a statement about the evidence rather than about the person.

Time is handled carefully. Camera events carry ``pts_ms``, the frame
presentation timestamp, which starts at 0 for the first frame of a stream. Every
other source carries wall clock. `normalise_timestamp` converts a source-local
PTS into epoch milliseconds against the stream's declared origin, which is what
stops a zero-valued PTS from being mistaken for the epoch, and what avoids the
falsy-zero defect recorded in docs/EVAC120_PROVENANCE.md.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Iterator


class EventType(str, Enum):
    """The complete event vocabulary. Nothing outside this set is ingestible."""

    # -- Drill lifecycle -------------------------------------------------------
    DRILL_CREATED = "DRILL_CREATED"
    DRILL_STARTED = "DRILL_STARTED"
    ALARM_ACTIVATED = "ALARM_ACTIVATED"
    DRILL_COMPLETED = "DRILL_COMPLETED"

    # -- Detection and tracking ------------------------------------------------
    PERSON_DETECTED = "PERSON_DETECTED"
    TRACK_CREATED = "TRACK_CREATED"
    TRACK_UPDATED = "TRACK_UPDATED"
    TRACK_LOST = "TRACK_LOST"

    # -- Face and identity -----------------------------------------------------
    FACE_OBSERVED = "FACE_OBSERVED"
    FACE_UNAVAILABLE = "FACE_UNAVAILABLE"
    IDENTITY_CANDIDATE = "IDENTITY_CANDIDATE"
    IDENTITY_CONFIRMED = "IDENTITY_CONFIRMED"
    IDENTITY_RECONFIRMED = "IDENTITY_RECONFIRMED"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"

    # -- Movement --------------------------------------------------------------
    PERSON_EXITED_BUILDING = "PERSON_EXITED_BUILDING"
    PERSON_ENTERED_ASSEMBLY = "PERSON_ENTERED_ASSEMBLY"
    PERSON_LEFT_ASSEMBLY = "PERSON_LEFT_ASSEMBLY"

    # -- Accountability outcomes -----------------------------------------------
    PERSON_ACCOUNTED = "PERSON_ACCOUNTED"
    PERSON_UNCERTAIN = "PERSON_UNCERTAIN"
    PERSON_UNACCOUNTED = "PERSON_UNACCOUNTED"

    # -- Warden (human evidence, invariant 9) ----------------------------------
    WARDEN_CONFIRMED = "WARDEN_CONFIRMED"
    WARDEN_REJECTED = "WARDEN_REJECTED"
    WARDEN_SWEEP_COMPLETE = "WARDEN_SWEEP_COMPLETE"
    WARDEN_NOTE = "WARDEN_NOTE"

    # -- System health (invariant 8) -------------------------------------------
    CAMERA_FAILURE = "CAMERA_FAILURE"
    CAMERA_RECOVERED = "CAMERA_RECOVERED"
    SYSTEM_DEGRADED = "SYSTEM_DEGRADED"
    SYSTEM_RECOVERED = "SYSTEM_RECOVERED"
    SEQUENCE_GAP = "SEQUENCE_GAP"


class SourceKind(str, Enum):
    """Where an event came from. Determines how its timestamp is read."""

    CAMERA = "camera"      # pts_ms is a frame presentation timestamp
    EDGE = "edge"          # pts_ms is wall clock, epoch milliseconds
    WARDEN = "warden"      # pts_ms is wall clock, epoch milliseconds
    SYSTEM = "system"      # pts_ms is wall clock, epoch milliseconds


#: Events a camera source is allowed to emit. A warden device claiming to have
#: observed a face, or a camera claiming a warden confirmation, is rejected at
#: ingest rather than trusted.
_ALLOWED_BY_SOURCE: dict[SourceKind, frozenset[EventType]] = {
    SourceKind.CAMERA: frozenset({
        EventType.PERSON_DETECTED, EventType.TRACK_CREATED,
        EventType.TRACK_UPDATED, EventType.TRACK_LOST,
        EventType.FACE_OBSERVED, EventType.FACE_UNAVAILABLE,
        EventType.IDENTITY_CANDIDATE, EventType.IDENTITY_CONFIRMED,
        EventType.IDENTITY_RECONFIRMED, EventType.IDENTITY_CONFLICT,
        EventType.PERSON_EXITED_BUILDING, EventType.PERSON_ENTERED_ASSEMBLY,
        EventType.PERSON_LEFT_ASSEMBLY,
    }),
    SourceKind.WARDEN: frozenset({
        EventType.WARDEN_CONFIRMED, EventType.WARDEN_REJECTED,
        EventType.WARDEN_SWEEP_COMPLETE, EventType.WARDEN_NOTE,
    }),
    SourceKind.EDGE: frozenset({
        EventType.PERSON_ACCOUNTED, EventType.PERSON_UNCERTAIN,
        EventType.PERSON_UNACCOUNTED, EventType.PERSON_EXITED_BUILDING,
        EventType.PERSON_ENTERED_ASSEMBLY, EventType.PERSON_LEFT_ASSEMBLY,
        EventType.IDENTITY_CONFIRMED, EventType.IDENTITY_RECONFIRMED,
        EventType.IDENTITY_CONFLICT, EventType.SEQUENCE_GAP,
    }),
    SourceKind.SYSTEM: frozenset({
        EventType.DRILL_CREATED, EventType.DRILL_STARTED,
        EventType.ALARM_ACTIVATED, EventType.DRILL_COMPLETED,
        EventType.CAMERA_FAILURE, EventType.CAMERA_RECOVERED,
        EventType.SYSTEM_DEGRADED, EventType.SYSTEM_RECOVERED,
        EventType.SEQUENCE_GAP,
    }),
}


class EventValidationError(ValueError):
    """An event that cannot be trusted. Rejected at the boundary, never stored."""


@dataclass(frozen=True, slots=True)
class Event:
    """One immutable observation.

    ``seq`` is monotonic per ``source`` and is what makes ingest idempotent and
    gaps detectable. ``ts_ms`` is always epoch milliseconds: a camera's raw
    frame PTS is converted by `normalise_timestamp` before an Event exists, so
    nothing downstream has to know which kind of clock produced it.
    """

    tenant_id: str
    site_id: str
    drill_id: str
    source: str
    source_kind: SourceKind
    seq: int
    type: EventType
    ts_ms: int
    subject: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ingested_at_ms: int | None = None

    @property
    def dedupe_key(self) -> tuple[str, int]:
        """Ingest is idempotent on this. Two events sharing it are one event."""
        return (self.source, self.seq)


def normalise_timestamp(
    *, source_kind: SourceKind, raw_ms: int, stream_origin_ms: int | None = None
) -> int:
    """Convert a source-local timestamp into epoch milliseconds.

    A camera's ``pts_ms`` is relative to the start of its stream and begins at
    0, so it needs the stream's wall-clock origin to become meaningful. Every
    other source already speaks epoch milliseconds.

    Raising on a missing origin is deliberate. Silently treating a PTS of 0 as
    the Unix epoch would place a live drill in 1970 and make every elapsed-time
    calculation in `timing.py` meaningless, and a zero timestamp is exactly what
    triggers the falsy-zero defect recorded in docs/EVAC120_PROVENANCE.md.
    """
    if raw_ms < 0:
        raise EventValidationError(f"timestamp must not be negative, got {raw_ms}")

    if source_kind is not SourceKind.CAMERA:
        return raw_ms

    if stream_origin_ms is None:
        raise EventValidationError(
            "a camera event needs stream_origin_ms: its pts_ms is relative to "
            "the start of the stream and starts at 0"
        )
    if stream_origin_ms < 0:
        raise EventValidationError(
            f"stream_origin_ms must not be negative, got {stream_origin_ms}"
        )
    return stream_origin_ms + raw_ms


def validate(event: Event) -> Event:
    """Return the event, or raise `EventValidationError`.

    Checks identity of the event rather than plausibility of its contents: an
    event that passes here is well-formed and came from a source entitled to
    emit it. Whether its claim is *true* is the FSMs' problem, not this one's.
    """
    for name in ("tenant_id", "site_id", "drill_id", "source"):
        value = getattr(event, name)
        if not isinstance(value, str) or not value.strip():
            raise EventValidationError(f"{name} must be a non-empty string")

    if not isinstance(event.type, EventType):
        raise EventValidationError(f"unknown event type: {event.type!r}")

    if not isinstance(event.source_kind, SourceKind):
        raise EventValidationError(f"unknown source kind: {event.source_kind!r}")

    if not isinstance(event.seq, int) or isinstance(event.seq, bool) or event.seq < 0:
        raise EventValidationError(f"seq must be a non-negative int, got {event.seq!r}")

    if not isinstance(event.ts_ms, int) or event.ts_ms < 0:
        raise EventValidationError(
            f"ts_ms must be non-negative epoch milliseconds, got {event.ts_ms!r}"
        )

    allowed = _ALLOWED_BY_SOURCE[event.source_kind]
    if event.type not in allowed:
        raise EventValidationError(
            f"a {event.source_kind.value} source may not emit {event.type.value}"
        )

    if not isinstance(event.payload, dict):
        raise EventValidationError("payload must be a dict")

    if event.type in _SUBJECT_REQUIRED and not event.subject:
        raise EventValidationError(f"{event.type.value} requires a subject")

    return event


#: Event types that are meaningless without something to be about.
_SUBJECT_REQUIRED: frozenset[EventType] = frozenset({
    EventType.PERSON_DETECTED, EventType.TRACK_CREATED, EventType.TRACK_UPDATED,
    EventType.TRACK_LOST, EventType.FACE_OBSERVED, EventType.FACE_UNAVAILABLE,
    EventType.IDENTITY_CANDIDATE, EventType.IDENTITY_CONFIRMED,
    EventType.IDENTITY_RECONFIRMED, EventType.IDENTITY_CONFLICT,
    EventType.PERSON_EXITED_BUILDING, EventType.PERSON_ENTERED_ASSEMBLY,
    EventType.PERSON_LEFT_ASSEMBLY, EventType.PERSON_ACCOUNTED,
    EventType.PERSON_UNCERTAIN, EventType.PERSON_UNACCOUNTED,
    EventType.WARDEN_CONFIRMED, EventType.WARDEN_REJECTED,
    EventType.CAMERA_FAILURE, EventType.CAMERA_RECOVERED,
})


@dataclass(frozen=True, slots=True)
class Gap:
    """A hole in one source's sequence. Becomes a `SEQUENCE_GAP` event."""

    source: str
    after_seq: int
    before_seq: int

    @property
    def missing_count(self) -> int:
        return self.before_seq - self.after_seq - 1


class SequenceTracker:
    """Per-source sequence bookkeeping: deduplication and gap detection.

    A stream can deliver an event twice, out of order, or not at all. This
    distinguishes those cases so ingest can be idempotent without pretending a
    lost event never existed.

    A gap is **reported, never repaired**. Invariant 8: missing evidence
    degrades the system's confidence rather than being quietly interpolated.
    """

    def __init__(self) -> None:
        self._highest: dict[str, int] = {}
        self._seen: dict[str, set[int]] = {}
        self._gaps: list[Gap] = []

    def observe(self, source: str, seq: int) -> tuple[bool, Gap | None]:
        """Record one arrival.

        Returns ``(is_new, gap)``. ``is_new`` is False for a duplicate, which
        the caller must drop. ``gap`` is set when this arrival jumped over
        sequence numbers that have not been seen.
        """
        seen = self._seen.setdefault(source, set())
        if seq in seen:
            return False, None
        seen.add(seq)

        gap: Gap | None = None
        highest = self._highest.get(source)
        if highest is None:
            self._highest[source] = seq
        elif seq > highest + 1:
            gap = Gap(source=source, after_seq=highest, before_seq=seq)
            self._gaps.append(gap)
            self._highest[source] = seq
        elif seq > highest:
            self._highest[source] = seq
        # seq < highest is a late arrival, not a gap. If it fills a previously
        # reported hole, `outstanding_gaps` stops counting it.

        return True, gap

    def highest_seq(self, source: str) -> int | None:
        return self._highest.get(source)

    def has_seen(self, source: str, seq: int) -> bool:
        return seq in self._seen.get(source, ())

    def outstanding_gaps(self) -> list[Gap]:
        """Gaps whose missing sequence numbers have still not arrived.

        A late event that fills a hole removes it from this list, so a
        reordered stream stops reporting degradation once it has caught up.
        """
        out = []
        for gap in self._gaps:
            seen = self._seen.get(gap.source, set())
            if any(s not in seen for s in range(gap.after_seq + 1, gap.before_seq)):
                out.append(gap)
        return out

    def sources(self) -> list[str]:
        return sorted(self._highest)


def deduplicate(events: Iterable[Event]) -> Iterator[Event]:
    """Yield events once each, in arrival order, dropping repeats.

    Convenience over `SequenceTracker` for callers that only care about
    idempotency and handle gaps elsewhere.
    """
    tracker = SequenceTracker()
    for event in events:
        is_new, _ = tracker.observe(event.source, event.seq)
        if is_new:
            yield event
