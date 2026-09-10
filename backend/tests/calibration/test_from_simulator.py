"""Harvesting labelled observations from a simulated drill.

Found untested during a review pass: the bridge that feeds the calibration
harness had no test at all, which meant the harness was exercised only by a
throwaway script.
"""

from __future__ import annotations

import pytest

from app.calibration.dataset import Source, split
from app.calibration.from_simulator import harvest
from app.calibration.report import certify
from app.calibration.sweep import Sweep
from app.core.identity_fsm import PROVISIONAL_CONFIG
from app.simulator.agents import build_population, walk_all
from app.simulator.engine import DrillPlan
from app.simulator.injections import Injections
from app.simulator.site import default_site

T0 = 1_788_000_000_000


@pytest.fixture(scope="module")
def harvested():
    site = default_site()
    agents = build_population(site, count=120, seed=31)
    walk_all(agents, site, alarm_ms=T0, seed=31)
    plan = DrillPlan(
        site=site, agents=agents, alarm_ms=T0, seed=31,
        injections=Injections(face_loss_rate=0.3, poor_quality_rate=0.2,
                              bad_pose_rate=0.15,
                              lookalike_confusion_rate=0.25,
                              wrong_identity_rate=0.01))
    return harvest(plan)


class TestHarvesting:
    def test_it_produces_labelled_observations(self, harvested):
        assert len(harvested) > 100
        assert harvested.enrolled_people

    def test_an_enrolled_person_carries_their_true_identity(self, harvested):
        enrolled = [o for o in harvested.observations if o.is_enrolled]
        assert enrolled
        assert all(o.true_identity.startswith("EMP-") for o in enrolled)

    def test_unenrolled_people_are_labelled_as_having_no_identity(self, harvested):
        # Without these the false-accept rate cannot be measured at all, and
        # that is the error that marks a stranger safe under a colleague's name.
        unknown = harvested.unknown_observations
        assert unknown
        assert all(o.true_identity is None for o in unknown)
        assert all(o.proposed_identity for o in unknown)

    def test_observations_come_from_several_cameras(self, harvested):
        # A single-camera set produces thresholds that do not transfer.
        assert len(harvested.cameras) > 1

    def test_every_observation_is_marked_simulated(self, harvested):
        assert all(o.source is Source.SIMULATOR for o in harvested.observations)

    def test_the_set_names_itself_as_not_certifiable(self, harvested):
        assert "not certifiable" in (harvested.note or "")

    def test_harvesting_is_deterministic(self):
        site = default_site()
        agents = build_population(site, count=60, seed=7)
        walk_all(agents, site, alarm_ms=T0, seed=7)
        plan = DrillPlan(site=site, agents=agents, alarm_ms=T0, seed=7,
                         injections=Injections(face_loss_rate=0.3))
        first, second = harvest(plan), harvest(plan)
        assert len(first) == len(second)
        assert ([o.score for o in first.observations]
                == [o.score for o in second.observations])


class TestItFeedsTheHarness:
    def test_a_harvested_set_is_readable_by_the_sweep(self, harvested):
        readiness = harvested.readiness()
        assert readiness.usable is True
        sweep = Sweep(split=split(harvested),
                      base_config=PROVISIONAL_CONFIG).run()
        assert sweep.results

    def test_and_the_harness_refuses_to_certify_it(self, harvested):
        # The whole point of the bridge: it exercises the arithmetic, and the
        # refusal proves simulated data cannot become a validated threshold.
        report = certify(Sweep(split=split(harvested),
                               base_config=PROVISIONAL_CONFIG).run())
        assert report.certified is False
        assert report.config.calibrated is False
        assert any("entirely simulated" in r for r in report.refusals)

    def test_the_refusal_can_be_waived_only_for_testing_the_harness(self,
                                                                   harvested):
        report = certify(Sweep(split=split(harvested),
                               base_config=PROVISIONAL_CONFIG).run(),
                         require_real_footage=False)
        assert not any("simulated" in r for r in report.refusals)


class TestAnEmptyDrillHarvestsNothing:
    def test_a_drill_with_no_faces_produces_an_unusable_set(self):
        site = default_site()
        agents = build_population(site, count=40, seed=3)
        walk_all(agents, site, alarm_ms=T0, seed=3)
        plan = DrillPlan(site=site, agents=agents, alarm_ms=T0, seed=3,
                         injections=Injections(face_loss_rate=1.0))
        harvested = harvest(plan)
        assert len(harvested) == 0
        assert harvested.readiness().usable is False
