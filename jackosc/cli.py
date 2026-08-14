"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import socket
import subprocess
import sys
import time
from pathlib import Path

from jackosc import __version__
from jackosc.config import ConfigStore, default_config_path
from jackosc.engine import AnalysisEngine
from jackosc.state import ValueStore
from jackosc.web.auth import ConfigAuth
from jackosc.web.server import create_app

__all__ = ["main"]

# systemd user unit template; `jackosc systemd install` materializes it with
# the exact interpreter that is running.
_SYSTEMD_UNIT = """\
[Unit]
Description=jackosc — JACK audio to OSC bridge
# User unit: JACK lives inside the login session (PipeWire-JACK via rtkit),
# so the bridge must run in the same session — it inherits the JACK graph,
# rtkit privileges, and your XDG paths. No sudo, no User=.
After=pipewire.service
Wants=pipewire.service

[Service]
Type=simple
{execstart}
# Config default: ~/.config/jackosc/config.json (created on first save in the UI)
{env}# Restrict web config writes to yourself:
# Environment=JACKOSC_AUTH_TOKEN=change-me
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jackosc",
        description="JACK audio to OSC bridge with a live analysis web UI",
    )
    p.add_argument("--config", type=str, default=None, help="config file (default: ~/.config/jackosc/config.json)")
    p.add_argument("--jack-name", type=str, default=None, help="JACK client name (overrides config)")
    p.add_argument("--host", default=None, help="web bind host (default 127.0.0.1, or 0.0.0.0 with --lan)")
    p.add_argument("--port", type=int, default=8080, help="web port (default 8080)")
    p.add_argument("--lan", action="store_true", help="bind all interfaces for LAN access (0.0.0.0); warns if config writes are open")
    p.add_argument("--no-web", action="store_true", help="run without the web server")
    p.add_argument("--auth-token", default=None, help="require this bearer token for config writes (env JACKOSC_AUTH_TOKEN)")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"jackosc {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="command")
    sysd = sub.add_parser("systemd", help="manage the systemd user unit")
    sysa = sysd.add_subparsers(dest="action", required=True, metavar="action")
    ins = sysa.add_parser("install", help="write ~/.config/systemd/user/jackosc.service, enable and start it")
    ins.add_argument("--host", default=None, help="web bind host (default 127.0.0.1, or 0.0.0.0 with --lan)")
    ins.add_argument("--port", type=int, default=8080, help="web port (default 8080)")
    ins.add_argument("--lan", action="store_true", help="bind all interfaces for LAN access; warns if no JACKOSC_AUTH_TOKEN")
    ins.add_argument("--no-enable", action="store_true", help="write the unit but don't enable it")
    ins.add_argument("--no-start", action="store_true", help="write + enable but don't start it")
    sysa.add_parser("uninstall", help="stop, disable and remove the unit")
    sysa.add_parser("status", help="show `systemctl --user status jackosc`")
    return p


def _systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "systemd" / "user"


def resolve_host(host: str | None, lan: bool) -> str:
    """--host wins; otherwise --lan implies all interfaces."""
    return host or ("0.0.0.0" if lan else "127.0.0.1")


def _lan_ip() -> str | None:
    """Primary LAN address, found via a connect() that sends no packets."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except OSError:
        return None


def cmd_systemd(args: argparse.Namespace, log: logging.Logger) -> int:
    unit = _systemd_user_dir() / "jackosc.service"

    def systemctl(*a: str) -> subprocess.CompletedProcess | None:
        try:
            return subprocess.run(["systemctl", "--user", *a], capture_output=True, text=True)
        except FileNotFoundError:
            log.error("systemctl not found — no systemd user session on this host")
            return None

    if args.action == "install":
        host = resolve_host(args.host, args.lan)
        token = os.environ.get("JACKOSC_AUTH_TOKEN")
        if args.lan and not token:
            log.warning("--lan exposes the web UI to the LAN and config writes are OPEN — "
                        "set JACKOSC_AUTH_TOKEN and re-run install to gate them")
        env_line = f"Environment=JACKOSC_AUTH_TOKEN={token.replace('%', '%%')}\n" if token else ""
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(
            _SYSTEMD_UNIT.format(
                execstart=f"ExecStart={shlex.quote(sys.executable)} -m jackosc --host {host} --port {args.port}",
                env=env_line,
            ),
        )
        log.info("wrote %s", unit)
        r = systemctl("daemon-reload")
        if r is None:
            return 3
        if r.returncode != 0:
            log.error("daemon-reload failed: %s", r.stderr.strip())
            return 3
        if not args.no_enable:
            if (r := systemctl("enable", "jackosc.service")).returncode != 0:
                log.error("enable failed: %s", r.stderr.strip())
                return 3
        if not args.no_start:
            if (r := systemctl("start", "jackosc.service")).returncode != 0:
                log.error("start failed: %s", r.stderr.strip())
                return 3
            log.info("started — see: systemctl --user status jackosc")
        else:
            log.info("unit installed but not started — start with: systemctl --user start jackosc")
        log.info("config file: %s", default_config_path())
        log.info("next: patch JACK sources into channels from the web UI (or with jack_connect)")
        return 0

    if args.action == "uninstall":
        systemctl("stop", "jackosc.service")  # may already be stopped — ignore
        systemctl("disable", "jackosc.service")
        if unit.exists():
            unit.unlink()
            systemctl("daemon-reload")
            log.info("removed %s", unit)
        else:
            log.info("no unit at %s", unit)
        return 0

    if args.action == "status":
        r = systemctl("status", "jackosc.service")
        if r is None:
            return 3
        print((r.stdout + r.stderr).rstrip())
        return r.returncode
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("jackosc")

    if args.cmd == "systemd":
        return cmd_systemd(args, log)

    store = ConfigStore(Path(args.config) if args.config else default_config_path())
    try:
        cfg = store.load()
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    if args.jack_name:
        cfg = cfg.model_copy(update={"jack_name": args.jack_name})

    secret = os.environ.get("JACKOSC_AUTH_TOKEN") or args.auth_token or cfg.auth_token

    if args.lan and not secret:
        log.warning("--lan exposes the web UI to the LAN and config writes are OPEN — "
                    "set JACKOSC_AUTH_TOKEN (or pass --auth-token) to gate writes")

    state = ValueStore()
    engine = AnalysisEngine(state)
    try:
        engine.apply_config(cfg)
    except ValueError as exc:
        log.error("invalid config: %s", exc)
        return 2

    if engine.audio_available:
        st = engine.status()
        log.info("audio up: %s @ %.0f Hz, %d frames/period", cfg.jack_name, st["samplerate"], st["blocksize"])
    else:
        log.warning("audio unavailable — web/OSC still run; values stay silent")

    try:
        if args.no_web:
            log.info("running without web server (Ctrl-C to stop)")
            while True:
                time.sleep(3600)
        import uvicorn

        host = resolve_host(args.host, args.lan)
        app = create_app(engine, state, store, ConfigAuth(secret))
        if host == "0.0.0.0":
            lan = _lan_ip()
            log.info("web UI at http://%s:%d (LAN: http://%s:%d, config writes%s)",
                     host, args.port, lan or "<iface>", args.port,
                     " require auth token" if secret else " OPEN")
        else:
            log.info("web UI at http://%s:%d (config writes%s)", host, args.port,
                     " require auth token" if secret else " open")
        uvicorn.run(app, host=host, port=args.port, log_level="info")
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
