from __future__ import annotations

import pytest

from keystone_agents.agent_registry import AGENT_REGISTRY
from keystone_agents.agent_tool_policy import INTERNAL_WRITE_TOOL_NAMES
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
from keystone_agents.schemas.opportunity import OpportunityScoutResult
from keystone_agents.schemas.orchestrator import OrchestratorResult
from keystone_agents.schemas.outreach import OutreachDraft
from keystone_agents.sdk import Agent
from keystone_agents.tools.internal_data_tools import GOOGLE_WORKSPACE_TOOL_NAMES

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
                "search_opportunity_sources_placeholder",
                "score_opportunity",
                "handoff_to_business_research_analyst_placeholder",
                "save_opportunity_placeholder",
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
    agents = [
        build_gmail_triage_agent(),
        build_business_research_analyst_agent(),
        build_opportunity_scout_agent(),
        build_outreach_composer_agent(),
        build_orchestrator_agent(),
        build_chief_of_staff_agent(),
    ]

    expected_tools = set(GOOGLE_WORKSPACE_TOOL_NAMES)
    for agent in agents:
        assert expected_tools <= _tool_names(agent)


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


def test_main_agents_expose_web_data_structuring_helper() -> None:
    agents = [
        build_gmail_triage_agent(),
        build_business_research_analyst_agent(),
        build_opportunity_scout_agent(),
        build_outreach_composer_agent(),
        build_orchestrator_agent(),
        build_chief_of_staff_agent(),
    ]

    for agent in agents:
        assert WEB_STRUCTURING_TOOL_NAMES <= _tool_names(agent)


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
