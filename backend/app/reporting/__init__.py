"""Post-drill reporting and validation.

The hard problem here is not arithmetic. It is that **a real drill has no
oracle**. In simulation the agents know where they went, so "false accounted"
is a set difference. In a building on a Tuesday afternoon nobody has that
list — which is precisely why the warden's physical roll-call exists.

So validation is defined against the manual roll-call rather than against
truth, and the report says so. Everything downstream follows from that choice:
a drill where the wardens did not complete their sweeps cannot validate
anything, however good the system's own numbers look, and the report returns
INCONCLUSIVE rather than a pass.
"""
