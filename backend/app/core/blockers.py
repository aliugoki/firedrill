"""Why an all-clear is being refused, as something a screen can translate.

The board already refuses to show ALL CLEAR for a list of specific reasons and
has always said them in English prose. That is right for the post-drill report,
which is a document. It is wrong for the screen, which is read in Arabic at a
site in Sialkot, and the blocking list is the part a commander reads before
deciding whether to keep people outside.

`accountability_fsm.ReasonCode` made this move for the reason under a person's
name. This is the same move for the reasons under the verdict, and it is
arranged the same way: the code and the values travel, the wording happens in
the browser, and `describe()` keeps the English sentence for the report and for
any reader that does not know the code.

One source for both. `blocking_clean()` and its siblings return
`[b.describe() for b in ...blockers(...)]`, because two methods computing the
same list in two shapes is the drift this codebase keeps finding.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class BlockerCode(str, Enum):
    """Every reason an all-clear can be refused."""

    DRILL_NOT_STARTED = "DRILL_NOT_STARTED"
    NO_EXPECTED_PEOPLE = "NO_EXPECTED_PEOPLE"
    PEOPLE_UNACCOUNTED = "PEOPLE_UNACCOUNTED"
    OPEN_OUTAGES = "OPEN_OUTAGES"
    ROSTER_UNVERIFIED = "ROSTER_UNVERIFIED"

    NO_ZONES = "NO_ZONES"
    UNASSIGNED_PEOPLE = "UNASSIGNED_PEOPLE"
    NO_SWEEP_STARTED = "NO_SWEEP_STARTED"
    SWEEP_NOT_STARTED = "SWEEP_NOT_STARTED"
    SWEEP_IN_PROGRESS = "SWEEP_IN_PROGRESS"
    SWEEP_ESCALATED = "SWEEP_ESCALATED"
    ZONE_UNCONFIRMED = "ZONE_UNCONFIRMED"
    NO_HEADCOUNT = "NO_HEADCOUNT"
    HEADCOUNT_MISMATCH = "HEADCOUNT_MISMATCH"


#: The English sentence for each code. Held here rather than at the point the
#: blocker is built, so the prose and the code cannot drift apart and a reader
#: can see the whole vocabulary of refusals in one place.
_WORDS: dict[BlockerCode, str] = {
    BlockerCode.DRILL_NOT_STARTED: "the drill has not been started",
    BlockerCode.NO_EXPECTED_PEOPLE: "no expected people on the roster",
    BlockerCode.PEOPLE_UNACCOUNTED:
        "{outstanding} of {expected} people not accounted for",
    BlockerCode.OPEN_OUTAGES: "{count} outage(s) still open",
    BlockerCode.ROSTER_UNVERIFIED:
        "the roster could not be verified against its source",
    BlockerCode.NO_ZONES: "no zones have any expected people",
    BlockerCode.UNASSIGNED_PEOPLE:
        "{count} person(s) have no assembly zone on the roster, so no warden's "
        "list includes them and nobody can sweep for them",
    BlockerCode.NO_SWEEP_STARTED: "{zone_id}: no warden has started a sweep",
    BlockerCode.SWEEP_NOT_STARTED: "{zone_id}: the sweep has not been started",
    BlockerCode.SWEEP_IN_PROGRESS: "{zone_id}: the sweep is still in progress",
    BlockerCode.SWEEP_ESCALATED: "{zone_id}: escalated: {reason}",
    BlockerCode.ZONE_UNCONFIRMED:
        "{zone_id}: {count} person(s) in this zone have not been confirmed or "
        "reported",
    BlockerCode.NO_HEADCOUNT: "{zone_id}: no physical headcount has been recorded",
    BlockerCode.HEADCOUNT_MISMATCH: "{zone_id}: {summary}",
}


@dataclass(frozen=True, slots=True)
class Blocker:
    """One refusal, as a code and the values its wording needs."""

    code: BlockerCode
    detail: dict = field(default_factory=dict)

    def describe(self) -> str:
        """The English sentence. What the report prints and what a client that
        does not know the code falls back to."""
        return _WORDS[self.code].format(**self.detail)


def describe_all(blockers) -> list[str]:
    return [blocker.describe() for blocker in blockers]
