"""Configuration and secrets hygiene.

The secrets rule from the build brief -- keys live in .env, .env is ignored by
git, nothing is hardcoded -- is enforced by these tests, not just by habit.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from billie.config import PROJECT_ROOT, Settings

TRACKED_SOURCE_SUFFIXES = {".py", ".js", ".html", ".css", ".md", ".toml", ".txt", ".json"}

# Shapes of real credentials that must never appear in tracked files.
SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"),          # Anthropic
    re.compile(r"\bsk-[A-Za-z0-9]{32,}\b"),             # generic provider key
    re.compile(r"AIza[0-9A-Za-z\-_]{30,}"),             # Google
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),                # GitHub
]


def tracked_files() -> list[Path]:
    output = subprocess.run(
        ["git", "ls-files"], cwd=PROJECT_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [PROJECT_ROOT / line for line in output.splitlines() if line.strip()]


def test_env_is_git_ignored():
    result = subprocess.run(
        ["git", "check-ignore", ".env"], cwd=PROJECT_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, ".env must be git-ignored"


def test_env_file_is_not_tracked():
    assert not any(path.name == ".env" for path in tracked_files())


def test_env_example_is_tracked_and_has_no_values():
    example = PROJECT_ROOT / ".env.example"
    assert example.exists()
    for line in example.read_text().splitlines():
        if line.startswith(("ANTHROPIC_API_KEY", "BILLIE_API_TOKEN", "ELEVENLABS_API_KEY")):
            assert line.split("=", 1)[1].strip() == "", f"{line} must have an empty value"


def test_no_credentials_in_tracked_files():
    offenders = []
    for path in tracked_files():
        if path.suffix.lower() not in TRACKED_SOURCE_SUFFIXES or not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)} ({pattern.pattern})")
    assert not offenders, f"Possible hardcoded credentials: {offenders}"


def test_api_key_comes_only_from_the_environment(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-environment")
    settings = Settings(_env_file=None)
    assert settings.anthropic_api_key == "from-environment"
    assert settings.cloud_configured is True


def test_blank_key_is_treated_as_unconfigured():
    settings = Settings(_env_file=None, ANTHROPIC_API_KEY="   ", BILLIE_API_TOKEN="")
    assert settings.anthropic_api_key is None
    assert settings.api_token is None
    assert settings.cloud_configured is False


def test_relative_paths_resolve_under_the_project_root():
    settings = Settings(_env_file=None, BILLIE_DATA_DIR="data", BILLIE_NOTES_DIR="notes")
    assert settings.data_dir == (PROJECT_ROOT / "data").resolve()
    assert settings.db_path.name == "billie.sqlite3"
    assert settings.audit_log_path.parent == settings.data_dir


def test_defaults_match_the_documented_model():
    settings = Settings(_env_file=None)
    assert settings.cloud_model == "claude-opus-5"
    assert settings.default_route == "auto"
    assert 0.0 <= settings.complexity_threshold <= 1.0


@pytest.mark.parametrize("value", [-0.5, 1.5])
def test_complexity_threshold_is_bounded(value):
    with pytest.raises(Exception):
        Settings(_env_file=None, BILLIE_COMPLEXITY_THRESHOLD=value)
