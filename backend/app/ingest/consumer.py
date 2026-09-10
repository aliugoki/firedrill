"""The Redis Streams consumer: the last link between the pipeline and the core.

The pipeline publishes to `vt:evac:events:<tenant>`. This drains it into the
fold. Everything interesting here is about what happens when it stops working,
because it will.

**Acknowledge after folding, never before.** A crash between reading a message
and folding it must redeliver, not lose. Since `Ingestor.feed` is idempotent on
`(source, seq)`, at-least-once delivery is safe and at-most-once is not, so the
acknowledgement goes last. This pairing is the whole reason the fold was built
idempotent in the first place.

**Claim what a dead consumer stranded.** A process that dies holding fifty
unacknowledged messages leaves them in Redis's pending list forever. Nothing
reclaims them by default, and the events simply never arrive — silently, which
is the worst way for evidence to go missing. `claim_stale` exists for that.

**A Redis outage is a health event, not an exception.** It blinds the system,
and the board must say so rather than showing a calm, frozen picture. The
consumer reports it and keeps trying.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Protocol

from app.core.events import Event, EventType, SourceKind
from app.ingest.health import Component
from app.ingest.ingestor import Ingestor


class StreamClient(Protocol):
    """The slice of Redis this needs. Small on purpose: a smaller surface is a
    smaller fake, and a smaller fake is a more honest test."""

    def create_group(self, stream: str, group: str) -> None: ...
    def read_group(self, stream: str, group: str, consumer: str,
                   count: int, block_ms: int) -> list: ...
    def acknowledge(self, stream: str, group: str, message_ids: list) -> int: ...
    def claim_stale(self, stream: str, group: str, consumer: str,
                    min_idle_ms: int, count: int) -> list: ...


class StreamUnavailable(Exception):
    """Redis is unreachable. Blinding, and reported rather than raised onward."""


@dataclass
class ConsumerStats:
    messages_read: int = 0
    events_applied: int = 0
    duplicates: int = 0
    malformed: int = 0
    claimed: int = 0
    acknowledged: int = 0
    outages: int = 0
    last_error: str | None = None

    @property
    def unparseable_fraction(self) -> float:
        """How much of the stream this consumer could not understand.

        Worth watching: a pipeline that starts emitting a shape this does not
        know produces a stream that looks healthy and carries nothing.
        """
        total = self.messages_read
        return (self.malformed / total) if total else 0.0


@dataclass
class EventConsumer:
    """Drains one tenant's stream into one ingestor."""

    ingestor: Ingestor
    client: StreamClient
    stream: str
    group: str = "evac-edge"
    consumer_name: str = "edge-1"
    batch_size: int = 200
    block_ms: int = 2_000
    stale_after_ms: int = 60_000
    stats: ConsumerStats = field(default_factory=ConsumerStats)
    _degraded: bool = False

    def ensure_group(self) -> bool:
        """Create the consumer group. Returns False if Redis is unreachable."""
        try:
            self.client.create_group(self.stream, self.group)
            return True
        except StreamUnavailable as exc:
            self._mark_degraded(str(exc))
            return False
        except Exception:
            # Already exists is the common case and is not an error.
            return True

    def poll_once(self, now_ms: int) -> int:
        """One read-fold-acknowledge cycle. Returns events applied."""
        try:
            messages = self.client.read_group(
                self.stream, self.group, self.consumer_name,
                self.batch_size, self.block_ms)
        except StreamUnavailable as exc:
            self._mark_degraded(str(exc), now_ms)
            return 0
        except Exception as exc:
            self._mark_degraded(f"{type(exc).__name__}: {exc}", now_ms)
            return 0

        self._mark_recovered(now_ms)
        return self._process(messages, now_ms)

    def claim_stale(self, now_ms: int) -> int:
        """Take over messages a dead consumer left pending.

        Without this they sit in Redis forever and the events never arrive —
        silently, which is the worst way for evidence to go missing.
        """
        try:
            messages = self.client.claim_stale(
                self.stream, self.group, self.consumer_name,
                self.stale_after_ms, self.batch_size)
        except StreamUnavailable as exc:
            self._mark_degraded(str(exc), now_ms)
            return 0
        except Exception:
            return 0

        if messages:
            self.stats.claimed += len(messages)
        return self._process(messages, now_ms)

    def _process(self, messages: list, now_ms: int) -> int:
        applied = 0
        acknowledge: list[str] = []

        for message_id, fields in messages:
            self.stats.messages_read += 1
            event = parse(fields)
            if event is None:
                self.stats.malformed += 1
                # Acknowledged anyway. A message this consumer cannot parse
                # will never become parseable, and leaving it pending forever
                # would make every future claim_stale drag it along.
                acknowledge.append(message_id)
                continue

            was_new = self.ingestor.feed(event)
            if was_new:
                applied += 1
                self.stats.events_applied += 1
            else:
                self.stats.duplicates += 1
            # Only now. A crash before this line redelivers; a crash after it
            # would have lost the event.
            acknowledge.append(message_id)

        if acknowledge:
            try:
                self.stats.acknowledged += self.client.acknowledge(
                    self.stream, self.group, acknowledge)
            except Exception as exc:
                # The fold already happened and is idempotent, so redelivery is
                # harmless. Losing the acknowledgement is not worth failing over.
                self.stats.last_error = f"acknowledge failed: {exc}"

        return applied

    def _mark_degraded(self, reason: str, now_ms: int | None = None) -> None:
        self.stats.last_error = reason
        if self._degraded:
            return
        self._degraded = True
        self.stats.outages += 1
        self.ingestor.state.health.degrade(
            Component.EVENT_BUS, self.stream,
            now_ms if now_ms is not None else 0, reason)

    def _mark_recovered(self, now_ms: int) -> None:
        if not self._degraded:
            return
        self._degraded = False
        self.ingestor.state.health.recover(
            Component.EVENT_BUS, self.stream, now_ms)

    @property
    def is_degraded(self) -> bool:
        return self._degraded


def parse(fields: dict) -> Event | None:
    """Turn a Redis Stream entry into an Event, or None if it cannot be trusted.

    Returns None rather than raising. A malformed message is one bad producer,
    not a reason to stop draining a stream during an evacuation.
    """
    try:
        payload = fields.get("payload")
        if isinstance(payload, (str, bytes)):
            payload = json.loads(payload)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return None

        subject = fields.get("subject")
        # Redis stores everything as a string, so an absent subject arrives as
        # the four characters "null" rather than as nothing.
        if subject in (None, "", "null", "None"):
            subject = None

        built = {
            "tenant_id": str(fields["tenant_id"]),
            "site_id": str(fields["site_id"]),
            "drill_id": str(fields["drill_id"]),
            "source": str(fields["source"]),
            "source_kind": SourceKind(fields["source_kind"]),
            "seq": int(fields["seq"]),
            "type": EventType(fields["type"]),
            "ts_ms": int(fields["ts_ms"]),
            "subject": str(subject) if subject is not None else None,
            "payload": payload,
        }
        # Preserve the producer's event id when it sent one, so the same
        # observation carries one identity from the probe to the ledger.
        event_id = fields.get("event_id")
        if event_id:
            built["event_id"] = str(event_id)

        return Event(**built)
    except (KeyError, ValueError, TypeError, json.JSONDecodeError):
        return None


class RedisStreamClient:
    """The real client. A thin adapter, so the logic above stays testable.

    `redis` is imported lazily: an edge node running from the simulator or the
    API has no need of it, and a hard import would make the whole system refuse
    to start without a package it may never use.
    """

    def __init__(self, url: str) -> None:
        self.url = url
        self._redis = None

    @property
    def redis(self):
        if self._redis is None:
            try:
                import redis as redis_module
            except ImportError as exc:  # pragma: no cover - environment
                raise StreamUnavailable(
                    "the redis package is not installed") from exc
            self._redis = redis_module.from_url(
                self.url, decode_responses=True,
                socket_timeout=5, socket_connect_timeout=2)
        return self._redis

    def _wrap(self, call, *args, **kwargs):
        try:
            return call(*args, **kwargs)
        except Exception as exc:
            name = type(exc).__name__
            if "Connection" in name or "Timeout" in name:
                raise StreamUnavailable(f"{name}: {exc}") from exc
            raise

    def create_group(self, stream: str, group: str) -> None:
        # `mkstream` so a consumer can start before the pipeline has published
        # anything, which is the normal order on a cold edge node.
        self._wrap(self.redis.xgroup_create, stream, group, id="0",
                   mkstream=True)

    def read_group(self, stream: str, group: str, consumer: str,
                   count: int, block_ms: int) -> list:
        response = self._wrap(
            self.redis.xreadgroup, group, consumer, {stream: ">"},
            count=count, block=block_ms)
        if not response:
            return []
        return [(message_id, fields) for _, entries in response
                for message_id, fields in entries]

    def acknowledge(self, stream: str, group: str, message_ids: list) -> int:
        if not message_ids:
            return 0
        return self._wrap(self.redis.xack, stream, group, *message_ids)

    def claim_stale(self, stream: str, group: str, consumer: str,
                    min_idle_ms: int, count: int) -> list:
        result = self._wrap(
            self.redis.xautoclaim, stream, group, consumer,
            min_idle_time=min_idle_ms, count=count)
        # xautoclaim returns (next_cursor, entries, deleted) on Redis >= 7.
        entries = result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else []
        return list(entries)
