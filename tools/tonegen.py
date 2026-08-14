#!/usr/bin/env python3
"""JACK sine generator for soak testing.

Connects its output port to the first input port of the target client
(default: the first `jackosc` input port) and writes a sine into it.
Used by tools/soak.py; also handy standalone:

    .venv/bin/python tools/tonegen.py --freq 100 --connect-to jackosc
"""

from __future__ import annotations

import argparse
import math
import threading
import time

import jack
import numpy as np


class ToneGen:
    def __init__(self, client: jack.Client, freq: float, amp: float, burst: float = 0.0):
        self.client = client
        self.out = client.outports.register("out")
        self.freq = freq
        self.amp = amp
        self.burst = burst  # 0 = continuous; >0 = alternate tone/silence every N s
        self.phase = 0.0
        self.elapsed = 0.0

    def process(self, frames: int) -> None:
        sr = self.client.samplerate
        out = self.out.get_array()
        n = len(out)
        t = self.phase + np.arange(n, dtype=np.float64) / sr
        self.phase = (self.phase + n / sr) % (1.0 / self.freq)
        self.elapsed += n / sr
        if self.burst > 0.0 and (int(self.elapsed / self.burst) % 2) == 1:
            out[:] = 0.0  # silence half of each burst cycle (onset testing)
        else:
            out[:] = (self.amp * np.sin(2.0 * math.pi * self.freq * t)).astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--freq", type=float, default=100.0)
    ap.add_argument("--amp", type=float, default=0.5)
    ap.add_argument("--burst", type=float, default=0.0, help="alternate tone/silence every N s (onset testing)")
    ap.add_argument("--connect-to", default="jackosc", help="client-name substring to connect to")
    ap.add_argument("--seconds", type=float, default=0.0, help="0 = run until Ctrl-C")
    args = ap.parse_args()

    client = jack.Client("tonegen", no_start_server=True)
    gen = ToneGen(client, args.freq, args.amp, args.burst)
    client.set_process_callback(gen.process)
    stopped = threading.Event()
    client.set_shutdown_callback(stopped.set)
    client.activate()

    dest = None
    for port in client.get_ports(is_input=True):
        if args.connect_to in port.name:
            dest = port.name
            break
    if dest:
        client.connect(gen.out.name, dest)
        print(f"connected {gen.out.name} -> {dest}", flush=True)
    else:
        print(f"WARNING: no input port matching {args.connect_to!r}; running unconnected", flush=True)

    deadline = time.monotonic() + args.seconds if args.seconds > 0 else None
    try:
        while not stopped.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
