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
    #: global person id -> [(from_ts_ms, agent)], in time order. An ID switch
    #: hands one person another's track id, so a track genuinely belongs to two
    #: different people at different moments and a plain gid->agent map cannot
    #: say so. `truth` keeps the first owner; `who` is the accurate lookup.
    owners: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    #: event id -> when it actually reached the ingest, for events the transport
    #: delayed. Absent means it arrived at its own timestamp.
    arrival_ms: dict[str, int] = field(default_factory=dict)
    dropped: int = 0
    degraded_windows: list[tuple[int, int, str]] = field(default_factory=list)

    def who(self, gid: str | None, ts_ms: int) -> str | None:
        """Which real person was behind this track id at this moment."""
        if gid is None:
            return None
        person = self.truth.get(gid)
        for from_ts, agent in self.owners.get(gid, ()):
            if from_ts > ts_ms:
                break
            person = agent
        return person

    def arrival_of(self, event: Event) -> int:
        """When ingest saw it, which is its timestamp unless it was delayed."""
        return self.arrival_ms.get(event.event_id, event.ts_ms)


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
            # A late event keeps its own timestamp -- it happened when it
            # happened -- and gains an arrival time. The distance between the
            # two is the whole point: an event 5 s late and one 30 s late pose
            # different problems, and one delayed past the moment the drill is
            # concluded never informs the decision at all.
            late_by = dice.stream("delay_amount").randrange(
                1_000, max(2_000, inj.max_delay_ms))
            stream.arrival_ms[event.event_id] = event.ts_ms + late_by
            pending.append(event)
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

    switches = _plan_id_switches(plan, dice)
    for agent in plan.agents:
        _observe_agent(agent, plan, dice, stream, emit,
                       switches.get(agent.person_ref, ()))

    # Late events arrive after everything else, which is what makes them late,
    # and among themselves in the order their delays actually finished.
    for event in sorted(pending, key=stream.arrival_of):
        stream.events.append(event)

    _normalise_owners(stream)

    if inj.reorder_rate > 0:
        rng = dice.stream("reorder")
        for i in range(len(stream.events) - 1):
            if rng.random() < inj.reorder_rate:
                stream.events[i], stream.events[i + 1] = (
                    stream.events[i + 1], stream.events[i])
    return stream


#: How close in time two people have to be seen by one camera before a tracker
#: could plausibly confuse them. Wider than a frame, because the confusion
#: happens while they are crossing, not at one instant.
_SWITCH_WINDOW_MS = 2_000


def _live_cameras(plan, waypoint):
    """The cameras that can see this waypoint and are not in an outage."""
    offset_ms = waypoint.ts_ms - plan.alarm_ms
    return [camera
            for camera in plan.site.cameras_seeing(waypoint.floor_id, waypoint.point)
            if not any(outage.covers(offset_ms, camera.camera_id)
                       or outage.covers(offset_ms, "*")
                       for outage in plan.injections.camera_outages)]


def _visible_slots(plan) -> list[tuple[int, str, str]]:
    """(ts_ms, camera_id, person_ref) for every waypoint a camera would see.

    Applies exactly the visibility filter `_observe_agent` applies, and touches
    no dice: planning a switch must not shift which observations get occluded,
    or two runs that differ only in `id_switch_rate` stop being comparable.
    """
    slots = []
    for agent in plan.agents:
        for waypoint in agent.trajectory:
            if waypoint.ts_ms < plan.alarm_ms:
                continue
            if waypoint.zone_id is None or waypoint.zone_kind is ZoneKind.BLIND:
                continue
            live = _live_cameras(plan, waypoint)
            if live:
                slots.append((waypoint.ts_ms, live[0].camera_id, agent.person_ref))
    slots.sort()
    return slots


def _plan_id_switches(plan, dice) -> dict[str, tuple[tuple[int, str], ...]]:
    """Decide, before anything is emitted, who swaps track ids with whom.

    Only pairs a single camera holds at the same moment are eligible, because
    that is the only situation in which a real tracker confuses two people.
    Swapping strangers on different floors would be a teleport, and a teleport
    is not the problem this injection exists to pose -- the dangerous case is
    the plausible one, where a name already confirmed on a track stays on it
    while a different body carries it away.
    """
    rate = plan.injections.id_switch_rate
    if rate <= 0:
        return {}

    buckets: dict[tuple[str, int], list[str]] = {}
    for ts_ms, camera_id, person_ref in _visible_slots(plan):
        together = buckets.setdefault((camera_id, ts_ms // _SWITCH_WINDOW_MS), [])
        if person_ref not in together:
            together.append(person_ref)

    emitting_as = {agent.person_ref: agent.person_ref for agent in plan.agents}
    timeline: dict[str, list[tuple[int, str]]] = {}
    # `id_switch_rate` is per person per minute of camera time, because that is
    # the unit a tracker benchmark reports and the unit `docs/EVAC120_BENCHMARKS.md`
    # says sets this knob. Two other readings were tried and both are wrong: per
    # pair makes the rate quadratic in how crowded the frame is, so a 2% figure
    # shuffles a whole assembly point, and per observation makes it depend on
    # the camera's frame rate, so improving the cameras would make the tracker
    # look worse.
    per_window = rate * _SWITCH_WINDOW_MS / 60_000
    partners = dice.stream("id_switch_partner")
    for camera_id, bucket in sorted(buckets):
        together = buckets[(camera_id, bucket)]
        if len(together) < 2:
            continue
        ts_ms = bucket * _SWITCH_WINDOW_MS
        for index, person in enumerate(together):
            if not dice.hit("id_switch", per_window):
                continue
            others = together[:index] + together[index + 1:]
            partner = others[partners.randrange(len(others))]
            emitting_as[person], emitting_as[partner] = (
                emitting_as[partner], emitting_as[person])
            timeline.setdefault(person, []).append((ts_ms, emitting_as[person]))
            timeline.setdefault(partner, []).append((ts_ms, emitting_as[partner]))
    return {ref: tuple(entries) for ref, entries in timeline.items()}


def _emitting_as(switches, ts_ms: int, default: str) -> str:
    """Which track lineage an agent is on at this moment."""
    owner = default
    for from_ts, other in switches:
        if from_ts > ts_ms:
            break
        owner = other
    return owner


def _record_owner(stream, gid: str, ts_ms: int, person_ref: str) -> None:
    """Note that this track id belonged to this person at this moment.

    Appended raw and put in order later. Agents are observed one at a time, so
    the record for a track that changed hands arrives in agent order rather than
    in time order, and collapsing runs as they arrive would collapse the wrong
    ones.
    """
    stream.owners.setdefault(gid, []).append((ts_ms, person_ref))


def _normalise_owners(stream) -> None:
    """Put each track's owners in time order and collapse the repeats."""
    for gid, windows in stream.owners.items():
        windows.sort()
        collapsed = [windows[0]]
        for entry in windows[1:]:
            if entry[1] != collapsed[-1][1]:
                collapsed.append(entry)
        stream.owners[gid] = collapsed
        stream.truth[gid] = collapsed[0][1]


def _observe_agent(agent, plan, dice, stream, emit, switches=()) -> None:
    inj = plan.injections
    _record_owner(stream, f"gp-{agent.person_ref}", plan.alarm_ms, agent.person_ref)
    # One fragment counter per track lineage this agent emits under. After an ID
    # switch it continues on somebody else's track id, and that track's
    # fragment numbering is its own.
    fragments: dict[str, int] = {}

    for waypoint in agent.trajectory:
        if waypoint.ts_ms < plan.alarm_ms:
            continue
        if waypoint.zone_id is None or waypoint.zone_kind is ZoneKind.BLIND:
            continue  # no camera there; correctly produces no observation

        live = _live_cameras(plan, waypoint)
        if not live:
            continue
        if dice.hit("occlusion", inj.occlusion_rate):
            continue

        camera = live[0]

        owner = _emitting_as(switches, waypoint.ts_ms, agent.person_ref)
        base_gid = f"gp-{owner}"
        fragment = fragments.get(owner, 0)

        if dice.hit("fragment", inj.track_fragmentation_rate):
            previous = base_gid if fragment == 0 else f"{base_gid}#{fragment}"
            fragment += 1
            fragments[owner] = fragment
            gid = f"{base_gid}#{fragment}"
            emit(camera.camera_id, SourceKind.CAMERA, EventType.TRACK_LOST,
                 waypoint.ts_ms, previous, {"reason": "fragmentation"})
        else:
            gid = base_gid if fragment == 0 else f"{base_gid}#{fragment}"

        _record_owner(stream, gid, waypoint.ts_ms, agent.person_ref)

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
    state = replay([e for e in stream.events if stream.arrival_of(e) <= end], plan)
    state.presence.tick(end)
    state.identity.tick(end)
    return conclude(plan, state, stream, end)
