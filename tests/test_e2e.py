"""Playwright smoke suite: boots a real jackosc instance and drives the UI.

Skipped when playwright is not installed (it is part of `pip install -e
'.[dev]'`; `playwright install chromium` downloads the browser). No JACK
or audio is needed: the test config has no channels, so the engine runs
web-only while the UI exercises auto-apply, undo, duplication, validation
toasts, the help modal, and the packet inspector.
"""

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

playwright = pytest.importorskip("playwright.sync_api")  # noqa: F401
from playwright.sync_api import expect  # noqa: E402

BASE_CFG = {
    "version": 1,
    "jack_name": "e2e",
    "auto_connect": False,
    "sample_rate": 48000,
    "osc_rate": 60.0,
    "channels": [],
    "targets": [],
    "autosave": False,
}

DEBOUNCE = 600  # ms: auto-apply debounce (400) + PUT round trip


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _api(server: str, path: str = "/api/config", method: str = "GET", body=None) -> dict:
    req = urllib.request.Request(
        server + path,
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Content-Type": "application/json"} if body is not None else {},
    )
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.load(r)


@pytest.fixture(scope="session")
def server(tmp_path_factory) -> str:
    cfg = tmp_path_factory.mktemp("cfg") / "config.json"
    cfg.write_text(json.dumps(BASE_CFG))
    log = tmp_path_factory.mktemp("log") / "jackosc.log"
    port = _free_port()
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "jackosc",
            "--config", str(cfg), "--host", "127.0.0.1", "--port", str(port),
        ],
        stdout=log.open("w"), stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            _api(base)
            break
        except Exception:
            time.sleep(0.1)
    else:
        proc.terminate()
        raise RuntimeError(f"jackosc did not start; log: {log}")
    yield base
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(autouse=True)
def clean_config(server):
    """Reset the server config before each test (UI state persists there)."""
    _api(server, method="PUT", body=BASE_CFG)
    yield


def open_app(page, server):
    page.goto(server)
    page.wait_for_selector("#general")  # rendered by renderConfig: proves the app booted


def add_channel(page, name="kick"):
    page.on("dialog", lambda d: d.accept(name))
    page.click("#addChannel")
    expect(page.locator(".ch-editor")).to_have_count(1)
    page.wait_for_timeout(DEBOUNCE)


def add_rule(page):
    page.click('.ch-editor [data-act="add-rule"]')
    expect(page.locator("[data-rule]")).to_have_count(1)
    page.wait_for_timeout(DEBOUNCE)


# -- smoke -------------------------------------------------------------


def test_page_loads_and_renders(page, server):
    open_app(page, server)
    expect(page.locator("#helpBtn")).to_be_visible()
    expect(page.locator("#general")).to_be_visible()
    expect(page.locator("#general input:not([type='checkbox'])")).to_have_count(4)  # osc rate, jack name, sample rate, cb warn
    expect(page.locator("#status")).to_be_visible()  # status dot (audio off)


def test_add_channel_via_ui_applies(page, server):
    open_app(page, server)
    add_channel(page)
    assert _api(server)["config"]["channels"][0]["name"] == "kick"


def test_add_rule_and_toggle_apply(page, server):
    open_app(page, server)
    add_channel(page)
    add_rule(page)
    assert len(_api(server)["config"]["channels"][0]["rules"]) == 1
    # toggle the rule off → auto-applies immediately
    page.click('[data-path$=".enabled"]')
    page.wait_for_timeout(DEBOUNCE)
    assert _api(server)["config"]["channels"][0]["rules"][0]["enabled"] is False


def test_duplicate_rule_copies_and_applies(page, server):
    open_app(page, server)
    add_channel(page)
    add_rule(page)
    page.click('[data-act="dup-rule"]')
    expect(page.locator("[data-rule]")).to_have_count(2)
    page.wait_for_timeout(DEBOUNCE)
    assert len(_api(server)["config"]["channels"][0]["rules"]) == 2


def test_undo_reverts_server(page, server):
    open_app(page, server)
    add_channel(page)
    add_rule(page)
    assert len(_api(server)["config"]["channels"][0]["rules"]) == 1
    page.keyboard.press("Control+z")
    page.wait_for_timeout(DEBOUNCE)
    assert _api(server)["config"]["channels"][0]["rules"] == []


def test_invalid_config_shows_422_toast(page, server):
    open_app(page, server)
    add_channel(page)
    add_rule(page)
    # switch the rule to frequency_map, then set f0 > f1 → validation error
    page.select_option('[data-path$=".type"]', "frequency_map")
    page.click('[data-path$=".f0"]')
    page.keyboard.press("Control+a")
    page.keyboard.type("200")
    expect(page.locator("#toast:not([hidden])")).to_be_visible(timeout=3000)
    expect(page.locator("#toast")).to_contain_text("422")


def test_help_modal_opens_and_closes(page, server):
    open_app(page, server)
    page.click("#helpBtn")
    expect(page.locator("#helpModal:not([hidden])")).to_be_visible()
    expect(page.locator("#helpModal")).to_contain_text("Rule types")
    page.keyboard.press("Escape")
    expect(page.locator("#helpModal")).to_be_hidden()


def test_collapse_and_unique_patterns(page, server):
    open_app(page, server)
    add_channel(page)
    add_rule(page)
    # second rule gets a uniquified pattern
    page.click('.ch-editor [data-act="add-rule"]')
    page.wait_for_timeout(DEBOUNCE)
    pats = page.eval_on_selector_all("[data-rule] .pat", "els => els.map(e => e.value)")
    assert pats[0] == "/kick/amplitude"
    assert pats[1] == "/kick/amplitude/2"
    # collapse hides the body; expand restores it
    page.click('[data-act="toggle-channel"]')
    expect(page.locator(".ch-editor")).to_have_class("ch-editor collapsed")
    page.click('[data-act="toggle-channel"]')
    expect(page.locator(".ch-editor")).not_to_have_class("ch-editor collapsed")


def test_type_switch_to_multiband_applies(page, server):
    """Regression: switching a new rule to multiband must add bands, follow
    the pattern, and apply cleanly (used to crash the editor and 422)."""
    open_app(page, server)
    add_channel(page)
    add_rule(page)
    page.select_option('[data-rule] [data-path$=".type"]', "multiband")
    expect(page.locator(".band")).to_have_count(3)
    expect(page.locator('[data-rule] .pat')).to_have_value("/kick/multiband")
    page.wait_for_timeout(DEBOUNCE)
    rules = _api(server)["config"]["channels"][0]["rules"]
    assert len(rules) == 1
    assert rules[0]["type"] == "multiband"
    assert len(rules[0]["bands"]) == 3


def test_packet_inspector_test_send(page, server):
    open_app(page, server)
    answers = ["lights", "127.0.0.1", "9000"]
    page.on("dialog", lambda d: d.accept(answers.pop(0)))
    page.click("#addTarget")
    expect(page.locator("#targetsEditor .target")).to_have_count(1)
    page.wait_for_timeout(DEBOUNCE)
    assert _api(server)["config"]["targets"], "target add did not apply"
    page.click("#pktSend")
    page.wait_for_timeout(300)
    pkts = _api(server, path="/api/packets?limit=5")["packets"]
    assert any(p["address"] == "/test" for p in pkts), f"server missing record: {pkts}"
    expect(page.locator("#pktList .pkt")).to_have_count(1, timeout=5000)
    expect(page.locator("#pktList .pkt")).to_contain_text("/test")
