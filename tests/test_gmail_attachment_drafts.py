from __future__ import annotations

import base64
from email import message_from_bytes
from email.policy import default
from pathlib import Path
from typing import Any

import pytest

from keystone_agents.tools.gmail_tool import GmailTool


def test_gmail_provider_builds_one_bounded_multipart_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    derived = tmp_path / "artifacts" / "presentation-derived"
    derived.mkdir(parents=True)
    attachment = derived / "KBA_TEST_SLIDE-review.png"
    content = b"\x89PNG\r\n\x1a\nslide-copy"
    attachment.write_bytes(content)
    monkeypatch.setenv("KEYSTONE_PRESENTATION_DERIVED_ROOT", str(derived))
    gmail = GmailTool(live=True)
    monkeypatch.setattr(gmail, "current_account_email", lambda: "operator@example.com")
    captured: dict[str, Any] = {}

    def fake_request(method: str, endpoint: str, *, operation: str, **kwargs: Any):
        captured.update(method=method, endpoint=endpoint, operation=operation, kwargs=kwargs)
        return {"id": "draft-1", "message": {"id": "message-1"}}

    monkeypatch.setattr(gmail, "_request", fake_request)
    result = gmail.create_draft_with_attachment(
        "reviewer@example.com",
        "KBA_TEST_DRAFT slide",
        "KBA_TEST_DRAFT attached copy.",
        str(attachment),
        expected_account="operator@example.com",
    )

    raw = captured["kwargs"]["json"]["message"]["raw"]
    parsed = message_from_bytes(
        base64.urlsafe_b64decode(raw.encode("utf-8")),
        policy=default,
    )
    attachments = list(parsed.iter_attachments())
    assert captured["method"] == "POST"
    assert captured["endpoint"] == "drafts"
    assert parsed["To"] == "reviewer@example.com"
    assert len(attachments) == 1
    assert attachments[0].get_filename() == attachment.name
    assert attachments[0].get_content_type() == "image/png"
    assert attachments[0].get_payload(decode=True) == content
    assert result["attachment_size"] == len(content)
    assert result["sent"] is False


def test_gmail_provider_attachment_path_is_root_and_type_scoped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    derived = tmp_path / "derived"
    derived.mkdir()
    outside = tmp_path / "KBA_TEST_SLIDE-outside.png"
    outside.write_bytes(b"png")
    unsupported = derived / "KBA_TEST_SLIDE-example.txt"
    unsupported.write_text("not supported", encoding="utf-8")
    monkeypatch.setenv("KEYSTONE_PRESENTATION_DERIVED_ROOT", str(derived))
    gmail = GmailTool(live=True)
    monkeypatch.setattr(gmail, "current_account_email", lambda: "operator@example.com")

    for path in (outside, unsupported):
        with pytest.raises(RuntimeError):
            gmail.create_draft_with_attachment(
                "reviewer@example.com",
                "KBA_TEST_DRAFT slide",
                "KBA_TEST_DRAFT attached copy.",
                str(path),
                expected_account="operator@example.com",
            )
