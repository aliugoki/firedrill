"""Run the Phase 2 calibration harness and print the report.

The harness has existed since Phase 2 with no way to run it: `certify` and
`harvest` are the deliverable and could only be driven from a test, so the
thresholds a site would install had no command that produced them.

    cd backend && ../.venv/bin/python scripts/calibrate.py
    cd backend && ../.venv/bin/python scripts/calibrate.py --dataset labelled.json

With no dataset it runs against the simulator. That exercises the arithmetic
and **cannot certify anything**: simulated scores come from a model of a face
matcher rather than from one, so a threshold tuned on them is tuned to the
model. The report says so and the exit status is non-zero, which is the point
of running it -- a build that silently produced "calibrated" numbers from
synthetic data is the failure this whole module is arranged to prevent.

With `--dataset` it reads labelled rows produced by whatever tool a human used
to label recorded footage. Exit status is zero only when the thresholds are
certifiable, so this can gate a deployment.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.calibration.dataset import (                             # noqa: E402
    MalformedDataset,
    load,
    split,
)
from app.calibration.from_simulator import harvest                # noqa: E402
from app.calibration.report import certify                        # noqa: E402
from app.calibration.sweep import Sweep                           # noqa: E402
from app.core.identity_fsm import PROVISIONAL_CONFIG              # noqa: E402
from app.simulator.agents import build_population, walk_all       # noqa: E402
from app.simulator.engine import DrillPlan                        # noqa: E402
from app.simulator.injections import REALISTIC                    # noqa: E402
from app.simulator.site import default_site                       # noqa: E402

ALARM_MS = 1_788_000_000_000


def simulated(people: int, seed: int):
    """A drill big enough to clear the readiness floors.

    Under the realistic injection profile rather than a clean one: a corpus
    with no poor angles, no motion blur and no lookalikes would choose
    thresholds that only work on a building where nothing goes wrong.
    """
    site = default_site()
    agents = build_population(site, count=people, seed=seed)
    walk_all(agents, site, alarm_ms=ALARM_MS, seed=seed)
    return harvest(DrillPlan(site=site, agents=agents, alarm_ms=ALARM_MS,
                             injections=REALISTIC, seed=seed))


def main(argv: list | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", help="JSON file of labelled observations")
    parser.add_argument("--ceiling", type=float, default=0.01,
                        help="false-accept ceiling the operating point must "
                             "hold (default 0.01)")
    parser.add_argument("--people", type=int, default=300,
                        help="simulated population, when no dataset is given")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--validate-fraction", type=float, default=0.3,
                        help="share of people held out of tuning")
    args = parser.parse_args(argv)

    if args.dataset:
        try:
            calibration_set = load(args.dataset)
        except MalformedDataset as exc:
            # Named on stderr and refused. A dataset the harness half
            # understood would produce a report that looks like every other
            # report, which is worse than no report.
            print(f"cannot read the dataset: {exc}", file=sys.stderr)
            return 2
    else:
        calibration_set = simulated(args.people, args.seed)

    sweep = Sweep(split=split(calibration_set,
                              validate_fraction=args.validate_fraction),
                  base_config=PROVISIONAL_CONFIG).run()
    report = certify(sweep, false_accept_ceiling=args.ceiling)
    print("\n".join(report.render()))

    # Non-zero when the thresholds may not be installed, so this can gate a
    # deployment rather than being a thing somebody reads and interprets.
    return 0 if report.certified else 1


if __name__ == "__main__":
    raise SystemExit(main())
