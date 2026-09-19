from __future__ import annotations

import pytest
from pydantic import ValidationError

from keystone_agents import file_search
from keystone_agents.agent_registry import get_agent_spec
from keystone_agents.agent_tool_policy import tool_policy_for_agent
from keystone_agents.agents.rag_retrieval_specialist import (
    RAGFileSearchUnavailableError,
    build_rag_retrieval_specialist_agent,
    rag_retrieval_fixture,
    run_rag_retrieval_specialist_sdk,
)
from keystone_agents.langgraph_workflow import (
    _route_to_specialist_node,
    run_work_item_langgraph,
)
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.models import RAGRetrievalSDKInput, TypedAgentRunResult
from keystone_agents.schemas.rag_retrieval import (
    RAGGroundedClaim,
    RAGRetrievalResult,
    RAGSourceMatch,
)
from keystone_agents.schemas.work_item import WorkflowRunRequest, WorkItemRoute
from keystone_agents.work_items import (
    build_context_pack_for_route,
    create_or_load_work_item,
)


class FakeFileSearchTool:
    name = "file_search"

    def __init__(
        self,
        *,
        vector_store_ids: list[str],
        max_num_results: int | None = None,
        include_search_results: bool = False,
        **_: object,
    ) -> None:
        self.vector_store_ids = vector_store_ids
        self.max_num_results = max_num_results
        self.include_search_results = include_search_results


def _configure_file_search(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(file_search, "FileSearchTool", FakeFileSearchTool)
    monkeypatch.setenv(
        "KEYSTONE_RAG_RETRIEVAL_SPECIALIST_FILE_SEARCH_VECTOR_STORE_IDS",
        "vs_test_corpus",
    )
    monkeypatch.setenv(
        "KEYSTONE_RAG_RETRIEVAL_SPECIALIST_FILE_SEARCH_MAX_NUM_RESULTS",
        "6",
    )
    monkeypatch.setenv(
        "KEYSTONE_RAG_RETRIEVAL_SPECIALIST_FILE_SEARCH_INCLUDE_RESULTS",
        "true",
    )


def test_builder_attaches_only_configured_file_search(monkeypatch: pytest.MonkeyPatch) -> None:
    _configure_file_search(monkeypatch)

    agent = build_rag_retrieval_specialist_agent(
        request_text="Find the nearest articles about clinician trust in behavioral-health AI."
    )

    assert agent.name == "rag_retrieval_specialist"
    assert [tool.name for tool in agent.tools] == ["file_search"]
    tool = agent.tools[0]
    assert tool.vector_store_ids == ["vs_test_corpus"]
    assert tool.max_num_results == 6
    assert tool.include_search_results is True
    assert agent.output_type is RAGRetrievalResult


def test_agent_policy_is_vector_store_only() -> None:
    policy = tool_policy_for_agent("rag_retrieval_specialist")

    assert policy is not None
    assert policy.allowed_tool_names == frozenset({"file_search"})


def test_fixture_does_not_claim_retrieval() -> None:
    result = rag_retrieval_fixture("What predicts clinician trust in clinical AI?")

    assert result.match_status == "fixture_not_queried"
    assert result.file_search_performed is False
    assert result.matches == []
    assert result.external_write_performed is False
    assert result.send_enabled is False


def test_live_wrapper_requires_file_search_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_file_search(monkeypatch)
    captured: dict[str, object] = {}
    output = RAGRetrievalResult(
        query="clinician trust in behavioral-health AI",
        retrieval_mode="semantic_search",
        match_status="matched",
        answer="The nearest retained article discusses clinician trust.",
        matches=[
            RAGSourceMatch(
                rank=1,
                source_id="file-a",
                title="Clinician trust article",
            )
        ],
        claims=[
            RAGGroundedClaim(
                text="The article discusses clinician trust.",
                source_ids=["file-a"],
            )
        ],
        file_search_performed=True,
    )

    def fake_run_typed_sdk_agent(**kwargs: object) -> TypedAgentRunResult[RAGRetrievalResult]:
        captured.update(kwargs)
        return TypedAgentRunResult(
            agent_name="rag_retrieval_specialist",
            output=output,
            raw_result=None,
            live=True,
        )

    monkeypatch.setattr(
        "keystone_agents.agents.rag_retrieval_specialist.run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    result = run_rag_retrieval_specialist_sdk(
        RAGRetrievalSDKInput(query=output.query),
        live=True,
    )

    execution_contract = captured["tool_execution_contract"]
    budget_contract = captured["tool_call_budget_contract"]
    assert result.output == output
    assert execution_contract.required_groups[0].any_of_tool_names == ("file_search",)
    assert budget_contract.max_total_calls == 2
    assert budget_contract.limits[0].tool_name == "file_search"
    assert budget_contract.limits[0].max_calls == 2


def test_live_wrapper_fails_before_model_without_vector_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        file_search.GLOBAL_VECTOR_STORE_IDS_ENV,
        "KEYSTONE_RAG_RETRIEVAL_SPECIALIST_FILE_SEARCH_VECTOR_STORE_IDS",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(file_search.CONFIG_PATH_ENV, "/__missing_rag_config__.json")

    with pytest.raises(RAGFileSearchUnavailableError):
        run_rag_retrieval_specialist_sdk(
            RAGRetrievalSDKInput(query="semantic query"),
            live=True,
        )


def test_grounded_claims_must_reference_retained_matches() -> None:
    with pytest.raises(ValidationError, match="absent from matches"):
        RAGRetrievalResult(
            query="semantic question",
            retrieval_mode="semantic_search",
            match_status="matched",
            answer="Grounded answer",
            matches=[RAGSourceMatch(rank=1, source_id="file-a", title="Article A")],
            claims=[RAGGroundedClaim(text="Claim", source_ids=["file-b"])],
            file_search_performed=True,
        )


def test_not_found_cannot_retain_off_topic_matches_or_claims() -> None:
    with pytest.raises(ValidationError, match="cannot retain off-topic"):
        RAGRetrievalResult(
            query="unsupported corpus topic",
            retrieval_mode="semantic_search",
            match_status="not_found",
            answer="The corpus does not support this request.",
            matches=[RAGSourceMatch(rank=1, source_id="irrelevant", title="Off topic")],
            claims=[RAGGroundedClaim(text="No direct evidence", source_ids=["irrelevant"])],
            file_search_performed=True,
        )

    result = RAGRetrievalResult(
        query="unsupported corpus topic",
        retrieval_mode="semantic_search",
        match_status="not_found",
        answer="The corpus does not support this request.",
        limitations=["Retrieved candidates were off topic."],
        file_search_performed=True,
    )

    assert result.matches == []
    assert result.claims == []


def test_explicit_named_route_does_not_capture_basic_queries() -> None:
    named = infer_manual_request_plan(
        "Find the nearest articles about digital phenotyping relapse prediction.",
        requested_agent="rag retrieval specialist",
    )
    ordinary = infer_manual_request_plan("What is a vector database?")

    assert named.target_agent == "rag_retrieval_specialist"
    assert named.intent == "rag_retrieval"
    assert named.expected_artifact_type == "rag_retrieval_result"
    assert ordinary.target_agent != "rag_retrieval_specialist"


def test_work_item_context_and_graph_route_are_first_class() -> None:
    work_item = create_or_load_work_item(
        store=None,
        request_text="Find papers related to passive sensing and mood relapse.",
        route=WorkItemRoute.RAG_RETRIEVAL_SPECIALIST,
    )
    pack = build_context_pack_for_route(
        work_item,
        WorkItemRoute.RAG_RETRIEVAL_SPECIALIST,
    )

    assert work_item.kind.value == "rag_retrieval"
    assert pack.route == WorkItemRoute.RAG_RETRIEVAL_SPECIALIST
    assert pack.query == work_item.request_text
    assert _route_to_specialist_node(
        {"route": WorkItemRoute.RAG_RETRIEVAL_SPECIALIST.value}
    ) == "run_rag_retrieval"


def test_dependency_free_graph_executes_explicit_rag_node() -> None:
    outcome = run_work_item_langgraph(
        WorkflowRunRequest(
            request_text="Find nearest corpus matches about passive sensing and mood relapse.",
            requested_route=WorkItemRoute.RAG_RETRIEVAL_SPECIALIST,
            live_sdk=False,
            save=False,
        ),
        require_langgraph=False,
    )

    assert outcome.result.route == WorkItemRoute.RAG_RETRIEVAL_SPECIALIST
    assert "run_rag_retrieval" in outcome.node_path
    assert outcome.result.artifact_refs[0].artifact_type == "rag_retrieval_result"
    assert outcome.result.work_item.last_agent == "rag_retrieval_specialist"


def test_registry_declares_explicit_only_rag_contract() -> None:
    spec = get_agent_spec("rag_retrieval_specialist")

    assert spec.tools == ()
    assert spec.optional_tools == ("file_search",)
    assert spec.output_schema.endswith("RAGRetrievalResult")
    assert "RAGRetrievalContextPack" in spec.input_contract_schema
    assert any("explicitly" in note.lower() for note in spec.safety_notes)


def test_manager_exposes_rag_only_with_explicit_route_opt_in() -> None:
    from keystone_agents.specialist_agent_tools import build_specialist_agent_tools

    default = build_specialist_agent_tools(manager_agent_name="chief_of_staff")
    explicit = build_specialist_agent_tools(
        manager_agent_name="chief_of_staff",
        include_routes={"rag_retrieval_specialist"},
    )

    name = "rag_retrieval_specialist_as_specialist_tool"
    assert name not in {tool.name for tool in default}
    assert [tool.name for tool in explicit] == [name]
    assert explicit[0].specialist_write_authorized is False
