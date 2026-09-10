"""Event ingest — Phase 3.

Consumes `vt:evac:events:<tenant>` from Redis, deduplicates on (source, seq),
emits SEQUENCE_GAP on a hole, and feeds `app.core`. Writes projections to
Postgres and fans out over WebSocket. Edge-to-central replication uses the
store-and-forward pattern vendored in `app/vendor/deepstream/outbox.py`.

Ingest is idempotent: replaying a stream from any point converges to the same
projections.
"""
