"""Event ingest — Phase 3.

Consumes `vt:evac:events:<tenant>` from Redis, deduplicates on (source, seq),
emits SEQUENCE_GAP on a hole, and feeds `app.core`. Writes projections to
Postgres and fans out over WebSocket. Edge-to-central replication follows the
store-and-forward *pattern* of `app/vendor/deepstream/outbox.py` and does not
import it; `docs/EVAC120_PROVENANCE.md` says why, and why the distinction
matters to anyone chasing a buffering bug.

Ingest is idempotent: replaying a stream from any point converges to the same
projections.
"""
