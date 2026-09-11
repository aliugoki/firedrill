"""Physical headcount against system count, and why the two directions differ.

A warden at the muster point counts heads. The system also has a number. When
they disagree, the disagreement is the most valuable signal in the drill, and
the two directions of disagreement mean opposite things.

**The system counted more than the warden did.** The system believes people are
safe who are not standing there. This is the dangerous direction, and it is the
exact failure mode the whole product exists to prevent: an all-clear declared
over someone still inside. It escalates regardless of tolerance.

**The warden counted more than the system did.** There are people at the muster
point the system does not know about — unbadged visitors, contractors, a
neighbouring building's staff. Worth investigating, not alarming. This is the
direction that stays within tolerance.

Treating those symmetrically, as a single absolute difference against one
threshold, would let the dangerous direction hide inside a tolerance chosen to
accommodate the harmless one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum

from app.core.events import Event, EventType, SourceKind


class MismatchKind(str, Enum):
    MATCH = "MATCH"
    SYSTEM_OVERCOUNTED = "SYSTEM_OVERCOUNTED"
    """Fewer people are physically present than the system believes. Dangerous."""

    SYSTEM_UNDERCOUNTED = "SYSTEM_UNDERCOUNTED"
    """More people are present than the system knows about. Investigate."""


class Severity(str, Enum):
    NONE = "NONE"
    INVESTIGATE = "INVESTIGATE"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True, slots=True)
class HeadcountPolicy:
    """Tolerances. Asymmetric on purpose.

    `overcount_tolerance` is zero by default and should stay there. A system
    that thinks one more person is safe than actually is has made exactly the
    error that kills someone, and "within tolerance" is not a thing that error
    can be.
    """

    undercount_tolerance: int = 2
    overcount_tolerance: int = 0
    calibrated: bool = False
    source: str = "uncalibrated"

    def __post_init__(self) -> None:
        if self.undercount_tolerance < 0 or self.overcount_tolerance < 0:
            raise ValueError("tolerances must not be negative")
        if self.overcount_tolerance > 0:
            # Allowed, because a site may have a reason. Not silent.
            pass


DEFAULT_POLICY = HeadcountPolicy(
    undercount_tolerance=2, overcount_tolerance=0, calibrated=False,
    source="Phase 4 placeholder, unvalidated")


@dataclass(frozen=True, slots=True)
class Headcount:
    """One physical count, and what it says about the system's number."""

    zone_id: str
    warden_id: str
    device_id: str
    ts_ms: int
    physical_count: int
    system_count: int
    policy: HeadcountPolicy = DEFAULT_POLICY
    note: str | None = None

    def __post_init__(self) -> None:
        if self.physical_count < 0 or self.system_count < 0:
            raise ValueError("a headcount cannot be negative")

    @property
    def difference(self) -> int:
        """System minus physical. Positive means the system counted more."""
        return self.system_count - self.physical_count

    @property
    def kind(self) -> MismatchKind:
        if self.difference == 0:
            return MismatchKind.MATCH
        return (MismatchKind.SYSTEM_OVERCOUNTED if self.difference > 0
                else MismatchKind.SYSTEM_UNDERCOUNTED)

    @property
    def severity(self) -> Severity:
        if self.kind is MismatchKind.MATCH:
            return Severity.NONE
        if self.kind is MismatchKind.SYSTEM_OVERCOUNTED:
            # The dangerous direction. Any amount beyond tolerance escalates,
            # and the default tolerance is zero.
            return (Severity.NONE
                    if self.difference <= self.policy.overcount_tolerance
                    else Severity.ESCALATE)
        return (Severity.NONE
                if -self.difference <= self.policy.undercount_tolerance
                else Severity.INVESTIGATE)

    @property
    def is_mismatch(self) -> bool:
        """Whether this count calls for action under the site's own policy."""
        return self.severity is not Severity.NONE

    @property
    def tolerated_overcount(self) -> int:
        """People the system called safe whom the warden could not see, and
        whom the site's tolerance absorbed instead of reporting.

        Zero unless a site has raised `overcount_tolerance` above its default,
        which the policy's own docstring argues it should not: that error is
        the one that kills somebody, and "within tolerance" is not a thing it
        can be. `__post_init__` allows it and says the allowance will not be
        silent -- and then did nothing, so a count of 38 against a system's 40
        reported "No action. Record the count and continue." while two people
        were missing.
        """
        if self.kind is not MismatchKind.SYSTEM_OVERCOUNTED:
            return 0
        return self.difference if self.severity is Severity.NONE else 0

    @property
    def missing_from_the_muster_point(self) -> int:
        """How many people the system thinks are safe but nobody can see.

        The number an incident commander acts on. Zero when the warden counted
        at least as many as the system did.
        """
        return max(0, self.difference)

    def summary(self) -> str:
        if self.kind is MismatchKind.MATCH:
            return (f"{self.zone_id}: physical count {self.physical_count} "
                    f"matches the system")
        if self.kind is MismatchKind.SYSTEM_OVERCOUNTED:
            return (
                f"{self.zone_id}: the system counted {self.system_count} but "
                f"{self.warden_id} counted {self.physical_count}. "
                f"{self.difference} person(s) the system believes are safe are "
                "not at the muster point.")
        return (
            f"{self.zone_id}: {self.warden_id} counted {self.physical_count} "
            f"against the system's {self.system_count}. {-self.difference} "
            "person(s) present that the system does not know about; tag them as "
            "visitors or contractors.")

    def recommended_action(self) -> str:
        if self.severity is Severity.NONE:
            absorbed = self.tolerated_overcount
            if absorbed:
                return (
                    f"This site's policy absorbs an overcount of up to "
                    f"{self.policy.overcount_tolerance}, so no action is "
                    f"required of you. {absorbed} person(s) the system "
                    "believes are safe are still not in front of you. Say so "
                    "to the command centre.")
            return "No action. Record the count and continue."
        if self.severity is Severity.ESCALATE:
            return (
                "Do not declare all clear. Re-count, then work the priority "
                "list for this zone: every person the system marked accounted "
                "here needs eyes on them.")
        return (
            "Tag the unrecognised people as visitors or contractors so the "
            "roster reconciles, then re-count.")


@dataclass
class ZoneHeadcounts:
    """Every count taken at one zone during a drill.

    Kept as a series rather than a latest value. A warden who counts 38, then
    40, then 38 again is telling you something a single number cannot, and the
    reconciliation afterwards needs all three.
    """

    zone_id: str
    counts: list[Headcount]

    def __init__(self, zone_id: str) -> None:
        self.zone_id = zone_id
        self.counts = []

    def record(self, headcount: Headcount) -> Headcount:
        if headcount.zone_id != self.zone_id:
            raise ValueError(
                f"headcount for {headcount.zone_id} recorded against "
                f"{self.zone_id}")
        self.counts.append(headcount)
        return headcount

    @property
    def latest(self) -> Headcount | None:
        return self.counts[-1] if self.counts else None

    @property
    def is_settled(self) -> bool:
        """Whether the most recent count agrees with the system."""
        latest = self.latest
        return latest is not None and not latest.is_mismatch

    @property
    def ever_escalated(self) -> bool:
        """Whether the dangerous direction was ever seen, even if later resolved.

        A zone that read 38 against 40 and then agreed at 40 had a real moment
        of disagreement, and a post-drill report should say so rather than
        showing only the reassuring final number.
        """
        return any(c.severity is Severity.ESCALATE for c in self.counts)

    def unstable(self) -> bool:
        """Whether the warden's own counts disagree with each other."""
        physical = {c.physical_count for c in self.counts}
        return len(physical) > 1


def to_event(headcount: Headcount, *, tenant_id: str, site_id: str,
             drill_id: str, seq: int) -> Event:
    """Put a physical count into the event log.

    It was not there. `Drill.record_headcount` folded the count into warden
    state and stopped, so the one measurement taken by a human that is
    independent of the cameras existed only in this process's memory: not
    stored, not replicated to central, not replayable after a restart. Two of
    the eight validation criteria are computed from it, and invariant 6 says
    every accountability decision is reconstructable from stored events.

    Carried as a `WARDEN_NOTE` rather than a new event type. The vocabulary is
    deliberately closed and already carries three other warden actions this
    way; the payload says what it is. The note is written out in words too, so
    the evidence ledger shows a readable trace rather than a bare number.
    """
    return Event(
        tenant_id=tenant_id, site_id=site_id, drill_id=drill_id,
        source=f"warden-device:{headcount.device_id}",
        source_kind=SourceKind.WARDEN, seq=seq, type=EventType.WARDEN_NOTE,
        ts_ms=headcount.ts_ms, subject=headcount.zone_id,
        payload={
            "headcount": {
                "zone_id": headcount.zone_id,
                "warden_id": headcount.warden_id,
                "device_id": headcount.device_id,
                "physical_count": headcount.physical_count,
                "system_count": headcount.system_count,
                "note": headcount.note,
            },
            "warden_id": headcount.warden_id,
            "device_id": headcount.device_id,
            "zone_id": headcount.zone_id,
            "human": True,
            "note": headcount.summary(),
        },
    )


def from_event(event: Event, *,
               policy: HeadcountPolicy = DEFAULT_POLICY) -> Headcount | None:
    """Read a count back out. None for any event that is not one.

    The policy is configuration rather than evidence, so it comes from the
    drill running now rather than from the stored row: a site that tightened
    its tolerance should see the old count judged by the new rule, not have the
    old rule resurrected from a replay.
    """
    if event.source_kind is not SourceKind.WARDEN:
        return None
    body = (event.payload or {}).get("headcount")
    if not isinstance(body, dict):
        return None
    return Headcount(
        zone_id=body.get("zone_id") or event.subject or "",
        warden_id=body.get("warden_id") or "",
        device_id=body.get("device_id") or "",
        ts_ms=event.ts_ms,
        physical_count=int(body.get("physical_count", 0)),
        system_count=int(body.get("system_count", 0)),
        policy=policy,
        note=body.get("note"),
    )
