# ---------------------------------------------------------------------------
# VENDORED from VisionTrack @ 3592c72
#   backend/app/modules/alerts/evaluator.py
# Copied 2026-09-10 for EVAC-120. Hold-time + hysteresis. Pure stdlib.
# Do NOT edit to fix an upstream bug -- fix it upstream and re-vendor.
# Local changes, if any, are listed in docs/EVAC120_PROVENANCE.md.
# ---------------------------------------------------------------------------
"""Rule evaluator.

Pure functions: given an observation (current zone occupancy or entry
event) and per-rule state from Redis, decide whether to fire.

Hold-time semantics (sustained condition):
  - On each tick where the condition is TRUE:
      if state.condition_since_ms is unset:
          state.condition_since_ms = now
      if (now - condition_since_ms) >= hold_time_s * 1000:
          if not state.armed:                # first time fire
              FIRE
              state.armed = True
              state.last_fire_ms = now
  - On each tick where the condition is FALSE:
      state.condition_since_ms = None
      # Don't immediately disarm — wait until the condition has stayed
      # false for hold_time_s seconds (rearm window). This is hysteresis
      # — without it a metric oscillating around the threshold spams.
      if state.armed and (now - last_fire_ms) >= rearm_ms:
          state.armed = False

Per-rule kinds:
  occupancy_max — fire when count > threshold and sustained
  occupancy_min — fire when count < threshold and sustained
  entry         — edge-triggered: fire when count goes 0 → ≥1
                   (handled at the consumer layer; no sustained logic)
  dwell         — DEFERRED to Step 8 (requires per-person homography)

`entry` is special. It's not "current count" but "transition event" —
when track presence on a camera-in-zone goes from 0 to ≥1. The
evaluator passes through; the calling layer decides if THIS tick is
an edge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


# Rearm window: once an alert fires, the condition must be false for at
# least this long (in addition to any hold_time_s) before the rule can
# fire again. Prevents oscillation spam.
REARM_MULTIPLIER = 2  # rearm_ms = hold_time_s * REARM_MULTIPLIER * 1000


@dataclass
class RuleState:
    """Per-(zone, rule) state we persist in Redis between ticks."""
    condition_since_ms: int | None
    armed: bool
    last_fire_ms: int | None


@dataclass
class EvaluationOutcome:
    """What the evaluator decides for a single rule on a single tick."""
    fire: bool
    new_state: RuleState
    # For observability — why we did/didn't fire. Logged only.
    reason: str


def evaluate_occupancy_rule(
    *,
    kind: Literal["occupancy_max", "occupancy_min"],
    threshold: float,
    hold_time_s: int,
    current_count: int,
    state: RuleState,
    now_ms: int,
) -> EvaluationOutcome:
    """Evaluate a single occupancy_max / occupancy_min rule for one tick.

    The caller invokes this once per evaluation tick per rule, passing
    the current zone count and the rule's last persisted state.
    Returns whether to fire + the next state to persist.
    """
    if kind == "occupancy_max":
        condition_now = current_count > threshold
    elif kind == "occupancy_min":
        condition_now = current_count < threshold
    else:
        # Defensive — caller should have filtered to these two kinds
        return EvaluationOutcome(
            fire=False, new_state=state, reason=f"unknown kind {kind}"
        )

    hold_ms = hold_time_s * 1000
    rearm_ms = max(hold_ms * REARM_MULTIPLIER, 5_000)  # minimum 5s rearm

    if condition_now:
        since = state.condition_since_ms or now_ms
        elapsed = now_ms - since
        next_state = RuleState(
            condition_since_ms=since,
            armed=state.armed,
            last_fire_ms=state.last_fire_ms,
        )

        if elapsed >= hold_ms and not state.armed:
            next_state.armed = True
            next_state.last_fire_ms = now_ms
            return EvaluationOutcome(
                fire=True,
                new_state=next_state,
                reason=(
                    f"condition_held_for_{elapsed}ms "
                    f"(threshold={hold_ms}ms count={current_count})"
                ),
            )
        return EvaluationOutcome(
            fire=False,
            new_state=next_state,
            reason=(
                f"condition_true_but_armed_or_not_held "
                f"({elapsed}ms < {hold_ms}ms or armed={state.armed})"
            ),
        )

    # Condition false on this tick — possibly disarm
    next_armed = state.armed
    if state.armed and state.last_fire_ms is not None:
        if (now_ms - state.last_fire_ms) >= rearm_ms:
            next_armed = False
    next_state = RuleState(
        condition_since_ms=None,
        armed=next_armed,
        last_fire_ms=state.last_fire_ms,
    )
    return EvaluationOutcome(
        fire=False,
        new_state=next_state,
        reason=f"condition_false (count={current_count})",
    )


def evaluate_entry_rule(
    *,
    threshold: float,
    prev_count: int,
    current_count: int,
    state: RuleState,
    now_ms: int,
) -> EvaluationOutcome:
    """Evaluate an entry rule.

    Fires when occupancy transitions from 0 to ≥1 — i.e. someone has
    entered the zone. Threshold is interpreted as "minimum number of
    new arrivals to trigger" — typically 1.

    `prev_count` is what we saw on the previous tick. The caller
    maintains this in Redis state separately from RuleState (because
    it's shared across all rules on the same zone — only one count
    measurement per zone per tick).

    Rearm: an entry rule rearms as soon as the zone goes empty again.
    We use the same `state.armed` flag — armed means "we already fired
    for this occupancy span; don't fire again until the zone clears."
    """
    transitioned_in = prev_count < threshold and current_count >= threshold

    if transitioned_in and not state.armed:
        return EvaluationOutcome(
            fire=True,
            new_state=RuleState(
                condition_since_ms=now_ms,
                armed=True,
                last_fire_ms=now_ms,
            ),
            reason=f"entry_transition prev={prev_count} now={current_count}",
        )

    # Zone went empty → ready for the next entry
    if current_count == 0 and state.armed:
        return EvaluationOutcome(
            fire=False,
            new_state=RuleState(
                condition_since_ms=None,
                armed=False,
                last_fire_ms=state.last_fire_ms,
            ),
            reason="zone_cleared_rearmed",
        )

    # No-op state preservation
    return EvaluationOutcome(
        fire=False,
        new_state=state,
        reason=(
            f"no_entry_edge prev={prev_count} now={current_count} "
            f"armed={state.armed}"
        ),
    )
