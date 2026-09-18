"""Run Billie: `python -m billie` (from the backend/ directory or via run.py)."""

from __future__ import annotations

import uvicorn

from billie.app import create_app
from billie.config import get_settings


def main() -> None:
    settings = get_settings()
    settings.ensure_dirs()
    app = create_app(settings)
    banner(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())


def banner(settings) -> None:
    cloud = settings.cloud_model if settings.cloud_configured else "not configured (no API key)"
    print("\n  Billie")
    print(f"  UI          http://{settings.host}:{settings.port}")
    print(f"  Cloud model {cloud}")
    print(f"  Local model {settings.ollama_model} via {settings.ollama_base_url}")
    print(f"  Notes       {settings.notes_dir}")
    print(f"  Data        {settings.data_dir}")
    token = "set" if settings.api_token else "not set (localhost only)"
    print(f"  API token   {token}\n")


if __name__ == "__main__":
    main()
