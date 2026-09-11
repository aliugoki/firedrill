"""Persisting the event log, and surviving not being able to.

Run against real SQL rather than a mock: the schema is written to be identical
on SQLite and Postgres precisely so these tests exercise the constraints that
matter, especially the uniqueness that makes appending idempotent.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from app.core.events import Event, EventType, SourceKind
from app.ingest.health import Component, HealthLog
from app.ingest.ingestor import Ingestor
from app.store.events import LOSS_TARGET, EventStore, rebuild
from app.store.schema import evac_events, metadata

T0 = 1_788_000_000_000


def event(seq: int, *, drill="d1", source="cam-1", subject="gp-1",
          event_type=EventType.TRACK_UPDATED, ts_ms=None) -> Event:
    return Event(
        tenant_id="t", site_id="s", drill_id=drill, source=source,
        source_kind=SourceKind.CAMERA, seq=seq, type=event_type,
        ts_ms=ts_ms if ts_ms is not None else T0 + seq * 100, subject=subject,
        payload={"zone_id": "floor-1", "zone_kind": "FLOOR",
                 "camera_id": "cam-1"})


@pytest.fixture
def engine():
    engine = sa.create_engine("sqlite://", poolclass=sa.pool.StaticPool,
                              connect_args={"check_same_thread": False})
    metadata.create_all(engine)
    return engine


@pytest.fixture
def store(engine):
    return EventStore(engine=engine, health=HealthLog())


class TestAppending:
    def test_an_event_is_stored_and_read_back_intact(self, store):
        store.append(event(1), now_ms=T0)
        [restored] = store.replay("d1")
        assert restored.type is EventType.TRACK_UPDATED
        assert restored.seq == 1
        assert restored.payload["zone_id"] == "floor-1"
        assert restored.event_id == restored.event_id

    def test_appending_the_same_event_twice_is_idempotent(self, store):
        # Enforced by the constraint rather than by checking first: a
        # check-then-insert races with itself under concurrent consumers.
        assert store.append(event(1), now_ms=T0) is True
        assert store.append(event(1), now_ms=T0) is False
        assert store.count("d1") == 1
        assert store.stats.duplicates == 1

    def test_the_constraint_is_scoped_to_the_drill(self, store):
        # A pipeline restarted between two drills begins its sequence again. A
        # global constraint would reject the second drill's first events as
        # duplicates of the first drill's.
        store.append(event(1, drill="d1"), now_ms=T0)
        assert store.append(event(1, drill="d2"), now_ms=T0) is True
        assert store.count("d1") == 1
        assert store.count("d2") == 1

    def test_different_sources_may_share_a_sequence_number(self, store):
        store.append(event(1, source="cam-1"), now_ms=T0)
        assert store.append(event(1, source="cam-2"), now_ms=T0) is True

    def test_a_batch_reports_how_many_were_new(self, store):
        events = [event(seq) for seq in range(1, 6)]
        assert store.append_many(events, now_ms=T0) == 5
        assert store.append_many(events, now_ms=T0) == 0


class TestNotEveryIntegrityErrorIsADuplicate:
    """Treating them alike means a schema bug becomes missing evidence.

    A NOT NULL violation counted as a redelivery makes the event vanish and the
    store report it as working normally, which is the quietest possible way to
    lose a record.
    """

    def test_a_uniqueness_violation_is_a_duplicate(self, store):
        store.append(event(1), now_ms=T0)
        assert store.append(event(1), now_ms=T0) is False
        assert store.stats.duplicates == 1
        assert store.pending == 0

    def test_another_integrity_failure_is_buffered_not_counted_as_one(self, store):
        broken = Event(
            tenant_id="t", site_id="s", drill_id="d1", source="cam-1",
            source_kind=SourceKind.CAMERA, seq=1,
            type=EventType.TRACK_UPDATED, ts_ms=T0, subject="gp-1",
            payload={})
        # Force a non-uniqueness integrity failure by nulling a required column
        # on the way to the database.
        import app.store.events as module

        original = module._to_row
        module._to_row = lambda e, now: {**original(e, now), "tenant_id": None}
        try:
            store.append(broken, now_ms=T0)
        finally:
            module._to_row = original

        assert store.stats.duplicates == 0
        assert store.pending == 1
        assert store.stats.failures == 1

    def test_the_classifier_recognises_both_dialects(self):
        from app.store.events import _is_duplicate

        class Fake(Exception):
            def __init__(self, text):
                self.orig = text

        assert _is_duplicate(Fake("UNIQUE constraint failed: evac_events.seq"))
        assert _is_duplicate(Fake('duplicate key value violates unique constraint'))
        assert not _is_duplicate(Fake("NOT NULL constraint failed"))


class TestReplay:
    def test_events_come_back_in_the_order_they_happened(self, store):
        # Ordered by timestamp, not insertion. A late arrival is written after
        # earlier events but happened before them, and replaying in insertion
        # order would let a later sighting be overwritten by an earlier one.
        store.append(event(3, ts_ms=T0 + 3_000), now_ms=T0)
        store.append(event(1, ts_ms=T0 + 1_000), now_ms=T0)
        store.append(event(2, ts_ms=T0 + 2_000), now_ms=T0)
        assert [e.seq for e in store.replay("d1")] == [1, 2, 3]

    def test_replay_is_scoped_to_one_drill(self, store):
        store.append(event(1, drill="d1"), now_ms=T0)
        store.append(event(1, drill="d2"), now_ms=T0)
        assert len(store.replay("d1")) == 1

    def test_it_can_resume_from_a_position(self, store):
        for seq in range(1, 6):
            store.append(event(seq), now_ms=T0)
        first = store.replay("d1", limit=2)
        rest = store.replay("d1", after_id=2)
        assert len(first) == 2
        assert len(rest) == 3

    def test_the_highest_sequence_is_readable_without_a_full_replay(self, store):
        # Lets a restarted consumer tell a genuine gap from events it has.
        for seq in (1, 2, 7):
            store.append(event(seq), now_ms=T0)
        assert store.highest_seq("d1", "cam-1") == 7
        assert store.highest_seq("d1", "cam-9") is None


class TestRebuildingFromTheLog:
    """The property that makes in-memory state acceptable everywhere else."""

    def test_a_restart_reaches_the_same_state(self, store):
        live = Ingestor()
        events = []
        for seq in range(1, 4):
            events.append(event(seq, event_type=EventType.FACE_OBSERVED))
        for seq, e in enumerate(events, start=1):
            e = Event(**{**{f: getattr(e, f) for f in e.__slots__},
                         "payload": {"candidate_id": "EMP-1", "score": 0.85,
                                     "margin": 0.4, "quality": 0.9}})
            events[seq - 1] = e
            live.feed(e)
            store.append(e, now_ms=T0)

        restarted = Ingestor()
        assert rebuild(store, "d1", restarted) == 3

        assert ({p.person_id: p.identity for p in restarted.state.identity}
                == {p.person_id: p.identity for p in live.state.identity})
        assert ({p.person_id: p.state for p in restarted.state.presence}
                == {p.person_id: p.state for p in live.state.presence})

    def test_rebuilding_twice_changes_nothing(self, store):
        for seq in range(1, 6):
            store.append(event(seq), now_ms=T0)
        restarted = Ingestor()
        rebuild(store, "d1", restarted)
        before = {p.person_id: p.state for p in restarted.state.presence}
        rebuild(store, "d1", restarted)
        assert {p.person_id: p.state for p in restarted.state.presence} == before


class TestSurvivingTheDatabase:
    """Phase 3 established that a Postgres outage is not blinding. This is that
    decision carried into the storage layer."""

    def _broken(self, store):
        store.engine = sa.create_engine("postgresql+psycopg2://nobody@127.0.0.1:1/x")

    def test_an_outage_does_not_raise(self, store):
        self._broken(store)
        store.append(event(1), now_ms=T0)  # must not raise
        assert store.stats.failures == 1

    def test_events_are_buffered_rather_than_lost(self, store):
        self._broken(store)
        for seq in range(1, 6):
            store.append(event(seq), now_ms=T0)
        assert store.pending == 5

    def test_the_outage_is_recorded_as_non_blinding(self, store):
        # Losing storage costs durability, not sight. Reporting it as blinding
        # would make the system blind itself over something it does not need to
        # see through.
        self._broken(store)
        store.append(event(1), now_ms=T0)
        assert store.health.is_degraded is True
        assert store.health.is_blind is False
        assert store.health.open_now()[0].component is Component.DATABASE

    def test_everything_lands_when_it_comes_back(self, store, engine):
        self._broken(store)
        for seq in range(1, 6):
            store.append(event(seq), now_ms=T0)
        store.engine = engine
        assert store.flush(T0 + 60_000) == 5
        assert store.pending == 0
        assert store.count("d1") == 5

    def test_recovery_closes_the_outage(self, store, engine):
        self._broken(store)
        store.append(event(1), now_ms=T0)
        store.engine = engine
        store.flush(T0 + 60_000)
        assert store.is_degraded is False
        assert store.health.is_degraded is False

    def test_a_full_buffer_says_so_rather_than_discarding_quietly(self, store):
        # A full buffer is a real risk to the record. A silent one is worse.
        self._broken(store)
        store.buffer_limit = 3
        for seq in range(1, 11):
            store.append(event(seq), now_ms=T0)
        assert store.pending == 3
        assert store.stats.dropped == 7
        assert store.stats.buffer_overflowed is True

    def test_a_permanent_loss_survives_the_database_coming_back(
            self, store, engine):
        """The failure that used to fix itself on paper.

        A buffer that overflows drops events no flush can recover. When the
        database returned, `_recovered` closed the outage, `is_degraded` went
        false and the node reported itself healthy -- while the drill it
        happened in could no longer be replayed in full. That is a false all
        clear about the record, which is the thing invariant 8 forbids.
        """
        self._broken(store)
        store.buffer_limit = 2
        for seq in range(1, 8):
            store.append(event(seq), now_ms=T0)
        assert store.stats.dropped == 5

        store.engine = engine
        store.flush(T0 + 60_000)

        assert store.pending == 0, "the buffered tail did land"
        assert store.is_degraded is True, (
            "the store reported itself healthy after losing evidence")

    def test_the_loss_is_its_own_degradation_not_the_outage(self, store, engine):
        # Separate targets, because the database returning ends one of them and
        # not the other. Sharing a target would close the record of the loss.
        self._broken(store)
        store.buffer_limit = 1
        for seq in range(1, 6):
            store.append(event(seq), now_ms=T0)
        store.engine = engine
        store.flush(T0 + 60_000)

        assert store.health.open_for(Component.DATABASE, "events") is None
        still_open = store.health.open_for(Component.DATABASE, LOSS_TARGET)
        assert still_open is not None
        assert "4 event(s) dropped" in still_open.reason
        assert "not recoverable" in still_open.reason

    def test_losing_evidence_does_not_blind_the_system(self, store):
        # It costs the record, not the view. Escalating it to blinding would
        # make every camera's silence uninterpretable over a storage problem.
        self._broken(store)
        store.buffer_limit = 1
        for seq in range(1, 4):
            store.append(event(seq), now_ms=T0)
        assert store.health.is_blind is False

    def test_the_count_in_the_reason_keeps_up(self, store):
        # An operator deciding whether to stop the drill needs the number, and
        # a reason frozen at "1 event" while thousands go would understate it.
        self._broken(store)
        store.buffer_limit = 1
        for seq in range(1, 4):
            store.append(event(seq), now_ms=T0)
        reason = store.health.open_for(Component.DATABASE, LOSS_TARGET).reason
        assert "2 event(s) dropped" in reason

    def test_a_still_broken_flush_keeps_everything(self, store):
        self._broken(store)
        for seq in range(1, 4):
            store.append(event(seq), now_ms=T0)
        assert store.flush(T0 + 1_000) == 0
        assert store.pending == 3


class TestTheSchemaAndTheMigrationAgree:
    """A migration that has drifted from the schema is a deployment that half
    works, and the half that does not is discovered in production."""

    def test_the_migration_produces_the_tables_the_code_expects(self, tmp_path):
        import os

        from alembic import command
        from alembic.config import Config

        url = f"sqlite:///{tmp_path / 'migrated.db'}"
        os.environ["EVAC_DATABASE_URL"] = url
        try:
            config = Config(str(
                __import__("pathlib").Path(__file__).resolve().parents[2]
                / "alembic.ini"))
            command.upgrade(config, "head")
        finally:
            os.environ.pop("EVAC_DATABASE_URL", None)

        migrated = sa.create_engine(url)
        inspector = sa.inspect(migrated)

        for table in metadata.sorted_tables:
            assert inspector.has_table(table.name), f"{table.name} was not created"
            declared = {column.name for column in table.columns}
            actual = {column["name"] for column in inspector.get_columns(table.name)}
            assert declared == actual, (
                f"{table.name} drifted: declared-only {declared - actual}, "
                f"migration-only {actual - declared}")

    def test_the_uniqueness_constraint_exists_in_the_migration(self, tmp_path):
        # The constraint is what makes appending idempotent. A migration without
        # it produces a store that silently accepts duplicates.
        import os

        from alembic import command
        from alembic.config import Config

        url = f"sqlite:///{tmp_path / 'migrated.db'}"
        os.environ["EVAC_DATABASE_URL"] = url
        try:
            config = Config(str(
                __import__("pathlib").Path(__file__).resolve().parents[2]
                / "alembic.ini"))
            command.upgrade(config, "head")
        finally:
            os.environ.pop("EVAC_DATABASE_URL", None)

        inspector = sa.inspect(sa.create_engine(url))
        constraints = inspector.get_unique_constraints("evac_events")
        assert any(set(c["column_names"]) == {"drill_id", "source", "seq"}
                   for c in constraints)

    def test_there_are_no_foreign_keys_between_the_tables(self):
        # An event whose drill row failed to write is still evidence. A
        # constraint that discards it to protect referential tidiness trades the
        # thing that matters for the thing that looks correct.
        for table in metadata.sorted_tables:
            assert not table.foreign_keys, f"{table.name} has a foreign key"


class TestAMigrationDoesNotSilenceTheProcess:
    """Alembic configures logging on the way in, and the default takes the
    application's loggers down with it.

    `fileConfig` disables every logger it does not name unless told otherwise,
    so a process that runs a migration -- a deployment wrapper, a recovery
    script, a test session -- would afterwards log nothing from `evac.edge` or
    `evac.api`. A node that has stopped speaking looks exactly like a node with
    nothing to say, which is the failure mode this whole system exists to avoid.
    """

    def test_running_a_migration_leaves_the_edge_logger_speaking(self, tmp_path):
        import logging
        import os
        from pathlib import Path

        from alembic import command
        from alembic.config import Config

        edge = logging.getLogger("evac.edge")
        edge.warning("before")
        assert edge.disabled is False

        url = f"sqlite:///{tmp_path / 'migrated.db'}"
        os.environ["EVAC_DATABASE_URL"] = url
        try:
            command.upgrade(
                Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")),
                "head")
        finally:
            os.environ.pop("EVAC_DATABASE_URL", None)

        assert edge.disabled is False, (
            "the migration disabled the edge node's logger")


class TestMigrationsRefuseToGuess:
    def test_a_missing_password_stops_a_migration(self, monkeypatch):
        # A password with a default is a password that ends up in production.
        import importlib.util
        from pathlib import Path

        monkeypatch.delenv("EVAC_DATABASE_URL", raising=False)
        monkeypatch.delenv("EVAC_DB_PASSWORD", raising=False)

        env_path = Path(__file__).resolve().parents[2] / "alembic" / "env.py"
        source = env_path.read_text()
        namespace: dict = {"os": __import__("os")}
        start = source.index("def database_url()")
        end = source.index("def run_migrations_offline")
        exec(source[start:end], namespace)

        with pytest.raises(RuntimeError, match="EVAC_DB_PASSWORD"):
            namespace["database_url"]()
