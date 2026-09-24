"""Surviving a restart mid-drill.

This is the point of having built the store. An edge node that restarts during
an evacuation must come back with the same board, not an empty one, and these
tests drive a real drill through real SQL to prove it.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.core.accountability_fsm import AccountabilityState
from app.core.events import Event, EventType, SourceKind
from app.core.roster import ExpectationReason, Roster
from app.drill import Drill, DrillRegistry, DrillStatus
from app.store.drills import DrillStore
from app.store.events import EventStore
from app.store.schema import metadata
from app.warden.actions import ActionKind, WardenAction

T0 = 1_788_000_000_000
ASSEMBLY = frozenset({"assembly-north"})


@pytest.fixture
def engine():
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool,
                              connect_args={"check_same_thread": False})
    metadata.create_all(engine)
    return engine


@pytest.fixture
def stores(engine):
    return EventStore(engine=engine), DrillStore(engine=engine)


def roster_of(people=4):
    roster = Roster()
    for i in range(people):
        roster.add_employee(
            emp_id=f"EMP-{i:03d}", display_name=f"Person {i}",
            has_gallery_entry=True, department="Engineering",
            home_floor_id="floor-2", assigned_assembly_zone="assembly-north",
            reason=ExpectationReason.ON_SHIFT)
    return roster.snapshot(T0)


def make_drill(stores, drill_id="d1", people=4) -> Drill:
    events_store, drill_store = stores
    return Drill(
        drill_id=drill_id, tenant_id="t", site_id="s", name="Q3 drill",
        roster=roster_of(people), created_ms=T0, assembly_zones=ASSEMBLY,
        events_store=events_store, drill_store=drill_store)


def camera_event(seq, subject, event_type, payload, ts_ms):
    return Event(
        tenant_id="t", site_id="s", drill_id="d1", source="cam-9",
        source_kind=SourceKind.CAMERA, seq=seq, type=event_type,
        ts_ms=ts_ms, subject=subject, payload=payload)


def walk_someone_to_assembly(drill, gid="gp-1", emp="EMP-000", start=T0 + 1_000):
    seq = 0
    events = []
    for i in range(3):
        seq += 1
        events.append(camera_event(
            seq, gid, EventType.FACE_OBSERVED,
            {"candidate_id": emp, "score": 0.85, "margin": 0.4, "quality": 0.9,
             "association": "SHARED_TRACK",
             "camera_id": "cam-9"}, start + i * 100))
    for i in range(2):
        seq += 1
        events.append(camera_event(
            seq, gid, EventType.TRACK_UPDATED,
            {"zone_id": "assembly-north", "zone_kind": "ASSEMBLY",
             "camera_id": "cam-9"}, start + 10_000 * (i + 1)))
    drill.feed(events)
    return seq


class TestPersistBeforeFolding:
    """Folding first leaves a window where the state has an event the record
    does not, and a restart silently loses it."""

    def test_events_reach_the_store(self, stores):
        drill = make_drill(stores)
        drill.start(T0)
        walk_someone_to_assembly(drill)
        assert stores[0].count("d1") >= 6

    def test_the_drill_start_is_itself_recorded(self, stores):
        drill = make_drill(stores)
        drill.start(T0)
        types = {e.type for e in stores[0].replay("d1")}
        assert EventType.DRILL_STARTED in types

    def test_a_warden_action_is_recorded(self, stores):
        drill = make_drill(stores)
        drill.start(T0)
        drill.record_warden_action(WardenAction(
            kind=ActionKind.CONFIRM_PRESENT, warden_id="warden-7",
            device_id="tablet-3", zone_id="assembly-north", ts_ms=T0 + 60_000,
            subject="emp:EMP-001"))
        types = {e.type for e in stores[0].replay("d1")}
        assert EventType.WARDEN_CONFIRMED in types


class TestRecoveringADrill:
    def test_a_restarted_drill_reaches_the_same_board(self, stores):
        live = make_drill(stores)
        live.start(T0)
        walk_someone_to_assembly(live)
        before = live.board(T0 + 600_000)

        # A new process. Same stores, nothing in memory.
        restarted = make_drill(stores)
        restarted.status = DrillStatus.RUNNING
        restarted.started_ms = T0
        assert restarted.recover(T0 + 600_000) > 0

        after = restarted.board(T0 + 600_000)
        assert after.accounted == before.accounted
        assert after.expected == before.expected
        assert [row.state for row in after.rows] == [row.state for row in before.rows]

    def test_an_accounted_person_stays_accounted(self, stores):
        live = make_drill(stores)
        live.start(T0)
        walk_someone_to_assembly(live)
        assert live.board(T0 + 600_000).accounted == 1

        restarted = make_drill(stores)
        restarted.status = DrillStatus.RUNNING
        restarted.started_ms = T0
        restarted.recover(T0 + 600_000)
        assert restarted.board(T0 + 600_000).accounted == 1

    def test_recovering_twice_changes_nothing(self, stores):
        live = make_drill(stores)
        live.start(T0)
        walk_someone_to_assembly(live)

        restarted = make_drill(stores)
        restarted.status = DrillStatus.RUNNING
        restarted.started_ms = T0
        restarted.recover(T0 + 600_000)
        once = restarted.board(T0 + 600_000).accounted
        restarted.recover(T0 + 600_000)
        assert restarted.board(T0 + 600_000).accounted == once

    def test_the_device_sequence_survives_the_restart(self, stores):
        """Otherwise a tablet still holding unacknowledged actions has every
        one of them applied a second time after the node comes back.

        `device_seq` arrived on the wire, was used for duplicate detection in
        memory, and was thrown away -- so nothing about what a device had
        already delivered outlived the process.
        """
        live = make_drill(stores)
        live.start(T0)
        live.record_warden_action(WardenAction(
            kind=ActionKind.CONFIRM_PRESENT, warden_id="warden-7",
            device_id="tablet-3", zone_id="assembly-north", ts_ms=T0 + 60_000,
            subject="emp:EMP-001", device_seq=4))

        restarted = make_drill(stores)
        restarted.status = DrillStatus.RUNNING
        restarted.started_ms = T0
        restarted.recover(T0 + 600_000)

        assert restarted.warden.devices["tablet-3"].last_device_seq == 4

    def test_an_older_event_without_one_still_recovers(self, stores):
        # Events written before the sequence was carried have no `device_seq`,
        # and a drill recorded from one of those is better off deduping on
        # nothing than refusing to load.
        live = make_drill(stores)
        live.start(T0)
        live.record_warden_action(WardenAction(
            kind=ActionKind.CONFIRM_PRESENT, warden_id="warden-7",
            device_id="tablet-3", zone_id="assembly-north", ts_ms=T0 + 60_000,
            subject="emp:EMP-001"))

        restarted = make_drill(stores)
        restarted.status = DrillStatus.RUNNING
        restarted.started_ms = T0
        assert restarted.recover(T0 + 600_000) > 0
        assert restarted.warden.devices["tablet-3"].last_device_seq == 0

    def test_recovery_without_a_store_is_a_no_op(self, stores):
        bare = Drill(drill_id="d9", tenant_id="t", site_id="s", name="x",
                     roster=roster_of(1), created_ms=T0)
        assert bare.recover(T0) == 0


class TestTheRosterIsFrozenNotRefetched:
    """A drill recovered after a restart must see the same expected set. A fresh
    fetch would silently change the denominator mid-drill."""

    def test_the_roster_survives_the_round_trip(self, stores):
        drill = make_drill(stores, people=6)
        drill.start(T0)

        registry = DrillRegistry()
        recovered = registry.recover(
            drill_store=stores[1], events_store=stores[0], site_id="s",
            now_ms=T0 + 600_000, assembly_zones=ASSEMBLY)
        assert len(recovered) == 1
        assert recovered[0].roster.expected_count == 6

    def test_the_roster_entries_come_back_whole(self, stores):
        drill = make_drill(stores, people=2)
        drill.start(T0)

        registry = DrillRegistry()
        [recovered] = registry.recover(
            drill_store=stores[1], events_store=stores[0], site_id="s",
            now_ms=T0 + 600_000, assembly_zones=ASSEMBLY)
        entry = recovered.roster.by_emp_id("EMP-000")
        assert entry.department == "Engineering"
        assert entry.assigned_assembly_zone == "assembly-north"
        assert entry.has_gallery_entry is True


class TestTheRegistryRecovers:
    def test_only_running_drills_are_reloaded(self, stores):
        # A completed drill is history; a running one is a building that may
        # still have people in it.
        finished = make_drill(stores, drill_id="done")
        finished.start(T0)
        finished.complete(T0 + 300_000)

        running = make_drill(stores, drill_id="live")
        running.start(T0)

        registry = DrillRegistry()
        recovered = registry.recover(
            drill_store=stores[1], events_store=stores[0], site_id="s",
            now_ms=T0 + 600_000)
        assert [d.drill_id for d in recovered] == ["live"]

    def test_a_recovered_drill_is_the_running_one(self, stores):
        make_drill(stores).start(T0)
        registry = DrillRegistry()
        registry.recover(drill_store=stores[1], events_store=stores[0],
                         site_id="s", now_ms=T0 + 600_000)
        assert registry.running() is not None

    def test_recovery_does_not_duplicate_a_drill_already_held(self, stores):
        drill = make_drill(stores)
        drill.start(T0)
        registry = DrillRegistry()
        registry.add(drill)
        recovered = registry.recover(
            drill_store=stores[1], events_store=stores[0], site_id="s",
            now_ms=T0 + 600_000)
        assert recovered == []
        assert len(registry.list()) == 1

    def test_nothing_to_recover_is_not_an_error(self, stores):
        registry = DrillRegistry()
        assert registry.recover(drill_store=stores[1], events_store=stores[0],
                                site_id="s", now_ms=T0) == []


class TestADrillRunsWithoutAStore:
    """An operator with an evacuation in progress needs the drill more than the
    software needs its own bookkeeping."""

    def test_a_drill_with_no_store_still_runs(self):
        drill = Drill(drill_id="d9", tenant_id="t", site_id="s", name="x",
                      roster=roster_of(2), created_ms=T0,
                      assembly_zones=ASSEMBLY)
        drill.start(T0)
        walk_someone_to_assembly(drill)
        assert drill.board(T0 + 600_000).accounted == 1

    def test_it_says_it_is_not_durable(self):
        # Surfaced rather than assumed: the post-drill report is why most
        # drills are run at all.
        drill = Drill(drill_id="d9", tenant_id="t", site_id="s", name="x",
                      roster=roster_of(2), created_ms=T0)
        assert drill.is_durable is False

    def test_a_drill_with_both_stores_is_durable(self, stores):
        assert make_drill(stores).is_durable is True

    def test_a_failing_drill_store_does_not_stop_the_drill(self, stores):
        events_store, drill_store = stores
        drill_store.engine = sa.create_engine(
            "postgresql+psycopg2://nobody@127.0.0.1:1/x")
        drill = make_drill((events_store, drill_store))
        drill.start(T0)  # must not raise
        assert drill.status is DrillStatus.RUNNING
        assert drill_store.last_error is not None


class TestTheWardensWorkSurvivesARestart:
    """`recover` said warden state was rebuilt. Only the ingest fold was.

    The fold understands confirmations and rejections, which is why a person
    accounted for by a warden came back. It understands nothing about sweeps,
    headcount context, notes, escalations or tagged unknowns, so a node that
    restarted mid-drill came back with every zone unswept and asked its wardens
    to walk the building again -- during an evacuation.
    """

    def _worked_zone(self, stores) -> Drill:
        from app.warden.actions import ActionKind, WardenAction

        drill = make_drill(stores)
        drill.start(T0)
        for kind, extra in (
            (ActionKind.CONFIRM_PRESENT, {"subject": "emp:EMP-000"}),
            (ActionKind.NOT_HERE, {"subject": "emp:EMP-001"}),
            (ActionKind.TAG_UNKNOWN, {"note": "contractor, lift engineer"}),
            (ActionKind.SWEEP_COMPLETE, {}),
        ):
            drill.record_warden_action(WardenAction(
                kind=kind, warden_id="warden-7", device_id="tablet-3",
                zone_id="assembly-north", ts_ms=T0 + 60_000, **extra))
        # A different zone, because escalation outranks completion and a zone
        # carrying both would not prove the completion came back.
        drill.record_warden_action(WardenAction(
            kind=ActionKind.ESCALATE, warden_id="warden-9",
            device_id="tablet-4", zone_id="assembly-south", ts_ms=T0 + 70_000,
            note="smoke in the north stairwell"))
        return drill

    def _restarted(self, stores) -> Drill:
        fresh = make_drill(stores, drill_id="d1")
        fresh.start(T0)
        fresh.recover(T0 + 120_000)
        return fresh

    def test_a_completed_sweep_is_still_completed(self, stores):
        self._worked_zone(stores)
        sweep = self._restarted(stores).warden.sweeps["assembly-north"]
        assert sweep.is_complete is True
        assert sweep.confirmed == {"emp:EMP-000"}
        assert sweep.not_here == {"emp:EMP-001"}

    def test_a_tagged_unknown_is_not_forgotten(self, stores):
        self._worked_zone(stores)
        assert self._restarted(stores).warden.tagged_unknowns() == 1

    def test_an_escalation_survives(self, stores):
        # The one action that says a warden needs help. Losing it on a restart
        # loses the request, and nobody is told it was lost.
        self._worked_zone(stores)
        escalated = self._restarted(stores).warden.escalations()
        assert [s.escalation_reason for s in escalated] == [
            "smoke in the north stairwell"]

    def test_the_device_can_carry_on_where_it_left_off(self, stores):
        # Its next sequence number is past everything already replayed, so a
        # device that reconnects is not treated as resending old work.
        self._worked_zone(stores)
        fresh = self._restarted(stores)
        # Four actions came from this tablet, so the next one it sends is 5.
        # Without the replay the queue starts at 1 and its next action collides
        # with work already stored.
        assert fresh.warden.device("tablet-3", "warden-7").next_seq == 5

    def test_recovery_does_not_duplicate_the_evidence_it_recovers(self, stores):
        # Re-recording each action would write it to the store again with a
        # fresh sequence, which is worse than losing it.
        events_store, _ = stores
        self._worked_zone(stores)
        before = events_store.count("d1")
        self._restarted(stores)
        # Not one row more. The fresh drill's own start event carries the same
        # sequence as the original's, and the uniqueness constraint rejects it.
        assert events_store.count("d1") == before


class TestThePhysicalCountIsEvidenceToo:
    """The one measurement in a drill that a human took and a camera did not.

    Two of the eight validation criteria are computed from it, and it lived in
    the process's memory alone: `record_headcount` folded it into warden state
    and never wrote an event. Not stored, not replicated to central, gone on a
    restart -- and invariant 6 says every accountability decision is
    reconstructable from stored events.
    """

    def _counted(self, stores) -> Drill:
        from app.warden.headcount import Headcount

        drill = make_drill(stores)
        drill.start(T0)
        drill.record_headcount(Headcount(
            zone_id="assembly-north", warden_id="warden-7",
            device_id="tablet-3", ts_ms=T0 + 90_000,
            physical_count=3, system_count=4))
        return drill

    def test_it_reaches_the_event_log(self, stores):
        events_store, _ = stores
        self._counted(stores)
        stored = [e for e in events_store.replay("d1")
                  if "headcount" in (e.payload or {})]
        assert len(stored) == 1
        assert stored[0].payload["headcount"]["physical_count"] == 3

    def test_it_comes_back_after_a_restart(self, stores):
        self._counted(stores)
        fresh = make_drill(stores, drill_id="d1")
        fresh.start(T0)
        fresh.recover(T0 + 120_000)

        latest = fresh.warden.sweeps["assembly-north"].headcounts.latest
        assert latest is not None
        assert latest.physical_count == 3
        assert latest.system_count == 4

    def test_the_mismatch_it_produced_comes_back_with_it(self, stores):
        # The number matters because of what it disagrees with. A recovery that
        # restored the count and lost the mismatch would report a clean drill.
        self._counted(stores)
        fresh = make_drill(stores, drill_id="d1")
        fresh.start(T0)
        fresh.recover(T0 + 120_000)
        assert len(fresh.warden.mismatches()) == 1

    def test_it_is_written_in_words_for_the_ledger(self, stores):
        # A bare number in an evidence trail tells a reader nothing about which
        # side of it was the system's.
        events_store, _ = stores
        self._counted(stores)
        note = [e for e in events_store.replay("d1")
                if "headcount" in (e.payload or {})][0].payload["note"]
        assert "counted 3" in note or "physical count 3" in note

    def test_an_ordinary_warden_note_is_not_read_as_a_count(self, stores):
        from app.warden.actions import ActionKind, WardenAction
        from app.warden.headcount import from_event

        drill = make_drill(stores)
        drill.start(T0)
        drill.record_warden_action(WardenAction(
            kind=ActionKind.NOTE, warden_id="warden-7", device_id="tablet-3",
            zone_id="assembly-north", ts_ms=T0 + 60_000, note="door jammed"))
        events_store, _ = stores
        for event in events_store.replay("d1"):
            assert from_event(event) is None


class TestADrillStopsHoldingWhatItMayNotKeep:
    """`purge_drill` says "everything a finished drill should no longer be
    holding" and was called by nothing, so the DRILL_END trigger never fired.

    The edge node's hourly check is the backstop and cannot be the control for
    this class. A device thumbnail is the shortest-lived thing in the system
    precisely because it leaves the building in somebody's hands, and "within
    the hour" is not what "purged at drill end" means.
    """

    def _drill_holding(self, stores):
        from app.infra.retention import DataClass, Item, RetentionLedger

        ledger = RetentionLedger()
        ledger.track(Item("thumb-1", DataClass.DEVICE_THUMBNAIL, T0, "d1"))
        ledger.track(Item("ev-1", DataClass.EVIDENCE, T0, "d1"))
        gone = []

        drill = make_drill(stores)
        drill.retention = ledger
        drill.retention_remover = gone.append
        drill.start(T0)
        return drill, ledger, gone

    def test_the_thumbnail_goes_when_the_drill_is_declared_over(self, stores):
        drill, ledger, gone = self._drill_holding(stores)
        assert [i.item_id for i in ledger.held()] == ["thumb-1", "ev-1"]

        drill.complete(T0 + 600_000)
        assert [i.item_id for i in gone] == ["thumb-1"]

    def test_the_evidence_stays(self, stores):
        # Evidence is not biometrics. The claim outlives the material by a year
        # because a post-incident report needs it and a face does not.
        drill, ledger, _ = self._drill_holding(stores)
        drill.complete(T0 + 600_000)
        assert [i.item_id for i in ledger.held()] == ["ev-1"]

    def test_a_failing_purge_does_not_stop_the_drill_ending(self, stores):
        # A drill that has just been declared over must finish being declared
        # over. The hourly job retries and `verify` reports what is left.
        class Broken:
            def purge_drill(self, *args, **kwargs):
                raise OSError("the store is gone")

        drill = make_drill(stores)
        drill.retention = Broken()
        drill.start(T0)
        drill.complete(T0 + 600_000)
        assert drill.status.value == "COMPLETE"

    def test_a_drill_with_no_ledger_completes_as_before(self, stores):
        drill = make_drill(stores)
        drill.start(T0)
        drill.complete(T0 + 600_000)
        assert drill.completed_ms == T0 + 600_000
