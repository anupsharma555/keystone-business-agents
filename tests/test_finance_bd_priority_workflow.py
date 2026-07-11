from __future__ import annotations

import pytest

from keystone_agents.finance_bd_priority_workflow import (
    BusinessDevelopmentOption,
    collect_current_quarter_finance_packet,
    execute_finance_bd_priority_decision,
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
                "Q3 current-quarter finance summary: income and expense records were read. "
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


def test_offline_decision_plan_is_sanitized_and_no_write() -> None:
    packet = _packet()
    result = execute_finance_bd_priority_decision(packet, live_sdk=False)

    assert result["status"] == "validated_offline"
    assert result["plan"]["max_openai_requests"] == 1
    assert result["plan"]["provider_writes"] is False
    assert packet.finance_summary not in str(result)


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
        approved_private_context=True,
        decision_runner=fake_decision_runner,
    )

    assert result["status"] == "passed"
    assert result["selected_option_id"] == "option-alpha"
    assert result["both_options_considered"] is True
    assert [item["schema"] for item in result["specialist_receipts"]] == [
        "keystone.finance_context.receipt.v1",
        "keystone.manager_decision.receipt.v1",
    ]
    assert result["specialist_receipts"][1]["downstream_owner"] == "opportunity_scout"
    assert captured["kwargs"]["attach_tools"] is False
    assert captured["kwargs"]["include_specialist_tools"] is False
    assert captured["kwargs"]["quality_budget"].max_turns == 1
    assert packet.finance_summary in captured["sdk_input"]["finance_context"]
    assert result["data_handling"]["response_store"] is False


def test_live_decision_requires_private_context_approval() -> None:
    with pytest.raises(ValueError, match="private-context approval"):
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
            approved_private_context=True,
            decision_runner=fake_runner,
        )
