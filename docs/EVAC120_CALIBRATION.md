# EVAC-120 calibration

**No threshold in this system has been calibrated.** This document exists so
that the claim is written down somewhere findable, rather than inferred from an
absent file.

---

## 1. Status

Every configuration object in EVAC-120 carries `calibrated=False` and a `source`
string saying where its provisional values came from. Nothing reads True, and
nothing will until a labelled set from real footage exists.

| Config | Provisional values from |
|---|---|
| `IdentityConfig` | `score_threshold` and `min_margin` are DeepStream's shipped `config_pipeline.example.toml`; the rest are conservative guesses |
| `PresenceConfig` | Phase 1 placeholders |
| `AccountabilityConfig` | Phase 1 placeholder |
| `HeadcountPolicy` | Phase 4 placeholder |
| `RetentionPolicy` | Phase 4 default, not reviewed against a legal opinion |
| `Thresholds` (validation) | Phase 5 default, no live drill has run |

---

## 2. The harness is built

`app/calibration/`, 35 tests. Labelled observations in, validated thresholds
out, or a refusal explaining why not. It runs today against simulator output and
**refuses to certify it**, because a set that is entirely simulated measures a
model of a matcher rather than a matcher.

Three properties worth knowing before it is used in anger.

**The split is by person, not by observation.** The same face appears in dozens
of frames. Splitting by frame puts near-duplicates on both sides, and the
held-out half then agrees with the tuning half for reasons that have nothing to
do with the threshold.

**Observations of unenrolled people are mandatory.** Without them the
false-accept rate cannot be measured at all, and that is the error that marks a
stranger safe under a colleague's name. A set lacking them is refused outright
rather than warned about.

**An impossible ceiling raises rather than relaxes.** If no threshold pair holds
the false-accept rate under the ceiling, that is a finding about the pipeline.
Loosening it afterwards would be choosing the number after seeing the answer.

Certification fails closed on any of: too few observations, too few enrolled
people, no unenrolled observations, a person leaking across both halves, a
held-out false-accept rate above the ceiling, drift beyond two points, or a set
that is entirely simulated.

---

## 3. What has to happen

1. **Unblock the pipeline.** No real footage can be labelled until face and body
   share one tracker. See `EVAC120_DEEPSTREAM.md` §2 for a route that may be
   cheaper than the four already planned.
2. **Record two walk-through drills** at the site the thresholds will run on.
   Different lenses, angles and lighting than the enrolment photos.
3. **Label them.** Every face detection paired with who it actually was, and —
   critically — including people who are not in the gallery at all.
4. **Run the harness.** `split`, `Sweep.run()`, `certify()`.
5. **Replace every provisional config** from the certified output, and record
   the numbers and the method here.

Until step 5, no drill report may present its numbers as validated, and the
report says so on every render.

---

## 4. What will be recorded here

Left as a template so the shape is agreed before anyone is under pressure to
produce a favourable one.

- The corpus: how many observations, how many enrolled people, how many
  unenrolled, how many cameras, which site, which dates
- The split: how many people held out
- The sweep: the trade-off curve, and the ceiling chosen **before** seeing it
- The operating point: score threshold, margin, and why
- Measured on the tuning half: true-accept rate, false-accept rate, unknown
  accept rate
- Measured on held-out people: the same three, plus the drift between them
- The certification verdict, and every refusal if it did not certify
