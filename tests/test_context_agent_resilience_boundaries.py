from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from keystone_agents.agents.orchestrator import OrchestratorPreflight
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.planning.compatibility import infer_manual_request_plan
from keystone_agents.receipts.mutations import operation_is_mutation
from keystone_agents.receipts.normalization import identity_fingerprints
from keystone_agents.schemas.decision_ownership import AgentDecisionRecord
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.orchestrator import OrchestratorResult
from keystone_agents.sdk import agent_with_isolated_tool_state
from keystone_agents.tools import internal_data_tools


def _sdk_tool_result(*pairs: tuple[str, str, dict[str, object]]) -> SimpleNamespace:
    items: list[SimpleNamespace] = []
    for call_id, tool_name, output in pairs:
        items.extend(
            [
                SimpleNamespace(
                    type="tool_call_item",
                    call_id=call_id,
                    tool_name=tool_name,
                ),
                SimpleNamespace(
                    type="tool_call_output_item",
                    call_id=call_id,
                    output=json.dumps(output),
                ),
            ]
        )
    return SimpleNamespace(usage=None, new_items=items)


def test_run_local_tool_state_isolation_preserves_shared_tools_and_predicates() -> None:
    shared_sdk_tool = internal_data_tools.airtable_read_records.sdk_tool

    def enable_predicate(*_args: object) -> bool:
        return True

    shared_fake_tool = SimpleNamespace(
        name="fake_provider_read",
        is_enabled=enable_predicate,
        tool_input_guardrails=[],
        tool_output_guardrails=[],
    )
    original = SimpleNamespace(tools=[shared_sdk_tool, shared_fake_tool])

    isolated = agent_with_isolated_tool_state(original)

    assert isolated is not original
    assert isolated.tools[0] is not shared_sdk_tool
    assert isolated.tools[1] is not shared_fake_tool
    assert isolated.tools[1].is_enabled is enable_predicate
    assert isolated.tools[1].tool_input_guardrails is not shared_fake_tool.tool_input_guardrails
    isolated.tools[0].is_enabled = False
    isolated.tools[1].tool_input_guardrails.append("run-local")
    assert shared_sdk_tool.is_enabled is not False
    assert shared_fake_tool.tool_input_guardrails == []


def _airtable_decision(*candidate_ids: str) -> AgentDecisionRecord:
    return AgentDecisionRecord(
        decision_stage="airtable_record_selection",
        selected_candidate_ids=list(candidate_ids),
        candidate_assessments=[
            {
                "candidate_id": candidate_id,
                "disposition": "selected",
                "rationale": "This verified provider record answers the bounded request.",
            }
            for candidate_id in candidate_ids
        ],
        reasoning=(
            "The verified provider records were selected."
            if candidate_ids
            else "No provider record was selected before evidence was available."
        ),
    )


def _provider_decision(
    decision_stage: str,
    *candidate_ids: str,
) -> AgentDecisionRecord:
    return AgentDecisionRecord(
        decision_stage=decision_stage,
        selected_candidate_ids=list(candidate_ids),
        candidate_assessments=[
            {
                "candidate_id": candidate_id,
                "disposition": "selected",
                "rationale": "This verified provider object answers the bounded request.",
            }
            for candidate_id in candidate_ids
        ],
        reasoning=(
            "The verified provider object was selected."
            if candidate_ids
            else "No provider object was selected before evidence was available."
        ),
    )


class _Request:
    def __init__(self, payload: object = None, *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error

    def execute(self) -> object:
        if self.error is not None:
            raise self.error
        return self.payload


class _FolderFiles:
    def __init__(
        self,
        *,
        readback: dict[str, object] | None = None,
        update_payload: dict[str, object] | None = None,
        readback_error: Exception | None = None,
    ) -> None:
        self.readback = readback or {}
        self.update_payload = update_payload or {}
        self.readback_error = readback_error
        self.updated: list[dict[str, object]] = []

    def get(self, **_kwargs: object) -> _Request:
        return _Request(self.readback, error=self.readback_error)

    def update(self, **kwargs: object) -> _Request:
        self.updated.append(dict(kwargs))
        return _Request(self.update_payload)


class _Drive:
    def __init__(self, files: _FolderFiles) -> None:
        self._files = files

    def files(self) -> _FolderFiles:
        return self._files


def _allow_folder_write(
    monkeypatch: pytest.MonkeyPatch,
    files: _FolderFiles,
) -> None:
    monkeypatch.setattr(
        internal_data_tools,
        "_require_google_workspace_write_approval",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": _Drive(files)},
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_configured_google_account",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_assert_drive_file_under_folder",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_ensure_drive_folder_path",
        lambda *_args, **_kwargs: "folder-kba",
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_resolve_drive_folder_id",
        lambda *_args, **_kwargs: "folder-kba",
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_list_drive_folder_children",
        lambda *_args, **_kwargs: [],
    )


def test_drive_rename_is_a_mutation_and_requires_a_write_guardrail() -> None:
    assert operation_is_mutation("rename_folder") is True
    assert cli._direct_context_tool_is_mutation("google_drive_rename_folder", {}) is True


def test_drive_create_folder_returns_durable_readback_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _FolderFiles(
        readback={
            "id": "folder-kba",
            "name": "Forecasts",
            "mimeType": internal_data_tools.GOOGLE_FOLDER_MIME_TYPE,
            "trashed": False,
            "webViewLink": "https://drive.google.test/folder-kba",
        }
    )
    _allow_folder_write(monkeypatch, files)
    monkeypatch.setattr(
        internal_data_tools,
        "_find_drive_folder_path",
        lambda *_args, **_kwargs: "",
    )

    result = internal_data_tools.google_drive_create_folder_impl(
        "KNIOps/Forecasts",
        approval_reference="approval:test",
        live=True,
    )

    assert result["status"] == "success"
    assert result["operation"] == "create_folder"
    assert result["provider_write"] is True
    assert result["verification"]["passed"] is True
    assert result["verification"]["folder_id_match"] is True


def test_drive_rename_and_remove_use_separate_provider_readback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rename_files = _FolderFiles(
        readback={
            "id": "folder-kba",
            "name": "Forecasts 2027",
            "mimeType": internal_data_tools.GOOGLE_FOLDER_MIME_TYPE,
            "trashed": False,
            "webViewLink": "https://drive.google.test/folder-kba",
        },
        update_payload={"id": "folder-kba", "name": "Forecasts 2027"},
    )
    _allow_folder_write(monkeypatch, rename_files)
    monkeypatch.setattr(
        internal_data_tools,
        "_ensure_drive_folder_path",
        lambda *_args, **_kwargs: "root-kba",
    )

    renamed = internal_data_tools.google_drive_rename_folder_impl(
        "folder-kba",
        "Forecasts 2027",
        approval_reference="approval:test",
        live=True,
    )

    assert renamed["operation"] == "rename_folder"
    assert renamed["provider_write"] is True
    assert renamed["verification"]["passed"] is True

    remove_files = _FolderFiles(
        readback={
            "id": "folder-kba",
            "name": "Forecasts 2027",
            "mimeType": internal_data_tools.GOOGLE_FOLDER_MIME_TYPE,
            "trashed": True,
            "webViewLink": "https://drive.google.test/folder-kba",
        },
        update_payload={
            "id": "folder-kba",
            "name": "Forecasts 2027",
            "trashed": True,
        },
    )
    _allow_folder_write(monkeypatch, remove_files)
    monkeypatch.setattr(
        internal_data_tools,
        "_ensure_drive_folder_path",
        lambda *_args, **_kwargs: "root-kba",
    )

    removed = internal_data_tools.google_drive_remove_folder_impl(
        "folder-kba",
        approval_reference="approval:test",
        live=True,
    )

    assert removed["operation"] == "remove_folder"
    assert removed["provider_write"] is True
    assert removed["verification"]["passed"] is True
    assert removed["verification"]["trashed_match"] is True


def test_drive_mutation_with_missing_readback_is_not_reported_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = _FolderFiles(
        update_payload={"id": "folder-kba", "name": "Forecasts 2027"},
        readback_error=RuntimeError("synthetic readback outage"),
    )
    _allow_folder_write(monkeypatch, files)
    monkeypatch.setattr(
        internal_data_tools,
        "_ensure_drive_folder_path",
        lambda *_args, **_kwargs: "root-kba",
    )

    result = internal_data_tools.google_drive_rename_folder_impl(
        "folder-kba",
        "Forecasts 2027",
        approval_reference="approval:test",
        live=True,
    )

    assert result["status"] == "verification_failed"
    assert result["provider_write"] is True
    assert result["verification"]["passed"] is False
    assert cli._context_agent_unverified_write_blocker([result])


def test_invocation_without_receipt_disables_mutation_during_repair() -> None:
    mutation_tool = SimpleNamespace(
        name="google_drive_rename_folder",
        is_enabled=True,
    )
    read_tool = SimpleNamespace(name="google_drive_search_files", is_enabled=True)
    agent = SimpleNamespace(tools=[mutation_tool, read_tool])
    invocations = [
        {
            "tool_name": "google_drive_rename_folder",
            "invocation_index": 1,
            "status": "started",
        }
    ]

    changed = cli._disable_direct_context_mutation_tools(
        agent,
        [],
        invocations=invocations,
    )

    assert mutation_tool.is_enabled is False
    assert read_tool.is_enabled is True
    assert changed == ((mutation_tool, True),)
    assert cli._context_agent_unverified_write_blocker(
        [],
        tool_invocations=invocations,
    )


def test_private_orchestrator_decision_is_specialist_context_not_authority() -> None:
    plan = ManualRequestPlan(
        source="canonical:test",
        target_agent="google_workspace_context_agent",
        intent="context_lookup",
        provider_system="google_workspace",
        provider_operations=["read", "search"],
        ask_shape={"permission_state": "read_only"},
    )
    preflight = OrchestratorPreflight(
        request_text="Find the latest scoped review document.",
        selected_agent="google_workspace_context_agent",
        manual_request_plan=plan,
        route_result=OrchestratorResult(
            route="google_workspace_context_agent",
            target_agent="google_workspace_context_agent",
            rationale="Workspace owns the bounded artifact selection.",
            approval_required=False,
            requires_human_review=False,
            decision=AgentDecisionRecord(
                decision_owner="orchestrator",
                decision_stage="orchestrator_route_selection",
                selected_candidate_id="google_workspace_context_agent",
                candidate_assessments=[
                    {
                        "candidate_id": "google_workspace_context_agent",
                        "disposition": "selected",
                        "rationale": "The ask requires scoped Drive evidence.",
                    }
                ],
                reasoning="Workspace owns the provider-dependent decision.",
            ),
        ),
    )

    context = cli._direct_context_orchestrator_decision_text(preflight)

    assert "Workspace owns the bounded artifact selection" in context
    assert "not provider-write authority" in context
    assert "do not copy route identities" in context.lower()
    assert "Workspace owns the provider-dependent decision" not in context
    assert '"decision_owner"' not in context
    assert '"send_enabled": false' in context


def test_schema_tool_postcondition_exists_even_if_admission_drops_the_tool() -> None:
    request = (
        "Which Business Expenses columns are editable, and which are formulas? "
        "Only inspect schema; do not open any rows or change anything."
    )

    contract = cli._context_agent_tool_execution_contract(
        "airtable_context_agent",
        input_text=request,
        selected_tool_names=[],
        manual_plan=infer_manual_request_plan(
            request,
            requested_agent="airtable_context_agent",
        ),
    )

    assert contract is not None
    assert [group.name for group in contract.required_groups] == ["airtable_schema"]
    assert contract.required_groups[0].any_of_tool_names == (
        "airtable_get_base_schema",
    )


def test_schema_only_postcondition_does_not_require_negated_record_read() -> None:
    request = (
        "Before I set up an expense entry, walk me through the Business Expenses "
        "table layout—what fields are manually entered, what is computed, and which "
        "fields Airtable controls. Please don't inspect any records or make changes."
    )

    contract = cli._context_agent_tool_execution_contract(
        "airtable_context_agent",
        input_text=request,
        selected_tool_names=[
            "airtable_get_base_schema",
            "airtable_read_records",
        ],
        manual_plan=infer_manual_request_plan(
            request,
            requested_agent="airtable_context_agent",
        ),
    )

    assert contract is not None
    assert [group.name for group in contract.required_groups] == ["airtable_schema"]


def test_missing_required_schema_tool_stops_before_model_execution(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = (
        "Which Business Expenses columns are editable, and which are formulas? "
        "Only inspect schema; do not open any rows or change anything."
    )
    plan = infer_manual_request_plan(
        request,
        requested_agent="airtable_context_agent",
    )
    monkeypatch.delenv(cli.AIRTABLE_LIVE_READS_ENV, raising=False)
    monkeypatch.delenv("KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE", raising=False)
    monkeypatch.delenv(cli.AIRTABLE_ALLOWED_OPERATION_ENV, raising=False)
    model_calls = 0

    def fail_if_called(*_args, **_kwargs):
        nonlocal model_calls
        model_calls += 1
        raise AssertionError("The model must not run without its required schema tool.")

    monkeypatch.setattr(
        cli,
        "tool_scope_receipt_for_agent",
        lambda _agent: {"selected_tool_names": []},
    )
    monkeypatch.setattr(cli, "run_typed_sdk_sync", fail_if_called)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_context_agent_live(
        "airtable_context_agent",
        request,
        json_output=True,
        manual_plan=plan,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert model_calls == 0
    assert payload["block_kind"] == "required_tool_admission_missing"
    assert payload["sdk_failure"]["attempt_count"] == 0
    assert payload["tool_execution"]["model_tool_call_count"] == 0
    assert os.environ.get(cli.AIRTABLE_LIVE_READS_ENV) is None
    assert os.environ.get("KEYSTONE_AIRTABLE_OPERATOR_APPROVAL_REFERENCE") is None
    assert os.environ.get(cli.AIRTABLE_ALLOWED_OPERATION_ENV) is None


def test_workspace_public_payload_replaces_raw_ids_with_stable_references() -> None:
    raw_id = "drive-private-object-123"
    output = {
        "summary": f"Use {raw_id} for this review.",
        "relevant_files": [raw_id],
        "recommended_target": raw_id,
        "decision": {
            "selected_candidate_id": raw_id,
            "selected_candidate_ids": [raw_id],
            "candidate_assessments": [
                {
                    "candidate_id": raw_id,
                    "disposition": "selected",
                    "rationale": "Current matching file.",
                }
            ],
        },
    }
    receipts = [
        {
            "status": "success",
            "operation": "search_files",
            "file_id": raw_id,
            "provider_link": f"https://drive.google.test/{raw_id}",
        }
    ]

    public_output, public_receipts = cli._context_agent_public_payload(
        "google_workspace_context_agent",
        output,
        receipts,
    )

    assert raw_id not in str(public_output)
    assert raw_id not in {
        str(value)
        for receipt in public_receipts
        for key, value in receipt.items()
        if key != "provider_link"
    }
    assert public_receipts[0]["provider_identity_fingerprints"]


def test_usage_aggregation_includes_initial_and_repair_attempts() -> None:
    first = SimpleNamespace(
        usage=SimpleNamespace(
            requests=1,
            input_tokens=100,
            output_tokens=20,
            total_tokens=120,
            input_tokens_details=SimpleNamespace(cached_tokens=10),
            output_tokens_details=SimpleNamespace(reasoning_tokens=4),
        )
    )
    repair = SimpleNamespace(
        usage=SimpleNamespace(
            requests=1,
            input_tokens=80,
            output_tokens=15,
            total_tokens=95,
            input_tokens_details=SimpleNamespace(cached_tokens=20),
            output_tokens_details=SimpleNamespace(reasoning_tokens=3),
        )
    )

    usage = cli._aggregate_direct_context_usage([first, repair])

    assert usage["complete"] is True
    assert usage["attempt_count"] == 2
    assert usage["requests"] == 2
    assert usage["input_tokens"] == 180
    assert usage["output_tokens"] == 35
    assert usage["total_tokens"] == 215
    assert usage["reasoning_output_tokens"] == 7


def test_direct_context_missing_required_tools_gets_one_bounded_correction(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = "List the newest Airtable records and show their available fields."
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="airtable_context_agent",
        target_agent="airtable_context_agent",
        intent="context_lookup",
        provider_system="airtable",
        provider_operations=["read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        primary_target="Recent records",
        objective=request,
        ask_shape={"permission_state": "read_only"},
    )
    prompts: list[str] = []

    def fake_sdk(_agent, prompt, _output_type, **_kwargs):
        prompts.append(str(prompt))
        if len(prompts) == 1:
            return (
                _sdk_tool_result(),
                cli.AirtableContextResult(
                    summary="I need current Airtable evidence.",
                    decision=_airtable_decision(),
                ),
            )
        return (
            _sdk_tool_result(
                (
                    "call-schema",
                    "airtable_get_base_schema",
                    {
                        "status": "success",
                        "operation": "read_schema",
                        "provider": "airtable",
                        "provider_read": True,
                    },
                ),
                (
                    "call-records",
                    "airtable_read_records",
                    {
                        "status": "success",
                        "operation": "read_records",
                        "provider": "airtable",
                        "provider_read": True,
                        "records": [{"record_id": "rec-verified"}],
                    },
                ),
            ),
            cli.AirtableContextResult(
                summary="The newest verified Airtable record is available.",
                candidate_record_ids=["rec-verified"],
                decision=_airtable_decision("rec-verified"),
            ),
        )

    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_sdk)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_context_agent_live(
        "airtable_context_agent",
        request,
        json_output=True,
        manual_plan=plan,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(prompts) == 2
    assert prompts[1].startswith(request)
    assert "Bounded tool-execution correction" in prompts[1]
    correction = payload["request_cache"]["tool_execution_correction"]
    assert correction["attempted"] is True
    assert correction["terminal_postcondition"]["satisfied"] is True
    assert payload["tool_execution"]["postcondition"]["satisfied"] is True


@pytest.mark.parametrize(
    (
        "route",
        "operator_request",
        "provider_system",
        "completed_tool_name",
        "correction_tool_name",
        "decision_stage",
        "candidate_id",
    ),
    [
        (
            "airtable_context_agent",
            (
                "Count the newest Airtable records and list their record names. "
                "Do not change Airtable."
            ),
            "airtable",
            "airtable_read_records",
            "airtable_aggregate_records",
            "airtable_record_selection",
            "rec-current-private",
        ),
        (
            "google_workspace_context_agent",
            (
                "Find the latest quarterly review file in Drive and show its file name, "
                "owner, modified time, and MIME type. Do not change Workspace."
            ),
            "google_workspace",
            "google_drive_search_files",
            "google_drive_get_file_metadata",
            "workspace_artifact_selection",
            "drive-current-private",
        ),
        (
            "zotero_context_agent",
            (
                "Find the latest Zotero article about outcome measurement and show its "
                "title, authors, year, DOI, publication, and abstract status. Do not edit "
                "Zotero."
            ),
            "zotero",
            "zotero_resolve_article_context",
            "zotero_read_api_metadata",
            "zotero_item_selection",
            "ZOTERO-CURRENT-PRIVATE",
        ),
    ],
)
def test_direct_context_tool_correction_then_decision_repair_replays_all_reads(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    route: str,
    operator_request: str,
    provider_system: str,
    completed_tool_name: str,
    correction_tool_name: str,
    decision_stage: str,
    candidate_id: str,
) -> None:
    plan = ManualRequestPlan(
        source="llm",
        requested_agent=route,
        target_agent=route,
        intent="context_lookup",
        provider_system=provider_system,
        provider_operations=["read", "search"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        target_type=(
            "zotero_article"
            if route == "zotero_context_agent"
            else "business_system_context"
        ),
        primary_target="Current evidence object",
        objective=operator_request,
        ask_shape={"permission_state": "read_only"},
    )
    prompts: list[str] = []
    enabled_tools_by_attempt: list[dict[str, bool]] = []
    model_called_tools: list[str] = []

    def context_output(
        *,
        summary: str,
        decision: AgentDecisionRecord,
    ) -> object:
        if route == "airtable_context_agent":
            return cli.AirtableContextResult(
                summary=summary,
                candidate_record_ids=[candidate_id],
                decision=decision,
            )
        if route == "google_workspace_context_agent":
            return cli.GoogleWorkspaceContextResult(
                summary=summary,
                relevant_files=[candidate_id],
                recommended_target=candidate_id,
                decision=decision,
            )
        return cli.ZoteroContextResult(
            summary=summary,
            library_context="Synthetic read-only Zotero metadata.",
            article_titles=["Current outcome measurement review"],
            zotero_item_keys=[candidate_id],
            decision=decision,
        )

    def fake_sdk(agent, prompt, _output_type, **_kwargs):
        prompts.append(str(prompt))
        enabled_tools_by_attempt.append(
            {
                str(getattr(tool, "name", "")): getattr(tool, "is_enabled", True)
                is not False
                for tool in list(getattr(agent, "tools", []) or [])
            }
        )
        assert not any(
            operation_is_mutation(tool_name)
            for tool_name in enabled_tools_by_attempt[-1]
        )
        if len(prompts) == 1:
            model_called_tools.append(completed_tool_name)
            first_output = context_output(
                summary="The current provider object was found; one more read is needed.",
                decision=_provider_decision(decision_stage, candidate_id),
            )
            candidate_row = {
                (
                    "record_id"
                    if route == "airtable_context_agent"
                    else "id"
                    if route == "google_workspace_context_agent"
                    else "item_key"
                ): candidate_id,
                "title": "CURRENT-CANDIDATE-DESCRIPTION",
            }
            provider_payload: dict[str, object] = {
                "status": "success",
                "provider": provider_system,
                "provider_read": True,
                "identity_fingerprints": identity_fingerprints([candidate_id]),
                **(
                    {"record_id": candidate_id}
                    if route == "airtable_context_agent"
                    else {}
                ),
                (
                    "records"
                    if route == "airtable_context_agent"
                    else "items"
                    if route == "google_workspace_context_agent"
                    else "results"
                ): [
                    candidate_row
                ],
            }
            return (
                _sdk_tool_result(
                    ("call-completed-read", completed_tool_name, provider_payload)
                ),
                first_output,
            )

        if len(prompts) == 2:
            assert enabled_tools_by_attempt[-1][completed_tool_name] is False
            assert enabled_tools_by_attempt[-1][correction_tool_name] is True
            model_called_tools.append(correction_tool_name)
            provider_payload = {
                "status": "success",
                "provider": provider_system,
                "provider_read": True,
                "identity_fingerprints": identity_fingerprints([candidate_id]),
                **(
                    {"count": 1}
                    if route == "airtable_context_agent"
                    else {
                        (
                            "file_id"
                            if route == "google_workspace_context_agent"
                            else "item_key"
                        ): candidate_id
                    }
                ),
            }
            invalid_decision = _provider_decision(
                decision_stage,
                candidate_id,
            ).model_copy(update={"decision_owner": "orchestrator"})
            return (
                _sdk_tool_result(
                    ("call-missing-read", correction_tool_name, provider_payload)
                ),
                context_output(
                    summary="Both required reads completed, but ownership is invalid.",
                    decision=invalid_decision,
                ),
            )

        assert enabled_tools_by_attempt[-1][completed_tool_name] is False
        assert enabled_tools_by_attempt[-1][correction_tool_name] is False
        return (
            _sdk_tool_result(),
            context_output(
                summary="The current provider object was verified after bounded repair.",
                decision=_provider_decision(decision_stage, candidate_id),
            ),
        )

    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_sdk)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_context_agent_live(
        route,
        operator_request,
        json_output=True,
        manual_plan=plan,
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(prompts) == 3
    assert prompts[1].startswith(operator_request)
    assert "Bounded tool-execution correction" in prompts[1]
    assert "Sanitized decision evidence replay" in prompts[2]
    assert "CURRENT-CANDIDATE-DESCRIPTION" in prompts[2]
    assert model_called_tools == [completed_tool_name, correction_tool_name]
    correction = payload["request_cache"]["tool_execution_correction"]
    assert correction["attempted"] is True
    assert correction["completed_read_tool_names"] == [completed_tool_name]
    assert correction["disabled_completed_tool_names"] == [completed_tool_name]
    assert correction["observed_mutation_tool_names"] == []
    assert correction["evidence_replay"]["provider_calls_during_repair"] == 0
    assert correction["terminal_postcondition"]["satisfied"] is True
    assert payload["tool_execution"]["postcondition"]["satisfied"] is True
    assert payload["tool_execution"]["model_called_tool_names"] == model_called_tools
    repair = payload["request_cache"]["decision_ownership"]["repair_evidence"]
    assert repair["source_tool_call_count"] == 2
    assert set(repair["replayed_tool_names"]) == {
        completed_tool_name,
        correction_tool_name,
    }
    assert repair["provider_calls_during_repair"] == 0
    assert payload["request_cache"]["decision_ownership"]["attempt_count"] == 2
    assert payload["external_write_state"] == "not_performed"


def test_direct_context_required_tool_correction_exhaustion_is_truthful(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    request = "List the newest Airtable records and show their available fields."
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="airtable_context_agent",
        target_agent="airtable_context_agent",
        intent="context_lookup",
        provider_system="airtable",
        provider_operations=["read"],
        provider_read_scope="bounded_collection",
        provider_result_mode="items",
        primary_target="Recent records",
        objective=request,
        ask_shape={"permission_state": "read_only"},
    )
    calls = 0

    def fake_sdk(_agent, _prompt, _output_type, **_kwargs):
        nonlocal calls
        calls += 1
        return (
            _sdk_tool_result(),
            cli.AirtableContextResult(
                summary="No provider evidence was retrieved.",
                decision=_airtable_decision(),
            ),
        )

    monkeypatch.setattr(cli, "run_typed_sdk_sync", fake_sdk)
    monkeypatch.setattr(cli.SQLiteStore, "save_agent_run", lambda *_args, **_kwargs: 1)

    exit_code = cli._run_ask_context_agent_live(
        "airtable_context_agent",
        request,
        json_output=True,
        manual_plan=plan,
    )

    assert exit_code == 1
    payload = json.loads(capsys.readouterr().out)
    assert calls == 2
    assert payload["status"] == "failed"
    assert payload["block_kind"] == "required_tool_execution_missing"
    assert payload["sdk_failure"]["attempt_count"] == 2
    assert payload["sdk_failure"]["request_cache"]["tool_execution_correction"][
        "terminal_postcondition"
    ]["satisfied"] is False
    assert "after one bounded correction attempt" in payload["human_summary"]


def test_direct_context_tool_correction_disables_completed_reads_and_mutations() -> None:
    completed_read = SimpleNamespace(name="airtable_read_records", is_enabled=True)
    missing_read = SimpleNamespace(name="airtable_get_base_schema", is_enabled=True)
    mutation = SimpleNamespace(name="airtable_write_record", is_enabled=True)
    agent = SimpleNamespace(tools=[completed_read, missing_read, mutation])
    raw_result = _sdk_tool_result(
        (
            "call-read",
            "airtable_read_records",
            {
                "status": "success",
                "operation": "read_records",
                "provider": "airtable",
                "provider_read": True,
                "records": [],
            },
        ),
        (
            "call-write",
            "airtable_write_record",
            {
                "status": "success",
                "operation": "update",
                "provider": "airtable",
                "provider_write": True,
                "record_id": "rec-verified",
                "verification": {"passed": True},
            },
        ),
    )

    changed, completed_reads, observed_mutations = (
        cli._disable_direct_context_completed_tools_for_correction(
            agent,
            raw_result,
            receipts=[
                {
                    "tool_name": "airtable_write_record",
                    "status": "success",
                    "operation": "update",
                    "provider_write": True,
                }
            ],
            invocations=[],
        )
    )

    assert completed_reads == ("airtable_read_records",)
    assert observed_mutations == ("airtable_write_record",)
    assert completed_read.is_enabled is False
    assert mutation.is_enabled is False
    assert missing_read.is_enabled is True
    cli._restore_direct_context_tools(changed)
    assert completed_read.is_enabled is True
    assert mutation.is_enabled is True
