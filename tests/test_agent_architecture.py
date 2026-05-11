from __future__ import annotations

import pytest

from keystone_agents.agents.business_research_analyst import build_business_research_analyst_agent
from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
from keystone_agents.agents.opportunity_scout import build_opportunity_scout_agent
from keystone_agents.agents.orchestrator import (
    build_orchestrator_agent,
    build_orchestrator_review_agent,
)
from keystone_agents.agents.outreach_composer import build_outreach_composer_agent
from keystone_agents.schemas.company_profile import CompanyProfile
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.schemas.opportunity import OpportunityScoutResult
from keystone_agents.schemas.orchestrator import OrchestratorResult
from keystone_agents.schemas.outreach import OutreachDraft
from keystone_agents.sdk import Agent

MODEL_ENV_VARS = (
    "KEYSTONE_OPENAI_MODEL",
    "KEYSTONE_ORCHESTRATOR_MODEL",
    "KEYSTONE_GMAIL_TRIAGE_MODEL",
    "KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL",
    "KEYSTONE_OPPORTUNITY_SCOUT_MODEL",
    "KEYSTONE_OUTREACH_COMPOSER_MODEL",
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


def test_no_agent_exposes_send_tool() -> None:
    agents = [
        build_gmail_triage_agent(),
        build_business_research_analyst_agent(),
        build_opportunity_scout_agent(),
        build_outreach_composer_agent(),
        build_orchestrator_agent(),
    ]

    tool_names = {name for agent in agents for name in _tool_names(agent)}
    assert not any(name.startswith("send") or "send_email" in name for name in tool_names)
    assert "create_gmail_draft_reply" in tool_names
    assert "create_approval_queue_item" in tool_names
    assert "create_approval_request_placeholder" in tool_names


def test_main_agents_expose_allowlisted_local_context_tools() -> None:
    agents = [
        build_gmail_triage_agent(),
        build_business_research_analyst_agent(),
        build_opportunity_scout_agent(),
        build_outreach_composer_agent(),
        build_orchestrator_agent(),
    ]

    expected_tools = {
        "list_local_context_sources",
        "search_local_context",
        "read_local_context_file",
    }
    for agent in agents:
        assert expected_tools <= _tool_names(agent)


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

    assert build_orchestrator_agent().model == "openai-orchestrator-fixture"
    assert build_gmail_triage_agent().model == "gemini-gmail-fixture"
    assert build_business_research_analyst_agent().model == "openai-account-fixture"
    assert build_opportunity_scout_agent().model == "openai-opportunity-fixture"
    assert build_outreach_composer_agent().model == "gemini-outreach-fixture"
    assert build_gmail_triage_agent(model="explicit-fixture").model == "explicit-fixture"


def test_orchestrator_agents_use_reasoning_model_settings() -> None:
    route_agent = build_orchestrator_agent()
    review_agent = build_orchestrator_review_agent()

    assert route_agent.model_settings.reasoning.effort == "low"
    assert route_agent.model_settings.verbosity == "low"
    assert review_agent.model_settings.reasoning.effort == "low"
    assert review_agent.model_settings.verbosity == "low"
    assert review_agent.model_settings.max_tokens == 1800
