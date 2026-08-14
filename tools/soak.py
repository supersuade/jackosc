#!/usr/bin/env python3
"""Soak test for jackosc.

Spawns a tone generator connected to a running jackosc instance, listens
for the OSC output on a UDP port, and polls the jackosc API for audio
health: xruns, ring drops, and process-callback timing.

    .venv/bin/python tools/soak.py --base-url http://127.0.0.1:8080 \
        --seconds 1800 --freq 100

Adds a temporary `soaksink` target (removed afterwards) so the sink port
never collides with your real targets. Exit 0 = PASS (no xruns, no drops,
callback p99 within budget, OSC packets flowing).
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

SINK_TARGET = "soaksink"


def api(base: str, path: str, method: str = "GET", body=None, token: str | None = None) -> dict:
    req = urllib.request.Request(base + path, method=method)
    if body is not None:
        req.add_header("Content-Type", "application/json")
        req.data = json.dumps(body).encode()
    if token:
        req.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--jack-name", default="jackosc", help="client name tonegen connects to")
    ap.add_argument("--freq", type=float, default=100.0)
    ap.add_argument("--seconds", type=float, default=300.0)
    ap.add_argument("--target-port", type=int, default=9102)
    ap.add_argument("--cb-budget-us", type=float, default=2000.0, help="callback p99 ceiling (µs)")
    ap.add_argument("--token", default=os.environ.get("JACKOSC_AUTH_TOKEN"), help="auth token for config writes")
    args = ap.parse_args()

    # preflight: reachable + audio on
    try:
        cfg = api(args.base_url, "/api/config")
    except Exception as exc:
        print(f"preflight FAIL: cannot reach {args.base_url}: {exc}")
        return 1
    st = cfg["status"]
    if not st.get("audio"):
        print(f"preflight FAIL: audio off ({st.get('audio_error')}) — is jackosc running and connected to JACK?")
        return 1
    print(f"audio: {st['samplerate']:.0f} Hz, {st['blocksize']} frames/period, budget {st['blocksize']/st['samplerate']*1e6:.0f} µs")

    # ensure a sink target exists (restored at the end)
    original = cfg["config"]
    has_sink = any(
        t["name"] == SINK_TARGET and t["host"] == "127.0.0.1" and t["port"] == args.target_port
        for t in original["targets"]
    )
    if not has_sink:
        cfg2 = json.loads(json.dumps(original))
        cfg2["targets"].append(
            {"name": SINK_TARGET, "host": "127.0.0.1", "port": args.target_port, "enabled": True, "prefix": ""}
        )
        api(args.base_url, "/api/config", "PUT", cfg2, args.token)
        print(f"added temporary target {SINK_TARGET}:{args.target_port}")

    # UDP sink
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", args.target_port))
    except OSError as exc:
        print(f"FAIL: cannot bind sink on {args.target_port}: {exc}")
        return 1
    sock.settimeout(0.2)

    tonegen = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).parent / "tonegen.py"),
            "--freq", str(args.freq),
            "--connect-to", args.jack_name,
            "--seconds", str(args.seconds + 5),
        ]
    )

    packets = 0
    polls = []
    start = time.monotonic()
    deadline = start + args.seconds
    next_poll = start
    try:
        while time.monotonic() < deadline:
            try:
                while True:
                    sock.recvfrom(2048)
                    packets += 1
                    if time.monotonic() >= deadline:
                        break
            except socket.timeout:
                pass
            now = time.monotonic()
            if now >= next_poll:
                next_poll = now + 2.0
                st = api(args.base_url, "/api/config")["status"]
                polls.append(st)
                rate = packets / max(now - start, 0.1)
                print(
                    f"  t={now-start:6.1f}s xruns={st['xruns']} dropped={st['dropped']} "
                    f"cb p99={st.get('cb_p99_us', 0)}µs max={st.get('cb_max_us', 0)}µs "
                    f"osc={packets} ({rate:.1f}/s)",
                    flush=True,
                )
    except KeyboardInterrupt:
        print("interrupted")
    finally:
        try:
            tonegen.wait(timeout=10)
        except subprocess.TimeoutExpired:
            tonegen.kill()
        sock.close()
        if not has_sink:
            try:
                restored = json.loads(json.dumps(original))
                api(args.base_url, "/api/config", "PUT", restored, args.token)
                print("restored original config (sink target removed)")
            except Exception as exc:
                print(f"WARNING: could not restore config: {exc}")

    duration = time.monotonic() - start
    xruns = max(p["xruns"] for p in polls) if polls else 0
    dropped = max(p["dropped"] for p in polls) if polls else 0
    p99 = max(p.get("cb_p99_us", 0) for p in polls) if polls else 0
    p50 = sum(p.get("cb_p50_us", 0) for p in polls) / len(polls) if polls else 0
    maxcb = max(p.get("cb_max_us", 0) for p in polls) if polls else 0
    rate = packets / max(duration, 0.1)

    print("\n=== soak summary ===")
    print(f"duration: {duration:.1f}s, polls: {len(polls)}")
    print(f"xruns: {xruns} (must be 0)")
    print(f"ring drops: {dropped} (must be 0)")
    print(f"callback: avg p50 {p50:.1f}µs, max p99 {p99:.1f}µs, max {maxcb:.1f}µs (budget {args.cb_budget_us:.0f}µs)")
    print(f"osc packets: {packets} ({rate:.1f}/s)")

    ok = xruns == 0 and dropped == 0 and p99 < args.cb_budget_us and packets > 0
    print("RESULT: PASS" if ok else "RESULT: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
