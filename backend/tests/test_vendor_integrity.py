"""Every vendored file imports, and every one still declares where it came from.

Phase 0 gate. The behaviour tests in the sibling files exercise what EVAC-120
actually depends on; this one covers the rest, so a vendored module that nothing
imports yet still cannot rot silently. It also enforces the provenance rule: a
file under app/vendor without a provenance header is either an accidental copy
or an edit that dropped it, and both need catching at the gate rather than in
Phase 3.
"""

import importlib
import pkgutil
from pathlib import Path

import pytest

import app.vendor

VENDOR_ROOT = Path(app.vendor.__file__).parent

VENDORED_MODULES = sorted(
    m.name
    for m in pkgutil.walk_packages([str(VENDOR_ROOT)], prefix="app.vendor.")
    if not m.ispkg
)

VENDORED_FILES = sorted(
    p for p in VENDOR_ROOT.rglob("*.py") if p.name != "__init__.py"
)


def test_the_vendor_tree_is_not_empty():
    # Guards the two parametrised tests below: an empty collection would make
    # them vacuously pass and the gate meaningless.
    assert len(VENDORED_FILES) == 8
    assert len(VENDORED_MODULES) == 8


@pytest.mark.parametrize("module_name", VENDORED_MODULES)
def test_vendored_module_imports(module_name):
    # Catches an import rewrite that was missed or applied to the wrong path.
    assert importlib.import_module(module_name) is not None


@pytest.mark.parametrize("path", VENDORED_FILES, ids=lambda p: p.name)
def test_vendored_file_declares_its_origin(path):
    head = path.read_text().split("\n", 8)
    joined = "\n".join(head)
    assert "VENDORED from" in joined, f"{path.name} has no provenance header"
    assert "docs/EVAC120_PROVENANCE.md" in joined


@pytest.mark.parametrize("module_name", VENDORED_MODULES)
def test_no_vendored_module_reaches_back_into_its_source_repo(module_name):
    # A leftover `from app.modules...` import would resolve in VisionTrack and
    # fail here, or worse, be silently satisfied if this repo ever grows a
    # module of that name. Vendored code must only reference app.vendor.
    source = Path(importlib.import_module(module_name).__file__).read_text()
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import app.", "from app.")):
            assert stripped.startswith(("import app.vendor.", "from app.vendor.")), (
                f"{module_name} still imports its source repo: {stripped}"
            )


class TestTheUsageReviewIsCheckedRatherThanTrusted:
    """`EVAC120_PROVENANCE.md` carries a table headed "What is actually used, a
    review later", introduced with: "Vendoring is a promise about future use,
    and promises drift. Checked against the code rather than against the Phase
    0 intent."

    That check was done once, by hand, and then drifted itself. It credited
    `zones_math.py` to `app/sync/geometry.py`, which mentions the vendored
    coordinate convention in a comment and imports nothing. A review that is
    only true on the day it is written is the Phase 0 rationale again with a
    later date on it, so this computes the answer.
    """

    DOC = (Path(__file__).resolve().parents[2] / "docs"
           / "EVAC120_PROVENANCE.md")

    def real_importers(self) -> dict:
        """Who imports each vendored module, ignoring the vendor tree itself.

        A vendored file importing another vendored file is not use: it is the
        copy bringing its own dependency, and counting it would make every
        module look wanted.
        """
        import ast

        app_root = VENDOR_ROOT.parent
        importers: dict = {m: set() for m in VENDORED_MODULES}
        for path in app_root.rglob("*.py"):
            if "vendor" in path.parts:
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module]
                for name in names:
                    if name in importers:
                        importers[name].add(str(path.relative_to(app_root)))
        return importers

    def review_rows(self) -> dict:
        """The doc's own table, as {filename: line}."""
        text = self.DOC.read_text()
        start = text.index("### What is actually used")
        section = text[start:text.index("\n## ", start)]
        rows = {}
        for line in section.splitlines():
            if not line.startswith("|") or "---" in line:
                continue
            for module in VENDORED_MODULES:
                filename = module.rsplit(".", 1)[-1] + ".py"
                if f"`{filename}`" in line:
                    rows[filename] = line
        return rows

    def test_the_scan_finds_the_table(self):
        # A parse that silently matched nothing would pass everything below.
        assert len(self.review_rows()) >= 6

    def test_every_module_the_code_imports_is_marked_in_use(self):
        rows = self.review_rows()
        wrong = []
        for module, importers in self.real_importers().items():
            filename = module.rsplit(".", 1)[-1] + ".py"
            row = rows.get(filename)
            if row is None or not importers:
                continue
            if "In use" not in row:
                wrong.append(f"{filename} is imported by {sorted(importers)} "
                             f"and the review does not say so")
        assert wrong == []

    def test_nothing_is_claimed_in_use_that_nobody_imports(self):
        # The direction that actually rots: a module drops out of the code and
        # the table keeps its credit.
        rows = self.review_rows()
        importers = self.real_importers()
        wrong = []
        for filename, row in rows.items():
            if "In use" not in row:
                continue
            module = f"app.vendor.visiontrack.{filename[:-3]}"
            alt = f"app.vendor.deepstream.{filename[:-3]}"
            used = importers.get(module) or importers.get(alt) or set()
            if not used:
                wrong.append(f"{filename} is marked In use and nothing "
                             "outside the vendor tree imports it")
        assert wrong == []

    def test_the_row_names_a_real_importer(self):
        """The column says *who*, and naming the wrong file sends a reader
        looking for a dependency that is not there."""
        rows = self.review_rows()
        importers = self.real_importers()
        wrong = []
        for filename, row in rows.items():
            if "In use" not in row:
                continue
            module = f"app.vendor.visiontrack.{filename[:-3]}"
            alt = f"app.vendor.deepstream.{filename[:-3]}"
            used = importers.get(module) or importers.get(alt) or set()
            if not any(f"app/{path}" in row for path in used):
                wrong.append(f"{filename}: row names none of {sorted(used)}")
        assert wrong == []
