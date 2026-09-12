"""Drill-wide warden state, and the bridge into the accountability board.

Holds every zone's sweep and every device's queue, and derives the
`WardenEvidence` the accountability machine consumes. The derivation is the
interesting part, because it is where a human's assertion becomes something the
software acts on, and the rules for that have to be conservative in one specific
direction.

**A confirmation counts as assembly-zone presence only if the warden was at an
assembly zone.** A warden checking a floor is doing something valuable, but
recognising a colleague in a corridor is not evidence they got out.

**The most recent assertion about a person wins, but nothing is erased.** A
warden who confirms someone and then reports them not here has changed their
mind, and the later statement is the operative one. Both stay in the record so
the reversal is visible.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.accountability_fsm import WardenEvidence
from app.warden.actions import ActionKind, DeviceQueue, WardenAction
from app.warden.headcount import Headcount, Severity
from app.warden.sweep import SweepState


@dataclass
class WardenState:
    """Every warden's work on one drill."""

    sweeps: dict = field(default_factory=dict)
    devices: dict = field(default_factory=dict)
    actions: list = field(default_factory=list)
    #: assembly zone ids, so a confirmation made elsewhere is not mistaken for
    #: evidence that someone reached safety.
    assembly_zones: frozenset = frozenset()

    def sweep(self, zone_id: str) -> SweepState:
        state = self.sweeps.get(zone_id)
        if state is None:
            state = SweepState(zone_id=zone_id)
            self.sweeps[zone_id] = state
        return state

    def device(self, device_id: str, warden_id: str) -> DeviceQueue:
        queue = self.devices.get(device_id)
        if queue is None:
            queue = DeviceQueue(device_id=device_id, warden_id=warden_id)
            self.devices[device_id] = queue
        return queue

    def tagged_unknowns(self) -> int:
        """People at assembly points who were not on any warden's list.

        A visitor who never signed in, a contractor, somebody from the building
        next door. They are not in `expected`, so no row on the board mentions
        them and no count includes them -- this is the only number that says
        the building held people the roster did not know about.

        Wardens, not cameras. A camera can only offer tracks it could not put a
        name to, and fragmentation makes five tracks out of one person, so a
        track count presented as a headcount would be an invented number on a
        life-safety screen. A warden standing in front of somebody is counting
        people.
        """
        return sum(sweep.unknown_tagged for sweep in self.sweeps.values())

    def unknowns_by_zone(self) -> dict:
        """Where they were tagged. Only zones with any."""
        return {zone_id: sweep.unknown_tagged
                for zone_id, sweep in sorted(self.sweeps.items())
                if sweep.unknown_tagged}

    # -- applying actions ------------------------------------------------------

    def apply(self, action: WardenAction) -> None:
        """Fold one action into the drill's warden state."""
        self.actions.append(action)
        sweep = self.sweep(action.zone_id)
        kind = action.kind

        if kind is ActionKind.CONFIRM_PRESENT and action.subject:
            sweep.confirm(action.subject, action.warden_id, action.ts_ms)
        elif kind is ActionKind.NOT_HERE and action.subject:
            sweep.report_not_here(action.subject, action.warden_id, action.ts_ms)
        elif kind is ActionKind.MARK_ABSENT and action.subject:
            sweep.mark_absent(action.subject, action.warden_id, action.ts_ms)
        elif kind is ActionKind.TAG_UNKNOWN:
            sweep.tag_unknown(action.warden_id, action.ts_ms)
        elif kind is ActionKind.SWEEP_COMPLETE:
            sweep.complete(action.warden_id, action.ts_ms)
        elif kind is ActionKind.ESCALATE:
            sweep.escalate(action.note or "no reason recorded",
                           action.warden_id, action.ts_ms)
        elif kind is ActionKind.NOTE:
            sweep.note(action.note or "", action.warden_id, action.ts_ms)
        elif kind is ActionKind.WRONG_PERSON and action.subject:
            # Handled by the identity machine through the event stream. Recorded
            # here so the zone panel shows the warden did something.
            sweep.note(f"rejected identity {action.identity} for {action.subject}",
                       action.warden_id, action.ts_ms)

    def record_headcount(self, headcount: Headcount) -> Headcount:
        return self.sweep(headcount.zone_id).record_headcount(headcount)

    # -- deriving evidence -----------------------------------------------------

    def evidence_for(self, person_ref: str) -> WardenEvidence:
        """What the wardens collectively established about one person.

        The latest assertion wins. Earlier ones stay in `self.actions`, so a
        reversal is visible in the audit log even though only one of them is
        operative.
        """
        relevant = [a for a in self.actions
                    if a.subject == person_ref and a.is_about_a_person]
        if not relevant:
            return WardenEvidence()

        latest = max(relevant, key=lambda a: a.ts_ms)
        at_assembly = (latest.zone_id
                       if latest.zone_id in self.assembly_zones else None)

        return WardenEvidence(
            confirmed=latest.kind is ActionKind.CONFIRM_PRESENT,
            rejected=latest.kind is ActionKind.WRONG_PERSON,
            at_assembly_zone=(at_assembly
                              if latest.kind is ActionKind.CONFIRM_PRESENT
                              else None),
            marked_absent=latest.kind is ActionKind.MARK_ABSENT,
            warden_id=latest.warden_id,
            ts_ms=latest.ts_ms,
        )

    def all_evidence(self) -> dict:
        subjects = {a.subject for a in self.actions
                    if a.subject and a.is_about_a_person}
        return {ref: self.evidence_for(ref) for ref in subjects}

    # -- the command centre's view --------------------------------------------

    def mismatches(self) -> list:
        """Zones whose physical count disagrees with the system, worst first."""
        out = []
        for zone_id, sweep in self.sweeps.items():
            latest = sweep.headcounts.latest
            if latest is not None and latest.is_mismatch:
                out.append(latest)
        return sorted(out, key=lambda h: (h.severity is not Severity.ESCALATE,
                                          -h.difference))

    def tolerated_overcounts(self) -> list:
        """Counts a site's own tolerance absorbed rather than reported.

        Not mismatches -- the site decided that -- and not nothing either. They
        reach the drill report so the decision is visible next to its
        consequence.
        """
        out = []
        for sweep in self.sweeps.values():
            latest = sweep.headcounts.latest
            if latest is not None and latest.tolerated_overcount:
                out.append(latest)
        return sorted(out, key=lambda h: -h.tolerated_overcount)

    def count_history(self) -> list:
        """How each zone's physical count moved, where it moved at all.

        The report reads `headcounts.latest` everywhere, so a zone that counted
        38 against the system's 40 and then agreed at 40 appears clean and the
        moment of disagreement is gone. `ever_escalated` says in its own
        docstring that a post-drill report should say so rather than showing
        only the reassuring final number, and nothing called it.

        Two things are worth surfacing and neither is the final figure: a count
        that reached the dangerous direction at any point, and a warden whose
        own counts disagree with each other. The first means two people were
        unaccounted for a while. The second means the count itself is shaky,
        which is a different problem and needs a different response.

        Zones that were counted once and agreed are left out: this is a list of
        exceptions, and padding it with the quiet zones buries them.
        """
        out = []
        for zone_id, sweep in sorted(self.sweeps.items()):
            counts = sweep.headcounts.counts
            if not counts:
                continue
            escalated = sweep.headcounts.ever_escalated
            unstable = sweep.headcounts.unstable()
            if not escalated and not unstable:
                continue
            out.append({
                "zone_id": zone_id,
                "physical_counts": [c.physical_count for c in counts],
                "system_count": counts[-1].system_count,
                "ever_escalated": escalated,
                "unstable": unstable,
                "settled_now": sweep.headcounts.is_settled,
                "first_escalated_ms": next(
                    (c.ts_ms for c in counts
                     if c.severity is Severity.ESCALATE), None),
            })
        return out

    def escalations(self) -> list:
        return [s for s in self.sweeps.values() if s.is_escalated]

    def stale_devices(self, now_ms: int, *, threshold_ms: int = 120_000) -> list:
        """Devices holding unsynced work for longer than they should.

        The device's own view of its queue. Nothing on the server populates
        `pending` -- actions arrive already synced -- so this reports nothing
        there, and `silent_devices` is the question the command centre can
        actually answer.
        """
        return [
            {"device_id": queue.device_id, "warden_id": queue.warden_id,
             "pending": queue.depth, "stale_ms": queue.staleness_ms(now_ms)}
            for queue in self.devices.values()
            if (queue.staleness_ms(now_ms) or 0) >= threshold_ms
        ]

    def silent_devices(self, now_ms: int) -> list:
        """Every device that has spoken to this drill, and how long ago.

        No threshold here on purpose. How long a warden's tablet may be quiet
        before somebody walks over to it is an operational decision a site
        makes, not a number this can derive, and reporting the duration lets
        the screen decide while the record keeps the fact.

        A zone with a silent warden and a zone with a warden still walking it
        look identical on the board: neither is swept. During an evacuation one
        means wait and the other means send somebody.
        """
        return sorted(
            ({"device_id": queue.device_id, "warden_id": queue.warden_id,
              "zone_id": queue.last_zone_id,
              "silent_ms": queue.silence_ms(now_ms)}
             for queue in self.devices.values()
             if queue.last_contact_ms is not None),
            key=lambda row: -row["silent_ms"])

    def heard_from(self, device_id: str, warden_id: str, now_ms: int,
                   zone_id: str | None = None) -> None:
        self.device(device_id, warden_id).heard_from(now_ms, zone_id)

    def silence_for_zone(self, zone_id: str, now_ms: int) -> int | None:
        """The quietest device covering a zone, or None if none has spoken.

        The quietest rather than the most recent: if two devices cover a zone
        and one has gone away, that is the fact worth surfacing.
        """
        silences = [queue.silence_ms(now_ms)
                    for queue in self.devices.values()
                    if queue.last_zone_id == zone_id
                    and queue.last_contact_ms is not None]
        return max(silences) if silences else None

    def zone_panels(self, expected_by_zone: dict,
                    now_ms: int | None = None) -> list:
        """One summary per zone, for the command centre's assembly panel."""
        panels = []
        for zone_id, expected in sorted(expected_by_zone.items()):
            sweep = self.sweeps.get(zone_id) or SweepState(zone_id=zone_id)
            panel = sweep.summary({e.person_ref for e in expected})
            panel["warden_silent_ms"] = (
                None if now_ms is None else self.silence_for_zone(zone_id, now_ms))
            panels.append(panel)
        return panels

    def is_every_zone_clean(self, expected_by_zone: dict) -> bool:
        """Whether every zone's warden has finished and agrees with the system.

        The manual half of the all-clear. The board's own counts are the other
        half, and both are required: a drill where the cameras are content and
        no warden has walked their zone is not an all-clear, it is an untested
        assumption.
        """
        if not expected_by_zone:
            return False
        for zone_id, expected in expected_by_zone.items():
            sweep = self.sweeps.get(zone_id)
            if sweep is None:
                return False
            if not sweep.is_clean({e.person_ref for e in expected}):
                return False
        return True

    def blocking_clean(self, expected_by_zone: dict) -> list:
        reasons = []
        if not expected_by_zone:
            return ["no zones have any expected people"]
        for zone_id, expected in sorted(expected_by_zone.items()):
            sweep = self.sweeps.get(zone_id)
            if sweep is None:
                reasons.append(f"{zone_id}: no warden has started a sweep")
                continue
            for reason in sweep.blocking_clean({e.person_ref for e in expected}):
                reasons.append(f"{zone_id}: {reason}")
        return reasons
