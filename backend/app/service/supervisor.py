"""Running the edge node's background work, and surviving parts of it failing.

Four jobs, on four clocks:

    tick          age tracks and identities on wall clock
    consume       drain the event stream into the fold
    replicate     flush the outbox toward central
    sync          refresh geometry from VisionTrack

The scheduling is driven by an injected clock and exposed as `run_due(now_ms)`,
so every property below is tested without sleeping. A supervisor whose only test
is "start it and watch for ten seconds" is a supervisor nobody can reason about.

**Two clocks, and the difference is the point.** Scheduling runs on a monotonic
source; the `now_ms` handed to a job is wall clock, because it has to be
comparable with the timestamps VisionTrack puts on its observations and with
the evidence already in the ledger. Deciding *when* to run on wall clock was a
freeze waiting for an edge node's first NTP correction -- see rule four.

Four rules shape it.

**One failing job never stops another.** If the geometry sync cannot reach
VisionTrack, the consumer must keep draining. If replication cannot reach
central, the drill must keep running. Each job is isolated, and a job that
raises is recorded and rescheduled rather than propagated.

**The tick must never stop.** Presence ages on wall clock: a track goes stale,
a person becomes LOST, an identity expires. If the tick stops, none of that
happens and the board freezes into optimism — every person keeps the last state
they had, and the screen looks calm while nothing is being reassessed. It is the
only job that is never backed off.

**Backoff, but never give up.** A job failing repeatedly slows down so it stops
hammering a dead dependency, and keeps trying, because the dependency coming
back must not need a human to notice.

**A clock correction is not a schedule.** This runs on an edge node with no
Internet, which is a node whose clock is wrong at boot and gets corrected the
moment a network appears. Scheduling on wall clock meant a backward correction
of forty minutes stopped every job for forty minutes -- `now_ms >= last_run_ms
+ interval` is false while the clock is catching up -- and stopped them
silently, because a job that never runs never fails, so the supervisor went on
reporting itself healthy. The tick is the job that must never stop, and this
was the way to stop it that nothing in the suite could see.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


#: How often the edge flushes to central. Named because it is also the producer
#: half of the recovery point objective -- events made inside this window exist
#: only in memory -- and the health report has to quote the same number the
#: supervisor actually uses, not a copy of it that drifts.
DEFAULT_REPLICATE_INTERVAL_MS = 15_000


class CentralUnreachable(RuntimeError):
    """Central has not answered for several flushes in a row.

    Distinct from `ReplicationBlocked`, which is a credential central refused
    and which no amount of waiting fixes. This one usually does fix itself,
    and the job keeps retrying with backoff -- it is raised so that the health
    endpoint stops claiming everything is fine while the replica falls behind.
    """


class ReplicationBlocked(RuntimeError):
    """Replication cannot proceed and waiting will not change that.

    Its own class so the supervisor's `last_error` names the one failure on
    this node that no amount of retrying resolves.
    """


#: How far the two clocks may disagree before it counts as a correction rather
#: than as jitter. Wall clock and `time.monotonic` drift apart by microseconds
#: per second and both are read a few instructions apart, so a small
#: disagreement means nothing. Two seconds is comfortably above that and
#: comfortably below the smallest correction worth reporting -- and it is the
#: only number here, so it is named rather than written into a comparison.
CLOCK_STEP_TOLERANCE_MS = 2_000


@dataclass(frozen=True, slots=True)
class ClockStep:
    """A wall clock that moved by more than time did.

    `delta_ms` is signed, and the sign is the whole finding.

    **Backwards** is the dangerous direction and does not heal itself. Every
    `last_seen_ms` already recorded sits in what is now the future, so for the
    length of the step every one of those people looks as though they were seen
    a moment ago. Nobody ages, nobody becomes LOST, and the board holds whatever
    it last believed -- which is the false optimism invariant 8 exists to
    forbid. It blinds the system for exactly as long as the step.

    **Forwards** inflates every age at once and the board over-reports people
    as unobserved, which is the safe direction to be wrong in. It is still
    wrong: two hundred people turning orange because NTP fired is not a state
    anybody should reason from, and an evacuation timed across a forward
    correction is not a measurement of an evacuation -- which matters here more
    than most places, because this system's whole claim is a number that was
    measured.

    So both directions blind, and they differ in how long. Backwards lasts
    exactly as long as the step: wall clock has to climb back past the readings
    taken before it before any of them means anything again. Forwards lasts
    until the next tick, by which point every age has been recomputed against
    the new clock and the board is at least self-consistent.
    """

    at_ms: int
    """Wall clock after the step, which is what a human reading a log has."""
    delta_ms: int
    monotonic_ms: int
    #: The floor on a forward step's blind window. Not zero: a zero-length
    #: interval is invisible to `was_degraded_at`, and the whole reason this is
    #: recorded as an interval is so a post-drill report can answer "was the
    #: system trustworthy when it said that?".
    settle_ms: int = 1_000

    @property
    def backwards(self) -> bool:
        return self.delta_ms < 0

    @property
    def blind_for_ms(self) -> int:
        return abs(self.delta_ms) if self.backwards else self.settle_ms

    def describe(self) -> str:
        direction = "backwards" if self.backwards else "forwards"
        return (f"the system clock moved {direction} by "
                f"{abs(self.delta_ms) // 1000}s; ages cannot be trusted for "
                f"{self.blind_for_ms // 1000}s")


class ClockWatch:
    """Compares wall clock against a clock that cannot be corrected.

    Time passing shows up in both. A correction shows up in only one, which is
    what makes it detectable at all -- and detecting it is the only way this
    node can tell "nothing has happened for forty minutes" from "somebody fixed
    the clock".
    """

    def __init__(self, tolerance_ms: int = CLOCK_STEP_TOLERANCE_MS):
        self.tolerance_ms = tolerance_ms
        self._wall: int | None = None
        self._tick: int | None = None

    def observe(self, now_ms: int, tick_ms: int) -> ClockStep | None:
        previous_wall, previous_tick = self._wall, self._tick
        self._wall, self._tick = now_ms, tick_ms
        if previous_wall is None:
            # The first reading establishes the pair. There is nothing to
            # compare it against, and a node whose clock was wrong at boot has
            # not stepped -- it is merely wrong, which NTP is about to fix and
            # this will then report.
            return None

        drift = (now_ms - previous_wall) - (tick_ms - previous_tick)
        if abs(drift) <= self.tolerance_ms:
            return None
        return ClockStep(at_ms=now_ms, delta_ms=drift, monotonic_ms=tick_ms)


@dataclass
class Job:
    """One piece of background work and its schedule."""

    name: str
    interval_ms: int
    run: Callable[[int], object]
    critical: bool = False
    """A critical job is never backed off. Only the tick is critical: slowing it
    down under load is exactly when a stale track most needs ageing."""

    max_backoff_ms: int = 60_000

    last_run_ms: int | None = None
    """Wall clock, and reported rather than scheduled on. It is what a health
    endpoint shows a human, and a human reads wall clock."""
    last_ok_ms: int | None = None
    #: The monotonic readings the schedule is actually built from. Separate
    #: fields rather than one clock reinterpreted, because the two answer
    #: different questions and conflating them is the defect this pair fixes.
    last_run_tick: int | None = None
    last_ok_tick: int | None = None
    consecutive_failures: int = 0
    runs: int = 0
    failures: int = 0
    last_error: str | None = None

    def due_at(self) -> int:
        """The monotonic reading at which this job may run again."""
        if self.last_run_tick is None:
            return 0
        return self.last_run_tick + self._effective_interval()

    def _effective_interval(self) -> int:
        if self.critical or self.consecutive_failures == 0:
            return self.interval_ms
        # Exponential, capped. A dead dependency should not be hammered, and a
        # recovering one should not wait minutes to be noticed.
        backoff = self.interval_ms * (2 ** min(self.consecutive_failures, 6))
        return min(backoff, self.max_backoff_ms)

    def is_due(self, tick_ms: int) -> bool:
        """Asked of the monotonic reading, never of wall clock."""
        return tick_ms >= self.due_at()

    def execute(self, now_ms: int, tick_ms: int | None = None) -> "JobResult":
        """Run the job at wall-clock `now_ms`, scheduled at `tick_ms`.

        `tick_ms` defaults to `now_ms` so a caller that has only one clock --
        every existing test, and anything driving this deterministically --
        behaves exactly as before.
        """
        tick_ms = now_ms if tick_ms is None else tick_ms
        self.last_run_ms = now_ms
        self.last_run_tick = tick_ms
        self.runs += 1
        try:
            value = self.run(now_ms)
        except Exception as exc:
            self.failures += 1
            self.consecutive_failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"
            return JobResult(name=self.name, ok=False, error=self.last_error)

        self.consecutive_failures = 0
        self.last_ok_ms = now_ms
        self.last_ok_tick = tick_ms
        self.last_error = None
        return JobResult(name=self.name, ok=True, value=value)

    @property
    def is_healthy(self) -> bool:
        return self.consecutive_failures == 0

    def staleness_ms(self, now_ms: int, tick_ms: int | None = None) -> int | None:
        """How long since this job last succeeded.

        Measured on the monotonic reading when there is one. The wall-clock
        answer was wrapped in `max(0, ...)`, so a clock that had stepped
        backwards reported every job as having succeeded this instant -- the
        one number that would have revealed a frozen supervisor, reading
        perfect health for the length of the step.
        """
        if tick_ms is not None and self.last_ok_tick is not None:
            return max(0, tick_ms - self.last_ok_tick)
        if self.last_ok_ms is None:
            return None
        return max(0, now_ms - self.last_ok_ms)


@dataclass(frozen=True, slots=True)
class JobResult:
    name: str
    ok: bool
    value: object = None
    error: str | None = None


@dataclass
class Supervisor:
    """Runs jobs when they are due, and keeps going when they fail."""

    jobs: list = field(default_factory=list)
    started_ms: int | None = None
    stopping: bool = False

    def add(self, job: Job) -> Job:
        if any(existing.name == job.name for existing in self.jobs):
            raise ValueError(f"a job named {job.name} is already registered")
        self.jobs.append(job)
        return job

    def job(self, name: str) -> Job:
        for job in self.jobs:
            if job.name == name:
                return job
        raise KeyError(name)

    def start(self, now_ms: int) -> None:
        self.started_ms = now_ms
        self.stopping = False

    def run_due(self, now_ms: int, tick_ms: int | None = None) -> list:
        """Run every job that is due. Returns what happened.

        `now_ms` is wall clock and is what each job is handed. `tick_ms` is the
        monotonic reading the schedule is decided from, and defaults to
        `now_ms` for a caller that has only one clock.

        A job that raises is recorded and the next one still runs. That is the
        whole point: a geometry sync that cannot reach VisionTrack must not stop
        the consumer draining events during a drill.
        """
        tick_ms = now_ms if tick_ms is None else tick_ms
        results = []
        for job in self.jobs:
            if self.stopping and not job.critical:
                continue
            if job.is_due(tick_ms):
                results.append(job.execute(now_ms, tick_ms))
        return results

    def next_due_ms(self, now_ms: int, tick_ms: int | None = None) -> int:
        """How long to sleep before anything needs doing.

        Asked of the monotonic reading. Answered from wall clock, a backward
        correction returned the size of the correction, and the loop below
        slept through an evacuation.
        """
        tick_ms = now_ms if tick_ms is None else tick_ms
        if not self.jobs:
            return 1_000
        soonest = min(job.due_at() for job in self.jobs)
        return max(0, soonest - tick_ms)

    def health(self, now_ms: int) -> dict:
        """What `/healthz` reports about the background work.

        A job that has never succeeded is distinguished from one that succeeded
        long ago: the first is a misconfiguration, the second is an outage, and
        they need different people.
        """
        jobs = {}
        for job in self.jobs:
            jobs[job.name] = {
                "healthy": job.is_healthy,
                "runs": job.runs,
                "failures": job.failures,
                "consecutive_failures": job.consecutive_failures,
                "never_succeeded": job.last_ok_ms is None,
                "stale_ms": job.staleness_ms(now_ms),
                "last_error": job.last_error,
            }
        unhealthy = [name for name, state in jobs.items() if not state["healthy"]]
        return {
            "running": self.started_ms is not None and not self.stopping,
            "uptime_ms": (now_ms - self.started_ms
                          if self.started_ms is not None else 0),
            "degraded": bool(unhealthy),
            "unhealthy_jobs": unhealthy,
            "jobs": jobs,
        }

    def stop(self) -> None:
        """Ask the loop to wind down. Critical jobs keep running until it exits."""
        self.stopping = True


def build_supervisor(
    *,
    ingestor,
    consumer=None,
    replicator=None,
    sync_runner: Callable[[int], object] | None = None,
    retention_check: Callable[[int], object] | None = None,
    tick_interval_ms: int = 1_000,
    consume_interval_ms: int = 250,
    replicate_interval_ms: int = DEFAULT_REPLICATE_INTERVAL_MS,
    sync_interval_ms: int = 300_000,
    retention_interval_ms: int = 3_600_000,
) -> Supervisor:
    """Assemble the edge node's jobs. Anything not supplied is simply absent.

    The intervals encode what each job is for. The tick runs every second
    because a person becoming stale is a second-scale event. Consumption runs
    four times a second because the board is meant to be live. Replication runs
    every fifteen seconds, which is also the producer half of the recovery point
    objective. The sync runs every five minutes, because a floor plan changing
    mid-drill is not a thing that happens.
    """
    supervisor = Supervisor()

    # Critical: presence and identity age on wall clock. If this stops, nobody
    # ever becomes LOST and the board freezes into optimism.
    supervisor.add(Job(name="tick", interval_ms=tick_interval_ms,
                       run=lambda now_ms: ingestor.tick(now_ms),
                       critical=True))

    if consumer is not None:
        def consume(now_ms: int) -> int:
            applied = consumer.poll_once(now_ms)
            # Reclaim what a dead consumer stranded, on a slower cadence than
            # the read itself: it is a recovery path, not a hot loop.
            if consumer.stats.messages_read % 40 == 0:
                applied += consumer.claim_stale(now_ms)
            return applied

        supervisor.add(Job(name="consume", interval_ms=consume_interval_ms,
                           run=consume))

    if replicator is not None:
        def replicate(now_ms: int) -> int:
            """Flush to central, and fail loudly when flushing cannot work.

            `Replicator.flush` returns zero when the credential has been
            refused, deliberately: it stops the pointless retrying that would
            hide a problem waiting had no chance of fixing. But zero is also
            what a quiet drill returns, so this job succeeded every fifteen
            seconds forever and the supervisor reported it healthy while
            nothing had reached central since the node booted.

            Raising instead puts the reason in `last_error`, marks the job
            unhealthy, and backs the interval off -- all three of which are the
            right response to a failure only a human can clear.
            """
            if replicator.is_blocked:
                raise ReplicationBlocked(replicator.blocked_reason
                                         or "central refused the credential")
            sent = replicator.flush(now_ms)
            # The same reasoning as above, for the far commoner failure. A link
            # that is simply down also makes `flush` return zero, so this job
            # went on succeeding every fifteen seconds while nothing had
            # reached central for ten minutes -- measured: 39 consecutive
            # failed flushes, `job.failures` 0, `job.is_healthy` true.
            #
            # Raised only once the link has been down long enough to mean it,
            # because a job that goes unhealthy on one dropped packet is a job
            # whose health nobody reads.
            if replicator.is_offline:
                raise CentralUnreachable(
                    replicator.stats.last_failure_reason
                    or "central is not answering")
            return sent

        supervisor.add(Job(name="replicate", interval_ms=replicate_interval_ms,
                           run=replicate))

    if sync_runner is not None:
        supervisor.add(Job(name="sync", interval_ms=sync_interval_ms,
                           run=sync_runner))

    if retention_check is not None:
        # Hourly, because the shortest retention class is measured in hours and
        # the check is a scan of what is held rather than work. `retention.py`
        # says a policy nobody checks is a promise; this is what makes it a
        # control, and it reports through `/healthz` either way.
        supervisor.add(Job(name="retention", interval_ms=retention_interval_ms,
                           run=retention_check))

    return supervisor


def serve(
    supervisor: Supervisor, *, clock: Callable[[], int] | None = None,
    sleep: Callable[[float], None] | None = None,
    max_iterations: int | None = None,
    monotonic: Callable[[], int] | None = None,
    on_clock_step: Callable[[object], object] | None = None,
) -> int:
    """Drive the supervisor until it is asked to stop.

    Two clocks. `clock` is wall clock and is what each job is handed, because
    a job's `now_ms` has to be comparable with the timestamps VisionTrack puts
    on its observations. `monotonic` is what the schedule is decided from, and
    it is the one that cannot move: `time.monotonic` counts forward regardless
    of what NTP does to the wall clock.

    Both are injected so this can be driven deterministically in a test.
    `max_iterations` bounds a test run; production passes None.

    `on_clock_step` is called with a `ClockStep` whenever the two clocks
    disagree about how much time has passed. That disagreement is an
    infrastructure failure, and invariant 8 says an infrastructure failure is
    reported rather than absorbed.
    """
    clock = clock or (lambda: int(time.time() * 1000))
    monotonic = monotonic or (lambda: int(time.monotonic() * 1000))
    sleep = sleep or time.sleep

    watch = ClockWatch()
    supervisor.start(clock())
    iterations = 0

    while not supervisor.stopping:
        if max_iterations is not None and iterations >= max_iterations:
            break
        now_ms = clock()
        tick_ms = monotonic()

        step = watch.observe(now_ms, tick_ms)
        if step is not None and on_clock_step is not None:
            on_clock_step(step)

        supervisor.run_due(now_ms, tick_ms)
        iterations += 1

        wait_ms = supervisor.next_due_ms(clock(), monotonic())
        if wait_ms > 0:
            # Capped so a stop request is noticed promptly rather than after
            # the longest interval in the schedule.
            sleep(min(wait_ms, 250) / 1000)

    return iterations
