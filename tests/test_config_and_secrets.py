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


def test_indexed_folders_parse_from_a_comma_separated_env_var(monkeypatch, tmp_path):
    """The .env form is a plain comma-separated string, not JSON."""
    first, second = tmp_path / "research", tmp_path / "projects"
    monkeypatch.setenv("BILLIE_INDEXED_FOLDERS", f"{first}, {second}")
    settings = Settings(_env_file=None)
    assert settings.indexed_folders == [first.resolve(), second.resolve()]


def test_an_empty_allow_list_means_no_extra_folders(monkeypatch):
    monkeypatch.setenv("BILLIE_INDEXED_FOLDERS", "")
    assert Settings(_env_file=None).indexed_folders == []


def test_relative_allow_list_entries_resolve_under_the_project_root(monkeypatch):
    monkeypatch.setenv("BILLIE_INDEXED_FOLDERS", "docs")
    settings = Settings(_env_file=None)
    assert settings.indexed_folders == [(PROJECT_ROOT / "docs").resolve()]


def test_index_roots_always_include_notes_and_workspace_without_duplicates(tmp_path):
    notes = tmp_path / "notes"
    settings = Settings(
        _env_file=None,
        BILLIE_NOTES_DIR=str(notes),
        BILLIE_WORKSPACE_DIR=str(tmp_path / "workspace"),
        # Naming the notes folder again must not index it twice.
        BILLIE_INDEXED_FOLDERS=f"{notes},{tmp_path / 'research'}",
    )
    names = [name for name, _ in settings.index_roots()]
    assert names == ["notes", "workspace", "research"]


def test_two_folders_with_the_same_name_stay_distinguishable(tmp_path):
    """Citations are keyed by root name, so collisions have to be resolved."""
    settings = Settings(
        _env_file=None,
        BILLIE_INDEXED_FOLDERS=f"{tmp_path / 'a' / 'docs'},{tmp_path / 'b' / 'docs'}",
    )
    names = [name for name, _ in settings.index_roots()]
    assert names == ["notes", "workspace", "docs", "docs-2"]


@pytest.mark.parametrize("value", [-0.5, 1.5])
def test_complexity_threshold_is_bounded(value):
    with pytest.raises(Exception):
        Settings(_env_file=None, BILLIE_COMPLEXITY_THRESHOLD=value)
