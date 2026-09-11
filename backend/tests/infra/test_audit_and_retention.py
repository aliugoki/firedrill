"""The audit log and the retention policy."""

from __future__ import annotations

import pytest

from app.infra.audit import AuditAction, AuditError, AuditLog
from app.infra.retention import (
    BIOMETRIC,
    DEFAULT_RULES,
    DataClass,
    Item,
    RetentionLedger,
    RetentionPolicy,
    Rule,
    Trigger,
)

T0 = 1_788_000_000_000
DAY = 86_400_000


@pytest.fixture
def log() -> AuditLog:
    return AuditLog()


class TestTheAuditLogRecordsWhoDidWhat:
    def test_an_entry_with_no_actor_is_refused(self, log):
        with pytest.raises(AuditError, match="no actor"):
            log.record(action=AuditAction.DRILL_STARTED, actor_id="  ", ts_ms=T0)

    def test_an_override_without_before_and_after_is_refused(self, log):
        # An override recorded without the state it overrode is unreviewable:
        # nobody can tell afterwards whether it was a correction or a mistake.
        with pytest.raises(AuditError, match="before and after"):
            log.record(action=AuditAction.MANUAL_OVERRIDE, actor_id="commander-1",
                       ts_ms=T0, subject="emp:EMP-1",
                       summary="marked accounted by hand")

    def test_an_override_with_both_is_recorded(self, log):
        entry = log.record(
            action=AuditAction.MANUAL_OVERRIDE, actor_id="commander-1", ts_ms=T0,
            subject="emp:EMP-1", summary="marked accounted by hand",
            before={"state": "UNACCOUNTED", "reason": "never observed"},
            after={"state": "ACCOUNTED", "reason": "seen by the deputy"})
        assert entry.is_override is True
        assert entry.before["state"] == "UNACCOUNTED"

    def test_a_plain_observation_needs_no_before_and_after(self, log):
        log.record(action=AuditAction.WARDEN_ACTION, actor_id="warden-7",
                   ts_ms=T0, subject="emp:EMP-1", summary="confirmed present")
        assert len(log) == 1

    def test_overrides_are_findable_on_their_own(self, log):
        # The first thing anyone reviewing a drill should read: the places
        # where the recorded outcome is not the one the evidence produced.
        for i in range(5):
            log.record(action=AuditAction.WARDEN_ACTION, actor_id="warden-7",
                       ts_ms=T0 + i, drill_id="d1", summary="confirmed")
        log.record(action=AuditAction.MANUAL_OVERRIDE, actor_id="commander-1",
                   ts_ms=T0 + 9, drill_id="d1", summary="overrode",
                   before={"state": "UNCERTAIN"}, after={"state": "ACCOUNTED"})
        assert len(log.overrides("d1")) == 1

    def test_exports_are_logged_as_disclosures(self, log):
        # Personal data leaving the building.
        log.record(action=AuditAction.REPORT_EXPORTED, actor_id="officer-1",
                   ts_ms=T0, drill_id="d1", summary="PDF to the safety file")
        assert len(log.disclosures("d1")) == 1

    def test_it_reconstructs_what_was_happening_around_a_moment(self, log):
        for i in range(10):
            log.record(action=AuditAction.WARDEN_ACTION, actor_id="warden-7",
                       ts_ms=T0 + i * 30_000, summary=f"action {i}")
        around = log.at(T0 + 150_000, window_ms=60_000)
        assert 3 <= len(around) <= 5

    def test_the_summary_counts_what_matters(self, log):
        log.record(action=AuditAction.DRILL_STARTED, actor_id="c", ts_ms=T0,
                   drill_id="d1")
        log.record(action=AuditAction.MANUAL_OVERRIDE, actor_id="c", ts_ms=T0,
                   drill_id="d1", before={"a": 1}, after={"a": 2})
        log.record(action=AuditAction.REPORT_EXPORTED, actor_id="o", ts_ms=T0,
                   drill_id="d1")
        summary = log.summarise("d1")
        assert summary["total"] == 3
        assert summary["actors"] == 2
        assert summary["overrides"] == 1
        assert summary["disclosures"] == 1


class TestEvidenceIsNotBiometrics:
    """The distinction the whole policy turns on."""

    def test_embeddings_and_crops_are_biometric_but_evidence_is_not(self):
        assert DataClass.FACE_EMBEDDING in BIOMETRIC
        assert DataClass.FACE_CROP in BIOMETRIC
        assert DataClass.EVIDENCE not in BIOMETRIC
        assert DataClass.EVENT not in BIOMETRIC

    def test_biometric_material_dies_with_the_drill_or_sooner(self):
        policy = RetentionPolicy()
        for rule in policy.biometric_rules:
            if rule.trigger is Trigger.AGE:
                # Only the operator snapshot survives past the drill, and only
                # long enough for a debrief.
                assert rule.max_age_ms <= 7 * DAY

    def test_evidence_outlives_the_faces_it_describes(self):
        policy = RetentionPolicy()
        evidence = policy.rule_for(DataClass.EVIDENCE)
        crop = policy.rule_for(DataClass.FACE_CROP)
        assert evidence.max_age_ms >= 365 * DAY
        assert crop.trigger is Trigger.IMMEDIATE

    def test_the_audit_log_outlives_the_evidence(self):
        # The record of the record. It has to survive what it describes, or it
        # cannot show what happened to it.
        policy = RetentionPolicy()
        assert (policy.rule_for(DataClass.AUDIT).max_age_ms
                >= policy.rule_for(DataClass.EVIDENCE).max_age_ms)

    def test_a_wardens_thumbnails_are_the_shortest_lived_class(self):
        # They leave the building in someone's hands.
        policy = RetentionPolicy()
        assert policy.rule_for(DataClass.DEVICE_THUMBNAIL).trigger is Trigger.DRILL_END


class TestThePolicyCoversEverything:
    def test_an_unclassified_data_class_is_refused(self):
        # An item nobody decided about lives forever by default, which is how a
        # drill system becomes a biometric database nobody signed up for.
        partial = tuple(r for r in DEFAULT_RULES
                        if r.data_class is not DataClass.FACE_CROP)
        with pytest.raises(ValueError, match="FACE_CROP"):
            RetentionPolicy(rules=partial)

    def test_the_default_policy_is_marked_unreviewed(self):
        policy = RetentionPolicy()
        assert policy.calibrated is False
        assert "NO" in "\n".join(policy.describe())

    def test_a_rule_retained_by_age_needs_an_age(self):
        with pytest.raises(ValueError, match="no age given"):
            Rule(DataClass.SNAPSHOT, Trigger.AGE)

    def test_a_rule_not_retained_by_age_must_not_have_one(self):
        with pytest.raises(ValueError, match="not retained by age"):
            Rule(DataClass.FACE_CROP, Trigger.IMMEDIATE, max_age_ms=1000)

    def test_the_description_names_every_class_and_its_reason(self):
        text = "\n".join(RetentionPolicy().describe())
        for data_class in DataClass:
            assert data_class.value in text


class TestPurging:
    def _ledger(self):
        ledger = RetentionLedger()
        ledger.track(Item("emb-1", DataClass.FACE_EMBEDDING, T0, "d1"))
        ledger.track(Item("crop-1", DataClass.FACE_CROP, T0, "d1"))
        ledger.track(Item("thumb-1", DataClass.DEVICE_THUMBNAIL, T0, "d1"))
        ledger.track(Item("snap-1", DataClass.SNAPSHOT, T0, "d1"))
        ledger.track(Item("ev-1", DataClass.EVIDENCE, T0, "d1"))
        return ledger

    def test_embeddings_and_crops_go_immediately(self, log):
        ledger = self._ledger()
        result = ledger.purge(T0, audit=log)
        assert result["removed"].get("FACE_EMBEDDING") == 1
        assert result["removed"].get("FACE_CROP") == 1

    def test_device_thumbnails_go_when_the_drill_ends(self, log):
        ledger = self._ledger()
        ledger.purge(T0 + 1000, audit=log)  # drill still running
        assert any(i.item_id == "thumb-1" for i in ledger.held())
        ledger.purge_drill("d1", T0 + 600_000, audit=log)
        assert not any(i.item_id == "thumb-1" for i in ledger.held())

    def test_evidence_survives_the_drill_by_a_year(self, log):
        ledger = self._ledger()
        ledger.purge_drill("d1", T0 + 600_000, audit=log)
        assert any(i.item_id == "ev-1" for i in ledger.held())
        ledger.purge(T0 + 400 * DAY, audit=log)
        assert not any(i.item_id == "ev-1" for i in ledger.held())

    def test_a_purge_writes_an_audit_entry(self, log):
        # A policy that cannot be verified is a promise rather than a control.
        ledger = self._ledger()
        ledger.purge(T0, actor_id="retention-job", audit=log)
        entries = [e for e in log.entries if e.action is AuditAction.RETENTION_PURGE]
        assert len(entries) == 1
        assert entries[0].context["removed"]["FACE_CROP"] == 1

    def test_a_failed_removal_is_not_marked_purged(self, log):
        # Recording a deletion that did not happen would make the policy a lie
        # that passes its own verification.
        ledger = self._ledger()

        def broken(item):
            raise OSError("storage unavailable")

        result = ledger.purge(T0, audit=log, remover=broken)
        assert result["total"] == 0
        assert len(result["failed"]) == 2
        assert any(i.item_id == "crop-1" for i in ledger.held())

    def test_a_partial_failure_purges_what_it_can(self, log):
        ledger = self._ledger()

        def flaky(item):
            if item.item_id == "crop-1":
                raise OSError("storage unavailable")

        result = ledger.purge(T0, audit=log, remover=flaky)
        assert result["removed"].get("FACE_EMBEDDING") == 1
        assert result["failed"] == ["crop-1"]

    def test_nothing_is_purged_twice(self, log):
        ledger = self._ledger()
        ledger.purge(T0, audit=log)
        assert ledger.purge(T0, audit=log)["total"] == 0


class TestVerification:
    def test_a_clean_ledger_is_compliant(self):
        ledger = RetentionLedger()
        ledger.track(Item("ev-1", DataClass.EVIDENCE, T0, "d1"))
        assert ledger.verify(T0 + 1000)["compliant"] is True

    def test_biometric_material_past_its_class_is_a_finding(self):
        # Deliberately blunt. Any biometric item past its expiry is a finding,
        # not a warning.
        ledger = RetentionLedger()
        ledger.track(Item("crop-1", DataClass.FACE_CROP, T0, "d1"))
        report = ledger.verify(T0 + 1000)
        assert report["compliant"] is False
        assert report["overdue_biometric"] == 1
        assert "outlived their retention class" in report["finding"]

    def test_overdue_non_biometric_data_is_reported_without_a_finding(self):
        ledger = RetentionLedger()
        ledger.track(Item("ev-1", DataClass.EVIDENCE, T0, "d1"))
        report = ledger.verify(T0 + 400 * DAY)
        assert report["compliant"] is False
        assert report["finding"] is None

    def test_purged_items_stop_being_overdue(self):
        ledger = RetentionLedger()
        ledger.track(Item("crop-1", DataClass.FACE_CROP, T0, "d1"))
        ledger.purge(T0)
        assert ledger.verify(T0 + 1000)["compliant"] is True


class TestEveryActionEitherHappensOrIsDeclaredUnbuilt:
    """An action kind with no call site is one of two things, and the
    difference matters: a feature nobody has built, or a place somebody forgot
    to log.

    Until this week every one of the fourteen was the second.
    """

    def recorded_somewhere(self) -> set:
        import pathlib

        root = pathlib.Path(__file__).resolve().parents[2] / "app"
        source = "\n".join(
            path.read_text() for path in root.rglob("*.py")
            if "vendor" not in path.parts and path.name != "audit.py")
        return {action for action in AuditAction
                if f"AuditAction.{action.name}" in source}

    def test_everything_not_declared_unbuilt_has_a_call_site(self):
        from app.infra.audit import NOT_YET_REACHABLE

        expected = set(AuditAction) - NOT_YET_REACHABLE
        assert self.recorded_somewhere() == expected

    def test_the_unbuilt_ones_really_have_none(self):
        # Otherwise the list is an excuse rather than a description.
        from app.infra.audit import NOT_YET_REACHABLE

        assert not (self.recorded_somewhere() & NOT_YET_REACHABLE)

    def test_most_of_the_vocabulary_is_in_use(self):
        assert len(self.recorded_somewhere()) >= 10
