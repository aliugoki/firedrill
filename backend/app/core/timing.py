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
    CLOCK_CORRECTED = "CLOCK_CORRECTED"
    """Their arrival lands before the start because the clock moved, not
    because they were standing at the muster point when the alarm went.

    Its own reason because the alternative was a false statement about a real
    person. A backward correction puts every later arrival in front of the
    start, and every one of them was excluded as ARRIVED_BEFORE_START --
    "already at the muster point" -- which is not what happened to them."""


@dataclass(frozen=True, slots=True)
class ClockCorrection:
    """The node's clock was stepped during the drill, by `delta_ms`.

    Plain data rather than the health log's `Degradation`: `core/` imports
    nothing but the standard library, and a percentile has no business knowing
    what a component is. The edge hands these in; this module only has to know
    that time moved by something other than time passing.
    """

    at_ms: int
    delta_ms: int

    @property
    def backwards(self) -> bool:
        return self.delta_ms < 0

    def explains(self, arrival_ms: int, started_ms: int) -> bool:
        """Whether this correction accounts for an arrival before the start.

        Only backwards can: a forward step moves arrivals further from the
        start, never in front of it. The correction has to have happened after
        the drill began, and the gap has to be no larger than the step -- a
        person who arrived an hour before a drill that crossed a ten-second
        correction was genuinely already there.
        """
        if not self.backwards or self.at_ms < started_ms:
            return False
        return (started_ms - arrival_ms) <= abs(self.delta_ms)

    def describe(self) -> str:
        direction = "backwards" if self.backwards else "forwards"
        return (f"the system clock moved {direction} by "
                f"{abs(self.delta_ms) // 1000}s during the drill")


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
    clock_corrections: tuple = ()
    """Corrections to the node's clock during the window these numbers cover.

    A duration is the difference between two wall-clock readings. If somebody
    moved the clock between them, the difference is not a duration, and no
    amount of sample size fixes that."""

    @property
    def is_reliable(self) -> bool:
        """Whether the P95 is a claim worth acting on.

        Two ways for it not to be, and they are unrelated. Too few samples and
        the 95th percentile is just the slowest person or two. A clock
        correction and the arithmetic itself is wrong: a duration is the
        difference between two wall-clock readings, and if somebody moved the
        clock in between then no sample size repairs it.

        Measured on a hundred-person drill that crossed a forty-minute
        correction: forwards, P95 read 2608s against a true 208s, with full
        coverage, nothing excluded and not one caveat. Backwards it read 94s --
        a drill that failed its 120s target reported as passing it, because
        sixty people fell out of the sample.
        """
        if self.clock_corrections:
            return False
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

    def caveats(self) -> tuple[str, ...]:
        """Everything that belongs beside the number, not the first of them.

        A small sample and a low coverage are different problems with different
        responses -- one says the percentile is weak, the other says it
        describes almost nobody and may have improved by losing people, which
        is the gaming vector this module exists to make visible. Returning the
        first meant a drill where 92 of 100 people produced no timing printed
        "Only 8 measurements" and never mentioned the 92.
        """
        notes: list[str] = []
        # First, because it is the one that says the arithmetic is wrong rather
        # than weak. The others qualify a number; this one withdraws it.
        #
        # Ahead of the empty case too, and that is not a detail: a backward
        # correction is most destructive exactly when it has excluded
        # everybody, and "nobody had both a start and an arrival" is a true
        # sentence that hides why.
        for correction in self.clock_corrections:
            notes.append(
                f"{correction.describe().capitalize()}. A duration is the "
                "difference between two clock readings, so these numbers are "
                "not measurements of anything."
            )

        if self.sample_size == 0:
            notes.append(
                "No measurements: nobody had both a start and an arrival.")
            return tuple(notes)

        # Asked of the sample size, not of `is_reliable`. Once a clock
        # correction could also make that false, reusing it printed "Only 100
        # measurements" on a drill that had a hundred of them.
        if self.sample_size < MIN_SAMPLES_FOR_P95:
            notes.append(
                f"Only {self.sample_size} measurements. The 95th percentile "
                f"here is close to the slowest individual, not a distribution."
            )
        cov = self.coverage
        if cov is not None and cov < 0.9:
            notes.append(
                f"{self.excluded} of {self.sample_size + self.excluded} people "
                f"produced no timing ({(1 - cov) * 100:.0f}% excluded). The "
                "percentiles describe only those who were tracked end to end."
            )
        return tuple(notes)


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
    clock_corrections: tuple = (),
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
        #
        # Unless the clock moved under the drill, in which case it is not what
        # happened to them at all, and saying it is puts a false statement
        # about a real person in a post-incident report.
        explained = any(c.explains(assembly_arrival_ms, drill_started_ms)
                        for c in clock_corrections)
        reason = (ExclusionReason.CLOCK_CORRECTED if explained
                  else ExclusionReason.ARRIVED_BEFORE_START)

    return PersonTiming(
        person_id=person_id, started_ms=drill_started_ms,
        arrived_ms=assembly_arrival_ms, zone_id=zone_id, floor_id=floor_id,
        excluded_because=reason,
    )


def summarise(timings: list[PersonTiming], label: str = "building",
              clock_corrections: tuple = ()) -> PercentileSummary:
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
        clock_corrections=tuple(clock_corrections),
    )


def summarise_by(
    timings: list[PersonTiming], key: str, clock_corrections: tuple = ()
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
    # The corrections apply to every group: a clock is a property of the node,
    # not of a floor. A per-floor number carrying no caveat while the building
    # number carries one is the shape a reader trusts by mistake.
    return {name: summarise(items, label=name, clock_corrections=clock_corrections)
            for name, items in sorted(grouped.items())}


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
