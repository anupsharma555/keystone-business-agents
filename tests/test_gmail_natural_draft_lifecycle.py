from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from keystone_agents.gmail_triage.draft_actions import GMAIL_TEST_DRAFT_DELETE_ENV
from keystone_agents.gmail_triage.execution_plan import infer_gmail_execution_plan
from keystone_agents.schemas.email_triage import EmailTriageResult
from scripts.run_gmail_natural_draft_lifecycle import (
    EXPECTED_MODEL,
    EXPECTED_REQUESTS,
    _safe_provider_receipt,
    _validate_args,
    build_parser,
    execute_lifecycle,
)


class FakeProvider:
    live = True

    def __init__(self) -> None:
        self.drafts: dict[str, dict[str, Any]] = {}

    def create_draft(self, to, subject, body, *, expected_account=None):
        self.drafts["draft-1"] = {
            "draft_id": "draft-1",
            "message_id": "message-1",
            "to": to,
            "subject": subject,
            "body": body,
            "sent": False,
        }
        return {"status": "draft_created", "draft_id": "draft-1", "sent": False}

    def update_draft(self, draft_id, to, subject, body, *, expected_account=None):
        self.drafts[draft_id].update(to=to, subject=subject, body=body, sent=False)
        return {"status": "draft_updated", "draft_id": draft_id, "sent": False}

    def get_draft(self, draft_id):
        return dict(self.drafts[draft_id])

    def delete_draft(self, draft_id, *, expected_account=None):
        self.drafts.pop(draft_id)
        return {"status": "deleted", "draft_id": draft_id, "sent": False}

    def draft_exists(self, draft_id):
        return draft_id in self.drafts


def _result(
    draft: str,
    *,
    message_id: str = "synthetic-natural-draft-a1b2c3d4e5",
    retries: int = 0,
    approval_required: bool = True,
    draft_created: bool = False,
):
    output = EmailTriageResult.model_validate(
        {
            "message_id": message_id,
            "thread_id": f"{message_id}-thread",
            "subject": "KBA marked clinical operations advisory validation",
            "category": "consulting_opportunity",
            "confidence": 0.95,
            "priority": "high",
            "summary": "Synthetic advisory context.",
            "reasoning": "Draft-only response.",
            "needs_reply": True,
            "recommended_action": "Review before external use.",
            "draft_reply": draft,
            "draft_created": draft_created,
            "approval_required": approval_required,
            "requires_human_review": True,
        }
    )
    return SimpleNamespace(
        final_output=output,
        usage={
            "available": True,
            "requests": 1,
            "input_tokens": 100,
            "cached_input_tokens": 0,
            "output_tokens": 20,
            "reasoning_output_tokens": 0,
            "total_tokens": 120,
        },
        cost={"estimated_usd": 0.02},
        request_cache={"rate_limit_retries": retries},
    )


def test_future_runner_defaults_to_two_requests_and_ten_cent_budget(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_gmail_natural_draft_lifecycle.py",
            "--session-id",
            "case",
            "--session-db",
            "case.sqlite",
        ],
    )
    args = build_parser().parse_args()

    assert args.model == EXPECTED_MODEL
    assert args.max_openai_requests == EXPECTED_REQUESTS
    assert args.budget_usd == 0.10
    _validate_args(args)


def test_fake_model_and_provider_join_create_update_cleanup(monkeypatch) -> None:
    import scripts.run_gmail_natural_draft_lifecycle as runner

    drafts = iter(
        [
            _result(
                "Hi Alex, thanks for the clinical operations note. I would be glad to "
                "compare notes on a possible short advisory project.\n\nSincerely,\nAnup"
            ),
            _result(
                "Hi Alex, thanks for the clinical operations note. Happy to compare "
                "notes on a short advisory project.\n\nSincerely,\nAnup"
            ),
        ]
    )
    monkeypatch.setattr(runner, "_run_model", lambda *_args, **_kwargs: next(drafts))
    monkeypatch.setenv(GMAIL_TEST_DRAFT_DELETE_ENV, "true")
    provider = FakeProvider()

    payload = execute_lifecycle(
        model=EXPECTED_MODEL,
        budget_usd=0.10,
        session=object(),
        provider=provider,
        account="operator@example.test",
        recipient="reviewer@example.test",
        suffix="a1b2c3d4e5",
    )

    assert payload["status"] == "pass"
    assert payload["usage"]["requests"] == 2
    assert payload["rate_limit_retries"] == 0
    assert all(payload["model_checks"]["create"].values())
    assert all(payload["model_checks"]["update"].values())
    assert payload["safety"]["same_draft_identity"] is True
    assert payload["safety"]["draft_absent_after"] is True
    assert payload["safety"]["email_sent"] is False
    assert provider.drafts == {}


def test_model_revision_identity_drift_fails_but_cleans_up(monkeypatch) -> None:
    import scripts.run_gmail_natural_draft_lifecycle as runner

    drafts = iter(
        [
            _result(
                "Hi Alex, thanks for the clinical operations note. I would be glad to "
                "compare notes on a possible short advisory project.\n\nSincerely,\nAnup"
            ),
            _result(
                "Hi Alex, happy to discuss the clinical operations advisory.\n\n"
                "Sincerely,\nAnup",
                message_id="wrong-message-id",
            ),
        ]
    )
    monkeypatch.setattr(runner, "_run_model", lambda *_args, **_kwargs: next(drafts))
    monkeypatch.setenv(GMAIL_TEST_DRAFT_DELETE_ENV, "true")
    provider = FakeProvider()

    payload = execute_lifecycle(
        model=EXPECTED_MODEL,
        budget_usd=0.10,
        session=object(),
        provider=provider,
        account="operator@example.test",
        recipient="reviewer@example.test",
        suffix="a1b2c3d4e5",
    )

    assert payload["status"] == "partial"
    assert payload["model_checks"]["update"]["same_message_identity"] is False
    assert payload["provider"]["update"]["draft_id_present"] is False
    assert payload["provider"]["delete"]["verification"]["absent"] is True
    assert payload["safety"]["draft_absent_after"] is True
    assert provider.drafts == {}


def test_retry_evidence_fails_but_cleans_up(monkeypatch) -> None:
    import scripts.run_gmail_natural_draft_lifecycle as runner

    drafts = iter(
        [
            _result(
                "Hi Alex, thanks for the clinical operations note. I would be glad to "
                "compare notes on a possible short advisory project.\n\nSincerely,\nAnup",
                retries=1,
            ),
            _result(
                "Hi Alex, happy to compare clinical operations notes on the short "
                "advisory project.\n\nSincerely,\nAnup"
            ),
        ]
    )
    monkeypatch.setattr(runner, "_run_model", lambda *_args, **_kwargs: next(drafts))
    monkeypatch.setenv(GMAIL_TEST_DRAFT_DELETE_ENV, "true")
    provider = FakeProvider()

    payload = execute_lifecycle(
        model=EXPECTED_MODEL,
        budget_usd=0.10,
        session=object(),
        provider=provider,
        account="operator@example.test",
        recipient="reviewer@example.test",
        suffix="a1b2c3d4e5",
    )

    assert payload["status"] == "partial"
    assert payload["rate_limit_retries"] == 1
    assert payload["safety"]["draft_absent_after"] is True
    assert provider.drafts == {}


def test_safe_receipt_omits_recipient_subject_and_body() -> None:
    safe = _safe_provider_receipt(
        {
            "status": "draft_created",
            "operation": "create",
            "draft_id": "draft-1",
            "approval_reference": "approval:create",
            "to": "private@example.test",
            "subject": "private subject",
            "body": "private body",
            "verification": {"passed": True},
        }
    )

    encoded = json.dumps(safe)
    assert safe["draft_id_present"] is True
    assert "private@example" not in encoded
    assert "private subject" not in encoded
    assert "private body" not in encoded


def test_comma_separated_do_not_clause_blocks_provider_draft_creation() -> None:
    plan = infer_gmail_execution_plan(
        "Prepare a concise reply for review. Do not send, create a provider draft, "
        "search, post, schedule, share, or write externally."
    )

    assert plan.operation == "draft_reply"
    assert plan.create_gmail_drafts is False
    assert plan.artifact_policy == "draft_text_in_output"
    assert plan.side_effect_policy == "read_only_or_draft_only"


def test_persisted_plan_requires_fresh_approval_and_no_search(require_local_evidence) -> None:
    plan = json.loads(
        require_local_evidence(
            "artifacts/test-pack/next-live-gmail-natural-draft-lifecycle-plan.json"
        ).read_text()
    )

    assert plan["approval_status"] == (
        "fresh_approval_required_after_current_allowance_exhaustion"
    )
    assert plan["expected_openai_requests"] == 2
    assert plan["budget_usd"] == 0.10
    assert plan["live_search"] is False
    assert plan["retries_allowed"] == 0
