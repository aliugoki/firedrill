"""Request and response shapes for /api/evac/*.

Two rules run through all of them.

**No raw AI metrics on an operator response.** Match scores, margins and vote
counts are real and they are in the ledger, but putting them on the live board
invites an operator to second-guess a threshold mid-evacuation. What reaches the
screen is a state, a colour, and a reason in words. The numbers are one click
away in the explain drawer, which is where someone deciding whether to trust a
call should be looking.

**Every state carries its reason.** A response that says UNCERTAIN without
saying why is a response nobody can act on.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class CreateDrill(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    site_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)


class DrillSummary(BaseModel):
    drill_id: str
    name: str
    site_id: str
    status: str
    created_ms: int
    started_ms: int | None = None
    completed_ms: int | None = None
    expected: int


class PersonRowOut(BaseModel):
    person_ref: str
    display_name: str
    department: str | None
    assigned_assembly_zone: str | None
    state: str
    colour: str
    reason: str
    qualifying_evidence: list[str] = []
    blockers: list[str] = []
    last_zone_id: str | None = None
    last_camera_id: str | None = None
    last_seen_ms: int | None = None
    needs_human_to_account: bool = False


class HealthOut(BaseModel):
    degraded: bool
    blind: bool
    open_outages: int
    total_outages: int
    blind_fraction: float
    longest_blind_ms: int
    caveat: str | None = None


class BoardOut(BaseModel):
    drill_id: str
    now_ms: int
    elapsed_ms: int
    status: str

    expected: int
    accounted: int
    uncertain: int
    unaccounted: int
    currently_unobserved: int
    still_evacuating: int
    unknown_people: int
    excluded_from_the_count: dict = {}
    """On the roster and not in `expected`, by reason. No row on the board
    mentions these people and nobody is looking for them, so the number reaches
    an operator here or not at all."""

    all_clear: bool
    blocking_all_clear: list[str]
    roster_trustworthy: bool
    health: HealthOut
    rows: list[PersonRowOut]


class PercentilesOut(BaseModel):
    label: str
    sample_size: int
    excluded: int
    p50: float | None
    p90: float | None
    p95: float | None
    p99: float | None
    maximum: float | None
    reliable: bool
    coverage: float | None
    exclusion_reasons: dict = {}
    """Why the people with no timing have none, by reason.

    The count alone hides the difference that matters: somebody the cameras
    never saw is a coverage problem, and somebody who was seen and never
    reached the muster point is a person. Both are excluded from the
    percentiles and only one of them is a measurement problem."""
    caveats: list[str] = []
    """Everything that qualifies these numbers. Plural because a small sample
    and a low coverage are different problems and a reader needs both."""


class TimingOut(BaseModel):
    building: PercentilesOut
    by_floor: dict[str, PercentilesOut]
    by_zone: dict[str, PercentilesOut]
    accountability_completion_s: float | None
    target_p95_s: float
    meets_target: bool | None
    """None is a real answer: the drill did not produce enough data to say,
    which is different from failing and very different from passing."""


class EvidenceOut(BaseModel):
    ts_ms: int
    kind: str
    stance: str
    summary: str
    source: str | None = None
    identity: str | None = None
    is_human: bool = False


class ExplanationOut(BaseModel):
    subject: str
    decision: str | None
    decided_at_ms: int | None
    is_disputed: bool
    has_human_confirmation: bool
    narrative: list[str]
    supporting: list[EvidenceOut]
    contradicting: list[EvidenceOut]
    context: list[EvidenceOut]


class WardenActionIn(BaseModel):
    """One assertion from a warden device.

    `device_seq` is assigned by the device, not here. That is what makes an
    offline queue orderable and a lost action distinguishable from a late one.
    """

    kind: str
    warden_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    zone_id: str = Field(min_length=1)
    ts_ms: int = Field(ge=0)
    device_seq: int = Field(ge=1)
    subject: str | None = None
    identity: str | None = None
    note: str | None = None
    queued_offline: bool = False


class WardenSyncIn(BaseModel):
    """A device draining its queue. Ordered by the device's own sequence."""

    actions: list[WardenActionIn]


class Refusal(BaseModel):
    """One action the server would not take, and why."""

    device_seq: int
    reason: str


class WardenSyncOut(BaseModel):
    accepted: int
    duplicates: int
    refusals: list[Refusal] = []
    """Which actions were refused, keyed by the device's own sequence number.

    Structured because the device deletes what it believes landed. It used to
    read the sequence back out of `rejected` with a regular expression over the
    prose, which made the wording of an error message a wire contract: rephrase
    it and every refusal silently reads as an acceptance, and the device throws
    away a warden's confirmation while telling them it synced."""

    rejected: list[str] = []
    """The same refusals as sentences, for display and for older devices. Kept
    because a device and a server can be different versions -- the edge node
    updates on its own schedule -- and losing confirmations during an upgrade is
    exactly the failure this pair of fields exists to avoid."""


class HeadcountIn(BaseModel):
    zone_id: str = Field(min_length=1)
    warden_id: str = Field(min_length=1)
    device_id: str = Field(min_length=1)
    ts_ms: int = Field(ge=0)
    physical_count: int = Field(ge=0)
    note: str | None = None


class HeadcountOut(BaseModel):
    zone_id: str
    physical_count: int
    system_count: int
    difference: int
    kind: str
    severity: str
    missing_from_the_muster_point: int
    summary: str
    recommended_action: str


class ExitMeasureOut(BaseModel):
    zone_id: str
    completed: int
    queue: int
    throughput_per_min: float | None
    median_dwell_s: float | None
    worst_dwell_s: float | None
    capacity: int | None
    density: float | None
    """None when no capacity was recorded for the zone. A crowding figure
    against an invented denominator is worse than none: it is the kind of
    number that ends up in a report."""

    is_congested: bool


class BottlenecksOut(BaseModel):
    exits: list[ExitMeasureOut]
    limiting_zone_id: str | None
    total_through: int
    measured_over_s: float
    caveats: list[str] = []


class ZonePanelOut(BaseModel):
    zone_id: str
    warden_id: str | None
    status: str
    expected: int
    confirmed: int
    not_here: int
    marked_absent: int
    outstanding: int
    unknown_tagged: int
    physical_count: int | None
    system_count: int | None
    mismatch: str | None
    severity: str | None
    is_clean: bool
    blocking: list[str]


class WardenZoneOut(BaseModel):
    """What a warden's device caches at drill start and works from offline."""

    drill_id: str
    zone_id: str
    warden_id: str
    cached_at_ms: int
    panel: ZonePanelOut
    roster: list[PersonRowOut]
    system_health: HealthOut
    """On the warden's screen too, so they know when to rely on their own count
    rather than the list in front of them."""
