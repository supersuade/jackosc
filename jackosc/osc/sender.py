"""OSC sender: samples the ValueStore at a fixed cadence and sends to
all enabled targets.

One thread owns every socket, so no locking is needed. UDP is
non-blocking; dropped packets are fine (latest-value semantics for
lights). ``min_change`` per rule gates redundant sends.

Every emitted message is recorded — bounded history plus an optional
tap queue — so the web packet inspector shows exactly what leaves the
box (the ground truth for debugging the lights app).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from typing import Callable

from pythonosc import osc_bundle_builder, osc_message_builder, udp_client

from jackosc.rules import MultibandRule
from jackosc.state import ValueStore

__all__ = ["OscSender"]

log = logging.getLogger(__name__)

HISTORY_LEN = 512


def _delta(v, prev) -> float:
    """Magnitude of change between a value and its previously sent value."""
    if isinstance(v, (list, tuple)):
        return max(abs(a - b) for a, b in zip(v, prev))
    return abs(v - prev)


class OscSender(threading.Thread):
    def __init__(
        self,
        store: ValueStore,
        cfg_getter: Callable[[], object],
        rate: float = 60.0,
        tap: queue.Queue | None = None,
    ):
        super().__init__(name="jackosc-osc", daemon=True)
        self.store = store
        self.cfg_getter = cfg_getter
        self.rate = rate
        self._stop = threading.Event()
        self._clients: dict[str, udp_client.SimpleUDPClient] = {}
        self.history: deque = deque(maxlen=HISTORY_LEN)
        self.tap = tap  # optional queue.Queue of packet records for the inspector
        self._cmds: queue.Queue = queue.Queue()
        self._last_bundle: dict = {}

    def stop(self) -> None:
        self._stop.set()

    def send_test(self, address: str, value: float) -> None:
        """Queue an immediate message to all enabled targets (web test button)."""
        self._cmds.put({"cmd": "send", "address": address, "value": value})

    def run(self) -> None:
        last: dict[tuple[int, int], object] = {}
        while not self._stop.is_set():
            t0 = time.perf_counter()
            self._drain_cmds()
            cfg = self.cfg_getter()
            rate = cfg.osc_rate if cfg is not None else self.rate
            if cfg is not None:
                try:
                    self._send_once(cfg, last)
                except Exception:  # never kill the sender loop
                    log.exception("OSC send iteration failed")
            elapsed = time.perf_counter() - t0
            time.sleep(max(0.0, 1.0 / max(rate, 0.1) - elapsed))

    def _drain_cmds(self) -> None:
        while True:
            try:
                cmd = self._cmds.get_nowait()
            except queue.Empty:
                return
            if cmd["cmd"] == "send":
                self._send_test(cmd["address"], cmd["value"])

    def _send_test(self, address: str, value: float) -> None:
        cfg = self.cfg_getter()
        if cfg is None:
            return
        targets = [t for t in cfg.targets if t.enabled]
        if not targets:
            return
        clients = [self._client_for(t) for t in targets]
        self._send_message(clients, targets, address, value)

    def _send_once(self, cfg, last: dict) -> None:
        targets = [t for t in cfg.targets if t.enabled]
        if not targets:
            return
        direct = [t for t in targets if not t.bundle]
        bundlers = [t for t in targets if t.bundle]

        # gather current values (NaN rules stay silent; multiband → float list)
        values: dict[tuple, object] = {}
        rules: dict[tuple, object] = {}
        for ci, ch in enumerate(cfg.channels):
            for ri, rule in enumerate(ch.rules):
                if not rule.enabled:
                    continue
                if isinstance(rule, MultibandRule):
                    arr = self.store.multi(ci, ri)
                    if arr is None or len(arr) == 0 or any(v != v for v in arr):
                        continue
                    values[(ci, ri)] = [float(v) for v in arr]
                else:
                    val = self.store.value(ci, ri)
                    if val != val:  # NaN: no data yet / uncalibrated
                        continue
                    values[(ci, ri)] = float(val)
                rules[(ci, ri)] = rule

        # individual-mode targets: unchanged behavior (per-rule min_change gating)
        if direct:
            clients = [self._client_for(t) for t in direct]
            for key, v in values.items():
                rule = rules[key]
                prev = last.get(key)
                if prev is not None and _delta(v, prev) < rule.min_change:
                    continue
                last[key] = v
                self._send_message(clients, direct, rule.osc_pattern, v)

        # bundle-mode targets: one atomic #bundle per cycle, only when something changed
        if bundlers and values:
            changed = False
            for key, v in values.items():
                prev = self._last_bundle.get(key)
                if prev is None or _delta(v, prev) >= rules[key].min_change:
                    changed = True
                    break
            if changed:
                for t in bundlers:
                    self._send_bundle(t, values, rules)
                self._last_bundle = {k: v for k, v in values.items()}

    def _send_bundle(self, target, values, rules) -> None:
        """Build and send one OSC #bundle with every rule's current value."""
        b = osc_bundle_builder.OscBundleBuilder(osc_bundle_builder.IMMEDIATELY)
        for key, v in values.items():
            rule = rules[key]
            addr = target.prefix + rule.osc_pattern if target.prefix else rule.osc_pattern
            m = osc_message_builder.OscMessageBuilder(address=addr)
            if isinstance(v, list):
                for x in v:
                    m.add_arg(float(x))
            else:
                m.add_arg(float(v))
            b.add_content(m.build())
        bundle = b.build()
        client = self._client_for(target)
        try:
            client.send(bundle)
        except OSError as exc:
            log.warning("OSC bundle send to %s:%d failed: %s", target.host, target.port, exc)
            return
        for key, v in values.items():
            addr = target.prefix + rules[key].osc_pattern if target.prefix else rules[key].osc_pattern
            self._record(target.name, addr, v)

    def _client_for(self, target) -> udp_client.SimpleUDPClient:
        client = self._clients.get(target.name)
        if client is None:
            client = udp_client.SimpleUDPClient(target.host, target.port)
            self._clients[target.name] = client
        return client

    def _send_message(self, clients, targets, pattern, value) -> None:
        for client, target in zip(clients, targets):
            addr = target.prefix + pattern if target.prefix else pattern
            try:
                if isinstance(value, (list, tuple)):
                    # python-osc expands a list value into one arg per element
                    client.send_message(addr, [float(v) for v in value])
                    rec = [float(v) for v in value]
                else:
                    client.send_message(addr, float(value))
                    rec = float(value)
            except OSError as exc:
                log.warning("OSC send to %s:%d failed: %s", target.host, target.port, exc)
                continue
            self._record(target.name, addr, rec)

    def _record(self, target: str, address: str, value: float) -> None:
        rec = {"t": time.monotonic_ns(), "target": target, "address": address, "value": value}
        self.history.append(rec)
        if self.tap is None:
            return
        q = self.tap
        try:
            q.put_nowait(rec)
        except queue.Full:  # drop-oldest to keep the inspector fresh
            try:
                q.get_nowait()
            except queue.Empty:
                pass
            try:
                q.put_nowait(rec)
            except queue.Full:
                pass
