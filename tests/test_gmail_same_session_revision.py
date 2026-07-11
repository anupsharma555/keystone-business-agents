from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from keystone_agents.models import GmailTriageSDKInput
from keystone_agents.schemas.email_triage import EmailTriageResult
from scripts.run_gmail_same_session_revision import (
    FOLLOWUP_REQUEST,
    MESSAGE_ID,
    ORIGINAL_DRAFT,
    SUBJECT,
    _existing_draft_from_session,
    _revision_checks,
    _seed_items,
    _write_result_atomic,
    build_parser,
    main,
)


def test_seeded_revision_session_contains_prior_identity_and_draft() -> None:
    items = _seed_items()
    assert len(items) == 2
    assert items[0]["role"] == "user"
    assert items[1]["role"] == "assistant"
    prior = json.loads(items[1]["content"][0]["text"])
    assert prior["message_id"] == MESSAGE_ID
    assert prior["subject"] == SUBJECT
    assert prior["draft_reply"] == ORIGINAL_DRAFT
    assert "a few times that work" not in FOLLOWUP_REQUEST.lower()


def test_revision_checks_require_identity_style_facts_and_no_side_effects() -> None:
    result = EmailTriageResult.model_validate(
        {
            "message_id": MESSAGE_ID,
            "thread_id": "synthetic-gmail-thread-1",
            "subject": SUBJECT,
            "category": "consulting_opportunity",
            "confidence": 0.95,
            "priority": "high",
            "summary": "Revised synthetic draft.",
            "reasoning": "Used prior session context.",
            "needs_reply": True,
            "recommended_action": "Review before external use.",
            "draft_reply": (
                "Hi Alex,\n\nThanks for the clinical operations note. Happy to compare "
                "notes on a short advisory project. Please send a few times that work."
                "\n\nSincerely,\nAnup"
            ),
            "draft_created": False,
            "approval_required": True,
            "requires_human_review": True,
            "human_work_context": {"missing_context": []},
        }
    )
    assert all(_revision_checks(result).values())


def test_revision_checks_reject_false_missing_prior_draft_claim() -> None:
    result = EmailTriageResult.model_validate(
        {
            "message_id": MESSAGE_ID,
            "thread_id": "synthetic-gmail-thread-1",
            "subject": SUBJECT,
            "category": "consulting_opportunity",
            "confidence": 0.95,
            "priority": "high",
            "summary": "Revised synthetic draft.",
            "reasoning": "Used prior session context.",
            "needs_reply": True,
            "recommended_action": "Review before external use.",
            "draft_reply": (
                "Hi Alex,\n\nThanks for the clinical operations note. Happy to compare "
                "notes on a short advisory project. Please send a few times that work."
                "\n\nSincerely,\nAnup"
            ),
            "draft_created": False,
            "approval_required": True,
            "requires_human_review": True,
            "human_work_context": {
                "missing_context": ["The existing draft was not provided."],
            },
        }
    )
    assert _revision_checks(result)["prior_draft_not_reported_missing"] is False


def test_existing_draft_is_resolved_from_exact_session_artifact() -> None:
    class Session:
        async def get_items(self):
            return _seed_items()

    assert asyncio.run(_existing_draft_from_session(Session())) == ORIGINAL_DRAFT


def test_typed_input_places_existing_draft_in_current_model_prompt() -> None:
    prompt = GmailTriageSDKInput(
        subject=SUBJECT,
        body="",
        message_id=MESSAGE_ID,
        thread_id="synthetic-gmail-thread-1",
        existing_draft=ORIGINAL_DRAFT,
    ).to_prompt()

    assert "Existing draft artifact for this exact message/thread" in prompt
    assert ORIGINAL_DRAFT in prompt
    assert "do not report it missing" in prompt


def test_live_revision_runner_defaults_to_one_request_and_five_cent_budget(
    monkeypatch,
    tmp_path,
    require_local_evidence,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["run_gmail_same_session_revision.py", "--session-id", "case", "--session-db", "case.db"],
    )
    args = build_parser().parse_args()

    assert args.model == "gpt-5.4-mini"
    assert args.max_openai_requests == 1
    assert args.budget_usd == 0.05
    receipt = tmp_path / "receipt.json"
    _write_result_atomic(receipt, {"status": "pass", "requests": 1})
    assert json.loads(receipt.read_text(encoding="utf-8"))["requests"] == 1
    plan = json.loads(
        require_local_evidence(
            "artifacts/test-pack/next-live-gmail-revision-plan.json"
        ).read_text(encoding="utf-8")
    )
    assert plan["approval_status"] == "completed_within_eight_sequential_run_allowance"
    assert "SDK trace metadata" in plan["execution_identity"]
    assert plan["billing_baseline"]["chrome_billing_refresh"] == "completed"
    assert plan["billing_baseline"]["chrome_billing_credit_balance_usd"] == 2.82
    assert plan["result"]["passed_attempt"]["status"] == "pass"


def test_live_revision_runner_rejects_non_approved_model(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_gmail_same_session_revision.py",
            "--session-id",
            "model-guard",
            "--session-db",
            str(tmp_path / "session.sqlite"),
            "--model",
            "other-model",
        ],
    )

    with pytest.raises(SystemExit, match="model=gpt-5.4-mini"):
        main()


def test_main_uses_same_execution_identity_for_trace_and_receipt(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    class FakeSession:
        async def add_items(self, _items) -> None:
            return None

        async def get_items(self):
            return _seed_items()

    result = EmailTriageResult.model_validate(
        {
            "message_id": MESSAGE_ID,
            "thread_id": "synthetic-gmail-thread-1",
            "subject": SUBJECT,
            "category": "consulting_opportunity",
            "confidence": 0.95,
            "priority": "high",
            "summary": "Revised synthetic draft.",
            "reasoning": "Used prior session context.",
            "needs_reply": True,
            "recommended_action": "Review before external use.",
            "draft_reply": (
                "Hi Alex,\n\nThanks for the clinical operations note. Happy to compare "
                "notes on a short advisory project. Please send a few times that work."
                "\n\nSincerely,\nAnup"
            ),
            "draft_created": False,
            "approval_required": True,
            "requires_human_review": True,
            "human_work_context": {"missing_context": []},
        }
    )

    def fake_run_typed_sdk_agent(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            final_output=result,
            usage={"available": True, "requests": 1},
            cost={"available": True, "estimated_usd": 0.01},
            budget_guard={"enforced": True, "exceeded": False},
            request_cache={"rate_limit_retries": 0},
        )

    output = tmp_path / "gmail-live.json"
    monkeypatch.setattr(
        "scripts.run_gmail_same_session_revision.build_sdk_session",
        lambda _spec: FakeSession(),
    )
    monkeypatch.setattr(
        "scripts.run_gmail_same_session_revision.run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )
    monkeypatch.setattr(
        "scripts.run_gmail_same_session_revision.build_gmail_triage_agent",
        lambda **_kwargs: object(),
    )
    monkeypatch.setattr(
        "scripts.run_gmail_same_session_revision.load_settings",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_gmail_same_session_revision.py",
            "--session-id",
            "case",
            "--session-db",
            str(tmp_path / "session.db"),
            "--output",
            str(output),
        ],
    )

    assert main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert captured["trace_metadata"]["run_id"] == payload["execution_identity"]["run_id"]
    assert captured["trace_metadata"]["case_id"] == payload["execution_identity"]["case_id"]
    assert json.loads(output.read_text(encoding="utf-8")) == payload
