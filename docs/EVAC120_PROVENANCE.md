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

## From VisionTrack

| Vendored as | Origin | Loc | Why |
|---|---|---|---|
| `visiontrack/zones_math.py` | `backend/app/modules/floor_plans/zones_math.py` | 120 | Ray-cast point-in-polygon and area-weighted centroid. Every zone decision rests on it. |
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
| `deepstream/outbox.py` | `utils/outbox.py` | 96 | SQLite WAL store-and-forward with `synchronous=FULL`, so buffered events survive power loss. Edge-to-central replication in Phase 3. |

### Changes on copy

None. Both files are byte-identical below the provenance header.

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
as a strict xfail, so it flips to XPASS the moment it is fixed upstream. Phase 1
must normalise timestamps to epoch milliseconds at the ingest boundary, or fix
the check when this logic is reimplemented in `presence_fsm.py`.

### 2. Outbox configuration is read at two different times

`OUTBOX_DIR` is a module-level constant read at import; `COMPANY_ID` is read
from the environment on every `_path()` call. Phase 3's config layer must set
`OUTBOX_DIR` from `EVAC_OUTBOX_DIR` **before** first import of the module.
Not a bug upstream, but a trap worth naming.

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
