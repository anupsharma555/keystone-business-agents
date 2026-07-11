from __future__ import annotations

from pathlib import Path

import pytest

import scripts.run_gmail_slide_attachment_lifecycle as lifecycle


def _verified(**values):
    return {"status": "success", "verification": {"passed": True}, **values}


def test_slide_attachment_lifecycle_preserves_identity_and_cleans_both_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = tmp_path / "KBA_TEST_SLIDE-case.png"
    artifact.write_bytes(b"png")
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        lifecycle,
        "presentation_extract_slide_copy_local_impl",
        lambda *args, **kwargs: _verified(
            artifact_path=str(artifact),
            derived_copy_created=True,
            parent_modified=False,
        ),
    )

    def fake_draft_action(gmail, **kwargs):
        draft_id = kwargs.get("draft_id") or "draft-attachment-1"
        operation = "update" if kwargs.get("draft_id") else "create"
        calls.append((operation, draft_id))
        return _verified(
            operation=f"{operation}_draft_attachment",
            draft_id=draft_id,
            attachment_filename=artifact.name,
            attachment_size=3,
            attachment_sha256="hash",
            sent=False,
            send_enabled=False,
        )

    monkeypatch.setattr(
        lifecycle,
        "execute_approved_gmail_draft_attachment_action",
        fake_draft_action,
    )
    monkeypatch.setattr(
        lifecycle,
        "delete_approved_gmail_test_draft",
        lambda gmail, **kwargs: _verified(
            operation="delete_test_draft",
            draft_id=kwargs["draft_id"],
            sent=False,
        ),
    )
    monkeypatch.setattr(
        lifecycle,
        "presentation_delete_test_artifact_local_impl",
        lambda *args, **kwargs: _verified(
            artifact_path=str(artifact),
            operation="delete_test_slide_artifact",
        ),
    )

    result = lifecycle.execute_gmail_slide_attachment_lifecycle(
        relative_path="deck.pptx",
        slide_number=2,
        account="operator@example.com",
        recipient="reviewer@example.com",
        suffix="case",
        approval_reference="operator:slide-draft",
        live=True,
        gmail=object(),
    )

    assert result["status"] == "passed"
    assert result["openai_requests"] == 0
    assert result["sent"] is False
    assert calls == [("create", "draft-attachment-1"), ("update", "draft-attachment-1")]
    assert result["create"]["draft_id"] == result["update"]["draft_id"]
    assert result["draft_cleanup"]["verification"]["passed"] is True
    assert result["artifact_cleanup"]["verification"]["passed"] is True
    assert "operator@example.com" not in str(result)
    assert "reviewer@example.com" not in str(result)
