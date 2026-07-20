"""Tool-free response profile for complete explicitly routed asks."""

from __future__ import annotations

import inspect
from typing import Any

from keystone_agents.agent_registry import AGENT_REGISTRY
from keystone_agents.schemas.execution_request import DirectAgentResponse
from keystone_agents.sdk import build_sdk_agent, load_prompt

_DIRECT_RESPONSE_ROUTES = frozenset(
    {
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "gmail_triage",
    }
)


def build_direct_supplied_response_agent(
    route: str,
    *,
    request_text: str,
) -> Any:
    """Adapt the named specialist to a one-turn, zero-tool response contract."""

    if route not in _DIRECT_RESPONSE_ROUTES:
        raise ValueError(f"Unsupported direct supplied-response route: {route}")
    spec = AGENT_REGISTRY[route]
    builder = spec.resolve_builder()
    parameters = inspect.signature(builder).parameters
    builder_kwargs: dict[str, Any] = {}
    if "request_text" in parameters:
        builder_kwargs["request_text"] = request_text
    if "attach_tools" in parameters:
        builder_kwargs["attach_tools"] = False
    if "include_tools" in parameters:
        builder_kwargs["include_tools"] = False
    if "compact_instructions" in parameters:
        builder_kwargs["compact_instructions"] = True
    base_agent = builder(**builder_kwargs)
    base_instructions = getattr(base_agent, "instructions", "")
    if not isinstance(base_instructions, str):
        raise TypeError(f"{route} direct-response instructions must be a string")
    instructions = "\n\n".join(
        [
            base_instructions.rstrip(),
            "<!-- direct_supplied_response.md -->\n"
            + load_prompt("direct_supplied_response.md").strip(),
        ]
    )
    return build_sdk_agent(
        name=str(getattr(base_agent, "name", "") or route),
        instructions=instructions,
        output_type=DirectAgentResponse,
        tools=[],
        guardrails={
            "input": list(getattr(base_agent, "input_guardrails", []) or []),
            "output": [],
        },
        handoffs=[],
        model=str(getattr(base_agent, "model", "") or "") or None,
        model_settings=getattr(base_agent, "model_settings", None),
        handoff_description=(
            "Answer one complete operator-supplied request without tools or handoffs."
        ),
        enforce_tool_policy=False,
    )


__all__ = ["build_direct_supplied_response_agent"]
