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
