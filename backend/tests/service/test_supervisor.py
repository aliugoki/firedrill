"""The background scheduler, driven by an injected clock rather than by waiting.

A supervisor whose only test is "start it and watch for ten seconds" is a
supervisor nobody can reason about.
"""

from __future__ import annotations

import pytest

from app.ingest.ingestor import Ingestor
from app.ingest.health import BLINDING, Component
from app.service.supervisor import (
    ClockStep, ClockWatch, Job, Supervisor, build_supervisor, serve,
)

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


class TestAClockCorrectedUnderTheNodesFeet:
    """This runs on an edge node with no Internet, which is a node whose clock
    is wrong at boot and gets stepped the moment a network appears.

    Scheduling on wall clock made that stop every job for the length of the
    correction, silently -- a job that never runs never fails, so the
    supervisor reported itself healthy throughout, and `staleness_ms` wrapped
    its answer in `max(0, ...)` so it reported zero.

    The tick is the job the module docstring says must never stop, and this was
    the way to stop it that nothing in the suite could see.
    """

    BACKWARDS = 40 * 60 * 1000

    def _stepping_clocks(self, delta_ms, after=3):
        """Wall clock that jumps by `delta_ms` once, and a clock that cannot."""
        state = {"wall": T0, "mono": 0, "reads": 0, "stepped": False}

        def monotonic():
            state["mono"] += 500
            state["wall"] += 500
            return state["mono"]

        def clock():
            state["reads"] += 1
            if state["reads"] > after and not state["stepped"]:
                state["stepped"] = True
                state["wall"] += delta_ms
            return state["wall"]

        return clock, monotonic, state

    def test_a_backward_correction_no_longer_stops_every_job(self):
        job, calls = counting_job(interval_ms=1_000, critical=True)
        supervisor = Supervisor(jobs=[job])
        clock, monotonic, _ = self._stepping_clocks(-self.BACKWARDS)

        serve(supervisor, clock=clock, monotonic=monotonic,
              sleep=lambda _: None, max_iterations=200)

        # Forty minutes of wall clock to catch up. On the old schedule this
        # was three.
        assert len(calls) > 50, len(calls)

    def test_and_the_freeze_was_invisible_before(self):
        """The same run, scheduled the way it used to be: one clock for both.

        Kept as a test rather than as a paragraph, because the fix is a pair of
        clocks and a reader has to be able to see what the second one buys.
        """
        job, calls = counting_job(interval_ms=1_000, critical=True)
        supervisor = Supervisor(jobs=[job])
        clock, monotonic, _ = self._stepping_clocks(-self.BACKWARDS)

        # `monotonic` also advances wall clock, so passing it as both is the
        # old single-clock behaviour exactly.
        serve(supervisor, clock=clock, monotonic=clock,
              sleep=lambda _: None, max_iterations=200)

        assert len(calls) < 10, "the old failure no longer reproduces"
        assert job.is_healthy, "and it reported itself healthy while frozen"

    def test_staleness_stops_reading_zero_on_a_clock_that_went_back(self):
        job, _ = counting_job(interval_ms=1_000)
        job.execute(T0, tick_ms=10_000)
        # Wall clock has since moved back an hour; the monotonic reading has
        # advanced thirty seconds.
        assert job.staleness_ms(T0 - 3_600_000, tick_ms=40_000) == 30_000
        # And without a monotonic reading it still answers, for every caller
        # that has only one clock.
        assert job.staleness_ms(T0 + 5_000) == 5_000

    def test_a_forward_correction_is_invisible_to_the_schedule(self):
        """The run count is the same with the jump as without it.

        Asserted as a comparison rather than against a number, because the
        number is a property of the fake clocks and the finding is that the
        jump does not change it. A schedule reading wall clock would have
        fired a burst the moment it leapt forty minutes.
        """
        def run(step_ms):
            job, calls = counting_job(interval_ms=1_000)
            supervisor = Supervisor(jobs=[job])
            clock, monotonic, _ = self._stepping_clocks(step_ms)
            serve(supervisor, clock=clock, monotonic=monotonic,
                  sleep=lambda _: None, max_iterations=40)
            return len(calls)

        assert run(self.BACKWARDS) == run(0)
        assert run(-self.BACKWARDS) == run(0)

    def test_the_step_is_reported_rather_than_absorbed(self):
        job, _ = counting_job(interval_ms=1_000)
        supervisor = Supervisor(jobs=[job])
        clock, monotonic, _ = self._stepping_clocks(-self.BACKWARDS)
        seen = []

        serve(supervisor, clock=clock, monotonic=monotonic,
              sleep=lambda _: None, max_iterations=200,
              on_clock_step=seen.append)

        assert len(seen) == 1, [s.describe() for s in seen]
        assert seen[0].backwards
        assert seen[0].delta_ms == pytest.approx(-self.BACKWARDS, abs=2_000)


class TestNoticingTheClockMoved:
    def test_ordinary_drift_is_not_a_correction(self):
        # Wall clock and a monotonic source are read a few instructions apart
        # and drift by microseconds. A watch that called that a correction
        # would report one every second.
        watch = ClockWatch()
        assert watch.observe(T0, 0) is None
        for n in range(1, 60):
            assert watch.observe(T0 + n * 1_000 + n, n * 1_000) is None

    def test_the_first_reading_is_never_a_step(self):
        # A node whose clock was wrong at boot has not stepped. It is merely
        # wrong, which NTP is about to fix, and that is what gets reported.
        assert ClockWatch().observe(0, 999_999) is None

    def test_it_catches_a_correction_in_either_direction(self):
        watch = ClockWatch()
        watch.observe(T0, 0)
        step = watch.observe(T0 + 500 - 3_600_000, 500)
        assert step is not None and step.backwards

        watch = ClockWatch()
        watch.observe(T0, 0)
        step = watch.observe(T0 + 500 + 3_600_000, 500)
        assert step is not None and not step.backwards

    def test_it_reports_a_correction_once_and_not_forever_after(self):
        # The pair is re-based on the post-step readings, so the next
        # comparison is an ordinary one. A watch that kept comparing against
        # the pre-step reading would report the same correction every second
        # until the drill ended.
        watch = ClockWatch()
        watch.observe(T0, 0)
        assert watch.observe(T0 + 500 - 3_600_000, 500) is not None
        for n in range(2, 20):
            assert watch.observe(T0 + n * 500 - 3_600_000, n * 500) is None


class TestWhatAStepCosts:
    def test_backwards_blinds_for_exactly_as_long_as_the_step(self):
        # Every `last_seen_ms` already recorded now sits in the future, so for
        # the length of the step everybody looks as though they were seen a
        # moment ago and nobody becomes LOST.
        step = ClockStep(at_ms=T0, delta_ms=-2_400_000, monotonic_ms=0)
        assert step.blind_for_ms == 2_400_000

    def test_forwards_blinds_only_until_the_next_tick(self):
        # The board over-reports people as unobserved, which is the safe
        # direction, and one tick later every age has been recomputed.
        step = ClockStep(at_ms=T0, delta_ms=2_400_000, monotonic_ms=0)
        assert step.blind_for_ms == 1_000

    def test_the_window_is_never_zero(self):
        # A zero-length interval is invisible to `was_degraded_at`, and the
        # reason this is an interval at all is so a post-drill report can
        # answer "was the system trustworthy when it said that?".
        for delta in (3_000, -3_000):
            assert ClockStep(at_ms=T0, delta_ms=delta,
                             monotonic_ms=0).blind_for_ms > 0

    def test_the_clock_is_a_blinding_component(self):
        # A correction does not stop a camera seeing. It stops ageing, and
        # ageing is how this system decides nobody has seen somebody.
        assert Component.CLOCK in BLINDING


class TestWindingDown:
    """While stopping, only critical jobs run.

    The skip is checked per job inside the pass, not once at the top, so a
    signal arriving part-way through a pass takes effect for the rest of that
    same pass. Presence and identity keep ageing; the stream read and the
    replication flush stop, and the outbox gets its last drain from `main`'s
    `finally` instead.
    """

    def build(self):
        from app.service.supervisor import Job, Supervisor

        ran = []
        supervisor = Supervisor()
        supervisor.add(Job(name="tick", interval_ms=1,
                           run=lambda now_ms: ran.append("tick"),
                           critical=True))
        supervisor.add(Job(name="sync", interval_ms=1,
                           run=lambda now_ms: ran.append("sync")))
        return supervisor, ran

    def test_everything_runs_while_it_is_running(self):
        supervisor, ran = self.build()
        supervisor.start(0)
        supervisor.run_due(1_000)
        assert ran == ["tick", "sync"]

    def test_only_the_critical_job_runs_while_stopping(self):
        supervisor, ran = self.build()
        supervisor.start(0)
        supervisor.stopping = True
        supervisor.run_due(1_000)
        assert ran == ["tick"]

    def test_starting_again_clears_the_stop(self):
        supervisor, ran = self.build()
        supervisor.stopping = True
        supervisor.start(0)
        supervisor.run_due(1_000)
        assert ran == ["tick", "sync"]

    def test_a_signal_part_way_through_a_pass_takes_effect_at_once(self):
        """The realistic arrival: a SIGTERM lands while jobs are running.

        `run_due` re-reads `stopping` for each job rather than once at the top,
        so the rest of that pass winds down too instead of completing as though
        nothing had happened.
        """
        from app.service.supervisor import Job, Supervisor, serve

        ran = []
        supervisor = Supervisor()
        # A signal arrives while the first iteration is running.
        supervisor.add(Job(
            name="tick", interval_ms=1, critical=True,
            run=lambda now_ms: (ran.append("tick"), supervisor.stop())[0]))
        supervisor.add(Job(name="sync", interval_ms=1,
                           run=lambda now_ms: ran.append("sync")))

        iterations = serve(supervisor, clock=lambda: 1_000,
                           sleep=lambda _seconds: None)

        # One pass, and `sync` was skipped within it: `tick` is critical and
        # set the stop while running, and the non-critical job after it in the
        # same pass did not run.
        assert iterations == 1
        assert ran == ["tick"]


class TestLookingUpAJob:
    def test_a_named_job_comes_back(self):
        from app.service.supervisor import Job, Supervisor

        supervisor = Supervisor()
        supervisor.add(Job(name="tick", interval_ms=1, run=lambda now_ms: None))
        assert supervisor.job("tick").name == "tick"

    def test_an_unknown_name_raises_rather_than_returning_none(self):
        # A None here would be used as a job and fail somewhere less obvious.
        from app.service.supervisor import Supervisor

        with pytest.raises(KeyError):
            Supervisor().job("nothing-like-this")

    def test_an_empty_supervisor_still_says_how_long_to_sleep(self):
        from app.service.supervisor import Supervisor

        assert Supervisor().next_due_ms(0) == 1_000
