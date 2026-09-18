"""The audit log."""

from __future__ import annotations

import json

from billie.audit import AuditLog


def test_events_are_appended_as_json_lines(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    log.record("chat.request", provider="anthropic", chars=12)
    log.record("chat.response", provider="anthropic", confidence="grounded")

    lines = (tmp_path / "audit.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "chat.request"
    assert first["detail"]["provider"] == "anthropic"
    assert first["ts"].endswith("+00:00") or "T" in first["ts"]


def test_secrets_are_redacted_before_they_are_written(tmp_path):
    # Assembled at runtime: a literal here would (rightly) trip the
    # hardcoded-credential scanner in test_config_and_secrets.py.
    fake_key = "sk-" + "ant-api03-" + "N0TAREALKEY" * 3
    log = AuditLog(tmp_path / "audit.jsonl")
    entry = log.record(
        "external.call",
        api_key=fake_key,
        headers={"Authorization": f"Bearer {fake_key}"},
        note=f"calling with Bearer {fake_key} inline",
    )
    written = (tmp_path / "audit.jsonl").read_text()
    assert "N0TAREALKEY" not in written
    assert "[redacted]" in written
    assert entry["detail"]["api_key"] == "[redacted]"
    assert "[redacted]" in entry["detail"]["note"]


def test_tail_returns_recent_entries_and_survives_a_missing_file(tmp_path):
    log = AuditLog(tmp_path / "nested" / "audit.jsonl")
    assert AuditLog(tmp_path / "absent.jsonl").tail() == []
    for index in range(10):
        log.record("tick", index=index)
    recent = log.tail(3)
    assert [entry["detail"]["index"] for entry in recent] == [7, 8, 9]
