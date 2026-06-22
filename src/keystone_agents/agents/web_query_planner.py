"""Shared SDK-backed web query planner."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.web_query_plan import WebQueryPlan
from keystone_agents.sdk import Agent, build_sdk_agent, compose_instructions


@dataclass(frozen=True)
class WebQueryPlannerInput:
    subject: str
    request_text: str
    fallback_queries: tuple[str, ...]
    max_queries: int = 12
    planner_context: str = ""

    def to_prompt(self) -> str:
        fallback = build_fallback_web_query_plan(
            subject=self.subject,
            request_text=self.request_text,
            fallback_queries=self.fallback_queries,
            max_queries=self.max_queries,
        )
        context = (
            f"Additional orchestrator/context notes:\n{self.planner_context}\n\n"
            if self.planner_context
            else ""
        )
        return (
            "Plan bounded web search queries for this Keystone retrieval request.\n\n"
            f"Subject: {self.subject or '(unknown)'}\n"
            f"Request: {self.request_text or '(not provided)'}\n"
            f"Maximum queries: {self.max_queries}\n\n"
            "Fallback query plan for reference:\n"
            f"{fallback.model_dump_json(indent=2)}\n\n"
            f"{context}"
            "Return only a valid WebQueryPlan."
        )


def build_fallback_web_query_plan(
    *,
    subject: str,
    request_text: str = "",
    fallback_queries: Sequence[str] = (),
    max_queries: int = 12,
) -> WebQueryPlan:
    """Return the deterministic query plan used when live planning is off."""

    queries = [
        query.strip()
        for query in fallback_queries
        if str(query or "").strip()
    ][: max(1, min(12, max_queries))]
    if not queries and subject.strip():
        queries = [
            f"{subject.strip()} official website",
            f"{subject.strip()} recent news",
            f"{subject.strip()} independent coverage",
        ][: max(1, min(12, max_queries))]
    return WebQueryPlan(
        source="heuristic",
        subject=subject,
        request_text=request_text,
        queries=queries,
        rationale="Local fallback preserved the existing deterministic query list.",
    )


def build_web_query_planner_agent(model: str | None = None) -> Agent:
    """Build the bounded query planner agent."""

    return build_sdk_agent(
        name="web_query_planner",
        instructions=compose_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "web_query_planner.md",
        ),
        output_type=WebQueryPlan,
        tools=[],
        model=model,
        handoff_description=(
            "Use to expand a natural-language web research request into bounded "
            "provider-neutral search queries."
        ),
    )


def resolve_web_query_plan(
    *,
    subject: str,
    request_text: str = "",
    fallback_queries: Sequence[str] = (),
    max_queries: int = 12,
    live: bool = False,
    run_config: Any | None = None,
    model: str | None = None,
    planner_context: str = "",
    cost_callback: Callable[[Any], None] | None = None,
) -> WebQueryPlan:
    """Return an LLM-expanded query plan when requested, otherwise fallback queries."""

    bounded_max = max(1, min(12, max_queries))
    fallback = build_fallback_web_query_plan(
        subject=subject,
        request_text=request_text,
        fallback_queries=fallback_queries,
        max_queries=bounded_max,
    )
    if not live and run_config is None:
        return fallback
    try:
        result = run_typed_sdk_agent(
            agent=build_web_query_planner_agent(model=model),
            typed_input=WebQueryPlannerInput(
                subject=subject,
                request_text=request_text,
                fallback_queries=tuple(fallback.queries),
                max_queries=bounded_max,
                planner_context=planner_context,
            ),
            output_type=WebQueryPlan,
            run_config=run_config,
            live=live,
            workflow_name="Keystone web query planning",
            tracing_disabled=True,
        )
        if cost_callback is not None:
            cost_callback(result)
    except Exception as exc:
        return fallback.model_copy(
            update={
                "planner_warnings": [
                    *fallback.planner_warnings,
                    f"Live query planner unavailable; used local fallback: {exc}",
                ]
            }
        )
    merged = result.output.model_copy(update={"source": "llm"})
    if not merged.queries:
        return fallback.model_copy(
            update={
                "planner_warnings": [
                    *fallback.planner_warnings,
                    "Live query planner returned no queries; used local fallback.",
                ]
            }
        )
    return merged.model_copy(update={"queries": merged.queries[:bounded_max]})
