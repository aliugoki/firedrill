"""The DeepStream code vendored into app/vendor/deepstream still works here.

Phase 0 gate, and a copy-integrity check. These two files are the ones EVAC-120
builds directly on:

  * `recognition.py` — the Gallery match and the per-track voting that
    `app/core/identity_fsm.py` extends in Phase 1.
  * `outbox.py` — the SQLite store-and-forward buffer that edge-to-central
    replication uses in Phase 3.

Several tests below assert the *limits* of the vendored code rather than its
capabilities. Those limits are exactly what Phase 1 has to close, so they are
pinned here to stop anyone assuming the vendored class already does the job.
"""

import numpy as np
import pytest

from app.vendor.deepstream.outbox import DBUnavailable
from app.vendor.deepstream.recognition import (
    Gallery,
    TrackIdentityManager,
    accept_match,
)


def _unit(*components: float) -> np.ndarray:
    v = np.array(components, dtype=np.float32)
    return v / np.linalg.norm(v)


class FakeClock:
    """Deterministic replacement for time.monotonic."""

    def __init__(self, t: float = 0.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class TestGallery:
    def test_empty_gallery_matches_nothing(self):
        # Invariant 1: an empty gallery is FACE_UNAVAILABLE, not a match.
        g = Gallery()
        best_id, score, margin = g.match(_unit(1, 0, 0))
        assert best_id is None
        assert score == -1.0
        assert margin == -1.0

    def test_match_returns_the_nearest_identity(self):
        g = Gallery()
        g.replace({"EMP-1": _unit(1, 0, 0), "EMP-2": _unit(0, 1, 0)})
        best_id, score, margin = g.match(_unit(0.99, 0.01, 0))
        assert best_id == "EMP-1"
        assert score > 0.9

    def test_margin_is_the_gap_to_the_runner_up(self):
        # The margin gate is what separates a confident match from a coin flip
        # between two look-alike employees.
        g = Gallery()
        g.replace({"EMP-1": _unit(1, 0, 0), "EMP-2": _unit(0, 1, 0)})
        _, _, wide = g.match(_unit(1, 0, 0))
        _, _, narrow = g.match(_unit(1, 1, 0))
        assert wide > narrow
        assert abs(narrow) < 1e-6

    def test_single_entry_gallery_reports_maximum_margin(self):
        g = Gallery()
        g.replace({"EMP-1": _unit(1, 0, 0)})
        _, _, margin = g.match(_unit(1, 0, 0))
        assert margin == 1.0

    def test_zero_vector_probe_matches_nothing(self):
        # A failed embed must not become a match against whoever is first.
        g = Gallery()
        g.replace({"EMP-1": _unit(1, 0, 0)})
        best_id, score, _ = g.match(np.zeros(3, dtype=np.float32))
        assert best_id is None
        assert score == -1.0

    def test_replace_is_atomic_and_drops_unusable_vectors(self):
        g = Gallery()
        g.replace({"EMP-1": _unit(1, 0, 0), "EMP-2": None,
                   "EMP-3": np.zeros(3, dtype=np.float32)})
        assert len(g) == 1
        assert g.match(_unit(1, 0, 0))[0] == "EMP-1"


class TestAcceptMatch:
    def test_both_gates_must_pass(self):
        assert accept_match(0.50, 0.10, threshold=0.35, min_margin=0.05) is True
        assert accept_match(0.30, 0.10, threshold=0.35, min_margin=0.05) is False
        assert accept_match(0.50, 0.01, threshold=0.35, min_margin=0.05) is False

    def test_empty_gallery_sentinel_is_rejected(self):
        # Gallery.match returns (-1.0, -1.0) when it has nothing.
        assert accept_match(-1.0, -1.0, threshold=0.35, min_margin=0.05) is False


class TestTrackIdentityManager:
    def test_identity_needs_min_votes_before_it_commits(self):
        # Invariant 2 in embryo: one noisy frame cannot label a person.
        m = TrackIdentityManager(threshold=0.35, min_margin=0.05, min_votes=3)
        assert m.observe("track-1", "EMP-1", 0.8, 0.4) is None
        assert m.observe("track-1", "EMP-1", 0.8, 0.4) is None
        assert m.observe("track-1", "EMP-1", 0.8, 0.4) == "EMP-1"
        assert m.is_committed("track-1") is True

    def test_observations_below_the_gates_never_vote(self):
        m = TrackIdentityManager(threshold=0.35, min_margin=0.05, min_votes=3)
        for _ in range(10):
            assert m.observe("track-1", "EMP-1", 0.20, 0.40) is None
        assert m.is_committed("track-1") is False

    def test_unknown_face_never_votes(self):
        # Invariant 1: absence of a match is not evidence about identity.
        m = TrackIdentityManager(min_votes=1)
        for _ in range(5):
            assert m.observe("track-1", None, -1.0, -1.0) is None
        assert m.is_committed("track-1") is False

    def test_commit_is_sticky_against_a_later_contradiction(self):
        # Invariant 2: a strong identity is not overwritten by a later
        # observation while the track is alive.
        m = TrackIdentityManager(min_votes=2)
        m.observe("track-1", "EMP-1", 0.9, 0.5)
        assert m.observe("track-1", "EMP-1", 0.9, 0.5) == "EMP-1"
        for _ in range(10):
            assert m.observe("track-1", "EMP-2", 0.95, 0.6) == "EMP-1"

    def test_stale_tracks_are_evicted_by_ttl(self):
        clock = FakeClock()
        m = TrackIdentityManager(min_votes=1, ttl_seconds=30.0, time_fn=clock)
        m.observe("track-1", "EMP-1", 0.9, 0.5)
        assert len(m) == 1
        clock.advance(31.0)
        m.observe("track-2", "EMP-2", 0.9, 0.5)
        assert len(m) == 1
        assert m.is_committed("track-1") is False


class TestVendoredIdentityLimits:
    """What the vendored manager does NOT do. Phase 1 closes each of these."""

    def test_there_are_only_two_identity_outcomes(self):
        # Committed or nothing. No CANDIDATE, no TEMPORARILY_UNAVAILABLE, no
        # CONFLICT, no REJECTED. This is the identity FSM gap EVAC-120 exists
        # to close, so it is pinned rather than assumed.
        m = TrackIdentityManager(min_votes=3)
        m.observe("track-1", "EMP-1", 0.9, 0.5)
        assert m.observe("track-1", "EMP-1", 0.9, 0.5) is None
        assert m.is_committed("track-1") is False

    def test_conflicting_evidence_is_resolved_silently_by_majority(self):
        # Invariant 3 says conflict must surface as IDENTITY_CONFLICT and then
        # MANUAL_VERIFICATION_REQUIRED. The vendored class instead picks the
        # vote leader and says nothing. Phase 1 must detect the split before
        # this class ever commits.
        m = TrackIdentityManager(min_votes=3)
        m.observe("track-1", "EMP-1", 0.9, 0.5)
        m.observe("track-1", "EMP-2", 0.9, 0.5)
        m.observe("track-1", "EMP-1", 0.9, 0.5)
        assert m.observe("track-1", "EMP-1", 0.9, 0.5) == "EMP-1"

    def test_identity_is_keyed_on_a_camera_local_track_id(self):
        # The same person on two cameras is two independent identities here.
        # EVAC-120 keys identity on the global person id instead, so this class
        # is an input to the FSM, never the FSM itself.
        m = TrackIdentityManager(min_votes=1)
        assert m.observe("cam1-track-7", "EMP-1", 0.9, 0.5) == "EMP-1"
        assert m.observe("cam2-track-3", "EMP-1", 0.9, 0.5) == "EMP-1"
        assert len(m) == 2


class TestOutbox:
    """Store-and-forward. Phase 3 replication depends on every one of these."""

    @pytest.fixture(autouse=True)
    def _isolated_outbox(self, tmp_path, monkeypatch):
        # The module reads OUTBOX_DIR at import time, so the test rebinds the
        # module attribute rather than the environment variable. Phase 3's
        # config layer must set OUTBOX_DIR from EVAC_OUTBOX_DIR before import.
        import app.vendor.deepstream.outbox as outbox

        monkeypatch.setattr(outbox, "OUTBOX_DIR", str(tmp_path))
        # COMPANY_ID is read from the environment on every _path() call, so it
        # is set here rather than patched as an attribute.
        monkeypatch.setenv("COMPANY_ID", "test-tenant")
        self.outbox = outbox
        yield

    def test_events_survive_being_written_and_counted(self):
        assert self.outbox.count() == 0
        self.outbox.add({"type": "PERSON_ENTERED_ASSEMBLY", "seq": 1})
        self.outbox.add({"type": "PERSON_ENTERED_ASSEMBLY", "seq": 2})
        assert self.outbox.count() == 2

    def test_flush_drains_the_buffer_when_the_writer_succeeds(self):
        for i in range(5):
            self.outbox.add({"type": "PERSON_ACCOUNTED", "seq": i})
        sent = []
        assert self.outbox.flush(sent.append) == 5
        assert self.outbox.count() == 0
        assert len(sent) == 5

    def test_a_failing_writer_loses_nothing(self):
        # Invariant 8: an outage degrades, it does not discard evidence.
        for i in range(3):
            self.outbox.add({"type": "PERSON_ACCOUNTED", "seq": i})

        def writer_that_is_down(_task):
            raise DBUnavailable("central unreachable")

        self.outbox.flush(writer_that_is_down)
        assert self.outbox.count() == 3

    def test_the_buffer_is_per_tenant(self, tmp_path):
        # One SQLite file per company id. A second tenant on the same edge node
        # must not read or drain the first tenant's pending events.
        import os

        self.outbox.add({"type": "PERSON_ACCOUNTED", "seq": 1})
        assert self.outbox.count() == 1
        os.environ["COMPANY_ID"] = "other-tenant"
        assert self.outbox.count() == 0
        assert (tmp_path / "test-tenant.db").exists()

    def test_events_replay_in_the_order_they_were_written(self):
        for i in range(10):
            self.outbox.add({"type": "TRACK_UPDATED", "seq": i})
        seen = []
        self.outbox.flush(lambda task: seen.append(task["seq"]))
        assert seen == list(range(10))
