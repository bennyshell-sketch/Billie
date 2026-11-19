"""Bearer-token auth for the local API.

Phase 1 runs on localhost, where a token is optional. Phase 6 exposes the API
to your phones over Tailscale, and from then on a token is required for any
request that does not come from this machine. The rule is enforced here from
the start so the boundary does not have to be retrofitted.
"""

from __future__ import annotations

import ipaddress
import secrets

from fastapi import Depends, HTTPException, Request, status

from billie.config import Settings, get_settings

LOOPBACK_HOSTS = {"localhost", "::1"}


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host in LOOPBACK_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _presented_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-billie-token") or None


def settings_from_app(request: Request) -> Settings:
    """Prefer the settings the app was built with; fall back to the global ones."""
    return getattr(request.app.state, "settings", None) or get_settings()


def require_auth(
    request: Request, settings: Settings = Depends(settings_from_app)
) -> None:
    """Allow the request, or raise 401.

    * No token configured -> loopback is allowed, anything else is refused.
    * Token configured -> loopback may be exempt (configurable); everyone else
      must present the token.
    """
    client_host = request.client.host if request.client else None
    loopback = _is_loopback(client_host)

    if not settings.api_token:
        if loopback:
            return
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "No BILLIE_API_TOKEN is configured, so only requests from this "
                "machine are accepted. Set BILLIE_API_TOKEN in .env to allow "
                "remote access."
            ),
        )

    if loopback and settings.allow_loopback_unauthenticated:
        return

    presented = _presented_token(request)
    if not presented or not secrets.compare_digest(presented, settings.api_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid API token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
