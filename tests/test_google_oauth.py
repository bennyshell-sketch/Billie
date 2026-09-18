"""The Google sign-in flow."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import pytest

from billie.integrations.google_oauth import GoogleOAuth, NotConnected, OAuthError
from billie.integrations.token_store import TokenStore

REDIRECT = "http://127.0.0.1:8765/api/calendar/oauth/callback"


def query_of(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(url).query).items()}


# --- step 1: the authorisation URL ---------------------------------------


def test_the_authorisation_url_uses_pkce_and_asks_for_offline_access(oauth: GoogleOAuth):
    params = query_of(oauth.authorisation_url(REDIRECT))
    assert params["response_type"] == "code"
    assert params["code_challenge_method"] == "S256"
    assert params["code_challenge"] and "code_verifier" not in params
    # Both are needed for Google to return a refresh token at all.
    assert params["access_type"] == "offline"
    assert params["prompt"] == "consent"
    assert params["redirect_uri"] == REDIRECT


def test_the_scopes_are_narrower_than_full_calendar_access(oauth: GoogleOAuth):
    scopes = query_of(oauth.authorisation_url(REDIRECT))["scope"].split()
    assert "https://www.googleapis.com/auth/calendar.events" in scopes
    assert "https://www.googleapis.com/auth/calendar" not in scopes


def test_without_a_client_id_the_error_says_what_to_do(token_store: TokenStore):
    oauth = GoogleOAuth(None, None, token_store)
    assert oauth.configured is False
    with pytest.raises(OAuthError) as excinfo:
        oauth.authorisation_url(REDIRECT)
    assert "GOOGLE_CLIENT_ID" in str(excinfo.value)


# --- step 2: coming back with a code --------------------------------------


async def test_a_successful_exchange_stores_the_refresh_token(oauth: GoogleOAuth, google):
    state = query_of(oauth.authorisation_url(REDIRECT))["state"]
    result = await oauth.exchange_code("auth-code", state)

    assert result["email"] == "ben@example.com"
    record = oauth.token_store.load(oauth.account)
    assert record["refresh_token"] == "refresh-token-abc"

    sent = google.token_requests[-1]
    assert sent["grant_type"] == "authorization_code"
    assert sent["code_verifier"], "PKCE verifier must be sent on exchange"
    assert sent["redirect_uri"] == REDIRECT


async def test_a_mismatched_state_is_rejected(oauth: GoogleOAuth):
    oauth.authorisation_url(REDIRECT)
    with pytest.raises(OAuthError) as excinfo:
        await oauth.exchange_code("auth-code", "not-the-state")
    assert "did not match" in str(excinfo.value)


async def test_a_code_with_no_sign_in_in_progress_is_rejected(oauth: GoogleOAuth):
    with pytest.raises(OAuthError) as excinfo:
        await oauth.exchange_code("auth-code", "some-state")
    assert "No sign-in is in progress" in str(excinfo.value)


async def test_an_expired_sign_in_is_rejected(oauth: GoogleOAuth):
    state = query_of(oauth.authorisation_url(REDIRECT))["state"]
    oauth._pending.created_at = time.monotonic() - 3600  # noqa: SLF001

    with pytest.raises(OAuthError) as excinfo:
        await oauth.exchange_code("auth-code", state)
    assert "took too long" in str(excinfo.value)


async def test_a_missing_refresh_token_explains_the_fix(oauth: GoogleOAuth, google):
    google.refresh_token_on_exchange = None
    state = query_of(oauth.authorisation_url(REDIRECT))["state"]

    with pytest.raises(OAuthError) as excinfo:
        await oauth.exchange_code("auth-code", state)
    assert "myaccount.google.com/permissions" in str(excinfo.value)


async def test_googles_own_error_is_passed_through(oauth: GoogleOAuth, google):
    state = query_of(oauth.authorisation_url(REDIRECT))["state"]
    google.fail_next_with = (400, "invalid_grant")

    with pytest.raises(OAuthError):
        await oauth.exchange_code("auth-code", state)


# --- ongoing: access tokens ----------------------------------------------


async def test_a_valid_access_token_is_reused(connected_oauth: GoogleOAuth, google):
    assert await connected_oauth.access_token() == "access-token-1"
    assert google.token_requests == [], "no refresh call was needed"


async def test_an_expired_access_token_is_refreshed(connected_oauth: GoogleOAuth, google):
    record = connected_oauth.token_store.load(connected_oauth.account)
    record["expires_at"] = time.time() - 10
    connected_oauth.token_store.save(connected_oauth.account, record)
    google.access_token = "access-token-2"

    assert await connected_oauth.access_token() == "access-token-2"
    assert google.token_requests[-1]["grant_type"] == "refresh_token"
    # The refresh token is kept when Google does not send a new one.
    assert connected_oauth.token_store.load(connected_oauth.account)["refresh_token"] == (
        "refresh-token-abc"
    )


async def test_asking_for_a_token_without_connecting_says_so(oauth: GoogleOAuth):
    with pytest.raises(NotConnected) as excinfo:
        await oauth.access_token()
    assert "No Google account is connected" in str(excinfo.value)


# --- disconnecting --------------------------------------------------------


async def test_disconnecting_revokes_with_google_and_forgets_the_token(
    connected_oauth: GoogleOAuth, google
):
    result = await connected_oauth.disconnect()
    assert result == {"disconnected": True, "revoked_with_google": True}
    assert google.revoked == ["refresh-token-abc"]
    assert connected_oauth.connected() is False


async def test_the_token_is_forgotten_even_if_google_is_unreachable(
    connected_oauth: GoogleOAuth, google
):
    google.fail_next_with = (500, "boom")
    result = await connected_oauth.disconnect()
    assert result["disconnected"] is True
    assert result["revoked_with_google"] is False
    assert connected_oauth.connected() is False


# --- what the UI is told --------------------------------------------------


def test_the_connection_summary_contains_no_secrets(connected_oauth: GoogleOAuth):
    summary = connected_oauth.connection()
    assert summary["connected"] is True
    assert summary["email"] == "ben@example.com"
    assert "refresh_token" not in str(summary)
    assert "access_token" not in str(summary)


async def test_the_audit_log_never_records_a_token(oauth: GoogleOAuth, audit):
    state = query_of(oauth.authorisation_url(REDIRECT))["state"]
    await oauth.exchange_code("auth-code", state)

    written = audit.path.read_text(encoding="utf-8")
    assert "refresh-token-abc" not in written
    assert "access-token-1" not in written
    assert "google.connected" in written
