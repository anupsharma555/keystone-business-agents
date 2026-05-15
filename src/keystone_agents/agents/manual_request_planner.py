"""Manual request planner SDK agent."""

from __future__ import annotations

import contextlib
import io
import os
from dataclasses import dataclass
from typing import Any

from keystone_agents.manual_request import (
    infer_manual_request_plan,
    merge_manual_request_plan,
    normalize_manual_agent,
)
from keystone_agents.model_provider import ModelConfig, get_runtime_agent_model_config
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.sdk import Agent, build_sdk_agent, compose_instructions


@dataclass(frozen=True)
class ManualRequestPlannerInput:
    request_text: str
    requested_agent: str | None = None
    fallback_plan: ManualRequestPlan | None = None

    def to_prompt(self) -> str:
        fallback = self.fallback_plan or infer_manual_request_plan(
            self.request_text,
            requested_agent=self.requested_agent,
        )
        requested = self.requested_agent or "not specified"
        return (
            "Plan this manual Keystone agent request before execution.\n\n"
            f"Requested agent mention: {requested}\n"
            f"Operator request: {self.request_text}\n\n"
            "Local fallback plan for reference:\n"
            f"{fallback.model_dump_json(indent=2)}\n\n"
            "Return only a valid ManualRequestPlan."
        )


def build_manual_request_planner_agent(model: str | None = None) -> Agent:
    return build_sdk_agent(
        name="manual_request_planner",
        instructions=compose_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "manual_request_planner.md",
        ),
        output_type=ManualRequestPlan,
        tools=[],
        model=model,
        handoff_description=(
            "Use to turn a manual Slack or CLI request into a safe structured agent-execution plan."
        ),
    )


def resolve_manual_request_plan(
    request_text: str | None,
    *,
    requested_agent: str | None = None,
    live: bool = False,
    run_config: Any | None = None,
    model: str | None = None,
) -> ManualRequestPlan:
    """Return an LLM manual-request plan when requested, otherwise local fallback."""

    fallback = infer_manual_request_plan(request_text, requested_agent=requested_agent)
    if not live and run_config is None:
        return fallback
    errors: list[str] = []
    for config in _planner_model_configs(requested_agent=requested_agent, model=model):
        stdout_capture = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout_capture):
                result = run_typed_sdk_agent(
                    agent=build_manual_request_planner_agent(model=config.model),
                    typed_input=ManualRequestPlannerInput(
                        request_text=str(request_text or ""),
                        requested_agent=requested_agent,
                        fallback_plan=fallback,
                    ),
                    output_type=ManualRequestPlan,
                    run_config=run_config,
                    live=live,
                    config=config,
                    workflow_name="Keystone manual request planning",
                    tracing_disabled=True,
                )
        except Exception as exc:
            captured = " ".join(stdout_capture.getvalue().split())
            suffix = f" ({captured})" if captured else ""
            errors.append(f"{config.provider}/{config.model}: {exc}{suffix}")
            continue
        return merge_manual_request_plan(
            fallback,
            result.output.model_copy(update={"source": "llm"}),
        )
    return fallback.model_copy(
        update={
            "planner_warnings": [
                *fallback.planner_warnings,
                "Live manual planner unavailable; used local fallback: " + " | ".join(errors),
            ]
        }
    )


def _planner_model_configs(
    *,
    requested_agent: str | None,
    model: str | None = None,
) -> list[ModelConfig]:
    """Return provider-aware planner configs with bounded fallback."""

    policy = os.getenv("KEYSTONE_MANUAL_PLANNER_PROVIDER_POLICY", "target_with_openai_fallback")
    policy = policy.strip().lower() or "target_with_openai_fallback"
    target_agent = normalize_manual_agent(requested_agent) or "orchestrator"
    target_config = get_runtime_agent_model_config(target_agent, model_override=model)
    openai_config = get_runtime_agent_model_config(
        "orchestrator",
        model_override=model,
        provider_override="openai",
    )
    if policy == "openai":
        return [openai_config]
    if policy == "target":
        return [target_config]
    configs = [target_config]
    if (
        target_config.provider,
        target_config.model,
        target_config.base_url,
    ) != (
        openai_config.provider,
        openai_config.model,
        openai_config.base_url,
    ):
        configs.append(openai_config)
    return configs
