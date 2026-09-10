"""The HTTP surface: /api/evac/*.

Thin on purpose. Every endpoint resolves a drill, calls one domain method, and
shapes the result. No accountability reasoning happens here, because a rule that
lives in a route handler cannot be driven by the simulator and cannot be proved
by the chaos suite.

Authorisation is by the four role-shaped permissions in `app.infra.permissions`.
The separation they enforce is not decoration: a warden cannot start or stop a
drill, and an operator cannot sign off a physical headcount. Collapsing those
into one role would defeat the two-source evidence model the whole system rests
on, so the dependency that checks them is applied per route rather than globally.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Callable

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.api import schemas
from app.core.roster import RosterSnapshot
from app.drill import Drill, DrillError, DrillRegistry, DrillStatus
from app.infra.permissions import (
    EVAC_ADMIN,
    EVAC_OPERATE,
    EVAC_READ,
    EVAC_WARDEN,
)
from app.warden.actions import ActionKind, WardenAction, WardenActionError, validate
from app.warden.headcount import DEFAULT_POLICY, Headcount


def now_ms() -> int:
    return int(time.time() * 1000)


# --- authorisation ------------------------------------------------------------
# Phase 4 reads the caller from headers a gateway is expected to set. The JWT
# verification itself is deployment shell rather than domain logic, and putting
# a hand-rolled one here would be worse than leaving the seam visible.


class Caller:
    def __init__(self, user_id: str, permissions: set[str], zones: set[str]) -> None:
        self.user_id = user_id
        self.permissions = permissions
        self.zones = zones

    def may(self, permission: str) -> bool:
        return permission in self.permissions

    def covers(self, zone_id: str) -> bool:
        """A warden is scoped to the zones they were assigned.

        Not cosmetic: a warden confirming people at a zone they are not standing
        in is making the one claim the system trusts above its own cameras,
        about a place they cannot see.
        """
        return not self.zones or zone_id in self.zones


def get_caller(
    x_user_id: str = Header(default=""),
    x_permissions: str = Header(default=""),
    x_zones: str = Header(default=""),
) -> Caller:
    if not x_user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "no caller identity")
    return Caller(
        user_id=x_user_id,
        permissions={p.strip() for p in x_permissions.split(",") if p.strip()},
        zones={z.strip() for z in x_zones.split(",") if z.strip()},
    )


def requires(permission: str) -> Callable:
    def dependency(caller: Caller = Depends(get_caller)) -> Caller:
        if not caller.may(permission):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"{permission} is required for this action")
        return caller

    return dependency


def create_app(registry: DrillRegistry | None = None,
               roster_provider: Callable[[str], RosterSnapshot] | None = None,
               assembly_zones: frozenset = frozenset()) -> FastAPI:
    app = FastAPI(
        title="EVAC-120",
        version="0.4.0",
        description=(
            "Fire drill and evacuation accountability.\n\n"
            "This system supplements, and never replaces, certified fire and "
            "life-safety systems. Its output is decision support for a human "
            "incident commander, and a floor warden's physical count is the "
            "final authority."
        ),
    )
    app.state.registry = registry or DrillRegistry()
    app.state.roster_provider = roster_provider
    app.state.assembly_zones = assembly_zones

    def drill_or_404(drill_id: str) -> Drill:
        try:
            return app.state.registry.get(drill_id)
        except KeyError:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"no drill {drill_id}")

    # --- drills ---------------------------------------------------------------

    @app.post("/api/evac/drills", response_model=schemas.DrillSummary,
              status_code=status.HTTP_201_CREATED, tags=["drills"])
    def create_drill(body: schemas.CreateDrill,
                     caller: Caller = Depends(requires(EVAC_OPERATE))):
        if app.state.roster_provider is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "no roster source is configured; a drill without a roster has "
                "no denominator and cannot account for anyone")
        roster = app.state.roster_provider(body.site_id)
        drill = Drill(
            drill_id=str(uuid.uuid4()), tenant_id=body.tenant_id,
            site_id=body.site_id, name=body.name, roster=roster,
            created_ms=now_ms(), assembly_zones=app.state.assembly_zones)
        app.state.registry.add(drill)
        return _summary(drill)

    @app.get("/api/evac/drills", response_model=list[schemas.DrillSummary],
             tags=["drills"])
    def list_drills(caller: Caller = Depends(requires(EVAC_READ))):
        return [_summary(d) for d in app.state.registry.list()]

    @app.get("/api/evac/drills/{drill_id}", response_model=schemas.DrillSummary,
             tags=["drills"])
    def get_drill(drill_id: str, caller: Caller = Depends(requires(EVAC_READ))):
        return _summary(drill_or_404(drill_id))

    @app.post("/api/evac/drills/{drill_id}/start",
              response_model=schemas.DrillSummary, tags=["drills"])
    def start_drill(drill_id: str,
                    caller: Caller = Depends(requires(EVAC_OPERATE))):
        drill = drill_or_404(drill_id)
        running = app.state.registry.running()
        if running is not None and running.drill_id != drill_id:
            # Two simultaneous evacuations of one building is not a scenario;
            # it is a bug that would split the roster in half.
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"drill {running.drill_id} is already running on this site")
        try:
            drill.start(now_ms())
        except DrillError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
        return _summary(drill)

    @app.post("/api/evac/drills/{drill_id}/complete",
              response_model=schemas.DrillSummary, tags=["drills"])
    def complete_drill(drill_id: str,
                       caller: Caller = Depends(requires(EVAC_OPERATE))):
        drill = drill_or_404(drill_id)
        try:
            drill.complete(now_ms())
        except DrillError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc))
        return _summary(drill)

    # --- the live board -------------------------------------------------------

    @app.get("/api/evac/drills/{drill_id}/board", response_model=schemas.BoardOut,
             tags=["board"])
    def get_board(drill_id: str, caller: Caller = Depends(requires(EVAC_READ))):
        drill = drill_or_404(drill_id)
        at = now_ms()
        return _board_out(drill, at)

    @app.get("/api/evac/drills/{drill_id}/priority",
             response_model=list[schemas.PersonRowOut], tags=["board"])
    def get_priority(drill_id: str, limit: int = 50,
                     caller: Caller = Depends(requires(EVAC_READ))):
        drill = drill_or_404(drill_id)
        return [_row_out(r) for r in drill.board(now_ms()).priority(limit=limit)]

    @app.get("/api/evac/drills/{drill_id}/timing", response_model=schemas.TimingOut,
             tags=["board"])
    def get_timing(drill_id: str, caller: Caller = Depends(requires(EVAC_READ))):
        drill = drill_or_404(drill_id)
        timing = drill.timing(now_ms())
        return schemas.TimingOut(
            building=_percentiles(timing.building),
            by_floor={k: _percentiles(v) for k, v in timing.by_floor.items()},
            by_zone={k: _percentiles(v) for k, v in timing.by_zone.items()},
            accountability_completion_s=timing.accountability_completion_s,
            target_p95_s=timing.target_p95_s,
            meets_target=timing.meets_target)

    @app.get("/api/evac/drills/{drill_id}/people/{person_ref:path}/explain",
             response_model=schemas.ExplanationOut, tags=["board"])
    def explain_person(drill_id: str, person_ref: str,
                       caller: Caller = Depends(requires(EVAC_READ))):
        drill = drill_or_404(drill_id)
        explanation = drill.explain(person_ref)
        if explanation is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"{person_ref} is not on this drill's roster")
        return schemas.ExplanationOut(
            subject=explanation.subject, decision=explanation.decision,
            decided_at_ms=explanation.decided_at_ms,
            is_disputed=explanation.is_disputed,
            has_human_confirmation=explanation.has_human_confirmation,
            narrative=explanation.narrate(),
            supporting=[_evidence(e) for e in explanation.supporting],
            contradicting=[_evidence(e) for e in explanation.contradicting],
            context=[_evidence(e) for e in explanation.context])

    @app.get("/api/evac/drills/{drill_id}/zones",
             response_model=list[schemas.ZonePanelOut], tags=["board"])
    def get_zones(drill_id: str, caller: Caller = Depends(requires(EVAC_READ))):
        return [schemas.ZonePanelOut(**p) for p in drill_or_404(drill_id).zone_panels()]

    # --- warden ---------------------------------------------------------------

    @app.get("/api/evac/drills/{drill_id}/warden/{zone_id}",
             response_model=schemas.WardenZoneOut, tags=["warden"])
    def warden_zone(drill_id: str, zone_id: str,
                    caller: Caller = Depends(requires(EVAC_WARDEN))):
        drill = drill_or_404(drill_id)
        if not caller.covers(zone_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"you are not assigned to {zone_id}")
        at = now_ms()
        board = drill.board(at)
        rows = board.by_assembly_zone().get(zone_id, ())
        panels = {p["zone_id"]: p for p in drill.zone_panels()}
        panel = panels.get(zone_id) or drill.warden.sweep(zone_id).summary(set())
        return schemas.WardenZoneOut(
            drill_id=drill_id, zone_id=zone_id, warden_id=caller.user_id,
            cached_at_ms=at, panel=schemas.ZonePanelOut(**panel),
            roster=[_row_out(r) for r in rows],
            system_health=_health(board))

    @app.post("/api/evac/drills/{drill_id}/warden/sync",
              response_model=schemas.WardenSyncOut, tags=["warden"])
    def warden_sync(drill_id: str, body: schemas.WardenSyncIn,
                    caller: Caller = Depends(requires(EVAC_WARDEN))):
        """Drain a device's queue.

        Partial success is the normal case and is reported as such. A device
        that syncs forty actions and has one refused must be able to tell which,
        or a warden's screen shows work that never landed.
        """
        drill = drill_or_404(drill_id)
        accepted = duplicates = 0
        rejected: list[str] = []

        for item in sorted(body.actions, key=lambda a: a.device_seq):
            if not caller.covers(item.zone_id):
                rejected.append(
                    f"seq {item.device_seq}: not assigned to {item.zone_id}")
                continue
            try:
                kind = ActionKind(item.kind)
            except ValueError:
                rejected.append(f"seq {item.device_seq}: unknown action {item.kind}")
                continue

            action = WardenAction(
                kind=kind, warden_id=item.warden_id, device_id=item.device_id,
                zone_id=item.zone_id, ts_ms=item.ts_ms, subject=item.subject,
                identity=item.identity, note=item.note,
                queued_offline=item.queued_offline, synced_at_ms=now_ms())
            try:
                validate(action)
            except WardenActionError as exc:
                rejected.append(f"seq {item.device_seq}: {exc}")
                continue

            queue = drill.warden.device(item.device_id, item.warden_id)
            if item.device_seq < queue.next_seq:
                duplicates += 1
                continue
            queue.next_seq = item.device_seq
            drill.record_warden_action(action)
            accepted += 1

        return schemas.WardenSyncOut(accepted=accepted, duplicates=duplicates,
                                     rejected=rejected)

    @app.post("/api/evac/drills/{drill_id}/warden/headcount",
              response_model=schemas.HeadcountOut, tags=["warden"])
    def warden_headcount(drill_id: str, body: schemas.HeadcountIn,
                         caller: Caller = Depends(requires(EVAC_WARDEN))):
        drill = drill_or_404(drill_id)
        if not caller.covers(body.zone_id):
            raise HTTPException(status.HTTP_403_FORBIDDEN,
                                f"you are not assigned to {body.zone_id}")
        board = drill.board(now_ms())
        rows = board.by_assembly_zone().get(body.zone_id, ())
        system_count = sum(1 for r in rows if r.colour == "GREEN")

        headcount = Headcount(
            zone_id=body.zone_id, warden_id=body.warden_id,
            device_id=body.device_id, ts_ms=body.ts_ms,
            physical_count=body.physical_count, system_count=system_count,
            policy=DEFAULT_POLICY, note=body.note)
        drill.record_headcount(headcount)
        return schemas.HeadcountOut(
            zone_id=headcount.zone_id, physical_count=headcount.physical_count,
            system_count=headcount.system_count, difference=headcount.difference,
            kind=headcount.kind.value, severity=headcount.severity.value,
            missing_from_the_muster_point=headcount.missing_from_the_muster_point,
            summary=headcount.summary(),
            recommended_action=headcount.recommended_action())

    # --- health ---------------------------------------------------------------

    @app.get("/healthz", tags=["health"])
    def healthz():
        """What `scripts/evac_chaos.sh` polls.

        Reports degraded when anything is wrong, so a chaos run can tell the
        difference between a service that stayed up and one that stayed up and
        lied.
        """
        drill = app.state.registry.running()
        if drill is None:
            return {"status": "ok", "degraded": False, "drill": None,
                    "replication_backlog": 0}
        state = drill.ingestor.state
        return {
            "status": "ok",
            "degraded": state.health.is_degraded,
            "blind": state.health.is_blind,
            "drill": drill.drill_id,
            "open_outages": len(state.health.open_now()),
            "events_accepted": state.accepted,
            "duplicates_dropped": state.duplicates_dropped,
            "outstanding_gaps": len(state.tracker.outstanding_gaps()),
            "replication_backlog": 0,
        }

    _mount_frontend(app)
    return app


def _mount_frontend(app: FastAPI) -> None:
    """Serve the command centre and the warden PWA from the same origin.

    Same origin matters for the PWA: a service worker can only control pages on
    its own origin, and a warden's tablet has to start from cache with no
    network at all. Serving the front end from a separate host would mean the
    app cannot install, which is the one thing it must do.
    """
    root = Path(__file__).resolve().parents[3] / "frontend"
    if not root.is_dir():
        return

    @app.get("/static/sw.js", include_in_schema=False)
    def service_worker():
        # Registered before the static mount, which would otherwise shadow it
        # and drop the header. Without Service-Worker-Allowed the browser
        # refuses a worker served from /static/ the scope /evac/, and the PWA
        # silently loses its ability to start offline — the one thing it exists
        # to do, failing in the one way nobody notices until a real drill.
        return FileResponse(
            root / "sw.js", media_type="application/javascript",
            headers={"Service-Worker-Allowed": "/"})

    app.mount("/static", StaticFiles(directory=root), name="static")

    @app.get("/", include_in_schema=False)
    def command_centre():
        return FileResponse(root / "index.html")

    @app.get("/evac/warden", include_in_schema=False)
    def warden():
        return FileResponse(root / "warden.html")


# --- shaping ------------------------------------------------------------------

def _summary(drill: Drill) -> schemas.DrillSummary:
    return schemas.DrillSummary(
        drill_id=drill.drill_id, name=drill.name, site_id=drill.site_id,
        status=drill.status.value, created_ms=drill.created_ms,
        started_ms=drill.started_ms, completed_ms=drill.completed_ms,
        expected=drill.roster.expected_count)


def _row_out(row) -> schemas.PersonRowOut:
    return schemas.PersonRowOut(
        person_ref=row.person_ref, display_name=row.display_name,
        department=row.department,
        assigned_assembly_zone=row.assigned_assembly_zone,
        state=row.state.value, colour=row.colour, reason=row.decision.reason,
        qualifying_evidence=list(row.decision.qualifying_evidence),
        blockers=list(row.decision.blockers),
        last_zone_id=row.last_zone_id, last_camera_id=row.last_camera_id,
        last_seen_ms=row.last_seen_ms,
        needs_human_to_account=row.needs_human_to_account)


def _health(board) -> schemas.HealthOut:
    return schemas.HealthOut(
        degraded=board.health.open_outages > 0,
        blind=board.health.blind_fraction > 0 and board.health.open_outages > 0,
        open_outages=board.health.open_outages,
        total_outages=board.health.total_outages,
        blind_fraction=board.health.blind_fraction,
        longest_blind_ms=board.health.longest_blind_ms,
        caveat=board.health.caveat())


def _board_out(drill: Drill, at: int) -> schemas.BoardOut:
    board = drill.board(at)
    return schemas.BoardOut(
        drill_id=drill.drill_id, now_ms=at, elapsed_ms=drill.elapsed_ms(at),
        status=drill.status.value,
        expected=board.expected, accounted=board.accounted,
        uncertain=board.uncertain, unaccounted=board.unaccounted,
        currently_unobserved=board.currently_unobserved,
        still_evacuating=board.still_evacuating,
        unknown_people=board.unknown_people,
        all_clear=drill.all_clear(at),
        blocking_all_clear=drill.blocking_all_clear(at),
        roster_trustworthy=board.roster_trustworthy,
        health=_health(board),
        rows=[_row_out(r) for r in board.rows])


def _percentiles(summary) -> schemas.PercentilesOut:
    return schemas.PercentilesOut(
        label=summary.label, sample_size=summary.sample_size,
        excluded=summary.excluded, p50=summary.p50, p90=summary.p90,
        p95=summary.p95, p99=summary.p99, maximum=summary.maximum,
        reliable=summary.is_reliable, coverage=summary.coverage,
        caveat=summary.caveat())


def _evidence(item) -> schemas.EvidenceOut:
    return schemas.EvidenceOut(
        ts_ms=item.ts_ms, kind=item.kind.value, stance=item.stance.value,
        summary=item.summary, source=item.source, identity=item.identity,
        is_human=item.is_human)
