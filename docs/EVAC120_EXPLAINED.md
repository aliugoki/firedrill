# EVAC-120, explained

For anyone who needs to understand what this system does and how it fits with
FaceTrack and VisionTrack. No engineering background assumed. Every other
document in `docs/` is written for a developer; this one is not.

> **Read this first.** EVAC-120 supplements, and never replaces, certified
> fire-detection and life-safety systems. It does not sound alarms, unlock
> doors, or trigger suppression. It helps a human decide, and a floor warden's
> own headcount always wins.

---

## 1. The problem, in one paragraph

A fire alarm sounds. Two hundred people leave a building. Somebody standing in
the car park has to answer one question: **is everyone out?** Today that is
answered with a clipboard and a shout of names, it takes fifteen or twenty
minutes, and nobody records how long the building actually took to empty. If
somebody is missing, the delay before anyone knows is the whole problem.

EVAC-120 answers that question continuously, from the moment the alarm sounds,
and it measures how long the evacuation took. The target is that 95 out of
every 100 people are out within 120 seconds — hence the name.

---

## 2. What it is not

Getting this wrong is the most expensive mistake available, so it is worth
stating plainly.

- **It is not a fire alarm.** It does not detect fire and it does not sound
  anything. It starts working when a drill is started.
- **It is not a decision-maker.** It never declares a building safe by itself.
  It says what it has seen and how confident it is, and a person decides.
- **It is not a surveillance system.** The cameras belong to a system that is
  already there. EVAC-120 reads what they report during a drill and throws the
  biometric material away when the drill ends.
- **It is not a substitute for a warden.** If the software and the warden
  disagree, the warden is right by definition. That is built into the code, not
  a policy written on top of it.

---

## 3. The three systems, and who owns what

This is the part most people need explained, because the names sound similar
and the boundaries matter.

Think of a building that already has two systems running, and EVAC-120 as a
third that arrives for twenty minutes a quarter and then goes quiet.

| System | What it is | What it owns | What it knows nothing about |
|---|---|---|---|
| **FaceTrack** | The attendance system | Who works here, their name, their enrolled face photo, whether they badged in today | Where anybody is right now |
| **VisionTrack** | The CCTV and analytics platform | The cameras, the floor plans, where the zones are, who is where | Whether a drill is happening |
| **EVAC-120** | This system | Whether each expected person is safe, and how long it took | Nothing about faces or cameras that the other two do not tell it |

The one-line version: **FaceTrack says who should be here. VisionTrack says
where people are. EVAC-120 works out who is still inside.**

### 3.1 Why three systems and not one

Because each already exists and each is somebody else's product. EVAC-120 is a
separate application with its own database, its own screens and its own code.
It reads from the other two and writes to neither. If EVAC-120 has a bug, the
attendance system keeps working and the CCTV keeps recording.

That rule is enforced rather than promised: the connection EVAC-120 opens to
VisionTrack's database is opened in read-only mode by the database server
itself, so a mistake in this code cannot corrupt a running CCTV deployment. A
test asserts it.

### 3.2 What comes from FaceTrack

**The roster** — the list of people the building expects today.

For each person, FaceTrack supplies their employee number, their name, whether
they have a face photo enrolled, and whether they badged in this morning.

Three things FaceTrack does **not** know, which EVAC-120 has to hold itself:

- which department they are in,
- which floor they normally work on,
- **which assembly point they are assigned to**, which is the important one,
  because that decides whose list they appear on when a warden starts counting.

A person with no assembly point is on nobody's list. The board says so in those
words rather than pretending they belong to a zone nobody has swept.

One subtlety worth understanding. FaceTrack's "badged in today" is a turnstile
reading, and a turnstile says *whether*, not *why*. Annual leave, working from
home, a forgotten badge, and walking in behind a colleague all look identical
to it. So EVAC-120 records "did not check in" and never upgrades that to "on
leave" — and it counts those people separately so nobody quietly disappears
from the denominator.

If FaceTrack cannot be reached when a drill starts, EVAC-120 can run from an
exported file instead. It then marks the roster as not verified, which is one
of the four things that hold back an all-clear, and it says how many hours old
the file is.

### 3.3 What comes from VisionTrack

**Two things, and they arrive by different routes.**

**The floor plans and zones**, read from VisionTrack's database before a drill.
This is how EVAC-120 knows that "floor-2-open" is a floor, "exit-main" is an
exit, and "assembly-north" is the car park. A site that has synced once can run
a drill with VisionTrack switched off entirely, because the geometry is kept
locally. If it is stale, the screen says how stale.

**The observations**, which arrive live during the drill on a message queue
(Redis). Each message says: at this moment, this tracked person was seen in this
zone by this camera, or this face matched this employee with this confidence.

EVAC-120 does not run the cameras and does not do the face matching. It consumes
the result. The component producing those messages is the DeepStream pipeline,
which is the part of the work currently blocked — see §8.

### 3.4 The one identifier that ties it together

VisionTrack assigns every person it is tracking a **global person id** — a
label like `gp-4471` that follows somebody from camera to camera. It is not a
name. It is "the person the cameras are following".

Face matching then proposes: *this global person id is probably employee
EMP-0042*. "Probably" is the whole difficulty, and §5 is about it.

---

## 4. What happens during a drill, step by step

1. **Somebody presses Start.** EVAC-120 takes a snapshot of the roster at that
   instant and freezes it. The drill is judged against that snapshot, so a
   person who "disappeared" because HR updated a record cannot be confused with
   a person who disappeared in a stairwell.

2. **Cameras start reporting.** Observations flow in: person seen on floor 3,
   person seen at the main exit, face matched to an employee.

3. **The board fills in.** Every expected person gets a row and a colour.
   Green means accounted for, yellow means uncertain, orange means the system
   cannot currently see them, red means unaccounted for and past the deadline.

4. **Wardens walk the assembly points.** Each has a tablet showing only their
   own zone's list. They tap Confirm present, Not here, Wrong person, or Not on
   site today, and they enter a physical headcount — an actual count of heads in
   front of them.

5. **The two halves have to agree.** The system's count and the warden's count
   are compared. If they disagree, the board says so and does not resolve it.

6. **The commander decides.** The board will show ALL CLEAR only when every
   expected person is accounted for, every zone has been swept and counted, the
   counts agree, no outage is open, and the roster was verified. Any one of
   those missing, and the board lists what is outstanding instead.

7. **A report is produced.** How long the evacuation took, who disagreed with
   whom, what the system could not see, and whether the drill produced enough
   evidence to judge the system at all.

---

## 5. The idea the whole design rests on

**Not seeing somebody is not the same as them not being there.**

This sounds obvious and is violated constantly by systems like this. A camera
goes offline; the people it was watching stop generating observations; a naive
system concludes they have left. That is a false all-clear, and a false
all-clear in an evacuation is the failure that kills somebody.

So EVAC-120 separates three questions that look like one:

- **Identity** — who is this? (unknown, candidate, confirmed, disputed…)
- **Presence** — where are they? (in the building, in transit, at the assembly
  point, briefly unobserved, lost…)
- **Accountability** — are they safe? (not evacuated, evacuating, accounted,
  uncertain, unaccounted, needs a human to check)

They are kept separate deliberately. There is no single "is this person out"
field anywhere in the code, because a single field forces the system to guess
when the honest answer is "I do not know".

**There are exactly two ways a person can be marked safe.** Either a camera saw
them at an assembly point *and* their identity is confirmed, or a warden
confirmed them in person at an assembly point. Nothing else sets it — not
elapsed time, not being seen near an exit, not the absence of bad news.

And when the system cannot see, it says so. Every outage is recorded as an
interval with a cause, so afterwards you can ask "was the system blind at
10:42:15, and which cameras were dark when this person was last seen?" — which
is what a warden actually wants to know about somebody unaccounted for.

---

## 6. When things disagree

The system is arranged so that disagreement is visible rather than resolved.

**Two names for one person.** If face matching proposes two different employees
for the same tracked person with real evidence behind each, EVAC-120 refuses to
pick. It marks the person as needing a human and shows both names. The
underlying vendor code resolves this by majority vote; that behaviour is
specifically not reused, and the reason is written down.

**The warden disagrees with the system.** The warden wins. If a warden says the
person in front of them is not who the system thinks, that is recorded as
evidence and the system's claim is dropped.

**The headcount disagrees.** A warden counting 38 heads where the system
believes 40 are safe is the dangerous direction, and it is never absorbed
silently. Even if the count is later corrected and agrees, the report still
records that there was a moment when it did not.

---

## 7. Privacy, in plain terms

A drill generates thousands of face images and face measurements. Those are
biometric data about people who did not choose to take part in a drill.

The design draws one line: **the evidence is not the biometrics.** "A face
matched EMP-0042 at 10:41 with a score above the threshold on camera 9" is the
evidence, and a post-incident report needs it for years. The face image it came
from is needed for as long as the matching takes, and no longer.

So the claim is kept and the material is deleted. Face images and measurements
go immediately. Thumbnails cached on a warden's tablet — the shortest-lived
thing in the system, because they leave the building in somebody's hands — are
deleted when the drill ends, not on a timer afterwards.

Two honest caveats. Nothing produces biometric material yet, because the camera
pipeline is blocked, so none of this has been exercised on real data. And the
deletion step refuses to record a deletion nobody performed: until something is
wired up to actually remove the bytes, the system reports the material as
*unremovable* rather than marking it deleted. That is deliberate. A policy that
passes its own audit while keeping the data is worse than no policy.

---

## 8. What is built and what is not

| Part | State |
|---|---|
| The accountability logic, the three state machines, the evidence ledger | Built and tested |
| The command centre and the warden tablet app | Built and tested, including in a real browser |
| Edge-to-central replication, recovery after a restart, the chaos suite | Built and tested |
| The post-drill report and how a drill is judged | Built and tested |
| The camera pipeline that produces the observations | **Designed, not built** |
| Calibration of the confidence thresholds | **Not done** — every number is provisional |
| Real drills in a real building | **Not done** — none has run |

The blockage is a crash in NVIDIA's Python bindings for DeepStream, which
occurs about five seconds after attaching the component that reads results out
of the video pipeline. Until that is resolved, the system runs against a
simulator that generates realistic drills, including realistic failures.

**What this means for anyone evaluating it.** Every number the system currently
produces comes from simulated people. The logic is tested; the accuracy is not
measured. The documents say so in the places where a number would otherwise be
quoted, and the calibration tool refuses to certify thresholds derived from
simulated data.

---

## 9. Seeing it for yourself

A populated drill, five minutes in, with one assembly point swept and one still
being walked:

```
cd backend && ../.venv/bin/python scripts/serve_demo.py --port 8811
```

Then open:

- **Command centre** — <http://127.0.0.1:8811/?drill=demo>
- **Warden tablet** —
  <http://127.0.0.1:8811/evac/warden?drill=demo&zone=assembly-north&warden=warden-1>

Add `&lang=ar` to either for Arabic, which flips the layout right to left.

Things worth trying: the `?` button on any person opens the evidence behind
their state; the People tab on the warden screen is where confirmations happen;
the Unknown people tab is where a contractor nobody had on a list gets tagged.

---

## 10. Where to read next

| If you want | Read |
|---|---|
| The phase-by-phase status and what each gate produced | `EVAC120.md` |
| How the pieces fit together technically | `EVAC120_ARCHITECTURE.md` |
| The three state machines in full, including transitions that deliberately do not exist | `EVAC120_STATE_MACHINES.md` |
| What happens when each thing fails | `EVAC120_RESILIENCE.md` |
| Who can do what, and what the design does not yet address | `EVAC120_SECURITY.md` |
| How a drill is judged when there is no correct answer to check against | `EVAC120_VALIDATION.md` |
| Setting up a warden's tablet, written for the person doing it | `EVAC120_WARDEN_PWA.md` |
| Running an edge node | `EVAC120_DEPLOYMENT.md` |
| Why the camera work is blocked and what the way around might be | `EVAC120_DEEPSTREAM.md` |
