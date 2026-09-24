# EVAC-120 DeepStream integration design

**Status: design complete, not built.** The GPU work is blocked, and the reason
is worth reading before anything else here: §2 argues that the blocker has a
cheaper way around it than the four already planned, and that the way around is
already running in production one repository over.

Nothing in this document has been executed on a GPU. Every claim about the crash
is inference from evidence collected by others plus code that demonstrably runs.
Where that distinction matters it is stated in the text.

---

## 1. What the pipeline has to produce

EVAC-120 consumes events, not frames. The pipeline's entire job is to turn video
into the stream defined in `docs/EVAC120.md` §5, with three properties:

**A shared track id.** Face and body must come from one tracker. Today they come
from two, correlated by bounding-box overlap, and `app/core/fusion.py` exists to
make the weakness of that correlation visible rather than to fix it. A shared
track id is the fix, and it is the single highest-value change in Phase 2.

**Per-source sequence numbers.** Every event carries a monotonic `seq` from its
source so ingest can be idempotent and gaps detectable. This is not optional
plumbing: `SequenceTracker` reports a hole rather than interpolating across it,
and that reporting is what stops a dropped event from reading as a quiet moment.

**Non-blocking probes.** A probe that does work on the streaming thread drops
frames. Probes push typed events onto a queue; a publisher thread drains it into
a local SQLite outbox and from there into Redis. The outbox is the vendored
DeepStream one, already proven in production.

---

## 2. The P2.3b segfault, and a fifth way around it

### 2.1 What is established

From `~/visiontrack-checkpoints/p2-3-deferred/README.md`, which records a
two-session investigation:

- `ai-worker-ds` segfaults, SIGSEGV exit 139, about five seconds after a buffer
  probe is registered downstream of an SGIE that produces tensor user-meta.
- It crashes identically with `nvinfer` and with `nvinferserver` (Triton).
- It crashes regardless of probe pad, probe body (a trivial lambda crashes too),
  and the `output-tensor-meta` setting.

The conclusion drawn there is that the bug is in pyds metadata dispatch rather
than in the inference plugin. Testing two independent inference backends and
getting the same crash is good evidence for that, and nothing here disputes it.

The four planned remedies are a pyds rebuild from pinned bindings, a DeepStream
7.0 downgrade A/B, a C-side probe, and an NVIDIA reproducer. Each is sound. All
four are also expensive: the cheapest starts with a 26 GB image rebuild, and the
project's own notes record that a previous rebuild caused a libstdc++ ABI
regression.

### 2.2 What the sibling repository already does

The DeepStream face pipeline in `~/deploy/deepstream` runs in production. Its
canonical entry point builds:

```
streammux → nvinfer (PGIE, YOLOv8n-face) → nvtracker → nvvideoconvert(RGBA)
          → capsfilter → [enterprise probe] → tiler → nvvideoconvert → nvdsosd → tee
```

Two things about it matter.

**There is no SGIE.** One `nvinfer` element, the primary detector. Nothing else.

**Buffer probes work.** `main_enterprise.py` attaches a buffer probe to the PGIE
src pad, and the enterprise recognition probe to the RGBA capsfilter. Both run in
production against live cameras.

Face embeddings are produced inside the probe by a standalone ArcFace TensorRT
engine. `utils/probe_enterprise.py` says so in its own module docstring:
embeddings are computed in-probe by a standalone engine, *so this probe runs
without an ArcFace SGIE*.

### 2.3 The inference

The documented crash condition is a buffer probe **downstream of an SGIE
producing tensor user-meta**. The production pipeline has buffer probes and no
SGIE, so it never creates that condition, and it does not crash.

That suggests a fifth remedy, cheaper than all four planned: **do not use an
SGIE for embeddings.** Extract the region of interest in a probe attached where
probes are already proven stable, and run the embedding model as a standalone
TensorRT engine in the probe, exactly as the face pipeline already does.

Applied to EVAC-120's requirements:

| Need | SGIE approach (crashes) | In-probe approach (proven) |
|---|---|---|
| Body Re-ID (OSNet) | SGIE after tracker, probe on its src pad | Crop person ROI in the tracker-src probe, batch through a standalone OSNet engine |
| Face embedding (ArcFace) | Second SGIE, second probe | Already done this way in `probe_enterprise.py` |

### 2.4 What this is not

This is an inference from two facts, not a reproduction. Specifically:

- The segfault has not been reproduced here. The 26 GB image is not built on
  this machine and the project's notes advise against rebuilding it casually.
- The production pipeline's stability is evidence that *its* configuration does
  not crash. It is not proof that removing the SGIE is what makes the difference,
  because the two pipelines differ in other ways too — different base image,
  different models, different DeepStream version.
- Moving inference into a Python probe has a real cost the SGIE does not: the
  embedding batch runs on the streaming thread unless it is handed off, and the
  face pipeline manages this by embedding once per track rather than per frame.
  Whether that budget survives adding OSNet on every person is a measurement,
  and §4 is how to take it.

**The cheap test that settles it** does not need a rebuild: take the pipeline
that currently crashes, remove the SGIE, keep the probe, and attach a standalone
engine in the probe. If it survives past five seconds, the inference holds. If
it still crashes, the inference is wrong and the four planned remedies are the
route. That test costs one config change and one restart.

### 2.5 What it costs while it is unfixed

With no shared tracker, a face is attached to a body by geometry, and
`identity_fsm.gate` refuses a geometric association outright rather than
discounting it. The simulator injects that failure as `misassociation_rate`,
and the shape is a cliff rather than a slope. Measured on a 200-person drill,
seed 20260910, with nothing else injected:

| Faces misassociated | Accounted | Falsely accounted |
|---|---|---|
| none | 170 | 0 |
| half | 172 | 0 |
| nine in ten | 143 | 0 |
| all | 0 | 0 |

Partial weakness is close to free, because the frames that survive still carry
enough votes to confirm. Total weakness accounts for nobody through the cameras
and falls back entirely on the wardens. Nothing is cleared falsely at any rate,
which is the gate behaving as designed — the cost of this bug is measured in
accountability, never in safety.

---

## 3. Proposed pipeline

```
nvurisrcbin (reconnect) ─┐
nvurisrcbin (reconnect) ─┼→ nvstreammux → nvinfer (PeopleNet PGIE)
nvurisrcbin (reconnect) ─┘                      ↓
                                          nvtracker (NvDCF)
                                                ↓
                                     nvvideoconvert (RGBA, unified)
                                                ↓
                                    ┌───── [evac probe] ─────┐
                                    │  person ROI → OSNet    │
                                    │  face detect on ROI    │
                                    │  landmarks → align     │
                                    │  → ArcFace engine      │
                                    │  → zone/line resolve   │
                                    │  → typed events → queue│
                                    └────────────┬───────────┘
                                                 ↓
                                              fakesink
                                                 │
                   publisher thread ← queue ─────┘
                          ↓
              SQLite outbox (WAL, synchronous=FULL)
                          ↓
              Redis  vt:evac:events:<tenant>
```

One tracker, so face and body share a `track_id` and `fusion.py` reports
`SHARED_TRACK` rather than `SPATIAL_IOU`. That single change is what promotes
face evidence from inadmissible to admissible under the current identity config.

### 3.1 Probe responsibilities

The probe emits typed events and does no accountability reasoning. Zone and line
events come from the vendored `zone_resolve.project_foot`, using the homography
from `cameras.calibration`, so the pipeline and the core agree on geometry by
construction rather than by convention.

| Event | Emitted when |
|---|---|
| `TRACK_CREATED` / `TRACK_UPDATED` / `TRACK_LOST` | Tracker lifecycle |
| `FACE_OBSERVED` | A face was embedded and matched, with score, margin, quality, pose |
| `FACE_UNAVAILABLE` | A person was tracked and no usable face was obtained |
| `PERSON_ENTERED_ASSEMBLY` / `PERSON_LEFT_ASSEMBLY` | Foot point crossed an `ASSEMBLY` polygon |
| `PERSON_EXITED_BUILDING` | Foot point crossed an `EXIT` line |
| `CAMERA_FAILURE` / `CAMERA_RECOVERED` | Reliability watchdog, from vendored `reliability.py` |

`FACE_UNAVAILABLE` is emitted deliberately and often. A tracked person with no
usable face is a fact worth recording; silence would be indistinguishable from
the person not being there.

### 3.2 Timestamps

Camera events carry `pts_ms`, the frame presentation timestamp, which starts at
0. Every event is normalised to epoch milliseconds against the stream origin
before it becomes an `Event`. This is not a detail: a PTS of 0 taken literally
puts a live drill in 1970, and it is also the value that triggers the
falsy-zero defect recorded in `docs/EVAC120_PROVENANCE.md`.

---

## 4. Benchmarks to take

Numbers to measure, not to estimate. None of these has a value yet.

| Measurement | Why it decides something |
|---|---|
| FPS per stream, at 1, 4, 8, 16 streams | Where the box saturates, which sets cameras per edge node |
| Probe residency per frame | Whether in-probe inference fits the frame budget (§2.4) |
| End-to-end event latency, PTS to Redis | Whether the live board is live |
| GPU memory at each stream count | Whether an RTX 3070's 8 GB holds the models plus decode |
| ID-switch rate per person-minute | Directly sets `id_switch_rate` in the simulator |
| Face TPR and FPR at the chosen thresholds | Feeds `docs/EVAC120_CALIBRATION.md` |
| Duplicate-count rate at the double-covered exit | Whether one person becomes two |
| Reconnect time after a camera drop | Sets `t_lost_ms` honestly |

The last three feed straight back into configuration. Until they exist, every
threshold stays `calibrated=False`.

---

## 5. Calibration

The harness is built and tested: `app/calibration/`, 35 tests. It takes labelled
observations, splits them by person, sweeps score and margin, and picks an
operating point by holding the false-accept rate under a stated ceiling.

It is exercised on simulator output today and **refuses to certify it**, because
a set that is entirely simulated measures the model of a matcher rather than a
matcher. That refusal is the design working. When recorded footage exists, the
only new work is labelling.

Three properties worth knowing before Phase 2 uses it:

- **The split is by person, not by observation.** The same face appears in dozens
  of frames; splitting by frame puts near-duplicates on both sides and leaks the
  answer.
- **Unenrolled faces are required.** Without observations of people who are not
  in the gallery, the false-accept rate cannot be measured at all, and that is
  the error that marks a stranger safe under a colleague's name.
- **An impossible ceiling raises rather than relaxes.** If no threshold pair
  holds the false-accept rate, that is a finding about the pipeline. Loosening
  the ceiling afterwards is choosing the number after seeing the answer.

---

## 6. What Phase 2 does, in order

1. **Test the §2.3 inference.** Remove the SGIE from the crashing pipeline, keep
   the probe, embed in-probe. One config change, one restart. Survives 60 s or
   it does not.
2. If it survives, build the §3 pipeline on one camera and take the §4
   benchmarks.
3. If it does not, fall back to the runbook's four remedies in their stated
   order, starting with the pinned pyds rebuild.
4. Record two walk-through drills, label them, and run the harness. Replace
   every provisional config from the certified output.
5. Write `docs/EVAC120_CALIBRATION.md` and `docs/EVAC120_BENCHMARKS.md` from
   measurements only.

Steps 1 to 3 need the GPU host and a built image. Steps 4 and 5 need recorded
footage. None of it can be faked, and none of it is faked here.

**Steps 1 to 3 are not this repository's to run**, and that is worth stating
because the list above reads like a backlog. They act on `ai-worker-ds` — its
pipeline wiring, its SGIE configs, its image — which lives in VisionTrack, and
`CLAUDE.md` makes all three source repositories read-only from here. VisionTrack
has its own ordered plan for them in
`~/visiontrack-checkpoints/p2-3-deferred/RUNBOOK.md`, with pre-flight
checkpoints, a rollback tag and a success signal, and the parked code to
re-enable rather than rewrite.

So §2.3 is a **recommendation to that team**, not work queued here: their
runbook opens with a pinned-pyds rebuild, and the argument above is that
removing the SGIE is cheaper and should be tried first. What EVAC-120 owns is
everything downstream of the embeddings, which is built and tested against the
simulator, and steps 4 and 5 the day real footage exists.

Checked on this host: the GPU is present and so is a `visiontrack-ai-worker`
image, so the test is runnable — by whoever owns that stack. The
SGIE-disabled pipeline parked in that checkpoint directory is the *stable
baseline*, not the proposed test, so the §2.3 inference remains untried.
