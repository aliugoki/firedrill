# Integrating EVAC-120 with FaceTrack and VisionTrack

A step-by-step procedure for standing a site up, in the order the dependencies
actually bind. Every command here has been run against this repository; every
variable named is one the code reads, and where a variable exists but is not
wired yet this says so rather than letting you find out at the first drill.

For what the three systems are and who owns what, read
[`EVAC120_EXPLAINED.md`](EVAC120_EXPLAINED.md) §3 first. For running the node
once it is integrated, [`EVAC120_DEPLOYMENT.md`](EVAC120_DEPLOYMENT.md).

> **What you are integrating.** FaceTrack says who should be here. VisionTrack
> says where people are. EVAC-120 works out who is still inside. It reads from
> the other two and writes to neither.

---

## 0. Before you start

| | |
|---|---|
| Python | 3.10 |
| Postgres | one database EVAC-120 owns, **not** VisionTrack's |
| Redis | reachable from the edge node |
| FaceTrack | reachable **once**, to export a roster |
| VisionTrack | reachable **once**, to read floor plans and zones |

Neither FaceTrack nor VisionTrack needs to be reachable during a drill. That is
the point of the design, and §6 is how you prove it on your own site.

```bash
git clone https://github.com/aliugoki/firedrill && cd firedrill
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
cp .env.example .env          # then work through the steps below
```

---

## 1. Identity and database

```bash
# .env
EVAC_SITE_ID=<uuid>           # the building this node covers
EVAC_TENANT_ID=<uuid>         # must match the tenant VisionTrack publishes under
EVAC_DB_HOST=localhost
EVAC_DB_NAME=firedrill
EVAC_DB_USER=firedrill
EVAC_DB_PASSWORD=<secret>
```

`EVAC_TENANT_ID` is not cosmetic: the event stream key is built from it
(§3), so a mismatch here means a node that consumes an empty stream forever
and reports nothing wrong with the stream itself.

Create the schema:

```bash
cd backend
EVAC_DB_PASSWORD=<secret> ../.venv/bin/alembic upgrade head
```

Alembic only. Never hand-written DDL — the events table is the source of truth
and everything else is derived from it.

---

## 2. FaceTrack — who is expected

**The roster is the one thing a drill cannot be created without.** An empty
roster produces a board reading "0 of 0 accounted", which looks like success
and is the most dangerous screen this system can show, so a missing roster
refuses the drill instead.

### 2.1 Export the roster

```bash
curl -H "X-API-Key: <facetrack-key>" \
     "http://<facetrack-host>:5002/api/employees?company_id=<uuid>" \
     -o /var/lib/evac120/roster.json
```

### 2.2 Add what FaceTrack does not know

FaceTrack supplies `emp_id`, `first_name`, `last_name`, `photo` and `present`.
It does **not** know department, home floor, or — the important one —
**which assembly point each person is assigned to**. A person with no assembly
zone is on nobody's list, and the board says so in those words rather than
pretending they belong to a zone nobody has swept.

Merge those in under `local_fields`, keyed by `emp_id`:

```json
{
  "employees": [
    {"emp_id": "EMP-0001", "first_name": "Ayesha", "last_name": "Khan",
     "photo": "…", "present": true}
  ],
  "local_fields": {
    "EMP-0001": {"department": "Engineering",
                 "home_floor_id": "floor-2",
                 "assigned_assembly_zone": "assembly-north"}
  }
}
```

```bash
# .env
EVAC_ROSTER_FILE=/var/lib/evac120/roster.json
```

> **`present` is a turnstile reading, not a statement about the building.**
> Annual leave, working from home, a forgotten badge and tailgating all look
> identical to it. EVAC-120 records "did not check in" and never upgrades that
> to "on leave", and counts those people separately so nobody quietly leaves
> the denominator.

### 2.3 What is not built yet

`EVAC_FACETRACK_URL`, `EVAC_FACETRACK_API_KEY` and `EVAC_FACETRACK_COMPANY_ID`
are in `.env.example` and **no code reads them** — the HTTP client is a Phase 5
item. Setting them supplies no roster. The export above is the supported path
today, and it is also the fallback when FaceTrack is unreachable at drill
start, at which point the board marks the roster unverified and says how many
hours old the file is.

---

## 3. VisionTrack — where people are

Two things arrive from VisionTrack by two different routes.

### 3.1 Geometry: floor plans and zones, read once

Either straight from its Postgres, read-only:

```bash
# .env
EVAC_VISIONTRACK_DSN=postgresql://reader:<pw>@<vt-host>:5432/visiontrack
```

The connection is opened with `SET SESSION READ ONLY` by the server itself, so
a bug in EVAC-120 cannot corrupt a running CCTV deployment. A test asserts it.
Grant the account `SELECT` on `floor_plans` and `cameras` and nothing more.

Or from an export, which is the offline path and not a degraded one:

```bash
# .env
EVAC_GEOMETRY_FILE=/var/lib/evac120/geometry.json
```

**A site that has synced once can run a drill with VisionTrack switched off
entirely.** If the geometry is stale the screen says how stale.

### 3.2 Tag every zone

There is no safe default for a zone whose purpose nobody has stated, so
EVAC-120 refuses to guess:

```bash
# .env
EVAC_ZONE_KINDS=floor-1-open=FLOOR,exit-main=EXIT,assembly-north=ASSEMBLY,assembly-south=ASSEMBLY
EVAC_ASSEMBLY_ZONES=assembly-north,assembly-south
```

Both are needed and they are not the same thing. `EVAC_ZONE_KINDS` tells the
presence machine what a zone *is*; `EVAC_ASSEMBLY_ZONES` tells the API which
zones a warden can be standing at. Assembly-zone presence is half of what
`ACCOUNTED` requires, so a drill with these unset accounts for nobody through
the cameras.

### 3.3 Observations: the live stream

```bash
# .env
EVAC_REDIS_URL=redis://<host>:6379/0
EVAC_EVENT_STREAM_PREFIX=vt:evac:events
```

The key read is **`<prefix>:<tenant_id>`** — with the defaults above and tenant
`t-1`, that is `vt:evac:events:t-1`. The consumer group is `evac-edge` and the
consumer name defaults to `edge-<site_id>`; set `EVAC_CONSUMER_NAME` if two
processes read one stream.

Each message is one event:

```json
{"tenant_id": "…", "site_id": "…", "drill_id": "…", "source": "cam-9",
 "source_kind": "camera", "seq": 41, "type": "TRACK_UPDATED",
 "ts_ms": 1788000000000, "subject": "gp-4471",
 "payload": {"zone_id": "assembly-north", "zone_kind": "ASSEMBLY"},
 "event_id": "…"}
```

`seq` is per `source` and makes ingest idempotent and gaps detectable. `ts_ms`
is **always epoch milliseconds**: a camera's raw `pts_ms` starts at zero for
the first frame of a stream and must be converted before an Event exists.

> **`EVAC_TRACK_STREAM` and `EVAC_TRACK_LIFECYCLE_STREAM` are not read by any
> code.** VisionTrack publishes raw tracks on those, and the design has
> EVAC-120 consuming only the evacuation stream above, which the DeepStream
> pipeline populates. That pipeline is **designed and not built** — blocked on
> a crash in NVIDIA's pyds bindings, see
> [`EVAC120_DEEPSTREAM.md`](EVAC120_DEEPSTREAM.md). Until it exists, run
> against the simulator (§7).

---

## 4. Central replica, if you have one

```bash
# .env, on edge nodes only — unset on central itself
EVAC_CENTRAL_URL=https://central.example
EVAC_CENTRAL_TOKEN=<token>     # required whenever CENTRAL_URL is set
EVAC_OUTBOX_DIR=/var/lib/evac120/outbox
EVAC_OUTBOX_FLUSH_SEC=15
```

`EVAC_OUTBOX_FLUSH_SEC` is the window during which an event exists and is not
yet durable at central — the producer half of the recovery point objective.
Shortening it costs I/O and buys a smaller loss window; `/healthz` reports the
interval actually in use under `rpo.producer_exposure_ms`.

Replication is one-directional and never on the critical path. **Nothing an
edge node decides waits for central to acknowledge it.**

---

## 5. Authentication

```bash
# .env
EVAC_JWT_SECRET=<secret>       # HMAC only; RS256 and alg:none are refused
```

With no secret and no trusted gateway, every request is refused — which is the
safe default. `EVAC_TRUST_IDENTITY_HEADERS=true` is only for a gateway that has
already authenticated the caller: with it on, anyone who can reach the service
names their own permissions.

---

## 6. Verify the integration

Start both processes:

```bash
cd backend
../.venv/bin/uvicorn app.api.main:app --host 0.0.0.0 --port 8100   # API + both screens
../.venv/bin/python -m app.service.main                            # ingest, ageing, replication
```

**Both processes start even when misconfigured** and name each missing piece
rather than failing at the first drill. They report to different places, and
it is worth knowing which tells you what:

| Surface | Tells you |
|---|---|
| `GET :8100/healthz` | configuration gaps, auth gaps, whether a drill was recovered, audit durability |
| the edge process's **log** | the same configuration gaps, plus live ingest, geometry, replication and RPO counters |

The edge process has no HTTP surface of its own, by design — it is the thing
that must keep running when everything else is broken. Its live counters are
therefore read from its log, not curled.

```bash
curl -s localhost:8100/healthz | jq '{degraded, configuration_gaps}'
```

Work down the list. Each check fails loudly if the step above it was not
really done:

| # | Check | Passes when |
|---|---|---|
| 1 | `jq '.configuration_gaps'` | `null` — every gap names a consequence, not a variable |
| 2 | `jq '.degraded'` | `false` |
| 3 | Edge log at startup | no `configuration gap:` warnings, and it lists the zones it resolved |
| 4 | Create a drill: `POST /api/evac/drills` | `201`, not `503 no roster source is configured` |
| 5 | `GET /api/evac/drills/{drill_id}/board` | `expected` is non-zero, and `excluded_from_the_count` explains any shortfall |
| 6 | Edge log, `running jobs:` | includes `consume` — absent means no event stream |
| 7 | Open the command centre | `http://<host>:8100/?drill=<id>` |
| 8 | Open a warden tablet | `http://<host>:8100/evac/warden?drill=<id>&zone=assembly-north&warden=warden-1` |

> **`expected: 0` is the failure this system is most careful about.** A board
> reading "0 of 0 accounted" looks exactly like a building that has safely
> emptied. If step 5 gives you nought, stop and fix the roster before going
> further.

**Expect `expected` to be lower than your headcount, and check why before you
assume it is broken.** Anyone whose FaceTrack row says `present: false` is
counted as `NOT_CHECKED_IN` and kept out of the denominator — but reported
beside it, never dropped:

```json
{"expected": 1, "excluded_from_the_count": {"NOT_CHECKED_IN": 1}}
```

A person who forgot to badge in is still in the building, so that number is
one to look at rather than one to ignore. It is `roster_gaps` on the drill
summary and `excluded_from_the_count` on the board.

**If step 1 reports `no such table: drills`,** the Alembic migration in §1 has
not been run against the database this node is pointed at. The message names
the table and the statement, and a drill that was running when the process
stopped has not been reloaded — the board is empty for that reason rather than
because the building is.

Then prove what the architecture claims:

```bash
# 9. Pull the network between this node and everything else.
#    The drill must keep working.
curl -s localhost:8100/api/evac/drills/{drill_id}/board -H "…" | jq '.expected, .accounted'
```

Accountability runs on the edge node. If step 9 fails, the site is not
integrated — it is merely configured.

---

## 7. Before real cameras exist

The camera pipeline is blocked (§3.3), so run against the simulator to
exercise everything downstream of the observations:

```bash
cd backend && ../.venv/bin/python scripts/serve_demo.py --port 8811
```

A 200-person drill five minutes in, one assembly point swept and one still
being walked. Append `&lang=ar` to either screen for Arabic, which flips the
layout right to left.

---

## 8. Order of operations, in one list

1. Clone, venv, `cp .env.example .env`.
2. Set `EVAC_SITE_ID`, `EVAC_TENANT_ID`, the `EVAC_DB_*` block. Run Alembic.
3. Export the FaceTrack roster, merge `local_fields`, set `EVAC_ROSTER_FILE`.
4. Point at VisionTrack geometry (`EVAC_VISIONTRACK_DSN` or
   `EVAC_GEOMETRY_FILE`) and tag zones with `EVAC_ZONE_KINDS` **and**
   `EVAC_ASSEMBLY_ZONES`.
5. Set `EVAC_REDIS_URL`; confirm the stream key is `<prefix>:<tenant_id>`.
6. Set `EVAC_JWT_SECRET`.
7. Optionally set `EVAC_CENTRAL_URL` + `EVAC_CENTRAL_TOKEN`.
8. Start both processes; work down the table in §6 until `gaps` is empty.
9. Pull the network and confirm the drill continues.

---

## 9. What this does not give you

Integrating cleanly does not mean the system is calibrated or validated.

- **No camera pipeline.** Blocked on the pyds segfault; everything runs against
  the simulator until it is unblocked, and that work belongs to VisionTrack.
- **No calibrated thresholds.** Every number is provisional and the harness
  refuses to certify thresholds derived from simulated data.
- **No live drill has run.** `EVAC120_BENCHMARKS.md` and
  `EVAC120_CALIBRATION.md` are empty on purpose and say why.

EVAC-120 supplements, and never replaces, certified fire-detection and
life-safety systems. A floor warden's physical count is the final authority.
