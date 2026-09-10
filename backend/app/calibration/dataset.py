"""Labelled observations, and the split that stops a threshold fitting noise.

Every observation carries the identity the pipeline proposed and the identity
that was actually true. That pairing is the whole dataset; everything else is
arithmetic over it.

**The tune/validate split is not optional.** A threshold chosen on the same data
it is measured on will look excellent and generalise badly, and the failure mode
is silent: the calibration report shows a 99% true-positive rate and the drill
shows people being misidentified. `split` enforces it, and `Sweep` refuses to
report a validated operating point without both halves.

The split is **by person, not by observation**. The same face appears in dozens
of frames; splitting by frame puts near-duplicates of one person on both sides
and leaks the answer across the boundary, which makes the validate half agree
with the tune half for reasons that have nothing to do with the threshold.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum


#: The person key shared by everyone who is not in the gallery.
UNKNOWN_KEY = "__unknown__"


class Source(str, Enum):
    """Where a labelled observation came from. Recorded so a report can say."""

    SIMULATOR = "SIMULATOR"
    RECORDED_DRILL = "RECORDED_DRILL"
    LIVE_DRILL = "LIVE_DRILL"
    ENROLMENT_GALLERY = "ENROLMENT_GALLERY"


@dataclass(frozen=True, slots=True)
class LabelledObservation:
    """One face match with the truth beside it.

    `true_identity` is None when the person genuinely is not in the gallery — a
    visitor, a contractor, a member of the public. Those are the observations
    that measure the false-accept rate, and a calibration set without them
    measures only half the problem.
    """

    observation_id: str
    true_identity: str | None
    proposed_identity: str | None
    score: float
    margin: float
    quality: float = 1.0
    pose_deviation_deg: float = 0.0
    track_confidence: float = 1.0
    association_is_strong: bool = True
    camera_id: str | None = None
    source: Source = Source.SIMULATOR

    @property
    def person_key(self) -> str:
        """What the split groups on. Unenrolled people share a key: they are
        interchangeable for measuring false accepts, and `split` handles them
        per observation rather than as one indivisible person."""
        return self.true_identity or UNKNOWN_KEY

    @property
    def is_enrolled(self) -> bool:
        return self.true_identity is not None


@dataclass
class CalibrationSet:
    """A labelled corpus, with the checks that decide whether it is usable."""

    observations: list[LabelledObservation] = field(default_factory=list)
    name: str = "unnamed"
    note: str | None = None

    def add(self, observation: LabelledObservation) -> None:
        self.observations.append(observation)

    def extend(self, observations: list[LabelledObservation]) -> None:
        self.observations.extend(observations)

    def __len__(self) -> int:
        return len(self.observations)

    @property
    def people(self) -> set[str]:
        return {o.person_key for o in self.observations}

    @property
    def enrolled_people(self) -> set[str]:
        return {o.true_identity for o in self.observations if o.true_identity}

    @property
    def unknown_observations(self) -> list[LabelledObservation]:
        return [o for o in self.observations if not o.is_enrolled]

    @property
    def cameras(self) -> set[str]:
        return {o.camera_id for o in self.observations if o.camera_id}

    def by_person(self) -> dict[str, list[LabelledObservation]]:
        grouped: dict[str, list[LabelledObservation]] = {}
        for o in self.observations:
            grouped.setdefault(o.person_key, []).append(o)
        return grouped

    def readiness(self) -> "Readiness":
        """Whether this set can support a calibration claim, and what is missing.

        Checked before a sweep rather than after, because the most expensive
        mistake in calibration is discovering the corpus was inadequate only
        once its numbers are already in a report.
        """
        problems: list[str] = []
        warnings: list[str] = []

        if len(self.observations) < MIN_OBSERVATIONS:
            problems.append(
                f"{len(self.observations)} observations; at least "
                f"{MIN_OBSERVATIONS} are needed for a rate to mean anything")
        if len(self.enrolled_people) < MIN_ENROLLED_PEOPLE:
            problems.append(
                f"{len(self.enrolled_people)} enrolled people; at least "
                f"{MIN_ENROLLED_PEOPLE} are needed to sample face variation")
        if not self.unknown_observations:
            problems.append(
                "no observations of unenrolled people, so the false-accept rate "
                "cannot be measured at all")
        elif len(self.unknown_observations) < MIN_UNKNOWN_OBSERVATIONS:
            warnings.append(
                f"only {len(self.unknown_observations)} observations of "
                "unenrolled people; the false-accept rate will be coarse")

        if len(self.cameras) < 2:
            warnings.append(
                "observations come from fewer than two cameras; thresholds may "
                "not transfer to a different lens, angle, or lighting")

        sources = Counter(o.source for o in self.observations)
        if set(sources) == {Source.SIMULATOR}:
            warnings.append(
                "this set is entirely simulated. The harness is exercised, but "
                "no threshold derived from it may be marked calibrated")

        counts = Counter(o.person_key for o in self.observations)
        thin = [p for p, n in counts.items() if n < 3 and p != UNKNOWN_KEY]
        if thin:
            warnings.append(
                f"{len(thin)} people have fewer than 3 observations and "
                "contribute almost nothing")

        return Readiness(usable=not problems, problems=tuple(problems),
                         warnings=tuple(warnings))


#: Floors below which a rate is noise. Not tuned: they are the point at which a
#: percentage stops being a measurement, and they are deliberately conservative.
MIN_OBSERVATIONS = 200
MIN_ENROLLED_PEOPLE = 20
MIN_UNKNOWN_OBSERVATIONS = 30


@dataclass(frozen=True, slots=True)
class Readiness:
    usable: bool
    problems: tuple[str, ...]
    warnings: tuple[str, ...]

    def describe(self) -> list[str]:
        lines = []
        if self.usable:
            lines.append("Set is usable for a threshold sweep.")
        else:
            lines.append("Set is NOT usable for a threshold sweep.")
        for problem in self.problems:
            lines.append(f"  BLOCKER: {problem}")
        for warning in self.warnings:
            lines.append(f"  warning: {warning}")
        return lines


@dataclass(frozen=True, slots=True)
class Split:
    tune: CalibrationSet
    validate: CalibrationSet
    held_out_people: tuple[str, ...]

    @property
    def is_clean(self) -> bool:
        """No person appears on both sides.

        If this is ever False the validate half is measuring the tune half, and
        every number downstream is worthless.

        The shared unenrolled key is excluded: it is not a person, and it spans
        both halves on purpose.
        """
        shared = (self.tune.people & self.validate.people) - {UNKNOWN_KEY}
        return not shared


def split(
    calibration_set: CalibrationSet, *, validate_fraction: float = 0.3,
    salt: str = "evac120",
) -> Split:
    """Split by person, deterministically.

    Deterministic so a report can be reproduced, and hash-based rather than
    index-based so the split does not shift when observations are appended.
    """
    if not 0 < validate_fraction < 1:
        raise ValueError("validate_fraction must be between 0 and 1")

    grouped = calibration_set.by_person()
    held_out: list[str] = []
    for person in sorted(grouped):
        if person == UNKNOWN_KEY:
            continue
        if _fraction(salt, person) < validate_fraction:
            held_out.append(person)

    tune = CalibrationSet(name=f"{calibration_set.name}:tune")
    validate = CalibrationSet(name=f"{calibration_set.name}:validate")
    for person, items in grouped.items():
        if person == UNKNOWN_KEY:
            # Unenrolled people are not one person, and grouping them as one put
            # every observation of an unenrolled face on a single side of the
            # split. Whichever half missed out could not measure the
            # false-accept rate at all, which is the number the split exists to
            # protect. They are interchangeable for that purpose, so they are
            # split per observation instead.
            for item in items:
                target = (validate if _fraction(salt, item.observation_id)
                          < validate_fraction else tune)
                target.add(item)
            continue
        (validate if person in held_out else tune).extend(items)

    return Split(tune=tune, validate=validate, held_out_people=tuple(held_out))


def _fraction(salt: str, key: str) -> float:
    digest = hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()
    return (int(digest[:8], 16) % 10_000) / 10_000
