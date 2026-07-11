from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from keystone_agents.execution_identity import create_validation_execution_identity
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.tools.email_style_tool import load_email_style_profile_fixture
from keystone_agents.tools.storage_tool import StorageTool
from scripts.approve_email_style_profile import approve_profile
from scripts.run_gmail_sent_style_comparison import (
    EXPECTED_MODEL,
    PROFILE_ID,
    _build_payload,
    _typed_input,
    build_parser,
)


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'style.sqlite'}"


def _pending_live_profile():
    return load_email_style_profile_fixture(
        "sample_email_style_profile_anup_approved"
    ).model_copy(
        update={
            "profile_id": PROFILE_ID,
            "source": "live_gmail_sent",
            "source_id": "gmail:sent",
            "source_url": "gmail://sent",
            "approval_state": ApprovalState.PENDING,
            "signoffs": ["Sincerely,"],
            "raw_sent_email_bodies_included": False,
            "send_enabled": False,
            "sent": False,
        }
    )


def _passing_result() -> TypedAgentRunResult[EmailTriageResult]:
    output = EmailTriageResult.model_validate(
        {
            "message_id": "synthetic-style-comparison-1",
            "thread_id": "synthetic-style-comparison-thread-1",
            "subject": "Example Health clinical operations note",
            "category": "consulting_opportunity",
            "confidence": 0.95,
            "priority": "high",
            "summary": "Synthetic style comparison.",
            "reasoning": "Used the approved aggregate style profile.",
            "needs_reply": True,
            "recommended_action": "Review before external use.",
            "draft_reply": (
                "Hi Alex,\n\nThanks for the clinical operations note. I would be glad "
                "to compare notes on a short advisory project. Could you send any "
                "non-sensitive context that would help?\n\nSincerely,\nAnup"
            ),
            "draft_created": False,
            "style_profile_used": True,
            "style_profile_id": PROFILE_ID,
            "approval_required": True,
            "requires_human_review": True,
        }
    )
    return TypedAgentRunResult(
        agent_name="gmail_triage",
        output=output,
        raw_result=SimpleNamespace(new_items=[]),
        live=True,
        usage={"available": True, "requests": 1},
        cost={"available": True, "estimated_usd": 0.02},
        budget_guard={"enforced": True, "exceeded": False},
        request_cache={"rate_limit_retries": 0},
    )


def _identity():
    return create_validation_execution_identity(
        scenario="gmail_approved_sent_style_comparison",
        route="gmail_triage",
        now=datetime(2026, 7, 11, 12, 30, tzinfo=UTC),
        nonce="a1b2c3d4",
    )


def test_exact_pending_live_profile_can_be_approved_for_drafting(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    StorageTool(database_url).save_email_style_profile(_pending_live_profile())

    payload = approve_profile(
        profile_id=PROFILE_ID,
        database_url=database_url,
        reviewer="Anup",
        notes="Approved aggregate style only.",
    )

    assert payload["status"] == "pass"
    assert payload["approval_state"] == "approved_for_drafting"
    assert payload["approval_scope"] == "drafting"
    assert payload["raw_sent_email_bodies_included"] is False
    assert payload["send_enabled"] is False
    assert payload["verified"] is True


def test_style_comparison_input_and_receipt_use_exact_approved_profile(tmp_path: Path) -> None:
    profile = _pending_live_profile().model_copy(
        update={"approval_state": ApprovalState.APPROVED_FOR_DRAFTING}
    )
    typed_input = _typed_input(profile)
    payload = _build_payload(
        _passing_result(),
        execution_identity=_identity(),
        model=EXPECTED_MODEL,
        budget_usd=0.05,
    )

    assert PROFILE_ID in typed_input.email_style_profile
    assert "Sincerely" in typed_input.email_style_profile
    assert payload["status"] == "pass"
    assert all(payload["checks"].values())
    assert payload["safety"]["provider_writes"] == 0


def test_runner_defaults_to_one_request_and_rejects_profile_substitution(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["run_gmail_sent_style_comparison.py", "--database-url", "sqlite:///test.db"],
    )
    args = build_parser().parse_args()

    assert args.model == EXPECTED_MODEL
    assert args.max_openai_requests == 1
    assert args.budget_usd == 0.05
    assert args.profile_id == PROFILE_ID


def test_approval_receipt_contains_no_raw_message_content(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    StorageTool(database_url).save_email_style_profile(_pending_live_profile())
    payload = approve_profile(
        profile_id=PROFILE_ID,
        database_url=database_url,
        reviewer="Anup",
        notes="Approved aggregate style only.",
    )

    encoded = json.dumps(payload)
    assert "raw_sent_email_bodies_included" in encoded
    assert "alex@example" not in encoded
