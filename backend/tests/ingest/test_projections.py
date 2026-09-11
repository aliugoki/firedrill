"""Read models: the board, the priority list, and what blocks an all-clear."""

from __future__ import annotations

import pytest

from app.core.accountability_fsm import AccountabilityState, WardenEvidence
from app.core.events import Event, EventType, SourceKind
from app.core.roster import ExpectationReason, Population, Roster
from app.ingest.ingestor import Ingestor
from app.ingest.projections import build_board, resolve_identities

T0 = 1_788_000_000_000
SEQ = {"n": 0}


def ev(event_type, ts_ms, subject=None, payload=None, source="cam-1",
       kind=SourceKind.CAMERA) -> Event:
    SEQ["n"] += 1
    return Event(
        tenant_id="t", site_id="s", drill_id="d", source=source,
        source_kind=kind, seq=SEQ["n"], type=event_type, ts_ms=ts_ms,
        subject=subject, payload=payload or {})


def roster_of(*specs):
    roster = Roster()
    for emp_id, name, zone, dept, gallery in specs:
        roster.add_employee(emp_id=emp_id, display_name=name,
                            has_gallery_entry=gallery, department=dept,
                            assigned_assembly_zone=zone,
                            reason=ExpectationReason.ON_SHIFT)
    return roster.snapshot(T0)


def confirmed_at_assembly(ingestor: Ingestor, gid: str, emp: str,
                          start=T0 + 1_000) -> None:
    ingestor.feed(ev(EventType.DRILL_STARTED, T0, kind=SourceKind.SYSTEM,
                     source="system"))
    for i in range(3):
        ingestor.feed(ev(EventType.FACE_OBSERVED, start + i * 100, gid,
                         {"candidate_id": emp, "score": 0.85, "margin": 0.4,
                          "quality": 0.9, "camera_id": "cam-9"}))
    for i in range(2):
        ingestor.feed(ev(EventType.TRACK_UPDATED, start + 10_000 * (i + 1), gid,
                         {"zone_id": "assembly-north", "zone_kind": "ASSEMBLY",
                          "camera_id": "cam-9"}))


@pytest.fixture
def ingestor() -> Ingestor:
    SEQ["n"] = 0
    return Ingestor()


class TestResolution:
    def test_a_roster_entry_maps_to_the_track_claiming_its_identity(self, ingestor):
        confirmed_at_assembly(ingestor, "gp-1", "EMP-1")
        roster = roster_of(("EMP-1", "Ayesha Khan", "north", "Engineering", True))
        resolution = resolve_identities(ingestor.state, roster)["emp:EMP-1"]
        assert resolution.claimed_by == ("gp-1",)
        assert resolution.is_observed is True

    def test_an_unclaimed_entry_is_simply_unobserved(self, ingestor):
        roster = roster_of(("EMP-9", "Nobody Seen", "north", "HR", True))
        resolution = resolve_identities(ingestor.state, roster)["emp:EMP-9"]
        assert resolution.claimed_by == ()
        assert resolution.is_observed is False

    def test_a_contested_identity_is_still_findable(self, ingestor):
        # A track in CONFLICT drops its claim. Without tracking the contest, the
        # employees involved would read as never observed, which is wrong twice
        # over: somebody was seen, and the system knows why it cannot say who.
        for i in range(3):
            ingestor.feed(ev(EventType.FACE_OBSERVED, T0 + i * 100, "gp-1",
                             {"candidate_id": "EMP-1", "score": 0.85,
                              "margin": 0.4, "quality": 0.9}))
        for i in range(3):
            ingestor.feed(ev(EventType.FACE_OBSERVED, T0 + 5_000 + i * 100, "gp-1",
                             {"candidate_id": "EMP-2", "score": 0.85,
                              "margin": 0.4, "quality": 0.9}))
        roster = roster_of(("EMP-1", "A", "north", "Eng", True),
                           ("EMP-2", "B", "north", "Eng", True))
        resolutions = resolve_identities(ingestor.state, roster)
        assert resolutions["emp:EMP-1"].contested_on == ("gp-1",)
        assert resolutions["emp:EMP-1"].is_ambiguous is True

    def test_sequential_fragments_are_not_a_conflict(self, ingestor):
        # Ordinary fragmentation: one person's evidence split in half. Calling
        # that a conflict would bury the real ones.
        confirmed_at_assembly(ingestor, "gp-1", "EMP-1", start=T0 + 1_000)
        confirmed_at_assembly(ingestor, "gp-1#2", "EMP-1", start=T0 + 400_000)
        roster = roster_of(("EMP-1", "A", "north", "Eng", True))
        resolution = resolve_identities(ingestor.state, roster)["emp:EMP-1"]
        assert len(resolution.claimed_by) == 2
        assert resolution.simultaneous == ()

    def test_overlapping_tracks_claiming_one_person_are_a_conflict(self, ingestor):
        # One person cannot be in two places while both are being watched.
        confirmed_at_assembly(ingestor, "gp-1", "EMP-1", start=T0 + 1_000)
        confirmed_at_assembly(ingestor, "gp-2", "EMP-1", start=T0 + 2_000)
        roster = roster_of(("EMP-1", "A", "north", "Eng", True))
        resolution = resolve_identities(ingestor.state, roster)["emp:EMP-1"]
        assert set(resolution.simultaneous) == {"gp-1", "gp-2"}


class TestBoardCounts:
    def test_a_confirmed_person_at_assembly_is_green(self, ingestor):
        confirmed_at_assembly(ingestor, "gp-1", "EMP-1")
        roster = roster_of(("EMP-1", "Ayesha Khan", "north", "Engineering", True))
        board = build_board(ingestor.state, roster, now_ms=T0 + 60_000)
        assert board.accounted == 1
        assert board.rows[0].colour == "GREEN"

    def test_someone_never_seen_is_red_once_the_drill_has_run(self, ingestor):
        ingestor.feed(ev(EventType.DRILL_STARTED, T0, kind=SourceKind.SYSTEM,
                         source="system"))
        roster = roster_of(("EMP-9", "Nobody", "north", "HR", True))
        board = build_board(ingestor.state, roster, now_ms=T0 + 600_000)
        assert board.unaccounted == 1
        assert board.rows[0].colour == "RED"

    def test_the_expected_count_is_the_roster_not_the_tracks(self, ingestor):
        confirmed_at_assembly(ingestor, "gp-1", "EMP-1")
        roster = roster_of(("EMP-1", "A", "north", "Eng", True),
                           ("EMP-2", "B", "north", "Eng", True),
                           ("EMP-3", "C", "south", "HR", True))
        board = build_board(ingestor.state, roster, now_ms=T0 + 600_000)
        assert board.expected == 3
        assert board.accounted == 1

    def test_a_row_carries_where_to_go_and_look(self, ingestor):
        ingestor.feed(ev(EventType.DRILL_STARTED, T0, kind=SourceKind.SYSTEM,
                         source="system"))
        for i in range(3):
            ingestor.feed(ev(EventType.FACE_OBSERVED, T0 + i * 100, "gp-1",
                             {"candidate_id": "EMP-1", "score": 0.85,
                              "margin": 0.4, "quality": 0.9}))
        ingestor.feed(ev(EventType.TRACK_UPDATED, T0 + 5_000, "gp-1",
                         {"zone_id": "floor-3-open", "zone_kind": "FLOOR",
                          "camera_id": "cam-7"}))
        roster = roster_of(("EMP-1", "A", "north", "Eng", True))
        row = build_board(ingestor.state, roster, now_ms=T0 + 600_000).rows[0]
        assert row.last_zone_id == "floor-3-open"
        assert row.last_camera_id == "cam-7"
        assert "floor-3-open" in row.decision.reason


class TestAllClear:
    def test_it_requires_everyone(self, ingestor):
        confirmed_at_assembly(ingestor, "gp-1", "EMP-1")
        roster = roster_of(("EMP-1", "A", "north", "Eng", True),
                           ("EMP-2", "B", "north", "Eng", True))
        board = build_board(ingestor.state, roster, now_ms=T0 + 600_000)
        assert board.all_clear is False
        assert "1 of 2 people not accounted for" in board.blocking_all_clear()

    def test_everyone_accounted_and_healthy_is_all_clear(self, ingestor):
        confirmed_at_assembly(ingestor, "gp-1", "EMP-1")
        roster = roster_of(("EMP-1", "A", "north", "Eng", True))
        board = build_board(ingestor.state, roster, now_ms=T0 + 60_000)
        assert board.all_clear is True
        assert board.blocking_all_clear() == []

    def test_an_open_outage_blocks_it_however_good_the_counts_look(self, ingestor):
        # The single failure invariant 8 exists to prevent. A blind system
        # cannot declare an all-clear, whatever its numbers say.
        confirmed_at_assembly(ingestor, "gp-1", "EMP-1")
        ingestor.feed(ev(EventType.CAMERA_FAILURE, T0 + 30_000, "cam-3",
                         {"reason": "offline"}, source="system",
                         kind=SourceKind.SYSTEM))
        roster = roster_of(("EMP-1", "A", "north", "Eng", True))
        board = build_board(ingestor.state, roster, now_ms=T0 + 60_000)
        assert board.accounted == 1
        assert board.all_clear is False
        assert any("outage" in r for r in board.blocking_all_clear())

    def test_an_unverifiable_roster_blocks_it(self, ingestor):
        confirmed_at_assembly(ingestor, "gp-1", "EMP-1")
        roster = roster_of(("EMP-1", "A", "north", "Eng", True))
        unverified = type(roster)(
            taken_at_ms=roster.taken_at_ms, entries=roster.entries,
            source_reachable=False, source_note="FaceTrack unreachable")
        board = build_board(ingestor.state, unverified, now_ms=T0 + 60_000)
        assert board.all_clear is False
        assert any("roster" in r for r in board.blocking_all_clear())

    def test_an_empty_roster_is_not_an_all_clear(self, ingestor):
        board = build_board(ingestor.state, roster_of(), now_ms=T0 + 60_000)
        assert board.all_clear is False
        assert "no expected people on the roster" in board.blocking_all_clear()


class TestPriorityList:
    def test_the_worst_come_first(self, ingestor):
        ingestor.feed(ev(EventType.DRILL_STARTED, T0, kind=SourceKind.SYSTEM,
                         source="system"))
        confirmed_at_assembly(ingestor, "gp-1", "EMP-1")
        # EMP-2 seen inside and then lost; EMP-3 never seen at all.
        for i in range(3):
            ingestor.feed(ev(EventType.FACE_OBSERVED, T0 + i * 100, "gp-2",
                             {"candidate_id": "EMP-2", "score": 0.85,
                              "margin": 0.4, "quality": 0.9}))
        ingestor.feed(ev(EventType.TRACK_UPDATED, T0 + 1_000, "gp-2",
                         {"zone_id": "floor-2-open", "zone_kind": "FLOOR",
                          "camera_id": "cam-2"}))
        roster = roster_of(("EMP-1", "A", "north", "Eng", True),
                           ("EMP-2", "B", "north", "Eng", True),
                           ("EMP-3", "C", "north", "Eng", True))
        board = build_board(ingestor.state, roster, now_ms=T0 + 600_000)
        priority = board.priority()
        assert priority
        assert all(row.state is not AccountabilityState.ACCOUNTED
                   for row in priority)
        assert priority[0].state is AccountabilityState.UNACCOUNTED

    def test_it_excludes_people_already_safe(self, ingestor):
        confirmed_at_assembly(ingestor, "gp-1", "EMP-1")
        roster = roster_of(("EMP-1", "A", "north", "Eng", True))
        assert build_board(ingestor.state, roster, now_ms=T0 + 60_000).priority() == ()

    def test_it_can_be_capped_for_a_screen(self, ingestor):
        ingestor.feed(ev(EventType.DRILL_STARTED, T0, kind=SourceKind.SYSTEM,
                         source="system"))
        roster = roster_of(*[(f"EMP-{i}", f"P{i}", "north", "Eng", True)
                             for i in range(20)])
        board = build_board(ingestor.state, roster, now_ms=T0 + 600_000)
        assert len(board.priority(limit=5)) == 5


class TestPanels:
    def test_people_group_by_assembly_zone(self, ingestor):
        roster = roster_of(("EMP-1", "A", "north", "Eng", True),
                           ("EMP-2", "B", "south", "HR", True))
        grouped = build_board(ingestor.state, roster,
                              now_ms=T0 + 60_000).by_assembly_zone()
        assert set(grouped) == {"north", "south"}

    def test_people_group_by_department(self, ingestor):
        roster = roster_of(("EMP-1", "A", "north", "Engineering", True),
                           ("EMP-2", "B", "north", "Finance", True))
        grouped = build_board(ingestor.state, roster,
                              now_ms=T0 + 60_000).by_department()
        assert set(grouped) == {"Engineering", "Finance"}

    def test_people_the_cameras_cannot_settle_are_listed_separately(self, ingestor):
        # Surfaced so a warden knows who to walk to, rather than an operator
        # waiting for a match that can never arrive.
        roster = roster_of(("EMP-1", "Enrolled", "north", "Eng", True),
                           ("EMP-2", "Never enrolled", "north", "Eng", False))
        board = build_board(ingestor.state, roster, now_ms=T0 + 60_000)
        assert [r.person_ref for r in board.needing_a_warden()] == ["emp:EMP-2"]


class TestWardenEvidenceReachesTheBoard:
    def test_a_warden_confirmation_accounts_for_someone(self, ingestor):
        ingestor.feed(ev(EventType.DRILL_STARTED, T0, kind=SourceKind.SYSTEM,
                         source="system"))
        roster = roster_of(("EMP-1", "A", "north", "Eng", True))
        board = build_board(
            ingestor.state, roster, now_ms=T0 + 600_000,
            warden_evidence={"emp:EMP-1": WardenEvidence(
                confirmed=True, at_assembly_zone="north", warden_id="warden-7")})
        assert board.accounted == 1
        assert "warden-7" in board.rows[0].decision.reason

    def test_a_warden_outranks_a_blind_system(self, ingestor):
        ingestor.feed(ev(EventType.DRILL_STARTED, T0, kind=SourceKind.SYSTEM,
                         source="system"))
        ingestor.feed(ev(EventType.CAMERA_FAILURE, T0, "*", {"reason": "all dark"},
                         source="system", kind=SourceKind.SYSTEM))
        roster = roster_of(("EMP-1", "A", "north", "Eng", True))
        board = build_board(
            ingestor.state, roster, now_ms=T0 + 600_000,
            warden_evidence={"emp:EMP-1": WardenEvidence(
                confirmed=True, at_assembly_zone="north", warden_id="warden-7")})
        assert board.rows[0].state is AccountabilityState.ACCOUNTED
        # The person is safe. The system still is not healthy, and says so.
        assert board.all_clear is False


class TestAWardenRulingOnSomethingTheCamerasNeverSaw:
    """The warden tablet acts on roster rows, so its subject is a person
    reference and not a track id.

    The ingest used to hand that string to `IdentityRegistry.get`, which
    creates on miss, so a warden confirming somebody invented an identity
    record for a person the drill had never observed. The rejected identity
    stored on it then matched no roster key, so the one thing the record was
    for reached nobody. The ruling belongs in the warden's own state, which is
    what accountability reads, and that is where it stays.
    """

    def test_it_invents_no_identity_record(self):
        from app.core.events import Event, EventType, SourceKind
        from app.ingest.ingestor import Ingestor

        ingestor = Ingestor()
        ingestor.feed_batch([
            Event(tenant_id="t", site_id="s", drill_id="d", source="tablet-1",
                  source_kind=SourceKind.WARDEN, seq=1,
                  type=EventType.WARDEN_CONFIRMED, ts_ms=1,
                  subject="emp:EMP-0001",
                  payload={"identity": "EMP-0001", "warden_id": "w"}),
            Event(tenant_id="t", site_id="s", drill_id="d", source="tablet-1",
                  source_kind=SourceKind.WARDEN, seq=2,
                  type=EventType.WARDEN_REJECTED, ts_ms=2,
                  subject="emp:EMP-0002",
                  payload={"identity": "EMP-0002", "warden_id": "w"}),
        ])
        assert [p.person_id for p in ingestor.state.identity] == []

    def test_the_evidence_is_still_recorded(self):
        # Not inventing the record must not mean losing the ruling.
        from app.core.events import Event, EventType, SourceKind
        from app.ingest.ingestor import Ingestor

        ingestor = Ingestor()
        ingestor.feed_batch([
            Event(tenant_id="t", site_id="s", drill_id="d", source="tablet-1",
                  source_kind=SourceKind.WARDEN, seq=1,
                  type=EventType.WARDEN_REJECTED, ts_ms=2,
                  subject="emp:EMP-0002",
                  payload={"identity": "EMP-0002", "warden_id": "w"}),
        ])
        assert ingestor.state.ledger.for_subject("emp:EMP-0002")

    def test_a_ruling_on_a_real_track_still_lands(self):
        from app.core.events import Event, EventType, SourceKind
        from app.core.identity_fsm import IdentityState
        from app.ingest.ingestor import Ingestor

        ingestor = Ingestor()
        ingestor.state.identity.get("gp-1")
        ingestor.feed_batch([
            Event(tenant_id="t", site_id="s", drill_id="d", source="tablet-1",
                  source_kind=SourceKind.WARDEN, seq=1,
                  type=EventType.WARDEN_REJECTED, ts_ms=2, subject="gp-1",
                  payload={"identity": "EMP-0002", "warden_id": "w"}),
        ])
        person = ingestor.state.identity.find("gp-1")
        assert person.state is IdentityState.REJECTED
        assert "EMP-0002" in person.rejected_identities
