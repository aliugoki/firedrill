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
from app.ingest.ingestor import Ingestor
from app.ingest.projections import LiveBoard, build_board, resolve_identities
from app.warden.actions import WardenAction, to_event
from app.warden.headcount import Headcount
from app.warden.state import WardenState


class DrillStatus(str, Enum):
    DRAFT = "DRAFT"
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"


class DrillError(RuntimeError):
    """An operation the drill's lifecycle does not allow."""


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

    ingestor: Ingestor = field(init=False)
    warden: WardenState = field(init=False)
    _system_seq: int = 0

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
        return self.ingestor.feed_batch(events)

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
        self.feed([event])
        return event

    def record_headcount(self, headcount: Headcount) -> Headcount:
        return self.warden.record_headcount(headcount)

    def tick(self, now_ms: int) -> None:
        self.ingestor.tick(now_ms)

    # -- output ----------------------------------------------------------------

    def board(self, now_ms: int) -> LiveBoard:
        return build_board(
            self.ingestor.state, self.roster, now_ms=now_ms,
            warden_evidence=self.warden.all_evidence(),
            config=self.accountability_config)

    def explain(self, person_ref: str) -> Explanation | None:
        """Why one person is in the state they are in.

        Resolves the roster entry to whichever track claimed their identity, so
        an operator asking about an employee gets that employee's evidence
        rather than having to know a global person id.
        """
        resolution = resolve_identities(self.ingestor.state, self.roster).get(person_ref)
        if resolution is None:
            return None
        subjects = list(resolution.claimed_by) or list(resolution.contested_on)
        if not subjects:
            return self.ingestor.state.ledger.explain(person_ref)
        return self.ingestor.state.ledger.explain(subjects[0])

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
        """
        return self.events_store is not None and self.drill_store is not None

    def recover(self, now_ms: int) -> int:
        """Rebuild state from the stored event log. Returns events replayed.

        The fold is deterministic and idempotent, so a recovered drill reaches
        exactly the state it had. Warden state is rebuilt too, because warden
        actions are events like any other.
        """
        if self.events_store is None:
            return 0
        events = self.events_store.replay(self.drill_id)
        applied = self.ingestor.feed_batch(events)
        self.ingestor.tick(now_ms)
        return applied

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
                now_ms: int, assembly_zones: frozenset = frozenset()) -> list:
        """Reload drills that were running when the process stopped.

        Only running ones. A completed drill is history; a running one is a
        building that may still have people in it, and an edge node that
        restarts mid-evacuation must come back with the same board rather than
        an empty one.
        """
        from app.store.drills import roster_from_json

        recovered = []
        for row in drill_store.unfinished(site_id):
            if row["drill_id"] in self.drills:
                continue
            drill = Drill(
                drill_id=row["drill_id"], tenant_id=row["tenant_id"],
                site_id=row["site_id"], name=row["name"],
                roster=roster_from_json(row["roster_snapshot"] or {}),
                created_ms=row["created_ms"],
                assembly_zones=assembly_zones,
                events_store=events_store, drill_store=drill_store)
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
