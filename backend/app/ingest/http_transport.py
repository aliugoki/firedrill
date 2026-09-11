"""Delivering buffered events to central over HTTP.

The edge half of replication. It is deliberately unremarkable — a POST with a
shared secret — and the interesting decisions are about what it does with the
answer.

**Partial success is the normal case, and it is reported precisely.** Central
replies with the sequence numbers it accepted and the ones it refused. The edge
acknowledges only what landed, so a batch where three of five events were
malformed leaves two buffered rather than all five or none.

**A refused event is dropped, not retried forever.** Malformed is malformed; it
will not become valid on the ninth attempt, and retrying it would keep the
outbox permanently full and block everything behind it. It is counted and
surfaced instead.

**Authentication is a per-site shared secret, and that is a real limit.** It
proves the sender knows a token, not that it is the node it claims to be. A
stolen token lets someone inject events for that site. On a private link between
an edge node and its own central this is proportionate; over the open internet
it is not, and `docs/EVAC120_SECURITY.md` says so rather than leaving the reader
to infer it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from app.ingest.replication import ReplicationRejected, TransportUnavailable

#: Re-exported: this is where a reader of the transport looks for it, and the
#: definition lives in `replication` so `Replicator.flush` can use `isinstance`.
__all__ = ["HttpTransport", "ReplicationRejected"]


@dataclass
class HttpTransport:
    """Sends batches to a central node. Raises only when the link is at fault.

    `opener` is injected so the transport can be tested against a stub without a
    server, and so an operator can swap in a client with the site's own TLS
    trust store without this module knowing about certificates.
    """

    url: str
    token: str
    site_id: str
    timeout_s: float = 10.0
    opener: object | None = None
    accepted: int = 0
    refused: int = 0
    batches: int = 0
    last_refusals: tuple = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.url:
            raise ValueError(
                "a central URL is required; a transport with nowhere to send "
                "would buffer forever while reporting itself healthy")
        if not self.token:
            raise ValueError(
                "a replication token is required; central refuses unauthenticated "
                "batches, so a node without one would retry until the disk fills")

    def send(self, batch: list) -> None:
        """Deliver a batch. Raises `TransportUnavailable` if central is not there.

        Anything central *decided* — a refusal, a duplicate — is not an
        exception. Only being unable to ask is.
        """
        payload = json.dumps({"site_id": self.site_id, "events": batch}).encode()
        try:
            response = self._post(payload)
        except (TransportUnavailable, ReplicationRejected):
            # Both pass through unchanged. Wrapping a rejected credential as a
            # link failure would put it behind the retry-and-backoff path, where
            # it retries forever, never succeeds, and fills the disk while
            # reporting itself as a transient outage.
            raise
        except Exception as exc:
            raise TransportUnavailable(f"{type(exc).__name__}: {exc}") from exc

        self.batches += 1
        self.accepted += response.get("accepted", 0)
        refusals = tuple(response.get("refused", ()))
        self.refused += len(refusals)
        self.last_refusals = refusals

    def _post(self, payload: bytes) -> dict:
        if self.opener is not None:
            return self.opener(self.url, payload, self._headers())

        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            self.url, data=payload, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as handle:
                return json.loads(handle.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                # Not a connectivity problem. Retrying forever with the wrong
                # credential fills the disk and never succeeds, so it is raised
                # as itself and the operator sees an authentication failure.
                raise ReplicationRejected(
                    f"central rejected the credential ({exc.code}); check "
                    "EVAC_CENTRAL_TOKEN") from exc
            raise TransportUnavailable(f"HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise TransportUnavailable(f"{exc.reason}") from exc

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "X-EVAC-Site": self.site_id,
            "X-EVAC-Replication-Token": self.token,
        }


