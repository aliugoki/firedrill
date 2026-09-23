"""The fold: an event stream in, accountability state out.

This is the single place events become state. The simulator drives it, and the
edge service drives it with the same method calls, so a property proved against
the simulator is proved about the production path rather than about a test
double that resembles it.

Three guarantees, each tested:

**Idempotent.** `(source, seq)` is the identity of an event. A stream replayed
from any point converges to the same state. Redis Streams redeliver on consumer
restart, so this is load-bearing rather than defensive.

**Gap-detecting, never gap-filling.** A hole in a sequence produces a
`SEQUENCE_GAP` in the evidence ledger and nothing else. Interpolating across it
would manufacture observations, which is the exact inverse of invariant 1.

**Order-tolerant.** Events arrive late. The fold sorts by timestamp within a
batch and tolerates a late arrival filling a hole afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.events import Event, EventType, SequenceTracker
from app.core.identity_fsm import (
    PROVISIONAL_CONFIG as ID_CONFIG,
    FaceObservation,
    IdentityConfig,
    IdentityRegistry,
    RejectionReason,
    gate,
)
from app.core.ledger import EvidenceKind, EvidenceLedger, Stance
from app.core.timing import ClockCorrection
from app.core.presence_fsm import (
    PROVISIONAL_CONFIG as PRESENCE_CONFIG,
    PresenceConfig,
    PresenceRegistry,
    PresenceState,
    ZoneKind,
    ZoneSighting,
)
from app.ingest.bottlenecks import BottleneckTracker
from app.core.fusion import (
    DEFAULT_ASSOCIATION_WEIGHTS,
    AssociationKind,
    association_from,
)
from app.ingest.health import Component, HealthLog


@dataclass
class IngestState:
    """Everything the fold has concluded so far."""

    presence: PresenceRegistry
    identity: IdentityRegistry
    ledger: EvidenceLedger
    tracker: SequenceTracker
    health: HealthLog = field(default_factory=HealthLog)
    #: Wall clock from which ages mean something again, or None when they
    #: always did. Set when the clock is corrected under this node's feet.
    clock_trusted_from_ms: int | None = None
    #: Every correction, as plain data. The health log has each one as an
    #: interval with a cause, which is the right shape for "was the system
    #: trustworthy at 10:42" and the wrong shape for the timing report, which
    #: needs the signed size to know a duration is not a duration.
    clock_corrections: list = field(default_factory=list)
    bottlenecks: BottleneckTracker = field(default_factory=BottleneckTracker)
    drill_started_ms: int | None = None
    drill_completed_ms: int | None = None
    assembly_arrival_ms: dict = field(default_factory=dict)

    # -- counters, for the health endpoint and the chaos suite ----------------
    #: Exactly one of these counts every event the fold has seen.
    accepted: int = 0
    duplicates_dropped: int = 0
    rejected: int = 0
    """Events admitted by the sequence tracker that no handler could apply --
    no subject, a sighting with no zone, a warden ruling with no identity.

    Declared from the start and never incremented, so a producer emitting
    rubbish showed `accepted` climbing while nothing reached the state, and the
    health endpoint reported a healthy ingest. Silence is what a working
    pipeline and a broken one had in common."""
    last_event_ms: int | None = None

    @property
    def is_degraded(self) -> bool:
        return self.health.is_degraded

    @property
    def is_blind(self) -> bool:
        return self.health.is_blind

    def elapsed_ms(self, now_ms: int) -> int:
        return 0 if self.drill_started_ms is None else now_ms - self.drill_started_ms


class Ingestor:
    """Folds events into state. Stateless between calls except for `state`."""

    def __init__(
        self,
        *,
        identity_config: IdentityConfig = ID_CONFIG,
        presence_config: PresenceConfig = PRESENCE_CONFIG,
        state: IngestState | None = None,
    ) -> None:
        self.identity_config = identity_config
        self.presence_config = presence_config
        self.state = state or IngestState(
            presence=PresenceRegistry(presence_config),
            identity=IdentityRegistry(identity_config),
            ledger=EvidenceLedger(
                min_claims_for_dispute=identity_config.conflict_votes),
            tracker=SequenceTracker(),
        )

    # -- entry points ----------------------------------------------------------

    def feed(self, event: Event) -> bool:
        """Apply one event. Returns False if it was a duplicate."""
        state = self.state
        is_new, gap = state.tracker.observe(event.source, event.seq)
        if not is_new:
            state.duplicates_dropped += 1
            return False

        if gap is not None:
            # Reported, never repaired. The hole becomes evidence that the
            # record is incomplete, which is what stops a later reader treating
            # the silence as a quiet period.
            subject = event.subject or f"source:{gap.source}"
            state.ledger.record(
                subject=subject, kind=EvidenceKind.SEQUENCE_GAP,
                ts_ms=event.ts_ms, stance=Stance.CONTEXT, source=event.source,
                summary=f"{gap.missing_count} event(s) missing from {gap.source} "
                        f"between seq {gap.after_seq} and {gap.before_seq}")

        # Counted after the fold, not before it: a handler that cannot use the
        # event says so by refusing, and an event that was admitted and then
        # dropped is not an accepted one.
        refused_before = state.rejected
        self._apply(event)
        if state.rejected != refused_before:
            return False

        state.accepted += 1
        state.last_event_ms = max(state.last_event_ms or 0, event.ts_ms)
        return True

    def feed_batch(self, events: list[Event]) -> int:
        """Apply a batch in timestamp order. Returns how many were new.

        Sorting within the batch matters: a batch drained from a stream can
        contain events from several cameras whose clocks interleave, and
        applying them in arrival order would let a later sighting be overwritten
        by an earlier one.
        """
        applied = 0
        for event in sorted(events, key=lambda e: (e.ts_ms, e.source, e.seq)):
            if self.feed(event):
                applied += 1
        return applied

    def tick(self, now_ms: int) -> None:
        """Advance time with no new events. Ages tracks and identities."""
        self._close_clock_outage(now_ms)
        self.state.presence.tick(now_ms)
        self.state.identity.tick(now_ms)

    # -- the node's own sense of time ------------------------------------------

    def clock_stepped(self, step) -> None:
        """Somebody corrected the clock. Record it as an outage.

        Ageing is the thing a clock step breaks, and ageing is how this system
        decides nobody has seen somebody for ninety seconds. Every
        `last_seen_ms` already recorded was taken on the old clock, so until
        wall clock has moved past them again they mean nothing -- and after a
        backward step that takes exactly as long as the step did.

        Invariant 8: an infrastructure failure degrades the system rather than
        being absorbed by it. This is an infrastructure failure that makes the
        board *more* optimistic, which is the direction the invariant exists to
        forbid, so it is recorded rather than logged and forgotten.
        """
        self.state.clock_trusted_from_ms = step.at_ms + step.blind_for_ms
        self.state.clock_corrections.append(
            ClockCorrection(at_ms=step.at_ms, delta_ms=step.delta_ms))
        self.state.health.degrade(
            Component.CLOCK, "*", step.at_ms, step.describe())

    def _close_clock_outage(self, now_ms: int) -> None:
        """Close it once wall clock has caught up with what it already knew.

        Closed on the tick rather than on a timer, because the tick is the job
        that does the ageing: the outage should end at the moment the next
        piece of work it invalidates is about to run correctly again.
        """
        until = self.state.clock_trusted_from_ms
        if until is not None and now_ms >= until:
            self.state.health.recover(Component.CLOCK, "*", now_ms)
            self.state.clock_trusted_from_ms = None

    # -- per-event handling ----------------------------------------------------

    def _refuse(self, event: Event, reason: str) -> None:
        """An event no handler can use. Counted and recorded, never silent."""
        self.state.rejected += 1
        self.state.ledger.record(
            subject=event.subject or f"source:{event.source}",
            kind=EvidenceKind.MALFORMED_EVENT, ts_ms=event.ts_ms,
            stance=Stance.CONTEXT, source=event.source,
            summary=f"{event.type.value} could not be applied: {reason}")

    def _apply(self, event: Event) -> None:
        handler = _HANDLERS.get(event.type)
        if handler is not None:
            handler(self, event)

    def _drill_started(self, event: Event) -> None:
        self.state.drill_started_ms = event.ts_ms

    def _drill_completed(self, event: Event) -> None:
        self.state.drill_completed_ms = event.ts_ms
        self.state.health.close_all(event.ts_ms)

    def _camera_failure(self, event: Event) -> None:
        target = event.subject or "*"
        opened = self.state.health.degrade(
            Component.CAMERA, target, event.ts_ms,
            event.payload.get("reason", "camera offline"))
        if opened is None:
            return  # already down; a flapping camera is not a new outage
        self.state.presence.mark_camera_degraded(
            target, event.ts_ms, opened.reason)

    def _camera_recovered(self, event: Event) -> None:
        target = event.subject or "*"
        if self.state.health.recover(Component.CAMERA, target, event.ts_ms):
            self.state.presence.mark_camera_recovered(target, event.ts_ms)

    def _system_degraded(self, event: Event) -> None:
        component = _component_from(event.payload)
        self.state.health.degrade(
            component, event.subject or "*", event.ts_ms,
            event.payload.get("reason", "infrastructure outage"))

    def _system_recovered(self, event: Event) -> None:
        component = _component_from(event.payload)
        self.state.health.recover(component, event.subject or "*", event.ts_ms)

    def _track_updated(self, event: Event) -> None:
        subject, payload = event.subject, event.payload
        if not subject or "zone_id" not in payload:
            return self._refuse(
                event, "a sighting needs a track and a zone to be about")
        sighting = ZoneSighting(
            ts_ms=event.ts_ms, zone_id=payload["zone_id"],
            zone_kind=ZoneKind(payload["zone_kind"]),
            camera_id=payload.get("camera_id"))
        # Fed from the same sighting the presence machine uses, so the
        # bottleneck panel cannot disagree with the board about where somebody
        # was.
        self.state.bottlenecks.observe(
            subject, sighting.zone_id, sighting.zone_kind, event.ts_ms)
        transition = self.state.presence.observe(subject, sighting)
        if transition is None:
            return

        if transition.to_state is PresenceState.ASSEMBLY_PRESENT:
            self.state.assembly_arrival_ms.setdefault(subject, event.ts_ms)
            self.state.ledger.record(
                subject=subject, kind=EvidenceKind.ASSEMBLY_ARRIVAL,
                ts_ms=event.ts_ms, stance=Stance.SUPPORTS,
                source=payload.get("camera_id"),
                summary=f"settled in assembly zone {sighting.zone_id}")
        elif (transition.from_state is PresenceState.ASSEMBLY_PRESENT
              and transition.to_state is PresenceState.IN_BUILDING):
            self.state.assembly_arrival_ms.pop(subject, None)
            self.state.ledger.record(
                subject=subject, kind=EvidenceKind.ASSEMBLY_DEPARTURE,
                ts_ms=event.ts_ms, stance=Stance.CONTRADICTS,
                source=payload.get("camera_id"),
                summary="left the assembly zone and went back inside")

    def _track_lost(self, event: Event) -> None:
        if not event.subject:
            return self._refuse(event, "no track is named")
        self.state.presence.get(event.subject).track_lost(event.ts_ms)
        self.state.ledger.record(
            subject=event.subject, kind=EvidenceKind.TRACK_LOST,
            ts_ms=event.ts_ms, stance=Stance.CONTEXT, source=event.source,
            summary=event.payload.get("reason", "tracker dropped the track"))

    def _face_unavailable(self, event: Event) -> None:
        if not event.subject:
            return self._refuse(event, "no track is named")
        self.state.ledger.record(
            subject=event.subject, kind=EvidenceKind.FACE_UNAVAILABLE,
            ts_ms=event.ts_ms, stance=Stance.CONTEXT, source=event.source,
            summary=event.payload.get("reason", "no usable face"))

    def _face_observed(self, event: Event) -> None:
        subject, payload = event.subject, event.payload
        if not subject:
            return self._refuse(event, "no track is named")
        association = association_from(payload)
        observation = FaceObservation(
            ts_ms=event.ts_ms, candidate_id=payload.get("candidate_id"),
            score=payload.get("score", -1.0), margin=payload.get("margin", -1.0),
            quality=payload.get("quality", 1.0),
            pose_deviation_deg=payload.get("pose_deviation_deg", 0.0),
            # Scaled by how the face was attached to the body, which is the
            # weight half of the two-part rule in `fusion.py`. The structural
            # half is `association_is_strong` below; the module's own docstring
            # explains why a multiplier alone is not safe.
            track_confidence=(payload.get("track_confidence", 1.0)
                              * DEFAULT_ASSOCIATION_WEIGHTS[association]),
            camera_id=payload.get("camera_id"),
            association_is_strong=association is AssociationKind.SHARED_TRACK)

        verdict = gate(observation, self.identity_config)
        admissible = verdict is RejectionReason.ACCEPTED
        transition = self.state.identity.observe(subject, observation)

        # Only an admissible match is an identity claim. Filing a rejected one
        # with an identity attached makes the ledger report a dispute against a
        # candidate whose every observation already failed a gate.
        self.state.ledger.record(
            subject=subject, kind=EvidenceKind.FACE_MATCH, ts_ms=event.ts_ms,
            stance=Stance.CONTEXT, source=event.source,
            identity=payload.get("candidate_id") if admissible else None,
            summary=(f"face matched {payload.get('candidate_id')} "
                     f"score {payload.get('score', -1.0):.2f} "
                     f"margin {payload.get('margin', -1.0):.2f}"
                     + f", {_ASSOCIATION_WORDS[association]}"
                     + ("" if admissible else f" — not admissible: {verdict.value}")))

        if transition is None:
            return
        kind = _EVIDENCE_FOR_IDENTITY.get(transition.to_state.value)
        if kind is None:
            return
        self.state.ledger.record(
            subject=subject, kind=kind, ts_ms=event.ts_ms,
            stance=(Stance.SUPPORTS if kind is EvidenceKind.IDENTITY_CONFIRMED
                    else Stance.CONTEXT),
            identity=transition.identity, source=event.source,
            summary=transition.reason)

    def _warden_confirmed(self, event: Event) -> None:
        subject, payload = event.subject, event.payload
        if not subject or "identity" not in payload:
            return self._refuse(
                event, "a warden ruling needs a subject and the identity ruled on")
        tracked = self.state.identity.find(subject)
        if tracked is not None:
            tracked.warden_confirms(payload["identity"], event.ts_ms,
                                    payload.get("warden_id", "unknown"))
        self.state.ledger.record(
            subject=subject, kind=EvidenceKind.WARDEN_CONFIRMATION,
            ts_ms=event.ts_ms, stance=Stance.SUPPORTS, source=event.source,
            identity=payload["identity"],
            summary=(f"warden confirmed in person at "
                     f"{payload.get('zone_id', 'an unrecorded location')}"))

    def _warden_rejected(self, event: Event) -> None:
        subject, payload = event.subject, event.payload
        if not subject or "identity" not in payload:
            return self._refuse(
                event, "a warden ruling needs a subject and the identity ruled on")
        # Only when the subject is a track the cameras actually produced. The
        # warden PWA acts on roster rows, so its subject is a person reference,
        # and `get` used to invent an identity record under that key: a person
        # the drill never saw, whose rejected identity then matched no roster
        # entry and so reached nobody. The ruling still lands in the ledger
        # below and in the warden's own state, which is what accountability
        # reads, so nothing is lost by not inventing the record.
        tracked = self.state.identity.find(subject)
        if tracked is not None:
            tracked.warden_rejects(payload["identity"], event.ts_ms,
                                   payload.get("warden_id", "unknown"))
        self.state.ledger.record(
            subject=subject, kind=EvidenceKind.WARDEN_REJECTION,
            ts_ms=event.ts_ms, stance=Stance.CONTRADICTS, source=event.source,
            identity=payload["identity"],
            summary="warden rejected the system's identity")

    def _warden_note(self, event: Event) -> None:
        if not event.subject:
            return self._refuse(event, "a note needs somebody to be about")
        self.state.ledger.record(
            subject=event.subject, kind=EvidenceKind.WARDEN_NOTE,
            ts_ms=event.ts_ms, stance=Stance.CONTEXT, source=event.source,
            summary=event.payload.get("note", ""))


#: What each strength reads like in an evidence trail. An operator asking why
#: a face was not admitted deserves the reason in words, and "the pipeline did
#: not say how the face was attached to the body" is a different problem from
#: "it said, and the answer was geometry".
_ASSOCIATION_WORDS: dict[AssociationKind, str] = {
    AssociationKind.SHARED_TRACK: "face and body on one track",
    AssociationKind.SPATIAL_IOU: "face attached to the body by box overlap",
    AssociationKind.TEMPORAL_ONLY: "face and body only seen together",
    AssociationKind.NONE: "the pipeline did not say how the face was attached "
                          "to a body",
}


def _component_from(payload: dict) -> Component:
    raw = payload.get("component")
    if raw:
        try:
            return Component(raw)
        except ValueError:
            pass
    return Component.PIPELINE


_EVIDENCE_FOR_IDENTITY = {
    "CONFIRMED": EvidenceKind.IDENTITY_CONFIRMED,
    "CANDIDATE": EvidenceKind.IDENTITY_CANDIDATE,
    "CONFLICT": EvidenceKind.IDENTITY_CONFLICT,
    "REJECTED": EvidenceKind.IDENTITY_REJECTED,
}

_HANDLERS = {
    EventType.DRILL_STARTED: Ingestor._drill_started,
    EventType.DRILL_COMPLETED: Ingestor._drill_completed,
    EventType.CAMERA_FAILURE: Ingestor._camera_failure,
    EventType.CAMERA_RECOVERED: Ingestor._camera_recovered,
    EventType.SYSTEM_DEGRADED: Ingestor._system_degraded,
    EventType.SYSTEM_RECOVERED: Ingestor._system_recovered,
    EventType.TRACK_UPDATED: Ingestor._track_updated,
    EventType.TRACK_LOST: Ingestor._track_lost,
    EventType.FACE_UNAVAILABLE: Ingestor._face_unavailable,
    EventType.FACE_OBSERVED: Ingestor._face_observed,
    EventType.WARDEN_CONFIRMED: Ingestor._warden_confirmed,
    EventType.WARDEN_REJECTED: Ingestor._warden_rejected,
    EventType.WARDEN_NOTE: Ingestor._warden_note,
}
