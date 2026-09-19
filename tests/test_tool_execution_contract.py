from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from keystone_agents.entrypoints import cli_impl
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.model_provider import ModelConfig
from keystone_agents.run import (
    _tool_execution_summary_from_journals,
    run_typed_sdk_agent,
    sdk_run_failure_metadata,
)
from keystone_agents.runtime.tool_execution import (
    ToolEvidenceGroup,
    ToolExecutionContract,
    ToolExecutionContractError,
    ToolExecutionMode,
    build_tool_execution_summary,
    evaluate_tool_execution_contract,
    external_write_state_from_evidence,
)
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.sdk import (
    agent_with_retry_compatible_tool_choice,
    build_model_settings,
)


def _raw_result(*items: Any) -> Any:
    return SimpleNamespace(new_items=list(items), usage={"requests": 1})


def _tool_call(call_id: str, name: str) -> Any:
    return SimpleNamespace(type="tool_call_item", call_id=call_id, tool_name=name)


def _tool_output(call_id: str, output: Any) -> Any:
    return SimpleNamespace(type="tool_call_output_item", call_id=call_id, output=output)


def _hosted_file_search_call(call_id: str, *, status: str) -> Any:
    return SimpleNamespace(
        type="tool_call_item",
        raw_item=SimpleNamespace(
            type="file_search_call",
            id=call_id,
            status=status,
            results=[],
        ),
    )


def _required_search_contract() -> ToolExecutionContract:
    return ToolExecutionContract.required(
        ToolEvidenceGroup("current_public_research", ("search_web",)),
        stage="business_research_current_evidence",
    )


def test_tool_execution_summary_separates_requests_receipts_and_execution_modes() -> None:
    summary = build_tool_execution_summary(
        mode="workflow_preacquired_provider_context",
        selected_tool_names=["read_google_calendar_window"],
        preacquired_context_tool_names=["read_google_calendar_window"],
        preacquired_context_count=1,
        preacquired_context_source="preacquired_provider_context",
        provider_request_attempt_count=2,
        provider_request_success_count=1,
        provider_receipt_count=1,
        context_receipt_count=1,
        context_receipt_source="preacquired_provider_context",
    )

    assert summary["model_tool_call_count"] == 0
    assert summary["workflow_tool_call_count"] == 0
    assert summary["preacquired_context_count"] == 1
    assert summary["preacquired_context_tool_names"] == [
        "read_google_calendar_window"
    ]
    assert summary["tool_origins"]["workflow_called"] == []
    assert summary["tool_origins"]["preacquired_context"] == [
        "read_google_calendar_window"
    ]
    assert summary["provider_request_attempt_count"] == 2
    assert summary["provider_request_attempt_count_available"] is True
    assert summary["provider_request_success_count"] == 1
    assert summary["provider_receipt_count"] == 1
    assert summary["provider_receipt_count_available"] is True
    assert summary["context_receipt_source"] == "preacquired_provider_context"


def test_tool_execution_summary_marks_unrecorded_provider_counts_unavailable() -> None:
    summary = build_tool_execution_summary(
        mode="model_tools_attached_no_call",
        selected_tool_names=["read_google_calendar_window"],
    )

    assert summary["provider_request_attempt_count"] == 0
    assert summary["provider_request_attempt_count_available"] is False
    assert summary["provider_request_success_count_available"] is False
    assert summary["provider_receipt_count_available"] is False


def test_shared_runner_labels_preacquired_receipt_as_context_not_workflow_call() -> None:
    receipt = {
        "status": "success",
        "provider": "gmail",
        "tool_name": "query_gmail_messages",
        "operation": "read",
    }

    summary = _tool_execution_summary_from_journals(
        selected_tool_names=["query_gmail_messages"],
        invocations=[],
        tool_receipts=[],
        preacquired_tool_receipts=[receipt],
        postcondition=None,
        failed=False,
    )

    assert summary["mode"] == "preacquired_provider_context"
    assert summary["workflow_tool_call_count"] == 0
    assert summary["workflow_called_tool_names"] == []
    assert summary["preacquired_context_tool_names"] == ["query_gmail_messages"]
    assert summary["provider_request_attempt_count_available"] is False
    assert summary["provider_receipt_count"] == 1
    assert summary["provider_receipt_count_available"] is True


def test_shared_runner_aggregates_provider_request_counts_from_receipts() -> None:
    summary = _tool_execution_summary_from_journals(
        selected_tool_names=["create_google_calendar_event"],
        invocations=[
            {
                "status": "started",
                "tool_name": "create_google_calendar_event",
            }
        ],
        tool_receipts=[
            {
                "tool_name": "create_google_calendar_event",
                "provider_request_attempt_count": 3,
                "provider_request_success_count": 3,
            }
        ],
        preacquired_tool_receipts=[],
        postcondition=None,
        failed=False,
    )

    assert summary["provider_request_attempt_count"] == 3
    assert summary["provider_request_attempt_count_available"] is True
    assert summary["provider_request_success_count"] == 3
    assert summary["provider_request_success_count_available"] is True
    assert summary["provider_receipt_count"] == 1


def test_external_write_state_requires_conclusive_receipt_or_complete_no_write_evidence() -> None:
    performed = external_write_state_from_evidence(
        receipts=[
            {
                "tool_name": "create_google_calendar_event",
                "operation": "create",
                "status": "success",
                "verification": {"passed": True, "create_read_back": True},
            }
        ],
        tool_names=["create_google_calendar_event"],
        evidence_complete=True,
    )
    unknown = external_write_state_from_evidence(
        tool_names=["create_google_calendar_event"],
        evidence_complete=True,
    )
    not_performed = external_write_state_from_evidence(
        receipts=[
            {
                "tool_name": "create_google_calendar_event",
                "operation": "create",
                "status": "blocked",
                "provider_write": False,
            }
        ],
        tool_names=["create_google_calendar_event"],
        evidence_complete=True,
    )

    assert performed == "performed"
    assert unknown == "unknown"
    assert not_performed == "not_performed"


def test_required_tool_attachment_without_a_call_is_not_evidence() -> None:
    outcome = evaluate_tool_execution_contract(_raw_result(), _required_search_contract())

    assert outcome.satisfied is False
    assert outcome.missing_groups == ("current_public_research",)
    assert outcome.attempted_tool_names == ()
    assert outcome.completed_tool_names == ()


def test_required_tool_needs_a_matching_successful_output() -> None:
    attempted_only = evaluate_tool_execution_contract(
        _raw_result(_tool_call("call-1", "search_web")),
        _required_search_contract(),
    )
    failed_output = evaluate_tool_execution_contract(
        _raw_result(
            _tool_call("call-1", "search_web"),
            _tool_output("call-1", {"status": "failed"}),
        ),
        _required_search_contract(),
    )
    completed = evaluate_tool_execution_contract(
        _raw_result(
            _tool_call("call-1", "search_web"),
            _tool_output("call-1", {"status": "success", "results": []}),
        ),
        _required_search_contract(),
    )

    assert attempted_only.satisfied is False
    assert failed_output.satisfied is False
    assert completed.satisfied is True
    assert completed.completed_tool_names == ("search_web",)


def test_required_gmail_query_receipt_status_read_is_successful_failure_evidence() -> None:
    contract = ToolExecutionContract.required(
        ToolEvidenceGroup(
            "gmail_query",
            ("query_gmail_message_summaries",),
        ),
        stage="gmail_agent_owned_selection",
    )

    outcome = evaluate_tool_execution_contract(
        None,
        contract,
        tool_receipts=[
            {
                "schema_name": "keystone.gmail.message_query.v1",
                "tool_name": "query_gmail_message_summaries",
                "status": "read",
                "item_count": 1,
                "send_enabled": False,
            }
        ],
    )

    assert outcome.satisfied is True
    assert outcome.missing_groups == ()
    assert outcome.receipt_tool_names == ("query_gmail_message_summaries",)


def test_completed_hosted_file_search_call_is_self_contained_tool_evidence() -> None:
    contract = ToolExecutionContract.required(
        ToolEvidenceGroup("vector_store_evidence", ("file_search",)),
        stage="rag_vector_store_retrieval",
    )

    completed = evaluate_tool_execution_contract(
        _raw_result(_hosted_file_search_call("fs-1", status="completed")),
        contract,
    )
    failed = evaluate_tool_execution_contract(
        _raw_result(_hosted_file_search_call("fs-2", status="failed")),
        contract,
    )

    assert completed.satisfied is True
    assert completed.attempted_tool_names == ("file_search",)
    assert completed.completed_tool_names == ("file_search",)
    assert failed.satisfied is False
    assert failed.completed_tool_names == ()


def test_required_tool_contract_preserves_completed_evidence_across_one_correction() -> None:
    contract = ToolExecutionContract.required(
        ToolEvidenceGroup("search", ("search_web",)),
        ToolEvidenceGroup("extract", ("fetch_selected_url",)),
        stage="research_and_extract",
    )
    first = evaluate_tool_execution_contract(
        _raw_result(
            _tool_call("search-1", "search_web"),
            _tool_output("search-1", {"status": "success"}),
        ),
        contract,
    )
    repaired = evaluate_tool_execution_contract(
        _raw_result(
            _tool_call("fetch-1", "fetch_selected_url"),
            _tool_output("fetch-1", {"status": "success"}),
        ),
        contract,
        prior_attempted_tool_names=first.attempted_tool_names,
        prior_completed_tool_names=first.completed_tool_names,
    )

    assert first.missing_groups == ("extract",)
    assert repaired.satisfied is True
    assert repaired.completed_tool_names == ("search_web", "fetch_selected_url")


@pytest.mark.parametrize(
    "output",
    [
        {"status": "partial"},
        {"status": "verification_failed"},
        {"status": "dry-run"},
        {"status": "timeout"},
        {"status": "not_found"},
        {"status": "no_results"},
        {"status": "empty"},
        {"status": "success", "verification": {"passed": False}},
    ],
)
def test_nonverified_tool_outputs_do_not_satisfy_required_evidence(output: Any) -> None:
    outcome = evaluate_tool_execution_contract(
        _raw_result(
            _tool_call("call-1", "search_web"),
            _tool_output("call-1", output),
        ),
        _required_search_contract(),
    )

    assert outcome.satisfied is False
    assert outcome.attempted_tool_names == ("search_web",)
    assert outcome.completed_tool_names == ()


def test_wrong_tool_does_not_satisfy_required_group() -> None:
    outcome = evaluate_tool_execution_contract(
        _raw_result(
            _tool_call("call-1", "retrieve_memory"),
            _tool_output("call-1", {"status": "success"}),
        ),
        _required_search_contract(),
    )

    assert outcome.satisfied is False
    assert outcome.completed_tool_names == ("retrieve_memory",)


def test_tagged_zotero_metadata_read_does_not_require_redundant_local_resolution() -> None:
    request = (
        "Find the newest two Zotero items tagged digital phenotyping and give "
        "their titles, years, publications, and whether each has an abstract."
    )
    plan = infer_manual_request_plan(request, requested_agent="zotero_context_agent")
    contract = cli_impl._context_agent_tool_execution_contract(
        "zotero_context_agent",
        input_text=request,
        selected_tool_names=(
            "zotero_resolve_article_context",
            "zotero_read_api_metadata",
        ),
        manual_plan=plan,
    )

    assert contract is not None
    outcome = evaluate_tool_execution_contract(
        _raw_result(
            _tool_call("zotero-1", "zotero_read_api_metadata"),
            _tool_output(
                "zotero-1",
                {"status": "success", "provider_read": True, "item_count": 2},
            ),
        ),
        contract,
    )

    assert outcome.satisfied is True
    assert outcome.missing_groups == ()


def test_verified_preacquired_receipt_satisfies_required_group() -> None:
    outcome = evaluate_tool_execution_contract(
        _raw_result(),
        _required_search_contract(),
        preacquired_receipts=(
            {"tool_name": "search_web", "status": "success", "verification": {"passed": True}},
        ),
    )

    assert outcome.satisfied is True
    assert outcome.receipt_tool_names == ("search_web",)


def test_optional_and_forbidden_tool_free_stages_remain_valid() -> None:
    optional = evaluate_tool_execution_contract(
        _raw_result(),
        ToolExecutionContract(mode=ToolExecutionMode.OPTIONAL, stage="review"),
    )
    forbidden = evaluate_tool_execution_contract(
        _raw_result(),
        ToolExecutionContract(mode=ToolExecutionMode.FORBIDDEN, stage="supplied_synthesis"),
    )

    assert optional.satisfied is True
    assert forbidden.satisfied is True


def test_forbidden_stage_rejects_an_emitted_tool_call() -> None:
    outcome = evaluate_tool_execution_contract(
        _raw_result(_tool_call("call-1", "search_web")),
        ToolExecutionContract(mode=ToolExecutionMode.FORBIDDEN, stage="supplied_synthesis"),
    )

    assert outcome.satisfied is False
    assert outcome.prohibited_tool_names == ("search_web",)


def test_shared_sdk_runner_does_not_return_success_when_required_tool_is_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAgent:
        name = "business_research_analyst"
        model = "gpt-test"
        tools: list[Any] = []

    def fake_run_typed_sdk_sync(*_args: Any, **_kwargs: Any) -> tuple[Any, ChiefOfStaffResult]:
        return (
            _raw_result(),
            ChiefOfStaffResult(mode="llm", summary="Unsupported current answer."),
        )

    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(
        "keystone_agents.run.enforce_agent_run_budget",
        lambda **_kwargs: {"enforced": False},
    )

    with pytest.raises(ToolExecutionContractError) as exc_info:
        run_typed_sdk_agent(
            agent=FakeAgent(),
            typed_input={"request": "current company research"},
            output_type=ChiefOfStaffResult,
            config=ModelConfig(provider="openai", model="gpt-test", api_key="test-key"),
            tool_execution_contract=_required_search_contract(),
        )

    assert exc_info.value.outcome.missing_groups == ("current_public_research",)
    failure = sdk_run_failure_metadata(exc_info.value)
    assert failure["failure_kind"] == "required_tool_execution_missing"
    assert failure["usage"]["requests"] == 2
    assert failure["request_cache"]["tool_execution_correction"]["attempted"] is True


def test_shared_sdk_runner_gives_one_bounded_required_tool_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAgent:
        name = "business_research_analyst"
        model = "gpt-test"
        tools = [SimpleNamespace(name="search_web")]

    prompts: list[Any] = []
    turn_limits: list[int | None] = []
    raw_results = [
        _raw_result(),
        _raw_result(
            _tool_call("call-1", "search_web"),
            _tool_output("call-1", {"status": "success", "results": []}),
        ),
    ]

    def fake_run_typed_sdk_sync(*args: Any, **kwargs: Any) -> tuple[Any, ChiefOfStaffResult]:
        prompts.append(args[1])
        turn_limits.append(kwargs.get("max_turns"))
        return (
            raw_results.pop(0),
            ChiefOfStaffResult(mode="llm", summary="Grounded current answer."),
        )

    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(
        "keystone_agents.run.enforce_agent_run_budget",
        lambda **_kwargs: {"enforced": False},
    )

    result = run_typed_sdk_agent(
        agent=FakeAgent(),
        typed_input={"request": "current company research"},
        output_type=ChiefOfStaffResult,
        config=ModelConfig(provider="openai", model="gpt-test", api_key="test-key"),
        tool_execution_contract=_required_search_contract(),
        max_turns=4,
        tool_correction_max_turns=2,
        decision_repair_max_turns=1,
    )

    assert len(prompts) == 2
    assert turn_limits == [4, 2]
    assert result.request_cache["semantic_attempt_turn_limits"] == {
        "schema": "keystone.semantic_attempt_turn_limits.v1",
        "initial": 4,
        "tool_correction": 2,
        "decision_repair": 1,
    }
    assert "Bounded tool-execution correction" in str(prompts[1])
    assert result.request_cache["tool_corrections"] == 1
    assert result.request_cache["tool_execution"]["model_called_tool_names"] == [
        "search_web"
    ]
    assert result.usage["requests"] == 2
    assert result.usage["attempt_count"] == 2


def test_tool_correction_keeps_unsuccessful_read_enabled_for_one_safe_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calendar_tool = SimpleNamespace(
        name="read_google_calendar_window",
        is_enabled=True,
    )

    class FakeAgent:
        name = "chief_of_staff"
        model = "gpt-test"
        tools = [calendar_tool]

    contract = ToolExecutionContract.required(
        ToolEvidenceGroup(
            "google_calendar_context",
            ("read_google_calendar_window",),
        ),
        stage="chief_of_staff_provider_context_selection",
    )
    attempts = 0

    def fake_run_typed_sdk_sync(*args: Any, **_kwargs: Any) -> tuple[Any, ChiefOfStaffResult]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return (
                _raw_result(
                    _tool_call("calendar-1", "read_google_calendar_window"),
                    _tool_output("calendar-1", {"status": "dry-run", "events": []}),
                ),
                ChiefOfStaffResult(mode="llm", summary="Calendar preview only."),
            )
        active_tool = list(getattr(args[0], "tools", []) or [])[0]
        assert active_tool.is_enabled is True
        return (
            _raw_result(
                _tool_call("calendar-2", "read_google_calendar_window"),
                _tool_output("calendar-2", {"status": "success", "events": []}),
            ),
            ChiefOfStaffResult(mode="llm", summary="Calendar context verified."),
        )

    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(
        "keystone_agents.run.tool_invocation_journal",
        lambda: [
            {
                "tool_name": "read_google_calendar_window",
                "invocation_index": 1,
                "status": "started",
            },
            {
                "tool_name": "read_google_calendar_window",
                "invocation_index": 1,
                "status": "returned_unsuccessful",
            },
        ],
    )
    monkeypatch.setattr(
        "keystone_agents.run.enforce_agent_run_budget",
        lambda **_kwargs: {"enforced": False},
    )

    result = run_typed_sdk_agent(
        agent=FakeAgent(),
        typed_input={"request": "Read the required calendar context."},
        output_type=ChiefOfStaffResult,
        config=ModelConfig(provider="openai", model="gpt-test", api_key="test-key"),
        tool_execution_contract=contract,
    )

    assert attempts == 2
    assert result.request_cache["tool_corrections"] == 1
    assert result.request_cache["tool_execution_postcondition"]["satisfied"] is True
    assert calendar_tool.is_enabled is True


def test_tool_correction_disables_completed_read_and_reuses_its_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    search_tool = SimpleNamespace(name="search_web", is_enabled=True)
    extract_tool = SimpleNamespace(name="fetch_selected_url", is_enabled=True)

    class FakeAgent:
        name = "business_research_analyst"
        model = "gpt-test"
        tools = [search_tool, extract_tool]

    contract = ToolExecutionContract.required(
        ToolEvidenceGroup("search", ("search_web",)),
        ToolEvidenceGroup("extract", ("fetch_selected_url",)),
        stage="research_and_extract",
    )
    attempt = 0

    def fake_run_typed_sdk_sync(*args: Any, **_kwargs: Any) -> tuple[Any, ChiefOfStaffResult]:
        nonlocal attempt
        attempt += 1
        if attempt == 1:
            return (
                _raw_result(
                    _tool_call("search-1", "search_web"),
                    _tool_output(
                        "search-1",
                        {
                            "status": "success",
                            "results": [
                                {
                                    "title": "Northstar evidence update",
                                    "url": "https://northstar.example/evidence",
                                    "snippet": "A bounded current measurement update.",
                                }
                            ],
                        },
                    ),
                ),
                ChiefOfStaffResult(mode="llm", summary="Search complete."),
            )
        active_tools = {
            tool.name: tool for tool in list(getattr(args[0], "tools", []) or [])
        }
        assert active_tools["search_web"].is_enabled is False
        assert active_tools["fetch_selected_url"].is_enabled is True
        # Retry state is run-local; the caller's reusable tool objects stay enabled.
        assert search_tool.is_enabled is True
        assert extract_tool.is_enabled is True
        assert "Northstar evidence update" in str(args[1])
        return (
            _raw_result(
                _tool_call("fetch-1", "fetch_selected_url"),
                _tool_output("fetch-1", {"status": "success"}),
            ),
            ChiefOfStaffResult(mode="llm", summary="Grounded answer."),
        )

    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(
        "keystone_agents.run.enforce_agent_run_budget",
        lambda **_kwargs: {"enforced": False},
    )

    result = run_typed_sdk_agent(
        agent=FakeAgent(),
        typed_input={"request": "Research and read the selected source."},
        output_type=ChiefOfStaffResult,
        config=ModelConfig(provider="openai", model="gpt-test", api_key="test-key"),
        tool_execution_contract=contract,
    )

    assert attempt == 2
    assert result.request_cache["tool_execution_postcondition"]["satisfied"] is True
    assert result.request_cache["tool_execution_postcondition"][
        "completed_tool_names"
    ] == ["search_web", "fetch_selected_url"]
    assert result.request_cache["tool_execution_correction"][
        "disabled_completed_tool_names"
    ] == ["search_web"]
    assert result.request_cache["tool_execution_correction"]["evidence_replay"][
        "provider_calls_during_repair"
    ] == 0
    assert search_tool.is_enabled is True


def test_tool_correction_fails_closed_when_completed_read_cannot_be_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAgent:
        name = "business_research_analyst"
        model = "gpt-test"
        tools = [
            SimpleNamespace(name="search_web", is_enabled=True),
            SimpleNamespace(name="fetch_selected_url", is_enabled=True),
        ]

    contract = ToolExecutionContract.required(
        ToolEvidenceGroup("search", ("search_web",)),
        ToolEvidenceGroup("extract", ("fetch_selected_url",)),
        stage="research_and_extract",
    )
    calls = 0

    def fake_run_typed_sdk_sync(*args: Any, **_kwargs: Any) -> tuple[Any, ChiefOfStaffResult]:
        nonlocal calls
        calls += 1
        return (
            _raw_result(
                _tool_call("search-1", "search_web"),
                _tool_output("search-1", {"status": "success"}),
            ),
            ChiefOfStaffResult(mode="llm", summary="Insufficient evidence."),
        )

    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run_typed_sdk_sync)

    with pytest.raises(Exception) as exc_info:
        run_typed_sdk_agent(
            agent=FakeAgent(),
            typed_input={"request": "Search, then read the selected source."},
            output_type=ChiefOfStaffResult,
            config=ModelConfig(provider="openai", model="gpt-test", api_key="test-key"),
            tool_execution_contract=contract,
        )

    assert calls == 1
    failure = sdk_run_failure_metadata(exc_info.value)
    assert failure["failure_kind"] == "tool_correction_evidence_replay_unavailable"
    assert failure["request_cache"]["tool_execution_correction"]["attempted"] is True


def test_structured_retry_blocks_failed_mutation_with_unknown_write_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ModelBehaviorError(Exception):
        pass

    create_tool = SimpleNamespace(name="create_google_calendar_event", is_enabled=True)

    class FakeAgent:
        name = "calendar_action_interpreter"
        model = "gpt-test"
        tools = [create_tool]

    calls = 0

    def fake_run_typed_sdk_sync(*_args: Any, **_kwargs: Any) -> tuple[Any, ChiefOfStaffResult]:
        nonlocal calls
        calls += 1
        raise ModelBehaviorError("structured output invalid")

    monkeypatch.setenv("KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES", "1")
    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(
        "keystone_agents.run.tool_invocation_journal",
        lambda: [
            {
                "tool_name": "create_google_calendar_event",
                "invocation_index": 1,
                "status": "started",
            },
            {
                "tool_name": "create_google_calendar_event",
                "invocation_index": 1,
                "status": "failed",
                "error_type": "ProviderTimeout",
            },
        ],
    )

    with pytest.raises(Exception) as exc_info:
        run_typed_sdk_agent(
            agent=FakeAgent(),
            typed_input={"request": "Create the approved test event."},
            output_type=ChiefOfStaffResult,
            live=True,
            config=ModelConfig(provider="openai", model="gpt-test", api_key="test-key"),
        )

    assert calls == 1
    failure = sdk_run_failure_metadata(exc_info.value)
    assert failure["failure_kind"] == "mutation_retry_state_unknown"
    assert create_tool.is_enabled is True


@pytest.mark.parametrize(
    "tool_name",
    ["read_google_calendar_window", "create_google_calendar_event"],
)
def test_structured_retry_disables_completed_tool_without_normalized_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tool_name: str,
) -> None:
    class ModelBehaviorError(Exception):
        pass

    completed_tool = SimpleNamespace(name=tool_name, is_enabled=True)

    class FakeAgent:
        name = "calendar_action_interpreter"
        model = "gpt-test"
        tools = [completed_tool]
        model_settings = build_model_settings(tool_choice=tool_name)

    calls = 0

    def fake_run_typed_sdk_sync(*args: Any, **_kwargs: Any) -> tuple[Any, ChiefOfStaffResult]:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert args[0].model_settings.tool_choice == tool_name
            raise ModelBehaviorError("structured output invalid")
        active_tool = list(getattr(args[0], "tools", []) or [])[0]
        assert active_tool.is_enabled is False
        assert args[0].model_settings.tool_choice is None
        # The shared input tool must not inherit request-local retry state.
        assert completed_tool.is_enabled is True
        return (
            _raw_result(),
            ChiefOfStaffResult(mode="llm", summary="Recovered from existing evidence."),
        )

    monkeypatch.setenv("KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES", "1")
    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(
        "keystone_agents.run.tool_invocation_journal",
        lambda: [
            {"tool_name": tool_name, "invocation_index": 1, "status": "started"},
            {"tool_name": tool_name, "invocation_index": 1, "status": "completed"},
        ],
    )
    monkeypatch.setattr(
        "keystone_agents.run.enforce_agent_run_budget",
        lambda **_kwargs: {"enforced": False},
    )

    original_agent = FakeAgent()
    result = run_typed_sdk_agent(
        agent=original_agent,
        typed_input={"request": "Use the already completed operation result."},
        output_type=ChiefOfStaffResult,
        live=True,
        config=ModelConfig(provider="openai", model="gpt-test", api_key="test-key"),
    )

    assert calls == 2
    assert result.request_cache["structured_output_retries"] == 1
    assert result.request_cache["retry_safety"][0][
        "disabled_completed_tool_names"
    ] == [tool_name]
    assert result.request_cache["retry_tool_choice_adjustments"] == [
        {
            "schema": "keystone.retry_tool_choice_adjustment.v1",
            "prior_choice": tool_name,
            "adjusted_choice": "auto",
            "reason": "forced_tool_disabled_after_completed_operation",
            "disabled_tool_names": [tool_name],
            "enabled_tool_names": [],
            "attempt_index": 2,
        }
    ]
    assert original_agent.model_settings.tool_choice == tool_name
    assert completed_tool.is_enabled is True


def test_retry_clears_sdk_function_sentinel_when_all_tools_are_disabled() -> None:
    disabled_tool = SimpleNamespace(
        name="query_gmail_message_summaries",
        is_enabled=False,
    )
    agent = SimpleNamespace(
        name="gmail_triage",
        tools=[disabled_tool],
        model_settings=build_model_settings(tool_choice="function"),
    )

    retry_agent, adjustment = agent_with_retry_compatible_tool_choice(
        agent,
        disabled_tool_names=["query_gmail_message_summaries"],
    )

    assert retry_agent.model_settings.tool_choice is None
    assert adjustment == {
        "schema": "keystone.retry_tool_choice_adjustment.v1",
        "prior_choice": "function",
        "adjusted_choice": "auto",
        "reason": "forced_tool_disabled_after_completed_operation",
        "disabled_tool_names": ["query_gmail_message_summaries"],
        "enabled_tool_names": [],
    }
    assert agent.model_settings.tool_choice == "function"


def test_structured_retry_replays_model_visible_tool_evidence_without_reread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ModelBehaviorError(Exception):
        pass

    completed_tool = SimpleNamespace(
        name="query_gmail_message_summaries",
        is_enabled=True,
    )

    class FakeAgent:
        name = "gmail_triage"
        model = "gpt-test"
        tools = [completed_tool]
        model_settings = build_model_settings(tool_choice="function")

    calls = 0

    def fake_run_typed_sdk_sync(*args: Any, **_kwargs: Any) -> tuple[Any, ChiefOfStaffResult]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ModelBehaviorError("structured output invalid")
        assert args[0].model_settings.tool_choice is None
        assert args[0].tools[0].is_enabled is False
        assert "Verified model tool evidence" in str(args[1])
        assert "thread-verified" in str(args[1])
        return (
            _raw_result(),
            ChiefOfStaffResult(mode="llm", summary="Recovered from replayed evidence."),
        )

    monkeypatch.setenv("KEYSTONE_SDK_STRUCTURED_OUTPUT_MAX_RETRIES", "1")
    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(
        "keystone_agents.run.tool_invocation_journal",
        lambda: [
            {
                "tool_name": "query_gmail_message_summaries",
                "invocation_index": 1,
                "status": "completed",
            }
        ],
    )
    monkeypatch.setattr(
        "keystone_agents.run.enforce_agent_run_budget",
        lambda **_kwargs: {"enforced": False},
    )

    result = run_typed_sdk_agent(
        agent=FakeAgent(),
        typed_input={"request": "Select the relevant Gmail thread."},
        output_type=ChiefOfStaffResult,
        live=True,
        config=ModelConfig(provider="openai", model="gpt-test", api_key="test-key"),
        structured_retry_evidence_provider=lambda: (
            {
                "tool_name": "query_gmail_message_summaries",
                "output": {"thread_id": "thread-verified"},
            },
        ),
    )

    assert calls == 2
    replay = result.request_cache["structured_output_retry_evidence_replay"]
    assert replay["status"] == "model_visible"
    assert replay["entry_count"] == 1
    assert replay["provider_calls_during_replay"] == 0
    assert len(replay["evidence_sha256"]) == 64
