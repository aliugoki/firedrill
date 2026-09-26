# EVAC-120

Fire Drill & Evacuation Accountability.

**Target:** P95 building evacuation ≤ 120 s, measured on real drills. The number
is an operational goal to be measured against, not a guarantee the software
makes.

> EVAC-120 supplements, and never replaces, certified fire-detection and
> life-safety systems. It does not control alarms, doors or suppression. Its
> output is decision support for a human incident commander, and a floor
> warden's physical count is the final authority.

**New here?** Read [`docs/EVAC120_EXPLAINED.md`](docs/EVAC120_EXPLAINED.md).
It explains the whole system with no engineering background assumed, including
how it fits with FaceTrack and VisionTrack.

## What it does

Answers one question, continuously, during an evacuation: **who is still in the
building?** It answers it from evidence, and it says how confident it is.

Three separate state machines — identity, presence and accountability — feed an
append-only evidence ledger. Every decision is reconstructable: asking why a
given person was marked accounted returns the list of observations that led
there. Nothing resolves a conflict silently; disagreement escalates to a human.

## How it fits with FaceTrack and VisionTrack

Three separate systems. EVAC-120 reads from the other two and writes to
neither — the connection to VisionTrack's database is opened read-only by the
server itself, and a test asserts it.

| System | Supplies | EVAC-120 uses it for |
|---|---|---|
| **FaceTrack** | Roster: employee ids, names, enrolled faces, badged-in today | Who is expected. Department, home floor and assembly-zone assignment are *not* in FaceTrack and are owned here. |
| **VisionTrack** | Floor plans and zones (read once, before a drill); live observations on a Redis stream during it | Where people are, and which zone is a floor, an exit or an assembly point. |

A site that has synced its geometry once can run a drill with VisionTrack
switched off. If FaceTrack cannot be reached, a drill runs from an exported
roster file and the board marks the roster unverified, which is one of the four
things that hold back an all-clear.

## See it running

```bash
cd backend && ../.venv/bin/python scripts/serve_demo.py --port 8811
```

A 200-person simulated drill, five minutes in, one assembly point swept and one
still being walked.

- Command centre — <http://127.0.0.1:8811/?drill=demo>
- Warden tablet — <http://127.0.0.1:8811/evac/warden?drill=demo&zone=assembly-north&warden=warden-1>

Append `&lang=ar` for Arabic, which flips the layout right to left.

## Where things are

| Path | What |
|---|---|
| `backend/app/core/` | Pure-Python domain. No database, no Redis, no framework. |
| `backend/app/vendor/` | Code copied from VisionTrack and DeepStream. See `docs/EVAC120_PROVENANCE.md`. |
| `backend/app/simulator/` | Synthetic drills and failure injection. |
| `backend/app/ingest/` | Redis consumer, projections, edge-to-central replication. |
| `backend/app/api/` | HTTP and WebSocket surface. |
| `backend/app/infra/` | Config, auth, permissions, audit, retention. |
| `backend/app/calibration/` | Threshold sweep and the certification report. |
| `frontend/` | Command centre and the warden PWA. No framework, no build step. |
| `prototype/` | An early React mock. Design reference only. |

## Getting set up

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cp .env.example .env        # then fill in; the required values have no defaults
```

## Running the tests

```bash
cd backend && ../.venv/bin/python -m pytest      # 1465 tests, ~7 min
cd frontend && npm test                          # 155 tests, under a second
```

The backend run includes a browser gate that starts headless Chrome and drives
both screens against a live server. It **skips** where Chrome or Node is
absent, which is the normal state of an edge node.

## Status

The accountability logic, both screens, replication, recovery, the chaos suite
and the post-drill report are built and tested. The camera pipeline that would
feed real observations is designed and blocked on a crash in NVIDIA's pyds
bindings, so everything currently runs against a simulator. No threshold has
been calibrated and no live drill has run. `docs/EVAC120_BENCHMARKS.md` and
`docs/EVAC120_CALIBRATION.md` are empty on purpose and say why.

## Documentation

| Document | Contents |
|---|---|
| `docs/EVAC120_EXPLAINED.md` | **Start here if you are not a developer.** The whole system, and the FaceTrack/VisionTrack linkage |
| `docs/EVAC120.md` | Assessment, phase results, deliverables checklist |
| `docs/EVAC120_ARCHITECTURE.md` | Components, data flow, and where each invariant is enforced |
| `docs/EVAC120_STATE_MACHINES.md` | The three machines, including the transitions that deliberately do not exist |
| `docs/EVAC120_PROVENANCE.md` | What was vendored, from where, and what changed |
| `docs/EVAC120_DEEPSTREAM.md` | Pipeline design, and a cheaper route around the pyds segfault |
| `docs/EVAC120_RESILIENCE.md` | What each failure costs, and the recovery point objective |
| `docs/EVAC120_SECURITY.md` | Permissions, biometric data, and what is *not* addressed |
| `docs/EVAC120_VALIDATION.md` | How a drill is judged when there is no oracle |
| `docs/EVAC120_INTEGRATION.md` | **Standing a site up:** step by step, with FaceTrack and VisionTrack |
| `docs/EVAC120_DEPLOYMENT.md` | Running an edge node |
| `docs/EVAC120_WARDEN_PWA.md` | Setting up a warden's tablet, written for the person doing it |
| `docs/EVAC120_CALIBRATION.md` | Empty on purpose: nothing is calibrated yet |
| `docs/EVAC120_BENCHMARKS.md` | Empty on purpose: nothing is measured yet |
| `CLAUDE.md` | Conventions and the nine invariants |
