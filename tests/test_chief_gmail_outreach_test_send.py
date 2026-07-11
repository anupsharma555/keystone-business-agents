from __future__ import annotations

from types import SimpleNamespace

from scripts.run_chief_gmail_outreach_test_send import _chief_checks, _chief_request


def test_chief_request_preserves_role_split_and_exact_thread() -> None:
    payload = _chief_request({"thread_id": "thread-1", "message_id": "message-1"})
    request = payload["request"]
    assert "Gmail Triage owns" in request
    assert "Outreach Composer" in request
    assert "Chief sends directly" in request
    assert payload["provider_call_context"]["gmail"]["selected_exact_thread"] is True


def test_chief_checks_require_coordination_and_no_direct_send() -> None:
    output = SimpleNamespace(
        approval_required=True,
        send_enabled=False,
        slack_post_allowed=False,
        model_dump=lambda **_: {
            "summary": "Gmail Triage owns the draft action; Outreach composes the reply.",
        },
    )
    assert all(_chief_checks(output).values())
