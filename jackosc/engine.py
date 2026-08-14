"""AnalysisEngine: owns the JACK client, per-channel workers, and the
OSC sender.

Configuration is hot-swapped atomically: workers read ``self._cfg``
once per iteration, so a config change applies at the next window
boundary. Channel add/remove requires a JACK rebind (deactivate →
re-register → activate), which stops workers first; rules-only changes
never interrupt audio.
"""

from __future__ import annotations

import logging
import queue
import threading
import time

import numpy as np

from jackosc.audio.client import AudioUnavailable, JackClient
from jackosc.config import AppConfig
from jackosc.dsp.extract import ChannelExtractor
from jackosc.dsp.window import WindowAccumulator
from jackosc.osc.sender import OscSender
from jackosc.rules import FrequencyMapRule, MultibandRule, OnsetRule
from jackosc.state import MAX_RULES_PER_CHANNEL, ValueStore

__all__ = ["AnalysisEngine"]

log = logging.getLogger(__name__)

RING_CAPACITY = 1 << 15  # 32768 samples ≈ 0.68 s @ 48 kHz
SCRATCH = 1 << 12  # samples pulled per worker iteration
RECONNECT_INTERVAL = 2.0  # seconds between audio health checks


class AnalysisEngine:
    def __init__(self, store: ValueStore):
        self.store = store
        self._cfg: AppConfig | None = None
        self._client: JackClient | None = None
        self._workers: list[threading.Thread] = []
        self._stop = threading.Event()
        self._sender: OscSender | None = None
        self._calib: dict | None = None
        self._tap: queue.Queue = queue.Queue(maxsize=1024)
        self.audio_available = False
        self._audio_error: str | None = None
        self._lock = threading.RLock()
        self._stop_monitor = threading.Event()
        self._monitor: threading.Thread | None = None
        self._reconnect_interval = RECONNECT_INTERVAL

    @property
    def tap(self) -> queue.Queue:
        """Packet-record queue consumed by the /ws/packets stream."""
        return self._tap

    # -- configuration (control plane) --------------------------------

    def apply_config(self, cfg: AppConfig) -> None:
        """Hot-swap configuration; rebind JACK ports if channels changed."""
        with self._lock:
            for ch in cfg.channels:
                if len(ch.rules) > MAX_RULES_PER_CHANNEL:
                    raise ValueError(
                        f"channel {ch.name!r}: at most {MAX_RULES_PER_CHANNEL} rules per channel"
                    )
            old = self._cfg
            channels_changed = (
                old is None or [c.name for c in old.channels] != [c.name for c in cfg.channels]
            )
            if channels_changed:
                self._stop_workers()
                self._teardown_client()
            self._cfg = cfg
            self.store.reconfigure(cfg.channels)
            if channels_changed:
                self._setup_audio(cfg)
                if not self.audio_available:
                    log.warning("audio unavailable: %s", self._audio_error)
            self._ensure_sender(cfg)
            self._ensure_monitor()

    def _ensure_sender(self, cfg: AppConfig) -> None:
        """The OSC sender runs whenever a config exists — even with no audio
        (offline, rule values stay NaN and only test packets flow)."""
        if self._sender is None:
            self._sender = OscSender(
                self.store, lambda: self._cfg, rate=cfg.osc_rate, tap=self._tap
            )
            self._sender.start()

    def packets(self, limit: int = 200) -> list[dict]:
        """Recent emitted packets (newest last)."""
        sender = self._sender
        if sender is None:
            return []
        return list(sender.history)[-limit:]

    def send_test(self, address: str, value: float) -> int:
        """Queue an immediate test OSC message to all enabled targets.

        Returns the number of enabled targets it will be sent to.
        """
        with self._lock:
            cfg = self._cfg
            n = len([t for t in cfg.targets if t.enabled]) if cfg else 0
            if n and self._sender is not None:
                self._sender.send_test(address, value)
            return n

    @property
    def config(self) -> AppConfig | None:
        return self._cfg

    def status(self) -> dict:
        client = self._client
        alive = client is not None and client.running
        dropped = 0
        if client is not None:
            dropped = sum(ring.dropped for _p, _s, ring, _w in client.binds)
        cb = client.cb_stats() if client is not None else {"count": 0, "p50_us": 0.0, "p99_us": 0.0, "max_us": 0.0}
        return {
            "audio": self.audio_available and alive,
            "audio_error": None if alive else self._audio_error,
            "xruns": client.xruns if client is not None else 0,
            "samplerate": client.samplerate if client is not None else None,
            "blocksize": client.blocksize if client is not None else None,
            "dropped": dropped,
            "cb_count": cb["count"],
            "cb_p50_us": cb["p50_us"],
            "cb_p99_us": cb["p99_us"],
            "cb_max_us": cb["max_us"],
        }

    def calibrate(self, channel_name: str, rule_idx: int, seconds: float = 3.0, band: int | None = None) -> dict:
        """Capture raw values, then set calibration bounds.

        frequency_map/onset: whole-rule bounds. multiband: one band at a
        time (`band` required). Blocks until the capture window elapses.
        """
        with self._lock:
            cfg = self._cfg
            if cfg is None:
                raise ValueError("no configuration loaded")
            ci = next((i for i, c in enumerate(cfg.channels) if c.name == channel_name), None)
            if ci is None:
                raise KeyError(channel_name)
            rule = cfg.channels[ci].rules[rule_idx]
            if isinstance(rule, MultibandRule):
                if band is None:
                    raise ValueError("calibrating a multiband rule requires a band index")
                if not (0 <= band < len(rule.bands)):
                    raise ValueError(f"band index out of range (0..{len(rule.bands) - 1})")
            elif isinstance(rule, (FrequencyMapRule, OnsetRule)):
                if band is not None:
                    raise ValueError("band only applies to multiband rules")
            else:
                raise ValueError(
                    "calibration applies to frequency_map, onset, or multiband rules only"
                )
            ch = cfg.channels[ci]
            sr = cfg.sample_rate or (self._client.samplerate if self._client else None) or 48000.0
            target = max(10, int(seconds * sr / ch.hop))
            self._calib = {
                "channel": ci,
                "rule": rule_idx,
                "band": band,
                "target": target,
                "raw": [],
            }

        deadline = time.monotonic() + seconds * 3.0 + 1.0
        while time.monotonic() < deadline:
            calib = self._calib
            if calib is None or len(calib["raw"]) >= calib["target"]:
                break
            time.sleep(0.02)

        with self._lock:
            calib = self._calib
            self._calib = None
            if calib is None or len(calib["raw"]) < calib["target"] * 0.5:
                raise ValueError("calibration timed out (no audio data?)")
            raw = np.asarray(calib["raw"], dtype=np.float64)
            new_cfg = cfg.model_copy(deep=True)
            new_rule = new_cfg.channels[ci].rules[rule_idx]
            if isinstance(rule, MultibandRule):
                lo, hi = np.percentile(raw, [2.0, 98.0])
                if hi <= lo:
                    hi = lo + 1e-9
                new_rule.bands[band].cal_min = float(lo)
                new_rule.bands[band].cal_max = float(hi)
                result = {"band": band, "cal_min": float(lo), "cal_max": float(hi), "samples": int(len(raw))}
            elif isinstance(rule, FrequencyMapRule):
                lo, hi = np.percentile(raw, [2.0, 98.0])
                if hi <= lo:
                    hi = lo + 1e-9
                new_rule.cal_min = float(lo)
                new_rule.cal_max = float(hi)
                result = {"cal_min": float(lo), "cal_max": float(hi), "samples": int(len(raw))}
            else:  # OnsetRule: flux level the signal exceeds ~5% of the time
                new_rule.threshold = max(float(np.percentile(raw, 95.0)), 1e-9)
                result = {"threshold": new_rule.threshold, "samples": int(len(raw))}
            self.apply_config(new_cfg)
            return result

    def stop(self) -> None:
        with self._lock:
            self._stop_monitor.set()
            if self._monitor is not None:
                self._monitor.join(timeout=2.0)
                self._monitor = None
            self._stop_workers()
            self._teardown_client()

    # -- internals ------------------------------------------------------

    def _ensure_monitor(self) -> None:
        """Watch for JACK coming up (or coming back) and reconnect."""
        if self._monitor is None:
            self._monitor = threading.Thread(
                target=self._monitor_loop, name="jackosc-reconnect", daemon=True
            )
            self._monitor.start()

    def _monitor_loop(self) -> None:
        while not self._stop_monitor.is_set():
            self._stop_monitor.wait(timeout=self._reconnect_interval)
            if self._stop_monitor.is_set():
                break
            try:
                self._check_audio()
            except Exception:
                log.exception("audio monitor check failed")

    def _check_audio(self) -> None:
        """Reconnect when audio should be up but the client is missing or dead."""
        with self._lock:
            cfg = self._cfg
            if cfg is None or not cfg.channels:
                return
            client = self._client
            if client is not None and client.running:
                return
            if client is not None:  # JACK died: stop workers, close the client
                self._stop_workers()
                self._teardown_client()
            err_before = self._audio_error
            self._setup_audio(cfg)
            self._ensure_sender(cfg)
            if self.audio_available:
                log.info("audio reconnected: %s @ %.0f Hz", cfg.jack_name, self.status()["samplerate"])
            elif self._audio_error and self._audio_error != err_before:
                log.warning("audio unavailable: %s", self._audio_error)

    def _setup_audio(self, cfg: AppConfig) -> None:
        self.audio_available = False
        self._audio_error = None
        if not cfg.channels:
            self._audio_error = "no channels configured"
            return
        specs = []
        for i, ch in enumerate(cfg.channels):
            ct = ch.connect_to
            if ct is None and cfg.auto_connect:
                ct = "auto"
            specs.append((ch.name, ct, RING_CAPACITY))
        client = JackClient(cfg.jack_name)
        try:
            client.open(specs)
        except AudioUnavailable as exc:
            self._audio_error = str(exc)
            return
        self._client = client
        self.audio_available = True
        self._audio_error = None
        self._stop.clear()
        self._workers = [t for t in self._workers if t.is_alive()]
        for i, ch in enumerate(cfg.channels):
            t = threading.Thread(
                target=self._worker, args=(i,), name=f"jackosc-{ch.name}", daemon=True
            )
            self._workers.append(t)
            t.start()

    def _stop_workers(self) -> None:
        self._stop.set()
        if self._sender is not None:
            self._sender.stop()
            self._sender = None
        for t in self._workers:
            t.join(timeout=2.0)
        self._workers = []
        self._stop.clear()

    def _teardown_client(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
        self.audio_available = False

    # -- analysis worker (one per channel) ------------------------------

    def _worker(self, channel_idx: int) -> None:
        client = self._client
        if client is None:
            return
        ring = client.ring(channel_idx)
        wake = client.wake_event(channel_idx)
        sr = self._cfg.sample_rate or client.samplerate or 48000.0
        ch0 = self._cfg.channels[channel_idx]
        acc = WindowAccumulator(ch0.window, ch0.hop)
        extractor = ChannelExtractor(ch0, sr)
        scratch = np.empty(SCRATCH, dtype=np.float32)
        while not self._stop.is_set():
            if not client.running:
                break
            wake.wait(timeout=0.05)
            wake.clear()
            n = ring.read_into(scratch)
            if n == 0:
                continue
            cfg = self._cfg
            ch = cfg.channels[channel_idx]
            if ch.window != acc.window or ch.hop != acc.hop:
                acc = WindowAccumulator(ch.window, ch.hop)
                extractor = ChannelExtractor(ch, sr)
            else:
                extractor.set_rules(ch.rules)
            for window in acc.feed(scratch[:n]):
                for rule_idx, final, _raw in extractor.process_window(window):
                    self.store.set_value(channel_idx, rule_idx, final)
                    mv = extractor.multi_value(rule_idx)
                    if mv is not None:
                        self.store.set_multi(channel_idx, rule_idx, mv)
                self._capture(channel_idx, extractor)
                self.store.set_spectrum(channel_idx, extractor.last_spectrum())

    def _capture(self, channel_idx: int, extractor) -> None:
        calib = self._calib
        if calib is None or calib["channel"] != channel_idx:
            return
        if len(calib["raw"]) >= calib["target"]:
            return
        raw = extractor.raw_value(calib["rule"], calib.get("band"))
        if raw is not None:
            calib["raw"].append(raw)
