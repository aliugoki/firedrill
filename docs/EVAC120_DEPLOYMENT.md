# EVAC-120 deployment

Two node types. The distinction between them is an ownership rule, not a
configuration flag.

**The edge node is the authority during a drill.** It holds the accountability
core in memory, decides every person's state, and never waits on anything
outside the building. One per site.

**Central is a replica** for administration and reporting. It is explicitly
forbidden from being used for accountability when its copy is incomplete, and
its reconciliation report says so when that is the case.

A site whose uplink is severed mid-drill loses reporting and loses nothing else.

---

## 1. What runs where

| Component | Edge | Central | Notes |
|---|---|---|---|
| EVAC-120 API + front ends | yes | yes | Same image; behaviour differs by `EVAC_NODE_ROLE` |
| Postgres | yes | yes | Projections. Losing it does not blind the system |
| Redis | yes | no | The event bus the pipeline publishes to |
| DeepStream pipeline | yes | no | One container per site. See `EVAC120_DEEPSTREAM.md` |
| SQLite outbox | yes | no | Store-and-forward to central. A file, not a service |

The edge node needs a GPU for the pipeline. Central does not.

---

## 2. Before the first drill

### 2.1 The roster is not optional

A drill cannot be created without a roster source, and this is enforced rather
than warned about. A roster-less drill has no denominator, and the board reads
"0 of 0 accounted" — a screen that looks like success.

Set one of:

```bash
EVAC_ROSTER_FILE=/var/lib/evac120/roster.json   # exported, works with no network
# or, from Phase 5, EVAC_FACETRACK_URL + EVAC_FACETRACK_API_KEY
```

The file format matches FaceTrack's `GET /api/employees` payload, plus the
fields FaceTrack does not have:

```json
{
  "employees": [
    {"emp_id": "EMP-0001", "first_name": "Ayesha", "last_name": "Khan",
     "photo": "/api/images/c/1.jpg", "present": true}
  ],
  "local_fields": {
    "EMP-0001": {"department": "Engineering", "home_floor_id": "floor-2",
                 "assigned_assembly_zone": "assembly-north"}
  }
}
```

**Department, home floor and assembly zone are not in FaceTrack.** Nothing will
populate them for you. A roster without them still runs a drill, but the warden
lists are hard to work through and nobody owns the people with no assigned zone.
`RosterSnapshot.coverage_gaps()` counts them; check it before the first drill,
not during one.

### 2.2 Zones must be tagged

Every zone needs a kind: `FLOOR`, `EXIT`, `ASSEMBLY` or `BLIND`. `BLIND` matters
as much as the others — a stairwell with no camera earns a longer grace window,
and leaving it untagged means people get marked as stale tracks for walking
through an architectural coverage hole.

```bash
EVAC_ASSEMBLY_ZONES=assembly-north,assembly-south
```

### 2.3 Migrations

```bash
cd backend
EVAC_DB_PASSWORD=... ../.venv/bin/alembic upgrade head
```

The URL is assembled from the environment, not read from `alembic.ini`, and a
missing `EVAC_DB_PASSWORD` refuses to run rather than falling back to a default.
A DSN in a checked-in config file eventually points at the wrong database, and
"which one did that migration just run against" is a question nobody wants to
ask.

There is one migration and one table that matters. `evac_events` is the source
of truth: presence, identity, the ledger, the board and the report are all
derived from it, so a node that has its events can be rebuilt from nothing.

`downgrade` exists because Alembic expects it. Running it destroys the only
record of a drill.

### 2.4 Nothing is calibrated

Every threshold in the system reports `calibrated=False` until a labelled
calibration set exists. Identity thresholds, presence timings, the accountability
deadline, and the retention durations are all provisional. See
`EVAC120_DEEPSTREAM.md` §5 for the harness.

**Do not present a drill's numbers as validated while this is true.**

---

## 3. Configuration

Full list in `.env.example`. The ones that decide behaviour:

```bash
EVAC_NODE_ROLE=edge                  # edge | central
EVAC_SITE_ID=<uuid>                  # REQUIRED
EVAC_TENANT_ID=<uuid>                # REQUIRED

EVAC_DB_PASSWORD=                    # REQUIRED, no default
EVAC_REDIS_URL=redis://localhost:6379/0
EVAC_EVENT_STREAM_PREFIX=vt:evac:events

EVAC_OUTBOX_DIR=/var/lib/evac120/outbox
EVAC_OUTBOX_FLUSH_SEC=15             # also the producer's exposure window

EVAC_CENTRAL_URL=                    # unset on the central node itself
EVAC_CENTRAL_TOKEN=                  # REQUIRED when CENTRAL_URL is set.
                                     # Without it the edge reports a gap rather
                                     # than buffering forever against a central
                                     # that will refuse every batch.

EVAC_JWT_SECRET=                     # REQUIRED unless headers are trusted
EVAC_JWT_ALGORITHM=HS256             # HMAC only; RS256 is refused
EVAC_JWT_ISSUER=                     # optional, checked when set
EVAC_JWT_AUDIENCE=                   # optional, checked when set

# Only when a gateway in front of this service has already authenticated the
# caller. With this off and no secret set, every request is refused, which is
# the safe default. With it on, anyone who can reach the service can name their
# own permissions.
EVAC_TRUST_IDENTITY_HEADERS=false
```

`EVAC_OUTBOX_FLUSH_SEC` is not only a tuning knob. It is the window during which
an event exists but is not yet durable, and therefore the producer half of the
recovery point objective. Shortening it costs I/O and buys a smaller loss
window. See `EVAC120_RESILIENCE.md` §5.

---

## 4. Running it

Two processes, on purpose. A crash in one must not take the other with it, and
an operator who can still reach the board while ingest is restarting has more
than one who can reach nothing.

```bash
cd backend

# the API and both front ends
../.venv/bin/uvicorn app.api.main:app --host 0.0.0.0 --port 8100

# the background work: draining the stream, ageing the core, replicating,
# refreshing geometry
../.venv/bin/python -m app.service.main
```

On startup the edge process reloads any drill that was running when it stopped
and replays its events, so a node that restarts mid-evacuation comes back with
the same board rather than an empty one. This happens before the first request,
because the first request may be an operator looking for people.

The edge process starts even when it is misconfigured, and logs each missing
piece as a warning at startup rather than failing at the first drill. A process
that exits because Redis is unset gives an operator nothing to look at; one that
starts and says "there is no event stream" gives them the answer. `/healthz`
reports the same gaps, so a monitoring system sees them too.

Shutdown is graceful: on SIGTERM the supervisor stops taking new work, the
outbox is flushed one last time, and only then does the process exit. A drill
interrupted by a deploy should lose nothing.

### 4.0 Background jobs and their clocks

| Job | Every | Why that interval |
|---|---|---|
| `tick` | 1 s | A person becoming stale is a second-scale event. **Never backed off**, because slowing it under load is exactly when a stale track most needs ageing |
| `consume` | 250 ms | The board is meant to be live |
| `replicate` | 15 s | Also the producer half of the recovery point objective |
| `sync` | 5 min | A floor plan changing mid-drill is not a thing that happens |

A failing job backs off exponentially to a cap and never gives up: a dependency
coming back must not need a human to notice. One job failing never stops
another — a geometry sync that cannot reach VisionTrack must not stop the
consumer draining events during a drill.

The front ends are served by the same process at the same origin. That is
required rather than convenient: a service worker can only control pages on its
own origin, and serving the PWA from a different host means it cannot install,
which is the one thing it must do.

### 4.1 TLS

A service worker will not install over plain HTTP except on `localhost`. An
edge node serving a warden PWA to tablets over the site network **must** have
TLS, or the PWA silently loses its ability to start offline — failing in the
one way nobody notices until a real drill.

A self-signed certificate is acceptable on a closed site network provided it is
installed on the tablets. An untrusted certificate fails the same way.

---

## 5. Verifying a deployment

Run these before the first drill, not during one.

```bash
# 1. The service answers and reports itself healthy
curl -s localhost:8100/healthz | jq

# 2. The PWA can install: the worker is allowed a root scope
curl -sI localhost:8100/static/sw.js | grep -i service-worker-allowed

# 3. Everything the worker caches actually exists
#    (a 404 in the install list means it never installs at all)
cd backend && ../.venv/bin/python -m pytest tests/api -k ServingTheFrontEnds

# 4. The roster has the fields the warden screens need
#    coverage_gaps() should be zero across the board

# 5. Chaos, against the real containers
DRY_RUN=1 scripts/evac_chaos.sh     # see what it would kill
scripts/evac_chaos.sh                # then actually kill it
```

Step 5 is worth doing on a quiet afternoon rather than discovering the answers
during a drill. It kills each of the camera feed, the pipeline, Redis, Postgres,
the network and central, and checks the service stayed up and reported itself
degraded. A service that stays up and reports healthy while blind is the failure
it exists to catch.

---

## 6. Backups and recovery

| Loss | Recovery |
|---|---|
| Edge process **mid-drill** | Restart. Running drills are reloaded at startup and their state is replayed from `evac_events`, arriving at the same board |
| Edge process | Restart. State rebuilds from the event log |
| Edge power | Restart. The outbox fsyncs on commit; nothing committed is lost |
| Edge Postgres | Projections rebuild from the ledger |
| Edge disk | Everything central has not acknowledged is gone. This is the argument for a short flush interval |
| Central | Edge replays its whole buffer |

Back up the outbox directory and Postgres. The event stream is the source of
truth; everything else is derived from it.

---

## 7. What is not built yet

Named rather than left to be discovered:

- **JWT verification.** The API reads caller identity from headers a gateway
  is expected to set. Do not expose it beyond the edge node's network until
  that gateway exists.
- **JWT verification.** The API reads identity from headers a gateway sets. Do
  not expose it beyond the edge node's network until that gateway exists.
- **The FaceTrack HTTP client.** Use `EVAC_ROSTER_FILE` until it lands.
- **Alembic migrations.** No schema is persisted yet, so there is nothing to
  migrate. `evac_events` is the first table when the consumer lands.
