"""Evacuation timing: the measured numbers, and how far to trust them.

EVAC-120 targets a P95 evacuation of 120 seconds. This module produces that
number and, just as importantly, says when it is not worth producing.

**A percentile needs enough samples to mean anything.** With 40 people, the 95th
percentile is the 38th value: it is the second-slowest person, not a
distributional claim. Reporting "P95 = 118 s" from 40 samples invites a decision
that the data cannot support. `PercentileSummary.is_reliable` marks that, and
every consumer is expected to show it.

**Only people with a real start and a real end are timed.** Someone who was
never observed has no evacuation time, and excluding them is not a way of
hiding them: they appear in the accountability board as UNACCOUNTED. Mixing the
two would let a timing improvement come from losing track of the slow people,
which is the single most dangerous way this metric could be gamed. `excluded`
and `exclusion_reasons` keep that visible.

The percentile method is **nearest-rank**, which always returns an observed
value rather than an interpolation between two people. For a life-safety number
an interpolated 118.4 s that nobody actually took is worse than the real 121 s
that somebody did.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

#: Below this many samples, a P95 is effectively "the slowest person or two" and
#: is reported as unreliable. 20 is the point at which the 95th percentile stops
#: being the maximum under nearest-rank.
MIN_SAMPLES_FOR_P95 = 20

#: The percentiles every report carries.
STANDARD_PERCENTILES: tuple[int, ...] = (50, 90, 95, 99)


class ExclusionReason(str, Enum):
    """Why someone has no evacuation time. Always reported, never silent."""

    NEVER_OBSERVED = "NEVER_OBSERVED"
    NEVER_REACHED_ASSEMBLY = "NEVER_REACHED_ASSEMBLY"
    NO_DRILL_START = "NO_DRILL_START"
    ARRIVED_BEFORE_START = "ARRIVED_BEFORE_START"


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile: always an observed value, never an interpolation.

    Returns None for an empty sample rather than 0, because "nobody evacuated in
    0 seconds" and "we have no measurements" must not look alike.
    """
    if not values:
        return None
    if not 0 < p <= 100:
        raise ValueError(f"percentile must be within (0, 100], got {p}")
    ordered = sorted(values)
    rank = math.ceil(p / 100 * len(ordered))
    return ordered[max(1, rank) - 1]


@dataclass(frozen=True, slots=True)
class PercentileSummary:
    """A distribution, with an honest note about how much it can bear."""

    label: str
    sample_size: int
    excluded: int
    p50: float | None
    p90: float | None
    p95: float | None
    p99: float | None
    maximum: float | None
    minimum: float | None
    mean: float | None
    exclusion_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def is_reliable(self) -> bool:
        """Whether the P95 is a distributional claim or just the slowest person."""
        return self.sample_size >= MIN_SAMPLES_FOR_P95

    @property
    def coverage(self) -> float | None:
        """Fraction of the population that produced a timing at all.

        Low coverage with a good P95 is the shape of a metric that improved by
        losing people rather than by moving them, so it is surfaced next to the
        number it qualifies.
        """
        total = self.sample_size + self.excluded
        if total == 0:
            return None
        return self.sample_size / total

    def caveat(self) -> str | None:
        """One sentence to print beside the number, or None if it stands alone."""
        if self.sample_size == 0:
            return "No measurements: nobody had both a start and an arrival."
        if not self.is_reliable:
            return (
                f"Only {self.sample_size} measurements. The 95th percentile here "
                f"is close to the slowest individual, not a distribution."
            )
        cov = self.coverage
        if cov is not None and cov < 0.9:
            return (
                f"{self.excluded} of {self.sample_size + self.excluded} people "
                f"produced no timing ({(1 - cov) * 100:.0f}% excluded). The "
                "percentiles describe only those who were tracked end to end."
            )
        return None


@dataclass(frozen=True, slots=True)
class PersonTiming:
    """One person's evacuation, or the reason they have no time."""

    person_id: str
    started_ms: int | None
    arrived_ms: int | None
    zone_id: str | None = None
    floor_id: str | None = None
    excluded_because: ExclusionReason | None = None

    @property
    def duration_ms(self) -> int | None:
        if self.excluded_because is not None:
            return None
        if self.started_ms is None or self.arrived_ms is None:
            return None
        return self.arrived_ms - self.started_ms

    @property
    def duration_s(self) -> float | None:
        ms = self.duration_ms
        return None if ms is None else ms / 1000.0


def measure(
    *,
    person_id: str,
    drill_started_ms: int | None,
    assembly_arrival_ms: int | None,
    zone_id: str | None = None,
    floor_id: str | None = None,
    was_observed: bool = True,
) -> PersonTiming:
    """Build one person's timing, or record why there is none."""
    reason: ExclusionReason | None = None
    if drill_started_ms is None:
        reason = ExclusionReason.NO_DRILL_START
    elif not was_observed:
        reason = ExclusionReason.NEVER_OBSERVED
    elif assembly_arrival_ms is None:
        reason = ExclusionReason.NEVER_REACHED_ASSEMBLY
    elif assembly_arrival_ms < drill_started_ms:
        # Already standing at the muster point when the alarm went. Real, and
        # not an evacuation: timing it would flatter the distribution.
        reason = ExclusionReason.ARRIVED_BEFORE_START

    return PersonTiming(
        person_id=person_id, started_ms=drill_started_ms,
        arrived_ms=assembly_arrival_ms, zone_id=zone_id, floor_id=floor_id,
        excluded_because=reason,
    )


def summarise(timings: list[PersonTiming], label: str = "building") -> PercentileSummary:
    """Roll a set of timings into percentiles, in seconds."""
    durations = [t.duration_s for t in timings if t.duration_s is not None]
    excluded = [t for t in timings if t.duration_s is None]

    reasons: dict[str, int] = defaultdict(int)
    for t in excluded:
        reasons[(t.excluded_because or ExclusionReason.NEVER_REACHED_ASSEMBLY).value] += 1

    return PercentileSummary(
        label=label,
        sample_size=len(durations),
        excluded=len(excluded),
        p50=percentile(durations, 50),
        p90=percentile(durations, 90),
        p95=percentile(durations, 95),
        p99=percentile(durations, 99),
        maximum=max(durations) if durations else None,
        minimum=min(durations) if durations else None,
        mean=sum(durations) / len(durations) if durations else None,
        exclusion_reasons=dict(reasons),
    )


def summarise_by(
    timings: list[PersonTiming], key: str
) -> dict[str, PercentileSummary]:
    """Break the distribution down by ``zone_id`` or ``floor_id``.

    A building-wide P95 hides the floor that is the actual problem, which is
    what the bottleneck panel exists to surface.
    """
    if key not in ("zone_id", "floor_id"):
        raise ValueError("key must be zone_id or floor_id")
    grouped: dict[str, list[PersonTiming]] = defaultdict(list)
    for t in timings:
        group = getattr(t, key)
        if group is not None:
            grouped[group].append(t)
    return {name: summarise(items, label=name) for name, items in sorted(grouped.items())}


@dataclass(frozen=True, slots=True)
class DrillTiming:
    """The whole drill's numbers, including the one people forget to measure."""

    building: PercentileSummary
    by_floor: dict[str, PercentileSummary]
    by_zone: dict[str, PercentileSummary]
    accountability_completed_ms: int | None
    target_p95_s: float = 120.0

    @property
    def accountability_completion_s(self) -> float | None:
        """How long until *every* person had a settled state.

        Distinct from the evacuation P95 and usually much longer. The building
        empties in a couple of minutes; establishing that nobody is left takes
        as long as the last uncertain person takes to resolve. This is the
        number that decides when an incident commander can stand down.
        """
        if self.accountability_completed_ms is None:
            return None
        return self.accountability_completed_ms / 1000.0

    @property
    def meets_target(self) -> bool | None:
        """Whether the measured P95 met the target, or None if unmeasurable.

        None is a real answer. It means the drill did not produce enough data to
        say, which is different from failing, and very different from passing.
        """
        if self.building.p95 is None or not self.building.is_reliable:
            return None
        return self.building.p95 <= self.target_p95_s
