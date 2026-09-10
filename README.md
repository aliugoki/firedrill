# EVAC-120

Fire Drill & Evacuation Accountability.

**Target:** P95 building evacuation ≤ 120 s, measured on real drills. The number
is an operational goal to be measured against, not a guarantee the software
makes.

> EVAC-120 supplements, and never replaces, certified fire-detection and
> life-safety systems. It does not control alarms, doors or suppression. Its
> output is decision support for a human incident commander, and a floor
> warden's physical count is the final authority.

## What it does

Answers one question, continuously, during an evacuation: **who is still in the
building?** It answers it from evidence, and it says how confident it is.

Three separate state machines — identity, presence and accountability — feed an
append-only evidence ledger. Every decision is reconstructable: asking why a
given person was marked accounted returns the list of observations that led
there. Nothing resolves a conflict silently; disagreement escalates to a human.

## Where things are

| Path | What |
|---|---|
| `backend/app/core/` | Pure-Python domain. No database, no Redis, no framework. |
| `backend/app/vendor/` | Code copied from VisionTrack and DeepStream. See `docs/EVAC120_PROVENANCE.md`. |
| `backend/app/simulator/` | Synthetic drills and failure injection. |
| `backend/app/ingest/` | Redis consumer, projections, edge-to-central replication. |
| `backend/app/api/` | HTTP and WebSocket surface. |
| `backend/app/infra/` | Config, database, auth, permissions. |
| `frontend/` | Command center and the warden PWA. |
| `prototype/` | An early React mock. Design reference only. |

## Getting set up

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
```

## Documentation

Start with `docs/EVAC120.md`. It carries the phase status and links everything
else.

| Document | Contents |
|---|---|
| `docs/EVAC120.md` | Assessment, phase results, deliverables checklist |
| `docs/EVAC120_ARCHITECTURE.md` | Components, data flow, and where each invariant is enforced |
| `docs/EVAC120_STATE_MACHINES.md` | The three machines, including the transitions that deliberately do not exist |
| `docs/EVAC120_PROVENANCE.md` | What was vendored, from where, and what changed |
| `docs/EVAC120_DEEPSTREAM.md` | Pipeline design, and a cheaper route around the pyds segfault |
| `docs/EVAC120_RESILIENCE.md` | What each failure costs, and the recovery point objective |
| `docs/EVAC120_SECURITY.md` | Permissions, biometric data, and what is *not* addressed |
| `docs/EVAC120_VALIDATION.md` | How a drill is judged when there is no oracle |
| `docs/EVAC120_DEPLOYMENT.md` | Running an edge node |
| `docs/EVAC120_WARDEN_PWA.md` | Setting up a warden's tablet, written for the person doing it |
| `docs/EVAC120_CALIBRATION.md` | Empty on purpose: nothing is calibrated yet |
| `docs/EVAC120_BENCHMARKS.md` | Empty on purpose: nothing is measured yet |
| `CLAUDE.md` | Conventions and the nine invariants |

## Running the tests

```bash
cd backend && ../.venv/bin/python -m pytest      # 712 backend tests
cd frontend && npm test                          # 45 frontend tests
```
