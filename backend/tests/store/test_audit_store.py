"""The audit log, surviving a restart.

The table has existed since the first migration and nothing wrote to it, so the
log lived in one process's memory and died with it. For a record whose purpose
is answering questions after an incident, that is close to not having one: an
incident is exactly when somebody restarts things.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.infra.audit import AuditAction, AuditLog
from app.store.audit import AuditStore
from app.store.drills import StoreUnavailable
from app.store.schema import metadata

T0 = 1_788_000_000_000


@pytest.fixture
def engine():
    engine = sa.create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    return engine


@pytest.fixture
def log(engine) -> AuditLog:
    return AuditLog(store=AuditStore(engine=engine))


class TestItSurvivesTheProcess:

    def test_an_entry_written_here_is_read_back_there(self, engine, log):
        log.record(action=AuditAction.DRILL_COMPLETED, actor_id="commander-1",
                   ts_ms=T0, drill_id="d1", summary="ended Q3 drill",
                   all_clear=False, accounted=38, expected=40)

        # A different process, a different log object, the same database.
        after_restart = AuditStore(engine=engine).for_drill("d1")
        assert len(after_restart) == 1
        entry = after_restart[0]
        assert entry.action is AuditAction.DRILL_COMPLETED
        assert entry.actor_id == "commander-1"
        assert entry.summary == "ended Q3 drill"
        assert entry.context["accounted"] == 38

    def test_entries_come_back_oldest_first(self, engine, log):
        for i in range(3):
            log.record(action=AuditAction.WARDEN_ACTION, actor_id="warden-7",
                       ts_ms=T0 + i * 1_000, drill_id="d1", summary=f"#{i}")
        assert [e.summary for e in AuditStore(engine=engine).for_drill("d1")] \
            == ["#0", "#1", "#2"]

    def test_the_newest_page_is_bounded(self, engine, log):
        for i in range(10):
            log.record(action=AuditAction.ACCESS_DENIED, actor_id="nosy",
                       ts_ms=T0 + i * 1_000, summary=f"#{i}")
        recent = AuditStore(engine=engine).recent(limit=3)
        assert [e.summary for e in recent] == ["#7", "#8", "#9"]

    def test_before_and_after_survive_the_round_trip(self, engine, log):
        # An override recorded without the state it overrode is unreviewable,
        # so it is the part that must not be lost in serialisation.
        log.record(action=AuditAction.MANUAL_OVERRIDE, actor_id="commander-1",
                   ts_ms=T0, drill_id="d1", summary="marked accounted by hand",
                   before={"state": "UNCERTAIN"}, after={"state": "ACCOUNTED"})

        entry = AuditStore(engine=engine).for_drill("d1")[0]
        assert entry.before == {"state": "UNCERTAIN"}
        assert entry.after == {"state": "ACCOUNTED"}
        assert entry.is_override is True


class TestAWriteThatCannotLand:

    def test_it_is_counted_rather_than_raised(self):
        # An audit write must not interrupt an evacuation.
        unmigrated = AuditStore(engine=sa.create_engine("sqlite:///:memory:"))
        log = AuditLog(store=unmigrated)

        log.record(action=AuditAction.DRILL_STARTED, actor_id="commander-1",
                   ts_ms=T0, drill_id="d1")

        assert len(log.entries) == 1        # still in memory
        assert log.unpersisted == 1         # and known to be only there
        assert "no such table" in log.last_store_error.lower()

    def test_a_log_with_no_store_counts_nothing(self, engine):
        log = AuditLog()
        log.record(action=AuditAction.DRILL_STARTED, actor_id="c", ts_ms=T0)
        assert log.unpersisted == 0
        assert log.last_store_error is None


class TestAReadThatCannotBeAnswered:

    def test_it_raises_rather_than_returning_nothing(self):
        unmigrated = AuditStore(engine=sa.create_engine("sqlite:///:memory:"))
        with pytest.raises(StoreUnavailable):
            unmigrated.for_drill("d1")
        with pytest.raises(StoreUnavailable):
            unmigrated.recent()
