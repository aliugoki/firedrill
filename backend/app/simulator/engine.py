"""Runs a drill: ground truth in, observed event stream out, decisions after.

Three stages, deliberately separated so tests can attack each one.

    observe(...)   ground truth -> the events a real pipeline would have
                   produced, with failures injected
    replay(...)    events -> the core state machines, idempotent and
                   gap-detecting. This is the same fold Phase 3's ingest will
                   perform, so a property proved here is proved about the real
                   path too.
    conclude(...)  state -> accountability decisions and timings

The split is what makes the central property testable: `observe` may lie in
every way the catalogue allows, and `conclude` must still never produce
ACCOUNTED for someone `agents.py` knows never reached an assembly point.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.accountability_fsm import (
    AccountabilityBoard,
    AccountabilityConfig,
    AccountabilityState,
    Context,
    Decision,
    WardenEvidence,
)
from app.core.accountability_fsm import PROVISIONAL_CONFIG as ACC_CONFIG
from app.core.events import (
    Event,
    EventType,
    SequenceTracker,
    SourceKind,
)
from app.core.identity_fsm import PROVISIONAL_CONFIG as ID_CONFIG
from app.core.identity_fsm import (
    FaceObservation,
    IdentityConfig,
    IdentityRegistry,
    RejectionReason,
    gate,
)
from app.core.ledger import EvidenceKind, EvidenceLedger, Stance
from app.core.presence_fsm import PROVISIONAL_CONFIG as PRESENCE_CONFIG
from app.core.presence_fsm import (
    PresenceConfig,
    PresenceRegistry,
    PresenceState,
    ZoneKind,
    ZoneSighting,
)
from app.core.roster import ExpectationReason, Population, Roster, RosterSnapshot
from app.core.timing import DrillTiming, measure, summarise, summarise_by
from app.simulator.agents import Agent, Behaviour
from app.simulator.injections import Dice, Injections, NONE
from app.simulator.site import Site


@dataclass
class DrillPlan:
    site: Site
    agents: list[Agent]
    alarm_ms: int
    injections: Injections = NONE
    seed: int = 20260910
    horizon_ms: int = 600_000
    identity_config: IdentityConfig = ID_CONFIG
    presence_config: PresenceConfig = PRESENCE_CONFIG
    accountability_config: AccountabilityConfig = ACC_CONFIG


@dataclass
class ObservedStream:
    """What the pipeline claimed to see, and what it silently did not."""

    events: list[Event] = field(default_factory=list)
    #: global person id -> the agent it really was. Test-only ground truth;
    #: nothing in `replay` or `conclude` may read this.
    truth: dict[str, str] = field(default_factory=dict)
    dropped: int = 0
    degraded_windows: list[tuple[int, int, str]] = field(default_factory=list)


def observe(plan: DrillPlan) -> ObservedStream:
    """Turn ground truth into the imperfect stream a real pipeline emits."""
    dice = Dice(plan.seed)
    inj = plan.injections
    stream = ObservedStream()
    seq: dict[str, int] = {}
    pending: list[Event] = []

    def emit(source: str, kind: SourceKind, event_type: EventType, ts_ms: int,
             subject: str | None, payload: dict) -> None:
        seq[source] = seq.get(source, 0) + 1
        event = Event(
            tenant_id="tenant-sim", site_id=plan.site.site_id, drill_id="drill-sim",
            source=source, source_kind=kind, seq=seq[source], type=event_type,
            ts_ms=ts_ms, subject=subject, payload=payload,
        )
        if dice.hit("drop", inj.drop_rate):
            stream.dropped += 1
            return
        if dice.hit("delay", inj.delay_rate):
            delayed = Event(
                **{**{f: getattr(event, f) for f in event.__slots__},
                   "ts_ms": event.ts_ms}
            )
            pending.append(delayed)
            return
        stream.events.append(event)
        if dice.hit("duplicate", inj.duplicate_rate):
            stream.events.append(event)

    # Outage windows are expressed relative to the alarm, which is how anyone
    # describing a drill talks about them ("camera 3 died at two minutes").
    # They are emitted as absolute timestamps, because everything downstream
    # works in epoch milliseconds. Getting this wrong once put every outage
    # event decades before the drill, where it sorted first and recovered
    # before anything had happened.
    def at(offset_ms: int) -> int:
        return plan.alarm_ms + offset_ms

    for outage in inj.camera_outages:
        stream.degraded_windows.append((outage.start_ms, outage.end_ms, outage.target))
        emit("system", SourceKind.SYSTEM, EventType.CAMERA_FAILURE,
             at(outage.start_ms), outage.target, {"reason": "camera offline"})
        emit("system", SourceKind.SYSTEM, EventType.CAMERA_RECOVERED,
             at(outage.end_ms), outage.target, {})
    for outage in list(inj.redis_outages) + list(inj.db_outages) + list(
            inj.network_partitions):
        emit("system", SourceKind.SYSTEM, EventType.SYSTEM_DEGRADED,
             at(outage.start_ms), None, {"reason": "infrastructure outage"})
        emit("system", SourceKind.SYSTEM, EventType.SYSTEM_RECOVERED,
             at(outage.end_ms), None, {})

    emit("system", SourceKind.SYSTEM, EventType.DRILL_STARTED, plan.alarm_ms,
         None, {})

    for agent in plan.agents:
        _observe_agent(agent, plan, dice, stream, emit)

    # Late events arrive after everything else, which is what makes them late.
    for event in pending:
        stream.events.append(event)

    if inj.reorder_rate > 0:
        rng = dice.stream("reorder")
        for i in range(len(stream.events) - 1):
            if rng.random() < inj.reorder_rate:
                stream.events[i], stream.events[i + 1] = (
                    stream.events[i + 1], stream.events[i])
    return stream


def _observe_agent(agent, plan, dice, stream, emit) -> None:
    inj = plan.injections
    base_gid = f"gp-{agent.person_ref}"
    stream.truth[base_gid] = agent.person_ref
    fragment = 0

    for waypoint in agent.trajectory:
        if waypoint.ts_ms < plan.alarm_ms:
            continue
        if waypoint.zone_id is None or waypoint.zone_kind is ZoneKind.BLIND:
            continue  # no camera there; correctly produces no observation

        cameras = plan.site.cameras_seeing(waypoint.floor_id, waypoint.point)
        if not cameras:
            continue

        live = [c for c in cameras
                if not any(o.covers(waypoint.ts_ms - plan.alarm_ms, c.camera_id)
                           or o.covers(waypoint.ts_ms - plan.alarm_ms, "*")
                           for o in inj.camera_outages)]
        if not live:
            continue
        if dice.hit("occlusion", inj.occlusion_rate):
            continue

        camera = live[0]

        if dice.hit("fragment", inj.track_fragmentation_rate):
            fragment += 1
            gid = f"{base_gid}#{fragment}"
            stream.truth[gid] = agent.person_ref
            emit(camera.camera_id, SourceKind.CAMERA, EventType.TRACK_LOST,
                 waypoint.ts_ms, base_gid if fragment == 1 else f"{base_gid}#{fragment-1}",
                 {"reason": "fragmentation"})
        else:
            gid = base_gid if fragment == 0 else f"{base_gid}#{fragment}"

        emit(camera.camera_id, SourceKind.CAMERA, EventType.TRACK_UPDATED,
             waypoint.ts_ms, gid,
             {"zone_id": waypoint.zone_id, "zone_kind": waypoint.zone_kind.value,
              "camera_id": camera.camera_id})

        _observe_face(agent, gid, camera, waypoint, plan, dice, emit)


def _observe_face(agent, gid, camera, waypoint, plan, dice, emit) -> None:
    """Emit one frame's face evidence, or the absence of it.

    Enrolled and unenrolled people go through the same path on purpose. A face
    detector does not know who is in the gallery, so detection failure, blur and
    pose affect both alike. Only the match that comes back differs: an enrolled
    person's own entry, or the nearest stranger's.
    """
    inj = plan.injections

    if dice.hit("face_loss", inj.face_loss_rate):
        emit(camera.camera_id, SourceKind.CAMERA, EventType.FACE_UNAVAILABLE,
             waypoint.ts_ms, gid, {"reason": "no face detected"})
        return

    enrolled = agent.has_gallery_entry and not agent.is_visitor

    if enrolled:
        candidate = agent.emp_id
        score, margin = 0.82, 0.31
    else:
        # Not enrolled, but the matcher does not know that. It returns its
        # nearest gallery entry at a poor score — usually poor enough to be
        # refused, occasionally not. Skipping straight to FACE_UNAVAILABLE here
        # would make the false-accept rate against strangers unmeasurable, and
        # that is the most dangerous error the system can make.
        rng = dice.stream("unenrolled")
        candidate = f"EMP-{rng.randrange(0, 500):04d}"
        score = rng.gauss(0.22, 0.07)
        margin = abs(rng.gauss(0.03, 0.03))

    quality, pose = 0.92, 6.0
    if dice.hit("poor_quality", inj.poor_quality_rate):
        quality = 0.2
    if dice.hit("bad_pose", inj.bad_pose_rate):
        pose = 72.0

    if enrolled and agent.lookalike_of and dice.hit(
            "lookalike", inj.lookalike_confusion_rate):
        # The gallery cannot cleanly separate the pair, and the outcome varies
        # frame to frame. All three outcomes have to be reachable: only ever
        # flipping to the other person produces a confident wrong answer and no
        # conflict at all, which is not what a look-alike pair does in practice.
        roll = dice.stream("lookalike").random()
        if roll < 0.4:
            margin = 0.01           # margin gate refuses; no vote either way
        elif roll < 0.7:
            candidate = agent.lookalike_of
            margin = 0.06           # the wrong one wins, narrowly
        else:
            margin = 0.06           # the right one wins, narrowly

    if dice.hit("wrong_identity", inj.wrong_identity_rate):
        candidate = "EMP-9999"

    emit(camera.camera_id, SourceKind.CAMERA, EventType.FACE_OBSERVED,
         waypoint.ts_ms, gid,
         {"candidate_id": candidate, "score": score, "margin": margin,
          "quality": quality, "pose_deviation_deg": pose,
          "camera_id": camera.camera_id, "association_is_strong": True,
          "enrolled": enrolled})


@dataclass
class DrillState:
    """Everything the core concluded from the stream."""

    presence: PresenceRegistry
    identity: IdentityRegistry
    ledger: EvidenceLedger
    tracker: SequenceTracker
    degraded_cameras: set = field(default_factory=set)
    system_degraded: bool = False
    drill_started_ms: int | None = None
    duplicates_dropped: int = 0
    assembly_arrival_ms: dict = field(default_factory=dict)


def replay(events: list[Event], plan: DrillPlan) -> DrillState:
    """Fold an event stream into the core. Idempotent, order-tolerant.

    Two properties this must hold and the tests check: replaying the same stream
    twice changes nothing, and a shuffled stream reaches the same conclusions
    for everything except genuinely time-ordered transitions.
    """
    state = DrillState(
        presence=PresenceRegistry(plan.presence_config),
        identity=IdentityRegistry(plan.identity_config),
        ledger=EvidenceLedger(
            min_claims_for_dispute=plan.identity_config.conflict_votes),
        tracker=SequenceTracker(),
    )

    for event in sorted(events, key=lambda e: (e.ts_ms, e.source, e.seq)):
        is_new, gap = state.tracker.observe(event.source, event.seq)
        if not is_new:
            state.duplicates_dropped += 1
            continue
        if gap is not None and event.subject:
            state.ledger.record(
                subject=event.subject, kind=EvidenceKind.SEQUENCE_GAP,
                ts_ms=event.ts_ms, stance=Stance.CONTEXT, source=event.source,
                summary=f"{gap.missing_count} events missing from {gap.source}")
        _apply(event, state, plan)
    return state


def _apply(event: Event, state: DrillState, plan: DrillPlan) -> None:
    et = event.type
    payload = event.payload

    if et is EventType.DRILL_STARTED:
        state.drill_started_ms = event.ts_ms
        return

    if et is EventType.CAMERA_FAILURE:
        state.degraded_cameras.add(event.subject)
        state.presence.mark_camera_degraded(
            event.subject or "*", event.ts_ms, "camera offline")
        return
    if et is EventType.CAMERA_RECOVERED:
        state.degraded_cameras.discard(event.subject)
        state.presence.mark_camera_recovered(event.subject or "*", event.ts_ms)
        return
    if et is EventType.SYSTEM_DEGRADED:
        state.system_degraded = True
        return
    if et is EventType.SYSTEM_RECOVERED:
        state.system_degraded = False
        return

    subject = event.subject
    if not subject:
        return

    if et is EventType.TRACK_UPDATED:
        sighting = ZoneSighting(
            ts_ms=event.ts_ms, zone_id=payload["zone_id"],
            zone_kind=ZoneKind(payload["zone_kind"]),
            camera_id=payload.get("camera_id"))
        transition = state.presence.observe(subject, sighting)
        if transition and transition.to_state is PresenceState.ASSEMBLY_PRESENT:
            state.assembly_arrival_ms.setdefault(subject, event.ts_ms)
            state.ledger.record(
                subject=subject, kind=EvidenceKind.ASSEMBLY_ARRIVAL,
                ts_ms=event.ts_ms, stance=Stance.SUPPORTS,
                source=payload.get("camera_id"),
                summary=f"settled in assembly zone {sighting.zone_id}")
        elif transition and transition.to_state is PresenceState.IN_BUILDING \
                and transition.from_state is PresenceState.ASSEMBLY_PRESENT:
            state.assembly_arrival_ms.pop(subject, None)
            state.ledger.record(
                subject=subject, kind=EvidenceKind.ASSEMBLY_DEPARTURE,
                ts_ms=event.ts_ms, stance=Stance.CONTRADICTS,
                source=payload.get("camera_id"),
                summary="left the assembly zone and went back inside")
        return

    if et is EventType.TRACK_LOST:
        state.presence.get(subject).track_lost(event.ts_ms)
        state.ledger.record(
            subject=subject, kind=EvidenceKind.TRACK_LOST, ts_ms=event.ts_ms,
            stance=Stance.CONTEXT, source=event.source,
            summary=payload.get("reason", "tracker dropped the track"))
        return

    if et is EventType.FACE_UNAVAILABLE:
        state.ledger.record(
            subject=subject, kind=EvidenceKind.FACE_UNAVAILABLE, ts_ms=event.ts_ms,
            stance=Stance.CONTEXT, source=event.source,
            summary=payload.get("reason", "no face"))
        return

    if et is EventType.FACE_OBSERVED:
        observation = FaceObservation(
            ts_ms=event.ts_ms, candidate_id=payload.get("candidate_id"),
            score=payload.get("score", -1.0), margin=payload.get("margin", -1.0),
            quality=payload.get("quality", 1.0),
            pose_deviation_deg=payload.get("pose_deviation_deg", 0.0),
            track_confidence=payload.get("track_confidence", 1.0),
            camera_id=payload.get("camera_id"),
            association_is_strong=payload.get("association_is_strong", True))
        verdict = gate(observation, plan.identity_config)
        admissible = verdict is RejectionReason.ACCEPTED
        transition = state.identity.observe(subject, observation)
        # Only an admissible match is an identity *claim*. Filing a rejected one
        # with an identity attached made the ledger report a dispute between the
        # confirmed person and a look-alike whose every observation had already
        # failed the margin gate, so an ACCOUNTED person carried a DISPUTED
        # banner. Invariant 1: an inadmissible observation is not evidence in
        # either direction, and that has to hold in the record too.
        state.ledger.record(
            subject=subject, kind=EvidenceKind.FACE_MATCH, ts_ms=event.ts_ms,
            stance=Stance.CONTEXT, source=event.source,
            identity=payload.get("candidate_id") if admissible else None,
            summary=(f"face matched {payload.get('candidate_id')} "
                     f"score {payload.get('score'):.2f} "
                     f"margin {payload.get('margin'):.2f}"
                     + ("" if admissible else f" — not admissible: {verdict.value}")))
        if transition:
            kind = {
                "CONFIRMED": EvidenceKind.IDENTITY_CONFIRMED,
                "CANDIDATE": EvidenceKind.IDENTITY_CANDIDATE,
                "CONFLICT": EvidenceKind.IDENTITY_CONFLICT,
                "REJECTED": EvidenceKind.IDENTITY_REJECTED,
            }.get(transition.to_state.value)
            if kind:
                state.ledger.record(
                    subject=subject, kind=kind, ts_ms=event.ts_ms,
                    stance=(Stance.SUPPORTS
                            if kind is EvidenceKind.IDENTITY_CONFIRMED
                            else Stance.CONTEXT),
                    identity=transition.identity, source=event.source,
                    summary=transition.reason)
        return

    if et is EventType.WARDEN_CONFIRMED:
        state.identity.get(subject).warden_confirms(
            payload["identity"], event.ts_ms, payload.get("warden_id", "unknown"))
        state.ledger.record(
            subject=subject, kind=EvidenceKind.WARDEN_CONFIRMATION,
            ts_ms=event.ts_ms, stance=Stance.SUPPORTS, source=event.source,
            identity=payload["identity"],
            summary=f"warden confirmed at {payload.get('zone_id')}")
        return

    if et is EventType.WARDEN_REJECTED:
        state.identity.get(subject).warden_rejects(
            payload["identity"], event.ts_ms, payload.get("warden_id", "unknown"))
        state.ledger.record(
            subject=subject, kind=EvidenceKind.WARDEN_REJECTION, ts_ms=event.ts_ms,
            stance=Stance.CONTRADICTS, source=event.source,
            identity=payload["identity"], summary="warden rejected the identity")
        return


@dataclass
class DrillResult:
    board: AccountabilityBoard
    decisions: dict
    state: DrillState
    timing: DrillTiming
    roster: RosterSnapshot
    stream: ObservedStream

    def state_of(self, person_ref: str) -> AccountabilityState | None:
        decision = self.decisions.get(person_ref)
        return decision.state if decision else None

    def accounted_refs(self) -> set:
        return {ref for ref, d in self.decisions.items()
                if d.state is AccountabilityState.ACCOUNTED}


def build_roster(agents: list[Agent], at_ms: int) -> RosterSnapshot:
    roster = Roster()
    for agent in agents:
        if agent.is_visitor:
            roster.sign_in(visitor_ref=agent.person_ref.split(":", 1)[1],
                           display_name=agent.display_name, at_ms=at_ms,
                           population=Population.VISITOR,
                           assigned_assembly_zone=agent.target_assembly)
        else:
            roster.add_employee(
                emp_id=agent.emp_id, display_name=agent.display_name,
                has_gallery_entry=agent.has_gallery_entry,
                reason=ExpectationReason.ON_SHIFT,
                department=agent.department,
                home_floor_id=agent.home_floor_id,
                assigned_assembly_zone=agent.target_assembly)
    return roster.snapshot(at_ms)


def conclude(plan: DrillPlan, state: DrillState, stream: ObservedStream,
             now_ms: int) -> DrillResult:
    """Derive accountability per rostered person and roll up the timings.

    Roster entries are matched to tracked people by the identity the system
    **claimed**, never by ground truth. That distinction is the whole point: the
    real system has no idea which body belongs to which employee except through
    its own identity machinery, and matching on truth here would quietly hide
    every misidentification the simulator injects. An employee nobody claimed to
    be is simply unobserved, which is exactly what the real system would report.
    """
    roster = build_roster(plan.agents, plan.alarm_ms)
    board = AccountabilityBoard(plan.accountability_config)
    decisions: dict[str, Decision] = {}
    timings = []
    elapsed = now_ms - plan.alarm_ms

    # emp_id -> the tracked people claiming to be them. More than one means the
    # same name was attached to two bodies, which is a conflict at the roster
    # level even when each track individually looked confident.
    claimed: dict[str, list[str]] = {}
    #: emp_id -> tracks whose identity is contested or was rejected by a warden.
    #: A track in CONFLICT has dropped its identity claim, so without this its
    #: candidates vanish from `claimed` entirely and the employees involved fall
    #: through to "never observed". That is the wrong answer twice over: the
    #: system did see somebody who might be them, and it knows exactly why it
    #: cannot say. Both belong in front of a human.
    contested: dict[str, list[str]] = {}
    for person in state.identity:
        if person.identity:
            claimed.setdefault(person.identity, []).append(person.person_id)
        for candidate in person.conflict_with:
            contested.setdefault(candidate, []).append(person.person_id)
        for rejected in person.rejected_identities:
            contested.setdefault(rejected, []).append(person.person_id)

    for agent in plan.agents:
        gids = claimed.get(agent.emp_id, []) if agent.emp_id else []
        best_decision = None

        for gid in gids:
            identity = state.identity.get(gid)
            presence = state.presence.get(gid)
            ctx = Context(
                person_id=agent.person_ref,
                drill_elapsed_ms=elapsed,
                is_expected=True,
                presence=presence,
                identity=identity,
                system_degraded=state.system_degraded,
                degraded_reason=("infrastructure outage"
                                 if state.system_degraded else None),
                config=plan.accountability_config,
            )
            decision, _ = board.evaluate(ctx)
            if best_decision is None or _rank(decision.state) < _rank(best_decision.state):
                best_decision = decision

        disputed = contested.get(agent.emp_id, []) if agent.emp_id else []
        if disputed and not gids:
            # Somebody was seen who might be this person, and the system knows
            # it cannot tell. That is a verification job, not an absence.
            best_decision = Decision(
                person_id=agent.person_ref,
                state=AccountabilityState.MANUAL_VERIFICATION_REQUIRED,
                reason=(f"{agent.emp_id} is one of the contested identities on "
                        f"{len(disputed)} track(s); the system will not choose"),
                blockers=("identity contested between look-alikes",),
            )
            decisions[agent.person_ref] = best_decision
            board._last[agent.person_ref] = best_decision
            timings.append(measure(
                person_id=agent.person_ref,
                drill_started_ms=state.drill_started_ms,
                assembly_arrival_ms=None,
                zone_id=agent.target_assembly, floor_id=agent.home_floor_id,
                was_observed=True))
            continue

        simultaneous = _simultaneous_claims(state, gids)
        if simultaneous:
            # Two tracks claiming this employee were being observed at the same
            # time. One person cannot be in two places, so neither track can be
            # accounted for on camera evidence alone, however confident each
            # looked on its own.
            #
            # Sequential tracks are deliberately not treated this way: those are
            # ordinary fragmentation, one person's evidence split in half, and
            # calling that a conflict would bury the real ones.
            best_decision = Decision(
                person_id=agent.person_ref,
                state=AccountabilityState.MANUAL_VERIFICATION_REQUIRED,
                reason=(f"{len(simultaneous)} tracks were identified as "
                        f"{agent.emp_id} at the same time; a human must "
                        "establish which is real"),
                blockers=("the same identity claimed by simultaneous tracks",),
            )

        if best_decision is None:
            ctx = Context(person_id=agent.person_ref, drill_elapsed_ms=elapsed,
                          is_expected=True, config=plan.accountability_config)
            best_decision, _ = board.evaluate(ctx)

        decisions[agent.person_ref] = best_decision
        _file_decision(state, gids, best_decision, now_ms)

        arrival = None
        for gid in gids:
            if gid in state.assembly_arrival_ms:
                arrival = min(filter(None, [arrival, state.assembly_arrival_ms[gid]]))
        timings.append(measure(
            person_id=agent.person_ref,
            drill_started_ms=state.drill_started_ms,
            assembly_arrival_ms=arrival,
            zone_id=agent.target_assembly,
            floor_id=agent.home_floor_id,
            was_observed=bool(gids),
        ))

    settled_ms = _accountability_completion(decisions, elapsed)
    return DrillResult(
        board=board, decisions=decisions, state=state,
        timing=DrillTiming(
            building=summarise(timings),
            by_floor=summarise_by(timings, "floor_id"),
            by_zone=summarise_by(timings, "zone_id"),
            accountability_completed_ms=settled_ms),
        roster=roster, stream=stream)


def _file_decision(state: DrillState, gids: list[str], decision: Decision,
                   now_ms: int) -> None:
    """Write the conclusion back into the ledger.

    Invariant 6 is not satisfied by a decision that exists only in a dashboard.
    Without this, `explain` narrated the observations and then said "no decision
    recorded yet", which is the one thing the drawer is opened to find out.
    """
    for gid in gids:
        state.ledger.record(
            subject=gid, kind=EvidenceKind.DECISION, ts_ms=now_ms,
            stance=(Stance.SUPPORTS if decision.is_safe else Stance.CONTEXT),
            summary=decision.state.value,
            detail={"reason": decision.reason,
                    "qualifying": list(decision.qualifying_evidence),
                    "blockers": list(decision.blockers)})


def _simultaneous_claims(state: DrillState, gids: list[str]) -> list[str]:
    """Tracks among ``gids`` whose observation windows overlap another's."""
    if len(gids) < 2:
        return []
    people = [state.presence.get(gid) for gid in gids]
    clashing = set()
    for i, a in enumerate(people):
        for b in people[i + 1:]:
            if a.overlaps(b):
                clashing.add(a.person_id)
                clashing.add(b.person_id)
    return sorted(clashing)


#: Ordering used only to pick the most-informative fragment. Lower is better.
_RANK = {
    AccountabilityState.ACCOUNTED: 0,
    AccountabilityState.EVACUATING: 1,
    AccountabilityState.MANUAL_VERIFICATION_REQUIRED: 2,
    AccountabilityState.UNCERTAIN: 3,
    AccountabilityState.UNACCOUNTED: 4,
    AccountabilityState.NOT_EVACUATED: 5,
}


def _rank(state: AccountabilityState) -> int:
    return _RANK[state]


def _accountability_completion(decisions: dict, elapsed_ms: int) -> int | None:
    unresolved = sum(
        1 for d in decisions.values()
        if d.state in (AccountabilityState.UNCERTAIN,
                       AccountabilityState.MANUAL_VERIFICATION_REQUIRED,
                       AccountabilityState.NOT_EVACUATED))
    return None if unresolved else elapsed_ms


def run_drill(plan: DrillPlan, *, now_ms: int | None = None) -> DrillResult:
    """observe -> replay -> conclude, the whole pipeline in one call."""
    stream = observe(plan)
    end = now_ms if now_ms is not None else plan.alarm_ms + plan.horizon_ms
    # Events past the moment we are concluding at have not happened yet. Folding
    # them in let an outage's recovery event, scheduled beyond the horizon,
    # clear a degradation that was still in force when the drill ended.
    state = replay([e for e in stream.events if e.ts_ms <= end], plan)
    state.presence.tick(end)
    state.identity.tick(end)
    return conclude(plan, state, stream, end)
