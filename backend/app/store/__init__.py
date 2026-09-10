"""Durable storage for the edge node.

One table earns its place above all others: `evac_events`. It is the source of
truth, and everything else in the system is derived from it — presence,
identity, the ledger, the board, the report. If the events survive, a node can
be rebuilt from nothing. If they do not, nothing else being durable helps.

So the priorities are, in order: the event log, the audit log, and then the
things that are merely inconvenient to lose.
"""
