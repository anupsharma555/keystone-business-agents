from __future__ import annotations

from decimal import Decimal

import pytest

from keystone_agents.models import TypedAgentRunResult
from keystone_agents.schemas.chief_of_staff import (
    ChiefOfStaffResult,
    ChiefOfStaffRouteRecommendation,
)
from keystone_agents.schemas.weekly_ops import WeeklyOpsAssemblyInput
from keystone_agents.weekly_ops_packet import build_weekly_ops_privacy_minimized_packet
from keystone_agents.weekly_ops_runner import (
    run_weekly_ops_packet_synthesis,
    weekly_ops_privacy_preview,
)


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


def _packet_summary() -> str:
    return "\n".join(
        [
            "Executive focus areas",
            "1 Slack workstream is completed. 1 Gmail follow-up requires follow up.",
            "Workstreams and decisions",
            "The Gmail action state is follow up required.",
            "Completed runs and outcomes",
            "1 completed run covered research operations.",
            "Carry forward",
            "One carry-forward.",
            "One-time Calendar focus",
            "1 one-time Calendar item covered calendar operations.",
            "Recurring Calendar cadence",
            "One cadence.",
            "Next actions",
            "One action.",
            "Source basis",
            "Slack, Gmail, completed runs, and Calendar.",
            "Operational health",
            "No exception.",
            "Packet metadata",
            "One request; internal review destination.",
        ]
    )


def test_weekly_runner_defaults_to_offline_validation() -> None:
    result = run_weekly_ops_packet_synthesis(_payload())

    assert result["status"] == "validated_offline"
    assert result["plan"]["workflow_name"] == "Chief Prior Week Packet"
    assert result["plan"]["max_openai_requests"] == 1
    assert result["plan"]["provider_writes"] is False
    assert result["bundle"]["delivery_plan"]["slack_channel_name"] == "ops-finance"


def test_weekly_privacy_packet_has_typed_operational_relationships() -> None:
    packet = build_weekly_ops_privacy_minimized_packet(_payload())
    assertions = {
        (item.subject, item.predicate, item.object): item.count
        for item in packet.assertions
    }

    assert assertions[("slack_workstream", "status", "completed")] == 1
    assert assertions[("gmail_follow_up", "action_state", "follow_up_required")] == 1
    assert assertions[("completed_run", "workstream", "research_operations")] == 1
    assert assertions[("one_time_calendar", "workstream", "calendar_operations")] == 1
    rendered = str(packet.model_dump(mode="json", by_alias=True))
    assert "thread-1" not in rendered
    assert "Client review" not in rendered
    assert "One follow-up is relevant" not in rendered


def test_weekly_privacy_preview_is_an_executable_zero_call_receipt() -> None:
    result = weekly_ops_privacy_preview(_payload())

    assert result["status"] == "privacy_minimized_preview"
    assert result["synthesis_ready"] is True
    assert result["source_count"] == 4
    assert result["assertion_count"] >= 4
    assert result["plan"]["transmission_mode"] == "privacy_minimized_assertions"
    assert result["bundle"]["schema"] == "keystone.privacy_minimized_synthesis.v1"
    assert result["openai_requests_made"] == 0
    assert result["provider_writes"] == 0
    assert result["send_enabled"] is False


def test_weekly_runner_enforces_exact_live_limits() -> None:
    with pytest.raises(ValueError, match="max_openai_requests=1"):
        run_weekly_ops_packet_synthesis(_payload(), max_openai_requests=2)
    with pytest.raises(ValueError, match="cost ceiling"):
        run_weekly_ops_packet_synthesis(_payload(), max_cost_usd=Decimal("0.06"))


def test_weekly_runner_minimized_live_path_accepts_typed_source_counts() -> None:
    captured: dict[str, object] = {}

    def fake_runner(typed_input: object, **kwargs: object) -> TypedAgentRunResult:
        captured["input"] = typed_input
        captured.update(kwargs)
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=_packet_summary(),
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
        approved_privacy_minimized_context=True,
        runner=fake_runner,
    )

    assert result["status"] == "success"
    assert result["proof_scope"] == "sanitized_context_proof"

    budget = captured["quality_budget"]
    assert budget.max_turns == 1
    assert budget.max_tool_calls == 0
    assert captured["include_specialist_tools"] is False
    assert captured["attach_tools"] is False
    minimized = captured["input"]["privacy_minimized_context"]
    assert minimized["schema"] == "keystone.privacy_minimized_synthesis.v1"
    assert minimized["proof_scope"] == "sanitized_context_proof"
    assert "thread-1" not in str(minimized)
    assert "One follow-up is relevant" not in str(minimized)
    assert "2026-07-04 through 2026-07-11" in captured["input"]["request"]
    assert "Typed operational basis" in result["packet"]["summary"]
    assert "Gmail follow up action state: follow up required (count 1)" in result[
        "packet"
    ]["summary"]


def test_weekly_runner_materializes_missing_typed_wording_without_retry() -> None:
    def fake_runner(_typed_input: object, **_kwargs: object) -> TypedAgentRunResult:
        summary = _packet_summary().replace(
            "The Gmail action state is follow up required.",
            "The email queue needs review.",
        ).replace(
            "1 Gmail follow-up requires follow up.",
            "Email follow-up items are included.",
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(mode="llm", summary=summary),
            raw_result=None,
            live=True,
            usage={"requests": 1},
            cost={"estimated_usd": 0.01},
        )

    result = run_weekly_ops_packet_synthesis(
        _payload(),
        live_sdk=True,
        approved_privacy_minimized_context=True,
        runner=fake_runner,
    )

    assert result["status"] == "success"
    assert "Gmail follow up action state: follow up required (count 1)" in result[
        "packet"
    ]["summary"]


def test_weekly_runner_rejects_trusted_private_context() -> None:
    with pytest.raises(ValueError, match="Trusted-private weekly synthesis is disabled"):
        run_weekly_ops_packet_synthesis(
            _payload(),
            live_sdk=True,
            approved_private_context=True,
        )


def test_weekly_runner_rejects_missing_usage_or_cost_receipt() -> None:
    def fake_runner(_typed_input: object, **_kwargs: object) -> TypedAgentRunResult:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=_packet_summary(),
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
            approved_privacy_minimized_context=True,
            runner=fake_runner,
        )


def test_weekly_runner_rejects_cost_over_ceiling() -> None:
    def fake_runner(_typed_input: object, **_kwargs: object) -> TypedAgentRunResult:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=_packet_summary(),
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
            approved_privacy_minimized_context=True,
            runner=fake_runner,
        )


def test_weekly_runner_rejects_slack_or_write_requests() -> None:
    def fake_runner(_typed_input: object, **_kwargs: object) -> TypedAgentRunResult:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=_packet_summary(),
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
            approved_privacy_minimized_context=True,
            runner=fake_runner,
        )


def test_weekly_runner_rejects_missing_required_sections() -> None:
    def fake_runner(_typed_input: object, **_kwargs: object) -> TypedAgentRunResult:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(mode="llm", summary="A generic weekly summary."),
            raw_result=None,
            live=True,
            usage={"requests": 1},
            cost={"estimated_usd": 0.01},
        )

    with pytest.raises(RuntimeError, match="required sections"):
        run_weekly_ops_packet_synthesis(
            _payload(),
            live_sdk=True,
            approved_privacy_minimized_context=True,
            runner=fake_runner,
        )
