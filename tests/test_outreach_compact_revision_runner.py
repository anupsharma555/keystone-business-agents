from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from keystone_agents.execution_identity import create_validation_execution_identity
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.schemas.outreach import OutreachDraft
from scripts.run_outreach_compact_revision import (
    EXPECTED_MODEL,
    EXPECTED_REQUESTS,
    _approved_context,
    _build_payload,
    _revalidate_saved_receipt,
    _typed_input,
    _write_result_atomic,
    build_parser,
)


def _passing_result() -> TypedAgentRunResult[OutreachDraft]:
    context = _approved_context()
    company_source = next(
        source_id
        for source_id in context.allowed_source_ids
        if source_id == "fixture:curebase_company"
    )
    draft = OutreachDraft.model_validate(
        {
            "company_name": "Curebase",
            "contact_name": "Dr. Example",
            "contact_title": "Clinical Operations Lead",
            "email_subject": "Compare notes on clinical research",
            "email_body": (
                "Hi Dr. Example,\n\nCurebase's decentralized clinical research workflows "
                "stood out. Happy to compare notes on evaluation support. Would a brief "
                "conversation be useful?\n\nSincerely,\nAnup"
            ),
            "linkedin_note": "",
            "personalization_rationale": "Uses only approved Curebase context.",
            "source_ids_used": [company_source, "keystone_profile"],
            "facts_used": [
                fact.model_dump(mode="json")
                for fact in context.allowed_facts
                if fact.source_id in {company_source, "keystone_profile"}
            ],
            "approved_context_used": True,
            "drafting_mode": "llm_constrained",
            "revision_request": context.revision_request,
            "approval_required": True,
            "approval_scope": "external_use",
            "send_enabled": False,
            "sent": False,
            "can_send_email": False,
        }
    )
    return TypedAgentRunResult(
        agent_name="outreach_composer",
        output=draft,
        raw_result=SimpleNamespace(new_items=[]),
        live=True,
        usage={"available": True, "requests": 1},
        cost={"available": True, "estimated_usd": 0.01},
        budget_guard={"enforced": True, "exceeded": False, "budget_usd": 0.05},
        request_cache={"rate_limit_retries": 0},
    )


def _identity():
    return create_validation_execution_identity(
        scenario="outreach_compact_approved_revision",
        route="outreach_composer",
        now=datetime(2026, 7, 11, 12, 30, tzinfo=UTC),
        nonce="a1b2c3d4",
    )


def test_runner_defaults_to_one_request_and_five_cent_budget(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["run_outreach_compact_revision.py"])
    args = build_parser().parse_args()

    assert args.model == EXPECTED_MODEL
    assert args.max_openai_requests == EXPECTED_REQUESTS
    assert args.budget_usd == 0.05


def test_runner_input_contains_approved_facts_existing_draft_and_style() -> None:
    context = _approved_context()
    typed_input = _typed_input(context)

    assert "existing_draft" in typed_input.approved_context
    assert "allowed_facts" in typed_input.approved_context
    assert "already works with" in typed_input.approved_context
    assert "Sincerely" in typed_input.email_style_profile


def test_passing_payload_requires_quality_identity_safety_and_receipts() -> None:
    context = _approved_context()
    payload = _build_payload(
        _passing_result(),
        context=context,
        execution_identity=_identity(),
        model=EXPECTED_MODEL,
        request_ceiling=EXPECTED_REQUESTS,
        budget_usd=0.05,
    )

    assert payload["status"] == "pass"
    assert all(payload["checks"].values())
    assert payload["safety"]["tool_count"] == 0
    assert payload["safety"]["provider_writes"] == 0
    assert payload["execution_identity"]["run_id"].startswith("kba_outreach_")


def test_approved_fact_fidelity_does_not_require_literal_clinical_research_phrase() -> None:
    context = _approved_context()
    result = _passing_result()
    result.final_output.email_body = (
        "Hi Dr. Example,\n\nI saw Curebase's work in decentralized clinical trial "
        "operations and thought there may be a useful fit with Keystone's clinical AI "
        "and research operations support.\n\nWould a brief conversation be useful?"
        "\n\nSincerely,\nAnup"
    )
    result.final_output.body = result.final_output.email_body

    payload = _build_payload(
        result,
        context=context,
        execution_identity=_identity(),
        model=EXPECTED_MODEL,
        request_ceiling=EXPECTED_REQUESTS,
        budget_usd=0.05,
    )

    assert payload["checks"]["approved_company_fact_preserved"] is True
    assert payload["checks"]["approved_keystone_positioning_preserved"] is True
    assert payload["checks"]["facts_used_approved_and_bounded"] is True


def test_payload_fails_on_request_overrun_or_tool_use() -> None:
    context = _approved_context()
    result = _passing_result()
    object.__setattr__(result, "usage", {"available": True, "requests": 2})
    result.raw_result.new_items.append(SimpleNamespace(type="tool_call_item"))

    payload = _build_payload(
        result,
        context=context,
        execution_identity=_identity(),
        model=EXPECTED_MODEL,
        request_ceiling=EXPECTED_REQUESTS,
        budget_usd=0.05,
    )

    assert payload["status"] == "partial"
    assert payload["checks"]["exact_request_count"] is False
    assert payload["checks"]["no_tools"] is False


def test_receipt_is_written_atomically(tmp_path) -> None:
    receipt = tmp_path / "outreach.json"
    _write_result_atomic(receipt, {"status": "pass", "requests": 1})

    assert json.loads(receipt.read_text(encoding="utf-8"))["requests"] == 1
    assert not receipt.with_suffix(".json.tmp").exists()


def test_saved_receipt_can_be_revalidated_without_model_call(tmp_path) -> None:
    result = _passing_result()
    receipt = tmp_path / "saved.json"
    saved = {
        "status": "partial",
        "output": result.final_output.model_dump(mode="json"),
        "usage": result.usage,
        "cost": result.cost,
        "budget_guard": result.budget_guard,
        "request_cache": result.request_cache,
        "execution_identity": _identity().receipt(),
    }
    receipt.write_text(json.dumps(saved), encoding="utf-8")

    restored, identity, initial_status = _revalidate_saved_receipt(receipt)

    assert restored.final_output.company_name == "Curebase"
    assert identity.run_id == saved["execution_identity"]["run_id"]
    assert initial_status == "partial"


def test_persisted_plan_requires_fresh_approval_with_current_billing_refresh() -> None:
    plan = json.loads(
        Path("artifacts/test-pack/next-live-outreach-compact-revision-plan.json").read_text(
            encoding="utf-8"
        )
    )

    assert plan["approval_status"] == "completed_within_eight_sequential_run_allowance"
    assert plan["model"] == EXPECTED_MODEL
    assert plan["expected_openai_requests"] == EXPECTED_REQUESTS
    assert plan["max_openai_requests"] == EXPECTED_REQUESTS
    assert plan["budget_usd"] == 0.05
    assert plan["provider_reads"] == 0
    assert plan["provider_writes"] == 0
    assert plan["live_search"] is False
    assert plan["retries_allowed"] == 0
    assert plan["result"]["status"] == "pass"
    assert plan["result"]["openai_requests"] == 1
    assert "SDK trace metadata" in plan["execution_identity"]
    assert plan["billing_baseline"]["chrome_billing_refresh"] == "completed"
    assert plan["billing_baseline"]["chrome_billing_credit_balance_usd"] == 2.93


def test_main_uses_same_execution_identity_for_trace_and_receipt(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    import scripts.run_outreach_compact_revision as runner

    captured: dict[str, object] = {}

    def fake_constrained_sdk(*_args, **kwargs):
        captured.update(kwargs)
        return _passing_result()

    output = tmp_path / "outreach-live.json"
    monkeypatch.setattr(runner, "run_outreach_composer_constrained_sdk", fake_constrained_sdk)
    monkeypatch.setattr(runner, "load_settings", lambda **_kwargs: None)
    monkeypatch.setattr(
        "sys.argv",
        ["run_outreach_compact_revision.py", "--output", str(output)],
    )

    assert runner.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert captured["trace_metadata"]["run_id"] == payload["execution_identity"]["run_id"]
    assert captured["trace_metadata"]["case_id"] == payload["execution_identity"]["case_id"]
    assert json.loads(output.read_text(encoding="utf-8")) == payload
