from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any

import pytest

from keystone_agents.agent_decision_contracts import context_agent_decision_contract
from keystone_agents.agents.airtable_context import build_airtable_context_agent
from keystone_agents.agents.google_workspace_context import (
    build_google_workspace_context_agent,
)
from keystone_agents.agents.zotero_context import build_zotero_context_agent
from keystone_agents.capabilities.tool_scope import tool_scope_receipt_for_agent
from keystone_agents.receipts.normalization import identity_fingerprint, identity_fingerprints
from keystone_agents.run import (
    run_typed_sdk_agent,
    sdk_run_failure_metadata,
)
from keystone_agents.runtime.decision_validation import AgentDecisionValidationError
from keystone_agents.schemas.airtable import airtable_schema_snapshot_sha256
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.operational_context import (
    AirtableContextResult,
    GoogleWorkspaceContextResult,
    ZoteroContextResult,
)
from keystone_agents.sdk import build_local_run_config
from keystone_agents.tools import internal_data_tools, zotero_context_tools

try:
    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )
except ImportError:
    pytestmark = pytest.mark.skip(reason="OpenAI Agents SDK fake-model hooks unavailable.")


class FakeModel(Model):
    """Deterministic model fixture; it never reaches an external provider."""

    def __init__(self, outputs: list[list[Any]]) -> None:
        self.outputs = outputs
        self.calls: list[dict[str, Any]] = []

    async def get_response(
        self,
        system_instructions: str | None,
        input: str | list[Any],
        model_settings: Any,
        tools: list[Any],
        output_schema: Any,
        handoffs: list[Any],
        tracing: Any,
        *,
        previous_response_id: str | None,
        conversation_id: str | None,
        prompt: Any,
    ) -> ModelResponse:
        self.calls.append(
            {
                "input": input,
                "tool_names": [tool.name for tool in tools],
                "system_instructions": system_instructions,
            }
        )
        return ModelResponse(
            output=self.outputs.pop(0),
            usage=Usage(requests=1),
            response_id=f"context-matrix-fake-{len(self.calls)}",
        )

    def stream_response(self, *_args: Any, **_kwargs: Any) -> AsyncIterator[Any]:
        raise NotImplementedError


class FakeProvider(ModelProvider):
    def __init__(self, model: FakeModel) -> None:
        self.model = model

    def get_model(self, _model_name: str | None) -> Model:
        return self.model


@dataclass(frozen=True)
class ContextAgentCase:
    route: str
    prompt: str
    builder: Callable[..., Any]
    output_type: type[Any]
    tool_calls: tuple[Any, ...]
    provider_marker: str
    actual_id: str
    alternative_id: str
    decision_stage: str


def _tool_call(name: str, arguments: dict[str, Any], *, call_id: str) -> Any:
    return ResponseFunctionToolCall(
        type="function_call",
        name=name,
        call_id=call_id,
        arguments=json.dumps(arguments),
        status="completed",
    )


def _structured_message(payload: dict[str, Any]) -> Any:
    return ResponseOutputMessage(
        id="context-matrix-output",
        type="message",
        role="assistant",
        status="completed",
        content=[
            ResponseOutputText(
                type="output_text",
                text=json.dumps(payload),
                annotations=[],
            )
        ],
    )


def _model_input_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def _decision(
    *,
    stage: str,
    selected_id: str,
    candidate_ids: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "decision_owner": "specialist_agent",
        "decision_stage": stage,
        "selected_candidate_id": selected_id,
        "candidate_assessments": [
            {
                "candidate_id": candidate_id,
                "disposition": "selected" if candidate_id == selected_id else "excluded",
                "rationale": (
                    "The bounded provider result matches the requested current object."
                    if candidate_id == selected_id
                    else "The bounded provider result is a plausible but older alternative."
                ),
            }
            for candidate_id in candidate_ids
        ],
        "reasoning": "Selected the best matching identity from the visible provider results.",
        "limitations": ["Synthetic provider metadata only; no write was attempted."],
        "needs_more_context": False,
    }


def _output_payload(
    case: ContextAgentCase,
    *,
    selected_id: str,
    candidate_ids: tuple[str, ...],
) -> dict[str, Any]:
    decision = _decision(
        stage=case.decision_stage,
        selected_id=selected_id,
        candidate_ids=candidate_ids,
    )
    if case.route == "airtable_context_agent":
        return {
            "mode": "llm",
            "summary": "Selected the current evaluation record after reading schema and records.",
            "base_alias": "eval_tracker",
            "relevant_tables": ["Eval Runs"],
            "relevant_fields": ["Run Name", "Status"],
            "candidate_record_ids": list(candidate_ids),
            "record_summaries": [
                {"key": selected_id, "value": "Current evaluation run", "note": "Ready"}
            ],
            "recommended_record_identity": selected_id,
            "decision": decision,
        }
    if case.route == "google_workspace_context_agent":
        return {
            "mode": "llm",
            "summary": "Selected the current Drive artifact from bounded metadata.",
            "relevant_files": list(candidate_ids),
            "recommended_target": selected_id,
            "decision": decision,
        }
    return {
        "mode": "llm",
        "summary": "Selected the current Zotero item from bounded library metadata.",
        "library_context": "Synthetic read-only Zotero cache",
        "article_titles": ["Current evidence review"],
        # Item keys are identities actually used by the result. Source IDs retain
        # the excluded candidate so the validator can require its assessment.
        "zotero_item_keys": [selected_id],
        "source_ids": [
            candidate_id for candidate_id in candidate_ids if candidate_id != selected_id
        ],
        "decision": decision,
    }


def _configure_case(
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> ContextAgentCase:
    if route == "airtable_context_agent":
        actual_id = "rec-eval-current"
        alternative_id = "rec-eval-older"
        marker = "SCHEMA_FIXTURE_EVAL_RUNS"

        def fake_schema(**_kwargs: Any) -> dict[str, Any]:
            return {
                "status": "success",
                "provider_read": True,
                "schema": {
                    "marker": marker,
                    "base_alias": "eval_tracker",
                    "tables": [
                        {
                            "name": "Eval Runs",
                            "fields": [{"name": "Run Name"}, {"name": "Status"}],
                        }
                    ],
                },
                "send_enabled": False,
            }

        def fake_records(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "status": "success",
                "provider_read": True,
                "table": "Eval Runs",
                "records": [
                    {"id": actual_id, "fields": {"Run Name": "Current", "Status": "Ready"}},
                    {"id": alternative_id, "fields": {"Run Name": "Older", "Status": "Done"}},
                ],
                "identity_fingerprints": identity_fingerprints([actual_id, alternative_id]),
                "item_count": 2,
                "send_enabled": False,
            }

        monkeypatch.setattr(internal_data_tools, "airtable_get_base_schema_impl", fake_schema)
        monkeypatch.setattr(internal_data_tools, "airtable_read_records_impl", fake_records)
        return ContextAgentCase(
            route=route,
            prompt=(
                "Inspect the Eval Tracker schema, find the current Ready evaluation run, "
                "and identify the best matching record. Do not modify Airtable."
            ),
            builder=build_airtable_context_agent,
            output_type=AirtableContextResult,
            tool_calls=(
                _tool_call(
                    "airtable_get_base_schema",
                    {"base_alias": "eval_tracker", "live": False},
                    call_id="airtable-schema",
                ),
                _tool_call(
                    "airtable_read_records",
                    {
                        "table": "Eval Runs",
                        "base_alias": "eval_tracker",
                        "max_records": 4,
                        "live": False,
                    },
                    call_id="airtable-records",
                ),
            ),
            provider_marker=marker,
            actual_id=actual_id,
            alternative_id=alternative_id,
            decision_stage="airtable_record_selection",
        )

    if route == "google_workspace_context_agent":
        actual_id = "drive-file-current"
        alternative_id = "drive-file-older"
        marker = "DRIVE_FIXTURE_CURRENT_REVIEW"

        def fake_search(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
            return {
                "status": "success",
                "operation": "search_files",
                "provider_read": True,
                "folder_path": "KNIOps",
                "query": "quarterly review",
                "marker": marker,
                "items": [
                    {
                        "id": actual_id,
                        "name": "Quarterly Review - Current",
                        "mimeType": "application/vnd.google-apps.document",
                    },
                    {
                        "id": alternative_id,
                        "name": "Quarterly Review - Archive",
                        "mimeType": "application/vnd.google-apps.document",
                    },
                ],
                "identity_fingerprints": identity_fingerprints([actual_id, alternative_id]),
                "item_count": 2,
                "send_enabled": False,
            }

        monkeypatch.setattr(internal_data_tools, "google_drive_search_files_impl", fake_search)
        return ContextAgentCase(
            route=route,
            prompt=(
                "Find the current quarterly review document in KNIOps and identify which "
                "Drive file should be used. Do not modify Workspace."
            ),
            builder=build_google_workspace_context_agent,
            output_type=GoogleWorkspaceContextResult,
            tool_calls=(
                _tool_call(
                    "google_drive_search_files",
                    {
                        "query": "quarterly review",
                        "folder_path": "KNIOps",
                        "max_items": 5,
                        "live": False,
                    },
                    call_id="workspace-search",
                ),
            ),
            provider_marker=marker,
            actual_id=actual_id,
            alternative_id=alternative_id,
            decision_stage="workspace_artifact_selection",
        )

    actual_id = "ZOTERO-CURRENT"
    alternative_id = "ZOTERO-OLDER"
    marker = "ZOTERO_FIXTURE_EVIDENCE_REVIEW"

    monkeypatch.setenv("ZOTERO_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "synthetic-library")
    monkeypatch.setattr(
        zotero_context_tools,
        "_read_zotero_api_json",
        lambda *_args, **_kwargs: [
            {
                "key": actual_id,
                "data": {
                    "key": actual_id,
                    "title": "Current evidence review",
                    "date": "2026-08-01",
                    "marker": marker,
                },
            },
            {
                "key": alternative_id,
                "data": {
                    "key": alternative_id,
                    "title": "Earlier evidence review",
                    "date": "2025-04-01",
                },
            },
        ],
    )
    return ContextAgentCase(
        route=route,
        prompt=(
            "Search Zotero for the current evidence review and "
            "identify the best item for research. Do not edit Zotero."
        ),
        builder=build_zotero_context_agent,
        output_type=ZoteroContextResult,
        tool_calls=(
            _tool_call(
                "zotero_read_api_metadata",
                {
                    "query": "evidence review",
                    "limit": 5,
                    "item_type": "journalArticle",
                    "selection_count": 2,
                    "live": True,
                },
                call_id="zotero-read",
            ),
        ),
        provider_marker=marker,
        actual_id=actual_id,
        alternative_id=alternative_id,
        decision_stage="zotero_item_selection",
    )


def _run_case(case: ContextAgentCase, model: FakeModel) -> Any:
    provider_system = {
        "airtable_context_agent": "airtable",
        "google_workspace_context_agent": "google_workspace",
        "zotero_context_agent": "zotero",
    }[case.route]
    manual_plan = ManualRequestPlan(
        source="canonical:test",
        target_agent=case.route,
        intent="context_lookup",
        task_objective="context_lookup",
        provider_system=provider_system,
        provider_operations=["read", "search"],
        target_type=(
            "zotero_article"
            if case.route == "zotero_context_agent"
            else "business_system_context"
        ),
        provider_action_steps=(
            [{"operation": "search", "resource_type": "zotero_item"}]
            if case.route == "zotero_context_agent"
            else []
        ),
        ask_shape={"permission_state": "read_only"},
    )
    agent = case.builder(request_text=case.prompt, manual_plan=manual_plan)
    scope = tool_scope_receipt_for_agent(agent)
    assert scope["effective_mode"] == "request_scoped"
    assert not any(
        marker in tool_name
        for tool_name in scope["selected_tool_names"]
        for marker in (
            "append",
            "create",
            "delete",
            "import",
            "remove",
            "rename",
            "trash",
            "update",
            "upload",
            "write",
        )
    )
    return run_typed_sdk_agent(
        agent=agent,
        typed_input=case.prompt,
        output_type=case.output_type,
        run_config=build_local_run_config(FakeProvider(model)),
        decision_contract=context_agent_decision_contract(
            case.route,
            provider_candidate_ids=(case.actual_id, case.alternative_id),
        ),
    )


@pytest.mark.parametrize(
    "route",
    [
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
    ],
)
def test_context_agent_model_sees_provider_candidates_before_selection(
    route: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _configure_case(monkeypatch, route)
    candidate_ids = (case.actual_id, case.alternative_id)
    payload = _output_payload(
        case,
        selected_id=case.actual_id,
        candidate_ids=candidate_ids,
    )
    model = FakeModel(
        outputs=[
            *[[tool_call] for tool_call in case.tool_calls],
            [_structured_message(payload)],
        ]
    )

    result = _run_case(case, model)

    initial_input = _model_input_text(model.calls[0]["input"])
    selection_input = _model_input_text(model.calls[-1]["input"])
    assert case.actual_id not in initial_input
    assert case.alternative_id not in initial_input
    assert case.provider_marker not in initial_input
    assert case.actual_id in selection_input
    assert case.alternative_id in selection_input
    assert case.provider_marker in selection_input
    if route == "airtable_context_agent":
        records_turn_input = _model_input_text(model.calls[1]["input"])
        assert case.provider_marker in records_turn_input
        assert case.actual_id not in records_turn_input
        assert [call.name for call in case.tool_calls] == [
            "airtable_get_base_schema",
            "airtable_read_records",
        ]

    assert result.output.decision.selected_candidate_ids == [case.actual_id]
    telemetry = result.request_cache["decision_ownership"]
    assert telemetry["validator_outcome"]["status"] == "accepted"
    assert telemetry["selected_candidate_ids"] == [case.actual_id]
    assert set(telemetry["candidate_ids"]) == set(candidate_ids)
    pre_model = result.request_cache["pre_model_decision_context"]
    assert pre_model["context_source"] == "model_tool_loop"
    assert pre_model["candidate_ids"] == []
    receipt_fingerprints = {
        fingerprint
        for receipt in result.tool_receipts
        for fingerprint in receipt.get("identity_fingerprints", [])
    }
    assert identity_fingerprint(case.actual_id) in receipt_fingerprints
    assert identity_fingerprint(case.alternative_id) in receipt_fingerprints


def test_airtable_specialist_reads_exact_schema_detail_and_raw_records_before_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actual_id = "rec-current-exact"
    alternative_id = "rec-older-exact"
    long_description = "Source condition. " * 32 + "NOT approved for external use."
    schema_payload = {
        "tables": [
            {
                "id": "tblEvidence",
                "name": "Evidence —  Review",
                "primaryFieldId": "fldName",
                "fields": [
                    {"id": "fldName", "name": "Name", "type": "singleLineText"},
                    {
                        "id": "fldStatus",
                        "name": "Decision —  source",
                        "type": "singleSelect",
                        "description": long_description,
                        "options": {
                            "choices": [
                                {"id": f"sel{index}", "name": f"State {index}"}
                                for index in range(21)
                            ]
                        },
                    },
                    {
                        "id": "fldFormula",
                        "name": "Derived  decision",
                        "type": "formula",
                        "options": {
                            "formula": 'IF({fldStatus}="Needs — Review","Hold  now","Proceed")',
                            "isValid": True,
                            "referencedFieldIds": ["fldStatus"],
                            "result": {
                                "type": "currency",
                                "options": {"precision": 2, "symbol": "£"},
                            },
                        },
                    },
                ],
            }
        ]
    }
    source_sha256 = airtable_schema_snapshot_sha256(schema_payload)
    record_pages = {
        "": {
            "records": [
                {
                    "id": actual_id,
                    "fields": {
                        "Name": "Current",
                        "Amount": 0,
                        "Flag": False,
                        "Blank": "",
                        "Related": [],
                        "Nested": {"unit": "£", "amount": -2.5},
                        "Status": "NOT approved",
                    },
                }
            ],
            "offset": "itr-second",
        },
        "itr-second": {
            "records": [
                {
                    "id": alternative_id,
                    "fields": {
                        "Name": "Older",
                        "Amount": -4,
                        "Related": ["recTarget"],
                        "Status": "Only after review",
                    },
                }
            ]
        },
    }

    def fake_airtable_send(request: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        if "/meta/bases/" in str(request.get("url") or ""):
            return schema_payload
        params = request.get("params") if isinstance(request.get("params"), dict) else {}
        return record_pages[str(params.get("offset") or "")]

    monkeypatch.setenv("AIRTABLE_BASE_ID", "appSynthetic")
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "pat_synthetic")
    monkeypatch.setenv("AIRTABLE_ALLOWED_TABLES", "Evidence —  Review")
    monkeypatch.setattr(internal_data_tools, "_airtable_send", fake_airtable_send)
    case = ContextAgentCase(
        route="airtable_context_agent",
        prompt=(
            "Inspect the exact Airtable schema and later Status details, then compare the "
            "two bounded records without writing. Preserve raw values and limitations."
        ),
        builder=build_airtable_context_agent,
        output_type=AirtableContextResult,
        tool_calls=(
            _tool_call(
                "airtable_get_base_schema",
                {"base_id": "appSynthetic", "live": True},
                call_id="airtable-schema",
            ),
            _tool_call(
                "airtable_read_schema_detail",
                {
                    "base_id": "appSynthetic",
                    "read_mode": "field_detail",
                    "table_id": "tblEvidence",
                    "field_id": "fldStatus",
                    "max_chars": 2_000,
                    "expected_source_sha256": source_sha256,
                    "live": True,
                },
                call_id="airtable-schema-detail",
            ),
            _tool_call(
                "airtable_read_records",
                {
                    "table": "Evidence —  Review",
                    "base_id": "appSynthetic",
                    "max_records": 3,
                    "fetch_all": True,
                    "live": True,
                },
                call_id="airtable-records",
            ),
        ),
        provider_marker="NOT approved for external use.",
        actual_id=actual_id,
        alternative_id=alternative_id,
        decision_stage="airtable_record_selection",
    )
    payload = _output_payload(
        case,
        selected_id=actual_id,
        candidate_ids=(actual_id, alternative_id),
    )
    model = FakeModel(
        outputs=[
            *[[tool_call] for tool_call in case.tool_calls],
            [_structured_message(payload)],
        ]
    )

    result = _run_case(case, model)

    schema_turn = _model_input_text(model.calls[1]["input"])
    schema_output = json.loads(
        next(
            item["output"]
            for item in reversed(model.calls[1]["input"])
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        )
    )
    detail_output = json.loads(
        next(
            item["output"]
            for item in reversed(model.calls[2]["input"])
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        )
    )
    records_output = json.loads(
        next(
            item["output"]
            for item in reversed(model.calls[3]["input"])
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        )
    )
    assert "Decision \\\\u2014  source" in schema_turn
    assert "Derived  decision" in schema_turn
    assert "Hold  now" in schema_turn
    formula_field = schema_output["schema"]["tables"][0]["fields"][2]
    assert formula_field["validity"] == "valid"
    assert formula_field["result_options"] == {"precision": 2, "symbol": "£"}
    exact_detail = json.loads(detail_output["detail_text"])
    assert exact_detail["description"] == long_description
    assert exact_detail["options"]["choices"][-1]["name"] == "State 20"
    assert [record["id"] for record in records_output["records"]] == [
        actual_id,
        alternative_id,
    ]
    assert records_output["records"][0]["fields"]["Amount"] == 0
    assert records_output["records"][0]["fields"]["Flag"] is False
    assert records_output["records"][0]["fields"]["Blank"] == ""
    assert records_output["records"][0]["fields"]["Related"] == []
    assert records_output["records"][0]["fields"]["Status"] == "NOT approved"
    assert result.output.decision.selected_candidate_ids == [actual_id]
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == (
        "accepted"
    )


def test_airtable_large_schema_preview_is_bounded_in_actual_sdk_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema_payload = {
        "tables": [
            {
                "id": f"tbl{table_index:02d}",
                "name": f"Table {table_index:02d}",
                "primaryFieldId": f"fld{table_index:02d}00",
                "fields": [
                    {
                        "id": f"fld{table_index:02d}{field_index:02d}",
                        "name": f"Field {table_index:02d}-{field_index:02d}",
                        "type": "singleLineText",
                    }
                    for field_index in range(20)
                ],
            }
            for table_index in range(12)
        ]
    }
    monkeypatch.setenv("AIRTABLE_BASE_ID", "appSynthetic")
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "pat_synthetic")
    monkeypatch.setenv(
        "AIRTABLE_ALLOWED_TABLES",
        ",".join(table["name"] for table in schema_payload["tables"]),
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_airtable_send",
        lambda *_args, **_kwargs: schema_payload,
    )
    prompt = "List the exact visible Airtable field identities and types; do not read rows."
    plan = ManualRequestPlan(
        source="canonical:test",
        target_agent="airtable_context_agent",
        intent="context_lookup",
        task_objective="context_lookup",
        provider_system="airtable",
        provider_operations=["read"],
        target_type="business_system_context",
        ask_shape={"permission_state": "read_only"},
    )
    model = FakeModel(
        outputs=[
            [
                _tool_call(
                    "airtable_get_base_schema",
                    {"base_id": "appSynthetic", "live": True},
                    call_id="airtable-large-schema",
                )
            ],
            [
                _structured_message(
                    {
                        "mode": "llm",
                        "summary": "Reviewed the bounded exact schema preview.",
                        "base_id": "appSynthetic",
                        "relevant_tables": ["Table 00"],
                        "relevant_fields": ["Field 00-00"],
                        "blockers": [],
                    }
                )
            ],
        ]
    )
    agent = build_airtable_context_agent(
        request_text=prompt,
        manual_plan=plan,
        tool_tier="core_read",
    )

    result = run_typed_sdk_agent(
        agent=agent,
        typed_input=prompt,
        output_type=AirtableContextResult,
        run_config=build_local_run_config(FakeProvider(model)),
    )

    schema_output_text = next(
        item["output"]
        for item in reversed(model.calls[1]["input"])
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    )
    schema_output = json.loads(schema_output_text)
    raw_chars = len(
        json.dumps(schema_payload, ensure_ascii=False, separators=(",", ":"))
    )
    preview_chars = len(
        json.dumps(schema_output["schema"], ensure_ascii=False, separators=(",", ":"))
    )
    visible_fields = sum(
        len(table["fields"])
        for table in schema_output["schema"]["tables"]
    )

    assert visible_fields == 240
    assert schema_output["schema"]["tables"][0]["fields"][0]["name"] == "Field 00-00"
    assert schema_output["schema"]["tables"][-1]["fields"][-1]["name"] == (
        "Field 11-19"
    )
    assert preview_chars <= raw_chars * 4
    assert result.output.summary == "Reviewed the bounded exact schema preview."


def test_google_sheet_source_cells_reach_actual_specialist_input_before_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reply:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def execute(self) -> dict[str, Any]:
            return self.payload

    class SheetsService:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def spreadsheets(self) -> SheetsService:
            return self

        def values(self) -> SheetsService:
            return self

        def get(self, **kwargs: Any) -> Reply:
            self.calls.append(dict(kwargs))
            if kwargs.get("includeGridData") is True:
                return Reply(
                    {
                        "spreadsheetId": "sheetSynthetic",
                        "sheets": [
                            {
                                "properties": {
                                    "sheetId": 7,
                                    "title": "Evidence",
                                },
                                "data": [
                                    {
                                        "startRow": 0,
                                        "startColumn": 0,
                                        "rowData": [
                                            {
                                                "values": [
                                                    {
                                                        "userEnteredValue": {
                                                            "formulaValue": "=SUM(C1:C3)"
                                                        },
                                                        "effectiveValue": {"numberValue": 1.2344},
                                                        "formattedValue": "$1.23",
                                                        "note": (
                                                            "Late qualification: NOT approved "
                                                            "without external review."
                                                        ),
                                                        "hyperlink": ("https://example.com/source"),
                                                    },
                                                    {
                                                        "userEnteredValue": {
                                                            "stringValue": "=SUM(C1:C3)"
                                                        },
                                                        "effectiveValue": {
                                                            "stringValue": "=SUM(C1:C3)"
                                                        },
                                                        "formattedValue": "=SUM(C1:C3)",
                                                    },
                                                ]
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                )
            return Reply(
                {
                    "spreadsheetId": "sheetSynthetic",
                    "properties": {
                        "title": "Evidence workbook",
                        "locale": "en_US",
                        "timeZone": "America/New_York",
                    },
                    "sheets": [
                        {
                            "properties": {
                                "sheetId": 7,
                                "title": "Evidence",
                                "gridProperties": {
                                    "rowCount": 100,
                                    "columnCount": 20,
                                },
                            }
                        }
                    ],
                    "namedRanges": [],
                }
            )

    service = SheetsService()
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": object(), "sheets": service},
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_configured_google_account",
        lambda *_: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_google_sheet_under_kniops",
        lambda *_: None,
    )
    prompt = (
        "Inspect A1:B1 in the exact Evidence sheet. Explain which cell is a formula, "
        "which is a literal, the unrounded value, and the qualification. Do not write."
    )
    plan = ManualRequestPlan(
        source="canonical:test",
        target_agent="google_workspace_context_agent",
        intent="context_lookup",
        task_objective="context_lookup",
        provider_system="google_workspace",
        provider_operations=["read"],
        target_type="business_system_context",
        provider_action_steps=[{"operation": "read", "resource_type": "google_spreadsheet"}],
        ask_shape={"permission_state": "read_only"},
    )
    decision = {
        "decision_owner": "specialist_agent",
        "decision_stage": "workspace_artifact_selection",
        "selected_candidate_id": "sheetSynthetic",
        "candidate_assessments": [
            {
                "candidate_id": "sheetSynthetic",
                "disposition": "selected",
                "rationale": "The exact requested spreadsheet supplied the bounded cells.",
            }
        ],
        "reasoning": "Selected the exact spreadsheet identity returned by the tool.",
        "limitations": ["Synthetic provider response; no write was attempted."],
        "needs_more_context": False,
    }
    model = FakeModel(
        outputs=[
            [
                _tool_call(
                    "google_sheet_read_table",
                    {
                        "spreadsheet_id_or_url": "sheetSynthetic",
                        "range_a1": "'Evidence'!A1:B1",
                        "max_rows": 1,
                        "max_columns": 2,
                        "representation": "source",
                        "live": True,
                    },
                    call_id="workspace-sheet-source",
                )
            ],
            [
                _structured_message(
                    {
                        "mode": "llm",
                        "summary": (
                            "A1 is the calculated source; B1 is a literal. "
                            "The exact value is 1.2344 and external review is required."
                        ),
                        "relevant_files": ["sheetSynthetic"],
                        "recommended_target": "sheetSynthetic",
                        "decision": decision,
                    }
                )
            ],
        ]
    )
    agent = build_google_workspace_context_agent(
        request_text=prompt,
        manual_plan=plan,
        tool_tier="core_read",
    )

    result = run_typed_sdk_agent(
        agent=agent,
        typed_input=prompt,
        output_type=GoogleWorkspaceContextResult,
        run_config=build_local_run_config(FakeProvider(model)),
        decision_contract=context_agent_decision_contract(
            "google_workspace_context_agent",
            provider_candidate_ids=("sheetSynthetic",),
        ),
    )

    tool_output = json.loads(
        next(
            item["output"]
            for item in reversed(model.calls[1]["input"])
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        )
    )
    cells = {cell["coordinate"]: cell for cell in tool_output["cells"]}
    assert tool_output["requested_range"] == "'Evidence'!A1:B1"
    assert tool_output["resolved_range"] == "'Evidence'!A1:B1"
    assert tool_output["source_identity"]["locale"] == "en_US"
    assert cells["A1"]["formula_provenance"] == "formula"
    assert cells["A1"]["effective"]["value"] == 1.2344
    assert cells["A1"]["display"] == "$1.23"
    assert "NOT approved" in cells["A1"]["note"]
    assert cells["A1"]["links"] == ["https://example.com/source"]
    assert cells["B1"]["formula_provenance"] == "literal"
    assert result.output.decision.selected_candidate_ids == ["sheetSynthetic"]
    assert result.request_cache["decision_ownership"]["validator_outcome"]["status"] == ("accepted")


def test_google_sheet_long_value_continuations_reach_actual_specialist_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Reply:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def execute(self) -> dict[str, Any]:
            return self.payload

    long_value = "Source qualification. " * 55 + "NOT approved for external use."

    class SheetsService:
        def spreadsheets(self) -> SheetsService:
            return self

        def values(self) -> SheetsService:
            return self

        def get(self, **kwargs: Any) -> Reply:
            if kwargs.get("includeGridData") is True:
                return Reply(
                    {
                        "spreadsheetId": "sheetSynthetic",
                        "sheets": [
                            {
                                "properties": {"sheetId": 7, "title": "Evidence"},
                                "data": [
                                    {
                                        "startRow": 1,
                                        "startColumn": 1,
                                        "rowData": [
                                            {
                                                "values": [
                                                    {
                                                        "userEnteredValue": {
                                                            "stringValue": long_value
                                                        },
                                                        "effectiveValue": {
                                                            "stringValue": long_value
                                                        },
                                                        "formattedValue": long_value,
                                                    }
                                                ]
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                    }
                )
            return Reply(
                {
                    "spreadsheetId": "sheetSynthetic",
                    "properties": {
                        "title": "Evidence workbook",
                        "locale": "en_US",
                        "timeZone": "America/New_York",
                    },
                    "sheets": [
                        {
                            "properties": {
                                "sheetId": 7,
                                "title": "Evidence",
                                "gridProperties": {"rowCount": 2, "columnCount": 2},
                            }
                        }
                    ],
                    "namedRanges": [],
                }
            )

    service = SheetsService()
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": object(), "sheets": service},
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_configured_google_account",
        lambda *_: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_google_sheet_under_kniops",
        lambda *_: None,
    )
    prompt = (
        "After paging to B2, read its complete exact value and preserve its qualification. "
        "Do not write."
    )
    plan = ManualRequestPlan(
        source="canonical:test",
        target_agent="google_workspace_context_agent",
        intent="context_lookup",
        task_objective="context_lookup",
        provider_system="google_workspace",
        provider_operations=["read"],
        target_type="business_system_context",
        provider_action_steps=[{"operation": "read", "resource_type": "google_spreadsheet"}],
        ask_shape={"permission_state": "read_only"},
    )
    base_args = {
        "spreadsheet_id_or_url": "sheetSynthetic",
        "range_a1": "'Evidence'!A1:B2",
        "max_rows": 1,
        "max_columns": 1,
        "max_cells": 1,
        "row_start": 1,
        "column_start": 1,
        "row_band_start": 1,
        "row_band_rows": 1,
        "column_band_start": 1,
        "column_band_columns": 1,
        "representation": "source",
        "max_value_chars": 500,
        "live": True,
    }
    detail_args = {
        "spreadsheet_id_or_url": "sheetSynthetic",
        "range_a1": "'Evidence'!B2:B2",
        "max_rows": 1,
        "max_columns": 1,
        "max_cells": 1,
        "row_band_start": 0,
        "row_band_rows": 1,
        "column_band_start": 0,
        "column_band_columns": 1,
        "row_chunk_rows": 1,
        "representation": "source",
        "max_value_chars": 500,
        "live": True,
    }
    decision = {
        "decision_owner": "specialist_agent",
        "decision_stage": "workspace_artifact_selection",
        "selected_candidate_id": "sheetSynthetic",
        "candidate_assessments": [
            {
                "candidate_id": "sheetSynthetic",
                "disposition": "selected",
                "rationale": "The exact requested spreadsheet supplied all value windows.",
            }
        ],
        "reasoning": "Selected the exact spreadsheet after reading every value window.",
        "limitations": ["Synthetic provider response; no write was attempted."],
        "needs_more_context": False,
    }
    model = FakeModel(
        outputs=[
            [_tool_call("google_sheet_read_table", base_args, call_id="sheet-long-0")],
            [
                _tool_call(
                    "google_sheet_read_table",
                    {**detail_args, "value_start_char": 500},
                    call_id="sheet-long-500",
                )
            ],
            [
                _tool_call(
                    "google_sheet_read_table",
                    {**detail_args, "value_start_char": 1000},
                    call_id="sheet-long-1000",
                )
            ],
            [
                _structured_message(
                    {
                        "mode": "llm",
                        "summary": "Read the complete value; external use is not approved.",
                        "relevant_files": ["sheetSynthetic"],
                        "recommended_target": "sheetSynthetic",
                        "decision": decision,
                    }
                )
            ],
        ]
    )
    agent = build_google_workspace_context_agent(
        request_text=prompt,
        manual_plan=plan,
        tool_tier="core_read",
    )

    result = run_typed_sdk_agent(
        agent=agent,
        typed_input=prompt,
        output_type=GoogleWorkspaceContextResult,
        run_config=build_local_run_config(FakeProvider(model)),
        decision_contract=context_agent_decision_contract(
            "google_workspace_context_agent",
            provider_candidate_ids=("sheetSynthetic",),
        ),
    )

    pages = [
        json.loads(
            next(
                item["output"]
                for item in reversed(model.calls[index]["input"])
                if isinstance(item, dict) and item.get("type") == "function_call_output"
            )
        )
        for index in (1, 2, 3)
    ]
    assert "".join(page["cells"][0]["display"] for page in pages) == long_value
    assert [
        page["cells"][0]["value_coverage"]["requested_start"] for page in pages
    ] == [0, 500, 1000]
    assert pages[0]["semantic_continuations"][0]["kind"] == "cell_value"
    assert pages[0]["semantic_continuations"][0]["next_request"]["range_a1"] == (
        "'Evidence'!B2:B2"
    )
    assert pages[0]["cells"][0]["coordinate"] == "B2"
    assert pages[-1]["semantic_complete"] is True
    assert result.output.decision.selected_candidate_ids == ["sheetSynthetic"]


@pytest.mark.parametrize(
    "route",
    [
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
    ],
)
def test_context_agent_fabricated_identity_is_rejected_without_substitution(
    route: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _configure_case(monkeypatch, route)
    fabricated_id = f"fabricated-{route}"
    candidate_ids = (case.actual_id, case.alternative_id, fabricated_id)
    invalid_payload = _output_payload(
        case,
        selected_id=fabricated_id,
        candidate_ids=candidate_ids,
    )
    model = FakeModel(
        outputs=[
            *[[tool_call] for tool_call in case.tool_calls],
            [_structured_message(invalid_payload)],
            [_structured_message(invalid_payload)],
        ]
    )

    with pytest.raises(AgentDecisionValidationError) as exc_info:
        _run_case(case, model)

    first_selection_input = _model_input_text(model.calls[len(case.tool_calls)]["input"])
    assert case.actual_id in first_selection_input
    assert case.alternative_id in first_selection_input
    assert fabricated_id not in first_selection_input
    metadata = sdk_run_failure_metadata(exc_info.value)
    decision = metadata["request_cache"]["decision_ownership"]
    assert decision["attempt_count"] == 2
    assert decision["selected_candidate_ids"] == [fabricated_id]
    assert decision["validator_outcome"]["status"] == "repair_required"
    assert decision["validator_outcome"]["reason_code"] == (
        "selected_identity_not_in_candidate_set"
    )
    repair_evidence = decision["repair_evidence"]
    assert set(repair_evidence["candidate_ids"]) == {
        case.actual_id,
        case.alternative_id,
    }
    assert fabricated_id not in repair_evidence["candidate_ids"]
    assert repair_evidence["provider_calls_during_repair"] == 0
    assert all(
        tool_name not in model.calls[-1]["tool_names"]
        for tool_name in repair_evidence["disabled_read_tool_names"]
    )
    assert all(
        attempt["selected_candidate_ids"] == [fabricated_id] for attempt in decision["attempts"]
    )
    receipt_fingerprints = {
        fingerprint
        for receipt in metadata["tool_receipts"]
        for fingerprint in receipt.get("identity_fingerprints", [])
    }
    assert identity_fingerprint(case.actual_id) in receipt_fingerprints
    assert identity_fingerprint(fabricated_id) not in receipt_fingerprints


@pytest.mark.parametrize(
    "route",
    [
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
    ],
)
def test_provider_candidate_universe_cannot_be_narrowed_by_model_output(
    route: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _configure_case(monkeypatch, route)
    provider_candidates = (case.actual_id, case.alternative_id)
    payload = _output_payload(
        case,
        selected_id=case.actual_id,
        candidate_ids=provider_candidates,
    )
    if route == "airtable_context_agent":
        payload["candidate_record_ids"] = [case.actual_id]
    elif route == "google_workspace_context_agent":
        payload["relevant_files"] = [case.actual_id]
    else:
        payload["source_ids"] = []
    model = FakeModel(
        outputs=[
            *[[tool_call] for tool_call in case.tool_calls],
            [_structured_message(payload)],
        ]
    )

    result = _run_case(case, model)

    decision = result.request_cache["decision_ownership"]
    assert decision["validator_outcome"]["status"] == "accepted"
    assert set(decision["candidate_ids"]) == set(provider_candidates)


@pytest.mark.parametrize(
    "route",
    ["airtable_context_agent", "google_workspace_context_agent"],
)
def test_recommended_identity_must_match_agent_selection_and_provider_evidence(
    route: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    case = _configure_case(monkeypatch, route)
    provider_candidates = (case.actual_id, case.alternative_id)
    payload = _output_payload(
        case,
        selected_id=case.actual_id,
        candidate_ids=provider_candidates,
    )
    fabricated = f"fabricated-recommendation-{route}"
    if route == "airtable_context_agent":
        payload["candidate_record_ids"] = [case.actual_id]
        payload["recommended_record_identity"] = fabricated
    else:
        payload["relevant_files"] = [case.actual_id]
        payload["recommended_target"] = fabricated
    model = FakeModel(
        outputs=[
            *[[tool_call] for tool_call in case.tool_calls],
            [_structured_message(payload)],
            [_structured_message(payload)],
        ]
    )

    with pytest.raises(AgentDecisionValidationError) as exc_info:
        _run_case(case, model)

    metadata = sdk_run_failure_metadata(exc_info.value)
    outcome = metadata["request_cache"]["decision_ownership"]["validator_outcome"]
    assert outcome["status"] == "repair_required"
    assert outcome["reason_code"] == "output_identity_not_owned_by_decision"
