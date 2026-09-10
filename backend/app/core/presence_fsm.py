"""Presence state machine: where a person is, and how sure we are of it.

Presence is about **observation**, not about identity and not about safety. It
answers "where was this body last seen, and how stale is that?" — nothing more.
Who the body belongs to is `identity_fsm`'s problem, and whether they are safe is
`accountability_fsm`'s.

Six states:

    NOT_OBSERVED            never seen during this drill
    IN_BUILDING             seen in a FLOOR zone
    IN_TRANSIT              seen in an EXIT zone, on the way out
    ASSEMBLY_PRESENT        settled in an ASSEMBLY zone
    TEMPORARILY_UNOBSERVED  track dropped, within the grace window
    LOST                    track dropped, past the grace window

Zones are tagged FLOOR, EXIT, ASSEMBLY or BLIND. BLIND matters: a corridor with
no camera is a place where *not seeing someone is the expected outcome*. Being
unobserved there gets a longer grace window, because treating an architectural
gap as evidence would manufacture alarm out of a known coverage hole.

Two rules do most of the work here.

**Invariant 1.** LOST means "the track is stale", never "the person is gone". A
person in LOST with a confirmed identity and a warden confirmation is still
accounted for. Nothing downstream may read LOST as absence.

**Invariant 8.** While a camera or the pipeline is degraded, a track cannot age
into LOST. Our own outage is not evidence about a person's whereabouts. The
grace clock is suspended for the duration and resumes on recovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterator


class ZoneKind(str, Enum):
    FLOOR = "FLOOR"
    EXIT = "EXIT"
    ASSEMBLY = "ASSEMBLY"
    BLIND = "BLIND"


class PresenceState(str, Enum):
    NOT_OBSERVED = "NOT_OBSERVED"
    IN_BUILDING = "IN_BUILDING"
    IN_TRANSIT = "IN_TRANSIT"
    ASSEMBLY_PRESENT = "ASSEMBLY_PRESENT"
    TEMPORARILY_UNOBSERVED = "TEMPORARILY_UNOBSERVED"
    LOST = "LOST"


#: States that mean "currently observed somewhere".
OBSERVED_STATES: frozenset[PresenceState] = frozenset({
    PresenceState.IN_BUILDING, PresenceState.IN_TRANSIT,
    PresenceState.ASSEMBLY_PRESENT,
})

#: States that mean "inside the building envelope".
INSIDE_STATES: frozenset[PresenceState] = frozenset({
    PresenceState.IN_BUILDING, PresenceState.IN_TRANSIT,
})


@dataclass(frozen=True, slots=True)
class PresenceConfig:
    """Timings. Configuration, not literals — invariant 7.

    `t_lost_blind_ms` is deliberately larger than `t_lost_ms`: losing a track in
    a zone with no camera coverage is the expected outcome there, so it earns
    more patience before it counts as staleness.
    """

    t_lost_ms: int
    t_lost_blind_ms: int
    assembly_dwell_ms: int
    calibrated: bool = False
    source: str = "uncalibrated"

    def __post_init__(self) -> None:
        if self.t_lost_ms <= 0:
            raise ValueError("t_lost_ms must be positive")
        if self.t_lost_blind_ms < self.t_lost_ms:
            raise ValueError(
                "t_lost_blind_ms must be at least t_lost_ms: an uncovered zone "
                "cannot be less forgiving than a covered one"
            )
        if self.assembly_dwell_ms < 0:
            raise ValueError("assembly_dwell_ms must not be negative")


#: Phase 1 / simulator starting point. Nothing here is validated; Phase 2
#: replaces it from docs/EVAC120_CALIBRATION.md.
PROVISIONAL_CONFIG = PresenceConfig(
    t_lost_ms=15_000,
    t_lost_blind_ms=45_000,
    assembly_dwell_ms=5_000,
    calibrated=False,
    source="Phase 1 placeholder, unvalidated",
)


@dataclass(frozen=True, slots=True)
class ZoneSighting:
    """One observation of a person in a zone."""

    ts_ms: int
    zone_id: str
    zone_kind: ZoneKind
    camera_id: str | None = None
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class LastKnown:
    """Everything the command centre shows next to a person who needs checking.

    Kept as a value object because it is the answer to "where do I send someone
    to look?", and that answer must survive the track being lost.
    """

    ts_ms: int
    zone_id: str
    zone_kind: ZoneKind
    camera_id: str | None
    state: PresenceState


@dataclass(frozen=True, slots=True)
class PresenceTransition:
    person_id: str
    from_state: PresenceState
    to_state: PresenceState
    ts_ms: int
    reason: str
    zone_id: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class PersonPresence:
    """Presence for one global person id."""

    person_id: str
    config: PresenceConfig
    state: PresenceState = PresenceState.NOT_OBSERVED
    first_sighting_ms: int | None = None
    last_sighting_ms: int | None = None
    last_known: LastKnown | None = None
    state_before_unobserved: PresenceState | None = None
    assembly_since_ms: int | None = None
    degraded_since_ms: int | None = None
    _grace_credit_ms: int = 0

    # -- queries ---------------------------------------------------------------

    @property
    def is_at_assembly(self) -> bool:
        return self.state is PresenceState.ASSEMBLY_PRESENT

    @property
    def is_degraded(self) -> bool:
        return self.degraded_since_ms is not None

    @property
    def was_at_assembly(self) -> bool:
        """Whether the person reached assembly, even if the track has since gone.

        A person who walked into the assembly point and then out of camera view
        did not un-arrive. Accountability reads this, not `is_at_assembly`.
        """
        if self.is_at_assembly:
            return True
        return (
            self.state in (PresenceState.TEMPORARILY_UNOBSERVED, PresenceState.LOST)
            and self.state_before_unobserved is PresenceState.ASSEMBLY_PRESENT
        )

    def overlaps(self, other: "PersonPresence") -> bool:
        """Whether two tracks were being observed at the same time.

        One person cannot be two simultaneously observed tracks, so an overlap
        between two tracks claiming the same identity is a real conflict.
        Sequential tracks are the ordinary result of fragmentation and mean only
        that one person's evidence got split.
        """
        if None in (self.first_sighting_ms, self.last_sighting_ms,
                    other.first_sighting_ms, other.last_sighting_ms):
            return False
        return (self.first_sighting_ms <= other.last_sighting_ms
                and other.first_sighting_ms <= self.last_sighting_ms)

    def staleness_ms(self, now_ms: int) -> int | None:
        if self.last_sighting_ms is None:
            return None
        return max(0, now_ms - self.last_sighting_ms)

    def _grace_ms(self) -> int:
        """How long this person may go unobserved before the track is stale."""
        if self.last_known is not None and self.last_known.zone_kind is ZoneKind.BLIND:
            return self.config.t_lost_blind_ms
        return self.config.t_lost_ms

    # -- transitions -----------------------------------------------------------

    def observe(self, sighting: ZoneSighting) -> PresenceTransition | None:
        """Fold in one sighting. Returns a transition if the state changed."""
        previous = self.state
        if self.first_sighting_ms is None:
            self.first_sighting_ms = sighting.ts_ms
        self.last_sighting_ms = sighting.ts_ms
        self.last_known = LastKnown(
            ts_ms=sighting.ts_ms, zone_id=sighting.zone_id,
            zone_kind=sighting.zone_kind, camera_id=sighting.camera_id,
            state=self.state,
        )

        target = self._state_for(sighting)

        if target is PresenceState.ASSEMBLY_PRESENT:
            # Hysteresis: a person clipping the corner of an assembly zone on
            # their way past has not assembled. They have to settle there.
            if self.assembly_since_ms is None:
                self.assembly_since_ms = sighting.ts_ms
            dwelled = sighting.ts_ms - self.assembly_since_ms
            if dwelled < self.config.assembly_dwell_ms:
                if previous in (PresenceState.TEMPORARILY_UNOBSERVED,
                                PresenceState.LOST):
                    # Track is back, even if not yet settled at assembly.
                    return self._to(
                        self.state_before_unobserved or PresenceState.IN_TRANSIT,
                        sighting, "track reacquired inside the assembly zone")
                return None
        else:
            self.assembly_since_ms = None

        if target is previous:
            return None
        return self._to(target, sighting, self._reason_for(previous, target, sighting))

    def _state_for(self, sighting: ZoneSighting) -> PresenceState:
        if sighting.zone_kind is ZoneKind.ASSEMBLY:
            return PresenceState.ASSEMBLY_PRESENT
        if sighting.zone_kind is ZoneKind.EXIT:
            return PresenceState.IN_TRANSIT
        if sighting.zone_kind is ZoneKind.FLOOR:
            # Coming back inside from assembly is a real thing during a drill
            # (someone fetching a colleague). It is not blocked.
            return PresenceState.IN_BUILDING
        # BLIND: the person is somewhere with no coverage. Being seen at the
        # edge of one tells us they are still inside, nothing finer.
        if self.state in (PresenceState.NOT_OBSERVED,
                          PresenceState.TEMPORARILY_UNOBSERVED,
                          PresenceState.LOST):
            return PresenceState.IN_BUILDING
        return self.state_before_unobserved or self.state

    @staticmethod
    def _reason_for(
        previous: PresenceState, target: PresenceState, sighting: ZoneSighting
    ) -> str:
        if previous in (PresenceState.TEMPORARILY_UNOBSERVED, PresenceState.LOST):
            return f"track reacquired in {sighting.zone_kind.value} zone {sighting.zone_id}"
        return f"observed in {sighting.zone_kind.value} zone {sighting.zone_id}"

    def track_lost(self, now_ms: int, reason: str = "tracker dropped the track"
                   ) -> PresenceTransition | None:
        """The tracker lost this person. Grace window starts."""
        if self.state in (PresenceState.TEMPORARILY_UNOBSERVED, PresenceState.LOST):
            return None
        if self.state is PresenceState.NOT_OBSERVED:
            return None
        self.state_before_unobserved = self.state
        return self._set(PresenceState.TEMPORARILY_UNOBSERVED, now_ms, reason)

    def tick(self, now_ms: int) -> PresenceTransition | None:
        """Advance time. The only path into LOST."""
        if self.state is not PresenceState.TEMPORARILY_UNOBSERVED:
            return None
        if self.is_degraded:
            # Invariant 8. Our own outage is not evidence about this person, so
            # the grace clock does not run while it lasts.
            return None
        stale = self.staleness_ms(now_ms)
        if stale is None:
            return None
        if stale - self._grace_credit_ms < self._grace_ms():
            return None
        return self._set(
            PresenceState.LOST, now_ms,
            f"unobserved for {stale - self._grace_credit_ms} ms, grace {self._grace_ms()} ms",
        )

    def mark_degraded(self, now_ms: int, reason: str) -> PresenceTransition | None:
        """A camera or the pipeline covering this person failed.

        Does not change state. It stops the grace clock, and it makes the reason
        visible so the command centre can show CAMERA_DEGRADED rather than
        implying anything about the person.
        """
        if self.degraded_since_ms is None:
            self.degraded_since_ms = now_ms
        return None

    def mark_recovered(self, now_ms: int) -> None:
        """Coverage is back. Time spent degraded is credited back to the grace
        window, so a person is not declared stale for an outage they had no part
        in."""
        if self.degraded_since_ms is None:
            return
        self._grace_credit_ms += max(0, now_ms - self.degraded_since_ms)
        self.degraded_since_ms = None

    # -- internals -------------------------------------------------------------

    def _to(self, target: PresenceState, sighting: ZoneSighting, reason: str
            ) -> PresenceTransition:
        if self.state in (PresenceState.TEMPORARILY_UNOBSERVED, PresenceState.LOST):
            self._grace_credit_ms = 0
            self.state_before_unobserved = None
        return self._set(target, sighting.ts_ms, reason, zone_id=sighting.zone_id)

    def _set(self, target: PresenceState, ts_ms: int, reason: str,
             zone_id: str | None = None) -> PresenceTransition:
        previous = self.state
        self.state = target
        return PresenceTransition(
            person_id=self.person_id, from_state=previous, to_state=target,
            ts_ms=ts_ms, reason=reason,
            zone_id=zone_id or (self.last_known.zone_id if self.last_known else None),
        )


class PresenceRegistry:
    """Presence for everyone in one drill."""

    def __init__(self, config: PresenceConfig = PROVISIONAL_CONFIG) -> None:
        self.config = config
        self._people: dict[str, PersonPresence] = {}

    def get(self, person_id: str) -> PersonPresence:
        person = self._people.get(person_id)
        if person is None:
            person = PersonPresence(person_id=person_id, config=self.config)
            self._people[person_id] = person
        return person

    def observe(self, person_id: str, sighting: ZoneSighting
                ) -> PresenceTransition | None:
        return self.get(person_id).observe(sighting)

    def tick(self, now_ms: int) -> list[PresenceTransition]:
        return [t for t in (p.tick(now_ms) for p in self._people.values()) if t]

    def mark_camera_degraded(self, camera_id: str, now_ms: int, reason: str) -> int:
        """Suspend the grace clock for everyone last seen on this camera."""
        touched = 0
        for person in self._people.values():
            if person.last_known and person.last_known.camera_id == camera_id:
                person.mark_degraded(now_ms, reason)
                touched += 1
        return touched

    def mark_camera_recovered(self, camera_id: str, now_ms: int) -> int:
        touched = 0
        for person in self._people.values():
            if person.last_known and person.last_known.camera_id == camera_id:
                person.mark_recovered(now_ms)
                touched += 1
        return touched

    def in_state(self, state: PresenceState) -> list[PersonPresence]:
        return [p for p in self._people.values() if p.state is state]

    def at_assembly(self) -> list[PersonPresence]:
        return [p for p in self._people.values() if p.was_at_assembly]

    def still_inside(self) -> list[PersonPresence]:
        return [p for p in self._people.values() if p.state in INSIDE_STATES]

    def __len__(self) -> int:
        return len(self._people)

    def __iter__(self) -> Iterator[PersonPresence]:
        return iter(self._people.values())
