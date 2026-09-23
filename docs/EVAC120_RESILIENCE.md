# EVAC-120 failure and recovery

The system's job during a failure is to be **honestly less useful**, never
quietly wrong. Every design decision here follows from that, and from invariant
8: any infrastructure failure degrades to `DEGRADED` or
`MANUAL_VERIFICATION_REQUIRED`, never to a false `ALL CLEAR`.

---

## 1. Where authority lives

The **edge node is the authority** during a drill. It holds the accountability
core in memory, decides every person's state, and never waits on anything
outside the building to do so. Central is a replica for administration and
reporting, and it is explicitly forbidden from being used for accountability
when its copy is incomplete.

This is the zero-Internet requirement expressed as an ownership rule rather than
as a feature. A site whose uplink is severed mid-drill loses reporting and loses
nothing else.

---

## 2. What each failure costs

The distinction that matters is whether a failure **blinds** the system. A
blinded system cannot infer anything from silence; a merely degraded one can
still see, and only loses durability or reach.

| Failure | Blinds? | Immediate effect | What the operator sees |
|---|---|---|---|
| One camera | Yes, for its coverage | Grace clock suspends for people it was watching | `CAMERA_DEGRADED`, that camera named |
| Every camera | Yes, totally | No new observations at all | Blind fraction climbs; all-clear blocked |
| DeepStream pipeline | Yes | Event production stops | `SYSTEM_DEGRADED`, pipeline named |
| Redis (event bus) | Yes | Events stop arriving; gaps on resume | `SEQUENCE_GAP` in each person's evidence |
| Postgres (projections) | **No** | Read models stop persisting | `SYSTEM_DEGRADED`, but counts keep updating |
| Link to central | **No** | Replication buffers locally | Backlog depth rises; drill unaffected |
| FaceTrack (roster) | **No** | Roster snapshot cannot be refreshed | Roster marked unverified; all-clear blocked |
| The node's own clock is corrected | Yes | Ageing stops meaning anything; see below | `SYSTEM_DEGRADED`, the correction and its size named |

Two rows in that table are the ones people get wrong.

**A Postgres outage is not blinding.** The core holds its state in memory and
the projections are recomputable from the ledger. Treating a database failure as
a sight failure would make the system blind itself over something it does not
need to see through. The simulator originally made exactly this mistake, and a
chaos test caught it.

**A severed link to central is not an incident.** It is the designed operating
mode of a site with no Internet.

---

## 3. Health is stored as intervals, not as a flag

A boolean answers "is the system degraded now?". That is the least useful
version of the question, because by the time anyone reads a report the outage is
over and the flag reads healthy.

`HealthLog` stores closed intervals with a cause, which makes three questions
answerable:

- **Was the system degraded at 10:42:15?** The moment a particular call was made.
- **How much of the drill was degraded?** Overlapping outages are unioned, not
  summed, so three cameras down at once is one blind period rather than three.
- **Which cameras were dark when this person was last seen?** The question a
  warden actually asks at the assembly point, and the one that turns "we don't
  know where they are" into "the camera on their floor was down for two minutes".

Every drill report carries a caveat generated from this. Above 50% blind, the
caveat says plainly that the manual roll-call is the authority and the system's
counts describe the minority of the drill it was watching.

---

## 4. Ingest guarantees

One fold, `app.ingest.Ingestor`, used by both the edge service and the
simulator. A property proved against the simulator is therefore a property of
the production path rather than of a lookalike.

**Idempotent.** `(source, seq)` is an event's identity. Redis Streams redeliver
on consumer restart, so folding the same events twice must reach the same state,
and a chaos test asserts exactly that against a doubled stream.

**Gap-detecting, never gap-filling.** A hole produces a `SEQUENCE_GAP` in the
affected person's evidence and nothing else. Interpolating across it would
manufacture observations, which is the precise inverse of invariant 1. A gap
that a late event fills stops being reported; one that is never filled stays
visible for the life of the drill.

**Order-tolerant.** Batches are sorted by timestamp before folding, because a
batch drained from a stream contains several cameras whose clocks interleave,
and applying them in arrival order would let a later sighting be overwritten by
an earlier one.

---

## 5. Replication and the recovery point objective

Edge to central is one-directional, asynchronous, and never on the critical
path. Durability comes from a SQLite WAL buffer with `synchronous=FULL`, which
fsyncs on commit. That is slower, and it is the entire reason the buffer is
worth having.

### 5.1 The numbers

| Scenario | Events lost | Why |
|---|---|---|
| Edge process crashes | **0** | Everything committed is on disk and replayed on restart |
| Edge loses power | **0** | `synchronous=FULL` fsyncs before returning |
| Link to central drops | **0** | Buffered locally; delivered on reconnect |
| Central refuses one record | **0**, but nothing behind it moves | The queue is oldest-first, so a record central will never accept blocks everything behind it. Reported as a stall, never dropped |
| Central is rebuilt | **0** | Edge replays its whole buffer |
| **Edge disk fails** | Everything central has not acknowledged | The only unbounded loss, and the argument for a short flush interval |
| **Producer crashes mid-flight** | Up to one flush interval | Events produced but not yet committed |

The last two are the real RPO, and they are reported by `measure_rpo` from live
counters rather than asserted here. That matters: the fsync claim is only true
while the pragma is set, so a test asserts the pragma directly. Change it, and
the test fails rather than the guarantee silently becoming false.

"We buffer events" invites the belief that nothing can be lost. Something can.
It is a bounded, measured amount, and the bound is worth knowing before anyone
relies on it.

### 5.2 A rejected credential is not an outage

The two look identical from the backlog depth alone, and they need opposite
responses. An unreachable central comes back on its own, so the replicator
retries with backoff. A wrong token never will, so the replicator **stops**,
records why, and reports it separately in `/healthz`.

Retrying a credential failure forever is the worse default: it fills the disk,
never succeeds, and reports itself the whole time as a transient outage that
somebody is presumably already handling. Nothing is lost while blocked: the
events stay buffered.

**Clearing it means restarting the edge process**, and this used to say "a
human clears it once the token is fixed" as though there were a button. There
is not, and there should not be one that does less than this: the token is read
from the environment at startup and held by the transport, so clearing the flag
without replacing the credential retries something central has already refused.
`Replicator.unblock` exists for a caller that holds the transport and can swap
the credential; nothing calls it at runtime. `/healthz` carries the instruction
beside the refusal, because an operator reading a message from central needs to
be told what to do with it and that nothing is lost meanwhile.

The blocked replicate job also **fails** rather than returning zero, so the
supervisor reports it unhealthy and names the reason. It used to succeed every
fifteen seconds: `flush` returns zero when blocked, and zero is also what a
quiet drill returns, so a node that had sent central nothing since it booted
reported its background work in good order.

### 5.3 Convergence

Central deduplicates on arrival and reports gaps rather than filling them. Its
reconciliation report states plainly when the copy is incomplete, and says that
the edge node's copy is authoritative and central must not be used for
accountability until it is not.

A chaos test asserts convergence in the strong sense: after a partition and a
flood, central does not merely hold the same number of events, it reaches the
same accountability conclusions as the edge.

---

## 6. Recovery behaviour

| Component | Recovery | Compensation |
|---|---|---|
| Camera | On `CAMERA_RECOVERED` | Time spent degraded is credited back to each affected person's grace window |
| Pipeline | On `SYSTEM_RECOVERED` | Ingest resumes; the gap in sequence numbers is reported |
| Redis | Consumer reconnects and replays from its last acknowledged id | Duplicates dropped; gaps reported |
| A stalled outbox | Only by a person: nothing resolves it on its own | The record stays queued. `attempts` on the head of the queue is what separates this from an outage, and the health endpoint now reports it |
| Postgres | Projections rebuilt from the ledger | Nothing lost; the ledger is the source of truth |
| Clock | Backwards: when wall clock climbs past where it was. Forwards: on the next tick | The outage stays in the record, so the report can say the minutes either side of it were not trustworthy |
| Central link | Replicator drains the backlog | Reconciler deduplicates the flood |

The camera credit is worth spelling out. Without it, a ten-minute camera failure
would age every person it covered into `LOST` the instant it came back, turning
one outage into a wave of false alarms at exactly the moment the operator
regained sight.

---


### An outage and a poison record look identical

The outbox is drained oldest-first, so whatever is at the head of the queue is
what every flush retries. If central refuses that one record — a field it does
not know, a schema it has moved past — the batch fails, and it fails again in
fifteen seconds, and again, forever. The backlog grows, `failed_flushes`
climbs, `last_failure_reason` carries whatever central said.

Every one of those numbers looks the same as a link that is simply down. The
two need opposite responses: an outage resolves itself the moment the link
returns, and this one never resolves at all.

`attempts` is what tells them apart, and it had been incremented on every
failed flush and carried through a schema migration without anything ever
reading it. Measured: two hundred flushes against a poisoned batch left five
rows each recording two hundred attempts in SQLite, `is_blocked` false, and no
field anywhere on the health endpoint saying so.

The head of the queue's attempt count is now reported, and twenty failures
against the same record — a little over five minutes at the fifteen-second
flush interval — marks the node degraded with an instruction saying this is not
an outage waiting to clear.

**Nothing is dropped on the strength of it.** This outbox deletes only what
central confirmed, and a record central is refusing is still evidence somebody
may need. Saying so loudly is the remedy; discarding it is not — the same
reason `unblock` is not automatic.

### The clock is a dependency

It is the one this node cannot get from anywhere else, and the one most likely
to be wrong: accountability runs on a site edge node with **zero Internet**, so
its clock is whatever the RTC said at boot until a network appears and NTP
corrects it — which may well be during a drill.

Two clocks, therefore. Scheduling runs on `time.monotonic`, which counts
forward regardless of what anybody does to the wall clock. The `now_ms` each
job is handed is wall clock, because it has to be comparable with the
timestamps VisionTrack puts on its observations and with the evidence already
in the ledger.

Scheduling on wall clock, which is what this did, meant a backward correction
of forty minutes stopped every job for forty minutes: `now_ms >= last_run_ms +
interval` is false for as long as the clock is catching up. Including the tick,
which the design says must never stop. And silently — a job that never runs
never fails, so the supervisor reported itself healthy throughout, and
`staleness_ms` wrapped its answer in `max(0, …)` so it reported zero.

The correction itself is detected by comparing the two clocks: time passing
shows up in both, a correction in only one. It is recorded as a `CLOCK` outage,
which is **blinding** — a correction does not stop a camera seeing, but it
stops ageing, and ageing is how the system decides nobody has seen somebody for
ninety seconds. The two directions cost differently:

| Direction | What it does | How long it blinds |
|---|---|---|
| Backwards | Every recorded `last_seen_ms` is now in the future, so everybody looks freshly seen and nobody becomes `LOST`. This is the false optimism invariant 8 exists to forbid. | Exactly the size of the step |
| Forwards | Every age inflates at once; the board over-reports people as unobserved, which is the safe direction to be wrong in | Until the next tick |

## 7. The chaos suite

Two layers, because they prove different things.

**`backend/tests/chaos/`** kills each component in a simulated drill and asserts
the software's response: nobody falsely cleared, the board reports degraded
while it is degraded, and state converges on replay. These run in the normal
test suite, in seconds, with no infrastructure.

**`scripts/evac_chaos.sh`** kills real containers against a live stack. Its
assertions are coarser by necessity: that the service stayed up, kept answering,
and reported itself degraded. What it proves that the pytest layer cannot is
that the failures are the ones that actually happen.

A service that stays up and reports healthy while blind is the failure both
layers exist to catch.

### Scenarios

| Scenario | pytest class | Container |
|---|---|---|
| One camera dies mid-drill | `TestCameraDiesMidDrill` | `mediamtx` |
| Every camera dies | `TestEveryCameraDies` | — |
| DeepStream container dies | `TestThePipelineDies` | `vt-ai-worker-ds` |
| Redis dies | `TestTheEventBusDies` | `vt-redis` |
| Postgres dies | `TestTheDatabaseDies` | `firedrill-postgres` |
| Link to central severed | `TestTheLinkToCentralDies` | network disconnect |
| Partition then flood | `TestNetworkPartitionThenReplay` | — |
| All of it at once | `TestEverythingAtOnce` | — |

---

## 8. What is not yet built

The edge service's process wrapper: the Redis consumer loop, the HTTP health
endpoint that `evac_chaos.sh` polls, and the Postgres projection writer. The
logic they wrap is built and tested; what is missing is the deployment shell
around it, which lands with the API in Phase 4.

Until then `scripts/evac_chaos.sh` skips every scenario on a host where the
containers do not exist, which is the honest behaviour: it reports skipped, not
passed.
