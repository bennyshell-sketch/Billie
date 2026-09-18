"""Where OAuth refresh tokens are kept."""

from __future__ import annotations

import json
import os
import stat

import pytest

from billie.integrations.token_store import TokenStore, TokenStoreUnavailable


class FakeKeyring:
    """Stands in for the `keyring` module and its OS backend."""

    class _Backend:
        pass

    def __init__(self) -> None:
        self.storage: dict[tuple[str, str], str] = {}

    def get_keyring(self):
        return self._Backend()

    def set_password(self, service, account, value):
        self.storage[(service, account)] = value

    def get_password(self, service, account):
        return self.storage.get((service, account))

    def delete_password(self, service, account):
        del self.storage[(service, account)]


@pytest.fixture
def keychain(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(TokenStore, "_backend", lambda self: fake)
    return fake


# --- the keychain path ----------------------------------------------------


def test_a_token_round_trips_through_the_keychain(keychain, tmp_path):
    store = TokenStore(fallback_path=tmp_path / "credentials.json")
    assert store.save("google-calendar", {"refresh_token": "secret"}) == "os-keychain"
    assert store.load("google-calendar") == {"refresh_token": "secret"}
    assert store.describe()["secure"] is True
    assert store.describe()["backend"] == "os-keychain"


def test_the_keychain_is_preferred_over_the_file(keychain, tmp_path):
    fallback = tmp_path / "credentials.json"
    store = TokenStore(fallback_path=fallback, allow_file=True)
    store.save("google-calendar", {"refresh_token": "secret"})
    assert not fallback.exists(), "nothing should be written to disk"


def test_deleting_removes_it_from_the_keychain(keychain, tmp_path):
    store = TokenStore(fallback_path=tmp_path / "credentials.json")
    store.save("google-calendar", {"refresh_token": "secret"})
    assert store.delete("google-calendar") is True
    assert store.load("google-calendar") is None
    assert store.delete("google-calendar") is False


# --- no keychain available ------------------------------------------------


def test_without_a_keychain_saving_refuses_rather_than_writing_a_file(tmp_path, monkeypatch):
    monkeypatch.setattr(TokenStore, "_backend", lambda self: None)
    store = TokenStore(fallback_path=tmp_path / "credentials.json", allow_file=False)

    with pytest.raises(TokenStoreUnavailable) as excinfo:
        store.save("google-calendar", {"refresh_token": "secret"})
    assert "BILLIE_ALLOW_FILE_TOKEN_STORE" in str(excinfo.value)
    assert not (tmp_path / "credentials.json").exists()
    assert store.describe()["backend"] == "none"
    assert store.describe()["secure"] is False


def test_the_file_fallback_is_opt_in_and_flagged_as_insecure(tmp_path, monkeypatch):
    monkeypatch.setattr(TokenStore, "_backend", lambda self: None)
    path = tmp_path / "credentials.json"
    store = TokenStore(fallback_path=path, allow_file=True)

    assert store.save("google-calendar", {"refresh_token": "secret"}) == "file"
    assert store.load("google-calendar") == {"refresh_token": "secret"}
    described = store.describe()
    assert described["backend"] == "file"
    assert described["secure"] is False
    assert "file permissions only" in described["detail"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_fallback_file_is_readable_only_by_you(tmp_path, monkeypatch):
    monkeypatch.setattr(TokenStore, "_backend", lambda self: None)
    path = tmp_path / "credentials.json"
    TokenStore(fallback_path=path, allow_file=True).save("acct", {"refresh_token": "s"})

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600, f"expected owner-only, got {oct(mode)}"


def test_loading_from_a_missing_or_corrupt_file_returns_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(TokenStore, "_backend", lambda self: None)
    path = tmp_path / "credentials.json"
    store = TokenStore(fallback_path=path, allow_file=True)
    assert store.load("google-calendar") is None

    path.write_text("{ not json", encoding="utf-8")
    assert store.load("google-calendar") is None


def test_deleting_from_the_file_leaves_other_accounts_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(TokenStore, "_backend", lambda self: None)
    path = tmp_path / "credentials.json"
    store = TokenStore(fallback_path=path, allow_file=True)
    store.save("one", {"refresh_token": "a"})
    # Two accounts in one file: saving replaces the file, so write both directly.
    path.write_text(json.dumps({"one": {"refresh_token": "a"}, "two": {"refresh_token": "b"}}))

    assert store.delete("one") is True
    assert store.load("one") is None
    assert store.load("two") == {"refresh_token": "b"}
