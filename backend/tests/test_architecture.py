"""Rules from CLAUDE.md, checked against the tree rather than trusted.

Each of these is a sentence somebody wrote down as a constraint and which no
unit test can reach: they are properties of the whole codebase, not of any
function in it. They cost milliseconds and they stop a rule decaying into a
paragraph nobody has verified since it was written.
"""

from __future__ import annotations

import ast
import pathlib
import sys

import pytest

BACKEND = pathlib.Path(__file__).resolve().parents[1]
APP = BACKEND / "app"


def modules(root: pathlib.Path, *, include_vendor: bool = False):
    for path in sorted(root.rglob("*.py")):
        if not include_vendor and "vendor" in path.parts:
            continue
        yield path


def imported_names(path: pathlib.Path) -> list[str]:
    names = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


class TestTheCoreStaysPure:
    """"`backend/app/core/` imports nothing but the standard library and numpy.
    If a module there needs a connection, it belongs in `ingest/` or `api/`."

    The state machines are the part that has to be reasoned about, and a core
    that can open a socket is a core that has to be mocked to be tested.
    """

    def test_nothing_outside_the_standard_library_and_numpy(self):
        offenders = []
        for path in modules(APP / "core"):
            for name in imported_names(path):
                root = name.split(".")[0]
                if root in sys.stdlib_module_names or root == "numpy":
                    continue
                if root == "app" and name.startswith("app.core"):
                    continue
                offenders.append(f"{path.relative_to(BACKEND)} imports {name}")
        assert offenders == []

    def test_the_scan_is_actually_looking_at_something(self):
        assert len(list(modules(APP / "core"))) >= 8


class TestNoFieldCollapsesTheThreeMachines:
    """Invariant 4. Identity, presence and accountability are three separate
    state machines, and there is no `is_evacuated` boolean anywhere.

    One flag would be the whole design undone: a value somebody can set, read
    later, and never reconstruct.
    """

    def test_no_such_field_exists(self):
        offenders = []
        for path in modules(APP):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                target = None
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    target = node.target.id
                elif isinstance(node, ast.Assign):
                    target = next((t.id for t in node.targets
                                   if isinstance(t, ast.Name)), None)
                if target in ("is_evacuated", "evacuated", "is_safe_flag"):
                    offenders.append(f"{path.relative_to(BACKEND)}: {target}")
        assert offenders == []


class TestAccountedHasExactlyTwoRoutesIn:
    """"`ACCOUNTED` requires assembly-zone presence AND (confirmed identity OR
    warden confirmation). Nothing else may set it."

    Two constructions, both in the accountability machine. A third anywhere is
    either a new route somebody has to justify or a bypass.
    """

    def constructions(self) -> list[str]:
        found = []
        for path in modules(APP):
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not (isinstance(node.func, ast.Name)
                        and node.func.id == "Decision"):
                    continue
                values = list(node.args) + [kw.value for kw in node.keywords]
                for value in values:
                    if (isinstance(value, ast.Attribute)
                            and value.attr == "ACCOUNTED"):
                        found.append(
                            f"{path.relative_to(BACKEND)}:{node.lineno}")
        return found

    def test_there_are_exactly_two(self):
        assert len(self.constructions()) == 2, self.constructions()

    def test_both_are_in_the_accountability_machine(self):
        for where in self.constructions():
            assert where.startswith("app/core/accountability_fsm.py")


class TestNothingReachesIntoAnotherRepository:
    """"Reusable code from VisionTrack and DeepStream is vendored into
    `backend/app/vendor/`, never imported across repos."

    An import that resolves on a developer's laptop and not on the edge node is
    a deployment that fails at the first drill.
    """

    def test_no_cross_repo_imports(self):
        offenders = []
        for path in modules(APP, include_vendor=True):
            for name in imported_names(path):
                root = name.split(".")[0]
                if root in ("visiontrack", "deepstream", "facetrack",
                            "attendance_system"):
                    offenders.append(f"{path.relative_to(BACKEND)}: {name}")
        assert offenders == []


class TestVendoredFilesSayWhereTheyCameFrom:
    """"Do not edit a vendored file to fix an upstream bug. Fix it upstream,
    re-vendor, record it in docs/EVAC120_PROVENANCE.md."

    That is only followable if each file still says what it is a copy of.
    """

    def test_every_vendored_module_carries_its_provenance(self):
        vendored = [p for p in (APP / "vendor").rglob("*.py")
                    if p.name != "__init__.py"]
        assert len(vendored) >= 6
        for path in vendored:
            head = "\n".join(path.read_text().splitlines()[:12])
            assert "VENDORED from" in head, path.relative_to(BACKEND)
            assert "Do NOT edit" in head, path.relative_to(BACKEND)


class TestSchemaChangesGoThroughAlembic:
    """"Alembic only for schema. Never manual DDL."

    The exception is the replication outbox, which is a local SQLite buffer on
    the edge node's own disk rather than part of the schema: it is created on
    first use because it has to exist before anything can be persisted at all.
    """

    ALLOWED = {"app/ingest/replication.py", "app/vendor/deepstream/outbox.py"}

    def test_no_ddl_outside_alembic(self):
        offenders = []
        for path in modules(APP, include_vendor=True):
            relative = str(path.relative_to(BACKEND))
            if relative in self.ALLOWED:
                continue
            text = path.read_text().upper()
            if any(statement in text for statement in
                   ("CREATE TABLE", "ALTER TABLE", "DROP TABLE")):
                offenders.append(relative)
        assert offenders == []

    def test_the_allowed_ones_are_still_buffers_not_schema(self):
        # If either of these grows a table that is not the outbox, the
        # exemption stops being true and this says so.
        for relative in self.ALLOWED:
            text = (BACKEND / relative).read_text()
            assert text.upper().count("CREATE TABLE") == 1, relative
            assert "pending" in text


class TestEveryModuleImports:
    """A module nothing imports is a module whose syntax nobody has checked.

    Coverage cannot see this: it reports absent, not zero, for a file no test
    ever loaded -- which is how two dead readers and two entry points went
    unnoticed.
    """

    @pytest.mark.parametrize(
        "dotted",
        [str(p.relative_to(BACKEND))[:-3].replace("/", ".")
         for p in modules(APP) if p.name != "__init__.py"])
    def test_it_can_be_imported(self, dotted):
        __import__(dotted)
