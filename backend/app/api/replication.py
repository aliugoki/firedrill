"""The central node's receiving endpoint.

Central is a replica. It accepts what edge nodes send, deduplicates, records
what it is missing, and refuses to pretend its copy is complete when it is not.

Two rules define what it is allowed to do.

**Central never corrects an edge node.** It does not reorder, backfill, or
infer. If it has a hole, it reports the hole; the edge node's copy is
authoritative and the reconciliation report says so in words.

**Central never becomes the authority.** A `ReconciliationReport` with
outstanding gaps states plainly that it must not be used for accountability.
That sentence exists because the failure mode is somebody running a report from
central during an incident because the edge node is the thing that just died.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.ingest.replication import Reconciler, from_wire


class ReplicationBatch(BaseModel):
    site_id: str = Field(min_length=1)
    events: list[dict]


class ReplicationResult(BaseModel):
    accepted: int
    duplicates: int
    refused: list[int]
    """Sequence numbers central would not take. The edge acknowledges everything
    else, so a batch where three of five were malformed leaves two buffered
    rather than all five or none."""

    held: int
    complete: bool


@dataclass
class CentralReplica:
    """What central holds, per site.

    One reconciler per site: two sites' sequence numbers are unrelated, and
    sharing a tracker between them would manufacture gaps out of nothing.
    """

    tokens: dict = field(default_factory=dict)
    reconcilers: dict = field(default_factory=dict)
    store: object | None = None

    def reconciler(self, site_id: str) -> Reconciler:
        reconciler = self.reconcilers.get(site_id)
        if reconciler is None:
            reconciler = Reconciler()
            self.reconcilers[site_id] = reconciler
        return reconciler

    def authenticate(self, site_id: str, token: str) -> bool:
        """Constant-time comparison against the site's own token.

        A per-site secret proves the sender knows a token, not that it is the
        node it claims to be. That is proportionate on a private link and is
        recorded as a limit in the security document rather than left implied.
        """
        expected = self.tokens.get(site_id)
        if not expected or not token:
            return False
        return hmac.compare_digest(expected, token)

    def accept(self, site_id: str, records: list) -> ReplicationResult:
        reconciler = self.reconciler(site_id)
        refused: list[int] = []
        good: list = []

        for record in records:
            try:
                event = from_wire(record)
            except (KeyError, ValueError, TypeError):
                seq = record.get("seq") if isinstance(record, dict) else None
                if isinstance(seq, int):
                    # Named so the edge can drop exactly this one. Malformed is
                    # malformed; it will not become valid on the ninth attempt,
                    # and retrying it would block everything behind it.
                    refused.append(seq)
                continue
            if event.site_id != site_id:
                refused.append(event.seq)
                continue
            good.append(record)

        before = len(reconciler.received)
        accepted = reconciler.accept(good)
        duplicates = len(good) - (len(reconciler.received) - before)

        if self.store is not None and accepted:
            for event in reconciler.received[before:]:
                self.store.append(event)

        report = reconciler.report()
        return ReplicationResult(
            accepted=accepted, duplicates=duplicates, refused=refused,
            held=report.events_held, complete=report.is_complete)


def build_router(replica: CentralReplica) -> APIRouter:
    router = APIRouter(prefix="/api/evac/replication", tags=["replication"])

    @router.post("/events", response_model=ReplicationResult)
    async def receive(
        request: Request,
        x_evac_site: str = Header(default=""),
        x_evac_replication_token: str = Header(default=""),
    ):
        body = await request.json()
        try:
            batch = ReplicationBatch(**body)
        except Exception:
            raise HTTPException(status.HTTP_400_BAD_REQUEST,
                                "a replication batch needs a site_id and events")

        if x_evac_site and x_evac_site != batch.site_id:
            # The header and the body disagree about who is sending. Refuse
            # rather than pick one: a node authenticated for site A must not be
            # able to write site B's history.
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "the authenticated site does not match the batch")

        if not replica.authenticate(batch.site_id, x_evac_replication_token):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                                "unknown site or bad replication token")

        return replica.accept(batch.site_id, batch.events)

    @router.get("/status/{site_id}")
    def status_for(site_id: str):
        """What central holds for a site, and what it is missing.

        Deliberately unauthenticated in the same way `/healthz` is: it exposes
        counts and completeness, never a person, and a monitoring system needs
        it without holding a replication credential.
        """
        report = replica.reconciler(site_id).report()
        return {
            "site_id": site_id,
            "events_held": report.events_held,
            "duplicates_seen": report.duplicates_seen,
            "outstanding_gaps": report.outstanding_gaps,
            "missing_events": report.missing_events,
            "complete": report.is_complete,
            "authoritative": False,
            "note": "\n".join(report.describe()),
        }

    return router
