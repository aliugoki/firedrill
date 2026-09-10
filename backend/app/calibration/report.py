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
from app.calibration.sweep import OperatingPoint, Sweep
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
    point = sweep.choose(false_accept_ceiling=false_accept_ceiling)
    refusals: list[str] = []
    caveats: list[str] = []

    combined = CalibrationSet(
        observations=list(sweep.split.tune.observations)
        + list(sweep.split.validate.observations),
        name="combined")
    readiness = combined.readiness()
    refusals.extend(readiness.problems)
    caveats.extend(readiness.warnings)

    if not sweep.split.is_clean:
        refusals.append(
            "the same person appears in both halves of the split, so the "
            "held-out measurement is not independent")

    if point.on_validate is None:
        refusals.append("no held-out half, so generalisation is unmeasured")
    else:
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
