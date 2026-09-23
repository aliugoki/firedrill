"""The edge process. `python -m app.service.main`.

Runs the background work: draining the event stream, ageing the core, flushing
to central, refreshing geometry. The API is a separate process by design — a
crash in one must not take the other with it, and an operator who can still
reach the board while ingest is restarting has more than one who cannot reach
anything.

Shutdown is graceful because the alternative loses evidence. On SIGTERM the
supervisor stops taking new work, the outbox is flushed one last time, and only
then does the process exit. A drill interrupted by a deploy should lose nothing.
"""

from __future__ import annotations

import logging
import signal
import sys

from app.service.edge import build_edge
from app.service.supervisor import serve

log = logging.getLogger("evac.edge")


def main(argv: list | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s  %(message)s")

    node = build_edge()

    log.info("edge node for site %s", node.site_id or "(unset)")
    for gap in node.gaps:
        # Loud, and at startup rather than at the first drill. A node that is
        # missing its roster should say so now, not when somebody presses start.
        log.warning("configuration gap: %s", gap)

    if node.geometry.has_geometry:
        for line in node.geometry.geometry.describe():
            log.info("%s", line)
    else:
        log.warning("no geometry: no zone can be resolved and nobody can "
                    "reach an assembly point")

    def request_stop(signum, _frame):
        log.info("signal %s received; winding down", signum)
        node.supervisor.stop()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    def clock_stepped(step) -> None:
        # Loud, because it is the one failure on this node that makes the board
        # more confident rather than less, and an edge node with no Internet is
        # exactly the node whose clock gets corrected under its own feet.
        log.warning("clock correction: %s", step.describe())
        node.ingestor.clock_stepped(step)

    log.info("running jobs: %s", ", ".join(j.name for j in node.supervisor.jobs))
    try:
        serve(node.supervisor, on_clock_step=clock_stepped)
    finally:
        # Last chance to get buffered events to central. A drill interrupted by
        # a deploy should lose nothing.
        if node.replicator is not None:
            drained = node.replicator.drain()
            log.info("flushed %s buffered event(s) on shutdown", drained)
            if node.replicator.backlog:
                log.warning(
                    "%s event(s) remain buffered; they survive on disk and "
                    "replay on restart", node.replicator.backlog)
        log.info("stopped")

    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    sys.exit(main())
