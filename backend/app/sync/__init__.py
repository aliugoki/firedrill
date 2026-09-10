"""Bringing VisionTrack's geometry into EVAC-120's model.

Floor plans, zone polygons and camera homographies already exist in VisionTrack,
drawn by whoever set the site up. Redrawing them here would be duplicated work
and, worse, duplicated truth: two sets of polygons that drift apart until an
exit line is in a different place depending on which system you ask.

So the geometry is synced. But it is a **transformation, not a copy**, because
EVAC-120 needs something VisionTrack has no concept of: what a zone is *for*.
"""
