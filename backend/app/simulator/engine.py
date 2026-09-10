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
from app.core.identity_fsm import IdentityConfig
from app.core.ledger import EvidenceKind, Stance
from app.core.presence_fsm import PROVISIONAL_CONFIG as PRESENCE_CONFIG
from app.core.presence_fsm import PresenceConfig, ZoneKind
from app.ingest.ingestor import IngestState, Ingestor
from app.ingest.projections import LiveBoard, build_board, resolve_identities
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
    # Each kind of infrastructure failure is emitted as its own component.
    # Flattening them into one generic outage was wrong in a way that mattered:
    # a Postgres failure costs durability but not sight, and reporting it as a
    # pipeline failure made the system blind itself over a database it does not
    # need to see through. It also collapsed overlapping outages into one, since
    # the health log treats a second failure of the same component and target as
    # the same ongoing outage.
    for component, outages, reason in (
        ("EVENT_BUS", inj.redis_outages, "redis unreachable"),
        ("DATABASE", inj.db_outages, "postgres unreachable"),
        ("CENTRAL_LINK", inj.network_partitions, "network partition from central"),
    ):
        for outage in outages:
            emit("system", SourceKind.SYSTEM, EventType.SYSTEM_DEGRADED,
                 at(outage.start_ms), None,
                 {"component": component, "reason": reason})
            emit("system", SourceKind.SYSTEM, EventType.SYSTEM_RECOVERED,
                 at(outage.end_ms), None, {"component": component})

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


#: The simulator does not own a fold of its own. `app.ingest.Ingestor` is the
#: production path, and driving it here is what makes a property proved against
#: the simulator a property of the real service rather than of a test double.
DrillState = IngestState


def replay(events: list[Event], plan: DrillPlan) -> IngestState:
    """Fold an event stream into the core, exactly as the edge service does."""
    ingestor = Ingestor(identity_config=plan.identity_config,
                        presence_config=plan.presence_config)
    ingestor.feed_batch(events)
    return ingestor.state


@dataclass
class DrillResult:
    board: AccountabilityBoard
    decisions: dict
    state: IngestState
    timing: DrillTiming
    roster: RosterSnapshot
    stream: ObservedStream
    live_board: LiveBoard | None = None

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


def conclude(plan: DrillPlan, state: IngestState, stream: ObservedStream,
             now_ms: int) -> DrillResult:
    """Derive accountability per rostered person and roll up the timings.

    All the resolution logic lives in `app.ingest.projections`, which is what
    the edge service uses. The simulator deliberately owns none of it: a board
    built here has exactly the same blind spots as the real one, including its
    inability to match a roster entry to a track by anything other than the
    identity the system itself claimed.
    """
    roster = build_roster(plan.agents, plan.alarm_ms)
    board_view = build_board(state, roster, now_ms=now_ms,
                             config=plan.accountability_config)
    resolutions = resolve_identities(state, roster)

    decisions: dict[str, Decision] = {}
    timings = []
    accountability_board = AccountabilityBoard(plan.accountability_config)

    for row in board_view.rows:
        decisions[row.person_ref] = row.decision
        accountability_board._last[row.person_ref] = row.decision
        resolution = resolutions[row.person_ref]
        _file_decision(state, list(resolution.claimed_by), row.decision, now_ms)

        arrival = None
        for gid in resolution.claimed_by:
            if gid in state.assembly_arrival_ms:
                candidate = state.assembly_arrival_ms[gid]
                arrival = candidate if arrival is None else min(arrival, candidate)

        entry = resolution.entry
        timings.append(measure(
            person_id=row.person_ref,
            drill_started_ms=state.drill_started_ms,
            assembly_arrival_ms=arrival,
            zone_id=entry.assigned_assembly_zone,
            floor_id=entry.home_floor_id,
            was_observed=resolution.is_observed))

    settled_ms = _accountability_completion(decisions, state.elapsed_ms(now_ms))
    return DrillResult(
        board=accountability_board, decisions=decisions, state=state,
        timing=DrillTiming(
            building=summarise(timings),
            by_floor=summarise_by(timings, "floor_id"),
            by_zone=summarise_by(timings, "zone_id"),
            accountability_completed_ms=settled_ms),
        roster=roster, stream=stream, live_board=board_view)


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
