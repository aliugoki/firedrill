"""Event schema, timestamp normalisation, and sequence tracking."""

import pytest

from app.core.events import (
    Event,
    EventType,
    EventValidationError,
    Gap,
    SequenceTracker,
    SourceKind,
    deduplicate,
    normalise_timestamp,
    validate,
)

T0 = 1_788_000_000_000  # a realistic epoch millisecond timestamp


def make_event(**overrides) -> Event:
    base = dict(
        tenant_id="tenant-1",
        site_id="site-1",
        drill_id="drill-1",
        source="cam-1",
        source_kind=SourceKind.CAMERA,
        seq=1,
        type=EventType.TRACK_UPDATED,
        ts_ms=T0,
        subject="gp-1",
    )
    base.update(overrides)
    return Event(**base)


class TestEventIdentity:
    def test_every_event_gets_a_unique_id(self):
        assert make_event().event_id != make_event().event_id

    def test_dedupe_key_is_source_and_seq(self):
        assert make_event(source="cam-2", seq=7).dedupe_key == ("cam-2", 7)

    def test_events_are_immutable(self):
        event = make_event()
        with pytest.raises(Exception):
            event.seq = 99


class TestValidation:
    def test_a_well_formed_event_passes(self):
        assert validate(make_event()) is not None

    @pytest.mark.parametrize("field", ["tenant_id", "site_id", "drill_id", "source"])
    def test_blank_identifiers_are_rejected(self, field):
        with pytest.raises(EventValidationError, match=field):
            validate(make_event(**{field: "   "}))

    def test_negative_sequence_is_rejected(self):
        with pytest.raises(EventValidationError, match="seq"):
            validate(make_event(seq=-1))

    def test_a_bool_is_not_a_sequence_number(self):
        # bool is a subclass of int, so True would otherwise pass as seq=1 and
        # silently collide with a real event.
        with pytest.raises(EventValidationError, match="seq"):
            validate(make_event(seq=True))

    def test_negative_timestamp_is_rejected(self):
        with pytest.raises(EventValidationError, match="ts_ms"):
            validate(make_event(ts_ms=-1))

    def test_payload_must_be_a_dict(self):
        with pytest.raises(EventValidationError, match="payload"):
            validate(make_event(payload=["not", "a", "dict"]))


class TestSourceAuthority:
    """A source may only say the kind of thing it is in a position to know."""

    def test_a_camera_cannot_claim_a_warden_confirmation(self):
        # Invariant 9 makes warden confirmation the final authority. If a camera
        # could forge one, that authority would be worthless.
        with pytest.raises(EventValidationError, match="camera source may not"):
            validate(make_event(type=EventType.WARDEN_CONFIRMED))

    def test_a_warden_device_cannot_claim_to_have_observed_a_face(self):
        with pytest.raises(EventValidationError, match="warden source may not"):
            validate(make_event(
                source="warden-device-3", source_kind=SourceKind.WARDEN,
                type=EventType.FACE_OBSERVED))

    def test_a_camera_cannot_declare_a_person_accounted(self):
        # Accountability is derived by the edge from the full evidence ledger,
        # never asserted by one camera.
        with pytest.raises(EventValidationError, match="camera source may not"):
            validate(make_event(type=EventType.PERSON_ACCOUNTED))

    def test_a_camera_cannot_start_a_drill(self):
        with pytest.raises(EventValidationError, match="camera source may not"):
            validate(make_event(type=EventType.DRILL_STARTED, subject=None))

    def test_a_warden_may_confirm_and_reject(self):
        for event_type in (EventType.WARDEN_CONFIRMED, EventType.WARDEN_REJECTED):
            validate(make_event(
                source="warden-device-3", source_kind=SourceKind.WARDEN,
                type=event_type, subject="EMP-1"))

    def test_the_edge_may_derive_accountability(self):
        validate(make_event(
            source="edge-1", source_kind=SourceKind.EDGE,
            type=EventType.PERSON_ACCOUNTED, subject="EMP-1"))


class TestSubjectRequirement:
    def test_a_person_event_without_a_subject_is_rejected(self):
        with pytest.raises(EventValidationError, match="requires a subject"):
            validate(make_event(
                source="edge-1", source_kind=SourceKind.EDGE,
                type=EventType.PERSON_ACCOUNTED, subject=None))

    def test_a_drill_event_needs_no_subject(self):
        validate(make_event(
            source="system", source_kind=SourceKind.SYSTEM,
            type=EventType.DRILL_STARTED, subject=None))


class TestVocabulary:
    def test_there_is_no_event_meaning_a_person_is_missing(self):
        # Invariant 1. The vocabulary itself refuses to express it: the system
        # can say evidence is absent, never that a person is.
        names = {e.value for e in EventType}
        assert "PERSON_MISSING" not in names
        assert "PERSON_SAFE" not in names
        assert EventType.FACE_UNAVAILABLE.value in names
        assert EventType.PERSON_UNACCOUNTED.value in names

    def test_every_type_is_emittable_by_exactly_one_kind_of_source_at_least(self):
        from app.core.events import _ALLOWED_BY_SOURCE

        emittable = set().union(*_ALLOWED_BY_SOURCE.values())
        assert emittable == set(EventType)


class TestTimestampNormalisation:
    def test_wall_clock_sources_pass_through(self):
        for kind in (SourceKind.EDGE, SourceKind.WARDEN, SourceKind.SYSTEM):
            assert normalise_timestamp(source_kind=kind, raw_ms=T0) == T0

    def test_camera_pts_is_offset_by_the_stream_origin(self):
        assert normalise_timestamp(
            source_kind=SourceKind.CAMERA, raw_ms=5_000, stream_origin_ms=T0
        ) == T0 + 5_000

    def test_a_zero_pts_becomes_the_stream_origin_not_the_epoch(self):
        # The first frame of a stream has pts_ms == 0. Treating that as epoch
        # milliseconds would place a live drill in 1970 and make every elapsed
        # time meaningless. It is also what triggers the upstream falsy-zero
        # defect recorded in docs/EVAC120_PROVENANCE.md.
        assert normalise_timestamp(
            source_kind=SourceKind.CAMERA, raw_ms=0, stream_origin_ms=T0
        ) == T0

    def test_a_camera_event_without_a_stream_origin_is_refused(self):
        with pytest.raises(EventValidationError, match="stream_origin_ms"):
            normalise_timestamp(source_kind=SourceKind.CAMERA, raw_ms=0)

    def test_negative_inputs_are_refused(self):
        with pytest.raises(EventValidationError):
            normalise_timestamp(source_kind=SourceKind.EDGE, raw_ms=-1)
        with pytest.raises(EventValidationError):
            normalise_timestamp(
                source_kind=SourceKind.CAMERA, raw_ms=1, stream_origin_ms=-1)


class TestSequenceTracker:
    def test_a_clean_run_reports_no_gaps(self):
        tracker = SequenceTracker()
        for seq in range(1, 11):
            is_new, gap = tracker.observe("cam-1", seq)
            assert is_new is True
            assert gap is None
        assert tracker.outstanding_gaps() == []
        assert tracker.highest_seq("cam-1") == 10

    def test_a_duplicate_is_reported_as_not_new(self):
        tracker = SequenceTracker()
        assert tracker.observe("cam-1", 1)[0] is True
        assert tracker.observe("cam-1", 1)[0] is False

    def test_a_jump_reports_a_gap_with_the_right_size(self):
        tracker = SequenceTracker()
        tracker.observe("cam-1", 1)
        _, gap = tracker.observe("cam-1", 5)
        assert gap == Gap(source="cam-1", after_seq=1, before_seq=5)
        assert gap.missing_count == 3

    def test_a_gap_stays_outstanding_until_it_is_filled(self):
        tracker = SequenceTracker()
        tracker.observe("cam-1", 1)
        tracker.observe("cam-1", 4)
        assert len(tracker.outstanding_gaps()) == 1
        tracker.observe("cam-1", 2)
        assert len(tracker.outstanding_gaps()) == 1
        tracker.observe("cam-1", 3)
        assert tracker.outstanding_gaps() == []

    def test_out_of_order_arrival_is_not_a_gap(self):
        tracker = SequenceTracker()
        tracker.observe("cam-1", 2)
        is_new, gap = tracker.observe("cam-1", 1)
        assert is_new is True
        assert gap is None

    def test_sources_are_tracked_independently(self):
        tracker = SequenceTracker()
        tracker.observe("cam-1", 1)
        tracker.observe("cam-2", 100)
        assert tracker.highest_seq("cam-1") == 1
        assert tracker.highest_seq("cam-2") == 100
        assert tracker.outstanding_gaps() == []
        assert tracker.sources() == ["cam-1", "cam-2"]

    def test_a_gap_is_never_silently_repaired(self):
        # Invariant 8: missing evidence degrades confidence. The tracker reports
        # the hole and does not interpolate across it.
        tracker = SequenceTracker()
        tracker.observe("cam-1", 1)
        tracker.observe("cam-1", 1000)
        gaps = tracker.outstanding_gaps()
        assert len(gaps) == 1
        assert gaps[0].missing_count == 998


class TestDeduplicate:
    def test_repeats_are_dropped_and_order_is_kept(self):
        events = [
            make_event(seq=1), make_event(seq=2), make_event(seq=1),
            make_event(seq=3), make_event(seq=2),
        ]
        assert [e.seq for e in deduplicate(events)] == [1, 2, 3]

    def test_the_same_seq_from_different_sources_is_two_events(self):
        events = [make_event(source="cam-1", seq=1), make_event(source="cam-2", seq=1)]
        assert len(list(deduplicate(events))) == 2
