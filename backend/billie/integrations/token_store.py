"""Where OAuth tokens live.

A Google refresh token is a long-lived credential: anyone holding it can read
and change your calendar until you revoke it. So it goes in the operating
system's credential store -- Credential Locker on Windows, Keychain on macOS,
Secret Service on Linux -- and never in `.env`, never in the database, never in
the audit log.

If no keychain backend is available (a headless Linux box, usually), Billie
does **not** quietly write the token to a file. It refuses, and tells you what
to install. Falling back to a plain file is possible, but only if you ask for
it explicitly with `BILLIE_ALLOW_FILE_TOKEN_STORE=true`, and Billie says so in
its status for as long as that is in force.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

logger = logging.getLogger("billie.tokens")

SERVICE_NAME = "billie"


class TokenStoreUnavailable(RuntimeError):
    """There is nowhere safe to put the token, and no opt-in to a file."""


class TokenStore:
    """Reads and writes one credential per account name."""

    def __init__(self, *, fallback_path: Path | None = None, allow_file: bool = False) -> None:
        self.fallback_path = Path(fallback_path) if fallback_path else None
        self.allow_file = allow_file
        self._keyring_error: str | None = None

    # ------------------------------------------------------------------
    @property
    def keyring_available(self) -> bool:
        return self._backend() is not None

    def _backend(self):
        try:
            import keyring
            from keyring.backends.fail import Keyring as FailKeyring
        except ImportError as exc:  # pragma: no cover - keyring is a dependency
            self._keyring_error = f"keyring is not installed ({exc})"
            return None
        backend = keyring.get_keyring()
        if isinstance(backend, FailKeyring):
            self._keyring_error = (
                "no OS keychain backend is available on this machine"
            )
            return None
        return keyring

    def describe(self) -> dict[str, Any]:
        """What the UI shows about where credentials are kept."""
        backend = self._backend()
        if backend is not None:
            return {
                "backend": "os-keychain",
                "detail": type(backend.get_keyring()).__name__,
                "secure": True,
            }
        if self.allow_file and self.fallback_path:
            return {
                "backend": "file",
                "detail": (
                    f"{self.fallback_path} -- protected by file permissions only, "
                    "because BILLIE_ALLOW_FILE_TOKEN_STORE is on"
                ),
                "secure": False,
            }
        return {
            "backend": "none",
            "detail": self._keyring_error or "unavailable",
            "secure": False,
        }

    def _unavailable(self) -> TokenStoreUnavailable:
        return TokenStoreUnavailable(
            f"Cannot store credentials securely: {self._keyring_error}. "
            "Install a keychain backend (on Linux: `pip install secretstorage` "
            "with a running Secret Service, or `pip install keyrings.alt`), or "
            "set BILLIE_ALLOW_FILE_TOKEN_STORE=true in .env to accept storing "
            "the token in a permission-protected file instead."
        )

    # ------------------------------------------------------------------
    def save(self, account: str, payload: dict[str, Any]) -> str:
        """Store a credential. Returns the backend that was used."""
        serialised = json.dumps(payload)
        backend = self._backend()
        if backend is not None:
            backend.set_password(SERVICE_NAME, account, serialised)
            return "os-keychain"

        if not (self.allow_file and self.fallback_path):
            raise self._unavailable()

        self.fallback_path.parent.mkdir(parents=True, exist_ok=True)
        # Create with owner-only permissions rather than writing and then
        # chmod-ing, so the token is never briefly world-readable.
        descriptor = os.open(
            self.fallback_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({account: payload}, handle)
        logger.warning(
            "Stored credentials in %s -- no OS keychain was available", self.fallback_path
        )
        return "file"

    def load(self, account: str) -> dict[str, Any] | None:
        backend = self._backend()
        if backend is not None:
            raw = backend.get_password(SERVICE_NAME, account)
            return json.loads(raw) if raw else None

        if not (self.allow_file and self.fallback_path and self.fallback_path.exists()):
            return None
        try:
            data = json.loads(self.fallback_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        value = data.get(account)
        return value if isinstance(value, dict) else None

    def delete(self, account: str) -> bool:
        backend = self._backend()
        if backend is not None:
            try:
                backend.delete_password(SERVICE_NAME, account)
                return True
            except Exception:  # noqa: BLE001 - backends raise their own not-found types
                return False

        if not (self.fallback_path and self.fallback_path.exists()):
            return False
        try:
            data = json.loads(self.fallback_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if account not in data:
            return False
        del data[account]
        self.fallback_path.write_text(json.dumps(data), encoding="utf-8")
        return True
