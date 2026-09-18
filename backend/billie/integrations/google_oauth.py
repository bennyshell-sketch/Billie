"""Google OAuth for a desktop app.

Google's own recommendation for an installed application is the authorisation
code flow with a loopback redirect and PKCE -- not the device flow, which
covers a limited set of scopes and does not include Calendar. Billie already
runs a local HTTP server, so its own port is the redirect target and no second
server is needed.

The client ID and secret come from `.env`; the refresh token comes back from
Google and goes straight into the OS keychain (see `token_store.py`). Neither
is ever written to the audit log.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from billie.integrations.token_store import TokenStore

logger = logging.getLogger("billie.oauth")

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
USERINFO_ENDPOINT = "https://www.googleapis.com/oauth2/v3/userinfo"

#: Read events and calendars, and write events. Deliberately not the full
#: `calendar` scope, which would also let Billie create and delete calendars.
CALENDAR_SCOPES = (
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/calendar.events",
)
ACCOUNT_KEY = "google-calendar"

#: Refresh a little before expiry rather than after a failed call.
EXPIRY_MARGIN_SECONDS = 120


class OAuthError(RuntimeError):
    """The message is shown to the user, so it explains the next step."""


class NotConnected(OAuthError):
    """No Google account has been connected yet."""


@dataclass
class PendingAuthorisation:
    state: str
    code_verifier: str
    redirect_uri: str
    created_at: float = field(default_factory=time.monotonic)

    def expired(self, ttl_seconds: float = 600.0) -> bool:
        return time.monotonic() - self.created_at > ttl_seconds


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


class GoogleOAuth:
    """Runs the authorisation dance and keeps the access token fresh."""

    def __init__(
        self,
        client_id: str | None,
        client_secret: str | None,
        token_store: TokenStore,
        *,
        scopes: tuple[str, ...] = CALENDAR_SCOPES,
        account: str = ACCOUNT_KEY,
        client: httpx.AsyncClient | None = None,
        audit=None,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_store = token_store
        self.scopes = scopes
        self.account = account
        self._client = client
        self.audit = audit
        self._pending: PendingAuthorisation | None = None

    # ------------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def connected(self) -> bool:
        return self.token_store.load(self.account) is not None

    def connection(self) -> dict[str, Any]:
        """Everything the UI needs, with no secrets in it."""
        record = self.token_store.load(self.account) or {}
        return {
            "configured": self.configured,
            "connected": bool(record),
            "email": record.get("email"),
            "scopes": list(record.get("scopes") or []),
            "connected_at": record.get("connected_at"),
            "storage": self.token_store.describe(),
        }

    def _http(self) -> httpx.AsyncClient:
        return self._client or httpx.AsyncClient(timeout=30.0)

    async def _post(self, url: str, data: dict[str, str]) -> dict[str, Any]:
        client = self._http()
        try:
            response = await client.post(url, data=data)
        except httpx.HTTPError as exc:
            raise OAuthError(f"Could not reach Google ({exc}).") from exc
        finally:
            if self._client is None:
                await client.aclose()

        if response.status_code >= 400:
            detail = ""
            try:
                payload = response.json()
                detail = payload.get("error_description") or payload.get("error") or ""
            except ValueError:
                detail = response.text[:200]
            raise OAuthError(f"Google rejected the request ({response.status_code}): {detail}")
        return response.json()

    # ------------------------------------------------------------------
    # Step 1: send the user to Google
    # ------------------------------------------------------------------
    def authorisation_url(self, redirect_uri: str) -> str:
        if not self.configured:
            raise OAuthError(
                "GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are not set in .env. "
                "Create an OAuth client of type 'Desktop app' in the Google Cloud "
                "console and paste them in."
            )
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(24)
        self._pending = PendingAuthorisation(
            state=state, code_verifier=verifier, redirect_uri=redirect_uri
        )
        query = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            # Both are needed to be handed a refresh token.
            "access_type": "offline",
            "prompt": "consent",
        }
        return f"{AUTH_ENDPOINT}?{urlencode(query)}"

    # ------------------------------------------------------------------
    # Step 2: Google sends the user back with a code
    # ------------------------------------------------------------------
    async def exchange_code(self, code: str, state: str) -> dict[str, Any]:
        pending = self._pending
        if pending is None:
            raise OAuthError("No sign-in is in progress. Start again from the UI.")
        if pending.expired():
            self._pending = None
            raise OAuthError("That sign-in took too long. Start again from the UI.")
        if not secrets.compare_digest(state, pending.state):
            raise OAuthError("The sign-in response did not match this session. Start again.")

        payload = await self._post(
            TOKEN_ENDPOINT,
            {
                "code": code,
                "client_id": self.client_id or "",
                "client_secret": self.client_secret or "",
                "redirect_uri": pending.redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": pending.code_verifier,
            },
        )
        self._pending = None

        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise OAuthError(
                "Google did not return a refresh token. Remove Billie's access at "
                "https://myaccount.google.com/permissions and connect again."
            )

        email = await self._fetch_email(payload.get("access_token", ""))
        record = {
            "refresh_token": refresh_token,
            "access_token": payload.get("access_token"),
            "expires_at": time.time() + float(payload.get("expires_in", 3600)),
            "scopes": (payload.get("scope") or " ".join(self.scopes)).split(),
            "email": email,
            "connected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        backend = self.token_store.save(self.account, record)
        if self.audit:
            self.audit.record(
                "google.connected", account=email, scopes=record["scopes"], storage=backend
            )
        return {"email": email, "scopes": record["scopes"], "storage": backend}

    async def _fetch_email(self, access_token: str) -> str | None:
        if not access_token:
            return None
        client = self._http()
        try:
            response = await client.get(
                USERINFO_ENDPOINT, headers={"Authorization": f"Bearer {access_token}"}
            )
            if response.status_code == 200:
                return response.json().get("email")
        except httpx.HTTPError:
            pass
        finally:
            if self._client is None:
                await client.aclose()
        return None

    # ------------------------------------------------------------------
    # Ongoing: a valid access token
    # ------------------------------------------------------------------
    async def access_token(self) -> str:
        record = self.token_store.load(self.account)
        if record is None:
            raise NotConnected(
                "No Google account is connected. Connect one under Calendar in the UI."
            )
        if record.get("access_token") and record.get("expires_at", 0) > (
            time.time() + EXPIRY_MARGIN_SECONDS
        ):
            return record["access_token"]

        payload = await self._post(
            TOKEN_ENDPOINT,
            {
                "client_id": self.client_id or "",
                "client_secret": self.client_secret or "",
                "refresh_token": record["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        record["access_token"] = payload.get("access_token")
        record["expires_at"] = time.time() + float(payload.get("expires_in", 3600))
        # Google only re-issues a refresh token occasionally; keep the old one
        # when it does not.
        if payload.get("refresh_token"):
            record["refresh_token"] = payload["refresh_token"]
        self.token_store.save(self.account, record)
        if self.audit:
            self.audit.record("google.token_refreshed", account=record.get("email"))
        return record["access_token"]

    async def disconnect(self) -> dict[str, Any]:
        record = self.token_store.load(self.account)
        revoked = False
        if record and record.get("refresh_token"):
            try:
                await self._post(REVOKE_ENDPOINT, {"token": record["refresh_token"]})
                revoked = True
            except OAuthError as exc:
                logger.warning("Revoking the token with Google failed: %s", exc)
        removed = self.token_store.delete(self.account)
        if self.audit:
            self.audit.record(
                "google.disconnected",
                account=(record or {}).get("email"),
                revoked_with_google=revoked,
            )
        return {"disconnected": removed, "revoked_with_google": revoked}
