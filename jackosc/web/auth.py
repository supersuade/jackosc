"""Configuration-auth boundary.

All config-mutating endpoints require :func:`require_config_auth`. Today
it is a constant-time Bearer-token check against a secret (env
``JACKOSC_AUTH_TOKEN`` or the config file), disabled by default so the
LAN tool works out of the box.

The seam is deliberately narrow so a real provider (sessions, OAuth,
per-user keys) can replace it without touching route handlers: swap
:class:`ConfigAuth` for anything exposing ``authenticate(request)`` and
wire it through the same dependency. Reads and the live WebSocket stay
open — analysis data is not secret; only configuration changes are
gated.
"""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request, status

__all__ = ["ConfigAuth", "auth_dependency"]


class ConfigAuth:
    def __init__(self, secret: str | None = None):
        self._secret = secret

    @property
    def enabled(self) -> bool:
        return self._secret is not None

    def authenticate(self, request: Request) -> None:
        """Raise 401 unless the request carries the bearer token."""
        if self._secret is None:
            return
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        if not token or not hmac.compare_digest(token, self._secret):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid or missing token",
            )


def auth_dependency(auth: ConfigAuth):
    """FastAPI dependency that enforces ConfigAuth on a route."""

    def _dep(request: Request) -> None:
        auth.authenticate(request)

    return _dep
