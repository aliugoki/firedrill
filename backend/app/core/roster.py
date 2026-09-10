"""The roster: who is expected, and who is merely present.

"Expected" is the denominator of every number on the command centre. Getting it
wrong is not a display bug: an inflated roster manufactures phantom missing
people and sends wardens to search for someone on annual leave, and a deflated
one hides a real absence.

Three populations, kept apart on purpose:

    employees     on the roster, drawn from FaceTrack, with a face in the gallery
    signed-in     visitors and contractors, no gallery entry, tracked as unknown
    unknown       observed, on no list — the residual, never reconciled away

**Invariant 5.** An unknown person is never folded onto an employee record. A
visitor who resembles an employee stays a visitor. The only thing that moves
someone between these populations is a human.

FaceTrack's `user_data` carries `emp_id`, names, `image_path` and
`feature_path`. It has no department, no home floor and no warden-zone
assignment, and the warden screens need all three, so firedrill owns those
fields. `RosterEntry` marks which came from upstream and which are local, since
a locally-owned field going stale is a different problem from an upstream sync
failing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum


class Population(str, Enum):
    EMPLOYEE = "EMPLOYEE"
    VISITOR = "VISITOR"
    CONTRACTOR = "CONTRACTOR"
    UNKNOWN = "UNKNOWN"


class ExpectationReason(str, Enum):
    """Why someone is, or is not, expected in the building today."""

    ON_SHIFT = "ON_SHIFT"
    SIGNED_IN = "SIGNED_IN"
    ON_LEAVE = "ON_LEAVE"
    REMOTE = "REMOTE"
    SIGNED_OUT = "SIGNED_OUT"
    NOT_ROSTERED = "NOT_ROSTERED"


#: Reasons that put someone in the expected set.
EXPECTED_REASONS: frozenset[ExpectationReason] = frozenset({
    ExpectationReason.ON_SHIFT, ExpectationReason.SIGNED_IN,
})


@dataclass(frozen=True, slots=True)
class RosterEntry:
    """One person the drill has to account for.

    `has_gallery_entry` decides whether face identification is even possible for
    this person. Someone enrolled without a usable face embedding can only be
    accounted for by a warden, and the command centre must say so rather than
    leaving an operator waiting for a match that cannot arrive.
    """

    person_ref: str
    display_name: str
    population: Population
    reason: ExpectationReason

    # From FaceTrack.
    emp_id: str | None = None
    has_gallery_entry: bool = False

    # Owned locally: FaceTrack has none of these.
    department: str | None = None
    home_floor_id: str | None = None
    assigned_assembly_zone: str | None = None
    warden_zone: str | None = None
    mobility_assistance: bool = False

    # Visitors and contractors only.
    host_emp_id: str | None = None
    signed_in_ms: int | None = None
    signed_out_ms: int | None = None

    @property
    def is_expected(self) -> bool:
        return self.reason in EXPECTED_REASONS

    @property
    def is_identifiable_by_face(self) -> bool:
        return self.population is Population.EMPLOYEE and self.has_gallery_entry

    @property
    def needs_human_to_account(self) -> bool:
        """People the cameras cannot settle on their own.

        Surfaced to wardens at drill start rather than discovered at minute
        four, and mobility assistance is included because those are the people
        a sweep must reach first.
        """
        return not self.is_identifiable_by_face or self.mobility_assistance


@dataclass(frozen=True, slots=True)
class RosterSnapshot:
    """The roster frozen at drill start.

    Taken once and held for the drill's duration. A roster that shifts mid-drill
    would make the denominator move under the operator, and a person who
    "disappeared" because HR updated a record is indistinguishable on screen
    from one who disappeared in a stairwell.
    """

    taken_at_ms: int
    entries: tuple[RosterEntry, ...]
    source_reachable: bool = True
    source_note: str | None = None

    @property
    def expected(self) -> tuple[RosterEntry, ...]:
        return tuple(e for e in self.entries if e.is_expected)

    @property
    def expected_count(self) -> int:
        return len(self.expected)

    @property
    def is_trustworthy(self) -> bool:
        """Whether this roster came from a live source.

        A stale or unreachable roster does not stop a drill; it changes what the
        numbers mean, and invariant 8 says that must be visible rather than
        assumed away.
        """
        return self.source_reachable and bool(self.entries)

    def by_ref(self, person_ref: str) -> RosterEntry | None:
        for entry in self.entries:
            if entry.person_ref == person_ref:
                return entry
        return None

    def by_emp_id(self, emp_id: str) -> RosterEntry | None:
        for entry in self.entries:
            if entry.emp_id == emp_id:
                return entry
        return None

    def in_population(self, population: Population) -> tuple[RosterEntry, ...]:
        return tuple(e for e in self.entries if e.population is population)

    def by_assembly_zone(self) -> dict[str, tuple[RosterEntry, ...]]:
        """Expected people grouped by the zone their warden covers.

        This is what a warden device caches at drill start, so it is built from
        the expected set only: a warden should not be handed a list containing
        people known to be on leave.
        """
        grouped: dict[str, list[RosterEntry]] = defaultdict(list)
        for entry in self.expected:
            grouped[entry.assigned_assembly_zone or "unassigned"].append(entry)
        return {zone: tuple(people) for zone, people in sorted(grouped.items())}

    def needing_human(self) -> tuple[RosterEntry, ...]:
        return tuple(e for e in self.expected if e.needs_human_to_account)

    def coverage_gaps(self) -> dict[str, int]:
        """Locally-owned fields nobody has filled in.

        These are the fields FaceTrack does not supply. A warden list with no
        department is hard to work through, and an unassigned assembly zone
        means nobody owns that person during a sweep.
        """
        gaps = {
            "no_department": 0,
            "no_assembly_zone": 0,
            "no_home_floor": 0,
            "no_gallery_entry": 0,
        }
        for entry in self.expected:
            if not entry.department:
                gaps["no_department"] += 1
            if not entry.assigned_assembly_zone:
                gaps["no_assembly_zone"] += 1
            if not entry.home_floor_id:
                gaps["no_home_floor"] += 1
            if entry.population is Population.EMPLOYEE and not entry.has_gallery_entry:
                gaps["no_gallery_entry"] += 1
        return gaps


@dataclass
class Roster:
    """Builds a snapshot, and tracks who was observed but is on no list."""

    entries: list[RosterEntry] = field(default_factory=list)
    unknown_people: dict[str, str | None] = field(default_factory=dict)

    def add_employee(
        self, *, emp_id: str, display_name: str, has_gallery_entry: bool,
        reason: ExpectationReason = ExpectationReason.ON_SHIFT, **local,
    ) -> RosterEntry:
        entry = RosterEntry(
            person_ref=f"emp:{emp_id}", display_name=display_name,
            population=Population.EMPLOYEE, reason=reason, emp_id=emp_id,
            has_gallery_entry=has_gallery_entry, **local,
        )
        self.entries.append(entry)
        return entry

    def sign_in(
        self, *, visitor_ref: str, display_name: str, at_ms: int,
        population: Population = Population.VISITOR, host_emp_id: str | None = None,
        **local,
    ) -> RosterEntry:
        """Register a visitor or contractor.

        They have no gallery entry and will never be identified by face. That is
        the expected outcome, not a failure, and the only route to accounting
        for them is a warden.
        """
        if population not in (Population.VISITOR, Population.CONTRACTOR):
            raise ValueError("sign_in is for visitors and contractors only")
        entry = RosterEntry(
            person_ref=f"visitor:{visitor_ref}", display_name=display_name,
            population=population, reason=ExpectationReason.SIGNED_IN,
            has_gallery_entry=False, host_emp_id=host_emp_id, signed_in_ms=at_ms,
            **local,
        )
        self.entries.append(entry)
        return entry

    def sign_out(self, visitor_ref: str, at_ms: int) -> RosterEntry | None:
        ref = f"visitor:{visitor_ref}"
        for i, entry in enumerate(self.entries):
            if entry.person_ref == ref:
                updated = RosterEntry(
                    **{**{f: getattr(entry, f) for f in entry.__slots__},
                       "reason": ExpectationReason.SIGNED_OUT,
                       "signed_out_ms": at_ms}
                )
                self.entries[i] = updated
                return updated
        return None

    def observe_unknown(self, person_id: str, tag: str | None = None) -> None:
        """Record someone the cameras saw who is on no list.

        Invariant 5: they are never matched to an employee to make the numbers
        tidy. They stay in their own population until a human tags them.
        """
        self.unknown_people[person_id] = tag

    def snapshot(self, at_ms: int, *, source_reachable: bool = True,
                 source_note: str | None = None) -> RosterSnapshot:
        return RosterSnapshot(
            taken_at_ms=at_ms, entries=tuple(self.entries),
            source_reachable=source_reachable, source_note=source_note,
        )


def from_facetrack(
    rows: list[dict], *, local_fields: dict[str, dict] | None = None
) -> Roster:
    """Build a roster from FaceTrack's `GET /api/employees` payload.

    Rows look like ``{"emp_id", "first_name", "last_name", "photo", "present"}``.
    Department, home floor and assembly zone are not in that payload, so they are
    merged in from `local_fields`, keyed by `emp_id`.

    `present` in FaceTrack means "checked in through the turnstile today". It is
    used as the default expectation and nothing more: an attendance system is
    not a life-safety system, and a person who forgot to badge in is still in the
    building.
    """
    roster = Roster()
    local_fields = local_fields or {}
    for row in rows:
        emp_id = str(row["emp_id"])
        name = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip() or emp_id
        roster.add_employee(
            emp_id=emp_id,
            display_name=name,
            has_gallery_entry=bool(row.get("photo")),
            reason=(ExpectationReason.ON_SHIFT if row.get("present")
                    else ExpectationReason.ON_LEAVE),
            **local_fields.get(emp_id, {}),
        )
    return roster
