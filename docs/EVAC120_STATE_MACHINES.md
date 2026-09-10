# EVAC-120 state machines

Three machines, never collapsed into one field. There is no `is_evacuated`
boolean anywhere in the codebase, and adding one would defeat the design rather
than simplify it.

| Machine | Answers | Module |
|---|---|---|
| Identity | who is this? | `app/core/identity_fsm.py` |
| Presence | where were they last seen, and how stale is that? | `app/core/presence_fsm.py` |
| Accountability | are they safe, and how do we know? | `app/core/accountability_fsm.py` |

They are separate because they fail separately. A camera can lose a track
without losing an identity. A face can go missing while a body stays tracked. A
person can be at the muster point and still not be accounted for, because
nobody can say which person they are. One field cannot express any of that.

Identity and presence are **stored**, because they accumulate from observations.
Accountability is **derived** on every read from presence, identity, warden
evidence and system health. Change any input and it changes with them.

---

## 1. Identity

Keyed on the **global person id**, never on a camera-local track id. The same
person on two cameras is one identity question, not two.

### States

| State | Meaning |
|---|---|
| `UNKNOWN` | Nothing yet, or nothing that cleared the gates |
| `CANDIDATE` | Admissible evidence, not yet enough to commit |
| `CONFIRMED` | Committed identity |
| `TEMPORARILY_UNAVAILABLE` | Confirmed earlier, face absent now, track still reliable |
| `CONFLICT` | Two identities with real support; a human must rule |
| `REJECTED` | A warden said this is the wrong person |

`CONFLICT` and `REJECTED` are terminal to the cameras. Only a human leaves them.

### Admissibility gates

An observation produces evidence only if it clears every gate. The reason for a
rejection is recorded, so "why was this person not identified?" has an answer.

| Gate | Rejection reason |
|---|---|
| A face was detected | `NO_FACE` |
| Face and body share one tracker track | `WEAK_ASSOCIATION` |
| Track confidence ≥ `min_track_confidence` | `LOW_TRACK_CONFIDENCE` |
| Face quality ≥ `min_face_quality` | `LOW_QUALITY` |
| Pose deviation ≤ `max_pose_deviation_deg` | `BAD_POSE` |
| Match score ≥ `score_threshold` | `BELOW_SCORE_THRESHOLD` |
| Top-1 minus top-2 ≥ `min_margin` | `BELOW_MARGIN` |

`WEAK_ASSOCIATION` is both a gate and a confidence multiplier, on purpose. A
multiplier alone rejects a weak association only when the product happens to
fall below a threshold configured elsewhere, and during Phase 1 those two
numbers landed exactly equal, letting every geometry-correlated face through.
The rule is too important to rest on a numeric coincidence.

### Transitions

| From | To | Trigger | Notes |
|---|---|---|---|
| `UNKNOWN` | `CANDIDATE` | First admissible observation | Below `min_votes` |
| `UNKNOWN` | `CONFIRMED` | `min_votes` reached in one step | Only when `min_votes` is 1 |
| `CANDIDATE` | `CONFIRMED` | Vote leader reaches `min_votes` | Mean score recorded |
| `CANDIDATE` | `CONFLICT` | A second identity reaches `conflict_votes` | |
| `CONFIRMED` | `TEMPORARILY_UNAVAILABLE` | No admissible face for `identity_expiry_ms` | Identity retained |
| `TEMPORARILY_UNAVAILABLE` | `CONFIRMED` | Face reobserved and matches | `IDENTITY_RECONFIRMED` |
| `CONFIRMED` / `TEMPORARILY_UNAVAILABLE` | `CONFLICT` | A rival reaches `conflict_votes` | Identity claim dropped |
| any | `CONFIRMED` | `WARDEN_CONFIRMED` | Overrides everything |
| any | `REJECTED` | `WARDEN_REJECTED` | Identity remembered as refused |

### Transitions that deliberately do not exist

| Not a transition | Why |
|---|---|
| `CONFIRMED` → `UNKNOWN` | The person was identified and nothing contradicted it. Forgetting is not neutral, it is wrong. |
| `CONFIRMED` → `CONFIRMED` (different identity) | Invariant 2. A rival must earn `CONFLICT` first. |
| `CONFLICT` → `CONFIRMED` by more camera evidence | Invariant 3. Only a human breaks a tie. |
| `REJECTED` → `CONFIRMED` by camera evidence | Invariant 9. A camera does not overrule a person. |
| Any transition on an inadmissible observation | Invariant 1. A face that failed a gate is not evidence in either direction. |

### Conflict is not a vote count

Two identities each reaching `conflict_votes` is a conflict, regardless of the
split. Nine observations against three is still a conflict: three independent
clean looks at a different person is real evidence, and outvoting it is exactly
the silent resolution invariant 3 forbids.

---

## 2. Presence

About observation, not safety. Zones are tagged `FLOOR`, `EXIT`, `ASSEMBLY` or
`BLIND`.

### States

| State | Meaning |
|---|---|
| `NOT_OBSERVED` | Never seen during this drill |
| `IN_BUILDING` | Seen in a `FLOOR` zone |
| `IN_TRANSIT` | Seen in an `EXIT` zone |
| `ASSEMBLY_PRESENT` | Settled in an `ASSEMBLY` zone past `assembly_dwell_ms` |
| `TEMPORARILY_UNOBSERVED` | Track dropped, inside the grace window |
| `LOST` | Track dropped, past the grace window |

### Transitions

| From | To | Trigger |
|---|---|---|
| any observed | `IN_BUILDING` | Sighting in a `FLOOR` zone |
| any observed | `IN_TRANSIT` | Sighting in an `EXIT` zone |
| any observed | `ASSEMBLY_PRESENT` | Sighting in an `ASSEMBLY` zone, dwelled past `assembly_dwell_ms` |
| `NOT_OBSERVED` / `TEMPORARILY_UNOBSERVED` / `LOST` | `IN_BUILDING` | Sighting at a `BLIND` zone edge |
| `IN_BUILDING` / `IN_TRANSIT` / `ASSEMBLY_PRESENT` | `TEMPORARILY_UNOBSERVED` | `TRACK_LOST` |
| `TEMPORARILY_UNOBSERVED` | `LOST` | Unobserved past the grace window |
| `TEMPORARILY_UNOBSERVED` / `LOST` | previous observed state | Track reacquired |

### The grace window

| Condition | Window |
|---|---|
| Last seen in a covered zone | `t_lost_ms` |
| Last seen at a `BLIND` zone | `t_lost_blind_ms` |
| Covering camera degraded | Suspended entirely |

`t_lost_blind_ms` is required to be at least `t_lost_ms`; the config refuses to
construct otherwise. Losing a track in a corridor with no camera is the expected
outcome there, and an architectural coverage hole must not be less forgiving
than a covered space.

While a camera is degraded the grace clock **stops**, and on recovery the time
spent degraded is **credited back**. Without the credit, a ten-minute camera
failure would age everyone it covered into `LOST` the instant it returned.

### What `LOST` does not mean

`LOST` is a statement about the track's staleness. It is never a statement about
the person. Someone in `LOST` with a confirmed identity and a warden
confirmation is fully accounted for. `last_known` survives precisely so a human
can be sent to the right place.

`was_at_assembly` stays true after a track is lost. Someone who walked into the
assembly point and then out of camera view did not un-arrive.

---

## 3. Accountability

Derived, never stored. Computed from presence, identity, warden evidence and
system health on every read.

### States

| State | Meaning |
|---|---|
| `NOT_EVACUATED` | Expected, not yet moving out |
| `EVACUATING` | Seen heading for or through an exit |
| `ACCOUNTED` | Safe, with evidence that qualifies |
| `UNCERTAIN` | Evidence exists but does not settle the question |
| `UNACCOUNTED` | Expected, drill has run long, no qualifying evidence |
| `MANUAL_VERIFICATION_REQUIRED` | The system cannot decide; a human must look |

### The only two routes to ACCOUNTED

1. The system observed them reach an assembly zone **and** their identity is
   `CONFIRMED` or `TEMPORARILY_UNAVAILABLE`.
2. A warden confirmed them **in person at an assembly zone**.

Nothing else. Not a high match score, not a long dwell, not a healthy camera,
not a timeout. The tests enumerate the routes that stay closed, including fifty
face matches at score 0.999 on a person still inside the building.

A warden confirmation recorded away from an assembly zone is identity evidence
only. Recognising a colleague in a corridor is not evidence they got out.

### Derivation order

The order is itself a safety property.

| Priority | Condition | Result |
|---|---|---|
| 1 | Identity is `CONFLICT` or `REJECTED` | `MANUAL_VERIFICATION_REQUIRED` |
| 2 | A warden rejected the identity | `MANUAL_VERIFICATION_REQUIRED` |
| 3 | Warden confirmed at an assembly zone | `ACCOUNTED` |
| 4 | Reached assembly **and** identity usable | `ACCOUNTED` |
| 5 | Reached assembly, identity not usable | `UNCERTAIN` |
| 6 | Coverage degraded | `MANUAL_VERIFICATION_REQUIRED` |
| 7 | Warden marked absent from site | `UNCERTAIN` |
| 8 | Presence `LOST` | `UNCERTAIN` |
| 9 | Presence `TEMPORARILY_UNOBSERVED` or `IN_TRANSIT` | `EVACUATING` |
| 10 | Presence `IN_BUILDING`, past `unaccounted_after_ms` | `UNACCOUNTED` |
| 11 | Presence `IN_BUILDING`, within the deadline | `NOT_EVACUATED` |
| 12 | Never observed, past `unaccounted_after_ms` | `UNACCOUNTED` |
| 13 | Never observed, within the deadline | `NOT_EVACUATED` |
| 14 | Not on the roster | `UNCERTAIN` |

Read as four rules:

- **Conflict beats everything.** Being at the assembly point with a contested
  identity means somebody is safe, not that this person is.
- **Humans beat cameras.** Rule 3 sits above rule 6 on purpose: a warden's eyes
  do not stop working because a camera did.
- **Cameras beat silence.** A real observation beats an inference.
- **Blindness beats inference.** Everything below rule 6 is reasoning from what
  was not observed, and reasoning drawn while blind is worthless.

### States that are deliberately not "safe"

| Situation | State | Why not `ACCOUNTED` |
|---|---|---|
| Warden marked absent from site | `UNCERTAIN` | "Not in the building today" is not "evacuated safely". Conflating them is how a genuinely absent person becomes a false all-clear. |
| Unknown person at an assembly zone | `UNCERTAIN` | Invariant 5. Somebody is safe; which employee is unknown, and forcing a match to tidy the numbers is the failure this system exists to prevent. |
| Identity rejected by a warden | `MANUAL_VERIFICATION_REQUIRED` | The system's answer was wrong. The person still needs identifying. |

---

## 4. Where the invariants live

| Invariant | Enforced by |
|---|---|
| 1. Absence of evidence is not evidence of absence | No `PERSON_MISSING` event type; inadmissible observations produce no evidence; `LOST` is about the track |
| 2. A strong identity is not overwritten by a weak observation | Identity transitions that do not exist |
| 3. Conflicts are never silently resolved | `conflict_votes`, not a vote comparison; `CONFLICT` terminal to cameras |
| 4. Three separate machines | This document; no `is_evacuated` field |
| 5. Re-ID similarity is not employee identity | `roster.py` populations; unknown people never reconciled |
| 6. Every decision is reconstructable | `ledger.explain`; every `Decision` carries a reason |
| 7. Thresholds are configuration | Every config carries `calibrated=False` until Phase 2 |
| 8. Failure degrades, never clears | Derivation rule 6; grace-clock suspension; gaps reported not repaired |
| 9. Human confirmation is final | Derivation rule 3; warden transitions override all camera evidence |

---

## 5. Configuration

Every threshold is configuration and every provisional value is marked
`calibrated=False`. Anything reading an uncalibrated config must surface that
rather than presenting its output as validated.

| Machine | Parameters |
|---|---|
| Identity | `score_threshold`, `min_margin`, `min_votes`, `conflict_votes`, `min_face_quality`, `max_pose_deviation_deg`, `min_track_confidence`, `identity_expiry_ms`, `require_strong_association` |
| Presence | `t_lost_ms`, `t_lost_blind_ms`, `assembly_dwell_ms` |
| Accountability | `unaccounted_after_ms` |

The Phase 1 values are placeholders. `score_threshold` and `min_margin` are the
numbers DeepStream ships in `config_pipeline.example.toml`; the rest were chosen
to be conservative. Phase 2 replaces all of them from a labelled calibration
set, recorded in `docs/EVAC120_CALIBRATION.md`.
