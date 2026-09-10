# EVAC-120 architecture

A drill has one question: **who is still in the building?** Everything here
exists to answer it from evidence, and to say how confident that answer is.

---

## 1. The shape

```
  cameras                 EDGE NODE (the authority during a drill)
     │
     │  RTSP
     ▼
┌──────────────┐   Redis      ┌──────────────────────────────────────────┐
│  DeepStream  │─────────────▶│  ingest                                  │
│  pipeline    │  vt:evac:    │    Ingestor.feed_batch()                  │
│              │  events:<t>  │      dedupe on (source, seq)              │
│  PGIE→track  │              │      detect gaps, never fill them         │
│  →probe      │              └───────────────┬──────────────────────────┘
└──────────────┘                              │
                                              ▼
   FaceTrack ──HTTP──▶ roster        ┌────────────────────┐
   (who is expected)                 │  core   (pure)     │
                                     │  ┌──────────────┐  │
   warden tablets ──┐                │  │ identity FSM │  │
     (PWA, offline) │                │  ├──────────────┤  │
                    │  warden        │  │ presence FSM │  │
                    └───actions─────▶│  ├──────────────┤  │
                                     │  │ ledger       │  │
                                     │  └──────┬───────┘  │
                                     │         ▼          │
                                     │  accountability    │  derived, never stored
                                     └─────────┬──────────┘
                                               ▼
                                     ┌────────────────────┐
                                     │  projections       │
                                     │   LiveBoard        │──▶ command centre
                                     │   zone panels      │──▶ warden PWA
                                     │   timing           │──▶ drill report
                                     └────────────────────┘
                                               │
                                     SQLite outbox (fsync on commit)
                                               │
                                               ▼
                                        CENTRAL (a replica)
                                          reconciler
                                          reports its own gaps
```

Two rules the diagram encodes.

**The edge node is the authority.** Nothing it decides waits on anything outside
the building. Central is a replica, and its reconciliation report says plainly
when its copy is incomplete and must not be used for accountability.

**Accountability is derived, never stored.** There is no `is_evacuated` field
anywhere. Change any input and the answer changes with it.

---

## 2. The modules

| Module | Lines | Depends on | Does |
|---|---|---|---|
| `app/core` | 2714 | numpy only | The three state machines, the ledger, timing, roster, fusion. No database, no framework |
| `app/vendor` | 1396 | — | Code copied from VisionTrack and DeepStream. See `EVAC120_PROVENANCE.md` |
| `app/ingest` | 1346 | `core` | The fold, health tracking, projections, edge-to-central replication |
| `app/simulator` | 1118 | `core`, `ingest` | Synthetic site, 512 agents, 20 injectable failures |
| `app/warden` | 855 | `core` | Warden actions, headcount asymmetry, sweep state |
| `app/calibration` | 741 | `core` | Labelled sets, threshold sweeps, certification |
| `app/api` | 732 | everything | 14 endpoints, OpenAPI, serves both front ends |
| `app/reporting` | 586 | `drill`, `infra` | Post-drill report and validation criteria |
| `app/infra` | 582 | — | Permissions, audit log, retention policy |
| `app/sync` | 380 | `core` | VisionTrack geometry into EVAC-120's model |
| `frontend/` | 1323 | — | Command centre and warden PWA. No framework |

10,330 lines of application code, 6,412 of tests, 712 tests.

### 2.1 The dependency rule

`app/core` imports nothing but the standard library and numpy. That constraint is
what makes the simulator and the edge service drive the *same* code: a property
proved against one is proved about the other, rather than about a test double
that behaves similarly today.

If a module in `core` ever needs a connection, it belongs in `ingest` or `api`.

---

## 3. How an observation becomes a decision

Following one person through, because the layering only makes sense end to end.

**1. A camera sees a body.** The DeepStream probe emits `TRACK_UPDATED` with a
zone resolved from the camera homography, carrying a per-source sequence number.

**2. A face is found on that body.** `fusion.py` decides how strongly the face is
attached: `SHARED_TRACK` if both came from one tracker, `SPATIAL_IOU` if only
geometry links them. Weak attachment is rejected outright, not merely
down-weighted — that rule is too important to rest on a numeric coincidence
between two independently-tuned thresholds.

**3. The face is matched.** `FACE_OBSERVED` carries the candidate, score and
margin. If nothing usable was found, `FACE_UNAVAILABLE` is emitted instead,
because silence and "we looked and could not tell" are different facts.

**4. Ingest folds it in.** Deduplicated on `(source, seq)`. A gap becomes a
`SEQUENCE_GAP` in that person's evidence and nothing else; interpolating across
it would manufacture observations.

**5. Identity accumulates.** Gates first — association, track confidence, face
quality, pose, score, margin — then votes. Enough votes commits. A rival with
real support raises `CONFLICT` rather than being outvoted.

**6. Presence tracks where.** Independent of identity. A dropped track becomes
`TEMPORARILY_UNOBSERVED`, then `LOST` past a grace window that is longer in a
blind zone and suspended entirely while a camera is down.

**7. A warden confirms them.** Highest-trust evidence in the system, carrying
warden id, device id, timestamp, and whether the device was online.

**8. Accountability is derived.** Conflict beats everything, humans beat
cameras, cameras beat silence, blindness beats inference. `ACCOUNTED` has exactly
two routes in and nothing else opens a third.

**9. The board renders it.** A state, a colour, and a reason in words. No raw AI
metrics: the scores are one click away in the explain drawer, which is where
someone deciding whether to trust a call should be looking.

---

## 4. Where each invariant is enforced

| Invariant | Enforced by |
|---|---|
| 1. Absence of evidence is not evidence of absence | The event vocabulary has no way to say a person is missing; inadmissible observations produce nothing; `LOST` is about the track |
| 2. Strong identity survives weak observation | Transitions that deliberately do not exist |
| 3. Conflicts never resolved silently | `conflict_votes`, not a vote comparison; `CONFLICT` terminal to cameras |
| 4. Three separate machines | No `is_evacuated` anywhere; accountability derived on read |
| 5. Re-ID is not employee identity | `roster.py` populations; only a human moves anyone between them |
| 6. Every decision reconstructable | `ledger.explain()`; every `Decision` carries a reason |
| 7. Thresholds are configuration | Every config carries `calibrated=False`; `certify()` is the only route to True |
| 8. Failure degrades, never clears | Derivation order; grace-clock suspension; the all-clear requires no open outage |
| 9. Human confirmation is final | Warden transitions override all camera evidence; sweep completion always accepted |

---

## 5. External systems

| System | Relationship | Failure means |
|---|---|---|
| VisionTrack | Produces person tracks on Redis streams | No new observations. Blinding |
| FaceTrack | Supplies the roster and the ArcFace gallery | The roster cannot be refreshed. Marked unverified; the drill still runs |
| DeepStream | The face engine, one container per site | No new observations. Blinding |
| Central | Receives replicated events | Reporting only. The drill is unaffected |

None of them is a shared database. Code from VisionTrack and DeepStream is
vendored rather than imported, which is why `EVAC120_PROVENANCE.md` exists and
why a re-vendor is a deliberate act.

---

## 6. What is not built

Named rather than left to be discovered.

| Missing | Consequence |
|---|---|
| Geometry sync *runner* | The transformation is built and tested; the process that reads VisionTrack's Postgres and writes firedrill's is not |
| Redis consumer loop | Events reach the system through the API or the simulator, not a live stream |
| Postgres projection persistence | Projections are in memory; a restart rebuilds them from the event stream |
| Alembic migrations | Nothing is persisted yet, so there is nothing to migrate. `evac_events` is the first table |
| JWT verification | The API reads identity from headers a gateway sets. Do not expose it beyond the edge node's network |
| FaceTrack HTTP client | Use `EVAC_ROSTER_FILE` |
| The DeepStream pipeline itself | Blocked on the pyds segfault. See `EVAC120_DEEPSTREAM.md` §2 |
