"""One name for "the database could not answer".

`events.py` and `drills.py` each declared a `StoreUnavailable`, as different
classes with the same name. Only one of them was ever raised; the other sat in
the module a future `except StoreUnavailable` would most naturally import from,
and would have caught nothing.

The same shape cost a wrong replication credential its ability to stop the
retry loop, and it is worth removing on sight.
"""

from __future__ import annotations


class StoreUnavailable(RuntimeError):
    """A read could not be answered.

    Writes report failure and carry on -- a drill that cannot be persisted still
    runs, because an operator with an evacuation in progress needs the board
    more than the bookkeeping. Reads raise, because every falsy thing a read
    could return is also a legitimate answer: no rows, no drill, nothing
    running. A caller that only counts what came back cannot tell "there were
    none" from "I could not look", and one of those is a building with people
    in it.
    """
