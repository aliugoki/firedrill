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
    roster_trustworthy: bool = True
    roster_gaps: dict = {}
    """Locally-owned fields nobody filled in, counted at the moment a drill is
    created -- which is the last moment an operator can do anything about them.
    `RosterSnapshot.coverage_gaps` says an unassigned assembly zone means
    nobody owns that person during a sweep, and it was computed and read by
    nothing, so a site found out at minute four."""


class PersonRowOut(BaseModel):
    person_ref: str
    display_name: str
    department: str | None
    assigned_assembly_zone: str | None
    state: str
    colour: str
    reason: str
    """English prose, and the fallback. A client that does not recognise the
    code below shows this rather than nothing, because an English sentence
    beats a blank line under somebody's name."""
    reason_code: str | None = None
    reason_detail: dict = {}
    """The same reason as something a screen can word in the language it is
    being read in. The warden PWA is used in Arabic on a tablet at an assembly
    point and this line is what tells a warden what to do about the person."""
    qualifying_evidence: list[str] = []
    blockers: list[str] = []
    last_zone_id: str | None = None
    last_camera_id: str | None = None
    last_seen_ms: int | None = None
    needs_human_to_account: bool = False
    blinded_by: list[str] = []
    """Cameras that were dark when this person was last seen, or that are
    blinding them now. The answer to why the system lost sight of somebody, as
    opposed to the fact that it did."""


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


class AuditEntryOut(BaseModel):
    """One line of the record of who did what."""

    entry_id: str
    action: str
    actor_id: str
    ts_ms: int
    subject: str | None = None
    summary: str = ""
    is_override: bool = False
    """A human changed what the system believed. `AuditLog.overrides` calls
    this the first thing anyone reviewing a drill should read, because it is
    the list of places where the recorded outcome is not the one the evidence
    produced."""
    is_disclosure: bool = False
    before: dict | None = None
    after: dict | None = None


class AuditOut(BaseModel):
    drill_id: str
    entries: list[AuditEntryOut]
    durable: bool
    """Whether these came from the database or from this process's memory. An
    in-memory log is complete only for what this process did, and is gone on a
    restart, which changes what the list can be used to prove."""
    summary: dict


class DisputeOut(BaseModel):
    """Two identities claimed for one track, reported and never adjudicated."""

    subject: str
    identities: list[str]
    first_seen_ms: int
    evidence: list[EvidenceOut]


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
    disputes: list[DisputeOut] = []
    """Who the competing claims are for. `is_disputed` said that there were
    some and never which, so the drawer could say the system cannot settle this
    person's identity without saying between whom -- and invariant 3's whole
    point is that a conflict is reported rather than resolved."""
    blindness: list[EvidenceOut] = []
    """Everything here that means "we could not see". Already inside `context`
    and indistinguishable there from an ordinary observation. It is the answer
    to the question a warden actually asks about somebody unaccounted for."""


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
    settled: list[int] = []
    """The device sequence numbers this server is now holding: everything it
    accepted, plus the duplicates it already had.

    The device deletes what it believes landed, and it had to infer that from
    "everything I sent that was not refused". That reads an unrecognised
    response as total success -- a proxy that rewrote the body, a captive
    portal at the assembly point answering 200, a field renamed in a later
    version -- and deletes a warden's whole queue. `queue.js` says "remove only
    what the server confirmed"; a filter over everything sent is the same
    clear-on-success wearing a filter.

    Duplicates are in here on purpose. The server already has them, so the
    device keeping them would resend forever."""
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


class DrillReportOut(BaseModel):
    """The post-drill report, as the verdict plus the text a person reads.

    The rendered lines are the artefact -- what gets filed and quoted in a
    post-incident review -- and the structured fields are there so a dashboard
    does not have to parse them back out of prose.
    """

    drill_id: str
    outcome: str | None
    summary: str | None
    is_safe_result: bool
    false_accounted: int
    false_unaccounted: int
    p95_s: float | None
    rendered: list[str]


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
    warden_silent_ms: int | None = None
    """How long since this zone's warden device was last heard from. None
    means it has never spoken, which reads on the screen as a zone with no
    warden rather than a warden who has gone quiet."""


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
