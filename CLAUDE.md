# EVAC-120 (firedrill) — Claude Code Project Context

## What this is
Fire Drill & Evacuation Accountability platform. A **standalone repository**,
not a module of VisionTrack. Operational target: **P95 building evacuation
≤ 120 s, measured** — never hard-coded, never asserted as a guarantee.

This system supplements, and never replaces, certified fire and life-safety
systems. Say so in the UI and in the docs.

## Architecture
- **Standalone.** Own Postgres, own schema, own API, own frontend.
- Consumes VisionTrack's existing Redis streams (`vt:tracks`,
  `vt:track_lifecycle`, `vt:evac:events:<tenant>`) and FaceTrack's roster over
  HTTP. Both are **external producers**; neither is a shared database.
- Accountability runs on the **site edge node**. Central is a replica for admin
  and reporting. The core path must work with **zero Internet**.
- Reusable code from VisionTrack and DeepStream is **vendored** into
  `backend/app/vendor/`, never imported across repos. See
  `docs/EVAC120_PROVENANCE.md`.

## Layout
```
backend/app/
  core/       pure-Python accountability domain — no DB, Redis, HTTP or framework
  vendor/     copied code, provenance headers, import-rewrites only
  simulator/  synthetic drills and failure injection
  ingest/     Redis consumer, projections, edge->central replication
  api/        HTTP + WebSocket surface, /api/evac/*
  infra/      config, database, auth, permissions
frontend/     command center (desktop) + warden PWA (/evac/warden)
prototype/    fire_drill_system.jsx — design reference only, not production code
docs/
```

## The nine invariants
Every module in `core/`, and every test, is written to these.

1. Absence of evidence is not evidence of absence. An unknown face is
   `FACE_UNAVAILABLE`, an offline camera is `CAMERA_DEGRADED`, a weak match is
   `CANDIDATE`. None of them is `MISSING`.
2. A strong identity is never overwritten by a weak or unknown observation while
   the track is still reliable.
3. Conflicting evidence is never silently resolved. It raises
   `IDENTITY_CONFLICT`, then `MANUAL_VERIFICATION_REQUIRED`.
4. Identity, presence and accountability are three separate state machines.
   There is no `is_evacuated` boolean anywhere.
5. Re-identification similarity is not employee identity.
6. Every accountability decision is reconstructable from stored events.
7. Thresholds are configuration, validated against the Phase 2 calibration set.
   No production number is invented in code.
8. Any infrastructure failure degrades to `DEGRADED` or
   `MANUAL_VERIFICATION_REQUIRED`, never to a false `ALL CLEAR`.
9. Warden confirmation is first-class evidence and the final authority.

`ACCOUNTED` requires assembly-zone presence AND (confirmed identity OR warden
confirmation). Nothing else may set it.

## Backend conventions
- `backend/app/core/` imports nothing but the standard library and numpy. If a
  module there needs a connection, it belongs in `ingest/` or `api/`.
- Alembic only for schema. Never manual DDL.
- Tenant-scope every query.
- Tests are pytest. Run from `backend/`: `.venv/bin/python -m pytest`.
- No new dependency without flagging it. This runs on an edge node.

## Frontend conventions

**No framework and no build step.** This is a deliberate reversal of the
earlier plan to use React and Vite, and the reasoning is worth keeping.

The warden PWA runs on a tablet at an assembly point, on an edge node with no
Internet, and it must start from cache when the network is gone. A bundle the
service worker has to cache is a bundle that must be rebuilt, versioned and
invalidated correctly or the app silently stops working offline. Both surfaces
are read-mostly with a handful of interactions, so a framework buys less here
than it usually does, and a life-safety-adjacent tool with several hundred
transitive dependencies is a liability rather than a convenience. What ships is
what a site engineer can open and read.

The cost is real and worth stating: no component model, no type checking, and
manual DOM updates. If either surface grows past a few screens, revisit this.

- Plain ES modules, served from the backend at the same origin. Same origin is
  not optional: a service worker can only control pages on its own origin.
- Pure logic lives in `render.js`, `queue.js` and `i18n.js` and is tested with
  `node --test`. DOM glue lives in `command.js` and `warden.js` and is not.
- i18n en/ar, RTL-aware. Every UI string in both tables; a missing one renders
  the key rather than falling back to English, so it is obvious in testing.
- Warden PWA is offline-first: service worker plus IndexedDB, roster cached at
  drill start, confirmations queued locally with device-assigned sequence
  numbers, synced on reconnect.
- Command centre colour states: GREEN accounted, YELLOW uncertain or manual,
  ORANGE currently unobserved, RED unaccounted. **No raw AI metrics on the
  operator screen.**

## Working with the source repos
Read-only. Do not edit VisionTrack, DeepStream or FaceTrack from here.
- FaceTrack: `~/deploy/attendance-system` — roster (`user_data`) and gallery
- DeepStream: `~/deploy/deepstream` — face engine
- VisionTrack: `~/visiontrack/visiontrack` — tracking platform

## DON'T
- Don't edit a vendored file to fix an upstream bug. Fix it upstream, re-vendor,
  record it in `docs/EVAC120_PROVENANCE.md`.
- Don't invent a threshold. Every number comes from the Phase 2 calibration set.
- Don't add an `is_evacuated` boolean, or any single field that collapses the
  three state machines.
- Don't let a failure path produce `ALL CLEAR`.

## Working style
Phase-gated. Stop at each gate, run the gate's tests, report measured results,
wait for sign-off. Report format: what was built, tests run and results,
measured numbers (never estimated), open risks and the decision needed.
