"""Integration tests against a live JACK server.

Skipped when no server is reachable, so the suite stays green on
machines without JACK. These exercise the real realtime path: port
registration, activation, and the process callback copying samples
into the ring.
"""

import time

import pytest

try:
    import jack
except ImportError:
    jack = None


def _jack_available() -> bool:
    if jack is None:
        return False
    try:
        c = jack.Client("jackosc-probe", no_start_server=True)
        c.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _jack_available(), reason="no JACK server reachable")


def test_client_registers_ports_in_graph():
    c = jack.Client("jackosc-itest", no_start_server=True)
    try:
        c.inports.register("probe")
        c.activate()
        time.sleep(0.3)
        names = [p.name for p in c.get_ports()]
        assert "jackosc-itest:probe" in names
    finally:
        c.close()


def test_jackclient_thin_callback_copies_samples():
    from jackosc.audio.client import JackClient

    jc = JackClient("jackosc-itest2")
    jc.open([("probe", None, 4096)])
    try:
        assert jc.samplerate > 0
        assert jc.blocksize > 0
        assert jc.port_name(0) == "jackosc-itest2:probe"
        time.sleep(0.4)  # several buffer periods
        assert jc.ring(0).readable > 0  # realtime callback copied samples
        assert jc.xruns >= 0
        cb = jc.cb_stats()
        assert cb["count"] > 0
        assert cb["p99_us"] >= 0.0
    finally:
        jc.close()
