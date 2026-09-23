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
from app.ingest.health import Component
from app.ingest.ingestor import Ingestor
from app.service.supervisor import ClockStep

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


class TestTheNodesOwnClockFailing:
    """A clock correction is an infrastructure failure, and it is the one that
    makes this board *more* confident rather than less.

    Invariant 8 says a failure degrades the system rather than being absorbed
    by it, so the step is recorded as an outage with a cause, the way a dark
    camera is -- which is what lets a post-drill report answer "was the system
    trustworthy when it said that?" about the minutes either side of it.
    """

    def back(self, by_ms=2_400_000, at=T0):
        return ClockStep(at_ms=at, delta_ms=-by_ms, monotonic_ms=0)

    def test_a_correction_opens_an_outage_rather_than_being_logged(self, ingestor):
        ingestor.clock_stepped(self.back())
        open_now = ingestor.state.health.open_now()
        assert [d.component for d in open_now] == [Component.CLOCK]
        assert "backwards" in open_now[0].reason

    def test_and_it_blinds_the_board_while_it_is_open(self, ingestor):
        # Ageing is how this system decides nobody has seen somebody for
        # ninety seconds, and after a backward step every reading it has sits
        # in the future. Silence stops carrying information.
        ingestor.clock_stepped(self.back())
        assert ingestor.state.is_blind

    def test_it_stays_open_until_wall_clock_has_caught_up(self, ingestor):
        ingestor.clock_stepped(self.back(by_ms=2_400_000))
        # A tick a minute later: wall clock is nowhere near back to where it
        # was, and the readings taken before the step still mean nothing.
        ingestor.tick(T0 + 60_000)
        assert ingestor.state.is_blind

        ingestor.tick(T0 + 2_400_000)
        assert not ingestor.state.is_blind
        assert ingestor.state.health.open_now() == []

    def test_a_forward_correction_clears_on_the_next_tick(self, ingestor):
        # The board over-reports people as unobserved, which is the safe
        # direction, and one tick later every age has been recomputed against
        # the new clock.
        ingestor.clock_stepped(
            ClockStep(at_ms=T0, delta_ms=2_400_000, monotonic_ms=0))
        assert ingestor.state.is_blind
        ingestor.tick(T0 + 1_000)
        assert not ingestor.state.is_blind

    def test_the_outage_is_still_in_the_record_after_it_closes(self, ingestor):
        # The whole reason it is an interval. A boolean is back to healthy by
        # the time anybody reads the report.
        ingestor.clock_stepped(self.back(by_ms=30_000))
        ingestor.tick(T0 + 30_000)
        assert ingestor.state.health.was_degraded_at(T0 + 10_000)
        assert not ingestor.state.health.was_degraded_at(T0 + 40_000)

    def test_two_corrections_in_a_row_do_not_stack_up(self, ingestor):
        # A node being brought into sync can step more than once. A second
        # outage for the same thing already down is noise in a report somebody
        # reads under pressure.
        ingestor.clock_stepped(self.back(by_ms=30_000))
        ingestor.clock_stepped(self.back(by_ms=30_000, at=T0 + 1_000))
        assert len(ingestor.state.health.open_now()) == 1

    def test_a_node_whose_clock_never_moves_records_nothing(self, ingestor):
        ingestor.tick(T0)
        ingestor.tick(T0 + 1_000)
        assert ingestor.state.health.degradations == []
