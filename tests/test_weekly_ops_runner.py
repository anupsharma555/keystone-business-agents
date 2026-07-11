from __future__ import annotations

from decimal import Decimal

import pytest

from keystone_agents.models import TypedAgentRunResult
from keystone_agents.schemas.chief_of_staff import (
    ChiefOfStaffResult,
    ChiefOfStaffRouteRecommendation,
)
from keystone_agents.schemas.weekly_ops import WeeklyOpsAssemblyInput
from keystone_agents.weekly_ops_runner import run_weekly_ops_packet_synthesis


def _payload() -> WeeklyOpsAssemblyInput:
    return WeeklyOpsAssemblyInput.model_validate(
        {
            "window": {
                "time_min": "2026-07-04T00:00:00-04:00",
                "time_max": "2026-07-11T00:00:00-04:00",
                "packet_date": "2026-07-10",
            },
            "slack": [{"message_ts": "1", "summary": "One workstream completed."}],
            "gmail": [
                {
                    "thread_id": "thread-1",
                    "subject": "Follow-up",
                    "summary": "One follow-up is relevant.",
                }
            ],
            "completed_runs": [
                {
                    "run_id": "run-1",
                    "agent_name": "business_research_analyst",
                    "completed_at": "2026-07-10T12:00:00-04:00",
                    "outcome_summary": "Research completed.",
                    "packet_role": "primary",
                    "relevance_reason": "Human-requested work.",
                }
            ],
            "calendar": [
                {
                    "event_id": "event-1",
                    "title": "Client review",
                    "start": "2026-07-09T13:00:00-04:00",
                }
            ],
        }
    )


def test_weekly_runner_defaults_to_offline_validation() -> None:
    result = run_weekly_ops_packet_synthesis(_payload())

    assert result["status"] == "validated_offline"
    assert result["plan"]["workflow_name"] == "Chief Prior Week Packet"
    assert result["plan"]["max_openai_requests"] == 1
    assert result["plan"]["provider_writes"] is False
    assert result["bundle"]["delivery_plan"]["slack_channel_name"] == "ops-finance"


def test_weekly_runner_enforces_exact_live_limits() -> None:
    with pytest.raises(ValueError, match="max_openai_requests=1"):
        run_weekly_ops_packet_synthesis(_payload(), max_openai_requests=2)
    with pytest.raises(ValueError, match="cost ceiling"):
        run_weekly_ops_packet_synthesis(_payload(), max_cost_usd=Decimal("0.06"))


def test_weekly_runner_live_path_is_one_turn_without_tools() -> None:
    captured: dict[str, object] = {}

    def fake_runner(typed_input: object, **kwargs: object) -> TypedAgentRunResult:
        captured["input"] = typed_input
        captured.update(kwargs)
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary="Review-only weekly packet.",
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="portfolio-review",
                    target_channel="#ops-finance",
                ),
                approval_required=True,
            ),
            raw_result=None,
            live=True,
            usage={"requests": 1},
            cost={"estimated_usd": 0.01},
        )

    result = run_weekly_ops_packet_synthesis(
        _payload(),
        live_sdk=True,
        approved_external_business_synthesis=True,
        runner=fake_runner,
    )

    budget = captured["quality_budget"]
    assert budget.max_turns == 1
    assert budget.max_tool_calls == 0
    assert captured["include_specialist_tools"] is False
    assert captured["attach_tools"] is False
    external_bundle = captured["input"]["weekly_ops_source_bundle"]
    assert external_bundle["schema"] == (
        "keystone.weekly_ops.external_synthesis_bundle.v1"
    )
    assert "source_id" not in str(external_bundle)
    assert result["usage"]["requests"] == 1
    assert result["provider_writes"] is False


def test_weekly_runner_rejects_missing_usage_or_cost_receipt() -> None:
    def fake_runner(_typed_input: object, **_kwargs: object) -> TypedAgentRunResult:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary="Packet without a receipt.",
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="portfolio-review"
                ),
            ),
            raw_result=None,
            live=True,
        )

    with pytest.raises(RuntimeError, match="usage evidence"):
        run_weekly_ops_packet_synthesis(
            _payload(),
            live_sdk=True,
            approved_external_business_synthesis=True,
            runner=fake_runner,
        )


def test_weekly_runner_rejects_cost_over_ceiling() -> None:
    def fake_runner(_typed_input: object, **_kwargs: object) -> TypedAgentRunResult:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary="Expensive packet.",
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="portfolio-review"
                ),
            ),
            raw_result=None,
            live=True,
            usage={"requests": 1},
            cost={"estimated_usd": 0.051},
        )

    with pytest.raises(RuntimeError, match="cost ceiling"):
        run_weekly_ops_packet_synthesis(
            _payload(),
            live_sdk=True,
            approved_external_business_synthesis=True,
            runner=fake_runner,
        )


def test_weekly_runner_rejects_slack_or_write_requests() -> None:
    def fake_runner(_typed_input: object, **_kwargs: object) -> TypedAgentRunResult:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary="Unsafe packet.",
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="portfolio-review",
                    target_channel="#ops-finance",
                ),
                slack_post_allowed=True,
                slack_post_policy="channel_policy_allowed",
                slack_target_channel="#ops-finance",
                blocked_side_effects=[
                    "gmail_send",
                    "calendar_create_or_update",
                    "repo_write",
                    "linkedin_publish",
                    "crm_write",
                ],
            ),
            raw_result=None,
            live=True,
            usage={"requests": 1},
            cost={"estimated_usd": 0.01},
        )

    with pytest.raises(RuntimeError, match="side-effect request"):
        run_weekly_ops_packet_synthesis(
            _payload(),
            live_sdk=True,
            approved_external_business_synthesis=True,
            runner=fake_runner,
        )
