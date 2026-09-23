"""The edge process's entry point, and the promise it makes on the way out.

Seventy lines nothing imported. Most of it is logging, but the `finally` block
is a safety property in disguise: a drill interrupted by a deploy should lose
nothing, which is true only if the outbox is flushed even when the supervisor
came down badly.
"""

from __future__ import annotations

import signal
import types

import pytest

from app.service import main as entry


class FakeReplicator:
    def __init__(self, backlog=0, drained=3):
        self.backlog = backlog
        self._drained = drained
        self.drains = 0

    def drain(self, *args, **kwargs):
        self.drains += 1
        return self._drained


class FakeSupervisor:
    def __init__(self):
        self.jobs = [types.SimpleNamespace(name="ingest"),
                     types.SimpleNamespace(name="replicate")]
        self.stopped = False

    def stop(self):
        self.stopped = True


def node_with(replicator=None, gaps=(), geometry=True):
    describe = types.SimpleNamespace(describe=lambda: ["two floors"])
    return types.SimpleNamespace(
        site_id="site-1",
        gaps=list(gaps),
        geometry=types.SimpleNamespace(has_geometry=geometry, geometry=describe),
        supervisor=FakeSupervisor(),
        replicator=replicator)


@pytest.fixture
def no_signals(monkeypatch):
    """Record handler installation instead of changing this process's."""
    installed = {}
    monkeypatch.setattr(signal, "signal",
                        lambda num, handler: installed.setdefault(num, handler))
    return installed


class TestShutdown:

    def test_the_outbox_is_flushed_on_a_clean_stop(self, monkeypatch, no_signals):
        replicator = FakeReplicator()
        monkeypatch.setattr(entry, "build_edge",
                            lambda: node_with(replicator=replicator))
        monkeypatch.setattr(entry, "serve", lambda supervisor, **kwargs: None)

        assert entry.main([]) == 0
        assert replicator.drains == 1

    def test_it_is_flushed_even_when_serving_raises(self, monkeypatch, no_signals):
        """The case the promise is about.

        A supervisor that comes down badly is exactly when buffered evidence is
        most likely to exist, so the flush belongs in a `finally` and this is
        what asserts it stays there.
        """
        replicator = FakeReplicator()
        monkeypatch.setattr(entry, "build_edge",
                            lambda: node_with(replicator=replicator))

        def explode(supervisor, **kwargs):
            raise RuntimeError("the ingest thread died")

        monkeypatch.setattr(entry, "serve", explode)

        with pytest.raises(RuntimeError, match="ingest thread died"):
            entry.main([])
        assert replicator.drains == 1

    def test_a_node_with_no_replicator_still_exits_cleanly(
            self, monkeypatch, no_signals):
        monkeypatch.setattr(entry, "build_edge", lambda: node_with())
        monkeypatch.setattr(entry, "serve", lambda supervisor, **kwargs: None)
        assert entry.main([]) == 0


class TestStartup:

    def test_a_signal_winds_the_supervisor_down(self, monkeypatch, no_signals):
        node = node_with()
        monkeypatch.setattr(entry, "build_edge", lambda: node)
        monkeypatch.setattr(entry, "serve", lambda supervisor, **kwargs: None)
        entry.main([])

        assert signal.SIGTERM in no_signals
        no_signals[signal.SIGTERM](signal.SIGTERM, None)
        assert node.supervisor.stopped is True

    def test_configuration_gaps_are_warned_about_at_startup(
            self, monkeypatch, no_signals, caplog):
        # At startup rather than at the first drill: a node missing its roster
        # should say so now, not when somebody presses start.
        monkeypatch.setattr(
            entry, "build_edge",
            lambda: node_with(gaps=["no roster source is configured"]))
        monkeypatch.setattr(entry, "serve", lambda supervisor, **kwargs: None)

        with caplog.at_level("WARNING"):
            entry.main([])
        assert any("no roster source" in r.getMessage() for r in caplog.records)

    def test_missing_geometry_is_warned_about(
            self, monkeypatch, no_signals, caplog):
        monkeypatch.setattr(entry, "build_edge",
                            lambda: node_with(geometry=False))
        monkeypatch.setattr(entry, "serve", lambda supervisor, **kwargs: None)

        with caplog.at_level("WARNING"):
            entry.main([])
        assert any("no geometry" in r.getMessage() for r in caplog.records)
