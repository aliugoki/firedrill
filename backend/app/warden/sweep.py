"""Zone sweep state: whether a warden has finished, and what finishing means.

A sweep is complete when a warden says it is. The system does not get a vote on
that, and it does not block the declaration on its own counts agreeing.

What it does do is refuse to let a completed sweep be mistaken for an all-clear.
`SweepState.is_clean` is deliberately narrow: complete, no outstanding
mismatch, and nobody in the zone still needing verification. A sweep can be
complete and not clean, and the command centre shows both, because "the warden
has finished looking" and "everyone here is safe" are different facts and
conflating them is how a zone gets signed off with someone still missing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.core.blockers import Blocker, BlockerCode, describe_all
from app.warden.headcount import Headcount, ZoneHeadcounts


class SweepStatus(str, Enum):
    NOT_STARTED = "NOT_STARTED"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETE = "COMPLETE"
    ESCALATED = "ESCALATED"


@dataclass
class SweepState:
    """One warden's progress through one zone."""

    zone_id: str
    warden_id: str | None = None
    status: SweepStatus = SweepStatus.NOT_STARTED
    started_ms: int | None = None
    completed_ms: int | None = None
    escalated_ms: int | None = None
    escalation_reason: str | None = None
    confirmed: set = field(default_factory=set)
    not_here: set = field(default_factory=set)
    marked_absent: set = field(default_factory=set)
    unknown_tagged: int = 0
    notes: list = field(default_factory=list)
    headcounts: ZoneHeadcounts | None = None

    def __post_init__(self) -> None:
        if self.headcounts is None:
            self.headcounts = ZoneHeadcounts(self.zone_id)

    # -- transitions -----------------------------------------------------------

    def begin(self, warden_id: str, now_ms: int) -> None:
        if self.status is SweepStatus.NOT_STARTED:
            self.status = SweepStatus.IN_PROGRESS
            self.started_ms = now_ms
            self.warden_id = warden_id

    def confirm(self, person_ref: str, warden_id: str, now_ms: int) -> None:
        self.begin(warden_id, now_ms)
        self.confirmed.add(person_ref)
        self.not_here.discard(person_ref)

    def report_not_here(self, person_ref: str, warden_id: str, now_ms: int) -> None:
        self.begin(warden_id, now_ms)
        self.not_here.add(person_ref)
        self.confirmed.discard(person_ref)

    def mark_absent(self, person_ref: str, warden_id: str, now_ms: int) -> None:
        self.begin(warden_id, now_ms)
        self.marked_absent.add(person_ref)

    def tag_unknown(self, warden_id: str, now_ms: int) -> None:
        self.begin(warden_id, now_ms)
        self.unknown_tagged += 1

    def note(self, text: str, warden_id: str, now_ms: int) -> None:
        self.begin(warden_id, now_ms)
        self.notes.append({"ts_ms": now_ms, "warden_id": warden_id, "text": text})

    def record_headcount(self, headcount: Headcount) -> Headcount:
        self.begin(headcount.warden_id, headcount.ts_ms)
        return self.headcounts.record(headcount)

    def complete(self, warden_id: str, now_ms: int) -> None:
        """The warden says they have checked the zone. Always accepted.

        Completion is a statement about what the warden did, and refusing it
        because the system disagrees would be the software overruling the human
        it has designated the final authority. The disagreement surfaces in
        `is_clean` instead.
        """
        self.begin(warden_id, now_ms)
        if self.status is not SweepStatus.ESCALATED:
            self.status = SweepStatus.COMPLETE
        self.completed_ms = now_ms

    def escalate(self, reason: str, warden_id: str, now_ms: int) -> None:
        """Something is wrong that the warden cannot resolve alone.

        Escalation outranks completion in both directions: a completed sweep
        that escalates becomes escalated, and an escalated one cannot be quietly
        completed away.
        """
        self.begin(warden_id, now_ms)
        self.status = SweepStatus.ESCALATED
        self.escalated_ms = now_ms
        self.escalation_reason = reason

    # -- questions -------------------------------------------------------------

    @property
    def is_complete(self) -> bool:
        return self.status is SweepStatus.COMPLETE

    @property
    def is_escalated(self) -> bool:
        return self.status is SweepStatus.ESCALATED

    def outstanding(self, expected: set) -> set:
        """People assigned to this zone that the warden has not resolved."""
        return set(expected) - self.confirmed - self.not_here - self.marked_absent

    def is_clean(self, expected: set) -> bool:
        """Complete, agreeing with the system, and nobody left unresolved.

        Narrower than `is_complete` on purpose. "The warden has finished
        looking" and "everyone here is safe" are different facts, and conflating
        them is how a zone gets signed off with someone still missing.
        """
        if self.status is not SweepStatus.COMPLETE:
            return False
        if self.outstanding(expected):
            return False
        latest = self.headcounts.latest
        if latest is None:
            # No physical count was taken. The warden may have walked the zone
            # and ticked everyone off a list, which is not the same as counting
            # heads: the list is the system's, and checking the system against
            # itself proves nothing. `blocking_clean` already said so; this is
            # the two agreeing.
            return False
        if latest.is_mismatch:
            return False
        return True

    def blocking_clean(self, expected: set) -> list[str]:
        """Why this zone is not clean, in words. Never empty when it is not."""
        return describe_all(self.blockers(expected))

    def blockers(self, expected: set) -> list[Blocker]:
        """The same refusals as codes, for a screen to word in its own language.

        One source for both: two methods computing the same list in two shapes
        is the drift this codebase keeps finding, so the sentences above are
        derived from these rather than written twice.

        The zone id travels in every one of them. These are read on a command
        centre that shows every zone at once, where "the sweep is still in
        progress" without a zone is a sentence nobody can act on.
        """
        out: list[Blocker] = []
        if self.status is SweepStatus.NOT_STARTED:
            out.append(Blocker(BlockerCode.SWEEP_NOT_STARTED,
                               {"zone_id": self.zone_id}))
        elif self.status is SweepStatus.IN_PROGRESS:
            out.append(Blocker(BlockerCode.SWEEP_IN_PROGRESS,
                               {"zone_id": self.zone_id}))
        elif self.status is SweepStatus.ESCALATED:
            out.append(Blocker(BlockerCode.SWEEP_ESCALATED, {
                "zone_id": self.zone_id,
                "reason": self.escalation_reason or "no reason recorded"}))

        outstanding = self.outstanding(expected)
        if outstanding:
            out.append(Blocker(BlockerCode.ZONE_UNCONFIRMED, {
                "zone_id": self.zone_id, "count": len(outstanding)}))

        latest = self.headcounts.latest
        if latest is None:
            out.append(Blocker(BlockerCode.NO_HEADCOUNT,
                               {"zone_id": self.zone_id}))
        elif latest.is_mismatch:
            # The mismatch's own sentence, which names the two numbers and who
            # counted. Carried as a value rather than rebuilt here, because the
            # wording of a disagreement is the headcount's business.
            out.append(Blocker(BlockerCode.HEADCOUNT_MISMATCH, {
                "zone_id": self.zone_id, "summary": latest.summary(),
                "physical": latest.physical_count,
                "system": latest.system_count,
                "difference": latest.difference}))
        return out

    def summary(self, expected: set) -> dict:
        """What the warden's own screen shows, and the zone panel mirrors."""
        latest = self.headcounts.latest
        return {
            "zone_id": self.zone_id,
            "warden_id": self.warden_id,
            "status": self.status.value,
            "expected": len(expected),
            "confirmed": len(self.confirmed),
            "not_here": len(self.not_here),
            "marked_absent": len(self.marked_absent),
            "outstanding": len(self.outstanding(expected)),
            "unknown_tagged": self.unknown_tagged,
            "physical_count": latest.physical_count if latest else None,
            "system_count": latest.system_count if latest else None,
            "mismatch": latest.kind.value if latest else None,
            "severity": latest.severity.value if latest else None,
            "is_clean": self.is_clean(expected),
            "blocking": self.blocking_clean(expected),
        }
