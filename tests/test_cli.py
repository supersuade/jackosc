"""CLI helpers: host resolution for --lan / --host."""

from jackosc.cli import resolve_host


def test_resolve_host_default_loopback():
    assert resolve_host(None, False) == "127.0.0.1"


def test_resolve_host_lan_implies_all_interfaces():
    assert resolve_host(None, True) == "0.0.0.0"


def test_resolve_host_explicit_host_wins_over_lan():
    assert resolve_host("10.0.0.5", True) == "10.0.0.5"
