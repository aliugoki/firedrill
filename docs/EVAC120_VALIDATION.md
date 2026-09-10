# EVAC-120 validation

**Status: no live drill has run.** Everything here describes how a drill will be
judged, not how one went. The criteria and the report generator are built and
tested against simulated drills; the three live drills Phase 5 requires need a
building, cameras, a roster and wardens.

---

## 1. The problem this has to solve

A real drill has no oracle.

In simulation the agents know where they went, so "false accounted" is a set
difference against ground truth. In a building on a Tuesday afternoon nobody has
that list. There is no register of where every person actually walked, which is
precisely why the warden's physical roll-call exists in the first place.

So validation is defined **against the manual roll-call**, not against truth.
That choice propagates through everything below, and it has one consequence
worth stating plainly: when the system and a warden disagree, the warden is
right by definition. Not as a courtesy — as the reference.

---

## 2. The two disagreements, which are not the same

| Measure | Meaning | Severity |
|---|---|---|
| **False accounted** | The system said safe; no warden confirmed it, or a warden said otherwise | Safety failure. Must be zero |
| **False unaccounted** | A warden confirmed someone the system could not account for | Quality failure |

A false unaccounted wastes a warden's time and erodes trust in the board. A
false accounted can leave someone in a building.

They are never merged into an "accuracy" figure, because averaging a safety
failure with an inconvenience produces a number that hides the only one that
matters. Every false accounted is listed individually in the report, with the
system's own reasoning beside it, because each one is a person somebody has to
go and find.

### Why the reference has to be a human, demonstrated

There is one failure the cameras cannot see at all, and it is the worst one.
Suppose the matcher is confident that A's face belongs to B -- not a thin
margin, not a flicker, but the same confident answer on every frame, because
A's gallery embedding genuinely sits closer to B's than to A's own. A walks to
the muster point. Nothing else ever observes B.

The identity machine has no second claim to conflict with, so no `CONFLICT`.
The presence machine watched one person arrive and settle, so `ASSEMBLY_PRESENT`.
Both halves of the `ACCOUNTED` test are satisfied and the board reports **B is
safe**. It also reports **A unaccounted**, so a search team is sent for someone
standing at the muster point while a person who never came to work that day is
recorded as evacuated.

No threshold refuses this, because nothing about the match is weak. No state
machine catches it, because there is no contradiction in the evidence. More
cameras make it worse, not better: every one of them agrees.

What catches it is the roll-call. A warden at the assembly point confirms who is
actually in front of them, B is not among them, and the report lists B as a
false accounted and fails `NO_FALSE_ACCOUNTED`. That is the entire argument for
measuring against a human rather than against the system's own confidence, and
`backend/tests/simulator/test_misidentification.py` asserts both halves of it:
that the cameras are fooled, and that the process is not.

When the substituted person *is* observed -- their own track claiming their own
name, overlapping in time -- the system does catch it, and refuses both. That
is `resolve_identities`, and it is the only defence available before a human
arrives.

---

## 3. Three outcomes, and the third is the important one

| Outcome | Means |
|---|---|
| **PASS** | The system agreed with the humans, and there was enough evidence for that agreement to mean something |
| **FAIL** | The system disagreed with the humans in a way that matters |
| **INCONCLUSIVE** | The drill did not produce enough evidence to judge |

**INCONCLUSIVE is not a soft pass.** A drill where the cameras were blind for
half the time, or where no warden completed a sweep, cannot validate the system
even if every number on the board is perfect — there was nothing to check the
board against.

This distinction exists because the alternative is a system that accumulates a
record of successful drills that prove nothing. A run where the wardens did not
turn up and the cameras saw everything will produce beautiful numbers, and it is
worth exactly as much as a self-marked exam.

### 3.1 Decision order

1. **A safety failure is a FAIL**, whatever else happened. One false accounted
   ends the assessment.
2. **Missing evidence is INCONCLUSIVE**, even when every other number looks
   good.
3. Anything else failing is a **FAIL**.
4. Otherwise, **PASS**.

---

## 4. The criteria

| Criterion | Passes when | Kind |
|---|---|---|
| `NO_FALSE_ACCOUNTED` | Zero | Safety |
| `SWEEPS_COMPLETED` | Every zone swept | Evidence |
| `HEADCOUNTS_TAKEN` | Every zone recorded a physical count | Evidence |
| `COVERAGE_SUFFICIENT` | ≥90% tracked end to end | Evidence |
| `SYSTEM_MOSTLY_SIGHTED` | Blind for ≤10% of the drill | Evidence |
| `HEADCOUNTS_AGREE` | No zone disagreed | Quality |
| `P95_MEASURABLE` | ≥20 samples and a reliable percentile | Quality |
| `P95_WITHIN_TARGET` | P95 ≤ 120 s | Quality |

Two are worth explaining.

**`HEADCOUNTS_TAKEN` is evidentiary, not quality.** Walking a zone and ticking
people off the system's own list is checking the system against itself. The
physical count is the independent measurement, and without it the drill has no
reference.

**`COVERAGE_SUFFICIENT` guards against the easiest way to fake a P95.** A
percentile computed only over people who were tracked end to end improves when
the slow, hard-to-track people fall out of the sample. Low coverage with a good
P95 is the shape of a number that got better by losing people rather than by
moving them.

All thresholds are marked `calibrated=False` and will stay that way until a live
drill exists to set them against.

---

## 5. What each drill reports

Per the brief, plus what the brief does not ask for: a statement of what this
particular drill could not establish.

- P50, P90, P95, P99 and the slowest individual, with the sample size, the
  coverage, and the caveat that belongs beside them
- Accountability completion time, which is separate from and usually much longer
  than the evacuation time. The building empties in two minutes; establishing
  that nobody is left takes as long as the last uncertain person takes to
  resolve. This is the number that decides when a commander can stand down
- False accounted, in full, each with the system's reasoning
- False unaccounted
- Unknown people
- Warden confirmations, identity rejections, and manual overrides from the audit
  log
- Sweeps completed and headcounts taken, per zone
- Headcount disagreements, quoted
- Escalations, with the warden's own words
- Outages, blind fraction, and the longest blind period
- The verdict, and every criterion behind it

---

## 6. A worked example

From a 200-agent simulated drill under the realistic injection profile,
driven through the real drill object rather than a test harness. Regenerate
it with `backend/scripts/worked_example.py`, which is the script that
produced exactly this:

```
EVAC-120 drill report — Worked example
  drill drill-worked-example at site-sialkot-office
  duration 600.0s

INCONCLUSIVE — this drill cannot judge the system: only 82% of people were tracked from alarm to arrival; 18% produced no timing at all, and a percentile that improves by losing the slow people is the easiest way to fake this number

Accountability against the manual roll-call
  expected                212
  accounted for           185
  unaccounted for         3
  uncertain               5
  needing verification    19
  unknown people          0

  FALSE ACCOUNTED         0   (system said safe, no warden confirmed)
  false unaccounted       19   (warden confirmed, system could not)

Evacuation times
  P50 68.9s   P90 116.8s   P95 143.0s   P99 317.4s
  slowest individual      347.9s
  measured on             173 people (82% coverage)
  caveat: 39 of 212 people produced no timing (18% excluded). The percentiles describe only those who were tracked end to end.
  accountability settled  —
  slowest floor           floor-4 at P95 215.8s

Wardens
  confirmations           204
  identity rejections     0
  manual overrides        0
  sweeps completed        2/2
  zones with a headcount  2/2

System health
  outages                 1
  blind for               20% of the drill
  longest blind period    120.0s
  The system was blind for 20% of this drill across 1 outage(s), the longest 120 s. Treat gaps in a person's history as unobserved rather than as absence.

NOTE: no threshold in this system has been validated against a calibration set.
      These numbers describe what happened. They do not yet describe how well
      the system works, because the settings that produced them are provisional.

INCONCLUSIVE — this drill cannot judge the system: only 82% of people were tracked from alarm to arrival; 18% produced no timing at all, and a percentile that improves by losing the slow people is the easiest way to fake this number

  PASS  NO_FALSE_ACCOUNTED  [0]
        nobody was marked accounted whom a warden did not confirm
  PASS  SWEEPS_COMPLETED  [2/2]
        every zone was swept
  PASS  HEADCOUNTS_TAKEN  [2/2]
        every zone recorded a physical headcount
  PASS  HEADCOUNTS_AGREE  [0]
        every physical count agreed with the system
  FAIL  COVERAGE_SUFFICIENT  [82%]
        only 82% of people were tracked from alarm to arrival; 18% produced no timing at all, and a percentile that improves by losing the slow people is the easiest way to fake this number
  FAIL  SYSTEM_MOSTLY_SIGHTED  [20%]
        the system was blind for 20% of the drill; this is a test of the wardens, not of the system
  PASS  P95_MEASURABLE  [173 samples]
        the sample supports a 95th percentile
  FAIL  P95_WITHIN_TARGET  [143.0s]
        P95 143.0s against a 120s target

Thresholds are not validated: Phase 5 default, no live drill has run
```

Worth reading carefully. Nobody was falsely accounted, every zone was swept and
counted, and the counts agreed — the system did well by the measures that matter
most. And the drill still cannot validate it, because a camera was down for two
minutes and 18% of people produced no timing at all.

That is the intended behaviour. A drill that says INCONCLUSIVE is telling you to
fix the coverage and run it again, not to celebrate the zero.

---

## 7. Getting to P95 ≤ 120 s

The simulated drill above sits at 143.0 s, and the fourth floor is the slowest
at 215.8 s. On simulated data that number means little, but the *shape* of the
problem is the one a real building will have, and the actions divide into two
kinds.

**Engineering** — makes the measurement better:
- Close the coverage gap. 18% of people produced no timing, and the exit with no
  camera is the obvious cause. An uncovered fire exit means people leave
  unobserved and their evacuation is never measured
- Fix the P2.3b segfault so face and body share one tracker. Without one, a
  face is attached to a body by geometry, and the identity gate refuses a
  geometric association outright rather than discounting it. The simulator now
  injects that failure, and the shape of it is a cliff rather than a slope: at
  half of all faces misassociated the accounted count moves by two, and at all
  of them the drill accounts for nobody through the cameras and falls back
  entirely on the wardens. It still clears nobody falsely at any rate
- Calibrate the thresholds. Every number in the system is provisional

**Operational** — makes the evacuation faster:
- The fourth floor is the slowest, at P95 215.8 s against a building P95 of
  143.0 s. It is the longest stair descent in the model, and a floor that is
  slowest because of distance is an operational problem, not a software one
- Reaction delay is the largest single component of an individual's time. Drill
  frequency and alarm audibility move it more than any software change

The distinction matters because they are different budgets and different people.
A P95 that improves because the cameras got better at seeing fast people is not
a building that got safer.
