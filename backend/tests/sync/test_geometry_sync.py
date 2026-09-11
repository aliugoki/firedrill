"""Importing VisionTrack geometry, and refusing to import it badly.

The shapes used here are the real ones, read from a live VisionTrack instance
rather than invented, including the detail that its only real zone is called
"Zone 1".
"""

from __future__ import annotations

import pytest

from dataclasses import replace

from app.core.presence_fsm import ZoneKind
from app.sync.geometry import sync
from app.sync.zone_kinds import Confidence, propose, review

REAL_HOMOGRAPHY = [
    -0.00012160351850530063, -0.0003091197624781208, 0.32745214834736747,
    -0.000494436238586845, 0.0002969340760542314, 0.37537582850366036,
    -0.0013370753136146406, 0.0007454414731469695, 1.0,
]

SQUARE = [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0},
          {"x": 1.0, "y": 1.0}, {"x": 0.0, "y": 1.0}]


def plan(zones, markers=None, plan_id="fp1"):
    return {"id": plan_id, "name": "Ground floor", "width_px": 1920,
            "height_px": 1080, "zones": zones, "markers": markers or []}


def zone(zone_id, name, polygon=None):
    return {"id": zone_id, "name": name, "color": "#ef4444", "rules": [],
            "polygon": polygon if polygon is not None else SQUARE}


def camera(camera_id="cam1", homography=None, floor_plan_id="fp1"):
    calibration = {}
    if homography is not None:
        calibration["homography"] = homography
    if floor_plan_id is not None:
        calibration["floor_plan_id"] = floor_plan_id
    return {"id": camera_id, "calibration": calibration}


READY_TAGS = {"z-floor": ZoneKind.FLOOR, "z-assembly": ZoneKind.ASSEMBLY}


def ready_site():
    return sync(
        floor_plans=[plan(
            [zone("z-floor", "Zone 1"), zone("z-assembly", "Zone 2")],
            [{"camera_id": "cam1", "x": 0.5, "y": 0.5}])],
        cameras=[camera(homography=REAL_HOMOGRAPHY)],
        site_id="s1", tagged_zones=READY_TAGS)


class TestInferenceProposesAndNeverDecides:
    def test_a_real_zone_name_says_nothing(self):
        # The live VisionTrack instance contains one zone, called "Zone 1".
        # This is the realistic case, not the edge case.
        proposal = propose("z1", "Zone 1")
        assert proposal.kind is None
        assert proposal.confidence is Confidence.UNKNOWN
        assert proposal.is_usable is False

    @pytest.mark.parametrize("name,expected", [
        ("North Car Park Assembly", ZoneKind.ASSEMBLY),
        ("Fire Exit West", ZoneKind.EXIT),
        ("Stairwell B", ZoneKind.BLIND),
        ("Level 2 open plan", ZoneKind.FLOOR),
    ])
    def test_an_obvious_name_is_proposed(self, name, expected):
        assert propose("z1", name).kind is expected

    def test_even_an_obvious_name_still_needs_confirming(self):
        # A proposal is a suggestion to whoever is tagging, never an answer.
        proposal = propose("z1", "North Car Park Assembly")
        assert proposal.confidence is Confidence.LIKELY
        assert proposal.needs_confirmation is True
        assert proposal.is_usable is False

    def test_a_human_tag_outranks_any_inference(self):
        # What a zone is called is a label somebody typed. What it is for is a
        # decision somebody made.
        proposal = propose("z1", "Fire Exit West",
                           {"z1": ZoneKind.ASSEMBLY})
        assert proposal.kind is ZoneKind.ASSEMBLY
        assert proposal.confidence is Confidence.CERTAIN
        assert proposal.is_usable is True

    def test_a_tag_given_as_a_string_is_accepted(self):
        assert propose("z1", "x", {"z1": "EXIT"}).kind is ZoneKind.EXIT

    def test_there_is_no_default_kind(self):
        # Defaulting to FLOOR means nobody is ever accounted for at the muster
        # point. Defaulting to ASSEMBLY marks everyone in a corridor safe.
        # There is no harmless default, so there is no default.
        assert propose("z1", "").kind is None
        assert propose("z1", "something meaningless").kind is None


class TestASiteIsNotReadyUntilItIsTagged:
    def test_untagged_zones_block(self):
        report = review([propose("z1", "Zone 1"), propose("z2", "Zone 2")])
        assert report.ready is False
        assert any("no confirmed kind" in b for b in report.blockers)

    def test_no_assembly_zone_blocks_even_when_everything_is_tagged(self):
        # Nobody could ever be accounted for, and the board would show a
        # building full of people it believes are still inside.
        report = review([
            propose("z1", "x", {"z1": ZoneKind.FLOOR}),
            propose("z2", "y", {"z2": ZoneKind.EXIT}),
        ])
        assert report.ready is False
        assert any("no zone is tagged ASSEMBLY" in b for b in report.blockers)

    def test_a_fully_tagged_site_with_an_assembly_zone_is_ready(self):
        report = review([
            propose("z1", "x", {"z1": ZoneKind.FLOOR}),
            propose("z2", "y", {"z2": ZoneKind.ASSEMBLY}),
        ])
        assert report.ready is True
        assert report.blockers == ()

    def test_the_report_lists_what_still_needs_confirming(self):
        report = review([
            propose("z1", "North Car Park Assembly"),
            propose("z2", "x", {"z2": ZoneKind.FLOOR}),
        ])
        assert len(report.needing_confirmation) == 1


class TestImportingGeometry:
    def test_a_tagged_zone_is_imported_with_its_polygon(self):
        result = ready_site()
        assert len(result.zones) == 2
        assert result.zones[0].polygon[0] == (0.0, 0.0)

    def test_an_untagged_zone_is_not_imported(self):
        # It has no kind, so nothing downstream could use it correctly.
        result = sync(floor_plans=[plan([zone("z1", "Zone 1")])],
                      cameras=[camera(homography=REAL_HOMOGRAPHY)],
                      site_id="s1")
        assert result.zones == ()

    def test_a_half_drawn_polygon_is_refused(self):
        # Not a small zone. A shape with no inside.
        result = sync(
            floor_plans=[plan([zone("z1", "half", [{"x": 0, "y": 0},
                                                    {"x": 1, "y": 0}])])],
            cameras=[camera(homography=REAL_HOMOGRAPHY)],
            site_id="s1", tagged_zones={"z1": ZoneKind.FLOOR})
        assert result.zones == ()
        assert any("at least 3" in p.detail for p in result.problems)

    def test_a_real_homography_is_imported(self):
        result = ready_site()
        assert result.cameras[0].homography == tuple(REAL_HOMOGRAPHY)
        assert result.cameras[0].can_resolve_zones is True

    def test_a_half_saved_homography_is_refused(self):
        # It would project every person to the same place, and that place is
        # inside whichever zone contains it.
        result = sync(floor_plans=[plan([])],
                      cameras=[camera(homography=[1, 0, 0])], site_id="s1")
        assert result.cameras[0].homography is None
        assert any("whole building in one room" in p.detail
                   for p in result.problems)

    def test_a_non_numeric_homography_is_refused(self):
        result = sync(floor_plans=[plan([])],
                      cameras=[camera(homography=["a"] * 9)], site_id="s1")
        assert result.cameras[0].homography is None

    def test_a_camera_with_no_geometry_is_imported_and_flagged(self):
        # Losing a camera silently is worse than importing one that only
        # contributes presence.
        result = sync(floor_plans=[plan([])],
                      cameras=[camera("cam2", homography=None,
                                      floor_plan_id=None)],
                      site_id="s1")
        assert len(result.cameras) == 1
        assert result.cameras[0].can_resolve_zones is False
        assert any("cannot place them in a zone" in p.detail
                   for p in result.problems)

    def test_a_camera_is_placed_by_its_marker_when_calibration_omits_the_plan(self):
        result = sync(
            floor_plans=[plan([], [{"camera_id": "cam1", "x": 0.5, "y": 0.5}])],
            cameras=[{"id": "cam1",
                      "calibration": {"homography": REAL_HOMOGRAPHY}}],
            site_id="s1")
        assert result.cameras[0].floor_plan_id == "fp1"
        assert result.cameras[0].marker == (0.5, 0.5)


class TestReadyForADrill:
    def test_a_complete_site_is_ready(self):
        result = ready_site()
        assert result.ready_for_a_drill is True
        assert result.blocking() == []

    def test_untagged_zones_stop_a_drill(self):
        result = sync(
            floor_plans=[plan([zone("z1", "Zone 1")])],
            cameras=[camera(homography=REAL_HOMOGRAPHY)], site_id="s1")
        assert result.ready_for_a_drill is False
        assert result.blocking()

    def test_a_site_with_no_calibrated_camera_stops_a_drill(self):
        # Nothing can place a person in a zone, so nobody can reach an assembly
        # point, so the drill would report a building nobody ever left.
        result = sync(
            floor_plans=[plan([zone("z-assembly", "Assembly")])],
            cameras=[camera(homography=None, floor_plan_id=None)],
            site_id="s1", tagged_zones={"z-assembly": ZoneKind.ASSEMBLY})
        assert result.ready_for_a_drill is False
        assert any("nobody can reach an assembly point" in b
                   for b in result.blocking())

    def test_a_site_with_no_cameras_at_all_stops_a_drill(self):
        result = sync(
            floor_plans=[plan([zone("z-assembly", "Assembly")])],
            cameras=[], site_id="s1",
            tagged_zones={"z-assembly": ZoneKind.ASSEMBLY})
        assert result.ready_for_a_drill is False
        assert "no cameras were imported" in result.blocking()

    def test_blocking_is_never_empty_when_it_is_not_ready(self):
        result = sync(floor_plans=[], cameras=[], site_id="s1")
        assert result.ready_for_a_drill is False
        assert result.blocking()

    def test_the_description_says_ready_or_why_not(self):
        text = "\n".join(ready_site().describe())
        assert "READY" in text
        blocked = "\n".join(sync(floor_plans=[], cameras=[],
                                 site_id="s1").describe())
        assert "NOT READY" in blocked


class TestTheSyncedGeometryWorksWithTheVendoredCode:
    def test_an_imported_polygon_can_be_tested_for_containment(self):
        # The whole point of syncing rather than redrawing: the coordinates
        # feed the vendored geometry directly, with no rescaling.
        from app.vendor.visiontrack.zones_math import point_in_polygon

        result = ready_site()
        polygon = list(result.zones[0].polygon)
        assert point_in_polygon((0.5, 0.5), polygon) is True
        assert point_in_polygon((1.5, 0.5), polygon) is False

    def test_an_imported_homography_projects_a_bounding_box(self):
        from app.vendor.visiontrack.zone_resolve import project_foot

        result = ready_site()
        point = project_foot(list(result.cameras[0].homography),
                             [100.0, 200.0, 300.0, 600.0])
        assert point is not None
        assert all(isinstance(value, float) for value in point)


class TestTwoReadingsOfEqualStrength:
    """"Exit Stairwell" is both, and it is a stairwell.

    The order of `_PATTERNS` used to decide it, and the reason shown to whoever
    confirms said only the half that won -- so the contradicting half never
    reached the person deciding. A stairwell tagged EXIT gets the 15-second
    grace window instead of the 45 an uncovered stairwell is owed, and people
    walking down it go LOST three times sooner.
    """

    @pytest.mark.parametrize("name", [
        "Exit Stairwell", "Fire Exit Stair", "Emergency Exit Lift",
    ])
    def test_it_refuses_to_choose(self, name):
        proposal = propose("z-1", name)
        assert proposal.kind is None
        assert proposal.confidence is Confidence.UNKNOWN

    def test_the_reason_names_both_readings(self, name="Exit Stairwell"):
        because = propose("z-1", name).because
        assert '"exit"' in because and '"stairwell"' in because
        assert "EXIT" in because and "BLIND" in because

    def test_a_weaker_second_reading_does_not_block_a_stronger_one(self):
        # ASSEMBLY is LIKELY on "assembly"; FLOOR is only GUESSED on "corridor".
        proposal = propose("z-1", "Assembly Corridor")
        assert proposal.kind is ZoneKind.ASSEMBLY
        assert proposal.confidence is Confidence.LIKELY

    def test_two_patterns_for_one_kind_are_not_an_ambiguity(self):
        proposal = propose("z-1", "Office Corridor")
        assert proposal.kind is ZoneKind.FLOOR

    def test_a_human_tag_still_wins_over_an_ambiguous_name(self):
        # The whole point of tagging: what a zone is for is a decision somebody
        # made, not a reading of the label.
        proposal = propose("z-1", "Exit Stairwell", tagged={"z-1": "BLIND"})
        assert proposal.kind is ZoneKind.BLIND
        assert proposal.confidence is Confidence.CERTAIN

    def test_an_ambiguous_zone_blocks_the_site(self):
        report = review([
            propose("z-1", "Exit Stairwell"),
            propose("z-2", "North Car Park Assembly"),
        ])
        assert report.ready is False
        assert any("no confirmed kind" in b for b in report.blockers)


class TestATagThisSystemDoesNotKnow:

    def test_it_is_a_blocker_rather_than_a_crash(self):
        # A typo in the site's own tag file used to raise out of `propose` and
        # stop the sync. It is one line for somebody to fix, once they are told.
        proposal = propose("z-1", "North Car Park", tagged={"z-1": "MUSTER_POINT"})
        assert proposal.kind is None
        assert "MUSTER_POINT" in proposal.because
        assert "not a kind this system has" in proposal.because

    def test_the_site_is_reported_as_not_ready(self):
        report = review([propose("z-1", "North Car Park",
                                 tagged={"z-1": "MUSTER_POINT"})])
        assert report.ready is False


class TestWhatAnOperatorReads:
    """The startup text, which nothing exercised."""

    def test_an_untagged_zone_says_a_drill_cannot_run(self):
        assert "A drill cannot run" in propose("z-1", "Zone 1").describe()

    def test_a_tagged_zone_says_what_it_is_and_why(self):
        line = propose("z-1", "North Car Park Assembly").describe()
        assert "ASSEMBLY" in line and "likely" in line and "assembly" in line

    def test_a_ready_report_says_so_in_its_first_line(self):
        report = review([propose("z-1", "Muster", tagged={"z-1": "ASSEMBLY"})])
        assert report.describe()[0] == "Zone tagging"

    def test_an_unready_report_says_so_in_its_first_line(self):
        report = review([propose("z-1", "Zone 1")])
        assert report.describe()[0].endswith("NOT READY")

    def test_every_zone_appears_with_a_usable_marker(self):
        report = review([
            propose("z-1", "Muster", tagged={"z-1": "ASSEMBLY"}),
            propose("z-2", "Zone 2"),
        ])
        lines = report.describe()
        assert any(line.startswith("  ok ") for line in lines)
        assert any(line.startswith("  ?? ") for line in lines)

    def test_the_blockers_are_listed_under_a_heading(self):
        lines = review([propose("z-1", "Zone 1")]).describe()
        assert "Blocking:" in lines
        assert any("no zone is tagged ASSEMBLY" in line for line in lines)


class TestWhatTheSyncTellsAnOperator:
    """The startup text and the readiness rule, which had no test.

    A node prints this once at boot. It is the only place somebody learns that
    a camera was imported without geometry, or that a zone was skipped.
    """

    def test_problems_are_listed_with_their_severity(self):
        result = sync(
            floor_plans=[plan([zone("z-assembly", "Zone 1")],
                              [{"camera_id": "cam1", "x": 0.5, "y": 0.5}])],
            cameras=[camera(homography=[1.0, 2.0])],
            site_id="s1", tagged_zones={"z-assembly": ZoneKind.ASSEMBLY})

        lines = result.describe()
        assert "Problems:" in lines
        assert any(line.startswith("  warn  ") for line in lines)
        assert any("expected 9" in line for line in lines)

    def test_a_fatal_problem_blocks_a_drill_and_is_marked(self):
        """Nothing in `sync` raises one today -- every geometry problem it can
        find is recoverable -- but `blocking` and `ready_for_a_drill` consult
        the flag, so the branch is exercised rather than merely present."""
        from app.sync.geometry import SyncProblem

        result = ready_site()
        assert result.ready_for_a_drill is True

        broken = replace(result, problems=result.problems + (
            SyncProblem(kind="plan", subject="fp1",
                        detail="the floor plan has no dimensions", fatal=True),))
        assert broken.ready_for_a_drill is False
        assert any("no dimensions" in reason for reason in broken.blocking())
        assert any(line.startswith("  FATAL ") for line in broken.describe())

    def test_a_marker_without_a_camera_is_skipped_rather_than_crashing(self):
        # VisionTrack's table allows it, so the importer has to.
        result = sync(
            floor_plans=[plan([zone("z-assembly", "Zone 1")],
                              [{"x": 0.5, "y": 0.5},
                               {"camera_id": "cam1", "x": 0.5, "y": 0.5}])],
            cameras=[camera(homography=REAL_HOMOGRAPHY)],
            site_id="s1", tagged_zones={"z-assembly": ZoneKind.ASSEMBLY})

        assert [c.camera_id for c in result.cameras] == ["cam1"]
        assert result.cameras[0].marker == (0.5, 0.5)
