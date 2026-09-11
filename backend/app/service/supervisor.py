"""Running the edge node's background work, and surviving parts of it failing.

Four jobs, on four clocks:

    tick          age tracks and identities on wall clock
    consume       drain the event stream into the fold
    replicate     flush the outbox toward central
    sync          refresh geometry from VisionTrack

The scheduling is driven by an injected clock and exposed as `run_due(now_ms)`,
so every property below is tested without sleeping. A supervisor whose only test
is "start it and watch for ten seconds" is a supervisor nobody can reason about.

Three rules shape it.

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
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable


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
    last_ok_ms: int | None = None
    consecutive_failures: int = 0
    runs: int = 0
    failures: int = 0
    last_error: str | None = None

    def due_at(self) -> int:
        if self.last_run_ms is None:
            return 0
        return self.last_run_ms + self._effective_interval()

    def _effective_interval(self) -> int:
        if self.critical or self.consecutive_failures == 0:
            return self.interval_ms
        # Exponential, capped. A dead dependency should not be hammered, and a
        # recovering one should not wait minutes to be noticed.
        backoff = self.interval_ms * (2 ** min(self.consecutive_failures, 6))
        return min(backoff, self.max_backoff_ms)

    def is_due(self, now_ms: int) -> bool:
        return now_ms >= self.due_at()

    def execute(self, now_ms: int) -> "JobResult":
        self.last_run_ms = now_ms
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
        self.last_error = None
        return JobResult(name=self.name, ok=True, value=value)

    @property
    def is_healthy(self) -> bool:
        return self.consecutive_failures == 0

    def staleness_ms(self, now_ms: int) -> int | None:
        """How long since this job last succeeded."""
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

    def run_due(self, now_ms: int) -> list:
        """Run every job that is due. Returns what happened.

        A job that raises is recorded and the next one still runs. That is the
        whole point: a geometry sync that cannot reach VisionTrack must not stop
        the consumer draining events during a drill.
        """
        results = []
        for job in self.jobs:
            if self.stopping and not job.critical:
                continue
            if job.is_due(now_ms):
                results.append(job.execute(now_ms))
        return results

    def next_due_ms(self, now_ms: int) -> int:
        """How long to sleep before anything needs doing."""
        if not self.jobs:
            return 1_000
        soonest = min(job.due_at() for job in self.jobs)
        return max(0, soonest - now_ms)

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
    replicate_interval_ms: int = 15_000,
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
        supervisor.add(Job(name="replicate", interval_ms=replicate_interval_ms,
                           run=lambda now_ms: replicator.flush(now_ms)))

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
) -> int:
    """Drive the supervisor until it is asked to stop.

    The clock and sleep are injected so this can be driven deterministically in
    a test. `max_iterations` bounds a test run; production passes None.
    """
    clock = clock or (lambda: int(time.time() * 1000))
    sleep = sleep or time.sleep

    supervisor.start(clock())
    iterations = 0

    while not supervisor.stopping:
        if max_iterations is not None and iterations >= max_iterations:
            break
        now_ms = clock()
        supervisor.run_due(now_ms)
        iterations += 1

        wait_ms = supervisor.next_due_ms(clock())
        if wait_ms > 0:
            # Capped so a stop request is noticed promptly rather than after
            # the longest interval in the schedule.
            sleep(min(wait_ms, 250) / 1000)

    return iterations
