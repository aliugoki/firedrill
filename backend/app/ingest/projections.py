"""Read models over the event stream. Everything the command centre shows.

A projection stores nothing that cannot be recomputed from the ledger. That is
what makes invariant 6 true in practice rather than in principle: if a number on
screen could only have come from a counter someone incremented, it cannot be
explained, and it cannot be checked.

The part that matters most here is `resolve_identities`. Matching a roster entry
to a tracked person is the hinge the whole board turns on, and it must be done
using **only what the system claimed**, never ground truth. This logic lived in
the simulator first, where it had access to the answer; that made the board look
right and hid every misidentification. It lives here now so the simulator and
the edge service resolve identities the same way, with the same blind spots.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.accountability_fsm import (
    PROVISIONAL_CONFIG as ACC_CONFIG,
    AccountabilityConfig,
    AccountabilityState,
    Context,
    Decision,
    WardenEvidence,
)
from app.core.identity_fsm import IdentityState
from app.core.presence_fsm import PresenceState
from app.core.roster import Population, RosterEntry, RosterSnapshot
from app.ingest.health import HealthSummary
from app.ingest.ingestor import IngestState


@dataclass(frozen=True, slots=True)
class Resolution:
    """How one roster entry maps onto tracked people, and how confidently."""

    entry: RosterEntry
    claimed_by: tuple[str, ...] = ()
    contested_on: tuple[str, ...] = ()
    simultaneous: tuple[str, ...] = ()

    @property
    def is_observed(self) -> bool:
        return bool(self.claimed_by or self.contested_on)

    @property
    def is_ambiguous(self) -> bool:
        return bool(self.simultaneous or self.contested_on)


def resolve_identities(
    state: IngestState, roster: RosterSnapshot
) -> dict[str, Resolution]:
    """Map roster entries to tracked people by claimed identity.

    Two kinds of ambiguity are separated, because they mean different things.

    **Contested.** A track's identity is in CONFLICT or was rejected, so it has
    dropped its claim. Without looking at `conflict_with` the candidates vanish
    from the board entirely and the employees involved read as never observed —
    wrong twice over, since the system did see somebody who might be them and
    knows exactly why it cannot say.

    **Simultaneous.** Two tracks claim the same person while both are being
    observed. One person cannot be in two places, so neither track can be
    accounted for on camera evidence. Sequential tracks are deliberately not
    treated this way: those are ordinary fragmentation, one person's evidence
    split in half, and calling that a conflict would bury the real ones.
    """
    claimed: dict[str, list[str]] = {}
    contested: dict[str, list[str]] = {}

    for person in state.identity:
        if person.identity:
            claimed.setdefault(person.identity, []).append(person.person_id)
        for candidate in person.conflict_with:
            contested.setdefault(candidate, []).append(person.person_id)
        for rejected in person.rejected_identities:
            contested.setdefault(rejected, []).append(person.person_id)

    resolutions: dict[str, Resolution] = {}
    for entry in roster.entries:
        key = entry.emp_id or entry.person_ref
        gids = tuple(sorted(claimed.get(key, ())))
        contested_gids = tuple(sorted(contested.get(key, ())))
        resolutions[entry.person_ref] = Resolution(
            entry=entry,
            claimed_by=gids,
            contested_on=contested_gids,
            simultaneous=tuple(_simultaneous(state, gids, contested_gids)),
        )
    return resolutions


def _simultaneous(state: IngestState, claimed: tuple[str, ...],
                  contested: tuple[str, ...] = ()) -> list[str]:
    """Tracks that cannot all be this person, because they overlap in time.

    A contested track counts. What put it in CONFLICT is independent accepted
    observations naming this person, so another track claiming the same name at
    the same moment is the two-places-at-once case whatever the contested track
    later turns out to be.

    Leaving it out was a hole with the shape the whole system exists to close: a
    look-alike's track, confirmed as somebody and standing at the assembly
    point, accounted for a person whose own track was sitting in CONFLICT on
    another floor at the same time. The board cleared them. The contested
    employee got MANUAL_VERIFICATION_REQUIRED and the claimed one got ACCOUNTED,
    from one pool of evidence, purely because one name also had a confirmed
    track somewhere else.

    Two contested tracks overlapping each other are not reported here: with no
    claim at all, `decide` already refuses on `contested_on`.
    """
    everyone = tuple(claimed) + tuple(contested)
    if len(everyone) < 2:
        return []
    claiming = set(claimed)
    people = [state.presence.get(gid) for gid in everyone]
    clashing: set[str] = set()
    for i, a in enumerate(people):
        for b in people[i + 1:]:
            if not a.overlaps(b):
                continue
            if a.person_id not in claiming and b.person_id not in claiming:
                continue
            clashing.add(a.person_id)
            clashing.add(b.person_id)
    return sorted(clashing)


def decide(
    state: IngestState,
    resolution: Resolution,
    *,
    now_ms: int,
    warden: WardenEvidence | None = None,
    config: AccountabilityConfig = ACC_CONFIG,
) -> Decision:
    """Derive one person's accountability from everything known about them."""
    entry = resolution.entry
    elapsed = state.elapsed_ms(now_ms)
    warden = warden or WardenEvidence()

    if resolution.simultaneous:
        return Decision(
            person_id=entry.person_ref,
            state=AccountabilityState.MANUAL_VERIFICATION_REQUIRED,
            reason=(f"{len(resolution.simultaneous)} tracks were identified as "
                    f"{entry.emp_id or entry.display_name} at the same time; a "
                    "human must establish which is real"),
            blockers=("the same identity claimed by simultaneous tracks",))

    if resolution.contested_on and not resolution.claimed_by:
        return Decision(
            person_id=entry.person_ref,
            state=AccountabilityState.MANUAL_VERIFICATION_REQUIRED,
            reason=(f"{entry.emp_id or entry.display_name} is one of the "
                    f"contested identities on {len(resolution.contested_on)} "
                    "track(s); the system will not choose"),
            blockers=("identity contested between look-alikes",))

    best: Decision | None = None
    for gid in resolution.claimed_by:
        context = Context(
            person_id=entry.person_ref,
            drill_elapsed_ms=elapsed,
            is_expected=entry.is_expected,
            presence=state.presence.get(gid),
            identity=state.identity.get(gid),
            warden=warden,
            system_degraded=state.health.is_blind,
            degraded_reason=(", ".join(state.health.causes_at(now_ms))
                             if state.health.is_blind else None),
            config=config)
        candidate = _derive(context)
        if best is None or _RANK[candidate.state] < _RANK[best.state]:
            best = candidate

    if best is not None:
        return best

    return _derive(Context(
        person_id=entry.person_ref, drill_elapsed_ms=elapsed,
        is_expected=entry.is_expected, warden=warden,
        system_degraded=state.health.is_blind,
        degraded_reason=(", ".join(state.health.causes_at(now_ms))
                         if state.health.is_blind else None),
        config=config))


def _derive(context: Context) -> Decision:
    from app.core.accountability_fsm import derive

    return derive(context)


#: Used only to pick the most-informative track when one person has several.
#: Lower is more informative, not "better": a person who reached assembly on one
#: fragment did reach assembly, and a fragment that saw nothing adds nothing.
_RANK = {
    AccountabilityState.ACCOUNTED: 0,
    AccountabilityState.EVACUATING: 1,
    AccountabilityState.MANUAL_VERIFICATION_REQUIRED: 2,
    AccountabilityState.UNCERTAIN: 3,
    AccountabilityState.UNACCOUNTED: 4,
    AccountabilityState.NOT_EVACUATED: 5,
}


@dataclass(frozen=True, slots=True)
class PersonRow:
    """One line on the board. Everything an operator needs without a click."""

    person_ref: str
    display_name: str
    department: str | None
    assigned_assembly_zone: str | None
    decision: Decision
    last_zone_id: str | None
    last_camera_id: str | None
    last_seen_ms: int | None
    identity_state: IdentityState | None
    presence_state: PresenceState | None
    needs_human_to_account: bool

    @property
    def state(self) -> AccountabilityState:
        return self.decision.state

    @property
    def colour(self) -> str:
        """GREEN accounted, YELLOW uncertain or manual, ORANGE currently
        unobserved, RED unaccounted. No raw AI metrics reach the operator."""
        if self.state is AccountabilityState.ACCOUNTED:
            return "GREEN"
        if self.state is AccountabilityState.UNACCOUNTED:
            return "RED"
        if self.presence_state in (PresenceState.TEMPORARILY_UNOBSERVED,
                                   PresenceState.LOST):
            return "ORANGE"
        return "YELLOW"


@dataclass(frozen=True, slots=True)
class LiveBoard:
    """The headline projection. Every count here is derived, never stored."""

    now_ms: int
    rows: tuple[PersonRow, ...]
    health: HealthSummary
    roster_trustworthy: bool
    unknown_people: int = 0
    #: On the roster and not in `expected`, by reason. Nobody looks for these
    #: people and no row mentions them, so the number reaches the operator here
    #: or not at all.
    excluded_from_the_count: dict = field(default_factory=dict)

    # -- counts ----------------------------------------------------------------

    @property
    def expected(self) -> int:
        return len(self.rows)

    def count(self, state: AccountabilityState) -> int:
        return sum(1 for r in self.rows if r.state is state)

    @property
    def accounted(self) -> int:
        return self.count(AccountabilityState.ACCOUNTED)

    @property
    def unaccounted(self) -> int:
        return self.count(AccountabilityState.UNACCOUNTED)

    @property
    def uncertain(self) -> int:
        return (self.count(AccountabilityState.UNCERTAIN)
                + self.count(AccountabilityState.MANUAL_VERIFICATION_REQUIRED))

    @property
    def currently_unobserved(self) -> int:
        return sum(1 for r in self.rows
                   if r.presence_state in (PresenceState.TEMPORARILY_UNOBSERVED,
                                           PresenceState.LOST)
                   and r.state is not AccountabilityState.ACCOUNTED)

    @property
    def still_evacuating(self) -> int:
        return (self.count(AccountabilityState.EVACUATING)
                + self.count(AccountabilityState.NOT_EVACUATED))

    @property
    def all_clear(self) -> bool:
        """Whether every expected person is accounted for.

        Deliberately conjoined with system health. An all-clear declared while
        the system was blind is the single failure invariant 8 exists to
        prevent, so a blind system cannot produce one however good the counts
        look.
        """
        return (self.accounted == self.expected
                and self.expected > 0
                and not self.health.open_outages
                and self.roster_trustworthy)

    def blocking_all_clear(self) -> list[str]:
        """Why the board is not showing all clear. Never empty when it is not."""
        reasons: list[str] = []
        if self.expected == 0:
            reasons.append("no expected people on the roster")
        outstanding = self.expected - self.accounted
        if outstanding > 0:
            reasons.append(f"{outstanding} of {self.expected} people not accounted for")
        if self.health.open_outages:
            reasons.append(f"{self.health.open_outages} outage(s) still open")
        if not self.roster_trustworthy:
            reasons.append("the roster could not be verified against its source")
        return reasons

    # -- panels ----------------------------------------------------------------

    def priority(self, limit: int | None = None) -> tuple[PersonRow, ...]:
        """Who a human should look at, worst first.

        Ordered unaccounted, then manual verification, then uncertain, then
        currently unobserved; within a band, the person unseen longest first,
        because staleness is what makes a search urgent.
        """
        def sort_key(row: PersonRow) -> tuple:
            band = {
                AccountabilityState.UNACCOUNTED: 0,
                AccountabilityState.MANUAL_VERIFICATION_REQUIRED: 1,
                AccountabilityState.UNCERTAIN: 2,
            }.get(row.state, 3)
            staleness = -(row.last_seen_ms or 0)
            return (band, staleness, row.person_ref)

        needing = sorted((r for r in self.rows if r.decision.needs_attention),
                         key=sort_key)
        return tuple(needing[:limit] if limit else needing)

    def by_assembly_zone(self) -> dict[str, tuple[PersonRow, ...]]:
        grouped: dict[str, list[PersonRow]] = {}
        for row in self.rows:
            grouped.setdefault(row.assigned_assembly_zone or "unassigned",
                               []).append(row)
        return {zone: tuple(rows) for zone, rows in sorted(grouped.items())}

    def by_department(self) -> dict[str, tuple[PersonRow, ...]]:
        grouped: dict[str, list[PersonRow]] = {}
        for row in self.rows:
            grouped.setdefault(row.department or "unassigned", []).append(row)
        return {dept: tuple(rows) for dept, rows in sorted(grouped.items())}

    def needing_a_warden(self) -> tuple[PersonRow, ...]:
        """People the cameras can never settle: no gallery entry, or assisted."""
        return tuple(r for r in self.rows if r.needs_human_to_account
                     and r.state is not AccountabilityState.ACCOUNTED)


def build_board(
    state: IngestState,
    roster: RosterSnapshot,
    *,
    now_ms: int,
    warden_evidence: dict[str, WardenEvidence] | None = None,
    config: AccountabilityConfig = ACC_CONFIG,
    unknown_people: int = 0,
) -> LiveBoard:
    """Recompute the whole board. Cheap enough to do on every tick."""
    warden_evidence = warden_evidence or {}
    resolutions = resolve_identities(state, roster)
    rows: list[PersonRow] = []

    for entry in roster.expected:
        resolution = resolutions[entry.person_ref]
        decision = decide(
            state, resolution, now_ms=now_ms,
            warden=warden_evidence.get(entry.person_ref), config=config)

        presence = identity = None
        last_zone = last_camera = None
        last_seen = None
        for gid in resolution.claimed_by:
            candidate_presence = state.presence.get(gid)
            if (candidate_presence.last_sighting_ms or 0) >= (last_seen or 0):
                presence = candidate_presence
                identity = state.identity.get(gid)
                last_seen = candidate_presence.last_sighting_ms
                known = candidate_presence.last_known
                last_zone = known.zone_id if known else None
                last_camera = known.camera_id if known else None

        rows.append(PersonRow(
            person_ref=entry.person_ref, display_name=entry.display_name,
            department=entry.department,
            assigned_assembly_zone=entry.assigned_assembly_zone,
            decision=decision, last_zone_id=last_zone,
            last_camera_id=last_camera, last_seen_ms=last_seen,
            identity_state=identity.state if identity else None,
            presence_state=presence.state if presence else None,
            needs_human_to_account=entry.needs_human_to_account))

    start = state.drill_started_ms or now_ms
    return LiveBoard(
        now_ms=now_ms, rows=tuple(rows),
        health=state.health.summary(start, now_ms),
        roster_trustworthy=roster.is_trustworthy,
        unknown_people=unknown_people,
        excluded_from_the_count=roster.excluded_by_reason())
