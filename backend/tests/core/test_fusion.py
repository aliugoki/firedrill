"""Fusing face evidence onto a body, and being honest about how strong the link is."""

import pytest

from app.core.fusion import (
    AssociationKind,
    BodyTrack,
    FaceDetection,
    associate,
    contains,
    detect_identity_switch,
    fuse,
    iou,
)
from dataclasses import replace

from app.core.identity_fsm import IdentityConfig, PersonIdentity, RejectionReason, gate

T0 = 1_788_000_000_000

CONFIG = IdentityConfig(
    score_threshold=0.35, min_margin=0.05, min_votes=3, conflict_votes=3,
    min_face_quality=0.5, max_pose_deviation_deg=45.0,
    min_track_confidence=0.5, identity_expiry_ms=30_000,
)


def body(track_id="t-1", person="gp-1", bbox=(100, 100, 200, 400), camera="cam-1",
         confidence=1.0) -> BodyTrack:
    return BodyTrack(track_id=track_id, camera_id=camera, ts_ms=T0, bbox=bbox,
                     global_person_id=person, confidence=confidence)


def face(bbox=(130, 110, 170, 160), camera="cam-1", track_id=None,
         candidate="EMP-1") -> FaceDetection:
    return FaceDetection(camera_id=camera, ts_ms=T0, bbox=bbox,
                         candidate_id=candidate, score=0.80, margin=0.30,
                         quality=0.9, track_id=track_id)


class TestGeometry:
    def test_iou_of_identical_boxes_is_one(self):
        assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0

    def test_iou_of_disjoint_boxes_is_zero(self):
        assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0

    def test_containment_is_the_right_measure_for_a_face_on_a_body(self):
        # A face box is a small fraction of a person box, so a perfectly placed
        # face scores a low IoU. Containment asks the question that matters.
        person = (100, 100, 200, 400)
        head = (130, 110, 170, 160)
        assert iou(person, head) < 0.1
        assert contains(person, head) == 1.0

    def test_a_face_half_outside_a_body_is_half_contained(self):
        assert contains((0, 0, 10, 10), (5, 0, 15, 10)) == pytest.approx(0.5)


class TestAssociation:
    def test_a_shared_track_is_the_only_strong_link(self):
        association = associate(face(track_id="t-1"), [body(track_id="t-1")])
        assert association.kind is AssociationKind.SHARED_TRACK
        assert association.is_strong is True

    def test_box_overlap_is_explicitly_weak(self):
        association = associate(face(), [body()])
        assert association.kind is AssociationKind.SPATIAL_IOU
        assert association.is_strong is False
        assert "weak" in association.describe()

    def test_two_plausible_bodies_produce_no_association(self):
        # Two people crossing at an exit, one turning to the camera. Attaching
        # the face to the better-scoring body is exactly the silent guess that
        # puts a name on the wrong person.
        crowded = [body(track_id="t-1", person="gp-1", bbox=(100, 100, 200, 400)),
                   body(track_id="t-2", person="gp-2", bbox=(110, 100, 210, 400))]
        assert associate(face(), crowded).kind is AssociationKind.NONE

    def test_a_lone_body_with_no_overlap_is_temporal_only(self):
        association = associate(face(bbox=(900, 900, 940, 950)), [body()])
        assert association.kind is AssociationKind.TEMPORAL_ONLY
        assert "very weak" in association.describe()

    def test_bodies_on_other_cameras_are_ignored(self):
        assert associate(face(camera="cam-1"),
                         [body(camera="cam-2")]).kind is AssociationKind.NONE

    def test_no_bodies_at_all_means_no_association(self):
        association = associate(face(), [])
        assert association.kind is AssociationKind.NONE
        assert "could not be attached" in association.describe()


class TestFusionFeedsTheIdentityGate:
    """Association strength scales track confidence, so a weak link fails the
    gate the identity FSM already applies. One place decides how much an
    observation is worth."""

    def test_a_shared_track_produces_admissible_evidence(self):
        fused = fuse(face(track_id="t-1"), [body(track_id="t-1")])
        assert fused.global_person_id == "gp-1"
        assert gate(fused.observation, CONFIG) is RejectionReason.ACCEPTED

    def test_a_box_overlap_link_is_rejected_outright(self):
        # This is the whole point of the module: the same face, the same score,
        # attached by weaker means, does not confirm an identity.
        fused = fuse(face(), [body()])
        assert fused.observation.association_is_strong is False
        assert gate(fused.observation, CONFIG) is RejectionReason.WEAK_ASSOCIATION

    def test_a_temporal_only_link_is_rejected_too(self):
        fused = fuse(face(bbox=(900, 900, 940, 950)), [body()])
        assert gate(fused.observation, CONFIG) is RejectionReason.WEAK_ASSOCIATION

    def test_the_rejection_does_not_rest_on_the_confidence_multiplier(self):
        # During Phase 1 a weight of 0.5 met a minimum track confidence of 0.5
        # exactly, and every geometry-correlated face passed the gate. The
        # structural check is what makes the rule independent of that
        # coincidence, so it is asserted even when the numbers happen to align.
        permissive = replace(CONFIG, min_track_confidence=0.0)
        fused = fuse(face(), [body()])
        assert gate(fused.observation, permissive) is RejectionReason.WEAK_ASSOCIATION

    def test_a_deployment_may_opt_into_weak_associations_explicitly(self):
        # Turning this off is a decision someone has to write down, not a
        # default that erodes.
        relaxed = replace(CONFIG, require_strong_association=False,
                          min_track_confidence=0.4)
        fused = fuse(face(), [body()])
        assert gate(fused.observation, relaxed) is RejectionReason.ACCEPTED

    def test_an_unattached_face_names_nobody(self):
        fused = fuse(face(), [])
        assert fused.global_person_id is None
        assert fused.is_usable is False

    def test_weak_association_never_confirms_an_identity(self):
        # Fifty clean, high-scoring face matches attached only by box overlap
        # still leave the person unidentified.
        person = PersonIdentity(person_id="gp-1", config=CONFIG)
        for i in range(50):
            detection = FaceDetection(
                camera_id="cam-1", ts_ms=T0 + i * 100, bbox=(130, 110, 170, 160),
                candidate_id="EMP-1", score=0.99, margin=0.9, quality=1.0)
            person.observe(fuse(detection, [body()]).observation)
        assert person.identity is None

    def test_a_low_confidence_body_track_drags_the_face_down_with_it(self):
        fused = fuse(face(track_id="t-1"),
                     [body(track_id="t-1", confidence=0.3)])
        assert fused.observation.track_confidence == pytest.approx(0.3)
        assert gate(fused.observation, CONFIG) is RejectionReason.LOW_TRACK_CONFIDENCE


class TestIdentitySwitchDetection:
    def test_a_teleporting_track_is_flagged(self):
        # A track that jumps further than a person could move has almost
        # certainly swapped subjects. Carrying a confirmed name across that
        # turns one missing person into two wrong answers.
        before = body(bbox=(100, 100, 200, 400))
        after = body(bbox=(900, 100, 1000, 400))
        assert detect_identity_switch(before, after, max_jump_px=200) is True

    def test_normal_walking_is_not_flagged(self):
        before = body(bbox=(100, 100, 200, 400))
        after = body(bbox=(140, 100, 240, 400))
        assert detect_identity_switch(before, after, max_jump_px=200) is False

    def test_different_tracks_are_not_compared(self):
        before = body(track_id="t-1", bbox=(100, 100, 200, 400))
        after = body(track_id="t-2", bbox=(900, 100, 1000, 400))
        assert detect_identity_switch(before, after, max_jump_px=200) is False
