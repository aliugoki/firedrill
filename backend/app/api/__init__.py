"""HTTP + WebSocket surface — Phase 3 and 4. Serves /api/evac/*.

All read models are projections over the event stream. No endpoint computes
accountability; it reads what `app.core` decided and `app.ingest` stored.
"""
