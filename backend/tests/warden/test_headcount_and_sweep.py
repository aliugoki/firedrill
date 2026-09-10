"""Headcount asymmetry, and what completing a sweep does and does not mean."""

from __future__ import annotations

import pytest

from app.warden.headcount import (
    DEFAULT_POLICY,
    Headcount,
    HeadcountPolicy,
    MismatchKind,
    Severity,
    ZoneHeadcounts,
)
from app.warden.sweep import SweepState, SweepStatus

T0 = 1_788_000_000_000


def count(physical: int, system: int, policy=DEFAULT_POLICY, zone="assembly-north"):
    return Headcount(zone_id=zone, warden_id="warden-7", device_id="tablet-3",
                     ts_ms=T0, physical_count=physical, system_count=system,
                     policy=policy)


class TestTheTwoDirectionsAreNotTheSame:
    """Treating them symmetrically would let the dangerous direction hide
    inside a tolerance chosen to accommodate the harmless one."""

    def test_agreement_is_no_mismatch(self):
        c = count(40, 40)
        assert c.kind is MismatchKind.MATCH
        assert c.severity is Severity.NONE
        assert c.is_mismatch is False

    def test_the_system_counting_more_escalates_immediately(self):
        # The system believes one more person is safe than is standing there.
        # That is the error that kills, and it cannot be "within tolerance".
        c = count(39, 40)
        assert c.kind is MismatchKind.SYSTEM_OVERCOUNTED
        assert c.severity is Severity.ESCALATE
        assert c.missing_from_the_muster_point == 1

    def test_the_warden_counting_more_only_asks_for_investigation(self):
        # Unbadged visitors, contractors, a neighbouring building's staff.
        # Two extra is inside the default tolerance; four is not, and even then
        # it only asks someone to look rather than blocking the drill.
        inside = count(42, 40)
        assert inside.kind is MismatchKind.SYSTEM_UNDERCOUNTED
        assert inside.severity is Severity.NONE

        beyond = count(44, 40)
        assert beyond.kind is MismatchKind.SYSTEM_UNDERCOUNTED
        assert beyond.severity is Severity.INVESTIGATE
        assert beyond.missing_from_the_muster_point == 0

    def test_the_same_absolute_difference_has_opposite_severity(self):
        # The point of the whole module, in one assertion.
        assert count(38, 40).severity is Severity.ESCALATE
        assert count(42, 40).severity is Severity.NONE

    def test_a_small_undercount_is_inside_tolerance(self):
        assert count(38, 40, HeadcountPolicy(undercount_tolerance=0)).severity \
            is Severity.ESCALATE  # this is an overcount by the system
        assert count(42, 40, HeadcountPolicy(undercount_tolerance=2)).severity \
            is Severity.NONE
        assert count(43, 40, HeadcountPolicy(undercount_tolerance=2)).severity \
            is Severity.INVESTIGATE

    def test_the_dangerous_tolerance_defaults_to_zero(self):
        assert DEFAULT_POLICY.overcount_tolerance == 0
        assert DEFAULT_POLICY.calibrated is False

    def test_negative_counts_are_refused(self):
        with pytest.raises(ValueError):
            count(-1, 40)
        with pytest.raises(ValueError):
            HeadcountPolicy(undercount_tolerance=-1)


class TestWhatTheWardenIsTold:
    def test_an_escalation_says_do_not_declare_all_clear(self):
        assert "Do not declare all clear" in count(38, 40).recommended_action()

    def test_an_escalation_names_the_number_of_people(self):
        summary = count(37, 40).summary()
        assert "3 person(s)" in summary
        assert "not at the muster point" in summary

    def test_an_investigation_asks_for_tagging(self):
        assert "visitors or contractors" in count(43, 40).recommended_action()

    def test_a_match_asks_for_nothing(self):
        assert "No action" in count(40, 40).recommended_action()


class TestASeriesOfCounts:
    def test_counts_are_kept_as_a_series_not_a_latest_value(self):
        # A warden who counts 38, then 40, then 38 is telling you something a
        # single number cannot.
        zone = ZoneHeadcounts("assembly-north")
        for physical in (38, 40, 38):
            zone.record(count(physical, 40))
        assert len(zone.counts) == 3
        assert zone.unstable() is True

    def test_a_resolved_mismatch_still_shows_in_the_history(self):
        # A post-drill report must not show only the reassuring final number.
        zone = ZoneHeadcounts("assembly-north")
        zone.record(count(38, 40))
        zone.record(count(40, 40))
        assert zone.is_settled is True
        assert zone.ever_escalated is True

    def test_a_count_for_the_wrong_zone_is_refused(self):
        zone = ZoneHeadcounts("assembly-north")
        with pytest.raises(ValueError, match="assembly-south"):
            zone.record(count(40, 40, zone="assembly-south"))


class TestSweepCompletionIsNotAnAllClear:
    """"The warden has finished looking" and "everyone here is safe" are
    different facts. Conflating them is how a zone gets signed off with someone
    still missing."""

    EXPECTED = {"emp:EMP-1", "emp:EMP-2", "emp:EMP-3"}

    def _swept(self) -> SweepState:
        sweep = SweepState(zone_id="assembly-north")
        for person in self.EXPECTED:
            sweep.confirm(person, "warden-7", T0)
        sweep.record_headcount(count(3, 3))
        sweep.complete("warden-7", T0 + 60_000)
        return sweep

    def test_a_finished_agreeing_sweep_is_clean(self):
        assert self._swept().is_clean(self.EXPECTED) is True

    def test_completion_is_always_accepted_even_when_the_system_disagrees(self):
        # Refusing it would be the software overruling the human it has
        # designated the final authority.
        sweep = SweepState(zone_id="assembly-north")
        sweep.record_headcount(count(1, 3))
        sweep.complete("warden-7", T0 + 60_000)
        assert sweep.is_complete is True

    def test_but_a_disagreeing_sweep_is_not_clean(self):
        sweep = SweepState(zone_id="assembly-north")
        for person in self.EXPECTED:
            sweep.confirm(person, "warden-7", T0)
        sweep.record_headcount(count(1, 3))
        sweep.complete("warden-7", T0 + 60_000)
        assert sweep.is_complete is True
        assert sweep.is_clean(self.EXPECTED) is False
        assert any("not at the muster point" in r
                   for r in sweep.blocking_clean(self.EXPECTED))

    def test_unresolved_people_block_clean(self):
        sweep = SweepState(zone_id="assembly-north")
        sweep.confirm("emp:EMP-1", "warden-7", T0)
        sweep.record_headcount(count(1, 1))
        sweep.complete("warden-7", T0 + 60_000)
        assert sweep.is_clean(self.EXPECTED) is False
        assert any("have not been confirmed" in r
                   for r in sweep.blocking_clean(self.EXPECTED))

    def test_no_headcount_at_all_blocks_clean(self):
        # Walking the zone and ticking everyone off the system's own list is
        # checking the system against itself. The physical count is the
        # independent measurement, and without it there is nothing to compare.
        sweep = SweepState(zone_id="assembly-north")
        for person in self.EXPECTED:
            sweep.confirm(person, "warden-7", T0)
        sweep.complete("warden-7", T0 + 60_000)
        assert sweep.is_complete is True
        assert sweep.is_clean(self.EXPECTED) is False
        assert any("no physical headcount" in r
                   for r in sweep.blocking_clean(self.EXPECTED))

    def test_blocking_is_never_empty_when_it_is_not_clean(self):
        sweep = SweepState(zone_id="assembly-north")
        assert sweep.is_clean(self.EXPECTED) is False
        assert sweep.blocking_clean(self.EXPECTED)


class TestSweepProgress:
    EXPECTED = {"emp:EMP-1", "emp:EMP-2", "emp:EMP-3"}

    def test_a_sweep_starts_on_the_first_action(self):
        sweep = SweepState(zone_id="assembly-north")
        assert sweep.status is SweepStatus.NOT_STARTED
        sweep.confirm("emp:EMP-1", "warden-7", T0)
        assert sweep.status is SweepStatus.IN_PROGRESS
        assert sweep.started_ms == T0

    def test_outstanding_shrinks_as_people_are_resolved(self):
        sweep = SweepState(zone_id="assembly-north")
        assert len(sweep.outstanding(self.EXPECTED)) == 3
        sweep.confirm("emp:EMP-1", "warden-7", T0)
        sweep.report_not_here("emp:EMP-2", "warden-7", T0)
        sweep.mark_absent("emp:EMP-3", "warden-7", T0)
        assert sweep.outstanding(self.EXPECTED) == set()

    def test_a_warden_can_change_their_mind(self):
        sweep = SweepState(zone_id="assembly-north")
        sweep.report_not_here("emp:EMP-1", "warden-7", T0)
        sweep.confirm("emp:EMP-1", "warden-7", T0 + 1_000)
        assert "emp:EMP-1" in sweep.confirmed
        assert "emp:EMP-1" not in sweep.not_here

    def test_escalation_outranks_completion_in_both_directions(self):
        sweep = SweepState(zone_id="assembly-north")
        sweep.complete("warden-7", T0)
        sweep.escalate("cannot access the stairwell", "warden-7", T0 + 1_000)
        assert sweep.status is SweepStatus.ESCALATED
        sweep.complete("warden-7", T0 + 2_000)
        assert sweep.status is SweepStatus.ESCALATED

    def test_an_escalated_sweep_is_never_clean(self):
        sweep = SweepState(zone_id="assembly-north")
        for person in self.EXPECTED:
            sweep.confirm(person, "warden-7", T0)
        sweep.record_headcount(count(3, 3))
        sweep.escalate("smoke in the west stairwell", "warden-7", T0 + 1_000)
        assert sweep.is_clean(self.EXPECTED) is False
        assert any("smoke in the west stairwell" in r
                   for r in sweep.blocking_clean(self.EXPECTED))

    def test_the_summary_is_what_the_warden_screen_shows(self):
        sweep = SweepState(zone_id="assembly-north")
        sweep.confirm("emp:EMP-1", "warden-7", T0)
        sweep.tag_unknown("warden-7", T0)
        sweep.note("gate B is chained shut", "warden-7", T0)
        sweep.record_headcount(count(2, 3))
        summary = sweep.summary(self.EXPECTED)
        assert summary["expected"] == 3
        assert summary["confirmed"] == 1
        assert summary["outstanding"] == 2
        assert summary["unknown_tagged"] == 1
        assert summary["severity"] == Severity.ESCALATE.value
        assert summary["is_clean"] is False
        assert summary["blocking"]
