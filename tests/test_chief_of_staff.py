from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import keystone_agents.agents.chief_of_staff as chief_of_staff_module
import keystone_agents.local_kni_evidence as local_kni_evidence
import keystone_agents.tools.internal_data_tools as internal_data_tools
from keystone_agents.agent_registry import AGENT_REGISTRY
from keystone_agents.agent_tool_policy import disallowed_tool_names, tool_policy_for_agent
from keystone_agents.agents.chief_of_staff import (
    build_chief_of_staff_agent,
    plan_chief_of_staff_request,
    run_chief_of_staff_sdk,
)
from keystone_agents.memory import chief_of_staff_memory_item
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.orchestrator.preflight_context import (
    MANUAL_REQUEST_PLAN_ENV,
    ORCHESTRATOR_PREFLIGHT_ENV,
)
from keystone_agents.quality_budget import QualityMode, chief_of_staff_quality_budget
from keystone_agents.schemas.airtable import airtable_base_schema_summary_from_metadata
from keystone_agents.schemas.chief_of_staff import (
    ChiefOfStaffResult,
    ChiefOfStaffRouteRecommendation,
    ChiefOfStaffSourceRef,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.chief_of_staff_tool import (
    list_chief_of_staff_context_sources,
    lookup_slack_workflow_capability,
    read_slack_repo_context_file,
    search_official_operations_docs,
    search_slack_repo_context,
    summarize_slack_runtime_config,
)
from keystone_agents.tools.internal_data_tools import (
    airtable_get_base_schema_impl,
    airtable_read_records_impl,
    airtable_write_record_impl,
    explicit_full_article_read_requested,
    google_doc_read_impl,
    google_doc_write_impl,
    google_drive_create_folder_impl,
    google_drive_list_folder_impl,
    google_drive_remove_folder_impl,
    google_drive_rename_folder_impl,
    google_sheet_append_rows_impl,
    google_sheet_create_impl,
    google_sheet_create_tab_impl,
    google_sheet_delete_rows_impl,
    google_sheet_list_impl,
    google_sheet_read_table_impl,
    google_sheet_remove_tab_impl,
    google_sheet_trash_impl,
    google_sheet_update_row_impl,
    google_sheet_update_tab_impl,
    read_linked_article_impl,
)
from keystone_agents.tools.website_extraction_tool import WebsiteExtractionError
from keystone_agents.visible_sources import append_visible_source_urls_to_output


def _payload(raw: str) -> dict[str, object]:
    return json.loads(raw)


def _load_run_chief_of_staff_script() -> object:
    script_path = Path(__file__).resolve().parents[1] / "scripts" / "run_chief_of_staff.py"
    spec = importlib.util.spec_from_file_location("run_chief_of_staff_script", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_chief_of_staff_builder_matches_schema_and_policy() -> None:
    agent = build_chief_of_staff_agent()
    tool_names = {getattr(tool, "name", "") for tool in agent.tools}

    assert agent.name == "chief_of_staff"
    assert agent.output_type is ChiefOfStaffResult
    assert "list_chief_of_staff_context_sources" in tool_names
    assert "summarize_slack_runtime_config" in tool_names
    assert "search_local_context" in tool_names
    assert "airtable_get_base_schema" in tool_names
    assert "airtable_read_records" in tool_names
    assert "airtable_write_record" in tool_names
    assert "google_doc_read" in tool_names
    assert "google_doc_write" in tool_names
    assert "google_drive_list_folder" in tool_names
    assert "google_drive_create_folder" in tool_names
    assert "google_drive_rename_folder" in tool_names
    assert "google_drive_remove_folder" in tool_names
    assert "google_sheet_list" in tool_names
    assert "google_sheet_create" in tool_names
    assert "google_sheet_read_table" in tool_names
    assert "google_sheet_append_rows" in tool_names
    assert "google_sheet_update_row" in tool_names
    assert "google_sheet_delete_rows" in tool_names
    assert "google_sheet_create_tab" in tool_names
    assert "google_sheet_update_tab" in tool_names
    assert "google_sheet_remove_tab" in tool_names
    assert "google_sheet_trash" in tool_names
    assert "read_linked_article" not in tool_names
    assert disallowed_tool_names("chief_of_staff", sorted(tool_names)) == []

    policy = tool_policy_for_agent("chief_of_staff")
    assert policy is not None
    assert "search_official_operations_docs" in policy.allowed_tool_names


def test_chief_of_staff_local_kni_document_request_skips_hosted_file_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_append_configured_file_search_tools(agent_name: str, tools: list[object]) -> list[object]:
        return [*tools, SimpleNamespace(name="file_search")]

    monkeypatch.setattr(
        chief_of_staff_module,
        "append_configured_file_search_tools",
        fake_append_configured_file_search_tools,
    )

    generic = build_chief_of_staff_agent(request_text="summarize Slack workflow docs")
    local_kni = build_chief_of_staff_agent(
        request_text=(
            "Using local KNI documents, what date was Keystone Neuroinformatics LLC formed? "
            "Return the date and evidence path."
        )
    )
    local_kni_formation_role = build_chief_of_staff_agent(
        request_text=(
            "Who formally organized Keystone Neuroinformatics LLC in Pennsylvania?"
        )
    )

    generic_tool_names = {getattr(tool, "name", "") for tool in generic.tools}
    local_kni_tool_names = {getattr(tool, "name", "") for tool in local_kni.tools}
    formation_role_tool_names = {
        getattr(tool, "name", "") for tool in local_kni_formation_role.tools
    }

    assert "file_search" in generic_tool_names
    assert "file_search" not in local_kni_tool_names
    assert "file_search" not in formation_role_tool_names
    assert "search_kni_documents" in local_kni_tool_names
    assert "read_kni_document_file" in local_kni_tool_names
    assert "search_kni_documents" in formation_role_tool_names
    assert "read_kni_document_file" in formation_role_tool_names


def test_chief_of_staff_local_kni_document_fallback_returns_evidence_packet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_search(query: str, *, max_results: int = 5) -> dict[str, object]:
        return {
            "status": "ready",
            "model_context_allowed": True,
            "blocked_result_count": 0,
            "matches": [
                {
                    "relative_path": (
                        "00_Admin/Formation/2-12-26-PA-FormationDocument-"
                        "Keystone Neuroinformatics LLC.pdf"
                    ),
                    "title": "PA FormationDocument Keystone Neuroinformatics LLC",
                    "snippet": "Certificate of Organization Date Filed: 2/6/2026",
                    "sensitivity_status": "allowed",
                    "review_required": False,
                    "review_reasons": [],
                }
            ],
            "refresh": {"attempted": False},
            "diagnostic": None,
        }

    def fake_read(relative_path: str, *, max_chars: int = 8_000) -> dict[str, object]:
        return {
            "relative_path": relative_path,
            "content": "Certificate of Organization\nDate Filed: 2/6/2026",
            "sensitivity_status": "allowed",
            "review_required": False,
            "review_reasons": [],
            "truncated": False,
            "local_only": True,
            "send_enabled": False,
        }

    monkeypatch.setattr(local_kni_evidence, "search_kni_documents_impl", fake_search)
    monkeypatch.setattr(local_kni_evidence, "read_kni_document_file_impl", fake_read)

    result = plan_chief_of_staff_request(
        (
            "Using local KNI documents, what date was Keystone Neuroinformatics LLC "
            "formed? Return the date, evidence path, and uncertainty if any."
        )
    )

    assert result.recommended_route.workflow_type == "project-context-review"
    assert "evidence is available for Chief of Staff synthesis" in result.summary
    assert "does not decide the substantive answer" in result.summary
    assert "00_Admin/Formation" in result.summary
    assert result.retrieval_diagnostics["local_only"] is True
    assert result.retrieval_diagnostics["effective_lookup_kind"] == "formation"
    assert any(
        "allowed_local_kni_document_lookup" in note for note in result.audit_notes
    )


def test_chief_of_staff_peo_insurance_question_uses_local_kni_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_search(query: str, *, max_results: int = 8) -> dict[str, object]:
        assert "insurance" in query.lower() or "peo" in query.lower()
        return {
            "status": "ready",
            "model_context_allowed": True,
            "blocked_result_count": 0,
            "matches": [
                {
                    "relative_path": "00_Admin/Insurance/InsurancePolicy/COI_AnupSharma_2026.pdf",
                    "title": "COI AnupSharma 2026",
                    "snippet": (
                        "CERTIFICATE OF LIABILITY INSURANCE PRODUCER IAO Inc dba "
                        "ProAssurance Agency INSURER A: CFC Underwriters"
                    ),
                    "sensitivity_status": "allowed",
                    "review_required": True,
                    "review_reasons": ["insurance_policy"],
                }
            ],
            "refresh": {"attempted": False},
            "diagnostic": None,
        }

    def fake_read(relative_path: str, *, max_chars: int = 12_000) -> dict[str, object]:
        return {
            "relative_path": relative_path,
            "content": (
                "CERTIFICATE OF LIABILITY INSURANCE CONTACT PRODUCER NAME: "
                "IAO Inc dba ProAssurance Agency INSURER(S) AFFORDING COVERAGE "
                "INSURER A : CFC Underwriters INSURED Keystone Neuroinformatics, LLC"
            ),
            "sensitivity_status": "allowed",
            "review_required": True,
            "review_reasons": ["insurance_policy"],
            "truncated": False,
            "local_only": True,
            "send_enabled": False,
        }

    monkeypatch.setattr(local_kni_evidence, "search_kni_documents_impl", fake_search)
    monkeypatch.setattr(local_kni_evidence, "read_kni_document_file_impl", fake_read)

    result = plan_chief_of_staff_request(
        "chief of staff who provides the PEO insurance for the company keystone neuroinformatics?"
    )

    assert result.recommended_route.workflow_type == "project-context-review"
    assert "evidence is available for Chief of Staff synthesis" in result.summary
    assert "does not decide the substantive answer" in result.summary
    assert "00_Admin/Insurance" in result.summary
    assert any("Review candidate evidence path: " in action for action in result.recommended_actions)
    assert result.retrieval_diagnostics["lookup_kind"] == "insurance"
    assert result.retrieval_diagnostics["effective_lookup_kind"] == "insurance"
    assert result.retrieval_diagnostics["local_only"] is True
    assert result.human_review_required is True
    assert any(
        "allowed_local_kni_document_lookup" in note for note in result.audit_notes
    )


def test_chief_of_staff_cfc_broker_question_prefers_proassurance_producer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_search(query: str, *, max_results: int = 8) -> dict[str, object]:
        assert "insurance" in query.lower() or "broker" in query.lower()
        return {
            "status": "ready",
            "model_context_allowed": True,
            "blocked_result_count": 0,
            "matches": [
                {
                    "relative_path": (
                        "00_Admin/Insurance/InsurancePolicy/"
                        "CFC POLICY stamped 042126.pdf"
                    ),
                    "title": "CFC policy stamped",
                    "snippet": "Insurance effected through the Coverholder: CFC Underwriting Limited",
                    "sensitivity_status": "allowed",
                    "review_required": True,
                    "review_reasons": ["insurance_policy"],
                },
                {
                    "relative_path": "00_Admin/Insurance/InsurancePolicy/COI_AnupSharma_2026.pdf",
                    "title": "COI AnupSharma 2026",
                    "snippet": (
                        "CERTIFICATE OF LIABILITY INSURANCE PRODUCER IAO, Inc. "
                        "DBA ProAssurance Agency INSURER A: CFC Underwriters"
                    ),
                    "sensitivity_status": "allowed",
                    "review_required": True,
                    "review_reasons": ["insurance_policy"],
                },
            ],
            "refresh": {"attempted": False},
            "diagnostic": None,
        }

    def fake_read(relative_path: str, *, max_chars: int = 12_000) -> dict[str, object]:
        if "COI_" in relative_path:
            content = (
                "CERTIFICATE OF LIABILITY INSURANCE PRODUCER IAO, Inc. DBA "
                "ProAssurance Agency INSURER A : CFC Underwriters INSURED "
                "Keystone Neuroinformatics, LLC"
            )
        else:
            content = (
                "Insurance effected through the Coverholder: CFC Underwriting Limited "
                "This policy is issued to Keystone Neuroinformatics, LLC"
            )
        return {
            "relative_path": relative_path,
            "content": content,
            "sensitivity_status": "allowed",
            "review_required": True,
            "review_reasons": ["insurance_policy"],
            "truncated": False,
            "local_only": True,
            "send_enabled": False,
        }

    monkeypatch.setattr(local_kni_evidence, "search_kni_documents_impl", fake_search)
    monkeypatch.setattr(local_kni_evidence, "read_kni_document_file_impl", fake_read)

    result = plan_chief_of_staff_request(
        "And who was the broker for the CFC insurance for Keystone Neuroinformatics?"
    )

    assert result.recommended_route.workflow_type == "project-context-review"
    assert "does not decide the substantive answer" in result.summary
    assert "COI_AnupSharma_2026.pdf" in result.summary
    assert result.retrieval_diagnostics["answer_focus"] == "broker"
    assert result.retrieval_diagnostics["effective_answer_focus"] == "broker"
    assert result.retrieval_diagnostics["evidence_paths"][0].endswith("COI_AnupSharma_2026.pdf")
    assert any("Review candidate evidence path: " in action for action in result.recommended_actions)


def test_chief_of_staff_cfc_insurance_followup_is_local_kni_context() -> None:
    local_kni = build_chief_of_staff_agent(
        request_text="And who was the broker for the CFC insurance?"
    )

    tool_names = {getattr(tool, "name", "") for tool in local_kni.tools}

    assert "search_kni_documents" in tool_names
    assert "read_kni_document_file" in tool_names
    assert "file_search" not in tool_names


def test_architecture_slack_history_does_not_trigger_tax_payment_shortcut() -> None:
    request = (
        "chief of staff I am deciding the next implementation pass for KNI. "
        "Review recent work and produce an execution brief. "
        "The channel also mentions APA 2026, the first next step, and summarize activity."
    )
    normalized = chief_of_staff_module._normalized_text(request)

    assert chief_of_staff_module._looks_like_tax_payment_or_estimate_request(normalized) is False
    assert chief_of_staff_module._looks_like_finance_tracker_request(request) is False


def test_project_state_summary_does_not_trigger_tax_payment_shortcut() -> None:
    request = "summarize the state of the KNI implementation pass for 2026 planning"
    normalized = chief_of_staff_module._normalized_text(request)

    assert chief_of_staff_module._looks_like_tax_payment_or_estimate_request(normalized) is False
    assert chief_of_staff_module._looks_like_finance_tracker_request(request) is False


@pytest.mark.parametrize(
    "request_text",
    [
        (
            "same response: 2026 tax payments Total paid $5,500. "
            "Let's see why above is posting -- were changes implemented in this run?"
        ),
        (
            "the request and response are completely unrelated -- why is that? "
            "2026 tax payments Federal $4,000 Pennsylvania $1,500"
        ),
        (
            "should we run it again? same response -- 2026 tax payments Total paid "
            "$5,500 Federal $4,000 Pennsylvania $1,500"
        ),
        "review Slack history and explain why @KNI keeps posting the same tax payment answer",
    ],
)
def test_wrong_response_diagnostics_do_not_trigger_tax_payment_shortcut(request_text: str) -> None:
    normalized = chief_of_staff_module._normalized_text(request_text)

    assert chief_of_staff_module._looks_like_tax_payment_or_estimate_request(normalized) is False
    assert chief_of_staff_module._looks_like_finance_tracker_request(request_text) is False


@pytest.mark.parametrize(
    "request_text",
    [
        "chief of staff confirm q2 tax estimated payments due on June 15, 2026",
        "Look online when is q2 payment due date per irs",
    ],
)
def test_tax_deadline_lookup_does_not_trigger_finance_tracker_shortcut(
    request_text: str,
) -> None:
    assert chief_of_staff_module._looks_like_finance_tracker_request(request_text) is False


def test_tax_deadline_slack_followup_uses_sdk_interpretation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_typed_sdk_agent(**kwargs: object) -> TypedAgentRunResult[ChiefOfStaffResult]:
        captured.update(kwargs)
        output = ChiefOfStaffResult(
            mode="llm",
            summary="Q2 estimated tax deadline lookup should be answered from official sources.",
            recommended_route=ChiefOfStaffRouteRecommendation(
                workflow_type="budget-resource-review",
                target_channel="current Slack thread",
            ),
            audit_notes=[],
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result={"sdk": "called"},
            live=True,
        )

    raw_request = (
        "chief of staff continue this prior Slack thread.\n"
        "Previous request: chief of staff confirm q2 tax estimated payments due on June 15, 2026\n"
        "Previous result title: Business Agents Chief of Staff Plan Ready\n"
        "Previous result: *Answer:* Q2 2026 tax payments Total paid: $0.00 "
        "Federal: $0.00 Pennsylvania: $0.00 Philadelphia: $0.00\n"
        "User follow-up: Look online when is q2 payment due date per irs\n"
        "Continue the same agent task."
    )

    monkeypatch.setattr(
        "keystone_agents.agents.chief_of_staff.run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    result = run_chief_of_staff_sdk(
        {"request": raw_request},
        live=True,
        force_sdk_interpretation=True,
    )

    typed_input = captured["typed_input"]
    assert result.raw_result == {"sdk": "called"}
    assert isinstance(typed_input, dict)
    assert typed_input["latest_operator_request"] == (
        "Look online when is q2 payment due date per irs"
    )
    assert typed_input["request"] == typed_input["latest_operator_request"]


def test_chief_of_staff_planner_blocks_unaligned_finance_shortcut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_typed_sdk_agent(**kwargs: object) -> TypedAgentRunResult[ChiefOfStaffResult]:
        captured.update(kwargs)
        output = ChiefOfStaffResult(
            mode="llm",
            summary="Planner routed this as an implementation brief, not a tax summary.",
            recommended_route=ChiefOfStaffRouteRecommendation(
                workflow_type="project-context-review",
                target_channel="current Slack channel",
            ),
            audit_notes=[],
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result={"sdk": "called"},
            live=True,
        )

    plan = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="slack_operations",
        primary_target="implementation pass",
        target_type="slack_channel",
        objective="Review the recent KNI architecture work and produce an execution brief.",
        task_objective="slack_operations",
        expected_artifact_type="slack_ops_summary",
        rationale="The request asks for implementation planning, not finance arithmetic.",
    )

    monkeypatch.setattr(
        "keystone_agents.agents.chief_of_staff.run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    result = run_chief_of_staff_sdk(
        "calculate the Q2 taxes paid for the implementation pass",
        live=True,
        manual_request_plan=plan,
    )

    assert result.raw_result == {"sdk": "called"}
    assert result.output.recommended_route.workflow_type == "project-context-review"
    assert captured["typed_input"] == "calculate the Q2 taxes paid for the implementation pass"


def test_chief_of_staff_deterministic_path_uses_planner_before_finance_shortcut() -> None:
    plan = ManualRequestPlan(
        source="parent_orchestrator",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="slack_operations",
        primary_target="implementation pass",
        target_type="slack_channel",
        objective="Review the recent KNI architecture work and produce an execution brief.",
        task_objective="slack_operations",
        expected_artifact_type="slack_ops_summary",
        rationale="The request asks for implementation planning, not tax arithmetic.",
    )

    result = plan_chief_of_staff_request(
        "calculate the Q2 taxes paid for the implementation pass",
        manual_request_plan=plan,
    )

    assert "finance_tax_tracker" not in result.summary
    assert result.recommended_route.workflow_type != "finance-tax-tracker"
    assert any(
        "Manual request planner ran before Chief of Staff deterministic routing" in note
        and "source=parent_orchestrator" in note
        for note in result.audit_notes
    )


def test_run_script_does_not_inherit_sdk_session_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_run_chief_of_staff_script()
    parser = script.build_parser()
    args = parser.parse_args(["--live-sdk", "--input", "review airtable tax notes"])
    monkeypatch.setenv("KEYSTONE_SDK_SESSIONS", "true")
    monkeypatch.setenv("KEYSTONE_SDK_SESSION_ID", "stale-slack-thread")

    def fail_if_called(*_: object, **__: object) -> object:
        raise AssertionError("implicit env session should not be inherited")

    monkeypatch.setattr(script, "sdk_session_from_args", fail_if_called)

    assert script._chief_of_staff_session_from_args(args) is None


def test_run_script_uses_explicit_sdk_session_arg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_run_chief_of_staff_script()
    parser = script.build_parser()
    args = parser.parse_args(
        [
            "--live-sdk",
            "--sdk-session",
            "--sdk-session-id",
            "explicit-thread",
            "--input",
            "review airtable tax notes",
        ]
    )
    captured: dict[str, object] = {}
    monkeypatch.setenv("USER", "local")

    def fake_sdk_session_from_args(*call_args: object, **kwargs: object) -> str:
        captured["args"] = call_args
        captured["kwargs"] = kwargs
        return "session-object"

    monkeypatch.setattr(script, "sdk_session_from_args", fake_sdk_session_from_args)

    assert script._chief_of_staff_session_from_args(args) == "session-object"
    assert captured["kwargs"] == {
        "scope": "chief_of_staff",
        "components": ("direct-script", "local", str(Path.cwd())),
        "default_enabled": False,
    }


def test_run_script_consumes_parent_orchestrator_preflight_env(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_run_chief_of_staff_script()
    plan = ManualRequestPlan(
        source="parent_orchestrator",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="slack_operations",
        primary_target="agent architecture",
        target_type="slack_channel",
        objective="Review agent architecture.",
        task_objective="slack_operations",
        expected_artifact_type="slack_ops_summary",
    )
    preflight = {
        "advisory_only": True,
        "selected_agent": "chief_of_staff",
        "manual_request_plan": plan.model_dump(mode="json"),
        "route_result": {"route": "chief_of_staff", "refused": False},
    }

    monkeypatch.setenv(MANUAL_REQUEST_PLAN_ENV, plan.model_dump_json())
    monkeypatch.setenv(ORCHESTRATOR_PREFLIGHT_ENV, json.dumps(preflight))

    assert script.main(["--json", "--input", "review agent architecture"]) == 0

    payload = _payload(capsys.readouterr().out)
    assert payload["manual_request_plan"]["source"] == "parent_orchestrator"
    assert payload["orchestrator_preflight"]["advisory_only"] is True
    assert payload["orchestrator_preflight"]["selected_agent"] == "chief_of_staff"


def test_run_script_live_sdk_runs_orchestrator_preflight_when_parent_absent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from keystone_agents.agents.orchestrator import OrchestratorPreflight
    from keystone_agents.schemas.orchestrator import OrchestratorResult

    script = _load_run_chief_of_staff_script()
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="slack_operations",
        primary_target="q2 estimated tax deadline",
        target_type="unknown",
        objective="Confirm the Q2 estimated tax deadline from official sources.",
        task_objective="slack_operations",
        expected_artifact_type="slack_ops_summary",
    )
    captured: dict[str, object] = {}

    class FakeModelConfig:
        def as_log_dict(self) -> dict[str, str]:
            return {"provider": "openai", "name": "gpt-5.4-mini"}

    def fake_preflight(*args: object, **kwargs: object) -> OrchestratorPreflight:
        captured["preflight_args"] = args
        captured["preflight_kwargs"] = kwargs
        return OrchestratorPreflight(
            request_text=str(args[0]),
            requested_agent="chief_of_staff",
            advisory_only=True,
            selected_agent="chief_of_staff",
            manual_request_plan=plan,
            route_result=OrchestratorResult(
                route="chief_of_staff",
                target_agent="chief_of_staff",
                rationale="Use Chief of Staff with source-backed interpretation.",
                audit_notes=["orchestrator preflight ran"],
            ),
        )

    def fake_run_chief_of_staff_sdk(*args: object, **kwargs: object) -> TypedAgentRunResult:
        captured["sdk_args"] = args
        captured["sdk_kwargs"] = kwargs
        output = ChiefOfStaffResult(
            mode="llm",
            summary="Official-source deadline answer.",
            recommended_route=ChiefOfStaffRouteRecommendation(
                workflow_type="budget-resource-review",
                target_channel="current Slack thread",
            ),
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result={"sdk": "called"},
            live=True,
        )

    monkeypatch.delenv(MANUAL_REQUEST_PLAN_ENV, raising=False)
    monkeypatch.delenv(ORCHESTRATOR_PREFLIGHT_ENV, raising=False)
    monkeypatch.setattr(script, "load_settings", lambda **_: None)
    monkeypatch.setattr(
        script,
        "get_runtime_agent_model_config",
        lambda *_args, **_kwargs: FakeModelConfig(),
    )
    monkeypatch.setattr(script, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(script, "run_chief_of_staff_sdk", fake_run_chief_of_staff_sdk)

    assert (
        script.main(
            [
                "--live-sdk",
                "--json",
                "--input",
                "chief of staff confirm q2 tax estimated payments due on June 15, 2026",
            ]
        )
        == 0
    )

    payload = _payload(capsys.readouterr().out)
    assert captured["preflight_kwargs"]["live_manual_plan"] is True
    assert captured["sdk_kwargs"]["manual_request_plan"] is plan
    assert payload["manual_request_plan"]["source"] == "llm"
    assert payload["orchestrator_preflight"]["selected_agent"] == "chief_of_staff"


def test_run_script_falls_back_when_live_sdk_misroutes_search_to_reference_capture(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from keystone_agents.agents.orchestrator import OrchestratorPreflight
    from keystone_agents.schemas.orchestrator import OrchestratorResult

    script = _load_run_chief_of_staff_script()
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="research_brief",
        primary_target="OpenAI mental health work",
        target_type="topic",
        objective="Brief what OpenAI is doing about mental health.",
        task_objective="source_research",
        expected_artifact_type="research_brief",
    )

    class FakeModelConfig:
        def as_log_dict(self) -> dict[str, str]:
            return {"provider": "openai", "name": "gpt-5.4-mini"}

    def fake_preflight(*args: object, **kwargs: object) -> OrchestratorPreflight:
        return OrchestratorPreflight(
            request_text=str(args[0]),
            requested_agent="chief_of_staff",
            advisory_only=True,
            selected_agent="chief_of_staff",
            manual_request_plan=plan,
            route_result=OrchestratorResult(
                route="chief_of_staff",
                target_agent="chief_of_staff",
                rationale="Use Chief of Staff for a source-backed brief.",
            ),
        )

    def fake_run_chief_of_staff_sdk(*args: object, **kwargs: object) -> TypedAgentRunResult:
        output = ChiefOfStaffResult(
            mode="llm",
            summary="Captured as a reference note for later research, not executed as a live brief.",
            recommended_route=ChiefOfStaffRouteRecommendation(
                workflow_type="reference-capture",
                target_channel="current Slack thread",
            ),
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result={"sdk": "called"},
            live=True,
        )

    monkeypatch.delenv(MANUAL_REQUEST_PLAN_ENV, raising=False)
    monkeypatch.delenv(ORCHESTRATOR_PREFLIGHT_ENV, raising=False)
    monkeypatch.setattr(script, "load_settings", lambda **_: None)
    monkeypatch.setattr(
        script,
        "get_runtime_agent_model_config",
        lambda *_args, **_kwargs: FakeModelConfig(),
    )
    monkeypatch.setattr(script, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(script, "run_chief_of_staff_sdk", fake_run_chief_of_staff_sdk)
    monkeypatch.setattr(script, "_chief_of_staff_output_review", lambda **_: {"status": "ok"})

    assert (
        script.main(
            [
                "--live-sdk",
                "--json",
                "--input",
                (
                    "chief of staff What is OpenAI doing about mental health? "
                    "Please do a deeper search brief."
                ),
            ]
        )
        == 0
    )

    payload = _payload(capsys.readouterr().out)
    output = payload["output"]
    assert output["recommended_route"]["workflow_type"] != "reference-capture"
    assert "Captured as a reference note" not in output["summary"]
    assert any(
        "deterministic Chief of Staff fallback was rendered instead" in note
        for note in output["audit_notes"]
    )
    assert payload["original_orchestrator_review"] == {"status": "ok"}


def test_run_script_allows_local_kni_evidence_lookup_as_reference_capture() -> None:
    script = _load_run_chief_of_staff_script()
    output = ChiefOfStaffResult(
        mode="llm",
        summary=(
            "Keystone Neuroinformatics LLC appears to have been formed on "
            "February 6, 2026. Evidence path: "
            "00_Admin/Formation/2-12-26-PA-FormationDocument-Keystone "
            "Neuroinformatics LLC.pdf."
        ),
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="reference-capture",
            target_channel="current Slack thread",
        ),
    )

    assert (
        script._reference_capture_mismatch(
            (
                "Find the date Keystone Neuroinformatics LLC was formed using "
                "local KNI documents. Return the date and evidence path."
            ),
            output,
        )
        is False
    )


def test_run_script_rejects_local_kni_reference_capture_without_evidence_path() -> None:
    script = _load_run_chief_of_staff_script()
    output = ChiefOfStaffResult(
        mode="llm",
        summary="Keystone Neuroinformatics LLC was formed on 2026-02-06.",
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="reference-capture",
            target_channel="current Slack thread",
        ),
    )

    assert (
        script._reference_capture_mismatch(
            (
                "Find the date Keystone Neuroinformatics LLC was formed using "
                "local KNI documents. Return the date and evidence path."
            ),
            output,
        )
        is True
    )


def test_run_script_live_sdk_passes_local_kni_evidence_packet_to_model(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_run_chief_of_staff_script()
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="slack_operations",
        primary_target="CFC insurance broker",
        target_type="slack_channel",
        objective="Identify the CFC insurance broker from local KNI evidence.",
        task_objective="slack_operations",
        expected_artifact_type="slack_ops_summary",
    )

    class FakeModelConfig:
        def as_log_dict(self) -> dict[str, str]:
            return {"provider": "openai", "name": "gpt-5.4-mini"}

    captured: dict[str, object] = {}

    def fake_run_chief_of_staff_sdk(*args: object, **kwargs: object) -> TypedAgentRunResult:
        captured["sdk_args"] = args
        captured["sdk_kwargs"] = kwargs
        output = ChiefOfStaffResult(
            mode="llm",
            summary=(
                "Based on the local KNI evidence packet, the broker/producer/agency "
                "is IAO, Inc. DBA ProAssurance Agency. Evidence path: "
                "00_Admin/Insurance/InsurancePolicy/COI_AnupSharma_2026.pdf."
            ),
            recommended_route=ChiefOfStaffRouteRecommendation(
                workflow_type="project-context-review",
                target_channel="current-thread",
            ),
            retrieval_diagnostics={
                "local_only": True,
                "send_enabled": False,
                "lookup_kind": "insurance",
                "evidence_path": "00_Admin/Insurance/InsurancePolicy/COI_AnupSharma_2026.pdf",
            },
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result={"sdk": "called"},
            live=True,
        )

    def fake_plan(*_args: object, **_kwargs: object) -> ChiefOfStaffResult:
        raise AssertionError("local KNI live evidence must be built from the raw query")

    def fake_read(relative_path: str, *, max_chars: int = 4_000) -> dict[str, object]:
        if "COI_" in relative_path:
            content = (
                "CERTIFICATE OF LIABILITY INSURANCE PRODUCER IAO, Inc. DBA "
                "ProAssurance Agency INSURER A : CFC Underwriters."
            )
        else:
            content = (
                "Insurance effected through the Coverholder: CFC Underwriting Limited. "
                "Insured: Keystone Neuroinformatics, LLC."
            )
        return {
            "relative_path": relative_path,
            "title": Path(relative_path).stem,
            "extension": "pdf",
            "sensitivity_status": "allowed",
            "review_required": True,
            "review_reasons": ["insurance_policy"],
            "truncated": False,
            "content": content,
            "local_only": True,
            "model_context_allowed": True,
            "send_enabled": False,
        }

    def fake_search(query: str, *, max_results: int = 8) -> dict[str, object]:
        return {
            "status": "ready",
            "matches": [
                {
                    "relative_path": "00_Admin/Insurance/InsurancePolicy/COI_AnupSharma_2026.pdf",
                    "title": "COI AnupSharma 2026",
                    "snippet": "PRODUCER IAO, Inc. DBA ProAssurance Agency",
                    "sensitivity_status": "allowed",
                    "review_required": True,
                    "review_reasons": ["insurance_policy"],
                },
                {
                    "relative_path": "00_Admin/Insurance/InsurancePolicy/CFC POLICY stamped 042126.pdf",
                    "title": "CFC policy stamped",
                    "snippet": "Coverholder: CFC Underwriting Limited",
                    "sensitivity_status": "allowed",
                    "review_required": True,
                    "review_reasons": ["insurance_policy"],
                },
            ],
            "blocked_result_count": 0,
            "local_only": True,
            "send_enabled": False,
        }

    monkeypatch.setenv(MANUAL_REQUEST_PLAN_ENV, plan.model_dump_json())
    monkeypatch.delenv(ORCHESTRATOR_PREFLIGHT_ENV, raising=False)
    monkeypatch.setattr(script, "load_settings", lambda **_: None)
    monkeypatch.setattr(
        script,
        "get_runtime_agent_model_config",
        lambda *_args, **_kwargs: FakeModelConfig(),
    )
    monkeypatch.setattr(script, "run_chief_of_staff_sdk", fake_run_chief_of_staff_sdk)
    monkeypatch.setattr(script, "plan_chief_of_staff_request", fake_plan)
    monkeypatch.setattr(
        script,
        "runtime_source_layer_policy_context",
        lambda agent_name: {
            "agent_name": agent_name,
            "status": "declared",
            "available": True,
            "layers": [
                {
                    "layer": "local_kni_documents",
                    "runtime_status": "ready",
                    "runtime_available": True,
                },
                {
                    "layer": "hosted_file_search",
                    "runtime_status": "available",
                    "runtime_available": True,
                },
            ],
        },
    )
    monkeypatch.setattr(local_kni_evidence, "read_kni_document_file_impl", fake_read)
    monkeypatch.setattr(local_kni_evidence, "search_kni_documents_impl", fake_search)

    assert (
        script.main(
            [
                "--live-sdk",
                "--json",
                "--input",
                "who was the broker for the CFC insurance?",
            ]
        )
        == 0
    )

    payload = _payload(capsys.readouterr().out)
    assert payload["mode"] == "live_sdk"
    assert payload["live_sdk"] is True
    assert payload["output"]["retrieval_diagnostics"]["lookup_kind"] == "insurance"
    assert "ProAssurance Agency" in payload["output"]["summary"]
    typed_input = captured["sdk_args"][0]
    packet = typed_input["local_kni_evidence_packet"]
    assert packet["local_only"] is True
    assert packet["send_enabled"] is False
    assert packet["packet_type"] == "bounded_local_kni_document_evidence"
    assert "summary" not in packet
    assert "synthesis" not in packet
    assert packet["answer_policy"]["deterministic_prefetch_is_not_final_answer"] is True
    assert packet["retrieval_diagnostics"]["effective_lookup_kind"] == "insurance"
    assert packet["retrieval_diagnostics"]["effective_answer_focus"] == "broker"
    assert packet["candidate_documents"][0]["relative_path"].endswith("COI_AnupSharma_2026.pdf")
    assert "ProAssurance" in packet["candidate_documents"][0]["content_excerpt"]
    assert any(
        doc["relative_path"].endswith("CFC POLICY stamped 042126.pdf")
        for doc in packet["candidate_documents"]
    )
    assert any(
        match["relative_path"].endswith("COI_AnupSharma_2026.pdf")
        for match in packet["search_matches"]
    )
    assert "not as a prewritten final answer" in typed_input["local_kni_instruction"]
    source_layer_policy = typed_input["runtime_source_layer_policy"]
    assert source_layer_policy["agent_name"] == "chief_of_staff"
    layer_status = {
        layer["layer"]: layer["runtime_status"]
        for layer in source_layer_policy["layers"]
    }
    assert layer_status["local_kni_documents"] == "ready"
    assert layer_status["hosted_file_search"] == "available"


def test_run_script_preserves_local_kni_live_answer_when_path_is_repaired(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_run_chief_of_staff_script()
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="slack_operations",
        primary_target="formation confirmation department",
        target_type="slack_channel",
        objective="Identify the department from local KNI formation evidence.",
        task_objective="slack_operations",
        expected_artifact_type="slack_ops_summary",
    )

    class FakeModelConfig:
        def as_log_dict(self) -> dict[str, str]:
            return {"provider": "openai", "name": "gpt-5.4-mini"}

    def fake_run_chief_of_staff_sdk(*_args: object, **_kwargs: object) -> TypedAgentRunResult:
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=ChiefOfStaffResult(
                mode="llm",
                summary=(
                    "The confirmation appears to come from the Pennsylvania "
                    "Department of State."
                ),
                recommended_route=ChiefOfStaffRouteRecommendation(
                    workflow_type="project-context-review",
                    target_channel="current-thread",
                ),
                retrieval_diagnostics={
                    "local_only": True,
                    "send_enabled": False,
                    "lookup_kind": "formation",
                },
            ),
            raw_result={"sdk": "called"},
            live=True,
        )

    def fake_plan(*_args: object, **_kwargs: object) -> ChiefOfStaffResult:
        raise AssertionError("local KNI live answer should be repaired, not replaced")

    def fake_read(relative_path: str, *, max_chars: int = 4_000) -> dict[str, object]:
        return {
            "relative_path": relative_path,
            "title": Path(relative_path).stem,
            "extension": "pdf",
            "sensitivity_status": "allowed",
            "review_required": True,
            "review_reasons": ["legal_contract"],
            "truncated": False,
            "content": (
                "Pennsylvania Department of State\n"
                "Confirmation of organized documentation for Keystone Neuroinformatics LLC."
            ),
            "local_only": True,
            "model_context_allowed": True,
            "send_enabled": False,
        }

    def fake_search(query: str, *, max_results: int = 8) -> dict[str, object]:
        return {
            "status": "ready",
            "matches": [
                {
                    "relative_path": (
                        "00_Admin/Formation/2-12-26-PA-FormationDocument-"
                        "Keystone Neuroinformatics LLC.pdf"
                    ),
                    "title": "PA FormationDocument Keystone Neuroinformatics LLC",
                    "snippet": "Pennsylvania Department of State confirmation",
                    "sensitivity_status": "allowed",
                    "review_required": True,
                    "review_reasons": ["legal_contract"],
                }
            ],
            "blocked_result_count": 0,
            "local_only": True,
            "send_enabled": False,
        }

    monkeypatch.setenv(MANUAL_REQUEST_PLAN_ENV, plan.model_dump_json())
    monkeypatch.delenv(ORCHESTRATOR_PREFLIGHT_ENV, raising=False)
    monkeypatch.setattr(script, "load_settings", lambda **_: None)
    monkeypatch.setattr(
        script,
        "get_runtime_agent_model_config",
        lambda *_args, **_kwargs: FakeModelConfig(),
    )
    monkeypatch.setattr(script, "run_chief_of_staff_sdk", fake_run_chief_of_staff_sdk)
    monkeypatch.setattr(script, "plan_chief_of_staff_request", fake_plan)
    monkeypatch.setattr(
        script,
        "runtime_source_layer_policy_context",
        lambda agent_name: {"agent_name": agent_name, "layers": []},
    )
    monkeypatch.setattr(local_kni_evidence, "read_kni_document_file_impl", fake_read)
    monkeypatch.setattr(local_kni_evidence, "search_kni_documents_impl", fake_search)

    assert (
        script.main(
            [
                "--live-sdk",
                "--json",
                "--input",
                (
                    "chief of staff what department provided the confirmation of "
                    "organized documentation for Keystone Neuroinformatics llc in "
                    "pennsylvania?"
                ),
            ]
        )
        == 0
    )

    payload = _payload(capsys.readouterr().out)
    output = payload["output"]
    assert "Pennsylvania Department of State" in output["summary"]
    assert "evidence is available for Chief of Staff synthesis" not in output["summary"]
    assert output["retrieval_diagnostics"]["evidence_path"].startswith("00_Admin/Formation/")
    assert any(source["source_type"] == "local_kni_document" for source in output["sources"])
    assert any("live model answer was preserved" in note for note in output["audit_notes"])


def test_run_script_local_kni_formation_packet_filters_broad_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _load_run_chief_of_staff_script()
    output = ChiefOfStaffResult(
        mode="deterministic",
        summary=(
            "Keystone Neuroinformatics LLC appears to have been formed on February 6, 2026. "
            "Evidence path: 00_Admin/Formation/2-12-26-PA-FormationDocument-Keystone "
            "Neuroinformatics LLC.pdf."
        ),
        sources=[
            ChiefOfStaffSourceRef(
                title=(
                    "00_Admin/Formation/2-12-26-PA-FormationDocument-"
                    "Keystone Neuroinformatics LLC.pdf"
                ),
                url="",
                source_type="local_kni_document",
            )
        ],
        retrieval_diagnostics={
            "local_only": True,
            "send_enabled": False,
            "lookup_kind": "formation",
            "evidence_path": (
                "00_Admin/Formation/2-12-26-PA-FormationDocument-"
                "Keystone Neuroinformatics LLC.pdf"
            ),
        },
    )

    def fake_search(query: str, *, max_results: int = 8) -> dict[str, object]:
        return {
            "status": "ready",
            "matches": [
                {
                    "relative_path": (
                        "00_Admin/Internal_Policies/BoardRoom-Memos/"
                        "2026-03-29-keystone-neuroinformatics-12-month-positioning.md"
                    ),
                    "title": "12-month positioning",
                    "snippet": "Broad company memo",
                    "sensitivity_status": "allowed",
                    "review_required": False,
                    "review_reasons": [],
                },
                {
                    "relative_path": (
                        "00_Admin/Formation/InitialResolutions-LLC Single Member--"
                        "Keystone Neuroinformatics LLC.pdf"
                    ),
                    "title": "Initial resolutions",
                    "snippet": "Keystone Neuroinformatics LLC formation",
                    "sensitivity_status": "allowed",
                    "review_required": True,
                    "review_reasons": ["legal_contract"],
                },
            ],
            "blocked_result_count": 0,
            "local_only": True,
            "send_enabled": False,
        }

    def fake_read(relative_path: str, *, max_chars: int = 4_000) -> dict[str, object]:
        return {
            "relative_path": relative_path,
            "title": Path(relative_path).stem,
            "extension": "pdf",
            "sensitivity_status": "allowed",
            "review_required": "InitialResolutions" in relative_path,
            "review_reasons": ["legal_contract"] if "InitialResolutions" in relative_path else [],
            "truncated": False,
            "content": "Date Filed: 2/6/2026",
            "local_only": True,
            "model_context_allowed": True,
            "send_enabled": False,
        }

    monkeypatch.setattr(local_kni_evidence, "search_kni_documents_impl", fake_search)
    monkeypatch.setattr(local_kni_evidence, "read_kni_document_file_impl", fake_read)

    packet = script._local_kni_evidence_packet(
        output,
        query_text="what date was Keystone Neuroinformatics LLC formed?",
    )
    paths = [doc["relative_path"] for doc in packet["candidate_documents"]]

    assert paths == [
        "00_Admin/Formation/2-12-26-PA-FormationDocument-Keystone Neuroinformatics LLC.pdf",
        "00_Admin/Formation/InitialResolutions-LLC Single Member--Keystone Neuroinformatics LLC.pdf",
    ]
    assert all("BoardRoom-Memos" not in path for path in paths)


def test_run_script_falls_back_when_live_sdk_output_is_malformed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from keystone_agents.agents.orchestrator import OrchestratorPreflight
    from keystone_agents.schemas.orchestrator import OrchestratorResult

    script = _load_run_chief_of_staff_script()
    plan = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="chief_of_staff",
        intent="research_brief",
        primary_target="AI therapy chatbots and youth mental health",
        target_type="topic",
        objective="Create a sectioned source-backed brief.",
        task_objective="source_research",
        expected_artifact_type="research_brief",
    )

    class FakeModelConfig:
        def as_log_dict(self) -> dict[str, str]:
            return {"provider": "openai", "name": "gpt-5.4-mini"}

    def fake_preflight(*args: object, **kwargs: object) -> OrchestratorPreflight:
        return OrchestratorPreflight(
            request_text=str(args[0]),
            requested_agent="chief_of_staff",
            advisory_only=True,
            selected_agent="chief_of_staff",
            manual_request_plan=plan,
            route_result=OrchestratorResult(
                route="chief_of_staff",
                target_agent="chief_of_staff",
                rationale="Use Chief of Staff for a source-backed sectioned brief.",
            ),
        )

    def fake_run_chief_of_staff_sdk(*args: object, **kwargs: object) -> TypedAgentRunResult:
        raise ValueError('Invalid JSON when parsing {"agent_name":"chief_of_staff"')

    monkeypatch.delenv(MANUAL_REQUEST_PLAN_ENV, raising=False)
    monkeypatch.delenv(ORCHESTRATOR_PREFLIGHT_ENV, raising=False)
    monkeypatch.setattr(script, "load_settings", lambda **_: None)
    monkeypatch.setattr(
        script,
        "get_runtime_agent_model_config",
        lambda *_args, **_kwargs: FakeModelConfig(),
    )
    monkeypatch.setattr(script, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(script, "run_chief_of_staff_sdk", fake_run_chief_of_staff_sdk)
    monkeypatch.setattr(script, "_chief_of_staff_output_review", lambda **_: {"status": "ok"})

    assert (
        script.main(
            [
                "--live-sdk",
                "--json",
                "--input",
                (
                    "chief of staff prepare a sectioned brief on AI therapy chatbots "
                    "and youth mental health."
                ),
            ]
        )
        == 0
    )

    payload = _payload(capsys.readouterr().out)
    output = payload["output"]
    assert payload["output_type"] == "ChiefOfStaffResult"
    assert "usage" not in payload
    assert "cost" not in payload
    assert "request_cache" not in payload
    assert any(
        "Live SDK structured output could not be parsed or validated" in note
        for note in output["audit_notes"]
    )
    assert payload["orchestrator_review"] == {"status": "ok"}


def test_run_script_payload_includes_sdk_usage_cost_and_request_cache() -> None:
    script = _load_run_chief_of_staff_script()
    output = plan_chief_of_staff_request("review agent architecture")

    payload = script._payload(
        mode="live_sdk",
        live_sdk=True,
        model={"provider": "openai", "name": "gpt-5.4-mini"},
        output=output,
        input_text="review agent architecture",
        usage={"available": True, "input_tokens": 100, "cached_input_tokens": 40},
        cost={"amount_usd": 0.01, "currency": "USD"},
        request_cache={"request_layout": "static_agent_prefix_then_dynamic_typed_input"},
    )

    assert payload["usage"]["cached_input_tokens"] == 40
    assert payload["cost"]["amount_usd"] == 0.01
    assert payload["request_cache"]["request_layout"] == (
        "static_agent_prefix_then_dynamic_typed_input"
    )


def test_run_script_json_exception_emits_operator_failure_payload(
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = _load_run_chief_of_staff_script()

    exit_code = script._handle_uncaught_exception(
        RuntimeError("Error code: 429 - rate limit reached"),
        ["--json"],
    )

    assert exit_code == 1
    payload = _payload(capsys.readouterr().out)
    failure = payload["output"]["failure"]
    assert failure["schema"] == "keystone.operator_failure.v1"
    assert failure["kind"] == "provider_rate_limit"
    assert failure["retryable"] is True
    assert "rate limit" in failure["summary"].lower()


def test_run_script_makes_structured_source_urls_visible_in_summary() -> None:
    output = ChiefOfStaffResult(
        mode="llm",
        summary="Confirmed: the 2026 Q2 estimated tax payment date is June 15, 2026.",
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="budget-resource-review",
            target_channel="current Slack thread",
        ),
        sources=[
            ChiefOfStaffSourceRef(
                title="IRS Publication 505",
                url="https://www.irs.gov/publications/p505",
                source_type="irs",
            )
        ],
    )

    visible = append_visible_source_urls_to_output(output)

    assert "https://www.irs.gov/publications/p505" in visible.summary
    assert "Sources: IRS Publication 505" in visible.summary


def test_run_script_allows_bounded_workspace_writes_for_explicit_doc_request() -> None:
    script = _load_run_chief_of_staff_script()
    request = (
        "review airtable tables and create a Q1 tax analysis in google docs. "
        "Create a folder and provide a link to the google doc"
    )

    assert script._approval_reference_for_request(request).startswith("chief-of-staff-command:")
    policy = script._live_side_effect_policy(request)

    assert "Live internal Airtable reads are allowed" in policy
    assert "Live Google Workspace folder/doc writes are allowed" in policy
    assert "supplied approval_reference" in policy
    assert "mutate Airtable unless separately requested" in policy


def test_chief_of_staff_article_reader_is_default_off_until_explicit() -> None:
    generic = build_chief_of_staff_agent(request_text="summarize links in #docs")
    generic_tool_names = {getattr(tool, "name", "") for tool in generic.tools}

    explicit = build_chief_of_staff_agent(
        request_text="read the full article linked in #docs and summarize it"
    )
    explicit_tool_names = {getattr(tool, "name", "") for tool in explicit.tools}
    deep_source = build_chief_of_staff_agent(
        request_text=(
            "What is OpenAI doing about mental health? Please do a deeper "
            "source-backed web search and give me a readable brief."
        )
    )
    deep_source_tool_names = {getattr(tool, "name", "") for tool in deep_source.tools}

    assert explicit_full_article_read_requested("read the full article at https://example.com")
    assert explicit_full_article_read_requested(
        "do a deeper source-backed web search with a readable brief"
    )
    assert explicit_full_article_read_requested(
        "Can you do a deeper read-only search on AI scribes? Please give a "
        "Detailed Summary that summarizes the source data first and 3-5 source links."
    )
    assert explicit_full_article_read_requested(
        "Read/extract these three URLs and return Answer, Detailed Summary, and Source evidence."
    )
    assert not explicit_full_article_read_requested("summarize links in #docs")
    assert "read_linked_article" not in generic_tool_names
    assert "read_linked_article" in explicit_tool_names
    assert "read_linked_article" in deep_source_tool_names

    result = plan_chief_of_staff_request("read the full article linked in #docs today")
    assert "article_link_review" in result.operating_capabilities
    assert "full_article_reading" in result.operating_capabilities


def test_chief_of_staff_finance_tracker_requests_short_circuit_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_read_records(
        table: str = "",
        **_: object,
    ) -> dict[str, object]:
        return {"status": "success", "records": [{"fields": {"Quarter": 1, "Amount": 100}}]}

    monkeypatch.setattr(chief_of_staff_module, "airtable_read_records_impl", fake_read_records)

    result = run_chief_of_staff_sdk(
        "read up to 1 live Airtable record from finance_tax_tracker table Business Income",
        live=True,
    )

    assert result.raw_result == {"deterministic": "finance_tax_tracker"}
    assert result.output.recommended_route.workflow_type == "budget-resource-review"
    assert "Business Income" in result.output.summary
    assert "No dollar amounts" in result.output.summary


def test_chief_of_staff_finance_tracker_uses_first_requested_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_tables: list[str] = []

    def fake_read_records(
        table: str = "",
        **_: object,
    ) -> dict[str, object]:
        requested_tables.append(table)
        return {"status": "success", "records": [{"fields": {"Source": "Employer"}}]}

    monkeypatch.setattr(chief_of_staff_module, "airtable_read_records_impl", fake_read_records)

    result = run_chief_of_staff_sdk(
        "read up to 1 live Airtable record from finance_tax_tracker table "
        "Personal Income. Summarize visible fields and how Personal Income differs "
        "operationally from Business Income. Do not expose dollar amounts or mutate records.",
        live=True,
    )

    assert requested_tables == ["Personal Income"]
    assert "Personal Income" in result.output.summary
    assert "Business Income" not in result.output.summary


def test_chief_of_staff_finance_tracker_totals_income_tables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_tables: list[tuple[str, int, bool]] = []

    def fake_read_records(
        table: str = "",
        **kwargs: object,
    ) -> dict[str, object]:
        requested_tables.append(
            (table, int(kwargs.get("max_records", 0)), bool(kwargs.get("fetch_all")))
        )
        records_by_table = {
            "Business Income": [
                {"fields": {"Amount": 100, "Investment Income": 999}},
                {"fields": {"Self-Employed Income": "25.50"}},
            ],
            "Personal Income": [
                {"fields": {"Investment Income": "$10.25", "Quarter": 1}},
            ],
        }
        return {"status": "success", "records": records_by_table[table]}

    def fake_schema(**_: object) -> dict[str, object]:
        return {
            "status": "success",
            "schema": {
                "tables": [
                    {
                        "name": "Business Income",
                        "fields": [
                            {"name": "Amount"},
                            {"name": "Investment Income"},
                            {"name": "Quarter"},
                        ],
                    },
                    {
                        "name": "Personal Income",
                        "fields": [
                            {"name": "Amount"},
                            {"name": "Investment Income"},
                            {"name": "Quarter"},
                        ],
                    },
                    {"name": "Business Expenses", "fields": [{"name": "Amount"}]},
                ],
            },
        }

    monkeypatch.setattr(chief_of_staff_module, "airtable_read_records_impl", fake_read_records)
    monkeypatch.setattr(chief_of_staff_module, "airtable_get_base_schema_impl", fake_schema)

    result = run_chief_of_staff_sdk(
        "can you total all income for 2026 from the finance_tax_tracker Airtable base? "
        "Please include Business Income plus Personal Income, state which tables/fields "
        "you used, and flag any tax-review caveats. Read-only: do not mutate records.",
        live=True,
    )

    assert requested_tables == [
        ("Business Income", 0, True),
        ("Personal Income", 0, True),
    ]
    assert "$135.75" in result.output.summary
    assert result.output.summary.startswith(
        "Total income in the `finance_tax_tracker` Airtable base is $135.75."
    )
    assert "Business Income" in result.output.summary
    assert "Personal Income" in result.output.summary
    assert "not final tax advice" not in result.output.summary
    assert "Schema-first Airtable aggregate planning selected tables from live schema." in (
        result.output.audit_notes
    )


def test_chief_of_staff_finance_tracker_totals_expense_tables_from_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_tables: list[str] = []

    def fake_schema(**_: object) -> dict[str, object]:
        return {
            "status": "success",
            "schema": {
                "tables": [
                    {"name": "Business Expenses", "fields": [{"name": "Amount"}]},
                    {"name": "Personal Expenses", "fields": [{"name": "Total Expenses"}]},
                    {"name": "Tax Payments", "fields": [{"name": "Amount"}]},
                ],
            },
        }

    def fake_read_records(table: str = "", **_: object) -> dict[str, object]:
        requested_tables.append(table)
        records_by_table = {
            "Business Expenses": [{"fields": {"Amount": 80}}],
            "Personal Expenses": [{"fields": {"Total Expenses": "20.25"}}],
        }
        return {"status": "success", "records": records_by_table[table]}

    monkeypatch.setattr(chief_of_staff_module, "airtable_get_base_schema_impl", fake_schema)
    monkeypatch.setattr(chief_of_staff_module, "airtable_read_records_impl", fake_read_records)

    result = run_chief_of_staff_sdk(
        "total all expenses for 2026 from finance_tax_tracker",
        live=True,
    )

    assert requested_tables == ["Business Expenses", "Personal Expenses"]
    assert "$100.25" in result.output.summary
    assert result.output.summary.startswith(
        "Total expense in the `finance_tax_tracker` Airtable base is $100.25."
    )
    assert "Business Expenses" in result.output.summary
    assert "Personal Expenses" in result.output.summary
    assert "Tax Payments" not in result.output.summary


def test_chief_of_staff_finance_tracker_totals_income_and_expenses_by_quarter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_tables: list[str] = []

    def fake_schema(**_: object) -> dict[str, object]:
        return {
            "status": "success",
            "schema": {
                "tables": [
                    {
                        "name": "Business Income",
                        "fields": [{"name": "Amount"}, {"name": "Quarter"}],
                    },
                    {
                        "name": "Personal Income",
                        "fields": [{"name": "Amount"}, {"name": "Quarter"}],
                    },
                    {
                        "name": "Business Expenses",
                        "fields": [{"name": "Amount"}, {"name": "Quarter"}],
                    },
                    {
                        "name": "Personal Expenses",
                        "fields": [{"name": "Amount"}, {"name": "Quarter"}],
                    },
                ],
            },
        }

    def fake_read_records(table: str = "", **_: object) -> dict[str, object]:
        requested_tables.append(table)
        records_by_table = {
            "Business Income": [
                {"fields": {"Amount": 100, "Quarter": "Q1"}},
                {"fields": {"Amount": 200, "Quarter": "Q3"}},
            ],
            "Personal Income": [{"fields": {"Amount": 300, "Quarter": 2}}],
            "Business Expenses": [{"fields": {"Amount": "40.50", "Quarter": "Q1"}}],
            "Personal Expenses": [
                {"fields": {"Amount": 10, "Quarter": "Q2"}},
                {"fields": {"Amount": 99, "Quarter": "Q4"}},
                {
                    "fields": {
                        "Amount": 100,
                        "Total Expenses": 125,
                        "Quarter": "Q2",
                    }
                },
                {
                    "fields": {
                        "Amount": 999,
                        "Quarter": "Q3",
                        "Date of Expense": "2026-05-01",
                    }
                },
            ],
        }
        return {"status": "success", "records": records_by_table[table]}

    monkeypatch.setattr(chief_of_staff_module, "airtable_get_base_schema_impl", fake_schema)
    monkeypatch.setattr(chief_of_staff_module, "airtable_read_records_impl", fake_read_records)

    result = run_chief_of_staff_sdk(
        (
            "chief of staff can you total all income for Q1 and Q2 2026 and all "
            "expenses for Q1 and Q2 2026. Output as a clear summary"
        ),
        live=True,
    )

    assert requested_tables == [
        "Business Income",
        "Personal Income",
        "Business Expenses",
        "Personal Expenses",
    ]
    assert result.raw_result == {"deterministic": "finance_tax_tracker"}
    assert "For Q1 and Q2 2026" in result.output.summary
    assert "* Total income: $400.00" in result.output.summary
    assert "* Total expenses: $175.50" in result.output.summary
    assert "Income detail" in result.output.summary
    assert "Expense detail" in result.output.summary
    assert "Q3" not in result.output.summary
    assert "Q4" not in result.output.summary
    assert any(
        "Schema-first Airtable multi-aggregate planning selected tables" in note
        for note in result.output.audit_notes
    )


def test_chief_of_staff_finance_tracker_finds_top_business_expense(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_tables: list[str] = []

    def fake_schema(**_: object) -> dict[str, object]:
        return {
            "status": "success",
            "schema": {
                "tables": [
                    {
                        "name": "Business Expenses",
                        "fields": [
                            {"name": "Item"},
                            {"name": "Amount"},
                            {"name": "Total Expenses"},
                            {"name": "Date of Expense"},
                            {"name": "Categories"},
                            {"name": "Expense Client/Vendor"},
                        ],
                    },
                    {
                        "name": "Personal Expenses",
                        "fields": [{"name": "Item"}, {"name": "Total Expenses"}],
                    },
                ],
            },
        }

    def fake_read_records(table: str = "", **_: object) -> dict[str, object]:
        requested_tables.append(table)
        assert table == "Business Expenses"
        return {
            "status": "success",
            "records": [
                {
                    "id": "recSmall",
                    "fields": {
                        "Item": "Office Supplies",
                        "Amount": 97.45,
                        "Total Expenses": 97.45,
                    },
                },
                {
                    "id": "recLarge",
                    "fields": {
                        "Item": "PEO Insurance",
                        "Amount": 1405,
                        "Total Expenses": 1400,
                        "Date of Expense": "2026-04-21",
                        "Categories": "Insurance",
                        "Expense Client/Vendor": "ProAssurance",
                    },
                },
            ],
        }

    monkeypatch.setattr(chief_of_staff_module, "airtable_get_base_schema_impl", fake_schema)
    monkeypatch.setattr(chief_of_staff_module, "airtable_read_records_impl", fake_read_records)

    result = run_chief_of_staff_sdk("what was top business expense", live=True)

    assert requested_tables == ["Business Expenses"]
    assert result.raw_result == {"deterministic": "finance_tax_tracker"}
    assert "Top expense record in finance_tax_tracker" in result.output.summary
    assert "PEO Insurance" in result.output.summary
    assert "* Amount used: $1,400.00" in result.output.summary
    assert "* Table: Business Expenses" in result.output.summary
    assert "* Category: Insurance" in result.output.summary
    assert "Personal Expenses" not in result.output.summary
    assert any(
        "Schema-first Airtable top-record planning selected tables" in note
        for note in result.output.audit_notes
    )


def test_chief_of_staff_finance_tracker_totals_personal_expenses_by_quarter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested_tables: list[str] = []

    def fake_schema(**_: object) -> dict[str, object]:
        return {
            "status": "success",
            "schema": {
                "tables": [
                    {"name": "Personal Expenses", "fields": [{"name": "Total Expenses"}]},
                    {"name": "Business Expenses", "fields": [{"name": "Amount"}]},
                ],
            },
        }

    def fake_read_records(table: str = "", **_: object) -> dict[str, object]:
        requested_tables.append(table)
        assert table == "Personal Expenses"
        return {
            "status": "success",
            "records": [
                {"fields": {"Total Expenses": 100, "Estimated Tax Periods": "Q1"}},
                {"fields": {"Total Expenses": 200, "Estimated Tax Periods": "Q2"}},
                {
                    "fields": {
                        "Total Expenses": 700,
                        "Estimated Tax Periods": "Q2",
                        "Date of Expense": "2025-05-01",
                    }
                },
                {"fields": {"Total Expenses": 999, "Estimated Tax Periods": "Q3"}},
            ],
        }

    monkeypatch.setattr(chief_of_staff_module, "airtable_get_base_schema_impl", fake_schema)
    monkeypatch.setattr(chief_of_staff_module, "airtable_read_records_impl", fake_read_records)

    result = run_chief_of_staff_sdk(
        "sum the Q1 and Q2 personal expenses. Report expenses for each quarter as a single sum",
        live=True,
    )

    assert requested_tables == ["Personal Expenses"]
    assert "Q1: $100.00" in result.output.summary
    assert "Q2: $900.00" in result.output.summary
    assert "Q3" not in result.output.summary


def test_chief_of_staff_schema_views_use_allowed_tables_as_dry_run_fallback() -> None:
    tables = chief_of_staff_module._schema_table_views(
        {
            "status": "dry-run",
            "schema": {
                "allowed_tables": [
                    "Business Income",
                    "Business Expenses",
                    "Personal Income",
                    "Personal Expenses",
                    "Tax Payments",
                ],
                "tables": [],
            },
        }
    )

    assert [table.name for table in tables] == [
        "Business Income",
        "Business Expenses",
        "Personal Income",
        "Personal Expenses",
        "Tax Payments",
    ]


def test_finance_tracker_date_quarters_use_estimated_tax_periods() -> None:
    assert chief_of_staff_module._year_quarter_from_date_value("2026-03-31") == (2026, 1)
    assert chief_of_staff_module._year_quarter_from_date_value("2026-04-01") == (2026, 2)
    assert chief_of_staff_module._year_quarter_from_date_value("2026-05-31") == (2026, 2)
    assert chief_of_staff_module._year_quarter_from_date_value("2026-06-01") == (2026, 3)
    assert chief_of_staff_module._year_quarter_from_date_value("2026-08-31") == (2026, 3)
    assert chief_of_staff_module._year_quarter_from_date_value("2026-09-01") == (2026, 4)


def test_finance_tracker_estimated_tax_period_field_is_authoritative() -> None:
    fields = {
        "Estimated Tax Periods": "2",
        "Date of Expense": "2025-05-01",
    }

    assert chief_of_staff_module._record_matches_period(
        fields,
        quarters=(2,),
        years=(2026,),
    )
    assert chief_of_staff_module._record_quarter(fields) == 2


def test_chief_of_staff_finance_tracker_counts_schema_selected_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_schema(**_: object) -> dict[str, object]:
        return {
            "status": "success",
            "schema": {
                "tables": [
                    {"name": "Tax Payments", "fields": [{"name": "Payment Name"}]},
                ],
            },
        }

    def fake_read_records(table: str = "", **_: object) -> dict[str, object]:
        assert table == "Tax Payments"
        return {
            "status": "success",
            "records": [
                {"fields": {"Payment Name": "Q1"}},
                {"fields": {"Payment Name": "Q2"}},
            ],
        }

    monkeypatch.setattr(chief_of_staff_module, "airtable_get_base_schema_impl", fake_schema)
    monkeypatch.setattr(chief_of_staff_module, "airtable_read_records_impl", fake_read_records)

    result = run_chief_of_staff_sdk(
        "how many Tax Payments are in finance_tax_tracker?",
        live=True,
    )

    assert "2 records" in result.output.summary
    assert result.output.summary.startswith("I found 2 records in `finance_tax_tracker`")
    assert "Tax Payments" in result.output.summary


def test_chief_of_staff_finance_tracker_summarizes_q2_tax_payments_and_rolling_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_schema(**_: object) -> dict[str, object]:
        return {
            "status": "success",
            "schema": {
                "tables": [
                    {"name": "Tax Payments", "fields": [{"name": "Payment Name"}]},
                ],
            },
        }

    def fake_read_records(table: str = "", **kwargs: object) -> dict[str, object]:
        assert table == "Tax Payments"
        assert kwargs["fetch_all"] is True
        return {
            "status": "success",
            "records": [
                {
                    "fields": {
                        "Payment Name": "IRS Estimated Taxes: Q2 2026",
                        "Estimated Tax Periods": 2,
                        "Tax Type": "Federal",
                        "Amount": 2000,
                    },
                },
                {
                    "fields": {
                        "Payment Name": "PA Estimated Taxes: Q2 2026",
                        "Estimated Tax Periods": 2,
                        "Tax Type": "State",
                        "Amount": 750,
                    },
                },
                {
                    "fields": {
                        "Payment Name": "Q2 Rolling Taxes Summary",
                        "Estimated Tax Periods": 2,
                        "Amount": 0,
                        "Notes": (
                            "Q2 Rolling Tax Estimates. Total 2026 YTD income; "
                            "self-employment and dividends; federal IRS estimate; "
                            "Pennsylvania state estimate; Philadelphia NPT and SIT; "
                            "planning estimate for accountant review."
                        ),
                    },
                },
            ],
        }

    monkeypatch.setattr(chief_of_staff_module, "airtable_get_base_schema_impl", fake_schema)
    monkeypatch.setattr(chief_of_staff_module, "airtable_read_records_impl", fake_read_records)

    result = run_chief_of_staff_sdk(
        "calculate the Q2 taxes paid to federal, state, and city from the Airtable tracker",
        live=True,
    )

    assert "$2,750.00" in result.output.summary
    assert "Federal: $2,000.00" in result.output.summary
    assert "Pennsylvania: $750.00" in result.output.summary
    assert "Philadelphia: $0.00" in result.output.summary
    assert "Q2 Rolling Taxes Summary" in result.output.summary
    assert "not final tax advice" not in result.output.summary


def test_chief_of_staff_finance_tracker_ytd_expands_through_requested_quarter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_schema(**_: object) -> dict[str, object]:
        return {
            "status": "success",
            "schema": {
                "tables": [
                    {
                        "name": "Business Income",
                        "fields": [
                            {"name": "Estimated Tax Periods"},
                            {"name": "Amount"},
                        ],
                    },
                ],
            },
        }

    def fake_read_records(table: str = "", **_: object) -> dict[str, object]:
        assert table == "Business Income"
        return {
            "status": "success",
            "records": [
                {"fields": {"Estimated Tax Periods": 1, "Amount": 100}},
                {"fields": {"Estimated Tax Periods": 2, "Amount": 200}},
                {"fields": {"Estimated Tax Periods": 3, "Amount": 300}},
            ],
        }

    monkeypatch.setattr(chief_of_staff_module, "airtable_get_base_schema_impl", fake_schema)
    monkeypatch.setattr(chief_of_staff_module, "airtable_read_records_impl", fake_read_records)

    result = run_chief_of_staff_sdk(
        "sum Q2 YTD business income from the Airtable tracker",
        live=True,
    )

    assert "$300.00" in result.output.summary
    assert "$600.00" not in result.output.summary


def test_chief_of_staff_finance_tracker_followup_uses_current_request_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_schema(**_: object) -> dict[str, object]:
        return {
            "status": "success",
            "schema": {
                "tables": [
                    {
                        "name": "Business Income",
                        "fields": [
                            {"name": "Estimated Tax Periods"},
                            {"name": "Amount"},
                        ],
                    },
                    {
                        "name": "Personal Expenses",
                        "fields": [
                            {"name": "Estimated Tax Periods"},
                            {"name": "Total Expenses"},
                        ],
                    },
                ],
            },
        }

    def fake_read_records(table: str = "", **_: object) -> dict[str, object]:
        assert table == "Business Income"
        return {
            "status": "success",
            "records": [
                {"fields": {"Estimated Tax Periods": 1, "Amount": 3675}},
                {"fields": {"Estimated Tax Periods": 2, "Amount": 0}},
            ],
        }

    monkeypatch.setattr(chief_of_staff_module, "airtable_get_base_schema_impl", fake_schema)
    monkeypatch.setattr(chief_of_staff_module, "airtable_read_records_impl", fake_read_records)

    result = run_chief_of_staff_sdk(
        (
            "chief of staff continue this prior Slack thread.\n"
            "Previous request: chief of staff sum the Q1 and Q2 personal expenses. "
            "Report expenses for each quarter as a single sum\n"
            "Previous result: Expense totals by quarter in finance_tax_tracker: "
            "Q1: $4,265.88; Q2: $4,504.23.\n"
            "User follow-up: ok what about business income sum for Q1 and Q2"
        ),
        live=True,
    )

    assert "income" in result.output.summary.lower()
    assert "$3,675.00" in result.output.summary
    assert "expense" not in result.output.summary.lower()
    assert "Personal Expenses" not in result.output.summary


def test_chief_of_staff_finance_tracker_tax_table_write_plan_uses_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_tables: list[str] = []

    def fake_schema(**_: object) -> dict[str, object]:
        return {
            "status": "success",
            "schema": {
                "tables": [
                    {"name": "Tax Expenses", "fields": [{"name": "Amount"}]},
                ],
            },
        }

    def fake_write(
        fields_json: str,
        *,
        table: str,
        **_: object,
    ) -> dict[str, object]:
        write_tables.append(table)
        return {"status": "dry-run", "request": {"payload": json.loads(fields_json)}}

    monkeypatch.setattr(chief_of_staff_module, "airtable_get_base_schema_impl", fake_schema)
    monkeypatch.setattr(chief_of_staff_module, "airtable_write_record_impl", fake_write)

    result = run_chief_of_staff_sdk(
        "prepare a dry-run write plan for Tax Expenses in finance_tax_tracker",
        live=True,
    )

    assert write_tables == ["Tax Expenses"]
    assert "Tax Expenses" in result.output.summary
    assert any(
        "Schema/text-aware tax table resolution" in note for note in result.output.audit_notes
    )


def test_chief_of_staff_finance_tracker_prepares_total_expenses_sync_plan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_previews: list[tuple[str, str, dict[str, object], bool]] = []

    def fake_schema(**_: object) -> dict[str, object]:
        return {
            "status": "success",
            "schema": {
                "tables": [
                    {
                        "name": "Business Expenses",
                        "fields": [
                            {"name": "Amount", "is_computed": False},
                            {"name": "Additional Taxes", "is_computed": False},
                            {"name": "Total Expenses", "is_computed": False},
                        ],
                    },
                    {
                        "name": "Personal Expenses",
                        "fields": [
                            {"name": "Amount", "is_computed": False},
                            {"name": "Total Expenses", "is_computed": False},
                        ],
                    },
                ],
            },
        }

    def fake_read_records(table: str = "", **_: object) -> dict[str, object]:
        records_by_table = {
            "Business Expenses": [
                {"id": "rec1", "fields": {"Amount": 100, "Additional Taxes": 8}},
                {
                    "id": "rec2",
                    "fields": {"Amount": 50, "Additional Taxes": 0, "Total Expenses": 55},
                },
            ],
            "Personal Expenses": [
                {"id": "rec3", "fields": {"Amount": 25}},
                {"id": "rec4", "fields": {"Amount": 10, "Total Expenses": 10}},
            ],
        }
        return {"status": "success", "records": records_by_table[table]}

    def fake_write(
        fields_json: str,
        *,
        table: str,
        record_id: str,
        live: bool = False,
        **_: object,
    ) -> dict[str, object]:
        fields = json.loads(fields_json)
        write_previews.append((table, record_id, fields, live))
        return {"status": "success" if live else "dry-run"}

    monkeypatch.setattr(chief_of_staff_module, "airtable_get_base_schema_impl", fake_schema)
    monkeypatch.setattr(chief_of_staff_module, "airtable_read_records_impl", fake_read_records)
    monkeypatch.setattr(chief_of_staff_module, "airtable_write_record_impl", fake_write)

    result = run_chief_of_staff_sdk(
        "sync Amount plus Additional Taxes to Total Expenses in the Airtable tracker",
        live=True,
    )

    assert write_previews == [
        ("Business Expenses", "rec1", {"Total Expenses": 108.0}, False),
        ("Personal Expenses", "rec3", {"Total Expenses": 25.0}, False),
    ]
    assert "2 blank Total Expenses values" in result.output.summary
    assert "1 nonblank mismatch" in result.output.summary
    assert "No live write was performed" in result.output.summary

    review_result = run_chief_of_staff_sdk(
        "review the Airtable tracker data and find missing Total Expenses values",
        live=True,
    )

    assert "I reviewed the expense tables against the live schema" in review_result.output.summary
    assert len(write_previews) == 4
    assert write_previews[-2:] == [
        ("Business Expenses", "rec1", {"Total Expenses": 108.0}, False),
        ("Personal Expenses", "rec3", {"Total Expenses": 25.0}, False),
    ]

    live_result = run_chief_of_staff_sdk(
        "approved: find missing Total Expenses values and fill them in too",
        live=True,
    )

    assert "Live writes were requested for blank values only" in live_result.output.summary
    assert write_previews[-2:] == [
        ("Business Expenses", "rec1", {"Total Expenses": 108.0}, True),
        ("Personal Expenses", "rec3", {"Total Expenses": 25.0}, True),
    ]


def test_chief_of_staff_finance_tracker_update_requests_use_sdk_interpretation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_typed_sdk_agent(**kwargs: object) -> TypedAgentRunResult[ChiefOfStaffResult]:
        captured.update(kwargs)
        output = ChiefOfStaffResult(
            mode="llm",
            summary="Use Airtable tools to resolve one Personal Expenses record before updating.",
            recommended_route=ChiefOfStaffRouteRecommendation(
                workflow_type="budget-resource-review",
                target_channel="docs",
            ),
            audit_notes=[],
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result={"sdk": "called"},
            live=True,
        )

    monkeypatch.setattr(
        "keystone_agents.agents.chief_of_staff.run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    result = run_chief_of_staff_sdk(
        (
            "chief of staff can u update the personal expenses Quarter 3 condo fees "
            "amount/total expenses to the updated value of 1304.88"
        ),
        live=True,
    )

    assert result.raw_result == {"sdk": "called"}
    assert "resolve one Personal Expenses record" in result.output.summary
    assert "update the personal expenses" in str(captured["typed_input"])


def test_chief_of_staff_force_sdk_interpretation_uses_llm_for_finance_language(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_typed_sdk_agent(**kwargs: object) -> TypedAgentRunResult[ChiefOfStaffResult]:
        captured.update(kwargs)
        output = ChiefOfStaffResult(
            mode="llm",
            summary="The LLM should interpret the finance request and choose tools if needed.",
            recommended_route=ChiefOfStaffRouteRecommendation(
                workflow_type="budget-resource-review",
                target_channel="docs",
            ),
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result={"sdk": "called"},
            live=True,
        )

    monkeypatch.setattr(
        "keystone_agents.agents.chief_of_staff.run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    result = run_chief_of_staff_sdk(
        "sum the Q1 personal expenses",
        live=True,
        force_sdk_interpretation=True,
    )

    assert result.raw_result == {"sdk": "called"}
    assert captured["typed_input"] == "sum the Q1 personal expenses"


def test_chief_of_staff_finance_doc_request_uses_live_sdk_not_tax_payment_shortcut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fail_schema(**_: object) -> dict[str, object]:
        raise AssertionError("finance doc artifacts should not use compact tax shortcut")

    def fake_run_typed_sdk_agent(**kwargs: object) -> TypedAgentRunResult[ChiefOfStaffResult]:
        captured.update(kwargs)
        output = ChiefOfStaffResult(
            mode="llm",
            summary=(
                "Created Q1 finance tax analysis doc: "
                "https://docs.google.com/document/d/doc_q1/edit"
            ),
            recommended_route=ChiefOfStaffRouteRecommendation(
                workflow_type="artifact-write-plan",
                target_channel="docs",
            ),
            audit_notes=[],
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result={"sdk": "called"},
            live=True,
        )

    monkeypatch.setattr(chief_of_staff_module, "airtable_get_base_schema_impl", fail_schema)
    monkeypatch.setattr(
        "keystone_agents.agents.chief_of_staff.run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    request = (
        "review airtable tables, analyze and provide a tax summary and analysis "
        "for Q1 in google docs. Create a folder and doc within the gdrive for "
        "the tax updates. Provide a link to the google doc in the reply"
    )
    result = run_chief_of_staff_sdk(request, live=True)

    assert result.raw_result == {"sdk": "called"}
    assert "docs.google.com" in result.output.summary
    assert captured["typed_input"] == request


def test_chief_of_staff_finance_doc_request_dry_plan_includes_doc_and_folder_only() -> None:
    request = (
        "review airtable tables, analyze and provide a tax summary and analysis "
        "for Q1 in google docs. Create a folder and doc within the gdrive for "
        "the tax updates. Provide a link to the google doc in the reply"
    )

    result = run_chief_of_staff_sdk(request, live=False)

    assert result.raw_result == {"deterministic": "finance_tax_tracker_artifact_plan"}
    assert result.output.recommended_route.workflow_type == "artifact-write-plan"
    assert "finance_tax_tracker tax analysis" in result.output.summary
    assert "business research analyst" not in result.output.summary.lower()
    assert "company/contact" not in result.output.summary.lower()
    assert {write.destination.value for write in result.output.write_requests} == {
        "google_doc",
        "google_drive_folder",
    }


def test_chief_of_staff_finance_tracker_record_lookup_uses_schema_tax_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_tables: list[str] = []

    def fake_schema(**_: object) -> dict[str, object]:
        return {
            "status": "success",
            "schema": {
                "tables": [
                    {"name": "Tax Expenses", "fields": [{"name": "Amount"}]},
                ],
            },
        }

    def fake_read_records(table: str = "", **_: object) -> dict[str, object]:
        read_tables.append(table)
        return {"status": "success", "records": [{"fields": {"Amount": 0}}]}

    monkeypatch.setattr(chief_of_staff_module, "airtable_get_base_schema_impl", fake_schema)
    monkeypatch.setattr(chief_of_staff_module, "airtable_read_records_impl", fake_read_records)

    result = run_chief_of_staff_sdk(
        "verify recabcdefghijk in the Tax Expenses table for finance_tax_tracker",
        live=True,
    )

    assert read_tables == ["Tax Expenses"]
    assert "Tax Expenses record" in result.output.summary


def test_internal_data_tools_dry_run_are_gated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIRTABLE_DEFAULT_TABLE", "Companies")
    monkeypatch.setenv("AIRTABLE_BASE_ID", "app_test")

    article = read_linked_article_impl(
        "https://example.com/article",
        request_text="summarize links",
    )
    assert article["status"] == "not_enabled"

    full_article = read_linked_article_impl(
        "https://example.com/article",
        request_text="read the full article",
    )
    assert full_article["status"] == "dry-run"
    explicit_url_extract = read_linked_article_impl(
        "https://example.com/article",
        request_text=(
            "Read/extract these three URLs and return Answer, Detailed Summary, "
            "and Source evidence."
        ),
    )
    assert explicit_url_extract["status"] == "dry-run"

    class FakeExtractionResult:
        status = "success"
        url = "https://example.com/article"
        title = "Example Article"
        provider = "trafilatura"
        text_or_markdown = "Extracted page text for source-aware synthesis."
        claims = ["Source-backed claim"]
        metadata = {"duration_ms": 12, "secret": "redacted_by_safe_metadata"}

    def fake_extract_website_content(*_args: object, **kwargs: object) -> FakeExtractionResult:
        assert kwargs["live"] is True
        return FakeExtractionResult()

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.setattr(
        internal_data_tools,
        "extract_website_content",
        fake_extract_website_content,
    )
    deep_article = read_linked_article_impl(
        "https://example.com/article",
        request_text="deeper source-backed web search with source URLs",
    )
    assert deep_article["status"] == "success"
    assert deep_article["text_or_markdown"] == "Extracted page text for source-aware synthesis."
    extracted_url_article = read_linked_article_impl(
        "https://example.com/article",
        request_text=(
            "Read/extract these three URLs and return Answer, Detailed Summary, "
            "and Source evidence."
        ),
    )
    assert extracted_url_article["status"] == "success"
    assert (
        extracted_url_article["text_or_markdown"]
        == "Extracted page text for source-aware synthesis."
    )

    def fake_extract_website_content_error(*_args: object, **_kwargs: object) -> object:
        raise WebsiteExtractionError(
            "Website extraction failed for https://example.com/blocked with HTTP 403."
        )

    monkeypatch.setattr(
        internal_data_tools,
        "extract_website_content",
        fake_extract_website_content_error,
    )
    blocked_article = read_linked_article_impl(
        "https://example.com/blocked",
        request_text="deeper source-backed web search with source URLs",
    )
    assert blocked_article["status"] == "extraction_failed"
    assert blocked_article["provider"] == "trafilatura"
    assert blocked_article["text_or_markdown"] == ""
    assert "HTTP 403" in blocked_article["error"]

    airtable_read = airtable_read_records_impl("Companies")
    assert airtable_read["status"] == "dry-run"
    assert airtable_read["request"]["table"] == "Companies"

    airtable_write = airtable_write_record_impl('{"Name": "Mentavi"}', table="Companies")
    assert airtable_write["status"] == "dry-run"
    assert airtable_write["request"]["payload"]["fields"]["Name"] == "Mentavi"

    schema = airtable_get_base_schema_impl()
    assert schema["status"] == "dry-run"
    assert schema["schema"]["base_name"] == "2026 Finance & Tax Tracker"

    doc_read = google_doc_read_impl("https://docs.google.com/document/d/doc123/edit")
    assert doc_read["status"] == "dry-run"
    assert doc_read["document_id"] == "doc123"

    doc_write = google_doc_write_impl("Company Note", "Source-backed note.")
    assert doc_write["status"] == "dry-run"
    assert doc_write["title"] == "Company Note"
    assert doc_write["folder_path"] == "KNIOps"

    folder_list = google_drive_list_folder_impl("Research")
    assert folder_list["status"] == "dry-run"
    assert folder_list["folder_path"] == "KNIOps / Research"

    folder_create = google_drive_create_folder_impl("Research/Briefs")
    assert folder_create["status"] == "dry-run"
    assert folder_create["folder_path"] == "KNIOps / Research / Briefs"

    folder_rename = google_drive_rename_folder_impl("Research/Briefs", "Reviewed Briefs")
    assert folder_rename["status"] == "dry-run"
    assert folder_rename["folder_path_or_id"] == "KNIOps / Research / Briefs"
    assert folder_rename["new_name"] == "Reviewed Briefs"

    folder_remove = google_drive_remove_folder_impl("Research/Briefs")
    assert folder_remove["status"] == "dry-run"
    assert folder_remove["folder_path_or_id"] == "KNIOps / Research / Briefs"

    sheet_list = google_sheet_list_impl("Operations")
    assert sheet_list["status"] == "dry-run"
    assert sheet_list["folder_path"] == "KNIOps / Operations"

    sheet_create = google_sheet_create_impl(tabs_json='["Contacts", "Meetings"]')
    assert sheet_create["status"] == "dry-run"
    assert sheet_create["title"] == "KNIOps Structured Data"
    assert sheet_create["tabs"] == ["Contacts", "Meetings"]

    sheet_read = google_sheet_read_table_impl(
        "https://docs.google.com/spreadsheets/d/sheet123/edit"
    )
    assert sheet_read["status"] == "dry-run"
    assert sheet_read["spreadsheet_id"] == "sheet123"

    sheet_append = google_sheet_append_rows_impl(
        '[{"record_key": "contact-1", "email": "example@example.com"}]'
    )
    assert sheet_append["status"] == "dry-run"
    assert sheet_append["headers"] == ["record_key", "email"]


def test_airtable_schema_summary_is_bounded_to_metadata() -> None:
    payload = {
        "tables": [
            {
                "id": "tbl_business_expenses",
                "name": "Business Expenses",
                "primaryFieldId": "fld_payee",
                "fields": [
                    {"id": "fld_payee", "name": "Payee", "type": "singleLineText"},
                    {
                        "id": "fld_category",
                        "name": "Tax Category",
                        "type": "singleSelect",
                        "options": {
                            "choices": [
                                {"id": "sel1", "name": "Software"},
                                {"id": "sel2", "name": "Meals"},
                            ]
                        },
                    },
                    {
                        "id": "fld_review",
                        "name": "Needs Human Tax Review",
                        "type": "formula",
                        "options": {"formula": "IF({Mixed Use}, 1, 0)"},
                    },
                ],
            }
        ]
    }

    summary = airtable_base_schema_summary_from_metadata(
        payload,
        base_id="app_finance",
        allowed_tables=("Business Expenses", "Tax Expenses"),
    )

    assert summary.base_id == "app_finance"
    assert summary.tables[0].name == "Business Expenses"
    assert summary.tables[0].fields[1].select_choices == ["Software", "Meals"]
    assert summary.tables[0].fields[2].is_computed is True
    assert summary.missing_allowed_tables == ["Tax Expenses"]


def test_airtable_schema_live_can_persist_prompt_safe_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AIRTABLE_BASE_ID", "app_finance")
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "pat_test")
    monkeypatch.setenv("AIRTABLE_ALLOWED_TABLES", "Business Expenses,Tax Expenses")

    def fake_send(request: dict[str, object], **_: object) -> dict[str, object]:
        assert request["method"] == "GET"
        return {
            "tables": [
                {
                    "id": "tbl_business_expenses",
                    "name": "Business Expenses",
                    "fields": [{"id": "fld_payee", "name": "Payee", "type": "singleLineText"}],
                }
            ]
        }

    monkeypatch.setattr(internal_data_tools, "_airtable_send", fake_send)
    database_url = f"sqlite:///{tmp_path / 'keystone.db'}"

    result = airtable_get_base_schema_impl(
        live=True,
        persist_memory=True,
        database_url=database_url,
    )
    memories = SQLiteStore(database_url).retrieve_memory(
        query="Airtable schema",
        object_key="finance-tax-tracker",
        memory_types=["operator_reference"],
    )

    assert result["status"] == "success"
    assert result["memory_id"]
    assert memories
    assert "Business Expenses" in memories[0].summary
    assert "pat_test" not in json.dumps(
        [memory.model_dump(mode="json") for memory in memories],
        sort_keys=True,
    )


def test_airtable_default_view_applies_only_to_default_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIRTABLE_BASE_ID", "app_finance")
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "pat_test")
    monkeypatch.setenv("AIRTABLE_DEFAULT_TABLE", "Business Income")
    monkeypatch.setenv("AIRTABLE_DEFAULT_VIEW", "viw_business_income")
    monkeypatch.setenv("AIRTABLE_ALLOWED_TABLES", "Business Income,Business Expenses")
    requests: list[dict[str, object]] = []

    def fake_send(request: dict[str, object], **_: object) -> dict[str, object]:
        requests.append(request)
        return {"records": []}

    monkeypatch.setattr(internal_data_tools, "_airtable_send", fake_send)

    default_table = airtable_read_records_impl("Business Income", live=True)
    other_table = airtable_read_records_impl("Business Expenses", live=True)

    assert default_table["status"] == "success"
    assert other_table["status"] == "success"
    assert requests[0]["params"] == {"maxRecords": 10, "view": "viw_business_income"}
    assert requests[1]["params"] == {"maxRecords": 10}


def test_airtable_fetch_all_paginates_live_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIRTABLE_BASE_ID", "app_finance")
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "pat_test")
    monkeypatch.setenv("AIRTABLE_ALLOWED_TABLES", "Personal Expenses")
    requests: list[dict[str, object]] = []

    def fake_send(request: dict[str, object], **_: object) -> dict[str, object]:
        requests.append(request)
        if "offset" not in request["params"]:
            return {
                "records": [{"id": "rec1", "fields": {"Amount": 1}}],
                "offset": "itr_next",
            }
        return {"records": [{"id": "rec2", "fields": {"Amount": 2}}]}

    monkeypatch.setattr(internal_data_tools, "_airtable_send", fake_send)

    result = airtable_read_records_impl("Personal Expenses", fetch_all=True, live=True)

    assert result["status"] == "success"
    assert [record["id"] for record in result["records"]] == ["rec1", "rec2"]
    assert result["fetch_all"] is True
    assert result["page_count"] == 2
    assert requests[0]["params"] == {"pageSize": 100}
    assert requests[1]["params"] == {"pageSize": 100, "offset": "itr_next"}


def test_airtable_allowed_tables_and_update_matching_are_guarded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIRTABLE_ALLOWED_TABLES", "Business Expenses,Tax Expenses")
    monkeypatch.setenv("AIRTABLE_BASE_ID", "app_finance")

    with pytest.raises(RuntimeError, match="AIRTABLE_ALLOWED_TABLES"):
        airtable_read_records_impl("Personal Expenses")

    blocked = airtable_write_record_impl(
        '{"Tax Category": "Software"}',
        table="Business Expenses",
        operation="update",
    )
    preview = airtable_write_record_impl(
        '{"Tax Category": "Software"}',
        table="Business Expenses",
        operation="update",
        match_filter_formula="{Payee}='OpenAI'",
    )

    assert blocked["status"] == "blocked"
    assert "record_id" in blocked["reason"]
    assert preview["status"] == "dry-run"
    assert "exactly one record" in preview["audit_notes"][1]

    sheet_update = google_sheet_update_row_impl(
        '{"status": "reviewed"}',
        spreadsheet_id_or_url="sheet123",
        key_column="record_key",
        key_value="contact-1",
    )
    assert sheet_update["status"] == "dry-run"
    assert sheet_update["key_value"] == "contact-1"

    sheet_delete = google_sheet_delete_rows_impl("sheet123", row_index=2)
    assert sheet_delete["status"] == "dry-run"
    assert sheet_delete["row_index"] == 2

    tab_create = google_sheet_create_tab_impl("sheet123", "FollowUps")
    assert tab_create["status"] == "dry-run"
    assert tab_create["sheet_name"] == "FollowUps"

    tab_update = google_sheet_update_tab_impl("sheet123", "FollowUps", "ClosedFollowUps")
    assert tab_update["status"] == "dry-run"
    assert tab_update["new_name"] == "ClosedFollowUps"

    tab_remove = google_sheet_remove_tab_impl("sheet123", "ClosedFollowUps")
    assert tab_remove["status"] == "dry-run"
    assert tab_remove["sheet_name"] == "ClosedFollowUps"

    sheet_trash = google_sheet_trash_impl("sheet123")
    assert sheet_trash["status"] == "dry-run"
    assert sheet_trash["spreadsheet_id"] == "sheet123"


def test_airtable_live_write_returns_read_after_write_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIRTABLE_BASE_ID", "app_finance")
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "pat_test")
    monkeypatch.setenv("AIRTABLE_ALLOWED_TABLES", "Personal Expenses")
    monkeypatch.setenv("AIRTABLE_ALLOW_WRITES", "true")
    monkeypatch.setenv("AIRTABLE_WRITE_DRY_RUN", "false")
    requests: list[dict[str, object]] = []

    def fake_send(request: dict[str, object], **_: object) -> dict[str, object]:
        requests.append(request)
        if request["method"] == "PATCH":
            return {
                "id": "rec_verified",
                "fields": {"Amount": 1304.88, "Total Expenses": 1304.88},
            }
        return {
            "records": [
                {
                    "id": "rec_verified",
                    "fields": {"Amount": 1304.88, "Total Expenses": 1304.88},
                }
            ]
        }

    monkeypatch.setattr(internal_data_tools, "_airtable_send", fake_send)

    result = airtable_write_record_impl(
        '{"Amount": 1304.88, "Total Expenses": 1304.88}',
        table="Personal Expenses",
        record_id="rec_verified",
        approval_reference="slack-test-approved",
        operation="update",
        live=True,
    )

    assert result["status"] == "success"
    assert result["record_id"] == "rec_verified"
    assert result["verified_record"]["fields"]["Total Expenses"] == 1304.88
    assert requests[0]["method"] == "PATCH"
    assert requests[1]["method"] == "GET"
    assert "RECORD_ID()" in requests[1]["params"]["filterByFormula"]


def test_chief_of_staff_registry_card_is_canonical() -> None:
    spec = AGENT_REGISTRY["chief_of_staff"]

    assert spec.builder_name == "build_chief_of_staff_agent"
    assert spec.resolve_output_schema() is ChiefOfStaffResult
    assert "chief_of_staff.md" in spec.prompt_files
    assert "tests/test_chief_of_staff.py" in spec.validation_paths
    assert "scoped internal Slack communication" in spec.handoff_description


def test_chief_of_staff_quality_budget_fast_for_simple_scope() -> None:
    budget = chief_of_staff_quality_budget(request_text="what is your scope for this slack?")

    assert budget.mode == QualityMode.FAST
    assert budget.max_turns == 4
    assert budget.reasoning_effort == "low"
    assert budget.enable_context_deepening is False


def test_chief_of_staff_quality_budget_deep_for_cross_channel_audit() -> None:
    budget = chief_of_staff_quality_budget(
        request_text="audit current automations and prepare a cross-channel review plan"
    )

    assert budget.mode == QualityMode.DEEP
    assert budget.max_turns == 14
    assert budget.max_tokens == 6500
    assert budget.enable_context_deepening is True


def test_chief_of_staff_quality_budget_explicit_mode_wins() -> None:
    fast = chief_of_staff_quality_budget("fast", request_text="audit automations", live_sdk=True)
    deep = chief_of_staff_quality_budget("deep", request_text="what is your scope?")

    assert fast.mode == QualityMode.FAST
    assert deep.mode == QualityMode.DEEP


def test_chief_of_staff_builder_uses_budget_model_settings() -> None:
    budget = chief_of_staff_quality_budget("deep", request_text="audit automations")

    agent = build_chief_of_staff_agent(quality_budget=budget)
    settings = agent.model_settings

    assert agent.name == "chief_of_staff"
    assert agent.output_type is ChiefOfStaffResult
    if isinstance(settings, dict):
        assert settings["max_tokens"] == 6500
        assert settings["verbosity"] == "medium"
    else:
        assert getattr(settings, "max_tokens", None) == 6500
        assert getattr(settings, "verbosity", None) == "medium"


def test_chief_of_staff_schema_allows_scoped_internal_slack_posts() -> None:
    result = ChiefOfStaffResult(
        mode="llm",
        summary="Post the approved internal channel summary.",
        recommended_route=ChiefOfStaffRouteRecommendation(
            workflow_type="slack-runtime-review",
            target_channel="grants-and-funding",
            requires_human_approval_before_post=False,
        ),
        blocked_side_effects=[
            "gmail_send",
            "calendar_create_or_update",
            "repo_write",
            "linkedin_publish",
            "crm_write",
        ],
        slack_post_allowed=True,
        slack_post_policy="channel_policy_allowed",
        slack_target_channel="grants-and-funding",
        slack_post_reason="Routine channel summary allowed by channel policy.",
    )

    assert result.slack_post_allowed is True
    assert result.slack_post_policy == "channel_policy_allowed"
    assert result.send_enabled is False


def test_chief_of_staff_schema_blocks_unscoped_slack_posts() -> None:
    with pytest.raises(ValueError):
        ChiefOfStaffResult(
            summary="Unsafe post.",
            slack_post_allowed=True,
            slack_post_policy="requires_human_review",
            slack_target_channel="general",
        )


def test_calendar_meetings_request_routes_read_only_to_meetings_channel() -> None:
    result = plan_chief_of_staff_request(
        "take a look at my calendar and add an update to #meetings"
    )

    assert result.recommended_route.workflow_type == "calendar-read"
    assert result.recommended_route.command_text == "/kni calendar today"
    assert result.recommended_route.target_channel == "meetings"
    assert result.slack_post_allowed is False
    assert result.slack_post_policy == "not_allowed"
    assert result.send_enabled is False
    assert "calendar_create_or_update" in result.blocked_side_effects
    assert result.recommended_route.requires_human_approval_before_post is True


def test_email_onboarding_request_routes_to_gmail_summary_without_send() -> None:
    result = plan_chief_of_staff_request(
        "see my email and send an update to the #onboarding channel"
    )

    assert result.recommended_route.workflow_type == "gmail-summary"
    assert result.recommended_route.command_text == "/kni gmail summarize onboarding"
    assert result.recommended_route.target_channel == "onboarding"
    assert result.send_enabled is False
    assert result.slack_post_allowed is False
    assert "gmail_send" in result.blocked_side_effects
    assert "selected_gmail_context" in result.context_sources_considered
    assert any("Gmail" in source.title or "Slack" in source.title for source in result.sources)


def test_cross_channel_article_followup_request_sets_temporal_capabilities() -> None:
    result = plan_chief_of_staff_request(
        "chief of staff summarize articles and unresolved follow-ups across #docs and "
        "#grants-and-funding today"
    )

    assert result.recommended_route.workflow_type == "slack-cross-channel-review"
    assert result.time_window == "today"
    assert result.target_channels == ["docs", "grants-and-funding"]
    assert "cross_channel_synthesis" in result.operating_capabilities
    assert "article_link_review" in result.operating_capabilities
    assert "follow_up_tracking" in result.operating_capabilities
    assert result.send_enabled is False


def test_company_contact_artifact_write_request_routes_to_structured_plan() -> None:
    result = plan_chief_of_staff_request(
        "chief of staff save contact info and company info for Mentavi to Airtable "
        "and a Google Doc as artifacts"
    )

    assert result.recommended_route.workflow_type == "artifact-write-plan"
    assert "artifact_write_planning" in result.operating_capabilities
    assert result.write_requests
    destinations = {request.destination.value for request in result.write_requests}
    assert {"airtable", "google_doc"} <= destinations
    assert all("contact_candidates" in str(request.metadata) for request in result.write_requests)
    assert result.slack_post_allowed is False


def test_google_doc_text_artifact_request_is_not_limited_to_company_info() -> None:
    result = plan_chief_of_staff_request(
        "chief of staff create a Google Doc named KNI Ops Test with a one sentence "
        "summary note: Chief of Staff can create internal text artifacts in KNIOps."
    )

    assert result.recommended_route.workflow_type == "artifact-write-plan"
    assert "artifact_write_planning" in result.operating_capabilities
    assert [request.destination.value for request in result.write_requests] == ["google_doc"]
    assert result.write_requests[0].title == "Text Artifact"
    assert result.write_requests[0].approval_required is True
    assert "note" in result.write_requests[0].metadata
    assert result.slack_post_allowed is False


def test_google_drive_folder_management_stays_scoped_to_kniops() -> None:
    result = plan_chief_of_staff_request(
        "chief of staff list the Google Drive KNIOps folder and create a subfolder "
        "named Test Briefs if approved; do not delete anything."
    )

    assert result.recommended_route.workflow_type == "google-drive-management"
    assert "google_drive_folder_management" in result.operating_capabilities
    assert [request.destination.value for request in result.write_requests] == [
        "google_drive_folder"
    ]
    assert "KNIOps" in result.summary
    assert any("Do not delete" in action for action in result.recommended_actions)


def test_google_sheets_management_routes_to_structured_data_plan() -> None:
    result = plan_chief_of_staff_request(
        "chief of staff create a Google Sheet for structured contacts in KNIOps "
        "and append one row if approved"
    )

    assert result.recommended_route.workflow_type == "google-drive-management"
    assert "google_sheets_structured_data" in result.operating_capabilities
    assert [request.destination.value for request in result.write_requests] == ["google_sheet"]
    assert "KNIOps Structured Data" in result.summary
    assert any("Trash" in action or "trash" in action for action in result.recommended_actions)


def test_project_context_request_routes_to_flexible_context_review() -> None:
    result = plan_chief_of_staff_request(
        "chief of staff obtain context on the adolescent depression measurement project"
    )

    assert result.recommended_route.workflow_type == "project-context-review"
    assert "project_context" in result.operating_capabilities
    assert "business_workflow_state" in result.context_sources_considered
    assert any("known" in action.lower() for action in result.recommended_actions)
    assert result.send_enabled is False


def test_budget_request_requires_known_records_or_validation_plan() -> None:
    result = plan_chief_of_staff_request(
        "chief of staff what is the budget for the youth anxiety pilot?"
    )

    assert result.recommended_route.workflow_type == "budget-resource-review"
    assert "budget_aware_execution" in result.operating_capabilities
    assert "never invent numbers" not in result.summary.lower()
    assert any("known amounts" in action.lower() for action in result.recommended_actions)
    assert result.slack_post_allowed is False


def test_meeting_prep_request_stays_read_only() -> None:
    result = plan_chief_of_staff_request(
        "chief of staff prepare me for a meeting with Headway about measurement-based care"
    )

    assert result.recommended_route.workflow_type == "meeting-prep"
    assert "meeting_prep" in result.operating_capabilities
    assert "selected_calendar_context" in result.context_sources_considered
    assert "calendar_create_or_update" in result.blocked_side_effects


def test_outreach_request_routes_to_draft_only_owner() -> None:
    result = plan_chief_of_staff_request(
        "chief of staff write an email to Alex about a behavioral health analytics intro"
    )

    assert result.recommended_route.workflow_type == "business-agents-route"
    assert "outreach composer" in result.recommended_route.command_text.lower()
    assert "outreach_drafting" in result.operating_capabilities
    assert any("human approval" in action.lower() for action in result.recommended_actions)
    assert result.send_enabled is False


def test_document_and_portfolio_requests_have_distinct_intents() -> None:
    docs = plan_chief_of_staff_request(
        "chief of staff review these docs and extract decisions, risks, and claims"
    )
    portfolio = plan_chief_of_staff_request(
        "chief of staff give me a weekly executive summary across active projects"
    )

    assert docs.recommended_route.workflow_type == "slack-docs-review"
    assert "document_review" in docs.operating_capabilities
    assert portfolio.recommended_route.workflow_type == "portfolio-review"
    assert "portfolio_oversight" in portfolio.operating_capabilities


def test_scope_question_returns_scope_plan_not_clarification() -> None:
    result = plan_chief_of_staff_request("what is your scope for this slack?")

    assert result.recommended_route.workflow_type == "slack-runtime-review"
    assert result.recommended_route.command_text.startswith("@KNI chief of staff")
    assert result.recommended_route.target_channel == "current-thread"
    assert result.summary == "Explain Chief of Staff scope for KNI Slack operations."
    assert result.slack_post_allowed is False
    assert any("Plan KNI Slack routing" in action for action in result.recommended_actions)
    assert "keystone_slack_runtime_repo" in result.context_sources_considered


def test_web_search_brief_fallback_does_not_return_scope_blurb() -> None:
    request = (
        '"What is OpenAI doing about mental health? Please do a deeper search and '
        "give me a readable brief, not a diagnostics report. Include a short answer, "
        'a synthesis with source URLs, key concerns, and metadata with providers used."'
    )

    result = plan_chief_of_staff_request(request)

    assert result.recommended_route.workflow_type == "research-direction-review"
    assert result.recommended_route.requires_live_connector is True
    assert result.summary != "Explain Chief of Staff scope for KNI Slack operations."
    assert "source-backed web brief request" in result.summary
    assert "web_search_brief" in result.operating_capabilities
    assert any("Answer and Detailed Summary" in action for action in result.recommended_actions)
    assert not any("Answer and Synthesis" in action for action in result.recommended_actions)
    assert any(
        "Do not render this as a diagnostics report" in action
        for action in result.recommended_actions
    )


def test_chief_request_plan_marks_web_brief_as_live_source_research() -> None:
    plan = chief_of_staff_module._chief_request_plan(
        (
            '"What is OpenAI doing about mental health? Please do a deeper search and '
            'give me a readable brief with source URLs and metadata." *Sent using*'
        ),
        None,
    )

    assert plan.intent == "research_brief"
    assert plan.task_objective == "source_research"
    assert plan.expected_artifact_type == "research_brief"
    assert plan.requires_live_search is True
    assert "visible source URLs" in plan.constraints


def test_supplied_slack_history_digest_renders_timestamped_answer() -> None:
    result = plan_chief_of_staff_request(
        "\n".join(
            [
                "chief of staff what were the last articles posted in "
                "#grants-and-funding regarding?",
                "",
                "Read-only Slack message-history context supplied by the KNI Slack runtime.",
                "Slack channel history digest:",
                "Channel: #grants-and-funding",
                "Channel id: C0ASKGN9946",
                "Messages reviewed: 3",
                "Recent candidate messages, newest first:",
                (
                    "- ts=1778779000.000100 author=UKNI title=Early psychosis prediction: "
                    "ClinicalTrials.gov watchlist generated for `early psychosis prediction`. "
                    "*Workflow:* `trials-watch` *Status:* `ok` *Run ID:* `run_123`"
                ),
                (
                    "- ts=1778778000.000100 author=UKNI title=Esketamine and bipolar: "
                    "ClinicalTrials.gov watchlist generated for `esketamine bipolar`. "
                    "*Workflow:* `trials-watch` *Status:* `ok`"
                ),
                (
                    "- ts=1778777000.000100 author=UKNI title=Topics:: "
                    "Topics: translational psychiatry | Score: 1 *Matched on:* topic focus "
                    "*Link:* *Summary:* Long trial summary"
                ),
            ]
        )
    )

    assert result.recommended_route.workflow_type == "slack-runtime-review"
    assert result.mode == "deterministic"
    assert result.time_window == "recent"
    assert result.target_channels == ["grants-and-funding"]
    assert "article_link_review" in result.operating_capabilities
    assert "Summary:" in result.summary
    assert "The supplied channel history centers on early psychosis prediction" in result.summary
    assert "Useful follow-ups:" in result.summary
    assert "Review `early psychosis prediction` from 2026-05-14 13:16 EDT" in result.summary
    assert "Review `esketamine bipolar` from 2026-05-14 13:00 EDT" in result.summary
    assert (
        "Review `translational psychiatry | Score: 1` from 2026-05-14 12:43 EDT" in result.summary
    )
    assert "Metadata:" not in result.summary
    assert "run_123" not in result.summary
    assert "Matched on" not in result.summary
    assert "\n\nTheme:" in result.summary
    assert "Theme:" in result.summary
    assert "supplied_slack_message_history_digest" in result.context_sources_considered
    assert result.slack_post_allowed is False


def test_run_chief_of_staff_sdk_passes_budget_to_typed_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_typed_sdk_agent(**kwargs: object) -> TypedAgentRunResult[ChiefOfStaffResult]:
        captured.update(kwargs)
        output = ChiefOfStaffResult(
            mode="llm",
            summary="Captured budgeted plan.",
            recommended_route=ChiefOfStaffRouteRecommendation(
                workflow_type="slack-runtime-review",
                target_channel="ai-agents-workflow",
            ),
            audit_notes=[],
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result={"fake": True},
            live=True,
        )

    monkeypatch.setattr(
        "keystone_agents.agents.chief_of_staff.run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    result = run_chief_of_staff_sdk(
        {"request": "audit automations"},
        live=True,
        quality_mode="deep",
    )

    assert captured["max_turns"] == 14
    trace_metadata = captured["trace_metadata"]
    assert isinstance(trace_metadata, dict)
    assert trace_metadata["quality_mode"] == "deep"
    assert trace_metadata["quality_max_turns"] == 14
    assert trace_metadata["quality_reasoning_effort"] == "medium"
    assert "audit automations" not in str(trace_metadata)
    assert result.output.approval_required is True
    assert result.output.send_enabled is False
    assert any("quality budget used: deep" in note for note in result.output.audit_notes)


def test_run_chief_of_staff_sdk_prioritizes_latest_slack_followup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_typed_sdk_agent(**kwargs: object) -> TypedAgentRunResult[ChiefOfStaffResult]:
        captured.update(kwargs)
        output = ChiefOfStaffResult(
            mode="llm",
            summary="Checklist ready.",
            recommended_route=ChiefOfStaffRouteRecommendation(
                workflow_type="slack-follow-up-review",
                target_channel="ai-agents-workflow",
            ),
            recommended_actions=[
                "Confirm the initial Chief of Staff answer uses current evidence labels.",
                "Run the same Chief of Staff prompt in a fresh Slack thread.",
            ],
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result={"fake": True},
            live=True,
        )

    monkeypatch.setattr(
        "keystone_agents.agents.chief_of_staff.run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    raw_request = (
        "chief of staff continue this prior Slack thread.\n"
        "Previous request: chief of staff review the current business-agent testing state.\n"
        "Previous result: Business Research Analyst should be tested next.\n"
        "User follow-up: Can you turn that into a concise ordered checklist and say "
        "which agent we should test next?\n"
        "Continue the same agent task."
    )

    result = run_chief_of_staff_sdk({"request": raw_request}, live=True)

    typed_input = captured["typed_input"]
    assert isinstance(typed_input, dict)
    assert typed_input["request"].startswith("Can you turn that into")
    assert typed_input["latest_operator_request"] == typed_input["request"]
    assert typed_input["raw_slack_thread_request"] == raw_request
    assert "checklist" in typed_input["response_shape_instruction"].lower()
    assert "Business Research Analyst should be tested next" not in typed_input["request"]
    assert result.output.recommended_actions


def test_run_chief_of_staff_sdk_uses_llm_for_finance_aggregate_when_forced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_typed_sdk_agent(**kwargs: object) -> TypedAgentRunResult[ChiefOfStaffResult]:
        captured.update(kwargs)
        output = ChiefOfStaffResult(
            mode="llm",
            summary="Interpreted by the Chief of Staff LLM path.",
            recommended_route=ChiefOfStaffRouteRecommendation(
                workflow_type="budget-resource-review",
                target_channel="docs",
            ),
        )
        return TypedAgentRunResult(
            agent_name="chief_of_staff",
            output=output,
            raw_result={"sdk": "called"},
            live=True,
        )

    monkeypatch.setattr(
        "keystone_agents.agents.chief_of_staff.run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    result = run_chief_of_staff_sdk(
        "sum the Q1 personal expenses",
        live=True,
        force_sdk_interpretation=True,
    )

    assert result.raw_result == {"sdk": "called"}
    assert captured["typed_input"] == "sum the Q1 personal expenses"


def test_reference_capture_request_saves_operator_memory(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'keystone.db'}"
    url = "https://braininitiative.nih.gov/news-events/blog/register-now-nih-brain-neuroai-workshop"

    result = plan_chief_of_staff_request(
        (
            "chief of staff keep this for future reference: "
            f"NIH AI conference with virtual attendees: {url}"
        ),
        database_url=database_url,
    )

    assert result.recommended_route.workflow_type == "reference-capture"
    assert result.summary.startswith("Saved reference for future use:")
    assert result.slack_post_allowed is False
    assert result.artifact_refs
    assert result.artifact_refs[0].artifact_type == "operator_reference_memory"
    assert result.artifact_refs[0].url == url

    memories = SQLiteStore(database_url).retrieve_memory(
        "NIH AI conference",
        memory_types=["operator_reference"],
    )
    assert len(memories) == 1
    assert memories[0].title == "NIH AI conference with virtual attendees"
    assert memories[0].content["url"] == url


def test_chief_of_staff_strategic_memory_capture_saves_typed_memory(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'keystone.db'}"

    result = plan_chief_of_staff_request(
        (
            "chief of staff remember this as a goal for Project Lighthouse: "
            "support multiple behavioral health projects with one chief of staff agent"
        ),
        database_url=database_url,
    )

    assert result.recommended_route.workflow_type == "reference-capture"
    assert result.artifact_refs[0].artifact_type == "chief_of_staff_memory"
    assert json.loads(result.artifact_refs[0].metadata)["memory_type"] == "project_goal"

    memories = SQLiteStore(database_url).retrieve_memory(
        "behavioral health projects",
        memory_types=["project_goal"],
    )
    assert len(memories) == 1
    assert memories[0].object_key == "lighthouse"
    assert memories[0].safe_for_prompt is True


def test_project_context_request_uses_only_approved_chief_memory(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'keystone.db'}"
    store = SQLiteStore(database_url)
    store.save_memory_item(
        chief_of_staff_memory_item(
            memory_type="project_goal",
            title="Lighthouse goal",
            summary="Build a reusable Chief of Staff layer for behavioral health projects.",
            object_id="Lighthouse",
            object_key="Lighthouse",
            source_ids=["operator"],
        )
    )
    store.save_memory_item(
        chief_of_staff_memory_item(
            memory_type="project_constraint",
            title="Draft constraint",
            summary="This pending item must not be visible yet.",
            object_id="Lighthouse",
            object_key="Lighthouse",
            approval_state="pending",
            source_ids=["operator"],
        )
    )

    result = plan_chief_of_staff_request(
        "chief of staff obtain context on Project Lighthouse",
        database_url=database_url,
    )

    assert result.recommended_route.workflow_type == "project-context-review"
    assert result.memory_context is not None
    assert [item.title for item in result.memory_context.records] == ["Lighthouse goal"]
    assert any("Lighthouse goal" in action for action in result.recommended_actions)


def test_budget_request_with_no_memory_uses_validation_plan(tmp_path: Path) -> None:
    result = plan_chief_of_staff_request(
        "chief of staff what is budget for Project Beacon?",
        database_url=f"sqlite:///{tmp_path / 'keystone.db'}",
    )

    assert result.recommended_route.workflow_type == "budget-resource-review"
    assert result.memory_context is not None
    assert result.memory_context.records == []
    assert "No approved prompt-safe" in result.memory_context.missing_reason
    assert any("validation plan" in action.lower() for action in result.recommended_actions)


def test_slack_repo_context_tools_are_read_only_and_secret_filtered(tmp_path: Path) -> None:
    repo = tmp_path / "keystone-slack"
    package = repo / "kni_integrations"
    package.mkdir(parents=True)
    (repo / "AGENTS.md").write_text("Slack is the primary frontend.\n", encoding="utf-8")
    (package / "slack_socket_mode.py").write_text(
        "Socket Mode receives slash commands and sends acknowledgements.\n",
        encoding="utf-8",
    )
    (repo / ".env").write_text("SLACK_BOT_TOKEN=fake-slack-token\n", encoding="utf-8")

    search = _payload(search_slack_repo_context("Socket Mode", repo_path=str(repo)))
    assert search["send_enabled"] is False
    assert search["matches"]
    assert search["matches"][0]["relative_path"] == "kni_integrations/slack_socket_mode.py"

    read = _payload(read_slack_repo_context_file("AGENTS.md", repo_path=str(repo)))
    assert read["repo_write_enabled"] is False
    assert "Slack is the primary frontend" in read["content"]

    with pytest.raises(ValueError):
        read_slack_repo_context_file(".env", repo_path=str(repo))


def test_runtime_summary_and_docs_catalog_are_capped_and_official(tmp_path: Path) -> None:
    repo = tmp_path / "keystone-slack"
    package = repo / "kni_integrations"
    manifest_dir = repo / "slack"
    package.mkdir(parents=True)
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "kni-app-manifest.yaml").write_text(
        "oauth_config:\n  scopes:\n    bot:\n      - channels:history\n",
        encoding="utf-8",
    )
    (package / "config.py").write_text(
        'gmail_channel=get("SLACK_GMAIL_CHANNEL", "gmail") or "gmail"\n',
        encoding="utf-8",
    )

    summary = _payload(summarize_slack_runtime_config(repo_path=str(repo)))
    assert summary["send_enabled"] is False
    assert summary["key_files_present"]["slack/kni-app-manifest.yaml"] is True
    assert summary["default_channels"]["gmail_channel"] == "gmail"

    docs = _payload(search_official_operations_docs("Slack Socket Mode OpenAI Agents SDK"))
    urls = [item["url"] for item in docs["results"]]
    assert any(url.startswith("https://docs.slack.dev/") for url in urls)
    assert any(url.startswith("https://developers.openai.com/") for url in urls)

    context_sources = _payload(list_chief_of_staff_context_sources())
    source_ids = [item["source_id"] for item in context_sources["sources"]]
    assert "selected_gmail_context" in source_ids
    assert "selected_calendar_context" in source_ids
    assert "github_and_local_repos" in source_ids
    assert context_sources["slack_post_allowed"] is False


def test_lookup_slack_workflow_capability_blocks_posts() -> None:
    payload = _payload(lookup_slack_workflow_capability("calendar next week to #meetings"))

    assert payload["send_enabled"] is False
    assert payload["slack_post_allowed"] is False
    assert payload["capability"]["workflow_type"] == "calendar-read"
    assert payload["capability"]["target_channel"] == "meetings"
