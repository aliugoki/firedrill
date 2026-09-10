"""The documentation describes something that exists.

Docs rot in a specific, predictable way: a file is renamed, a module moves, and
a reference in a document that nobody runs quietly starts pointing at nothing.
Six months later someone follows it during an incident.

These are cheap and they catch exactly that.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DOCS = ROOT / "docs"
BACKEND = ROOT / "backend"

#: The other repositories this one documents its relationship with. Checked when
#: they are present on the machine and skipped when they are not: a reference to
#: VisionTrack's source is a real reference worth verifying here, and a test
#: that fails on a laptop without those repos checked out is a test people
#: disable.
EXTERNAL_ROOTS = tuple(
    path for path in (
        Path.home() / "visiontrack" / "visiontrack",
        Path.home() / "visiontrack" / "visiontrack" / "backend",
        Path.home() / "deploy" / "deepstream",
        Path.home() / "deploy" / "attendance-system",
        Path.home() / "deploy" / "attendance-system" / "backend",
        Path.home(),
    ) if path.is_dir()
)

#: Where a path in a document might live inside this repo.
LOCAL_ROOTS = (ROOT, BACKEND, BACKEND / "app", BACKEND / "app" / "vendor",
               ROOT / "frontend")

MARKDOWN = sorted(DOCS.glob("*.md")) + [ROOT / "README.md", ROOT / "CLAUDE.md"]


def backtick_paths(text: str) -> set[str]:
    """Paths mentioned in backticks that look like real files."""
    found = set()
    for token in re.findall(r"`([^`\n]+)`", text):
        token = token.strip()
        if token.endswith((".md", ".py", ".js", ".sh", ".html", ".webmanifest")):
            if " " in token or token.startswith(("http", "<")):
                continue
            found.add(token)
    return found


def resolves(token: str) -> tuple[bool, bool]:
    """Returns (found, was_external)."""
    if token.startswith("~"):
        expanded = Path(token).expanduser()
        return expanded.exists(), True

    for base in LOCAL_ROOTS:
        if (base / token).exists():
            return True, False

    for base in EXTERNAL_ROOTS:
        if (base / token).exists():
            return True, True

    # A bare module name like `identity_fsm.py` is a reference, not a path.
    return "/" not in token, False


class TestEveryDocumentedPathExists:
    @pytest.mark.parametrize("doc", MARKDOWN, ids=lambda p: p.name)
    def test_referenced_files_resolve(self, doc):
        missing = []
        for token in backtick_paths(doc.read_text()):
            found, external = resolves(token)
            if found:
                continue
            if external or EXTERNAL_ROOTS == ():
                # A reference into a repository that is not checked out here is
                # unverifiable, not wrong.
                continue
            missing.append(token)
        assert not sorted(missing), (
            f"{doc.name} references paths that do not exist: {sorted(missing)}")

    def test_the_index_names_every_document(self):
        # A document nobody links to is a document nobody reads.
        index = (DOCS / "EVAC120.md").read_text()
        others = [p.name for p in DOCS.glob("*.md") if p.name != "EVAC120.md"]
        orphans = [name for name in others if name not in index]
        assert not orphans, f"not referenced from EVAC120.md: {orphans}"

    def test_there_are_no_documents_that_only_exist_as_references(self):
        index = (DOCS / "EVAC120.md").read_text()
        referenced = {t for t in backtick_paths(index) if t.startswith("docs/")}
        missing = sorted(t for t in referenced if not (ROOT / t).exists())
        assert not missing, f"EVAC120.md points at missing documents: {missing}"


class TestTheSafetyNoticeIsEverywhereItShouldBe:
    """The system supplements and never replaces certified fire systems. That
    has to appear where someone will actually see it, not only in a README."""

    def test_it_is_in_the_main_document(self):
        assert "never replaces" in (DOCS / "EVAC120.md").read_text()

    def test_it_is_in_the_readme(self):
        assert "never replaces" in (ROOT / "README.md").read_text()

    def test_it_is_in_the_openapi_description(self):
        from app.api.app import create_app

        spec = create_app().openapi()
        assert "never replaces, certified fire" in spec["info"]["description"]

    def test_it_is_on_both_front_ends(self):
        for page in ("index.html", "warden.html"):
            source = (ROOT / "frontend" / page).read_text()
            assert 'id="safety"' in source, f"{page} has no safety notice element"

    def test_it_is_in_both_languages(self):
        i18n = (ROOT / "frontend" / "js" / "i18n.js").read_text()
        assert i18n.count("app.safety_notice") >= 2


class TestUncalibratedClaimsStayHonest:
    """Every provisional threshold says it is provisional. If one of these ever
    reads True without a calibration report behind it, the system is presenting
    invented numbers as validated."""

    def test_no_shipped_config_claims_to_be_calibrated(self):
        from app.core.accountability_fsm import PROVISIONAL_CONFIG as ACC
        from app.core.identity_fsm import PROVISIONAL_CONFIG as IDENT
        from app.core.presence_fsm import PROVISIONAL_CONFIG as PRESENCE
        from app.infra.retention import RetentionPolicy
        from app.reporting.validation import Thresholds
        from app.warden.headcount import DEFAULT_POLICY as HEADCOUNT

        for config in (IDENT, PRESENCE, ACC, HEADCOUNT, RetentionPolicy(),
                       Thresholds()):
            assert config.calibrated is False, f"{type(config).__name__} claims calibration"

    def test_the_calibration_document_says_so_too(self):
        text = (DOCS / "EVAC120_CALIBRATION.md").read_text()
        assert "No threshold in this system has been calibrated" in text

    def test_the_benchmark_document_contains_no_invented_numbers(self):
        # A plausible-looking FPS figure nobody measured is worse than an empty
        # table, because it will be quoted.
        text = (DOCS / "EVAC120_BENCHMARKS.md").read_text()
        assert "Nothing here has been measured" in text
        assert "No estimate appears in this file" in text
