"""JACK client with an allocation-free realtime process callback.

The callback copies each input port's samples into a lock-free ring
buffer and sets a wake event; nothing else. All DSP runs in worker
threads, so Python's GC, GIL contention, and allocation cannot cause
xruns on this path.

Uses `jack-client` (jackclient-python). The package is imported lazily:
if it is missing or no JACK server is reachable, ``open`` raises
:class:`AudioUnavailable` and the rest of the app keeps running.
"""

from __future__ import annotations

import logging
import re
import threading
import time

import numpy as np

from jackosc.audio.ring import RingBuffer

__all__ = ["AudioUnavailable", "JackClient"]

log = logging.getLogger(__name__)

try:
    import jack
except ImportError:  # pragma: no cover
    jack = None

# Recent process-callback durations (ns) kept for p50/p99/max reporting.
# Fixed size, index published with the GIL — no allocation on the realtime path.
CB_SLOTS = 1 << 13


class AudioUnavailable(RuntimeError):
    """JACK could not be reached (client lib missing or server absent)."""


def _sanitize_port(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    return clean or "in"


class JackClient:
    """Owns the JACK connection and the per-channel rings.

    ``channels`` is a list of ``(name, connect_to, capacity)`` tuples:
    one input port per channel, ring buffers created up front so the
    process callback never allocates.
    """

    def __init__(self, name: str = "jackosc"):
        self.name = name
        self._client = None
        self._binds: list[tuple] = []  # (port, short_name, ring, wake_event)
        self.xruns = 0
        self.samplerate: float | None = None
        self.blocksize: int | None = None
        self.running = False
        self._cb_times = np.zeros(CB_SLOTS, dtype=np.float64)
        self._cb_idx = 0
        self._cb_count = 0

    # -- lifecycle ----------------------------------------------------

    def open(self, channels: list[tuple[str, str | None, int]]) -> None:
        if jack is None:
            raise AudioUnavailable("jack-client package is not installed")
        try:
            client = jack.Client(self.name, no_start_server=True)
        except TypeError:  # older jack-client without no_start_server
            client = jack.Client(self.name)
        except Exception as exc:  # jack.CantOpenError etc.
            raise AudioUnavailable(f"cannot open JACK client: {exc}") from exc
        self._client = client

        binds = []
        try:
            for name, _connect_to, cap in channels:
                short = _sanitize_port(name)
                ring = RingBuffer(cap)
                wake = threading.Event()
                port = client.inports.register(short)
                binds.append((port, short, ring, wake))
            self._binds = binds
            client.set_process_callback(self._process)
            client.set_xrun_callback(self._on_xrun)
            client.set_shutdown_callback(self._on_shutdown)
            client.activate()
        except Exception as exc:
            self.close()
            raise AudioUnavailable(f"cannot set up JACK client: {exc}") from exc

        self.running = True
        self.samplerate = float(client.samplerate)
        self.blocksize = int(client.blocksize)
        for i, (_name, connect_to, _cap) in enumerate(channels):
            if connect_to is None:
                continue
            src = f"system:capture_{i + 1}" if connect_to == "auto" else connect_to
            try:
                client.connect(src, self.port_name(i))
            except Exception as exc:
                log.warning("auto-connect %s -> %s failed: %s", src, self.port_name(i), exc)
        log.info("JACK client %s up: %.0f Hz, %d frames/period", self.name, self.samplerate, self.blocksize)

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.deactivate()
            finally:
                self._client.close()
                self._client = None
        self._binds = []
        self.running = False

    # -- accessors ----------------------------------------------------

    @property
    def binds(self) -> list[tuple]:
        return self._binds

    def ring(self, index: int) -> RingBuffer:
        return self._binds[index][2]

    def wake_event(self, index: int) -> threading.Event:
        return self._binds[index][3]

    def port_name(self, index: int) -> str:
        return f"{self.name}:{self._binds[index][1]}"

    def cb_stats(self) -> dict:
        """p50/p99/max of recent callback durations (µs). Reader-side only."""
        n = min(self._cb_count, CB_SLOTS)
        if n == 0:
            return {"count": 0, "p50_us": 0.0, "p99_us": 0.0, "max_us": 0.0}
        mask = CB_SLOTS - 1
        start = (self._cb_idx - n) & mask
        idx = np.arange(n, dtype=np.int64)
        arr = self._cb_times[(start + idx) & mask]
        p50, p99 = np.percentile(arr, [50.0, 99.0])
        return {
            "count": self._cb_count,
            "p50_us": round(p50 / 1e3, 1),
            "p99_us": round(p99 / 1e3, 1),
            "max_us": round(float(arr.max()) / 1e3, 1),
        }

    # -- JACK callbacks (realtime thread) ------------------------------

    def _process(self, frames: int) -> None:
        """Realtime thread: copy in, publish index, wake the worker.

        One small numpy view object per port per period (get_array);
        no data allocation, no locks, no I/O. Callback duration is
        recorded into a fixed ring (perf_counter_ns is a vDSO read).
        """
        t0 = time.perf_counter_ns()
        for port, _short, ring, wake in self._binds:
            ring.write(port.get_array())
            wake.set()
        dt = time.perf_counter_ns() - t0
        self._cb_times[self._cb_idx & (CB_SLOTS - 1)] = dt
        self._cb_idx += 1
        self._cb_count += 1

    def _on_xrun(self) -> None:
        self.xruns += 1

    def _on_shutdown(self) -> None:
        self.running = False
        log.warning("JACK server shut down; audio offline")
