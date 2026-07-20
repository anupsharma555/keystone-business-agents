from __future__ import annotations

from keystone_agents.gmail_triage import draft_actions as lifecycle


class FakeGmail:
    def __init__(self, *, live: bool = True) -> None:
        self.live = live


def _verified(operation: str, draft_id: str = "draft-123") -> dict[str, object]:
    return {
        "status": "ok",
        "operation": operation,
        "draft_id": draft_id,
        "verification": {"passed": True},
        "sent": False,
        "send_enabled": False,
    }


def test_gmail_lifecycle_dry_run_stops_after_create(monkeypatch) -> None:
    monkeypatch.setattr(
        lifecycle,
        "execute_approved_gmail_draft_action",
        lambda *_args, **_kwargs: {
            "status": "dry-run",
            "operation": "create",
            "draft_id": "",
            "verification": {"passed": False},
            "sent": False,
            "send_enabled": False,
        },
    )

    result = lifecycle.execute_gmail_test_draft_lifecycle(
        FakeGmail(live=False),
        marker="KBA_TEST_DRAFT abc",
        expected_account="operator@example.com",
        recipient="recipient@example.com",
        approval_reference="approval",
    )

    assert result["status"] == "dry-run"
    assert result["openai_requests"] == 0


def test_gmail_lifecycle_checks_cleanup_gate_before_create(monkeypatch) -> None:
    calls: list[dict[str, object]] = []
    monkeypatch.delenv(lifecycle.GMAIL_TEST_DRAFT_DELETE_ENV, raising=False)
    monkeypatch.setattr(
        lifecycle,
        "execute_approved_gmail_draft_action",
        lambda *_args, **kwargs: calls.append(kwargs),
    )

    try:
        lifecycle.execute_gmail_test_draft_lifecycle(
            FakeGmail(),
            marker="KBA_TEST_DRAFT guarded",
            expected_account="operator@example.com",
            recipient="operator@example.com",
            approval_reference="approval",
        )
    except RuntimeError as exc:
        assert "cleanup gate before create" in str(exc)
    else:  # pragma: no cover - the gate must remain fail-closed
        raise AssertionError("Expected the cleanup gate to block before create.")

    assert calls == []


def test_gmail_lifecycle_verifies_update_and_cleanup(monkeypatch) -> None:
    monkeypatch.setenv(lifecycle.GMAIL_TEST_DRAFT_DELETE_ENV, "true")
    calls: list[dict[str, object]] = []

    def fake_action(_provider, **kwargs):
        calls.append(kwargs)
        return _verified("update" if kwargs.get("draft_id") else "create")

    monkeypatch.setattr(lifecycle, "execute_approved_gmail_draft_action", fake_action)
    monkeypatch.setattr(
        lifecycle,
        "delete_approved_gmail_test_draft",
        lambda *_args, **_kwargs: _verified("delete_test_draft"),
    )

    result = lifecycle.execute_gmail_test_draft_lifecycle(
        FakeGmail(),
        marker="KBA_TEST_DRAFT abc",
        expected_account="operator@example.com",
        recipient="recipient@example.com",
        approval_reference="approval",
    )

    assert result["status"] == "success"
    assert calls[1]["draft_id"] == "draft-123"
    assert result["delete"]["verification"]["passed"] is True
    assert result["sent"] is False


def test_gmail_lifecycle_attempts_cleanup_after_update_failure(monkeypatch) -> None:
    monkeypatch.setenv(lifecycle.GMAIL_TEST_DRAFT_DELETE_ENV, "true")
    action_calls = 0

    def fake_action(_provider, **_kwargs):
        nonlocal action_calls
        action_calls += 1
        if action_calls == 1:
            return _verified("create")
        raise RuntimeError("update failed")

    cleanup_ids: list[str] = []
    monkeypatch.setattr(lifecycle, "execute_approved_gmail_draft_action", fake_action)

    def fake_delete(_provider, **kwargs):
        cleanup_ids.append(kwargs["draft_id"])
        return _verified("delete_test_draft")

    monkeypatch.setattr(lifecycle, "delete_approved_gmail_test_draft", fake_delete)

    result = lifecycle.execute_gmail_test_draft_lifecycle(
        FakeGmail(),
        marker="KBA_TEST_DRAFT abc",
        expected_account="operator@example.com",
        recipient="recipient@example.com",
        approval_reference="approval",
    )

    assert result["status"] == "failed"
    assert result["failure"] == "RuntimeError: update failed"
    assert cleanup_ids == ["draft-123"]
    assert result["delete"]["verification"]["passed"] is True
