"""Warden state, and how a human assertion becomes something the software acts on."""

from __future__ import annotations

import pytest

from app.core.accountability_fsm import AccountabilityState
from app.core.roster import ExpectationReason, Roster
from app.ingest.ingestor import Ingestor
from app.ingest.projections import build_board
from app.warden.actions import ActionKind, WardenAction
from app.warden.headcount import Headcount
from app.warden.state import WardenState

T0 = 1_788_000_000_000
ASSEMBLY = frozenset({"assembly-north", "assembly-south"})


def action(kind=ActionKind.CONFIRM_PRESENT, zone="assembly-north", **overrides):
    base = dict(kind=kind, warden_id="warden-7", device_id="tablet-3",
                zone_id=zone, ts_ms=T0, subject="emp:EMP-0001")
    base.update(overrides)
    return WardenAction(**base)


def roster_of(*specs):
    roster = Roster()
    for emp_id, zone in specs:
        roster.add_employee(emp_id=emp_id, display_name=emp_id,
                            has_gallery_entry=True, assigned_assembly_zone=zone,
                            reason=ExpectationReason.ON_SHIFT)
    return roster.snapshot(T0)


@pytest.fixture
def state() -> WardenState:
    return WardenState(assembly_zones=ASSEMBLY)


class TestDerivingEvidence:
    def test_a_confirmation_at_an_assembly_zone_counts_as_arrival(self, state):
        state.apply(action())
        evidence = state.evidence_for("emp:EMP-0001")
        assert evidence.confirmed is True
        assert evidence.at_assembly_zone == "assembly-north"
        assert evidence.confirms_at_assembly is True

    def test_a_confirmation_on_a_floor_is_identity_evidence_only(self, state):
        # Recognising a colleague in a corridor is not evidence they got out.
        state.apply(action(zone="floor-3"))
        evidence = state.evidence_for("emp:EMP-0001")
        assert evidence.confirmed is True
        assert evidence.at_assembly_zone is None
        assert evidence.confirms_at_assembly is False

    def test_someone_with_no_warden_action_has_no_warden_evidence(self, state):
        assert state.evidence_for("emp:EMP-9999").confirmed is False

    def test_the_latest_assertion_is_the_operative_one(self, state):
        state.apply(action(ts_ms=T0))
        state.apply(action(ActionKind.NOT_HERE, ts_ms=T0 + 60_000))
        evidence = state.evidence_for("emp:EMP-0001")
        assert evidence.confirmed is False

    def test_but_nothing_is_erased(self, state):
        # A warden changing their mind is itself information.
        state.apply(action(ts_ms=T0))
        state.apply(action(ActionKind.NOT_HERE, ts_ms=T0 + 60_000))
        assert len(state.actions) == 2

    def test_an_out_of_order_arrival_does_not_beat_a_later_assertion(self, state):
        # Offline queues sync late. The action's own timestamp decides, not the
        # order it reached the server.
        state.apply(action(ActionKind.NOT_HERE, ts_ms=T0 + 60_000))
        state.apply(action(ts_ms=T0))
        assert state.evidence_for("emp:EMP-0001").confirmed is False

    def test_marking_absent_is_carried_through(self, state):
        state.apply(action(ActionKind.MARK_ABSENT))
        assert state.evidence_for("emp:EMP-0001").marked_absent is True


class TestEvidenceReachesTheBoard:
    def test_a_warden_confirmation_accounts_for_someone(self, state):
        state.apply(action())
        ingestor = Ingestor()
        board = build_board(
            ingestor.state, roster_of(("EMP-0001", "assembly-north")),
            now_ms=T0 + 600_000, warden_evidence=state.all_evidence())
        assert board.accounted == 1
        assert board.rows[0].state is AccountabilityState.ACCOUNTED

    def test_a_floor_confirmation_does_not(self, state):
        state.apply(action(zone="floor-3"))
        ingestor = Ingestor()
        board = build_board(
            ingestor.state, roster_of(("EMP-0001", "assembly-north")),
            now_ms=T0 + 600_000, warden_evidence=state.all_evidence())
        assert board.rows[0].state is not AccountabilityState.ACCOUNTED

    def test_a_warden_reporting_someone_absent_does_not_clear_them(self, state):
        # "Not on site today" is not "evacuated safely".
        state.apply(action(ActionKind.MARK_ABSENT))
        ingestor = Ingestor()
        board = build_board(
            ingestor.state, roster_of(("EMP-0001", "assembly-north")),
            now_ms=T0 + 600_000, warden_evidence=state.all_evidence())
        assert board.rows[0].state is AccountabilityState.UNCERTAIN


class TestMismatchesReachTheCommandCentre:
    def _count(self, physical, system, zone="assembly-north"):
        return Headcount(zone_id=zone, warden_id="warden-7", device_id="tablet-3",
                         ts_ms=T0, physical_count=physical, system_count=system)

    def test_the_dangerous_direction_sorts_first(self, state):
        state.record_headcount(self._count(44, 40, "assembly-south"))
        state.record_headcount(self._count(38, 40, "assembly-north"))
        mismatches = state.mismatches()
        assert mismatches[0].zone_id == "assembly-north"

    def test_an_agreeing_zone_does_not_appear(self, state):
        state.record_headcount(self._count(40, 40))
        assert state.mismatches() == []

    def test_escalations_are_listed(self, state):
        state.apply(action(ActionKind.ESCALATE, subject=None,
                           note="smoke in the west stairwell"))
        assert [s.zone_id for s in state.escalations()] == ["assembly-north"]

    def test_a_device_out_of_contact_is_flagged(self, state):
        # A warden working from a roster that may have moved.
        queue = state.device("tablet-9", "warden-9")
        queue.record(action(ts_ms=T0), online=False)
        stale = state.stale_devices(T0 + 240_000, threshold_ms=120_000)
        assert stale and stale[0]["device_id"] == "tablet-9"
        assert stale[0]["stale_ms"] == 240_000

    def test_a_recently_synced_device_is_not_flagged(self, state):
        queue = state.device("tablet-9", "warden-9")
        queue.record(action(ts_ms=T0), online=False)
        assert state.stale_devices(T0 + 10_000, threshold_ms=120_000) == []


class TestEveryZoneClean:
    """The manual half of the all-clear."""

    def _expected(self):
        roster = roster_of(("EMP-1", "assembly-north"), ("EMP-2", "assembly-north"))
        return roster.by_assembly_zone()

    def test_a_drill_where_no_warden_walked_is_not_clean(self, state):
        # Cameras being content and nobody having checked is an untested
        # assumption, not an all-clear.
        assert state.is_every_zone_clean(self._expected()) is False
        assert any("no warden has started" in r
                   for r in state.blocking_clean(self._expected()))

    def test_a_fully_swept_agreeing_zone_is_clean(self, state):
        for emp in ("emp:EMP-1", "emp:EMP-2"):
            state.apply(action(subject=emp))
        state.record_headcount(
            Headcount(zone_id="assembly-north", warden_id="warden-7",
                      device_id="tablet-3", ts_ms=T0, physical_count=2,
                      system_count=2))
        state.apply(action(ActionKind.SWEEP_COMPLETE, subject=None,
                           ts_ms=T0 + 60_000))
        assert state.is_every_zone_clean(self._expected()) is True

    def test_one_unresolved_person_blocks_every_zone(self, state):
        state.apply(action(subject="emp:EMP-1"))
        state.record_headcount(
            Headcount(zone_id="assembly-north", warden_id="warden-7",
                      device_id="tablet-3", ts_ms=T0, physical_count=1,
                      system_count=1))
        state.apply(action(ActionKind.SWEEP_COMPLETE, subject=None,
                           ts_ms=T0 + 60_000))
        assert state.is_every_zone_clean(self._expected()) is False

    def test_an_empty_site_is_not_an_all_clear(self, state):
        assert state.is_every_zone_clean({}) is False

    def test_the_zone_panel_shows_progress(self, state):
        state.apply(action(subject="emp:EMP-1"))
        panels = state.zone_panels(self._expected())
        assert len(panels) == 1
        assert panels[0]["expected"] == 2
        assert panels[0]["confirmed"] == 1
        assert panels[0]["outstanding"] == 1
