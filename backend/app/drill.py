"""A drill: the object everything else hangs off.

Holds the roster snapshot, the ingest fold, and the warden state for one
evacuation, and is the only place the two halves of an all-clear are combined.

The lifecycle is deliberately small. A drill is created, started, and completed,
and it cannot be started twice or completed before it starts. There is no
"paused" or "cancelled": an evacuation that stops being an evacuation is over,
and pretending otherwise would leave a live board on a screen with nobody
watching it.

**The all-clear needs both halves.** The board's counts say every expected
person is accounted for. Every zone being swept and agreeing says a human
checked. Either alone is insufficient — cameras content with nobody having
walked a zone is an untested assumption, and a warden's sweep without the
system agreeing is one person's word against a gap in the record. Both, and no
open outage, and a roster that was verified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.core.accountability_fsm import (
    PROVISIONAL_CONFIG as ACC_CONFIG,
    AccountabilityConfig,
)
from app.core.events import Event, EventType, SourceKind
from app.core.identity_fsm import PROVISIONAL_CONFIG as ID_CONFIG, IdentityConfig
from app.core.ledger import Explanation
from app.core.presence_fsm import PROVISIONAL_CONFIG as PRESENCE_CONFIG, PresenceConfig
from app.core.roster import RosterSnapshot
from app.core.timing import DrillTiming, measure, summarise, summarise_by
from app.ingest.health import Component
from app.ingest.ingestor import Ingestor
from app.ingest.projections import LiveBoard, build_board, resolve_identities
from app.warden.actions import WardenAction, from_event, to_event
from app.warden.headcount import (
    Headcount,
    from_event as headcount_from_event,
    to_event as headcount_to_event,
)
from app.warden.state import WardenState


class DrillStatus(str, Enum):
    DRAFT = "DRAFT"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"


class DrillError(RuntimeError):
    """An operation the drill's lifecycle does not allow."""


class RecoveryUnavailable(RuntimeError):
    """The store could not say which drills were running.

    Distinct from there being none. An empty board and an unreadable database
    look identical to a caller who only counts what came back, and this is the
    startup path for a node that may have restarted mid-evacuation.
    """


@dataclass
class Drill:
    drill_id: str
    tenant_id: str
    site_id: str
    name: str
    roster: RosterSnapshot
    created_ms: int
    assembly_zones: frozenset = frozenset()
    identity_config: IdentityConfig = ID_CONFIG
    presence_config: PresenceConfig = PRESENCE_CONFIG
    accountability_config: AccountabilityConfig = ACC_CONFIG

    status: DrillStatus = DrillStatus.DRAFT
    started_ms: int | None = None
    completed_ms: int | None = None

    #: Durable storage. Optional: a drill runs without it, degraded rather than
    #: refused. An operator with an evacuation in progress needs the drill more
    #: than the software needs its own bookkeeping.
    events_store: object | None = None
    drill_store: object | None = None
    #: Central's queue. A drill's own events -- warden confirmations, sweeps,
    #: headcounts, the start and the end -- are produced here rather than read
    #: off the camera stream, so the consumer never sees them and nothing else
    #: would put them on the wire.
    replicator: object | None = None

    ingestor: Ingestor = field(init=False)
    warden: WardenState = field(init=False)
    _system_seq: int = 0
    #: Highest stored row this drill has folded. The API process and the edge
    #: process each hold a `Drill` for the same evacuation and write different
    #: halves of it, so each has to pick up what the other wrote.
    _replay_cursor: int = 0
    #: Warden events already folded into `self.warden`. The ingest fold is
    #: idempotent on `(source, seq)` and warden state is not: a sweep's
    #: confirmations are a set, but its tagged-unknown count and its headcounts
    #: are lists, and applying one twice invents evidence.
    _folded_warden: set = field(default_factory=set)

    def __post_init__(self) -> None:
        self.ingestor = Ingestor(identity_config=self.identity_config,
                                 presence_config=self.presence_config)
        self.warden = WardenState(assembly_zones=self.assembly_zones)

    # -- lifecycle -------------------------------------------------------------

    def start(self, now_ms: int) -> Event:
        if self.status is not DrillStatus.DRAFT:
            raise DrillError(f"a {self.status.value.lower()} drill cannot be started")
        self.status = DrillStatus.RUNNING
        self.started_ms = now_ms
        event = self._system_event(EventType.DRILL_STARTED, now_ms)
        self.feed([event])
        self._persist()
        return event

    def complete(self, now_ms: int) -> Event:
        if self.status is not DrillStatus.RUNNING:
            raise DrillError(
                f"a {self.status.value.lower()} drill cannot be completed")
        self.status = DrillStatus.COMPLETE
        self.completed_ms = now_ms
        event = self._system_event(EventType.DRILL_COMPLETED, now_ms)
        self.feed([event])
        self._persist()
        return event

    @property
    def is_running(self) -> bool:
        return self.status is DrillStatus.RUNNING

    def elapsed_ms(self, now_ms: int) -> int:
        if self.started_ms is None:
            return 0
        return (self.completed_ms or now_ms) - self.started_ms

    # -- input -----------------------------------------------------------------

    def feed(self, events: list[Event]) -> int:
        """Ingest camera and system events. Returns how many were new.

        **Persist before folding.** The full chain from Redis is read, append,
        fold, acknowledge, and every crash point in it is safe:

          * before the append — not acknowledged, so redelivered;
          * after the append, before the fold — durable, redelivered, and the
            store rejects the duplicate while the fold still happens;
          * after the fold, before the acknowledgement — redelivered, and the
            fold is idempotent.

        Folding first would leave a window where the state has an event the
        record does not, and a restart would silently lose it.
        """
        if self.events_store is not None:
            for event in events:
                self.events_store.append(event)
            self._mirror_store_health(events)
        self._replicate(events)
        return self.ingestor.feed_batch(events)

    def _replicate(self, events: list[Event]) -> None:
        """Queue events for central. Never raises, never blocks a decision.

        After the local store, because central must never hold an event this
        node's own record lacks. Before the fold is fine and after would be
        too; what matters is the order against the store, not against the
        board, which is in memory here either way.
        """
        if self.replicator is None:
            return
        for event in events:
            try:
                self.replicator.enqueue(event, event.ts_ms)
            except Exception:
                # Replication is not on the critical path. An edge node with no
                # link to central is fully operational, and one whose outbox is
                # broken must be too.
                pass

    def _mirror_store_health(self, events: list[Event]) -> None:
        """Put the store's trouble into this drill's health log.

        The edge node hands its store the node's own health log, so a database
        outage there lands in the report. The API process cannot: it has one
        store and a drill per evacuation, and a shared log would attribute one
        drill's outage to the next. So the drill mirrors what the store says
        about itself into the log the report actually reads.

        Without this, a drill run through the API could buffer its entire event
        log into memory, drop the overflow, and produce a report whose System
        health section said no outages and nothing blind. The board would be
        right and the record behind it would not exist.
        """
        if not events:
            return
        # Drill time, not wall clock. The health log's intervals are read back
        # against event timestamps, and mixing the two would make
        # `was_degraded_at` answer about a moment that never happened.
        ts_ms = max(event.ts_ms for event in events)
        health = self.ingestor.state.health
        if self.events_store.is_degraded:
            health.degrade(
                Component.DATABASE, "events", ts_ms,
                self.events_store.stats.last_error or "unreachable")
        else:
            health.recover(Component.DATABASE, "events", ts_ms)

    def record_warden_action(self, action: WardenAction) -> Event:
        """Apply a warden's assertion to both the warden state and the stream.

        Both, because they answer different questions. The warden state drives
        the zone panels and the sweep; the event stream is what makes the
        assertion reconstructable evidence in the person's own history.
        """
        queue = self.warden.device(action.device_id, action.warden_id)
        event = to_event(action, tenant_id=self.tenant_id, site_id=self.site_id,
                         drill_id=self.drill_id, seq=queue.next_seq)
        queue.next_seq += 1
        self.warden.apply(action)
        self._folded_warden.add(event.event_id)
        self.feed([event])
        return event

    def record_headcount(self, headcount: Headcount) -> Headcount:
        """Record a physical count, and put it in the event log.

        The second half was missing. A headcount is the only measurement in a
        drill taken by a human and independent of the cameras, two of the eight
        validation criteria are computed from it, and it lived in this
        process's memory alone: not stored, not replicated to central, gone on
        a restart. Invariant 6 says every accountability decision is
        reconstructable from stored events, and this one was not.
        """
        recorded = self.warden.record_headcount(headcount)
        queue = self.warden.device(headcount.device_id, headcount.warden_id)
        event = headcount_to_event(
            recorded, tenant_id=self.tenant_id, site_id=self.site_id,
            drill_id=self.drill_id, seq=queue.next_seq)
        queue.next_seq += 1
        self._folded_warden.add(event.event_id)
        self.feed([event])
        return recorded

    def tick(self, now_ms: int) -> None:
        self.ingestor.tick(now_ms)

    # -- output ----------------------------------------------------------------

    def board(self, now_ms: int) -> LiveBoard:
        return build_board(
            self.ingestor.state, self.roster, now_ms=now_ms,
            warden_evidence=self.warden.all_evidence(),
            config=self.accountability_config,
            # No caller ever passed this, so the operator tile, the warden tab
            # and the report line all read zero on every drill ever run --
            # including drills where a warden had tagged somebody standing in
            # front of them who was on nobody's list.
            unknown_people=self.warden.tagged_unknowns())

    def explain(self, person_ref: str) -> Explanation | None:
        """Why one person is in the state they are in.

        Resolves the roster entry to whichever track claimed their identity, so
        an operator asking about an employee gets that employee's evidence
        rather than having to know a global person id.
        """
        resolution = resolve_identities(self.ingestor.state, self.roster).get(person_ref)
        if resolution is None:
            return None
        # Every track, not the first of them. One person's evidence is split
        # across as many global ids as the tracker fragmented them into, and a
        # contested track holds evidence about this person too -- that is what
        # put it in conflict, and it is usually why the operator opened the
        # drawer.
        subjects = list(resolution.claimed_by) + list(resolution.contested_on)
        if not subjects:
            return self.ingestor.state.ledger.explain(person_ref)
        return self.ingestor.state.ledger.explain_many(subjects)

    def bottlenecks(self, now_ms: int):
        """Where the evacuation is slow, and which exit is holding it up."""
        return self.ingestor.state.bottlenecks.measure(
            now_ms, started_ms=self.started_ms)

    def zone_panels(self) -> list:
        return self.warden.zone_panels(self.roster.by_assembly_zone())

    def timing(self, now_ms: int) -> DrillTiming:
        resolutions = resolve_identities(self.ingestor.state, self.roster)
        arrivals = self.ingestor.state.assembly_arrival_ms
        timings = []
        for entry in self.roster.expected:
            resolution = resolutions[entry.person_ref]
            arrival = None
            for gid in resolution.claimed_by:
                if gid in arrivals:
                    candidate = arrivals[gid]
                    arrival = candidate if arrival is None else min(arrival, candidate)
            timings.append(measure(
                person_id=entry.person_ref,
                drill_started_ms=self.started_ms,
                assembly_arrival_ms=arrival,
                zone_id=entry.assigned_assembly_zone,
                floor_id=entry.home_floor_id,
                was_observed=resolution.is_observed))

        settled = self._accountability_completion(now_ms)
        return DrillTiming(
            building=summarise(timings),
            by_floor=summarise_by(timings, "floor_id"),
            by_zone=summarise_by(timings, "zone_id"),
            accountability_completed_ms=settled)

    def _accountability_completion(self, now_ms: int) -> int | None:
        from app.core.accountability_fsm import AccountabilityState

        unresolved = {
            AccountabilityState.UNCERTAIN,
            AccountabilityState.MANUAL_VERIFICATION_REQUIRED,
            AccountabilityState.NOT_EVACUATED,
            AccountabilityState.EVACUATING,
        }
        board = self.board(now_ms)
        if any(row.state in unresolved for row in board.rows):
            return None
        return self.elapsed_ms(now_ms)

    # -- the all-clear ---------------------------------------------------------

    def all_clear(self, now_ms: int) -> bool:
        """Both halves, or it is not an all-clear."""
        return (self.board(now_ms).all_clear
                and self.warden.is_every_zone_clean(self.roster.by_assembly_zone()))

    def blocking_all_clear(self, now_ms: int) -> list[str]:
        """Every reason the drill cannot be signed off. Never empty when it cannot."""
        reasons = list(self.board(now_ms).blocking_all_clear())
        reasons.extend(self.warden.blocking_clean(self.roster.by_assembly_zone()))
        if self.status is DrillStatus.DRAFT:
            reasons.insert(0, "the drill has not been started")
        return reasons

    def _persist(self) -> bool:
        """Record the drill row. A failure degrades rather than stops."""
        if self.drill_store is None:
            return False
        return self.drill_store.save(self)

    @property
    def is_durable(self) -> bool:
        """Whether this drill would survive a restart.

        Surfaced rather than assumed: an operator running a drill that is not
        being recorded should know, because the post-drill report is the reason
        most drills are run at all.

        A store being attached is not the same as a store working. While the
        database is unreachable the events are in this process's memory and a
        restart loses them, and once the buffer has overflowed some of them are
        gone whatever happens next. Both used to read as `True` here, which
        made the property say the opposite of its first sentence at exactly the
        moment it mattered.
        """
        return (self.events_store is not None and self.drill_store is not None
                and not self.events_store.is_degraded)

    def recover(self, now_ms: int) -> int:
        """Rebuild state from the stored event log. Returns events replayed.

        The fold is deterministic and idempotent, so a recovered drill reaches
        exactly the state it had. Warden state is rebuilt too, because warden
        actions are events like any other -- which this said before it was
        true. Only the ingest fold was replayed, and the fold knows about
        confirmations and rejections and nothing else a warden does. A node
        that restarted mid-drill came back with every zone unswept, every note
        gone, and its wardens asked to walk the building again.
        """
        return self.catch_up(now_ms)

    def catch_up(self, now_ms: int) -> int:
        """Fold whatever the store has gained since this drill last looked.

        The same work as `recover`, done repeatedly and cheaply, and it is what
        makes two processes one drill. The edge process consumes the camera
        stream and writes it down; the API process holds the board an operator
        is looking at. Without this the board showed only what the API process
        itself produced -- the start, the wardens' work -- and the cameras
        might as well have been switched off.

        Nothing is written here. Events read out of the store are already
        stored and already queued for central, and re-persisting them on every
        board refresh would be a write amplification measured in drills.
        """
        if self.events_store is None:
            return 0
        events, cursor = self.events_store.replay_since(
            self.drill_id, self._replay_cursor)
        if not events:
            return 0
        applied = self.ingestor.feed_batch(events)
        self._replay_warden_actions(events)
        self._replay_cursor = cursor
        self.ingestor.tick(now_ms)
        return applied

    def _replay_warden_actions(self, events: list[Event]) -> None:
        """Fold the warden half of the log back into `self.warden`.

        Applied rather than re-recorded: `record_warden_action` would write
        each one to the store again and hand it a fresh device sequence, and a
        recovery that duplicates the evidence it is recovering is worse than
        one that loses it.

        Device sequences are advanced past what was replayed so a device that
        reconnects after the restart is not treated as sending numbers it has
        already sent.
        """
        for event in events:
            if event.source_kind is not SourceKind.WARDEN:
                continue
            if event.event_id in self._folded_warden:
                continue
            self._folded_warden.add(event.event_id)
            count = headcount_from_event(event)
            if count is not None:
                self.warden.record_headcount(count)
                queue = self.warden.device(count.device_id, count.warden_id)
                queue.next_seq = max(queue.next_seq, event.seq + 1)
                continue

            action = from_event(event)
            if action is None:
                continue
            self.warden.apply(action)
            queue = self.warden.device(action.device_id, action.warden_id)
            queue.next_seq = max(queue.next_seq, event.seq + 1)

    def _system_event(self, event_type: EventType, now_ms: int) -> Event:
        self._system_seq += 1
        return Event(
            tenant_id=self.tenant_id, site_id=self.site_id, drill_id=self.drill_id,
            source=f"edge:{self.site_id}", source_kind=SourceKind.SYSTEM,
            seq=self._system_seq, type=event_type, ts_ms=now_ms, payload={})


@dataclass
class DrillRegistry:
    """Every drill this node knows about.

    In-memory by design for Phase 4: the edge node is the authority during a
    drill and its state is reconstructable from the event stream, so durability
    is a Phase 5 concern rather than a correctness one. What is not acceptable
    is losing a drill silently, so `get` raises rather than returning None.
    """

    drills: dict = field(default_factory=dict)

    def add(self, drill: Drill) -> Drill:
        if drill.drill_id in self.drills:
            raise DrillError(f"drill {drill.drill_id} already exists")
        self.drills[drill.drill_id] = drill
        return drill

    def get(self, drill_id: str) -> Drill:
        drill = self.drills.get(drill_id)
        if drill is None:
            raise KeyError(drill_id)
        return drill

    def list(self) -> list:
        return sorted(self.drills.values(), key=lambda d: -d.created_ms)

    def recover(self, *, drill_store, events_store, site_id: str,
                now_ms: int, assembly_zones: frozenset = frozenset(),
                replicator=None) -> list:
        """Reload drills that were running when the process stopped.

        Only running ones. A completed drill is history; a running one is a
        building that may still have people in it, and an edge node that
        restarts mid-evacuation must come back with the same board rather than
        an empty one.
        """
        from app.store.drills import roster_from_json

        try:
            rows = drill_store.unfinished(site_id)
        except Exception as exc:
            # An empty list would mean one of two things the caller cannot tell
            # apart: nothing was running, or the database could not be read.
            # The second must not come back as a clean, empty board.
            raise RecoveryUnavailable(
                f"could not read which drills were running: {exc}") from exc

        recovered = []
        for row in rows:
            if row["drill_id"] in self.drills:
                continue
            drill = Drill(
                drill_id=row["drill_id"], tenant_id=row["tenant_id"],
                site_id=row["site_id"], name=row["name"],
                roster=roster_from_json(row["roster_snapshot"] or {}),
                created_ms=row["created_ms"],
                assembly_zones=assembly_zones,
                events_store=events_store, drill_store=drill_store,
                replicator=replicator)
            drill.status = DrillStatus(row["status"])
            drill.started_ms = row["started_ms"]
            drill.completed_ms = row["completed_ms"]
            drill.recover(now_ms)
            self.drills[drill.drill_id] = drill
            recovered.append(drill)
        return recovered

    def running(self) -> Drill | None:
        """The drill currently in progress, if any.

        At most one runs at a time on a site. Two simultaneous evacuations of
        one building is not a scenario; it is a bug that would split the roster.
        """
        for drill in self.drills.values():
            if drill.is_running:
                return drill
        return None
