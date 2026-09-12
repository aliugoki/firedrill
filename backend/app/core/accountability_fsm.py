"""Accountability state machine: is this person safe, and how do we know?

This is the only module that answers the operational question, and it answers it
by **deriving**, never by storing. There is no `is_evacuated` flag anywhere
(invariant 4). Given presence, identity, warden evidence and system health, the
state falls out; change any input and the state changes with it.

Six states:

    NOT_EVACUATED                 expected, and not yet moving out
    EVACUATING                    on the way, seen heading for or through an exit
    ACCOUNTED                     safe, with evidence that qualifies
    UNCERTAIN                     evidence exists but does not settle the question
    UNACCOUNTED                   expected, drill has run long, no qualifying evidence
    MANUAL_VERIFICATION_REQUIRED  the system cannot decide; a human must look

`ACCOUNTED` has exactly two routes in, and both are checked in `derive`:

    1. the system saw them reach an assembly zone AND their identity is
       confirmed (or was confirmed and the face has merely gone away);
    2. a warden confirmed them in person at an assembly zone.

Nothing else sets it. Not a high match score, not a long dwell, not a healthy
camera. This is the single most important rule in the system, and it is asserted
as a property test over every failure injection the simulator can produce.

The ordering in `derive` is itself a safety property, so it is spelled out:

    conflict beats everything      an unresolved identity dispute cannot be
                                   accounted for, however good the rest looks
    humans beat cameras            invariant 9
    cameras beat silence           a real observation beats an inference
    blindness beats inference      invariant 8: if we could not see, we say so
                                   rather than guessing
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.core.identity_fsm import IdentityState, PersonIdentity
from app.core.presence_fsm import PersonPresence, PresenceState


class AccountabilityState(str, Enum):
    NOT_EVACUATED = "NOT_EVACUATED"
    EVACUATING = "EVACUATING"
    ACCOUNTED = "ACCOUNTED"
    UNCERTAIN = "UNCERTAIN"
    UNACCOUNTED = "UNACCOUNTED"
    MANUAL_VERIFICATION_REQUIRED = "MANUAL_VERIFICATION_REQUIRED"


#: States that need a human to look at this person. The command centre's
#: priority list is exactly these, ordered by how long they have been there.
NEEDS_ATTENTION: frozenset[AccountabilityState] = frozenset({
    AccountabilityState.UNCERTAIN,
    AccountabilityState.UNACCOUNTED,
    AccountabilityState.MANUAL_VERIFICATION_REQUIRED,
})

#: The only state that means "safe". Kept as a set of one so no future edit can
#: quietly widen what counts as safe without this line changing.
SAFE: frozenset[AccountabilityState] = frozenset({AccountabilityState.ACCOUNTED})


@dataclass(frozen=True, slots=True)
class AccountabilityConfig:
    """When silence starts to mean something.

    `unaccounted_after_ms` is the point at which "we have not seen them yet"
    becomes "we cannot account for them". It is a claim about the drill, not
    about the person, and it is configuration because the right value depends on
    building size and drill protocol.
    """

    unaccounted_after_ms: int
    calibrated: bool = False
    source: str = "uncalibrated"

    def __post_init__(self) -> None:
        if self.unaccounted_after_ms <= 0:
            raise ValueError("unaccounted_after_ms must be positive")


PROVISIONAL_CONFIG = AccountabilityConfig(
    unaccounted_after_ms=180_000,
    calibrated=False,
    source="Phase 1 placeholder, unvalidated",
)


@dataclass(frozen=True, slots=True)
class WardenEvidence:
    """What a human physically established, and where.

    `at_assembly_zone` is the crux: a warden standing at the muster point who
    confirms a person has established both halves of the ACCOUNTED test at once,
    because they are the assembly-zone observation as well as the identity one.
    A confirmation recorded anywhere else is identity evidence only.
    """

    confirmed: bool = False
    rejected: bool = False
    at_assembly_zone: str | None = None
    marked_absent: bool = False
    warden_id: str | None = None
    ts_ms: int | None = None

    @property
    def confirms_at_assembly(self) -> bool:
        return self.confirmed and self.at_assembly_zone is not None


@dataclass(frozen=True, slots=True)
class Context:
    """Everything `derive` is allowed to look at. Nothing hidden, nothing global."""

    person_id: str
    drill_elapsed_ms: int
    is_expected: bool = True
    presence: PersonPresence | None = None
    identity: PersonIdentity | None = None
    warden: WardenEvidence = field(default_factory=WardenEvidence)
    system_degraded: bool = False
    degraded_reason: str | None = None
    config: AccountabilityConfig = PROVISIONAL_CONFIG


class ReasonCode(str, Enum):
    """Why a person is in the state they are in, as something to translate.

    `reason` is English prose and stays that way: the post-drill report is a
    document a safety officer reads, and prose is what a document is made of.
    A screen is not. The warden PWA is read in Arabic on a tablet at an
    assembly point, and the reason under a person's name is the sentence that
    tells the warden what to do about them -- so it arrives as a code and a
    handful of values, and the screen words it in the language being read.

    `timing.py` made the same call for its caveats and says why: "worded here
    rather than passed through from the server, because the server's caveats
    are English prose and this screen is read in Arabic too". The reasons were
    the other half of that and stayed English.
    """

    IDENTITY_DISPUTED = "IDENTITY_DISPUTED"
    WARDEN_REJECTED_IDENTITY = "WARDEN_REJECTED_IDENTITY"
    WARDEN_CONFIRMED_AT_ASSEMBLY = "WARDEN_CONFIRMED_AT_ASSEMBLY"
    ASSEMBLY_WITH_IDENTITY = "ASSEMBLY_WITH_IDENTITY"
    ASSEMBLY_WITHOUT_IDENTITY = "ASSEMBLY_WITHOUT_IDENTITY"
    COVERAGE_DEGRADED = "COVERAGE_DEGRADED"
    NOT_ON_ROSTER = "NOT_ON_ROSTER"
    WARDEN_MARKED_ABSENT = "WARDEN_MARKED_ABSENT"
    TRACK_LOST = "TRACK_LOST"
    BRIEFLY_UNOBSERVED = "BRIEFLY_UNOBSERVED"
    MOVING_THROUGH_EXIT = "MOVING_THROUGH_EXIT"
    INSIDE_OVERDUE = "INSIDE_OVERDUE"
    INSIDE = "INSIDE"
    NEVER_OBSERVED_OVERDUE = "NEVER_OBSERVED_OVERDUE"
    NEVER_OBSERVED = "NEVER_OBSERVED"


@dataclass(frozen=True, slots=True)
class Decision:
    """A state and the reason for it. The reason is not optional."""

    person_id: str
    state: AccountabilityState
    reason: str
    qualifying_evidence: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    code: ReasonCode | None = None
    """The same reason, as something a screen can translate. Optional in the
    type and mandatory in practice: a branch that returns no code renders as
    English on an Arabic tablet, which `TestEveryBranchCanBeTranslated`
    refuses."""
    detail: dict = field(default_factory=dict)
    """The values the wording needs -- a warden's name, a zone, a count of
    seconds, where somebody was last seen. Names rather than positions, so a
    translation can put them in a different order."""

    @property
    def is_safe(self) -> bool:
        return self.state in SAFE

    @property
    def needs_attention(self) -> bool:
        return self.state in NEEDS_ATTENTION


def derive(ctx: Context) -> Decision:
    """Compute accountability from evidence. Pure, total, and order-sensitive.

    Every branch returns a reason. A state without a reason would be a state
    nobody can audit, which invariant 6 forbids.
    """
    # --- 1. An unresolved identity dispute outranks everything ---------------
    # Invariant 3. Being at the assembly point with a contested identity means
    # *somebody* is safe, but not that this person is. Marking them accounted
    # would be exactly the silent resolution the invariant forbids.
    if ctx.identity is not None and ctx.identity.needs_human:
        detail = (
            f"identity is {ctx.identity.state.value}"
            + (f" between {', '.join(ctx.identity.conflict_with)}"
               if ctx.identity.conflict_with else "")
        )
        return Decision(
            ctx.person_id, AccountabilityState.MANUAL_VERIFICATION_REQUIRED,
            reason=f"{detail}; the system will not choose between them",
            blockers=(detail,),
            code=ReasonCode.IDENTITY_DISPUTED,
            detail={"state": ctx.identity.state.value,
                    "identities": list(ctx.identity.conflict_with)},
        )

    if ctx.warden.rejected:
        return Decision(
            ctx.person_id, AccountabilityState.MANUAL_VERIFICATION_REQUIRED,
            reason="a warden rejected the system's identity for this person",
            blockers=("warden rejection",),
            code=ReasonCode.WARDEN_REJECTED_IDENTITY,
        )

    # --- 2. A human at the muster point outranks every camera ----------------
    # Invariant 9. This is deliberately above the degradation check: a warden's
    # eyes do not stop working because a camera did.
    if ctx.warden.confirms_at_assembly:
        return Decision(
            ctx.person_id, AccountabilityState.ACCOUNTED,
            reason=(
                f"warden {ctx.warden.warden_id or 'unknown'} confirmed in person "
                f"at assembly zone {ctx.warden.at_assembly_zone}"
            ),
            qualifying_evidence=("warden confirmation at an assembly zone",),
            code=ReasonCode.WARDEN_CONFIRMED_AT_ASSEMBLY,
            detail={"warden_id": ctx.warden.warden_id or "",
                    "zone_id": ctx.warden.at_assembly_zone or ""},
        )

    # --- 3. System evidence: both halves, or nothing -------------------------
    reached_assembly = ctx.presence is not None and ctx.presence.was_at_assembly
    identity_usable = (
        ctx.identity is not None and ctx.identity.is_usable_for_accountability
    )

    if reached_assembly and identity_usable:
        assert ctx.identity is not None
        note = (
            " (face not currently visible, identity confirmed earlier)"
            if ctx.identity.state is IdentityState.TEMPORARILY_UNAVAILABLE else ""
        )
        return Decision(
            ctx.person_id, AccountabilityState.ACCOUNTED,
            reason=f"observed at an assembly zone with a confirmed identity{note}",
            qualifying_evidence=(
                "assembly-zone presence",
                f"identity {ctx.identity.state.value}",
            ),
            code=ReasonCode.ASSEMBLY_WITH_IDENTITY,
            detail={"face_visible": not note},
        )

    if reached_assembly and not identity_usable:
        # Somebody is standing at the muster point. Who, we do not know. That is
        # a person to walk up to, not a person to mark safe.
        return Decision(
            ctx.person_id, AccountabilityState.UNCERTAIN,
            reason="someone reached an assembly zone but their identity is not confirmed",
            qualifying_evidence=("assembly-zone presence",),
            blockers=("identity not confirmed",),
            code=ReasonCode.ASSEMBLY_WITHOUT_IDENTITY,
        )

    # --- 4. If we could not see, say so ---------------------------------------
    # Invariant 8. Everything below this line is an inference from what we did
    # not observe, and an inference drawn while blind is worthless.
    presence_degraded = ctx.presence is not None and ctx.presence.is_degraded
    if ctx.system_degraded or presence_degraded:
        why = ctx.degraded_reason or "coverage degraded"
        return Decision(
            ctx.person_id, AccountabilityState.MANUAL_VERIFICATION_REQUIRED,
            reason=f"cannot decide while coverage is degraded: {why}",
            blockers=(why,),
            code=ReasonCode.COVERAGE_DEGRADED,
            detail={"why": why},
        )

    if identity_usable and not reached_assembly:
        # Known person, definitely still inside. This is the operationally
        # urgent case and it is not softened.
        return _still_inside(ctx, identity_known=True)

    # --- 5. No qualifying evidence -------------------------------------------
    if not ctx.is_expected:
        # An unknown person: a visitor, a contractor, someone off the roster.
        # They are never forced onto an employee record (invariant 5), and they
        # are never quietly dropped either.
        return Decision(
            ctx.person_id, AccountabilityState.UNCERTAIN,
            reason="not on the roster; an unknown person needing a visitor or "
                   "contractor tag",
            blockers=("not on the roster",),
            code=ReasonCode.NOT_ON_ROSTER,
        )

    return _still_inside(ctx, identity_known=identity_usable)


def _still_inside(ctx: Context, *, identity_known: bool) -> Decision:
    """Nobody has seen this person reach safety. How worried should we be?"""
    presence_state = ctx.presence.state if ctx.presence else PresenceState.NOT_OBSERVED
    overdue = ctx.drill_elapsed_ms >= ctx.config.unaccounted_after_ms

    if ctx.warden.marked_absent:
        # A warden checked and says this person is not in the building at all —
        # on leave, off site, working from home. Human evidence, so it settles
        # the question, but it is not "safe": it is "not applicable".
        return Decision(
            ctx.person_id, AccountabilityState.UNCERTAIN,
            reason=f"warden {ctx.warden.warden_id or 'unknown'} reports this person "
                   "is not on site today; confirm against the roster",
            qualifying_evidence=("warden marked absent",),
            blockers=("absence not independently verified",),
            code=ReasonCode.WARDEN_MARKED_ABSENT,
            detail={"warden_id": ctx.warden.warden_id or ""},
        )

    if presence_state is PresenceState.LOST:
        return Decision(
            ctx.person_id, AccountabilityState.UNCERTAIN,
            reason=_last_seen(ctx, "track went stale"),
            blockers=("track lost before reaching an assembly zone",),
            code=ReasonCode.TRACK_LOST, detail=_where(ctx),
        )

    if presence_state is PresenceState.TEMPORARILY_UNOBSERVED:
        return Decision(
            ctx.person_id, AccountabilityState.EVACUATING,
            reason=_last_seen(ctx, "briefly unobserved, still within the grace window"),
            code=ReasonCode.BRIEFLY_UNOBSERVED, detail=_where(ctx),
        )

    if presence_state is PresenceState.IN_TRANSIT:
        return Decision(
            ctx.person_id, AccountabilityState.EVACUATING,
            reason=_last_seen(ctx, "moving through an exit"),
            code=ReasonCode.MOVING_THROUGH_EXIT, detail=_where(ctx),
        )

    if presence_state is PresenceState.IN_BUILDING:
        if overdue:
            return Decision(
                ctx.person_id, AccountabilityState.UNACCOUNTED,
                reason=_last_seen(
                    ctx,
                    f"still inside {ctx.drill_elapsed_ms // 1000} s into the drill",
                ),
                blockers=("inside the building, past the drill deadline",),
                code=ReasonCode.INSIDE_OVERDUE,
                detail={**_where(ctx),
                        "seconds": ctx.drill_elapsed_ms // 1000},
            )
        return Decision(
            ctx.person_id, AccountabilityState.NOT_EVACUATED,
            reason=_last_seen(ctx, "inside the building"),
            code=ReasonCode.INSIDE, detail=_where(ctx),
        )

    # NOT_OBSERVED: expected on the roster, never seen by any camera.
    if overdue:
        return Decision(
            ctx.person_id, AccountabilityState.UNACCOUNTED,
            reason=(
                f"on the roster but never observed, {ctx.drill_elapsed_ms // 1000} s "
                "into the drill"
            ),
            blockers=("no observation at all",),
            code=ReasonCode.NEVER_OBSERVED_OVERDUE,
            detail={"seconds": ctx.drill_elapsed_ms // 1000},
        )
    return Decision(
        ctx.person_id, AccountabilityState.NOT_EVACUATED,
        reason="on the roster, not yet observed",
        code=ReasonCode.NEVER_OBSERVED,
    )


def _where(ctx: Context) -> dict:
    """Where a human should go and look, as values rather than as a sentence."""
    known = ctx.presence.last_known if ctx.presence else None
    if known is None:
        return {}
    return {"zone_kind": known.zone_kind.value, "zone_id": known.zone_id,
            "camera_id": known.camera_id or ""}


def _last_seen(ctx: Context, situation: str) -> str:
    """Phrase a decision around where a human should go and look."""
    known = ctx.presence.last_known if ctx.presence else None
    if known is None:
        return situation
    return (
        f"{situation}; last seen in {known.zone_kind.value} zone {known.zone_id}"
        + (f" on {known.camera_id}" if known.camera_id else "")
    )


class AccountabilityBoard:
    """Derived accountability for everyone in a drill.

    Holds no state of its own beyond the last decision per person, which exists
    only so transitions can be detected and turned into events. Ask it again
    with different inputs and it will answer differently, which is the point.
    """

    def __init__(self, config: AccountabilityConfig = PROVISIONAL_CONFIG) -> None:
        self.config = config
        self._last: dict[str, Decision] = {}

    def evaluate(self, ctx: Context) -> tuple[Decision, bool]:
        """Derive one person's state. Returns the decision and whether it changed."""
        decision = derive(ctx)
        previous = self._last.get(ctx.person_id)
        self._last[ctx.person_id] = decision
        return decision, previous is None or previous.state is not decision.state

    def last(self, person_id: str) -> Decision | None:
        return self._last.get(person_id)

    def tally(self) -> dict[AccountabilityState, int]:
        counts = {state: 0 for state in AccountabilityState}
        for decision in self._last.values():
            counts[decision.state] += 1
        return counts

    def needing_attention(self) -> list[Decision]:
        return [d for d in self._last.values() if d.needs_attention]

    def accounted(self) -> list[Decision]:
        return [d for d in self._last.values() if d.is_safe]

    def __len__(self) -> int:
        return len(self._last)
