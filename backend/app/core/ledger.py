"""Evidence ledger: the reason behind every decision.

Invariant 6 says every accountability decision must be reconstructable — asking
"why was EMP-00482 marked ACCOUNTED?" has to return the evidence, not a
confidence score. This module is that answer.

The ledger is append-only. Evidence is never edited, never deleted, and never
re-weighted after the fact. A later observation that contradicts an earlier one
does not erase it; both stand, and the contradiction itself becomes visible.
That is what makes invariant 3 enforceable: a conflict cannot be resolved
silently if both sides of it are still in the record.

Evidence carries a **stance** toward the subject being accounted for:

    SUPPORTS      argues the person is accounted for
    CONTRADICTS   argues against it
    CONTEXT       neither, but changes how much the rest is worth

`CONTEXT` is doing real work. A camera outage supports nothing and contradicts
nothing; it means the silence that follows carries no information. Without a
stance for that, a gap in the record reads as an absence of concern, which is
exactly the failure invariant 1 exists to prevent.
"""

from __future__ import annotations

from bisect import insort
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Iterator


class Stance(str, Enum):
    SUPPORTS = "SUPPORTS"
    CONTRADICTS = "CONTRADICTS"
    CONTEXT = "CONTEXT"


class EvidenceKind(str, Enum):
    """What kind of observation this is. Drives how `explain` narrates it."""

    FACE_MATCH = "FACE_MATCH"
    FACE_UNAVAILABLE = "FACE_UNAVAILABLE"
    IDENTITY_CONFIRMED = "IDENTITY_CONFIRMED"
    IDENTITY_CANDIDATE = "IDENTITY_CANDIDATE"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    IDENTITY_REJECTED = "IDENTITY_REJECTED"
    ZONE_SIGHTING = "ZONE_SIGHTING"
    ASSEMBLY_ARRIVAL = "ASSEMBLY_ARRIVAL"
    ASSEMBLY_DEPARTURE = "ASSEMBLY_DEPARTURE"
    EXITED_BUILDING = "EXITED_BUILDING"
    TRACK_LOST = "TRACK_LOST"
    TRACK_REACQUIRED = "TRACK_REACQUIRED"
    WARDEN_CONFIRMATION = "WARDEN_CONFIRMATION"
    WARDEN_REJECTION = "WARDEN_REJECTION"
    WARDEN_NOTE = "WARDEN_NOTE"
    CAMERA_DEGRADED = "CAMERA_DEGRADED"
    CAMERA_RECOVERED = "CAMERA_RECOVERED"
    SYSTEM_DEGRADED = "SYSTEM_DEGRADED"
    SYSTEM_RECOVERED = "SYSTEM_RECOVERED"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    DECISION = "DECISION"


#: Evidence a human produced. Invariant 9 makes this the final authority, and
#: `explain` surfaces it first regardless of when it arrived.
HUMAN_KINDS: frozenset[EvidenceKind] = frozenset({
    EvidenceKind.WARDEN_CONFIRMATION, EvidenceKind.WARDEN_REJECTION,
    EvidenceKind.WARDEN_NOTE,
})

#: Evidence meaning "the system could not see", as distinct from "the system saw
#: nothing there". These degrade confidence; they are never absence.
BLINDNESS_KINDS: frozenset[EvidenceKind] = frozenset({
    EvidenceKind.CAMERA_DEGRADED, EvidenceKind.SYSTEM_DEGRADED,
    EvidenceKind.SEQUENCE_GAP, EvidenceKind.FACE_UNAVAILABLE,
    EvidenceKind.TRACK_LOST,
})


@dataclass(frozen=True, slots=True, order=True)
class Evidence:
    """One immutable item in the record.

    Ordered by timestamp first so a ledger stays sorted as it grows, which is
    what lets `explain` narrate chronologically without re-sorting.
    """

    ts_ms: int
    seq_hint: int = field(compare=True, default=0)
    kind: EvidenceKind = field(compare=False, default=EvidenceKind.DECISION)
    subject: str = field(compare=False, default="")
    stance: Stance = field(compare=False, default=Stance.CONTEXT)
    summary: str = field(compare=False, default="")
    source: str | None = field(compare=False, default=None)
    identity: str | None = field(compare=False, default=None)
    detail: dict = field(compare=False, default_factory=dict)

    @property
    def is_human(self) -> bool:
        return self.kind in HUMAN_KINDS

    @property
    def is_blindness(self) -> bool:
        return self.kind in BLINDNESS_KINDS


@dataclass(frozen=True, slots=True)
class IdentityDispute:
    """Two identities claimed for one subject. Never auto-resolved."""

    subject: str
    identities: tuple[str, ...]
    first_seen_ms: int
    evidence: tuple[Evidence, ...]


@dataclass(frozen=True, slots=True)
class Explanation:
    """The answer to "why is this person in this state?"

    Deliberately not a score. A number would let an operator round it off; a
    list of observations forces the actual question, which is whether the
    evidence is good enough.
    """

    subject: str
    decision: str | None
    decided_at_ms: int | None
    supporting: tuple[Evidence, ...]
    contradicting: tuple[Evidence, ...]
    context: tuple[Evidence, ...]
    human_evidence: tuple[Evidence, ...]
    disputes: tuple[IdentityDispute, ...]

    @property
    def has_human_confirmation(self) -> bool:
        return any(e.kind is EvidenceKind.WARDEN_CONFIRMATION
                   for e in self.human_evidence)

    @property
    def is_disputed(self) -> bool:
        return bool(self.disputes)

    @property
    def blindness(self) -> tuple[Evidence, ...]:
        """Everything that means "we could not see", from any stance."""
        everything = self.supporting + self.contradicting + self.context
        return tuple(sorted(e for e in everything if e.is_blindness))

    def narrate(self) -> list[str]:
        """Chronological plain-language account, human evidence flagged.

        This is what the command centre's explain drawer renders and what a
        post-incident report quotes. It says what was observed and when, and it
        never editorialises about what the person did.
        """
        lines: list[str] = []
        if self.decision:
            when = _fmt_ms(self.decided_at_ms)
            lines.append(f"Decision: {self.decision} at {when}.")
        else:
            lines.append("No decision recorded yet.")

        if self.disputes:
            for dispute in self.disputes:
                names = ", ".join(dispute.identities)
                lines.append(
                    f"DISPUTED: two identities claimed — {names}. "
                    "A human must rule; the system will not choose."
                )

        for item in sorted(self.supporting + self.contradicting + self.context):
            marker = {
                Stance.SUPPORTS: "+", Stance.CONTRADICTS: "-", Stance.CONTEXT: "·",
            }[item.stance]
            who = f" [{item.source}]" if item.source else ""
            human = " (human)" if item.is_human else ""
            lines.append(
                f"  {marker} {_fmt_ms(item.ts_ms)} {item.kind.value}{human}: "
                f"{item.summary}{who}"
            )

        if not self.supporting and not self.contradicting:
            lines.append(
                "  No evidence for or against. Absence of evidence is not "
                "evidence of absence."
            )
        return lines


def _fmt_ms(ts_ms: int | None) -> str:
    if ts_ms is None:
        return "unknown time"
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )[:-3] + "Z"


class EvidenceLedger:
    """Append-only evidence, indexed by subject.

    Holds every observation for the life of a drill. Nothing is pruned during a
    drill: retention is a post-drill policy decision, and dropping evidence
    mid-drill would make `explain` lie by omission.
    """

    def __init__(self) -> None:
        self._by_subject: dict[str, list[Evidence]] = defaultdict(list)
        self._decisions: dict[str, Evidence] = {}
        self._counter = 0

    def record(
        self,
        *,
        subject: str,
        kind: EvidenceKind,
        ts_ms: int,
        stance: Stance = Stance.CONTEXT,
        summary: str = "",
        source: str | None = None,
        identity: str | None = None,
        detail: dict | None = None,
    ) -> Evidence:
        """Append one item. Returns it, so callers can log what they filed."""
        if not subject:
            raise ValueError("evidence must be about a subject")
        self._counter += 1
        item = Evidence(
            ts_ms=ts_ms, seq_hint=self._counter, kind=kind, subject=subject,
            stance=stance, summary=summary, source=source, identity=identity,
            detail=detail or {},
        )
        insort(self._by_subject[subject], item)
        if kind is EvidenceKind.DECISION:
            self._decisions[subject] = item
        return item

    def for_subject(self, subject: str) -> tuple[Evidence, ...]:
        return tuple(self._by_subject.get(subject, ()))

    def subjects(self) -> list[str]:
        return sorted(self._by_subject)

    def disputes_for(self, subject: str) -> tuple[IdentityDispute, ...]:
        """Identity claims that disagree.

        A dispute is any subject with two or more distinct identities asserted
        by non-rejected evidence. It is reported, never adjudicated: invariant 3
        forbids the software from picking a winner.
        """
        claims: dict[str, list[Evidence]] = defaultdict(list)
        rejected: set[str] = set()
        for item in self._by_subject.get(subject, ()):
            if item.kind is EvidenceKind.IDENTITY_REJECTED and item.identity:
                rejected.add(item.identity)
            elif item.identity and item.kind in (
                EvidenceKind.IDENTITY_CONFIRMED, EvidenceKind.IDENTITY_CANDIDATE,
                EvidenceKind.FACE_MATCH, EvidenceKind.WARDEN_CONFIRMATION,
            ):
                claims[item.identity].append(item)

        live = {name: items for name, items in claims.items() if name not in rejected}
        if len(live) < 2:
            return ()

        # A warden ruling settles it. Invariant 9: the human is the authority,
        # so a confirmed identity is not "in dispute" with the cameras it
        # overruled.
        confirmed_by_human = {
            name for name, items in live.items()
            if any(i.kind is EvidenceKind.WARDEN_CONFIRMATION for i in items)
        }
        if len(confirmed_by_human) == 1:
            return ()

        flat = tuple(sorted(i for items in live.values() for i in items))
        return (IdentityDispute(
            subject=subject,
            identities=tuple(sorted(live)),
            first_seen_ms=min(i.ts_ms for i in flat),
            evidence=flat,
        ),)

    def explain(self, subject: str) -> Explanation:
        """Everything known about one subject, sorted into stances."""
        items = self._by_subject.get(subject, ())
        decision = self._decisions.get(subject)
        return Explanation(
            subject=subject,
            decision=decision.summary if decision else None,
            decided_at_ms=decision.ts_ms if decision else None,
            supporting=tuple(i for i in items if i.stance is Stance.SUPPORTS),
            contradicting=tuple(i for i in items if i.stance is Stance.CONTRADICTS),
            context=tuple(i for i in items if i.stance is Stance.CONTEXT),
            human_evidence=tuple(i for i in items if i.is_human),
            disputes=self.disputes_for(subject),
        )

    def all_disputes(self) -> tuple[IdentityDispute, ...]:
        out: list[IdentityDispute] = []
        for subject in self._by_subject:
            out.extend(self.disputes_for(subject))
        return tuple(sorted(out, key=lambda d: (d.first_seen_ms, d.subject)))

    def __len__(self) -> int:
        return sum(len(v) for v in self._by_subject.values())

    def __iter__(self) -> Iterator[Evidence]:
        return iter(sorted(i for items in self._by_subject.values() for i in items))

    def extend(self, items: Iterable[Evidence]) -> None:
        for item in items:
            insort(self._by_subject[item.subject], item)
            if item.kind is EvidenceKind.DECISION:
                self._decisions[item.subject] = item
