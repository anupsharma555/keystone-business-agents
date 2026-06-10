"""Opportunity Search Planner SDK agent."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from keystone_agents.opportunity_scout.search_plan import (
    infer_opportunity_search_plan,
    merge_opportunity_search_plan,
)
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.opportunity_search_plan import OpportunitySearchPlan
from keystone_agents.sdk import Agent, build_sdk_agent, compose_instructions


@dataclass(frozen=True)
class OpportunitySearchPlannerInput:
    request_text: str
    desired_count: int = 5
    fallback_plan: OpportunitySearchPlan | None = None
    planner_context: str = ""

    def to_prompt(self) -> str:
        fallback = self.fallback_plan or infer_opportunity_search_plan(
            self.request_text,
            desired_count=self.desired_count,
        )
        planner_context = (
            f"Orchestrator and WorkItem context for this planning pass:\n{self.planner_context}\n\n"
            if self.planner_context
            else ""
        )
        return (
            "Plan Opportunity Scout retrieval for this operator request.\n\n"
            f"Desired count: {self.desired_count}\n"
            f"Operator request: {self.request_text}\n\n"
            "Local fallback plan for reference:\n"
            f"{fallback.model_dump_json(indent=2)}\n\n"
            f"{planner_context}"
            "Return only a valid OpportunitySearchPlan."
        )


def build_opportunity_search_planner_agent(model: str | None = None) -> Agent:
    return build_sdk_agent(
        name="opportunity_search_planner",
        instructions=compose_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "opportunity_search_planner.md",
        ),
        output_type=OpportunitySearchPlan,
        tools=[],
        model=model,
        handoff_description=(
            "Use to turn a natural-language opportunity request into a safe "
            "structured retrieval plan."
        ),
    )


def resolve_opportunity_search_plan(
    request_text: str | None,
    *,
    desired_count: int = 5,
    live: bool = False,
    run_config: Any | None = None,
    model: str | None = None,
    planner_context: str = "",
    cost_callback: Callable[[Any], None] | None = None,
) -> OpportunitySearchPlan:
    """Return an LLM search plan when requested, otherwise the local fallback."""

    fallback = infer_opportunity_search_plan(request_text, desired_count=desired_count)
    if not live and run_config is None:
        return fallback
    try:
        result = run_typed_sdk_agent(
            agent=build_opportunity_search_planner_agent(model=model),
            typed_input=OpportunitySearchPlannerInput(
                request_text=str(request_text or ""),
                desired_count=desired_count,
                fallback_plan=fallback,
                planner_context=planner_context,
            ),
            output_type=OpportunitySearchPlan,
            run_config=run_config,
            live=live,
            workflow_name="Keystone opportunity search planning",
            tracing_disabled=True,
        )
        if cost_callback is not None:
            cost_callback(result)
    except Exception as exc:
        return fallback.model_copy(
            update={
                "planner_warnings": [
                    *fallback.planner_warnings,
                    f"Live search planner unavailable; used local fallback: {exc}",
                ]
            }
        )
    return merge_opportunity_search_plan(
        fallback,
        result.output.model_copy(update={"source": "llm"}),
    )
