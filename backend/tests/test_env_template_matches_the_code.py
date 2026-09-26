"""`.env.example` against what the code actually reads.

An operator's first act is copying this file and filling it in, so a variable
it omits is one they never set and a variable it lists that nothing reads is
one they set and trust. Both were true here:

  * `EVAC_ROSTER_FILE` is the only thing that supplies a roster and was not in
    the template at all, while `EVAC_FACETRACK_URL` -- which no client reads --
    was, and satisfied the startup check. A node configured exactly as written
    reported no roster problem at boot and refused to create a drill.
  * `EVAC_ASSEMBLY_ZONES` decides which zones are muster points, and half of
    what ACCOUNTED requires is standing at one. Also absent.
  * `EVAC_OUTBOX_FLUSH_SEC` is described in `EVAC120_DEPLOYMENT.md` as the
    producer half of the recovery point objective, with advice to shorten it.
    Nothing read it.

Both directions are checked. A variable that is deliberately not read yet is
allowed, and has to say so in a comment above it -- which is the existing
convention in that file, applied consistently rather than to some of them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / ".env.example"
APP = ROOT / "backend" / "app"

#: A marker in the comment block above a variable saying it is not wired yet.
#: Kept as the file's own words rather than a new convention.
UNWIRED = "NOT READ"


def declared() -> dict:
    """Every `NAME=` in the template, with the comment block above it."""
    out, comment = {}, []
    for line in TEMPLATE.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            comment.append(stripped)
            continue
        match = re.match(r"^([A-Z][A-Z0-9_]*)=", stripped)
        if match:
            out[match.group(1)] = "\n".join(comment)
        elif not stripped:
            # A blank line ends a block. A variable does not: the file groups
            # related settings under one comment, and "the three below" means
            # all three.
            comment = []
    return out


def read_by_code() -> set:
    names = set()
    for path in APP.rglob("*.py"):
        names |= set(re.findall(r'["\'](EVAC_[A-Z0-9_]+)["\']', path.read_text()))
    return names


def test_the_scan_found_both_sides():
    # Guards everything below: an empty set either way would pass vacuously.
    assert len(declared()) > 15, "the template scan found almost nothing"
    assert len(read_by_code()) > 10, "the code scan found almost nothing"


@pytest.mark.parametrize("name", sorted(read_by_code()))
def test_every_variable_the_code_reads_is_in_the_template(name):
    # The direction that bites hardest: a setting nobody knows to set, whose
    # absence is found at the first drill.
    assert name in declared(), (
        f"{name} is read by the code and absent from .env.example, so an "
        "operator filling in that file will never set it")


@pytest.mark.parametrize("name", sorted(declared()))
def test_every_variable_in_the_template_is_read_or_says_it_is_not(name):
    rows = declared()
    if name in read_by_code():
        return
    assert UNWIRED in rows[name], (
        f"{name} is in .env.example and nothing reads it, and the comment "
        f"above it does not say so. An operator will set it and believe it "
        f"did something. Either wire it up or mark it '{UNWIRED}'.")
