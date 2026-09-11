"""The drill store's contract, which is asymmetric on purpose.

A write that cannot land reports failure and the drill carries on: the operator
has an evacuation in progress, and refusing to start one because a replica is
failing over would be the software choosing its own bookkeeping over the thing
it exists for.

A read that cannot be answered raises. Every falsy thing a read could return is
also a legitimate answer -- no rows, nothing running -- so a caller that only
counts what came back cannot tell "there were none" from "I could not look",
and one of those is a building with people in it.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.core.roster import ExpectationReason, Roster
from app.drill import Drill
from app.store.drills import DrillStore
from app.store.errors import StoreUnavailable

T0 = 1_788_000_000_000


@pytest.fixture
def unmigrated() -> DrillStore:
    """An engine pointed at a database whose tables were never created."""
    return DrillStore(engine=sa.create_engine("sqlite:///:memory:"))


@pytest.fixture
def drill() -> Drill:
    roster = Roster()
    roster.add_employee(emp_id="EMP-001", display_name="Ali",
                        has_gallery_entry=True,
                        reason=ExpectationReason.ON_SHIFT)
    return Drill(drill_id="d1", tenant_id="t", site_id="site-1", name="Q3",
                 roster=roster.snapshot(T0), created_ms=T0)


class TestAReadThatCouldNotBeAnswered:

    def test_unfinished_raises_rather_than_returning_nothing(self, unmigrated):
        with pytest.raises(StoreUnavailable):
            unmigrated.unfinished("site-1")
        assert unmigrated.last_error

    def test_the_error_is_kept_for_the_operator(self, unmigrated):
        with pytest.raises(StoreUnavailable):
            unmigrated.unfinished("site-1")
        assert "no such table" in unmigrated.last_error.lower()


class TestAWriteThatCouldNotLand:

    def test_it_reports_rather_than_raising(self, unmigrated, drill):
        assert unmigrated.save(drill) is False
        assert unmigrated.last_error

    def test_a_working_store_saves_and_reads_back(self, drill):
        from app.store.schema import metadata

        engine = sa.create_engine("sqlite:///:memory:")
        metadata.create_all(engine)
        store = DrillStore(engine=engine)

        drill.start(T0 + 1_000)
        assert store.save(drill) is True
        assert store.last_error is None

        running = store.unfinished("site-1")
        assert [row["drill_id"] for row in running] == ["d1"]
