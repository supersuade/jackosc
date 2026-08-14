"""FastAPI application: REST config API (writes gated by ConfigAuth),
a live WebSocket stream, and the static UI."""

from __future__ import annotations

import asyncio
import logging
import queue
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import TypeAdapter, ValidationError

from jackosc.config import AppConfig, Channel, ConfigStore
from jackosc.engine import AnalysisEngine
from jackosc.rules import Rule
from jackosc.state import ValueStore
from jackosc.web.auth import ConfigAuth, auth_dependency

__all__ = ["create_app"]

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

_RULE_ADAPTER = TypeAdapter(Rule)


def _validation_detail(exc: ValidationError) -> list[dict]:
    """pydantic errors() can embed non-JSON-serializable ctx values (e.g.
    ValueError); strip ctx so the 422 response itself can't explode."""
    return [
        {k: v for k, v in err.items() if k != "ctx"}
        for err in exc.errors(include_url=False)
    ]


def _public(cfg: AppConfig, engine: AnalysisEngine, auth: ConfigAuth) -> dict:
    return {
        "config": cfg.model_dump(exclude={"auth_token"}),
        "status": engine.status(),
        "auth_enabled": auth.enabled,
    }


def _state_payload(store: ValueStore, engine: AnalysisEngine) -> dict:
    snap = store.snapshot()
    values = snap["values"].tolist()
    values = [[None if v != v else round(float(v), 6) for v in row] for row in values]
    multi = {
        f"{ci}:{ri}": [None if v != v else round(float(v), 6) for v in arr]
        for (ci, ri), arr in snap["multi"].items()
    }
    spectra = {str(k): [float(x) for x in mag] for k, mag in snap["spectra"].items()}
    cfg = engine.config
    return {
        "type": "state",
        "status": engine.status(),
        "channels": (
            [{"name": c.name, "window": c.window, "hop": c.hop} for c in cfg.channels]
            if cfg
            else []
        ),
        "values": values,
        "multi": multi,
        "rule_ids": snap["rule_ids"],
        "spectra": spectra,
    }


def create_app(
    engine: AnalysisEngine,
    store: ValueStore,
    cfg_store: ConfigStore,
    auth: ConfigAuth,
) -> FastAPI:
    app = FastAPI(title="jackosc", version="0.1.0")
    app.state.engine = engine
    app.state.store = store
    app.state.cfg_store = cfg_store
    app.state.auth = auth
    write_auth = Depends(auth_dependency(auth))

    def _mutate(new_cfg: AppConfig) -> dict:
        engine.apply_config(new_cfg)
        if new_cfg.autosave:
            cfg_store.save(new_cfg)
        return _public(new_cfg, engine, auth)

    # -- config -------------------------------------------------------

    @app.get("/api/config")
    async def get_config():
        if engine.config is None:
            raise HTTPException(status_code=503, detail="no configuration loaded")
        return _public(engine.config, engine, auth)

    @app.put("/api/config")
    async def put_config(request: Request, _=write_auth):
        body = await request.json()
        body.pop("auth_token", None)  # token comes from env/file only
        try:
            cfg = AppConfig.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_validation_detail(exc))
        try:
            return _mutate(cfg)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    # -- channels -----------------------------------------------------

    @app.post("/api/channels")
    async def add_channel(request: Request, _=write_auth):
        body = await request.json()
        cfg = engine.config
        if cfg is None:
            raise HTTPException(status_code=503, detail="no configuration loaded")
        if any(c.name == body.get("name") for c in cfg.channels):
            raise HTTPException(status_code=409, detail="channel exists")
        new_cfg = cfg.model_copy(deep=True)
        try:
            new_cfg.channels.append(Channel.model_validate(body))
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_validation_detail(exc))
        return _mutate(new_cfg)

    @app.delete("/api/channels/{name}")
    async def remove_channel(name: str, _=write_auth):
        cfg = engine.config
        if cfg is None:
            raise HTTPException(status_code=503, detail="no configuration loaded")
        new_cfg = cfg.model_copy(deep=True)
        before = len(new_cfg.channels)
        new_cfg.channels = [c for c in new_cfg.channels if c.name != name]
        if len(new_cfg.channels) == before:
            raise HTTPException(status_code=404, detail="no such channel")
        return _mutate(new_cfg)

    # -- rules ---------------------------------------------------------

    @app.post("/api/channels/{name}/rules")
    async def add_rule(name: str, request: Request, _=write_auth):
        body = await request.json()
        cfg = engine.config
        if cfg is None:
            raise HTTPException(status_code=503, detail="no configuration loaded")
        new_cfg = cfg.model_copy(deep=True)
        ch = next((c for c in new_cfg.channels if c.name == name), None)
        if ch is None:
            raise HTTPException(status_code=404, detail="no such channel")
        try:
            ch.rules.append(_RULE_ADAPTER.validate_python(body))
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=_validation_detail(exc))
        try:
            return _mutate(new_cfg)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.delete("/api/channels/{name}/rules/{idx}")
    async def remove_rule(name: str, idx: int, _=write_auth):
        cfg = engine.config
        if cfg is None:
            raise HTTPException(status_code=503, detail="no configuration loaded")
        new_cfg = cfg.model_copy(deep=True)
        ch = next((c for c in new_cfg.channels if c.name == name), None)
        if ch is None or not 0 <= idx < len(ch.rules):
            raise HTTPException(status_code=404, detail="no such rule")
        del ch.rules[idx]
        return _mutate(new_cfg)

    @app.post("/api/channels/{name}/rules/{idx}/calibrate")
    async def calibrate_rule(name: str, idx: int, request: Request, _=write_auth):
        body = await request.json()
        seconds = float(body.get("seconds", 3.0))
        band = body.get("band")
        band = int(band) if band is not None else None
        try:
            result = await asyncio.to_thread(engine.calibrate, name, idx, seconds, band)
        except KeyError:
            raise HTTPException(status_code=404, detail="no such channel")
        except (ValueError, IndexError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if engine.config.autosave:
            cfg_store.save(engine.config)
        result["config"] = _public(engine.config, engine, auth)
        return result

    # -- profiles -------------------------------------------------------

    @app.get("/api/profiles")
    async def list_profiles():
        return {"profiles": cfg_store.list_profiles()}

    @app.post("/api/profiles/{name}")
    async def save_profile(name: str, _=write_auth):
        if engine.config is None:
            raise HTTPException(status_code=503, detail="no configuration loaded")
        try:
            path = cfg_store.save_profile(name, engine.config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "path": str(path)}

    @app.post("/api/profiles/{name}/load")
    async def load_profile(name: str, _=write_auth):
        try:
            cfg = cfg_store.load_profile(name)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="no such profile")
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return _mutate(cfg)

    @app.delete("/api/profiles/{name}")
    async def delete_profile(name: str, _=write_auth):
        try:
            cfg_store.delete_profile(name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True}

    # -- OSC packet inspector -------------------------------------------

    @app.get("/api/packets")
    async def get_packets(limit: int = 200):
        return {"packets": engine.packets(limit)}

    @app.post("/api/packets/test")
    async def test_packet(request: Request, _=write_auth):
        body = await request.json()
        address = str(body.get("address", "/test"))
        value = float(body.get("value", 0.0))
        n = engine.send_test(address, value)
        return {"queued": n > 0, "targets": n}

    @app.websocket("/ws/packets")
    async def ws_packets(websocket: WebSocket):
        """Streams emitted packets as they happen. One tap queue is shared
        by all subscribers (single-user LAN tool); the first to drain wins.

        Sends a periodic empty heartbeat so a client that disconnected
        while the tap was idle doesn't leave a zombie handler draining
        the shared queue (which would steal packets from live clients).
        """
        await websocket.accept()
        tap = engine.tap
        try:
            while True:
                batch: list[dict] = []
                for _ in range(200):
                    try:
                        batch.append(tap.get_nowait())
                    except queue.Empty:
                        break
                if batch:
                    await websocket.send_json({"type": "packets", "packets": batch})
                    continue
                # idle: receive() returns a disconnect message as soon as the
                # client closes, so a dead connection can't leave a zombie
                # handler draining the shared tap (which would steal packets)
                try:
                    msg = await asyncio.wait_for(websocket.receive(), timeout=0.05)
                except asyncio.TimeoutError:
                    continue
                if msg["type"] == "websocket.disconnect":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass

    # -- live stream ----------------------------------------------------

    @app.websocket("/ws")
    async def ws_live(websocket: WebSocket):
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(_state_payload(store, engine))
                await asyncio.sleep(1 / 30.0)
        except (WebSocketDisconnect, RuntimeError):
            pass

    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app
