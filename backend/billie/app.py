"""FastAPI application factory and wiring."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from billie import __version__
from billie.api import calendar as calendar_api
from billie.api import chat, files, memory, system
from billie.assistant import Assistant
from billie.audit import AuditLog
from billie.config import PROJECT_ROOT, Settings, get_settings
from billie.confirmations import ConfirmationQueue
from billie.integrations.google_calendar import GoogleCalendar
from billie.integrations.google_oauth import GoogleOAuth
from billie.integrations.token_store import TokenStore
from billie.memory.embeddings import get_embedder
from billie.memory.store import MemoryStore
from billie.models.anthropic_provider import AnthropicProvider
from billie.models.base import ProviderError, ProviderUnavailable
from billie.models.ollama_provider import OllamaProvider
from billie.models.router import ModelRouter
from billie.permissions import PermissionDenied, Permissions
from billie.watcher import CalendarSyncTask, FileWatcher
from billie.workspace import Workspace

FRONTEND_DIR = PROJECT_ROOT / "frontend"
logger = logging.getLogger("billie")


def build_assistant(settings: Settings, owner: str = "the user") -> Assistant:
    """Wire the real providers, store, and audit log together."""
    settings.ensure_dirs()
    audit = AuditLog(settings.audit_log_path)
    store = MemoryStore(settings.db_path, get_embedder(settings))
    cloud = AnthropicProvider(
        api_key=settings.anthropic_api_key,
        model=settings.cloud_model,
        max_tokens=settings.cloud_max_tokens,
        effort=settings.cloud_effort,
        cooldown_seconds=settings.provider_cooldown_seconds,
    )
    local = OllamaProvider(
        base_url=settings.ollama_base_url,
        model=settings.ollama_model,
        cooldown_seconds=settings.provider_cooldown_seconds,
    )
    router = ModelRouter(
        cloud=cloud,
        local=local,
        default_route=settings.default_route,
        complexity_threshold=settings.complexity_threshold,
        audit=audit,
    )
    permissions = Permissions(store, audit)
    confirmations = ConfirmationQueue(
        store, audit, ttl_minutes=settings.proposal_ttl_minutes
    )

    calendar = None
    if settings.google_configured:
        token_store = TokenStore(
            fallback_path=settings.token_fallback_path,
            allow_file=settings.allow_file_token_store,
        )
        oauth = GoogleOAuth(
            settings.google_client_id,
            settings.google_client_secret,
            token_store,
            audit=audit,
        )
        calendar = GoogleCalendar(oauth, confirmations, audit)
    else:
        logger.info(
            "Google Calendar is not configured (no GOOGLE_CLIENT_ID/SECRET in .env)"
        )

    workspace = Workspace(
        settings.workspace_dir,
        store,
        audit,
        backup_dir=settings.backup_dir,
        ttl_minutes=settings.proposal_ttl_minutes,
    )
    return Assistant(
        settings=settings,
        store=store,
        router=router,
        audit=audit,
        permissions=permissions,
        workspace=workspace,
        confirmations=confirmations,
        calendar=calendar,
        owner=owner,
    )


def create_app(
    settings: Settings | None = None,
    assistant: Assistant | None = None,
    *,
    serve_frontend: bool = True,
) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.assistant.audit.record(
            "app.start",
            version=__version__,
            host=settings.host,
            port=settings.port,
            cloud_configured=settings.cloud_configured,
            cloud_model=settings.cloud_model,
            local_model=settings.ollama_model,
        )
        # Keep memory in sync with the allow-listed folders on every start.
        try:
            report = app.state.assistant.reindex_files()
            logger.info("Indexed files: %s", report.to_dict())
            for refusal in report.refused_roots:
                logger.warning("Refused to index %s", refusal)
        except PermissionDenied:
            logger.info("Indexing skipped: the file.read permission is revoked")
        except Exception:  # noqa: BLE001 - indexing must never block startup
            logger.exception("Indexing failed at startup")

        watcher = FileWatcher(app.state.assistant, settings.watch_interval_seconds)
        app.state.watcher = watcher
        watcher.start()

        calendar_sync = CalendarSyncTask(
            app.state.assistant, settings.calendar_sync_interval_seconds
        )
        app.state.calendar_sync = calendar_sync
        calendar_sync.start()
        try:
            yield
        finally:
            await calendar_sync.stop()
            await watcher.stop()
            app.state.assistant.store.close()

    app = FastAPI(
        title="Billie",
        description="A local-first personal assistant.",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.assistant = assistant or build_assistant(settings)

    app.include_router(system.router)
    app.include_router(chat.router)
    app.include_router(memory.router)
    app.include_router(files.permissions_router)
    app.include_router(files.workspace_router)
    app.include_router(calendar_api.router)
    app.include_router(calendar_api.callback_router)

    @app.exception_handler(ProviderUnavailable)
    async def _unavailable(_request, exc: ProviderUnavailable) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    @app.exception_handler(ProviderError)
    async def _provider_error(_request, exc: ProviderError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": str(exc)})

    @app.exception_handler(PermissionDenied)
    async def _permission_denied(_request, exc: PermissionDenied) -> JSONResponse:
        return JSONResponse(
            status_code=403, content={"detail": str(exc), "permission": exc.tool}
        )

    if serve_frontend and FRONTEND_DIR.exists():
        app.mount(
            "/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static"
        )

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(str(Path(FRONTEND_DIR) / "index.html"))

    return app


app_factory = create_app
