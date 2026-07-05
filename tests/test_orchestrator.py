from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import scripts.run_orchestrator as run_orchestrator
from keystone_agents.agents.orchestrator import (
    BUSINESS_RESEARCH_TOOL_NAME,
    INTENDED_HANDOFFS,
    OPPORTUNITY_SCOUT_TOOL_NAME,
    ORCHESTRATOR_SPECIALIST_TOOLS_ENV,
    _resume_from_state_result,
    build_orchestrator_agent,
    load_orchestrator_workflow_state,
    load_workflow_state_context,
    review_specialist_output,
    route_request,
    run_orchestrator_preflight,
)
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.models import AgentRunRequest, RunMode, TypedAgentRunResult
from keystone_agents.run import run_agent_dry
from keystone_agents.schemas.approval import ApprovalQueueItem
from keystone_agents.schemas.company_profile import CompanyProfile
from keystone_agents.schemas.orchestrator import (
    OrchestratorDecision,
    OrchestratorOutputReview,
    OrchestratorResult,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.test_pack_specs import get_test_pack_spec


def _database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'orchestrator.db'}"


def _tool_names(agent) -> set[str]:
    return {str(getattr(tool, "name", "")) for tool in agent.tools}


def test_build_orchestrator_agent() -> None:
    agent = build_orchestrator_agent()

    assert agent.name == "orchestrator"
    assert agent.output_type is OrchestratorResult
    assert "Orchestrator Agent" in agent.instructions
    assert "Keystone Profile" in agent.instructions
    assert BUSINESS_RESEARCH_TOOL_NAME not in _tool_names(agent)


def test_build_orchestrator_agent_can_opt_into_read_only_specialist_tool() -> None:
    agent = build_orchestrator_agent(include_handoffs=False, include_specialist_tools=True)

    assert BUSINESS_RESEARCH_TOOL_NAME in _tool_names(agent)
    assert OPPORTUNITY_SCOUT_TOOL_NAME in _tool_names(agent)
    assert agent.handoffs == []


def test_build_orchestrator_agent_specialist_tool_env_is_default_off(monkeypatch) -> None:
    monkeypatch.delenv(ORCHESTRATOR_SPECIALIST_TOOLS_ENV, raising=False)
    default_agent = build_orchestrator_agent(include_handoffs=False)

    monkeypatch.setenv(ORCHESTRATOR_SPECIALIST_TOOLS_ENV, "1")
    opt_in_agent = build_orchestrator_agent(include_handoffs=False)

    assert BUSINESS_RESEARCH_TOOL_NAME not in _tool_names(default_agent)
    assert BUSINESS_RESEARCH_TOOL_NAME in _tool_names(opt_in_agent)
    assert OPPORTUNITY_SCOUT_TOOL_NAME in _tool_names(opt_in_agent)


def test_orchestrator_cli_read_input_accepts_long_plain_text() -> None:
    request = "Find the best company for Keystone to contact this week. " * 10

    assert run_orchestrator._read_input(request) == request


def test_orchestrator_dry_run() -> None:
    agent = build_orchestrator_agent()
    result = run_agent_dry(agent, AgentRunRequest(mode=RunMode.DRY_RUN, payload={"input": None}))

    assert result.mode == RunMode.DRY_RUN
    assert result.agent_name == "orchestrator"


def test_resume_gate_does_not_block_concrete_thread_followup() -> None:
    result = _resume_from_state_result(
        (
            "workitem opportunity scout continue this prior Slack thread.\n"
            "Previous request: opportunity scout find 3 behavioral health AI opportunities.\n"
            "User follow-up: ok provide a weblink for each one of these 3"
        ),
        workflow_state={"pending_approvals": [{"id": "stale_approval"}]},
    )

    assert result is None


def test_resume_gate_does_not_block_source_link_followup_without_marker() -> None:
    result = _resume_from_state_result(
        (
            "chief of staff continue this prior Slack thread. "
            "Linked WorkItem: wi_123. Previous request: chief of staff deeper search. "
            "can u summarize link 1"
        ),
        workflow_state={"pending_approvals": [{"id": "stale_approval"}]},
    )

    assert result is None


def test_route_request_source_link_followup_bypasses_pending_approval_gate() -> None:
    result = route_request(
        "can u summarize link 1",
        workflow_state={"pending_approvals": [{"id": "approval_1"}]},
    )

    assert result.route == "chief_of_staff"
    assert result.refused is False
    assert result.stop_reason is None
    assert result.state_context_used is True


def test_route_request_generic_link_research_does_not_use_source_followup_shortcut() -> None:
    result = route_request("find links about OpenAI mental health")

    assert result.route != "chief_of_staff"


def test_resume_gate_does_not_block_natural_source_followup_with_pending_approval() -> None:
    result = _resume_from_state_result(
        (
            "chief of staff continue this prior Slack thread.\n"
            "Previous request: chief of staff source-backed research.\n"
            "Latest request: please explain the first source"
        ),
        workflow_state={"pending_approvals": [{"id": "stale_approval"}]},
    )

    assert result is None


def test_resume_gate_still_blocks_pure_continue_with_pending_approval() -> None:
    result = _resume_from_state_result(
        "continue this prior Slack thread",
        workflow_state={"pending_approvals": [{"id": "approval_1"}]},
    )

    assert result is not None
    assert result.route == "clarification"
    assert result.stop_reason == "Pending approval gate must be resolved before continuing."


def test_resume_gate_still_blocks_agent_prefixed_pure_continue() -> None:
    result = _resume_from_state_result(
        "chief of staff continue this prior Slack thread",
        workflow_state={"pending_approvals": [{"id": "approval_1"}]},
    )

    assert result is not None
    assert result.route == "clarification"
    assert result.stop_reason == "Pending approval gate must be resolved before continuing."


def test_orchestrator_decision_requires_review() -> None:
    decision = OrchestratorDecision(workflow=["triage"], stop_reason="insufficient evidence")

    assert decision.requires_human_review is True
    assert decision.send_enabled is False


def test_orchestrator_fills_draft_only_crm_fields_when_crm_is_requested() -> None:
    result = OrchestratorResult(
        route="clarification",
        workflow=["candidate_identification", "draft_preflight", "crm_preflight"],
        forbidden_actions=["save_to_crm", "send_email"],
        clarification_request="Confirm target and contact before CRM write.",
    )

    fields = {field.name: field.value for field in result.artifacts.crm_ready_fields}

    assert fields["company"] == "needs confirmation"
    assert fields["contact_email"] == "needs confirmation"
    assert fields["outreach_status"] == "draft_only_blocked_pending_approval"
    assert result.send_enabled is False
    assert any("not written externally" in note for note in result.artifacts.notes)


def test_orchestrator_business_research_route_includes_next_safe_agent_call() -> None:
    result = OrchestratorResult(
        route="business_research_analyst",
        artifacts={"crm_ready_fields": {"company": "Mentavi"}},
    )

    assert any(
        "run Business Research Analyst for Mentavi" in note for note in result.artifacts.notes
    )


def test_orchestrator_reviews_specialist_output_for_human_readability() -> None:
    review = review_specialist_output(
        agent_name="outreach_composer",
        request_summary="Draft source-backed Curebase outreach.",
        run_type="deterministic fixture",
        output={
            "company_name": "Curebase",
            "email_subject": "Curebase research workflow discussion",
            "email_body": "Hi Dr. Example, happy to compare notes if useful.",
            "linkedin_note": "Open to a brief exchange?",
            "personalization_rationale": "Uses approved Curebase context.",
            "facts_used": [{"claim_text": "Curebase is a clinical trial software company."}],
            "source_ids_used": ["fixture:curebase_company"],
            "approval_required": True,
            "send_enabled": False,
            "secret_note": "token=SHOULD_NOT_APPEAR_111111111",
        },
    )
    encoded = review.model_dump_json()

    assert isinstance(review, OrchestratorOutputReview)
    assert review.status == "pass"
    assert review.structure.status == "pass"
    assert review.readability.status == "pass"
    assert review.relevance.status == "pass"
    assert review.approval_boundary_ok is True
    assert review.send_enabled is False
    assert "SHOULD_NOT_APPEAR" not in encoded


def test_orchestrator_review_flags_non_human_metadata_and_send_boundary() -> None:
    review = review_specialist_output(
        agent_name="gmail_triage",
        output={
            "summary": "Consulting inquiry.",
            "category": "consulting_opportunity",
            "priority": "high",
            "recommended_action": "Draft for approval.",
            "risk_flags": [],
            "raw_result": {"provider_payload": "debug only"},
            "trace_metadata": {"span": "debug only"},
            "send_enabled": True,
        },
    )

    assert review.status == "fail"
    assert review.approval_boundary_ok is False
    assert review.test_pack_checks["Preserves no-send behavior"] == "fail"
    assert any("non-human metadata" in gap for gap in review.observed_gaps)
    assert any("side-effect" in gap for gap in review.observed_gaps)


def test_orchestrator_review_flags_em_dashes_in_specialist_output() -> None:
    review = review_specialist_output(
        agent_name="outreach_composer",
        output={
            "email_subject": "Clinical AI discussion",
            "email_body": "Hi Dr. Example — open to comparing notes?",
            "linkedin_note": "Open to a brief exchange?",
            "personalization_rationale": "Uses approved context.",
            "facts_used": [{"claim_text": "Curebase supports clinical trial operations."}],
            "source_ids_used": ["fixture:curebase_company"],
            "approval_required": True,
        },
    )

    assert review.tone.status == "partial"
    assert any("em dashes" in gap for gap in review.observed_gaps)


def test_orchestrator_review_flags_unrelated_chief_of_staff_output() -> None:
    review = review_specialist_output(
        agent_name="chief_of_staff",
        request_summary=(
            "Produce an execution brief for improving diverse operator requests. "
            "Prioritize specialist paths, missing context, approval gates, and validation."
        ),
        output={
            "summary": "2026 tax payments",
            "recommended_actions": ["Federal: $4,000", "Pennsylvania: $1,500"],
            "approval_required": True,
            "send_enabled": False,
            "audit_notes": ["No external write attempted."],
        },
    )

    assert review.relevance.status == "fail"
    assert any("Request/output term overlap is low" in gap for gap in review.observed_gaps)


@pytest.mark.parametrize(
    "request_summary",
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
def test_orchestrator_review_flags_wrong_response_diagnostic_wrong_lane(
    request_summary: str,
) -> None:
    review = review_specialist_output(
        agent_name="chief_of_staff",
        request_summary=request_summary,
        output={
            "summary": "2026 tax payments",
            "recommended_actions": ["Federal: $4,000", "Pennsylvania: $1,500"],
            "approval_required": True,
            "send_enabled": False,
        },
    )

    assert review.relevance.status == "fail"
    assert any(
        "diagnose a wrong or unrelated prior response" in gap for gap in review.observed_gaps
    )


def test_orchestrator_review_allows_wrong_response_diagnostic_answer() -> None:
    review = review_specialist_output(
        agent_name="chief_of_staff",
        request_summary=(
            "same response: 2026 tax payments Total paid $5,500. "
            "Let's see why above is posting -- were changes implemented in this run?"
        ),
        output={
            "summary": (
                "The prior response was stale and unrelated to the architecture request. "
                "The fix is to route through Orchestrator preflight and verify context."
            ),
            "recommended_actions": [
                "Inspect prior Slack context.",
                "Rerun the request through the canonical WorkItem manager loop.",
            ],
            "approval_required": True,
            "send_enabled": False,
        },
    )

    assert review.relevance.status != "fail"
    assert not any(
        "diagnose a wrong or unrelated prior response" in gap for gap in review.observed_gaps
    )


def test_orchestrator_review_ignores_echoed_request_when_checking_alignment() -> None:
    review = review_specialist_output(
        agent_name="chief_of_staff",
        request_summary=(
            "Produce an execution brief for improving diverse operator requests. "
            "Prioritize specialist paths, missing context, approval gates, and validation."
        ),
        output={
            "intent": (
                "Produce an execution brief for improving diverse operator requests. "
                "Prioritize specialist paths, missing context, approval gates, and validation."
            ),
            "summary": "2026 tax payments",
            "recommended_actions": ["Federal: $4,000", "Pennsylvania: $1,500"],
            "approval_required": True,
            "send_enabled": False,
            "audit_notes": ["No external write attempted."],
        },
    )

    assert review.relevance.status == "fail"
    assert any("Request/output term overlap is low" in gap for gap in review.observed_gaps)


def test_orchestrator_review_accepts_aligned_chief_of_staff_output() -> None:
    review = review_specialist_output(
        agent_name="chief_of_staff",
        request_summary=(
            "Produce an execution brief for improving diverse operator requests. "
            "Prioritize specialist paths, missing context, approval gates, and validation."
        ),
        output={
            "summary": (
                "Execution brief: improve diverse operator requests by tightening "
                "specialist paths, context gathering, approval gates, and validation."
            ),
            "recommended_actions": [
                "Route by capability shape before specialist handoff.",
                "Capture missing context and approval gates in the WorkItem trace.",
                "Validate with mixed operator request fixtures.",
            ],
            "approval_required": True,
            "send_enabled": False,
            "audit_notes": ["Read-only planning output."],
        },
    )

    assert review.relevance.status == "pass"
    assert not any("Request/output term overlap is low" in gap for gap in review.observed_gaps)


def test_orchestrator_review_accepts_concise_synonym_architecture_output() -> None:
    review = review_specialist_output(
        agent_name="chief_of_staff",
        request_summary=(
            "Assess the agent architecture for diverse requests, planner placement, "
            "routing, memory context, approvals, and validation feedback."
        ),
        output={
            "summary": "Planner-executor memo: merge Orchestrator control with agent handoffs.",
            "recommended_actions": [
                "Keep context packs and Slack thread memory attached to the control plane.",
                "Use gate checks and evaluator feedback before final user-facing output.",
            ],
            "approval_required": True,
            "send_enabled": False,
            "audit_notes": ["Read-only architecture review."],
        },
    )

    assert review.relevance.status == "pass"
    assert not any("Request/output term overlap is low" in gap for gap in review.observed_gaps)


def test_orchestrator_review_accepts_explained_scout_zero_result() -> None:
    review = review_specialist_output(
        agent_name="opportunity_scout",
        request_summary="Find consulting opportunities with strict exclusions.",
        output={
            "topic": (
                "Find opportunities, but exclude startups under 10 employees, exclude "
                "on-site roles, exclude unpaid roles, and exclude roles requiring a "
                "full-time practicing clinician."
            ),
            "records": [],
            "raw_search_result_count": 63,
            "deduped_candidate_count": 0,
            "audit_notes": [
                "Live search provider was used.",
                "Ran 8 targeted search queries.",
                "Applied strict hard filters for role discovery.",
                "Hard filters removed 63 candidate(s) before scoring.",
                (
                    "No candidates satisfied the requested exclusions with enough "
                    "evidence to verify role type, company size, remote status, and "
                    "compensation."
                ),
                "No outreach drafts were generated.",
            ],
            "outreach_generated": False,
        },
    )

    assert review.status == "pass"
    assert review.structure.status == "pass"
    assert review.relevance.status == "pass"
    assert review.approval_boundary_ok is True


def test_orchestrator_review_accepts_outreach_variant_set() -> None:
    review = review_specialist_output(
        agent_name="outreach_composer",
        request_summary="Write three grounded Curebase outreach variants.",
        output={
            "company_name": "Curebase",
            "outreach_goal": "Write three versions: formal, warm-professional, and very concise.",
            "requested_variant_labels": ["formal", "warm-professional", "very concise"],
            "variants": [
                {
                    "variant_label": "formal",
                    "draft": {
                        "email_subject": "Curebase clinical trial operations discussion",
                        "email_body": (
                            "Hi Dr. Shah,\n\nI thought there might be mutual interest "
                            "in working together on shared interests in clinical "
                            "research technology.\n\nBest,\nAnup"
                        ),
                        "linkedin_note": (
                            "I thought there might be mutual interest in clinical "
                            "research technology."
                        ),
                        "personalization_rationale": (
                            "Grounded in the approved Curebase research brief."
                        ),
                        "facts_used": [
                            {"claim_text": "Curebase provides clinical trial software."}
                        ],
                        "source_ids_used": ["fixture:curebase_research_brief", "keystone_profile"],
                        "approval_required": True,
                        "send_enabled": False,
                    },
                },
                {
                    "variant_label": "warm-professional",
                    "draft": {
                        "email_subject": "Curebase and clinical research technology",
                        "email_body": (
                            "Hi Dr. Shah,\n\nI thought there might be mutual interest "
                            "in working together on shared interests in clinical "
                            "research technology. Keystone may be relevant where "
                            "evidence generation and validation matter.\n\nBest,\nAnup"
                        ),
                        "linkedin_note": (
                            "Thought there may be mutual interest in clinical research technology."
                        ),
                        "personalization_rationale": (
                            "Uses the same approved context with a warmer tone."
                        ),
                        "facts_used": [
                            {"claim_text": "Curebase supports clinical trial operations."}
                        ],
                        "source_ids_used": ["fixture:curebase_research_brief", "keystone_profile"],
                        "approval_required": True,
                        "send_enabled": False,
                    },
                },
                {
                    "variant_label": "very concise",
                    "draft": {
                        "email_subject": "Curebase note",
                        "email_body": (
                            "Hi Dr. Shah,\n\nI thought there might be mutual interest "
                            "in working together on shared interests in clinical "
                            "research technology.\n\nBest,\nAnup"
                        ),
                        "linkedin_note": "Mutual interest in clinical research technology.",
                        "personalization_rationale": "Keeps only approved core context.",
                        "facts_used": [
                            {
                                "claim_text": (
                                    "Curebase is relevant to clinical research operations."
                                )
                            }
                        ],
                        "source_ids_used": ["fixture:curebase_research_brief", "keystone_profile"],
                        "approval_required": True,
                        "send_enabled": False,
                    },
                },
            ],
            "approval_required": True,
            "approval_scope": "external_use",
            "send_enabled": False,
        },
    )

    assert review.status == "pass"
    assert review.structure.status == "pass"
    assert review.relevance.status == "pass"
    assert review.approval_boundary_ok is True


def test_sample_email_routes_to_gmail_triage() -> None:
    result = route_request(
        {
            "from": "alex@example.com",
            "subject": "Consulting discussion",
            "body": "Would Keystone be open to discussing clinical AI evaluation support?",
        }
    )

    assert result.route == "gmail_triage"
    assert result.decision_trace is not None
    assert result.decision_trace.selected_route == "gmail_triage"
    assert "no_send_enforced" in result.decision_trace.safety_gates_applied
    assert result.target_agent == "Gmail Inbound Triage Agent"
    assert result.send_enabled is False


@pytest.mark.parametrize(
    "spec_id",
    ["GT-1", "GT-2", "GT-3", "GT-4", "GT-5"],
)
def test_gmail_test_pack_prompts_route_to_gmail_triage(spec_id: str) -> None:
    prompt = get_test_pack_spec(spec_id).natural_prompt

    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")

    result = route_request(prompt, manual_plan=manual_plan)

    assert result.route == "gmail_triage"
    assert result.target_agent == "Gmail Inbound Triage Agent"
    assert result.workflow == ["gmail_triage"]
    assert result.send_enabled is False
    assert "send_email" in result.forbidden_actions


def test_gmail_first_cross_agent_request_preserves_downstream_workflow() -> None:
    prompt = (
        "A Gmail consulting inquiry came in. Triage it, research the company, "
        "create an opportunity record, and draft a response only after approval."
    )

    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")
    result = route_request(prompt, manual_plan=manual_plan)

    assert result.route == "gmail_triage"
    assert result.workflow == [
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
    ]
    assert result.send_enabled is False


def test_company_url_routes_to_business_research_analyst() -> None:
    result = route_request("https://www.neuroflow.com")

    assert result.route == "business_research_analyst"
    assert result.target_agent == "Business Research Analyst"
    assert result.retrieval_hint is not None
    assert result.retrieval_hint.source == "request_heuristic"


def test_find_behavioral_health_ai_companies_routes_to_opportunity_scout() -> None:
    result = route_request("find 5 behavioral health AI companies")

    assert result.route == "opportunity_scout"
    assert result.decision_trace is not None
    assert result.decision_trace.handoff_readiness == "ready_for_sdk_handoff"
    assert result.target_agent == "Opportunity Scout Agent"
    assert result.retrieval_hint is not None
    assert result.retrieval_hint.source == "request_heuristic"


def test_explicit_business_research_smoke_request_does_not_route_to_scout() -> None:
    result = route_request(
        "LangGraph smoke 1: use preprints context agent history and Zotero context "
        "agent handoff, then run Business Research for NeuroFlow as an internal "
        "evidence-packet planning note. Live SDK is approved only for this bounded "
        "read-only smoke if the backend would normally use it; live web search is "
        "not approved. Use local/dry-run retrieval where possible. Do not send "
        "email, create drafts, post elsewhere, publish, schedule, or write external "
        "systems."
    )

    assert result.route == "business_research_analyst"
    assert result.target_agent == "Business Research Analyst"
    assert result.retrieval_hint is not None


def test_business_research_only_smoke_ignores_negated_scouting_and_outreach() -> None:
    prompt = (
        "Research smoke: research NeuroFlow for a short internal read-only company note. "
        "Stay on Business Research only; do not scout opportunities or draft outreach. "
        "Live SDK is approved only for this bounded read-only smoke if the backend would "
        "normally use it; live web search is not approved. Use local/dry-run retrieval "
        "where possible. Do not send email, create drafts, post elsewhere, publish, "
        "schedule, or write external systems."
    )
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")
    result = route_request(prompt, manual_plan=manual_plan)

    assert manual_plan.target_agent == "business_research_analyst"
    assert result.route == "business_research_analyst"
    assert result.target_agent == "Business Research Analyst"
    assert result.workflow == ["business_research_analyst"]
    assert "opportunity_scout" not in result.workflow
    assert "outreach_composer" not in result.workflow
    assert result.send_enabled is False


def test_recent_remote_role_search_routes_to_opportunity_scout() -> None:
    result = route_request(
        "Find up to 5 active U.S.-based remote roles posted in the last 7 days "
        "for a physician-scientist with behavioral health, clinical research, "
        "and AI experience. Exclude AI tutor roles."
    )

    assert result.route == "opportunity_scout"
    assert result.target_agent == "Opportunity Scout Agent"
    assert result.retrieval_hint is not None
    assert result.decision_trace is not None
    assert result.decision_trace.handoff_readiness == "ready_for_sdk_handoff"


def test_multiline_no_result_role_search_routes_to_opportunity_scout() -> None:
    result = route_request(get_test_pack_spec("OS-5").natural_prompt)

    assert result.route == "opportunity_scout"
    assert result.target_agent == "Opportunity Scout Agent"
    assert result.refused is False
    assert result.send_enabled is False


def test_opportunity_crm_save_request_is_scout_with_no_write_boundary() -> None:
    result = route_request("Find roles and save the top 3 to my CRM.")

    fields = {field.name: field.value for field in result.artifacts.crm_ready_fields}

    assert result.route == "opportunity_scout"
    assert result.target_agent == "Opportunity Scout Agent"
    assert "save_to_crm" in result.forbidden_actions
    assert "crm_write" in result.forbidden_actions
    assert "crm_preflight" in result.workflow
    assert result.approval_required is True
    assert result.external_use_approval_required is True
    assert result.send_enabled is False
    assert fields["outreach_status"] == "draft_only_blocked_pending_approval"
    assert any("CRM save/write request" in note for note in result.audit_notes)


def test_finance_tracker_airtable_expense_write_does_not_trigger_crm_boundary() -> None:
    result = route_request(
        "chief of staff add a business expense to the airtable business expenses "
        "based on the receipt details which are: "
        "/tmp/example-business-cards-receipt.pdf"
    )

    assert result.route == "chief_of_staff"
    assert "crm_preflight" not in result.workflow
    assert "save_to_crm" not in result.forbidden_actions
    assert "crm_write" not in result.forbidden_actions
    assert not any("CRM save/write request" in note for note in result.audit_notes)


def test_negated_crm_record_creation_does_not_add_crm_boundary() -> None:
    result = route_request(
        "Opportunity scout agent: use only this sanitized inline context. "
        "Baylight Rehab is considering whether Keystone could review an outcomes "
        "dashboard before an internal pilot. Do not draft outreach, send, schedule, "
        "write files, create CRM records, publish, or post elsewhere."
    )

    assert result.route == "opportunity_scout"
    assert "crm_preflight" not in result.workflow
    assert "save_to_crm" not in result.forbidden_actions
    assert "crm_write" not in result.forbidden_actions
    assert not any("CRM save/write request" in note for note in result.audit_notes)


@pytest.mark.parametrize(
    "prompt",
    [
        "Create no CRM records; summarize the provided context only.",
        "Create zero CRM records and do not write to Airtable.",
        "Research the company, but create no CRM records or opportunity records.",
    ],
)
def test_zero_crm_record_constraints_do_not_add_crm_boundary(prompt: str) -> None:
    result = route_request(prompt)

    assert "crm_preflight" not in result.workflow
    assert "save_to_crm" not in result.forbidden_actions
    assert "crm_write" not in result.forbidden_actions


def test_chief_of_staff_handoff_with_negated_outreach_does_not_trigger_draft_gate() -> None:
    prompt = (
        "chief of staff agent: diagnostic case diag_cos_20260620_003_diverse_ops_handoff "
        "Use only this sanitized inline context. Cedar Lane Diagnostics is considering whether "
        "Keystone could help review an internal lab-operations quality dashboard before an "
        "October management review. No PHI is included. No external action is approved. "
        "Return a concise internal handoff with the best next owner or agent, why that path "
        "fits, what information Keystone should request before committing, and what remains "
        "blocked. If recommending another agent, use Chief of Staff -> Business Research Agent "
        "or Chief of Staff -> Airtable Context Agent notation as appropriate. Do not access "
        "Gmail, Airtable, Drive, Zotero, Slack history, web search, browser automation, or "
        "external tools. Do not draft outreach, send, schedule, write files, create CRM records, "
        "publish, or post elsewhere."
    )
    manual_plan = infer_manual_request_plan(prompt, requested_agent="chief_of_staff")

    result = route_request(prompt, manual_plan=manual_plan)

    assert result.route == "chief_of_staff"
    assert result.refused is False
    assert result.workflow == ["chief_of_staff"]
    assert "outreach_composer" not in result.workflow
    assert result.stop_reason is None
    assert not any("approval-context gate blocked outreach" in note for note in result.audit_notes)


@pytest.mark.parametrize(
    ("requested_agent", "expected_route"),
    [
        ("rss_context_agent", "rss_context_agent"),
        ("preprints_context_agent", "preprints_context_agent"),
    ],
)
def test_explicit_feed_context_agents_preserve_route(
    requested_agent: str,
    expected_route: str,
) -> None:
    prompt = (
        "Read-only context test. Use available feed context, not browser automation "
        "or live web search. Return a concise Answer and Detailed Summary."
    )
    manual_plan = infer_manual_request_plan(prompt, requested_agent=requested_agent)
    result = route_request(prompt, manual_plan=manual_plan)

    assert result.route == expected_route
    assert result.workflow == [expected_route]
    assert result.send_enabled is False


@pytest.mark.parametrize(
    ("prompt", "expected_route"),
    [
        (
            "@KNI rss context agent read-only eval: inspect announcement history. "
            "Do not refresh feeds, post to Slack, create files, schedule, draft "
            "outreach, or mutate announcement history.",
            "rss_context_agent",
        ),
        (
            "@KNI preprints context agent read-only eval: inspect preliminary "
            "preprint evidence. Do not refresh feeds, post to Slack, create files, "
            "schedule, draft outreach, or treat preliminary findings as validated.",
            "preprints_context_agent",
        ),
    ],
)
def test_explicit_feed_context_agents_ignore_negated_post_side_effects(
    prompt: str,
    expected_route: str,
) -> None:
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")
    result = route_request(prompt, manual_plan=manual_plan)

    assert manual_plan.target_agent == expected_route
    assert manual_plan.intent == "context_lookup"
    assert result.route == expected_route
    assert result.refused is False
    assert result.workflow == [expected_route]
    assert result.send_enabled is False


def test_colon_named_business_research_mention_preserves_agent_reasoning_path() -> None:
    prompt = (
        "@KNI business research analyst: diagnostic case diag_business_research_abrdg_001 "
        "Read-only source-backed business research test for Abridge. Use live search if "
        "available. Identify up to 2 practical healthcare buyer-fit angles for Keystone, "
        "explain why each might fit, and state key caveats. Keep answer concise and include "
        "visible source URLs. Do not draft outreach, send, schedule, write files, create CRM "
        "records, publish, or post elsewhere."
    )
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")
    result = route_request(prompt, manual_plan=manual_plan)

    assert manual_plan.target_agent == "business_research_analyst"
    assert manual_plan.primary_target == "Abridge"
    assert result.route == "business_research_analyst"
    assert result.workflow == ["business_research_analyst"]
    assert "opportunity_scout" not in result.workflow
    assert "outreach_composer" not in result.workflow
    assert result.send_enabled is False
    assert result.retrieval_hint is not None


def test_context_agent_affirmative_post_request_still_refuses_send_side_effect() -> None:
    prompt = (
        "@KNI rss context agent inspect announcement history and post this message "
        "to Slack for the channel."
    )
    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")
    result = route_request(prompt, manual_plan=manual_plan)

    assert manual_plan.target_agent == "rss_context_agent"
    assert manual_plan.intent == "blocked_send"
    assert result.route == "clarification"
    assert result.refused is True
    assert result.approval_required is True
    assert result.send_enabled is False


def test_find_and_send_outreach_preserves_draft_only_workflow() -> None:
    result = route_request("Find and send outreach to the best three companies.")

    assert result.route == "opportunity_scout"
    assert result.refused is False
    assert result.send_enabled is False
    assert result.approval_scope == "external_use"
    assert "send_email" in result.forbidden_actions
    assert result.workflow == [
        "opportunity_scout",
        "business_research_analyst",
        "outreach_composer",
        "send_blocked",
    ]
    assert "draft-only" in result.approval_rationale


def test_find_and_draft_outreach_preserves_cross_agent_workflow() -> None:
    result = route_request("Find outreach targets and draft emails to the best three companies.")

    assert result.route == "opportunity_scout"
    assert result.refused is False
    assert result.send_enabled is False
    assert "send_email" in result.forbidden_actions
    assert result.workflow == [
        "opportunity_scout",
        "business_research_analyst",
        "outreach_composer",
    ]


@pytest.mark.parametrize(
    ("spec_id", "expected_workflow"),
    [
        (
            "OR-1",
            [
                "gmail_triage",
                "business_research_analyst",
                "opportunity_scout",
                "outreach_composer",
                "chief_of_staff",
            ],
        ),
        (
            "OR-2",
            [
                "business_research_analyst",
                "opportunity_scout",
                "outreach_composer",
            ],
        ),
        (
            "OR-5",
            [
                "gmail_triage",
                "business_research_analyst",
                "opportunity_scout",
                "outreach_composer",
                "chief_of_staff",
            ],
        ),
    ],
)
def test_orchestrator_workflow_prompts_preserve_requested_agent_sequence(
    spec_id: str,
    expected_workflow: list[str],
) -> None:
    prompt = (
        get_test_pack_spec(spec_id)
        .natural_prompt.replace("[Company]", "Lindus Health")
        .replace(
            "[CEO, Head of Clinical Operations, Head of Partnerships, or Medical Director]",
            "Head of Partnerships",
        )
    )

    manual_plan = infer_manual_request_plan(prompt, requested_agent="orchestrator")
    result = route_request(prompt, manual_plan=manual_plan)

    assert result.workflow == expected_workflow
    assert "crm_preflight" not in result.workflow
    assert "crm_write" not in result.forbidden_actions


@pytest.mark.parametrize(
    "prompt",
    [
        "Review the business-agent architecture changes and recommend the next three implementation steps.",
        "Audit enabled automations and identify stale, duplicate, or unsafe schedules without changing them.",
        (
            "Summarize the selected Slack thread, identify unresolved operator requests, "
            "and propose an internal follow-up plan."
        ),
    ],
)
def test_orchestrator_routes_operational_planning_to_chief_of_staff(prompt: str) -> None:
    result = route_request(prompt)

    assert result.route == "chief_of_staff"
    assert result.workflow == ["chief_of_staff"]
    assert result.refused is False
    assert result.send_enabled is False


def test_outreach_request_without_approved_profile_refuses() -> None:
    result = route_request("draft outreach to NeuroFlow")

    assert result.route == "clarification"
    assert result.refused is True
    assert result.approved_context_present is False
    assert "Approved CompanyProfile" in (result.stop_reason or "")
    assert result.send_enabled is False
    assert result.approval_scope == "drafting"
    assert "blocked" in result.approval_rationale


def test_outreach_request_without_approved_profile_has_sectioned_operator_summary() -> None:
    result = route_request("draft outreach to NeuroFlow")

    assert result.clarification_request is not None
    assert "What should this outreach focus on?" in result.clarification_request
    assert "*Answer:*" in result.clarification_request
    assert "*What I need:*" in result.clarification_request
    assert "*Reply with:*" in result.clarification_request
    assert "Focus: what the email or message should accomplish." in result.clarification_request
    assert "recipient, target contact, or target organization" in result.clarification_request
    assert "permission to use the context already in this thread" in result.clarification_request
    assert "external action has been taken" in result.clarification_request
    assert "Outreach Composer needs approved drafting context" not in result.clarification_request
    assert "cannot create draft-only outreach" not in result.clarification_request
    assert "blocked" not in result.clarification_request.lower()
    assert "CompanyProfile" not in result.clarification_request
    assert "OpportunityRecord" not in result.clarification_request
    assert "WorkItem" not in result.clarification_request
    assert result.route == "clarification"
    assert result.refused is True


def test_orchestrator_preflight_hard_safety_summary_preserves_execution_gate() -> None:
    preflight = run_orchestrator_preflight(
        (
            "@KNI outreach composer draft a note using this named patient story: "
            "Jane Doe improved after treatment."
        ),
        requested_agent="outreach_composer",
    )

    assert preflight.execution_allowed is False
    assert preflight.block_kind == "safety"
    assert preflight.route_result.route == "clarification"
    assert preflight.route_result.clarification_request is not None
    assert "Outreach blocked by safety gate" in preflight.route_result.clarification_request
    assert "*Answer:*" in preflight.route_result.clarification_request
    assert "*Detailed Summary:*" in preflight.route_result.clarification_request
    assert "*Next step:*" in preflight.route_result.clarification_request
    assert "No draft, send, post" in preflight.route_result.clarification_request


def test_generic_outreach_to_this_company_blocks_for_missing_context() -> None:
    result = route_request("Write an outreach email to this company.")

    assert result.route == "clarification"
    assert result.refused is True
    assert result.send_enabled is False
    assert "Approved CompanyProfile" in (result.stop_reason or "")


def test_attached_research_brief_outreach_blocks_without_context_object() -> None:
    result = route_request(
        "Write a short outreach email to Curebase based only on the attached "
        "research brief. Focus on Keystone fit. Do not invent shared contacts, "
        "traction, or product details."
    )

    assert result.route == "clarification"
    assert result.refused is True
    assert result.send_enabled is False
    assert "Approved CompanyProfile" in (result.stop_reason or "")


@pytest.mark.parametrize("spec_id", ["BR-1", "BR-4"])
def test_business_research_prompts_with_outreach_angle_do_not_route_to_composer(
    spec_id: str,
) -> None:
    prompt = get_test_pack_spec(spec_id).natural_prompt

    result = route_request(prompt)

    assert result.route == "business_research_analyst"
    assert result.refused is False
    assert result.workflow == ["business_research_analyst"]


@pytest.mark.parametrize("spec_id", ["OC-2", "OC-3", "OC-4", "OC-5"])
def test_outreach_test_pack_prompts_block_without_approved_context(spec_id: str) -> None:
    prompt = get_test_pack_spec(spec_id).natural_prompt.replace("[Company]", "Curebase")

    result = route_request(prompt)

    assert result.route == "clarification"
    assert result.refused is True
    assert result.send_enabled is False
    assert "Approved CompanyProfile" in (result.stop_reason or "")


def test_research_then_outreach_request_routes_to_research_first() -> None:
    result = route_request("Research Mentavi and prepare draft-only outreach only after approval")

    assert result.route == "business_research_analyst"
    assert result.target_agent == "Business Research Analyst"
    assert result.approval_required is True
    assert result.approval_scope == "drafting"
    assert result.retrieval_hint is not None
    assert result.retrieval_hint.source == "request_heuristic"
    assert "research must run first" in result.rationale
    assert result.send_enabled is False


def test_outreach_request_with_approved_profile_routes_to_composer() -> None:
    profile = CompanyProfile(
        name="NeuroFlow",
        website="https://www.neuroflow.com",
        description="Behavioral health technology company.",
        fit_summary="Relevant to behavioral health analytics.",
    )

    result = route_request("draft outreach to NeuroFlow", approved_company_profile=profile)

    assert result.route == "outreach_composer"
    assert result.target_agent == "Outreach Composer Agent"
    assert result.approved_context_present is True
    assert result.send_enabled is False
    assert result.approval_scope == "external_use"
    assert result.external_use_approval_required is True
    assert "external-use approval" in result.approval_rationale


def test_outreach_request_with_approved_inline_context_routes_to_composer() -> None:
    result = route_request(
        "outreach composer agent: diagnostic case diag_outreach "
        "Use only this approved inline context from a sanitized Gmail triage diagnostic. "
        "Do not research externally. Target recipient: Alex Rivera, Partnerships Lead, "
        "Example Health. Approved context: Alex asked whether Keystone could help review "
        "Example Health's remote patient monitoring AI validation workflow before a July "
        "pilot proposal. No PHI is included. Return a human-useful draft-only email "
        "paragraph plus brief caveats. Do not send email, create a Gmail draft, publish, "
        "or post elsewhere."
    )

    assert result.route == "outreach_composer"
    assert result.approved_context_present is True
    assert result.send_enabled is False
    assert result.external_use_approval_required is True


def test_outreach_request_accepts_flexible_approved_context_labels() -> None:
    result = route_request(
        "outreach composer agent: diagnostic case diag_outreach flexible labels "
        "Prepare a draft-only email paragraph. Target contact: Alex Rivera at Example "
        "Health. Approved evidence: Example Health asked whether Keystone could review "
        "its remote patient monitoring AI validation workflow before a July pilot. "
        "No PHI is included. Do not send email, create a Gmail draft, publish, or post."
    )

    assert result.route == "outreach_composer"
    assert result.approved_context_present is True
    assert result.send_enabled is False
    assert result.external_use_approval_required is True


def test_orchestrator_can_attach_optional_operator_feedback_request() -> None:
    profile = CompanyProfile(
        name="NeuroFlow",
        website="https://www.neuroflow.com",
        description="Behavioral health technology company.",
        fit_summary="Relevant to behavioral health analytics.",
    )

    result = route_request(
        "draft outreach to NeuroFlow",
        approved_company_profile=profile,
        include_operator_feedback_request=True,
    )

    assert result.route == "outreach_composer"
    assert result.operator_feedback_request is not None
    assert result.operator_feedback_request.object_type == "outreach_draft"
    assert result.operator_feedback_request.optional is True
    assert result.operator_feedback_request.send_enabled is False
    assert "weak_personalization" in result.operator_feedback_request.suggested_tags
    assert "not_worth_using" in result.operator_feedback_request.suggested_tags


def test_orchestrator_feedback_request_is_opt_in() -> None:
    result = route_request("https://www.neuroflow.com")

    assert result.route == "business_research_analyst"
    assert result.operator_feedback_request is None


def test_llm_orchestrator_route_preserves_explicit_retrieval_hint() -> None:
    result = route_request(
        "please handle this",
        use_llm=True,
        llm_router=lambda *_args, **_kwargs: {
            "route": "business_research_analyst",
            "rationale": "Route to company research.",
            "retrieval_hint": {
                "source": "llm_router",
                "needs_structured_enrichment": True,
                "reasons": ["leadership context likely matters"],
            },
        },
    )

    assert result.route == "business_research_analyst"
    assert result.retrieval_hint is not None
    assert result.retrieval_hint.source == "llm_router"
    assert result.retrieval_hint.needs_structured_enrichment is True


def test_orchestrator_cli_prints_feedback_question_when_requested(capsys) -> None:
    assert (
        run_orchestrator.main(
            [
                "--input",
                "https://www.neuroflow.com",
                "--ask-feedback",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out

    assert "Feedback object: company_profile business_research_analyst" in output
    assert "Question: What should the operator approve, revise, or reject" in output
    assert "Rate good, okay, poor" in output


def test_orchestrator_cli_live_sdk_loads_dotenv_and_outputs_json(monkeypatch, capsys) -> None:
    calls: list[str] = []

    def fake_load_settings(*, force_dotenv: bool = False):
        calls.append(f"force_dotenv={force_dotenv}")
        return None

    def fake_model_config(_agent_name: str):
        return SimpleNamespace(
            as_log_dict=lambda: {
                "provider": "openai",
                "model": "gpt-5.4-mini",
                "base_url": None,
                "use_responses": None,
                "api_key": "[masked]",
                "gemini_api_key": None,
                "litellm_base_url": None,
            }
        )

    def fake_run_orchestrator_sdk(input_text: str, *, live: bool):
        assert input_text == "boundary test"
        assert live is True
        return TypedAgentRunResult(
            agent_name="orchestrator",
            output=OrchestratorResult(
                route="clarification",
                rationale="Live SDK route stayed approval gated.",
                send_enabled=False,
                can_send_email=False,
            ),
            raw_result=object(),
            live=True,
        )

    monkeypatch.setattr(run_orchestrator, "load_settings", fake_load_settings)
    monkeypatch.setattr(run_orchestrator, "get_runtime_agent_model_config", fake_model_config)
    monkeypatch.setattr(run_orchestrator, "run_orchestrator_sdk", fake_run_orchestrator_sdk)

    assert run_orchestrator.main(["--input", "boundary test", "--live-sdk", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert calls == ["force_dotenv=True"]
    assert payload["live_sdk"] is True
    assert payload["model"]["model"] == "gpt-5.4-mini"
    assert payload["output"]["send_enabled"] is False


def test_send_email_request_is_refused() -> None:
    result = route_request("send email to this lead")

    assert result.route == "clarification"
    assert result.refused is True
    assert result.send_enabled is False
    assert result.can_send_email is False
    assert result.approval_scope == "external_use"
    assert "does not enable automatic email sending" in result.approval_rationale
    assert "send_email" in result.forbidden_actions


def test_uncertain_input_returns_clarification_request() -> None:
    result = route_request("please handle this")

    assert result.route == "clarification"
    assert result.clarification_request
    assert result.send_enabled is False
    assert result.routing_mode == "deterministic"


def test_ambiguous_input_can_use_llm_router_after_hard_gates() -> None:
    calls: list[dict[str, object]] = []

    def fake_llm_router(prompt: str, workflow_state: object) -> dict[str, object]:
        calls.append({"prompt": json.loads(prompt), "workflow_state": workflow_state})
        return {
            "route": "opportunity_scout",
            "rationale": "The ambiguous request appears to ask for lead prioritization.",
        }

    result = route_request(
        "help me prioritize where to look next",
        use_llm=True,
        llm_router=fake_llm_router,
    )

    assert result.route == "opportunity_scout"
    assert result.target_agent == "Opportunity Scout Agent"
    assert result.routing_mode == "llm"
    assert result.send_enabled is False
    assert result.can_send_email is False
    assert calls
    assert "opportunity_scout" in calls[0]["prompt"]["allowed_routes"]


def test_llm_requested_without_runner_uses_deterministic_fallback() -> None:
    result = route_request("please handle this", use_llm=True)

    assert result.route == "clarification"
    assert result.routing_mode == "llm_unavailable"
    assert result.stop_reason == "uncertain_input"
    assert any("deterministic fallback" in note for note in result.audit_notes)


def test_send_email_request_is_refused_before_llm_router() -> None:
    def fail_llm_router(_prompt: str, _workflow_state: object) -> dict[str, object]:
        raise AssertionError("LLM router must not run for send requests")

    result = route_request(
        "send email to this lead",
        use_llm=True,
        llm_router=fail_llm_router,
    )

    assert result.route == "clarification"
    assert result.refused is True
    assert result.routing_mode == "deterministic"
    assert "send_email" in result.forbidden_actions


@pytest.mark.parametrize(
    "text",
    [
        "Patient Jane Doe was diagnosed with depression and needs a draft.",
        "Please review the contract terms and tell me our legal position.",
        "Give medical advice and a treatment plan for this case.",
        "Urgent payment needed today, login and update ACH details.",
    ],
)
def test_safety_risks_are_refused_before_llm_router(text: str) -> None:
    def fail_llm_router(_prompt: str, _workflow_state: object) -> dict[str, object]:
        raise AssertionError("LLM router must not run for hard safety refusals")

    result = route_request(text, use_llm=True, llm_router=fail_llm_router)

    assert result.route == "clarification"
    assert result.refused is True
    assert result.send_enabled is False
    assert result.artifacts["risk_flags"]
    assert "Python safety gate" in (result.stop_reason or "")


def test_pending_approval_state_blocks_resume() -> None:
    result = route_request(
        "resume this workflow",
        workflow_state={
            "approval_items": [
                {
                    "id": "approval-1",
                    "object_type": "outreach_draft",
                    "object_id": "draft-1",
                    "approval_status": "pending",
                    "title": "Review draft",
                }
            ]
        },
    )

    assert result.route == "clarification"
    assert result.refused is True
    assert result.state_context_used is True
    assert result.artifacts["state_gate"] == "pending_approval"
    assert "Pending approval gate" in (result.stop_reason or "")


def test_approved_state_context_can_route_draft_request() -> None:
    result = route_request(
        "draft outreach to NeuroFlow",
        workflow_state={
            "approval_items": [
                {
                    "id": "approval-company",
                    "object_type": "company_profile",
                    "object_id": "company-1",
                    "approval_status": "approved",
                    "title": "Approved profile",
                }
            ]
        },
    )

    assert result.route == "outreach_composer"
    assert result.approved_context_present is True
    assert result.state_context_used is True
    assert result.send_enabled is False


def test_resume_from_prior_route_decision() -> None:
    result = route_request(
        "continue from the last step",
        workflow_state={
            "prior_route_decisions": [
                {
                    "route": "business_research_analyst",
                    "target_agent": "Business Research Analyst",
                    "stop_reason": "",
                }
            ]
        },
    )

    assert result.route == "business_research_analyst"
    assert result.target_agent == "Business Research Analyst"
    assert result.artifacts["resumed_from_route"] == "business_research_analyst"
    assert result.state_context_used is True


def test_database_workflow_state_merges_caller_slack_context(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    SQLiteStore(database_url)

    result = route_request(
        "chief of staff summarize the selected Slack thread",
        database_url=database_url,
        workflow_state={
            "slack_context": {
                "channel_id": "C123",
                "selected_message_ts": "1715366400.000100",
                "thread_ts": "1715366400.000100",
            },
            "recent_slack_thread": [
                {
                    "ts": "1715366400.000100",
                    "summary": "Please summarize the current blockers.",
                }
            ],
        },
    )

    state_summary = result.workflow_state_summary.model_dump(mode="json")
    assert result.state_context_used is True
    assert state_summary["slack_context"]["channel_id"] == "C123"
    assert state_summary["recent_slack_thread"][0]["summary"] == (
        "Please summarize the current blockers."
    )


def test_workflow_state_context_summarizes_storage_without_bodies(tmp_path) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_outreach_draft(
        {
            "company_name": "Curebase",
            "contact_name": "Dr. Example",
            "email_subject": "Clinical AI workflow discussion",
            "email_body": "Draft body with token=SHOULD_NOT_APPEAR_111111111.",
            "approval_state": "pending",
        }
    )
    store.save_approval_item(
        ApprovalQueueItem(
            id="approval-state",
            object_type="outreach_draft",
            object_id="draft-1",
            title="Draft review",
            summary="Review draft before external use.",
            draft_text="Sensitive draft token=SHOULD_NOT_APPEAR_222222222.",
            source_agent="outreach_composer",
        )
    )
    store.save_orchestrator_result(
        OrchestratorResult(route="business_research_analyst", rationale="Prior route."),
        input_summary="prior route",
    )

    context = load_workflow_state_context(database_url=database_url)
    tool_context = json.loads(load_orchestrator_workflow_state(database_url=database_url))
    encoded = json.dumps(context, sort_keys=True)

    assert context["pending_approvals"][0]["id"] == "approval-state"
    assert context["outreach_drafts"][0]["subject"] == "Clinical AI workflow discussion"
    assert context["prior_route_decisions"][0]["route"] == "business_research_analyst"
    assert context["send_enabled"] is False
    assert tool_context["send_enabled"] is False
    assert "SHOULD_NOT_APPEAR" not in encoded
    assert "Draft body with token" not in encoded


def test_handoff_metadata_or_intended_handoff_list_exists() -> None:
    agent = build_orchestrator_agent()

    assert len(INTENDED_HANDOFFS) == 9
    assert {handoff.agent_name for handoff in INTENDED_HANDOFFS} == {
        "Gmail Inbound Triage Agent",
        "Business Research Analyst",
        "Opportunity Scout Agent",
        "Outreach Composer Agent",
        "Airtable Context Agent",
        "Google Workspace Context Agent",
        "Zotero Context Agent",
        "RSS Context Agent",
        "Preprints Context Agent",
    }
    assert getattr(agent, "handoffs", None)
    assert getattr(agent, "intended_handoffs", None) or getattr(agent, "handoff_contract", None)


def test_orchestrator_agent_can_disable_executable_handoffs_for_route_only_sdk() -> None:
    agent = build_orchestrator_agent(include_handoffs=False)

    assert not getattr(agent, "handoffs", None)
    assert getattr(agent, "intended_handoffs", None) or getattr(agent, "handoff_contract", None)


def test_no_route_can_send_email() -> None:
    results = [
        route_request({"subject": "Hello", "from": "person@example.com", "body": "Hi"}),
        route_request("https://example.com"),
        route_request("find grants and partners"),
        route_request("draft outreach", approved_company_profile=CompanyProfile(name="Example Co")),
        route_request("send email to someone"),
        route_request("unclear"),
    ]

    assert all(result.send_enabled is False for result in results)
    assert all(result.can_send_email is False for result in results)
