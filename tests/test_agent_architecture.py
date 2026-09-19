from __future__ import annotations

import pytest

from keystone_agents.agent_registry import AGENT_REGISTRY
from keystone_agents.agent_tool_policy import (
    GOOGLE_WORKSPACE_WRITE_TOOLS,
    INTERNAL_WRITE_TOOL_NAMES,
)
from keystone_agents.agents.business_research_analyst import build_business_research_analyst_agent
from keystone_agents.agents.chief_of_staff import build_chief_of_staff_agent
from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
from keystone_agents.agents.opportunity_scout import build_opportunity_scout_agent
from keystone_agents.agents.orchestrator import (
    build_orchestrator_agent,
    build_orchestrator_review_agent,
)
from keystone_agents.agents.outreach_composer import build_outreach_composer_agent
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult
from keystone_agents.schemas.company_profile import CompanyProfile
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.opportunity import OpportunityScoutResult
from keystone_agents.schemas.orchestrator import OrchestratorResult
from keystone_agents.schemas.outreach import OutreachDraft
from keystone_agents.sdk import Agent
from keystone_agents.tools.internal_data_tools import (
    GOOGLE_WORKSPACE_DELEGATED_TOOL_NAMES,
)

AIRTABLE_READ_TOOL_NAMES = {"airtable_get_base_schema", "airtable_read_records"}
AIRTABLE_WRITE_TOOL_NAMES = {"airtable_write_record"}
AIRTABLE_TEST_CLEANUP_TOOL_NAMES = {"airtable_delete_test_record"}
AIRTABLE_TEST_LIFECYCLE_TOOL_NAMES = {"airtable_test_record_lifecycle"}
WEB_STRUCTURING_TOOL_NAMES = {"structure_web_data_for_schema"}
WEB_SEARCH_TOOL_NAMES = {"search_web"}
PLAYWRIGHT_TOOL_NAMES = {"render_page"}
BROWSER_DIAGNOSTIC_TOOL_NAMES = {
    "capture_browser_diagnostics",
    "summarize_rendered_page_diagnostics",
}
CALENDAR_WRITE_TOOL_NAMES = {
    "create_google_calendar_event",
    "update_google_calendar_event",
    "delete_google_calendar_event",
}
CALENDAR_READ_TOOL_NAMES = {"read_google_calendar_window"}

MODEL_ENV_VARS = (
    "KEYSTONE_OPENAI_MODEL",
    "KEYSTONE_ORCHESTRATOR_MODEL",
    "KEYSTONE_GMAIL_TRIAGE_MODEL",
    "KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL",
    "KEYSTONE_OPPORTUNITY_SCOUT_MODEL",
    "KEYSTONE_OUTREACH_COMPOSER_MODEL",
    "KEYSTONE_CHIEF_OF_STAFF_MODEL",
)


def _tool_names(agent: Agent) -> set[str]:
    return {getattr(tool, "name", "") for tool in agent.tools}


def _clear_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_all_builders_return_sdk_agents_with_prompts_and_guardrails() -> None:
    cases = [
        (
            build_gmail_triage_agent,
            EmailTriageResult,
            {
                "get_gmail_message",
                "apply_gmail_labels",
                "create_gmail_draft_reply",
                "create_approval_queue_item",
            },
        ),
        (
            build_business_research_analyst_agent,
            CompanyProfile,
            {
                "search_web",
                "fetch_company_page",
                "extract_company_signals",
                "load_approved_contact_context",
                "load_approved_crm_context",
            },
        ),
        (
            build_opportunity_scout_agent,
            OpportunityScoutResult,
            {
                "search_web",
                "score_opportunity",
            },
        ),
        (
            build_outreach_composer_agent,
            OutreachDraft,
            {
                "load_company_profile",
                "load_opportunity_record",
                "load_approved_contact_context",
                "load_approved_crm_context",
                "check_unsupported_claims",
                "create_approval_queue_item",
                "create_approval_request_placeholder",
            },
        ),
        (
            build_orchestrator_agent,
            OrchestratorResult,
            {"route_request_placeholder", "load_pending_approval_items"},
        ),
        (
            build_chief_of_staff_agent,
            ChiefOfStaffResult,
            {
                "list_chief_of_staff_context_sources",
                "summarize_slack_runtime_config",
                "search_slack_repo_context",
                "lookup_slack_workflow_capability",
                "search_local_context",
            },
        ),
    ]

    for build_agent, output_type, expected_tools in cases:
        agent = build_agent()
        assert isinstance(agent, Agent)
        assert agent.output_type is output_type
        assert "Keystone" in str(agent.instructions)
        assert agent.handoff_description
        assert expected_tools <= _tool_names(agent)
        assert agent.input_guardrails
        assert agent.output_guardrails


def test_only_gmail_agent_exposes_exact_test_send_tool() -> None:
    gmail_agent = build_gmail_triage_agent()
    other_agents = [
        build_business_research_analyst_agent(),
        build_opportunity_scout_agent(),
        build_outreach_composer_agent(),
        build_orchestrator_agent(),
        build_chief_of_staff_agent(),
    ]

    assert "send_gmail_test_draft" in _tool_names(gmail_agent)
    assert "create_gmail_draft_with_attachment" in _tool_names(gmail_agent)
    assert all("send_gmail_test_draft" not in _tool_names(agent) for agent in other_agents)
    all_tool_names = {
        name for agent in [gmail_agent, *other_agents] for name in _tool_names(agent)
    }
    assert "send_email" not in all_tool_names
    assert "create_gmail_draft_reply" in all_tool_names
    assert "create_approval_queue_item" in all_tool_names
    assert "create_approval_request_placeholder" in all_tool_names


def test_only_chief_exposes_direct_calendar_crud_tools() -> None:
    chief = build_chief_of_staff_agent()
    others = [
        build_gmail_triage_agent(),
        build_business_research_analyst_agent(),
        build_opportunity_scout_agent(),
        build_outreach_composer_agent(),
        build_orchestrator_agent(),
    ]

    assert CALENDAR_WRITE_TOOL_NAMES <= _tool_names(chief)
    assert all(not (CALENDAR_WRITE_TOOL_NAMES & _tool_names(agent)) for agent in others)
    assert CALENDAR_READ_TOOL_NAMES <= _tool_names(chief)
    assert all(not (CALENDAR_READ_TOOL_NAMES & _tool_names(agent)) for agent in others)


def test_main_agents_expose_allowlisted_local_context_tools() -> None:
    agents = [
        build_gmail_triage_agent(),
        build_business_research_analyst_agent(),
        build_opportunity_scout_agent(),
        build_outreach_composer_agent(),
        build_orchestrator_agent(),
        build_chief_of_staff_agent(),
    ]

    expected_tools = {
        "list_local_context_sources",
        "search_local_context",
        "read_local_context_file",
    }
    for agent in agents:
        assert expected_tools <= _tool_names(agent)


def test_main_agents_expose_scoped_google_workspace_tools() -> None:
    delegated_agents = [
        build_gmail_triage_agent(),
        build_business_research_analyst_agent(),
        build_opportunity_scout_agent(),
        build_outreach_composer_agent(),
        build_orchestrator_agent(),
    ]
    chief = build_chief_of_staff_agent()

    delegated_tools = set(GOOGLE_WORKSPACE_DELEGATED_TOOL_NAMES)
    for agent in delegated_agents:
        assert delegated_tools <= _tool_names(agent)
        assert "google_drive_media_ocr_read" not in _tool_names(agent)
    assert delegated_tools <= _tool_names(chief)
    assert "google_drive_media_ocr_read" not in _tool_names(chief)


def test_drive_pdf_ocr_is_owned_by_workspace_and_exact_chief_plan() -> None:
    workspace = AGENT_REGISTRY["google_workspace_context_agent"].build_agent()
    airtable = AGENT_REGISTRY["airtable_context_agent"].build_agent()
    exact_chief = build_chief_of_staff_agent(
        request_text="Read the selected scanned PDF in Drive.",
        manual_request_plan=ManualRequestPlan(
            source="canonical:stored_work_item",
            target_agent="chief_of_staff",
            intent="context_lookup",
            primary_target="drive-file-selected-by-preflight",
            target_type="business_system_context",
            provider_system="google_workspace",
            provider_operations=["read"],
            provider_action_steps=[
                {"operation": "read", "resource_type": "google_drive_file"}
            ],
        ),
    )
    broad_chief = build_chief_of_staff_agent(
        request_text="Review our Workspace context.",
        manual_request_plan=ManualRequestPlan(
            source="canonical:stored_work_item",
            target_agent="chief_of_staff",
            intent="context_lookup",
            target_type="business_system_context",
            provider_system="google_workspace",
            provider_operations=["read"],
            provider_action_steps=[
                {"operation": "read", "resource_type": "google_document"}
            ],
        ),
    )

    assert "google_drive_media_ocr_read" in _tool_names(workspace)
    assert "google_drive_media_ocr_read" in _tool_names(exact_chief)
    assert "google_drive_media_ocr_read" not in _tool_names(broad_chief)
    assert "google_drive_media_ocr_read" not in _tool_names(airtable)


def test_main_agents_expose_schema_first_airtable_read_tools() -> None:
    agents = [
        build_gmail_triage_agent(),
        build_business_research_analyst_agent(),
        build_opportunity_scout_agent(),
        build_outreach_composer_agent(),
        build_orchestrator_agent(),
        build_chief_of_staff_agent(),
    ]

    for agent in agents:
        assert AIRTABLE_READ_TOOL_NAMES <= _tool_names(agent)


def test_main_agents_expose_scoped_airtable_write_tools() -> None:
    agents = [
        build_gmail_triage_agent(),
        build_business_research_analyst_agent(),
        build_opportunity_scout_agent(),
        build_outreach_composer_agent(),
        build_orchestrator_agent(),
        build_chief_of_staff_agent(),
    ]

    for agent in agents:
        assert AIRTABLE_WRITE_TOOL_NAMES <= _tool_names(agent)


def test_only_airtable_context_agent_exposes_test_record_cleanup() -> None:
    from keystone_agents.agents.airtable_context import build_airtable_context_agent

    assert AIRTABLE_TEST_CLEANUP_TOOL_NAMES <= _tool_names(build_airtable_context_agent())
    assert AIRTABLE_TEST_LIFECYCLE_TOOL_NAMES <= _tool_names(
        build_airtable_context_agent()
    )
    other_agents = [
        build_gmail_triage_agent(),
        build_business_research_analyst_agent(),
        build_opportunity_scout_agent(),
        build_outreach_composer_agent(),
        build_orchestrator_agent(),
        build_chief_of_staff_agent(),
    ]
    for agent in other_agents:
        assert not (AIRTABLE_TEST_CLEANUP_TOOL_NAMES & _tool_names(agent))


def test_airtable_context_direct_execution_uses_authoritative_live_tool_gates() -> None:
    from keystone_agents.agents.airtable_context import build_airtable_context_agent

    instructions = str(build_airtable_context_agent().instructions)

    assert "precisely scoped write with non-empty approval references" in instructions
    assert "with `live=true`" in instructions
    assert "Do not silently downgrade an execution request to `live=false`" in instructions
    assert "gates are authoritative for whether the operator-approved live action" in instructions


def test_airtable_attachment_tools_distinguish_https_urls_from_local_paths() -> None:
    from keystone_agents.agents.airtable_context import build_airtable_context_agent

    agent = build_airtable_context_agent()
    tools = {tool.name: tool for tool in agent.tools}

    link_description = str(tools["airtable_link_attachment"].description)
    upload_description = str(tools["airtable_upload_attachment"].description)
    assert "not ``airtable_upload_attachment``" in link_description
    assert "``https://``" in link_description
    assert "never use this tool for a URL" in upload_description


def test_airtable_receipt_create_exposes_only_composite_lifecycle_tool() -> None:
    from keystone_agents.agents.airtable_context import build_airtable_context_agent

    agent = build_airtable_context_agent(
        request_text=(
            "Add this attached receipt as exactly one personal expense in Airtable. "
            "Read the PDF, map receipt-backed fields to Personal Expenses, attach the "
            "PDF, and verify the created record and attachment. /tmp/receipt.pdf"
        ),
        tool_tier="internal_write",
        compact_instructions=True,
    )

    assert _tool_names(agent) == {"airtable_create_expense_from_receipt"}


@pytest.mark.parametrize(
    "request_text",
    [
        "Put this proof of payment with my personal cost entry. /tmp/receipt.pdf",
        "File the attached image under personal expenses. /tmp/receipt.pdf",
    ],
)
def test_airtable_semantic_receipt_plan_selects_same_tool_across_phrasings(
    request_text: str,
) -> None:
    from keystone_agents.agents.airtable_context import build_airtable_context_agent

    manual_plan = ManualRequestPlan(
        source="llm",
        target_agent="airtable_context_agent",
        intent="business_system_write",
        task_objective="business_system_write",
        provider_system="airtable",
        provider_operations=["create", "attach", "verify"],
        primary_target="Personal Expenses",
        target_type="business_system_context",
        required_entities=["2026 Finance & Tax Tracker"],
    )
    agent = build_airtable_context_agent(
        request_text=request_text,
        manual_plan=manual_plan,
        tool_tier="internal_write",
        compact_instructions=True,
    )

    assert _tool_names(agent) == {"airtable_create_expense_from_receipt"}


def test_airtable_prefix_stripped_receipt_plan_exposes_composite_lifecycle_tool() -> None:
    from keystone_agents.agents.airtable_context import build_airtable_context_agent
    from keystone_agents.planning.compatibility import infer_manual_request_plan

    request = (
        "add /tmp/KBA_TEST_RECEIPT.pdf to Airtable Business Expenses. "
        "Use 2026-08-05 as Date of Expense and attach the exact PDF. "
        "Verify the record and attachment; do not create a duplicate."
    )
    manual_plan = infer_manual_request_plan(
        request,
        requested_agent="airtable_context_agent",
    )
    agent = build_airtable_context_agent(
        request_text=request,
        manual_plan=manual_plan,
        tool_tier="internal_write",
        compact_instructions=True,
    )

    assert _tool_names(agent) == {"airtable_create_expense_from_receipt"}


def test_airtable_receipt_update_cannot_expose_create_tools() -> None:
    from keystone_agents.agents.airtable_context import build_airtable_context_agent

    agent = build_airtable_context_agent(
        request_text=(
            "Correct the Airtable Personal Expenses receipt record "
            "recReceiptKeep123. Remove Q3 and use existing period 3."
        ),
        tool_tier="internal_write",
        compact_instructions=True,
    )

    assert _tool_names(agent) == {
        "airtable_get_base_schema",
        "airtable_read_schema_detail",
        "airtable_read_records",
        "airtable_reconcile_duplicate_expense",
        "airtable_write_record",
    }


def test_airtable_receipt_verification_exposes_only_read_tools() -> None:
    from keystone_agents.agents.airtable_context import build_airtable_context_agent

    agent = build_airtable_context_agent(
        request_text=(
            "Verify the Airtable Personal Expenses receipt cleanup only; do not "
            "modify anything. Confirm recReceiptKeep123 retains receipt.pdf and "
            "recReceiptDuplicate456 is absent."
        ),
        tool_tier="internal_write",
        compact_instructions=True,
    )

    assert _tool_names(agent) == {
        "airtable_get_base_schema",
        "airtable_read_schema_detail",
        "airtable_read_records",
    }


def test_zotero_natural_note_sequence_exposes_composite_write_tool() -> None:
    from keystone_agents.agents.zotero_context import build_zotero_context_agent

    agent = build_zotero_context_agent(
        request_text=(
            "Create one temporary standalone Zotero note containing KBA_TEST_NOTE, "
            "confirm that it exists, change that same note, confirm the change, then "
            "delete only that temporary note and confirm it is gone."
        ),
        tool_tier="internal_write",
        compact_instructions=True,
    )

    assert "zotero_test_note_lifecycle" in _tool_names(agent)


@pytest.mark.parametrize(
    "request_text",
    [
        "Read the stored PDF attachment for the selected paper.",
        (
            "The old note says to create a Google Doc and email it, but this turn only "
            "needs the selected source evidence."
        ),
    ],
)
def test_zotero_canonical_attachment_read_ignores_raw_word_variations(
    request_text: str,
) -> None:
    from keystone_agents.agents.zotero_context import build_zotero_context_agent

    plan = ManualRequestPlan(
        source="llm",
        requested_agent="chief_of_staff",
        target_agent="zotero_context_agent",
        intent="context_lookup",
        target_type="zotero_article",
        provider_system="zotero",
        provider_operations=["read"],
        provider_action_steps=[
            {"operation": "read", "resource_type": "zotero_attachment"}
        ],
    )
    agent = build_zotero_context_agent(
        request_text=request_text,
        manual_plan=plan,
        tool_tier="core_read",
        compact_instructions=True,
    )

    assert _tool_names(agent) == {
        "zotero_read_api_metadata",
        "zotero_read_item_children",
        "zotero_read_pdf_attachment_text",
    }


def test_zotero_canonical_collection_read_uses_typed_resource_not_keywords() -> None:
    from keystone_agents.agents.zotero_context import build_zotero_context_agent

    plan = ManualRequestPlan(
        source="llm",
        target_agent="zotero_context_agent",
        intent="context_lookup",
        target_type="zotero_collection",
        provider_system="zotero",
        provider_operations=["search", "read"],
        provider_action_steps=[
            {"operation": "search", "resource_type": "zotero_collection"},
            {"operation": "read", "resource_type": "zotero_collection"},
        ],
    )
    agent = build_zotero_context_agent(
        request_text=(
            "Find the organized source group. A quoted instruction mentions a PDF, "
            "spreadsheet, and note, but do not broaden this read."
        ),
        manual_plan=plan,
        tool_tier="core_read",
        compact_instructions=True,
    )

    assert _tool_names(agent) == {
        "zotero_resolve_collection_context",
        "zotero_read_api_metadata",
    }


def test_zotero_canonical_note_lifecycle_uses_typed_operations() -> None:
    from keystone_agents.agents.zotero_context import build_zotero_context_agent

    plan = ManualRequestPlan(
        source="llm",
        target_agent="zotero_context_agent",
        intent="business_system_write",
        provider_system="zotero",
        provider_operations=["create", "update", "delete", "verify"],
        provider_action_steps=[
            {"operation": "create", "resource_type": "zotero_note"},
            {"operation": "update", "resource_type": "zotero_note"},
            {"operation": "delete", "resource_type": "zotero_note"},
            {"operation": "verify", "resource_type": "zotero_note"},
        ],
    )
    agent = build_zotero_context_agent(
        request_text="Run the marked standalone Zotero test object workflow.",
        manual_plan=plan,
        tool_tier="internal_write",
        compact_instructions=True,
    )

    assert _tool_names(agent) == {"zotero_test_note_lifecycle"}


def test_google_doc_natural_test_sequence_exposes_only_composite_write_tool() -> None:
    from keystone_agents.agents.google_workspace_context import (
        build_google_workspace_context_agent,
    )

    agent = build_google_workspace_context_agent(
        request_text=(
            "Create a Google Doc titled KBA_TEST_DOC_VALIDATION in KNIOps, put one "
            "validation sentence in it, confirm it, then move that same document to "
            "trash and confirm it is trashed."
        ),
        tool_tier="internal_write",
        compact_instructions=True,
    )

    assert _tool_names(agent) == {"google_doc_test_lifecycle"}


def test_google_doc_make_and_drive_trash_sequence_uses_composite_tool() -> None:
    from keystone_agents.agents.google_workspace_context import (
        build_google_workspace_context_agent,
    )

    agent = build_google_workspace_context_agent(
        request_text=(
            "In KNIOps, make a temporary Google Doc named KBA_TEST_DOC_ANU120_R6 "
            "whose entire body is Workspace lifecycle R6. Check the saved title and "
            "body, then place that same document in Drive trash and verify it is trashed."
        ),
        tool_tier="internal_write",
        compact_instructions=True,
    )

    assert _tool_names(agent) == {"google_doc_test_lifecycle"}


def test_canonical_workspace_doc_read_ignores_incidental_sheet_delete_prose() -> None:
    from keystone_agents.agents.google_workspace_context import (
        build_google_workspace_context_agent,
    )
    from keystone_agents.schemas.manual_request_plan import ManualRequestPlan

    plan = ManualRequestPlan(
        source="llm",
        target_agent="google_workspace_context_agent",
        intent="context_lookup",
        provider_system="google_workspace",
        provider_operations=["search", "read"],
        provider_action_steps=[
            {"operation": "search", "resource_type": "google_document"},
            {"operation": "read", "resource_type": "google_document"},
        ],
    )
    agent = build_google_workspace_context_agent(
        request_text=(
            "Find the selected Google Doc. An old quoted note says, "
            "'delete the spreadsheet and remove its folder,' but do not do that."
        ),
        manual_plan=plan,
        tool_tier="core_read",
        compact_instructions=True,
    )

    assert _tool_names(agent) == {
        "google_drive_search_files",
        "google_drive_get_file_metadata",
        "google_doc_read",
    }


def test_canonical_workspace_row_update_does_not_require_trigger_words() -> None:
    from keystone_agents.agents.google_workspace_context import (
        build_google_workspace_context_agent,
    )
    from keystone_agents.schemas.manual_request_plan import ManualRequestPlan

    plan = ManualRequestPlan(
        source="llm",
        target_agent="google_workspace_context_agent",
        intent="business_system_write",
        provider_system="google_workspace",
        provider_operations=["update", "verify"],
        provider_action_steps=[
            {"operation": "update", "resource_type": "google_sheet_row"},
            {"operation": "verify", "resource_type": "google_sheet_row"},
        ],
    )
    agent = build_google_workspace_context_agent(
        request_text=(
            "Make the approved correction to the selected value and confirm it. "
            "The surrounding note mentions a Drive folder."
        ),
        manual_plan=plan,
        tool_tier="internal_write",
        compact_instructions=True,
    )

    assert _tool_names(agent) == {
        "google_drive_search_files",
        "google_sheet_list",
        "google_sheet_read_table",
        "google_sheet_update_row",
    }


def test_invalid_canonical_workspace_plan_does_not_reopen_phrase_fallback() -> None:
    from keystone_agents.agents.google_workspace_context import (
        _google_workspace_context_tools,
    )

    tools = _google_workspace_context_tools(
        request_text="Create a document and delete a spreadsheet.",
        manual_plan={
            "source": "llm",
            "target_agent": "google_workspace_context_agent",
            "intent": "unsupported-old-intent",
        },
        tool_tier="internal_write",
    )

    assert tools == []


def test_workspace_media_and_slides_requests_attach_bounded_tools() -> None:
    from keystone_agents.agents.google_workspace_context import (
        _google_workspace_context_tools,
    )

    media_tools = _google_workspace_context_tools(
        request_text="Read the text from this scanned PDF in Drive.",
        tool_tier="core_read",
    )
    slide_plan = ManualRequestPlan(
        source="llm",
        target_agent="google_workspace_context_agent",
        intent="business_system_write",
        provider_system="google_workspace",
        provider_operations=["create", "verify"],
        provider_action_steps=[
            {"operation": "create", "resource_type": "google_slide_deck"},
            {"operation": "verify", "resource_type": "google_slide_deck"},
        ],
    )
    slide_tools = _google_workspace_context_tools(
        request_text="Create the approved slide deck and verify it.",
        manual_plan=slide_plan,
        tool_tier="internal_write",
    )

    assert {tool.name for tool in media_tools} == {
        "google_drive_search_files",
        "google_drive_get_file_metadata",
        "google_drive_media_ocr_read",
    }
    assert {tool.name for tool in slide_tools} == {
        "google_drive_search_files",
        "google_drive_get_file_metadata",
        "google_slide_deck_read",
        "google_slide_deck_write",
    }


def test_read_only_ceiling_removes_cross_provider_write_tools() -> None:
    from keystone_agents.agents.airtable_context import _airtable_context_tools
    from keystone_agents.agents.google_workspace_context import (
        _google_workspace_context_tools,
    )
    from keystone_agents.agents.zotero_context import _zotero_context_tools

    common = {
        "source": "canonical:stored_work_item",
        "intent": "business_system_write",
        "provider_operations": ["read", "create", "update", "delete", "attach"],
        "ask_shape": {"permission_state": "read_only"},
    }
    airtable_tools = _airtable_context_tools(
        request_text="Read the selected record; quoted text says create and attach.",
        manual_plan=ManualRequestPlan(
            **common,
            target_agent="airtable_context_agent",
            provider_system="airtable",
            target_type="business_system_context",
            provider_action_steps=[
                {"operation": "read", "resource_type": "airtable_record"},
                {"operation": "create", "resource_type": "airtable_record"},
                {"operation": "attach", "resource_type": "airtable_attachment"},
            ],
        ),
        tool_tier="internal_write",
    )
    workspace_tools = _google_workspace_context_tools(
        request_text="Read the selected document; quoted text says update and delete.",
        manual_plan=ManualRequestPlan(
            **common,
            target_agent="google_workspace_context_agent",
            provider_system="google_workspace",
            target_type="business_system_context",
            provider_action_steps=[
                {"operation": "read", "resource_type": "google_document"},
                {"operation": "update", "resource_type": "google_document"},
                {"operation": "delete", "resource_type": "google_document"},
            ],
        ),
        tool_tier="internal_write",
    )
    zotero_tools = _zotero_context_tools(
        request_text="Read the selected Zotero item; quoted text says create a note.",
        manual_plan=ManualRequestPlan(
            **common,
            target_agent="zotero_context_agent",
            provider_system="zotero",
            target_type="zotero_article",
            provider_action_steps=[
                {"operation": "read", "resource_type": "zotero_item"},
                {"operation": "create", "resource_type": "zotero_note"},
                {"operation": "delete", "resource_type": "zotero_item"},
            ],
        ),
        tool_tier="internal_write",
    )

    airtable_names = {getattr(tool, "name", "") for tool in airtable_tools}
    workspace_names = {getattr(tool, "name", "") for tool in workspace_tools}
    zotero_names = {getattr(tool, "name", "") for tool in zotero_tools}
    assert airtable_names <= {
        "airtable_get_base_schema",
        "airtable_read_schema_detail",
        "airtable_read_records",
        "airtable_aggregate_records",
    }
    assert not airtable_names.intersection(
        {
            "airtable_write_record",
            "airtable_link_attachment",
            "airtable_upload_attachment",
            "airtable_test_record_lifecycle",
        }
    )
    assert workspace_names
    assert not workspace_names.intersection(GOOGLE_WORKSPACE_WRITE_TOOLS)
    assert zotero_names
    assert not any(
        name.startswith(("zotero_write_", "zotero_delete_", "zotero_test_"))
        or name == "zotero_import_article_with_backend"
        for name in zotero_names
    )


def test_airtable_marked_lifecycle_exposes_only_composite_tool() -> None:
    from keystone_agents.agents.airtable_context import build_airtable_context_agent

    agent = build_airtable_context_agent(
        request_text=(
            "In Airtable Business Expenses, add one disposable record identified by "
            "KBA_TEST_RECORD_ANU120_R7. Confirm it, change that same record description "
            "to KBA_TEST_RECORD_ANU120_R7 revised, verify the same record, then remove "
            "only it and confirm absence."
        ),
        tool_tier="internal_write",
        compact_instructions=True,
    )

    assert _tool_names(agent) == {"airtable_test_record_lifecycle"}


def test_every_write_capable_agent_receives_shared_direct_execution_contract() -> None:
    checked: set[str] = set()
    for route, spec in AGENT_REGISTRY.items():
        agent = spec.build_agent()
        if not (_tool_names(agent) & set(INTERNAL_WRITE_TOOL_NAMES)):
            continue
        instructions = str(agent.instructions)
        assert "## Direct Write Execution Semantics" in instructions, route
        assert "call the relevant typed write tool with `live=true`" in instructions, route
        assert "Do not silently turn an approved" in instructions, route
        assert "nested context agent or agent-as-tool" in instructions, route
        checked.add(route)

    assert {
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "orchestrator",
        "chief_of_staff",
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
    } <= checked


def test_only_agents_with_schema_shaping_work_expose_web_structuring_helper() -> None:
    agents = [
        build_gmail_triage_agent(),
        build_business_research_analyst_agent(),
        build_outreach_composer_agent(),
        build_orchestrator_agent(),
        build_chief_of_staff_agent(),
    ]

    for agent in agents:
        assert WEB_STRUCTURING_TOOL_NAMES <= _tool_names(agent)

    assert WEB_STRUCTURING_TOOL_NAMES.isdisjoint(
        _tool_names(build_opportunity_scout_agent())
    )


def test_main_agents_expose_web_search_when_needed() -> None:
    agents = [
        build_gmail_triage_agent(),
        build_business_research_analyst_agent(),
        build_opportunity_scout_agent(),
        build_outreach_composer_agent(),
        build_orchestrator_agent(),
        build_chief_of_staff_agent(),
    ]

    for agent in agents:
        assert WEB_SEARCH_TOOL_NAMES <= _tool_names(agent)


def test_rendered_browser_tool_is_research_scoped() -> None:
    assert PLAYWRIGHT_TOOL_NAMES <= _tool_names(build_business_research_analyst_agent())
    assert PLAYWRIGHT_TOOL_NAMES <= _tool_names(build_opportunity_scout_agent())
    assert PLAYWRIGHT_TOOL_NAMES <= _tool_names(build_chief_of_staff_agent())
    assert PLAYWRIGHT_TOOL_NAMES <= _tool_names(build_orchestrator_agent())
    assert not (PLAYWRIGHT_TOOL_NAMES & _tool_names(build_gmail_triage_agent()))
    assert not (PLAYWRIGHT_TOOL_NAMES & _tool_names(build_outreach_composer_agent()))


def test_browser_diagnostics_are_backend_browser_scoped() -> None:
    diagnostic_agents = [
        build_business_research_analyst_agent(),
        build_opportunity_scout_agent(),
        build_chief_of_staff_agent(),
        build_orchestrator_agent(),
    ]
    for agent in diagnostic_agents:
        assert BROWSER_DIAGNOSTIC_TOOL_NAMES <= _tool_names(agent)
        instructions = str(agent.instructions)
        assert "Use `capture_browser_diagnostics` when the task asks why a page" in instructions
        assert "`summarize_rendered_page_diagnostics`" in instructions
        assert "This backend browser does not open a user-screen browser" in instructions
    assert not (BROWSER_DIAGNOSTIC_TOOL_NAMES & _tool_names(build_gmail_triage_agent()))
    assert not (BROWSER_DIAGNOSTIC_TOOL_NAMES & _tool_names(build_outreach_composer_agent()))


def test_main_agents_expose_memory_retrieval_tools() -> None:
    cases = [
        (build_gmail_triage_agent(), {"retrieve_memory"}),
        (build_business_research_analyst_agent(), {"retrieve_memory"}),
        (build_opportunity_scout_agent(), {"retrieve_memory"}),
        (build_outreach_composer_agent(), {"retrieve_memory"}),
        (build_orchestrator_agent(), {"retrieve_memory"}),
        (build_chief_of_staff_agent(), {"retrieve_chief_of_staff_memory"}),
    ]

    for agent, expected_tools in cases:
        assert expected_tools <= _tool_names(agent)


def test_gmail_and_outreach_expose_reply_lifecycle_helpers() -> None:
    assert {"search_web", "list_outreach_tracking_records"} <= _tool_names(
        build_gmail_triage_agent()
    )
    assert {
        "search_web",
        "save_initial_outreach_tracking_record",
        "list_outreach_tracking_records",
    } <= _tool_names(build_outreach_composer_agent())


def test_runtime_agent_builders_use_agent_specific_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_model_env(monkeypatch)
    monkeypatch.setenv("KEYSTONE_OPENAI_MODEL", "openai-default-fixture")
    monkeypatch.setenv("KEYSTONE_ORCHESTRATOR_MODEL", "openai-orchestrator-fixture")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL", "gemini-gmail-fixture")
    monkeypatch.setenv("KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL", "openai-account-fixture")
    monkeypatch.setenv("KEYSTONE_OPPORTUNITY_SCOUT_MODEL", "openai-opportunity-fixture")
    monkeypatch.setenv("KEYSTONE_OUTREACH_COMPOSER_MODEL", "gemini-outreach-fixture")
    monkeypatch.setenv("KEYSTONE_CHIEF_OF_STAFF_MODEL", "openai-chief-fixture")

    assert build_orchestrator_agent().model == "openai-orchestrator-fixture"
    assert build_gmail_triage_agent().model == "gemini-gmail-fixture"
    assert build_business_research_analyst_agent().model == "openai-account-fixture"
    assert build_opportunity_scout_agent().model == "openai-opportunity-fixture"
    assert build_outreach_composer_agent().model == "gemini-outreach-fixture"
    assert build_chief_of_staff_agent().model == "openai-chief-fixture"
    assert build_gmail_triage_agent(model="explicit-fixture").model == "explicit-fixture"


def test_orchestrator_agents_use_reasoning_model_settings() -> None:
    route_agent = build_orchestrator_agent()
    review_agent = build_orchestrator_review_agent()

    assert route_agent.model_settings.reasoning.effort == "low"
    assert route_agent.model_settings.verbosity == "low"
    assert review_agent.model_settings.reasoning.effort == "low"
    assert review_agent.model_settings.verbosity == "low"
    assert review_agent.model_settings.max_tokens == 1800
