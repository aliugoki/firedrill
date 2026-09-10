"""Chaos: kill each component during a drill and check what the system says.

The gate for Phase 3. Each test kills one thing, and asserts three properties
that must survive it:

    **Nobody is falsely cleared.** Checked against ground truth the core cannot
    see. This is the same property Phase 1 proves, re-proved against the real
    ingest path with infrastructure failing underneath it.

    **The board says DEGRADED while it is degraded.** A failure the operator
    cannot see is worse than the failure, because it looks like calm.

    **It converges on replay.** Whatever was buffered arrives, and the state
    afterwards matches the state that never failed.

The failures are simulated rather than containers being killed, because the
properties are about the software's response and that response has to be
asserted, not eyeballed. `scripts/evac_chaos.sh` does the container-level
version against a live stack, where the assertions are coarser but real.
"""

from __future__ import annotations

import pytest

from app.core.accountability_fsm import AccountabilityState
from app.core.events import Event, EventType, SourceKind
from app.ingest.health import Component
from app.ingest.ingestor import Ingestor
from app.ingest.projections import build_board
from app.ingest.replication import (
    InMemoryTransport,
    Outbox,
    Replicator,
)
from app.simulator.agents import build_population, walk_all
from app.simulator.engine import DrillPlan, build_roster, observe, run_drill
from app.simulator.injections import NONE, Injections, Outage
from app.simulator.site import default_site

ALARM_MS = 1_788_000_000_000
HORIZON_MS = 600_000


@pytest.fixture(scope="module")
def site():
    return default_site()


@pytest.fixture(scope="module")
def agents(site):
    people = build_population(site, count=150, seed=7)
    walk_all(people, site, alarm_ms=ALARM_MS, seed=7)
    return people


def plan_for(site, agents, injections=NONE) -> DrillPlan:
    return DrillPlan(site=site, agents=agents, alarm_ms=ALARM_MS,
                     injections=injections, seed=7)


def truth(agents) -> set:
    return {a.person_ref for a in agents if a.reached_assembly}


def falsely_accounted(result, agents) -> set:
    """Everyone the system cleared who did not reach an assembly point.

    See `tests/simulator/conftest.py` for the one exclusion and why it is
    there: a stable misidentification of somebody nothing else observes leaves
    no camera evidence to contradict it, and the manual roll-call is what
    catches it.
    """
    substituted = {f"emp:{emp_id}"
                   for emp_id in result.stream.misidentified_as.values()}
    return result.accounted_refs() - truth(agents) - substituted


def board_for(state, plan, now_ms=ALARM_MS + HORIZON_MS):
    return build_board(state, build_roster(plan.agents, plan.alarm_ms),
                       now_ms=now_ms, config=plan.accountability_config)


def system_event(event_type: EventType, ts_ms: int, subject=None,
                 payload=None, seq=1) -> Event:
    return Event(
        tenant_id="tenant-sim", site_id="site-sialkot-office", drill_id="drill-sim",
        source="chaos", source_kind=SourceKind.SYSTEM, seq=seq, type=event_type,
        ts_ms=ts_ms, subject=subject, payload=payload or {})


class TestCameraDiesMidDrill:
    def test_the_board_shows_degraded_while_it_is_down(self, site, agents):
        plan = plan_for(site, agents, Injections(
            camera_outages=(Outage(60_000, 180_000, "cam-floor-2-open"),)))
        stream = observe(plan)
        ingestor = Ingestor(identity_config=plan.identity_config,
                            presence_config=plan.presence_config)

        during = [e for e in stream.events if e.ts_ms <= ALARM_MS + 120_000]
        ingestor.feed_batch(during)
        assert ingestor.state.health.is_blind is True
        assert ingestor.state.health.causes_at(ALARM_MS + 120_000)

    def test_the_board_recovers_when_the_camera_does(self, site, agents):
        plan = plan_for(site, agents, Injections(
            camera_outages=(Outage(60_000, 180_000, "cam-floor-2-open"),)))
        ingestor = Ingestor(identity_config=plan.identity_config,
                            presence_config=plan.presence_config)
        ingestor.feed_batch(observe(plan).events)
        assert ingestor.state.health.is_blind is False

    def test_the_outage_is_still_in_the_record_afterwards(self, site, agents):
        # A boolean would read healthy by now. A report has to be able to say
        # the system was blind when it made a particular call.
        plan = plan_for(site, agents, Injections(
            camera_outages=(Outage(60_000, 180_000, "cam-floor-2-open"),)))
        ingestor = Ingestor(identity_config=plan.identity_config,
                            presence_config=plan.presence_config)
        ingestor.feed_batch(observe(plan).events)
        health = ingestor.state.health
        assert health.was_degraded_at(ALARM_MS + 120_000) is True
        assert health.was_degraded_at(ALARM_MS + 300_000) is False

    def test_nobody_is_falsely_cleared(self, site, agents):
        plan = plan_for(site, agents, Injections(
            camera_outages=(Outage(60_000, 180_000, "cam-floor-2-open"),)))
        result = run_drill(plan)
        assert falsely_accounted(result, agents) == set()

    def test_people_it_was_watching_are_not_aged_into_lost(self, site, agents):
        # Invariant 8. Our own outage is not evidence about a person, so the
        # grace clock stops for the people that camera covered.
        clean = run_drill(plan_for(site, agents, NONE))
        outage = run_drill(plan_for(site, agents, Injections(
            camera_outages=(Outage(30_000, 90_000, "cam-floor-2-open"),))))
        clean_lost = sum(1 for p in clean.state.presence if p.state.value == "LOST")
        outage_lost = sum(1 for p in outage.state.presence if p.state.value == "LOST")
        assert outage_lost <= clean_lost + 5


class TestEveryCameraDies:
    def test_a_totally_blind_system_clears_nobody_new(self, site, agents):
        plan = plan_for(site, agents, Injections(
            camera_outages=(Outage(0, 10_000_000, "*"),)))
        result = run_drill(plan)
        assert result.accounted_refs() == set()

    def test_and_it_says_so_rather_than_looking_calm(self, site, agents):
        plan = plan_for(site, agents, Injections(
            camera_outages=(Outage(0, 10_000_000, "*"),)))
        result = run_drill(plan)
        board = board_for(result.state, plan)
        assert board.all_clear is False
        assert board.blocking_all_clear()
        assert board.health.blind_fraction > 0.9

    def test_the_caveat_defers_to_the_manual_count(self, site, agents):
        plan = plan_for(site, agents, Injections(
            camera_outages=(Outage(0, 10_000_000, "*"),)))
        result = run_drill(plan)
        caveat = board_for(result.state, plan).health.caveat()
        assert caveat and "manual roll-call is the authority" in caveat


class TestThePipelineDies:
    def test_a_dead_pipeline_degrades_the_board(self, site, agents):
        plan = plan_for(site, agents, NONE)
        ingestor = Ingestor(identity_config=plan.identity_config,
                            presence_config=plan.presence_config)
        ingestor.feed_batch(observe(plan).events)
        ingestor.feed(system_event(
            EventType.SYSTEM_DEGRADED, ALARM_MS + 300_000,
            payload={"component": "PIPELINE", "reason": "DeepStream container died"}))
        assert ingestor.state.health.is_blind is True

    def test_people_are_not_cleared_while_it_is_down(self, site, agents):
        plan = plan_for(site, agents, NONE)
        ingestor = Ingestor(identity_config=plan.identity_config,
                            presence_config=plan.presence_config)
        ingestor.feed_batch(observe(plan).events)
        ingestor.feed(system_event(
            EventType.SYSTEM_DEGRADED, ALARM_MS + 300_000,
            payload={"component": "PIPELINE", "reason": "container died"}))
        board = board_for(ingestor.state, plan)
        assert board.all_clear is False
        assert any("outage" in r for r in board.blocking_all_clear())


class TestTheEventBusDies:
    """Redis. Events stop arriving; the system must not read that as calm."""

    def test_lost_events_leave_a_reported_gap(self, site, agents):
        plan = plan_for(site, agents, Injections(drop_rate=0.2))
        ingestor = Ingestor(identity_config=plan.identity_config,
                            presence_config=plan.presence_config)
        ingestor.feed_batch(observe(plan).events)
        assert ingestor.state.tracker.outstanding_gaps()

    def test_a_gap_becomes_evidence_against_certainty(self, site, agents):
        from app.core.ledger import EvidenceKind

        plan = plan_for(site, agents, Injections(drop_rate=0.2))
        ingestor = Ingestor(identity_config=plan.identity_config,
                            presence_config=plan.presence_config)
        ingestor.feed_batch(observe(plan).events)
        assert any(e.kind is EvidenceKind.SEQUENCE_GAP
                   for e in ingestor.state.ledger)

    def test_losing_everything_clears_nobody(self, site, agents):
        result = run_drill(plan_for(site, agents, Injections(drop_rate=1.0)))
        assert result.accounted_refs() == set()

    def test_a_bus_outage_is_blinding(self, site, agents):
        plan = plan_for(site, agents, NONE)
        ingestor = Ingestor(identity_config=plan.identity_config,
                            presence_config=plan.presence_config)
        ingestor.feed(system_event(
            EventType.SYSTEM_DEGRADED, ALARM_MS,
            payload={"component": "EVENT_BUS", "reason": "redis unreachable"}))
        assert ingestor.state.health.is_blind is True


class TestTheDatabaseDies:
    """Postgres. The system can still see; it may not be able to remember."""

    def test_a_database_outage_does_not_blind_the_system(self, site, agents):
        plan = plan_for(site, agents, NONE)
        ingestor = Ingestor(identity_config=plan.identity_config,
                            presence_config=plan.presence_config)
        ingestor.feed(system_event(
            EventType.SYSTEM_DEGRADED, ALARM_MS,
            payload={"component": "DATABASE", "reason": "postgres down"}))
        assert ingestor.state.health.is_degraded is True
        assert ingestor.state.health.is_blind is False

    def test_accountability_keeps_working_through_it(self, site, agents):
        # The core holds its state in memory. Losing the projection store costs
        # durability and reporting, not the ability to run the drill.
        plan = plan_for(site, agents, NONE)
        ingestor = Ingestor(identity_config=plan.identity_config,
                            presence_config=plan.presence_config)
        ingestor.feed(system_event(
            EventType.SYSTEM_DEGRADED, ALARM_MS,
            payload={"component": "DATABASE", "reason": "postgres down"}))
        ingestor.feed_batch(observe(plan).events)
        board = board_for(ingestor.state, plan)
        assert board.accounted > 0

    def test_but_the_board_still_refuses_an_all_clear(self, site, agents):
        plan = plan_for(site, agents, NONE)
        ingestor = Ingestor(identity_config=plan.identity_config,
                            presence_config=plan.presence_config)
        ingestor.feed_batch(observe(plan).events)
        ingestor.feed(system_event(
            EventType.SYSTEM_DEGRADED, ALARM_MS + 300_000,
            payload={"component": "DATABASE", "reason": "postgres down"}))
        assert board_for(ingestor.state, plan).all_clear is False


class TestTheLinkToCentralDies:
    def test_the_drill_is_unaffected(self, site, agents, tmp_path):
        # The edge is the authority. Losing central must cost nothing
        # operationally.
        plan = plan_for(site, agents, NONE)
        with_link = run_drill(plan)

        transport = InMemoryTransport()
        transport.up = False
        replicator = Replicator(outbox=Outbox(tmp_path / "o.db"),
                                transport=transport)
        stream = observe(plan)
        for event in stream.events:
            replicator.enqueue(event, now_ms=event.ts_ms)
            replicator.flush(event.ts_ms)

        ingestor = Ingestor(identity_config=plan.identity_config,
                            presence_config=plan.presence_config)
        ingestor.feed_batch(stream.events)
        without = board_for(ingestor.state, plan)
        assert without.accounted == board_for(with_link.state, plan).accounted

    def test_nothing_is_lost_while_the_link_is_down(self, site, agents, tmp_path):
        plan = plan_for(site, agents, NONE)
        transport = InMemoryTransport()
        transport.up = False
        replicator = Replicator(outbox=Outbox(tmp_path / "o.db"),
                                transport=transport)
        stream = observe(plan)
        for event in stream.events:
            replicator.enqueue(event, now_ms=event.ts_ms)
        replicator.flush(ALARM_MS)
        assert replicator.backlog == len(stream.events)

    def test_central_converges_when_the_link_returns(self, site, agents, tmp_path):
        plan = plan_for(site, agents, NONE)
        transport = InMemoryTransport()
        transport.up = False
        replicator = Replicator(outbox=Outbox(tmp_path / "o.db"),
                                transport=transport)
        stream = observe(plan)
        for event in stream.events:
            replicator.enqueue(event, now_ms=event.ts_ms)

        transport.up = True
        replicator.drain(ALARM_MS + HORIZON_MS)
        assert replicator.backlog == 0

        report = transport.reconciler.report()
        assert report.is_complete is True
        assert report.events_held == len(stream.events)

    def test_central_reaches_the_same_conclusions_as_the_edge(
        self, site, agents, tmp_path
    ):
        # Convergence means the same answers, not merely the same event count.
        plan = plan_for(site, agents, NONE)
        transport = InMemoryTransport()
        replicator = Replicator(outbox=Outbox(tmp_path / "o.db"),
                                transport=transport)
        stream = observe(plan)
        for event in stream.events:
            replicator.enqueue(event, now_ms=event.ts_ms)
        replicator.drain(ALARM_MS + HORIZON_MS)

        edge = Ingestor(identity_config=plan.identity_config,
                        presence_config=plan.presence_config)
        edge.feed_batch(stream.events)
        central = Ingestor(identity_config=plan.identity_config,
                           presence_config=plan.presence_config)
        central.feed_batch(transport.reconciler.received)

        edge_board = board_for(edge.state, plan)
        central_board = board_for(central.state, plan)
        assert central_board.accounted == edge_board.accounted
        assert central_board.unaccounted == edge_board.unaccounted


class TestNetworkPartitionThenReplay:
    def test_a_partition_then_a_flood_converges(self, site, agents, tmp_path):
        # The realistic shape: the link drops, thousands of events pile up, and
        # the link returns all at once.
        plan = plan_for(site, agents, NONE)
        transport = InMemoryTransport()
        replicator = Replicator(outbox=Outbox(tmp_path / "o.db"),
                                transport=transport, batch_size=100)
        stream = observe(plan)

        for i, event in enumerate(stream.events):
            if 1_000 <= i < 5_000:
                transport.up = False
            else:
                transport.up = True
            replicator.enqueue(event, now_ms=event.ts_ms)
            replicator.flush(event.ts_ms)

        transport.up = True
        replicator.drain(ALARM_MS + HORIZON_MS)
        assert replicator.backlog == 0
        assert transport.reconciler.report().is_complete is True

    def test_redelivery_after_a_partition_is_harmless(self, site, agents):
        # Redis redelivers on consumer restart. Folding the same events twice
        # must reach the same state.
        plan = plan_for(site, agents, NONE)
        events = observe(plan).events
        once = Ingestor(identity_config=plan.identity_config,
                        presence_config=plan.presence_config)
        once.feed_batch(events)
        twice = Ingestor(identity_config=plan.identity_config,
                         presence_config=plan.presence_config)
        twice.feed_batch(events)
        twice.feed_batch(events)

        assert board_for(twice.state, plan).accounted == \
               board_for(once.state, plan).accounted
        assert twice.state.duplicates_dropped == len(events)


class TestEverythingAtOnce:
    def test_the_system_degrades_and_still_clears_nobody_falsely(self, site, agents):
        plan = plan_for(site, agents, Injections(
            face_loss_rate=0.5, drop_rate=0.15, duplicate_rate=0.2,
            reorder_rate=0.2, track_fragmentation_rate=0.2,
            camera_outages=(Outage(30_000, 240_000, "*"),),
            redis_outages=(Outage(90_000, 150_000),),
            db_outages=(Outage(200_000, 260_000),),
            network_partitions=(Outage(120_000, 200_000),)))
        result = run_drill(plan)
        assert falsely_accounted(result, agents) == set()

        board = board_for(result.state, plan)
        assert board.all_clear is False
        assert board.health.total_outages >= 2
        assert board.health.caveat() is not None

    def test_a_lost_recovery_event_leaves_the_system_degraded(self, site, agents):
        """The conservative direction, and the one that matters.

        Under event loss a CAMERA_RECOVERED can go missing. The system must
        keep believing the camera is down: staying degraded when it has in fact
        recovered costs an operator some confidence, while falsely recovering
        would let a blind system declare an all-clear. Only one of those errors
        is survivable.
        """
        plan = plan_for(site, agents, Injections(
            drop_rate=0.15,
            camera_outages=(Outage(30_000, 240_000, "*"),)))
        stream = observe(plan)
        types = [e.type for e in stream.events]
        if EventType.CAMERA_RECOVERED in types:
            pytest.skip("this seed did not drop the recovery event")

        ingestor = Ingestor(identity_config=plan.identity_config,
                            presence_config=plan.presence_config)
        ingestor.feed_batch(stream.events)
        assert ingestor.state.health.is_blind is True
        assert ingestor.state.health.open_now()

    def test_an_outage_that_never_closes_blocks_the_all_clear_forever(
        self, site, agents
    ):
        plan = plan_for(site, agents, NONE)
        ingestor = Ingestor(identity_config=plan.identity_config,
                            presence_config=plan.presence_config)
        ingestor.feed_batch(observe(plan).events)
        ingestor.feed(system_event(
            EventType.CAMERA_FAILURE, ALARM_MS + 10_000, "cam-1",
            {"reason": "offline"}, seq=99))
        board = board_for(ingestor.state, plan, now_ms=ALARM_MS + 10_000_000)
        assert board.all_clear is False
        assert any("outage" in r for r in board.blocking_all_clear())

    def test_every_person_still_has_a_state_and_a_reason(self, site, agents):
        plan = plan_for(site, agents, Injections(
            face_loss_rate=0.5, drop_rate=0.15,
            camera_outages=(Outage(30_000, 240_000, "*"),)))
        result = run_drill(plan)
        board = board_for(result.state, plan)
        assert len(board.rows) == board.expected
        for row in board.rows:
            assert row.decision.reason.strip()
            assert row.colour in {"GREEN", "YELLOW", "ORANGE", "RED"}

    def test_the_priority_list_puts_the_worst_first(self, site, agents):
        plan = plan_for(site, agents, Injections(
            face_loss_rate=0.5, camera_outages=(Outage(30_000, 240_000, "*"),)))
        result = run_drill(plan)
        priority = board_for(result.state, plan).priority(limit=20)
        assert priority
        states = [row.state for row in priority]
        if AccountabilityState.UNACCOUNTED in states:
            assert states[0] is AccountabilityState.UNACCOUNTED
