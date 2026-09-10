"""Simulated people, and the ground truth about where they actually were.

The simulator knows the truth. That is the whole point: a test can compare what
the system concluded against what really happened, and assert the one thing that
must never occur — someone marked ACCOUNTED who never reached an assembly point.

Agents are given the properties that make accountability hard rather than a
uniform crowd:

  * a **reaction delay**, so people do not all start at the alarm;
  * a **walking speed**, including slow movers and people needing assistance;
  * a **look-alike partner** for a few pairs, so the identity gate has to earn
    its margin against a genuinely similar face;
  * a few who **never leave** (asleep at a desk, headphones on, in the basement
    plant room) — the people the whole system exists to find;
  * a few **genuinely absent** from the building, who must never become phantom
    missing people;
  * **visitors and contractors** with no gallery entry, who can only be
    accounted for by a warden.

Everything is driven by a seeded RNG, so a failing test reproduces exactly.
"""

from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from enum import Enum

from app.simulator.site import Site, Zone
from app.core.presence_fsm import ZoneKind

Point = tuple[float, float]


def stable_seed(*parts) -> int:
    """A seed derived from strings that is the same in every process.

    `hash()` on a str is salted per interpreter by PYTHONHASHSEED, so
    `random.Random(hash((seed, name)))` produces different streams on different
    runs. The simulator claimed to be deterministic for a given seed and was
    not: two runs of the same drill produced different trajectories, and a test
    that failed in one process passed in the next. A failing run has to
    reproduce, so the digest is taken explicitly.
    """
    material = "|".join(str(p) for p in parts).encode()
    return int.from_bytes(hashlib.sha256(material).digest()[:4], "big")


class Behaviour(str, Enum):
    EVACUATES = "EVACUATES"
    SLOW = "SLOW"
    NEEDS_ASSISTANCE = "NEEDS_ASSISTANCE"
    NEVER_LEAVES = "NEVER_LEAVES"
    ABSENT_FROM_SITE = "ABSENT_FROM_SITE"
    WANDERS_THEN_LEAVES = "WANDERS_THEN_LEAVES"
    LEAVES_ASSEMBLY_EARLY = "LEAVES_ASSEMBLY_EARLY"


@dataclass(frozen=True, slots=True)
class Waypoint:
    ts_ms: int
    floor_id: str
    point: Point
    zone_id: str | None
    zone_kind: ZoneKind | None


@dataclass
class Agent:
    """One simulated person, with their true trajectory."""

    person_ref: str
    display_name: str
    home_floor_id: str
    behaviour: Behaviour
    emp_id: str | None = None
    has_gallery_entry: bool = True
    is_visitor: bool = False
    target_assembly: str | None = None
    lookalike_of: str | None = None
    reaction_ms: int = 0
    speed: float = 1.0
    department: str | None = None
    trajectory: list[Waypoint] = field(default_factory=list)

    @property
    def reached_assembly(self) -> bool:
        """Ground truth: did this person actually get to an assembly zone?"""
        return any(w.zone_kind is ZoneKind.ASSEMBLY for w in self.trajectory)

    @property
    def assembly_arrival_ms(self) -> int | None:
        for waypoint in self.trajectory:
            if waypoint.zone_kind is ZoneKind.ASSEMBLY:
                return waypoint.ts_ms
        return None

    @property
    def was_in_building(self) -> bool:
        return self.behaviour is not Behaviour.ABSENT_FROM_SITE

    def at(self, ts_ms: int) -> Waypoint | None:
        """Where the person truly was at a moment."""
        found = None
        for waypoint in self.trajectory:
            if waypoint.ts_ms <= ts_ms:
                found = waypoint
            else:
                break
        return found


#: Behaviours a population must contain, however small it is, and how many.
#:
#: A mix expressed only as probabilities silently omits its rarest cases at
#: small population sizes: 120 agents at 1.5% produced zero people who never
#: leave, and a suite testing "someone still at their desk is never marked safe"
#: passed without ever containing one. These are guaranteed instead.
GUARANTEED: dict[Behaviour, int] = {
    Behaviour.NEVER_LEAVES: 3,
    Behaviour.ABSENT_FROM_SITE: 2,
    Behaviour.LEAVES_ASSEMBLY_EARLY: 2,
    Behaviour.NEEDS_ASSISTANCE: 2,
    Behaviour.SLOW: 3,
    Behaviour.WANDERS_THEN_LEAVES: 2,
}

#: Behaviour mix. Chosen so a default run contains every hard case at least
#: once at 500 agents, including the ones that must never be lost in the noise.
DEFAULT_MIX: dict[Behaviour, float] = {
    Behaviour.EVACUATES: 0.72,
    Behaviour.SLOW: 0.12,
    Behaviour.NEEDS_ASSISTANCE: 0.03,
    Behaviour.WANDERS_THEN_LEAVES: 0.07,
    Behaviour.LEAVES_ASSEMBLY_EARLY: 0.03,
    Behaviour.NEVER_LEAVES: 0.015,
    Behaviour.ABSENT_FROM_SITE: 0.025,
}

_FIRST = ["Ayesha", "Bilal", "Chen", "Dania", "Emeka", "Farah", "Gulzar", "Hina",
          "Imran", "Jamila", "Kashif", "Laiba", "Mustafa", "Nadia", "Omar",
          "Parveen", "Qasim", "Rabia", "Saad", "Tahira", "Usman", "Verda",
          "Waleed", "Yusra", "Zain"]
_LAST = ["Khan", "Ahmed", "Wei", "Rauf", "Okafor", "Siddiqui", "Baig", "Malik",
         "Chaudhry", "Iqbal", "Sheikh", "Raza", "Butt", "Qureshi", "Ansari"]
_DEPTS = ["Engineering", "Finance", "HR", "Operations", "IT", "Facilities",
          "Executive"]


def _pick(rng: random.Random, mix: dict[Behaviour, float]) -> Behaviour:
    roll = rng.random()
    cumulative = 0.0
    for behaviour, weight in mix.items():
        cumulative += weight
        if roll <= cumulative:
            return behaviour
    return Behaviour.EVACUATES


def build_population(
    site: Site,
    *,
    count: int = 500,
    seed: int = 20260910,
    visitors: int = 12,
    lookalike_pairs: int = 6,
    mix: dict[Behaviour, float] | None = None,
    guarantee: dict[Behaviour, int] | None = None,
) -> list[Agent]:
    """Create a population. Deterministic for a given seed.

    The behaviours in `guarantee` are assigned first, so a small population
    still contains every case worth testing. Pass an empty dict to sample purely
    from the probability mix.
    """
    rng = random.Random(seed)
    mix = mix or DEFAULT_MIX
    guarantee = GUARANTEED if guarantee is None else guarantee

    required: list[Behaviour] = []
    for behaviour, minimum in guarantee.items():
        required.extend([behaviour] * minimum)
    if len(required) > count:
        raise ValueError(
            f"population of {count} cannot hold the {len(required)} guaranteed "
            "behaviours; raise count or pass a smaller guarantee"
        )
    work_floors = [f.floor_id for f in site.floors
                   if f.floor_id not in ("outside",)]
    assembly = [z.zone_id for z in site.assembly_zones()]

    agents: list[Agent] = []
    for i in range(count):
        behaviour = required[i] if i < len(required) else _pick(rng, mix)
        speed = {
            Behaviour.SLOW: rng.uniform(0.45, 0.7),
            Behaviour.NEEDS_ASSISTANCE: rng.uniform(0.25, 0.45),
        }.get(behaviour, rng.uniform(0.85, 1.25))

        agents.append(Agent(
            person_ref=f"emp:EMP-{i:04d}",
            emp_id=f"EMP-{i:04d}",
            display_name=f"{rng.choice(_FIRST)} {rng.choice(_LAST)}",
            home_floor_id=rng.choice(work_floors),
            behaviour=behaviour,
            has_gallery_entry=rng.random() > 0.04,   # a few never got enrolled
            target_assembly=rng.choice(assembly),
            reaction_ms=int(rng.expovariate(1 / 12_000)),
            speed=speed,
            department=rng.choice(_DEPTS),
        ))

    # A few pairs who genuinely look alike. The identity margin gate has to
    # separate them, and when it cannot, the answer must be CONFLICT.
    #
    # They are drawn from people a camera actually sees. Taking the first N
    # agents instead put every pair on the guaranteed-behaviour slots at the
    # head of the list, several of whom are not in the building at all, so the
    # look-alike injection had nothing to act on and the conflict it exists to
    # produce never appeared.
    #
    # Only the genuinely absent are excluded now. Excluding the desk-bound as
    # well was over-cautious -- they are observed at their desks for the whole
    # drill -- and it removed the pairing that matters most: one of the pair
    # reaches the assembly point while the other never leaves. That is the shape
    # a false ACCOUNTED has, and while it could not be generated, no number of
    # property runs could find one.
    observable = [a for a in agents
                  if a.behaviour is not Behaviour.ABSENT_FROM_SITE
                  and a.has_gallery_entry]
    stays = [a for a in observable if a.behaviour is Behaviour.NEVER_LEAVES]
    leaves = [a for a in observable if a.behaviour is not Behaviour.NEVER_LEAVES]
    pairs = list(zip(stays, leaves))
    spare = leaves[len(pairs):]
    pairs += list(zip(spare[0::2], spare[1::2]))
    for a, b in pairs[:lookalike_pairs]:
        a.lookalike_of = b.emp_id
        b.lookalike_of = a.emp_id

    # Visitors and contractors: no gallery entry, so no face route to identity.
    for v in range(visitors):
        agents.append(Agent(
            person_ref=f"visitor:V-{v:03d}",
            emp_id=None,
            display_name=f"Visitor {v:03d}",
            home_floor_id=rng.choice(work_floors),
            behaviour=_pick(rng, {Behaviour.EVACUATES: 0.85,
                                  Behaviour.WANDERS_THEN_LEAVES: 0.15}),
            has_gallery_entry=False,
            is_visitor=True,
            target_assembly=rng.choice(assembly),
            reaction_ms=int(rng.expovariate(1 / 20_000)),
            speed=rng.uniform(0.7, 1.1),
        ))
    return agents


def _zone_point(rng: random.Random, zone: Zone) -> Point:
    xs = [p[0] for p in zone.polygon]
    ys = [p[1] for p in zone.polygon]
    # Inset so a point never lands exactly on an edge, where the vendored
    # ray-cast is documented as ambiguous.
    return (rng.uniform(min(xs) + 0.02, max(xs) - 0.02),
            rng.uniform(min(ys) + 0.02, max(ys) - 0.02))


def walk(
    agent: Agent, site: Site, *, alarm_ms: int, step_ms: int = 1_000,
    seed: int = 20260910, horizon_ms: int = 600_000,
) -> None:
    """Fill in the agent's true trajectory. Mutates `agent.trajectory`.

    The route is open plan, corridor, stairwell, ground floor, exit, assembly.
    The stairwell leg is the interesting one: it is a blind zone, so a correct
    system loses the track there and must not conclude anything from that.
    """
    rng = random.Random(stable_seed(seed, agent.person_ref))
    trajectory: list[Waypoint] = []

    def add(ts_ms: int, floor_id: str, point: Point) -> None:
        zone = site.locate(floor_id, point)
        trajectory.append(Waypoint(
            ts_ms=ts_ms, floor_id=floor_id, point=point,
            zone_id=zone.zone_id if zone else None,
            zone_kind=zone.kind if zone else None,
        ))

    if agent.behaviour is Behaviour.ABSENT_FROM_SITE:
        agent.trajectory = []
        return

    home = agent.home_floor_id
    open_zone = site.zone(f"{home}-open")
    corridor = site.zone(f"{home}-corridor")
    stair = site.zone(f"{home}-stair")
    assert open_zone and corridor and stair

    t = alarm_ms - rng.randint(20_000, 60_000)
    for _ in range(3):  # milling about before the alarm
        add(t, home, _zone_point(rng, open_zone))
        t += step_ms * 5

    t = alarm_ms + agent.reaction_ms

    if agent.behaviour is Behaviour.NEVER_LEAVES:
        # Still at their desk. Headphones on, or in the basement plant room.
        # These are the people the system exists to find.
        while t < alarm_ms + horizon_ms:
            add(t, home, _zone_point(rng, open_zone))
            t += step_ms * 10
        agent.trajectory = trajectory
        return

    leg = int(step_ms * 6 / max(agent.speed, 0.2))

    if agent.behaviour is Behaviour.WANDERS_THEN_LEAVES:
        for _ in range(rng.randint(2, 5)):
            add(t, home, _zone_point(rng, open_zone))
            t += leg

    add(t, home, _zone_point(rng, open_zone)); t += leg
    add(t, home, _zone_point(rng, corridor)); t += leg

    # Descend the stairwell. Blind on every floor, by design.
    level = next(f.level for f in site.floors if f.floor_id == home)
    ground_level = 1
    steps_down = abs(level - ground_level) + (1 if level < ground_level else 0)
    for _ in range(max(1, steps_down)):
        add(t, home, _zone_point(rng, stair))
        t += leg
    add(t, "floor-1", _zone_point(rng, site.zone("floor-1-stair")))
    t += leg

    add(t, "floor-1", _zone_point(rng, site.zone("floor-1-corridor")))
    t += leg

    # Most take the main exit; some take the fire exit, which has no camera.
    exit_zone = site.zone("exit-main" if rng.random() < 0.72 else "exit-fire")
    add(t, "floor-1", _zone_point(rng, exit_zone))
    t += leg

    target = site.zone(agent.target_assembly or "assembly-north")
    assert target
    add(t, "outside", _zone_point(rng, target))
    t += step_ms * 5

    if agent.behaviour is Behaviour.LEAVES_ASSEMBLY_EARLY:
        # Wandered off to find a colleague. They arrived, then stopped being
        # there, and both facts are true.
        for _ in range(3):
            add(t, "outside", _zone_point(rng, target))
            t += step_ms * 5
        for _ in range(6):
            add(t, "floor-1", _zone_point(rng, site.zone("floor-1-corridor")))
            t += step_ms * 10
    else:
        while t < alarm_ms + horizon_ms:
            add(t, "outside", _zone_point(rng, target))
            t += step_ms * 15

    agent.trajectory = trajectory


def walk_all(agents: list[Agent], site: Site, *, alarm_ms: int,
             seed: int = 20260910, horizon_ms: int = 600_000) -> None:
    for agent in agents:
        walk(agent, site, alarm_ms=alarm_ms, seed=seed, horizon_ms=horizon_ms)
