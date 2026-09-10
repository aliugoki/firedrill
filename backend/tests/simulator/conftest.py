"""Shared drill fixtures. Populations are cached per shape: walking 500 agents
is the expensive part of a run and it is deterministic, so it is done once."""

from __future__ import annotations

import pytest

from app.simulator.agents import build_population, walk_all
from app.simulator.engine import DrillPlan, run_drill
from app.simulator.injections import Injections
from app.simulator.site import default_site

ALARM_MS = 1_788_000_000_000

_POPULATIONS: dict[tuple, list] = {}


@pytest.fixture(scope="session")
def site():
    return default_site()


def population(site, count: int, seed: int):
    key = (count, seed)
    cached = _POPULATIONS.get(key)
    if cached is None:
        agents = build_population(site, count=count, seed=seed)
        walk_all(agents, site, alarm_ms=ALARM_MS, seed=seed)
        _POPULATIONS[key] = agents
        cached = agents
    # Agents are read-only after walking, so the cache is safe to share.
    return cached


def drill(site, injections: Injections, *, count: int = 120, seed: int = 20260910):
    agents = population(site, count, seed)
    plan = DrillPlan(site=site, agents=agents, alarm_ms=ALARM_MS,
                     injections=injections, seed=seed)
    return agents, run_drill(plan)


def truly_reached_assembly(agents) -> set:
    """Ground truth. Nothing in the core may read this; only tests may."""
    return {a.person_ref for a in agents if a.reached_assembly}
