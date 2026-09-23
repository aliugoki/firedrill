"""What makes a drill a pass, and what makes it unable to say.

Three outcomes, and the third is the one that matters most.

    PASS           the system agreed with the humans, and there was enough
                   evidence for that agreement to mean something
    FAIL           the system disagreed with the humans in a way that matters
    INCONCLUSIVE   the drill did not produce enough evidence to judge

An INCONCLUSIVE drill is not a soft pass. A run where the cameras were blind for
half the time, or where no warden completed a sweep, cannot validate the system
even if every number on the board looks perfect — there was nothing to check the
board against. Reporting that as a pass is how a system accumulates a record of
successful drills that proves nothing.

**The manual roll-call is the reference, not the system.** In a real building
nobody has a list of where everyone actually went; the warden walking the
assembly point with their eyes is the closest thing to one. So "false accounted"
means the system said safe and a human said otherwise, and that asymmetry is
deliberate: when they disagree, the human is right by definition.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Outcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"


class Criterion(str, Enum):
    """What each check is about. Ordered by how much it matters."""

    NO_FALSE_ACCOUNTED = "NO_FALSE_ACCOUNTED"
    """The system marked someone safe whom a warden did not confirm. The only
    criterion whose failure is a safety failure rather than a quality one."""

    RECORD_COMPLETE = "RECORD_COMPLETE"
    """Events were dropped, so the drill cannot be replayed from what was
    stored. Invariant 6 does not hold for this run, and a validation nobody can
    re-derive is not a validation."""

    SWEEPS_COMPLETED = "SWEEPS_COMPLETED"
    HEADCOUNTS_TAKEN = "HEADCOUNTS_TAKEN"
    HEADCOUNTS_AGREE = "HEADCOUNTS_AGREE"
    COVERAGE_SUFFICIENT = "COVERAGE_SUFFICIENT"
    SYSTEM_MOSTLY_SIGHTED = "SYSTEM_MOSTLY_SIGHTED"
    P95_MEASURABLE = "P95_MEASURABLE"
    P95_WITHIN_TARGET = "P95_WITHIN_TARGET"
    ACCOUNTED_VERIFIED = "ACCOUNTED_VERIFIED"


#: Criteria that, if not satisfied, mean the drill cannot judge the system at
#: all. Distinct from failing: there was nothing to check against.
#:
#: `P95_MEASURABLE` belongs here and was missing. Too few measurements for a
#: percentile to mean anything is the definition of not having enough evidence,
#: and while it counted as a failure a ten-person office reported FAIL on a
#: drill where nobody was falsely accounted, every zone was swept and counted,
#: the counts agreed, coverage was total and the cameras never blinked. No
#: number of good drills could have changed it, because the site is smaller
#: than the sample a 95th percentile needs. Reporting that as a failure of the
#: system says something untrue about the system.
#:
#: It only ever decides the outcome alone on a small roster. Too few samples
#: from a large one means people went untracked, and `COVERAGE_SUFFICIENT`
#: fails too -- also evidentiary, same verdict.
#: `RECORD_COMPLETE` is evidentiary rather than a failure, and the distinction
#: is worth being precise about. Dropping events does not mean the system got
#: anything wrong -- the board was folded from every event, whether or not the
#: database took it -- so calling it FAIL would blame the accountability logic
#: for a storage outage. What it means is that nobody can check. A drill signed
#: off on numbers that cannot be re-derived from the stored record is a drill
#: whose sign-off rests on the report being believed.
EVIDENTIARY: frozenset[Criterion] = frozenset({
    Criterion.SWEEPS_COMPLETED, Criterion.HEADCOUNTS_TAKEN,
    Criterion.COVERAGE_SUFFICIENT, Criterion.SYSTEM_MOSTLY_SIGHTED,
    Criterion.P95_MEASURABLE, Criterion.RECORD_COMPLETE,
    #: Somebody the cameras accounted for whom no warden laid eyes on has not
    #: been checked. That is missing evidence, and it used to be counted as a
    #: safety failure -- which made every drill with an unswept zone a FAIL
    #: rather than INCONCLUSIVE, because rule 1 of the decision order
    #: short-circuits rule 3.
    Criterion.ACCOUNTED_VERIFIED,
})

#: The one whose failure is a safety failure.
SAFETY_CRITICAL: frozenset[Criterion] = frozenset({Criterion.NO_FALSE_ACCOUNTED})

#: Criteria whose failure is a disagreement that actually happened, rather than
#: a judgement the drill was not equipped to make.
#:
#: These outrank missing evidence. The wardens counted, the system counted, and
#: the two differed -- that is true whether or not the drill was large enough to
#: support a percentile or complete enough to check anything else. Reporting
#: INCONCLUSIVE there buries the second most important signal a drill produces
#: behind "not enough evidence", when the evidence in question is exactly what
#: was collected.
#:
#: `P95_WITHIN_TARGET` is deliberately not here: a target that could not be
#: measured is not a target that was missed.
DISAGREEMENT: frozenset[Criterion] = frozenset({Criterion.HEADCOUNTS_AGREE})


@dataclass(frozen=True, slots=True)
class Thresholds:
    """What "enough" means. Configuration, and unvalidated until a real drill.

    `max_blind_fraction` is the one to argue about. A drill where the system
    could not see for a third of the time is not a test of the system; it is a
    test of the wardens. Set it low.
    """

    target_p95_s: float = 120.0
    min_sweep_completion: float = 1.0
    min_coverage: float = 0.9
    max_blind_fraction: float = 0.10
    min_timing_samples: int = 20
    calibrated: bool = False
    source: str = "Phase 5 default, no live drill has run"


@dataclass(frozen=True, slots=True)
class Check:
    criterion: Criterion
    passed: bool
    detail: str
    measured: str | None = None

    @property
    def is_evidentiary(self) -> bool:
        return self.criterion in EVIDENTIARY

    @property
    def is_safety_critical(self) -> bool:
        return self.criterion in SAFETY_CRITICAL

    @property
    def is_disagreement(self) -> bool:
        return self.criterion in DISAGREEMENT


@dataclass(frozen=True, slots=True)
class Validation:
    outcome: Outcome
    checks: tuple = ()
    thresholds: Thresholds = field(default_factory=Thresholds)

    @property
    def failed(self) -> tuple:
        return tuple(c for c in self.checks if not c.passed)

    @property
    def safety_failures(self) -> tuple:
        return tuple(c for c in self.failed if c.is_safety_critical)

    @property
    def missing_evidence(self) -> tuple:
        return tuple(c for c in self.failed if c.is_evidentiary)

    @property
    def disagreements(self) -> tuple:
        return tuple(c for c in self.failed if c.is_disagreement)

    def summary(self) -> str:
        if self.outcome is Outcome.FAIL:
            if self.safety_failures:
                return (f"FAIL — {self.safety_failures[0].detail}")
            if self.disagreements:
                return f"FAIL — {self.disagreements[0].detail}"
            return f"FAIL — {self.failed[0].detail}"
        if self.outcome is Outcome.INCONCLUSIVE:
            return ("INCONCLUSIVE — this drill cannot judge the system: "
                    + self.missing_evidence[0].detail)
        return "PASS"

    def describe(self) -> list[str]:
        lines = [self.summary(), ""]
        for check in self.checks:
            mark = "PASS" if check.passed else "FAIL"
            measured = f"  [{check.measured}]" if check.measured else ""
            lines.append(f"  {mark}  {check.criterion.value}{measured}")
            lines.append(f"        {check.detail}")
        if not self.thresholds.calibrated:
            lines += ["", f"Thresholds are not validated: {self.thresholds.source}"]
        return lines


def validate(
    *,
    false_accounted: int,
    accounted_unverified: int = 0,
    accounted_total: int = 0,
    false_unaccounted: int,
    sweeps_completed: int,
    sweeps_expected: int,
    zones_with_headcount: int,
    headcount_mismatches: int,
    timing_samples: int,
    timing_coverage: float | None,
    p95_s: float | None,
    p95_reliable: bool,
    clock_corrected: bool = False,
    blind_fraction: float,
    events_dropped: int = 0,
    thresholds: Thresholds | None = None,
) -> Validation:
    """Judge a drill.

    The order of the decision matters as much as the checks. A safety failure is
    a FAIL whatever else happened. Missing evidence is INCONCLUSIVE, even when
    every other number looks good, because there was nothing to check against.
    Only a drill that produced enough evidence and agreed with the humans passes.
    """
    thresholds = thresholds or Thresholds()
    checks: list[Check] = []

    checks.append(Check(
        criterion=Criterion.NO_FALSE_ACCOUNTED,
        passed=false_accounted == 0,
        measured=str(false_accounted),
        # Contradictions only. The wording used to say "not confirmed by any
        # warden", which described the far commoner case of a warden simply not
        # having got there yet -- and counted it as the error that ends the
        # assessment.
        detail=("no warden contradicted anybody the system marked accounted"
                if false_accounted == 0 else
                f"{false_accounted} person(s) were marked accounted by the "
                "system and a warden said they were not there"),
    ))

    checks.append(Check(
        criterion=Criterion.ACCOUNTED_VERIFIED,
        passed=accounted_unverified == 0,
        measured=f"{accounted_unverified} of {accounted_total}",
        detail=("every accounted person was confirmed by a warden"
                if accounted_unverified == 0 else
                f"{accounted_unverified} of {accounted_total} accounted "
                "people were accounted by camera and confirmed by no warden, "
                "so the roll-call has not checked them"),
    ))

    checks.append(Check(
        criterion=Criterion.RECORD_COMPLETE,
        passed=events_dropped == 0,
        measured=f"{events_dropped} dropped",
        detail=("every event this drill produced reached the database"
                if events_dropped == 0 else
                f"{events_dropped} event(s) were dropped while the database was "
                "unreachable and the buffer was full; this drill cannot be "
                "replayed from what was stored, so no number in this report can "
                "be independently re-derived"),
    ))

    completion = (sweeps_completed / sweeps_expected) if sweeps_expected else 0.0
    checks.append(Check(
        criterion=Criterion.SWEEPS_COMPLETED,
        passed=sweeps_expected > 0 and completion >= thresholds.min_sweep_completion,
        measured=f"{sweeps_completed}/{sweeps_expected}",
        detail=("every zone was swept"
                if sweeps_expected and completion >= thresholds.min_sweep_completion
                else "not every zone was swept, so there is no independent "
                     "check on the system's counts"),
    ))

    checks.append(Check(
        criterion=Criterion.HEADCOUNTS_TAKEN,
        passed=sweeps_expected > 0 and zones_with_headcount >= sweeps_expected,
        measured=f"{zones_with_headcount}/{sweeps_expected}",
        detail=("every zone recorded a physical headcount"
                if sweeps_expected and zones_with_headcount >= sweeps_expected
                else "a zone recorded no physical count, so the system was only "
                     "checked against its own list"),
    ))

    checks.append(Check(
        criterion=Criterion.HEADCOUNTS_AGREE,
        passed=headcount_mismatches == 0,
        measured=str(headcount_mismatches),
        detail=("every physical count agreed with the system"
                if headcount_mismatches == 0 else
                f"{headcount_mismatches} zone(s) disagreed with the system"),
    ))

    coverage = timing_coverage if timing_coverage is not None else 0.0
    checks.append(Check(
        criterion=Criterion.COVERAGE_SUFFICIENT,
        passed=coverage >= thresholds.min_coverage,
        measured=f"{coverage:.0%}",
        detail=(f"{coverage:.0%} of people were tracked end to end"
                if coverage >= thresholds.min_coverage else
                f"only {coverage:.0%} of people were tracked from alarm to "
                f"arrival; {1 - coverage:.0%} produced no timing at all, and a "
                "percentile that improves by losing the slow people is the "
                "easiest way to fake this number"),
    ))

    checks.append(Check(
        criterion=Criterion.SYSTEM_MOSTLY_SIGHTED,
        passed=blind_fraction <= thresholds.max_blind_fraction,
        measured=f"{blind_fraction:.0%}",
        detail=(f"the system could see for {1 - blind_fraction:.0%} of the drill"
                if blind_fraction <= thresholds.max_blind_fraction else
                f"the system was blind for {blind_fraction:.0%} of the drill; "
                "this is a test of the wardens, not of the system"),
    ))

    checks.append(Check(
        criterion=Criterion.P95_MEASURABLE,
        passed=(p95_s is not None and p95_reliable
                and timing_samples >= thresholds.min_timing_samples),
        measured=f"{timing_samples} samples",
        # The two ways this fails are unrelated and the wrong one sends a
        # reader looking for people. A clock correction is not a shortage of
        # measurements: it is measurements that are not measurements, and a
        # hundred-sample drill told "100 measurements is too few" is a report
        # arguing with itself.
        detail=("the sample supports a 95th percentile"
                if p95_s is not None and p95_reliable else
                "the node's clock was corrected during the drill, so the "
                "durations are not durations" if clock_corrected else
                f"{timing_samples} measurements is too few for a 95th "
                "percentile to mean anything"),
    ))

    checks.append(Check(
        criterion=Criterion.P95_WITHIN_TARGET,
        passed=p95_s is not None and p95_s <= thresholds.target_p95_s,
        measured=f"{p95_s:.1f}s" if p95_s is not None else "not measured",
        detail=(f"P95 {p95_s:.1f}s against a {thresholds.target_p95_s:.0f}s target"
                if p95_s is not None else
                "no P95 was produced, so the target cannot be assessed"),
    ))

    result = tuple(checks)
    failed = [c for c in result if not c.passed]
    safety = [c for c in failed if c.is_safety_critical]
    disagreed = [c for c in failed if c.is_disagreement]
    evidentiary = [c for c in failed if c.is_evidentiary]

    if safety or disagreed:
        # A disagreement that happened is not an absence of evidence, so it is
        # checked before the evidence gate rather than behind it.
        outcome = Outcome.FAIL
    elif evidentiary:
        # Not a soft pass. There was nothing to check the system against.
        outcome = Outcome.INCONCLUSIVE
    elif failed:
        outcome = Outcome.FAIL
    else:
        outcome = Outcome.PASS

    return Validation(outcome=outcome, checks=result, thresholds=thresholds)
