"""The transport's own HTTP handling, without a server.

`tests/ingest/test_replication_http.py` drives the edge against a real central
app, but it does so through an injected opener, so the block that actually
speaks HTTP -- and decides which failures are worth retrying -- had no test at
all. That block is where a wrong credential is told apart from a dead link, and
getting it wrong once already produced a node that retried a bad token until the
disk filled.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from app.ingest.http_transport import HttpTransport, ReplicationRejected
from app.ingest.replication import TransportUnavailable


def transport(**overrides) -> HttpTransport:
    return HttpTransport(url="http://central/api/evac/replication/events",
                         token="a-secret", site_id="site-1", **overrides)


class Body:
    """The context manager `urlopen` returns."""

    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def opening(monkeypatch, result):
    """Point the transport's urllib at `result`, which may be an exception."""
    import urllib.request

    def fake(request, timeout=None):
        if isinstance(result, Exception):
            raise result
        return Body(result)

    monkeypatch.setattr(urllib.request, "urlopen", fake)


class TestRefusingToStartWithoutSomewhereToSend:

    def test_no_url_is_refused(self):
        with pytest.raises(ValueError, match="central URL is required"):
            HttpTransport(url="", token="t", site_id="s")

    def test_no_token_is_refused(self):
        # A node without one retries until the disk fills, which looks like a
        # storage problem rather than a missing setting.
        with pytest.raises(ValueError, match="replication token is required"):
            HttpTransport(url="http://central/", token="", site_id="s")


class TestWhatCentralDecided:

    def test_a_good_answer_is_counted(self, monkeypatch):
        opening(monkeypatch, json.dumps({"accepted": 5, "refused": []}))
        sender = transport()
        sender.send([{"seq": 1}])
        assert sender.accepted == 5
        assert sender.batches == 1
        assert sender.last_refusals == ()

    def test_refusals_are_surfaced_rather_than_raised(self, monkeypatch):
        # Anything central *decided* is not an exception. Only being unable to
        # ask is.
        opening(monkeypatch, json.dumps({"accepted": 3, "refused": [7, 9]}))
        sender = transport()
        sender.send([{"seq": 1}])
        assert sender.refused == 2
        assert sender.last_refusals == (7, 9)

    def test_an_empty_body_is_not_a_failure(self, monkeypatch):
        opening(monkeypatch, "")
        sender = transport()
        sender.send([{"seq": 1}])
        assert sender.batches == 1


class TestTellingAWrongTokenFromADeadLink:
    """The distinction the retry path depends on."""

    @pytest.mark.parametrize("code", [401, 403])
    def test_an_authentication_failure_is_raised_as_itself(
            self, monkeypatch, code):
        opening(monkeypatch, urllib.error.HTTPError(
            "http://central/", code, "no", {}, None))
        with pytest.raises(ReplicationRejected, match="EVAC_CENTRAL_TOKEN"):
            transport().send([{"seq": 1}])

    @pytest.mark.parametrize("code", [400, 500, 502, 503])
    def test_every_other_status_is_a_link_problem(self, monkeypatch, code):
        opening(monkeypatch, urllib.error.HTTPError(
            "http://central/", code, "no", {}, None))
        with pytest.raises(TransportUnavailable, match=str(code)):
            transport().send([{"seq": 1}])

    def test_an_unreachable_host_is_a_link_problem(self, monkeypatch):
        opening(monkeypatch, urllib.error.URLError("connection refused"))
        with pytest.raises(TransportUnavailable, match="connection refused"):
            transport().send([{"seq": 1}])

    def test_a_body_that_is_not_json_is_a_link_problem(self, monkeypatch):
        # A proxy returning its own error page with a 200 is the shape of this.
        opening(monkeypatch, "<html>gateway timeout</html>")
        with pytest.raises(TransportUnavailable):
            transport().send([{"seq": 1}])

    def test_a_rejection_is_not_retried_by_the_replicator(self, tmp_path):
        """The point of the distinction, asserted end to end.

        The replicator tests this by class rather than by class *name*, which
        it used to do -- renaming the exception would have silently stopped a
        wrong credential from blocking, and a wrong credential that does not
        block retries until the disk fills.
        """
        from app.core.events import Event, EventType, SourceKind
        from app.ingest.replication import Outbox, Replicator

        class Refusing:
            def send(self, batch):
                raise ReplicationRejected("central rejected the credential")

        replicator = Replicator(outbox=Outbox(tmp_path / "outbox.db"),
                                transport=Refusing())
        replicator.enqueue(Event(
            tenant_id="t", site_id="s", drill_id="d", source="cam-1",
            source_kind=SourceKind.CAMERA, seq=1,
            type=EventType.TRACK_UPDATED, ts_ms=1, subject="gp-1",
            payload={}), now_ms=1)

        assert replicator.flush(1) == 0
        assert replicator.blocked_reason is not None
        assert replicator.backlog == 1


class TestWhatIsSent:

    def test_the_site_and_token_are_in_the_headers(self):
        headers = transport()._headers()
        assert headers["X-EVAC-Site"] == "site-1"
        assert headers["X-EVAC-Replication-Token"] == "a-secret"

    def test_the_batch_is_wrapped_with_its_site(self, monkeypatch):
        seen = {}

        def opener(url, payload, headers):
            seen["payload"] = json.loads(payload)
            return {"accepted": 1, "refused": []}

        transport(opener=opener).send([{"seq": 4}])
        assert seen["payload"]["site_id"] == "site-1"
        assert seen["payload"]["events"] == [{"seq": 4}]
