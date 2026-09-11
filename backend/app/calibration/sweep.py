"""Threshold sweeps, error rates, and choosing an operating point.

The output of a sweep is not "the best threshold". It is the trade-off curve and
an explicit statement of which error was preferred, because in an evacuation the
two errors are not symmetric and pretending otherwise is how a threshold ends up
chosen by whichever number looked nicer.

    A **false accept** attaches an employee's name to the wrong person. It can
    mark someone safe who is still inside. It is the error that kills.

    A **false reject** leaves a person unidentified. They become UNCERTAIN, a
    warden walks over and looks at them, and the drill takes longer.

So the default operating point is chosen by holding the false-accept rate at or
below a stated ceiling and taking the best true-accept rate available under that
constraint — not by maximising accuracy, and not by any single-number score that
would let one error trade freely against the other.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.calibration.dataset import CalibrationSet, LabelledObservation, Split
from app.core.identity_fsm import IdentityConfig, RejectionReason, gate
from app.core.identity_fsm import FaceObservation


@dataclass(frozen=True, slots=True)
class ThresholdOutcome:
    """What one threshold pair did to a whole set.

    `false_accepts` counts only the dangerous case: an observation admitted with
    the *wrong* name on it. An observation admitted with no name, or rejected,
    cannot mislabel anybody.
    """

    score_threshold: float
    min_margin: float
    true_accepts: int
    false_accepts: int
    false_rejects: int
    true_rejects: int
    admitted_unknown: int

    @property
    def total(self) -> int:
        return (self.true_accepts + self.false_accepts
                + self.false_rejects + self.true_rejects)

    @property
    def true_accept_rate(self) -> float | None:
        """Of the enrolled people we could have identified, how many did we?

        `admitted_unknown` is excluded from the denominator, and used to not
        be. An unenrolled stranger given somebody's name is not an enrolled
        person we failed to identify -- there was no right answer to get -- so
        counting them dragged this number down in proportion to how many
        strangers walked past the camera, which is a property of the crowd
        rather than of the matcher. Eighty of a hundred enrolled people named
        correctly read as 62% when thirty strangers were also admitted.

        It biased `choose` too: a looser threshold admits more strangers, so
        the penalty grew with looseness and pushed the selection toward
        stricter pairs for a reason nobody intended. `false_accept_rate` and
        `unknown_accept_rate` are where those admissions belong, and both
        already count them.
        """
        wrongly_named = self.false_accepts - self.admitted_unknown
        eligible = self.true_accepts + self.false_rejects + wrongly_named
        return self.true_accepts / eligible if eligible else None

    @property
    def false_accept_rate(self) -> float | None:
        """Of everything admitted, how much carried the wrong name?

        Denominated on admissions rather than on the whole set on purpose. "One
        in a thousand observations was wrong" sounds harmless; "one in twenty
        admitted identities was wrong" is the number an operator needs.
        """
        admitted = self.true_accepts + self.false_accepts
        return self.false_accepts / admitted if admitted else None

    @property
    def unknown_accept_rate(self) -> float | None:
        """How often an unenrolled person was given an employee's name.

        The purest measure of the dangerous error, because the true answer is
        unambiguous: nobody.
        """
        unknown_total = self.admitted_unknown + self.true_rejects
        return self.admitted_unknown / unknown_total if unknown_total else None


def evaluate(
    calibration_set: CalibrationSet, config: IdentityConfig
) -> ThresholdOutcome:
    """Run one configuration over a labelled set."""
    true_accepts = false_accepts = false_rejects = true_rejects = 0
    admitted_unknown = 0

    for item in calibration_set.observations:
        admitted = gate(_as_observation(item), config) is RejectionReason.ACCEPTED
        if not admitted:
            if item.is_enrolled:
                false_rejects += 1
            else:
                true_rejects += 1
            continue
        if not item.is_enrolled:
            admitted_unknown += 1
            false_accepts += 1
        elif item.proposed_identity == item.true_identity:
            true_accepts += 1
        else:
            false_accepts += 1

    return ThresholdOutcome(
        score_threshold=config.score_threshold, min_margin=config.min_margin,
        true_accepts=true_accepts, false_accepts=false_accepts,
        false_rejects=false_rejects, true_rejects=true_rejects,
        admitted_unknown=admitted_unknown)


def _as_observation(item: LabelledObservation) -> FaceObservation:
    return FaceObservation(
        ts_ms=0, candidate_id=item.proposed_identity, score=item.score,
        margin=item.margin, quality=item.quality,
        pose_deviation_deg=item.pose_deviation_deg,
        track_confidence=item.track_confidence,
        association_is_strong=item.association_is_strong)


@dataclass(frozen=True, slots=True)
class OperatingPoint:
    """A chosen configuration, with the evidence for choosing it."""

    config: IdentityConfig
    on_tune: ThresholdOutcome
    on_validate: ThresholdOutcome | None
    ceiling: float
    rationale: str
    #: Whether the chosen pair sits on the edge of the grid that was searched.
    #: A point on the boundary means the best pair may lie outside where anyone
    #: looked, so the number is a limit of the search rather than an optimum.
    #: Reported rather than corrected: widening the grid automatically would
    #: hide that the first attempt was aimed wrongly.
    on_grid_boundary: tuple[str, ...] = ()

    @property
    def generalises(self) -> bool | None:
        """Whether the held-out half agrees the ceiling was respected.

        None when there is no held-out half, which is itself the answer: without
        one, nothing is known about generalisation.
        """
        if self.on_validate is None:
            return None
        rate = self.on_validate.false_accept_rate
        return rate is not None and rate <= self.ceiling

    @property
    def drift(self) -> float | None:
        """How much worse the false-accept rate got on unseen people.

        Large drift means the threshold was fitted to the tune half. It is
        reported rather than corrected: correcting it against the validate half
        would consume the only independent measurement available.
        """
        if self.on_validate is None:
            return None
        tuned = self.on_tune.false_accept_rate
        held = self.on_validate.false_accept_rate
        if tuned is None or held is None:
            return None
        return held - tuned


@dataclass
class Sweep:
    """A grid over score threshold and margin, and the choice made from it."""

    split: Split
    base_config: IdentityConfig
    results: list[ThresholdOutcome] = field(default_factory=list)

    #: The grid the last `run` covered, so `choose` can say when its answer sat
    #: on the edge of it.
    searched: dict = field(default_factory=dict)

    def run(
        self, *, score_range: tuple[float, float, float] = (0.20, 0.75, 0.025),
        margin_range: tuple[float, float, float] = (0.0, 0.30, 0.01),
    ) -> "Sweep":
        from dataclasses import replace

        self.results = []
        self.searched = {"score": (score_range[0], score_range[1]),
                         "margin": (margin_range[0], margin_range[1])}
        for score in _steps(*score_range):
            for margin in _steps(*margin_range):
                config = replace(self.base_config, score_threshold=score,
                                 min_margin=margin)
                self.results.append(evaluate(self.split.tune, config))
        return self

    def choose(self, *, false_accept_ceiling: float = 0.01) -> OperatingPoint:
        """Best true-accept rate subject to a false-accept ceiling.

        Raises if nothing clears the ceiling. That is the honest outcome: it
        means this pipeline cannot hit the required accuracy on this data, and
        the answer is better enrolment or better cameras, not a looser ceiling
        chosen after the fact to make the report pass.
        """
        from dataclasses import replace

        if not self.results:
            raise ValueError("run the sweep before choosing an operating point")

        eligible = [
            r for r in self.results
            if r.false_accept_rate is not None
            and r.false_accept_rate <= false_accept_ceiling
            and r.true_accept_rate is not None
        ]
        if not eligible:
            best = min(
                (r for r in self.results if r.false_accept_rate is not None),
                key=lambda r: r.false_accept_rate, default=None)
            achieved = f"{best.false_accept_rate:.3f}" if best else "unknown"
            raise NoAcceptableOperatingPoint(
                f"no threshold pair holds the false-accept rate at or below "
                f"{false_accept_ceiling:.3f}; the best achievable on this data "
                f"is {achieved}. Loosening the ceiling to make this pass would "
                f"be choosing the number after seeing the answer."
            )

        # Prefer the highest true-accept rate; break ties toward the stricter
        # threshold, because among equals the more conservative one is the one
        # that degrades more gracefully as conditions get worse than the
        # calibration set.
        best = max(eligible, key=lambda r: (r.true_accept_rate,
                                            r.score_threshold, r.min_margin))
        config = replace(
            self.base_config, score_threshold=best.score_threshold,
            min_margin=best.min_margin)

        validate_outcome = (
            evaluate(self.split.validate, config)
            if len(self.split.validate) else None)

        return OperatingPoint(
            config=config, on_tune=best, on_validate=validate_outcome,
            ceiling=false_accept_ceiling,
            rationale=(
                f"highest true-accept rate ({best.true_accept_rate:.3f}) among "
                f"{len(eligible)} threshold pairs holding false accepts at or "
                f"below {false_accept_ceiling:.3f}"),
            on_grid_boundary=self._boundary(best),
        )

    def _boundary(self, best: ThresholdOutcome) -> tuple[str, ...]:
        """Which axes the chosen pair sits on the edge of."""
        axes = (("score", "score_threshold", best.score_threshold),
                ("margin", "min_margin", best.min_margin))
        edges = []
        for key, name, value in axes:
            bounds = self.searched.get(key)
            if not bounds:
                continue
            low, high = bounds
            if abs(value - low) < 1e-9:
                edges.append(f"{name} chose the lowest value searched ({low})")
            elif abs(value - high) < 1e-9:
                edges.append(f"{name} chose the highest value searched ({high})")
        return tuple(edges)

    def curve(self) -> list[tuple[float, float]]:
        """(false-accept rate, true-accept rate) pairs, for plotting."""
        return sorted(
            (r.false_accept_rate, r.true_accept_rate) for r in self.results
            if r.false_accept_rate is not None and r.true_accept_rate is not None)


class NoAcceptableOperatingPoint(ValueError):
    """No threshold met the required accuracy. Not a bug: a finding."""


def _steps(start: float, stop: float, step: float) -> list[float]:
    if step <= 0:
        raise ValueError("step must be positive")
    out, value = [], start
    while value <= stop + 1e-9:
        out.append(round(value, 6))
        value += step
    return out
