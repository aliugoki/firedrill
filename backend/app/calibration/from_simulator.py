"""Labelled observations from a simulated drill.

Lets the harness be exercised, and its arithmetic tested, before any GPU work
lands. The simulator knows the truth, so the labels are free and exact.

What this cannot do is produce a *certifiable* calibration. Simulated scores are
drawn from a model of a face matcher, not from one, so a threshold tuned here
would be tuned to the model. `certify` refuses a set that is entirely simulated
for exactly that reason. The value is that the day recorded footage exists, only
the labelling is new work.
"""

from __future__ import annotations

from app.calibration.dataset import CalibrationSet, LabelledObservation, Source
from app.core.events import EventType
from app.simulator.engine import DrillPlan, ObservedStream, observe


def harvest(plan: DrillPlan, stream: ObservedStream | None = None) -> CalibrationSet:
    """Turn a drill's face observations into a labelled set.

    Ground truth comes from the stream's owner record, the mapping the core is
    never allowed to read. Using it here is correct: labelling is exactly the
    job a human does by hand on real footage. It is asked per observation
    rather than per track, because an ID switch means the face in front of the
    camera and the track id attached to it belong to two different people, and
    a human labelling that frame would write down the face.
    """
    stream = stream or observe(plan)
    by_ref = {a.person_ref: a for a in plan.agents}

    calibration_set = CalibrationSet(
        name=f"sim:{plan.site.site_id}:seed{plan.seed}",
        note="simulated; not certifiable")

    for event in stream.events:
        if event.type is not EventType.FACE_OBSERVED or not event.subject:
            continue
        person_ref = stream.who(event.subject, event.ts_ms)
        if person_ref is None:
            continue
        agent = by_ref.get(person_ref)
        if agent is None:
            continue

        payload = event.payload
        calibration_set.add(LabelledObservation(
            observation_id=event.event_id,
            # A visitor or an unenrolled employee has no true gallery identity.
            # These are the observations that measure the false-accept rate.
            true_identity=(agent.emp_id
                           if agent.has_gallery_entry and not agent.is_visitor
                           else None),
            proposed_identity=payload.get("candidate_id"),
            score=payload.get("score", -1.0),
            margin=payload.get("margin", -1.0),
            quality=payload.get("quality", 1.0),
            pose_deviation_deg=payload.get("pose_deviation_deg", 0.0),
            track_confidence=payload.get("track_confidence", 1.0),
            association_is_strong=payload.get("association_is_strong", True),
            camera_id=payload.get("camera_id"),
            source=Source.SIMULATOR,
        ))
    return calibration_set
