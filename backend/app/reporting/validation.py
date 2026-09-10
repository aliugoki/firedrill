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

    SWEEPS_COMPLETED = "SWEEPS_COMPLETED"
    HEADCOUNTS_TAKEN = "HEADCOUNTS_TAKEN"
    HEADCOUNTS_AGREE = "HEADCOUNTS_AGREE"
    COVERAGE_SUFFICIENT = "COVERAGE_SUFFICIENT"
    SYSTEM_MOSTLY_SIGHTED = "SYSTEM_MOSTLY_SIGHTED"
    P95_MEASURABLE = "P95_MEASURABLE"
    P95_WITHIN_TARGET = "P95_WITHIN_TARGET"


#: Criteria that, if not satisfied, mean the drill cannot judge the system at
#: all. Distinct from failing: there was nothing to check against.
EVIDENTIARY: frozenset[Criterion] = frozenset({
    Criterion.SWEEPS_COMPLETED, Criterion.HEADCOUNTS_TAKEN,
    Criterion.COVERAGE_SUFFICIENT, Criterion.SYSTEM_MOSTLY_SIGHTED,
})

#: The one whose failure is a safety failure.
SAFETY_CRITICAL: frozenset[Criterion] = frozenset({Criterion.NO_FALSE_ACCOUNTED})


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

    def summary(self) -> str:
        if self.outcome is Outcome.FAIL:
            if self.safety_failures:
                return (f"FAIL — {self.safety_failures[0].detail}")
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
    false_unaccounted: int,
    sweeps_completed: int,
    sweeps_expected: int,
    zones_with_headcount: int,
    headcount_mismatches: int,
    timing_samples: int,
    timing_coverage: float | None,
    p95_s: float | None,
    p95_reliable: bool,
    blind_fraction: float,
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
        detail=("nobody was marked accounted whom a warden did not confirm"
                if false_accounted == 0 else
                f"{false_accounted} person(s) were marked accounted by the "
                "system and not confirmed by any warden"),
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
        detail=("the sample supports a 95th percentile"
                if p95_s is not None and p95_reliable else
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
    safety = [c for c in result if not c.passed and c.is_safety_critical]
    evidentiary = [c for c in result if not c.passed and c.is_evidentiary]

    if safety:
        outcome = Outcome.FAIL
    elif evidentiary:
        # Not a soft pass. There was nothing to check the system against.
        outcome = Outcome.INCONCLUSIVE
    elif any(not c.passed for c in result):
        outcome = Outcome.FAIL
    else:
        outcome = Outcome.PASS

    return Validation(outcome=outcome, checks=result, thresholds=thresholds)
