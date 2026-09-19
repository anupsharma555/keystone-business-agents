"""Offline evidence tests for the backend agent-decision trace harness."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from keystone_agents.agent_decision_policy import get_agent_decision_policy
from keystone_agents.agent_registry import list_agent_specs
from keystone_agents.receipts.normalization import identity_fingerprint
from keystone_agents.runtime.decision_trace_harness import (
    DecisionAttemptEvidence,
    HandoffEvidence,
    ModelVisibleComponent,
    ProviderAttemptEvidence,
    assemble_backend_decision_scenario_trace,
    build_backend_decision_stage_trace,
    build_backend_decision_stage_trace_from_runtime,
    production_wrapper_matrix,
)


def _call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps(arguments),
    }


def _output(call_id: str, payload: Any) -> dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(payload),
    }


def _result(*items: Any) -> SimpleNamespace:
    return SimpleNamespace(new_items=list(items))


def _tool(name: str, *, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "params_json_schema": {
            "type": "object",
            "properties": properties or {},
            "additionalProperties": False,
        },
    }


def _decision(
    *,
    attempt: int,
    stage: str,
    candidates: tuple[str, ...],
    selected: str,
    status: str = "accepted",
    repair_requested: bool = False,
) -> DecisionAttemptEvidence:
    return DecisionAttemptEvidence(
        attempt=attempt,
        decision_owner="specialist_agent",
        decision_stage=stage,
        candidate_values=candidates,
        selected_values=(selected,) if selected else (),
        assessments=tuple(
            (
                candidate,
                "selected" if candidate == selected else "excluded",
                "Best bounded match." if candidate == selected else "Superseded or unrelated.",
            )
            for candidate in candidates
        ),
        reasoning="Selected the candidate that matches the current request and evidence.",
        limitations=("Provider bodies remain outside the trace packet.",),
        validator_status=status,
        validator_reason_code=(
            "selection_verified" if status == "accepted" else "unknown_identity"
        ),
        repair_requested=repair_requested,
    )


def _measured_kwargs(*, requests: int = 1) -> dict[str, Any]:
    return {
        "usage": {
            "available": True,
            "requests": requests,
            "input_tokens": 250,
            "output_tokens": 80,
            "provider_request_count_confirmed": True,
        },
        "cost": {
            "available": True,
            "estimated_usd": 0.0004,
            "source": "maintained_pricing_table",
        },
        "latency_ms": 125.0,
    }


def test_production_wrapper_matrix_covers_all_registered_agent_families() -> None:
    rows = production_wrapper_matrix()
    registered_routes = {spec.route_name for spec in list_agent_specs()}

    assert len(rows) == len(registered_routes)
    assert {row.route for row in rows} == registered_routes
    assert all(row.resolve_production_wrapper() for row in rows)
    assert all(row.builder and row.input_schema and row.output_schema for row in rows)
    assert all(row.decision_stage and row.expected_model_read_tools for row in rows)
    assert all("tool" not in row.natural_language_probe.lower() for row in rows)
    assert {row.wrapper_kind for row in rows} == {"direct_script", "work_item_runtime"}
    rows_by_route = {row.route: row for row in rows}
    for route in (
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
    ):
        assert rows_by_route[route].production_wrapper == (
            "keystone_agents.entrypoints.cli_impl:_run_ask_context_agent_live"
        )
        assert rows_by_route[route].wrapper_kind == "direct_script"


def test_rag_wrapper_matrix_preserves_typed_retrieval_contract() -> None:
    from keystone_agents.schemas.rag_retrieval import RAGRetrievalResult
    from keystone_agents.workflow_runner import run_prepared_work_item_specialist

    row = next(
        row for row in production_wrapper_matrix() if row.route == "rag_retrieval_specialist"
    )
    policy = get_agent_decision_policy(row.route)

    assert row.resolve_production_wrapper() is run_prepared_work_item_specialist
    assert row.wrapper_kind == "work_item_runtime"
    assert row.decision_stage == "rag_vector_store_retrieval"
    assert row.output_schema.endswith("RAGRetrievalResult")
    assert row.expected_model_read_tools == ["file_search"]
    assert row.verified_continuation_allowed is False
    assert policy.semantic_decisions and policy.deterministic_validators
    assert "decision" not in RAGRetrievalResult.model_fields


def test_joined_gmail_fake_model_trace_preserves_repair_without_repeating_provider_reads() -> None:
    current = "msg-current-2026"
    cancelled = "msg-cancelled-2026"
    obsolete = "msg-obsolete-2026"
    reminder = "msg-reminder-2026"
    fabricated = "msg-python-invented"
    query_output = {
        "status": "success",
        "messages": [
            {"message_id": current, "thread_id": "thread-current", "state": "current"},
            {"message_id": cancelled, "thread_id": "thread-cancelled", "state": "cancelled"},
            {"message_id": obsolete, "thread_id": "thread-obsolete", "state": "obsolete"},
            {"message_id": reminder, "thread_id": "thread-reminder", "state": "reminder"},
        ],
    }
    read_output = {
        "status": "success",
        "contexts": [
            {"message_id": current, "thread_id": "thread-current", "body": "private body"},
            {"message_id": cancelled, "thread_id": "thread-cancelled", "body": "private body"},
            {"message_id": obsolete, "thread_id": "thread-obsolete", "body": "private body"},
            {"message_id": reminder, "thread_id": "thread-reminder", "body": "private body"},
        ],
    }
    first_attempt = _result(
        _call("q1", "query_gmail_message_summaries", {"query": "synthetic G2i", "max_results": 4}),
        _output("q1", query_output),
        _call(
            "r1", "read_gmail_context", {"message_ids": [current, cancelled, obsolete, reminder]}
        ),
        _output("r1", read_output),
    )
    repair_attempt = _result()
    visible_context = json.dumps(query_output, sort_keys=True)
    visible_read = json.dumps(read_output, sort_keys=True)
    stage = build_backend_decision_stage_trace(
        stage_id="gmail-rank-and-reply",
        agent="gmail_triage",
        stage="gmail_candidate_selection",
        provider="openai",
        model="gpt-5.4-mini",
        run_id="run-synthetic-gmail",
        trace_id="trace-synthetic-gmail",
        model_visible_inputs=[
            f"Choose the current conversation.\n{visible_context}\n{visible_read}",
            (
                "Repair only the selection; reuse prior verified evidence.\n"
                f"{visible_context}\n{visible_read}"
            ),
        ],
        raw_results=[first_attempt, repair_attempt],
        attached_tools=[
            _tool("query_gmail_message_summaries", properties={"query": {"type": "string"}}),
            _tool("read_gmail_context", properties={"message_ids": {"type": "array"}}),
        ],
        input_components=[
            ModelVisibleComponent(
                category="typed_context_pack",
                value={"request": "choose current conversation", "read_only": True},
                source="GmailContextPack",
            ),
            ModelVisibleComponent(
                category="preacquired_evidence",
                value=query_output,
                source="query_gmail_message_summaries",
                required_identity_values=(current, cancelled, obsolete, reminder),
                verified=True,
            ),
        ],
        admission_by_tool={
            "query_gmail_message_summaries": {
                "tier": "core_read",
                "rationale": "Bounded mailbox candidate discovery.",
            },
            "read_gmail_context": {
                "tier": "core_read",
                "rationale": "Bounded context for the four returned candidates.",
            },
        },
        forbidden_tools=["create_gmail_draft_reply", "send_gmail_test_draft"],
        candidate_identity_paths={
            "query_gmail_message_summaries": ("messages[].message_id", "messages[].thread_id"),
            "read_gmail_context": ("contexts[].message_id", "contexts[].thread_id"),
        },
        decision_attempts=[
            _decision(
                attempt=1,
                stage="gmail_candidate_selection",
                candidates=(current, cancelled, obsolete, reminder, fabricated),
                selected=fabricated,
                status="repair_required",
                repair_requested=True,
            ),
            _decision(
                attempt=2,
                stage="gmail_candidate_selection",
                candidates=(current, cancelled, obsolete, reminder),
                selected=current,
            ),
        ],
        provider_attempts=[
            ProviderAttemptEvidence(
                provider="gmail_fixture",
                operation="query_and_read",
                status="success",
                receipt_verified=True,
                latency_ms=3.5,
            )
        ],
        receipts=[
            {
                "tool_name": "query_gmail_message_summaries",
                "provider": "gmail_fixture",
                "operation": "query",
                "status": "success",
                "verification": True,
                "identity_fingerprints": [
                    identity_fingerprint(value)
                    for value in (current, cancelled, obsolete, reminder)
                ],
            }
        ],
        provider_dependent=True,
        retries=1,
        **_measured_kwargs(requests=2),
    )

    assert [call.name for call in stage.model_called_tools] == [
        "query_gmail_message_summaries",
        "read_gmail_context",
    ]
    assert stage.consumption.agent_run_count == 1
    assert stage.consumption.model_request_count == 2
    assert stage.consumption.model_request_count_source == "sdk_usage"
    assert len(stage.decision_attempts) == 2
    assert stage.decision_attempts[0].repair_requested is True
    assert stage.decision_attempts[1].validator_status == "accepted"
    missing_provider_evidence = (
        "provider_dependent_stage_has_no_model_tool_call_or_"
        "verified_preacquired_context"
    )
    assert missing_provider_evidence not in stage.findings
    assert identity_fingerprint(fabricated) not in {
        item.identity_fingerprint for item in stage.candidate_universe
    }
    assert identity_fingerprint(current) in {
        item.identity_fingerprint for item in stage.candidate_universe
    }
    assert stage.input_components[1].required_identities_visible is True
    assert stage.attached_tools[0].schema_fingerprint
    assert stage.external_write_state == "not_performed"
    assert stage.evaluation_status == "pass"
    serialized = stage.model_dump_json()
    assert "private body" not in serialized
    assert current not in serialized
    assert fabricated not in serialized
    assert "synthetic G2i" not in serialized


def test_provider_candidate_universe_comes_from_raw_tool_output_not_model_report() -> None:
    returned = "provider-record-returned"
    model_claimed = "provider-record-model-only"
    stage = build_backend_decision_stage_trace(
        stage_id="airtable-candidate-binding",
        agent="airtable_context_agent",
        stage="airtable_record_selection",
        provider="openai",
        model="gpt-5.4-mini",
        run_id="run-airtable",
        trace_id="trace-airtable",
        model_visible_inputs=[f"records: {returned}"],
        raw_results=[
            _result(
                _call("a1", "airtable_read_records", {"limit": 3}),
                _output("a1", {"status": "success", "records": [{"record_id": returned}]}),
            )
        ],
        attached_tools=[_tool("airtable_read_records")],
        candidate_identity_paths={"airtable_read_records": ("records[].record_id",)},
        decision_attempts=[
            _decision(
                attempt=1,
                stage="airtable_record_selection",
                candidates=(returned, model_claimed),
                selected=model_claimed,
            )
        ],
        provider_dependent=True,
        **_measured_kwargs(),
    )

    assert {item.identity_fingerprint for item in stage.candidate_universe} == {
        identity_fingerprint(returned)
    }
    assert "agent_selection_not_bound_to_raw_provider_candidate_universe" in stage.findings
    assert stage.evaluation_status == "fail"


def test_provider_dependent_zero_model_calls_requires_verified_preacquired_context() -> None:
    failed = build_backend_decision_stage_trace(
        stage_id="rss-zero-model",
        agent="rss_context_agent",
        stage="signal_relevance_selection",
        provider="openai",
        model="gpt-5.4-mini",
        run_id="run-rss",
        trace_id="trace-rss",
        model_visible_inputs=[],
        provider_dependent=True,
        terminal_status="completed",
        usage={"available": True, "requests": 0, "provider_request_count_confirmed": True},
        cost={"available": True, "estimated_usd": 0.0},
        latency_ms=1.0,
    )
    supplied = build_backend_decision_stage_trace(
        stage_id="outreach-verified-context",
        agent="outreach_composer",
        stage="outreach_evidence_selection",
        provider="openai",
        model="gpt-5.4-mini",
        run_id="run-outreach",
        trace_id="trace-outreach",
        model_visible_inputs=["verified approved context fingerprint only"],
        input_components=[
            ModelVisibleComponent(
                category="preacquired_evidence",
                value={"context": "verified approved context fingerprint only"},
                source="approved_context_receipt",
                verified=True,
            )
        ],
        provider_dependent=True,
        preacquired_context_verified=True,
        intentional_tool_free_synthesis=True,
        **_measured_kwargs(),
    )

    assert failed.evaluation_status == "fail"
    assert (
        "provider_dependent_stage_reached_terminal_state_with_zero_model_requests"
        in failed.findings
    )
    assert supplied.evaluation_status == "pass"
    assert supplied.intentional_tool_free_synthesis is True
    assert supplied.preacquired_context_verified is True


def test_continuous_calendar_manager_gmail_slack_copy_flow_has_consumed_handoffs() -> None:
    event_id = "calendar-event-synthetic"
    message_id = "gmail-message-synthetic"
    started = datetime(2026, 8, 3, 14, 0, tzinfo=UTC)
    calendar = build_backend_decision_stage_trace(
        stage_id="calendar-read",
        agent="calendar_action_interpreter",
        stage="calendar_context_selection",
        provider="openai",
        model="gpt-5.4-mini",
        run_id="run-calendar",
        trace_id="trace-flow",
        model_visible_inputs=[f"Select the matching event from {event_id}"],
        raw_results=[
            _result(
                _call("cal1", "read_google_calendar_window", {"operation": "read", "limit": 4}),
                _output("cal1", {"status": "success", "events": [{"event_id": event_id}]}),
            )
        ],
        attached_tools=[_tool("read_google_calendar_window")],
        candidate_identity_paths={"read_google_calendar_window": ("events[].event_id",)},
        decision_attempts=[
            _decision(
                attempt=1,
                stage="calendar_context_selection",
                candidates=(event_id,),
                selected=event_id,
            )
        ],
        handoffs=[
            HandoffEvidence(
                source_agent="calendar_action_interpreter",
                downstream_agent="chief_of_staff",
                evidence_values=(event_id,),
                consumed=True,
                consumption_source="manager_calendar_context",
            )
        ],
        provider_dependent=True,
        started_at=started,
        ended_at=started + timedelta(milliseconds=40),
        **_measured_kwargs(),
    )
    manager = build_backend_decision_stage_trace(
        stage_id="manager-decision",
        agent="chief_of_staff",
        stage="chief_delegation_selection",
        provider="openai",
        model="gpt-5.4-mini",
        run_id="run-chief",
        trace_id="trace-flow",
        model_visible_inputs=[f"Calendar selected {event_id}; choose the next owner."],
        input_components=[
            ModelVisibleComponent(
                category="cross_provider_context",
                value={"event_id": event_id},
                source="calendar_provider_context_stage",
                required_identity_values=(event_id,),
                verified=True,
            )
        ],
        decision_attempts=[
            _decision(
                attempt=1,
                stage="chief_delegation_selection",
                candidates=("gmail_triage",),
                selected="gmail_triage",
            )
        ],
        handoffs=[
            HandoffEvidence(
                source_agent="chief_of_staff",
                downstream_agent="gmail_triage",
                evidence_values=(event_id,),
                consumed=True,
                consumption_source="GmailContextPack.cross_provider_context",
            )
        ],
        intentional_tool_free_synthesis=True,
        **_measured_kwargs(),
    )
    gmail = build_backend_decision_stage_trace(
        stage_id="gmail-query-read-rank",
        agent="gmail_triage",
        stage="gmail_candidate_selection",
        provider="openai",
        model="gpt-5.4-mini",
        run_id="run-gmail",
        trace_id="trace-flow",
        model_visible_inputs=[f"Event {event_id}; inspect message {message_id}."],
        raw_results=[
            _result(
                _call("gq1", "query_gmail_message_summaries", {"max_results": 4}),
                _output("gq1", {"status": "success", "messages": [{"message_id": message_id}]}),
                _call("gr1", "read_gmail_context", {"max_messages": 4}),
                _output("gr1", {"status": "success", "contexts": [{"message_id": message_id}]}),
            )
        ],
        attached_tools=[
            _tool("query_gmail_message_summaries"),
            _tool("read_gmail_context"),
        ],
        candidate_identity_paths={
            "query_gmail_message_summaries": ("messages[].message_id",),
            "read_gmail_context": ("contexts[].message_id",),
        },
        decision_attempts=[
            _decision(
                attempt=1,
                stage="gmail_candidate_selection",
                candidates=(message_id,),
                selected=message_id,
            )
        ],
        handoffs=[
            HandoffEvidence(
                source_agent="gmail_triage",
                downstream_agent="outreach_composer",
                evidence_values=(message_id,),
                consumed=True,
                consumption_source="approved_reply_context",
            )
        ],
        provider_dependent=True,
        **_measured_kwargs(),
    )
    slack_copy = build_backend_decision_stage_trace(
        stage_id="slack-only-reply-copy",
        agent="outreach_composer",
        stage="outreach_evidence_selection",
        provider="openai",
        model="gpt-5.4-mini",
        run_id="run-copy",
        trace_id="trace-flow",
        model_visible_inputs=[f"Use verified message {message_id}; return Slack-only copy."],
        input_components=[
            ModelVisibleComponent(
                category="mandatory_context",
                value={"message_id": message_id, "send_enabled": False},
                source="approved_reply_context",
                required_identity_values=(message_id,),
                verified=True,
            )
        ],
        preacquired_context_verified=True,
        intentional_tool_free_synthesis=True,
        provider_dependent=True,
        **_measured_kwargs(),
    )
    request = (
        "Check tomorrow's partner meeting, find the related email, decide whether a "
        "reply is warranted, and give me reply copy here only."
    )
    scenario = assemble_backend_decision_scenario_trace(
        scenario_id="calendar-gmail-slack-copy",
        request_text=request,
        stages=[calendar, manager, gmail, slack_copy],
    )

    assert scenario.request_text == request
    assert scenario.request_text_retained is True
    assert scenario.agent_run_count == 4
    assert scenario.model_request_count == 4
    assert scenario.evaluation_status == "pass"
    assert scenario.findings == []
    assert [stage.stage_id for stage in scenario.stages] == [
        "calendar-read",
        "manager-decision",
        "gmail-query-read-rank",
        "slack-only-reply-copy",
    ]


def test_trace_packet_withholds_private_request_and_never_retains_sensitive_bodies() -> None:
    private_request = "Review private.person@example.com and this KBA_TEST fixture."
    stage = build_backend_decision_stage_trace(
        stage_id="privacy",
        agent="gmail_triage",
        stage="gmail_candidate_selection",
        provider="fixture",
        model="fake-model",
        run_id="run-private",
        trace_id="trace-private",
        model_visible_inputs=[{"body": "highly private Gmail body", "message_id": "private-msg"}],
        input_components=[
            ModelVisibleComponent(
                category="typed_context_pack",
                value={"body": "highly private Gmail body", "message_id": "private-msg"},
                source="synthetic_private_fixture",
            )
        ],
        intentional_tool_free_synthesis=True,
        preacquired_context_verified=True,
        **_measured_kwargs(),
    )
    scenario = assemble_backend_decision_scenario_trace(
        scenario_id="privacy-guard",
        request_text=private_request,
        stages=[stage],
    )
    serialized = scenario.model_dump_json()

    assert scenario.request_text == ""
    assert scenario.request_text_retained is False
    assert scenario.request_capture_note == "request_text_withheld_by_trace_privacy_guard"
    assert "private.person@example.com" not in serialized
    assert "highly private Gmail body" not in serialized
    assert "private-msg" not in serialized
    assert scenario.stages[0].raw_sensitive_bodies_retained is False


def test_unavailable_usage_cost_latency_and_handoff_proof_are_reported_not_inferred() -> None:
    stage = build_backend_decision_stage_trace(
        stage_id="chief-handoff-partial",
        agent="chief_of_staff",
        stage="chief_delegation_selection",
        provider="unknown",
        model="unknown",
        run_id="",
        trace_id="",
        model_visible_inputs=["Delegate the bounded next step."],
        handoffs=[
            HandoffEvidence(
                source_agent="chief_of_staff",
                downstream_agent="business_research_analyst",
                evidence_values=("synthetic-context",),
                consumed=False,
            )
        ],
        intentional_tool_free_synthesis=True,
    )

    assert stage.evaluation_status == "fail"
    assert "handoff_has_no_downstream_consumption_postcondition" in stage.findings
    assert set(stage.unavailable_evidence) >= {
        "token_usage_not_recorded",
        "cost_evidence_not_recorded",
        "stage_latency_not_recorded",
        "run_id_not_recorded",
        "trace_id_not_recorded",
    }
    assert stage.consumption.model_request_count == 1
    assert stage.consumption.model_request_count_source == "captured_model_inputs"


def test_production_runtime_compiler_preserves_origins_and_reports_missing_capture() -> None:
    stage = build_backend_decision_stage_trace_from_runtime(
        stage_id="orchestrator-preflight-failure",
        agent="orchestrator",
        stage="orchestrator_route_selection",
        run_id="attempt-123",
        failure_metadata={
            "provider": "openai",
            "model": "gpt-5.4-mini",
            "usage": {
                "available": False,
                "requests": 0,
                "provider_request_count_confirmed": False,
            },
            "tool_execution": {
                "selected_tool_names": ["read_google_calendar_window"],
                "model_tool_call_count": 0,
                "model_called_tool_names": [],
                "workflow_tool_call_count": 0,
                "workflow_called_tool_names": [],
                "workflow_called_helper_names": [],
                "preacquired_context_tool_names": ["verified_calendar_context"],
                "provider_request_attempt_count": 0,
                "provider_request_attempt_count_available": False,
                "provider_request_success_count": 0,
                "provider_request_success_count_available": False,
                "provider_receipt_count": 1,
                "provider_receipt_count_available": True,
            },
            "tool_receipts": [
                {
                    "tool_name": "verified_calendar_context",
                    "operation": "read",
                    "status": "success",
                    "verification": {"passed": True},
                }
            ],
        },
        terminal_status="failed",
    )

    assert stage.preacquired_context_tools == ["verified_calendar_context"]
    assert stage.workflow_called_tools == []
    assert stage.external_write_state == "not_performed"
    assert stage.evaluation_status == "fail"
    assert "terminal_stage_failed" in stage.findings
    assert set(stage.unavailable_evidence) >= {
        "model_visible_input_not_captured",
        "attached_tool_schemas_not_captured",
        "provider_request_attempt_count_not_recorded",
        "provider_request_success_count_not_recorded",
        "decision_attempts_not_recorded",
    }


def test_production_runtime_compiler_hydrates_success_and_nested_repair_attempts() -> None:
    private_candidate = "provider-record-private-001"
    private_alternative = "provider-record-private-002"
    private_reasoning = "Select the private provider record described in the body."
    private_assessment_rationale = "Related record retained as a plausible alternative."
    linked_output = {
        "_sdk_model": {
            "provider": "openai",
            "name": "gpt-5.4-mini",
            "run_mode": "live_sdk",
        },
        "_sdk_usage": {
            "available": True,
            "requests": 2,
            "input_tokens": 420,
            "output_tokens": 90,
            "provider_request_count_confirmed": True,
        },
        "_sdk_cost": {"available": True, "estimated_usd": 0.0012},
        "_sdk_request_cache": {
            "static_prefix_sha256": "a" * 64,
            "instructions_sha256": "b" * 64,
            "output_schema_sha256": "c" * 64,
            "tool_names_sha256": "d" * 64,
            "dynamic_prompt_sha256": "e" * 64,
            "dynamic_prompt_chars": 240,
            "request_tool_scope": {
                "selected_tool_names": ["read_provider_records"],
                "selection_fingerprint": "f" * 64,
                "tool_schema_fingerprints": {
                    "read_provider_records": "1" * 64,
                },
            },
            "trace_summary": {
                "event_id": 17,
                "trace_id": "trace-safe-17",
                "model_tool_calls": [
                    {
                        "call_order": 1,
                        "name": "read_provider_records",
                        "status": "completed",
                        "output_observed": True,
                        "call_id_fingerprint": "2" * 64,
                    }
                ],
            },
            "tool_execution": {
                "mode": "llm_selected_function_tools",
                "selected_tool_names": ["read_provider_records"],
                "model_tool_call_count": 1,
                "model_called_tool_names": ["read_provider_records"],
                "workflow_tool_call_count": 0,
                "workflow_called_tool_names": [],
                "workflow_called_helper_names": [],
                "preacquired_context_tool_names": [],
                "provider_request_attempt_count": 1,
                "provider_request_attempt_count_available": True,
                "provider_request_success_count": 1,
                "provider_request_success_count_available": True,
                "provider_receipt_count": 1,
                "provider_receipt_count_available": True,
            },
            "decision_ownership": {
                "decision_owner": "specialist_agent",
                "decision_stage": "provider_record_selection",
                "attempt_count": 2,
                "repair_attempted": True,
                "attempts": [
                    {
                        "attempt": 1,
                        "candidate_ids": [private_candidate, private_alternative],
                        "selected_candidate_ids": ["invented-record"],
                        "candidate_assessments": [
                            {
                                "candidate_id": "invented-record",
                                "disposition": "selected",
                                "rationale": "Invalid first selection.",
                            },
                            {
                                "candidate_id": private_candidate,
                                "disposition": "plausible",
                                "rationale": private_assessment_rationale,
                            },
                        ],
                        "reasoning": private_reasoning,
                        "validator_outcome": {
                            "status": "repair_required",
                            "reason_code": "unknown_identity",
                        },
                    },
                    {
                        "attempt": 2,
                        "candidate_ids": [private_candidate, private_alternative],
                        "selected_candidate_ids": [private_candidate],
                        "candidate_assessments": [
                            {
                                "candidate_id": private_candidate,
                                "disposition": "selected",
                                "rationale": "Best verified match.",
                            },
                            {
                                "candidate_id": private_alternative,
                                "disposition": "plausible",
                                "rationale": private_assessment_rationale,
                            },
                        ],
                        "reasoning": private_reasoning,
                        "validator_outcome": {
                            "status": "accepted",
                            "reason_code": "selection_verified",
                        },
                    },
                ],
            },
        },
        "tool_receipts": [
            {
                "tool_name": "read_provider_records",
                "provider": "fixture_provider",
                "operation": "read",
                "status": "success",
                "record_id": private_candidate,
                "verification": True,
            }
        ],
    }

    stage = build_backend_decision_stage_trace_from_runtime(
        stage_id="linked-success",
        agent="synthetic_specialist",
        stage="provider_record_selection",
        run_id="42",
        linked_output=linked_output,
        terminal_status="success",
        latency_ms=88.5,
    )

    assert stage.provider == "openai"
    assert stage.model == "gpt-5.4-mini"
    assert stage.request_cache["fingerprints"]["static_prefix_sha256"] == "a" * 64
    assert stage.request_cache["fingerprints"]["dynamic_prompt_sha256"] == "e" * 64
    assert stage.trace_event_id == 17
    assert stage.model_inputs[0].fingerprint == "e" * 64
    assert stage.attached_tools[0].schema_fingerprint == "1" * 64
    assert stage.tool_admission_fingerprint == "f" * 64
    assert stage.model_called_tools[0].call_id_fingerprint == "2" * 64
    assert stage.provider_request_attempt_count == 1
    assert stage.provider_request_success_count == 1
    assert stage.provider_receipt_count == 1
    assert len(stage.decision_attempts) == 2
    assert stage.decision_attempts[0].repair_requested is True
    assert stage.decision_attempts[1].validator_status == "accepted"
    assert [
        assessment.disposition for assessment in stage.decision_attempts[1].assessments
    ] == ["selected", "plausible"]
    assert stage.decision_attempts[1].assessments[1].rationale_fingerprint
    serialized = stage.model_dump_json()
    assert private_candidate not in serialized
    assert private_alternative not in serialized
    assert private_reasoning not in serialized
    assert private_assessment_rationale not in serialized


def test_production_runtime_compiler_hydrates_sanitized_signal_candidate_universe() -> None:
    private_candidate = "rss-private-candidate-001"
    tool_name = "retrieve_rss_announcement_history"
    linked_output = {
        "_sdk_model": {
            "provider": "openai",
            "name": "gpt-5.4-mini",
            "run_mode": "live_sdk",
        },
        "_sdk_usage": {
            "available": True,
            "requests": 2,
            "provider_request_count_confirmed": True,
        },
        "_sdk_cost": {"available": True, "estimated_usd": 0.001},
        "_sdk_request_cache": {
            "static_prefix_sha256": "a" * 64,
            "instructions_sha256": "b" * 64,
            "output_schema_sha256": "c" * 64,
            "tool_names_sha256": "d" * 64,
            "dynamic_prompt_sha256": "e" * 64,
            "dynamic_prompt_chars": 180,
            "request_tool_scope": {
                "selected_tool_names": [tool_name],
                "selection_fingerprint": "f" * 64,
                "tool_schema_fingerprints": {tool_name: "1" * 64},
            },
            "trace_summary": {
                "event_id": 19,
                "trace_id": "signal-trace-19",
                "model_tool_calls": [
                    {
                        "call_order": 1,
                        "name": tool_name,
                        "status": "completed",
                        "output_observed": True,
                        "call_id_fingerprint": "2" * 64,
                    }
                ],
            },
            "tool_execution": {
                "mode": "llm_selected_function_tools",
                "selected_tool_names": [tool_name],
                "model_tool_call_count": 1,
                "model_called_tool_names": [tool_name],
                "workflow_tool_call_count": 0,
                "workflow_called_tool_names": [],
                "workflow_called_helper_names": [],
                "preacquired_context_tool_names": [],
                "provider_request_attempt_count": 1,
                "provider_request_attempt_count_available": True,
                "provider_request_success_count": 1,
                "provider_request_success_count_available": True,
                "provider_receipt_count": 0,
                "provider_receipt_count_available": True,
            },
            "signal_decision_evidence": {
                "source": "first_attempt_model_called_history_tool",
                "tool_name": tool_name,
                "candidate_count": 1,
                "candidate_ids": [private_candidate],
                "candidates": [
                    {
                        "candidate_id": private_candidate,
                        "title": "Private candidate title",
                    }
                ],
                "evidence_fingerprint": "3" * 64,
                "raw_provider_payload_retained": False,
            },
            "decision_ownership": {
                "decision_owner": "specialist_agent",
                "decision_stage": "signal_relevance_selection",
                "attempts": [
                    {
                        "attempt": 1,
                        "candidate_ids": [private_candidate],
                        "selected_candidate_ids": [private_candidate],
                        "validator_outcome": {"status": "accepted"},
                    }
                ],
            },
        },
    }

    stage = build_backend_decision_stage_trace_from_runtime(
        stage_id="rss-signal",
        agent="rss_context_agent",
        stage="signal_relevance_selection",
        run_id="91",
        linked_output=linked_output,
        terminal_status="success",
        latency_ms=25.0,
    )

    assert stage.evaluation_status == "pass"
    assert stage.unavailable_evidence == []
    assert stage.candidate_universe[0].identity_fingerprint == identity_fingerprint(
        private_candidate
    )
    assert stage.candidate_universe[0].source_tool == tool_name
    assert stage.request_cache["candidate_evidence"] == {
        "source": "first_attempt_model_called_history_tool",
        "tool_name": tool_name,
        "candidate_count": 1,
        "evidence_fingerprint": "3" * 64,
        "raw_provider_payload_retained": False,
    }
    assert private_candidate not in stage.model_dump_json()
    assert "Private candidate title" not in stage.model_dump_json()


def test_production_runtime_compiler_ingests_nested_specialist_trace_records() -> None:
    candidate_fingerprint = identity_fingerprint("private-provider-record")
    reasoning_fingerprint = "a" * 64
    linked_output = {
        "_sdk_usage": {"available": True, "requests": 1},
        "_sdk_request_cache": {
            "dynamic_prompt_sha256": "b" * 64,
            "nested_specialist_executions": [
                {
                    "schema": "keystone.nested_specialist_trace.v1",
                    "origin": "sdk_tool_call_output_custom_data",
                    "custom_data_key": "keystone_nested_specialist_execution",
                    "route_name": "airtable_context_agent",
                    "tool_name": "airtable_context_agent_as_specialist_tool",
                    "tool_call_id_fingerprint": "c" * 64,
                    "execution_state": "executed_nested_specialist",
                    "nested_execution_mode": "live_read_only",
                    "decision_owner": "specialist_agent",
                    "decision_stage": "airtable_record_selection",
                    "decision_attempts": [
                        {
                            "attempt": 1,
                            "decision_owner": "specialist_agent",
                            "decision_stage": "airtable_record_selection",
                            "candidate_identity_fingerprints": [candidate_fingerprint],
                            "selected_identity_fingerprints": [candidate_fingerprint],
                            "excluded_identity_fingerprints": [],
                            "reasoning_fingerprint": reasoning_fingerprint,
                            "limitation_fingerprints": ["d" * 64],
                            "validator_status": "accepted",
                            "validator_reason_code": "selection_verified",
                        }
                    ],
                    "candidate_identity_fingerprints": [candidate_fingerprint],
                    "selected_identity_fingerprints": [candidate_fingerprint],
                    "receipt_count": 1,
                    "receipt_fingerprints": ["e" * 64],
                    "repair_attempts": 0,
                    "handoff": {
                        "state": "executed",
                        "consumption_status": "returned_to_chief_model",
                        "terminal_status": "completed",
                    },
                    "provider_write_executed": False,
                    "raw_sensitive_values_retained": False,
                }
            ],
            "tool_execution": {
                "mode": "llm_selected_function_tools",
                "model_tool_call_count": 1,
                "model_called_tool_names": [
                    "airtable_context_agent_as_specialist_tool"
                ],
                "workflow_tool_call_count": 0,
                "provider_receipt_count_available": True,
                "provider_receipt_count": 1,
            },
        },
    }

    stage = build_backend_decision_stage_trace_from_runtime(
        stage_id="chief-nested-airtable",
        agent="chief_of_staff",
        stage="nested_context_selection",
        run_id="nested-1",
        linked_output=linked_output,
        terminal_status="success",
    )

    assert stage.decision_attempts[0].candidate_identity_fingerprints == [
        candidate_fingerprint
    ]
    assert stage.decision_attempts[0].reasoning_fingerprint == reasoning_fingerprint
    assert stage.handoffs[0].source_agent == "airtable_context_agent"
    assert stage.handoffs[0].downstream_agent == "chief_of_staff"
    assert stage.handoffs[0].consumed is True
    nested = stage.request_cache["nested_specialist_executions"][0]
    assert nested["origin"] == "sdk_tool_call_output_custom_data"
    assert "private-provider-record" not in stage.model_dump_json()


def test_production_runtime_compiler_accepts_verified_preacquired_tool_free_synthesis() -> None:
    linked_output = {
        "_sdk_usage": {
            "available": True,
            "requests": 1,
            "provider_request_count_confirmed": True,
        },
        "_sdk_cost": {"available": True, "estimated_usd": 0.0002},
        "_sdk_request_cache": {
            "dynamic_prompt_sha256": "7" * 64,
            "dynamic_prompt_chars": 120,
            "pre_model_decision_context": {
                "context_source": "preacquired_verified_context",
                "candidate_ids": ["private-context-id"],
                "model_input_visibility_checked": True,
                "context_complete": True,
            },
            "tool_execution": {
                "mode": "verified_context_tool_free",
                "selected_tool_names": [],
                "model_tool_call_count": 0,
                "model_called_tool_names": [],
                "workflow_tool_call_count": 0,
                "workflow_called_tool_names": [],
                "workflow_called_helper_names": ["compile_verified_context"],
                "preacquired_context_tool_names": ["read_context_upstream"],
                "provider_request_attempt_count": 1,
                "provider_request_attempt_count_available": True,
                "provider_request_success_count": 1,
                "provider_request_success_count_available": True,
                "provider_receipt_count": 1,
                "provider_receipt_count_available": True,
            },
            "decision_ownership": {
                "decision_owner": "specialist_agent",
                "decision_stage": "context_selection",
                "attempts": [
                    {
                        "attempt": 1,
                        "candidate_ids": ["private-context-id"],
                        "selected_candidate_ids": ["private-context-id"],
                        "validator_outcome": {"status": "accepted"},
                    }
                ],
            },
        },
        "preacquired_tool_receipts": [
            {
                "tool_name": "read_context_upstream",
                "operation": "read",
                "status": "success",
                "verification": True,
            }
        ],
    }

    stage = build_backend_decision_stage_trace_from_runtime(
        stage_id="preacquired-tool-free",
        agent="synthetic_specialist",
        stage="context_selection",
        run_id="43",
        linked_output=linked_output,
        terminal_status="success",
        latency_ms=25.0,
    )

    assert stage.intentional_tool_free_synthesis is True
    assert stage.preacquired_context_verified is True
    assert stage.model_called_tools == []
    assert stage.preacquired_context_tools == ["read_context_upstream"]
    missing_provider_evidence = (
        "provider_dependent_stage_has_no_model_tool_call_or_"
        "verified_preacquired_context"
    )
    assert missing_provider_evidence not in stage.findings
    assert "private-context-id" not in stage.model_dump_json()


def test_production_runtime_compiler_preserves_failure_repair_evidence() -> None:
    linked_output = {
        "sdk_run_failure": {
            "failure_kind": "agentdecisionvalidationerror",
            "attempt_count": 2,
            "usage": {"available": True, "requests": 2},
            "cost": {"available": True, "estimated_usd": 0.0006},
            "request_cache": {
                "dynamic_prompt_sha256": "8" * 64,
                "dynamic_prompt_chars": 90,
                "decision_repairs": 1,
                "tool_execution": {
                    "mode": "tool_free_failed",
                    "model_tool_call_count": 0,
                    "model_called_tool_names": [],
                    "workflow_tool_call_count": 0,
                    "workflow_called_tool_names": [],
                    "workflow_called_helper_names": [],
                    "preacquired_context_tool_names": [],
                    "provider_request_attempt_count": 0,
                    "provider_request_attempt_count_available": True,
                    "provider_request_success_count": 0,
                    "provider_request_success_count_available": True,
                    "provider_receipt_count": 0,
                    "provider_receipt_count_available": True,
                },
                "decision_ownership": {
                    "decision_owner": "specialist_agent",
                    "decision_stage": "candidate_selection",
                    "attempts": [
                        {
                            "attempt": 1,
                            "candidate_ids": ["candidate-a"],
                            "selected_candidate_ids": ["invented-a"],
                            "validator_outcome": {
                                "status": "repair_required",
                                "reason_code": "unknown_identity",
                            },
                        },
                        {
                            "attempt": 2,
                            "candidate_ids": ["candidate-a"],
                            "selected_candidate_ids": ["invented-b"],
                            "validator_outcome": {
                                "status": "rejected",
                                "reason_code": "unknown_identity",
                            },
                        },
                    ],
                },
            },
        }
    }

    stage = build_backend_decision_stage_trace_from_runtime(
        stage_id="linked-failure",
        agent="synthetic_specialist",
        stage="candidate_selection",
        run_id="44",
        linked_output=linked_output,
        terminal_status="failed",
        latency_ms=30.0,
    )

    assert stage.terminal_status == "failed"
    assert stage.failure_evidence == {
        "kind": "agentdecisionvalidationerror",
        "attempt_count": 2,
        "decision_repairs": 1,
        "raw_error_retained": False,
    }
    assert len(stage.decision_attempts) == 2
    assert stage.decision_attempts[-1].validator_status == "rejected"
    assert "terminal_stage_failed" in stage.findings
