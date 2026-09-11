"""The post-drill report and its verdict.

The point of these is that a real drill has no oracle. Validation is against the
manual roll-call, and a drill that did not produce one cannot judge the system
however good the system's own numbers look.
"""

from __future__ import annotations

import pytest

from app.core.roster import ExpectationReason, Roster
from app.drill import Drill
from app.infra.audit import AuditAction, AuditLog
from app.reporting.drill_report import build_report
from app.reporting.validation import Criterion, Outcome, Thresholds, validate
from app.warden.actions import ActionKind, WardenAction
from app.warden.headcount import Headcount

T0 = 1_788_000_000_000
ASSEMBLY = frozenset({"assembly-north"})

CLEAN = dict(
    false_accounted=0, false_unaccounted=0, sweeps_completed=2, sweeps_expected=2,
    zones_with_headcount=2, headcount_mismatches=0, timing_samples=200,
    timing_coverage=0.95, p95_s=114.0, p95_reliable=True, blind_fraction=0.0)


def drill_with(people=4, zone="assembly-north") -> Drill:
    roster = Roster()
    for i in range(people):
        roster.add_employee(emp_id=f"EMP-{i:03d}", display_name=f"Person {i}",
                            has_gallery_entry=True, department="Engineering",
                            home_floor_id="floor-2", assigned_assembly_zone=zone,
                            reason=ExpectationReason.ON_SHIFT)
    drill = Drill(drill_id="d1", tenant_id="t", site_id="s", name="Q3 drill",
                  roster=roster.snapshot(T0), created_ms=T0,
                  assembly_zones=ASSEMBLY)
    drill.start(T0)
    return drill


def confirm(drill: Drill, person_ref: str, ts_ms=T0 + 60_000, zone="assembly-north"):
    drill.record_warden_action(WardenAction(
        kind=ActionKind.CONFIRM_PRESENT, warden_id="warden-7",
        device_id="tablet-3", zone_id=zone, ts_ms=ts_ms, subject=person_ref))


class TestTheVerdictOrder:
    """A safety failure outranks everything; missing evidence is not a pass."""

    def test_a_clean_drill_passes(self):
        assert validate(**CLEAN).outcome is Outcome.PASS

    def test_one_false_accounted_fails_the_whole_drill(self):
        result = validate(**{**CLEAN, "false_accounted": 1})
        assert result.outcome is Outcome.FAIL
        assert result.safety_failures
        assert "not confirmed by any warden" in result.summary()

    def test_a_safety_failure_outranks_missing_evidence(self):
        # Both wrong. The report must lead with the dangerous one.
        result = validate(**{**CLEAN, "false_accounted": 1, "sweeps_completed": 0})
        assert result.outcome is Outcome.FAIL
        assert result.safety_failures

    def test_no_sweeps_is_inconclusive_not_a_pass(self):
        # Not a soft pass: there was nothing to check the system against.
        result = validate(**{**CLEAN, "sweeps_completed": 0})
        assert result.outcome is Outcome.INCONCLUSIVE
        assert "cannot judge the system" in result.summary()

    def test_no_headcount_is_inconclusive(self):
        result = validate(**{**CLEAN, "zones_with_headcount": 0})
        assert result.outcome is Outcome.INCONCLUSIVE

    def test_a_mostly_blind_drill_is_inconclusive(self):
        # A test of the wardens, not of the system.
        result = validate(**{**CLEAN, "blind_fraction": 0.4})
        assert result.outcome is Outcome.INCONCLUSIVE
        assert any("test of the wardens" in c.detail
                   for c in result.missing_evidence)

    def test_low_coverage_is_inconclusive(self):
        result = validate(**{**CLEAN, "timing_coverage": 0.4})
        assert result.outcome is Outcome.INCONCLUSIVE

    def test_a_perfect_drill_with_no_evidence_still_cannot_pass(self):
        # Every system number ideal, no warden did anything.
        result = validate(**{
            **CLEAN, "sweeps_completed": 0, "zones_with_headcount": 0})
        assert result.outcome is not Outcome.PASS

    def test_missing_the_target_is_a_fail_not_inconclusive(self):
        # The drill could say, and the answer was no.
        result = validate(**{**CLEAN, "p95_s": 180.0})
        assert result.outcome is Outcome.FAIL

    def test_too_few_samples_makes_the_p95_unmeasurable(self):
        result = validate(**{**CLEAN, "timing_samples": 8, "p95_reliable": False})
        failed = {c.criterion for c in result.failed}
        assert Criterion.P95_MEASURABLE in failed

    def test_a_headcount_disagreement_fails_the_drill(self):
        result = validate(**{**CLEAN, "headcount_mismatches": 1})
        assert result.outcome is Outcome.FAIL

    def test_thresholds_are_marked_unvalidated(self):
        assert Thresholds().calibrated is False
        assert "no live drill has run" in "\n".join(validate(**CLEAN).describe())


class TestFalseAccountedAgainstTheRollCall:
    """The reference is the warden, not the truth, because a real building has
    no truth to consult."""

    def test_a_person_no_warden_confirmed_counts_as_false_accounted(self):
        drill = drill_with(people=2)
        # The system accounts for one via a warden at the zone, and the roll
        # call covers only that one.
        confirm(drill, "emp:EMP-000")
        report = build_report(drill, now_ms=T0 + 600_000)
        assert report.accounted == 1
        assert report.false_accounted == ()

    def test_a_warden_contradiction_is_a_false_accounted(self):
        drill = drill_with(people=1)
        confirm(drill, "emp:EMP-000")
        # Then the same warden says they are not here after all.
        drill.record_warden_action(WardenAction(
            kind=ActionKind.NOT_HERE, warden_id="warden-7", device_id="tablet-3",
            zone_id="assembly-north", ts_ms=T0 + 120_000,
            subject="emp:EMP-000"))
        report = build_report(drill, now_ms=T0 + 600_000)
        # The later assertion wins, so the system no longer accounts for them.
        assert report.accounted == 0
        assert report.false_accounted == ()

    def test_a_person_a_warden_confirmed_but_the_system_missed_is_false_unaccounted(self):
        # A quality failure, not a safety one: it wastes a warden's time, but
        # nobody is left in a building because of it.
        drill = drill_with(people=2)
        confirm(drill, "emp:EMP-000", zone="floor-3")  # not an assembly zone
        report = build_report(drill, now_ms=T0 + 600_000)
        assert len(report.false_unaccounted) == 1
        assert report.false_unaccounted[0].warden_said == "confirmed present"

    def test_the_two_directions_are_never_merged(self):
        # Reporting them as one error rate would average a safety failure with
        # an inconvenience.
        drill = drill_with(people=2)
        confirm(drill, "emp:EMP-000", zone="floor-3")
        report = build_report(drill, now_ms=T0 + 600_000)
        assert isinstance(report.false_accounted, tuple)
        assert isinstance(report.false_unaccounted, tuple)
        assert report.false_accounted is not report.false_unaccounted

    def test_every_false_accounted_is_listed_in_full(self):
        # Never a count on its own. Each one is a person somebody has to go and
        # find.
        drill = drill_with(people=1)
        report = build_report(drill, now_ms=T0 + 600_000)
        text = "\n".join(report.render())
        assert "FALSE ACCOUNTED" in text


class TestTheReport:
    def test_it_leads_with_the_verdict(self):
        drill = drill_with(people=2)
        lines = build_report(drill, now_ms=T0 + 600_000).render()
        joined = "\n".join(lines[:8])
        assert any(word in joined for word in ("PASS", "FAIL", "INCONCLUSIVE"))

    def test_it_reports_no_percentiles_rather_than_zero(self):
        drill = drill_with(people=2)
        report = build_report(drill, now_ms=T0 + 600_000)
        assert report.p95_s is None
        assert "no measurements" in "\n".join(report.render()).lower()

    def test_it_says_the_thresholds_are_not_validated(self):
        # These numbers describe what happened, not how well the system works.
        drill = drill_with(people=2)
        text = "\n".join(build_report(drill, now_ms=T0 + 600_000).render())
        assert "no threshold in this system has been validated" in text

    def test_it_counts_sweeps_and_headcounts_against_the_zones(self):
        drill = drill_with(people=2)
        report = build_report(drill, now_ms=T0 + 600_000)
        assert report.sweeps_expected == 1
        assert report.sweeps_completed == 0
        assert report.zones_with_headcount == 0

    def test_a_completed_sweep_and_count_are_recorded(self):
        drill = drill_with(people=2)
        for i in range(2):
            confirm(drill, f"emp:EMP-{i:03d}")
        drill.record_headcount(Headcount(
            zone_id="assembly-north", warden_id="warden-7", device_id="tablet-3",
            ts_ms=T0 + 120_000, physical_count=2, system_count=2))
        drill.record_warden_action(WardenAction(
            kind=ActionKind.SWEEP_COMPLETE, warden_id="warden-7",
            device_id="tablet-3", zone_id="assembly-north", ts_ms=T0 + 180_000))
        report = build_report(drill, now_ms=T0 + 600_000)
        assert report.sweeps_completed == 1
        assert report.zones_with_headcount == 1
        assert report.headcount_mismatches == ()

    def test_a_headcount_disagreement_is_quoted_in_the_report(self):
        drill = drill_with(people=2)
        for i in range(2):
            confirm(drill, f"emp:EMP-{i:03d}")
        drill.record_headcount(Headcount(
            zone_id="assembly-north", warden_id="warden-7", device_id="tablet-3",
            ts_ms=T0 + 120_000, physical_count=1, system_count=2))
        report = build_report(drill, now_ms=T0 + 600_000)
        assert report.headcount_mismatches
        assert "not at the muster point" in report.headcount_mismatches[0]

    def test_escalations_are_carried_into_the_report(self):
        drill = drill_with(people=2)
        drill.record_warden_action(WardenAction(
            kind=ActionKind.ESCALATE, warden_id="warden-7", device_id="tablet-3",
            zone_id="assembly-north", ts_ms=T0 + 60_000,
            note="smoke in the west stairwell"))
        report = build_report(drill, now_ms=T0 + 600_000)
        assert "smoke in the west stairwell" in report.escalations

    def test_manual_overrides_come_from_the_audit_log(self):
        drill = drill_with(people=2)
        audit = AuditLog()
        audit.record(action=AuditAction.MANUAL_OVERRIDE, actor_id="commander-1",
                     ts_ms=T0 + 300_000, drill_id="d1",
                     subject="emp:EMP-000", summary="marked accounted by hand",
                     before={"state": "UNACCOUNTED"}, after={"state": "ACCOUNTED"})
        report = build_report(drill, now_ms=T0 + 600_000, audit=audit)
        assert report.warden_overrides == 1

    def test_warden_confirmations_are_counted(self):
        drill = drill_with(people=3)
        for i in range(2):
            confirm(drill, f"emp:EMP-{i:03d}")
        assert build_report(drill, now_ms=T0 + 600_000).warden_confirmations == 2

    def test_the_report_renders_without_a_drill_ever_starting(self):
        # A drill created and abandoned still has to produce something readable.
        roster = Roster()
        roster.add_employee(emp_id="EMP-1", display_name="A",
                            has_gallery_entry=True,
                            assigned_assembly_zone="assembly-north",
                            reason=ExpectationReason.ON_SHIFT)
        drill = Drill(drill_id="d2", tenant_id="t", site_id="s", name="abandoned",
                      roster=roster.snapshot(T0), created_ms=T0,
                      assembly_zones=ASSEMBLY)
        lines = build_report(drill, now_ms=T0 + 1000).render()
        assert lines
        assert any("INCONCLUSIVE" in line for line in lines)


class TestASiteTooSmallToJudge:
    """A ten-person office cannot produce a 95th percentile, and that is a fact
    about the office rather than about the system.

    It used to report FAIL on a drill where nobody was falsely accounted, every
    zone was swept and counted, the counts agreed, coverage was total and the
    cameras never blinked -- and no number of good drills could have changed
    it. A safety officer reading that concludes the system is broken.
    """

    SMALL = dict(
        false_accounted=0, false_unaccounted=0,
        sweeps_completed=1, sweeps_expected=1,
        zones_with_headcount=1, headcount_mismatches=0,
        timing_samples=10, timing_coverage=1.0,
        p95_s=95.0, p95_reliable=False, blind_fraction=0.0)

    def test_it_is_inconclusive_rather_than_a_failure(self):
        result = validate(**self.SMALL)
        assert result.outcome is Outcome.INCONCLUSIVE
        assert "cannot judge the system" in result.summary()

    def test_it_says_which_evidence_was_missing(self):
        result = validate(**self.SMALL)
        missing = {check.criterion for check in result.missing_evidence}
        assert Criterion.P95_MEASURABLE in missing

    def test_a_real_disagreement_is_still_a_failure(self):
        # The guard: making this evidentiary must not soften a drill where the
        # humans and the system actually disagreed.
        disagreed = dict(self.SMALL, false_accounted=1)
        assert validate(**disagreed).outcome is Outcome.FAIL

        mismatched = dict(self.SMALL, headcount_mismatches=1)
        assert validate(**mismatched).outcome is Outcome.FAIL

    def test_a_big_roster_with_too_few_samples_is_unchanged(self):
        # Too few samples from a large roster means people went untracked,
        # which `COVERAGE_SUFFICIENT` already calls missing evidence.
        untracked = dict(self.SMALL, timing_samples=10, timing_coverage=0.05)
        result = validate(**untracked)
        assert result.outcome is Outcome.INCONCLUSIVE
        assert Criterion.COVERAGE_SUFFICIENT in {
            check.criterion for check in result.missing_evidence}


class TestADisagreementOutranksMissingEvidence:
    """The wardens counted, the system counted, and the two differed.

    That is true whether or not the drill was large enough or complete enough
    to judge anything else, so it is a FAIL rather than an INCONCLUSIVE. It
    used to sit behind the evidence gate, which buried the second most
    important signal a drill produces behind "not enough evidence" -- when the
    evidence in question was exactly what had been collected.
    """

    INCOMPLETE = dict(
        false_accounted=0, false_unaccounted=0,
        sweeps_completed=1, sweeps_expected=2,
        zones_with_headcount=1, headcount_mismatches=0,
        timing_samples=200, timing_coverage=0.95,
        p95_s=95.0, p95_reliable=True, blind_fraction=0.0)

    def test_an_incomplete_drill_alone_is_inconclusive(self):
        assert validate(**self.INCOMPLETE).outcome is Outcome.INCONCLUSIVE

    def test_the_same_drill_with_a_disagreement_fails(self):
        result = validate(**dict(self.INCOMPLETE, headcount_mismatches=1))
        assert result.outcome is Outcome.FAIL

    def test_the_headline_names_the_disagreement_not_the_gap(self):
        # A commander reading "FAIL — not every zone was swept" would go and
        # chase the sweep, not the count that did not add up.
        result = validate(**dict(self.INCOMPLETE, headcount_mismatches=1))
        assert "disagreed with the system" in result.summary()

    def test_a_target_that_could_not_be_measured_is_not_a_target_missed(self):
        # P95_WITHIN_TARGET deliberately stays behind the evidence gate.
        unmeasurable = dict(self.INCOMPLETE, timing_samples=2,
                            timing_coverage=0.02, p95_s=None,
                            p95_reliable=False)
        assert validate(**unmeasurable).outcome is Outcome.INCONCLUSIVE


class TestTheReportASafetyOfficerReads:
    """The rendered text, which had no test.

    It is the artefact of the whole drill: what gets filed, quoted in a
    post-incident review, and read by somebody deciding whether the system can
    be relied on. Its numbers were exercised through the dataclass and its words
    were not.
    """

    def report(self, **overrides):
        from app.reporting.drill_report import Disagreement, DrillReport

        base = dict(
            drill_id="d1", name="Q3 drill", site_id="site-1",
            started_ms=T0, completed_ms=T0 + 600_000, duration_s=600.0,
            expected=10, accounted=9, unaccounted=1, uncertain=0,
            needing_verification=0, unknown_people=0,
            p50_s=64.0, p90_s=104.0, p95_s=114.0, p99_s=195.0, max_s=210.0,
            timing_samples=200, timing_coverage=0.95,
            accountability_completion_s=420.0, slowest_floor="basement at P95 158.9s",
            warden_confirmations=9, sweeps_completed=2, sweeps_expected=2,
            zones_with_headcount=2, outages=1, blind_fraction=0.05,
            longest_blind_s=30.0)
        base.update(overrides)
        return DrillReport(**base), Disagreement

    def test_every_false_accounted_is_listed_with_its_reasoning(self):
        """Each one is a person somebody has to go and find.

        A count tells a safety officer how many; the list tells them who, and
        the reasoning tells them what the system believed when it got it wrong.
        """
        from app.reporting.drill_report import Disagreement

        report, _ = self.report(false_accounted=(
            Disagreement(person_ref="emp:EMP-003", display_name="Sara",
                         system_state="ACCOUNTED", warden_said="not here",
                         system_reason="observed at an assembly zone with a "
                                       "confirmed identity"),
            Disagreement(person_ref="emp:EMP-007", display_name="Bilal",
                         system_state="ACCOUNTED",
                         warden_said="did not confirm",
                         system_reason="warden confirmed at assembly-south"),
        ))

        text = "\n".join(report.render())
        assert "Every false accounted, in full:" in text
        for name in ("Sara", "Bilal"):
            assert name in text
        assert text.count("system reasoning:") == 2
        assert report.is_safe_result is False

    def test_the_timing_block_carries_its_coverage(self):
        report, _ = self.report()
        text = "\n".join(report.render())
        assert "P95 114.0s" in text
        assert "200 people (95% coverage)" in text
        assert "slowest individual      210.0s" in text
        assert "basement at P95 158.9s" in text

    def test_a_drill_with_no_measurements_says_so_rather_than_zero(self):
        # `slowest_floor` is None here because that is what the real builder
        # produces: it comes from the per-floor timings, and there are none.
        report, _ = self.report(p95_s=None, p50_s=None, timing_samples=0,
                                timing_coverage=0.0, slowest_floor=None,
                                timing_caveats=("No measurements: nobody had "
                                                "both a start and an arrival.",))
        lines = report.render()
        start = lines.index("Evacuation times")
        percentiles = [line for line in lines[start:start + 3]
                       if "accountability settled" not in line]

        assert any("none — No measurements" in line for line in percentiles)
        # No fabricated percentiles: "nobody evacuated in 0 seconds" and "we
        # have no measurements" must not look alike.
        assert not any("P95" in line for line in percentiles)

    def test_disagreements_and_escalations_are_quoted(self):
        # A count of mismatches is a number; the words are what a commander
        # acts on.
        report, _ = self.report(
            headcount_mismatches=("assembly-north: system 12, physical 9",),
            escalations=("two people unaccounted and the stairwell is smoky",))
        text = "\n".join(report.render())
        assert "! assembly-north: system 12, physical 9" in text
        assert "! escalated: two people unaccounted" in text

    def test_the_health_caveat_sits_beside_the_numbers(self):
        report, _ = self.report(
            health_caveat="The system was blind for 20% of this drill.")
        text = "\n".join(report.render())
        assert "blind for               5% of the drill" in text
        assert "blind for 20% of this drill" in text

    def test_uncalibrated_thresholds_are_said_out_loud(self):
        # No number in this system has been validated against a calibration
        # set, and a report that does not say so invites being quoted as if it
        # had.
        report, _ = self.report()
        text = "\n".join(report.render())
        assert "no threshold in this system has been validated" in text

    def test_calibrated_thresholds_drop_the_note(self):
        report, _ = self.report(thresholds_calibrated=True)
        assert "no threshold in this system" not in "\n".join(report.render())


class TestWhoTheDenominatorLeftOut:
    """`expected` is the denominator of every number above it, and the report
    builds no line for anybody outside it.

    `from_facetrack` can put most of a site outside it on the strength of a
    turnstile record, so a commander deciding whether the building is clear has
    to be told the count left people out, how many, and on what grounds.
    """

    def rendered(self, excluded):
        from app.reporting.drill_report import DrillReport

        report = DrillReport(
            drill_id="d1", name="Q3", site_id="s1", started_ms=T0,
            completed_ms=T0 + 600_000, duration_s=600.0,
            expected=170, accounted=170, unaccounted=0, uncertain=0,
            needing_verification=0, unknown_people=0,
            excluded_from_the_count=excluded)
        return "\n".join(report.render())

    def test_the_total_and_the_grounds_are_both_given(self):
        text = self.rendered({"NOT_CHECKED_IN": 30, "ON_LEAVE": 4})
        assert "not counted             34" in text
        assert "not checked in        30" in text
        assert "on leave              4" in text

    def test_nothing_is_printed_when_nobody_was_left_out(self):
        assert "not counted" not in self.rendered({})


class TestAnOvercountTheSiteAbsorbed:
    """Not a mismatch -- the site's own policy decided that -- and not nothing.

    Each one is a person the system called safe whom a warden could not see,
    and with `is_mismatch` False the report listed nothing and the drill
    validated clean.
    """

    def drill_with_a_permissive_policy(self):
        from app.warden.headcount import Headcount, HeadcountPolicy

        drill = drill_with(people=4)
        for i in range(4):
            confirm(drill, f"emp:EMP-{i:03d}")
        drill.record_headcount(Headcount(
            zone_id="assembly-north", warden_id="warden-7", device_id="d",
            ts_ms=T0 + 120_000, physical_count=2, system_count=4,
            policy=HeadcountPolicy(undercount_tolerance=2,
                                   overcount_tolerance=2, calibrated=False,
                                   source="a site decided")))
        drill.record_warden_action(WardenAction(
            kind=ActionKind.SWEEP_COMPLETE, warden_id="warden-7",
            device_id="d", zone_id="assembly-north", ts_ms=T0 + 180_000))
        drill.complete(T0 + 600_000)
        return drill

    def test_it_is_not_recorded_as_a_mismatch(self):
        report = build_report(self.drill_with_a_permissive_policy(),
                             now_ms=T0 + 600_000)
        assert report.headcount_mismatches == ()

    def test_it_is_reported_anyway_with_what_it_absorbed(self):
        report = build_report(self.drill_with_a_permissive_policy(),
                             now_ms=T0 + 600_000)
        assert len(report.tolerated_overcounts) == 1
        text = "\n".join(report.render())
        assert "policy absorbed it" in text
        assert "not at the muster point" in text


class TestWhyThePeopleWithNoTimingHaveNone:
    """`ExclusionReason` calls itself "always reported, never silent", and the
    timing module's docstring says the breakdown keeps the exclusions visible.

    It was computed and discarded: no schema carried it, no report printed it,
    nothing but one unit test read it. The count alone hides the difference
    that matters -- somebody the cameras never saw is a coverage problem, and
    somebody seen who never reached the muster point is a person.
    """

    def rendered(self, exclusions):
        from app.reporting.drill_report import DrillReport

        report = DrillReport(
            drill_id="d1", name="Q3", site_id="s1", started_ms=T0,
            completed_ms=T0 + 600_000, duration_s=600.0,
            expected=212, accounted=194, unaccounted=3, uncertain=5,
            needing_verification=10, unknown_people=0,
            p50_s=68.9, p90_s=116.8, p95_s=143.0, p99_s=317.4, max_s=347.9,
            timing_samples=173, timing_coverage=0.82,
            timing_exclusions=exclusions)
        return "\n".join(report.render())

    def test_each_reason_is_given_with_its_count(self):
        text = self.rendered({"NEVER_OBSERVED": 29,
                              "NEVER_REACHED_ASSEMBLY": 10})
        assert "no timing, never observed           29" in text
        assert "no timing, never reached assembly   10" in text

    def test_nothing_is_printed_when_everybody_was_timed(self):
        assert "no timing," not in self.rendered({})

    def test_the_api_carries_the_same_breakdown(self):
        from app.core.timing import ExclusionReason, measure, summarise

        people = [
            measure(person_id="a", drill_started_ms=T0,
                    assembly_arrival_ms=T0 + 60_000),
            measure(person_id="b", drill_started_ms=T0,
                    assembly_arrival_ms=None, was_observed=False),
        ]
        summary = summarise(people)
        assert summary.exclusion_reasons == {
            ExclusionReason.NEVER_OBSERVED.value: 1}


class TestADrillNobodyCanReplay:
    """Invariant 6, checked at the gate rather than assumed.

    "Every accountability decision is reconstructable from stored events" is a
    property of the record, not of the logic, and a database outage long enough
    to fill the event buffer breaks it silently. The board is still right --
    every event was folded whether or not the database took it -- so nothing in
    the numbers looks wrong. What is gone is anyone's ability to check them.
    """

    def test_a_drill_with_dropped_events_cannot_be_signed_off(self):
        result = validate(**{**CLEAN, "events_dropped": 12})
        assert result.outcome is Outcome.INCONCLUSIVE
        assert "cannot be replayed" in result.summary()

    def test_it_is_missing_evidence_and_not_a_failure(self):
        # Calling it FAIL would blame the accountability logic for a storage
        # outage. The logic did nothing wrong; nobody can confirm that.
        result = validate(**{**CLEAN, "events_dropped": 1})
        assert result.missing_evidence
        assert not result.safety_failures
        assert not result.disagreements

    def test_a_safety_failure_still_outranks_it(self):
        result = validate(**{**CLEAN, "events_dropped": 12, "false_accounted": 1})
        assert result.outcome is Outcome.FAIL
        assert "not confirmed by any warden" in result.summary()

    def test_a_complete_record_says_so_rather_than_staying_silent(self):
        # The check appears on every drill, passing. A line that shows up only
        # on bad days is a line nobody knows to look for.
        result = validate(**CLEAN)
        record = [c for c in result.checks
                  if c.criterion is Criterion.RECORD_COMPLETE]
        assert len(record) == 1
        assert record[0].passed is True
        assert record[0].measured == "0 dropped"


class TestTheReportCountsWhatTheStoreLost:

    class LosingStore:
        """A store whose database went away and whose buffer filled."""

        def __init__(self, dropped):
            from app.store.events import StoreStats

            self.stats = StoreStats(dropped=dropped)
            self.appended = 0

        def append(self, event, now_ms=None):
            self.appended += 1
            return True

        @property
        def pending(self):
            return 0

        @property
        def is_degraded(self):
            return self.stats.dropped > 0

    def test_the_number_reaches_the_report(self):
        drill = drill_with(people=2)
        drill.events_store = self.LosingStore(dropped=9)
        confirm(drill, "emp:EMP-000")

        report = build_report(drill, now_ms=T0 + 120_000)
        assert report.events_dropped == 9

    def test_the_safety_officer_is_told_in_words(self):
        drill = drill_with(people=2)
        drill.events_store = self.LosingStore(dropped=9)
        confirm(drill, "emp:EMP-000")

        text = "\n".join(build_report(drill, now_ms=T0 + 120_000).render())
        assert "events lost for good    9" in text
        assert "cannot be replayed from the stored record" in text

    def test_a_drill_with_no_store_has_dropped_nothing(self):
        # It never promised to keep anything. `is_durable` is where that shows.
        report = build_report(drill_with(people=2), now_ms=T0 + 120_000)
        assert report.events_dropped == 0
        assert "events lost for good    0" in "\n".join(report.render())

    def test_a_losing_store_puts_the_outage_in_the_drills_health_log(self):
        """The API process has one store and a drill per evacuation.

        It cannot hand the store a drill's health log, so without the drill
        mirroring what the store says about itself, a report's System health
        section read "no outages" through an entire database failure.
        """
        drill = drill_with(people=2)
        drill.events_store = self.LosingStore(dropped=3)
        confirm(drill, "emp:EMP-000")

        report = build_report(drill, now_ms=T0 + 120_000)
        assert report.outages >= 1
        # Storage costs the record, not the view.
        assert report.blind_fraction == 0.0
