"""Pulling geometry, and surviving not being able to."""

from __future__ import annotations

import json

import pytest

from app.core.presence_fsm import ZoneKind
from app.sync.runner import (
    GeometryStore,
    JsonExport,
    SourceUnavailable,
    SyncOutcome,
    VisionTrackDatabase,
    run_sync,
)

T0 = 1_788_000_000_000
SQUARE = [{"x": 0.0, "y": 0.0}, {"x": 1.0, "y": 0.0},
          {"x": 1.0, "y": 1.0}, {"x": 0.0, "y": 1.0}]
HOMOGRAPHY = [1.0, 0, 0, 0, 1.0, 0, 0, 0, 1.0]
TAGS = {"z-floor": ZoneKind.FLOOR, "z-assembly": ZoneKind.ASSEMBLY}


def geometry_payload():
    return {
        "floor_plans": [{
            "id": "fp1", "name": "Ground", "width_px": 1920, "height_px": 1080,
            "zones": [
                {"id": "z-floor", "name": "Zone 1", "polygon": SQUARE},
                {"id": "z-assembly", "name": "Zone 2", "polygon": SQUARE},
            ],
            "markers": [{"camera_id": "cam1", "x": 0.5, "y": 0.5}],
        }],
        "cameras": [{"id": "cam1",
                     "calibration": {"homography": HOMOGRAPHY,
                                     "floor_plan_id": "fp1"}}],
    }


class GoodSource:
    def __init__(self, payload=None):
        self.payload = payload or geometry_payload()
        self.calls = 0

    def floor_plans(self, site_id):
        self.calls += 1
        return self.payload["floor_plans"]

    def cameras(self, site_id):
        return self.payload["cameras"]


class DeadSource:
    def floor_plans(self, site_id):
        raise SourceUnavailable("VisionTrack is unreachable")

    def cameras(self, site_id):
        raise SourceUnavailable("VisionTrack is unreachable")


class BrokenSource:
    def floor_plans(self, site_id):
        raise RuntimeError("something unexpected")

    def cameras(self, site_id):
        return []


class TestPullingGeometry:
    def test_a_good_source_produces_a_ready_site(self):
        outcome = run_sync(GoodSource(), site_id="s1", now_ms=T0,
                           tagged_zones=TAGS)
        assert outcome.succeeded is True
        assert outcome.result.ready_for_a_drill is True

    def test_untagged_zones_still_sync_but_are_not_ready(self):
        outcome = run_sync(GoodSource(), site_id="s1", now_ms=T0)
        assert outcome.succeeded is True
        assert outcome.result.ready_for_a_drill is False


class TestFailingToPull:
    def test_an_unreachable_source_is_reported_not_raised(self):
        outcome = run_sync(DeadSource(), site_id="s1", now_ms=T0)
        assert outcome.succeeded is False
        assert "unreachable" in outcome.error

    def test_an_unexpected_error_is_also_contained(self):
        # A sync failure must never take down the process that runs drills.
        outcome = run_sync(BrokenSource(), site_id="s1", now_ms=T0)
        assert outcome.succeeded is False
        assert "RuntimeError" in outcome.error


class TestSurvivingWithoutVisionTrack:
    """A site that has synced once runs drills with VisionTrack switched off.
    That is the zero-Internet requirement applied to configuration."""

    def test_a_failed_sync_never_replaces_a_good_one(self):
        # Losing working geometry because VisionTrack was briefly down would
        # turn an inconvenience into a site that cannot run a drill.
        store = GeometryStore()
        run_sync(GoodSource(), site_id="s1", now_ms=T0, tagged_zones=TAGS,
                 store=store)
        assert store.ready_for_a_drill() is True

        run_sync(DeadSource(), site_id="s1", now_ms=T0 + 60_000, store=store)
        assert store.ready_for_a_drill() is True
        assert store.geometry is not None

    def test_a_site_that_has_never_synced_cannot_run(self):
        store = GeometryStore()
        assert store.ready_for_a_drill() is False
        assert "no geometry has been synced" in store.blocking()[0]

    def test_stale_geometry_is_reported_rather_than_assumed_current(self):
        # An operator should see a working drill built on last week's floor
        # plan, not assume it is current.
        store = GeometryStore()
        outcome = run_sync(GoodSource(), site_id="s1", now_ms=T0,
                           tagged_zones=TAGS, store=store)
        later = T0 + 30 * 3_600_000
        assert outcome.stale_ms(later) == 30 * 3_600_000
        assert any("hours old" in line for line in outcome.describe(later))

    def test_fresh_geometry_carries_no_warning(self):
        outcome = run_sync(GoodSource(), site_id="s1", now_ms=T0,
                           tagged_zones=TAGS)
        assert not any("WARNING" in line
                       for line in outcome.describe(T0 + 60_000))

    def test_a_failed_sync_describes_itself_usefully(self):
        outcome = run_sync(DeadSource(), site_id="s1", now_ms=T0)
        text = "\n".join(outcome.describe(T0))
        assert "failed" in text
        assert "still usable" in text


class TestJsonExport:
    """Not a fallback: the offline path. Treating it as degraded is how a
    system ends up unusable in the conditions it was built for."""

    def test_it_reads_an_export(self, tmp_path):
        path = tmp_path / "geometry.json"
        path.write_text(json.dumps(geometry_payload()))
        outcome = run_sync(JsonExport(str(path)), site_id="s1", now_ms=T0,
                           tagged_zones=TAGS)
        assert outcome.succeeded is True
        assert outcome.result.ready_for_a_drill is True

    def test_a_missing_file_is_reported_clearly(self, tmp_path):
        outcome = run_sync(JsonExport(str(tmp_path / "nope.json")),
                           site_id="s1", now_ms=T0)
        assert outcome.succeeded is False
        assert "no geometry export at" in outcome.error

    def test_invalid_json_names_the_file(self, tmp_path):
        path = tmp_path / "geometry.json"
        path.write_text("{not json")
        outcome = run_sync(JsonExport(str(path)), site_id="s1", now_ms=T0)
        assert outcome.succeeded is False
        assert "not valid JSON" in outcome.error


class TestTheDatabaseAdapter:
    def test_it_refuses_to_be_built_without_an_address(self):
        # A geometry source with no address would fail at the first drill
        # rather than at configuration time.
        with pytest.raises(ValueError, match="DSN is required"):
            VisionTrackDatabase("")

    def test_its_queries_are_read_only(self):
        # EVAC-120 never writes to VisionTrack. A bug here cannot corrupt a
        # running CCTV deployment.
        from app.sync.runner import CAMERA_QUERY, FLOOR_PLAN_QUERY

        for sql in (FLOOR_PLAN_QUERY, CAMERA_QUERY):
            upper = sql.upper()
            assert upper.strip().startswith("SELECT")
            for forbidden in ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER"):
                assert forbidden not in upper

    def test_the_read_only_guarantee_is_enforced_by_the_server(self):
        # Not by everyone remembering. A future UPDATE added in a hurry fails
        # against the connection rather than succeeding quietly.
        import inspect

        from app.sync.runner import VisionTrackDatabase as adapter

        source = inspect.getsource(adapter._query)
        assert "readonly=True" in source

    def test_a_connection_failure_becomes_source_unavailable(self):
        # So the caller handles it the same way as any other missing source.
        database = VisionTrackDatabase(
            "postgresql://nobody@127.0.0.1:1/nothing")
        with pytest.raises(SourceUnavailable):
            database.floor_plans("s1")


class TestBeforeTheFirstSync:
    """The state an edge node boots into, and holds until VisionTrack answers.

    It is also the state it stays in when VisionTrack never answers at all, so
    what it says about itself is what an operator has to act on.
    """

    def test_it_says_no_geometry_has_been_synced(self):
        store = GeometryStore()
        assert store.has_geometry is False
        assert store.ready_for_a_drill() is False
        assert store.blocking() == ["no geometry has been synced from VisionTrack"]

    def test_staleness_is_unknown_rather_than_zero(self):
        # Zero would read as "synced just now", which is the opposite.
        outcome = SyncOutcome(result=None, synced_at_ms=None,
                              source="JsonExport", error="nothing yet")
        assert outcome.stale_ms(T0) is None

    def test_a_failure_keeps_the_geometry_it_already_had(self):
        # Losing working geometry because VisionTrack was briefly down would
        # turn an inconvenience into a site that cannot run a drill.
        store = GeometryStore()
        good = run_sync(GoodSource(), site_id="s1", now_ms=T0, store=store,
                        tagged_zones=TAGS)
        assert store.has_geometry is True

        failed = run_sync(DeadSource(), site_id="s1", now_ms=T0 + 60_000,
                          store=store)
        assert failed.succeeded is False
        assert store.outcome is good
        assert store.has_geometry is True


class TestTheVisionTrackConnectionIsReadOnly:
    """EVAC-120 reads VisionTrack and never writes to it.

    The guarantee is enforced by the server rather than by everyone
    remembering, which is only true if the session is actually opened read-only
    -- and nothing checked. A bug here would be a bug in a running CCTV
    deployment, which is somebody else's product.
    """

    class FakeCursor:
        def __init__(self, rows, recorder):
            self.rows = rows
            self.recorder = recorder

        def execute(self, sql, params):
            self.recorder["sql"] = sql
            self.recorder["params"] = params

        def fetchall(self):
            return self.rows

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class FakeConnection:
        def __init__(self, rows, recorder):
            self.rows = rows
            self.recorder = recorder

        def set_session(self, **kwargs):
            self.recorder["session"] = kwargs

        def cursor(self, **kwargs):
            return TestTheVisionTrackConnectionIsReadOnly.FakeCursor(
                self.rows, self.recorder)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_psycopg2(self, monkeypatch, rows=(), recorder=None, boom=None):
        import sys
        import types

        recorder = recorder if recorder is not None else {}
        module = types.ModuleType("psycopg2")
        extras = types.ModuleType("psycopg2.extras")
        extras.RealDictCursor = object
        module.extras = extras

        def connect(dsn, connect_timeout=None):
            recorder["dsn"] = dsn
            recorder["timeout"] = connect_timeout
            if boom is not None:
                raise boom
            return TestTheVisionTrackConnectionIsReadOnly.FakeConnection(
                list(rows), recorder)

        module.connect = connect
        monkeypatch.setitem(sys.modules, "psycopg2", module)
        monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)
        return recorder

    def test_the_session_is_opened_read_only(self, monkeypatch):
        recorder = self.fake_psycopg2(monkeypatch, rows=[{"id": "fp1"}])
        rows = VisionTrackDatabase(dsn="postgres://vt").floor_plans("s1")

        assert rows == [{"id": "fp1"}]
        assert recorder["session"] == {"readonly": True, "autocommit": True}

    def test_the_query_is_scoped_to_the_site(self, monkeypatch):
        recorder = self.fake_psycopg2(monkeypatch, rows=[])
        VisionTrackDatabase(dsn="postgres://vt").cameras("site-7")
        assert recorder["params"] == {"site_id": "site-7"}

    def test_a_connection_failure_is_reported_not_raised_onward(
            self, monkeypatch):
        self.fake_psycopg2(monkeypatch, boom=OSError("no route to host"))
        with pytest.raises(SourceUnavailable, match="no route to host"):
            VisionTrackDatabase(dsn="postgres://vt").floor_plans("s1")

    def test_a_store_with_geometry_asks_it_what_is_blocking(self):
        store = GeometryStore()
        run_sync(GoodSource(), site_id="s1", now_ms=T0, store=store)
        # Nothing is tagged, so the geometry itself has the answer.
        assert any("ASSEMBLY" in reason for reason in store.blocking())
