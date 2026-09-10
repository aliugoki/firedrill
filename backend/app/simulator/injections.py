"""Failure injection: every way the real system lies to itself.

Each knob corresponds to something observed in production or documented as a
known gap. They are grouped by what they attack, because the point of a test run
is to answer "what happens when *this* breaks", not to produce generic noise.

`NONE` is a real and useful setting: a clean run establishes that the pipeline
finds people at all, which is the baseline every injected run is compared to.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from app.simulator.agents import stable_seed


@dataclass(frozen=True, slots=True)
class Outage:
    """A window during which something is unavailable."""

    start_ms: int
    end_ms: int
    target: str = "*"

    def covers(self, ts_ms: int, target: str | None = None) -> bool:
        if not self.start_ms <= ts_ms < self.end_ms:
            return False
        return self.target == "*" or self.target == target


@dataclass(frozen=True, slots=True)
class Injections:
    """What to break, and how often.

    Probabilities are per observation unless noted. Everything is driven by the
    run's seeded RNG, so a failing run reproduces exactly.
    """

    # --- the face pipeline ----------------------------------------------------
    face_loss_rate: float = 0.0
    """Face not detected at all. FACE_UNAVAILABLE, not evidence of absence."""

    poor_quality_rate: float = 0.0
    """Face found but blurred, small, or badly lit: below the quality gate."""

    bad_pose_rate: float = 0.0
    """Face turned too far from the camera to embed reliably."""

    lookalike_confusion_rate: float = 0.0
    """A look-alike's face scores close enough that the margin gate is tested.
    When it fails, the correct answer is CONFLICT, never a coin-flip winner."""

    wrong_identity_rate: float = 0.0
    """An outright misidentification: a confident match on the wrong person."""

    # --- the tracker ----------------------------------------------------------
    track_fragmentation_rate: float = 0.0
    """One person becomes two global ids. Their evidence is split in half."""

    id_switch_rate: float = 0.0
    """Two people swap tracks. The dangerous one: a confirmed name lands on a
    different body, turning one problem into two wrong answers."""

    occlusion_rate: float = 0.0
    """Person hidden behind another. Observed as a dropped sighting."""

    # --- the transport --------------------------------------------------------
    duplicate_rate: float = 0.0
    """The same event delivered twice. Ingest must be idempotent."""

    reorder_rate: float = 0.0
    """Events delivered out of sequence."""

    delay_rate: float = 0.0
    max_delay_ms: int = 30_000
    """Events arriving late, after decisions have already been made on the gap."""

    drop_rate: float = 0.0
    """Events lost outright. Produces a SEQUENCE_GAP; never interpolated."""

    # --- infrastructure -------------------------------------------------------
    camera_outages: tuple[Outage, ...] = ()
    redis_outages: tuple[Outage, ...] = ()
    db_outages: tuple[Outage, ...] = ()
    network_partitions: tuple[Outage, ...] = ()

    def any_infrastructure_failure(self) -> bool:
        return bool(self.camera_outages or self.redis_outages
                    or self.db_outages or self.network_partitions)


NONE = Injections()

#: A realistic bad day: a lossy face pipeline, a fragmenting tracker, a flaky
#: network, and one camera down for two minutes in the middle of the drill.
REALISTIC = Injections(
    face_loss_rate=0.35,
    poor_quality_rate=0.15,
    bad_pose_rate=0.10,
    lookalike_confusion_rate=0.30,
    wrong_identity_rate=0.01,
    track_fragmentation_rate=0.08,
    id_switch_rate=0.02,
    occlusion_rate=0.10,
    duplicate_rate=0.05,
    reorder_rate=0.05,
    delay_rate=0.05,
    drop_rate=0.02,
    camera_outages=(Outage(60_000, 180_000, "cam-floor-3-open"),),
)

#: Everything at once. Used to assert that the invariants hold under conditions
#: no real deployment should ever see.
HOSTILE = Injections(
    face_loss_rate=0.6,
    poor_quality_rate=0.3,
    bad_pose_rate=0.25,
    lookalike_confusion_rate=0.8,
    wrong_identity_rate=0.05,
    track_fragmentation_rate=0.25,
    id_switch_rate=0.10,
    occlusion_rate=0.25,
    duplicate_rate=0.20,
    reorder_rate=0.20,
    delay_rate=0.20,
    drop_rate=0.10,
    camera_outages=(Outage(30_000, 240_000, "*"),),
    redis_outages=(Outage(90_000, 150_000),),
    db_outages=(Outage(200_000, 260_000),),
    network_partitions=(Outage(120_000, 200_000),),
)


@dataclass
class Dice:
    """Seeded randomness, one stream per concern.

    Separate streams matter: turning up the face-loss rate must not shift which
    events get duplicated, or two runs stop being comparable.
    """

    seed: int
    _streams: dict = field(default_factory=dict)

    def stream(self, name: str) -> random.Random:
        rng = self._streams.get(name)
        if rng is None:
            rng = random.Random(stable_seed(self.seed, name))
            self._streams[name] = rng
        return rng

    def hit(self, name: str, probability: float) -> bool:
        if probability <= 0:
            return False
        return self.stream(name).random() < probability
