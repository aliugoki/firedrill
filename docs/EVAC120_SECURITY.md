# EVAC-120 security and data protection

Two things make this system's security profile unusual, and both cut against the
normal instinct to lock everything down.

**It holds biometric data about people who did not choose to be in a drill.**
Face embeddings and crops are special category data in most regimes. A drill
generates thousands of them in ten minutes.

**It must work when everything else does not.** An authentication service that
fails closed during a fire is a system that stops answering exactly when someone
needs it. Some things here are deliberately reachable without a token, and each
one is a decision rather than an oversight.

---

## 1. Who can do what

Four permissions, role-shaped rather than CRUD-shaped, because an evacuation has
four kinds of actor.

| Permission | Grants |
|---|---|
| `evac:read` | Dashboards, drill history, reports |
| `evac:operate` | Create, start and stop a drill |
| `evac:warden` | Confirm, reject, sweep, headcount — scoped to assigned zones |
| `evac:admin` | Configure zones, thresholds, retention, warden assignments |

Plus `drill:export` (reports leave the building carrying personal data, so it is
separate from read), `audit:view`, and `system:admin`, which no tenant role holds.

### 1.1 Separation of duty is a safety control

The seeded roles are built so that no single person can both run a drill and
sign off its physical headcount.

| Role | Holds | Deliberately does not |
|---|---|---|
| Incident Commander | read, operate, export, audit | warden, admin |
| Floor Warden | read, warden | operate, admin, export |
| Safety Officer | read, admin, export, audit | operate, warden |
| Viewer | read | everything else |

This is not org-chart decoration. The whole accountability model rests on two
independent sources of evidence: what the cameras observed, and what a human
physically confirmed. A role holding both `evac:operate` and `evac:warden`
collapses those into one, and a drill signed off by one person with one opinion
is exactly the outcome the system exists to prevent. A test asserts no seeded
role holds both, and it will fail if someone adds one.

### 1.2 Zone scoping

A warden is scoped to the zones they were assigned. Confirming someone at a zone
you are not standing in is making the one claim the system trusts above its own
cameras, about a place you cannot see.

A warden with **no** assigned zones is unrestricted — a roving supervisor. That
is explicit rather than a default that erodes: an empty list means "everywhere",
so a misconfigured assignment fails open, and that is the wrong direction. Sites
should assign zones explicitly and treat an unassigned warden as a finding.

---

## 2. What is reachable without a token, and why

| Endpoint | Auth | Reasoning |
|---|---|---|
| `/healthz` | none | The chaos script and the monitoring system poll it from outside. A health endpoint that needs a token cannot tell you the token service is down. It exposes counts and degradation state, never a person. |
| `/static/*`, `/`, `/evac/warden` | none | The PWA shell must load and cache before anyone signs in, or a warden arriving at a muster point with no signal has a blank screen. The shell contains no data. |
| `/openapi.json` | none | Standard. It describes shapes, not contents. |

Everything under `/api/evac/` requires a caller identity and a permission.

---

## 3. Biometric data

The distinction the retention policy turns on:

> **Evidence is not biometrics.** "A face matched EMP-482 at 10:41 above
> threshold on camera 9" is evidence, and a post-incident report needs it for
> years. The 512-float embedding that produced it, and the aligned crop it came
> from, are needed for as long as the matching takes. Keeping them because they
> are in the same pipeline is how a drill system becomes a biometric database
> nobody signed up for.

| Class | Biometric | Purged |
|---|---|---|
| Face embedding | yes | immediately after matching |
| Face crop | yes | immediately after embedding |
| Warden device thumbnail | yes | at drill end, synced or not |
| Operator snapshot | yes | 7 days |
| Event | no | 1 year |
| Evidence ledger | no | 1 year |
| Drill report | no | 7 years |
| Audit log | no | 7 years |

Three properties of this table matter more than the durations, which are not
reviewed against a legal opinion and are marked as such in code.

**Nothing is unclassified.** Constructing a policy that omits a data class
raises. An item nobody decided about lives forever by default.

**The audit log outlives the evidence, which outlives the faces.** Each layer
can account for the one below it.

**A purge that fails is not recorded as a purge.** The remover must actually
delete the bytes and raise if it cannot; an item whose removal failed stays
marked as held. Recording a deletion that did not happen would make the policy a
lie that passes its own verification.

`RetentionLedger.verify` reports anything that outlived its class, and treats
overdue biometric material as a finding rather than a warning.

### 3.1 On the warden's device

Thumbnails only, never embeddings. A tablet at a muster point can be put down,
borrowed, or lost, and a device carrying face vectors is a breach waiting for
someone to leave it on a wall. Thumbnails are purged at drill end whether or not
the device has synced, because a device that never comes back online must not
keep its cache indefinitely.

---

## 4. The audit log

Distinct from the evidence ledger, and they answer different questions. "Why was
EMP-482 marked accounted?" is the ledger. "Who declared all clear at 10:44, and
what did the board say at that moment?" is the audit log.

Every warden action and every manual override is recorded with the actor, the
device, the timestamp, and — for overrides — the state before and after.
Recording an override without the state it overrode is refused outright, because
nobody reviewing it afterwards could tell a correction from a mistake.

Report exports are logged as disclosures, because that is what they are:
personal data about people's movements leaving the building.

---

## 5. Threats this design takes seriously

**A forged confirmation.** A warden confirmation is the highest-trust evidence
in the system and can mark someone safe on its own. The event schema refuses one
from any source that is not a warden device, so a compromised camera cannot
produce one. Every confirmation carries a warden id, a device id, and a
device-assigned sequence number.

**A confident wrong identity.** Handled in the domain rather than here: two
identities with real support produce `CONFLICT` rather than a winner, and a
conflicted person cannot be accounted for. See `docs/EVAC120_STATE_MACHINES.md`.

**A blind system reporting all clear.** The board refuses an all-clear while any
outage is open, whatever its counts say.

**Data exfiltration through the report.** Exports are a separate permission and
are logged as disclosures. The warden role, which holds the most personal data
on a device, deliberately cannot export.

## 6. Threats this design does not yet address

Stated plainly rather than left to be discovered.

- **JWT verification is not implemented.** The API reads caller identity from
  headers a gateway is expected to set. The seam is visible on purpose; a
  hand-rolled verifier would be worse than an obvious gap. Until a gateway is in
  front of it, the API must not be exposed beyond the edge node's own network.
- **No transport security is configured.** TLS terminates at whatever fronts the
  service. On an edge node serving a warden PWA, that matters: a service worker
  will not install over plain HTTP except on localhost.
- **No rate limiting.** A device syncing in a loop can flood the ingest path.
- **Central replication authenticates the site, not the node.** A per-site
  shared secret proves the sender knows a token, not that it is the node it
  claims to be, so a stolen token lets someone inject events for that site. On a
  private link between an edge node and its own central this is proportionate;
  over the open internet it is not. The comparison is constant-time, a node
  authenticated for one site cannot write another's history, and central never
  becomes authoritative — but none of that turns a shared secret into an
  identity.
- **The retention durations are not reviewed.** They are marked `calibrated=False`
  and are a starting point for a data-protection review, not its conclusion.
