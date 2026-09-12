"""EVAC-120 accountability core — pure Python.

No database, no Redis, no HTTP, no framework imports. Everything here is
deterministic and testable without infrastructure, so the Phase 1 simulator can
drive it directly and the Phase 3 ingest service feeds it real events through
exactly the same interface. If a module in this package ever needs a connection,
it belongs in `app/ingest` or `app/api` instead.

Phase 1 lands, in dependency order:

    events.py             event schema + validators (docs/EVAC120.md, §5)
    presence_fsm.py       NOT_OBSERVED -> IN_BUILDING -> IN_TRANSIT ->
                          ASSEMBLY_PRESENT, plus TEMPORARILY_UNOBSERVED and LOST
    identity_fsm.py       UNKNOWN -> CANDIDATE -> CONFIRMED, plus
                          TEMPORARILY_UNAVAILABLE, CONFLICT, REJECTED
    accountability_fsm.py NOT_EVACUATED -> EVACUATING -> ACCOUNTED, plus
                          UNCERTAIN, UNACCOUNTED, MANUAL_VERIFICATION_REQUIRED
    ledger.py             evidence ledger, conflict detection, explain(subject)
    fusion.py             face and body evidence joined on one global person id
    timing.py             P50/P90/P95/P99 per person, zone, floor, building
    roster.py             expected set from FaceTrack plus visitor sign-in

`identity_fsm.py` was written from the vendored `TrackIdentityManager`
(`app/vendor/deepstream/recognition.py`) and imports nothing from it. That class
has two identity outcomes, committed or nothing, and the two-state model is the
gap EVAC-120 closes. It is also keyed on a camera-local tracker object id;
EVAC-120 keys identity on the **global person id**. Extending it was the Phase 0
plan and is not what happened: the voting and sticky-commit ideas carried over,
the code did not, and the vendored copy is not in the running path.

The nine invariants. Every module here, and every test, is written to them:

 1. Absence of evidence is not evidence of absence. An unknown face is
    FACE_UNAVAILABLE, an offline camera is CAMERA_DEGRADED, a weak match is
    CANDIDATE. None of them is MISSING.
 2. A strong identity is never overwritten by a weak or unknown observation
    while the track is still reliable.
 3. Conflicting evidence is never silently resolved. It raises
    IDENTITY_CONFLICT, then MANUAL_VERIFICATION_REQUIRED.
 4. Identity, presence and accountability are three separate state machines.
    There is no is_evacuated boolean anywhere in this codebase.
 5. Re-identification similarity is not employee identity. An unknown person is
    tracked as unknown and never forced onto an employee record.
 6. Every accountability decision is reconstructable from stored events.
    "Why was EMP-00482 marked ACCOUNTED?" returns the evidence list.
 7. Thresholds are configuration, validated against the Phase 2 calibration set.
    No production number is invented in code.
 8. Any infrastructure failure degrades to DEGRADED or
    MANUAL_VERIFICATION_REQUIRED, never to a false ALL CLEAR.
 9. Human confirmation by a warden is first-class evidence and the final
    authority.

ACCOUNTED requires assembly-zone presence AND (confirmed identity OR warden
confirmation). Nothing else may set it.
"""
