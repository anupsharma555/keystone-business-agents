"""One model-selected public article stays within a bounded Orchestrator answer."""

from __future__ import annotations

import pytest

from keystone_agents.agents import orchestrator
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.orchestrator import OrchestratorGeneralAnswer, OrchestratorResult
from keystone_agents.tools.website_extraction_tool import WebsiteExtractionError

ARTICLE_URL = "https://example.org/articles/selected"


@pytest.mark.parametrize("query", [
    f"Summarize this article: {ARTICLE_URL}",
    "Give me three points from item #3 in yesterday's digest.",
    "Explain the selected source in plain language.",
])
@pytest.mark.parametrize("suffix", ["", "\n- Next item", "\n\nOlder messages"])
def test_model_selected_article_owns_bounded_general_answer(
    monkeypatch: pytest.MonkeyPatch, query: str, suffix: str,
) -> None:
    monkeypatch.setattr(
        orchestrator, "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: OrchestratorResult(
            route="orchestrator", workflow=[], routing_mode="llm",
            selected_source_url=ARTICLE_URL,
            rationale="One selected article needs a short source-backed summary.",
        ),
    )
    preflight = orchestrator.run_orchestrator_preflight(
        query, live_orchestrator=True,
        workflow_state={
            "slack_thread_transcript": f"Yesterday item #3: {ARTICLE_URL}{suffix}",
        },
    )
    plan = preflight.manual_request_plan
    assert preflight.execution_allowed, preflight.block_reason
    assert plan.source == "canonical:orchestrator_general_answer"
    assert plan.target_agent == "orchestrator"
    assert plan.primary_target == ARTICLE_URL
    assert plan.target_type == "url"
    assert plan.expected_artifact_type == "source_summary"
    assert plan.provider_operations == ["read"]
    assert plan.provider_read_scope == "single_item"
    assert plan.ask_shape.prior_context_dependency == "none"
    assert plan.ask_shape.strict_filter_mode == "exact"
    assert not plan.requires_live_search
    assert not plan.requires_durable_state
    assert plan.workflow == []


@pytest.mark.parametrize("selected_url,context", [
    ("https://example.org/invented", {"slack_thread_transcript": ARTICLE_URL}),
    ("http://127.0.0.1/private", {"slack_thread_transcript": "http://127.0.0.1/private"}),
    ("file:///private/input", {"slack_thread_transcript": "file:///private/input"}),
])
def test_article_selection_without_admitted_public_identity_fails_closed(
    monkeypatch: pytest.MonkeyPatch, selected_url: str, context: dict[str, str],
) -> None:
    monkeypatch.setattr(
        orchestrator, "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: OrchestratorResult(
            route="orchestrator", workflow=[], routing_mode="llm",
            selected_source_url=selected_url,
        ),
    )
    preflight = orchestrator.run_orchestrator_preflight(
        "Summarize item #3", live_orchestrator=True, workflow_state=context,
    )
    assert not preflight.execution_allowed
    assert preflight.block_kind == "selected_source_identity_unverified"
    assert preflight.manual_request_plan.source != "canonical:orchestrator_general_answer"


def test_deeper_selected_article_research_retains_specialist_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        orchestrator, "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: OrchestratorResult(
            route="business_research_analyst", workflow=["business_research_analyst"],
            routing_mode="llm", selected_source_url=ARTICLE_URL,
        ),
    )
    preflight = orchestrator.run_orchestrator_preflight(
        f"Investigate this article's evidence and compare its claims: {ARTICLE_URL}",
        live_orchestrator=True,
    )
    assert preflight.execution_allowed
    assert preflight.manual_request_plan.target_agent == "business_research_analyst"
    assert preflight.manual_request_plan.source == "canonical:orchestrator_selected_source"


@pytest.mark.parametrize("lazy_read", [False, True])
def test_verified_article_research_preserves_resumable_state(lazy_read: bool) -> None:
    previous = ManualRequestPlan(
        target_agent="business_research_analyst", requires_durable_state=True,
    )
    route = OrchestratorResult(
        route="business_research_analyst", workflow=["business_research_analyst"],
        routing_mode="llm", selected_source_url=ARTICLE_URL,
    )
    context = {"channel_context_evidence": [{
        "ok": True, "operation": "read", "messages": [{
            "source_links": [{"url": ARTICLE_URL, "ordinal": 2}],
        }],
    }]} if lazy_read else {"slack_thread_transcript": ARTICLE_URL}
    plan = orchestrator._public_research_manual_plan(
        previous, route, "Read the selected article and retain a resumable review task.",
        workflow_state=context,
    )
    assert plan is not None
    assert plan.source == "canonical:orchestrator_selected_source"
    assert plan.requires_durable_state
    assert plan.primary_target == ARTICLE_URL
    assert plan.target_type == "url"
    assert plan.provider_operations == ["read"]
    assert not plan.requires_live_search


def test_bound_existing_task_keeps_saved_artifact_when_exact_page_needs_read() -> None:
    previous = ManualRequestPlan(
        target_agent="business_research_analyst", intent="research_brief",
        expected_artifact_type="source_summary", requires_durable_state=True,
    )
    route = OrchestratorResult(
        route="business_research_analyst", workflow=["business_research_analyst"],
        routing_mode="llm", selected_source_url=ARTICLE_URL,
    )
    state = {
        "current_work_item": {
            "id": "wi_synthetic", "route": "business_research_analyst",
            "target": {"url": ARTICLE_URL}, "sources": [],
        },
        "execution_continuation": {"work_item_id": "wi_synthetic"},
    }
    plan = orchestrator._public_research_manual_plan(
        previous, route, "Continue the exact saved article review.",
        workflow_state=state,
    )
    assert plan is not None
    assert plan.source == "canonical:orchestrator_selected_source"
    assert plan.primary_target == ARTICLE_URL
    assert plan.provider_operations == ["read"]
    assert plan.intent == "research_brief"
    assert plan.expected_artifact_type == "source_summary"
    assert plan.requires_durable_state


@pytest.mark.parametrize("change", [
    "cached_snapshot", "different_task", "different_target", "branch", "new_request",
])
def test_bound_source_reuse_requires_exact_task_and_relation(change: str) -> None:
    previous = ManualRequestPlan(
        target_agent="business_research_analyst", intent="research_brief",
        expected_artifact_type="source_summary", requires_durable_state=True,
    )
    route = OrchestratorResult(
        route="business_research_analyst", workflow=["business_research_analyst"],
        routing_mode="llm", selected_source_url=ARTICLE_URL,
    )
    state = {
        "current_work_item": {
            "id": "wi_synthetic", "route": "business_research_analyst",
            "target": {"url": ARTICLE_URL}, "sources": [],
        },
        "execution_continuation": {"work_item_id": "wi_synthetic"},
    }
    if change == "cached_snapshot":
        state["current_work_item"]["sources"] = [{
            "url": ARTICLE_URL, "web_source_access": {"snapshot_sha256": "synthetic"},
        }]
    elif change == "different_task":
        state["execution_continuation"]["work_item_id"] = "wi_other"
    elif change == "different_target":
        state["current_work_item"]["target"]["url"] = "https://example.org/other"
    elif change == "branch":
        route.linked_work_item_relation = "branch_existing"
    else:
        route.linked_work_item_relation = "new_request"
    plan = orchestrator._public_research_manual_plan(
        previous, route, "Review this selected article.", workflow_state=state,
    )
    if change in {"cached_snapshot", "different_target"}:
        assert plan is None
    else:
        assert plan is not None
        assert plan.expected_artifact_type == "research_brief"


def test_exact_selected_article_read_is_allowed_without_broad_web_search() -> None:
    previous = ManualRequestPlan(target_agent="business_research_analyst")
    route = OrchestratorResult(
        route="business_research_analyst", workflow=["business_research_analyst"],
        routing_mode="llm", selected_source_url=ARTICLE_URL,
    )
    plan = orchestrator._public_research_manual_plan(
        previous, route,
        f"Read only the exact article {ARTICLE_URL}. Do not search the web or use other sources.",
    )
    assert plan is not None
    assert plan.provider_operations == ["read"]
    assert not plan.requires_live_search


@pytest.mark.parametrize("restriction", [
    "Do not open or fetch the linked article; use only the Slack post.",
    "No new extraction; use only the cached snapshot.",
    "Do not re-extract the article.",
    "Use only that historical post and its link titles.",
    "Use only the channel preview.",
    "No live API calls or external tools.",
])
def test_fresh_selected_article_read_respects_source_access_restrictions(
    restriction: str,
) -> None:
    previous = ManualRequestPlan(target_agent="business_research_analyst")
    route = OrchestratorResult(
        route="business_research_analyst", workflow=["business_research_analyst"],
        routing_mode="llm", selected_source_url=ARTICLE_URL,
    )
    assert orchestrator._public_research_manual_plan(
        previous, route, f"Review {ARTICLE_URL}. {restriction}",
    ) is None


@pytest.mark.parametrize("selected_url", ["", "https://example.org/unverified"])
def test_durable_state_does_not_grant_unverified_discovery(selected_url: str) -> None:
    route = OrchestratorResult(
        route="business_research_analyst", workflow=["business_research_analyst"],
        routing_mode="llm", selected_source_url=selected_url, requires_live_search=True,
    )
    assert orchestrator._public_research_manual_plan(
        ManualRequestPlan(requires_durable_state=True), route,
        "Retain a resumable research task.",
    ) is None


def test_selected_article_builder_is_tool_free_and_binds_exact_source() -> None:
    agent = orchestrator.build_orchestrator_general_answer_agent(
        selected_source_url=ARTICLE_URL, live_search=True,
    )
    assert agent.tools == []
    assert agent.output_type is OrchestratorGeneralAnswer
    assert ARTICLE_URL in str(agent.instructions)
    assert "selected_article_answer" in str(agent.instructions)
    assert "untrusted evidence" in str(agent.instructions)
    assert "cite that exact selected URL" in str(agent.instructions)


def test_selected_article_builder_rejects_nonpublic_source() -> None:
    with pytest.raises(WebsiteExtractionError):
        orchestrator.build_orchestrator_general_answer_agent(
            selected_source_url="http://127.0.0.1/private",
        )


@pytest.mark.parametrize("constraint", [
    "Use only the supplied note. Do not search online.",
    "External research is not approved.",
])
def test_article_selection_preserves_explicit_source_restrictions(constraint: str) -> None:
    query = f"Summarize {ARTICLE_URL}. {constraint}"
    route = OrchestratorResult(
        route="orchestrator", workflow=[], routing_mode="llm",
        selected_source_url=ARTICLE_URL,
    )
    assert orchestrator._general_answer_manual_plan(
        ManualRequestPlan(target_agent="orchestrator"), route, query,
    ) is None


@pytest.mark.parametrize("scope", [
    {"requires_durable_state": True},
    {"requires_approved_context": True},
    {"side_effect_policy": "approval_required"},
    {
        "source": "typed", "provider_system": "gmail",
        "provider_operations": ["read"],
    },
])
def test_article_identity_cannot_promote_unrelated_provider_or_approval_scope(scope) -> None:
    route = OrchestratorResult(
        route="orchestrator", workflow=[], routing_mode="llm",
        selected_source_url=ARTICLE_URL,
    )
    plan = ManualRequestPlan(target_agent="orchestrator").model_copy(update=scope)
    assert orchestrator._general_answer_manual_plan(
        plan, route, f"Summarize {ARTICLE_URL}",
    ) is None
