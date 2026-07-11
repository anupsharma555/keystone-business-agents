from __future__ import annotations

import pytest
from pydantic import ValidationError

from keystone_agents.schemas.email_triage import GmailMailboxActionPlan
from scripts.run_gmail_mailbox_action_validation import _expected_operations, _prompt


def test_expected_toggle_operations_restore_initial_state() -> None:
    assert _expected_operations(set()) == ["star", "unstar"]
    assert _expected_operations({"STARRED"}) == ["unstar", "star"]
    assert "currently not starred" in _prompt(set())
    assert "currently starred" in _prompt({"STARRED"})


def test_action_plan_is_exact_and_no_send() -> None:
    plan = GmailMailboxActionPlan(
        request_summary="Toggle the selected message star and restore it.",
        operations=["star", "unstar"],
        rationale="This performs the requested reversible validation.",
    )
    assert plan.provider_id_required_from_user is False
    assert plan.approval_reference_required is True
    assert plan.send_enabled is False
    assert plan.sent is False


def test_action_plan_requires_label_only_for_label_operations() -> None:
    with pytest.raises(ValidationError):
        GmailMailboxActionPlan(
            request_summary="Add the test label.",
            operations=["add_label"],
            rationale="Requested label validation.",
        )
    with pytest.raises(ValidationError):
        GmailMailboxActionPlan(
            request_summary="Star it.",
            operations=["star"],
            label="unexpected",
            rationale="Invalid extra label.",
        )


def test_action_plan_cannot_disable_approval_reference() -> None:
    with pytest.raises(ValidationError):
        GmailMailboxActionPlan(
            request_summary="Star it and restore it.",
            operations=["star", "unstar"],
            rationale="Reversible test.",
            approval_reference_required=False,
        )
