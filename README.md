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

## Running the tests

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cd backend && ../.venv/bin/python -m pytest
```

## Documentation

| Document | Contents |
|---|---|
| `docs/EVAC120.md` | The brief, the architecture assessment, phase status |
| `docs/EVAC120_PROVENANCE.md` | What was vendored, from where, and what changed |
| `CLAUDE.md` | Conventions and the nine invariants |
