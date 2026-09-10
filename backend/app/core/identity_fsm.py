"""Identity state machine, keyed on the global person id.

The vendored `TrackIdentityManager` has two outcomes: committed, or nothing. It
is keyed on a camera-local tracker object id, and when two identities compete it
picks the vote leader and says nothing. That is the gap EVAC-120 exists to
close. This module keeps its good ideas — accumulate votes, commit once, make
the commit sticky — and adds the states an evacuation actually needs.

Six states:

    UNKNOWN                   nothing yet, or nothing that cleared the gates
    CANDIDATE                 accepted evidence, not yet enough to commit
    CONFIRMED                 committed identity
    TEMPORARILY_UNAVAILABLE   confirmed earlier, face absent now, track reliable
    CONFLICT                  two identities with real support; a human decides
    REJECTED                  a warden said this is the wrong person

Three invariants shape every transition.

**Invariant 1.** A face that is absent, low quality, badly posed, or below the
score and margin gates produces *no* identity evidence. It never counts against
an identity, and it never counts for one.

**Invariant 2.** While the track stays reliable, a weak or unknown observation
cannot move an identity out of CONFIRMED. Only a rival with real support, or a
human, can.

**Invariant 3.** A conflict is never resolved by majority vote. Once a rival
clears `conflict_votes`, the state becomes CONFLICT and stays there until a
warden rules. `accountability_fsm` turns that into
MANUAL_VERIFICATION_REQUIRED.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Iterator


class IdentityState(str, Enum):
    UNKNOWN = "UNKNOWN"
    CANDIDATE = "CANDIDATE"
    CONFIRMED = "CONFIRMED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"
    CONFLICT = "CONFLICT"
    REJECTED = "REJECTED"


#: States from which a warden ruling is the only way out.
TERMINAL_WITHOUT_HUMAN: frozenset[IdentityState] = frozenset({
    IdentityState.CONFLICT, IdentityState.REJECTED,
})


class RejectionReason(str, Enum):
    """Why an observation produced no evidence. Recorded, never inferred."""

    ACCEPTED = "ACCEPTED"
    NO_FACE = "NO_FACE"
    LOW_QUALITY = "LOW_QUALITY"
    BAD_POSE = "BAD_POSE"
    LOW_TRACK_CONFIDENCE = "LOW_TRACK_CONFIDENCE"
    WEAK_ASSOCIATION = "WEAK_ASSOCIATION"
    BELOW_SCORE_THRESHOLD = "BELOW_SCORE_THRESHOLD"
    BELOW_MARGIN = "BELOW_MARGIN"


@dataclass(frozen=True, slots=True)
class IdentityConfig:
    """Thresholds. Every one is configuration, never a literal in logic.

    `calibrated` is False until these numbers have been validated against the
    Phase 2 calibration set. Invariant 7 forbids shipping an invented
    production number, so anything reading a config with `calibrated=False`
    must surface that rather than present its output as trustworthy.
    """

    score_threshold: float
    min_margin: float
    min_votes: int
    conflict_votes: int
    min_face_quality: float
    max_pose_deviation_deg: float
    min_track_confidence: float
    identity_expiry_ms: int
    require_strong_association: bool = True
    calibrated: bool = False
    source: str = "uncalibrated"

    def __post_init__(self) -> None:
        if self.min_votes < 1:
            raise ValueError("min_votes must be at least 1")
        if self.conflict_votes < 1:
            raise ValueError("conflict_votes must be at least 1")
        if self.identity_expiry_ms <= 0:
            raise ValueError("identity_expiry_ms must be positive")
        if not 0.0 <= self.min_face_quality <= 1.0:
            raise ValueError("min_face_quality must be within 0..1")


#: Starting point for Phase 1 and the simulator. The score and margin values are
#: the ones DeepStream actually ships in `config/config_pipeline.example.toml`
#: (`rec_threshold = 0.35`, `rec_margin = 0.05`); the rest are placeholders
#: chosen to be conservative. None of them is validated, which is why
#: `calibrated` is False. Phase 2 replaces this whole object from
#: docs/EVAC120_CALIBRATION.md.
PROVISIONAL_CONFIG = IdentityConfig(
    score_threshold=0.35,
    min_margin=0.05,
    min_votes=3,
    conflict_votes=3,
    min_face_quality=0.5,
    max_pose_deviation_deg=45.0,
    min_track_confidence=0.5,
    identity_expiry_ms=30_000,
    require_strong_association=True,
    calibrated=False,
    source="DeepStream config_pipeline.example.toml, unvalidated",
)


@dataclass(frozen=True, slots=True)
class FaceObservation:
    """One frame's worth of face evidence about one global person.

    `candidate_id` is None when no face was found at all, which is
    FACE_UNAVAILABLE rather than a statement about who the person is not.
    """

    ts_ms: int
    candidate_id: str | None = None
    score: float = -1.0
    margin: float = -1.0
    quality: float = 1.0
    pose_deviation_deg: float = 0.0
    track_confidence: float = 1.0
    camera_id: str | None = None
    association_is_strong: bool = True
    """Whether the face and the body came from one tracker.

    Set by `fusion.fuse`. When False the face was attached to a body by
    geometry, which is the weak correlation that puts a name on the wrong person
    when two people cross. It is a separate gate rather than only a confidence
    multiplier, because a multiplier's effect depends on a numeric coincidence
    between two independently-configured values, and this rule is too important
    to rest on one.
    """


@dataclass(frozen=True, slots=True)
class Transition:
    """A state change and the evidence that caused it. Feeds the ledger."""

    person_id: str
    from_state: IdentityState
    to_state: IdentityState
    ts_ms: int
    reason: str
    identity: str | None = None
    detail: dict = field(default_factory=dict)


def gate(obs: FaceObservation, config: IdentityConfig) -> RejectionReason:
    """Decide whether an observation is admissible evidence, and why not if it
    is not. Pure, so the simulator and the calibration harness share it."""
    if obs.candidate_id is None:
        return RejectionReason.NO_FACE
    if config.require_strong_association and not obs.association_is_strong:
        return RejectionReason.WEAK_ASSOCIATION
    if obs.track_confidence < config.min_track_confidence:
        return RejectionReason.LOW_TRACK_CONFIDENCE
    if obs.quality < config.min_face_quality:
        return RejectionReason.LOW_QUALITY
    if obs.pose_deviation_deg > config.max_pose_deviation_deg:
        return RejectionReason.BAD_POSE
    if obs.score < config.score_threshold:
        return RejectionReason.BELOW_SCORE_THRESHOLD
    if obs.margin < config.min_margin:
        return RejectionReason.BELOW_MARGIN
    return RejectionReason.ACCEPTED


@dataclass
class PersonIdentity:
    """Identity state for one global person id."""

    person_id: str
    config: IdentityConfig
    state: IdentityState = IdentityState.UNKNOWN
    identity: str | None = None
    votes: Counter = field(default_factory=Counter)
    score_sum: dict = field(default_factory=dict)
    last_accepted_ms: int | None = None
    last_observed_ms: int | None = None
    rejected_identities: set = field(default_factory=set)
    conflict_with: tuple[str, ...] = ()

    # -- queries ---------------------------------------------------------------

    @property
    def is_usable_for_accountability(self) -> bool:
        """Whether this identity may support an ACCOUNTED decision on its own.

        TEMPORARILY_UNAVAILABLE counts: the person was confirmed and the track
        never became unreliable, so the identity still holds. That is precisely
        invariant 1 — the face going away is not evidence the person did.
        """
        return self.state in (
            IdentityState.CONFIRMED, IdentityState.TEMPORARILY_UNAVAILABLE
        )

    @property
    def needs_human(self) -> bool:
        return self.state in TERMINAL_WITHOUT_HUMAN

    # There is deliberately no `runner_up`. An earlier design detected conflict
    # by comparing the leader against the second place, and that is exactly the
    # vote-count comparison invariant 3 forbids: a 9-to-3 split is still a
    # conflict. `_detect_conflict` counts independent support instead, and a
    # helper suggesting otherwise would invite the wrong fix.
    def leader(self) -> tuple[str | None, int]:
        if not self.votes:
            return None, 0
        winner, count = self.votes.most_common(1)[0]
        return winner, count

    # -- transitions -----------------------------------------------------------

    def observe(self, obs: FaceObservation) -> Transition | None:
        """Fold one observation in. Returns a Transition if the state changed."""
        self.last_observed_ms = obs.ts_ms

        if self.state in TERMINAL_WITHOUT_HUMAN:
            # Only a warden leaves CONFLICT or REJECTED. More camera evidence
            # cannot break the tie that created it (invariant 3).
            return None

        verdict = gate(obs, self.config)
        if verdict is not RejectionReason.ACCEPTED:
            return self._observe_without_evidence(obs, verdict)

        assert obs.candidate_id is not None
        if obs.candidate_id in self.rejected_identities:
            # A warden already ruled this person is not that employee. The
            # camera does not get to overrule them (invariant 9).
            return None

        return self._observe_with_evidence(obs)

    def _observe_without_evidence(
        self, obs: FaceObservation, verdict: RejectionReason
    ) -> Transition | None:
        """No admissible evidence this frame.

        Invariant 1: this is never evidence against the current identity. The
        only thing it can do is let a confirmed identity age into
        TEMPORARILY_UNAVAILABLE, and even that is reversible.
        """
        if self.state is IdentityState.CONFIRMED and self._identity_expired(obs.ts_ms):
            return self._transition(
                IdentityState.TEMPORARILY_UNAVAILABLE, obs.ts_ms,
                reason=f"no admissible face for {self.config.identity_expiry_ms} ms "
                       f"({verdict.value}); track still reliable",
                detail={"gate": verdict.value},
            )
        return None

    def _observe_with_evidence(self, obs: FaceObservation) -> Transition | None:
        assert obs.candidate_id is not None
        candidate = obs.candidate_id
        self.votes[candidate] += 1
        self.score_sum[candidate] = self.score_sum.get(candidate, 0.0) + obs.score
        self.last_accepted_ms = obs.ts_ms

        conflict = self._detect_conflict()
        if conflict is not None:
            return self._transition(
                IdentityState.CONFLICT, obs.ts_ms,
                reason="two identities have independent support; a human must rule",
                detail={"contenders": list(conflict),
                        "votes": {k: self.votes[k] for k in conflict}},
            )

        if self.state in (IdentityState.CONFIRMED,
                          IdentityState.TEMPORARILY_UNAVAILABLE):
            if candidate == self.identity:
                # Reconfirmation. Refreshes the expiry clock and, if the face
                # had gone away, brings it back to CONFIRMED.
                if self.state is IdentityState.TEMPORARILY_UNAVAILABLE:
                    return self._transition(
                        IdentityState.CONFIRMED, obs.ts_ms,
                        reason="face reobserved and matched the committed identity",
                        identity=self.identity)
                return None
            # A rival that has not yet earned CONFLICT cannot dislodge a
            # committed identity (invariant 2).
            return None

        leader, count = self.leader()
        if count >= self.config.min_votes:
            return self._transition(
                IdentityState.CONFIRMED, obs.ts_ms,
                reason=f"{count} accepted observations, threshold {self.config.min_votes}",
                identity=leader,
                detail={"mean_score": self.score_sum[leader] / count})

        if self.state is IdentityState.UNKNOWN:
            return self._transition(
                IdentityState.CANDIDATE, obs.ts_ms,
                reason=f"{count} accepted observation(s), needs {self.config.min_votes}",
                identity=leader)
        return None

    def tick(self, now_ms: int) -> Transition | None:
        """Advance time with no observation at all.

        A confirmed identity whose face has not been seen for `identity_expiry_ms`
        becomes TEMPORARILY_UNAVAILABLE. It does **not** become UNKNOWN: the
        person was identified, and nothing has contradicted that.
        """
        if self.state is IdentityState.CONFIRMED and self._identity_expired(now_ms):
            return self._transition(
                IdentityState.TEMPORARILY_UNAVAILABLE, now_ms,
                reason=f"no face for {self.config.identity_expiry_ms} ms",
                identity=self.identity)
        return None

    def warden_confirms(self, identity: str, now_ms: int, warden_id: str) -> Transition:
        """A human says this is who it is. Invariant 9: this is final."""
        self.rejected_identities.discard(identity)
        self.conflict_with = ()
        return self._transition(
            IdentityState.CONFIRMED, now_ms,
            reason="warden confirmed in person",
            identity=identity,
            detail={"warden_id": warden_id, "human": True})

    def warden_rejects(self, identity: str, now_ms: int, warden_id: str) -> Transition:
        """A human says this is the wrong person.

        The rejected identity is remembered, so no amount of later camera
        evidence can re-attach it. The person becomes REJECTED, meaning "the
        system's answer was wrong and a new one is needed", not "this person is
        missing".
        """
        self.rejected_identities.add(identity)
        self.votes.pop(identity, None)
        self.score_sum.pop(identity, None)
        return self._transition(
            IdentityState.REJECTED, now_ms,
            reason="warden rejected the system's identity",
            identity=None,
            detail={"rejected": identity, "warden_id": warden_id, "human": True})

    # -- internals -------------------------------------------------------------

    def _identity_expired(self, now_ms: int) -> bool:
        if self.last_accepted_ms is None:
            return False
        return now_ms - self.last_accepted_ms >= self.config.identity_expiry_ms

    def _detect_conflict(self) -> tuple[str, ...] | None:
        """Two identities each with `conflict_votes` support is a conflict.

        Deliberately not a comparison of vote counts. A 9-to-3 split is still a
        conflict: three independent accepted observations of a different person
        is real evidence, and silently outvoting it is exactly what invariant 3
        forbids.
        """
        contenders = tuple(sorted(
            name for name, count in self.votes.items()
            if count >= self.config.conflict_votes
        ))
        if len(contenders) < 2:
            return None
        self.conflict_with = contenders
        return contenders

    def _transition(
        self, to_state: IdentityState, ts_ms: int, *, reason: str,
        identity: str | None = None, detail: dict | None = None,
    ) -> Transition:
        from_state = self.state
        self.state = to_state
        if to_state is IdentityState.CONFIRMED:
            self.identity = identity
        elif to_state in (IdentityState.REJECTED, IdentityState.CONFLICT):
            self.identity = None
        elif to_state is IdentityState.CANDIDATE:
            self.identity = identity
        return Transition(
            person_id=self.person_id, from_state=from_state, to_state=to_state,
            ts_ms=ts_ms, reason=reason,
            identity=self.identity if to_state is not IdentityState.CANDIDATE else identity,
            detail=detail or {})


class IdentityRegistry:
    """All identities in one drill, keyed on global person id."""

    def __init__(self, config: IdentityConfig = PROVISIONAL_CONFIG) -> None:
        self.config = config
        self._people: dict[str, PersonIdentity] = {}

    def get(self, person_id: str) -> PersonIdentity:
        person = self._people.get(person_id)
        if person is None:
            person = PersonIdentity(person_id=person_id, config=self.config)
            self._people[person_id] = person
        return person

    def observe(self, person_id: str, obs: FaceObservation) -> Transition | None:
        return self.get(person_id).observe(obs)

    def tick(self, now_ms: int) -> list[Transition]:
        return [t for t in (p.tick(now_ms) for p in self._people.values()) if t]

    def in_state(self, state: IdentityState) -> list[PersonIdentity]:
        return [p for p in self._people.values() if p.state is state]

    def needing_human(self) -> list[PersonIdentity]:
        return [p for p in self._people.values() if p.needs_human]

    def __len__(self) -> int:
        return len(self._people)

    def __iter__(self) -> Iterator[PersonIdentity]:
        return iter(self._people.values())
