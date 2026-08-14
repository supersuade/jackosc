"""Command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="jackosc",
        description="JACK audio to OSC bridge with a live analysis web UI",
    )
    p.add_argument("--config", type=str, default=None, help="config file (default: ~/.config/jackosc/config.json)")
    p.add_argument("--jack-name", type=str, default=None, help="JACK client name (overrides config)")
    p.add_argument("--host", default="127.0.0.1", help="web bind host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8080, help="web port (default 8080)")
    p.add_argument("--no-web", action="store_true", help="run without the web server")
    p.add_argument("--auth-token", default=None, help="require this bearer token for config writes (env JACKOSC_AUTH_TOKEN)")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--version", action="version", version=f"jackosc {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("jackosc")

    store = ConfigStore(Path(args.config) if args.config else default_config_path())
    try:
        cfg = store.load()
    except ValueError as exc:
        log.error("%s", exc)
        return 2
    if args.jack_name:
        cfg = cfg.model_copy(update={"jack_name": args.jack_name})

    secret = os.environ.get("JACKOSC_AUTH_TOKEN") or args.auth_token or cfg.auth_token

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

        app = create_app(engine, state, store, ConfigAuth(secret))
        log.info("web UI at http://%s:%d (config writes%s)", args.host, args.port,
                 " require auth token" if secret else " open")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
