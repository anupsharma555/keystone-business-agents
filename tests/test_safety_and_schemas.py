from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from keystone_agents.guardrails import (
    assess_text_guardrails,
    assess_tool_payload_guardrails,
    keystone_input_guardrail,
)
from keystone_agents.schemas.opportunity import (
    OpportunityRecord,
    OpportunityScoutResult,
    OpportunitySource,
)
from keystone_agents.schemas.outreach import OutreachDraft


def test_guardrail_assessment_blocks_phi_advice_and_secrets() -> None:
    text_assessment = assess_text_guardrails(
        "Patient Alex has a depression diagnosis and needs a treatment plan. "
        "Please provide medical advice."
    )
    tool_assessment = assess_tool_payload_guardrails(
        "storage_save_agent_run",
        {"api_key": "REDACTED_TEST_VALUE_123456"},
    )

    assert not text_assessment.allowed
    assert "possible PHI or patient-specific content" in text_assessment.reasons
    assert "medical, legal, tax, or regulatory advice" in text_assessment.reasons
    assert not tool_assessment.allowed
    assert "secret-like content" in tool_assessment.reasons


def test_tool_output_guardrail_allows_drive_metadata_clients_label_without_weakening_secrets() -> None:
    drive_output = assess_tool_payload_guardrails(
        "google_drive_list_folder",
        {
            "status": "success",
            "items": [
                {
                    "name": "Clients",
                    "type": "folder",
                    "mime_type": "application/vnd.google-apps.folder",
                }
            ],
            "send_enabled": False,
        },
        output=True,
    )
    secret_output = assess_tool_payload_guardrails(
        "google_drive_list_folder",
        {"api_key": "REDACTED_TEST_VALUE_123456"},
        output=True,
    )
    text_assessment = assess_text_guardrails("Our clients and case studies prove results.")

    assert drive_output.allowed
    assert "unsupported_claim" not in drive_output.risk_flags
    assert not secret_output.allowed
    assert "secret" in secret_output.risk_flags
    assert not text_assessment.allowed
    assert "unsupported_claim" in text_assessment.risk_flags


def test_input_guardrail_trips_on_patient_specific_content() -> None:
    result = keystone_input_guardrail.guardrail_function(
        None,
        None,
        "Please draft outreach using this Patient Alex depression diagnosis.",
    )

    assert result.tripwire_triggered
    assert "possible PHI or patient-specific content" in result.output_info["reasons"]


def test_input_guardrail_trips_on_named_patient_story_outreach() -> None:
    result = keystone_input_guardrail.guardrail_function(
        None,
        None,
        "Draft outreach using this named patient story as proof.",
    )

    assert result.tripwire_triggered
    assert "possible_phi" in result.output_info["risk_flags"]


def test_input_guardrail_allows_source_context_for_outreach_agent() -> None:
    result = keystone_input_guardrail.guardrail_function(
        None,
        SimpleNamespace(name="outreach_composer"),
        "Context says the company appears to support decentralized clinical trials.",
    )

    assert not result.tripwire_triggered
    assert "unsupported_claim" not in result.output_info["risk_flags"]


def test_input_guardrail_allows_internal_finance_google_doc_artifact_request() -> None:
    result = keystone_input_guardrail.guardrail_function(
        None,
        SimpleNamespace(name="chief_of_staff"),
        {
            "request": (
                "review airtable tables, analyze and provide a tax summary and analysis "
                "for Q1 in google docs. Create a folder and doc within the gdrive for "
                "the tax updates. Provide a link to the google doc in the reply"
            ),
            "side_effect_policy": (
                "internal KNIOps Google Docs artifact only; no Slack post, Gmail send, "
                "calendar write, repo write, tax filing, or tax payment"
            ),
        },
    )

    assert not result.tripwire_triggered
    assert result.output_info["risk_flags"] == ("finance_review",)


def test_input_guardrail_allows_browser_diagnostics_language() -> None:
    result = keystone_input_guardrail.guardrail_function(
        None,
        SimpleNamespace(name="orchestrator"),
        "Diagnose https://example.com with backend browser diagnostics.",
    )

    assert not result.tripwire_triggered
    assert "professional_advice" not in result.output_info["risk_flags"]


def test_guardrail_allows_negated_tax_advice_disclaimer_context() -> None:
    assessment = assess_text_guardrails(
        "This is an operational tracker summary, not final tax advice.",
        check_outreach_claims=False,
    )

    assert assessment.allowed
    assert "professional_advice" not in assessment.risk_flags

    blocked = assess_text_guardrails(
        "Please provide tax advice about whether this deduction is allowed.",
        check_outreach_claims=False,
    )

    assert not blocked.allowed
    assert "professional_advice" in blocked.risk_flags


def test_outreach_schema_requires_draft_only_human_approval_and_no_em_dash() -> None:
    valid = OutreachDraft.model_validate(
        {
            "company_name": "Example Co",
            "email_subject": "Research workflow discussion",
            "email_body": (
                "Hello, I noticed your public research workflow work and wondered if a "
                "short discussion would be useful."
            ),
            "linkedin_note": "Open to comparing notes on clinical AI workflows?",
            "personalization_rationale": "Uses public research workflow context.",
            "approval_required": True,
            "approval_state": "pending",
            "approval_scope": "send",
        }
    )
    assert valid.approval_required is True
    assert valid.approval_state == "pending"

    with pytest.raises(ValidationError):
        OutreachDraft.model_validate(
            {
                "company_name": "Example Co",
                "email_subject": "Research workflow discussion",
                "email_body": "Hello \u2014 this has an em dash.",
                "personalization_rationale": "Uses public research workflow context.",
                "approval_required": True,
                "approval_state": "pending",
                "approval_scope": "send",
            }
        )

    with pytest.raises(ValidationError):
        OutreachDraft.model_validate(
            {
                "company_name": "Example Co",
                "email_subject": "Research workflow discussion",
                "email_body": "Hello, this is ready.",
                "personalization_rationale": "Uses public research workflow context.",
                "approval_required": False,
                "approval_state": "pending",
                "approval_scope": "send",
            }
        )


def test_opportunity_schema_bounds_scores() -> None:
    output = OpportunityScoutResult.model_validate(
        {
            "topic": "clinical AI",
            "records": [
                OpportunityRecord(
                    company_name="Example Neuroscience Tools",
                    opportunity_type="clinical AI",
                    priority_score=72,
                    why_now_signal="Public workflow hiring signal.",
                    recommended_next_step="Review manually.",
                    sources=[
                        OpportunitySource(
                            title="Careers",
                            url="https://example.org/careers",
                            source_type="fixture",
                            supported_signal="Public workflow hiring signal.",
                        )
                    ],
                    keystone_fit_reason="Relevant to research operations.",
                    outside_consulting_likelihood=60,
                    handoff_to_business_research_analyst=True,
                )
            ],
        }
    )

    assert output.records[0].priority_score == 72

    with pytest.raises(ValidationError):
        OpportunityRecord.model_validate(
            {
                "company_name": "Bad Score Co",
                "opportunity_type": "clinical AI",
                "priority_score": 101,
                "why_now_signal": "Signal",
                "recommended_next_step": "Review.",
                "sources": [
                    {
                        "title": "Source",
                        "url": "https://example.org",
                        "source_type": "fixture",
                        "supported_signal": "Signal",
                    }
                ],
                "keystone_fit_reason": "Too high.",
                "outside_consulting_likelihood": 50,
                "handoff_to_business_research_analyst": True,
            }
        )


def test_opportunity_schema_supports_optional_discovery_metadata_without_legacy_noise() -> None:
    legacy_record = OpportunityRecord(
        company_name="Example Neuroscience Tools",
        opportunity_type="clinical AI",
        priority_score=72,
        why_now_signal="Public workflow hiring signal.",
        recommended_next_step="Review manually.",
        sources=[
            OpportunitySource(
                title="Careers",
                url="https://example.org/careers",
                source_type="fixture",
                supported_signal="Public workflow hiring signal.",
            )
        ],
        keystone_fit_reason="Relevant to research operations.",
        outside_consulting_likelihood=60,
        handoff_to_business_research_analyst=True,
    )
    legacy_result = OpportunityScoutResult(records=[legacy_record])

    legacy_dump = legacy_result.model_dump(mode="json")

    assert "search_lanes" not in legacy_dump
    assert "search_time_windows" not in legacy_dump
    assert "entity_kind" not in legacy_dump["records"][0]
    assert "canonical_entity_key" not in legacy_dump["records"][0]
    assert "usa_relevance" not in legacy_dump["records"][0]
    assert "novelty" not in legacy_dump["records"][0]
    assert "search_lanes" not in legacy_dump["records"][0]
    assert "search_time_windows" not in legacy_dump["records"][0]

    enriched_result = OpportunityScoutResult.model_validate(
        {
            "topic": "federal neuro grants",
            "search_lanes": ["grants", "publications"],
            "search_time_windows": ["last 12 months", "last 30 days"],
            "records": [
                {
                    "company_name": "NIMH SBIR Program",
                    "entity_kind": "grant_program",
                    "canonical_entity_key": "grant_program:nimh-sbir",
                    "opportunity_type": "grant or collaboration opportunity",
                    "usa_relevance": "U.S. federal sponsor and funding lane.",
                    "novelty": "new_to_keystone",
                    "search_lanes": ["grants"],
                    "search_time_windows": ["last 12 months"],
                    "priority_score": 81,
                    "why_now_signal": "Current grant cycle is open for neuroscience tooling.",
                    "recommended_next_step": "Review fit for outreach-safe follow-up.",
                    "sources": [
                        {
                            "title": "Program notice",
                            "url": "https://example.org/grants",
                            "source_type": "fixture",
                            "supported_signal": "Current grant cycle is open.",
                        }
                    ],
                    "keystone_fit_reason": "Relevant to evidence-generation partnerships.",
                    "outside_consulting_likelihood": 55,
                    "handoff_to_business_research_analyst": True,
                }
            ],
        }
    )

    enriched_dump = enriched_result.model_dump(mode="json")

    assert enriched_dump["search_lanes"] == ["grants", "publications"]
    assert enriched_dump["search_time_windows"] == ["last 12 months", "last 30 days"]
    assert enriched_dump["records"][0]["entity_kind"] == "grant_program"
    assert enriched_dump["records"][0]["canonical_entity_key"] == "grant_program:nimh-sbir"
    assert enriched_dump["records"][0]["usa_relevance"] == (
        "U.S. federal sponsor and funding lane."
    )
    assert enriched_dump["records"][0]["novelty"] == "new_to_keystone"
    assert enriched_dump["records"][0]["search_lanes"] == ["grants"]
    assert enriched_dump["records"][0]["search_time_windows"] == ["last 12 months"]
