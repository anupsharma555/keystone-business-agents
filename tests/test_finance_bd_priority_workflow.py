from __future__ import annotations

from dataclasses import replace

import pytest

from keystone_agents.finance_bd_priority_workflow import (
    BusinessDevelopmentOption,
    collect_current_quarter_finance_packet,
    execute_finance_bd_priority_decision,
    finance_bd_priority_privacy_preview,
)
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult


def _options() -> tuple[BusinessDevelopmentOption, ...]:
    return (
        BusinessDevelopmentOption(
            option_id="option-alpha",
            title="Option Alpha",
            description="A low fixed-cost pilot with a fast validation cycle.",
        ),
        BusinessDevelopmentOption(
            option_id="option-beta",
            title="Option Beta",
            description="A higher fixed-cost commitment with a longer validation cycle.",
        ),
    )


def _finance_result() -> TypedAgentRunResult[ChiefOfStaffResult]:
    return TypedAgentRunResult(
        agent_name="chief_of_staff",
        output=ChiefOfStaffResult(
            mode="deterministic",
            summary=(
                "Q3 current-quarter finance summary: Total income: $10,000.00. "
                "Total expenses: $3,000.00. "
                "The quarter supports one bounded experiment; one expense is uncategorized."
            ),
            write_requests=[],
        ),
        raw_result={"deterministic": "finance_tax_tracker"},
        live=True,
    )


def _packet():
    return collect_current_quarter_finance_packet(
        options=_options(),
        live=True,
        finance_runner=lambda *_args, **_kwargs: _finance_result(),
    )


def test_collect_finance_packet_requires_verified_deterministic_provider_result() -> None:
    captured = {}

    def fake_runner(request, **kwargs):
        captured["request"] = request
        captured["kwargs"] = kwargs
        return _finance_result()

    packet = collect_current_quarter_finance_packet(
        options=_options(),
        live=True,
        finance_runner=fake_runner,
    )

    assert packet.finance_receipt["provider_read_verified"] is True
    assert packet.finance_receipt["provider_writes"] == 0
    assert packet.finance_receipt["raw_records_persisted"] is False
    assert "read-only" in captured["request"].casefold()
    assert captured["kwargs"]["force_sdk_interpretation"] is False
    assert captured["kwargs"]["manual_request_plan"] == {
        "source": "canonical:l174_19_finance_packet",
        "target_agent": "chief_of_staff",
        "intent": "context_lookup",
        "task_objective": "context_lookup",
        "primary_target": "finance_tax_tracker current quarter",
        "target_type": "business_system_context",
        "provider_system": "airtable",
        "provider_operations": ["read"],
    }


def test_offline_decision_plan_is_sanitized_and_no_write() -> None:
    packet = _packet()
    result = execute_finance_bd_priority_decision(packet, live_sdk=False)

    assert result["status"] == "validated_offline"
    assert result["plan"]["max_openai_requests"] == 1
    assert result["plan"]["provider_writes"] is False
    assert result["plan"]["proof_scope"] == "sanitized_context_proof"
    assert result["plan"]["transmission_mode"] == (
        "privacy_minimized_concept_signals"
    )
    assert packet.finance_summary not in str(result)


def test_privacy_preview_uses_provider_receipt_but_omits_exact_aggregate() -> None:
    packet = _packet()

    result = finance_bd_priority_privacy_preview(packet)

    assert result["status"] == "privacy_minimized_preview"
    assert result["provider_read_verified"] is True
    assert result["openai_requests_made"] == 0
    assert result["provider_writes"] == 0
    assert result["exact_financial_values_transmitted"] is False
    assert result["decision_signal_sufficient"] is True
    assert packet.finance_summary not in str(result)
    context = result["bundle"]["finance_context"]
    assert context["proof_scope"] == "sanitized_context_proof"
    assert context["transmission_contract"]["exact_financial_values"] is False
    concepts = {fact["concept"] for fact in context["facts"]}
    assert "operating_margin_positive" in concepts
    assert "expense_load_low" in concepts
    assert "$10,000.00" not in str(context)


def test_privacy_preview_flags_context_without_decision_signal() -> None:
    packet = replace(
        _packet(),
        finance_summary=(
            "Q3 current-quarter income and expense records include one uncategorized gap."
        ),
    )

    result = finance_bd_priority_privacy_preview(packet)

    assert result["status"] == "privacy_minimized_preview_insufficient"
    assert result["decision_signal_sufficient"] is False
    assert result["openai_requests_made"] == 0


def test_trusted_private_finance_mode_is_disabled() -> None:
    with pytest.raises(ValueError, match="Trusted-private finance synthesis is disabled"):
        execute_finance_bd_priority_decision(
            _packet(),
            live_sdk=True,
            approved_private_context=True,
        )


def test_fake_model_decision_compares_both_selects_one_and_preserves_receipts() -> None:
    packet = _packet()
    captured = {}

    def fake_decision_runner(sdk_input, **kwargs):
        captured["sdk_input"] = sdk_input
        captured["kwargs"] = kwargs
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Option Alpha fits the current-quarter finance constraint because its "
                    "fixed cost and validation cycle are bounded. Option Beta creates a "
                    "larger budget commitment. Recommend Option Alpha, then ask Opportunity "
                    "Scout to validate it without search or writes."
                ),
            ),
            raw_result={},
            live=True,
            usage={"requests": 1},
            cost={"estimated_usd": 0.02},
        )

    result = execute_finance_bd_priority_decision(
        packet,
        live_sdk=True,
        approved_privacy_minimized_context=True,
        decision_runner=fake_decision_runner,
    )

    assert result["status"] == "passed"
    assert result["selected_option_id"] == "option-alpha"
    assert result["decision_storage"] == "local_artifact_only"
    assert "Option Alpha" in result["decision"]
    assert result["both_options_considered"] is True
    assert [item["schema"] for item in result["specialist_receipts"]] == [
        "keystone.finance_context.receipt.v1",
        "keystone.manager_decision.receipt.v1",
    ]
    assert result["specialist_receipts"][1]["downstream_owner"] == "opportunity_scout"
    assert captured["kwargs"]["attach_tools"] is False
    assert captured["kwargs"]["include_specialist_tools"] is False
    assert captured["kwargs"]["quality_budget"].max_turns == 1
    assert captured["sdk_input"]["manual_request_plan"] == {
        "source": "canonical:l174_19_finance_bd_priority",
        "target_agent": "chief_of_staff",
        "workflow": ["chief_of_staff", "opportunity_scout"],
        "intent": "route_request",
        "task_objective": "route_or_continue",
        "primary_target": "next business-development priority",
    }
    assert packet.finance_summary not in str(captured["sdk_input"]["finance_context"])
    assert captured["sdk_input"]["finance_context"]["proof_scope"] == (
        "sanitized_context_proof"
    )
    assert {
        fact["concept"] for fact in captured["sdk_input"]["finance_context"]["facts"]
    } >= {"current_quarter", "income_data_present", "expense_data_present"}
    assert result["exact_financial_values_transmitted"] is False
    assert result["data_handling"]["response_store"] is False


def test_live_decision_requires_private_context_approval() -> None:
    with pytest.raises(ValueError, match="privacy-minimized posture packet"):
        execute_finance_bd_priority_decision(_packet(), live_sdk=True)


def test_decision_rejects_ambiguous_selection() -> None:
    packet = _packet()

    def fake_runner(*_args, **_kwargs):
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "Option Alpha and Option Beta both affect the quarterly finance budget, "
                    "but no priority is selected."
                ),
            ),
            raw_result={},
            live=True,
            usage={"requests": 1},
            cost={"estimated_usd": 0.01},
        )

    with pytest.raises(RuntimeError, match="select exactly one"):
        execute_finance_bd_priority_decision(
            packet,
            live_sdk=True,
            approved_privacy_minimized_context=True,
            decision_runner=fake_runner,
        )
