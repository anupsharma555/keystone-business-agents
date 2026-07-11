from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import scripts.run_gmail_test_send_lifecycle as lifecycle


def test_test_send_preview_contains_send_and_cleanup_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "receipt.json"
    captured: dict[str, object] = {}
    monkeypatch.setattr(lifecycle, "GmailTool", lambda *, live: object())

    def fake_create(gmail, **kwargs):
        captured.update(kwargs)
        return {
            "status": "dry-run",
            "operation": "create",
            "draft_id": "",
            "sent": False,
            "send_enabled": False,
        }

    monkeypatch.setattr(lifecycle, "execute_approved_gmail_draft_action", fake_create)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_test_send_lifecycle.py",
            "--to",
            "reviewer@example.com",
            "--expected-account",
            "operator@example.com",
            "--approval-reference",
            "operator:test-send",
            "--output",
            str(output),
        ],
    )

    main = getattr(lifecycle.main, "__wrapped__", lifecycle.main)
    assert main() == 0
    assert lifecycle.GMAIL_TEST_EMAIL_MARKER in str(captured["subject"])
    assert lifecycle.GMAIL_TEST_DRAFT_MARKER in str(captured["subject"])
    assert lifecycle.GMAIL_TEST_EMAIL_MARKER in str(captured["body"])
    assert lifecycle.GMAIL_TEST_DRAFT_MARKER in str(captured["body"])
    receipt = json.loads(output.read_text(encoding="utf-8"))
    assert receipt["status"] == "dry-run"
    assert receipt["provider_send_count"] == 0
    assert receipt["openai_requests"] == 0
    assert "reviewer@example.com" not in output.read_text(encoding="utf-8")
