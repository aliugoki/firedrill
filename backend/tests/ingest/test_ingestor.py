"""The fold itself, at the boundary where a producer's payload becomes evidence.

Most of the fold is proved through the projections, the simulator and the chaos
suite, which is the right level for it. What belongs here is the reading of a
payload: the defaults chosen when a producer is silent, which nothing else
looks at and which decide what the rest of the system is allowed to conclude.
"""

from __future__ import annotations

import pytest

from app.core.events import Event, EventType, SourceKind
from app.core.identity_fsm import IdentityState
from app.ingest.ingestor import Ingestor

T0 = 1_788_000_000_000
SEQ = {"n": 0}


def ev(event_type, ts_ms, subject=None, payload=None, source="cam-1",
       kind=SourceKind.CAMERA) -> Event:
    SEQ["n"] += 1
    return Event(
        tenant_id="t", site_id="s", drill_id="d", source=source,
        source_kind=kind, seq=SEQ["n"], type=event_type, ts_ms=ts_ms,
        subject=subject, payload=payload or {})


@pytest.fixture
def ingestor() -> Ingestor:
    node = Ingestor()
    node.feed(ev(EventType.DRILL_STARTED, T0 - 1_000, kind=SourceKind.SYSTEM,
                 source="system"))
    return node




class TestHowTheFaceGotOntoTheBody:
    """`fusion.py` was imported by nothing.

    Its whole subject is that attaching a face to a body by bounding-box
    overlap is weak evidence, and that treating it as strong is how a name
    lands on the wrong person: two people crossing at an exit, one turning to a
    camera. It says the strength must reach the identity gate two ways, as a
    confidence multiplier and as a structural test, because a multiplier alone
    once let every geometry-correlated face through on a numeric coincidence.

    The ingest boundary read `payload.get("association_is_strong", True)`. A
    producer that said nothing -- which is every producer today, the pipeline
    being blocked behind the segfault -- got the strongest possible reading,
    and `WEAK_ASSOCIATION` could never fire for it. Absence of evidence was
    treated as the best evidence, in the one place the module exists to guard.
    """

    def face(self, ingestor, payload, subject="gp-1", ts_ms=T0):
        ingestor.feed(ev(EventType.FACE_OBSERVED, ts_ms, subject, {
            "candidate_id": "EMP-1", "score": 0.9, "margin": 0.4,
            "quality": 0.9, **payload}))

    def test_a_silent_producer_is_not_a_strong_association(self, ingestor):
        self.face(ingestor, {})
        assert ingestor.state.identity.get("gp-1").state is IdentityState.UNKNOWN

    def test_and_the_ledger_says_why_in_words(self, ingestor):
        # "The pipeline did not say" is a different problem from "it said, and
        # the answer was geometry", and an operator has to be able to tell.
        self.face(ingestor, {})
        summaries = [e.summary for e in ingestor.state.ledger]
        assert any("did not say how the face was attached" in s
                   for s in summaries)
        assert any("WEAK_ASSOCIATION" in s for s in summaries)

    def test_a_shared_track_is_admissible(self, ingestor):
        for i in range(3):
            self.face(ingestor, {"association": "SHARED_TRACK"},
                      ts_ms=T0 + i * 100)
        assert ingestor.state.identity.get("gp-1").identity == "EMP-1"

    def test_box_overlap_is_refused_by_the_structural_test(self, ingestor):
        # Not by the confidence multiplier. A rule this important must not
        # depend on two independently-tuned numbers happening to differ.
        for i in range(3):
            self.face(ingestor, {"association": "SPATIAL_IOU",
                                 "track_confidence": 1.0}, ts_ms=T0 + i * 100)
        assert ingestor.state.identity.get("gp-1").identity is None
        assert any("box overlap" in e.summary for e in ingestor.state.ledger)

    def test_the_older_boolean_is_still_understood(self, ingestor):
        # The simulator and the calibration harness emit it, and a stream in
        # flight during a deploy carries it.
        for i in range(3):
            self.face(ingestor, {"association_is_strong": True},
                      ts_ms=T0 + i * 100)
        assert ingestor.state.identity.get("gp-1").identity == "EMP-1"

    def test_a_strength_this_build_does_not_know_is_not_trusted(self, ingestor):
        # A producer naming something unrecognised is one whose association
        # cannot be trusted, not one to be given the benefit of the doubt.
        self.face(ingestor, {"association": "PROBABLY_FINE"})
        assert ingestor.state.identity.get("gp-1").state is IdentityState.UNKNOWN

    def test_the_weight_scales_the_confidence_as_well(self, ingestor):
        # The two halves of the rule are independent: this is the half that
        # degrades smoothly, and it must still be doing its job.
        from app.core.fusion import DEFAULT_ASSOCIATION_WEIGHTS, AssociationKind

        assert DEFAULT_ASSOCIATION_WEIGHTS[AssociationKind.SHARED_TRACK] == 1.0
        assert DEFAULT_ASSOCIATION_WEIGHTS[AssociationKind.NONE] == 0.0
