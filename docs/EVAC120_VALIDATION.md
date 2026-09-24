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
2. **A disagreement that happened is a FAIL**, ahead of the evidence gate. The
   wardens counted, the system counted, and the two differed; that is true
   whether or not the drill was large enough or complete enough to judge
   anything else. It used to sit behind rule 3, so a drill with a real
   headcount disagreement *and* an unswept zone reported INCONCLUSIVE, burying
   the second most important signal a drill produces behind "not enough
   evidence" — when the evidence in question was exactly what had been
   collected.
3. **Missing evidence is INCONCLUSIVE**, even when every other number looks
   good.
4. Anything else failing is a **FAIL**.
5. Otherwise, **PASS**.

A target that could not be measured is not a target that was missed, so
`P95_WITHIN_TARGET` stays behind the evidence gate rather than joining rule 2.

---

## 4. The criteria

| Criterion | Passes when | Kind |
|---|---|---|
| `NO_FALSE_ACCOUNTED` | Zero warden contradictions | Safety |
| `ACCOUNTED_VERIFIED` | Nobody accounted is still waiting on a roll-call that has not finished | Evidence |
| `SWEEPS_COMPLETED` | Every zone swept | Evidence |
| `HEADCOUNTS_TAKEN` | Every zone recorded a physical count | Evidence |
| `COVERAGE_SUFFICIENT` | ≥90% tracked end to end | Evidence |
| `SYSTEM_MOSTLY_SIGHTED` | Blind for ≤10% of the drill | Evidence |
| `HEADCOUNTS_AGREE` | No zone disagreed | Disagreement |
| `P95_MEASURABLE` | ≥20 samples, a reliable percentile, and a clock nobody corrected | Evidence |
| `P95_WITHIN_TARGET` | P95 ≤ 120 s | Quality |

`P95_MEASURABLE` is evidence, not quality. Too few measurements for a percentile
to mean anything is the definition of not having enough evidence. While it
counted as a quality failure, a ten-person office reported **FAIL** on a drill
where nobody was falsely accounted, every zone was swept and counted, the counts
agreed, coverage was total and the cameras never blinked — and no number of good
drills could ever have changed it, because the site is smaller than the sample a
95th percentile needs. Reporting that as a failure of the system says something
untrue about the system.

**A warden's silence is not a contradiction, and separating the two mattered
more than it sounds.** `ACCOUNTED` has two routes in by design — camera
presence with a confirmed identity, or a warden's word — and both used to land
in `NO_FALSE_ACCOUNTED` unless a warden had personally confirmed the person.
That is the safety-critical count, and rule 1 above says one of them ends the
assessment.

Measured on the demo drill — 175 accounted, one assembly point swept and one
still being walked:

| | Before | After |
|---|---|---|
| Safety failures | **80** | 0 |
| All of them "did not confirm" | yes | — |
| Warden contradictions | 0 | 0 |
| Accounted but unchecked | not distinguished | 80, as missing evidence |
| Verdict | **FAIL** | INCONCLUSIVE |

Three things were wrong with the old reading. It deleted half the
accountability state machine, by treating a legitimate route into `ACCOUNTED`
as a safety failure. It short-circuited the decision order, because rule 1
outranks rule 3 — so a drill with an unswept zone could never be INCONCLUSIVE,
which is precisely the verdict it deserves. And eighty false alarms is how the
one real contradiction stops being visible: the list of people a warden
actually disputed is always short and always matters, and it was buried.

What decides between them is whether the roll-call has **finished**. A sweep
still being walked has not reached everybody, so its silence is silence. A
sweep marked complete went through the entire list and never confirmed this
person, which is a contradiction — and it is exactly how a stable
misidentification is caught, since nothing in `core/` can see one and the
roll-call is the only thing that does.

So `NO_FALSE_ACCOUNTED` counts what a warden disputed *and* whoever a finished
roll-call passed over. `ACCOUNTED_VERIFIED` counts who nobody has looked at
yet, and is evidentiary.

Two more are worth explaining.

**`HEADCOUNTS_TAKEN` is evidentiary, not quality.** Walking a zone and ticking
people off the system's own list is checking the system against itself. The
physical count is the independent measurement, and without it the drill has no
reference.

**`COVERAGE_SUFFICIENT` guards against the easiest way to fake a P95.** A
percentile computed only over people who were tracked end to end improves when
the slow, hard-to-track people fall out of the sample. Low coverage with a good
P95 is the shape of a number that got better by losing people rather than by
moving them.

**A corrected clock withdraws the number rather than qualifying it.** An
evacuation time is the difference between two wall-clock readings, so if
somebody moved the clock between them it is not a duration and no sample size
repairs it. This is not hypothetical here: accountability runs on an edge node
with no Internet, whose clock is whatever the RTC said at boot until a network
appears and NTP steps it — see `EVAC120_RESILIENCE.md`.

Measured on a simulated hundred-person drill whose true P95 was 208 s, crossing
a forty-minute correction:

| | Reported P95 | Coverage | Excluded | Caveats |
|---|---|---|---|---|
| No correction | 208 s | 100% | 0 | none |
| Clock stepped **back** | **94 s** | 40% | 60 | coverage only |
| Clock stepped **forward** | **2608 s** | 100% | 0 | **none at all** |

The backward row is the dangerous one: a drill that missed its 120 s target by
88 seconds reported as comfortably inside it, because sixty people's arrivals
landed in front of the start and fell out of the sample — which is the gaming
vector `COVERAGE_SUFFICIENT` exists to catch, arriving by accident. Those sixty
were also excluded as `ARRIVED_BEFORE_START`, "already at the muster point when
the alarm went", which is a false statement about sixty real people in a
document that gets filed after an incident. They are `CLOCK_CORRECTED` now.

The forward row is quieter and no better: a fabricated number with full
coverage, nothing excluded and not one qualification on it.

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

### The one failure the cameras cannot see, swept rather than staged

A stable misidentification — one person's face confidently matched to another
employee's name, on a track nobody else is claiming — leaves no camera evidence
at all. The identity machine has nothing to conflict with, the presence machine
watches a real person walk to the muster point, and the board reports the
*other* employee safe. Nothing in `core/` can detect it; the manual roll-call
is the only thing that does.

That claim used to rest on one deliberately constructed pair. It is now swept:
injection mixes are turned up and ground truth is asked who actually arrived,
and the two directions are asserted separately.

| Mix | Camera-only false accepts | What is asserted |
|---|---|---|
| Wrong identity, alone or under track fragmentation | Yes | A finished roll-call catches every one; the drill reports FAIL |
| All ten knobs at once | Yes | Same, and the fold carries no blocker — nothing for an operator to notice |
| Look-alikes with ID switches | **None in 119 seeds** | Must stay none: a look-alike close enough to test the margin gate is a `CONFLICT`, not a coin-flip winner (invariant 3) |
| Misassociation with a lossy face pipeline | **None in 119 seeds** | Must stay none: `identity_fsm.gate` refuses a face pinned to the wrong body on the structure of the association, not on its score |

The second half is the more valuable one. Those two mixes are failure modes the
design claims to refuse outright, and if either ever starts putting a name on
the wrong body, the roll-call quietly becomes the only defence against
something the system was supposed to handle itself.

The "all ten knobs" case is not designed: it is the mix and seed that first
produced a false accept during a sweep of sixty random combinations, kept
because a case found is worth more than a case invented.

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

  Roster fields nobody had filled in:
    no department         12
    no gallery entry      13
    no home floor         12

  FALSE ACCOUNTED         0   (system said safe, a warden said not here)
  false unaccounted       19   (warden confirmed, system could not)
  accounted, unverified   0   (system said safe, no warden has looked)

  Identities the system could not settle:
    - track gp-emp:EMP-0078: claimed as EMP-0078 and EMP-0092, first at 3.0s
    - track gp-emp:EMP-0175: claimed as EMP-0091 and EMP-0175, first at 7.0s
    - track gp-emp:EMP-0152: claimed as EMP-0035 and EMP-0152, first at 32.7s
    - track gp-emp:EMP-0092: claimed as EMP-0078 and EMP-0092, first at 48.8s
    - track gp-emp:EMP-0112: claimed as EMP-0112 and EMP-0192, first at 69.3s
    - track gp-emp:EMP-0075#1: claimed as EMP-0017 and EMP-0075, first at 163.1s
    - track gp-emp:EMP-0000#2: claimed as EMP-0000 and EMP-0005, first at 211.2s
    - track gp-emp:EMP-0031#1: claimed as EMP-0031 and EMP-0184, first at 253.9s
    - track gp-emp:EMP-0156#3: claimed as EMP-0008 and EMP-0156, first at 478.6s
    Reported, not resolved. Invariant 3 forbids the software picking a winner;
    a human decides, and the record shows what they were deciding between.

Evacuation times
  P50 68.9s   P90 116.8s   P95 143.0s   P99 317.4s
  slowest individual      347.9s
  measured on             173 people (82% coverage)
  caveat: 39 of 212 people produced no timing (18% excluded). The percentiles describe only those who were tracked end to end.
    no timing, never observed           29
    no timing, never reached assembly   10
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
  events lost for good    0
  The system was blind for 20% of this drill across 1 outage(s), the longest 120 s. Treat gaps in a person's history as unobserved rather than as absence.

NOTE: no threshold in this system has been validated against a calibration set.
      These numbers describe what happened. They do not yet describe how well
      the system works, because the settings that produced them are provisional.

INCONCLUSIVE — this drill cannot judge the system: only 82% of people were tracked from alarm to arrival; 18% produced no timing at all, and a percentile that improves by losing the slow people is the easiest way to fake this number

  PASS  NO_FALSE_ACCOUNTED  [0]
        no warden contradicted anybody the system marked accounted
  PASS  ACCOUNTED_VERIFIED  [0 of 185]
        every accounted person was confirmed by a warden
  PASS  RECORD_COMPLETE  [0 dropped]
        every event this drill produced reached the database
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

The nine unsettled identities are the other half of that reading, and they are
not a failure. Sixteen distinct employees are named across them, against 19
people the board sent for manual verification: the lookalike injection put two
plausible names on one track, the system declined to choose, and a human was
asked instead. That is invariant 3 working. What would be a failure is the same
nine resolved silently by vote, which is what the vendored identity manager
does and the reason it was not reused.

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
