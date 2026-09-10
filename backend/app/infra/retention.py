"""Retention: how long each kind of data lives, and proof that it went.

Face embeddings and face crops are biometric data. Under most regimes that puts
them in a special category with a higher bar than "we'll tidy up eventually",
and a drill generates them by the thousand.

The policy is organised around one distinction that matters more than any
duration in it:

    **Evidence is not biometrics.** "A face matched EMP-482 at 10:41 with a
    score above threshold on camera 9" is the evidence, and a post-incident
    report needs it for years. The 512-float embedding that produced it, and the
    112×112 crop it came from, are needed for exactly as long as the matching
    takes. Keeping them because they are in the same pipeline is how a drill
    system becomes a biometric database nobody signed up for.

So the ledger keeps the *claim* and the retention policy purges the *material*.
`explain()` works forever; the face it was derived from does not survive the
drill.

A policy that cannot be verified is a promise rather than a control, so every
purge writes an audit entry saying what class it removed and how many items,
and `verify` reports anything that outlived its class.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from app.infra.audit import AuditAction, AuditLog


class DataClass(str, Enum):
    """What kind of thing this is, which decides how long it may live."""

    FACE_EMBEDDING = "FACE_EMBEDDING"
    """A 512-float vector. Biometric. Needed only while matching."""

    FACE_CROP = "FACE_CROP"
    """An aligned 112×112 image of somebody's face. Biometric."""

    SNAPSHOT = "SNAPSHOT"
    """A frame or person crop shown beside a person on the priority list, so an
    operator can see who they are being asked about."""

    DEVICE_THUMBNAIL = "DEVICE_THUMBNAIL"
    """A snapshot cached on a warden's tablet. Leaves the building in someone's
    hands, so it is the shortest-lived class in the system."""

    EVENT = "EVENT"
    """An observation. Not biometric: it references a person, it does not
    contain their face."""

    EVIDENCE = "EVIDENCE"
    """The ledger. What `explain()` reads."""

    AUDIT = "AUDIT"
    """Who did what. Outlives everything, because it is the record of the
    record."""

    DRILL_REPORT = "DRILL_REPORT"


#: Data that is biometric and therefore special category.
BIOMETRIC: frozenset[DataClass] = frozenset({
    DataClass.FACE_EMBEDDING, DataClass.FACE_CROP,
    DataClass.SNAPSHOT, DataClass.DEVICE_THUMBNAIL,
})


class Trigger(str, Enum):
    """When the clock starts, which is not always "when it was created"."""

    IMMEDIATE = "IMMEDIATE"
    """Purged as soon as the thing that needed it is done with it."""

    DRILL_END = "DRILL_END"
    AGE = "AGE"


@dataclass(frozen=True, slots=True)
class Rule:
    data_class: DataClass
    trigger: Trigger
    max_age_ms: int | None = None
    rationale: str = ""

    def __post_init__(self) -> None:
        if self.trigger is Trigger.AGE and not self.max_age_ms:
            raise ValueError(
                f"{self.data_class.value} is retained by age with no age given")
        if self.trigger is not Trigger.AGE and self.max_age_ms:
            raise ValueError(
                f"{self.data_class.value} has an age but is not retained by age")

    def expires_at(self, created_ms: int, drill_ended_ms: int | None) -> int | None:
        """When this item must be gone. None means it has no expiry."""
        if self.trigger is Trigger.IMMEDIATE:
            return created_ms
        if self.trigger is Trigger.DRILL_END:
            return drill_ended_ms
        return created_ms + (self.max_age_ms or 0)


#: The default policy.
#:
#: The durations here are not calibrated against a legal opinion and are marked
#: as such. What is not provisional is the *shape*: biometric material dies with
#: the drill or sooner, evidence outlives it by years, and the audit log outlives
#: the evidence.
DEFAULT_RULES: tuple[Rule, ...] = (
    Rule(DataClass.FACE_EMBEDDING, Trigger.IMMEDIATE,
         rationale="Needed only to produce a match. The match is kept; the "
                   "vector that produced it is not."),
    Rule(DataClass.FACE_CROP, Trigger.IMMEDIATE,
         rationale="Needed only to produce an embedding."),
    Rule(DataClass.DEVICE_THUMBNAIL, Trigger.DRILL_END,
         rationale="Leaves the building in a warden's hands. Purged when the "
                   "drill ends, whether or not the device has synced."),
    Rule(DataClass.SNAPSHOT, Trigger.AGE, max_age_ms=7 * 24 * 3_600_000,
         rationale="An operator reviewing a drill needs to see who they were "
                   "asked about. A week covers the debrief."),
    Rule(DataClass.EVENT, Trigger.AGE, max_age_ms=365 * 24 * 3_600_000,
         rationale="Not biometric. Needed to reconstruct any decision in the "
                   "drill for as long as the drill can be questioned."),
    Rule(DataClass.EVIDENCE, Trigger.AGE, max_age_ms=365 * 24 * 3_600_000,
         rationale="What explain() reads. Outlives the faces it describes."),
    Rule(DataClass.DRILL_REPORT, Trigger.AGE, max_age_ms=7 * 365 * 24 * 3_600_000,
         rationale="A fire drill record is a compliance artefact."),
    Rule(DataClass.AUDIT, Trigger.AGE, max_age_ms=7 * 365 * 24 * 3_600_000,
         rationale="The record of the record. Outlives what it describes, or it "
                   "cannot show what happened to it."),
)


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    rules: tuple = DEFAULT_RULES
    calibrated: bool = False
    source: str = "Phase 4 default, not reviewed against a legal opinion"

    def __post_init__(self) -> None:
        covered = {rule.data_class for rule in self.rules}
        missing = set(DataClass) - covered
        if missing:
            # An unclassified item is one nobody decided about, and it will live
            # forever by default. That is exactly how a drill system becomes a
            # biometric database nobody signed up for.
            raise ValueError(
                "every data class needs a rule; missing: "
                + ", ".join(sorted(c.value for c in missing)))

    def rule_for(self, data_class: DataClass) -> Rule:
        for rule in self.rules:
            if rule.data_class is data_class:
                return rule
        raise KeyError(data_class)

    @property
    def biometric_rules(self) -> tuple:
        return tuple(r for r in self.rules if r.data_class in BIOMETRIC)

    def longest_biometric_life_ms(self, drill_length_ms: int) -> int:
        """The worst case a data-protection review will ask about."""
        worst = 0
        for rule in self.biometric_rules:
            if rule.trigger is Trigger.IMMEDIATE:
                continue
            if rule.trigger is Trigger.DRILL_END:
                worst = max(worst, drill_length_ms)
            elif rule.max_age_ms:
                worst = max(worst, rule.max_age_ms)
        return worst

    def describe(self) -> list[str]:
        lines = ["EVAC-120 retention policy",
                 f"  reviewed: {'yes' if self.calibrated else 'NO — ' + self.source}",
                 ""]
        for rule in self.rules:
            marker = "  [biometric] " if rule.data_class in BIOMETRIC else "  "
            if rule.trigger is Trigger.IMMEDIATE:
                when = "immediately after use"
            elif rule.trigger is Trigger.DRILL_END:
                when = "at drill end"
            else:
                when = f"after {(rule.max_age_ms or 0) / 86_400_000:.0f} days"
            lines.append(f"{marker}{rule.data_class.value}: purged {when}")
            lines.append(f"      {rule.rationale}")
        return lines


@dataclass
class Item:
    """One retainable thing. The store is abstract on purpose: MinIO, disk and
    a device's cache all obey the same policy."""

    item_id: str
    data_class: DataClass
    created_ms: int
    drill_id: str | None = None
    location: str = ""
    purged_ms: int | None = None

    @property
    def is_purged(self) -> bool:
        return self.purged_ms is not None


@dataclass
class RetentionLedger:
    """Tracks retainable items and purges them on policy.

    Holds references, not content. A retention ledger that stored the faces it
    is meant to delete would be the problem it exists to solve.
    """

    policy: RetentionPolicy = field(default_factory=RetentionPolicy)
    items: dict = field(default_factory=dict)

    def track(self, item: Item) -> Item:
        self.items[item.item_id] = item
        return item

    def due(self, now_ms: int, drill_ended_ms: dict | None = None) -> list:
        """Everything that should already be gone."""
        drill_ended_ms = drill_ended_ms or {}
        out = []
        for item in self.items.values():
            if item.is_purged:
                continue
            rule = self.policy.rule_for(item.data_class)
            expiry = rule.expires_at(item.created_ms,
                                     drill_ended_ms.get(item.drill_id))
            if expiry is not None and now_ms >= expiry:
                out.append(item)
        return out

    def purge(
        self, now_ms: int, *, actor_id: str = "system",
        audit: AuditLog | None = None, drill_ended_ms: dict | None = None,
        remover=None,
    ) -> dict:
        """Purge everything due. Returns what went, by class.

        `remover` actually deletes the bytes and is expected to raise if it
        cannot. An item whose removal failed is **not** marked purged: recording
        a deletion that did not happen would make the policy a lie that passes
        its own verification.
        """
        removed: dict[str, int] = {}
        failed: list[str] = []

        for item in self.due(now_ms, drill_ended_ms):
            try:
                if remover is not None:
                    remover(item)
            except Exception:
                failed.append(item.item_id)
                continue
            item.purged_ms = now_ms
            removed[item.data_class.value] = removed.get(item.data_class.value, 0) + 1

        if audit is not None and (removed or failed):
            audit.record(
                action=AuditAction.RETENTION_PURGE, actor_id=actor_id,
                ts_ms=now_ms,
                summary=(f"purged {sum(removed.values())} item(s)"
                         + (f", {len(failed)} failed" if failed else "")),
                removed=removed, failed=len(failed))

        return {"removed": removed, "failed": failed,
                "total": sum(removed.values())}

    def purge_drill(self, drill_id: str, ended_ms: int, **kwargs) -> dict:
        """Everything a finished drill should no longer be holding."""
        return self.purge(ended_ms, drill_ended_ms={drill_id: ended_ms}, **kwargs)

    def verify(self, now_ms: int, drill_ended_ms: dict | None = None) -> dict:
        """What has outlived its class. Empty is the only acceptable result.

        A policy nobody checks is a promise. This is the check, and it is
        deliberately blunt: any biometric item past its expiry is a finding, not
        a warning.
        """
        overdue = self.due(now_ms, drill_ended_ms)
        biometric = [i for i in overdue if i.data_class in BIOMETRIC]
        return {
            "compliant": not overdue,
            "overdue": len(overdue),
            "overdue_biometric": len(biometric),
            "items": [i.item_id for i in overdue[:50]],
            "finding": (
                f"{len(biometric)} biometric item(s) have outlived their "
                "retention class"
            ) if biometric else None,
        }

    def held(self, data_class: DataClass | None = None) -> list:
        return [i for i in self.items.values()
                if not i.is_purged
                and (data_class is None or i.data_class is data_class)]
