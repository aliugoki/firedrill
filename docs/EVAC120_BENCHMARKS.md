# EVAC-120 benchmarks

**Nothing here has been measured.** The GPU pipeline does not run: the
DeepStream image is not built on the development host, and the P2.3b segfault
blocks the probe the whole design depends on.

This document exists so the absence is explicit and so the measurement plan is
agreed before anyone runs it.

---

## 1. Why there are no numbers

The pipeline needs a buffer probe downstream of the tracker. The documented
crash is a SIGSEGV about five seconds after such a probe is registered
downstream of an SGIE producing tensor metadata, identically under nvinfer and
under Triton.

`EVAC120_DEEPSTREAM.md` §2 argues there may be a cheaper route around it than
the four already planned, based on the sibling repository's face pipeline
running in production with buffer probes and no SGIE at all. That is an
inference from evidence, not a reproduction, and testing it costs one config
change and one restart.

**No estimate appears in this file.** A plausible-looking FPS figure nobody
measured is worse than an empty table, because it will be quoted.

---

## 2. What to measure

| Measurement | Decides |
|---|---|
| FPS per stream at 1, 4, 8, 16 streams | Cameras per edge node |
| Probe residency per frame | Whether in-probe inference fits the frame budget |
| End-to-end latency, frame PTS to Redis | Whether the live board is live |
| GPU memory at each stream count | Whether 8 GB holds the models plus decode |
| ID-switch rate per person-minute | Sets `id_switch_rate` in the simulator honestly |
| Face TPR and FPR at chosen thresholds | Feeds `EVAC120_CALIBRATION.md` |
| Duplicate-count rate at a double-covered exit | Whether one person becomes two |
| Reconnect time after a camera drop | Sets `t_lost_ms` honestly |

The last four feed straight back into configuration. Until they exist, the
simulator's failure rates are guesses, and a property proved under guessed rates
is proved under guessed rates.

---

## 3. Hardware the numbers will be tied to

Any figure recorded here is meaningless without this, and a benchmark quoted
without it will be applied to a different box.

| | |
|---|---|
| GPU | RTX 3070, 8 GB, driver 575.57.08 |
| DeepStream | 7.1 |
| Streams | to be recorded |
| Resolution and codec | to be recorded |
| Model set | PeopleNet PGIE, NvDCF tracker, OSNet, ArcFace |

The development host has the GPU above. A production edge node may not, and the
stream count it supports scales with it.

---

## 4. Method

1. One camera first. Confirm the pipeline survives sixty seconds with the probe
   attached, which is the current failure point.
2. Add streams one at a time until FPS per stream drops below real time. Record
   the count, not the first number that looks good.
3. Run each configuration for at least ten minutes. A thirty-second benchmark
   measures cache warmth.
4. Record GPU memory at the peak, not the average.
5. Take the ID-switch and duplicate-count rates from a recorded walk-through
   with known ground truth, not from a live feed where nobody knows the answer.
