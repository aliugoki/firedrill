"""System health, tracked as intervals rather than as a flag.

Invariant 8 says infrastructure failure degrades the system rather than clearing
it. Enforcing that needs more than a boolean: a post-drill report has to answer
"was the system trustworthy when it said that?", and a boolean cannot, because
by the time anyone reads it the outage is over and the flag is back to healthy.

So degradation is stored as **closed intervals with a cause**. That makes three
questions answerable that a flag cannot answer at all:

    was the system degraded at 10:42:15?          -> `was_degraded_at`
    how much of the drill was degraded?           -> `degraded_fraction`
    which cameras were dark when this person
    was last seen?                                -> `causes_at`

The third is the one that matters at the assembly point. A warden asking why
someone is unaccounted deserves "the camera covering their floor was down for
two minutes", not "the system is currently healthy".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Component(str, Enum):
    """What can fail. Each degrades the system differently."""

    CAMERA = "CAMERA"
    PIPELINE = "PIPELINE"
    EVENT_BUS = "EVENT_BUS"
    DATABASE = "DATABASE"
    CENTRAL_LINK = "CENTRAL_LINK"
    ROSTER_SOURCE = "ROSTER_SOURCE"


#: Components whose failure blinds the system to people. While one of these is
#: down, silence carries no information and nothing may be inferred from it.
BLINDING: frozenset[Component] = frozenset({
    Component.CAMERA, Component.PIPELINE, Component.EVENT_BUS,
})

#: Components whose failure costs durability or reach but not sight. The system
#: can still see; it may not be able to remember or report.
NON_BLINDING: frozenset[Component] = frozenset({
    Component.DATABASE, Component.CENTRAL_LINK, Component.ROSTER_SOURCE,
})


@dataclass
class Degradation:
    """One outage. `ended_ms` is None while it is still happening."""

    component: Component
    target: str
    started_ms: int
    reason: str
    ended_ms: int | None = None

    @property
    def is_open(self) -> bool:
        return self.ended_ms is None

    @property
    def blinds(self) -> bool:
        return self.component in BLINDING

    def duration_ms(self, now_ms: int) -> int:
        return (self.ended_ms if self.ended_ms is not None else now_ms) - self.started_ms

    def covers(self, ts_ms: int) -> bool:
        if ts_ms < self.started_ms:
            return False
        return self.ended_ms is None or ts_ms < self.ended_ms

    def describe(self) -> str:
        scope = "" if self.target == "*" else f" {self.target}"
        return f"{self.component.value.lower()}{scope}: {self.reason}"


@dataclass
class HealthLog:
    """Every degradation in one drill, open and closed.

    Append-only for the same reason the evidence ledger is: a report written
    afterwards has to be able to say the system was blind at the moment it
    made a particular call, and an overwritten flag cannot.
    """

    degradations: list[Degradation] = field(default_factory=list)

    # -- recording -------------------------------------------------------------

    def degrade(self, component: Component, target: str, ts_ms: int,
                reason: str) -> Degradation | None:
        """Open an outage. Re-degrading something already down is a no-op.

        Returns the new Degradation, or None if one was already open, so a
        caller can tell a genuine transition from a repeated notification. A
        flapping camera should not produce a hundred identical events.
        """
        if self.open_for(component, target) is not None:
            return None
        degradation = Degradation(component=component, target=target,
                                  started_ms=ts_ms, reason=reason)
        self.degradations.append(degradation)
        return degradation

    def recover(self, component: Component, target: str,
                ts_ms: int) -> Degradation | None:
        """Close an outage. Returns it, or None if nothing was open."""
        degradation = self.open_for(component, target)
        if degradation is None:
            return None
        degradation.ended_ms = ts_ms
        return degradation

    def close_all(self, ts_ms: int) -> list[Degradation]:
        """End every open outage, for when a drill finishes mid-failure."""
        closed = [d for d in self.degradations if d.is_open]
        for degradation in closed:
            degradation.ended_ms = ts_ms
        return closed

    # -- questions -------------------------------------------------------------

    def open_for(self, component: Component, target: str) -> Degradation | None:
        for degradation in self.degradations:
            if (degradation.is_open and degradation.component is component
                    and degradation.target == target):
                return degradation
        return None

    def open_now(self) -> list[Degradation]:
        return [d for d in self.degradations if d.is_open]

    @property
    def is_degraded(self) -> bool:
        return any(d.is_open for d in self.degradations)

    @property
    def is_blind(self) -> bool:
        """Whether anything currently stops the system seeing people."""
        return any(d.is_open and d.blinds for d in self.degradations)

    def was_degraded_at(self, ts_ms: int, *, blinding_only: bool = False) -> bool:
        return any(d.covers(ts_ms) and (d.blinds or not blinding_only)
                   for d in self.degradations)

    def causes_at(self, ts_ms: int) -> list[str]:
        """Why the system was degraded at a moment, in words a warden can use."""
        return [d.describe() for d in self.degradations if d.covers(ts_ms)]

    def blinded_targets_at(self, ts_ms: int) -> set[str]:
        return {d.target for d in self.degradations
                if d.covers(ts_ms) and d.blinds}

    def degraded_fraction(self, start_ms: int, end_ms: int, *,
                          blinding_only: bool = True) -> float:
        """Share of a window during which the system was degraded.

        Overlapping outages are unioned, not summed. Three cameras down at once
        is one blind period, and summing them could report more downtime than
        the drill lasted.
        """
        span = end_ms - start_ms
        if span <= 0:
            return 0.0
        windows = sorted(
            (max(d.started_ms, start_ms),
             min(d.ended_ms if d.ended_ms is not None else end_ms, end_ms))
            for d in self.degradations
            if (d.blinds or not blinding_only)
            and d.started_ms < end_ms
            and (d.ended_ms is None or d.ended_ms > start_ms)
        )
        total, cursor = 0, start_ms
        for begin, finish in windows:
            if finish <= cursor:
                continue
            total += finish - max(begin, cursor)
            cursor = max(cursor, finish)
        return min(1.0, total / span)

    def summary(self, start_ms: int, end_ms: int) -> "HealthSummary":
        return HealthSummary(
            total_outages=len(self.degradations),
            open_outages=len(self.open_now()),
            is_blind=self.is_blind,
            blind_fraction=self.degraded_fraction(start_ms, end_ms,
                                                  blinding_only=True),
            degraded_fraction=self.degraded_fraction(start_ms, end_ms,
                                                     blinding_only=False),
            longest_blind_ms=max(
                (d.duration_ms(end_ms) for d in self.degradations if d.blinds),
                default=0),
            by_component={
                component: sum(1 for d in self.degradations
                               if d.component is component)
                for component in Component
                if any(d.component is component for d in self.degradations)
            },
        )


@dataclass(frozen=True, slots=True)
class HealthSummary:
    """What a post-drill report says about whether the system could see."""

    total_outages: int
    open_outages: int
    blind_fraction: float
    degraded_fraction: float
    longest_blind_ms: int
    is_blind: bool = False
    """Whether a blinding outage is open *now*, as opposed to the historical
    `blind_fraction`. Callers used to reconstruct it as "some blindness has
    happened and some outage is open", which is true of a drill where a camera
    dropped for ten seconds an hour ago and the database is slow now."""
    by_component: dict[Component, int] = field(default_factory=dict)

    @property
    def was_clean(self) -> bool:
        return self.total_outages == 0

    def caveat(self) -> str | None:
        """One sentence to print beside every number the drill produced."""
        if self.was_clean:
            return None
        if self.blind_fraction >= 0.5:
            return (
                f"The system could not see for {self.blind_fraction:.0%} of this "
                "drill. Its counts describe the minority of the drill it was "
                "watching, and the manual roll-call is the authority."
            )
        if self.blind_fraction > 0:
            return (
                f"The system was blind for {self.blind_fraction:.0%} of this "
                f"drill across {self.total_outages} outage(s), the longest "
                f"{self.longest_blind_ms / 1000:.0f} s. Treat gaps in a person's "
                "history as unobserved rather than as absence."
            )
        return (
            f"{self.total_outages} non-blinding outage(s). The system kept "
            "watching; durability or reporting may have been affected."
        )
