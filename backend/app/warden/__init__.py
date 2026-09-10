"""Warden evidence: what a human physically established, and under what conditions.

Invariant 9 makes a warden's confirmation the final authority. That authority is
only worth having if the record says who exercised it, when, on which device,
and whether they were online at the time. A confirmation with no provenance is
indistinguishable from a guess, and it would be the highest-trust evidence in
the system.

Three things live here:

    actions.py    what a warden can assert, and what each assertion means
    headcount.py  physical count against system count, and the tolerance rule
    sweep.py      zone sweep state, escalation, and when a zone is finished
"""
