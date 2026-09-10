"""Deciding what a zone is for, and refusing to guess when it matters.

VisionTrack zones carry an id, a name, a colour, alert rules and a polygon.
Nothing says whether a zone is a floor, an exit, an assembly point or a blind
spot, because VisionTrack never needed to know. EVAC-120 cannot function without
it: the entire presence machine is built on the distinction.

The obvious move is to infer the kind from the zone name, and it half works —
"North Car Park Assembly" is not ambiguous. The live VisionTrack instance this
was written against contains exactly one zone, and it is called **"Zone 1"**.
That is the realistic case, and it is why inference proposes rather than
decides.

Two rules make the refusal safe rather than merely cautious.

**An untagged zone is `UNKNOWN`, never a default.** Defaulting to `FLOOR` is
wrong if the zone is the muster point, and people would never be accounted for
there. Defaulting to `ASSEMBLY` is far worse: everyone standing in a corridor
would be marked safe. There is no harmless default, so there is no default.

**A site with no assembly zone cannot run a drill.** Not a warning: nobody could
ever be accounted for, and the board would show a building full of people it
believes are still inside while the drill quietly proves nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from app.core.presence_fsm import ZoneKind


class Confidence(str, Enum):
    CERTAIN = "CERTAIN"
    """A human tagged it. The only confidence that can run a drill."""

    LIKELY = "LIKELY"
    """The name says so unambiguously. Still needs confirming."""

    GUESSED = "GUESSED"
    """A weak signal. Shown to whoever is confirming, and nothing more."""

    UNKNOWN = "UNKNOWN"
    """No signal at all. The common case on a real site."""


#: Word patterns that suggest a kind, strongest first. Deliberately narrow:
#: a pattern that fires often is worse than one that never fires, because it
#: produces confident wrong answers that a tired person confirms.
_PATTERNS: tuple[tuple[ZoneKind, str, Confidence], ...] = (
    (ZoneKind.ASSEMBLY, r"\b(assembly|muster|rally)\b", Confidence.LIKELY),
    (ZoneKind.ASSEMBLY, r"\b(car\s*park|garden|plaza|forecourt)\b", Confidence.GUESSED),
    (ZoneKind.EXIT, r"\b(fire\s*exit|emergency\s*exit|exit|egress)\b", Confidence.LIKELY),
    (ZoneKind.EXIT, r"\b(entrance|lobby\s*door|turnstile|gate)\b", Confidence.GUESSED),
    (ZoneKind.BLIND, r"\b(stair|stairwell|lift|elevator|riser|duct)\b", Confidence.LIKELY),
    (ZoneKind.BLIND, r"\b(plant\s*room|store|cupboard|toilet|wc)\b", Confidence.GUESSED),
    (ZoneKind.FLOOR, r"\b(open\s*plan|office|desk|workshop|floor|corridor)\b",
     Confidence.GUESSED),
)


@dataclass(frozen=True, slots=True)
class Proposal:
    """What the sync thinks a zone is, and how much that is worth."""

    zone_id: str
    zone_name: str
    kind: ZoneKind | None
    confidence: Confidence
    because: str

    @property
    def needs_confirmation(self) -> bool:
        return self.confidence is not Confidence.CERTAIN

    @property
    def is_usable(self) -> bool:
        """Whether a drill can run on this zone as it stands."""
        return self.kind is not None and self.confidence is Confidence.CERTAIN

    def describe(self) -> str:
        if self.kind is None:
            return (f"{self.zone_name}: no kind. A drill cannot run until "
                    "someone says what this zone is for.")
        return (f"{self.zone_name}: {self.kind.value} "
                f"({self.confidence.value.lower()}) — {self.because}")


def propose(zone_id: str, zone_name: str,
            tagged: dict | None = None) -> Proposal:
    """Work out what a zone is for. A human tag always wins.

    `tagged` maps zone id to kind and is the site's own record of what it
    decided. It outranks any inference, including a confident one: the name of a
    zone is a label somebody typed, and what it is for is a decision somebody
    made.
    """
    tagged = tagged or {}
    if zone_id in tagged:
        kind = tagged[zone_id]
        kind = kind if isinstance(kind, ZoneKind) else ZoneKind(kind)
        return Proposal(zone_id=zone_id, zone_name=zone_name, kind=kind,
                        confidence=Confidence.CERTAIN,
                        because="tagged for this site")

    haystack = (zone_name or "").lower()
    for kind, pattern, confidence in _PATTERNS:
        match = re.search(pattern, haystack)
        if match:
            return Proposal(
                zone_id=zone_id, zone_name=zone_name, kind=kind,
                confidence=confidence,
                because=f'the name contains "{match.group(0)}"')

    return Proposal(zone_id=zone_id, zone_name=zone_name, kind=None,
                    confidence=Confidence.UNKNOWN,
                    because="nothing in the name says what this zone is for")


@dataclass(frozen=True, slots=True)
class TaggingReport:
    """Whether a site's zones are ready to run a drill."""

    proposals: tuple
    ready: bool
    blockers: tuple
    needing_confirmation: tuple

    def describe(self) -> list[str]:
        lines = ["Zone tagging" if self.ready else "Zone tagging — NOT READY", ""]
        for proposal in self.proposals:
            mark = "  ok " if proposal.is_usable else "  ?? "
            lines.append(mark + proposal.describe())
        if self.blockers:
            lines += ["", "Blocking:"]
            lines += [f"  - {b}" for b in self.blockers]
        return lines


def review(proposals: list) -> TaggingReport:
    """Judge a whole site's zones.

    Two separate failures, and they read differently. Untagged zones are work
    somebody has to do. No assembly zone at all is a site that cannot hold a
    drill, however many zones are tagged.
    """
    untagged = [p for p in proposals if not p.is_usable]
    assembly = [p for p in proposals
                if p.is_usable and p.kind is ZoneKind.ASSEMBLY]

    blockers: list[str] = []
    if untagged:
        blockers.append(
            f"{len(untagged)} zone(s) have no confirmed kind: "
            + ", ".join(p.zone_name for p in untagged[:5])
            + ("…" if len(untagged) > 5 else ""))
    if not assembly:
        blockers.append(
            "no zone is tagged ASSEMBLY. Nobody could ever be accounted for, "
            "and the board would show a building full of people it believes "
            "are still inside")

    return TaggingReport(
        proposals=tuple(proposals),
        ready=not blockers,
        blockers=tuple(blockers),
        needing_confirmation=tuple(p for p in proposals if p.needs_confirmation))
