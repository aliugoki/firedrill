"""Fusing face and body evidence onto one person.

The system this replaces ran face recognition and body tracking on **separate
trackers** and correlated them by bounding-box overlap. That correlation is weak
evidence, and treating it as strong is how a face gets attached to the wrong
body: two people crossing at an exit, one turning to a camera, and the name
lands on whichever box happened to overlap.

The fix has two halves. Phase 2 puts face and body on one tracker so they share
a `track_id`. This module is the other half: it makes the **strength of the
association explicit** and carries it into the identity gate, so that when a
shared track is not available the resulting identity evidence is visibly weaker
rather than silently equal.

Four association strengths:

    SHARED_TRACK   one tracker, one track_id. The only strong link.
    SPATIAL_IOU    separate trackers, boxes overlap. Weak.
    TEMPORAL_ONLY  same camera, same moment, nothing spatial. Very weak.
    NONE           no link at all. Produces no identity evidence.

Association strength reaches the identity gate two ways, on purpose. It scales
the observation's `track_confidence`, so weak links degrade smoothly, and it
sets `association_is_strong`, which the gate tests outright.

The second one exists because the first is not safe on its own. A multiplier
only rejects a weak association if the product happens to fall below a threshold
configured somewhere else, and during Phase 1 those two numbers landed exactly
equal: a weight of 0.5 against a minimum track confidence of 0.5 let every
geometry-correlated face through the gate. A rule this important cannot depend
on a numeric coincidence between two independently-tuned values, so it is also
enforced structurally.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from app.core.identity_fsm import FaceObservation


class AssociationKind(str, Enum):
    SHARED_TRACK = "SHARED_TRACK"
    SPATIAL_IOU = "SPATIAL_IOU"
    TEMPORAL_ONLY = "TEMPORAL_ONLY"
    NONE = "NONE"


#: How much each association strength is worth as a multiplier on track
#: confidence. Configuration in spirit and provisional in fact: Phase 2's
#: calibration set measures the ID-switch rate at each strength and replaces
#: these. Until then, only SHARED_TRACK is trusted at face value.
DEFAULT_ASSOCIATION_WEIGHTS: dict[AssociationKind, float] = {
    AssociationKind.SHARED_TRACK: 1.0,
    AssociationKind.SPATIAL_IOU: 0.5,
    AssociationKind.TEMPORAL_ONLY: 0.2,
    AssociationKind.NONE: 0.0,
}


@dataclass(frozen=True, slots=True)
class BodyTrack:
    """A person track from the body pipeline."""

    track_id: str
    camera_id: str
    ts_ms: int
    bbox: tuple[float, float, float, float]
    global_person_id: str | None = None
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class FaceDetection:
    """A face and its match, before it has been attached to anybody."""

    camera_id: str
    ts_ms: int
    bbox: tuple[float, float, float, float]
    candidate_id: str | None
    score: float
    margin: float
    quality: float = 1.0
    pose_deviation_deg: float = 0.0
    track_id: str | None = None


@dataclass(frozen=True, slots=True)
class Association:
    """How a face was attached to a body, and how much that is worth."""

    kind: AssociationKind
    body_track: BodyTrack | None
    overlap: float = 0.0
    weight: float = 0.0

    @property
    def is_strong(self) -> bool:
        return self.kind is AssociationKind.SHARED_TRACK

    def describe(self) -> str:
        if self.kind is AssociationKind.SHARED_TRACK:
            return "face and body share one tracker track"
        if self.kind is AssociationKind.SPATIAL_IOU:
            return f"face and body correlated by {self.overlap:.0%} box overlap (weak)"
        if self.kind is AssociationKind.TEMPORAL_ONLY:
            return "face and body seen on the same camera at the same time (very weak)"
        return "face could not be attached to any tracked body"


def iou(a: tuple[float, float, float, float],
        b: tuple[float, float, float, float]) -> float:
    """Intersection over union of two xyxy boxes."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    intersection = iw * ih
    if intersection <= 0:
        return 0.0
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def contains(outer: tuple[float, float, float, float],
             inner: tuple[float, float, float, float]) -> float:
    """Fraction of ``inner`` that lies inside ``outer``.

    Plain IoU is the wrong measure for a face against a body: a face box is a
    small fraction of a person box, so a perfectly-placed face scores a low IoU.
    Containment asks the question that actually matters, which is whether the
    face is on that body.
    """
    ix1, iy1 = max(outer[0], inner[0]), max(outer[1], inner[1])
    ix2, iy2 = min(outer[2], inner[2]), min(outer[3], inner[3])
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inner_area = max(0.0, inner[2] - inner[0]) * max(0.0, inner[3] - inner[1])
    return (iw * ih) / inner_area if inner_area > 0 else 0.0


@dataclass(frozen=True, slots=True)
class FusedObservation:
    """A face attached to a global person, with the attachment's provenance."""

    global_person_id: str | None
    observation: FaceObservation
    association: Association

    @property
    def is_usable(self) -> bool:
        return (
            self.global_person_id is not None
            and self.association.kind is not AssociationKind.NONE
        )


def associate(
    face: FaceDetection,
    bodies: list[BodyTrack],
    *,
    min_containment: float = 0.6,
    weights: dict[AssociationKind, float] | None = None,
) -> Association:
    """Attach a face to a body track, and say how confident that attachment is.

    Ambiguity is treated as failure, not as a tie to be broken. If two bodies
    both plausibly own a face, attaching it to the better-scoring one is exactly
    the silent guess that produces an identity on the wrong person, so this
    returns NONE and the face contributes nothing.
    """
    weights = weights or DEFAULT_ASSOCIATION_WEIGHTS
    same_camera = [b for b in bodies if b.camera_id == face.camera_id]

    if face.track_id is not None:
        for body in same_camera:
            if body.track_id == face.track_id:
                return Association(
                    kind=AssociationKind.SHARED_TRACK, body_track=body,
                    overlap=1.0, weight=weights[AssociationKind.SHARED_TRACK])

    scored = [(contains(b.bbox, face.bbox), b) for b in same_camera]
    plausible = [(score, b) for score, b in scored if score >= min_containment]

    if len(plausible) > 1:
        return Association(kind=AssociationKind.NONE, body_track=None, weight=0.0)

    if plausible:
        score, body = plausible[0]
        return Association(
            kind=AssociationKind.SPATIAL_IOU, body_track=body, overlap=score,
            weight=weights[AssociationKind.SPATIAL_IOU])

    if len(same_camera) == 1:
        return Association(
            kind=AssociationKind.TEMPORAL_ONLY, body_track=same_camera[0],
            overlap=0.0, weight=weights[AssociationKind.TEMPORAL_ONLY])

    return Association(kind=AssociationKind.NONE, body_track=None, weight=0.0)


def fuse(
    face: FaceDetection,
    bodies: list[BodyTrack],
    *,
    min_containment: float = 0.6,
    weights: dict[AssociationKind, float] | None = None,
) -> FusedObservation:
    """Turn a raw face detection into identity evidence about a global person.

    The association's weight scales `track_confidence`, so a weak attachment
    fails the identity gate's existing track-confidence test. There is no
    separate association threshold to keep in sync.
    """
    association = associate(face, bodies, min_containment=min_containment,
                            weights=weights)
    body = association.body_track
    base_confidence = body.confidence if body else 0.0

    observation = FaceObservation(
        ts_ms=face.ts_ms,
        candidate_id=face.candidate_id,
        score=face.score,
        margin=face.margin,
        quality=face.quality,
        pose_deviation_deg=face.pose_deviation_deg,
        track_confidence=base_confidence * association.weight,
        camera_id=face.camera_id,
        association_is_strong=association.is_strong,
    )
    return FusedObservation(
        global_person_id=body.global_person_id if body else None,
        observation=observation,
        association=association,
    )


def detect_identity_switch(
    previous: BodyTrack, current: BodyTrack, *, max_jump_px: float
) -> bool:
    """Whether a track's position jumped further than a person could move.

    A track that teleports has almost certainly swapped subjects. Reporting it
    lets the identity FSM stop trusting the track rather than carrying a
    confirmed name onto a different body, which is the failure mode that turns
    one missing person into two wrong answers.
    """
    if previous.track_id != current.track_id:
        return False
    px = _centre(previous.bbox)
    cx = _centre(current.bbox)
    distance = ((px[0] - cx[0]) ** 2 + (px[1] - cx[1]) ** 2) ** 0.5
    return distance > max_jump_px


def _centre(bbox: tuple[float, float, float, float]) -> tuple[float, float]:
    x1, y1, x2, y2 = bbox
    return ((x1 + x2) / 2, (y1 + y2) / 2)
