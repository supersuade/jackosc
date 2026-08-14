import pytest
from pydantic import ValidationError

from jackosc.config import AppConfig, Channel, ConfigStore, Target
from jackosc.rules import AmplitudeRule, DominantRule, FrequencyMapRule


def test_roundtrip_save_load(tmp_path):
    p = tmp_path / "cfg.json"
    store = ConfigStore(p)
    cfg = AppConfig(
        channels=[
            Channel(
                name="kick",
                rules=[
                    AmplitudeRule(freq=60.0, osc_pattern="/kick/amp"),
                    FrequencyMapRule(f0=40, f1=80, osc_pattern="/kick/band"),
                ],
            )
        ],
        targets=[Target(name="lights", host="127.0.0.1", port=9000)],
    )
    store.save(cfg)
    loaded = store.load()
    assert loaded == cfg
    assert loaded.channels[0].rules[0].freq == 60.0
    assert list(tmp_path.iterdir()) == [p]  # no temp litter


def test_invalid_rule_type_rejected():
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"channels": [{"name": "a", "rules": [{"type": "nope"}]}]}
        )


def test_frequency_map_requires_ordered_band():
    with pytest.raises(ValidationError):
        FrequencyMapRule(f0=500, f1=200)


def test_auth_token_roundtrips_but_profiles_strip_it(tmp_path):
    store = ConfigStore(tmp_path / "cfg.json")
    cfg = AppConfig(auth_token="sekrit", channels=[Channel(name="a")])
    store.save(cfg)
    assert store.load().auth_token == "sekrit"
    prof = store.save_profile("live", cfg)
    assert "sekrit" not in prof.read_text()
    assert store.load_profile("live").auth_token is None


def test_profiles_list_and_delete(tmp_path):
    store = ConfigStore(tmp_path / "cfg.json")
    store.save_profile("show1", AppConfig())
    store.save_profile("show2", AppConfig())
    assert store.list_profiles() == ["show1", "show2"]
    store.delete_profile("show1")
    assert store.list_profiles() == ["show2"]


def test_profile_name_validation(tmp_path):
    store = ConfigStore(tmp_path / "cfg.json")
    with pytest.raises(ValueError):
        store.save_profile("../evil", AppConfig())


def test_dominant_rule_defaults_are_valid():
    r = DominantRule(osc_pattern="/dom")
    assert r.fmin == 20.0
    assert r.smoothing == [0.0, 20.0]


def test_manual_connect_is_default():
    """New configs do not auto-connect; explicit 'auto' still works."""
    cfg = AppConfig()
    assert cfg.auto_connect is False
    assert Channel(name="a").connect_to is None
    assert Channel(name="b", connect_to="auto").connect_to == "auto"


def test_gate_validation():
    with pytest.raises(ValidationError):
        FrequencyMapRule(f0=40, f1=80, gate_off=0.1)  # off requires on
    with pytest.raises(ValidationError):
        FrequencyMapRule(f0=40, f1=80, gate_on=0.1, gate_off=0.2)  # off must be < on
    r = FrequencyMapRule(f0=40, f1=80, gate_on=0.3, gate_off=0.1)
    assert r.gate_on == 0.3 and r.gate_off == 0.1 and r.invert is False
