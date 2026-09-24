# Vendored code — provenance

Everything under `backend/app/vendor/` was **copied**, not written here. This
file records where each file came from, why it was worth copying, and what was
changed on the way in.

Source revisions at copy time, 2026-09-10:

| Repo | Path | Revision |
|---|---|---|
| VisionTrack | `~/visiontrack/visiontrack` | `3592c72` |
| DeepStream | `~/deploy/deepstream` | `44d3ea2` |

## Rules

1. **Do not edit a vendored file to fix an upstream bug.** Fix it upstream and
   re-vendor. A defect found while vendoring is recorded here and pinned by a
   test, not patched in place.
2. The only edits permitted on copy are **import-path rewrites**, listed below.
3. Every vendored file carries a header naming its origin repo, path and
   revision.
4. Anything that needs a database session, a Redis client or a web framework was
   **not** vendored. Those dependencies are why the file was left behind.

Rules 1 and 2 are now **checked rather than trusted**. Every vendored file is
compared line by line against `git show <commit>:<path>` in its source repo,
taken from the file's own header, and the only difference accepted is an
import-path rewrite that imports the same symbols. Anything else — a line
added, a line removed, an operator changed — fails the gate and names the line.

That was the one rule with nothing behind it. The existing tests prove a
vendored file imports, still declares its origin, does not reach back into its
source repo, and is credited honestly in the usage table below; none of them
looks at what the code does, so a one-line fix made here rather than upstream
passed every gate — and the next re-vendor would silently revert it, which is
exactly what rule 1 exists to prevent.

The comparison skips when a source repo is not checked out, which is the normal
state of an edge node. A separate test fails if *every* comparison skips, so a
suite that has quietly stopped checking says so.

## From VisionTrack

| Vendored as | Origin | Loc | Why |
|---|---|---|---|
| `visiontrack/zones_math.py` | `backend/app/modules/floor_plans/zones_math.py` | 120 | Ray-cast point-in-polygon and area-weighted centroid. |
| `visiontrack/zone_resolve.py` | `backend/app/modules/analytics/zone_resolve.py` | 183 | Projects a bounding box to a floor point through the camera homography, then resolves which zones contain it. |
| `visiontrack/occupancy.py` | `backend/app/modules/analytics/occupancy.py` | 143 | Per-zone head counts, keeping known and unknown people separate. |
| `visiontrack/dwell.py` | `backend/app/modules/analytics/dwell.py` | 331 | Interval union and per-zone durations. The union is what stops a person being double-counted across two cameras. |
| `visiontrack/heatmap.py` | `backend/app/modules/analytics/heatmap.py` | 77 | Density binning for the bottleneck panel. |
| `visiontrack/rule_state.py` | `backend/app/modules/alerts/evaluator.py` | 195 | Hold-time and hysteresis. Presence transitions reuse this shape so one noisy frame cannot flip a person's state. |

### Changes on copy

Four import lines were rewritten. Nothing else changed.

```
occupancy.py:28    app.modules.floor_plans.zones_math -> app.vendor.visiontrack.zones_math
zone_resolve.py:34 app.modules.analytics.dwell        -> app.vendor.visiontrack.dwell
zone_resolve.py:35 app.modules.floor_plans.zones_math -> app.vendor.visiontrack.zones_math
dwell.py:41        app.modules.floor_plans.zones_math -> app.vendor.visiontrack.zones_math
```

The file was renamed `evaluator.py` to `rule_state.py` because only its
hold-time state machine is used here; EVAC-120 does not evaluate alert rules.

### What is actually used, a review later

Vendoring is a promise about future use, and promises drift. Checked against the
code rather than against the Phase 0 intent:

| Vendored | Used by | Status |
|---|---|---|
| `zones_math.py` | `app/simulator/site.py`, and every polygon test | **In use** |
| `dwell.py` | `app/ingest/bottlenecks.py`, for the interval union | **In use** |
| `zone_resolve.py` | nothing yet | Waiting on the DeepStream probe, which needs it to project a bounding box to a floor point |
| `occupancy.py` | nothing | The bottleneck panel counts from the event stream instead, which cannot disagree with the board |
| `heatmap.py` | nothing | Density binning for a heat map nobody has asked for. **Dead weight** |
| `rule_state.py` | nothing | Hold-time and hysteresis were reimplemented in `presence_fsm.py`, because presence needed blind-zone and degradation rules this does not have |

Two of six are used, one is waiting on blocked work, and three are not. That is
worth stating plainly rather than leaving the Phase 0 rationale standing as
though it still described the code.

**No vendored geometry is in the production path.** `zones_math` resolves
points against polygons for the simulator's site model; the running system
never does that arithmetic, because the DeepStream pipeline decides the zone
and puts `zone_id` in the event payload. The Phase 0 line that "every zone
decision rests on it" described a design where EVAC-120 projected bounding
boxes itself, which is the work `zone_resolve.py` is still waiting for.

This table is now derived from the code by
`TestTheUsageReviewIsCheckedRatherThanTrusted` in
`backend/tests/test_vendor_integrity.py`, which fails when a module is credited
to a file that does not import it, or is marked in use when nothing imports it
at all. The first version of this review said `zones_math` was used by
`app/sync/geometry.py`, which mentions the vendored coordinate convention in a
comment and imports nothing. A review that is only true on the day it is
written is the Phase 0 rationale again with a later date on it.

`heatmap.py` and `rule_state.py` should be removed when someone is confident
nothing will want them. They are kept for now because the DeepStream work is
blocked and its shape may still change; `occupancy.py` is the same call. What
they are not is evidence that vendoring was wrong — `zones_math` and `dwell` did
exactly what they were brought in to do.

### Deliberately not vendored

| Left behind | Why |
|---|---|
| `backend/app/modules/mv3dt/fuser.py` | Bound to VisionTrack's SQLAlchemy models and session. EVAC-120 consumes its **output** as `global_person_id` on the Redis stream instead. |
| `backend/app/modules/persons/matcher.py` | Requires Milvus. Re-identification similarity is not employee identity anyway, so it is an evidence source, not a dependency. |
| `backend/app/modules/alerts/track_state.py` | Redis-backed persistence of the state `rule_state.py` computes. Phase 3 writes its own, to firedrill's own Postgres. |
| `backend/app/modules/persons/face_identity_consumer.py` | A stream consumer tied to VisionTrack's schema. Phase 3 writes the equivalent against firedrill's event model. |

## From DeepStream

| Vendored as | Origin | Loc | Why |
|---|---|---|---|
| `deepstream/recognition.py` | `utils/recognition.py` | 192 | `Gallery` matmul with score and margin gates, and `TrackIdentityManager` per-track voting with sticky commit and TTL eviction. The starting point for `app/core/identity_fsm.py`. |
| `deepstream/outbox.py` | `utils/outbox.py` | 96 | SQLite WAL store-and-forward with `synchronous=FULL`, so buffered events survive power loss. Read as a design, **not imported**: see below. |

### Changes on copy

None. Both files are byte-identical below the provenance header.

### What is actually used

Neither. `recognition.py` was the starting point for `app/core/identity_fsm.py`
and the limits recorded at the bottom of this file are why that is a rewrite
rather than a wrapper.

`outbox.py` is the one worth being explicit about, because the code that
replaced it said otherwise. `app/ingest/replication.py` described its `Outbox`
as "a thin, testable wrapper over the vendored store" and claimed durability
came from it. It does not import it. The vendored module reads its directory
from a module-level global at import time -- defect 2 below -- so `replication`
owns its own connection and its own `CREATE TABLE`, and what it took from the
vendored file is the WAL and `synchronous=FULL` design rather than the code.
The difference matters: a reader chasing a buffering bug upstream would be
looking at a file that is not in the path.

### Deferred to Phase 2

`utils/face_align.py` (Umeyama alignment) and `utils/arcface_embedder.py`
(ONNX and TensorRT embedding) are needed only once a real pipeline runs. They
pull in `cv2` and a GPU runtime, so they stay out of the Phase 0/1 dependency
set. `utils/reliability.py` is mixed: its health-endpoint and watchdog halves
are stdlib and worth vendoring in Phase 3, but its source-reconnect half needs
GStreamer.

## Defects found while vendoring

### 1. Falsy-zero restarts the hold timer

`rule_state.py` (upstream `backend/app/modules/alerts/evaluator.py`) starts its hold timer with:

```python
since = state.condition_since_ms or now_ms
```

A `condition_since_ms` of exactly `0` is falsy, so the timer restarts every
tick and the rule never fires. VisionTrack never hits this because it passes
epoch milliseconds.

**EVAC-120 can hit it.** Camera events carry `pts_ms`, the frame presentation
timestamp, and PTS starts at 0 for the first frame of a stream.

Pinned by `TestZeroTimestampBug` in `backend/tests/test_vendor_visiontrack.py`
as a strict xfail, so it flips to XPASS the moment it is fixed upstream.

**The obligation on Phase 1 was met, and the pattern came back anyway.**
`normalise_timestamp` converts a camera's PTS to epoch milliseconds and refuses
a camera event that arrives without a `stream_origin_ms`, so the boundary is
sound. But `or` on a timestamp reappeared twice in code written here:

| Site | What a zero does |
|---|---|
| `build_board`: `state.drill_started_ms or now_ms` | The health window collapses to `(now, now)`. Measured: a drill blind for 60% of its length reported `blind_fraction` **0%**, and the caveat called the blackout non-blinding. |
| `Drill.elapsed_ms`: `(self.completed_ms or now_ms)` | A completed drill reads as still running — two lines under the `started_ms is None` check that gets it right. |

Both are `is None` now, which keeps the behaviour the fallback was for: an
unstarted drill genuinely has no window, and a running one genuinely measures
to now. Neither was reachable through today's production path, because a
`DRILL_STARTED` event carries wall clock — but `Event` validation explicitly
permits `ts_ms` of 0, and unreachable by accident, through a guard somewhere
else, is not the same as correct. Both are pinned by tests that set the
timestamp to 0 directly.

### 2. Outbox configuration is read at two different times

`OUTBOX_DIR` is a module-level constant read at import; `COMPANY_ID` is read
from the environment on every `_path()` call. So the two halves of one
configuration are fixed at different moments, and setting `OUTBOX_DIR` after
the module is imported has no effect. Not a bug upstream, but a trap worth
naming.

**Phase 3 resolved it by not importing the module.** `app/ingest/replication.py`
reimplemented the store-and-forward rather than vendoring it, and the usage
table above records `outbox.py` as read for its design and **not imported** —
a claim `test_vendor_integrity.py` enforces in both directions, so the day
anything does import it the table has to say so.

This section used to read "Phase 3's config layer must set `OUTBOX_DIR` …
before first import", which was an obligation on a phase that is finished and
that met it by a route the sentence did not anticipate. The trap is kept named
here for whoever imports the module later; it is not outstanding work.

## Limits of the vendored identity code

`TrackIdentityManager` is the starting point for Phase 1's identity FSM, not a
substitute for it. Three gaps, each pinned by a test in
`backend/tests/test_vendor_deepstream.py::TestVendoredIdentityLimits`:

1. **Two outcomes only** — committed or nothing. No `CANDIDATE`,
   `TEMPORARILY_UNAVAILABLE`, `CONFLICT` or `REJECTED`.
2. **Conflict is resolved silently by majority vote.** Two identities competing
   for one track produce a winner and no signal. Invariant 3 requires
   `IDENTITY_CONFLICT` then `MANUAL_VERIFICATION_REQUIRED`.
3. **Keyed on a camera-local tracker object id.** The same person on two cameras
   is two independent identities. EVAC-120 keys identity on the global person
   id.
