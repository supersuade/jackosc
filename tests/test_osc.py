import queue

from jackosc.config import AppConfig, Channel, Target
from jackosc.osc.sender import OscSender
from jackosc.rules import AmplitudeRule
from jackosc.state import ValueStore


def _setup(min_change=0.0):
    store = ValueStore()
    cfg = AppConfig(
        channels=[Channel(name="a", rules=[AmplitudeRule(freq=100.0, osc_pattern="/a", smoothing=[0, 0], min_change=min_change)])],
        targets=[Target(name="t", host="127.0.0.1", port=9, prefix="/p")],
    )
    store.reconfigure(cfg.channels)
    return store, cfg


def test_sender_sends_and_gates(monkeypatch):
    sent = []

    class FakeClient:
        def __init__(self, host, port):
            self.host, self.port = host, port

        def send_message(self, addr, value):
            sent.append((addr, value))

    monkeypatch.setattr("jackosc.osc.sender.udp_client.SimpleUDPClient", FakeClient)

    store, cfg = _setup(min_change=0.01)
    store.set_value(0, 0, 0.5)
    sender = OscSender(store, lambda: cfg, rate=60.0)
    last = {}
    sender._send_once(cfg, last)
    assert sent == [("/p/a", 0.5)]  # prefix applied

    sent.clear()
    sender._send_once(cfg, last)  # |Δ| = 0 < 0.01 → gated
    assert sent == []

    store.set_value(0, 0, 0.55)
    sender._send_once(cfg, last)  # Δ = 0.05 ≥ 0.01 → sent
    assert sent == [("/p/a", 0.55)]


def test_sender_skips_nan(monkeypatch):
    sent = []

    class FakeClient:
        def __init__(self, host, port):
            pass

        def send_message(self, addr, value):
            sent.append((addr, value))

    monkeypatch.setattr("jackosc.osc.sender.udp_client.SimpleUDPClient", FakeClient)

    store, cfg = _setup()
    # value stays NaN (no data yet)
    sender = OscSender(store, lambda: cfg, rate=60.0)
    last = {}
    sender._send_once(cfg, last)
    assert sent == []
    assert last == {}


def test_sender_records_history_and_tap(monkeypatch):
    sent = []

    class FakeClient:
        def __init__(self, host, port):
            pass

        def send_message(self, addr, value):
            sent.append((addr, value))

    monkeypatch.setattr("jackosc.osc.sender.udp_client.SimpleUDPClient", FakeClient)

    store, cfg = _setup()
    store.set_value(0, 0, 0.25)
    tap = queue.Queue()
    sender = OscSender(store, lambda: cfg, rate=60.0, tap=tap)
    last = {}
    sender._send_once(cfg, last)
    assert len(sender.history) == 1
    rec = sender.history[0]
    assert rec["target"] == "t"
    assert rec["address"] == "/p/a"
    assert rec["value"] == 0.25
    assert tap.get_nowait() == rec
    assert tap.empty()


def test_history_is_bounded(monkeypatch):
    sent = []

    class FakeClient:
        def __init__(self, host, port):
            pass

        def send_message(self, addr, value):
            sent.append((addr, value))

    monkeypatch.setattr("jackosc.osc.sender.udp_client.SimpleUDPClient", FakeClient)

    store, cfg = _setup()
    store.set_value(0, 0, 0.5)
    sender = OscSender(store, lambda: cfg, rate=60.0)
    last = {}
    for _ in range(600):
        sender._send_once(cfg, last)
    assert len(sender.history) == 512
    times = [rec["t"] for rec in sender.history]
    assert times == sorted(times)  # oldest dropped, order preserved


def test_bundle_target_sends_one_atomic_bundle(monkeypatch):
    import numpy as np

    from jackosc.rules import AmplitudeRule

    sent = []

    class FakeClient:
        def __init__(self, host, port):
            pass

        def send_message(self, addr, value):
            sent.append(("msg", addr, value))

        def send(self, content):
            sent.append(("bundle", content.dgram))

    monkeypatch.setattr("jackosc.osc.sender.udp_client.SimpleUDPClient", FakeClient)

    store = ValueStore()
    cfg = AppConfig(
        channels=[
            Channel(
                name="a",
                rules=[
                    AmplitudeRule(freq=100.0, osc_pattern="/a1", smoothing=[0, 0], min_change=0.01),
                    AmplitudeRule(freq=200.0, osc_pattern="/a2", smoothing=[0, 0], min_change=0.01),
                ],
            )
        ],
        targets=[Target(name="t", host="127.0.0.1", port=9, prefix="/p", bundle=True)],
    )
    store.reconfigure(cfg.channels)
    store.set_value(0, 0, 0.1)
    store.set_value(0, 1, 0.2)
    sender = OscSender(store, lambda: cfg, rate=60.0)
    last = {}

    sender._send_once(cfg, last)
    # exactly ONE bundle datagram, no individual messages
    assert len(sent) == 1
    kind, dgram = sent[0]
    assert kind == "bundle"
    assert dgram[:8] == b"#bundle\x00"
    assert b"/p/a1" in dgram and b"/p/a2" in dgram
    assert b",f" in dgram  # float args
    assert len(sender.history) == 2  # both bundled messages recorded

    # unchanged values → no new bundle
    sent.clear()
    sender._send_once(cfg, last)
    assert sent == []

    # one value moved beyond min_change → bundle again
    store.set_value(0, 0, 0.15)
    sent.clear()
    sender._send_once(cfg, last)
    assert len(sent) == 1 and sent[0][0] == "bundle"


def test_mixed_bundle_and_direct_targets(monkeypatch):
    sent = []

    class FakeClient:
        def __init__(self, host, port):
            pass

        def send_message(self, addr, value):
            sent.append(("msg", addr, value))

        def send(self, content):
            sent.append(("bundle", content.dgram))

    monkeypatch.setattr("jackosc.osc.sender.udp_client.SimpleUDPClient", FakeClient)

    store = ValueStore()
    cfg = AppConfig(
        channels=[
            Channel(name="a", rules=[AmplitudeRule(freq=100.0, osc_pattern="/a", smoothing=[0, 0])])
        ],
        targets=[
            Target(name="direct", host="127.0.0.1", port=9, bundle=False),
            Target(name="bundle", host="127.0.0.1", port=9, bundle=True),
        ],
    )
    store.reconfigure(cfg.channels)
    store.set_value(0, 0, 0.5)
    sender = OscSender(store, lambda: cfg, rate=60.0)
    last = {}
    sender._send_once(cfg, last)
    assert ("msg", "/a", 0.5) in sent  # direct target gets the individual message
    assert any(item[0] == "bundle" and b"/a" in item[1] for item in sent)  # bundle target gets the bundle


def test_sender_rate_tracks_config(monkeypatch):
    """The sender reads osc_rate from the live config, not a fixed value."""
    import time as _time

    sleeps = []
    store, cfg = _setup()
    store.set_value(0, 0, 0.5)
    sender = OscSender(store, lambda: cfg, rate=60.0)
    n = {"i": 0}
    orig_sleep = _time.sleep

    def fake_sleep(s):
        n["i"] += 1
        sleeps.append(s)
        if n["i"] == 1:
            cfg.osc_rate = 30.0  # hot change mid-run
        if n["i"] >= 3:
            sender._stop.set()
        orig_sleep(s)

    monkeypatch.setattr("jackosc.osc.sender.time.sleep", fake_sleep)
    sender.run()
    assert len(sleeps) >= 2
    assert abs(sleeps[0] - 1.0 / 60.0) < 0.01
    assert abs(sleeps[1] - 1.0 / 30.0) < 0.01


def test_multiband_sends_one_message_with_n_args(monkeypatch):
    import numpy as np

    from jackosc.rules import Band, MultibandRule

    sent = []

    class FakeClient:
        def __init__(self, host, port):
            pass

        def send_message(self, addr, value):
            if isinstance(value, (list, tuple)):  # python-osc expands lists
                sent.append((addr, list(value)))
            else:
                sent.append((addr, value))

    monkeypatch.setattr("jackosc.osc.sender.udp_client.SimpleUDPClient", FakeClient)

    store = ValueStore()
    cfg = AppConfig(
        channels=[
            Channel(
                name="a",
                rules=[
                    MultibandRule(
                        bands=[Band(f0=40, f1=80), Band(f0=500, f1=1500), Band(f0=4000, f1=8000)],
                        osc_pattern="/bands",
                        min_change=0.01,
                    )
                ],
            )
        ],
        targets=[Target(name="t", host="127.0.0.1", port=9, prefix="/p")],
    )
    store.reconfigure(cfg.channels)
    store.set_multi(0, 0, np.array([0.1, 0.5, 0.9]))
    sender = OscSender(store, lambda: cfg, rate=60.0)
    last = {}
    sender._send_once(cfg, last)
    assert sent == [("/p/bands", [0.1, 0.5, 0.9])]  # ONE message, N args

    sent.clear()
    sender._send_once(cfg, last)  # unchanged → gated by min_change
    assert sent == []

    store.set_multi(0, 0, np.array([0.12, 0.5, 0.9]))  # band 0 moved 0.02 ≥ 0.01
    sender._send_once(cfg, last)
    assert sent == [("/p/bands", [0.12, 0.5, 0.9])]

    store.set_multi(0, 0, np.array([0.1, float("nan"), 0.9]))  # uncalibrated band → silent
    sent.clear()
    sender._send_once(cfg, last)
    assert sent == []
