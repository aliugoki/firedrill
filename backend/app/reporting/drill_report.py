"""The post-drill report.

Everything the brief asks for after a drill, plus the thing it does not: an
explicit statement of what this drill could not establish.

The central measurement is **the system against the manual roll-call**, because
that is the only reference a real building has. Two disagreements are counted,
and they are not symmetric:

    false accounted     the system said safe, a warden did not confirm it.
                        A safety failure. Must be zero.
    false unaccounted    the system could not account for someone a warden
                        confirmed. A quality failure: it wastes a warden's time
                        and erodes trust in the board, but nobody is left in a
                        building because of it.

Reporting them as one "error rate" would average a safety failure together with
an inconvenience.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.accountability_fsm import AccountabilityState
from app.drill import Drill
from app.infra.audit import AuditLog
from app.reporting.validation import Thresholds, Validation, validate
from app.warden.actions import ActionKind
from app.warden.headcount import Severity


@dataclass(frozen=True, slots=True)
class Disagreement:
    """One person the system and the wardens saw differently."""

    person_ref: str
    display_name: str
    system_state: str
    warden_said: str
    system_reason: str
    zone_id: str | None = None

    def describe(self) -> str:
        return (f"{self.display_name} ({self.person_ref}): system said "
                f"{self.system_state}, warden said {self.warden_said}")


@dataclass(frozen=True, slots=True)
class DrillReport:
    drill_id: str
    name: str
    site_id: str
    started_ms: int | None
    completed_ms: int | None
    duration_s: float | None

    expected: int
    accounted: int
    unaccounted: int
    uncertain: int
    needing_verification: int
    unknown_people: int
    unknown_by_zone: dict = field(default_factory=dict)
    """Where the wardens found them. A total with no location tells a safety
    officer somebody was there and nothing about where to start asking."""
    excluded_from_the_count: dict = field(default_factory=dict)

    false_accounted: tuple = ()
    false_unaccounted: tuple = ()

    p50_s: float | None = None
    p90_s: float | None = None
    p95_s: float | None = None
    p99_s: float | None = None
    max_s: float | None = None
    timing_samples: int = 0
    timing_coverage: float | None = None
    timing_caveats: tuple[str, ...] = ()
    timing_exclusions: dict = field(default_factory=dict)
    """Why the people with no timing have none. `ExclusionReason` calls itself
    "always reported, never silent" and was computed and discarded."""
    accountability_completion_s: float | None = None
    slowest_floor: str | None = None

    warden_confirmations: int = 0
    warden_rejections: int = 0
    warden_overrides: int = 0
    overrides_in_full: tuple = ()
    """Each one in words. `AuditLog.overrides` calls itself the first thing
    anyone reviewing a drill should read, because it is the list of places
    where the recorded outcome is not the one the evidence produced -- and the
    report printed the length of that list and nothing else."""
    disclosures: tuple = ()
    """Who looked at what. Recorded since the audit log existed and reported
    nowhere, which makes it a control that cannot be checked."""
    sweeps_completed: int = 0
    sweeps_expected: int = 0
    zones_with_headcount: int = 0
    headcount_mismatches: tuple = ()
    tolerated_overcounts: tuple = ()
    """Counts a site's own overcount tolerance absorbed. Not mismatches -- the
    site decided that -- and not nothing either: each one is a person the
    system called safe whom a warden could not see."""
    count_history: tuple = ()
    """Zones whose physical count moved. A count that reached the dangerous
    direction and was then resolved leaves no trace in `headcount_mismatches`,
    which reads only the latest figure -- and two people unaccounted for four
    minutes is a fact about the drill whatever the final number says."""
    escalations: tuple = ()

    outages: int = 0
    blind_fraction: float = 0.0
    longest_blind_s: float = 0.0
    health_caveat: str | None = None
    identity_disputes: tuple = ()
    """Every identity the drill could not settle, in words. Invariant 3 says a
    conflict is reported and never adjudicated; reporting it is what this is,
    and the report said only how many people needed verification, never what
    the system could not decide about them."""
    events_dropped: int = 0
    """Events that never reached the database and are not coming back. Zero on
    every drill so far, and the report says so out loud rather than omitting the
    line, because a section that appears only on bad days is a section nobody
    knows to look for."""

    validation: Validation | None = None
    thresholds_calibrated: bool = False

    @property
    def is_safe_result(self) -> bool:
        return not self.false_accounted

    def render(self) -> list[str]:
        """The report a safety officer reads. Verdict first, always."""
        lines = [
            f"EVAC-120 drill report — {self.name}",
            f"  drill {self.drill_id} at {self.site_id}",
            f"  duration {self._fmt(self.duration_s)}",
            "",
        ]

        if self.validation is not None:
            lines.append(self.validation.summary())
            lines.append("")

        lines += [
            "Accountability against the manual roll-call",
            f"  expected                {self.expected}",
            f"  accounted for           {self.accounted}",
            f"  unaccounted for         {self.unaccounted}",
            f"  uncertain               {self.uncertain}",
            f"  needing verification    {self.needing_verification}",
            f"  unknown people          {self.unknown_people}",
        ]

        for zone, count in sorted(self.unknown_by_zone.items()):
            lines.append(f"    at {zone:<21}{count}")

        if self.excluded_from_the_count:
            # Not part of `expected`, so no row above mentions them and nobody
            # went looking. A commander deciding whether the building is clear
            # needs to know the denominator left somebody out, how many, and on
            # what grounds.
            total = sum(self.excluded_from_the_count.values())
            lines.append(f"  {'not counted':<24}{total}")
            for reason, count in sorted(self.excluded_from_the_count.items()):
                words = reason.lower().replace("_", " ")
                lines.append(f"    {words:<22}{count}")

        lines += [
            "",
            f"  FALSE ACCOUNTED         {len(self.false_accounted)}   "
            "(system said safe, no warden confirmed)",
            f"  false unaccounted       {len(self.false_unaccounted)}   "
            "(warden confirmed, system could not)",
        ]

        if self.identity_disputes:
            lines.append("")
            lines.append("  Identities the system could not settle:")
            for line in self.identity_disputes:
                lines.append(f"    - {line}")
            lines.append("    Reported, not resolved. Invariant 3 forbids the "
                         "software picking a winner;")
            lines.append("    a human decides, and the record shows what they "
                         "were deciding between.")

        if self.false_accounted:
            lines.append("")
            lines.append("  Every false accounted, in full:")
            for item in self.false_accounted:
                lines.append(f"    - {item.describe()}")
                lines.append(f"      system reasoning: {item.system_reason}")

        lines += ["", "Evacuation times"]
        if self.p95_s is None:
            for note in self.timing_caveats or ("no measurements",):
                lines.append(f"  none — {note}")
        else:
            lines += [
                f"  P50 {self._fmt(self.p50_s)}   P90 {self._fmt(self.p90_s)}   "
                f"P95 {self._fmt(self.p95_s)}   P99 {self._fmt(self.p99_s)}",
                f"  slowest individual      {self._fmt(self.max_s)}",
                f"  measured on             {self.timing_samples} people"
                + (f" ({self.timing_coverage:.0%} coverage)"
                   if self.timing_coverage is not None else ""),
            ]
            for note in self.timing_caveats:
                lines.append(f"  caveat: {note}")
        for reason, count in sorted(self.timing_exclusions.items()):
            # Split out because the difference matters: somebody the cameras
            # never saw is a coverage problem, and somebody seen who never
            # reached the muster point is a person. Only one of those is a
            # measurement problem.
            words = reason.lower().replace("_", " ")
            lines.append(f"    no timing, {words:<24} {count}")
        lines.append(
            f"  accountability settled  {self._fmt(self.accountability_completion_s)}")
        if self.slowest_floor:
            lines.append(f"  slowest floor           {self.slowest_floor}")

        lines += [
            "",
            "Wardens",
            f"  confirmations           {self.warden_confirmations}",
            f"  identity rejections     {self.warden_rejections}",
            f"  manual overrides        {self.warden_overrides}",
            f"  sweeps completed        {self.sweeps_completed}/{self.sweeps_expected}",
            f"  zones with a headcount  {self.zones_with_headcount}/{self.sweeps_expected}",
        ]
        for mismatch in self.headcount_mismatches:
            lines.append(f"    ! {mismatch}")
        for absorbed in self.tolerated_overcounts:
            lines.append(f"    ! {absorbed}")
        for override in self.overrides_in_full:
            lines.append(f"    ! override: {override}")
        for disclosure in self.disclosures:
            lines.append(f"    disclosure: {disclosure}")
        for escalation in self.escalations:
            lines.append(f"    ! escalated: {escalation}")

        if self.count_history:
            lines.append("")
            lines.append("  How the counts moved")
            for line in self.count_history:
                lines.append(f"    {line}")

        lines += [
            "",
            "System health",
            f"  outages                 {self.outages}",
            f"  blind for               {self.blind_fraction:.0%} of the drill",
            f"  longest blind period    {self._fmt(self.longest_blind_s)}",
            f"  events lost for good    {self.events_dropped}",
        ]
        if self.events_dropped:
            lines += [
                "",
                f"  WARNING: {self.events_dropped} event(s) never reached the "
                "database and cannot be recovered.",
                "           This drill cannot be replayed from the stored "
                "record, so no number above",
                "           can be checked independently of this report.",
            ]
        if self.health_caveat:
            lines.append(f"  {self.health_caveat}")

        if not self.thresholds_calibrated:
            lines += [
                "",
                "NOTE: no threshold in this system has been validated against a "
                "calibration set.",
                "      These numbers describe what happened. They do not yet "
                "describe how well",
                "      the system works, because the settings that produced them "
                "are provisional.",
            ]

        if self.validation is not None:
            lines += [""] + self.validation.describe()
        return lines

    @staticmethod
    def _fmt(seconds: float | None) -> str:
        if seconds is None:
            return "—"
        return f"{seconds:.1f}s"


def _count_line(history: dict) -> str:
    """One zone's count series, and what was wrong with it.

    The series first, because "38, 40, 38" says something a single number
    cannot and the reader can see it at a glance.
    """
    series = ", ".join(str(n) for n in history["physical_counts"])
    line = (f"{history['zone_id']}: counted {series} against the system's "
            f"{history['system_count']}")
    notes = []
    if history["ever_escalated"]:
        notes.append("reached the dangerous direction at least once")
    if history["unstable"]:
        notes.append("the warden's own counts disagree")
    if history["ever_escalated"] and history["settled_now"]:
        notes.append("agrees now")
    return f"{line} — {'; '.join(notes)}" if notes else line


def build_report(
    drill: Drill, *, now_ms: int, audit: AuditLog | None = None,
    thresholds: Thresholds | None = None,
) -> DrillReport:
    """Assemble the report from everything the drill accumulated."""
    board = drill.board(now_ms)
    timing = drill.timing(now_ms)
    warden = drill.warden
    expected_by_zone = drill.roster.by_assembly_zone()

    confirmed_by_warden = {
        action.subject for action in warden.actions
        if action.kind is ActionKind.CONFIRM_PRESENT and action.subject
    }
    denied_by_warden = {
        action.subject for action in warden.actions
        if action.kind in (ActionKind.NOT_HERE, ActionKind.WRONG_PERSON)
        and action.subject
    }

    false_accounted: list[Disagreement] = []
    false_unaccounted: list[Disagreement] = []

    for row in board.rows:
        is_accounted = row.state is AccountabilityState.ACCOUNTED
        # The manual roll-call is the reference. A person the system called safe
        # whom no warden laid eyes on has not been checked, and one a warden
        # actively said was absent has been checked and contradicted.
        if is_accounted and (row.person_ref in denied_by_warden
                             or row.person_ref not in confirmed_by_warden):
            false_accounted.append(Disagreement(
                person_ref=row.person_ref, display_name=row.display_name,
                system_state=row.state.value,
                warden_said=("not here" if row.person_ref in denied_by_warden
                             else "did not confirm"),
                system_reason=row.decision.reason,
                zone_id=row.assigned_assembly_zone))
        elif not is_accounted and row.person_ref in confirmed_by_warden:
            false_unaccounted.append(Disagreement(
                person_ref=row.person_ref, display_name=row.display_name,
                system_state=row.state.value, warden_said="confirmed present",
                system_reason=row.decision.reason,
                zone_id=row.assigned_assembly_zone))

    mismatches = warden.mismatches()
    absorbed = warden.tolerated_overcounts()
    by_floor = timing.by_floor
    slowest = None
    if by_floor:
        ranked = [(name, s.p95) for name, s in by_floor.items() if s.p95 is not None]
        if ranked:
            name, value = max(ranked, key=lambda pair: pair[1])
            slowest = f"{name} at P95 {value:.1f}s"

    sweeps_expected = len(expected_by_zone)
    sweeps_completed = sum(
        1 for zone in expected_by_zone
        if (warden.sweeps.get(zone) and warden.sweeps[zone].is_complete))
    zones_with_headcount = sum(
        1 for zone in expected_by_zone
        if (warden.sweeps.get(zone)
            and warden.sweeps[zone].headcounts.latest is not None))

    overrides = audit.overrides(drill.drill_id) if audit else []
    disclosures = audit.disclosures(drill.drill_id) if audit else []

    # `all_disputes` existed and nothing called it, so the one outcome
    # invariant 3 produces reached no document. Named by track rather than by
    # person on purpose: a disputed track is exactly one the system cannot
    # attach to a person, and calling it by a name would be the adjudication
    # the invariant forbids.
    disputes = tuple(
        f"track {d.subject}: claimed as {' and '.join(d.identities)}, "
        f"first at {d.first_seen_ms}"
        for d in drill.ingestor.state.ledger.all_disputes())

    # A store that dropped events makes this report unverifiable, and until now
    # the number lived only in `StoreStats`. A drill with no store at all has
    # dropped nothing -- it never promised to keep anything -- and says so
    # through `is_durable` instead.
    dropped = (drill.events_store.stats.dropped
               if drill.events_store is not None else 0)

    validation = validate(
        false_accounted=len(false_accounted),
        false_unaccounted=len(false_unaccounted),
        sweeps_completed=sweeps_completed,
        sweeps_expected=sweeps_expected,
        zones_with_headcount=zones_with_headcount,
        headcount_mismatches=len(mismatches),
        timing_samples=timing.building.sample_size,
        timing_coverage=timing.building.coverage,
        p95_s=timing.building.p95,
        p95_reliable=timing.building.is_reliable,
        blind_fraction=board.health.blind_fraction,
        events_dropped=dropped,
        thresholds=thresholds)

    return DrillReport(
        drill_id=drill.drill_id, name=drill.name, site_id=drill.site_id,
        started_ms=drill.started_ms, completed_ms=drill.completed_ms,
        duration_s=(drill.elapsed_ms(now_ms) / 1000
                    if drill.started_ms is not None else None),
        expected=board.expected, accounted=board.accounted,
        unaccounted=board.unaccounted,
        uncertain=board.count(AccountabilityState.UNCERTAIN),
        needing_verification=board.count(
            AccountabilityState.MANUAL_VERIFICATION_REQUIRED),
        unknown_people=board.unknown_people,
        unknown_by_zone=warden.unknowns_by_zone(),
        excluded_from_the_count=board.excluded_from_the_count,
        false_accounted=tuple(false_accounted),
        false_unaccounted=tuple(false_unaccounted),
        p50_s=timing.building.p50, p90_s=timing.building.p90,
        p95_s=timing.building.p95, p99_s=timing.building.p99,
        max_s=timing.building.maximum,
        timing_samples=timing.building.sample_size,
        timing_coverage=timing.building.coverage,
        timing_caveats=timing.building.caveats(),
        timing_exclusions=dict(timing.building.exclusion_reasons),
        accountability_completion_s=timing.accountability_completion_s,
        slowest_floor=slowest,
        warden_confirmations=len(confirmed_by_warden),
        warden_rejections=sum(
            1 for a in warden.actions if a.kind is ActionKind.WRONG_PERSON),
        warden_overrides=len(overrides),
        overrides_in_full=tuple(e.describe() for e in overrides),
        disclosures=tuple(e.describe() for e in disclosures),
        sweeps_completed=sweeps_completed, sweeps_expected=sweeps_expected,
        zones_with_headcount=zones_with_headcount,
        headcount_mismatches=tuple(m.summary() for m in mismatches),
        tolerated_overcounts=tuple(
            f"{h.summary()} This site's policy absorbed it; no mismatch was "
            "recorded." for h in absorbed),
        escalations=tuple(
            s.escalation_reason or "no reason recorded"
            for s in warden.escalations()),
        count_history=tuple(_count_line(h) for h in warden.count_history()),
        outages=board.health.total_outages,
        blind_fraction=board.health.blind_fraction,
        longest_blind_s=board.health.longest_blind_ms / 1000,
        health_caveat=board.health.caveat(),
        identity_disputes=disputes,
        events_dropped=dropped,
        validation=validation,
        thresholds_calibrated=drill.identity_config.calibrated,
    )
