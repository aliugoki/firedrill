"""The Redis Streams consumer.

The fake below implements pending-entry semantics, because those are the part
that matters: a message read and not acknowledged must be redeliverable, and a
consumer that dies holding messages must not strand them.
"""

from __future__ import annotations

import json

import pytest

from app.core.events import EventType
from app.ingest.consumer import (
    EventConsumer,
    StreamUnavailable,
    parse,
)
from app.ingest.health import Component
from app.ingest.ingestor import Ingestor

T0 = 1_788_000_000_000


def entry(seq: int, subject="gp-1", event_type="TRACK_UPDATED", **overrides):
    fields = {
        "tenant_id": "t", "site_id": "s", "drill_id": "d", "source": "cam-1",
        "source_kind": "camera", "seq": str(seq), "type": event_type,
        "ts_ms": str(T0 + seq * 100), "subject": subject,
        "payload": json.dumps({"zone_id": "floor-1", "zone_kind": "FLOOR",
                               "camera_id": "cam-1"}),
    }
    fields.update(overrides)
    return fields


class FakeStream:
    """Enough Redis to be worth testing against: groups, pending entries and
    claims, plus a switch to take it offline."""

    def __init__(self):
        self.entries = []          # (id, fields)
        self.delivered = {}        # id -> consumer holding it, unacknowledged
        self.acknowledged = set()
        self.up = True
        self._next = 0
        self.groups = set()

    def publish(self, fields):
        self._next += 1
        message_id = f"1-{self._next}"
        self.entries.append((message_id, fields))
        return message_id

    def _check(self):
        if not self.up:
            raise StreamUnavailable("redis is unreachable")

    def create_group(self, stream, group):
        self._check()
        if group in self.groups:
            raise RuntimeError("BUSYGROUP")
        self.groups.add(group)

    def read_group(self, stream, group, consumer, count, block_ms):
        self._check()
        out = []
        for message_id, fields in self.entries:
            if message_id in self.acknowledged or message_id in self.delivered:
                continue
            self.delivered[message_id] = consumer
            out.append((message_id, fields))
            if len(out) >= count:
                break
        return out

    def acknowledge(self, stream, group, message_ids):
        self._check()
        count = 0
        for message_id in message_ids:
            if message_id in self.delivered:
                del self.delivered[message_id]
                self.acknowledged.add(message_id)
                count += 1
        return count

    def claim_stale(self, stream, group, consumer, min_idle_ms, count):
        """Everything another consumer is still holding."""
        self._check()
        stranded = [mid for mid, holder in self.delivered.items()
                    if holder != consumer]
        out = []
        for message_id in stranded[:count]:
            self.delivered[message_id] = consumer
            fields = next(f for mid, f in self.entries if mid == message_id)
            out.append((message_id, fields))
        return out

    @property
    def pending(self):
        return len(self.delivered)


@pytest.fixture
def stream() -> FakeStream:
    return FakeStream()


@pytest.fixture
def consumer(stream) -> EventConsumer:
    return EventConsumer(ingestor=Ingestor(), client=stream,
                         stream="vt:evac:events:t")


class TestParsing:
    def test_a_well_formed_entry_becomes_an_event(self):
        event = parse(entry(1))
        assert event.type is EventType.TRACK_UPDATED
        assert event.seq == 1
        assert event.payload["zone_id"] == "floor-1"

    def test_the_producers_event_id_is_preserved(self):
        # So one observation carries one identity from the probe to the ledger.
        assert parse(entry(1, event_id="abc-123")).event_id == "abc-123"

    def test_an_absent_subject_arrives_as_the_string_null(self):
        # Redis stores everything as a string.
        assert parse(entry(1, subject="null")).subject is None
        assert parse(entry(1, subject="")).subject is None

    @pytest.mark.parametrize("field,value", [
        ("seq", "not a number"),
        ("type", "NONSENSE"),
        ("source_kind", "telepathy"),
        ("payload", "{not json"),
        ("ts_ms", "yesterday"),
    ])
    def test_a_malformed_entry_is_none_rather_than_an_exception(self, field, value):
        # One bad producer is not a reason to stop draining a stream during an
        # evacuation.
        fields = entry(1)
        fields[field] = value
        assert parse(fields) is None

    def test_a_missing_required_field_is_none(self):
        fields = entry(1)
        del fields["drill_id"]
        assert parse(fields) is None

    def test_a_payload_that_is_not_an_object_is_refused(self):
        assert parse(entry(1, payload=json.dumps([1, 2, 3]))) is None


class TestDraining:
    def test_it_folds_what_it_reads(self, consumer, stream):
        for seq in range(1, 6):
            stream.publish(entry(seq))
        assert consumer.poll_once(T0) == 5
        assert consumer.stats.events_applied == 5

    def test_it_acknowledges_only_after_folding(self, consumer, stream):
        # A crash between reading and folding must redeliver, not lose. Since
        # the fold is idempotent, at-least-once is safe and at-most-once is not.
        for seq in range(1, 4):
            stream.publish(entry(seq))

        folded = []
        original = consumer.ingestor.feed

        def watch(event):
            folded.append(event.seq)
            # Nothing may be acknowledged while a fold is still in flight.
            assert stream.acknowledged == set()
            return original(event)

        consumer.ingestor.feed = watch
        consumer.poll_once(T0)
        assert folded == [1, 2, 3]
        assert len(stream.acknowledged) == 3

    def test_redelivery_is_harmless(self, consumer, stream):
        for seq in range(1, 4):
            stream.publish(entry(seq))
        consumer.poll_once(T0)
        # Simulate the acknowledgement being lost and the messages coming back.
        stream.acknowledged.clear()
        stream.delivered.clear()
        consumer.poll_once(T0)
        assert consumer.stats.duplicates == 3
        assert consumer.stats.events_applied == 3

    def test_a_malformed_message_is_acknowledged_not_left_pending(self, consumer,
                                                                  stream):
        # It will never become parseable, and leaving it pending would make
        # every future claim drag it along forever.
        stream.publish(entry(1, type="NONSENSE"))
        stream.publish(entry(2))
        consumer.poll_once(T0)
        assert consumer.stats.malformed == 1
        assert stream.pending == 0

    def test_it_reports_how_much_of_the_stream_it_could_not_understand(
        self, consumer, stream
    ):
        # A pipeline emitting a shape this does not know produces a stream that
        # looks healthy and carries nothing.
        for seq in range(1, 4):
            stream.publish(entry(seq, type="NONSENSE"))
        stream.publish(entry(4))
        consumer.poll_once(T0)
        assert consumer.stats.unparseable_fraction == pytest.approx(0.75)


class TestStrandedMessages:
    def test_a_dead_consumers_messages_are_claimed(self, stream):
        # Without this they sit in Redis forever and the events never arrive,
        # silently, which is the worst way for evidence to go missing.
        dead = EventConsumer(ingestor=Ingestor(), client=stream,
                             stream="s", consumer_name="edge-dead")
        for seq in range(1, 6):
            stream.publish(entry(seq))
        dead.client.read_group("s", "g", "edge-dead", 200, 0)
        assert stream.pending == 5

        alive = EventConsumer(ingestor=Ingestor(), client=stream,
                              stream="s", consumer_name="edge-alive")
        assert alive.claim_stale(T0) == 5
        assert alive.stats.claimed == 5
        assert stream.pending == 0

    def test_claiming_nothing_is_not_an_error(self, consumer):
        assert consumer.claim_stale(T0) == 0

    def test_a_claim_during_an_outage_reports_rather_than_raises(self, consumer,
                                                                 stream):
        stream.up = False
        assert consumer.claim_stale(T0) == 0
        assert consumer.is_degraded is True


class TestOutages:
    def test_an_unreachable_stream_degrades_the_system(self, consumer, stream):
        # It blinds the system, and the board must say so rather than showing a
        # calm, frozen picture.
        stream.up = False
        assert consumer.poll_once(T0) == 0
        assert consumer.is_degraded is True
        assert consumer.ingestor.state.health.is_blind is True

    def test_the_outage_names_the_event_bus(self, consumer, stream):
        stream.up = False
        consumer.poll_once(T0)
        degradation = consumer.ingestor.state.health.open_now()[0]
        assert degradation.component is Component.EVENT_BUS

    def test_a_flapping_stream_is_one_outage(self, consumer, stream):
        stream.up = False
        for _ in range(10):
            consumer.poll_once(T0)
        assert consumer.stats.outages == 1
        assert len(consumer.ingestor.state.health.degradations) == 1

    def test_recovery_closes_the_outage_and_resumes(self, consumer, stream):
        for seq in range(1, 4):
            stream.publish(entry(seq))
        stream.up = False
        consumer.poll_once(T0)
        assert consumer.ingestor.state.health.is_blind is True

        stream.up = True
        assert consumer.poll_once(T0 + 60_000) == 3
        assert consumer.is_degraded is False
        assert consumer.ingestor.state.health.is_blind is False

    def test_the_outage_stays_in_the_record_after_recovery(self, consumer, stream):
        stream.up = False
        consumer.poll_once(T0)
        stream.up = True
        consumer.poll_once(T0 + 60_000)
        health = consumer.ingestor.state.health
        assert health.was_degraded_at(T0 + 30_000) is True
        assert health.was_degraded_at(T0 + 90_000) is False

    def test_nothing_is_lost_across_an_outage(self, consumer, stream):
        for seq in range(1, 4):
            stream.publish(entry(seq))
        stream.up = False
        consumer.poll_once(T0)
        for seq in range(4, 7):
            stream.publish(entry(seq))
        stream.up = True
        consumer.poll_once(T0 + 60_000)
        assert consumer.stats.events_applied == 6


class TestGroupSetup:
    def test_creating_the_group_succeeds(self, consumer):
        assert consumer.ensure_group() is True

    def test_an_existing_group_is_not_an_error(self, consumer):
        consumer.ensure_group()
        assert consumer.ensure_group() is True

    def test_an_unreachable_stream_is_reported(self, consumer, stream):
        stream.up = False
        assert consumer.ensure_group() is False
        assert consumer.is_degraded is True


class TestGroupSetupFailures:
    """Only "the group is already there" counts as success.

    Everything else was reported as success too, so the consumer went on to
    poll a group that does not exist and the operator was told "cannot read"
    rather than what actually went wrong.
    """

    def test_a_wrong_type_key_is_reported_rather_than_shrugged_off(
            self, consumer, stream):
        def refuse(_stream, _group):
            raise RuntimeError("WRONGTYPE Operation against a key holding "
                               "the wrong kind of value")

        stream.create_group = refuse
        assert consumer.ensure_group() is False
        assert consumer.is_degraded is True
        assert "WRONGTYPE" in consumer.stats.last_error

    def test_an_existing_group_is_still_not_an_error(self, consumer):
        consumer.ensure_group()
        assert consumer.ensure_group() is True
        assert consumer.is_degraded is False


class TestTheCameraStreamReachesTheBoard:
    """The drill's ingestor was fed by nothing except the drill itself.

    A `Drill` builds its own `Ingestor` and only sees what happens inside it:
    the start, the end, warden confirmations, headcounts. The consumer folded
    the camera stream into the node's ingestor, which the board never reads. So
    presence, identity, timing and every disagreement with the roll-call were
    computed from a fold that had seen no camera evidence.

    Nothing caught it because every unit test and the whole simulator feed a
    drill directly. That is the one path production did not use.
    """

    def a_drill(self, drill_id="d"):
        from app.core.roster import ExpectationReason, Roster
        from app.drill import Drill, DrillRegistry

        roster = Roster()
        roster.add_employee(emp_id="EMP-000", display_name="Person 0",
                            has_gallery_entry=True,
                            assigned_assembly_zone="assembly-north",
                            reason=ExpectationReason.ON_SHIFT)
        drill = Drill(drill_id=drill_id, tenant_id="t", site_id="s",
                      name="Q3", roster=roster.snapshot(T0), created_ms=T0,
                      assembly_zones=frozenset({"assembly-north"}))
        registry = DrillRegistry()
        registry.add(drill)
        return drill, registry

    def test_an_observation_reaches_the_drill_it_names(self):
        drill, registry = self.a_drill()
        drill.start(T0)
        stream = FakeStream()
        stream.publish(entry(1))
        consumer = EventConsumer(ingestor=Ingestor(), client=stream,
                                 stream="s", registry=registry)
        consumer.ensure_group()

        assert consumer.poll_once(T0) == 1
        assert drill.ingestor.state.presence.get("gp-1").last_sighting_ms

    def test_it_lands_in_the_drills_own_record(self):
        # Not only in the board. A drill recovered after a restart replays from
        # its stored events, and an observation that never reached the store is
        # one the recovered drill has never heard of.
        drill, registry = self.a_drill()
        drill.start(T0)
        appended = []
        drill.events_store = type("Store", (), {
            "append": lambda _self, event, now_ms=None: appended.append(event),
            "is_degraded": False,
        })()
        stream = FakeStream()
        stream.publish(entry(1))
        consumer = EventConsumer(ingestor=Ingestor(), client=stream,
                                 stream="s", registry=registry)
        consumer.ensure_group()
        consumer.poll_once(T0)

        assert [e.seq for e in appended] == [1]

    def test_an_event_for_a_drill_that_is_over_does_not_reopen_it(self):
        # Folding it in would change a record somebody has already signed.
        drill, registry = self.a_drill()
        drill.start(T0)
        drill.complete(T0 + 60_000)
        node = Ingestor()
        stream = FakeStream()
        stream.publish(entry(1))
        consumer = EventConsumer(ingestor=node, client=stream, stream="s",
                                 registry=registry)
        consumer.ensure_group()
        consumer.poll_once(T0 + 120_000)

        assert drill.ingestor.state.presence.get("gp-1").last_sighting_ms is None
        # Still counted, so `/healthz` does not go quiet about it.
        assert node.state.presence.get("gp-1").last_sighting_ms is not None

    def test_an_event_for_a_drill_this_node_does_not_have_is_still_counted(self):
        _, registry = self.a_drill(drill_id="somebody-elses")
        node = Ingestor()
        stream = FakeStream()
        stream.publish(entry(1))
        consumer = EventConsumer(ingestor=node, client=stream, stream="s",
                                 registry=registry)
        consumer.ensure_group()

        assert consumer.poll_once(T0) == 1
        assert node.state.presence.get("gp-1").last_sighting_ms is not None


class TestEveryEventIsQueuedForCentral:
    """`Replicator.enqueue` was called by no production code at all.

    The outbox stayed empty, the supervisor's replicate job flushed nothing
    every fifteen seconds, and `/healthz` reported a replication backlog of
    zero -- correctly, and meaning the opposite of what a reader takes from it.
    Central received nothing, ever.
    """

    class Spy:
        def __init__(self):
            self.events = []

        def enqueue(self, event, now_ms=None):
            self.events.append(event)

    def test_a_folded_event_is_handed_to_central(self):
        spy = self.Spy()
        stream = FakeStream()
        stream.publish(entry(1))
        stream.publish(entry(2))
        consumer = EventConsumer(ingestor=Ingestor(), client=stream,
                                 stream="s", replicator=spy)
        consumer.ensure_group()
        consumer.poll_once(T0)

        assert [e.seq for e in spy.events] == [1, 2]

    def test_a_redelivery_is_not_queued_twice(self):
        spy = self.Spy()
        stream = FakeStream()
        stream.publish(entry(1))
        consumer = EventConsumer(ingestor=Ingestor(), client=stream,
                                 stream="s", replicator=spy)
        consumer.ensure_group()
        consumer.poll_once(T0)
        stream.publish(entry(1))
        consumer.poll_once(T0 + 1_000)

        assert len(spy.events) == 1

    def test_a_broken_outbox_does_not_stop_the_drill(self):
        # Replication is never on the critical path. An edge node with no link
        # to central is fully operational, and one with a broken buffer must be.
        class Broken:
            def enqueue(self, event, now_ms=None):
                raise OSError("disk full")

        stream = FakeStream()
        stream.publish(entry(1))
        consumer = EventConsumer(ingestor=Ingestor(), client=stream,
                                 stream="s", replicator=Broken())
        consumer.ensure_group()

        assert consumer.poll_once(T0) == 1
        assert "disk full" in consumer.stats.last_error
