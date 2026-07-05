from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import keystone_agents.agents.business_research_analyst as business_research_module
import keystone_agents.agents.gmail_triage as gmail_triage_module
import keystone_agents.agents.opportunity_scout as opportunity_scout_module
import keystone_agents.agents.outreach_composer as outreach_composer_module

try:
    from agents.exceptions import (
        InputGuardrailTripwireTriggered,
        ModelBehaviorError,
        OutputGuardrailTripwireTriggered,
    )
    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.tool_context import ToolContext
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )
except ImportError:
    pytestmark = pytest.mark.skip(reason="OpenAI Agents SDK fake-model hooks unavailable.")

from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_agent,
    focused_brief_input_from_profile,
    run_business_research_analyst_focused_brief_sdk,
    run_business_research_analyst_sdk,
)
from keystone_agents.agents.chief_of_staff import (
    build_chief_of_staff_agent,
    run_chief_of_staff_sdk,
)
from keystone_agents.agents.gmail_triage import (
    build_gmail_triage_agent,
    run_gmail_priority_grouping_sdk,
    run_gmail_triage_sdk,
)
from keystone_agents.agents.opportunity_scout import (
    build_opportunity_scout_agent,
    run_opportunity_scout_sdk,
)
from keystone_agents.agents.orchestrator import (
    review_specialist_output_llm,
    run_orchestrator_sdk,
)
from keystone_agents.agents.outreach_composer import (
    build_outreach_composer_agent,
    run_outreach_composer_sdk,
)
from keystone_agents.company_research import research_company_fixture
from keystone_agents.costing import AgentRunBudgetExceededError
from keystone_agents.evals import score_output_against_expected
from keystone_agents.model_provider import (
    GEMINI_PROVIDER,
    MissingOpenAIAPIKeyError,
    ModelConfig,
    UnsafeTraceMetadataError,
)
from keystone_agents.models import (
    BusinessResearchFocusedBriefSDKInput,
    BusinessResearchSDKInput,
    GmailPriorityGroupingSDKInput,
    GmailTriageSDKInput,
    OpportunityScoutSDKInput,
    OutreachComposerSDKInput,
    TypedAgentRunResult,
)
from keystone_agents.run import (
    prompt_from_typed_input,
    run_retrieved_sdk_synthesis,
    run_typed_sdk_agent,
)
from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult, ChiefSpecialistToolInput
from keystone_agents.schemas.company_profile import CompanyProfile, CompanyResearchFocusedBrief
from keystone_agents.schemas.email_triage import EmailTriageResult, GmailPriorityGroupingResult
from keystone_agents.schemas.opportunity import OpportunityScoutResult
from keystone_agents.schemas.orchestrator import OrchestratorOutputReview, OrchestratorResult
from keystone_agents.schemas.outreach import OutreachDraft
from keystone_agents.sdk import Runner, build_local_run_config
from promptfoo.eval_database import list_eval_trace_events

RUNTIME_MODEL_ENV_VARS = (
    "KEYSTONE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "KEYSTONE_OPENAI_BASE_URL",
    "KEYSTONE_OPENAI_MODEL",
    "KEYSTONE_OPENAI_FALLBACK_MODEL",
    "KEYSTONE_OPENAI_FALLBACK_BASE_URL",
    "KEYSTONE_ORCHESTRATOR_MODEL",
    "KEYSTONE_ORCHESTRATOR_MODEL_PROVIDER",
    "KEYSTONE_ORCHESTRATOR_BASE_URL",
    "KEYSTONE_GMAIL_TRIAGE_MODEL",
    "KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER",
    "KEYSTONE_GMAIL_TRIAGE_BASE_URL",
    "KEYSTONE_GMAIL_TRIAGE_OPENAI_FALLBACK_MODEL",
    "KEYSTONE_GMAIL_TRIAGE_OPENAI_FALLBACK_BASE_URL",
    "KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL",
    "KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL_PROVIDER",
    "KEYSTONE_BUSINESS_RESEARCH_ANALYST_BASE_URL",
    "KEYSTONE_OPPORTUNITY_SCOUT_MODEL",
    "KEYSTONE_OPPORTUNITY_SCOUT_MODEL_PROVIDER",
    "KEYSTONE_OPPORTUNITY_SCOUT_BASE_URL",
    "KEYSTONE_OUTREACH_COMPOSER_MODEL",
    "KEYSTONE_OUTREACH_COMPOSER_MODEL_PROVIDER",
    "KEYSTONE_OUTREACH_COMPOSER_BASE_URL",
    "KEYSTONE_CHIEF_OF_STAFF_MODEL",
    "KEYSTONE_CHIEF_OF_STAFF_MODEL_PROVIDER",
    "KEYSTONE_CHIEF_OF_STAFF_BASE_URL",
    "GEMINI_API_KEY",
    "LITELLM_BASE_URL",
    "KEYSTONE_AGENT_RUN_BUDGET_USD",
    "KEYSTONE_SDK_SESSIONS",
    "KEYSTONE_SDK_SESSION_ID",
    "KEYSTONE_SDK_SESSION_DB",
    "KEYSTONE_SDK_SESSION_HISTORY_LIMIT",
)


def _clear_runtime_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in RUNTIME_MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _email_triage_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "message_id": "fake-message-1",
        "subject": "Potential consulting project",
        "sender_name": "Example Sender",
        "sender_email": "sender@example.com",
        "category": "consulting_opportunity",
        "confidence": 0.91,
        "priority": "high",
        "summary": "Consulting inquiry relevant to Keystone.",
        "reasoning": "Deterministic fake model response.",
        "needs_reply": True,
        "recommended_labels": ["Keystone/Triage"],
        "risk_flags": [],
        "recommended_action": "Create a draft for human approval.",
        "draft_reply": "Thanks for reaching out. I can review and follow up after approval.",
        "draft_created": True,
        "approval_required": True,
        "requires_human_review": True,
    }
    payload.update(overrides)
    return payload


def _priority_grouping_item(
    *,
    message_id: str,
    bucket: str,
    subject: str,
    category: str,
    priority: str,
    needs_reply: bool = False,
    draft_reply: str | None = None,
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "thread_id": f"thread-{message_id}",
        "subject": subject,
        "sender_name": "Example Sender",
        "sender_email": "sender@example.com",
        "bucket": bucket,
        "category": category,
        "confidence": 0.88,
        "priority": priority,
        "summary": f"{subject} grouped as {bucket}.",
        "reasoning": "Fake model grouped the sanitized message from the batch context.",
        "needs_reply": needs_reply,
        "recommended_action": "Review draft for approval." if draft_reply else "No draft needed.",
        "recommended_labels": ["Keystone/Triage"],
        "risk_flags": [],
        "draft_reply": draft_reply,
        "draft_created": draft_reply is not None,
        "approval_required": draft_reply is not None,
        "requires_human_review": True,
        "send_enabled": False,
        "sent": False,
    }


def _priority_grouping_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "request_summary": (
            "Review unread emails from the last 3 days and draft only urgent replies."
        ),
        "source_label": "UNREAD",
        "lookback_days": 3,
        "source_message_count": 4,
        "urgent": [
            _priority_grouping_item(
                message_id="urgent-1",
                bucket="urgent",
                subject="Client deadline today",
                category="consulting_opportunity",
                priority="urgent",
                needs_reply=True,
                draft_reply=(
                    "Hi Alex,\n\nThanks for the note. I can review the non-sensitive "
                    "project context today and follow up after approval.\n\nBest,\nKeystone"
                ),
            )
        ],
        "important": [
            _priority_grouping_item(
                message_id="important-1",
                bucket="important",
                subject="Research collaboration next month",
                category="collaboration_opportunity",
                priority="high",
            )
        ],
        "can_wait": [
            _priority_grouping_item(
                message_id="can-wait-1",
                bucket="can_wait",
                subject="Weekly digital health funding digest",
                category="newsletter",
                priority="low",
            )
        ],
        "ignore": [
            _priority_grouping_item(
                message_id="ignore-1",
                bucket="ignore",
                subject="Automated lead platform demo",
                category="vendor",
                priority="low",
            )
        ],
        "draft_count": 1,
        "send_enabled": False,
        "sent": False,
        "live_side_effects_enabled": False,
        "audit_notes": ["Fake model completed GT-1 without live side effects."],
    }
    payload.update(overrides)
    return payload


def _company_profile_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "name": "Curebase",
        "website": "https://www.curebase.com",
        "description": "Clinical trial software company.",
        "fit_summary": (
            "Relevant for decentralized clinical trial and health technology workflows."
        ),
        "behavioral_health_relevance": 0,
        "clinical_ai_relevance": 100,
        "cns_neuro_relevance": 0,
        "evidence_generation_need": 85,
        "outside_consulting_likelihood": 30,
        "consulting_fit_score": 94,
        "confidence_score": 0.66,
        "sources": [
            {
                "source_id": "fixture:curebase",
                "title": "Fixture record for Curebase",
                "url": "fixture://sample_company_curebase.json",
                "source_type": "fixture",
                "supported_claims": [
                    "Clinical trial software company.",
                    ("Relevant for decentralized clinical trial and health technology workflows."),
                ],
                "confidence": 0.7,
            }
        ],
        "evidence": [
            "Clinical trial software company.",
            "Relevant for decentralized clinical trial and health technology workflows.",
        ],
        "risks": [],
        "missing_information": ["LinkedIn or profile URL not supplied."],
    }
    payload.update(overrides)
    return payload


def _company_focused_brief_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "company_name": "Curebase",
        "brief_purpose": "partnership_or_advisory_relevance",
        "product": "Clinical trial software platform.",
        "customers": "Clinical research teams and trial sponsors.",
        "traction_signals": "Research workflow, partnership, and validation signals.",
        "leadership": "Unknown from provided sources.",
        "why_it_matters": (
            "Inference: Curebase may matter to Keystone because evidence-generation "
            "and clinical validation needs overlap with Keystone advisory focus."
        ),
        "facts": [
            {
                "text": "Curebase is a clinical trial software company.",
                "source_ids": ["fixture:curebase_company"],
                "confidence": 0.82,
            }
        ],
        "inferences": [
            "Potential advisory relevance is based on source-backed evidence-generation needs."
        ],
        "unknowns": ["Leadership was not source-backed in the provided context."],
        "source_ids_used": ["fixture:curebase_company"],
        "sources": [
            {
                "source_id": "fixture:curebase_company",
                "title": "Curebase fixture company profile",
                "url": "fixture://sample_company_curebase.json",
                "source_type": "fixture",
            }
        ],
        "raw_source_content_included": False,
        "send_enabled": False,
    }
    payload.update(overrides)
    return payload


def _research_brief_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "target_name": "Curebase",
        "target_type": "company",
        "research_goal": "Assess source-backed advisory relevance for Chief of Staff.",
        "summary": "Curebase has relevant clinical trial software context for CoS synthesis.",
        "key_findings": ["Clinical trial software context is relevant to evidence workflows."],
        "facts": [
            {
                "text": "Curebase is a clinical trial software company.",
                "source_ids": ["fixture:curebase"],
                "confidence": 0.82,
            }
        ],
        "inferences": [
            "Potential advisory relevance should be validated against current sources."
        ],
        "unknowns": ["Leadership and current traction need source review."],
        "limitations": ["Fixture data only."],
        "next_steps": ["Have Chief decide whether to request live research."],
        "source_ids_used": ["fixture:curebase"],
        "sources": [
            {
                "source_id": "fixture:curebase",
                "title": "Fixture record for Curebase",
                "url": "fixture://sample_company_curebase.json",
                "source_type": "fixture",
            }
        ],
        "raw_source_content_included": False,
        "send_enabled": False,
    }
    payload.update(overrides)
    return payload


def _opportunity_scout_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "topic": "behavioral health AI",
        "dry_run": True,
        "records": [
            {
                "company_name": "NeuroFlow",
                "opportunity_type": "behavioral health AI",
                "priority_score": 76,
                "why_now_signal": (
                    "Payer partnership and outcomes evidence for behavioral health measurement."
                ),
                "recommended_next_step": (
                    "Hand off to Business Research Analyst for source-attributed company research "
                    "before outreach."
                ),
                "sources": [
                    {
                        "source_id": "fixture:neuroflow-payer-partnership",
                        "title": "Fixture payer partnership announcement",
                        "url": "fixture://neuroflow-payer-partnership",
                        "source_type": "fixture",
                        "supported_signal": (
                            "Payer partnership and outcomes evidence for behavioral health "
                            "measurement."
                        ),
                    }
                ],
                "source_signals": ["payer partnership", "publication or outcomes evidence"],
                "keystone_fit_reason": (
                    "Signal intersects Keystone focus areas in clinical AI, neuroscience, "
                    "behavioral health, evidence generation, or clinical research operations."
                ),
                "outside_consulting_likelihood": 77,
                "handoff_to_business_research_analyst": True,
                "outreach_draft": None,
                "approval_required_before_outreach": True,
            }
        ],
        "audit_notes": ["Fixture mode only; no live APIs were called."],
        "outreach_generated": False,
    }
    payload.update(overrides)
    return payload


def _outreach_draft_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "company_name": "Curebase",
        "recipient": "Dr. Example",
        "contact_name": "Dr. Example",
        "contact_title": "Clinical Operations Lead",
        "outreach_goal": "compare notes on clinical AI evaluation support",
        "email_subject": "Curebase research workflow discussion",
        "email_body": (
            "Hi Dr. Example,\n\n"
            "I noticed Curebase's work around decentralized clinical trial operations. "
            "Keystone Neuroinformatics LLC is a physician-scientist-led consulting company "
            "focused on clinical AI evaluation and research operations.\n\n"
            "I am reaching out to see whether it would be useful to compare notes on "
            "clinical AI evaluation support. If relevant, I would welcome a brief "
            "introductory conversation."
        ),
        "linkedin_note": (
            "Hi Dr. Example, I noticed Curebase's clinical research work. Keystone works "
            "across clinical AI and research operations. Open to a brief exchange?"
        ),
        "personalization_rationale": (
            "Draft references approved source-backed context for Curebase."
        ),
        "facts_used": [
            {
                "claim_text": "Company name: Curebase",
                "source_id": "fixture:curebase",
                "confidence": 0.8,
                "claim_type": "company_identity",
            },
            {
                "claim_text": "decentralized clinical trial operations",
                "source_id": "fixture:lead",
                "confidence": 0.7,
                "claim_type": "opportunity_signal",
            },
        ],
        "unsupported_claims_flagged": [],
        "approval_required": True,
        "approval_state": "pending",
        "approval_scope": "send",
    }
    payload.update(overrides)
    return payload


def _orchestrator_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "route": "business_research_analyst",
        "target_agent": "Business Research Analyst",
        "workflow": ["business_research_analyst"],
        "routing_mode": "llm",
        "rationale": "Fake model selected business research.",
        "requires_human_review": True,
        "approval_required": True,
        "approval_rationale": "Human review remains required before downstream use.",
        "external_use_approval_required": True,
        "approved_context_present": False,
        "refused": False,
        "send_enabled": False,
        "can_send_email": False,
        "audit_notes": ["Fake model route."],
    }
    payload.update(overrides)
    return payload


def _chief_of_staff_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "agent_name": "chief_of_staff",
        "mode": "llm",
        "intent": "slack_operations",
        "summary": "Fake model produced an operations review plan.",
        "time_window": "current",
        "target_channels": ["ai-agents-workflow"],
        "operating_capabilities": ["planning", "review"],
        "recommended_route": {
            "workflow_type": "slack-runtime-review",
            "command_text": "@KNI chief of staff review agent runtime",
            "target_channel": "ai-agents-workflow",
            "rationale": "Operator asked for planning and review.",
            "requires_live_connector": False,
            "requires_human_approval_before_post": True,
        },
        "recommended_actions": ["Review cost telemetry after the next Slack run."],
        "blocked_side_effects": ["gmail_send", "slack_post"],
        "approval_required": True,
        "human_review_required": True,
        "send_enabled": False,
        "slack_post_allowed": False,
        "slack_post_policy": "not_allowed",
        "slack_target_channel": "",
        "slack_post_reason": "No explicit post approval.",
        "sources": [],
        "context_sources_considered": ["operator_request"],
        "repo_context_used": [],
        "write_requests": [],
        "artifact_refs": [],
        "audit_notes": ["Fake model CoS result."],
    }
    payload.update(overrides)
    return payload


def _orchestrator_output_review_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "reviewed_by": "orchestrator",
        "agent_name": "gmail_triage",
        "output_type": "EmailTriageResult",
        "review_mode": "llm",
        "overall_score": 91,
        "status": "pass",
        "structure": {
            "dimension": "structure",
            "score": 92,
            "status": "pass",
            "rationale": "Clear human-facing triage structure.",
        },
        "tone": {
            "dimension": "tone",
            "score": 90,
            "status": "pass",
            "rationale": "Professional and restrained.",
        },
        "readability": {
            "dimension": "readability",
            "score": 91,
            "status": "pass",
            "rationale": "Readable for a Keystone operator.",
        },
        "relevance": {
            "dimension": "relevance",
            "score": 92,
            "status": "pass",
            "rationale": "Relevant to the triage request.",
        },
        "human_readable": True,
        "metadata_relevance_ok": True,
        "approval_boundary_ok": True,
        "send_enabled": False,
        "can_send_email": False,
        "llm_review_used": True,
        "observed_gaps": ["None observed."],
        "recommended_next_step": "Ready for human review.",
        "test_pack_checks": {
            "Structure": "pass",
            "Tone": "pass",
            "Readability": "pass",
            "Relevance": "pass",
            "Preserves no-send behavior": "pass",
        },
        "audit_notes": ["Fake LLM review."],
    }
    payload.update(overrides)
    return payload


def _message_output(text: str, *, message_id: str = "fake-message-output") -> ResponseOutputMessage:
    return ResponseOutputMessage(
        id=message_id,
        type="message",
        role="assistant",
        status="completed",
        content=[
            ResponseOutputText(
                type="output_text",
                text=text,
                annotations=[],
            )
        ],
    )


def _structured_message(payload: dict[str, Any]) -> ResponseOutputMessage:
    return _message_output(json.dumps(payload))


def _model_input_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def test_prompt_from_typed_input_renders_structured_context_as_json() -> None:
    prompt = prompt_from_typed_input(
        {
            "request": "chief of staff review this Slack request",
            "manual_request_plan": {"target_agent": "chief_of_staff"},
            "orchestrator_preflight": {"selected_agent": "chief_of_staff"},
        }
    )

    parsed = json.loads(prompt)
    assert parsed["request"] == "chief of staff review this Slack request"
    assert parsed["manual_request_plan"]["target_agent"] == "chief_of_staff"
    assert parsed["orchestrator_preflight"]["selected_agent"] == "chief_of_staff"
    assert "'target_agent'" not in prompt


def _airtable_context_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "agent_name": "airtable_context_agent",
        "mode": "llm",
        "summary": "Resolved Airtable schema context.",
        "base_alias": "eval_tracker",
        "base_id": "app_eval",
        "relevant_tables": ["Eval Runs"],
        "relevant_fields": ["Status"],
        "candidate_record_ids": ["rec1"],
        "recommended_record_identity": "rec1",
        "recommended_actions": ["Ask Chief to confirm record before any write."],
        "write_plan": {
            "target_system": "airtable",
            "operation": "update",
            "target": "Eval Runs rec1",
            "scope": "status note",
            "field_mapping": {"Status": "Ready for review"},
            "approval_required": True,
            "approval_reference_needed": True,
            "live_write_allowed_for_specialist": False,
            "rationale": "Review-only plan; Airtable execution remains downstream.",
        },
        "blockers": ["Approval reference missing."],
        "approval_needs": ["Scoped Airtable approval required."],
        "human_work_context": {
            "work_functions": ["eval tracking"],
            "human_owner_hint": "Chief of Staff",
            "decision_needed": "Confirm target record",
            "handoff_ready_context": ["schema"],
            "missing_context": ["approval"],
            "integration_surfaces": ["Airtable"],
            "follow_up_actions": ["request approval"],
        },
        "sources": [],
        "diagnostics": {"fixture": "true"},
    }
    payload.update(overrides)
    return payload


def _google_workspace_context_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "agent_name": "google_workspace_context_agent",
        "mode": "llm",
        "summary": "Resolved Google Workspace artifact context.",
        "relevant_folders": ["KNI Ops / Evals"],
        "relevant_files": ["CoS eval tracker"],
        "relevant_docs": ["Chief of Staff run notes"],
        "relevant_sheets": ["Eval Runs"],
        "recommended_target": "KNI Ops / Evals / CoS eval tracker",
        "recommended_actions": ["Ask Chief to update the eval tracker after approval."],
        "write_plan": {
            "target_system": "google_workspace",
            "operation": "update_sheet",
            "target": "CoS eval tracker",
            "scope": "append run summary row",
            "field_mapping": {"Status": "Ready for review"},
            "approval_required": True,
            "approval_reference_needed": True,
            "live_write_allowed_for_specialist": False,
            "rationale": "Review-only plan; Workspace execution remains downstream.",
        },
        "blockers": ["Workspace approval reference missing."],
        "approval_needs": ["Scoped Google Workspace approval required."],
        "human_work_context": {
            "work_functions": ["eval reporting"],
            "human_owner_hint": "Chief of Staff",
            "decision_needed": "Confirm target folder and tracker.",
            "handoff_ready_context": ["folder", "file", "tab"],
            "missing_context": ["approval"],
            "integration_surfaces": ["Google Drive", "Google Sheets"],
            "follow_up_actions": ["request approval"],
        },
        "sources": [],
        "diagnostics": {"fixture": "true"},
    }
    payload.update(overrides)
    return payload


def _zotero_context_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "agent_name": "zotero_context_agent",
        "mode": "llm",
        "summary": "Resolved Zotero literature context.",
        "library_context": "Keystone research library",
        "collection_hints": ["behavioral health AI"],
        "collection_keys": ["COLL1"],
        "article_titles": ["Measurement-based care AI evaluation"],
        "zotero_item_keys": ["ITEM1"],
        "source_ids": ["zotero:ITEM1"],
        "relevant_evidence": ["Article supports measurement workflow context."],
        "recommended_artifact_plan": {
            "target_system": "google_workspace",
            "operation": "create_doc",
            "target": "Zotero literature synthesis",
            "scope": "draft internal summary",
            "approval_required": True,
            "approval_reference_needed": True,
            "live_write_allowed_for_specialist": False,
            "rationale": "Review-only artifact plan; execution remains downstream.",
        },
        "zotero_write_supported": False,
        "recommended_actions": ["Use these citations in the Chief synthesis."],
        "blockers": ["Confirm collection scope before artifact write."],
        "approval_needs": ["Scoped Workspace approval required before creating artifact."],
        "human_work_context": {
            "work_functions": ["literature triage"],
            "human_owner_hint": "Chief of Staff",
            "decision_needed": "Confirm which collection is in scope.",
            "handoff_ready_context": ["collection", "article", "source id"],
            "missing_context": ["collection confirmation"],
            "integration_surfaces": ["Zotero", "Google Docs"],
            "follow_up_actions": ["confirm collection"],
        },
        "sources": [
            {
                "source_id": "zotero:ITEM1",
                "title": "Measurement-based care AI evaluation",
                "location": "zotero://select/items/ITEM1",
                "source_type": "zotero",
                "note": "Fixture Zotero item.",
            }
        ],
        "diagnostics": {"fixture": "true"},
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("tool_name", "route_name", "payload", "expected_validation"),
    [
        (
            "gmail_triage_as_specialist_tool",
            "gmail_triage",
            _email_triage_payload(
                summary="Triaged Gmail thread context for Chief.",
                human_work_context={
                    "work_functions": ["email triage"],
                    "human_owner_hint": "Chief of Staff",
                    "decision_needed": "Approve draft before any send.",
                    "integration_surfaces": ["Gmail"],
                },
            ),
            "needs_review",
        ),
        (
            "business_research_analyst_as_specialist_tool",
            "business_research_analyst",
            _research_brief_payload(),
            "ok",
        ),
        (
            "opportunity_scout_as_specialist_tool",
            "opportunity_scout",
            _opportunity_scout_payload(),
            "ok",
        ),
        (
            "outreach_composer_as_specialist_tool",
            "outreach_composer",
            _outreach_draft_payload(),
            "needs_review",
        ),
        (
            "airtable_context_agent_as_specialist_tool",
            "airtable_context_agent",
            _airtable_context_payload(),
            "blocked",
        ),
        (
            "google_workspace_context_agent_as_specialist_tool",
            "google_workspace_context_agent",
            _google_workspace_context_payload(),
            "blocked",
        ),
        (
            "zotero_context_agent_as_specialist_tool",
            "zotero_context_agent",
            _zotero_context_payload(),
            "blocked",
        ),
    ],
)
def test_chief_specialist_agent_tools_invoke_nested_agents_with_fake_model(
    tool_name: str,
    route_name: str,
    payload: dict[str, Any],
    expected_validation: str,
) -> None:
    model = FakeModel(outputs=[[_structured_message(payload)]])
    agent = build_chief_of_staff_agent(include_specialist_tools=True)
    tool = next(
        item
        for item in agent.tools
        if getattr(item, "name", "") == tool_name
    )
    tool_input = ChiefSpecialistToolInput(
        raw_operator_request=(
            f"@KNI chief of staff use {route_name} for this #evals tracker question"
        ),
        specialist_task=f"Resolve {route_name} context and return blockers.",
        decision_context={
            "intent_family": "eval readiness",
            "desired_deliverable": "reviewable work plan",
            "success_criteria": "Friday Slack eval pilot is ready or blockers are explicit",
        },
        target_context={
            "channel": "evals",
            "tracker": "CoS eval tracker",
            "specialist_route": route_name,
        },
        coordination_context={
            "sibling_specialists": (
                "Gmail Triage, Business Research, Opportunity Scout, Outreach Composer, "
                "Airtable Context, Google Workspace Context, Zotero Context"
            ),
            "merge_need": "Chief integrates each specialist result into one recommendation.",
        },
        provider_call_context={
            "provider": route_name,
            "read_scope": "bounded eval readiness context",
            "date_window": "current eval cycle",
            "target_object": "CoS eval tracker or source thread",
            "approval_reference": "pending",
        },
        source_layer_manifest={"requested": route_name},
        approval_context={"approval_id": "pending"},
        side_effect_boundaries=["no_nested_live_write"],
    ).model_dump(mode="json")
    context = ToolContext(
        context=None,
        run_config=build_local_run_config(FakeProvider(model)),
        tool_name=tool.name,
        tool_call_id="call_airtable_context_test",
        tool_arguments=json.dumps(tool_input),
    )

    async def invoke_tool() -> str:
        return await tool.on_invoke_tool(context, json.dumps(tool_input))

    import asyncio

    envelope = json.loads(asyncio.run(invoke_tool()))

    assert envelope["route_name"] == route_name
    assert envelope["tool_name"] == tool_name
    assert envelope["parsed_output_status"] == "parsed"
    assert envelope["summary"]
    assert envelope["validation_status"] == expected_validation
    assert envelope["target_input_type"] == (
        "keystone_agents.schemas.chief_of_staff.ChiefNestedSpecialistResult"
    )
    assert envelope["payload_mode"] == "adapted"
    assert envelope["type_compatibility_status"] == "compatible"
    assert envelope["type_contract"]["target_agent"] == "chief_of_staff"
    assert envelope["type_contract"]["target_output_type"] == (
        "keystone_agents.schemas.chief_of_staff.ChiefOfStaffResult"
    )
    assert envelope["source_output_type"]
    nested_prompt = _model_input_text(model.calls[0]["input"])
    assert "Raw Operator Request" in nested_prompt
    assert route_name in nested_prompt
    assert "decision_context" in nested_prompt
    assert "target_context" in nested_prompt
    assert "coordination_context" in nested_prompt
    assert "provider_call_context" in nested_prompt
    assert "Friday Slack eval pilot" in nested_prompt
    assert "bounded eval readiness context" in nested_prompt
    assert "source_layer_manifest" in nested_prompt
    assert "no_nested_live_write" in nested_prompt


def _tool_call(name: str, arguments: dict[str, Any]) -> ResponseFunctionToolCall:
    return ResponseFunctionToolCall(
        type="function_call",
        name=name,
        call_id="fake-tool-call-1",
        arguments=json.dumps(arguments),
        status="completed",
    )


class FakeModel(Model):
    """Deterministic SDK model that never reaches a network provider."""

    def __init__(self, outputs: list[list[Any]], *, usage: Usage | None = None) -> None:
        self.outputs = outputs
        self.usage = usage or Usage(requests=1)
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
                "system_instructions": system_instructions,
                "input": input,
                "tool_names": [tool.name for tool in tools],
                "output_schema": output_schema,
            }
        )
        output = self.outputs.pop(0)
        return ModelResponse(
            output=output,
            usage=self.usage,
            response_id=f"fake-response-{len(self.calls)}",
        )

    def stream_response(
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
    ) -> AsyncIterator[Any]:
        raise NotImplementedError("Streaming is not needed for fake SDK tests.")


class FakeProvider(ModelProvider):
    def __init__(self, model: FakeModel) -> None:
        self.model = model
        self.requested_models: list[str | None] = []

    def get_model(self, model_name: str | None) -> Model:
        self.requested_models.append(model_name)
        return self.model


class FakeStorage:
    def __init__(self) -> None:
        self.agent_runs: list[dict[str, Any]] = []

    def save_agent_run(self, **kwargs: Any) -> dict[str, Any]:
        self.agent_runs.append(kwargs)
        return {"status": "saved", "id": len(self.agent_runs)}


def _run_with_fake_model(
    agent: Any,
    model: FakeModel,
    prompt: str = "Subject: Potential consulting project\nBody: We need advisory help.",
) -> Any:
    provider = FakeProvider(model)
    return Runner.run_sync(
        agent,
        prompt,
        run_config=build_local_run_config(provider),
    )


def test_run_typed_sdk_agent_retries_live_rate_limit_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    class FakeRateLimitError(Exception):
        status_code = 429

    class FakeAgent:
        name = "chief_of_staff"
        model = "gpt-test"

    def fake_run_typed_sdk_sync(
        *_args: Any, **_kwargs: Any
    ) -> tuple[dict[str, Any], ChiefOfStaffResult]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise FakeRateLimitError("Rate limit reached. Please try again in 1.5s.")
        return (
            {"fake": True},
            ChiefOfStaffResult(
                mode="llm",
                summary="Recovered after retry.",
                audit_notes=[],
            ),
        )

    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr("keystone_agents.run.time.sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setattr(
        "keystone_agents.run.enforce_agent_run_budget",
        lambda **_kwargs: {"enforced": False},
    )

    result = run_typed_sdk_agent(
        agent=FakeAgent(),
        typed_input={"request": "deepened search brief"},
        output_type=ChiefOfStaffResult,
        live=True,
        config=ModelConfig(provider="openai", model="gpt-test", api_key="test-key"),
    )

    assert calls == 2
    assert sleeps == [2.0]
    assert result.output.summary == "Recovered after retry."
    assert result.request_cache["rate_limit_retries"] == 1


def test_run_typed_sdk_agent_preserves_explicit_local_pdf_and_image_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_path = tmp_path / "receipt.pdf"
    image_path = tmp_path / "receipt.png"
    pdf_path.write_bytes(b"%PDF-1.4\nreceipt fixture")
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    captured: dict[str, Any] = {}

    class FakeAgent:
        name = "chief_of_staff"
        model = "gpt-test"

    def fake_run_typed_sdk_sync(
        _agent: Any,
        prompt: Any,
        _output_type: Any,
        **_kwargs: Any,
    ) -> tuple[dict[str, Any], ChiefOfStaffResult]:
        captured["prompt"] = prompt
        return (
            {"fake": True},
            ChiefOfStaffResult(mode="llm", summary="Read attached files."),
        )

    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(
        "keystone_agents.run.enforce_agent_run_budget",
        lambda **_kwargs: {"enforced": False},
    )

    result = run_typed_sdk_agent(
        agent=FakeAgent(),
        typed_input={"request": f"read {pdf_path} and {image_path}"},
        output_type=ChiefOfStaffResult,
        live=True,
        config=ModelConfig(provider="openai", model="gpt-test", api_key="test-key"),
    )

    prompt = captured["prompt"]
    assert result.output.summary == "Read attached files."
    assert isinstance(prompt, list)
    content = prompt[0]["content"]
    assert any(part.get("type") == "input_file" for part in content)
    assert any(part.get("type") == "input_image" for part in content)
    assert "data:application/pdf;base64," in next(
        part["file_data"] for part in content if part.get("type") == "input_file"
    )
    assert "data:image/png;base64," in next(
        part["image_url"] for part in content if part.get("type") == "input_image"
    )
    assert result.request_cache["dynamic_prompt_chars"] > 0


def test_run_typed_sdk_agent_records_sdk_run_summary_trace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_path = tmp_path / "evals.sqlite"

    class FakeAgent:
        name = "business_research_analyst"
        model = "gpt-test"
        instructions = "Return a structured summary."
        tools = [SimpleNamespace(name="search_web")]
        output_type = ChiefOfStaffResult

    def fake_run_typed_sdk_sync(
        *_args: Any, **_kwargs: Any
    ) -> tuple[dict[str, Any], ChiefOfStaffResult]:
        return (
            {
                "usage": {
                    "requests": 1,
                    "input_tokens": 100,
                    "output_tokens": 25,
                    "total_tokens": 125,
                },
                "new_items": [{"type": "function_call", "name": "search_web"}],
            },
            ChiefOfStaffResult(
                mode="llm",
                summary="Traceable result.",
                audit_notes=[],
            ),
        )

    monkeypatch.setenv("KEYSTONE_TRACE_PROCESSOR", "eval_summary")
    monkeypatch.setenv("KEYSTONE_TRACE_SUMMARY_DB", str(database_path))
    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run_typed_sdk_sync)
    monkeypatch.setattr(
        "keystone_agents.run.enforce_agent_run_budget",
        lambda **_kwargs: {"status": "ok", "exceeded": False},
    )

    result = run_typed_sdk_agent(
        agent=FakeAgent(),
        typed_input={"request": "Summarize current source-backed evidence."},
        output_type=ChiefOfStaffResult,
        live=True,
        config=ModelConfig(provider="openai", model="gpt-test", api_key="test-key"),
        max_turns=2,
        trace_metadata={
            "route": "business_research_analyst",
            "case_id": "case_trace_001",
            "work_item_id": "wi_trace_001",
            "run_id": "run_trace_001",
            "slack_channel_id": "C123",
            "slack_thread_ts": "1715366400.000100",
        },
    )

    rows = list_eval_trace_events(database_path=database_path)
    summary = rows[0]["metadata"]
    assert result.output.summary == "Traceable result."
    assert rows[0]["event_type"] == "sdk_run_summary"
    assert rows[0]["trace_id"] == "run_trace_001"
    assert rows[0]["group_id"] == "case_trace_001"
    assert summary["schema"] == "keystone.sdk_run_summary.v1"
    assert summary["agent"] == "business_research_analyst"
    assert summary["route"] == "business_research_analyst"
    assert summary["status"] == "ok"
    assert summary["correlation"]["work_item_id"] == "wi_trace_001"
    assert summary["model_provider"] == "openai"
    assert summary["model_name"] == "gpt-test"
    assert summary["max_turns"] == 2
    assert summary["sdk_request_count"] == 1
    assert summary["tool_call_counts"] == {"search_web": 1}
    assert summary["redaction"]["raw_tool_io_included"] is False
    assert "Summarize current source-backed evidence" not in str(rows)


@pytest.mark.parametrize(
    (
        "builder",
        "payload",
        "output_type",
        "prompt",
        "instruction_marker",
        "expected_tools",
    ),
    [
        (
            build_gmail_triage_agent,
            _email_triage_payload(),
            EmailTriageResult,
            "Subject: Potential consulting project\nBody: We need advisory help.",
            "Gmail Triage Agent",
            {"get_gmail_message", "apply_gmail_labels", "create_gmail_draft_reply"},
        ),
        (
            build_business_research_analyst_agent,
            _company_profile_payload(),
            CompanyProfile,
            "Research Curebase from approved fixture context.",
            "Business Research Analyst",
            {
                "search_web",
                "fetch_company_page",
                "fetch_linkedin_or_profile_placeholder",
                "extract_company_signals",
            },
        ),
        (
            build_opportunity_scout_agent,
            _opportunity_scout_payload(),
            OpportunityScoutResult,
            "Scout behavioral health AI opportunities in dry-run mode.",
            "Opportunity Scout Agent",
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
            _outreach_draft_payload(),
            OutreachDraft,
            "Draft approval-gated outreach from approved Curebase context.",
            "Outreach Composer Agent",
            {
                "load_company_profile",
                "load_opportunity_record",
                "check_unsupported_claims",
                "create_approval_request_placeholder",
            },
        ),
    ],
)
def test_all_specialist_agents_run_with_fake_model_without_openai_key(
    builder: Any,
    payload: dict[str, Any],
    output_type: type[Any],
    prompt: str,
    instruction_marker: str,
    expected_tools: set[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_SESSIONS", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_SESSION_ID", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_SESSION_DB", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_SESSION_HISTORY_LIMIT", raising=False)
    model = FakeModel(outputs=[[_structured_message(payload)]])

    result = _run_with_fake_model(builder(), model, prompt)

    assert isinstance(result.final_output, output_type)
    assert model.calls
    assert instruction_marker in model.calls[0]["system_instructions"]
    assert expected_tools <= set(model.calls[0]["tool_names"])
    assert model.calls[0]["output_schema"] is not None


@pytest.mark.parametrize(
    ("runner", "typed_input", "payload", "output_type"),
    [
        (
            run_gmail_triage_sdk,
            GmailTriageSDKInput(
                subject="Potential consulting project",
                body="We need advisory help.",
                sender_name="Example Sender",
                sender_email="sender@example.com",
            ),
            _email_triage_payload(),
            EmailTriageResult,
        ),
        (
            run_business_research_analyst_sdk,
            BusinessResearchSDKInput(company_name="Curebase"),
            _company_profile_payload(),
            CompanyProfile,
        ),
        (
            run_opportunity_scout_sdk,
            OpportunityScoutSDKInput(topic="behavioral health AI"),
            _opportunity_scout_payload(),
            OpportunityScoutResult,
        ),
        (
            run_outreach_composer_sdk,
            OutreachComposerSDKInput(
                company_name="Curebase",
                contact_name="Dr. Example",
                approved_context="Approved fixture context.",
            ),
            _outreach_draft_payload(),
            OutreachDraft,
        ),
        (
            run_chief_of_staff_sdk,
            {
                "request": "review agent runtime cost telemetry",
                "manual_request_plan": {
                    "source": "test",
                    "requested_agent": "chief_of_staff",
                    "target_agent": "chief_of_staff",
                    "intent": "slack_operations",
                    "primary_target": "agent runtime cost telemetry",
                    "target_type": "slack_channel",
                    "objective": "Review agent runtime cost telemetry.",
                    "task_objective": "slack_operations",
                    "expected_artifact_type": "slack_ops_summary",
                },
            },
            _chief_of_staff_payload(),
            ChiefOfStaffResult,
        ),
    ],
)
def test_typed_specialist_runtime_harness_uses_fake_model_without_openai_key(
    runner: Any,
    typed_input: Any,
    payload: dict[str, Any],
    output_type: type[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_SESSIONS", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_SESSION_ID", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_SESSION_DB", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_SESSION_HISTORY_LIMIT", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_SESSION_HISTORY_LIMIT", raising=False)
    model = FakeModel(outputs=[[_structured_message(payload)]])
    provider = FakeProvider(model)

    result = runner(typed_input, run_config=build_local_run_config(provider))

    assert isinstance(result, TypedAgentRunResult)
    assert isinstance(result.final_output, output_type)
    assert result.output is result.final_output
    assert result.live is False
    assert result.agent_name
    assert result.usage["available"] is True
    assert result.usage["requests"] == 1
    assert "cache_hit_rate" in result.usage
    assert "source" in result.cost
    assert result.budget_guard["status"]
    assert result.request_cache["request_layout"] == "static_agent_prefix_then_dynamic_typed_input"
    assert result.request_cache["repo_instruction_profile"] == "compact-runtime-policy"
    assert len(result.request_cache["static_prefix_sha256"]) == 64
    assert len(result.request_cache["dynamic_prompt_sha256"]) == 64
    assert result.request_cache["max_turns"] == 4
    assert result.request_cache["max_turns_source"] == "caller"
    assert result.request_cache["session_attached"] is False
    assert result.request_cache["session_history_mode"] == ""
    assert result.request_cache["session_history_limit"] == 0
    assert result.request_cache["session_truncation_configured"] is False
    assert model.calls


def test_direct_specialist_sdk_wrappers_pass_resolved_max_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def capture(module: Any) -> dict[str, Any]:
        captured: dict[str, Any] = {}

        def fake_run_typed_sdk_agent(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return kwargs

        monkeypatch.setattr(module, "run_typed_sdk_agent", fake_run_typed_sdk_agent)
        return captured

    research = capture(business_research_module)
    scout = capture(opportunity_scout_module)
    gmail = capture(gmail_triage_module)
    outreach = capture(outreach_composer_module)

    run_business_research_analyst_sdk(BusinessResearchSDKInput(company_name="Curebase"))
    run_opportunity_scout_sdk(OpportunityScoutSDKInput(topic="behavioral health AI"))
    run_gmail_triage_sdk(
        GmailTriageSDKInput(
            subject="Potential project",
            body="Could Keystone help us evaluate a workflow?",
        )
    )
    run_gmail_priority_grouping_sdk(
        GmailPriorityGroupingSDKInput(
            messages=[],
            request="Group recent messages.",
        )
    )
    run_outreach_composer_sdk(
        OutreachComposerSDKInput(
            company_name="Curebase",
            approved_context="Approved fixture context.",
        )
    )

    assert research["max_turns"] == 4
    assert scout["max_turns"] == 4
    assert gmail["max_turns"] == 4
    assert outreach["max_turns"] == 4


def test_direct_specialist_sdk_turn_policy_supports_quality_and_explicit_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def fake_run_typed_sdk_agent(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return kwargs

    monkeypatch.setattr(
        business_research_module,
        "run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    run_business_research_analyst_sdk(
        BusinessResearchSDKInput(
            company_name="OpenAI",
            context="Use the supplied company context.",
        ),
        live=True,
    )
    assert captured["max_turns"] == 8

    run_business_research_analyst_sdk(
        BusinessResearchSDKInput(company_name="OpenAI"),
        max_turns=2,
    )
    assert captured["max_turns"] == 2


def test_business_research_analyst_focused_brief_runtime_uses_llm_output_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    profile = research_company_fixture(
        company_name="Curebase",
        fixture_json="tests/fixtures/sample_company_curebase.json",
    )
    typed_input = focused_brief_input_from_profile(profile)
    model = FakeModel(outputs=[[_structured_message(_company_focused_brief_payload())]])
    provider = FakeProvider(model)

    result = run_business_research_analyst_focused_brief_sdk(
        typed_input,
        run_config=build_local_run_config(provider),
    )
    prompt = _model_input_text(model.calls[0]["input"])

    assert isinstance(typed_input, BusinessResearchFocusedBriefSDKInput)
    assert isinstance(result.final_output, CompanyResearchFocusedBrief)
    assert result.final_output.leadership.lower().startswith("unknown")
    assert result.final_output.facts[0].source_ids == ["fixture:curebase_company"]
    assert "BR-1 focused brief" in prompt
    assert "Do not invent facts" in prompt
    assert "fixture:curebase_company" in prompt
    assert result.live is False


def test_research_sdk_wrappers_default_to_read_only_core_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    research_model = FakeModel(outputs=[[_structured_message(_company_profile_payload())]])
    scout_model = FakeModel(outputs=[[_structured_message(_opportunity_scout_payload())]])

    run_business_research_analyst_sdk(
        BusinessResearchSDKInput(company_name="Curebase"),
        run_config=build_local_run_config(FakeProvider(research_model)),
    )
    run_opportunity_scout_sdk(
        OpportunityScoutSDKInput(topic="behavioral health AI"),
        run_config=build_local_run_config(FakeProvider(scout_model)),
    )

    research_tools = set(research_model.calls[0]["tool_names"])
    scout_tools = set(scout_model.calls[0]["tool_names"])

    assert "retrieve_memory" in research_tools
    assert "retrieve_memory" in scout_tools
    assert "search_web" not in research_tools
    assert "search_web" not in scout_tools
    assert "airtable_write_record" not in research_tools
    assert "save_opportunity_memory" not in scout_tools


def test_business_research_sdk_input_includes_runtime_source_layer_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("KEYSTONE_FILE_SEARCH_VECTOR_STORE_IDS", "vs_private_reference")
    model = FakeModel(outputs=[[_structured_message(_company_profile_payload())]])

    run_business_research_analyst_sdk(
        BusinessResearchSDKInput(company_name="Curebase", context="Existing source context."),
        run_config=build_local_run_config(FakeProvider(model)),
    )

    prompt = _model_input_text(model.calls[0]["input"])
    assert "Existing source context." in prompt
    assert "Runtime source-layer policy:" in prompt
    assert "hosted_file_search" in prompt
    assert "reasoning_contract=" in prompt
    assert "stable approved reference corpora" in prompt
    assert "public_web_search" in prompt
    assert "local_kni_documents" not in prompt
    assert "vs_private_reference" not in prompt


def test_research_sdk_wrappers_infer_deep_retrieval_without_write_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    research_model = FakeModel(outputs=[[_structured_message(_company_profile_payload())]])
    scout_model = FakeModel(outputs=[[_structured_message(_opportunity_scout_payload())]])

    run_business_research_analyst_sdk(
        BusinessResearchSDKInput(
            company_name="OpenAI",
            context="Please do a deeper source-backed search and summarize the source data.",
        ),
        live=True,
        run_config=build_local_run_config(FakeProvider(research_model)),
    )
    run_opportunity_scout_sdk(
        OpportunityScoutSDKInput(
            topic=(
                "deeper source-backed search for active AI-enabled behavioral health "
                "pilot, RFP, or grant opportunities"
            )
        ),
        live=True,
        run_config=build_local_run_config(FakeProvider(scout_model)),
    )

    research_tools = set(research_model.calls[0]["tool_names"])
    scout_tools = set(scout_model.calls[0]["tool_names"])

    assert "search_web" in research_tools
    assert "extract_research_claims_from_html" in research_tools
    assert "airtable_write_record" not in research_tools
    assert "google_sheet_append_rows" not in research_tools
    assert "search_web" in scout_tools
    assert "extract_research_claims_from_html" in scout_tools
    assert "score_opportunity" in scout_tools
    assert "save_opportunity_memory" not in scout_tools
    assert "airtable_write_record" not in scout_tools


def test_gmail_triage_sdk_defaults_to_read_only_unless_draft_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    read_model = FakeModel(outputs=[[_structured_message(_email_triage_payload())]])
    draft_model = FakeModel(outputs=[[_structured_message(_email_triage_payload())]])

    run_gmail_triage_sdk(
        GmailTriageSDKInput(
            subject="Potential project",
            body="Could Keystone help us evaluate a behavioral health AI workflow?",
        ),
        run_config=build_local_run_config(FakeProvider(read_model)),
    )
    run_gmail_triage_sdk(
        GmailTriageSDKInput(
            subject="Potential project",
            body="Could Keystone help us evaluate a behavioral health AI workflow?",
            request="Please draft a short reply but do not send it.",
        ),
        run_config=build_local_run_config(FakeProvider(draft_model)),
    )

    read_tools = set(read_model.calls[0]["tool_names"])
    draft_tools = set(draft_model.calls[0]["tool_names"])

    assert "get_gmail_message" in read_tools
    assert "create_gmail_draft_reply" not in read_tools
    assert "apply_gmail_labels" not in read_tools
    assert "airtable_write_record" not in read_tools
    assert "create_gmail_draft_reply" in draft_tools
    assert "create_approval_queue_item" in draft_tools
    assert "search_web" in draft_tools


def test_orchestrator_sdk_infers_tiered_tools_for_default_and_deep_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    default_model = FakeModel(outputs=[[_structured_message(_orchestrator_payload())]])
    deep_model = FakeModel(outputs=[[_structured_message(_orchestrator_payload())]])

    run_orchestrator_sdk(
        "Route Curebase for business research.",
        run_config=build_local_run_config(FakeProvider(default_model)),
    )
    run_orchestrator_sdk(
        "Run a deeper source-backed web search and summarize the source data.",
        live=True,
        run_config=build_local_run_config(FakeProvider(deep_model)),
    )

    default_tools = set(default_model.calls[0]["tool_names"])
    deep_tools = set(deep_model.calls[0]["tool_names"])

    assert "retrieve_memory" in default_tools
    assert "search_web" not in default_tools
    assert "airtable_write_record" not in default_tools
    assert "search_web" in deep_tools
    assert "extract_research_claims_from_html" in deep_tools
    assert "airtable_write_record" not in deep_tools
    assert "google_sheet_append_rows" not in deep_tools


def test_orchestrator_live_sdk_input_includes_runtime_source_layer_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_model_env(monkeypatch)
    monkeypatch.setenv("KEYSTONE_ORCHESTRATOR_FILE_SEARCH_VECTOR_STORE_IDS", "vs_router")
    model = FakeModel(outputs=[[_structured_message(_orchestrator_payload())]])

    run_orchestrator_sdk(
        "Route this public-company research request.",
        live=True,
        run_config=build_local_run_config(FakeProvider(model)),
    )

    raw_input = model.calls[0]["input"]
    assert isinstance(raw_input, list)
    payload = json.loads(raw_input[0]["content"])
    prompt = json.dumps(payload, ensure_ascii=True, sort_keys=True)
    assert payload["request"] == "Route this public-company research request."
    source_layer_policy = payload["runtime_source_layer_policy"]
    layers = {item["layer"]: item for item in source_layer_policy["layers"]}
    assert "hosted_file_search" in layers
    assert "public_web_search" in layers
    assert layers["hosted_file_search"]["runtime_configured"] is True
    assert "vs_router" not in prompt


def test_typed_specialist_runtime_missing_key_only_fails_for_live_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_model_env(monkeypatch)
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    typed_input = GmailTriageSDKInput(subject="Hello", body="Potential consulting project.")

    with pytest.raises(RuntimeError, match="fake/local run_config"):
        run_gmail_triage_sdk(typed_input)

    with pytest.raises(MissingOpenAIAPIKeyError):
        run_gmail_triage_sdk(typed_input, live=True)

    model = FakeModel(outputs=[[_structured_message(_email_triage_payload())]])
    provider = FakeProvider(model)
    result = run_gmail_triage_sdk(typed_input, run_config=build_local_run_config(provider))

    assert isinstance(result.final_output, EmailTriageResult)


def test_gmail_constrained_sdk_style_profile_output_matches_eval_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    typed_input = GmailTriageSDKInput(
        subject="Potential consulting project",
        body="We need advisory help around clinical operations.",
        sender_name="Example Sender",
        sender_email="sender@example.com",
        email_style_profile=(
            '{"profile_id":"default","approved_for_drafting":true,'
            '"preferred_phrases":["Happy to compare notes"],'
            '"cta_style":"context_request"}'
        ),
    )
    payload = _email_triage_payload(
        draft_reply=(
            "Hi Example Sender,\n\n"
            "Thanks for reaching out. Happy to compare notes if useful. "
            "Please send any non-sensitive context and a few times that work.\n\n"
            "Best,\nKeystone"
        ),
        style_profile_used=True,
        style_profile_id="default",
    )
    model = FakeModel(outputs=[[_structured_message(payload)]])
    provider = FakeProvider(model)

    result = run_gmail_triage_sdk(
        typed_input,
        run_config=build_local_run_config(provider),
    )
    score = score_output_against_expected(
        result.output.model_dump(mode="json"),
        {
            "category": "consulting_opportunity",
            "needs_reply": True,
            "risk_flags_exact": [],
            "draft_required": True,
            "draft_quality": {
                "min_words": 20,
                "max_words": 80,
                "required_terms": ["Happy to compare notes", "non-sensitive", "times"],
                "forbidden_terms": ["send automatically", "guaranteed", "patient"],
            },
        },
        agent="gmail",
    )

    assert isinstance(result.final_output, EmailTriageResult)
    assert result.final_output.approval_required is True
    assert result.final_output.draft_created is True
    assert result.final_output.style_profile_used is True
    assert result.final_output.style_profile_id == "default"
    assert "Optional approved aggregate email style profile" in _model_input_text(
        model.calls[0]["input"]
    )
    assert score.passed is True


def test_gmail_constrained_sdk_rejects_draft_without_approval_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    typed_input = GmailTriageSDKInput(
        subject="Potential consulting project",
        body="We need advisory help.",
    )
    model = FakeModel(
        outputs=[
            [
                _structured_message(
                    _email_triage_payload(
                        approval_required=False,
                        draft_created=True,
                        draft_reply="Thanks for reaching out. I can review this.",
                    )
                )
            ]
        ]
    )
    provider = FakeProvider(model)

    with pytest.raises(ModelBehaviorError):
        run_gmail_triage_sdk(typed_input, run_config=build_local_run_config(provider))


def test_gmail_gt1_priority_grouping_uses_llm_batch_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    typed_input = GmailPriorityGroupingSDKInput(
        messages=[
            GmailTriageSDKInput(
                message_id="urgent-1",
                subject="Client deadline today",
                body="Can we discuss the clinical operations project today?",
                sender_name="Alex",
                sender_email="alex@example.com",
            ),
            GmailTriageSDKInput(
                message_id="important-1",
                subject="Research collaboration next month",
                body="Could we compare notes next month?",
                sender_name="Jordan",
                sender_email="jordan@example.com",
            ),
            GmailTriageSDKInput(
                message_id="can-wait-1",
                subject="Weekly digital health funding digest",
                body="This week in digital health funding and webinars.",
            ),
            GmailTriageSDKInput(
                message_id="ignore-1",
                subject="Automated lead platform demo",
                body="We sell a lead platform and would like to book a demo.",
            ),
        ],
    )
    model = FakeModel(outputs=[[_structured_message(_priority_grouping_payload())]])
    provider = FakeProvider(model)

    result = run_gmail_priority_grouping_sdk(
        typed_input,
        run_config=build_local_run_config(provider),
    )
    prompt = _model_input_text(model.calls[0]["input"])

    assert isinstance(result.final_output, GmailPriorityGroupingResult)
    assert len(result.final_output.urgent) == 1
    assert len(result.final_output.important) == 1
    assert len(result.final_output.can_wait) == 1
    assert len(result.final_output.ignore) == 1
    assert result.final_output.urgent[0].draft_reply
    assert result.final_output.important[0].draft_reply is None
    assert result.final_output.can_wait[0].draft_reply is None
    assert result.final_output.ignore[0].draft_reply is None
    assert result.final_output.send_enabled is False
    assert result.final_output.sent is False
    assert result.final_output.live_side_effects_enabled is False
    assert "Group into urgent, important, can wait, and ignore" in prompt
    assert "Draft replies only for urgent items" in prompt
    assert "Source message count: 4" in prompt


def test_gmail_gt1_priority_grouping_rejects_non_urgent_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    typed_input = GmailPriorityGroupingSDKInput(
        messages=[
            GmailTriageSDKInput(
                message_id="important-1",
                subject="Research collaboration next month",
                body="Could we compare notes next month?",
            )
        ]
    )
    invalid_payload = _priority_grouping_payload(
        urgent=[],
        important=[
            _priority_grouping_item(
                message_id="important-1",
                bucket="important",
                subject="Research collaboration next month",
                category="collaboration_opportunity",
                priority="high",
                needs_reply=True,
                draft_reply="Thanks for reaching out. I can review this after approval.",
            )
        ],
        can_wait=[],
        ignore=[],
        source_message_count=1,
    )
    model = FakeModel(outputs=[[_structured_message(invalid_payload)]])
    provider = FakeProvider(model)

    with pytest.raises(ModelBehaviorError):
        run_gmail_priority_grouping_sdk(
            typed_input,
            run_config=build_local_run_config(provider),
        )


def test_retrieved_sdk_synthesis_harness_validates_and_audits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_SESSIONS", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_SESSION_ID", raising=False)
    monkeypatch.delenv("KEYSTONE_SDK_SESSION_DB", raising=False)
    raw_context = {
        "message_id": "fake-message-1",
        "subject": "Potential consulting project",
        "body": "We need advisory help.",
        "sender_name": "Example Sender",
        "sender_email": "sender@example.com",
    }
    events: list[str] = []
    model = FakeModel(outputs=[[_structured_message(_email_triage_payload())]])
    provider = FakeProvider(model)
    storage = FakeStorage()

    def retrieve() -> dict[str, str]:
        events.append("retrieve")
        return raw_context

    def normalize(value: dict[str, str]) -> GmailTriageSDKInput:
        events.append("normalize")
        return GmailTriageSDKInput(
            subject=value["subject"],
            body=value["body"],
            sender_name=value["sender_name"],
            sender_email=value["sender_email"],
            message_id=value["message_id"],
        )

    outcome = run_retrieved_sdk_synthesis(
        agent=build_gmail_triage_agent(),
        output_type=EmailTriageResult,
        retrieve=retrieve,
        normalize=normalize,
        input_summary="gmail message fake-message-1",
        input_audit_payload={
            "message_id": raw_context["message_id"],
            "subject": raw_context["subject"],
        },
        run_config=build_local_run_config(provider),
        save=True,
        storage=storage,
    )

    assert events == ["retrieve", "normalize"]
    assert isinstance(outcome.final_output, EmailTriageResult)
    assert outcome.live is False
    assert outcome.usage == {
        "available": True,
        "requests": 1,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cached_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "cache_hit_rate": 0.0,
        "prompt_cache_key_present": False,
        "prompt_cache_key_hash": "",
    }
    assert outcome.cost["amount_usd"] is None
    assert outcome.cost["source"] == "pricing_table_no_match"
    assert outcome.request_cache["request_layout"] == "static_agent_prefix_then_dynamic_typed_input"
    assert outcome.request_cache["repo_instruction_profile"] == "compact-runtime-policy"
    assert outcome.request_cache["session_attached"] is False
    assert outcome.request_cache["tool_count"] > 0
    assert len(outcome.request_cache["static_prefix_sha256"]) == 64
    assert len(outcome.request_cache["dynamic_prompt_sha256"]) == 64
    assert outcome.storage["agent_run"] == {"status": "saved", "id": 1}
    assert len(storage.agent_runs) == 1
    saved = storage.agent_runs[0]
    assert saved["status"] == "success"
    assert saved["dry_run"] is True
    assert saved["input_payload"] == {
        "message_id": "fake-message-1",
        "subject": "Potential consulting project",
    }
    assert "body" not in saved["input_payload"]
    assert isinstance(saved["output"]["result"], EmailTriageResult)
    assert saved["output"]["_sdk_usage"]["requests"] == 1
    assert saved["output"]["_sdk_request_cache"]["dynamic_prompt_chars"] > 0
    assert model.calls


def test_retrieved_sdk_synthesis_records_prompt_cache_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_model_env(monkeypatch)

    class RawResult:
        usage = {
            "requests": 1,
            "input_tokens": 10_000,
            "cached_input_tokens": 7_500,
            "output_tokens": 600,
            "total_tokens": 10_600,
        }
        _generated_prompt_cache_key = "keystone:gmail_triage:slack-thread:1715366400.000100"

    def fake_run_typed_sdk_sync(*_args: Any, **_kwargs: Any) -> tuple[RawResult, dict[str, Any]]:
        return RawResult(), _email_triage_payload()

    monkeypatch.setattr("keystone_agents.run.run_typed_sdk_sync", fake_run_typed_sdk_sync)

    outcome = run_retrieved_sdk_synthesis(
        agent=build_gmail_triage_agent(include_tools=False),
        output_type=EmailTriageResult,
        retrieve=lambda: {
            "subject": "Potential consulting project",
            "body": "Could we discuss consulting support?",
            "message_id": "fake-message-1",
        },
        normalize=lambda value: GmailTriageSDKInput(**value),
        input_summary="prompt cache metadata smoke test",
        config=ModelConfig(provider="openai", model="gpt-5.4-mini"),
    )

    expected_hash = hashlib.sha256(
        b"keystone:gmail_triage:slack-thread:1715366400.000100"
    ).hexdigest()[:12]
    assert outcome.usage["cache_hit_rate"] == 0.75
    assert outcome.usage["prompt_cache_key_present"] is True
    assert outcome.usage["prompt_cache_key_hash"] == expected_hash
    assert "1715366400" not in outcome.usage["prompt_cache_key_hash"]


def test_retrieved_sdk_synthesis_reports_gemini_free_tier_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_model_env(monkeypatch)
    model = FakeModel(outputs=[[_structured_message(_email_triage_payload())]])
    provider = FakeProvider(model)

    outcome = run_retrieved_sdk_synthesis(
        agent=build_gmail_triage_agent(),
        output_type=EmailTriageResult,
        retrieve=lambda: {
            "subject": "Potential consulting project",
            "body": "Could we discuss consulting support?",
            "sender_name": "Example Sender",
            "sender_email": "sender@example.com",
            "message_id": "fake-message-1",
        },
        normalize=lambda value: GmailTriageSDKInput(**value),
        input_summary="gmail message fake-message-1",
        input_audit_payload={"message_id": "fake-message-1"},
        run_config=build_local_run_config(provider),
        config=ModelConfig(provider=GEMINI_PROVIDER, model="gemini-2.5-flash"),
    )

    from keystone_agents.cli_sdk import sdk_synthesis_payload

    payload = sdk_synthesis_payload(outcome)

    assert payload["model"]["provider"] == "gemini"
    assert payload["model"]["name"] == "gemini-2.5-flash"
    assert payload["request_cache"]["request_layout"] == (
        "static_agent_prefix_then_dynamic_typed_input"
    )
    assert payload["gemini_free_tier_usage"]["available"] is True
    assert payload["gemini_free_tier_usage"]["requests_this_run"] == 1
    assert payload["gemini_free_tier_usage"]["requests_observed_today"] == 1
    assert payload["gemini_free_tier_usage"]["requests_per_day_limit"] == 250


def test_saved_sdk_synthesis_reports_local_gemini_request_day_total(
    tmp_path: Any,
) -> None:
    from keystone_agents.cli_sdk import sdk_synthesis_payload
    from keystone_agents.tools.storage_tool import StorageTool

    storage = StorageTool(f"sqlite:///{tmp_path / 'usage.db'}")
    model = FakeModel(
        outputs=[
            [_structured_message(_email_triage_payload())],
            [_structured_message(_email_triage_payload())],
        ]
    )
    provider = FakeProvider(model)

    def retrieve() -> dict[str, str]:
        return {
            "message_id": "fake-message-1",
            "subject": "Potential consulting project",
            "body": "We need advisory help.",
            "sender_name": "Example Sender",
            "sender_email": "sender@example.com",
        }

    def normalize(value: dict[str, str]) -> GmailTriageSDKInput:
        return GmailTriageSDKInput(
            subject=value["subject"],
            body=value["body"],
            sender_name=value["sender_name"],
            sender_email=value["sender_email"],
            message_id=value["message_id"],
        )

    for index in range(2):
        outcome = run_retrieved_sdk_synthesis(
            agent=build_gmail_triage_agent(),
            output_type=EmailTriageResult,
            retrieve=retrieve,
            normalize=normalize,
            input_summary=f"gmail message fake-message-{index + 1}",
            input_audit_payload={"message_id": f"fake-message-{index + 1}"},
            run_config=build_local_run_config(provider),
            config=ModelConfig(provider=GEMINI_PROVIDER, model="gemini-2.5-flash"),
            save=True,
            storage=storage,
        )

    payload = sdk_synthesis_payload(outcome)

    assert payload["gemini_free_tier_usage"]["daily_usage_source"] == (
        "local_sqlite_agent_runs_current_et_day"
    )
    assert payload["gemini_free_tier_usage"]["requests_this_run"] == 1
    assert payload["gemini_free_tier_usage"]["requests_observed_today"] == 2
    assert payload["gemini_free_tier_usage"]["requests_remaining_today"] == 248


def test_retrieved_sdk_synthesis_enforces_per_agent_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_model_env(monkeypatch)
    monkeypatch.setenv("KEYSTONE_AGENT_RUN_BUDGET_USD", "0.25")
    model = FakeModel(
        outputs=[[_structured_message(_email_triage_payload())]],
        usage=Usage(
            requests=1,
            input_tokens=100_000,
            output_tokens=100_000,
            total_tokens=200_000,
        ),
    )
    provider = FakeProvider(model)

    with pytest.raises(AgentRunBudgetExceededError, match="per-agent budget"):
        run_retrieved_sdk_synthesis(
            agent=build_gmail_triage_agent(),
            output_type=EmailTriageResult,
            retrieve=lambda: {"subject": "Budget test", "body": "Body"},
            normalize=lambda value: GmailTriageSDKInput(
                subject=value["subject"],
                body=value["body"],
                sender_email="sender@example.com",
            ),
            input_summary="budget guard",
            run_config=build_local_run_config(provider),
            config=ModelConfig(provider="openai", model="gpt-5.4-mini"),
        )
    assert model.calls


def test_retrieved_sdk_synthesis_records_error_for_invalid_model_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model = FakeModel(outputs=[[_structured_message({"category": "consulting_opportunity"})]])
    provider = FakeProvider(model)
    storage = FakeStorage()

    with pytest.raises(ModelBehaviorError):
        run_retrieved_sdk_synthesis(
            agent=build_gmail_triage_agent(),
            output_type=EmailTriageResult,
            retrieve=lambda: {
                "subject": "Potential consulting project",
                "body": "We need advisory help.",
            },
            normalize=lambda raw: GmailTriageSDKInput(
                subject=raw["subject"],
                body=raw["body"],
            ),
            input_summary="gmail invalid-output check",
            run_config=build_local_run_config(provider),
            save=True,
            storage=storage,
        )

    assert len(storage.agent_runs) == 1
    assert storage.agent_runs[0]["status"] == "error"
    assert storage.agent_runs[0]["error"] == "ModelBehaviorError"
    assert storage.agent_runs[0]["output"]["failure"]["kind"] == "schema_or_parse_error"
    assert storage.agent_runs[0]["output"]["send_enabled"] is False


def test_live_sdk_synthesis_retries_gemini_with_openai_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_runtime_model_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL", "gemini-2.5-flash")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_OPENAI_FALLBACK_MODEL", "gpt-5.4-mini")

    calls: list[str | None] = []

    class RawResult:
        final_output = _email_triage_payload()
        usage = Usage(requests=1, input_tokens=12, output_tokens=8, total_tokens=20)

    def fake_run_sync(agent: Any, prompt: str, *, run_config: Any) -> RawResult:
        calls.append(getattr(run_config, "model", None))
        if len(calls) == 1:
            raise RuntimeError("primary provider unavailable")
        return RawResult()

    monkeypatch.setattr(Runner, "run_sync", fake_run_sync)

    outcome = run_retrieved_sdk_synthesis(
        agent=build_gmail_triage_agent(include_tools=False),
        output_type=EmailTriageResult,
        retrieve=lambda: GmailTriageSDKInput(
            subject="Consulting support",
            body="Could we discuss consulting support?",
        ),
        normalize=lambda value: value,
        input_summary="fallback smoke test",
        live=True,
    )

    assert calls == ["gemini-2.5-flash", "gpt-5.4-mini"]
    assert outcome.model_provider == "openai"
    assert outcome.model_name == "gpt-5.4-mini"
    assert any("OpenAI fallback" in note for note in outcome.audit_notes)


def test_retrieved_sdk_synthesis_rejects_unsafe_trace_metadata_before_retrieval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model = FakeModel(outputs=[[_structured_message(_email_triage_payload())]])
    provider = FakeProvider(model)
    events: list[str] = []

    def retrieve() -> dict[str, str]:
        events.append("retrieve")
        return {"subject": "Potential consulting project", "body": "We need advisory help."}

    with pytest.raises(UnsafeTraceMetadataError):
        run_retrieved_sdk_synthesis(
            agent=build_gmail_triage_agent(),
            output_type=EmailTriageResult,
            retrieve=retrieve,
            normalize=lambda raw: GmailTriageSDKInput(
                subject=raw["subject"],
                body=raw["body"],
            ),
            input_summary="gmail unsafe trace check",
            run_config=build_local_run_config(provider),
            trace_metadata={"prompt": "Full email body must never be trace metadata."},
        )

    assert events == []
    assert model.calls == []


def test_retrieved_sdk_synthesis_rejects_sensitive_trace_payloads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model = FakeModel(outputs=[[_structured_message(_email_triage_payload())]])
    provider = FakeProvider(model)

    with pytest.raises(RuntimeError, match="must not include sensitive data"):
        run_retrieved_sdk_synthesis(
            agent=build_gmail_triage_agent(),
            output_type=EmailTriageResult,
            retrieve=lambda: {"subject": "Potential consulting project", "body": "Body."},
            normalize=lambda raw: GmailTriageSDKInput(
                subject=raw["subject"],
                body=raw["body"],
            ),
            input_summary="gmail sensitive trace check",
            run_config=build_local_run_config(provider),
            trace_include_sensitive_data=True,
        )

    assert model.calls == []


def test_retrieved_sdk_synthesis_requires_storage_before_model_when_saving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model = FakeModel(outputs=[[_structured_message(_email_triage_payload())]])
    provider = FakeProvider(model)

    with pytest.raises(RuntimeError, match="storage is required"):
        run_retrieved_sdk_synthesis(
            agent=build_gmail_triage_agent(),
            output_type=EmailTriageResult,
            retrieve=lambda: {"subject": "Potential consulting project", "body": "Body."},
            normalize=lambda raw: GmailTriageSDKInput(
                subject=raw["subject"],
                body=raw["body"],
            ),
            input_summary="gmail missing storage check",
            run_config=build_local_run_config(provider),
            save=True,
        )

    assert model.calls == []


def test_orchestrator_typed_runtime_uses_fake_model_without_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model = FakeModel(outputs=[[_structured_message(_orchestrator_payload())]])
    provider = FakeProvider(model)

    result = run_orchestrator_sdk(
        "Route Curebase for business research.",
        run_config=build_local_run_config(provider),
    )

    assert isinstance(result.final_output, OrchestratorResult)
    assert result.final_output.route == "business_research_analyst"
    assert result.final_output.send_enabled is False


def test_orchestrator_live_sdk_prompt_includes_backend_browser_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("KEYSTONE_PLAYWRIGHT_ENABLED", "true")
    model = FakeModel(outputs=[[_structured_message(_orchestrator_payload())]])
    provider = FakeProvider(model)

    run_orchestrator_sdk(
        "Diagnose https://example.com with backend browser diagnostics.",
        live=True,
        run_config=build_local_run_config(provider),
    )

    prompt = _model_input_text(model.calls[0]["input"])
    assert "backend_browser_diagnostics_allowed" in prompt
    assert "capture_browser_diagnostics" in prompt
    assert "Use live=true for backend browser diagnostics" in prompt
    assert "No writes, posts, sends" in prompt


def test_orchestrator_llm_output_review_uses_fake_model_with_cost_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model = FakeModel(outputs=[[_structured_message(_orchestrator_output_review_payload())]])
    provider = FakeProvider(model)

    review = review_specialist_output_llm(
        agent_name="gmail_triage",
        output=_email_triage_payload(
            body="Full body with token=SHOULD_NOT_APPEAR_111111111.",
            raw_result={"debug": "metadata"},
        ),
        request_summary="Review whether the triage output is human-readable.",
        run_type="local SDK",
        run_config=build_local_run_config(provider),
    )
    prompt = _model_input_text(model.calls[0]["input"])

    assert isinstance(review, OrchestratorOutputReview)
    assert review.review_mode == "llm"
    assert review.llm_review_used is True
    assert review.deterministic_baseline["status"] in {"pass", "partial", "fail"}
    assert review.cost_guard["mode"] == "llm_hybrid"
    assert review.cost_guard["deterministic_hard_gates_authoritative"] is True
    assert review.cost_guard["scope"] == (
        "structure, relevance, human readability, professional tone"
    )
    assert review.send_enabled is False
    assert any("SDK usage:" in note for note in review.audit_notes)
    assert any("SDK request cache diagnostics:" in note for note in review.audit_notes)
    assert "SHOULD_NOT_APPEAR" not in prompt
    assert "Full body with token" not in prompt
    assert "full body is not included in orchestrator review" in prompt


def test_orchestrator_llm_output_review_preserves_deterministic_hard_veto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model = FakeModel(outputs=[[_structured_message(_orchestrator_output_review_payload())]])
    provider = FakeProvider(model)

    review = review_specialist_output_llm(
        agent_name="gmail_triage",
        output=_email_triage_payload(send_enabled=True),
        request_summary="Review whether the triage output is safe to review.",
        run_type="local SDK",
        run_config=build_local_run_config(provider),
    )

    assert review.llm_review_used is True
    assert review.status == "fail"
    assert review.overall_score <= 50
    assert review.approval_boundary_ok is False
    assert review.test_pack_checks["Preserves no-send behavior"] == "fail"
    assert "deterministic=fail" in " ".join(review.audit_notes)


def test_outreach_constrained_sdk_style_sources_and_eval_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    typed_input = OutreachComposerSDKInput(
        company_name="Curebase",
        contact_name="Dr. Example",
        contact_title="Clinical Operations Lead",
        outreach_goal="compare notes on clinical AI evaluation support",
        approved_context=(
            "Approved source-backed context: Curebase decentralized clinical "
            "trial operations; source_ids fixture:curebase_company, "
            "fixture:sample_lead_curebase, fixture:contact_curebase_priya, "
            "keystone_profile."
        ),
        email_style_profile=(
            '{"profile_id":"default","approved_for_drafting":true,'
            '"preferred_phrases":["Happy to compare notes"],"signoffs":["Best,"]}'
        ),
    )
    payload = _outreach_draft_payload(
        email_body=(
            "Hi Dr. Example,\n\n"
            "I saw Curebase's decentralized clinical trial operations. "
            "Happy to compare notes on clinical AI evaluation support if useful.\n\n"
            "Best,\nKeystone"
        ),
        linkedin_note=(
            "Hi Dr. Example, happy to compare notes on clinical AI evaluation support if useful."
        ),
        personalization_rationale=(
            "Uses approved Curebase opportunity, contact, style, and Keystone context."
        ),
        facts_used=[
            {
                "claim_text": "Company name: Curebase",
                "source_id": "fixture:curebase_company",
                "confidence": 0.8,
                "claim_type": "company_identity",
            },
            {
                "claim_text": "decentralized clinical trial operations",
                "source_id": "fixture:sample_lead_curebase",
                "confidence": 0.7,
                "claim_type": "opportunity_signal",
            },
            {
                "claim_text": "Contact title: Clinical Operations Lead",
                "source_id": "fixture:contact_curebase_priya",
                "confidence": 0.7,
                "claim_type": "contact_context",
            },
            {
                "claim_text": (
                    "Keystone is focused on clinical AI evaluation and research operations."
                ),
                "source_id": "keystone_profile",
                "confidence": 1.0,
                "claim_type": "keystone_profile",
            },
        ],
        source_ids_used=[
            "fixture:curebase_company",
            "fixture:sample_lead_curebase",
            "fixture:contact_curebase_priya",
            "keystone_profile",
        ],
        style_profile_used=True,
        style_profile_id="default",
        approved_context_used=True,
    )
    model = FakeModel(outputs=[[_structured_message(payload)]])
    provider = FakeProvider(model)

    result = run_outreach_composer_sdk(
        typed_input,
        run_config=build_local_run_config(provider),
    )
    score = score_output_against_expected(
        result.output.model_dump(mode="json"),
        {
            "email_max_words": 180,
            "linkedin_max_chars": 300,
            "no_em_dash": True,
            "unsupported_claims_exact": [],
            "unsupported_claim_explanations_exact": [],
            "approval_required": True,
            "approval_status": "pending",
            "approved_context_used": True,
            "style_profile_used": True,
            "style_profile_id": "default",
            "facts_used_min": 4,
            "source_ids_used_min": 4,
            "copy_must_contain": ["Happy to compare notes", "Best"],
            "copy_must_not_contain": ["guaranteed", "proven results", "act now"],
        },
        agent="outreach",
    )

    assert isinstance(result.final_output, OutreachDraft)
    assert result.final_output.approval_required is True
    assert result.final_output.approval_scope == "external_use"
    assert result.final_output.send_enabled is False
    assert result.final_output.sent is False
    assert result.final_output.can_send_email is False
    assert result.final_output.style_profile_used is True
    assert set(result.final_output.source_ids_used) <= {
        fact.source_id for fact in result.final_output.facts_used
    }
    input_text = _model_input_text(model.calls[0]["input"])
    assert "Approved source-backed context" in input_text
    assert "Optional approved aggregate email style profile" in input_text
    assert score.passed is True


def test_outreach_constrained_sdk_receives_template_and_example_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    typed_input = OutreachComposerSDKInput(
        company_name="Curebase",
        contact_name="Dr. Example",
        contact_title="Clinical Operations Lead",
        outreach_goal="compare notes on clinical AI evaluation support",
        approved_context=(
            "Approved source-backed context only: Curebase decentralized clinical "
            "trial operations; source_ids fixture:curebase_company, "
            "fixture:sample_lead_curebase, keystone_profile."
        ),
        outreach_template=(
            '{"template_id":"low_pressure_intro","template_version":"v1",'
            '"fit_reason":"Good for first-touch outreach."}'
        ),
        example_guidance=(
            '{"examples":[{"example_id":"example_low_pressure_clinical_ai_intro",'
            '"structure_guidance":["open with one approved signal"],'
            '"cta_guidance":"Use a compare-notes CTA."}],'
            '"facts_policy":"Examples guide tone and structure only."}'
        ),
    )
    payload = _outreach_draft_payload(
        template_id="low_pressure_intro",
        template_version="v1",
        template_fit_reason="Good for first-touch outreach.",
        example_ids_used=["example_low_pressure_clinical_ai_intro"],
        example_guidance_used=True,
        approved_context_used=True,
    )
    model = FakeModel(outputs=[[_structured_message(payload)]])
    provider = FakeProvider(model)

    result = run_outreach_composer_sdk(
        typed_input,
        run_config=build_local_run_config(provider),
    )
    input_text = _model_input_text(model.calls[0]["input"])

    assert result.final_output.template_id == "low_pressure_intro"
    assert result.final_output.example_guidance_used is True
    assert result.final_output.send_enabled is False
    assert "Optional selected outreach template" in input_text
    assert "Optional approved RAG example guidance" in input_text
    assert "They do not provide factual claims about the current prospect" in input_text


def test_outreach_cli_sdk_can_complete_oc1_grounded_research_brief_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import scripts.run_outreach_draft as cli

    _clear_runtime_model_env(monkeypatch)
    model = FakeModel(
        outputs=[
            [
                _structured_message(
                    _outreach_draft_payload(
                        company_name="BriefOnly Health",
                        recipient=None,
                        contact_name=None,
                        contact_title=None,
                        outreach_goal="focus on Keystone's fit from the research brief",
                        email_subject="BriefOnly Health research workflow discussion",
                        email_body=(
                            "Hello,\n\n"
                            "I read the approved brief on BriefOnly Health's clinical "
                            "research workflow software and possible clinical AI "
                            "evaluation needs. Keystone's clinical AI evaluation and "
                            "research operations focus may be relevant.\n\n"
                            "Would a brief introductory conversation be useful?"
                        ),
                        linkedin_note=(
                            "Hello, open to a brief exchange on clinical AI evaluation "
                            "and research operations?"
                        ),
                        personalization_rationale=(
                            "Uses only the attached approved research brief and Keystone "
                            "profile context."
                        ),
                        facts_used=[
                            {
                                "claim_text": (
                                    "BriefOnly Health works on clinical research workflow software."
                                ),
                                "source_id": "fixture:brief_only_research_brief",
                                "confidence": 0.8,
                                "claim_type": "company_description",
                            },
                            {
                                "claim_text": (
                                    "The attached brief notes that BriefOnly Health may "
                                    "need support evaluating clinical AI workflows and "
                                    "research operations."
                                ),
                                "source_id": "fixture:brief_only_research_brief",
                                "confidence": 0.78,
                                "claim_type": "company_fit",
                            },
                            {
                                "claim_text": (
                                    "Keystone Neuroinformatics LLC is focused on clinical "
                                    "AI evaluation and research operations."
                                ),
                                "source_id": "keystone_profile",
                                "confidence": 1.0,
                                "claim_type": "keystone_profile",
                            },
                        ],
                        source_ids_used=[
                            "fixture:brief_only_research_brief",
                            "keystone_profile",
                        ],
                        drafting_mode="llm_constrained",
                        approved_context_used=True,
                        unsupported_claims_flagged=[],
                        approval_scope="external_use",
                    )
                )
            ]
        ]
    )
    provider = FakeProvider(model)
    monkeypatch.setattr(cli, "SDK_RUN_CONFIG_FACTORY", lambda: build_local_run_config(provider))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_outreach_draft.py",
            "--research-brief-fixture",
            "sample_company_brief_only",
            "--goal",
            "focus on Keystone's fit from the research brief",
            "--run-sdk",
            "--test-pack-report-dir",
            str(tmp_path),
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    draft = payload["output"]
    input_text = _model_input_text(model.calls[0]["input"])
    draft_text = "\n".join(
        [
            draft["email_subject"],
            draft["email_body"],
            draft["linkedin_note"],
            draft["personalization_rationale"],
        ]
    ).lower()

    assert payload["mode"] == "sdk-synthesis"
    assert payload["sdk_run_invoked"] is True
    assert payload["test_pack_report"]["status"] == "pass"
    assert draft["drafting_mode"] == "llm_constrained"
    assert draft["company_name"] == "BriefOnly Health"
    assert draft["approved_context_used"] is True
    assert draft["approval_required"] is True
    assert draft["send_enabled"] is False
    assert draft["sent"] is False
    assert draft["can_send_email"] is False
    assert draft["unsupported_claims_flagged"] == []
    assert set(draft["source_ids_used"]) <= {
        "fixture:brief_only_research_brief",
        "keystone_profile",
    }
    assert "Attached approved research brief only" in input_text
    assert "Do not invent shared contacts" in input_text
    assert "Default Keystone writing style guidance" in input_text
    assert "mutual interest" in input_text
    assert "fixture:sample_lead_curebase" not in input_text
    assert "shared contact" not in draft_text
    assert "series b" not in draft_text
    assert "fortune" not in draft_text
    report_markdown = Path(payload["test_pack_report"]["markdown_path"]).read_text(encoding="utf-8")
    report_payload = json.loads(
        Path(payload["test_pack_report"]["json_path"]).read_text(encoding="utf-8")
    )
    assert "OC-1 Grounded Outreach" in report_markdown
    assert "Usage And Cost" in report_markdown
    assert report_payload["input_summary"] == "focus on Keystone's fit from the research brief"
    assert report_payload["status"] == "pass"


def test_outreach_constrained_sdk_rejects_send_enabled_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    typed_input = OutreachComposerSDKInput(
        company_name="Curebase",
        approved_context="Approved fixture context.",
    )
    model = FakeModel(outputs=[[_structured_message(_outreach_draft_payload(send_enabled=True))]])
    provider = FakeProvider(model)

    with pytest.raises(ModelBehaviorError):
        run_outreach_composer_sdk(
            typed_input,
            run_config=build_local_run_config(provider),
        )


def test_outreach_constrained_sdk_rejects_unsupported_keystone_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    typed_input = OutreachComposerSDKInput(
        company_name="Curebase",
        approved_context="Approved fixture context.",
    )
    model = FakeModel(
        outputs=[
            [
                _structured_message(
                    _outreach_draft_payload(
                        email_body=(
                            "Hi Dr. Example,\n\n"
                            "Keystone has helped companies reduce enrollment delays. "
                            "Open to compare notes?"
                        ),
                    )
                )
            ]
        ]
    )
    provider = FakeProvider(model)

    with pytest.raises(OutputGuardrailTripwireTriggered):
        run_outreach_composer_sdk(
            typed_input,
            run_config=build_local_run_config(provider),
        )


def test_specialist_agent_runs_with_fake_model_without_openai_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model = FakeModel(outputs=[[_structured_message(_email_triage_payload())]])

    result = _run_with_fake_model(build_gmail_triage_agent(), model)

    assert isinstance(result.final_output, EmailTriageResult)
    assert result.final_output.category == "consulting_opportunity"
    assert result.final_output.approval_required is True


def test_fake_model_tool_call_executes_fixture_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model = FakeModel(
        outputs=[
            [_tool_call("get_gmail_message", {"message_id": "fixture-message"})],
            [_structured_message(_email_triage_payload())],
        ]
    )

    result = _run_with_fake_model(build_gmail_triage_agent(), model)

    assert isinstance(result.final_output, EmailTriageResult)
    assert len(model.calls) == 2
    assert "get_gmail_message" in model.calls[0]["tool_names"]
    assert any(item.type == "tool_call_item" for item in result.new_items)
    tool_outputs = [item for item in result.new_items if item.type == "tool_call_output_item"]
    assert tool_outputs
    assert "dry-run" in str(tool_outputs[0].output)


def test_chief_fake_model_cannot_call_receipt_create_tool_directly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from keystone_agents.finance_expense_receipts import FinanceReceiptEvidence

    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AIRTABLE_ALLOW_ATTACHMENT_UPLOADS", "true")
    monkeypatch.setenv("AIRTABLE_BASE_ID", "app_finance")
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "pat_test")
    monkeypatch.setenv("AIRTABLE_ALLOWED_TABLES", "Business Expenses")
    receipt = tmp_path / "receipt.pdf"
    receipt.write_bytes(b"%PDF-1.4\nreceipt fixture")
    monkeypatch.setattr(
        "keystone_agents.tools.internal_data_tools.extract_finance_receipt_evidence",
        lambda path: FinanceReceiptEvidence(
            source_path=str(path),
            filename="receipt.pdf",
            content_read=False,
            extraction_method="fixture_unreadable",
            blocker="fixture extraction unavailable",
        ),
    )
    monkeypatch.setattr(
        "keystone_agents.tools.internal_data_tools.airtable_get_base_schema_impl",
        lambda **_kwargs: {
            "status": "success",
            "schema": {
                "tables": [
                    {
                        "name": "Business Expenses",
                        "fields": [
                            {"name": "Merchant", "field_type": "multilineText"},
                            {"name": "Expense Date", "field_type": "date"},
                            {
                                "name": "Period",
                                "field_type": "singleSelect",
                                "select_choices": ["Q1", "Q2", "Q3", "Q4"],
                            },
                            {"name": "Grand Total", "field_type": "currency"},
                            {
                                "name": "Receipt File",
                                "field_type": "multipleAttachments",
                                "field_id": "fldReceipt",
                            },
                        ],
                    }
                ]
            },
        },
    )
    tool_args = {
        "local_file_path": str(receipt),
        "table": "Business Expenses",
        "base_alias": "finance_tax_tracker",
        "receipt_fields_json": json.dumps(
            {
                "vendor": "Acme Labs",
                "receipt_date": "2026-09-15",
                "description": "Lab supplies",
                "total": "199.25",
                "currency": "USD",
            },
            sort_keys=True,
        ),
        "field_values_json": json.dumps(
            {
                "Merchant": "Acme Labs",
                "Expense Date": "2026-09-15",
                "Period": "Q4",
                "Grand Total": "199.25",
            },
            sort_keys=True,
        ),
        "approval_reference": "slack-test-approved",
        "live": False,
    }
    model = FakeModel(
        outputs=[
            [_tool_call("airtable_create_expense_from_receipt", tool_args)],
            [
                _structured_message(
                    _chief_of_staff_payload(
                        summary=(
                            "Created a dry-run Airtable expense plan from model-read "
                            "receipt evidence."
                        ),
                        audit_notes=["Fake model called bounded receipt create tool."],
                    )
                )
            ],
        ]
    )
    request = (
        "chief of staff add a business expense to the airtable business expenses "
        f"based on the receipt details which are: {receipt}"
    )

    result = run_chief_of_staff_sdk(
        request,
        live=True,
        run_config=build_local_run_config(FakeProvider(model)),
    )

    assert result.output.summary == (
        "Created a dry-run Airtable expense plan from model-read receipt evidence."
    )
    assert len(model.calls) == 2
    assert "airtable_create_expense_from_receipt" in model.calls[0]["tool_names"]
    assert "airtable_get_base_schema" in model.calls[0]["tool_names"]


def test_fake_model_input_guardrail_tripwire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model = FakeModel(outputs=[[_structured_message(_email_triage_payload())]])
    provider = FakeProvider(model)

    with pytest.raises(InputGuardrailTripwireTriggered):
        Runner.run_sync(
            build_gmail_triage_agent(),
            "Please reply about patient John diagnosis and treatment.",
            run_config=build_local_run_config(provider),
        )

    assert model.calls == []


def test_fake_model_output_guardrail_tripwire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    unsafe_payload = _email_triage_payload(
        category="unrelated",
        priority="normal",
        summary="The message asks for medical advice.",
        recommended_action="Route to manual review.",
        draft_reply=None,
        draft_created=False,
        approval_required=False,
        risk_flags=["professional_advice"],
    )
    model = FakeModel(outputs=[[_structured_message(unsafe_payload)]])

    with pytest.raises(OutputGuardrailTripwireTriggered):
        _run_with_fake_model(build_gmail_triage_agent(), model)

    assert len(model.calls) == 1
