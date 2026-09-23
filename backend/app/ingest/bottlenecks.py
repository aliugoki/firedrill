"""Where the evacuation is actually slow.

A building-wide P95 tells an incident commander that people are taking too long.
It does not tell them where to send a marshal, and during a drill that is the
only question worth answering in the moment.

Four measures per exit, each from the event stream rather than from a model:

    throughput   people per minute crossing the exit
    queue        how many are in the exit zone right now
    dwell        how long a person spends there, median and worst
    density      queue against the zone's stated capacity

**Density is the one that is usually wrong.** It needs a capacity somebody
measured, and a zone with no capacity recorded produces no density rather than a
plausible-looking ratio against a guess. A crowding figure derived from an
invented denominator is worse than no figure: it is the kind of number that ends
up in a report.

The vendored `dwell.union_seconds` does the interval work, because a person seen
by two overlapping cameras must not be counted as two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime, timezone

from app.core.presence_fsm import ZoneKind
from app.vendor.visiontrack.dwell import union_seconds


class CaveatCode(str, Enum):
    """Everything that can qualify an exit measurement.

    Small on purpose. A caveat exists because somebody would otherwise read a
    number as more solid than it is, so each one has to be worth a line on a
    screen that has no room for a second opinion.
    """

    SHORT_WINDOW = "SHORT_WINDOW"
    NO_CAPACITY = "NO_CAPACITY"


#: The English sentence for each code, held here so the prose and the code
#: cannot drift apart. `blockers.py` is arranged the same way and says why.
_CAVEAT_WORDS: dict[CaveatCode, str] = {
    CaveatCode.SHORT_WINDOW:
        "Less than {seconds} seconds of drill; throughput is not yet "
        "meaningful.",
    CaveatCode.NO_CAPACITY:
        "No capacity recorded for {zone_list}, so density is not reported "
        "for them.",
}


@dataclass(frozen=True)
class Caveat:
    """One qualification, as a code a screen can word and prose it can fall
    back to."""

    code: CaveatCode
    detail: dict = field(default_factory=dict)

    def describe(self) -> str:
        values = dict(self.detail)
        if "zones" in values:
            values["zone_list"] = ", ".join(values["zones"])
        return _CAVEAT_WORDS[self.code].format(**values)


@dataclass
class Crossing:
    """One person's passage through one exit."""

    person_id: str
    zone_id: str
    entered_ms: int
    left_ms: int | None = None

    @property
    def is_complete(self) -> bool:
        return self.left_ms is not None

    def duration_ms(self, now_ms: int) -> int:
        return (self.left_ms if self.left_ms is not None else now_ms) - self.entered_ms


@dataclass(frozen=True, slots=True)
class ExitMeasure:
    """What one exit is doing. Every field is measured or None."""

    zone_id: str
    completed: int
    queue: int
    throughput_per_min: float | None
    median_dwell_s: float | None
    worst_dwell_s: float | None
    capacity: int | None
    density: float | None

    @property
    def is_congested(self) -> bool:
        """Whether this exit is holding people up.

        Deliberately not a threshold on density, which needs a capacity most
        sites never record. A queue that is not clearing is observable without
        one: people are in the zone and the median time to get through it is
        long.
        """
        return (self.queue > 0 and self.median_dwell_s is not None
                and self.median_dwell_s >= 20.0)

    def describe(self) -> str:
        parts = [f"{self.zone_id}: {self.completed} through"]
        if self.throughput_per_min is not None:
            parts.append(f"{self.throughput_per_min:.0f}/min")
        if self.queue:
            parts.append(f"{self.queue} waiting")
        if self.median_dwell_s is not None:
            parts.append(f"median {self.median_dwell_s:.0f}s")
        if self.density is not None:
            parts.append(f"{self.density:.0%} of capacity")
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class BottleneckPanel:
    exits: tuple = ()
    measured_over_s: float = 0.0
    caveats: tuple = ()
    """Plural on purpose. A single slot means a second true caveat silently
    disappears, and a caveat is exactly the thing that must not be dropped: it
    is there because somebody would otherwise read the number as more solid
    than it is."""
    caveat_codes: tuple = ()
    """The same caveats as `Caveat` records. The prose above is English and
    this panel is read on a command centre in Arabic."""

    @property
    def limiting(self) -> ExitMeasure | None:
        """The exit holding the building up, or None if none is.

        The one an incident commander acts on: send a marshal here.
        """
        congested = [e for e in self.exits if e.is_congested]
        if not congested:
            return None
        return max(congested, key=lambda e: (e.queue, e.median_dwell_s or 0))

    @property
    def total_through(self) -> int:
        return sum(e.completed for e in self.exits)

    def describe(self) -> list[str]:
        if not self.exits:
            return ["No exit was observed, so nothing can be said about "
                    "where the evacuation is slow."]
        lines = [e.describe() for e in self.exits]
        limiting = self.limiting
        if limiting is not None:
            lines.append(f"Limiting: {limiting.zone_id}")
        lines.extend(self.caveats)
        return lines


@dataclass
class BottleneckTracker:
    """Accumulates exit crossings as the drill runs.

    Fed from the same zone sightings the presence machine uses, so it cannot
    disagree with the board about where somebody was.
    """

    capacities: dict = field(default_factory=dict)
    crossings: list = field(default_factory=list)
    _open: dict = field(default_factory=dict)

    def observe(self, person_id: str, zone_id: str, zone_kind: ZoneKind,
                ts_ms: int) -> None:
        """One sighting. Only exit zones are tracked; the rest are irrelevant here."""
        current = self._open.get(person_id)

        if zone_kind is ZoneKind.EXIT:
            if current is None or current.zone_id != zone_id:
                if current is not None:
                    current.left_ms = ts_ms
                crossing = Crossing(person_id=person_id, zone_id=zone_id,
                                    entered_ms=ts_ms)
                self.crossings.append(crossing)
                self._open[person_id] = crossing
            return

        # Seen somewhere that is not this exit: they are through it.
        if current is not None:
            current.left_ms = ts_ms
            self._open.pop(person_id, None)

    def measure(self, now_ms: int, started_ms: int | None = None) -> BottleneckPanel:
        by_zone: dict[str, list] = {}
        for crossing in self.crossings:
            by_zone.setdefault(crossing.zone_id, []).append(crossing)

        window_s = ((now_ms - started_ms) / 1000
                    if started_ms is not None and now_ms > started_ms else 0.0)

        measures = []
        no_capacity = []
        for zone_id, crossings in sorted(by_zone.items()):
            completed = [c for c in crossings if c.is_complete]
            queue = sum(1 for c in crossings if not c.is_complete)

            durations = sorted(c.duration_ms(now_ms) / 1000 for c in completed)
            median = _median(durations)
            worst = durations[-1] if durations else None

            throughput = (len(completed) / (window_s / 60)
                          if window_s >= 30 else None)

            capacity = self.capacities.get(zone_id)
            if capacity:
                density = queue / capacity
            else:
                # No capacity recorded. A crowding figure against an invented
                # denominator is worse than none.
                density = None
                no_capacity.append(zone_id)

            measures.append(ExitMeasure(
                zone_id=zone_id, completed=len(completed), queue=queue,
                throughput_per_min=throughput, median_dwell_s=median,
                worst_dwell_s=worst, capacity=capacity, density=density))

        # Codes first, prose derived from them. Both shapes are needed -- the
        # post-drill report is a document and wants the English sentence, the
        # screen is read in Arabic and wants the code -- and building the
        # sentences separately is how the two drift apart.
        coded: list[Caveat] = []
        if window_s < 30:
            coded.append(Caveat(CaveatCode.SHORT_WINDOW, {"seconds": 30}))
        if no_capacity:
            coded.append(Caveat(CaveatCode.NO_CAPACITY,
                                {"zones": list(no_capacity)}))

        return BottleneckPanel(exits=tuple(measures), measured_over_s=window_s,
                               caveats=tuple(c.describe() for c in coded),
                               caveat_codes=tuple(coded))

    def total_exit_time_s(self, person_id: str, now_ms: int) -> float:
        """How long one person spent in exit zones, overlaps unioned.

        A person seen by two overlapping cameras at the main exit must not be
        counted as two passages, which is what the vendored interval union is
        for.
        """
        intervals = [
            (_at(c.entered_ms), _at(c.left_ms if c.left_ms is not None else now_ms))
            for c in self.crossings if c.person_id == person_id
        ]
        return union_seconds(intervals)


def _median(values: list) -> float | None:
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return values[middle]
    return (values[middle - 1] + values[middle]) / 2


def _at(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
