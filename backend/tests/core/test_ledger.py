"""Evidence ledger and explain()."""

import pytest

from app.core.ledger import (
    EvidenceKind,
    EvidenceLedger,
    Stance,
)

T0 = 1_788_000_000_000


@pytest.fixture
def ledger() -> EvidenceLedger:
    return EvidenceLedger()


def confirmed_and_assembled(ledger: EvidenceLedger, subject="EMP-482") -> None:
    ledger.record(subject=subject, kind=EvidenceKind.IDENTITY_CONFIRMED,
                  ts_ms=T0 + 12_000, stance=Stance.SUPPORTS, identity=subject,
                  source="cam-3", summary="face confirmed, 4 votes, mean score 0.81")
    ledger.record(subject=subject, kind=EvidenceKind.ASSEMBLY_ARRIVAL,
                  ts_ms=T0 + 47_000, stance=Stance.SUPPORTS, source="cam-9",
                  summary="settled in assembly zone North Car Park")
    ledger.record(subject=subject, kind=EvidenceKind.DECISION, ts_ms=T0 + 47_500,
                  stance=Stance.SUPPORTS, summary="ACCOUNTED")


class TestRecording:
    def test_evidence_must_be_about_someone(self, ledger):
        with pytest.raises(ValueError, match="subject"):
            ledger.record(subject="", kind=EvidenceKind.FACE_MATCH, ts_ms=T0)

    def test_items_are_returned_in_time_order(self, ledger):
        ledger.record(subject="EMP-1", kind=EvidenceKind.ASSEMBLY_ARRIVAL,
                      ts_ms=T0 + 5_000)
        ledger.record(subject="EMP-1", kind=EvidenceKind.FACE_MATCH, ts_ms=T0)
        ledger.record(subject="EMP-1", kind=EvidenceKind.TRACK_LOST, ts_ms=T0 + 2_000)
        assert [e.ts_ms for e in ledger.for_subject("EMP-1")] == [
            T0, T0 + 2_000, T0 + 5_000]

    def test_simultaneous_items_keep_their_arrival_order(self, ledger):
        # Two cameras can stamp the same millisecond. Insertion order breaks the
        # tie so a replay produces a stable narrative.
        for i in range(5):
            ledger.record(subject="EMP-1", kind=EvidenceKind.FACE_MATCH,
                          ts_ms=T0, summary=f"obs {i}")
        assert [e.summary for e in ledger.for_subject("EMP-1")] == [
            f"obs {i}" for i in range(5)]

    def test_subjects_are_kept_apart(self, ledger):
        ledger.record(subject="EMP-1", kind=EvidenceKind.FACE_MATCH, ts_ms=T0)
        ledger.record(subject="EMP-2", kind=EvidenceKind.FACE_MATCH, ts_ms=T0)
        assert len(ledger.for_subject("EMP-1")) == 1
        assert ledger.subjects() == ["EMP-1", "EMP-2"]

    def test_evidence_is_immutable(self, ledger):
        item = ledger.record(subject="EMP-1", kind=EvidenceKind.FACE_MATCH, ts_ms=T0)
        with pytest.raises(Exception):
            item.stance = Stance.CONTRADICTS


class TestExplain:
    def test_it_answers_why_someone_was_accounted(self, ledger):
        confirmed_and_assembled(ledger)
        explanation = ledger.explain("EMP-482")
        assert explanation.decision == "ACCOUNTED"
        assert len(explanation.supporting) == 3
        kinds = {e.kind for e in explanation.supporting}
        assert EvidenceKind.IDENTITY_CONFIRMED in kinds
        assert EvidenceKind.ASSEMBLY_ARRIVAL in kinds

    def test_an_unknown_subject_explains_honestly(self, ledger):
        explanation = ledger.explain("EMP-999")
        assert explanation.decision is None
        assert explanation.supporting == ()
        narrative = "\n".join(explanation.narrate())
        # Invariant 1, stated in the words an operator reads.
        assert "Absence of evidence is not evidence of absence" in narrative

    def test_the_narrative_is_chronological_and_marks_stance(self, ledger):
        ledger.record(subject="EMP-1", kind=EvidenceKind.IDENTITY_CONFIRMED,
                      ts_ms=T0, stance=Stance.SUPPORTS, summary="face confirmed")
        ledger.record(subject="EMP-1", kind=EvidenceKind.CAMERA_DEGRADED,
                      ts_ms=T0 + 1_000, stance=Stance.CONTEXT, summary="cam-3 offline")
        ledger.record(subject="EMP-1", kind=EvidenceKind.TRACK_LOST,
                      ts_ms=T0 + 2_000, stance=Stance.CONTRADICTS, summary="track lost")
        lines = ledger.explain("EMP-1").narrate()
        body = [line for line in lines if line.startswith("  ")]
        assert body[0].strip().startswith("+")
        assert body[1].strip().startswith("·")
        assert body[2].strip().startswith("-")

    def test_human_evidence_is_flagged_in_the_narrative(self, ledger):
        ledger.record(subject="EMP-1", kind=EvidenceKind.WARDEN_CONFIRMATION,
                      ts_ms=T0, stance=Stance.SUPPORTS, source="warden-7",
                      identity="EMP-1", summary="confirmed present at North Car Park")
        narrative = "\n".join(ledger.explain("EMP-1").narrate())
        assert "(human)" in narrative
        assert ledger.explain("EMP-1").has_human_confirmation is True

    def test_context_evidence_is_neither_for_nor_against(self, ledger):
        # A camera outage argues nothing about the person. It changes what the
        # following silence is worth, which is a different thing.
        ledger.record(subject="EMP-1", kind=EvidenceKind.CAMERA_DEGRADED,
                      ts_ms=T0, stance=Stance.CONTEXT, summary="cam-3 offline")
        explanation = ledger.explain("EMP-1")
        assert explanation.supporting == ()
        assert explanation.contradicting == ()
        assert len(explanation.context) == 1

    def test_blindness_is_collected_separately(self, ledger):
        ledger.record(subject="EMP-1", kind=EvidenceKind.IDENTITY_CONFIRMED,
                      ts_ms=T0, stance=Stance.SUPPORTS, identity="EMP-1")
        ledger.record(subject="EMP-1", kind=EvidenceKind.CAMERA_DEGRADED,
                      ts_ms=T0 + 1_000, stance=Stance.CONTEXT)
        ledger.record(subject="EMP-1", kind=EvidenceKind.SEQUENCE_GAP,
                      ts_ms=T0 + 2_000, stance=Stance.CONTEXT)
        ledger.record(subject="EMP-1", kind=EvidenceKind.FACE_UNAVAILABLE,
                      ts_ms=T0 + 3_000, stance=Stance.CONTEXT)
        blind = ledger.explain("EMP-1").blindness
        assert {e.kind for e in blind} == {
            EvidenceKind.CAMERA_DEGRADED, EvidenceKind.SEQUENCE_GAP,
            EvidenceKind.FACE_UNAVAILABLE}


class TestDisputes:
    def test_two_claimed_identities_are_a_dispute(self, ledger):
        ledger.record(subject="gp-1", kind=EvidenceKind.FACE_MATCH, ts_ms=T0,
                      identity="EMP-1", source="cam-1")
        ledger.record(subject="gp-1", kind=EvidenceKind.FACE_MATCH, ts_ms=T0 + 1_000,
                      identity="EMP-2", source="cam-2")
        disputes = ledger.disputes_for("gp-1")
        assert len(disputes) == 1
        assert disputes[0].identities == ("EMP-1", "EMP-2")

    def test_one_claimed_identity_is_not_a_dispute(self, ledger):
        for i in range(10):
            ledger.record(subject="gp-1", kind=EvidenceKind.FACE_MATCH,
                          ts_ms=T0 + i * 100, identity="EMP-1")
        assert ledger.disputes_for("gp-1") == ()

    def test_the_ledger_never_picks_a_winner(self, ledger):
        # Invariant 3. Nine sightings against one is still a dispute: the
        # ledger reports both and refuses to adjudicate.
        for i in range(9):
            ledger.record(subject="gp-1", kind=EvidenceKind.FACE_MATCH,
                          ts_ms=T0 + i * 100, identity="EMP-1")
        ledger.record(subject="gp-1", kind=EvidenceKind.FACE_MATCH,
                      ts_ms=T0 + 5_000, identity="EMP-2")
        assert ledger.disputes_for("gp-1")[0].identities == ("EMP-1", "EMP-2")

    def test_a_warden_ruling_settles_a_dispute(self, ledger):
        # Invariant 9. Once a human has ruled, the cameras they overruled are
        # not still "in dispute" with them.
        ledger.record(subject="gp-1", kind=EvidenceKind.FACE_MATCH, ts_ms=T0,
                      identity="EMP-1")
        ledger.record(subject="gp-1", kind=EvidenceKind.FACE_MATCH, ts_ms=T0 + 1_000,
                      identity="EMP-2")
        assert ledger.disputes_for("gp-1") != ()
        ledger.record(subject="gp-1", kind=EvidenceKind.WARDEN_CONFIRMATION,
                      ts_ms=T0 + 9_000, identity="EMP-2", source="warden-7")
        assert ledger.disputes_for("gp-1") == ()

    def test_two_wardens_disagreeing_stays_a_dispute(self, ledger):
        # Escalation, not resolution. Neither device outranks the other.
        ledger.record(subject="gp-1", kind=EvidenceKind.WARDEN_CONFIRMATION,
                      ts_ms=T0, identity="EMP-1", source="warden-7")
        ledger.record(subject="gp-1", kind=EvidenceKind.WARDEN_CONFIRMATION,
                      ts_ms=T0 + 1_000, identity="EMP-2", source="warden-9")
        assert ledger.disputes_for("gp-1")[0].identities == ("EMP-1", "EMP-2")

    def test_a_rejected_identity_stops_counting_as_a_claim(self, ledger):
        ledger.record(subject="gp-1", kind=EvidenceKind.FACE_MATCH, ts_ms=T0,
                      identity="EMP-1")
        ledger.record(subject="gp-1", kind=EvidenceKind.FACE_MATCH, ts_ms=T0 + 1_000,
                      identity="EMP-2")
        ledger.record(subject="gp-1", kind=EvidenceKind.IDENTITY_REJECTED,
                      ts_ms=T0 + 2_000, identity="EMP-1", source="warden-7")
        assert ledger.disputes_for("gp-1") == ()

    def test_the_narrative_says_a_dispute_will_not_be_auto_resolved(self, ledger):
        ledger.record(subject="gp-1", kind=EvidenceKind.FACE_MATCH, ts_ms=T0,
                      identity="EMP-1")
        ledger.record(subject="gp-1", kind=EvidenceKind.FACE_MATCH, ts_ms=T0 + 1_000,
                      identity="EMP-2")
        narrative = "\n".join(ledger.explain("gp-1").narrate())
        assert "DISPUTED" in narrative
        assert "human must rule" in narrative

    def test_disputes_are_findable_across_the_whole_drill(self, ledger):
        for subject in ("gp-1", "gp-2"):
            ledger.record(subject=subject, kind=EvidenceKind.FACE_MATCH, ts_ms=T0,
                          identity="EMP-1")
            ledger.record(subject=subject, kind=EvidenceKind.FACE_MATCH,
                          ts_ms=T0 + 1_000, identity="EMP-2")
        ledger.record(subject="gp-3", kind=EvidenceKind.FACE_MATCH, ts_ms=T0,
                      identity="EMP-3")
        assert [d.subject for d in ledger.all_disputes()] == ["gp-1", "gp-2"]


class TestAppendOnly:
    def test_a_contradiction_does_not_erase_what_it_contradicts(self, ledger):
        # Both sides stay in the record. That is what makes a conflict
        # detectable at all.
        ledger.record(subject="EMP-1", kind=EvidenceKind.ASSEMBLY_ARRIVAL,
                      ts_ms=T0, stance=Stance.SUPPORTS, summary="arrived")
        ledger.record(subject="EMP-1", kind=EvidenceKind.ASSEMBLY_DEPARTURE,
                      ts_ms=T0 + 60_000, stance=Stance.CONTRADICTS, summary="left")
        assert len(ledger.for_subject("EMP-1")) == 2
        explanation = ledger.explain("EMP-1")
        assert len(explanation.supporting) == 1
        assert len(explanation.contradicting) == 1

    def test_a_later_decision_supersedes_the_reported_one(self, ledger):
        ledger.record(subject="EMP-1", kind=EvidenceKind.DECISION, ts_ms=T0,
                      summary="UNCERTAIN")
        ledger.record(subject="EMP-1", kind=EvidenceKind.DECISION, ts_ms=T0 + 5_000,
                      summary="ACCOUNTED")
        assert ledger.explain("EMP-1").decision == "ACCOUNTED"
        # Both decisions remain in the record.
        decisions = [e for e in ledger.for_subject("EMP-1")
                     if e.kind is EvidenceKind.DECISION]
        assert len(decisions) == 2
