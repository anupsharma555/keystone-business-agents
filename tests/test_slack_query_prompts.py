from __future__ import annotations

import pytest

from keystone_agents.schemas.work_item import WorkItemRoute
from keystone_agents.skill_sets import select_agent_skill_names
from keystone_agents.slack_query_prompts import (
    SLACK_QUERY_PROMPT_SCHEMA,
    SlackQueryPromptKind,
    build_slack_query_prompt_input,
    resolve_slack_query_prompt,
    slack_query_prompt_external_context,
)


@pytest.mark.parametrize(
    ("request_text", "manual_plan", "expected_kind", "expected_route"),
    [
        (
            "research Acme Health and summarize the best sources",
            {"target_agent": "business_research_analyst", "intent": "company_research"},
            SlackQueryPromptKind.RESEARCH_SUMMARY,
            WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        ),
        (
            "do a deeper read-only search on teen safety sources",
            {"target_agent": "business_research_analyst", "intent": "research_brief"},
            SlackQueryPromptKind.DEEPER_RESEARCH,
            WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        ),
        (
            "find 3 partnership opportunities in behavioral health AI",
            {"target_agent": "opportunity_scout", "intent": "opportunity_search"},
            SlackQueryPromptKind.OPPORTUNITY_SEARCH,
            WorkItemRoute.OPPORTUNITY_SCOUT,
        ),
        (
            "draft an outreach email from approved context",
            {"target_agent": "outreach_composer", "intent": "outreach_draft"},
            SlackQueryPromptKind.OUTREACH_DRAFT,
            WorkItemRoute.OUTREACH_COMPOSER,
        ),
        (
            "summarize thread and recommend the next action",
            {"target_agent": "chief_of_staff", "intent": "slack_operations"},
            SlackQueryPromptKind.THREAD_SUMMARY_NEXT_ACTION,
            WorkItemRoute.CHIEF_OF_STAFF,
        ),
        (
            "revise this draft with my feedback",
            {"target_agent": "outreach_composer", "intent": "outreach_draft"},
            SlackQueryPromptKind.CONTINUE_OR_REVISE,
            WorkItemRoute.OUTREACH_COMPOSER,
        ),
    ],
)
def test_slack_query_prompt_library_maps_common_query_types(
    request_text: str,
    manual_plan: dict[str, str],
    expected_kind: SlackQueryPromptKind,
    expected_route: WorkItemRoute,
) -> None:
    selection = resolve_slack_query_prompt(
        build_slack_query_prompt_input(
            raw_request=request_text,
            selected_message_text="Thread says Acme Health is relevant.",
            manual_plan=manual_plan,
        )
    )

    assert selection is not None
    assert selection.kind == expected_kind
    assert selection.target_route == expected_route
    assert selection.task_brief.startswith("Reusable Slack query task brief.")
    assert "does not grant tools, live access, or approval" in selection.task_brief
    assert len(selection.dynamic_content_sha256) == 64


def test_slack_query_prompt_rejects_unsafe_side_effect_request() -> None:
    selection = resolve_slack_query_prompt(
        build_slack_query_prompt_input(
            raw_request="send this outreach email to Priya now",
            manual_plan={"target_agent": "outreach_composer", "intent": "outreach_draft"},
        )
    )

    assert selection is None


def test_slack_query_prompt_allows_negated_side_effect_constraints() -> None:
    selection = resolve_slack_query_prompt(
        build_slack_query_prompt_input(
            raw_request=(
                "reusable source-read test. Compare how three public AI companion "
                "or chatbot products describe teen safety, escalation, or trusted-contact "
                "features. Do not draft, send, publish, schedule, write files, or post elsewhere."
            ),
            target_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        )
    )

    assert selection is not None
    assert selection.kind == SlackQueryPromptKind.RESEARCH_SUMMARY
    assert selection.target_route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert selection.context_flags["needs_source_triage"]
    assert "Resolve three named products" in selection.task_brief
    assert "source organizations, news articles, policy reports" in selection.task_brief
    assert "evidence only" in selection.task_brief
    assert "AI companions or chatbots" in selection.task_brief
    assert "media outlets, regulators, or research organizations" in selection.task_brief
    assert (
        "Do not collapse a category request into one generic company profile"
        in selection.task_brief
    )


def test_slack_query_prompt_redacts_secret_like_values_and_bounds_context() -> None:
    selection = resolve_slack_query_prompt(
        build_slack_query_prompt_input(
            raw_request="research Acme using api_key=secret-123",
            selected_message_text="Slack token xoxb-123456789012-abcdef should not appear.",
            thread_summary="A" * 2000,
            manual_plan={"target_agent": "business_research_analyst", "intent": "company_research"},
        )
    )

    assert selection is not None
    assert "secret-123" not in selection.task_brief
    assert "xoxb-123456789012-abcdef" not in selection.task_brief
    assert "[redacted]" in selection.task_brief
    assert len(selection.task_brief) < 5000


def test_slack_query_prompt_keeps_deterministic_route_authoritative() -> None:
    selection = resolve_slack_query_prompt(
        build_slack_query_prompt_input(
            raw_request="find partnership opportunities but keep this on business research",
            manual_plan={
                "target_agent": "business_research_analyst",
                "intent": "opportunity_search",
            },
        )
    )

    assert selection is not None
    assert selection.kind == SlackQueryPromptKind.RESEARCH_SUMMARY
    assert selection.target_route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST
    assert selection.route_mismatch == {}


def test_slack_query_prompt_context_flags_select_reusable_agent_skill_contracts() -> None:
    selection = resolve_slack_query_prompt(
        build_slack_query_prompt_input(
            raw_request="do a deeper source-backed search",
            manual_plan={"target_agent": "business_research_analyst", "intent": "research_brief"},
        )
    )

    assert selection is not None
    business_skills = select_agent_skill_names(
        "business_research_analyst",
        request_text="",
        context_flags=selection.context_flags,
    )
    opportunity_skills = select_agent_skill_names(
        "opportunity_scout",
        request_text="",
        context_flags=selection.context_flags,
    )
    assert "source_triage_decision" in business_skills
    assert "evidence_attribution_and_claim_mapping" in business_skills
    assert "source_triage_decision" in opportunity_skills


def test_slack_query_prompt_metadata_excludes_full_task_brief() -> None:
    selection = resolve_slack_query_prompt(
        build_slack_query_prompt_input(
            raw_request="research Acme Health",
            manual_plan={"target_agent": "business_research_analyst", "intent": "company_research"},
        )
    )

    assert selection is not None
    metadata = selection.metadata()
    external_context = slack_query_prompt_external_context(selection)
    assert metadata["schema"] == SLACK_QUERY_PROMPT_SCHEMA
    assert "task_brief" not in metadata
    assert metadata["task_brief_chars"] == len(selection.task_brief)
    assert external_context["slack_query_prompt"]["task_brief"] == selection.task_brief
