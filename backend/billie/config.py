"""Configuration for Billie.

Every secret is read from the environment (loaded from the project's `.env`).
Nothing sensitive is ever hardcoded here or anywhere else in the source tree.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/billie/config.py -> backend/billie -> backend -> <project root>
PROJECT_ROOT = Path(__file__).resolve().parents[2]

Route = Literal["auto", "local", "cloud"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Settings(BaseSettings):
    """Runtime settings, loaded from environment variables and `.env`."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Cloud reasoning: Anthropic Claude API ---
    anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")
    cloud_model: str = Field(default="claude-opus-5", alias="BILLIE_CLOUD_MODEL")
    cloud_max_tokens: int = Field(default=16000, alias="BILLIE_CLOUD_MAX_TOKENS")
    cloud_effort: Effort = Field(default="high", alias="BILLIE_CLOUD_EFFORT")

    # --- Local model: Ollama ---
    ollama_base_url: str = Field(
        default="http://127.0.0.1:11434", alias="BILLIE_OLLAMA_BASE_URL"
    )
    ollama_model: str = Field(default="llama3.1:8b", alias="BILLIE_OLLAMA_MODEL")
    ollama_embed_model: str = Field(
        default="nomic-embed-text", alias="BILLIE_OLLAMA_EMBED_MODEL"
    )
    use_ollama_embeddings: bool = Field(
        default=False, alias="BILLIE_USE_OLLAMA_EMBEDDINGS"
    )

    # --- Routing ---
    default_route: Route = Field(default="auto", alias="BILLIE_DEFAULT_ROUTE")
    complexity_threshold: float = Field(
        default=0.45, ge=0.0, le=1.0, alias="BILLIE_COMPLEXITY_THRESHOLD"
    )
    # How long a provider stays marked "unavailable" after a failed call.
    provider_cooldown_seconds: float = Field(
        default=60.0, alias="BILLIE_PROVIDER_COOLDOWN_SECONDS"
    )

    # --- Retrieval ---
    retrieval_top_k: int = Field(default=5, alias="BILLIE_RETRIEVAL_TOP_K")
    retrieval_min_score: float = Field(
        default=0.18, ge=0.0, le=1.0, alias="BILLIE_RETRIEVAL_MIN_SCORE"
    )

    # --- Server / auth ---
    host: str = Field(default="127.0.0.1", alias="BILLIE_HOST")
    port: int = Field(default=8765, alias="BILLIE_PORT")
    api_token: str | None = Field(default=None, alias="BILLIE_API_TOKEN")
    allow_loopback_unauthenticated: bool = Field(
        default=True, alias="BILLIE_ALLOW_LOOPBACK_UNAUTHENTICATED"
    )

    # --- Storage ---
    data_dir: Path = Field(default=Path("data"), alias="BILLIE_DATA_DIR")
    notes_dir: Path = Field(default=Path("notes"), alias="BILLIE_NOTES_DIR")
    log_level: str = Field(default="INFO", alias="BILLIE_LOG_LEVEL")

    # --- File indexing (phase 2) ---
    #: Extra folders to index, beyond `notes_dir`. Comma-separated, and always
    #: an explicit list -- Billie never indexes a whole drive or home folder.
    #: `NoDecode` stops pydantic-settings trying to read the env var as JSON,
    #: so `_split_folders` below sees the raw string.
    indexed_folders: Annotated[list[Path], NoDecode] = Field(
        default_factory=list, alias="BILLIE_INDEXED_FOLDERS"
    )
    #: The one folder Billie may write to, and only via an approved diff.
    workspace_dir: Path = Field(default=Path("workspace"), alias="BILLIE_WORKSPACE_DIR")
    max_indexed_file_mb: float = Field(
        default=8.0, gt=0, alias="BILLIE_MAX_INDEXED_FILE_MB"
    )
    #: Seconds between checks for changed files. 0 turns the watcher off.
    watch_interval_seconds: float = Field(
        default=60.0, ge=0, alias="BILLIE_WATCH_INTERVAL_SECONDS"
    )
    #: How long an unapproved write proposal stays actionable.
    proposal_ttl_minutes: float = Field(
        default=120.0, gt=0, alias="BILLIE_PROPOSAL_TTL_MINUTES"
    )

    @field_validator("indexed_folders", mode="before")
    @classmethod
    def _split_folders(cls, value: object) -> object:
        """Accept a comma-separated string from .env, or a real list."""
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @field_validator("indexed_folders", mode="after")
    @classmethod
    def _absolutise_folders(cls, value: list[Path]) -> list[Path]:
        resolved: list[Path] = []
        for folder in value:
            path = folder if folder.is_absolute() else (PROJECT_ROOT / folder)
            resolved.append(path.resolve())
        return resolved

    @field_validator("anthropic_api_key", "api_token", mode="before")
    @classmethod
    def _blank_to_none(cls, value: object) -> object:
        """Treat `KEY=` in .env as "not configured" rather than an empty key."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("data_dir", "notes_dir", "workspace_dir", mode="after")
    @classmethod
    def _absolutise(cls, value: Path) -> Path:
        return value if value.is_absolute() else (PROJECT_ROOT / value).resolve()

    # --- Derived paths ---
    @property
    def db_path(self) -> Path:
        return self.data_dir / "billie.sqlite3"

    @property
    def audit_log_path(self) -> Path:
        return self.data_dir / "audit.log.jsonl"

    @property
    def cloud_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "workspace-backups"

    @property
    def max_indexed_file_bytes(self) -> int:
        return int(self.max_indexed_file_mb * 1024 * 1024)

    def index_roots(self) -> list[tuple[str, Path]]:
        """Every folder Billie may read, as `(name, path)` pairs.

        The notes folder and the workspace are always included -- Billie should
        be able to read back what it drafted. Everything else has to be named in
        `BILLIE_INDEXED_FOLDERS`. Duplicate names are disambiguated so two
        folders called "docs" stay distinguishable in citations.
        """
        roots: list[tuple[str, Path]] = [
            ("notes", self.notes_dir),
            ("workspace", self.workspace_dir),
        ]
        used = {"notes", "workspace"}
        for folder in self.indexed_folders:
            if folder in (self.notes_dir, self.workspace_dir):
                continue
            name = folder.name or "root"
            candidate = name
            suffix = 2
            while candidate in used:
                candidate = f"{name}-{suffix}"
                suffix += 1
            used.add(candidate)
            roots.append((candidate, folder))
        return roots

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.notes_dir.mkdir(parents=True, exist_ok=True)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings singleton (FastAPI dependency-friendly)."""
    return Settings()
