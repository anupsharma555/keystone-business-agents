"""Read-only hosted vector-store retrieval specialist."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from keystone_agents.agent_tool_policy import ToolTier
from keystone_agents.capabilities.tool_scope import (
    ToolScopeMode,
    attach_tool_scope_receipt,
    scope_tools_for_request,
    tool_scope_receipt_for_agent,
    tool_scope_trace_metadata_for_agent,
)
from keystone_agents.file_search import (
    append_configured_file_search_tools,
    file_search_availability_for_agent,
)
from keystone_agents.guardrails import keystone_guardrails
from keystone_agents.models import RAGRetrievalSDKInput, TypedAgentRunResult
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.runtime.tool_call_budget import (
    ToolCallBudgetContract,
    ToolCallLimit,
)
from keystone_agents.runtime.tool_execution import (
    ToolEvidenceGroup,
    ToolExecutionContract,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.rag_retrieval import RAGRetrievalResult
from keystone_agents.sdk import Agent, build_sdk_agent, compose_instructions
from keystone_agents.skill_sets import select_agent_skill_names, skill_request_text

AGENT_NAME = "rag_retrieval_specialist"


class RAGFileSearchUnavailableError(RuntimeError):
    """Raised before a live run when the specialist has no hosted corpus tool."""


def build_rag_retrieval_specialist_agent(
    model: str | None = None,
    *,
    request_text: str = "",
    context_flags: Mapping[str, bool] | None = None,
    include_all_skills: bool = False,
    tool_tier: ToolTier | str | int | None = ToolTier.DEEP_RETRIEVAL,
    attach_tools: bool = True,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
) -> Agent:
    """Build the vector-store-only RAG Retrieval Specialist."""

    skill_files = select_agent_skill_names(
        AGENT_NAME,
        request_text=request_text,
        context_flags=context_flags,
        include_all=include_all_skills,
    )
    instructions = compose_instructions(
        "keystone_profile.md",
        "safety_policy.md",
        "tools.md",
        "rag_retrieval_specialist.md",
        skill_files=skill_files,
    )
    candidate_tools = (
        append_configured_file_search_tools(AGENT_NAME, []) if attach_tools else []
    )
    attachment = scope_tools_for_request(
        AGENT_NAME,
        candidate_tools,
        manual_request_plan=manual_request_plan,
        tool_tier=tool_tier,
        mode=ToolScopeMode.FULL,
        required_tool_names=("file_search",),
    )
    agent = build_sdk_agent(
        name=AGENT_NAME,
        instructions=instructions,
        output_type=RAGRetrievalResult,
        tools=list(attachment.tools),
        guardrails=keystone_guardrails(),
        model=model,
        policy_agent_name=AGENT_NAME,
        handoff_description=(
            "Use only for explicitly requested natural-language or semantic retrieval "
            "from configured vector stores, including nearest-match and single-article lookup."
        ),
    )
    return attach_tool_scope_receipt(agent, attachment.scope)


def rag_retrieval_fixture(query: str) -> RAGRetrievalResult:
    """Return a deterministic no-provider result for fixture-safe development."""

    return RAGRetrievalResult(
        query=query or "Unspecified vector-store query",
        retrieval_mode="semantic_search",
        match_status="fixture_not_queried",
        answer="",
        limitations=[
            "Fixture mode did not query the configured vector store or retrieve article evidence."
        ],
        suggested_follow_up_queries=[query] if query.strip() else [],
        confidence=0.0,
        file_search_performed=False,
    )


def run_rag_retrieval_specialist_sdk(
    typed_input: RAGRetrievalSDKInput | str,
    *,
    run_config: object | None = None,
    live: bool = False,
    model: str | None = None,
    session: Any | None = None,
    max_turns: int = 4,
    manual_request_plan: ManualRequestPlan | Mapping[str, Any] | None = None,
) -> TypedAgentRunResult[RAGRetrievalResult]:
    """Run bounded hosted semantic retrieval through the typed SDK harness."""

    request_text = skill_request_text(typed_input)
    agent = build_rag_retrieval_specialist_agent(
        model=model,
        request_text=request_text,
        manual_request_plan=manual_request_plan,
    )
    if live and "file_search" not in {
        str(getattr(tool, "name", "") or "") for tool in agent.tools or []
    }:
        status = file_search_availability_for_agent(AGENT_NAME)
        raise RAGFileSearchUnavailableError(
            "RAG Retrieval Specialist requires configured hosted file search before a live run "
            f"(status={status.get('status', 'unknown')})."
        )
    result = run_typed_sdk_agent(
        agent=agent,
        typed_input=typed_input,
        output_type=RAGRetrievalResult,
        run_config=run_config,
        live=live,
        session=session,
        max_turns=max_turns,
        tool_call_budget_contract=ToolCallBudgetContract(
            limits=(ToolCallLimit("file_search", 2),),
            max_total_calls=2,
            stage="rag_vector_store_retrieval",
        ),
        tool_execution_contract=ToolExecutionContract.required(
            ToolEvidenceGroup("vector_store_evidence", ("file_search",)),
            stage="rag_vector_store_retrieval",
        ),
        trace_metadata=tool_scope_trace_metadata_for_agent(agent),
    )
    if isinstance(result.request_cache, dict):
        result.request_cache["request_tool_scope"] = tool_scope_receipt_for_agent(agent)
    return result
