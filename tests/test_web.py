import pytest
from fastapi.testclient import TestClient

from jackosc.audio.client import AudioUnavailable
from jackosc.config import AppConfig, Channel, ConfigStore, Target
from jackosc.engine import AnalysisEngine
from jackosc.rules import AmplitudeRule, FrequencyMapRule
from jackosc.state import ValueStore
from jackosc.web.auth import ConfigAuth
from jackosc.web.server import create_app


@pytest.fixture
def no_audio(monkeypatch):
    """Keep tests hermetic: never touch a real JACK server."""

    def fake_open(self, channels):
        raise AudioUnavailable("test: JACK disabled")

    FakeJack = type(
        "FakeJack",
        (),
        {"__init__": lambda self, name=None: None, "open": fake_open, "close": lambda self: None},
    )
    monkeypatch.setattr("jackosc.engine.JackClient", FakeJack)


def make_app(tmp_path, secret=None, cfg=None):
    store = ValueStore()
    engine = AnalysisEngine(store)
    engine.apply_config(cfg or AppConfig(autosave=False))
    app = create_app(engine, store, ConfigStore(tmp_path / "cfg.json"), ConfigAuth(secret))
    return engine, TestClient(app)


def test_get_config_public_and_token_never_leaks(tmp_path):
    _engine, client = make_app(tmp_path, secret="sekrit")
    r = client.get("/api/config")
    assert r.status_code == 200
    data = r.json()
    assert data["auth_enabled"] is True
    assert "auth_token" not in data["config"]


def test_writes_open_without_secret(tmp_path, no_audio):
    _engine, client = make_app(tmp_path)
    r = client.put("/api/config", json={"version": 1, "channels": []})
    assert r.status_code == 200


def test_writes_require_token_when_enabled(tmp_path, no_audio):
    _engine, client = make_app(tmp_path, secret="sekrit")
    body = {"version": 1, "channels": []}
    assert client.put("/api/config", json=body).status_code == 401
    assert (
        client.put("/api/config", json=body, headers={"Authorization": "Bearer wrong"}).status_code
        == 401
    )
    assert (
        client.put(
            "/api/config", json=body, headers={"Authorization": "Bearer sekrit"}
        ).status_code
        == 200
    )


def test_channel_and_rule_crud(tmp_path, no_audio):
    engine, client = make_app(tmp_path)

    r = client.post("/api/channels", json={"name": "kick", "connect_to": "auto"})
    assert r.status_code == 200
    assert [c.name for c in engine.config.channels] == ["kick"]

    r = client.post("/api/channels", json={"name": "kick"})
    assert r.status_code == 409

    r = client.post(
        "/api/channels/kick/rules",
        json={"type": "frequency_map", "f0": 40, "f1": 80, "osc_pattern": "/kick/band"},
    )
    assert r.status_code == 200
    assert isinstance(engine.config.channels[0].rules[0], FrequencyMapRule)

    r = client.delete("/api/channels/kick/rules/0")
    assert r.status_code == 200
    assert engine.config.channels[0].rules == []

    r = client.delete("/api/channels/kick")
    assert r.status_code == 200
    assert engine.config.channels == []

    assert client.delete("/api/channels/kick").status_code == 404


def test_bad_rule_rejected(tmp_path, no_audio):
    engine, client = make_app(tmp_path)
    client.post("/api/channels", json={"name": "a"})
    r = client.post("/api/channels/a/rules", json={"type": "bogus"})
    assert r.status_code == 422


def test_too_many_rules_rejected(tmp_path, no_audio):
    engine, client = make_app(tmp_path)
    client.post("/api/channels", json={"name": "a"})
    body = engine.config.model_dump()
    body["channels"][0]["rules"] = [{"type": "amplitude", "freq": 100.0}] * 17
    r = client.put("/api/config", json=body)
    assert r.status_code == 400


def test_profiles_flow(tmp_path, no_audio):
    engine, client = make_app(tmp_path)
    client.post("/api/channels", json={"name": "snare"})
    r = client.post("/api/profiles/show1")
    assert r.status_code == 200
    assert client.get("/api/profiles").json()["profiles"] == ["show1"]
    client.delete("/api/channels/snare")
    r = client.post("/api/profiles/show1/load")
    assert r.status_code == 200
    assert [c.name for c in engine.config.channels] == ["snare"]


def test_invalid_config_returns_422_not_500(tmp_path, no_audio):
    """pydantic ctx values must not explode the 422 response into a 500."""
    engine, client = make_app(tmp_path)
    r = client.put(
        "/api/config",
        json={
            "version": 1,
            "channels": [{"name": "a", "rules": [{"type": "frequency_map", "f0": 200, "f1": 80}]}],
        },
    )
    assert r.status_code == 422
    assert r.json() is not None  # serializable detail


def test_calibrate_uncalibrated_rule_errors(tmp_path, no_audio):
    engine, client = make_app(tmp_path)
    client.post(
        "/api/channels",
        json={"name": "a", "rules": [{"type": "amplitude", "freq": 100.0}]},
    )
    r = client.post("/api/channels/a/rules/0/calibrate", json={"seconds": 0.5})
    assert r.status_code == 400  # not a frequency_map rule


def test_calibrate_band_validation(tmp_path, no_audio):
    engine, client = make_app(tmp_path)
    client.post(
        "/api/channels",
        json={
            "name": "a",
            "rules": [
                {"type": "multiband", "bands": [{"f0": 40, "f1": 80}, {"f0": 500, "f1": 1500}]}
            ],
        },
    )
    r = client.post("/api/channels/a/rules/0/calibrate", json={"seconds": 0.5})
    assert r.status_code == 400  # band required for multiband
    r = client.post("/api/channels/a/rules/0/calibrate", json={"seconds": 0.5, "band": 5})
    assert r.status_code == 400  # band out of range


def test_packets_api_and_test_send(tmp_path, no_audio):
    import time

    engine, client = make_app(
        tmp_path,
        secret="sekrit",
        cfg=AppConfig(
            targets=[Target(name="t", host="127.0.0.1", port=9)],
            autosave=False,
        ),
    )
    assert client.get("/api/packets").json()["packets"] == []

    # test-send is a write: gated by auth
    assert (
        client.post("/api/packets/test", json={"address": "/x", "value": 0.5}).status_code
        == 401
    )
    r = client.post(
        "/api/packets/test",
        json={"address": "/x", "value": 0.5},
        headers={"Authorization": "Bearer sekrit"},
    )
    assert r.status_code == 200
    assert r.json() == {"queued": True, "targets": 1}

    time.sleep(0.15)  # sender thread drains the command queue
    pkts = client.get("/api/packets").json()["packets"]
    assert len(pkts) == 1
    assert pkts[0]["address"] == "/x"
    assert pkts[0]["value"] == 0.5
    assert pkts[0]["target"] == "t"
