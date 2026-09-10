"""The background scheduler, driven by an injected clock rather than by waiting.

A supervisor whose only test is "start it and watch for ten seconds" is a
supervisor nobody can reason about.
"""

from __future__ import annotations

import pytest

from app.ingest.ingestor import Ingestor
from app.service.supervisor import Job, Supervisor, build_supervisor, serve

T0 = 1_788_000_000_000


def counting_job(name="work", interval_ms=1_000, fails=False, critical=False):
    calls = []

    def run(now_ms):
        calls.append(now_ms)
        if fails:
            raise RuntimeError("dependency is down")
        return len(calls)

    return Job(name=name, interval_ms=interval_ms, run=run,
               critical=critical), calls


class TestScheduling:
    def test_a_new_job_is_immediately_due(self):
        job, calls = counting_job()
        supervisor = Supervisor(jobs=[job])
        supervisor.run_due(T0)
        assert calls == [T0]

    def test_it_waits_its_interval(self):
        job, calls = counting_job(interval_ms=1_000)
        supervisor = Supervisor(jobs=[job])
        supervisor.run_due(T0)
        supervisor.run_due(T0 + 500)
        assert len(calls) == 1
        supervisor.run_due(T0 + 1_000)
        assert len(calls) == 2

    def test_jobs_keep_their_own_clocks(self):
        fast, fast_calls = counting_job("fast", 250)
        slow, slow_calls = counting_job("slow", 10_000)
        supervisor = Supervisor(jobs=[fast, slow])
        for offset in range(0, 2_000, 250):
            supervisor.run_due(T0 + offset)
        assert len(fast_calls) == 8
        assert len(slow_calls) == 1

    def test_next_due_says_how_long_to_sleep(self):
        job, _ = counting_job(interval_ms=1_000)
        supervisor = Supervisor(jobs=[job])
        supervisor.run_due(T0)
        assert supervisor.next_due_ms(T0 + 300) == 700

    def test_a_duplicate_job_name_is_refused(self):
        supervisor = Supervisor()
        supervisor.add(counting_job("work")[0])
        with pytest.raises(ValueError, match="already registered"):
            supervisor.add(counting_job("work")[0])


class TestOneFailingJobNeverStopsAnother:
    """A geometry sync that cannot reach VisionTrack must not stop the consumer
    draining events during a drill."""

    def test_a_raising_job_does_not_prevent_the_next(self):
        broken, _ = counting_job("broken", 1_000, fails=True)
        working, working_calls = counting_job("working", 1_000)
        supervisor = Supervisor(jobs=[broken, working])
        results = supervisor.run_due(T0)
        assert len(working_calls) == 1
        assert [r.ok for r in results] == [False, True]

    def test_the_failure_is_recorded_rather_than_propagated(self):
        broken, _ = counting_job("broken", 1_000, fails=True)
        supervisor = Supervisor(jobs=[broken])
        supervisor.run_due(T0)  # must not raise
        assert broken.failures == 1
        assert "dependency is down" in broken.last_error

    def test_a_recovered_job_clears_its_failure_count(self):
        state = {"fails": True}

        def run(now_ms):
            if state["fails"]:
                raise RuntimeError("down")
            return "ok"

        job = Job(name="flaky", interval_ms=1_000, run=run)
        supervisor = Supervisor(jobs=[job])
        supervisor.run_due(T0)
        assert job.consecutive_failures == 1
        state["fails"] = False
        supervisor.run_due(T0 + 60_000)
        assert job.consecutive_failures == 0
        assert job.is_healthy is True


class TestBackoff:
    def test_a_failing_job_slows_down(self):
        job, calls = counting_job(interval_ms=1_000, fails=True)
        supervisor = Supervisor(jobs=[job])
        supervisor.run_due(T0)
        # After one failure it waits 2s rather than 1s.
        supervisor.run_due(T0 + 1_500)
        assert len(calls) == 1
        supervisor.run_due(T0 + 2_000)
        assert len(calls) == 2

    def test_backoff_is_capped(self):
        job, _ = counting_job(interval_ms=1_000, fails=True)
        job.max_backoff_ms = 8_000
        supervisor = Supervisor(jobs=[job])
        now = T0
        for _ in range(10):
            supervisor.run_due(now)
            now += 60_000
        assert job._effective_interval() == 8_000

    def test_it_never_gives_up(self):
        # A dependency coming back must not need a human to notice.
        job, calls = counting_job(interval_ms=1_000, fails=True)
        supervisor = Supervisor(jobs=[job])
        now = T0
        for _ in range(20):
            supervisor.run_due(now)
            now += 120_000
        assert len(calls) == 20

    def test_a_critical_job_is_never_backed_off(self):
        # Slowing the tick down under load is exactly when a stale track most
        # needs ageing.
        job, calls = counting_job(interval_ms=1_000, fails=True, critical=True)
        supervisor = Supervisor(jobs=[job])
        for offset in range(0, 5_000, 1_000):
            supervisor.run_due(T0 + offset)
        assert len(calls) == 5
        assert job._effective_interval() == 1_000


class TestTheTickIsSpecial:
    def test_the_tick_is_critical(self):
        # If it stops, nobody ever becomes LOST and the board freezes into
        # optimism: every person keeps the state they had while the screen
        # looks calm.
        supervisor = build_supervisor(ingestor=Ingestor())
        assert supervisor.job("tick").critical is True

    def test_the_tick_keeps_running_while_stopping(self):
        supervisor = build_supervisor(ingestor=Ingestor())
        supervisor.start(T0)
        supervisor.stop()
        results = supervisor.run_due(T0 + 5_000)
        assert [r.name for r in results] == ["tick"]

    def test_the_tick_actually_ages_the_core(self):
        from app.core.presence_fsm import PresenceState, ZoneKind, ZoneSighting

        ingestor = Ingestor()
        presence = ingestor.state.presence.get("gp-1")
        presence.observe(ZoneSighting(ts_ms=T0, zone_id="floor-1",
                                      zone_kind=ZoneKind.FLOOR,
                                      camera_id="cam-1"))
        presence.track_lost(T0 + 1_000)

        supervisor = build_supervisor(ingestor=ingestor)
        supervisor.run_due(T0 + 600_000)
        assert presence.state is PresenceState.LOST


class TestHealth:
    def test_a_clean_supervisor_is_not_degraded(self):
        job, _ = counting_job()
        supervisor = Supervisor(jobs=[job])
        supervisor.start(T0)
        supervisor.run_due(T0)
        health = supervisor.health(T0)
        assert health["degraded"] is False
        assert health["running"] is True

    def test_a_failing_job_degrades_the_supervisor(self):
        broken, _ = counting_job("broken", fails=True)
        supervisor = Supervisor(jobs=[broken])
        supervisor.start(T0)
        supervisor.run_due(T0)
        health = supervisor.health(T0)
        assert health["degraded"] is True
        assert health["unhealthy_jobs"] == ["broken"]

    def test_never_succeeded_is_distinguished_from_succeeded_long_ago(self):
        # A misconfiguration and an outage need different people.
        never, _ = counting_job("never", fails=True)
        once, _ = counting_job("once")
        supervisor = Supervisor(jobs=[never, once])
        supervisor.start(T0)
        supervisor.run_due(T0)
        health = supervisor.health(T0 + 600_000)
        assert health["jobs"]["never"]["never_succeeded"] is True
        assert health["jobs"]["once"]["never_succeeded"] is False
        assert health["jobs"]["once"]["stale_ms"] == 600_000

    def test_uptime_is_reported(self):
        supervisor = Supervisor(jobs=[counting_job()[0]])
        supervisor.start(T0)
        assert supervisor.health(T0 + 90_000)["uptime_ms"] == 90_000


class TestAssembly:
    def test_only_the_supplied_jobs_are_registered(self):
        supervisor = build_supervisor(ingestor=Ingestor())
        assert [j.name for j in supervisor.jobs] == ["tick"]

    def test_each_optional_job_is_added_when_given(self):
        class Stub:
            def __init__(self):
                self.stats = type("S", (), {"messages_read": 0})()

            def poll_once(self, now_ms):
                return 0

            def claim_stale(self, now_ms):
                return 0

            def flush(self, now_ms):
                return 0

        stub = Stub()
        supervisor = build_supervisor(
            ingestor=Ingestor(), consumer=stub, replicator=stub,
            sync_runner=lambda now_ms: None)
        assert [j.name for j in supervisor.jobs] == [
            "tick", "consume", "replicate", "sync"]

    def test_the_intervals_encode_what_each_job_is_for(self):
        supervisor = build_supervisor(
            ingestor=Ingestor(), sync_runner=lambda now_ms: None)
        # A person becoming stale is a second-scale event; a floor plan
        # changing mid-drill is not a thing that happens.
        assert supervisor.job("tick").interval_ms <= 1_000
        assert supervisor.job("sync").interval_ms >= 60_000


class TestServing:
    def test_it_runs_until_it_is_asked_to_stop(self):
        job, calls = counting_job(interval_ms=0)
        supervisor = Supervisor(jobs=[job])
        ticks = {"now": T0}

        def clock():
            ticks["now"] += 100
            return ticks["now"]

        iterations = serve(supervisor, clock=clock, sleep=lambda _: None,
                           max_iterations=5)
        assert iterations == 5
        assert len(calls) == 5

    def test_stopping_ends_the_loop(self):
        job, calls = counting_job(interval_ms=0)
        supervisor = Supervisor(jobs=[job])
        state = {"n": 0}

        def run(now_ms):
            state["n"] += 1
            if state["n"] >= 3:
                supervisor.stop()

        job.run = run
        iterations = serve(supervisor, clock=lambda: T0,
                           sleep=lambda _: None, max_iterations=100)
        assert iterations == 3

    def test_the_sleep_is_capped_so_a_stop_is_noticed_promptly(self):
        # Otherwise a stop request waits for the longest interval in the
        # schedule, which is five minutes.
        job, _ = counting_job(interval_ms=300_000)
        supervisor = Supervisor(jobs=[job])
        slept = []
        serve(supervisor, clock=lambda: T0, sleep=slept.append,
              max_iterations=2)
        assert all(duration <= 0.25 for duration in slept)
