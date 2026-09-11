"""The calibration report, and the caveats that belong beside every number.

A report that prints thresholds without saying what they were measured on is
worse than no report: it launders a guess into a validated figure. Everything
here is built so that a number cannot appear without its provenance.

`certify` is the only route to a config marked `calibrated=True`, and it refuses
in every case where the claim would not be true.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from app.calibration.dataset import CalibrationSet, Source, Split
from app.calibration.sweep import (
    NoAcceptableOperatingPoint,
    OperatingPoint,
    Sweep,
    evaluate,
)
from app.core.identity_fsm import IdentityConfig


class NotCertifiable(ValueError):
    """The evidence does not support calling these thresholds calibrated."""


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    operating_point: OperatingPoint
    split: Split
    certified: bool
    refusals: tuple[str, ...]
    caveats: tuple[str, ...]

    @property
    def config(self) -> IdentityConfig:
        return self.operating_point.config

    def render(self) -> list[str]:
        point = self.operating_point
        tune, validate = point.on_tune, point.on_validate
        lines = [
            "EVAC-120 identity threshold calibration",
            "",
            f"  score_threshold  {point.config.score_threshold:.3f}",
            f"  min_margin       {point.config.min_margin:.3f}",
            f"  certified        {'YES' if self.certified else 'NO'}",
            "",
            "Chosen because:",
            f"  {point.rationale}",
            "",
            "Measured on the tuning half:",
            f"  observations       {tune.total}",
            f"  true-accept rate   {_pct(tune.true_accept_rate)}",
            f"  false-accept rate  {_pct(tune.false_accept_rate)}",
            f"  unknown accepted   {_pct(tune.unknown_accept_rate)}",
        ]
        if validate is not None:
            lines += [
                "",
                f"Measured on {len(self.split.held_out_people)} held-out people:",
                f"  observations       {validate.total}",
                f"  true-accept rate   {_pct(validate.true_accept_rate)}",
                f"  false-accept rate  {_pct(validate.false_accept_rate)}",
                f"  unknown accepted   {_pct(validate.unknown_accept_rate)}",
                f"  holds the ceiling  {point.generalises}",
                f"  drift              {_delta(point.drift)}",
            ]
        else:
            lines += ["", "No held-out half: generalisation is unmeasured."]

        if self.refusals:
            lines += ["", "Not certified because:"]
            lines += [f"  - {r}" for r in self.refusals]
        if self.caveats:
            lines += ["", "Caveats:"]
            lines += [f"  - {c}" for c in self.caveats]
        return lines


def _pct(value: float | None) -> str:
    return "not measurable" if value is None else f"{value * 100:.2f}%"


def _delta(value: float | None) -> str:
    if value is None:
        return "not measurable"
    return f"{value * 100:+.2f} points"


def _no_operating_point(sweep: Sweep, ceiling: float) -> OperatingPoint:
    """The closest the sweep got, so a reader can see how far short it fell.

    Not a recommendation. Its config stays uncalibrated and the report is
    refused; this exists only so "no threshold works" comes with the number.
    """
    measurable = [r for r in sweep.results if r.false_accept_rate is not None]
    best = (min(measurable, key=lambda r: r.false_accept_rate)
            if measurable else None)
    from dataclasses import replace

    config = replace(sweep.base_config, calibrated=False,
                     source="no threshold pair met the required accuracy")
    if best is None:
        return OperatingPoint(
            config=config, on_tune=_empty_outcome(), on_validate=None,
            ceiling=ceiling,
            rationale="the sweep produced no measurable result at all")

    config = replace(config, score_threshold=best.score_threshold,
                     min_margin=best.min_margin)
    return OperatingPoint(
        config=config, on_tune=best,
        on_validate=(evaluate(sweep.split.validate, config)
                     if len(sweep.split.validate) else None),
        ceiling=ceiling,
        rationale=(f"closest available: false accepts "
                   f"{best.false_accept_rate:.3f} against a {ceiling:.3f} "
                   "ceiling. Shown so the shortfall is visible, not as a "
                   "recommendation"))


def _empty_outcome():
    from app.calibration.sweep import ThresholdOutcome

    return ThresholdOutcome(score_threshold=0.0, min_margin=0.0, true_accepts=0,
                   false_accepts=0, false_rejects=0, true_rejects=0,
                   admitted_unknown=0)


#: The most drift tolerated between the tuning and held-out halves before the
#: thresholds are treated as fitted to the tuning data rather than measured.
MAX_DRIFT = 0.02


def certify(
    sweep: Sweep, *, false_accept_ceiling: float = 0.01,
    require_real_footage: bool = True,
) -> CalibrationReport:
    """Produce a report, and certify the thresholds only if the evidence allows.

    Certification fails, rather than warns, on any of:

      * an unusable calibration set;
      * a split that leaked a person across both halves;
      * no held-out half at all;
      * a held-out false-accept rate above the ceiling;
      * drift beyond `MAX_DRIFT`, which means the thresholds were fitted;
      * a set made only of simulated data, when real footage is required.

    An uncertified report is still worth producing and reading. It just may not
    set `calibrated=True`, and nothing downstream may present its numbers as
    validated.
    """
    refusals: list[str] = []
    caveats: list[str] = []

    try:
        point = sweep.choose(false_accept_ceiling=false_accept_ceiling)
    except NoAcceptableOperatingPoint as exc:
        # A finding, not a crash. The docstring promises an uncertified report
        # is still worth reading, and a caller who cannot see how far short the
        # pipeline fell has nothing to act on: "it raised" does not say whether
        # the answer was 1.1% or 40%.
        #
        # Collected rather than returned early, so the other refusals still
        # appear. A set that is both simulated and short of the ceiling has two
        # problems, and reporting one hides the other.
        point = _no_operating_point(sweep, false_accept_ceiling)
        refusals.append(str(exc))

    combined = CalibrationSet(
        observations=list(sweep.split.tune.observations)
        + list(sweep.split.validate.observations),
        name="combined")
    readiness = combined.readiness()
    refusals.extend(readiness.problems)
    caveats.extend(readiness.warnings)

    if point.on_grid_boundary:
        # A caveat rather than a refusal: the measurement is sound, the search
        # may not have been. A pair on the edge of the grid means the best pair
        # could lie outside where anyone looked, and whoever signs these
        # thresholds off should know the number is a limit of the search rather
        # than an optimum.
        for edge in point.on_grid_boundary:
            caveats.append(
                f"{edge}, so a better pair may lie outside the range searched")

    if not sweep.split.is_clean:
        refusals.append(
            "the same person appears in both halves of the split, so the "
            "held-out measurement is not independent")

    if point.on_validate is None:
        refusals.append("no held-out half, so generalisation is unmeasured")
    elif not refusals or "no threshold pair" not in refusals[0]:
        if point.generalises is False:
            refusals.append(
                f"on held-out people the false-accept rate is "
                f"{_pct(point.on_validate.false_accept_rate)}, above the "
                f"{false_accept_ceiling * 100:.2f}% ceiling")
        drift = point.drift
        if drift is not None and drift > MAX_DRIFT:
            refusals.append(
                f"the false-accept rate drifts {_delta(drift)} on unseen "
                f"people, above the {MAX_DRIFT * 100:.0f}-point limit; these "
                "thresholds are fitted to the tuning half, not measured")

    sources = {o.source for o in combined.observations}
    if require_real_footage and sources <= {Source.SIMULATOR}:
        refusals.append(
            "the set is entirely simulated; thresholds for a live drill must be "
            "measured on recorded footage from the site they will run on")

    certified = not refusals
    config = replace(
        point.config,
        calibrated=certified,
        source=(f"calibration on {combined.name}, "
                f"{len(combined)} observations, "
                f"{len(combined.enrolled_people)} enrolled people"
                if certified else "uncertified calibration run"),
    )
    return CalibrationReport(
        operating_point=replace(point, config=config), split=sweep.split,
        certified=certified, refusals=tuple(refusals), caveats=tuple(caveats))
