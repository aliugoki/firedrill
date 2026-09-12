"""Claims in `docs/` that a test can check, checked.

Most of a design document is prose nobody can verify mechanically, and that is
fine. Some of it is not: a table of states, a list of configuration parameters,
a route named in a security section. Those drift exactly as silently as code
does and with less to catch them, which `EVAC120_PROVENANCE.md` demonstrated --
its own "checked against the code rather than against the Phase 0 intent"
review had itself gone stale, crediting a module to a file that imports nothing
from it.

So the checkable parts are checked here. This is not an attempt to verify the
documents; it is an attempt to stop the specific sentences that name something
in the code from outliving it.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

DOCS = Path(__file__).resolve().parents[2] / "docs"


def read(name: str) -> str:
    return (DOCS / name).read_text()


class TestEveryStateIsDocumented:
    """A state a machine can reach and no document names is a state nobody
    reviewing the design knows exists."""

    def enums(self):
        from app.core.accountability_fsm import AccountabilityState
        from app.core.identity_fsm import IdentityState
        from app.core.presence_fsm import PresenceState, ZoneKind

        return {"IdentityState": IdentityState,
                "PresenceState": PresenceState,
                "AccountabilityState": AccountabilityState,
                "ZoneKind": ZoneKind}

    def test_the_scan_has_something_to_check(self):
        assert sum(len(e) for e in self.enums().values()) >= 18

    @pytest.mark.parametrize("name", ["IdentityState", "PresenceState",
                                      "AccountabilityState", "ZoneKind"])
    def test_each_member_appears_in_the_state_machine_document(self, name):
        doc = read("EVAC120_STATE_MACHINES.md")
        missing = [m.value for m in self.enums()[name] if f"`{m.value}`" not in doc]
        assert missing == [], f"{name}: {missing} are not in the document"


class TestEveryThresholdIsDocumented:
    """"Every threshold is configuration", says the document, and then lists
    them. A parameter added to a config and not to that table is a number
    somebody will install without knowing it exists."""

    def configs(self):
        from app.core.accountability_fsm import AccountabilityConfig
        from app.core.identity_fsm import IdentityConfig
        from app.core.presence_fsm import PresenceConfig

        return (IdentityConfig, PresenceConfig, AccountabilityConfig)

    def test_every_field_is_named(self):
        doc = read("EVAC120_STATE_MACHINES.md")
        # `calibrated` and `source` are on every config and are about the
        # config rather than parameters of the machine.
        bookkeeping = {"calibrated", "source"}
        missing = []
        for config in self.configs():
            for field in dataclasses.fields(config):
                if field.name in bookkeeping:
                    continue
                if f"`{field.name}`" not in doc:
                    missing.append(f"{config.__name__}.{field.name}")
        assert missing == []

    def test_the_scan_found_the_parameters(self):
        total = sum(len(dataclasses.fields(c)) for c in self.configs())
        assert total >= 13


class TestARouteNamedInADocumentExists:
    """A security document that names an endpoint is telling a reader where a
    control lives. Naming one that was renamed or never built sends them
    looking for it.

    Only one route is written out across all the documents today, which is why
    the floor below is one rather than a round number: the documents describe
    behaviour and invariants, not the HTTP surface. That one is the audit
    route, and it is worth pinning because the section naming it exists to say
    the log can be read at all -- which until recently it could not.
    """

    def routes(self) -> set:
        from app.api.app import create_app
        from app.drill import DrillRegistry

        app = create_app(registry=DrillRegistry())
        return {getattr(r, "path", "") for r in app.routes}

    def documented(self) -> set:
        import re

        found = set()
        for path in sorted(DOCS.glob("*.md")):
            # The method may sit inside the backticks: `GET /api/evac/...`.
            for match in re.findall(r"`(?:[A-Z]+ )?(/api/evac/[^`\s]+)`",
                                    path.read_text()):
                match = match.rstrip(".,")
                # `/api/evac/*` names the surface, not a route.
                if match.endswith(("*", "/")):
                    continue
                found.add(match)
        return found

    def test_the_scan_found_a_route(self):
        assert self.documented(), "no route is written out in any document"

    def test_the_audit_route_is_documented_and_mounted(self):
        # The section naming it exists to say the log can be read at all.
        path = "/api/evac/drills/{drill_id}/audit"
        assert path in self.documented()
        assert path in self.routes()

    def test_every_documented_route_is_mounted(self):
        mounted = self.routes()
        missing = [path for path in self.documented() if path not in mounted]
        assert missing == [], f"documented and not mounted: {missing}"


class TestEveryReasonCodeIsWordedInBothLanguages:
    """The server decides the codes and the browser holds the words.

    A code added on one side and not the other renders as `reason.TRACK_LOST`
    under somebody's name, or falls back to an English sentence on an Arabic
    tablet -- the failure being avoided. Neither language's table can see the
    Python enum, so this is the seam and it is checked from here.
    """

    def codes(self) -> set:
        from app.core.accountability_fsm import ReasonCode

        return {code.value for code in ReasonCode}

    def tables(self) -> dict:
        """The two string tables, parsed out of `i18n.js`.

        Read rather than imported, because the frontend has no build step and
        no Python can execute it. The shape is a literal object of quoted keys,
        which is stable enough to scan and obvious enough to fix if it changes.
        """
        import re

        source = (DOCS.parent / "frontend" / "js" / "i18n.js").read_text()
        tables = {}
        for language in ("en", "ar"):
            start = source.index(f"  {language}: {{")
            end = source.index("\n  },", start)
            tables[language] = set(
                re.findall(r"'(reason\.[A-Za-z_]+)'", source[start:end]))
        return tables

    def test_the_scan_found_both_tables(self):
        tables = self.tables()
        assert len(tables["en"]) > 10 and len(tables["ar"]) > 10

    def test_the_two_tables_carry_the_same_reason_keys(self):
        tables = self.tables()
        assert tables["en"] == tables["ar"]

    def test_every_code_the_server_can_send_has_words(self):
        worded = {key.split(".", 1)[1] for key in self.tables()["en"]}
        missing = sorted(self.codes() - worded)
        assert missing == [], f"no wording for {missing}"

    def test_nothing_is_worded_for_a_code_that_cannot_arrive(self):
        # A string nobody will ever show is a translation somebody paid for and
        # a line the next reader has to work out.
        worded = {key.split(".", 1)[1] for key in self.tables()["en"]}
        # These are the pieces a sentence is assembled from rather than codes.
        parts = {"last_seen", "on_camera", "seconds_in",
                 "ASSEMBLY_WITH_IDENTITY_STALE"}
        extra = sorted(worded - self.codes() - parts)
        assert extra == [], f"worded but unreachable: {extra}"
