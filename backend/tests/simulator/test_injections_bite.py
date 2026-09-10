"""Every declared failure actually happens.

A knob that is declared, documented, and never read is worse than a missing
knob, because a test that names it looks like coverage. `id_switch_rate` was
exactly that for three phases: set in both injection profiles, drawn by a
property test, referenced in two documents, and read by nothing. Every run that
claimed to survive ID switches had survived none.

These tests are cheap and they close that class.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.core.events import EventType
from app.simulator.engine import DrillPlan, observe
from app.simulator.injections import NONE, Injections
from tests.simulator.conftest import ALARM_MS, population

HORIZON_MS = 600_000


def plan_for(site, agents, injections) -> DrillPlan:
    return DrillPlan(site=site, agents=agents, alarm_ms=ALARM_MS,
                     injections=injections, seed=20260910,
                     horizon_ms=HORIZON_MS)


def fingerprint(stream) -> tuple:
    """Everything about a stream that a failure could plausibly change."""
    return (
        len(stream.events),
        stream.dropped,
        tuple(sorted(stream.arrival_ms.items())),
        tuple((event.type.value, event.subject, event.ts_ms,
               tuple(sorted((k, str(v)) for k, v in event.payload.items())))
              for event in stream.events),
        tuple(sorted((gid, tuple(windows))
                     for gid, windows in stream.owners.items())),
    )


@pytest.fixture(scope="module")
def agents(site):
    return population(site, 60, 20260910)


RATE_KNOBS = tuple(
    field.name for field in dataclasses.fields(Injections)
    if field.name.endswith("_rate")
)


class TestEveryKnobBites:

    def test_the_catalogue_is_not_empty(self):
        # Guards the guard: if the fields are ever renamed away from `_rate`,
        # the parametrised test below would silently test nothing.
        assert len(RATE_KNOBS) >= 10

    @pytest.mark.parametrize("knob", RATE_KNOBS)
    def test_turning_it_up_changes_the_stream(self, site, agents, knob):
        baseline = fingerprint(observe(plan_for(site, agents, NONE)))
        injected = fingerprint(observe(plan_for(
            site, agents, dataclasses.replace(NONE, **{knob: 0.5}))))
        assert injected != baseline, f"{knob} is declared but never read"


class TestIdSwitch:

    def test_a_track_ends_up_carrying_a_different_person(self, site, agents):
        stream = observe(plan_for(site, agents, Injections(id_switch_rate=0.5)))
        shared = {gid: windows for gid, windows in stream.owners.items()
                  if len({person for _, person in windows}) > 1}
        assert shared, "no track ever changed hands"

    def test_the_face_reported_is_the_one_in_front_of_the_camera(
            self, site, agents):
        """A switch moves the track id, not the face.

        The whole danger is that the identity evidence keeps naming the person
        the track used to be while a different body carries it. If the face
        moved with the id there would be nothing to detect and nothing to
        survive.
        """
        stream = observe(plan_for(site, agents, Injections(id_switch_rate=0.5)))
        by_ref = {agent.person_ref: agent for agent in agents}

        checked = 0
        for event in stream.events:
            if event.type is not EventType.FACE_OBSERVED:
                continue
            seen = stream.who(event.subject, event.ts_ms)
            agent = by_ref.get(seen)
            if agent is None or not agent.has_gallery_entry or agent.is_visitor:
                continue
            assert event.payload["candidate_id"] == agent.emp_id
            checked += 1
        assert checked > 100

    def test_it_is_off_by_default(self, site, agents):
        stream = observe(plan_for(site, agents, NONE))
        assert all(len(windows) == 1 for windows in stream.owners.values())


class TestDelayHasALength:

    def test_a_late_event_keeps_its_timestamp_and_gains_an_arrival(
            self, site, agents):
        stream = observe(plan_for(
            site, agents, Injections(delay_rate=0.5, max_delay_ms=30_000)))
        assert stream.arrival_ms

        by_id = {event.event_id: event for event in stream.events}
        for event_id, arrival_ms in stream.arrival_ms.items():
            event = by_id[event_id]
            assert arrival_ms > event.ts_ms
            assert arrival_ms - event.ts_ms <= 30_000

    def test_a_long_enough_delay_misses_the_decision(self, site, agents):
        """The case worth injecting: an event that arrives after the commander
        has already stood the drill down never informed it."""
        plan = plan_for(site, agents,
                        Injections(delay_rate=1.0, max_delay_ms=300_000))
        stream = observe(plan)
        end_ms = ALARM_MS + HORIZON_MS
        missed = [event for event in stream.events
                  if event.ts_ms <= end_ms < stream.arrival_of(event)]
        assert missed
