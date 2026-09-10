"""Synthetic drill simulator — Phase 1. Drives `app.core` with no cameras.

Generates a synthetic site (floors, exits, assembly zones, cameras with coverage
polygons and blind spots) and 500+ agents with movement models, then injects the
failures that make real accountability hard:

  face loss · poor-quality face · track fragmentation · ID switch · occlusion ·
  camera handoff · duplicate events · out-of-order events · delayed events ·
  camera outage · Redis outage · DB outage · network partition ·
  conflicting identities · visitors · genuinely absent employees ·
  two look-alike employees

Two properties must hold under every injection, and are asserted as property
tests rather than examples:

  * no injection ever yields ACCOUNTED without qualifying evidence;
  * every failure yields DEGRADED or MANUAL_VERIFICATION_REQUIRED, never a
    false ALL CLEAR.
"""
