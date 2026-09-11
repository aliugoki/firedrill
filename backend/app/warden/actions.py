"""What a warden asserts, and the provenance that makes it trustworthy.

Every action carries the warden's identity, their device, the timestamp, and
whether the device was online when they made it. The offline flag is not
bookkeeping: a confirmation queued on a device that has been out of contact for
six minutes was made against a roster that may since have changed, and an
operator reconciling a disagreement needs to know that.

Actions are **immutable and append-only**, like the evidence ledger they feed.
A warden who changes their mind produces a second action, and both stand. That
is what makes a sequence of contradictory confirmations visible rather than
silently collapsing to whichever arrived last.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum

from app.core.events import Event, EventType, SourceKind


class ActionKind(str, Enum):
    """What a warden can assert. Each maps to exactly one event type."""

    CONFIRM_PRESENT = "CONFIRM_PRESENT"
    """This person is standing in front of me."""

    NOT_HERE = "NOT_HERE"
    """This person is not at my assembly point. Not the same as missing:
    they may be at the other muster point, or never came in today."""

    WRONG_PERSON = "WRONG_PERSON"
    """The system's identity for this person is wrong. Raises a conflict."""

    MARK_ABSENT = "MARK_ABSENT"
    """This person is not on site today. Human knowledge the roster lacks."""

    TAG_UNKNOWN = "TAG_UNKNOWN"
    """An unrecognised person is a visitor or contractor."""

    SWEEP_COMPLETE = "SWEEP_COMPLETE"
    """I have physically checked my whole zone."""

    ESCALATE = "ESCALATE"
    """I need help, or something is wrong that the system cannot see."""

    NOTE = "NOTE"
    """Free text or a transcribed voice note."""


#: Actions that assert something about one named person.
PERSON_ACTIONS: frozenset[ActionKind] = frozenset({
    ActionKind.CONFIRM_PRESENT, ActionKind.NOT_HERE, ActionKind.WRONG_PERSON,
    ActionKind.MARK_ABSENT,
})

#: Actions that can only be taken by a warden physically at their zone.
#: Enforced because the value of a confirmation is that someone stood there and
#: looked; one made from a desk is a different and much weaker claim.
REQUIRES_PRESENCE_AT_ZONE: frozenset[ActionKind] = frozenset({
    ActionKind.CONFIRM_PRESENT, ActionKind.SWEEP_COMPLETE,
})


class WardenActionError(ValueError):
    """An action that cannot be trusted as recorded."""


@dataclass(frozen=True, slots=True)
class WardenAction:
    """One immutable assertion by a human.

    `queued_offline` and `synced_at_ms` together say how stale the assertion
    was when it reached the system. A confirmation made offline and synced four
    minutes later is still valid; it is just evidence about four minutes ago.
    """

    kind: ActionKind
    warden_id: str
    device_id: str
    zone_id: str
    ts_ms: int
    subject: str | None = None
    identity: str | None = None
    note: str | None = None
    queued_offline: bool = False
    synced_at_ms: int | None = None
    action_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def sync_lag_ms(self) -> int | None:
        """How long the assertion sat on a device before reaching the system."""
        if self.synced_at_ms is None:
            return None
        return max(0, self.synced_at_ms - self.ts_ms)

    @property
    def is_about_a_person(self) -> bool:
        return self.kind in PERSON_ACTIONS

    def describe(self) -> str:
        who = f" for {self.subject}" if self.subject else ""
        how = " (queued offline)" if self.queued_offline else ""
        return (f"{self.kind.value.lower().replace('_', ' ')}{who} by "
                f"{self.warden_id} at {self.zone_id}{how}")


def validate(action: WardenAction) -> WardenAction:
    """Return the action, or raise. Provenance is not optional."""
    for name in ("warden_id", "device_id", "zone_id"):
        value = getattr(action, name)
        if not isinstance(value, str) or not value.strip():
            raise WardenActionError(
                f"{name} is required: an action without it cannot be attributed")

    if not isinstance(action.kind, ActionKind):
        raise WardenActionError(f"unknown action kind: {action.kind!r}")

    if action.ts_ms < 0:
        raise WardenActionError("ts_ms must be non-negative epoch milliseconds")

    if action.is_about_a_person and not action.subject:
        raise WardenActionError(
            f"{action.kind.value} must name the person it is about")

    if action.kind is ActionKind.WRONG_PERSON:
        if not action.identity:
            raise WardenActionError(
                "WRONG_PERSON must name the identity being rejected, or there "
                "is nothing for the system to stop believing")
        if ":" in action.identity:
            # A gallery identity is `EMP-0001`; a person reference is
            # `emp:EMP-0001`. The warden tablet sent the second, which is
            # truthy, so this passed and then matched no candidate the matcher
            # ever proposed -- the warden's rejection was recorded and did
            # nothing. Shape is all this layer can check, and it is enough to
            # turn a silent no-op into a refusal the warden sees.
            raise WardenActionError(
                f"identity must be the gallery identity the system claimed, "
                f"not a person reference ({action.identity})")

    if action.kind is ActionKind.TAG_UNKNOWN and not action.note:
        raise WardenActionError(
            "TAG_UNKNOWN must say what the person is (visitor, contractor)")

    if action.kind is ActionKind.NOTE and not (action.note or "").strip():
        raise WardenActionError("an empty note records nothing")

    if action.synced_at_ms is not None and action.synced_at_ms < action.ts_ms:
        raise WardenActionError(
            "synced_at_ms precedes ts_ms: the device clock is wrong, and an "
            "action whose time cannot be trusted cannot be evidence")

    return action


#: How each action becomes an event. TAG_UNKNOWN and ESCALATE have no dedicated
#: event type, so they are recorded as notes rather than invented into the
#: vocabulary; the event schema is deliberately closed.
_EVENT_FOR: dict[ActionKind, EventType] = {
    ActionKind.CONFIRM_PRESENT: EventType.WARDEN_CONFIRMED,
    ActionKind.NOT_HERE: EventType.WARDEN_NOTE,
    ActionKind.WRONG_PERSON: EventType.WARDEN_REJECTED,
    ActionKind.MARK_ABSENT: EventType.WARDEN_NOTE,
    ActionKind.TAG_UNKNOWN: EventType.WARDEN_NOTE,
    ActionKind.SWEEP_COMPLETE: EventType.WARDEN_SWEEP_COMPLETE,
    ActionKind.ESCALATE: EventType.WARDEN_NOTE,
    ActionKind.NOTE: EventType.WARDEN_NOTE,
}


def to_event(
    action: WardenAction, *, tenant_id: str, site_id: str, drill_id: str, seq: int
) -> Event:
    """Turn a validated action into an ingestible event.

    The device is the event source, so a device that starts producing nonsense
    can be identified and its sequence tracked independently of every other
    warden's.
    """
    validate(action)
    payload: dict = {
        "action_id": action.action_id,
        "action": action.kind.value,
        "warden_id": action.warden_id,
        "device_id": action.device_id,
        "zone_id": action.zone_id,
        "queued_offline": action.queued_offline,
        "human": True,
    }
    if action.identity:
        payload["identity"] = action.identity
    if action.note:
        payload["note"] = action.note
    if action.sync_lag_ms is not None:
        payload["sync_lag_ms"] = action.sync_lag_ms

    # A confirmation is only an assembly-zone confirmation when the warden was
    # standing at the zone. This is what lets the accountability machine treat
    # it as both halves of the ACCOUNTED test.
    if action.kind is ActionKind.CONFIRM_PRESENT:
        payload["at_assembly_zone"] = action.zone_id
    if action.kind is ActionKind.NOT_HERE:
        payload["note"] = (action.note
                           or f"warden reports {action.subject} is not at "
                              f"{action.zone_id}")
    if action.kind is ActionKind.MARK_ABSENT:
        payload["marked_absent"] = True
        payload["note"] = (action.note
                           or f"warden reports {action.subject} is not on site today")

    return Event(
        tenant_id=tenant_id, site_id=site_id, drill_id=drill_id,
        source=f"warden-device:{action.device_id}", source_kind=SourceKind.WARDEN,
        seq=seq, type=_EVENT_FOR[action.kind], ts_ms=action.ts_ms,
        subject=action.subject or action.zone_id, payload=payload,
    )


def from_event(event: Event) -> WardenAction | None:
    """The inverse of `to_event`, for rebuilding warden state after a restart.

    `Drill.recover` promised warden state was rebuilt "because warden actions
    are events like any other", and replayed the events into the ingest fold
    only. The fold knows about confirmations and rejections, which is why an
    ACCOUNTED person came back; it knows nothing about sweeps, notes,
    escalations or tagged unknowns, so a node that restarted mid-drill came
    back with every zone unswept and asked its wardens to walk them again.

    Returns None for anything this device did not produce. The action kind is
    read from the payload rather than inferred from the event type, because
    four kinds share `WARDEN_NOTE` -- the vocabulary is deliberately closed and
    guessing from the type would turn an escalation into a plain note.
    """
    if event.source_kind is not SourceKind.WARDEN:
        return None
    payload = event.payload or {}
    kind_name = payload.get("action")
    try:
        kind = ActionKind(kind_name)
    except ValueError:
        # A warden-sourced event whose payload does not name a kind this build
        # knows. Dropping it silently would be the quiet data loss this whole
        # function exists to undo, so it is the caller's problem to report.
        raise WardenActionError(
            f"warden event {event.event_id} carries no known action kind "
            f"({kind_name!r})")

    zone_id = payload.get("zone_id") or ""
    subject = event.subject if event.subject != zone_id else None
    return WardenAction(
        kind=kind,
        warden_id=payload.get("warden_id") or "",
        device_id=payload.get("device_id") or "",
        zone_id=zone_id,
        ts_ms=event.ts_ms,
        subject=subject,
        identity=payload.get("identity"),
        note=payload.get("note"),
        queued_offline=bool(payload.get("queued_offline", False)),
        synced_at_ms=(event.ts_ms + payload["sync_lag_ms"]
                      if payload.get("sync_lag_ms") is not None else None),
        action_id=payload.get("action_id") or str(uuid.uuid4()),
    )


@dataclass
class DeviceQueue:
    """A warden device's local queue, and the sequence it owns.

    Sequence numbers are assigned **on the device, at the moment of the action**,
    not on arrival at the server. That is what makes offline work orderable: a
    device that spends ten minutes out of contact and then syncs forty actions
    delivers them in the order the warden took them, and a gap means an action
    was genuinely lost rather than merely late.
    """

    device_id: str
    warden_id: str
    next_seq: int = 1
    pending: list[WardenAction] = field(default_factory=list)
    synced: list[WardenAction] = field(default_factory=list)

    def record(self, action: WardenAction, *, online: bool) -> WardenAction:
        """Take an action. Queued locally when offline, kept either way."""
        validate(action)
        stamped = WardenAction(
            **{**{f: getattr(action, f) for f in action.__slots__},
               "queued_offline": not online})
        self.pending.append(stamped)
        return stamped

    def sync(self, *, now_ms: int, tenant_id: str, site_id: str,
             drill_id: str) -> list[Event]:
        """Drain the queue into events. Returns them in the order taken."""
        events: list[Event] = []
        for action in sorted(self.pending, key=lambda a: a.ts_ms):
            synced = WardenAction(
                **{**{f: getattr(action, f) for f in action.__slots__},
                   "synced_at_ms": now_ms})
            events.append(to_event(
                synced, tenant_id=tenant_id, site_id=site_id,
                drill_id=drill_id, seq=self.next_seq))
            self.next_seq += 1
            self.synced.append(synced)
        self.pending.clear()
        return events

    @property
    def depth(self) -> int:
        return len(self.pending)

    def oldest_pending_ms(self) -> int | None:
        return min((a.ts_ms for a in self.pending), default=None)

    def staleness_ms(self, now_ms: int) -> int | None:
        oldest = self.oldest_pending_ms()
        return None if oldest is None else max(0, now_ms - oldest)
