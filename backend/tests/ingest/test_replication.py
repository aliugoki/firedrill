"""Edge to central replication, and what is actually lost when things break."""

import pytest

from app.core.events import Event, EventType, SourceKind
from app.ingest.replication import (
    InMemoryTransport,
    Outbox,
    Reconciler,
    Replicator,
    TransportUnavailable,
    from_wire,
    measure_rpo,
)

T0 = 1_788_000_000_000


def event(seq: int, source="cam-1", subject="gp-1") -> Event:
    return Event(
        tenant_id="t", site_id="s", drill_id="d", source=source,
        source_kind=SourceKind.CAMERA, seq=seq, type=EventType.TRACK_UPDATED,
        ts_ms=T0 + seq * 100, subject=subject,
        payload={"zone_id": "floor-1", "zone_kind": "FLOOR"})


@pytest.fixture
def replicator(tmp_path):
    transport = InMemoryTransport()
    return Replicator(outbox=Outbox(tmp_path / "outbox.db"), transport=transport)


class TestDurableBuffering:
    def test_events_are_buffered_and_delivered(self, replicator):
        for i in range(1, 11):
            replicator.enqueue(event(i), now_ms=T0)
        assert replicator.backlog == 10
        assert replicator.flush(T0) == 10
        assert replicator.backlog == 0
        assert replicator.transport.reconciler.report().events_held == 10

    def test_buffering_the_same_event_twice_is_idempotent(self, replicator):
        # A producer that crashes mid-write and retries must not send central
        # the same event twice.
        replicator.enqueue(event(1), now_ms=T0)
        replicator.enqueue(event(1), now_ms=T0)
        assert replicator.backlog == 1

    def test_the_buffer_survives_the_process(self, tmp_path):
        path = tmp_path / "outbox.db"
        first = Outbox(path)
        for i in range(1, 6):
            first.add(event(i), T0)
        first.close()

        # A new process, a new connection, the same events.
        second = Outbox(path)
        assert second.depth() == 5
        second.close()

    def test_it_fsyncs_on_commit(self, replicator):
        # The pragma is the whole reason the buffer is worth having. Asserting
        # it means a future change to loosen it fails here rather than silently
        # making the RPO claim false.
        pragma = replicator.outbox.conn.execute(
            "PRAGMA synchronous").fetchone()[0]
        assert pragma == 2  # FULL


class TestTheLinkFailing:
    def test_a_failed_flush_drops_nothing(self, replicator):
        for i in range(1, 11):
            replicator.enqueue(event(i), now_ms=T0)
        replicator.transport.up = False
        assert replicator.flush(T0) == 0
        assert replicator.backlog == 10

    def test_a_failed_flush_never_raises(self, replicator):
        # Replication failing must not interrupt a drill. An edge node with no
        # link to central is still fully operational.
        replicator.enqueue(event(1), now_ms=T0)
        replicator.transport.up = False
        replicator.flush(T0)  # must not raise
        assert replicator.stats.failed_flushes == 1
        assert "TransportUnavailable" in replicator.stats.last_failure_reason

    def test_everything_catches_up_when_the_link_returns(self, replicator):
        replicator.transport.up = False
        for i in range(1, 101):
            replicator.enqueue(event(i), now_ms=T0)
            replicator.flush(T0)
        assert replicator.backlog == 100

        replicator.transport.up = True
        assert replicator.drain(T0 + 60_000) == 100
        assert replicator.backlog == 0
        assert replicator.transport.reconciler.report().is_complete is True

    def test_retry_attempts_are_counted(self, replicator):
        replicator.enqueue(event(1), now_ms=T0)
        replicator.transport.up = False
        for _ in range(5):
            replicator.flush(T0)
        attempts = replicator.outbox.conn.execute(
            "SELECT attempts FROM pending").fetchone()[0]
        assert attempts == 5

    def test_lag_grows_while_the_link_is_down(self, replicator):
        replicator.transport.up = False
        replicator.enqueue(event(1), now_ms=T0)
        assert replicator.lag_ms(T0 + 60_000) == 60_000
        replicator.transport.up = True
        replicator.drain(T0 + 60_000)
        assert replicator.lag_ms(T0 + 60_000) is None


class TestReconciliation:
    def test_central_deduplicates_redelivered_batches(self):
        reconciler = Reconciler()
        batch = [_wire(event(i)) for i in range(1, 6)]
        assert reconciler.accept(batch) == 5
        assert reconciler.accept(batch) == 0
        assert reconciler.duplicates == 5

    def test_central_reports_a_gap_rather_than_filling_it(self):
        reconciler = Reconciler()
        reconciler.accept([_wire(event(1)), _wire(event(5))])
        report = reconciler.report()
        assert report.is_complete is False
        assert report.missing_events == 3
        assert "must not be used for accountability" in "\n".join(report.describe())

    def test_a_late_batch_closes_the_gap(self):
        reconciler = Reconciler()
        reconciler.accept([_wire(event(1)), _wire(event(5))])
        assert reconciler.is_complete is False
        reconciler.accept([_wire(event(i)) for i in (2, 3, 4)])
        assert reconciler.is_complete is True

    def test_malformed_records_are_rejected_not_crashed_on(self):
        reconciler = Reconciler()
        assert reconciler.accept([{"nonsense": True}, _wire(event(1))]) == 1
        assert reconciler.rejected == 1

    def test_a_complete_copy_says_so(self):
        reconciler = Reconciler()
        reconciler.accept([_wire(event(i)) for i in range(1, 21)])
        assert "complete" in "\n".join(reconciler.report().describe())


class TestWireFormat:
    def test_an_event_survives_the_round_trip(self):
        original = event(7)
        restored = from_wire(_wire(original))
        assert restored.event_id == original.event_id
        assert restored.dedupe_key == original.dedupe_key
        assert restored.type is original.type
        assert restored.payload == original.payload


class TestRPO:
    def test_a_drained_replicator_has_nothing_outstanding(self, replicator):
        for i in range(1, 11):
            replicator.enqueue(event(i), now_ms=T0)
        replicator.drain(T0)
        rpo = measure_rpo(replicator, now_ms=T0, flush_interval_ms=1_000)
        assert rpo.unsent_events == 0
        assert rpo.loss_on_disk_failure == 0

    def test_power_loss_costs_nothing_committed(self, replicator):
        for i in range(1, 51):
            replicator.enqueue(event(i), now_ms=T0)
        rpo = measure_rpo(replicator, now_ms=T0, flush_interval_ms=1_000)
        assert rpo.durable_loss_on_power_failure == 0

    def test_disk_loss_costs_everything_central_has_not_acknowledged(self, replicator):
        replicator.transport.up = False
        for i in range(1, 51):
            replicator.enqueue(event(i), now_ms=T0)
            replicator.flush(T0)
        rpo = measure_rpo(replicator, now_ms=T0, flush_interval_ms=1_000)
        assert rpo.loss_on_disk_failure == 50

    def test_the_producer_window_is_reported_not_hidden(self, replicator):
        # "We buffer events" invites the belief that nothing can be lost.
        # Something can: whatever was produced but not yet committed.
        rpo = measure_rpo(replicator, now_ms=T0, flush_interval_ms=2_500)
        assert rpo.exposure_window_ms == 2_500
        assert "produced but not yet committed" in "\n".join(rpo.describe())


def _wire(event_obj: Event) -> dict:
    from app.ingest.replication import _to_wire

    return _to_wire(event_obj)


class TestTwoDrillsOnOneNode:
    """The outbox keyed uniqueness on `(source, seq)` and dropped the second.

    A drill's system events are numbered from one and carry the node's own
    source, so `DRILL_STARTED` for the second drill a node ran collided with
    the first and never reached central. Silently: a rejected insert is
    indistinguishable from a redelivery, which is what the constraint is for.
    The event store's key had `drill_id` in it all along.
    """

    def start_of(self, drill_id):
        from app.core.events import Event, EventType, SourceKind

        return Event(tenant_id="t", site_id="s", drill_id=drill_id,
                     source="edge:s", source_kind=SourceKind.SYSTEM, seq=1,
                     type=EventType.DRILL_STARTED, ts_ms=T0, payload={})

    def test_both_drills_reach_the_buffer(self, tmp_path):
        outbox = Outbox(tmp_path / "o.db")
        assert outbox.add(self.start_of("morning"), T0) is True
        assert outbox.add(self.start_of("afternoon"), T0) is True
        assert outbox.depth() == 2

    def test_a_redelivery_within_one_drill_is_still_rejected(self, tmp_path):
        outbox = Outbox(tmp_path / "o.db")
        outbox.add(self.start_of("morning"), T0)
        assert outbox.add(self.start_of("morning"), T0) is False
        assert outbox.depth() == 1

    def test_a_buffer_written_by_an_older_build_is_brought_forward(self, tmp_path):
        """Its contents are the last copy outside this node.

        `CREATE TABLE IF NOT EXISTS` leaves an existing file exactly as it was,
        so a node upgraded mid-outage would keep the constraint that drops
        events. The unsent ones are rebuilt into the new table rather than left
        behind the old key.
        """
        import json
        import sqlite3

        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE pending (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   source TEXT NOT NULL, seq INTEGER NOT NULL,
                   payload TEXT NOT NULL, queued_at_ms INTEGER NOT NULL,
                   attempts INTEGER NOT NULL DEFAULT 0,
                   UNIQUE(source, seq))""")
        conn.execute(
            "INSERT INTO pending (source, seq, payload, queued_at_ms, attempts)"
            " VALUES (?, ?, ?, ?, ?)",
            ("edge:s", 1, json.dumps({"drill_id": "morning"}), T0, 3))
        conn.commit()
        conn.close()

        outbox = Outbox(path)
        assert outbox.depth() == 1
        # And the drill it could not previously fit now goes in beside it.
        assert outbox.add(self.start_of("afternoon"), T0) is True
        assert outbox.depth() == 2
