#!/usr/bin/env bash
#
# Container-level chaos for EVAC-120.
#
# The pytest suite in backend/tests/chaos proves how the software responds to
# each failure. This proves the failures are the ones that actually happen: it
# kills real containers and watches what the edge node does.
#
# The assertions here are coarser on purpose. A test can assert that a board
# refuses an all-clear; this can only assert that the service stayed up, kept
# answering, and reported itself degraded. Both are worth having, and neither
# substitutes for the other.
#
# Usage:
#   scripts/evac_chaos.sh                    run every scenario
#   scripts/evac_chaos.sh camera redis       run named scenarios
#   DRY_RUN=1 scripts/evac_chaos.sh          list what would be killed
#
set -uo pipefail

HEALTH_URL="${EVAC_HEALTH_URL:-http://localhost:8100/healthz}"
OUTAGE_SECONDS="${OUTAGE_SECONDS:-45}"
SETTLE_SECONDS="${SETTLE_SECONDS:-20}"
DRY_RUN="${DRY_RUN:-0}"

PASSED=0; FAILED=0; SKIPPED=0
FAILURES=()

log()  { printf '%s  %s\n' "$(date -u +%H:%M:%S)" "$*"; }
pass() { PASSED=$((PASSED+1)); printf '  \033[32mPASS\033[0m  %s\n' "$*"; }
fail() { FAILED=$((FAILED+1)); FAILURES+=("$*"); printf '  \033[31mFAIL\033[0m  %s\n' "$*"; }
skip() { SKIPPED=$((SKIPPED+1)); printf '  \033[33mSKIP\033[0m  %s\n' "$*"; }

# --- helpers ----------------------------------------------------------------

container_exists() { docker ps -a --format '{{.Names}}' | grep -qx "$1"; }

health_json() { curl -sf --max-time 5 "$HEALTH_URL" 2>/dev/null; }

# The two things that matter during an outage: the service is still answering,
# and it is telling the truth about being degraded. A service that stays up and
# reports healthy while blind is the failure this whole suite exists to catch.
assert_degraded() {
  local scenario="$1" body
  body="$(health_json)" || { fail "$scenario: health endpoint stopped answering"; return; }
  if grep -q '"degraded"[[:space:]]*:[[:space:]]*true' <<<"$body"; then
    pass "$scenario: service stayed up and reported itself degraded"
  else
    fail "$scenario: service reported healthy while $scenario was down"
  fi
}

assert_recovered() {
  local scenario="$1" body
  body="$(health_json)" || { fail "$scenario: no health response after recovery"; return; }
  if grep -q '"degraded"[[:space:]]*:[[:space:]]*false' <<<"$body"; then
    pass "$scenario: recovered cleanly"
  else
    fail "$scenario: still degraded ${SETTLE_SECONDS}s after recovery"
  fi
}

# Kill a container, hold it down, bring it back, check both transitions.
kill_and_restore() {
  local scenario="$1" container="$2"
  log "scenario: $scenario  (container $container)"

  if ! container_exists "$container"; then
    skip "$scenario: container $container is not present on this host"
    return
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    skip "$scenario: dry run, would stop $container for ${OUTAGE_SECONDS}s"
    return
  fi

  docker stop "$container" >/dev/null 2>&1 || { fail "$scenario: could not stop $container"; return; }
  sleep "$OUTAGE_SECONDS"
  assert_degraded "$scenario"

  docker start "$container" >/dev/null 2>&1 || { fail "$scenario: could not restart $container"; return; }
  sleep "$SETTLE_SECONDS"
  assert_recovered "$scenario"
}

# --- scenarios ---------------------------------------------------------------
# Each corresponds to a class in backend/tests/chaos/test_chaos.py.

scenario_camera()  { kill_and_restore "camera"   "${EVAC_CAMERA_CONTAINER:-mediamtx}"; }
scenario_pipeline(){ kill_and_restore "pipeline" "${EVAC_DS_CONTAINER:-vt-ai-worker-ds}"; }
scenario_redis()   { kill_and_restore "redis"    "${EVAC_REDIS_CONTAINER:-vt-redis}"; }
scenario_postgres(){ kill_and_restore "postgres" "${EVAC_DB_CONTAINER:-firedrill-postgres}"; }

# The network partition is not a container stop: the edge must keep running with
# no route to central at all, which is the zero-Internet requirement.
scenario_network() {
  local scenario="network"
  log "scenario: $scenario  (partition edge from central)"
  local container="${EVAC_EDGE_CONTAINER:-firedrill-edge}"

  if ! container_exists "$container"; then
    skip "$scenario: container $container is not present on this host"
    return
  fi
  if [[ "$DRY_RUN" == "1" ]]; then
    skip "$scenario: dry run, would disconnect $container from its network"
    return
  fi

  local network
  network="$(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' "$container" 2>/dev/null | head -1)"
  if [[ -z "$network" ]]; then
    skip "$scenario: could not determine the network for $container"
    return
  fi

  docker network disconnect "$network" "$container" >/dev/null 2>&1 \
    || { fail "$scenario: could not disconnect $container"; return; }
  sleep "$OUTAGE_SECONDS"

  # The edge is the authority during a drill. A partition from central must not
  # degrade it: it should keep deciding and buffer for later.
  if health_json >/dev/null; then
    pass "$scenario: edge kept answering while partitioned from central"
  else
    fail "$scenario: edge stopped answering when it lost central"
  fi

  docker network connect "$network" "$container" >/dev/null 2>&1
  sleep "$SETTLE_SECONDS"

  local body backlog
  body="$(health_json)"
  backlog="$(grep -o '"replication_backlog"[[:space:]]*:[[:space:]]*[0-9]*' <<<"$body" | grep -o '[0-9]*$')"
  if [[ -n "$backlog" && "$backlog" == "0" ]]; then
    pass "$scenario: replication backlog drained to zero after reconnect"
  else
    fail "$scenario: backlog is ${backlog:-unknown} ${SETTLE_SECONDS}s after reconnect"
  fi
}

scenario_central() {
  local scenario="central"
  log "scenario: $scenario  (central replica unreachable)"
  kill_and_restore "$scenario" "${EVAC_CENTRAL_CONTAINER:-firedrill-central}"
}

# --- runner -------------------------------------------------------------------

ALL=(camera pipeline redis postgres network central)
REQUESTED=("${@:-}")
[[ -z "${REQUESTED[0]:-}" ]] && REQUESTED=("${ALL[@]}")

log "EVAC-120 chaos suite"
log "health endpoint: $HEALTH_URL"
log "outage ${OUTAGE_SECONDS}s, settle ${SETTLE_SECONDS}s"
[[ "$DRY_RUN" == "1" ]] && log "DRY RUN: nothing will be killed"
echo

for name in "${REQUESTED[@]}"; do
  if ! declare -F "scenario_${name}" >/dev/null; then
    skip "unknown scenario: ${name}"
    continue
  fi
  "scenario_${name}"
  echo
done

log "passed ${PASSED}, failed ${FAILED}, skipped ${SKIPPED}"
if (( FAILED > 0 )); then
  echo
  log "failures:"
  printf '  - %s\n' "${FAILURES[@]}"
  exit 1
fi
exit 0
