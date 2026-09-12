"""The calibration harness, and its refusals.

Most of these test that the harness declines to certify. That is the point:
invariant 7 says no production number is invented, and the only way to enforce
that is a certification path that fails closed.
"""

from __future__ import annotations

import json

import pytest

from app.calibration.dataset import (
    MalformedDataset,
    load,
    MIN_ENROLLED_PEOPLE,
    UNKNOWN_KEY,
    CalibrationSet,
    LabelledObservation,
    Source,
    split,
)
from app.calibration.report import MAX_DRIFT, certify
from app.core.fusion import AssociationKind
from app.calibration.sweep import (
    NoAcceptableOperatingPoint,
    Sweep,
    _as_observation,
    evaluate,
)
from app.core.identity_fsm import (
    PROVISIONAL_CONFIG,
    IdentityConfig,
    RejectionReason,
    gate,
)


def obs(i: int, *, true=None, proposed=None, score=0.8, margin=0.3,
        source=Source.RECORDED_DRILL, camera="cam-1",
        association=AssociationKind.SHARED_TRACK, **kw) -> LabelledObservation:
    """One labelled row, from a pipeline that said how it associated the face.

    Stated rather than defaulted. `LabelledObservation` defaults to `NONE` --
    the source did not record it -- because a row that does not say has not
    established a shared track, and calibrating as though it had chooses
    thresholds against evidence that never existed. Every row here is a good
    one on purpose; the weak-association cases say so.
    """
    return LabelledObservation(
        observation_id=f"obs-{i}", true_identity=true, proposed_identity=proposed,
        score=score, margin=margin, camera_id=camera, source=source,
        association=association, **kw)


def good_set(*, people=40, per_person=6, unknowns=60,
             source=Source.RECORDED_DRILL) -> CalibrationSet:
    """A corpus that clears every readiness check."""
    cs = CalibrationSet(name="test")
    n = 0
    for p in range(people):
        emp = f"EMP-{p:03d}"
        for _ in range(per_person):
            cs.add(obs(n, true=emp, proposed=emp, score=0.80, margin=0.30,
                       camera=f"cam-{n % 3}", source=source))
            n += 1
    for u in range(unknowns):
        # An unenrolled face still gets a nearest match, at a poor score.
        cs.add(obs(n, true=None, proposed=f"EMP-{u % people:03d}",
                   score=0.22, margin=0.02, camera=f"cam-{n % 3}", source=source))
        n += 1
    return cs


class TestReadiness:
    def test_a_good_set_is_usable(self):
        assert good_set().readiness().usable is True

    def test_a_tiny_set_is_refused(self):
        cs = CalibrationSet(observations=[obs(i, true="EMP-1", proposed="EMP-1")
                                          for i in range(10)])
        readiness = cs.readiness()
        assert readiness.usable is False
        assert any("observations" in p for p in readiness.problems)

    def test_too_few_people_is_refused(self):
        cs = good_set(people=MIN_ENROLLED_PEOPLE - 1, per_person=20)
        assert any("enrolled people" in p for p in cs.readiness().problems)

    def test_no_unenrolled_people_is_a_blocker_not_a_warning(self):
        # Without them the false-accept rate cannot be measured at all, and
        # that is the number that matters most.
        cs = good_set(unknowns=0)
        readiness = cs.readiness()
        assert readiness.usable is False
        assert any("false-accept rate cannot be measured" in p
                   for p in readiness.problems)

    def test_a_single_camera_is_a_warning(self):
        cs = CalibrationSet(observations=[
            obs(i, true=f"EMP-{i % 40:03d}", proposed=f"EMP-{i % 40:03d}",
                camera="cam-only") for i in range(240)]
            + [obs(1000 + i, true=None, proposed="EMP-000", score=0.2,
                   camera="cam-only") for i in range(40)])
        assert any("fewer than two cameras" in w for w in cs.readiness().warnings)

    def test_a_simulated_set_is_flagged(self):
        cs = good_set(source=Source.SIMULATOR)
        assert any("entirely simulated" in w for w in cs.readiness().warnings)


class TestSplit:
    def test_it_splits_by_person_not_by_observation(self):
        # The same face appears in dozens of frames. Splitting by frame puts
        # near-duplicates on both sides and leaks the answer.
        result = split(good_set())
        assert result.is_clean is True

    def test_both_halves_get_unenrolled_observations(self):
        # Grouping every unenrolled face as one indivisible person put them all
        # on one side, leaving the other unable to measure a false-accept rate.
        result = split(good_set())
        assert result.tune.unknown_observations
        assert result.validate.unknown_observations

    def test_the_shared_unknown_key_does_not_count_as_leakage(self):
        result = split(good_set())
        assert UNKNOWN_KEY in result.tune.people
        assert UNKNOWN_KEY in result.validate.people
        assert result.is_clean is True

    def test_it_is_deterministic(self):
        a, b = split(good_set()), split(good_set())
        assert a.held_out_people == b.held_out_people

    def test_appending_data_does_not_reshuffle_existing_people(self):
        # Hash-based rather than index-based, so a report stays reproducible as
        # the corpus grows.
        base = good_set()
        before = set(split(base).held_out_people)
        base.add(obs(99_999, true="EMP-999", proposed="EMP-999"))
        after = set(split(base).held_out_people)
        assert before <= after

    def test_a_nonsense_fraction_is_refused(self):
        with pytest.raises(ValueError):
            split(good_set(), validate_fraction=0.0)
        with pytest.raises(ValueError):
            split(good_set(), validate_fraction=1.0)


class TestEvaluate:
    CONFIG = PROVISIONAL_CONFIG

    def test_a_correct_admitted_match_is_a_true_accept(self):
        cs = CalibrationSet(observations=[obs(0, true="EMP-1", proposed="EMP-1")])
        assert evaluate(cs, self.CONFIG).true_accepts == 1

    def test_a_wrong_admitted_match_is_a_false_accept(self):
        cs = CalibrationSet(observations=[obs(0, true="EMP-1", proposed="EMP-2")])
        assert evaluate(cs, self.CONFIG).false_accepts == 1

    def test_an_admitted_unenrolled_face_is_a_false_accept(self):
        # The purest form of the dangerous error: the true answer is nobody.
        cs = CalibrationSet(observations=[obs(0, true=None, proposed="EMP-1")])
        outcome = evaluate(cs, self.CONFIG)
        assert outcome.false_accepts == 1
        assert outcome.admitted_unknown == 1

    def test_a_rejected_enrolled_face_is_a_false_reject(self):
        cs = CalibrationSet(observations=[
            obs(0, true="EMP-1", proposed="EMP-1", score=0.1)])
        assert evaluate(cs, self.CONFIG).false_rejects == 1

    def test_a_rejected_unenrolled_face_is_the_right_answer(self):
        cs = CalibrationSet(observations=[
            obs(0, true=None, proposed="EMP-1", score=0.1)])
        assert evaluate(cs, self.CONFIG).true_rejects == 1

    def test_the_false_accept_rate_is_denominated_on_admissions(self):
        # "One in a thousand observations was wrong" sounds harmless. "One in
        # twenty admitted identities was wrong" is the operator's number.
        cs = CalibrationSet(observations=(
            [obs(i, true="EMP-1", proposed="EMP-1") for i in range(19)]
            + [obs(100, true="EMP-2", proposed="EMP-1")]
            + [obs(200 + i, true="EMP-3", proposed="EMP-3", score=0.05)
               for i in range(980)]))
        outcome = evaluate(cs, self.CONFIG)
        assert outcome.false_accept_rate == pytest.approx(0.05)

    def test_rates_are_none_rather_than_zero_when_unmeasurable(self):
        outcome = evaluate(CalibrationSet(), self.CONFIG)
        assert outcome.true_accept_rate is None
        assert outcome.false_accept_rate is None


class TestChoosingAnOperatingPoint:
    def test_it_holds_the_false_accept_ceiling(self):
        sweep = Sweep(split=split(good_set()), base_config=PROVISIONAL_CONFIG).run()
        point = sweep.choose(false_accept_ceiling=0.01)
        assert point.on_tune.false_accept_rate <= 0.01

    def test_it_refuses_rather_than_loosening_the_ceiling(self):
        # A pipeline whose unenrolled faces score as highly as its enrolled ones
        # cannot hit any useful ceiling. That is a finding about the pipeline,
        # not a reason to pick the number after seeing the answer.
        cs = CalibrationSet(name="overlapping")
        for i in range(40):
            emp = f"EMP-{i:03d}"
            for j in range(6):
                cs.add(obs(i * 10 + j, true=emp, proposed=emp,
                           score=0.80, margin=0.30, camera=f"cam-{j % 3}"))
        for u in range(60):
            # Indistinguishable from a genuine match on every gated feature.
            cs.add(obs(10_000 + u, true=None, proposed=f"EMP-{u % 40:03d}",
                       score=0.80, margin=0.30, camera=f"cam-{u % 3}"))
        sweep = Sweep(split=split(cs), base_config=PROVISIONAL_CONFIG).run()
        with pytest.raises(NoAcceptableOperatingPoint, match="after seeing"):
            sweep.choose(false_accept_ceiling=0.01)

    def test_a_ceiling_of_zero_is_reachable_when_the_data_allows(self):
        # Not a trick: if no admitted observation carries a wrong name, the
        # false-accept rate really is zero and the ceiling really is met.
        sweep = Sweep(split=split(good_set()), base_config=PROVISIONAL_CONFIG).run()
        point = sweep.choose(false_accept_ceiling=0.0)
        assert point.on_tune.false_accept_rate == 0.0

    def test_choosing_before_running_is_refused(self):
        sweep = Sweep(split=split(good_set()), base_config=PROVISIONAL_CONFIG)
        with pytest.raises(ValueError, match="run the sweep"):
            sweep.choose()

    def test_it_measures_the_held_out_half(self):
        sweep = Sweep(split=split(good_set()), base_config=PROVISIONAL_CONFIG).run()
        point = sweep.choose()
        assert point.on_validate is not None
        assert point.generalises is not None
        assert point.drift is not None

    def test_the_curve_is_available_for_plotting(self):
        sweep = Sweep(split=split(good_set()), base_config=PROVISIONAL_CONFIG).run()
        curve = sweep.curve()
        assert curve
        assert all(0 <= fa <= 1 and 0 <= ta <= 1 for fa, ta in curve)


class TestCertification:
    """Certification fails closed. Every refusal below is deliberate."""

    def _sweep(self, cs: CalibrationSet) -> Sweep:
        return Sweep(split=split(cs), base_config=PROVISIONAL_CONFIG).run()

    def test_good_real_data_certifies(self):
        report = certify(self._sweep(good_set()))
        assert report.certified is True
        assert report.config.calibrated is True
        assert "calibration on" in report.config.source

    def test_simulated_data_never_certifies(self):
        report = certify(self._sweep(good_set(source=Source.SIMULATOR)))
        assert report.certified is False
        assert report.config.calibrated is False
        assert any("entirely simulated" in r for r in report.refusals)

    def test_simulation_can_be_allowed_explicitly_for_harness_testing(self):
        report = certify(self._sweep(good_set(source=Source.SIMULATOR)),
                         require_real_footage=False)
        assert not any("simulated" in r for r in report.refusals)

    def test_an_unusable_set_never_certifies(self):
        report = certify(self._sweep(good_set(unknowns=0)))
        assert report.certified is False

    def test_a_refused_report_is_still_rendered(self):
        # Worth reading even when it cannot certify. It just may not claim to.
        report = certify(self._sweep(good_set(source=Source.SIMULATOR)))
        rendered = "\n".join(report.render())
        assert "certified        NO" in rendered
        assert "Not certified because:" in rendered

    def test_the_rendered_report_names_what_it_was_measured_on(self):
        report = certify(self._sweep(good_set()))
        rendered = "\n".join(report.render())
        assert "held-out people" in rendered
        assert "true-accept rate" in rendered
        assert "false-accept rate" in rendered

    def test_drift_on_unseen_people_refuses(self):
        """Thresholds fitted to the tuning half must not certify.

        Built by making only the held-out people hard: their faces produce
        high-scoring wrong matches that the tuning half never contains. A
        threshold chosen on the tuning half then looks excellent there and
        fails on people it has never seen, which is precisely the failure the
        split exists to catch.
        """
        base = good_set()
        held_out = set(split(base).held_out_people)
        assert held_out

        for n, person in enumerate(sorted(held_out)):
            for j in range(12):
                base.add(obs(50_000 + n * 100 + j, true=person,
                             proposed="EMP-999", score=0.80, margin=0.30,
                             camera=f"cam-{j % 3}"))

        report = certify(self._sweep(base))
        point = report.operating_point
        assert point.on_validate is not None
        assert point.drift is not None and point.drift > MAX_DRIFT
        assert report.certified is False
        assert any("fitted to the tuning half" in r or "above the" in r
                   for r in report.refusals)

    def test_a_clean_split_reports_small_drift(self):
        report = certify(self._sweep(good_set()))
        assert report.operating_point.drift is not None
        assert abs(report.operating_point.drift) <= MAX_DRIFT


class TestConfigIsOnlyCalibratedByCertification:
    def test_the_provisional_config_is_not_calibrated(self):
        assert PROVISIONAL_CONFIG.calibrated is False

    def test_nothing_else_sets_calibrated_true(self):
        # The only route to calibrated=True is certify(). A config constructed
        # by hand defaults to False, so a forgotten step fails safe.
        config = IdentityConfig(
            score_threshold=0.9, min_margin=0.5, min_votes=3, conflict_votes=3,
            min_face_quality=0.5, max_pose_deviation_deg=45.0,
            min_track_confidence=0.5, identity_expiry_ms=30_000)
        assert config.calibrated is False


class TestTheTrueAcceptDenominator:
    """Of the enrolled people we could have identified, how many did we?

    An unenrolled stranger given somebody's name is not an enrolled person we
    failed to identify -- there was no right answer to get. Counting them
    dragged this number down in proportion to how many strangers walked past
    the camera, which is a property of the crowd rather than of the matcher.
    """

    def outcome(self, **overrides):
        from app.calibration.sweep import ThresholdOutcome

        base = dict(score_threshold=0.4, min_margin=0.05,
                    true_accepts=80, false_accepts=5, false_rejects=15,
                    true_rejects=70, admitted_unknown=0)
        base.update(overrides)
        return ThresholdOutcome(**base)

    def test_strangers_do_not_count_against_it(self):
        # 80 of 100 enrolled named correctly, whatever the strangers did.
        clean = self.outcome()
        crowded = self.outcome(false_accepts=35, admitted_unknown=30)
        assert clean.true_accept_rate == pytest.approx(0.8)
        assert crowded.true_accept_rate == pytest.approx(0.8)

    def test_an_enrolled_person_named_wrongly_still_counts(self):
        # That one *is* an enrolled person we could have identified and did not.
        assert self.outcome(true_accepts=80, false_accepts=20,
                            false_rejects=0).true_accept_rate == pytest.approx(0.8)

    def test_the_stranger_rates_still_see_them(self):
        crowded = self.outcome(false_accepts=35, admitted_unknown=30)
        assert crowded.false_accept_rate == pytest.approx(35 / 115)
        assert crowded.unknown_accept_rate == pytest.approx(30 / 100)


class TestAnAnswerOnTheEdgeOfTheSearch:
    """A chosen pair on the boundary of the grid means the best pair may lie
    outside where anyone looked."""

    def test_the_boundary_is_reported(self):
        from app.calibration.sweep import Sweep
        from app.core.identity_fsm import PROVISIONAL_CONFIG

        # A grid of exactly one point: whatever it picks is on every edge.
        sweep = Sweep(split=split(good_set()), base_config=PROVISIONAL_CONFIG)
        sweep.run(score_range=(0.4, 0.4, 0.1), margin_range=(0.05, 0.05, 0.1))
        point = sweep.choose(false_accept_ceiling=1.0)
        assert point.on_grid_boundary
        assert any("score_threshold" in edge for edge in point.on_grid_boundary)

    def test_an_interior_answer_is_not_flagged(self):
        from app.calibration.sweep import Sweep
        from app.core.identity_fsm import PROVISIONAL_CONFIG

        # A grid wide enough that the answer is not against a wall.
        sweep = Sweep(split=split(good_set()), base_config=PROVISIONAL_CONFIG)
        sweep.run(score_range=(0.1, 0.99, 0.05), margin_range=(0.0, 0.6, 0.05))
        point = sweep.choose(false_accept_ceiling=1.0)
        assert not any("score_threshold" in edge
                       for edge in point.on_grid_boundary), point.on_grid_boundary

    def test_the_report_carries_it_as_a_caveat_not_a_refusal(self):
        from app.calibration.sweep import Sweep
        from app.core.identity_fsm import PROVISIONAL_CONFIG

        sweep = Sweep(split=split(good_set()), base_config=PROVISIONAL_CONFIG)
        sweep.run(score_range=(0.4, 0.4, 0.1), margin_range=(0.05, 0.05, 0.1))
        report = certify(sweep, require_real_footage=False)
        assert any("outside the range searched" in c for c in report.caveats)
        assert not any("outside the range searched" in r
                       for r in report.refusals)


class TestTheHarnessGatesLikeTheFoldDoes:
    """A sweep that admits observations the running system would reject is
    choosing thresholds for a pipeline that does not exist.

    `Ingestor._face_observed` scales `track_confidence` by how the face was
    attached to the body before gating it. `_as_observation` passed the raw
    value, so the two disagreed the moment the fold started applying the
    weight, and the numbers a sweep produced stopped describing the system.
    """

    def payload(self, association="SHARED_TRACK"):
        return {"candidate_id": "EMP-001", "score": 0.8, "margin": 0.3,
                "quality": 0.9, "pose_deviation_deg": 5.0,
                "track_confidence": 0.9, "association": association,
                "camera_id": "cam-1"}

    def from_the_fold(self, payload):
        """What the ingestor builds, reached through the ingestor itself."""
        from app.core.events import Event, EventType, SourceKind
        from app.ingest.ingestor import Ingestor

        built = {}
        node = Ingestor()
        original = node.state.identity.observe

        def spy(person_id, observation):
            built["observation"] = observation
            return original(person_id, observation)

        node.state.identity.observe = spy
        node.feed(Event(tenant_id="t", site_id="s", drill_id="d",
                        source="cam-1", source_kind=SourceKind.CAMERA, seq=1,
                        type=EventType.FACE_OBSERVED, ts_ms=0, subject="gp-1",
                        payload=payload))
        return built["observation"]

    def from_the_harness(self, payload):
        from app.calibration.from_simulator import association_from

        return _as_observation(LabelledObservation(
            observation_id="o-1", true_identity="EMP-001",
            proposed_identity=payload["candidate_id"],
            score=payload["score"], margin=payload["margin"],
            quality=payload["quality"],
            pose_deviation_deg=payload["pose_deviation_deg"],
            track_confidence=payload["track_confidence"],
            association=association_from(payload),
            camera_id=payload["camera_id"]))

    @pytest.mark.parametrize(
        "association", ["SHARED_TRACK", "SPATIAL_IOU", "TEMPORAL_ONLY"])
    def test_both_build_the_same_observation(self, association):
        payload = self.payload(association)
        folded = self.from_the_fold(payload)
        swept = self.from_the_harness(payload)

        for field in ("score", "margin", "quality", "pose_deviation_deg",
                      "track_confidence", "camera_id",
                      "association_is_strong"):
            assert getattr(folded, field) == getattr(swept, field), field

    def test_a_weak_association_is_rejected_by_both(self):
        payload = self.payload("SPATIAL_IOU")
        assert gate(self.from_the_fold(payload),
                    PROVISIONAL_CONFIG) is not RejectionReason.ACCEPTED
        assert gate(self.from_the_harness(payload),
                    PROVISIONAL_CONFIG) is not RejectionReason.ACCEPTED

    def test_a_row_that_does_not_say_is_not_swept_as_a_shared_track(self):
        # The default was True, so an unlabelled row calibrated as the
        # strongest kind and the chosen thresholds were tuned against evidence
        # that never existed.
        row = LabelledObservation(observation_id="o-1", true_identity="EMP-001",
                                  proposed_identity="EMP-001", score=0.9,
                                  margin=0.4)
        assert row.association_is_strong is False
        assert gate(_as_observation(row),
                    PROVISIONAL_CONFIG) is not RejectionReason.ACCEPTED


class TestGettingALabelledSetInFromOutside:
    """`from_simulator` says the value of the harness is that "the day recorded
    footage exists, only the labelling is new work". That was not quite true.

    There was no way to get a labelled set into the harness without writing
    Python, so labelling recorded footage was necessary and not sufficient, and
    the Phase 2 deliverable could only be driven from a test.
    """

    def rows(self, n=3):
        return [{"observation_id": f"o-{i}", "true_identity": "EMP-001",
                 "proposed_identity": "EMP-001", "score": 0.8, "margin": 0.3,
                 "source": "RECORDED_DRILL", "camera_id": "cam-1",
                 "association": "SHARED_TRACK"}
                for i in range(n)]

    def test_rows_become_a_set(self, tmp_path):
        path = tmp_path / "labelled.json"
        path.write_text(json.dumps(self.rows()))
        loaded = load(path)
        assert len(loaded) == 3
        assert loaded.enrolled_people == {"EMP-001"}

    def test_an_unenrolled_row_keeps_its_null(self, tmp_path):
        # The difference between measuring the dangerous error and not.
        rows = self.rows(1)
        rows[0]["true_identity"] = None
        path = tmp_path / "labelled.json"
        path.write_text(json.dumps(rows))
        assert load(path).unknown_observations

    def test_a_missing_field_is_refused_by_name(self, tmp_path):
        rows = self.rows(1)
        del rows[0]["true_identity"]
        path = tmp_path / "labelled.json"
        path.write_text(json.dumps(rows))
        with pytest.raises(MalformedDataset, match="true_identity"):
            load(path)

    def test_a_file_that_does_not_say_where_it_came_from_is_refused(self,
                                                                    tmp_path):
        # `source` decides whether the result may be certified at all, so a
        # file that does not say must not be read as recorded footage.
        rows = self.rows(1)
        del rows[0]["source"]
        path = tmp_path / "labelled.json"
        path.write_text(json.dumps(rows))
        with pytest.raises(MalformedDataset, match="source"):
            load(path)

    def test_an_unstated_association_is_not_read_as_a_shared_track(self,
                                                                   tmp_path):
        rows = self.rows(1)
        del rows[0]["association"]
        path = tmp_path / "labelled.json"
        path.write_text(json.dumps(rows))
        assert load(path).observations[0].association_is_strong is False

    def test_an_empty_file_is_refused_rather_than_swept(self, tmp_path):
        # A sweep over nothing produces rates of None and a report that reads
        # like a measurement.
        path = tmp_path / "labelled.json"
        path.write_text("[]")
        with pytest.raises(MalformedDataset, match="no observations"):
            load(path)

    def test_it_is_not_valid_json(self, tmp_path):
        path = tmp_path / "labelled.json"
        path.write_text("{not json")
        with pytest.raises(MalformedDataset, match="not valid JSON"):
            load(path)


class TestTheCalibrationEntryPoint:
    """A deliverable with no command that produces it is a deliverable nobody
    can hand over."""

    def script(self, argv):
        import importlib.util
        from pathlib import Path

        path = (Path(__file__).resolve().parents[2] / "scripts"
                / "calibrate.py")
        spec = importlib.util.spec_from_file_location("calibrate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.main(argv)

    def test_a_simulated_run_refuses_to_certify(self, capsys):
        # The whole point of running it: a build that silently produced
        # "calibrated" numbers from synthetic data is the failure this module
        # is arranged to prevent. Non-zero, so it can gate a deployment.
        assert self.script(["--people", "80"]) == 1
        assert "entirely simulated" in capsys.readouterr().out

    def test_a_dataset_it_cannot_read_is_named_and_refused(self, capsys,
                                                           tmp_path):
        # A dataset the harness half understood would produce a report that
        # looks like every other report, which is worse than no report.
        path = tmp_path / "bad.json"
        path.write_text('[{"observation_id": "o-1"}]')
        assert self.script(["--dataset", str(path)]) == 2
        assert "cannot read the dataset" in capsys.readouterr().err
