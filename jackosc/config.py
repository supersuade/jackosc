"""Persistent configuration: channels, rules, OSC targets, profiles.

Saved as JSON with atomic writes (temp file + rename + fsync). Default
location follows XDG_CONFIG_HOME. ``auth_token`` may be set in the file
or via JACKOSC_AUTH_TOKEN (env wins) and is never serialized back out.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from jackosc.rules import Rule

__all__ = [
    "AppConfig",
    "Channel",
    "Target",
    "ConfigStore",
    "default_config_path",
]

_PROFILE_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class Channel(BaseModel):
    """One JACK input port plus its rule set."""

    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    connect_to: str | None = Field(
        default=None,
        description='JACK source port to auto-connect ("auto" = system:capture_N); None = manual patch',
    )
    window: int = Field(default=1024, ge=64, description="FFT window, samples")
    hop: int = Field(default=512, ge=1, description="window advance, samples")
    rules: list[Rule] = Field(default_factory=list)


class Target(BaseModel):
    """An OSC destination; messages fan out to all enabled targets."""

    name: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    host: str
    port: int = Field(ge=1, le=65535)
    enabled: bool = True
    prefix: str = Field(default="", description="prepended to every rule osc_pattern")
    bundle: bool = Field(
        default=False,
        description="send all rule values as one OSC #bundle per cycle (atomic snapshot)",
    )


class AppConfig(BaseModel):
    version: int = 1
    jack_name: str = "jackosc"
    auto_connect: bool = Field(
        default=False,
        description="treat connect_to=None as 'auto' (system:capture_N); explicit 'auto' always connects",
    )
    sample_rate: float | None = Field(default=None, description="override analysis rate; default = JACK's")
    osc_rate: float = Field(default=60.0, ge=1.0, le=1000.0, description="OSC send cadence, Hz")
    cb_warn_us: float | None = Field(
        default=None,
        ge=1.0,
        description="UI status icon turns red when callback p99 >= this (µs); None = 25% of the period budget",
    )
    channels: list[Channel] = Field(default_factory=list)
    targets: list[Target] = Field(default_factory=list)
    auth_token: str | None = Field(
        default=None,
        description="bearer token for config writes; never returned by the API",
    )
    autosave: bool = True


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "jackosc" / "config.json"


class ConfigStore:
    """Atomic JSON persistence with named profiles."""

    def __init__(self, path: Path | None = None):
        self.path = path or default_config_path()

    def load(self) -> AppConfig:
        if not self.path.exists():
            return AppConfig()
        try:
            return AppConfig.model_validate_json(self.path.read_text())
        except Exception as exc:
            raise ValueError(f"invalid config {self.path}: {exc}") from exc

    def save(self, cfg: AppConfig) -> None:
        self._atomic_write(self.path, cfg.model_dump_json(indent=2))

    def save_profile(self, name: str, cfg: AppConfig) -> Path:
        if not _PROFILE_RE.fullmatch(name):
            raise ValueError("profile name: letters, digits, '_', '-' only")
        data = cfg.model_dump(exclude={"auth_token"})
        profile = AppConfig.model_validate(data)
        target = self.path.parent / f"{name}.json"
        if target == self.path:
            target = self.path.parent / f"{name}-profile.json"
        self._atomic_write(target, profile.model_dump_json(indent=2))
        return target

    def load_profile(self, name: str) -> AppConfig:
        path = self.path.parent / f"{name}.json"
        if not path.exists():
            raise FileNotFoundError(f"no profile {name!r} at {path}")
        return AppConfig.model_validate_json(path.read_text())

    def delete_profile(self, name: str) -> None:
        path = self.path.parent / f"{name}.json"
        if path == self.path:
            raise ValueError("cannot delete the default config file")
        path.unlink(missing_ok=True)

    def list_profiles(self) -> list[str]:
        return sorted(p.stem for p in self.path.parent.glob("*.json") if p != self.path)

    @staticmethod
    def _atomic_write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass
