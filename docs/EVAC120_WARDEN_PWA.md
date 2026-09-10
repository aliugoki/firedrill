# Warden device setup

For the person handing out tablets before a drill, not for a developer.

---

## 1. What the app is for

You are the check on the cameras. The system will tell you who it thinks is at
your assembly point; your job is to look at the actual people and say whether it
is right. **When you disagree with the system, you are correct.** That is not
politeness, it is how the system is built: your confirmation outranks anything a
camera saw.

The app works with no signal. Everything you tap is saved on the tablet and sent
when the network comes back.

---

## 2. Installing it

One tablet, once. It stays installed.

1. Connect the tablet to the site network.
2. Open `https://<edge-node>/evac/warden` in Chrome (Android) or Safari (iOS).
3. **Android:** menu, then *Install app* or *Add to Home screen*.
   **iOS:** share button, then *Add to Home Screen*.
4. Open it from the home-screen icon, not from the browser. It runs full screen
   and keeps working offline.
5. **Open it once while on the network before the drill.** That is what fills
   the offline cache. A tablet that has never been opened online will show a
   blank screen at the muster point.

If *Install app* does not appear, the page is being served over plain HTTP.
Tablets refuse to install an app that cannot work offline. Talk to whoever set
up the edge node; it needs a certificate.

### 2.1 Assigning a warden and a zone

Open with the warden and zone in the address:

```
https://<edge-node>/evac/warden?warden=warden-7&zone=assembly-north
```

The tablet remembers both. It also generates its own device id and keeps it, so
a tablet's work stays attributable even if two people share it.

You can only see and confirm people at **your** zone. If you need another zone,
you need to be assigned to it.

---

## 3. Using it during a drill

Three screens along the top.

### My zone

The counts, then the thing that matters most: a big box for the number of people
you can actually count in front of you.

**Count heads, type the number, press submit.** The app compares it with the
system and tells you one of three things:

- **Your count matches** — nothing to do.
- **You counted more than the system knows about** — there are people here it
  has not recognised. Tag them on the Unknown people screen.
- **The system counted more than you did** — *this is the serious one.* The
  system believes people are safe who are not standing in front of you. Do not
  let anyone declare all clear. Count again, then work down the list of people
  it says are here and find the ones nobody can see.

Below that: **Finish sweep** when you have checked your whole zone, and
**Escalate** when something is wrong you cannot resolve yourself.

Finishing a sweep is always accepted, even when the system disagrees with you.
But it does not mean the zone is clear — the command centre will still show the
disagreement, which is the point.

### People

Your zone's roster, worst first. People nobody has laid eyes on are at the top;
confirmed people sink to the bottom, so you are not scrolling past forty ticks
to find the two names that matter.

Four buttons per person:

| Button | Means |
|---|---|
| **Confirm present** | I am looking at this person, right now, here |
| **Not here** | Not at my assembly point. They may be at the other one |
| **Wrong person** | The system has the wrong name on this person |
| **Not on site today** | On leave, off site, working from home |

*Not here* is not the same as missing. Say it when they are not with you and let
the command centre work out where they are.

Search by name or department at the top.

### Unknown people

People the cameras saw and could not identify — usually visitors and
contractors. Tag them so the numbers reconcile.

---

## 4. The status bar

Across the top of every screen, and worth glancing at.

| It says | It means |
|---|---|
| **All work synced** | Everything you have tapped has reached the system |
| **3 waiting to sync (45s)** | Three actions are still on this tablet. Normal; they will go |
| **Offline — your work is saved on this device** | No signal. Keep working; nothing is lost |
| **2 refused** | The system rejected two actions. Tell the command centre |

And one you should take seriously:

> **The system cannot see properly. Rely on your own count.**

This means the cameras are down or the pipeline has failed. The list in front of
you is a guess. Your headcount is the only real number, so take it carefully and
say so when you report.

---

## 5. After the drill

Nothing to do. The photos cached on the tablet are deleted when the drill ends,
whether or not the tablet has been back on the network. The tablet never holds
face data, only small thumbnails, and never anything that could be used to
recognise someone elsewhere.

Leave the app installed. It will be ready for the next one.

---

## 6. If something goes wrong

| Problem | What to do |
|---|---|
| Blank screen at the muster point | The tablet was never opened online. Use paper, and open it on the network before the next drill |
| *Install app* does not appear | The edge node has no certificate. It is a setup problem, not yours |
| Cannot see your zone | You are not assigned to it. Ask the command centre |
| Actions stuck waiting for a long time | Normal with no signal. They are saved. Tell the command centre your count over the radio |
| The tablet is lost | Tell the safety officer. It holds thumbnails, no face data, and they expire at drill end |

---

## 7. One thing to remember

The system supplements the fire alarm. It does not replace it, and it does not
replace you. If the tablet and your eyes disagree, **your eyes are the record**.
