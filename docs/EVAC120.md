# EVAC-120 — Fire Drill & Evacuation Accountability

**Operational target:** P95 building evacuation ≤ 120 s, **measured**. Never
hard-coded, never asserted as a guarantee.

> **Safety notice.** EVAC-120 supplements, and never replaces, certified
> fire-detection and life-safety systems. It does not control alarms, doors, or
> suppression. Its output is decision support for a human incident commander,
> and a floor warden's physical count is the final authority.

This document is the standing reference: the architecture decision, the verified
facts about the systems EVAC-120 draws on, the tree map, and the running record
of each phase. It is updated at every phase gate.

---

## 1. Status

| Phase | Scope | State |
|---|---|---|
| 0 | Repo, vendored code, permissions, docs, tree map | Complete |
| 1 | Pure-Python core + simulator, no cameras | Complete |
| 2 | Single DeepStream pipeline on GPU host + calibration | Design + harness complete; GPU work blocked |
| 3 | Edge/central resilience, projections, chaos suite | Complete |
| 4 | Command Center + Warden Mobile PWA | Built; live dry run blocked |
| 5 | Three live drills, validation report | **Report and criteria built; live drills blocked** |

---

## 2. Architecture decision

EVAC-120 is a **standalone repository** with its own database, API and frontend.

This reverses the original brief, which specified a VisionTrack module family
(`backend/app/modules/evac/`). The standalone decision was taken on 2026-09-10.
What it buys and what it costs:

**Gains.** EVAC-120 ships, migrates and fails independently of VisionTrack. A
schema change here cannot break a live tracking deployment. The edge node runs
one product, not two. Life-safety-adjacent code is not coupled to a CCTV SaaS
release train.

**Costs.** Code that VisionTrack already had must be copied rather than
imported, so upstream fixes do not arrive automatically — hence
`docs/EVAC120_PROVENANCE.md` and the re-vendor rule. Floor plans, zones, camera
calibration and the roster now exist in two places and need a sync path.

### 2.1 Data plane

firedrill owns its Postgres schema. It treats VisionTrack and FaceTrack as
**external producers**, never as a shared database.

| Input | Source | Transport |
|---|---|---|
| Person tracks, global person id | VisionTrack workers | Redis streams `vt:tracks`, `vt:track_lifecycle` |
| Face identity + accountability events | DeepStream pipeline (Phase 2) | Redis stream `vt:evac:events:<tenant>` |
| Roster — who is expected | FaceTrack `user_data` | HTTP `GET /api/employees` |
| Floor plans, zones, camera homographies | VisionTrack | Synced into firedrill's own tables |
| Warden confirmations | Warden PWA | firedrill's own API |

Nothing on the accountability path requires the Internet. If every upstream
producer disappears mid-drill, the system degrades to
`DEGRADED / MANUAL_VERIFICATION_REQUIRED` and the warden's count carries the
drill. It never reports `ALL CLEAR` from silence.

### 2.2 Integration plan

Four seams, in the order they have to be closed. Each is independently useful,
which matters because two of them are blocked on hardware and the other two are
not.

**Seam 1 — DeepStream to firedrill.** The pipeline publishes typed events with
per-source sequence numbers to `vt:evac:events:<tenant>`. firedrill consumes
them via `app/ingest/consumer.py`, using consumer groups so a restart
redelivers rather than skips, acknowledging only after the fold, and claiming
what a dead consumer stranded. *Blocked* only on the producer: the P2.3b
segfault means nothing publishes to that stream yet. Contract in
`EVAC120_DEEPSTREAM.md` §3.1; event shapes in §5 below.

**Seam 2 — FaceTrack to firedrill.** The roster, fetched at drill start and
frozen for the drill's duration. FaceTrack supplies `emp_id`, names, and gallery
state; department, home floor and assembly zone do not exist upstream and are
owned here. *Not blocked* by hardware, only by FaceTrack currently
crash-looping. `EVAC_ROSTER_FILE` covers the gap and is also the offline
fallback, which is not a workaround: a site should be able to run a drill from
an exported roster with no network at all.

**Seam 3 — VisionTrack geometry to firedrill.** Floor plans, zone polygons and
camera homographies, synced into firedrill's tables. Zones additionally need a
kind — `FLOOR`, `EXIT`, `ASSEMBLY` or `BLIND` — which VisionTrack has no concept
of, so the sync is a transformation rather than a copy. *Not blocked, and now
built:* `app/sync/` holds the transformation and the runner, tested against the
shapes a live VisionTrack instance actually holds. It reads VisionTrack
read-only, and a site that has synced once runs drills with VisionTrack switched
off entirely.

**Seam 4 — edge to central.** One-directional, asynchronous, never on the
critical path. Built and tested, including convergence after a partition.
*Blocked* only on a credential: the transport interface exists, the
authentication does not.

### 2.3 What integration deliberately does not do

**No shared database.** Two systems writing one schema means every migration is
a cross-repo release. It also means a VisionTrack outage can take firedrill's
storage with it, and the whole point of the edge node is that it keeps deciding.

**No synchronous calls on the accountability path.** Nothing firedrill decides
waits on an HTTP round trip to another service. The roster is fetched once, at
drill start, and frozen — a roster that shifts mid-drill would move the
denominator under the operator, and a person who "disappeared" because HR
updated a record looks identical on screen to one who disappeared in a
stairwell.

**No writing back.** firedrill never updates VisionTrack or FaceTrack. It is a
consumer of both, and a bug here cannot corrupt either.


---

## 3. Verified facts — 2026-09-10

Each of the three source repositories was located by content, not by name, and
each fact below was checked against the code rather than taken from the brief.

| Role | Path | Confirmed by |
|---|---|---|
| FaceTrack — control plane, roster, gallery | `~/deploy/attendance-system` | `app/modules/pipeline/service.py` owns the `pipeline_jobs` queue |
| DeepStream — face engine | `~/deploy/deepstream` | `main_enterprise.py` plus the canonical `utils/` set |
| VisionTrack — tracking platform | `~/visiontrack/visiontrack` | Alembic head `0022_tenant_facetrack_feed` |

### 3.1 Corrections to the brief

**(a) Two "legacy" DeepStream files are load-bearing.** The brief says archive
every `main_*.py` and `utils/probe*`. Two sit in the canonical import closure of
`main_enterprise.py`: `main_udp.py` supplies `add_udp_rtsp_branches` and
`get_dir_signature`; `utils/probe_git.py` supplies `pgie_src_filter_probe` and
shared probe helpers to three canonical modules. Archiving them breaks the only
working pipeline. Both were kept. DeepStream's own `ARCHITECTURE.md` already
said so before Phase 0 began.

**(b) There is no `zones` table in VisionTrack.** Zones are a JSONB array on
`floor_plans`, and VisionTrack's `zones` module is a stub serving only a
`_ping` route. firedrill defines its own zone table, so tagging a zone `FLOOR`,
`EXIT`, `ASSEMBLY` or `BLIND` is a first-class column here rather than a schema
change to someone else's JSON.

**(c) Homographies live on `cameras.calibration`,** a JSONB column, not on
`floor_plans`. The vendored `zone_resolve.py` already reads them from there.

**(d) The FaceTrack roster has no department and no zone.** `user_data` carries
`user_id`, `company_id`, `emp_id`, `first_name`, `last_name`, `image_path` and
`feature_path`. `GET /api/employees` returns `emp_id`, `first_name`,
`last_name`, `photo` and `present`. The warden UI needs department, home floor
and warden-zone assignment; none exists upstream. firedrill must own those
fields. This is a schema requirement for Phase 1's `roster.py`, not a
FaceTrack change.

### 3.2 Live infrastructure state

| Service | State | Note |
|---|---|---|
| VisionTrack backend, Postgres, Redis, MinIO, frontend | up, healthy | 5 days |
| `vt-ai-worker` | up | YOLOv8 + OSNet, production path, produces `vt:tracks` |
| `vt-ai-worker-ds` | **not running** | DeepStream 7.1, blocked on the P2.3b segfault |
| Milvus / etcd | **not running** | needed by VisionTrack's re-identification |
| FaceTrack (`attendance-system`) | **crash-looping** | `asyncpg.exceptions.InvalidPasswordError: password authentication failed for user "postgres"` |

FaceTrack is the roster source. Its outage blocks building `roster.py` against
real data. It does not block the simulator.

---

## 4. State machines

Three, never collapsed into one field. The full transition table with the
triggering evidence for every transition is a Phase 1 deliverable,
`docs/EVAC120_STATE_MACHINES.md`.

**Presence.** `NOT_OBSERVED → IN_BUILDING → IN_TRANSIT → ASSEMBLY_PRESENT`,
plus `TEMPORARILY_UNOBSERVED` (track lost under `T_lost`) and `LOST` (over
`T_lost`). Zones are tagged `FLOOR`, `EXIT`, `ASSEMBLY` or `BLIND`.

**Identity.** `UNKNOWN → CANDIDATE → CONFIRMED`, plus
`TEMPORARILY_UNAVAILABLE` (confirmed earlier, face currently absent, track still
reliable), `CONFLICT` and `REJECTED`. Keyed on the **global person id**, not a
camera-local track id. Configurable: score threshold, top-1/top-2 margin,
minimum votes, hysteresis, face quality and pose gates, identity expiry, track
confidence.

**Accountability.** `NOT_EVACUATED → EVACUATING → ACCOUNTED`, or `UNCERTAIN`,
`UNACCOUNTED`, `MANUAL_VERIFICATION_REQUIRED`. Derived from presence, identity
and the evidence ledger. `ACCOUNTED` requires assembly-zone presence AND
(confirmed identity OR warden confirmation).

---

## 5. Event model

Append-only `evac_events`, a Phase 3 Alembic revision:

`event_id (uuid)`, `tenant_id`, `site_id`, `drill_id`, `source`
(camera/edge/warden/system), `seq` (per source, monotonic), `type`, `pts_ms`
(frame PTS for camera events, wall clock otherwise), `subject`
(global_person_id / emp_id / camera_id / zone_id), `payload jsonb`,
`ingested_at`.

Unique on `(source, seq)`. Ingest is idempotent. A hole in the sequence emits
`SEQUENCE_GAP`.

Event types:

```
DRILL_CREATED DRILL_STARTED ALARM_ACTIVATED PERSON_DETECTED TRACK_CREATED
TRACK_UPDATED TRACK_LOST FACE_OBSERVED FACE_UNAVAILABLE IDENTITY_CANDIDATE
IDENTITY_CONFIRMED IDENTITY_RECONFIRMED IDENTITY_CONFLICT
PERSON_EXITED_BUILDING PERSON_ENTERED_ASSEMBLY PERSON_LEFT_ASSEMBLY
PERSON_ACCOUNTED PERSON_UNCERTAIN PERSON_UNACCOUNTED WARDEN_CONFIRMED
WARDEN_REJECTED WARDEN_SWEEP_COMPLETE WARDEN_NOTE CAMERA_FAILURE
CAMERA_RECOVERED SYSTEM_DEGRADED SYSTEM_RECOVERED SEQUENCE_GAP DRILL_COMPLETED
```

All read models — dashboard, warden lists, reports — are projections over this
stream. Edge-to-central replication uses the store-and-forward pattern vendored
from DeepStream.

**Timestamp warning.** `pts_ms` starts at 0 for the first frame of a stream, and
the vendored hold-time logic has a falsy-zero defect that a 0 timestamp
triggers. Phase 1 must normalise to epoch milliseconds at the ingest boundary.
Detail in `docs/EVAC120_PROVENANCE.md`.

---

## 6. Permissions

Four core permissions in `backend/app/infra/permissions.py`, deliberately
**role-shaped rather than CRUD-shaped**, because an evacuation has four kinds of
actor.

| Constant | Key | Grants |
|---|---|---|
| `EVAC_READ` | `evac:read` | Dashboards, drill history, reports |
| `EVAC_OPERATE` | `evac:operate` | Create, start and stop a drill |
| `EVAC_WARDEN` | `evac:warden` | Confirm, reject, sweep, headcount — scoped to assigned zones |
| `EVAC_ADMIN` | `evac:admin` | Zones, thresholds, retention, warden assignments |

Plus `drill:export` (reports leave the building carrying personal data, so it is
separate from read), `audit:view`, and `system:admin` which no tenant role
holds.

**Separation of duty is the point of the seeded roles, not a side effect.** An
Incident Commander runs the drill but cannot sign off a physical headcount —
that is the warden's evidence and stays theirs. A Floor Warden confirms people
but cannot start, stop or reconfigure a drill. A Safety Officer configures the
system but does not operate a live drill. No default role holds every
permission, and combining `evac:operate` with `evac:warden` defeats the
two-source evidence model, so any deployment needing it must create a deliberate
custom role.

---

## 7. Tree map after Phase 0

```
firedrill/
├── README.md                      what this is, where things are
├── CLAUDE.md                      conventions + the nine invariants
├── .env.example                   28 variables, secrets marked REQUIRED
├── .gitignore                     PII, secrets, local state
├── docs/
│   ├── EVAC120.md                 this file
│   └── EVAC120_PROVENANCE.md      what was vendored, from where, what changed
├── backend/
│   ├── requirements.txt           numpy, pytest, pytest-cov, hypothesis
│   ├── pytest.ini
│   ├── app/
│   │   ├── core/                  Phase 1 domain — contract written, no code yet
│   │   ├── vendor/
│   │   │   ├── visiontrack/       zones_math, zone_resolve, occupancy,
│   │   │   │                      dwell, heatmap, rule_state        (1049 loc)
│   │   │   └── deepstream/        recognition, outbox                (288 loc)
│   │   ├── simulator/             Phase 1 contract
│   │   ├── ingest/                Phase 3 contract
│   │   ├── api/                   Phase 3-4 contract
│   │   └── infra/
│   │       └── permissions.py     4 core + 3 supporting, 4 seeded roles
│   └── tests/
│       ├── test_vendor_visiontrack.py    26 tests — geometry, hold-time, projection
│       ├── test_vendor_deepstream.py     21 tests — gallery, voting, outbox
│       ├── test_vendor_integrity.py      25 tests — imports + provenance headers
│       └── test_permissions.py           22 tests — separation of duty
├── frontend/                      Phase 4
└── prototype/
    └── fire_drill_system.jsx      early React mock — design reference only
```

The prototype is a self-contained React mock with randomly generated people. It
is useful for the command-center layout and nothing else: its data model has a
single flat `status` field including `"missing"`, which violates invariants 1
and 4. It must not be used as a schema reference.

---

## 8. What Phase 0 built

1. **The repository.** Layout above, `.gitignore`, `.env.example` with 28
   variables and every secret marked `REQUIRED` with no default.
2. **1393 lines of vendored code** from two repositories, each file carrying a
   provenance header, with the four import rewrites the copy required and
   nothing else changed.
3. **94 tests** proving the vendored code works under the new import paths, that
   the behaviour EVAC-120 depends on survived the copy, and that separation of
   duty holds in the seeded roles.
4. **Permission registry** with four seeded roles built around separation of
   duty.
5. **Package contracts** for `core/`, `simulator/`, `ingest/`, `api/` and
   `infra/`, each naming what lands there and in which phase.
6. **Documentation:** this file, the provenance record, `CLAUDE.md`, `README.md`.

### 8.1 Work done in the source repositories

**DeepStream** (branch `evac/phase-0`, staged, not committed). Archived 39
legacy files with `git mv`, history preserved, keeping the two that are
load-bearing and documenting why in `archive/README.md`. Removed a hard-coded
credential in `tools/vt_uuids.py`, which defaulted the VisionTrack database
password to the value in VisionTrack's compose file; the variable is now
required with no default. Extended `.env.example` from 14 to 28 variables, three
of them secrets that were read by code and documented nowhere. Updated
`ARCHITECTURE.md`.

**VisionTrack** (branch `fix/pytest-import-path`, staged, not committed). One
file: `backend/pytest.ini`. `make test` ran pytest from `/app` with no ini file,
so only `/app/tests` went on `sys.path` and all nine test modules failed to
import with `ModuleNotFoundError: No module named 'app'`. Setting
`pythonpath = .` fixes it without touching a test. An earlier evac module
skeleton and permission entries were reverted when the architecture moved to a
standalone repository.

**FaceTrack.** Untouched.

---

## 9. Phase 0 gate results

| Check | Result |
|---|---|
| firedrill test suite | **93 passed, 1 xfailed**, 0.77 s |
| firedrill coverage | 58% of 576 statements |
| Vendored files importing under rewritten paths | 8 of 8 |
| Vendored files modified beyond import rewrites | 0 |
| Vendored files still importing their source repo | 0 |
| DeepStream canonical closure compiles | 14 of 14 |
| DeepStream broken imports introduced by archiving | 0 |
| DeepStream env coverage | 28 of 28 referenced variables documented |
| VisionTrack backend tests | 66 passed, 0 failed |
| Tree map documented | §7 |

The one xfail is deliberate: a strict xfail pinning the upstream falsy-zero
defect, so it flips to XPASS the moment it is fixed.

### 9.1 What the tests actually assert

**Geometry and hold-time, 26 tests.** Concave zones are not treated as bounding
boxes. A half-drawn zone raises rather than silently swallowing a floor. A
centroid is area-weighted, so an assembly label lands inside its own zone.
Overlapping camera views union rather than sum, so nobody is double-counted at
an assembly point. A transient spike never fires, and one sustained breach is
one event rather than one per tick. The foot-point projection uses the
bottom-centre of a bounding box, because using the centre puts a person half a
body-length off, which at an exit line is the difference between inside and
outside the building. An uncalibrated camera produces no position rather than
a position of (0, 0), which would put everyone in whichever zone contains the
floor-plan origin.

**Identity and store-and-forward, 21 tests.** An empty gallery matches nothing.
A failed embed matches nothing. The margin gate separates a confident match from
a coin flip between two look-alike employees. An identity needs its minimum
votes, an unknown face never votes, a commit is sticky against a later
contradiction, and stale tracks are evicted by TTL. A failing writer loses no
buffered events, and events replay in write order.

Six of those pin the **limits** of the vendored code rather than its
capabilities, because those limits are what Phase 1 exists to close. They assert
that the vendored identity manager has only two outcomes, that it resolves
conflict silently by majority vote, and that it keys identity on a camera-local
track id. If someone later assumes that class is already the identity FSM, a
test says otherwise.

**Copy integrity, 25 tests.** Every vendored module imports, every vendored file
still declares its origin, and no vendored module reaches back into its source
repository. The tree is asserted to be non-empty first, so the parametrised
tests cannot pass vacuously.

**Separation of duty, 22 tests.** No seeded role both runs a drill and signs off
its own headcount. The warden cannot start, stop or reconfigure a drill. The
commander cannot confirm people. No default role holds every permission. Someone
can read the audit log, because invariant 6 is worthless if no role can inspect
the record.

---

## 9A. Phase 1 results

Eight core modules, a simulator, and 401 tests. Everything in `app/core` is pure
Python: no database, no Redis, no framework. The simulator drives it directly,
and Phase 3's ingest will drive it the same way.

### 9A.1 What was built

| Module | Lines | Does |
|---|---|---|
| `core/events.py` | 320 | Vocabulary, validation, timestamp normalisation, sequence and gap tracking |
| `core/identity_fsm.py` | 400 | Six-state identity keyed on the global person id |
| `core/presence_fsm.py` | 400 | Six-state presence with blind-zone and degradation handling |
| `core/accountability_fsm.py` | 330 | Derivation from presence, identity, warden evidence and health |
| `core/ledger.py` | 350 | Append-only evidence with stance, dispute detection, `explain` |
| `core/timing.py` | 260 | Percentiles, exclusions, and when not to trust them |
| `core/roster.py` | 330 | Expected set, three populations kept apart |
| `core/fusion.py` | 300 | Face-to-body association strength, carried into the identity gate |
| `simulator/` | 1332 | Synthetic site, 512 agents, 20 injectable failures, drill engine |

### 9A.2 Test results

| Area | Tests |
|---|---|
| Core state machines, ledger, timing, roster, fusion | 234 |
| Simulator scenarios and properties | 73 |
| Vendored code behaviour | 47 |
| Vendored copy integrity | 25 |
| Permissions and separation of duty | 22 |
| **Total** | **400 passed, 1 xfailed** in 100 s |

Coverage is 97% on `app/core` and 87% overall. The shortfall is entirely
vendored code paths EVAC-120 does not use yet, such as heatmap binning.

### 9A.3 Measured drill results

500 agents plus 12 visitors on the synthetic site, evacuating five floors and a
basement through two exits to two assembly points. Times in seconds.

| Profile | Events | Dropped | Duplicates | **False accounted** | Accounted | Uncertain | Unaccounted | Manual | P50 | P95 |
|---|---|---|---|---|---|---|---|---|---|---|
| clean | 41,019 | 0 | 0 | **0** | 448 | 0 | 64 | 0 | 59.8 | 114.0 |
| realistic | 39,416 | 719 | 1,818 | **0** | 445 | 11 | 51 | 3 | 67.2 | 120.4 |
| hostile | 22,888 | 2,155 | 3,145 | **0** | 97 | 337 | 76 | 2 | — | — |

The population deliberately contains 9 people who never leave their desks, 7
absent from the building entirely, 12 visitors with no gallery entry, 37
employees never enrolled, and 12 look-alikes in 6 pairs.

Three things in that table matter more than the percentiles.

**False accounted is zero in every profile**, and this is checked against ground
truth the core cannot see. That is the property the whole system exists to hold.

**The hostile profile collapses into uncertainty rather than staying
confident.** Accounted falls from 448 to 97 while uncertain rises from 0 to 337.
That is the correct response to being blind, and the opposite of what a system
optimising for a clean-looking dashboard would do.

**The hostile profile reports no P95 at all.** Under an all-camera outage nobody
was tracked from alarm to arrival, so there is no sample, and the timing module
says "No measurements: nobody had both a start and an arrival" rather than
producing a number from the few people who happened to be visible. A P95
computed from survivors of an outage would be the most flattering and least
honest number the system could print.

### 9A.4 Defects found and fixed during Phase 1

Six, all caught by tests written against the invariants rather than against the
implementation.

1. **Weak face-to-body association passed the identity gate.** Association
   strength was applied only as a multiplier on track confidence, and the weak
   weight of 0.5 met the minimum track confidence of 0.5 exactly. Every
   geometry-correlated face was admissible. Now enforced structurally as well,
   with a test that holds even when the numbers align.
2. **Rare behaviours vanished from small populations.** A mix expressed only as
   probabilities put zero people who never leave into a 120-agent run, so the
   test asserting "someone still at their desk is never marked safe" passed
   without ever containing one. Critical behaviours are now guaranteed.
3. **Look-alike pairs landed on people no camera ever saw.** Fixing (2) pushed
   the guaranteed behaviours to the head of the list, which is exactly where the
   pairs were assigned. Pairs are now drawn from people who walk past a camera.
4. **Outage windows were alarm-relative but emitted as absolute timestamps.**
   Every infrastructure outage sorted decades before the drill and recovered
   before anything happened.
5. **The board matched roster entries to tracks using ground truth.** The real
   system has no such access. Matching on truth hid every misidentification the
   simulator injects, and made the central property test partly tautological.
   Now matched on the identity the system claimed.
6. **The explain drawer contradicted itself.** An ACCOUNTED person carried a
   DISPUTED banner, for two separate reasons: inadmissible face matches were
   filed as identity claims, and the ledger's dispute threshold was one claim
   while the identity FSM's conflict threshold was three votes. Both fixed; the
   two now share the threshold.

### 9A.5 Gate: explain() reviewed

The brief requires `explain()` output reviewed for three sample people. Reviewed
on a 500-agent realistic run:

- **Accounted.** Decision, then the chronological evidence: face matches with
  score and margin, the assembly arrival that qualified it, and the gate
  rejections that did not. Reads correctly.
- **Still at their desk.** UNCERTAIN, with "track went stale; last seen in FLOOR
  zone floor-2-open on cam-floor-2-open". It names the place to send someone.
- **Needs a human.** "4 tracks were identified as EMP-0007 at the same time; a
  human must establish which is real", with both contested names shown.

All three defects in (6) above were found by this review rather than by a test,
which is why the brief asks for it.

---

## 9B. Phase 2: design and harness done, GPU work blocked

The pipeline cannot be built here. The 26 GB DeepStream image is not present on
this machine, and the project's own runbook advises against rebuilding it
casually after a previous rebuild caused a libstdc++ ABI regression. So Phase 2
split into what can be done without a GPU and what cannot.

Full design in `docs/EVAC120_DEEPSTREAM.md`.

### 9B.1 A cheaper way around the segfault

The blocker is the P2.3b crash: a buffer probe downstream of an SGIE producing
tensor user-meta segfaults after about five seconds, identically under nvinfer
and under Triton. Four remedies are planned, and the cheapest begins with a
26 GB rebuild.

The DeepStream face pipeline in the sibling repository suggests a fifth. It runs
in production with buffer probes attached, and **it has no SGIE at all**: one
primary detector, with face embeddings computed inside the probe by a standalone
ArcFace TensorRT engine. Its own module docstring says this is deliberate.

The documented crash condition needs an SGIE. That pipeline never creates one,
and it does not crash.

This is an inference, not a reproduction, and the document says so plainly. The
two pipelines differ in base image, models and DeepStream version, so removing
the SGIE is not proven to be the operative difference. Moving inference into a
Python probe also costs frame budget that an SGIE does not.

What makes it worth stating is the cost of testing it: take the pipeline that
crashes, remove the SGIE, keep the probe, embed in-probe. One config change and
one restart. It either survives sixty seconds or it does not, and either result
is informative. That is now step 1 of Phase 2, ahead of the four expensive
remedies.

### 9B.2 The calibration harness

Built and tested: `app/calibration/`, 35 tests. Labelled observations in,
validated thresholds out, or a refusal explaining why not.

It refuses in every case where certifying would be a lie: too few observations,
too few enrolled people, no observations of unenrolled people at all, a person
leaking across the tune and validate halves, a held-out false-accept rate above
the ceiling, drift beyond two points, or a set that is entirely simulated.

Three decisions in it are worth naming.

**The split is by person, not by observation.** The same face appears in dozens
of frames. Splitting by frame puts near-duplicates on both sides, and the
validate half then agrees with the tune half for reasons that have nothing to do
with the threshold.

**Unenrolled faces are mandatory, not optional.** Without observations of people
who are not in the gallery, the false-accept rate cannot be measured at all.
That is the error that marks a stranger safe under a colleague's name, so a set
lacking them is refused outright rather than warned about.

**An impossible ceiling raises rather than relaxes.** If no threshold pair holds
the false-accept rate under the ceiling, that is a finding about the pipeline.
The harness says so and stops. Loosening the ceiling afterwards would be
choosing the number after seeing the answer.

Run against a 300-agent simulated drill it produces a full report, selects an
operating point, measures drift on 83 held-out people, and then declines to
certify because the data is simulated. That refusal is the harness working.

### 9B.3 A simulator gap the harness exposed

The first harvest returned zero observations of unenrolled people, so the
false-accept rate was unmeasurable. The simulator was emitting `FACE_UNAVAILABLE`
for anyone without a gallery entry.

That was wrong in a way that flattered the system. A real matcher does not know
who is enrolled: it detects a stranger's face and returns its nearest gallery
entry, usually at a poor score and occasionally not. Skipping to
`FACE_UNAVAILABLE` meant the simulator could never produce the single most
dangerous error, and no test could have caught it.

Unenrolled people now go through the same detection, quality and pose path as
everyone else and receive a nearest-match at a low score. The safety property
still holds: zero false accounted across all three profiles.

### 9B.4 Measured drill results after the change

| Profile | False accounted | Accounted | Uncertain | Unaccounted | Manual | P50 | P95 |
|---|---|---|---|---|---|---|---|
| clean | **0** | 439 | 0 | 62 | 11 | 59.5 | 114.2 |
| realistic | **0** | 444 | 14 | 47 | 7 | 64.1 | 125.2 |
| hostile | **0** | 91 | 346 | 71 | 4 | 291.4 | 489.7 |

Eleven people now need human verification on a clean run, up from zero. They are
strangers whose nearest gallery match cleared the threshold, which is exactly
the population a warden should be walking over to check. The system finding them
is the point.

### 9B.5 What is still blocked

Everything requiring the GPU: reproducing the segfault, testing the §2.3
inference, building the pipeline, and taking any benchmark. Everything requiring
recorded footage: the calibration set, and therefore every certified threshold.

**No configuration in this system is marked calibrated, and none will be until
both exist.**

---

## 9C. Phase 3 results

Ingest, projections, replication and the chaos suite. Full detail in
`docs/EVAC120_RESILIENCE.md`.

### 9C.1 One fold, not two

The biggest change is structural. The simulator had its own copy of the
event-to-state fold, and the roster-to-track resolution that the board depends
on. Both now live in `app/ingest/`, and the simulator drives them.

That matters more than it sounds. A property proved against the simulator is now
a property of the production path, rather than of a lookalike that happens to
behave similarly today. It also removes the last place where the simulator could
quietly know something the real system cannot.

### 9C.2 Health is intervals, not a flag

`HealthLog` stores closed outage intervals with a cause, not a boolean. A
boolean answers "is the system degraded now?", which is the least useful version
of the question: by the time anyone reads a report, the outage is over and the
flag reads healthy.

Intervals answer the questions that matter. Was the system blind at the moment
it made this call. How much of the drill was it blind for, with overlapping
outages unioned rather than summed, so three cameras down at once is one blind
period. And which cameras were dark when this person was last seen, which is
what turns "we don't know where they are" into "the camera on their floor was
down for two minutes".

### 9C.3 What each failure actually costs

Two rows of that table are the ones that are easy to get wrong, and the chaos
suite pins both.

**A Postgres outage does not blind the system.** The core holds its state in
memory and projections are recomputable from the ledger. Treating a database
failure as a sight failure would make the system blind itself over something it
does not need to see through.

**A severed link to central is not an incident.** It is the designed operating
mode of a site with no Internet. The edge keeps deciding and buffers for later.

The simulator was getting the first one wrong: it emitted every infrastructure
outage as a generic pipeline failure, so a database outage read as blinding, and
three overlapping outages collapsed into one because the health log treats a
repeat failure of the same component as the same ongoing outage. A chaos test
caught it by counting outages. Each kind now carries its real component.

### 9C.4 Recovery point objective

| Scenario | Events lost |
|---|---|
| Edge process crashes | 0 |
| Edge loses power | 0 |
| Link to central drops | 0 |
| Central is rebuilt from scratch | 0 |
| Edge disk fails | Everything central has not acknowledged |
| Producer crashes mid-flight | Up to one flush interval |

The zeroes come from a SQLite WAL buffer with `synchronous=FULL`, which fsyncs
on commit. That claim is only true while the pragma is set, so a test asserts
the pragma directly: change it and the test fails rather than the guarantee
silently becoming false.

The last two rows are the real RPO, reported by `measure_rpo` from live counters
rather than from prose. "We buffer events" invites the belief that nothing can
be lost. Something can. It is bounded and measured, and the bound is worth
knowing before anyone relies on it.

### 9C.5 The all-clear cannot be declared while blind

`LiveBoard.all_clear` requires every expected person accounted for **and** no
open outage **and** a roster that was verified against its source. A blind
system cannot produce an all-clear however good its counts look, which is the
single failure invariant 8 exists to prevent.

`blocking_all_clear()` is never empty when the board is not clear, so an
operator always has the reason on screen rather than an absence of a green
light.

### 9C.6 Chaos suite

Two layers. `backend/tests/chaos/` kills each component in a simulated drill and
asserts the software's response in seconds with no infrastructure.
`scripts/evac_chaos.sh` kills real containers against a live stack, where the
assertions are coarser but the failures are real.

A service that stays up and reports healthy while blind is the failure both
layers exist to catch.

| Scenario | Asserted |
|---|---|
| One camera dies mid-drill | Board degrades, recovers, outage stays in the record, nobody aged into LOST |
| Every camera dies | Nobody newly cleared; caveat defers to the manual roll-call |
| DeepStream pipeline dies | Board degrades; all-clear blocked |
| Redis dies | Gaps reported as evidence, never filled |
| Postgres dies | System not blinded; accountability keeps working; all-clear still blocked |
| Link to central severed | Drill unaffected; nothing lost; central converges to the same conclusions |
| Partition then flood | Backlog drains; central complete; redelivery harmless |
| All of it at once | Nobody falsely cleared; every person keeps a state and a reason |

`scripts/evac_chaos.sh` skips every scenario on a host without the containers,
reporting skipped rather than passed. It has been syntax-checked and dry-run;
it has not been run against a live stack, because the edge service's process
wrapper lands with the API in Phase 4.

### 9C.7 A determinism bug the chaos suite exposed

Two tests passed in isolation and failed in the full suite. The cause was not
test pollution: the simulator seeded each agent's RNG with
`hash((seed, person_ref))`, and Python salts string hashing per interpreter.
Two runs of the same drill produced different trajectories.

That makes the whole suite untrustworthy in a specific way. A failing run could
not be reproduced, and a passing run proved nothing about the next one. Seeds
are now derived from an explicit digest, pinned by a test that asserts a fixed
value so a change of algorithm is visible rather than silent.

### 9C.8 Conservative recovery, found the same way

A chaos assertion counting outages failed because a 15% event-drop rate ate a
`CAMERA_RECOVERED`. The system's response was correct: it kept believing the
camera was down and refused an all-clear indefinitely.

That is the right direction to fail in. Staying degraded when the camera has
actually recovered costs an operator some confidence; falsely recovering would
let a blind system declare an all-clear. Only one of those is survivable, and
there is now a test asserting the system picks it.

### 9C.9 Phase 3 gate

| Check | Result |
|---|---|
| Full suite | **538 passed, 1 xfailed**, 182 s |
| Coverage | 88% overall, 97% on `app/core` |
| Chaos suite | 28 tests, all green |
| Container chaos script | syntax-checked and dry-run; skips on a host without the stack |
| RPO documented | `docs/EVAC120_RESILIENCE.md` §5 |
| False accounted, all profiles | **0** |

---

## 9D. Phase 4 results

Everything except the gate. The gate is an end-to-end dry run on the Sialkot
office with real cameras, a real roster and a real warden device, and that needs
hardware and people rather than code.

### 9D.1 What was built

| Piece | Where | Tests |
|---|---|---|
| Warden domain: actions, headcount, sweep | `app/warden/` | 70 |
| Drill lifecycle and the two-part all-clear | `app/drill.py` | via API |
| HTTP surface, 14 endpoints, OpenAPI | `app/api/` | 48 |
| Audit log and retention policy | `app/infra/` | 29 |
| Command centre | `frontend/index.html` | 45 shared |
| Warden PWA, offline-capable | `frontend/warden.html` | with the above |

Documents: `EVAC120_SECURITY.md`, `EVAC120_DEPLOYMENT.md`,
`EVAC120_WARDEN_PWA.md`.

### 9D.2 The headcount asymmetry

The most important decision in the phase. If the system counted more people than
the warden did, it believes people are safe who are not standing there, which is
the exact failure the product exists to prevent. It escalates at a difference of
one, and its tolerance defaults to zero. If the warden counted more, there are
unbadged people present — worth investigating, not alarming.

A single absolute difference against one threshold would let the dangerous
direction hide inside a tolerance chosen to accommodate the harmless one.

### 9D.3 Complete is not clean

Completing a sweep is always accepted, even when the system disagrees. Refusing
it would be the software overruling the human it designated the final authority.

But a clean zone needs four things: the sweep finished, nobody unresolved, a
physical headcount actually taken, and that count agreeing. Walking a zone
ticking people off the system's own list is checking the system against itself,
so a sweep with no independent count cannot be clean. That was a bug found by a
test, where `is_clean` said yes while `blocking_clean` explained why not.

The all-clear now has two halves, and both are required. The board's counts are
one; every zone swept and agreeing is the other.

### 9D.4 No framework on the front end

A reversal of the earlier plan, recorded in `CLAUDE.md` rather than left
implicit. The warden tablet must start from cache with no network, and a bundle
the service worker caches is a bundle that must be rebuilt, versioned and
invalidated correctly or the app silently stops working offline. Both surfaces
are read-mostly, and a life-safety tool with several hundred transitive
dependencies is a liability. The cost is stated too: no component model, no type
checking, manual DOM updates.

Two bugs came out of testing the offline queue against a working in-memory
IndexedDB rather than a mock of itself. It hung forever on an IndexedDB error
because only `onsuccess` was wired — on a tablet that looks like the app
thinking rather than failing, and a warden moves on. And it read
`navigator.onLine` from a global, which Node 22 makes read-only; that was a
signal, and the check is now injected.

### 9D.5 Evidence is not biometrics

The distinction the retention policy turns on. "A face matched EMP-482 at 10:41
above threshold on camera 9" is evidence a report needs for years. The 512-float
embedding that produced it is needed for as long as the matching takes. Keeping
it because it is in the same pipeline is how a drill system becomes a biometric
database nobody signed up for.

Embeddings and crops are purged immediately after use. Warden thumbnails go at
drill end, synced or not. The evidence ledger outlives the faces by a year, and
the audit log outlives the evidence, so each layer can account for the one below.

Three enforcement details matter more than the durations, which are marked
unreviewed:

- Constructing a policy that omits a data class raises. An item nobody decided
  about lives forever by default.
- A purge that fails is not recorded as a purge. Recording a deletion that did
  not happen would make the policy a lie that passes its own verification.
- `verify` treats overdue biometric material as a finding, not a warning.

### 9D.6 A gap a later review found

Phase 4 was reported as built, and the bottleneck panel the brief asks for —
people per minute per exit, queue length, density, dwell — had not been built at
all. Nothing in the documents claimed it specifically, which is how it went
unnoticed: the phase was marked done and the missing piece was inside it.

It exists now, in `app/ingest/bottlenecks.py`, measured from the same zone
sightings the presence machine uses so it cannot disagree with the board about
where somebody was. Density is reported only where a capacity was actually
recorded: a crowding figure against an invented denominator is worse than none,
because it is the kind of number that ends up in a report.

The same review found that five of the six modules vendored from VisionTrack in
Phase 0 were used by nothing. `docs/EVAC120_PROVENANCE.md` now says which, and
why, rather than leaving the original rationale standing as though it still
described the code.

### 9D.7 Phase 4 gate

| Check | Result |
|---|---|
| Backend suite | **685 passed, 1 xfailed**, 202 s |
| Frontend suite | 45 passed |
| Endpoints | 14, OpenAPI generated |
| Separation of duty | asserted per route |
| No raw AI metrics on the board | asserted |
| Service worker install list | every file verified to exist |
| **Live dry run at Sialkot** | **blocked: needs real cameras, roster and a warden device** |

---

## 9E. Phase 5: the report is built, the drills are not

Three live drills need a building, cameras, a roster and wardens. What does not
need any of those is the analysis that runs afterwards, and building it now
means the moment a real drill happens the assessment is ready rather than being
written in hindsight by someone who already knows the answer.

Full detail in `docs/EVAC120_VALIDATION.md`.

### 9E.1 A real drill has no oracle

The central problem. In simulation the agents know where they went, so "false
accounted" is a set difference against ground truth. In a building on a Tuesday
afternoon nobody has that list — which is exactly why the warden's physical
roll-call exists.

So validation is defined **against the manual roll-call**, not against truth.
When the system and a warden disagree, the warden is right by definition. Not as
a courtesy, as the reference.

### 9E.2 The two disagreements are never merged

**False accounted** means the system said safe and no warden confirmed it. A
safety failure. Must be zero, and every instance is listed individually with the
system's own reasoning beside it, because each one is a person somebody has to
go and find.

**False unaccounted** means a warden confirmed someone the system could not.
A quality failure: it wastes a warden's time and erodes trust in the board, but
nobody is left in a building because of it.

Averaging them into an accuracy figure would hide the only one that matters.

### 9E.3 INCONCLUSIVE is not a soft pass

Three outcomes, and the third does the most work. A drill where the cameras were
blind for half the time, or where no warden completed a sweep, cannot validate
the system even if every number on the board is perfect. There was nothing to
check the board against.

Without this, a system accumulates a record of successful drills that prove
nothing. A run where the wardens did not turn up and the cameras saw everything
produces beautiful numbers and is worth exactly as much as a self-marked exam.

Two criteria are evidentiary rather than quality, and the classification is the
interesting part. **Taking a physical headcount** is evidentiary because walking
a zone ticking people off the system's own list is checking the system against
itself. And **coverage** guards against the easiest way to fake a P95: a
percentile computed only over people tracked end to end improves when the slow,
hard-to-track people fall out of the sample.

### 9E.4 A worked example

A 200-agent simulated drill under the realistic profile, driven through the real
drill object rather than a test harness:

```
INCONCLUSIVE — only 83% of people were tracked from alarm to arrival

  FALSE ACCOUNTED         0
  false unaccounted       0
  P50 64.0s  P90 104.8s  P95 123.4s  P99 195.3s
  measured on             177 people (83% coverage)
  slowest floor           basement at P95 158.9s
  sweeps completed        2/2      blind for 20% of the drill
```

Nobody was falsely accounted, every zone was swept and counted, and the counts
agreed. The system did well by the measures that matter most, and the drill
still cannot validate it, because a camera was down for two minutes and 17% of
people produced no timing at all.

That is the intended behaviour. It says fix the coverage and run it again, not
celebrate the zero.

### 9E.5 Phase 5 gate

| Check | Result |
|---|---|
| Full suite | **712 passed, 1 xfailed**, 185 s |
| Report generator and criteria | built, 27 tests |
| Worked example on simulated data | §9E.4 |
| **Three live drills** | **blocked: needs a building, cameras, a roster and wardens** |

---

## 10. Open risks

1. **FaceTrack is down.** Postgres password authentication is failing.
   `roster.py` cannot be built against real data until it is fixed. The
   simulator does not depend on it.
2. **The P2.3b segfault is unresolved, and it is a pyds bug rather than an
   inference-plugin bug.** `~/visiontrack-checkpoints/p2-3-deferred/README.md`
   proves it: nvinfer and nvinferserver crash identically with SIGSEGV whenever
   any buffer probe is registered downstream of an SGIE producing tensor
   user-meta. Phase 2 depends on exactly that probe. Four documented paths
   forward: a pyds rebuild with a pinned version, a DeepStream 7.0 downgrade
   test, a C-side probe that keeps Python off the buffer path, or an NVIDIA
   reproducer.
3. **The roster has no department, floor or warden assignment** (§3.1d).
   firedrill must own those fields, and someone must populate them before a real
   drill.
4. **Floor plans, zones and camera calibration now live in two places.** The
   sync path from VisionTrack is Phase 3 work and is a real source of drift.
5. **No threshold is validated.** Nothing in Phases 1 and 2 may hard-code a
   production number. Every one comes from the Phase 2 calibration set and is
   recorded in `docs/EVAC120_CALIBRATION.md`.
6. **The vendored falsy-zero defect meets frame PTS** (§5). Phase 1 must
   normalise timestamps at the ingest boundary.

---

## 11. Deliverables checklist

Audited against the filesystem, not against memory.

| Deliverable | Where | State |
|---|---|---|
| Architecture assessment | §2-§3 | Done |
| Component / data-flow diagram | `docs/EVAC120_ARCHITECTURE.md` | Done |
| Integration plan | §2.2 | Done |
| Tree map | §7 | Done |
| Vendoring provenance | `docs/EVAC120_PROVENANCE.md` | Done |
| State-machine transition tables | `docs/EVAC120_STATE_MACHINES.md` | Done |
| Event schema | `app/core/events.py` | Done |
| Identity persistence algorithm | `app/core/identity_fsm.py` | Done |
| Accountability algorithm | `app/core/accountability_fsm.py` | Done |
| Test strategy | `backend/tests/`, 712 tests | Done |
| DeepStream integration design | `docs/EVAC120_DEEPSTREAM.md` | Done |
| Failure / recovery strategy | `docs/EVAC120_RESILIENCE.md` | Done |
| Security model | `docs/EVAC120_SECURITY.md` | Done |
| OpenAPI spec for `/api/evac/*` | generated at `/openapi.json`, 14 routes | Done |
| Deployment guide | `docs/EVAC120_DEPLOYMENT.md` | Done |
| Warden PWA install guide | `docs/EVAC120_WARDEN_PWA.md` | Done |
| Validation criteria and report | `docs/EVAC120_VALIDATION.md`, `app/reporting/` | Done |
| Alembic schema | `backend/alembic/versions/0001_event_log.py` | Done. `evac_events` plus drills, zone kinds and the audit log |
| **Calibration report** | `docs/EVAC120_CALIBRATION.md` | **Empty by necessity.** The harness is built and refuses to certify simulated data. Blocked on the pipeline and on recorded footage |
| **Benchmark report** | `docs/EVAC120_BENCHMARKS.md` | **Empty by necessity.** The measurement plan and the hardware it must be tied to are recorded; no figure is estimated |

The last three are stated as gaps rather than quietly omitted. Two of the
documents exist and contain no numbers on purpose: a plausible-looking FPS
figure nobody measured is worse than an empty table, because it will be quoted.
