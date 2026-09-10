"""Roster: who is expected, and who is merely present."""

import pytest

from app.core.roster import (
    ExpectationReason,
    Population,
    Roster,
    from_facetrack,
)

T0 = 1_788_000_000_000


def staffed_roster() -> Roster:
    roster = Roster()
    roster.add_employee(emp_id="EMP-1", display_name="Ayesha Khan",
                        has_gallery_entry=True, department="Engineering",
                        home_floor_id="floor-2", assigned_assembly_zone="north")
    roster.add_employee(emp_id="EMP-2", display_name="Bilal Ahmed",
                        has_gallery_entry=True, department="Finance",
                        home_floor_id="floor-3", assigned_assembly_zone="south")
    roster.add_employee(emp_id="EMP-3", display_name="Chen Wei",
                        has_gallery_entry=False, department="Operations",
                        home_floor_id="floor-1", assigned_assembly_zone="north")
    roster.add_employee(emp_id="EMP-4", display_name="Dania Rauf",
                        has_gallery_entry=True, reason=ExpectationReason.ON_LEAVE,
                        department="HR", assigned_assembly_zone="north")
    return roster


class TestExpectation:
    def test_only_people_on_site_are_expected(self):
        snapshot = staffed_roster().snapshot(T0)
        assert snapshot.expected_count == 3
        assert {e.emp_id for e in snapshot.expected} == {"EMP-1", "EMP-2", "EMP-3"}

    def test_someone_on_leave_is_on_the_roster_but_not_expected(self):
        # They must not become a phantom missing person, and they must not
        # vanish from the record either.
        snapshot = staffed_roster().snapshot(T0)
        on_leave = snapshot.by_emp_id("EMP-4")
        assert on_leave is not None
        assert on_leave.is_expected is False

    def test_a_signed_in_visitor_is_expected(self):
        roster = staffed_roster()
        roster.sign_in(visitor_ref="V-1", display_name="Site auditor", at_ms=T0)
        assert roster.snapshot(T0).expected_count == 4

    def test_signing_out_removes_someone_from_the_expected_set(self):
        roster = staffed_roster()
        roster.sign_in(visitor_ref="V-1", display_name="Site auditor", at_ms=T0)
        roster.sign_out("V-1", T0 + 3_600_000)
        snapshot = roster.snapshot(T0 + 3_600_000)
        assert snapshot.expected_count == 3
        assert snapshot.by_ref("visitor:V-1").reason is ExpectationReason.SIGNED_OUT


class TestPopulationsStayApart:
    def test_a_visitor_never_gets_a_gallery_entry(self):
        # Invariant 5. A visitor who resembles an employee stays a visitor.
        roster = Roster()
        entry = roster.sign_in(visitor_ref="V-1", display_name="Guest", at_ms=T0)
        assert entry.has_gallery_entry is False
        assert entry.is_identifiable_by_face is False

    def test_sign_in_refuses_to_create_an_employee(self):
        roster = Roster()
        with pytest.raises(ValueError, match="visitors and contractors"):
            roster.sign_in(visitor_ref="V-1", display_name="Guest", at_ms=T0,
                           population=Population.EMPLOYEE)

    def test_an_unknown_person_is_recorded_not_reconciled(self):
        roster = staffed_roster()
        roster.observe_unknown("gp-77")
        assert roster.unknown_people == {"gp-77": None}
        # They do not appear in the roster and are not matched to anyone.
        assert roster.snapshot(T0).by_ref("gp-77") is None

    def test_a_human_can_tag_an_unknown_person(self):
        roster = staffed_roster()
        roster.observe_unknown("gp-77", tag="contractor")
        assert roster.unknown_people["gp-77"] == "contractor"

    def test_contractors_are_their_own_population(self):
        roster = Roster()
        roster.sign_in(visitor_ref="C-1", display_name="Lift engineer", at_ms=T0,
                       population=Population.CONTRACTOR)
        snapshot = roster.snapshot(T0)
        assert len(snapshot.in_population(Population.CONTRACTOR)) == 1
        assert len(snapshot.in_population(Population.VISITOR)) == 0


class TestPeopleTheCamerasCannotSettle:
    def test_an_employee_without_a_gallery_entry_needs_a_human(self):
        # Surfaced at drill start, not discovered at minute four while an
        # operator waits for a match that can never arrive.
        snapshot = staffed_roster().snapshot(T0)
        assert snapshot.by_emp_id("EMP-3").needs_human_to_account is True
        assert snapshot.by_emp_id("EMP-1").needs_human_to_account is False

    def test_mobility_assistance_needs_a_human_too(self):
        roster = Roster()
        roster.add_employee(emp_id="EMP-9", display_name="Farid Sheikh",
                            has_gallery_entry=True, mobility_assistance=True)
        assert roster.snapshot(T0).needing_human()[0].emp_id == "EMP-9"

    def test_visitors_always_need_a_human(self):
        roster = Roster()
        roster.sign_in(visitor_ref="V-1", display_name="Guest", at_ms=T0)
        assert roster.snapshot(T0).needing_human()[0].display_name == "Guest"


class TestWardenLists:
    def test_people_group_by_the_zone_their_warden_covers(self):
        grouped = staffed_roster().snapshot(T0).by_assembly_zone()
        assert set(grouped) == {"north", "south"}
        assert {e.emp_id for e in grouped["north"]} == {"EMP-1", "EMP-3"}

    def test_a_warden_list_excludes_people_known_to_be_on_leave(self):
        # Handing a warden someone on annual leave sends them searching for a
        # person who was never in the building.
        grouped = staffed_roster().snapshot(T0).by_assembly_zone()
        assert "EMP-4" not in {e.emp_id for e in grouped["north"]}

    def test_people_with_no_zone_land_in_unassigned(self):
        roster = Roster()
        roster.add_employee(emp_id="EMP-9", display_name="Unassigned",
                            has_gallery_entry=True)
        assert "unassigned" in roster.snapshot(T0).by_assembly_zone()


class TestSnapshotIntegrity:
    def test_a_snapshot_does_not_move_when_the_roster_does(self):
        # A person who "disappeared" because HR updated a record looks exactly
        # like one who disappeared in a stairwell.
        roster = staffed_roster()
        snapshot = roster.snapshot(T0)
        roster.add_employee(emp_id="EMP-99", display_name="Late arrival",
                            has_gallery_entry=True)
        assert snapshot.expected_count == 3
        assert roster.snapshot(T0 + 1_000).expected_count == 4

    def test_an_unreachable_source_is_marked_untrustworthy(self):
        snapshot = staffed_roster().snapshot(
            T0, source_reachable=False, source_note="FaceTrack unreachable")
        assert snapshot.is_trustworthy is False
        assert snapshot.expected_count == 3  # the drill still runs

    def test_an_empty_roster_is_not_trustworthy(self):
        assert Roster().snapshot(T0).is_trustworthy is False


class TestLocallyOwnedFields:
    def test_missing_local_fields_are_counted(self):
        roster = Roster()
        roster.add_employee(emp_id="EMP-1", display_name="No metadata",
                            has_gallery_entry=True)
        gaps = roster.snapshot(T0).coverage_gaps()
        assert gaps["no_department"] == 1
        assert gaps["no_assembly_zone"] == 1
        assert gaps["no_home_floor"] == 1
        assert gaps["no_gallery_entry"] == 0

    def test_a_complete_roster_has_no_gaps(self):
        roster = Roster()
        roster.add_employee(emp_id="EMP-1", display_name="Complete",
                            has_gallery_entry=True, department="Engineering",
                            home_floor_id="floor-1", assigned_assembly_zone="north")
        assert set(roster.snapshot(T0).coverage_gaps().values()) == {0}


class TestFaceTrackImport:
    ROWS = [
        {"emp_id": "EMP-1", "first_name": "Ayesha", "last_name": "Khan",
         "photo": "/api/images/c/1.jpg", "present": True},
        {"emp_id": "EMP-2", "first_name": "Bilal", "last_name": "Ahmed",
         "photo": None, "present": True},
        {"emp_id": "EMP-3", "first_name": "Chen", "last_name": "Wei",
         "photo": "/api/images/c/3.jpg", "present": False},
    ]

    def test_names_are_joined_and_gallery_state_read_from_the_photo(self):
        snapshot = from_facetrack(self.ROWS).snapshot(T0)
        assert snapshot.by_emp_id("EMP-1").display_name == "Ayesha Khan"
        assert snapshot.by_emp_id("EMP-1").has_gallery_entry is True
        assert snapshot.by_emp_id("EMP-2").has_gallery_entry is False

    def test_attendance_drives_expectation_but_nothing_more(self):
        # FaceTrack's `present` means "badged in today". An attendance system is
        # not a life-safety system, so it sets the default and nothing else.
        snapshot = from_facetrack(self.ROWS).snapshot(T0)
        assert snapshot.expected_count == 2
        assert snapshot.by_emp_id("EMP-3").reason is ExpectationReason.ON_LEAVE

    def test_local_fields_are_merged_in_by_employee_id(self):
        local = {"EMP-1": {"department": "Engineering",
                           "assigned_assembly_zone": "north",
                           "home_floor_id": "floor-2"}}
        snapshot = from_facetrack(self.ROWS, local_fields=local).snapshot(T0)
        assert snapshot.by_emp_id("EMP-1").department == "Engineering"
        # Everyone else keeps the gap, and it is counted.
        assert snapshot.coverage_gaps()["no_department"] == 1

    def test_a_row_with_no_name_falls_back_to_the_employee_id(self):
        roster = from_facetrack([{"emp_id": "EMP-9", "present": True}])
        assert roster.snapshot(T0).by_emp_id("EMP-9").display_name == "EMP-9"
