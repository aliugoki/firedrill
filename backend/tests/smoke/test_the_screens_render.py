"""The two screens, in a real browser, against a real server.

Every other frontend test injects its own `fetch`. That is the right shape for
testing logic and it is why `Api` shipped with a default that could not work:
the handle was stored on the instance and called as `this._fetch(...)`, the
browser saw an `Api` as the receiver and answered "Illegal invocation", and
both screens said the server was down while it answered 200 to curl. A hundred
and forty-two tests passed throughout.

So this one runs the product. It starts the API with a populated drill, drives
headless Chrome over the DevTools protocol, and asserts the screens show the
drill rather than an empty shell. It is the only test here that can fail on a
line whose behaviour differs between Node and a browser.

Skipped, not failed, where Chrome or Node is absent. An edge node has neither
and does not need them, and a gate that cannot run on the machine in front of
you is a gate people learn to ignore.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PROBE = Path(__file__).parent / "probe.mjs"

CHROME = next((shutil.which(name) for name in
               ("google-chrome", "google-chrome-stable", "chromium",
                "chromium-browser")
               if shutil.which(name)), None)
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    not CHROME or not NODE,
    reason="needs a Chrome and a Node on PATH; neither belongs on an edge node")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def server():
    """The demo drill, served on a port of its own."""
    import uvicorn

    demo = _load("serve_demo", ROOT / "scripts" / "serve_demo.py")
    app, _, swept = demo.build()
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    if not server.started:
        pytest.skip("the demo server did not start")
    yield f"http://127.0.0.1:{port}", swept
    server.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(scope="module")
def browser():
    port = _free_port()
    profile = Path(__file__).parent / ".chrome-profile"
    process = subprocess.Popen(
        [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--hide-scrollbars", f"--remote-debugging-port={port}",
         f"--user-data-dir={profile}", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import urllib.request

    for _ in range(60):
        with contextlib.suppress(Exception):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version",
                                   timeout=1)
            break
        time.sleep(0.5)
    else:
        process.kill()
        pytest.skip("headless Chrome did not come up")
    yield port
    process.kill()
    process.wait(timeout=10)
    shutil.rmtree(profile, ignore_errors=True)


def probe(browser_port: int, url: str, selectors: dict) -> dict:
    result = subprocess.run(
        [NODE, str(PROBE), str(browser_port), url, json.dumps(selectors)],
        capture_output=True, text=True, timeout=120, cwd=str(ROOT))
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


class TestTheCommandCentre:

    @pytest.fixture(scope="class")
    def page(self, server, browser):
        base, _ = server
        return probe(browser, f"{base}/?drill=demo", {
            "verdict": "#verdict",
            "tiles": "#tiles",
            "priority": "#priority",
            "zones": "#zones",
            "stale": "#stale",
        })

    def test_the_page_threw_nothing(self, page):
        assert page["errors"] == []

    def test_it_is_not_still_waiting_for_the_server(self, page):
        # The exact wording both screens showed for as long as the default
        # `fetch` handle was the bare global.
        assert "No data has been received" not in (page["text"]["stale"] or "")
        assert "no data has been received" not in page["text"]["verdict"].lower()

    def test_the_counts_are_the_drill(self, page):
        # 212 expected is this fixture's roster. A board showing zeroes renders
        # identically to one that never loaded.
        assert "212" in page["text"]["tiles"]
        assert "Expected" in page["text"]["tiles"]

    def test_the_verdict_names_what_is_blocking(self, page):
        text = page["text"]["verdict"]
        assert "NOT ALL CLEAR" in text
        assert "not accounted for" in text

    def test_people_are_listed_worst_first(self, page):
        priority = page["text"]["priority"]
        assert "Unaccounted for" in priority
        assert priority.index("Unaccounted for") < priority.index("Uncertain")

    def test_the_assembly_panel_shows_both_zones(self, page):
        zones = page["text"]["zones"]
        assert "assembly-north" in zones and "assembly-south" in zones

    def test_a_warden_gone_quiet_is_named_there(self, page):
        # One tablet last spoke minutes ago and the other never connected.
        zones = page["text"]["zones"]
        assert "silent" in zones.lower()
        assert "no warden device has connected" in zones.lower()


class TestTheWardenScreen:

    @pytest.fixture(scope="class")
    def page(self, server, browser):
        base, swept = server
        return probe(
            browser,
            f"{base}/evac/warden?drill=demo&zone={swept}&warden=warden-1",
            {"sync": "#sync", "tiles": "#zone-tiles", "tabs": "#tabs"})

    def test_the_page_threw_nothing(self, page):
        assert page["errors"] == []

    def test_the_zone_counts_arrived(self, page):
        tiles = page["text"]["tiles"]
        assert "Expected" in tiles and "Confirmed" in tiles

    def test_the_device_is_not_reporting_a_failure(self, page):
        # The banner from the storage-failure gate. It must not be on a device
        # whose IndexedDB is fine, or a warden learns to ignore it.
        assert "CANNOT SAVE" not in (page["text"]["sync"] or "")

    def test_the_three_screens_are_offered(self, page):
        assert page["text"]["tabs"].count("\n") >= 2
