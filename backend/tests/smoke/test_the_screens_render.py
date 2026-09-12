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


def probe(browser_port: int, url: str, read: dict, steps: list | None = None,
          wait: int = 4000) -> dict:
    spec = {"url": url, "read": read, "steps": steps or [], "wait": wait}
    result = subprocess.run(
        [NODE, str(PROBE), str(browser_port), json.dumps(spec)],
        capture_output=True, text=True, timeout=180, cwd=str(ROOT))
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


class TestTheGlueNothingUnitTests:
    """`CLAUDE.md` exempts `command.js` and `warden.js` from unit testing on
    the grounds that they are DOM glue. That is a reasonable line and it leaves
    every click handler on both screens unexercised by anything.

    Exempt from unit testing is not exempt from being wrong. A handler bound to
    an element a repaint has since replaced fails silently, on the screen a
    warden is holding during an evacuation.
    """

    def test_the_explain_drawer_opens_and_says_something(self, server, browser):
        # The one place an operator can ask why, and the only route to the
        # evidence behind a decision.
        base, _ = server
        page = probe(browser, f"{base}/?drill=demo",
                     {"drawer": ".drawer"},
                     steps=[{"click": "[data-explain]", "wait": 2500}])
        assert page["errors"] == []
        assert page["text"]["drawer"], "the drawer never opened"
        # It resolved: the placeholder is what a drawer shows while waiting,
        # and one stuck on it is the failure the caller cannot see.
        assert "loading" not in page["text"]["drawer"].lower()
        assert "could not load" not in page["text"]["drawer"].lower()

    def test_opening_a_second_person_does_not_show_the_first_one_evidence(
            self, server, browser):
        """An explanation filed against the wrong person reads as an answer.

        `command.js` captures the drawer's element before the request rather
        than looking it up afterwards, for exactly this: open one person, click
        another before the first lands, and the first narrative arrives under
        the second name.
        """
        base, _ = server
        page = probe(
            browser, f"{base}/?drill=demo",
            {"drawer": ".drawer"},
            steps=[
                {"eval": "document.querySelectorAll('[data-explain]')[0].click()",
                 "wait": 60},
                {"eval": "document.querySelectorAll('[data-explain]')[1].click()",
                 "wait": 3000},
            ])
        assert page["errors"] == []
        heading = page["text"]["drawer"].splitlines()[0]
        # The drawer's heading is the person asked about second; the narrative
        # under it must be theirs or absent, never the first person's.
        assert heading.startswith("emp:") or heading.startswith("visitor:")

    def test_the_scrim_closes_the_drawer(self, server, browser):
        base, _ = server
        page = probe(browser, f"{base}/?drill=demo",
                     {"drawer": ".drawer"},
                     steps=[{"click": "[data-explain]", "wait": 2000},
                            {"click": ".scrim", "wait": 800}])
        assert page["errors"] == []
        assert page["text"]["drawer"] is None

    def test_arabic_turns_the_page_round_rather_than_only_translating(
            self, server, browser):
        """Both screens are read in Arabic, and a right-to-left language that
        renders left to right is harder to use than one that is untranslated."""
        base, swept = server
        page = probe(
            browser,
            f"{base}/evac/warden?drill=demo&zone={swept}&warden=warden-1",
            {"direction": "dir:html", "title": "#zone-title"},
            steps=[{"click": "#lang", "wait": 1500}])
        assert page["errors"] == []
        assert page["text"]["direction"] == "rtl"
        # And the strings actually changed, rather than the layout alone.
        assert not any(letter.isascii() and letter.isalpha()
                       for letter in page["text"]["title"].split("·")[0])

    def test_the_warden_tabs_switch_screens(self, server, browser):
        base, swept = server
        page = probe(
            browser,
            f"{base}/evac/warden?drill=demo&zone={swept}&warden=warden-1",
            {"roster": "visible:#screen-roster", "zone": "visible:#screen-zone"},
            steps=[{"click": '[data-screen="roster"]', "wait": 1500}])
        assert page["errors"] == []
        # Asked as "is it on screen", not "does it have text": the HTML spec
        # has `innerText` fall back to `textContent` for an element that is not
        # being rendered, so a hidden panel reads exactly like a shown one.
        assert page["text"]["roster"] is True, "the roster screen stayed hidden"
        assert page["text"]["zone"] is False, "the zone screen stayed on top"

    def test_a_warden_confirming_somebody_changes_the_board(self, server,
                                                            browser):
        """The whole point of the device, end to end.

        Tap confirm, and the action is written to IndexedDB, synced to the
        server, folded into the drill, and the zone panel moves. Nothing else
        in the suite crosses all four, and each of them is separately tested
        against a stub of the next one.

        Every step waits for a condition rather than for a duration. A sleep
        long enough for a loaded machine is wasted on every other run, and one
        tuned to a fast machine fails for reasons nobody can see -- which this
        test did, reading an empty panel before the first poll had landed and
        then comparing against the `undefined` it took from it.
        """
        base, swept = server
        counter = "document.querySelector('#zone-tiles .tile.green .n')"
        page = probe(
            browser,
            f"{base}/evac/warden?drill=demo&zone={swept}&warden=warden-1",
            {"before": "js:window.__before",
             "after": f"js:{counter} && Number({counter}.innerText)",
             "sync": "#sync",
             # Read even when the flow works: a failure that says only "the
             # number did not move" sends the next reader back through all four
             # layers to find out which one did nothing.
             "problems": "#sync-problems"},
            wait=500,
            steps=[
                {"until": f"{counter} !== null"},
                {"eval": f"window.__before = Number({counter}.innerText)"},
                {"click": '[data-screen="roster"]'},
                {"until": "document.querySelector('[data-act]') !== null"},
                # The first person this warden has not settled. The roster is
                # ordered worst-first, so this is somebody who matters.
                # Unquoted in the attribute selector on purpose:
                # CONFIRM_PRESENT is a valid CSS identifier, and quoting it
                # means escaping quotes through Python, then JSON, then
                # JavaScript. That produced a `SyntaxError` the probe swallowed
                # -- which is why it now reports one.
                {"eval": "document.querySelector("
                         "'[data-act=CONFIRM_PRESENT]').click()"},
                # Back to the zone screen to read the count. `paintZone` only
                # runs for the screen on top, so the tiles behind the roster
                # are the ones that were there when the warden left them.
                {"click": '[data-screen="zone"]'},
                {"until": f"Number({counter}.innerText) > window.__before",
                 "timeout": 25000},
            ])
        # The values first and the page's exceptions last: "the count did not
        # move, the queue held this, the strip said that" tells the next reader
        # which of the four layers did nothing. "A condition never held" does
        # not, and it is what fires first if the order is the other way round.
        assert page["text"]["before"] is not None, page["text"]
        # Asserted as a change, not as a number: a confirmation that never left
        # the device and one that never happened both leave the count alone.
        assert page["text"]["after"] > page["text"]["before"], page
        # And it reached the server rather than sitting in the queue.
        assert "CANNOT SAVE" not in page["text"]["sync"], page["text"]
        assert "pending" not in page["text"]["sync"].lower(), page["text"]
        assert page["errors"] == []
