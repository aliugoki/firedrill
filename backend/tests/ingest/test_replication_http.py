"""Edge to central over HTTP, driven end to end against a real central node.

The transport is stubbed only at the socket: the batch that leaves the edge is
handed to a real FastAPI app with a real reconciler behind it, so what these
prove is the contract between two halves rather than one half against a mock of
the other.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.api.replication import CentralReplica
from app.core.events import Event, EventType, SourceKind
from app.ingest.http_transport import HttpTransport, ReplicationRejected
from app.ingest.replication import (
    Outbox,
    Replicator,
    TransportUnavailable,
    _to_wire,
)

T0 = 1_788_000_000_000
TOKEN = "site-1-replication-secret"


def event(seq: int, site="site-1", drill="d1") -> Event:
    return Event(
        tenant_id="t", site_id=site, drill_id=drill, source="cam-1",
        source_kind=SourceKind.CAMERA, seq=seq, type=EventType.TRACK_UPDATED,
        ts_ms=T0 + seq * 100, subject="gp-1",
        payload={"zone_id": "floor-1", "zone_kind": "FLOOR"})


@pytest.fixture
def central():
    replica = CentralReplica(tokens={"site-1": TOKEN, "site-2": "other-secret"})
    return replica, TestClient(create_app(replica=replica))


@pytest.fixture
def transport(central):
    _, client = central

    def opener(url, payload, headers):
        response = client.post(url, content=payload, headers=headers)
        if response.status_code in (401, 403):
            raise ReplicationRejected(f"central refused ({response.status_code})")
        if response.status_code >= 500:
            raise TransportUnavailable(f"HTTP {response.status_code}")
        return response.json()

    return HttpTransport(url="/api/evac/replication/events", token=TOKEN,
                         site_id="site-1", opener=opener)


@pytest.fixture
def replicator(tmp_path, transport):
    return Replicator(outbox=Outbox(tmp_path / "outbox.db"), transport=transport)


class TestDelivery:
    def test_buffered_events_reach_central(self, replicator, central):
        replica, _ = central
        for seq in range(1, 11):
            replicator.enqueue(event(seq), now_ms=T0)
        assert replicator.drain(T0) == 10
        assert replicator.backlog == 0
        assert replica.reconciler("site-1").report().events_held == 10

    def test_central_deduplicates_a_redelivered_batch(self, transport, central):
        replica, _ = central
        batch = [_to_wire(event(seq)) for seq in range(1, 6)]
        transport.send(batch)
        transport.send(batch)
        report = replica.reconciler("site-1").report()
        assert report.events_held == 5
        assert report.duplicates_seen == 5

    def test_central_reaches_the_same_conclusions(self, replicator, central):
        # Convergence means the same answers, not merely the same event count.
        from app.ingest.ingestor import Ingestor

        replica, _ = central
        events = [event(seq) for seq in range(1, 21)]
        for e in events:
            replicator.enqueue(e, now_ms=T0)
        replicator.drain(T0)

        edge = Ingestor()
        edge.feed_batch(events)
        central_side = Ingestor()
        central_side.feed_batch(replica.reconciler("site-1").received)

        assert ({p.person_id: p.state for p in central_side.state.presence}
                == {p.person_id: p.state for p in edge.state.presence})


class TestAuthentication:
    def test_a_wrong_token_is_rejected_and_not_retried(self, tmp_path, central):
        # Retrying forever with the wrong credential fills the disk and never
        # succeeds, so it is raised as itself rather than as a link failure.
        _, client = central

        def opener(url, payload, headers):
            response = client.post(url, content=payload, headers=headers)
            if response.status_code in (401, 403):
                raise ReplicationRejected("refused")
            return response.json()

        transport = HttpTransport(url="/api/evac/replication/events",
                                  token="wrong", site_id="site-1",
                                  opener=opener)
        with pytest.raises(ReplicationRejected):
            transport.send([_to_wire(event(1))])

    def test_an_unknown_site_is_rejected(self, central):
        _, client = client_and(central)
        response = client.post(
            "/api/evac/replication/events",
            json={"site_id": "site-999", "events": []},
            headers={"X-EVAC-Site": "site-999",
                     "X-EVAC-Replication-Token": TOKEN})
        assert response.status_code == 401

    def test_a_node_cannot_write_another_sites_history(self, central):
        # A node authenticated for site A must not be able to write site B's.
        _, client = client_and(central)
        response = client.post(
            "/api/evac/replication/events",
            json={"site_id": "site-2", "events": []},
            headers={"X-EVAC-Site": "site-1",
                     "X-EVAC-Replication-Token": TOKEN})
        assert response.status_code == 403

    def test_an_event_for_another_site_inside_a_valid_batch_is_refused(
        self, transport, central
    ):
        replica, _ = central
        transport.send([_to_wire(event(1)), _to_wire(event(2, site="site-2"))])
        assert 2 in transport.last_refusals
        assert replica.reconciler("site-1").report().events_held == 1

    def test_the_comparison_is_constant_time(self):
        # Not for its own sake: a token comparison that short-circuits leaks its
        # prefix to anyone willing to time the responses.
        import inspect

        from app.api.replication import CentralReplica as replica_class

        source = inspect.getsource(replica_class.authenticate)
        assert "compare_digest" in source


class TestPartialSuccess:
    """A batch where three of five events were malformed must leave two
    buffered, not all five or none."""

    def test_only_what_landed_is_acknowledged(self, replicator, central):
        good = [event(seq) for seq in (1, 2)]
        for e in good:
            replicator.enqueue(e, now_ms=T0)
        # A malformed row that central will name in its refusals.
        replicator.outbox.conn.execute(
            "INSERT INTO pending (source, seq, payload, queued_at_ms) "
            "VALUES (?, ?, ?, ?)",
            ("cam-1", 99, json.dumps({"seq": 99, "nonsense": True}), T0))
        replicator.outbox.conn.commit()

        replicator.drain(T0)
        # The two good ones landed; the malformed one was named and dropped
        # rather than retried into a permanently full outbox.
        replica, _ = central
        assert replica.reconciler("site-1").report().events_held == 2

    def test_refusals_are_surfaced_rather_than_swallowed(self, transport):
        transport.send([_to_wire(event(1)), {"seq": 42, "garbage": True}])
        assert 42 in transport.last_refusals
        assert transport.refused == 1


class TestCentralIsNeverTheAuthority:
    def test_an_incomplete_copy_says_so(self, transport, central):
        replica, client = central
        transport.send([_to_wire(event(1)), _to_wire(event(5))])
        body = client.get("/api/evac/replication/status/site-1").json()
        assert body["complete"] is False
        assert body["missing_events"] == 3
        assert body["authoritative"] is False
        assert "must not be used for accountability" in body["note"]

    def test_a_late_batch_closes_the_gap(self, transport, central):
        _, client = central
        transport.send([_to_wire(event(1)), _to_wire(event(5))])
        transport.send([_to_wire(event(seq)) for seq in (2, 3, 4)])
        assert client.get(
            "/api/evac/replication/status/site-1").json()["complete"] is True

    def test_central_never_claims_authority_even_when_complete(self, transport,
                                                               central):
        _, client = central
        transport.send([_to_wire(event(seq)) for seq in range(1, 6)])
        body = client.get("/api/evac/replication/status/site-1").json()
        assert body["complete"] is True
        assert body["authoritative"] is False

    def test_sites_do_not_share_a_sequence_tracker(self, central):
        # Two sites' sequence numbers are unrelated; sharing a tracker would
        # manufacture gaps out of nothing.
        replica, _ = central
        replica.accept("site-1", [_to_wire(event(1))])
        replica.accept("site-2", [_to_wire(event(100, site="site-2"))])
        assert replica.reconciler("site-1").report().is_complete is True

    def test_the_status_endpoint_needs_no_credential(self, central):
        # A monitoring system needs completeness without holding a replication
        # secret. It exposes counts, never a person.
        replica, client = central
        replica.accept("site-1", [_to_wire(event(1))])
        assert client.get(
            "/api/evac/replication/status/site-1").status_code == 200

    def test_it_does_not_allocate_state_for_a_site_it_has_never_heard_of(
            self, central):
        """The read side creates nothing.

        It used to call the same create-on-miss lookup the write side does, so
        anyone who could reach central -- no credential needed -- could make it
        allocate a reconciler for every site id they cared to invent.
        """
        replica, client = central
        assert client.get(
            "/api/evac/replication/status/site-1").status_code == 404
        assert client.get(
            "/api/evac/replication/status/invented").status_code == 404
        assert replica.reconcilers == {}


class TestAnEdgeNodeDoesNotReceive:
    def test_only_central_mounts_the_endpoint(self):
        # An edge node exposing this would let anything with the token write
        # its history, and an edge node's history is the authoritative one.
        edge = TestClient(create_app())
        assert edge.post("/api/evac/replication/events",
                         json={"site_id": "s", "events": []}).status_code == 404


def client_and(central):
    replica, client = central
    return replica, client


class TestARejectedCredentialStopsRatherThanRetries:
    """A failure that waiting cannot fix must not sit behind exponential
    backoff pretending to be a transient outage."""

    def _blocked(self, tmp_path, central):
        _, client = central

        def opener(url, payload, headers):
            response = client.post(url, content=payload, headers=headers)
            if response.status_code in (401, 403):
                raise ReplicationRejected(f"central refused ({response.status_code})")
            return response.json()

        transport = HttpTransport(url="/api/evac/replication/events",
                                  token="wrong", site_id="site-1",
                                  opener=opener)
        return Replicator(outbox=Outbox(tmp_path / "outbox.db"),
                          transport=transport)

    def test_the_replicator_stops_after_a_rejection(self, tmp_path, central):
        replicator = self._blocked(tmp_path, central)
        replicator.enqueue(event(1), now_ms=T0)
        replicator.flush(T0)
        assert replicator.is_blocked is True
        assert "refused" in replicator.blocked_reason

    def test_nothing_is_lost_while_blocked(self, tmp_path, central):
        # The events are valid; only the credential is wrong.
        replicator = self._blocked(tmp_path, central)
        for seq in range(1, 6):
            replicator.enqueue(event(seq), now_ms=T0)
        replicator.flush(T0)
        assert replicator.backlog == 5

    def test_it_does_not_keep_hammering_central(self, tmp_path, central):
        replicator = self._blocked(tmp_path, central)
        replicator.enqueue(event(1), now_ms=T0)
        for _ in range(20):
            replicator.flush(T0)
        assert replicator.transport.batches == 0
        assert replicator.stats.failed_flushes == 1

    def test_a_human_can_resume_it(self, tmp_path, central):
        replicator = self._blocked(tmp_path, central)
        replicator.enqueue(event(1), now_ms=T0)
        replicator.flush(T0)

        replicator.transport.token = TOKEN
        replicator.unblock()
        assert replicator.flush(T0) == 1
        assert replicator.is_blocked is False

    def test_a_link_failure_is_still_retried(self, tmp_path):
        # The distinction is the whole point: an unreachable central comes back
        # on its own, a wrong token does not.
        from app.ingest.replication import InMemoryTransport

        transport = InMemoryTransport()
        transport.up = False
        replicator = Replicator(outbox=Outbox(tmp_path / "outbox.db"),
                                transport=transport)
        replicator.enqueue(event(1), now_ms=T0)
        for _ in range(5):
            replicator.flush(T0)
        assert replicator.is_blocked is False
        assert replicator.stats.failed_flushes == 5
