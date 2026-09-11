"""Warden actions: provenance, offline queueing, and what each assertion means."""

from __future__ import annotations

import pytest

from app.core.events import EventType, SourceKind, validate as validate_event
from app.warden.actions import (
    ActionKind,
    DeviceQueue,
    WardenAction,
    WardenActionError,
    to_event,
    validate,
)

T0 = 1_788_000_000_000


def action(kind=ActionKind.CONFIRM_PRESENT, **overrides) -> WardenAction:
    base = dict(kind=kind, warden_id="warden-7", device_id="tablet-3",
                zone_id="assembly-north", ts_ms=T0, subject="emp:EMP-0001")
    base.update(overrides)
    return WardenAction(**base)


class TestProvenance:
    """A confirmation with no provenance is indistinguishable from a guess, and
    it would be the highest-trust evidence in the system."""

    @pytest.mark.parametrize("field", ["warden_id", "device_id", "zone_id"])
    def test_an_unattributable_action_is_refused(self, field):
        with pytest.raises(WardenActionError, match=field):
            validate(action(**{field: "  "}))

    def test_a_person_action_must_name_the_person(self):
        with pytest.raises(WardenActionError, match="must name the person"):
            validate(action(subject=None))

    def test_rejecting_an_identity_must_say_which_one(self):
        with pytest.raises(WardenActionError, match="name the identity"):
            validate(action(ActionKind.WRONG_PERSON, identity=None))

    def test_a_person_reference_is_not_a_gallery_identity(self):
        """`emp:EMP-0001` is the roster reference; `EMP-0001` is the identity.

        The warden tablet sent the first. It is truthy, so it passed, and then
        matched no candidate the matcher had ever proposed -- the rejection was
        recorded and changed nothing. Shape is all this layer can check, and it
        is enough to turn a silent no-op into a refusal the warden sees.
        """
        with pytest.raises(WardenActionError, match="not a person reference"):
            validate(action(ActionKind.WRONG_PERSON, identity="emp:EMP-0001"))
        validate(action(ActionKind.WRONG_PERSON, identity="EMP-0001"))

    def test_tagging_an_unknown_person_must_say_what_they_are(self):
        with pytest.raises(WardenActionError, match="visitor, contractor"):
            validate(action(ActionKind.TAG_UNKNOWN, subject=None, note=None))

    def test_an_empty_note_records_nothing(self):
        with pytest.raises(WardenActionError, match="records nothing"):
            validate(action(ActionKind.NOTE, subject=None, note="   "))

    def test_a_backwards_clock_is_refused(self):
        # An action whose time cannot be trusted cannot be evidence.
        with pytest.raises(WardenActionError, match="device clock is wrong"):
            validate(action(synced_at_ms=T0 - 1_000))

    def test_actions_are_immutable(self):
        a = action()
        with pytest.raises(Exception):
            a.warden_id = "someone-else"


class TestOfflineProvenance:
    def test_the_offline_flag_survives_into_the_event(self):
        # A confirmation queued on a device out of contact for six minutes was
        # made against a roster that may since have changed.
        a = action(queued_offline=True, synced_at_ms=T0 + 360_000)
        event = to_event(a, tenant_id="t", site_id="s", drill_id="d", seq=1)
        assert event.payload["queued_offline"] is True
        assert event.payload["sync_lag_ms"] == 360_000

    def test_an_online_action_carries_no_lag(self):
        event = to_event(action(), tenant_id="t", site_id="s", drill_id="d", seq=1)
        assert event.payload["queued_offline"] is False
        assert "sync_lag_ms" not in event.payload


class TestActionsBecomeEvents:
    def _event(self, a):
        return to_event(a, tenant_id="t", site_id="s", drill_id="d", seq=1)

    def test_every_action_kind_maps_to_a_valid_event(self):
        specs = {
            ActionKind.CONFIRM_PRESENT: {},
            ActionKind.NOT_HERE: {},
            ActionKind.WRONG_PERSON: {"identity": "EMP-0001"},
            ActionKind.MARK_ABSENT: {},
            ActionKind.TAG_UNKNOWN: {"subject": None, "note": "contractor"},
            ActionKind.SWEEP_COMPLETE: {"subject": None},
            ActionKind.ESCALATE: {"subject": None, "note": "need help"},
            ActionKind.NOTE: {"subject": None, "note": "gate blocked"},
        }
        for kind, extra in specs.items():
            validate_event(self._event(action(kind, **extra)))

    def test_the_device_is_the_event_source(self):
        # A device producing nonsense can be identified, and its sequence
        # tracked independently of every other warden's.
        event = self._event(action())
        assert event.source == "warden-device:tablet-3"
        assert event.source_kind is SourceKind.WARDEN

    def test_a_confirmation_records_the_zone_it_was_made_at(self):
        # This is what lets the accountability machine treat it as both halves
        # of the ACCOUNTED test.
        event = self._event(action())
        assert event.type is EventType.WARDEN_CONFIRMED
        assert event.payload["at_assembly_zone"] == "assembly-north"

    def test_not_here_is_not_a_confirmation_of_anything(self):
        # "Not at my muster point" is not "missing": they may be at the other
        # one, or never came in.
        event = self._event(action(ActionKind.NOT_HERE))
        assert event.type is EventType.WARDEN_NOTE
        assert "not at" in event.payload["note"]

    def test_wrong_person_rejects_a_named_identity(self):
        event = self._event(action(ActionKind.WRONG_PERSON, identity="EMP-0001"))
        assert event.type is EventType.WARDEN_REJECTED
        assert event.payload["identity"] == "EMP-0001"

    def test_marking_absent_is_flagged_for_the_decision_logic(self):
        event = self._event(action(ActionKind.MARK_ABSENT))
        assert event.payload["marked_absent"] is True

    def test_every_action_is_flagged_as_human_evidence(self):
        event = self._event(action())
        assert event.payload["human"] is True


class TestDeviceQueue:
    def test_actions_taken_offline_are_kept(self):
        queue = DeviceQueue(device_id="tablet-3", warden_id="warden-7")
        for i in range(5):
            queue.record(action(ts_ms=T0 + i * 1_000), online=False)
        assert queue.depth == 5
        assert all(a.queued_offline for a in queue.pending)

    def test_syncing_drains_in_the_order_the_warden_took_them(self):
        # A device out of contact for ten minutes delivers forty actions in the
        # order they were taken, so a gap means one was lost rather than late.
        queue = DeviceQueue(device_id="tablet-3", warden_id="warden-7")
        for i in reversed(range(5)):
            queue.record(action(subject=f"emp:EMP-{i}", ts_ms=T0 + i * 1_000),
                         online=False)
        events = queue.sync(now_ms=T0 + 600_000, tenant_id="t", site_id="s",
                            drill_id="d")
        assert [e.subject for e in events] == [f"emp:EMP-{i}" for i in range(5)]
        assert [e.seq for e in events] == [1, 2, 3, 4, 5]

    def test_sequence_numbers_are_assigned_on_the_device(self):
        # Assigning them on arrival would make offline work unorderable.
        queue = DeviceQueue(device_id="tablet-3", warden_id="warden-7")
        queue.record(action(ts_ms=T0), online=False)
        first = queue.sync(now_ms=T0 + 1_000, tenant_id="t", site_id="s", drill_id="d")
        queue.record(action(ts_ms=T0 + 2_000), online=True)
        second = queue.sync(now_ms=T0 + 3_000, tenant_id="t", site_id="s", drill_id="d")
        assert first[0].seq == 1
        assert second[0].seq == 2

    def test_syncing_empties_the_queue(self):
        queue = DeviceQueue(device_id="tablet-3", warden_id="warden-7")
        queue.record(action(), online=False)
        queue.sync(now_ms=T0 + 1_000, tenant_id="t", site_id="s", drill_id="d")
        assert queue.depth == 0
        assert len(queue.synced) == 1

    def test_staleness_says_how_long_the_device_has_been_out_of_contact(self):
        queue = DeviceQueue(device_id="tablet-3", warden_id="warden-7")
        assert queue.staleness_ms(T0) is None
        queue.record(action(ts_ms=T0), online=False)
        assert queue.staleness_ms(T0 + 240_000) == 240_000

    def test_an_online_action_is_not_marked_offline(self):
        queue = DeviceQueue(device_id="tablet-3", warden_id="warden-7")
        recorded = queue.record(action(), online=True)
        assert recorded.queued_offline is False
