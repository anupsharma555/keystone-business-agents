from __future__ import annotations

import json
from pathlib import Path

import scripts.list_feedback as list_feedback_cli
import scripts.render_test_pack_case2_report as case2_report_cli
from keystone_agents.agents.gmail_triage import run_gmail_triage_fixture
from keystone_agents.agents.opportunity_scout import scout_opportunities_fixture
from keystone_agents.agents.outreach_composer import (
    compose_outreach_draft_fixture,
    load_company_profile,
    load_opportunity_record,
)
from keystone_agents.company_research import research_company_fixture
from keystone_agents.reporting import (
    APPROVAL_WARNING,
    build_gmail_priority_grouping_test_pack_payload,
    build_outreach_oc1_test_pack_payload,
    build_test_pack_case2_payload,
    build_test_pack_case_payload,
    render_company_comparison_report,
    render_company_profile_report,
    render_gmail_priority_grouping_test_pack_report,
    render_gmail_thread_summary_report,
    render_gmail_triage_report,
    render_local_operator_dashboard,
    render_markdown_table,
    render_operator_dashboard_decision,
    render_opportunity_scout_report,
    render_orchestrated_search_handoff_report,
    render_orchestrator_output_review,
    render_outreach_blocked_report,
    render_outreach_draft_report,
    render_outreach_oc1_test_pack_report,
    render_pipeline_report,
    render_review_card_markdown,
    render_review_card_slack_text,
    render_test_pack_case2_report,
    render_test_pack_case2_slack_text,
    render_test_pack_case_report,
    render_test_pack_slack_text,
    review_card_from_outreach_draft,
    safe_export_text,
    sensitive_text_summary,
    to_json,
)
from keystone_agents.schemas.approval import ApprovalQueueItem
from keystone_agents.schemas.email_triage import GmailPriorityGroupingResult
from keystone_agents.schemas.review_card import ReviewCard
from keystone_agents.storage.sqlite_store import SQLiteStore

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'reporting.db'}"


def _assert_report_clean(markdown: str) -> None:
    assert markdown.strip()
    assert "\u2014" not in markdown


def test_gmail_triage_report_renders_with_approval_warning() -> None:
    result = run_gmail_triage_fixture(
        FIXTURES / "sample_email_consulting.txt",
        sender_name="Example Sender",
        sender_email="sender@example.com",
    )

    markdown = render_gmail_triage_report(result)

    _assert_report_clean(markdown)
    assert "Sender:" in markdown
    assert "Subject:" in markdown
    assert "Draft Reply" in markdown
    assert APPROVAL_WARNING in markdown


def test_gmail_triage_report_summarizes_inbound_body_without_exporting_it() -> None:
    markdown = render_gmail_triage_report(
        {
            "sender_name": "Example Sender",
            "sender_email": "sender@example.com",
            "subject": "Sensitive inbound note",
            "category": "consulting_opportunity",
            "priority": "high",
            "needs_reply": True,
            "risk_flags": [],
            "summary": "Review without exporting full body.",
            "thread_context": "Earlier messages discussed scope and timing.",
            "body": (
                "Full inbound content with api_key=SHOULD_NOT_APPEAR_555555555 and private details."
            ),
            "approval_required": True,
        }
    )

    _assert_report_clean(markdown)
    assert "Inbound Body" in markdown
    assert "Thread Context" in markdown
    assert "scope and timing" in markdown
    assert "sha256=" in markdown
    assert "[omitted: full body is not included in exports]" in markdown
    assert "Full inbound content" not in markdown
    assert "SHOULD_NOT_APPEAR" not in markdown


def test_gmail_thread_summary_report_renders_read_only_sections() -> None:
    markdown = render_gmail_thread_summary_report(
        {
            "status": "read",
            "thread_id": "thread-123",
            "label": "SENT",
            "query": "to:curebase newer_than:14d",
            "message_count": 3,
            "needs_reply": False,
            "send_enabled": False,
            "summary": "Short thread summary.",
            "participants": ["anup@example.com", "priya@curebase.com"],
            "action_items": [
                {
                    "action": "Send the revised scope note.",
                    "owner": "Keystone",
                    "due_hint": "next week",
                    "source_message_id": "msg-2",
                }
            ],
            "deadlines": [
                {
                    "deadline": "next week",
                    "context": "Requested in the latest reply.",
                    "source_message_id": "msg-3",
                }
            ],
            "open_questions": ["Does Curebase want a short call or an email exchange?"],
            "triage_limitations": ["Thread summary is sanitized and read-only."],
        }
    )

    _assert_report_clean(markdown)
    assert "Gmail Thread Summary Report" in markdown
    assert "Source label: SENT" in markdown
    assert "Action Items" in markdown
    assert "Deadlines" in markdown
    assert "Read-only only." in markdown


def test_gmail_priority_grouping_test_pack_report_includes_human_draft_output() -> None:
    result = GmailPriorityGroupingResult.model_validate(
        {
            "request_summary": "GT-1 fixture grouping.",
            "source_label": "UNREAD",
            "lookback_days": 3,
            "source_message_count": 2,
            "urgent": [
                {
                    "message_id": "msg-1",
                    "thread_id": "thread-1",
                    "subject": "Consulting support",
                    "bucket": "urgent",
                    "category": "consulting_opportunity",
                    "confidence": 0.95,
                    "priority": "urgent",
                    "summary": "Consulting inquiry.",
                    "reasoning": "Relevant and timely.",
                    "needs_reply": True,
                    "recommended_action": "Draft for approval.",
                    "recommended_labels": ["Keystone/Triage"],
                    "draft_reply": "Full draft text should appear in the test report.",
                    "draft_created": True,
                    "approval_required": True,
                }
            ],
            "important": [
                {
                    "message_id": "msg-2",
                    "thread_id": "thread-2",
                    "subject": "Collaboration next month",
                    "bucket": "important",
                    "category": "collaboration_opportunity",
                    "confidence": 0.9,
                    "priority": "high",
                    "summary": "Potential collaboration.",
                    "reasoning": "Relevant but not urgent.",
                    "recommended_action": "Follow up later.",
                    "recommended_labels": ["Keystone/Triage"],
                }
            ],
            "can_wait": [],
            "ignore": [],
            "draft_count": 1,
            "send_enabled": False,
            "sent": False,
            "live_side_effects_enabled": False,
        }
    )

    payload = build_gmail_priority_grouping_test_pack_payload(
        result,
        run_type="live SDK",
        model="openai test-model",
        input_summary="Review unread emails.",
        input_source="Local fixtures.",
        usage={
            "available": True,
            "requests": 1,
            "input_tokens": 100,
            "cached_input_tokens": 0,
            "output_tokens": 50,
            "reasoning_output_tokens": 0,
            "total_tokens": 150,
        },
        cost={"source": "local_pricing_table", "estimated_usd": 0.0001},
        gemini_free_tier_usage={
            "available": True,
            "requests_this_run": 1,
            "requests_observed_today": 4,
            "daily_usage_source": "local_sqlite_agent_runs_current_et_day",
            "requests_per_day_limit": 250,
            "requests_remaining_today": 246,
            "used_to_improve_products": True,
        },
        orchestrator_review={
            "agent_name": "gmail_triage",
            "review_mode": "deterministic",
            "overall_score": 92,
            "status": "pass",
            "approval_boundary_ok": True,
            "send_enabled": False,
            "structure": {
                "dimension": "structure",
                "score": 95,
                "status": "pass",
                "rationale": "Human-readable grouping.",
            },
            "tone": {
                "dimension": "tone",
                "score": 90,
                "status": "pass",
                "rationale": "Professional tone.",
            },
            "readability": {
                "dimension": "readability",
                "score": 90,
                "status": "pass",
                "rationale": "Readable for operators.",
            },
            "relevance": {
                "dimension": "relevance",
                "score": 92,
                "status": "pass",
                "rationale": "Relevant for Keystone review.",
            },
            "observed_gaps": ["None observed."],
            "recommended_next_step": "Ready for human review.",
        },
    )
    markdown = render_gmail_priority_grouping_test_pack_report(payload)

    assert payload["status"] == "partial"
    assert payload["safety"]["send_enabled"] is False
    assert payload["orchestrator_review"]["status"] == "pass"
    assert payload["draft_outputs"][0]["draft_reply"] == (
        "Full draft text should appear in the test report."
    )
    assert "Draft Outputs" in markdown
    assert "Full draft text should appear in the test report." in markdown
    assert "GT-1 Priority Grouping" in markdown
    assert "Usage And Cost" in markdown
    assert "Gemini free-tier request context" in markdown
    assert "Drafts only urgent items" in markdown
    assert "Orchestrator Review" in markdown


def test_outreach_oc1_test_pack_report_includes_output_usage_and_safety() -> None:
    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
    )
    payload = build_outreach_oc1_test_pack_payload(
        draft,
        run_type="deterministic fixture",
        model="deterministic fixture/no LLM",
        input_summary="Write a short outreach email to Curebase using approved context.",
        input_source="Company and opportunity fixtures.",
        usage={"requests": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        cost={"estimated_cost_usd": 0.0, "currency": "USD"},
    )
    markdown = render_outreach_oc1_test_pack_report(payload)

    assert payload["status"] == "pass"
    assert payload["safety"]["send_enabled"] is False
    assert payload["safety"]["can_send_email"] is False
    assert "OC-1 Grounded Outreach" in markdown
    assert "Usage And Cost" in markdown
    assert "Send enabled: no" in markdown
    assert "Curebase" in markdown


def test_case2_test_pack_report_renders_gmail_draft_only_with_slack_summary() -> None:
    payload = build_test_pack_case2_payload(
        "GT-2",
        {
            "subject": "COI request",
            "draft_reply": (
                "Hi Pat,\n\nThanks for sending this over. Could you please send the "
                "certificate of insurance when you have a chance?\n\nBest,\nAnup"
            ),
            "draft_created": True,
            "approval_required": True,
            "send_enabled": False,
            "sent": False,
            "thread_id": "thread-coi",
            "thread_context": (
                "Two prior emails discussed the certificate request and broker follow-up."
            ),
        },
        run_type="live SDK",
        model="gemini/gemini-2.5-flash",
        input_source="live Gmail read path; draft-only output; no send",
        usage={"requests": 1, "input_tokens": 220, "output_tokens": 80, "total_tokens": 300},
    )
    markdown = render_test_pack_case2_report(payload)
    slack_text = render_test_pack_case2_slack_text(payload)

    assert payload["spec_id"] == "GT-2"
    assert payload["status"] == "pass"
    assert payload["live_llm_mode"] is True
    assert payload["safety"]["send_enabled"] is False
    assert "human_feedback" in payload["learning_policy"]["memory_types"]
    assert "Draft Only" in markdown
    assert "Agent Output" in markdown
    assert "Could you please send the certificate of insurance" in markdown
    assert "Thread Context" in markdown
    assert "Learning Capture" in markdown
    assert "positive replies or feedback" in markdown
    assert "broker follow-up" in slack_text
    assert "Operator Feedback" in markdown
    assert "Draft a reply to the insurance broker" in markdown
    assert "Keystone test-pack GT-2" in slack_text
    assert "certificate of insurance" in slack_text
    assert "Learning:" in slack_text
    assert len(slack_text) < 2400
    _assert_report_clean(markdown)
    _assert_report_clean(slack_text)


def test_test_pack_pass_with_partial_orchestrator_review_surfaces_revision_state() -> None:
    draft_reply = (
        "Hi Pat,\n\nThanks for sending this over. Could you please send the "
        "certificate of insurance when you have a chance?\n\nBest,\nAnup"
    )
    payload = build_test_pack_case2_payload(
        "GT-2",
        {
            "subject": "COI request",
            "draft_reply": draft_reply,
            "draft_created": True,
            "approval_required": True,
            "send_enabled": False,
            "sent": False,
        },
        run_type="live SDK",
        model="gemini/gemini-2.5-flash",
        input_source="live Gmail read path; draft-only output; no send",
        orchestrator_review={
            "agent_name": "gmail_triage",
            "review_mode": "deterministic",
            "overall_score": 78,
            "status": "partial",
            "approval_boundary_ok": True,
            "send_enabled": False,
            "structure": {
                "dimension": "structure",
                "score": 80,
                "status": "partial",
                "rationale": "Needs a cleaner operator-facing summary.",
            },
            "tone": {
                "dimension": "tone",
                "score": 90,
                "status": "pass",
                "rationale": "Professional.",
            },
            "readability": {
                "dimension": "readability",
                "score": 75,
                "status": "partial",
                "rationale": "Readable, but extra metadata slows review.",
            },
            "relevance": {
                "dimension": "relevance",
                "score": 80,
                "status": "partial",
                "rationale": "Useful but needs revision before approval.",
            },
            "observed_gaps": ["Extra metadata slows human review."],
            "recommended_next_step": "Revise the human-facing summary before approval.",
        },
    )

    markdown = render_test_pack_case2_report(payload)
    slack_text = render_test_pack_case2_slack_text(payload)

    assert payload["status"] == "pass"
    assert payload["orchestrator_review"]["status"] == "partial"
    assert payload["review_state"]["state"] == "revision_needed"
    assert payload["review_state"]["label"] == "revision-needed"
    assert payload["next_step"] == "Revise the human-facing summary before approval."
    assert "Review state: revision-needed" in markdown
    assert "Revise the human-facing summary before approval." in markdown
    assert "Review: revision-needed" in slack_text
    assert draft_reply in markdown
    _assert_report_clean(markdown)
    _assert_report_clean(slack_text)


def test_case2_gmail_missing_requested_action_is_feedback_gap_not_hard_fail() -> None:
    payload = build_test_pack_case2_payload(
        "GT-2",
        {
            "subject": "COI request",
            "draft_reply": (
                "Hi Pat,\n\nThanks for reaching out. We will review internally.\n\nBest,\nAnup"
            ),
            "draft_created": True,
            "approval_required": True,
            "send_enabled": False,
            "sent": False,
        },
        run_type="live SDK",
        model="gemini/gemini-2.5-flash",
        input_source="live Gmail read path; draft-only output; no send",
    )
    slack_text = render_test_pack_case2_slack_text(payload)

    assert payload["status"] == "partial"
    assert payload["checks"]["Asks broker for the COI"] == "partial"
    assert "missing_requested_action" in payload["operator_feedback_request"]["suggested_tags"]
    assert "Asks broker for the COI: partial" in slack_text


def test_case2_test_pack_report_renders_company_conflicts() -> None:
    payload = build_test_pack_case2_payload(
        "BR-2",
        {
            "company_name": "Example Health",
            "confidence_score": 0.62,
            "contradictions": [
                {
                    "claim": "Funding status",
                    "source_a": "Series A announced",
                    "source_b": "bootstrapped profile",
                }
            ],
            "sources": [
                {"source_id": "source:a", "url": "https://example.com/a"},
                {"source_id": "source:b", "url": "https://example.com/b"},
            ],
            "missing_information": ["Primary company funding page not found."],
        },
        run_type="live SDK",
        model="openai/gpt-5.4-mini",
        input_source="live Serper source bundle",
    )
    markdown = render_test_pack_case2_report(payload)
    slack_text = render_test_pack_case2_slack_text(payload)

    assert payload["status"] == "pass"
    assert payload["output_summary"]["conflict_count"] == 1
    assert payload["output_detail"]["contradictions"]
    assert "Conflicting Sources" in markdown
    assert "Funding status" in markdown
    assert "https://example.com/a" in slack_text
    assert "Confidence is lowered" in markdown
    assert "Keystone test-pack BR-2" in slack_text
    assert "Contradictions:" in slack_text
    assert len(slack_text.splitlines()) <= 19


def test_case2_test_pack_report_renders_opportunity_filters() -> None:
    payload = build_test_pack_case2_payload(
        "OS-2",
        {
            "topic": "filtered remote advisory roles",
            "records": [],
            "filtered_candidates": [
                {"title": "On-site clinician role", "reason": "Removed: on-site role."},
                {"title": "Unpaid advisor", "reason": "Removed: unpaid role."},
            ],
            "no_results_due_to_filters": True,
            "constraint_relaxation_suggestion": "Relax recency before relaxing remote-only.",
        },
        run_type="live SDK",
        model="openai/gpt-5.4-mini",
        input_source="live search results with hard-filter post-processing",
    )
    markdown = render_test_pack_case2_report(payload)
    slack_text = render_test_pack_case2_slack_text(payload)

    assert payload["status"] == "pass"
    assert payload["output_summary"]["returned_count"] == 0
    assert payload["output_summary"]["removed_count"] == 2
    assert "Hard Filters" in markdown
    assert "No opportunities returned." in markdown
    assert "Returns no records" in markdown
    assert "Keystone test-pack OS-2" in slack_text
    assert "No opportunities returned" in slack_text


def test_case2_test_pack_report_renders_outreach_variants_in_markdown_and_slack() -> None:
    payload = build_test_pack_case2_payload(
        "OC-2",
        {
            "variants": [
                {
                    "variant_label": "formal",
                    "email_body": "Formal approved context only.",
                    "source_ids_used": ["source:company", "keystone_profile"],
                    "approval_required": True,
                    "send_enabled": False,
                    "outreach_context": {
                        "company_profile": {
                            "website": "https://examplehealth.ai",
                            "sources": [
                                {
                                    "source_id": "source:company",
                                    "title": "Example Health profile",
                                    "url": "https://examplehealth.ai/about",
                                }
                            ],
                        }
                    },
                },
                {
                    "variant_label": "warm-professional",
                    "email_body": "Warm approved context only.",
                    "source_ids_used": ["keystone_profile", "source:company"],
                    "approval_required": True,
                    "send_enabled": False,
                },
                {
                    "variant_label": "very concise",
                    "email_body": "Concise approved context only.",
                    "source_ids_used": ["source:company", "keystone_profile"],
                    "approval_required": True,
                    "send_enabled": False,
                },
            ],
            "send_enabled": False,
        },
        run_type="live SDK",
        model="gemini/gemini-2.5-flash",
        input_source="approved company and opportunity fixtures",
        cost={"source": "pricing_table", "estimated_usd": 0.0002},
    )
    markdown = render_test_pack_case2_report(payload)
    slack_text = render_test_pack_case2_slack_text(payload)

    assert payload["status"] == "pass"
    assert payload["output_summary"]["variant_count"] == 3
    assert payload["safety"]["approval_required_detected"] is True
    assert "Tone Variants" in markdown
    assert "Relevant Links" in markdown
    assert "https://examplehealth.ai" in slack_text
    assert "Formal approved context only." in markdown
    assert "Facts remain constant" in markdown
    assert "Keystone test-pack OC-2" in slack_text
    assert "Formal approved context only" in slack_text
    assert len(slack_text) < 3000
    _assert_report_clean(markdown)
    _assert_report_clean(slack_text)


def test_test_pack_case3_report_renders_gmail_send_boundary() -> None:
    payload = build_test_pack_case_payload(
        "GT-3",
        {
            "subject": "Please reply",
            "draft_reply": "Hi Pat,\n\nThanks for the note. I can prepare this for review.",
            "recommended_action": "Draft created for human approval; no send path is available.",
            "draft_created": True,
            "approval_required": True,
            "send_enabled": False,
            "sent": False,
        },
        run_type="live SDK",
        model={"provider": "gemini", "name": "gemini-2.5-flash", "run_mode": "live_sdk"},
        input_source="live Gmail output; no send",
    )
    markdown = render_test_pack_case_report(payload)
    slack_text = render_test_pack_slack_text(payload)

    assert payload["spec_id"] == "GT-3"
    assert payload["status"] == "pass"
    assert payload["model"] == "gemini/gemini-2.5-flash (live_sdk)"
    assert "Send Boundary" in markdown
    assert "Draft Or Refusal Text" in markdown
    assert "Keystone test-pack GT-3" in slack_text
    assert "Draft/refusal" in slack_text
    _assert_report_clean(markdown)


def test_test_pack_case3_report_renders_company_comparison() -> None:
    payload = build_test_pack_case_payload(
        "BR-3",
        {
            "company_a": {"name": "Mentavi", "website": "https://www.mentavi.com"},
            "company_b": {"name": "NeuroFlow", "website": "https://www.neuroflow.com"},
            "decision_goal": "possible Keystone partnership",
            "decision_criteria": ["clinical_ai_relevance", "evidence_generation_need"],
            "side_by_side_entries": [
                {
                    "criterion_label": "Clinical AI relevance",
                    "company_a_summary": "ADHD assessment and treatment workflow.",
                    "company_b_summary": "Behavioral health engagement platform.",
                    "better_fit": "company_b",
                    "rationale": "Broader behavioral health workflow alignment.",
                    "company_a_unknowns": ["Enterprise partnership motion unclear."],
                    "company_b_unknowns": ["Current AI roadmap unclear."],
                }
            ],
            "recommendation": "Prioritize NeuroFlow for an initial exploratory conversation.",
            "recommended_company": "company_b",
            "evidence_gaps": ["Current partnership decision-maker is unknown."],
        },
        run_type="deterministic comparison",
        model="deterministic fixture/no LLM",
        input_source="two source-backed company profiles",
    )
    markdown = render_test_pack_case_report(payload)
    slack_text = render_test_pack_slack_text(payload)

    assert payload["status"] == "pass"
    assert payload["checks"]["Produces a side-by-side comparison"] == "pass"
    assert "Side-By-Side Entries" in markdown
    assert "Evidence Gaps" in markdown
    assert "Mentavi vs NeuroFlow" in slack_text


def test_test_pack_case3_report_renders_opportunity_ambiguity() -> None:
    payload = build_test_pack_case_payload(
        "OS-3",
        {
            "topic": "digital health opportunities",
            "scope_note": (
                "Assuming U.S.-based remote advisory or partnership opportunities aligned "
                "with Keystone clinical AI and behavioral health preferences."
            ),
            "records": [
                {
                    "company_name": "Example Health",
                    "title": "Clinical AI advisor",
                    "priority_score": 82,
                    "why_now_signal": "Behavioral health AI expansion.",
                    "keystone_fit_reason": "Aligns with Keystone clinical AI evaluation.",
                    "sources": [{"source_id": "source:a", "url": "https://example.com/a"}],
                }
            ],
        },
        run_type="live SDK",
        model="openai/gpt-5.4-mini",
        input_source="live search output",
    )
    markdown = render_test_pack_case_report(payload)
    slack_text = render_test_pack_slack_text(payload)

    assert payload["status"] == "pass"
    assert "Scope note" in markdown
    assert "Clinical AI advisor" in markdown
    assert "Scope:" in slack_text


def test_test_pack_case3_report_accepts_reasoned_opportunity_abstention() -> None:
    payload = build_test_pack_case_payload(
        "OS-3",
        {
            "topic": "digital health opportunities",
            "constraint_relaxation_suggestion": (
                "Broaden filters, extend recency, or include adjacent advisory categories."
            ),
            "records": [],
            "audit_notes": [
                "No candidates satisfied deterministic acceptance: Scout requires a real "
                "organization plus active opportunity evidence before scoring."
            ],
        },
        run_type="live SDK",
        model="openai/gpt-5.4-mini",
        input_source="live search output",
    )
    markdown = render_test_pack_case_report(payload)

    assert payload["status"] == "pass"
    assert payload["checks"]["States assumptions or narrows scope"] == "pass"
    assert payload["checks"]["Does not invent user intent"] == "pass"
    assert "No opportunities returned." in markdown


def test_test_pack_case3_report_renders_outreach_cta_discipline() -> None:
    payload = build_test_pack_case_payload(
        "OC-3",
        {
            "company_name": "NeuroFlow",
            "outreach_goal": "short exploratory call",
            "email_subject": "Exploratory conversation",
            "email_body": (
                "Hi Alex,\n\nKeystone Neuroinformatics focuses on clinical AI evaluation "
                "and behavioral health workflows. NeuroFlow's work in care engagement "
                "looks relevant to that focus.\n\nWould you be open to a short "
                "exploratory call?\n\nBest,\nAnup"
            ),
            "source_ids_used": ["source:company", "keystone_profile"],
            "approval_required": True,
            "send_enabled": False,
        },
        run_type="live SDK",
        model="gemini/gemini-2.5-flash",
        input_source="approved company context",
    )
    markdown = render_test_pack_case_report(payload)
    slack_text = render_test_pack_slack_text(payload)

    assert payload["status"] == "pass"
    assert payload["checks"]["Uses one clear CTA"] == "pass"
    assert "CTA Discipline" in markdown
    assert "Would you be open to a short exploratory call" in markdown
    assert "Keystone test-pack OC-3" in slack_text


def test_test_pack_case3_report_renders_orchestrator_boundary() -> None:
    payload = build_test_pack_case_payload(
        "OR-3",
        {
            "route": "clarification",
            "workflow": [{"step": "research candidate company"}, {"step": "draft outreach"}],
            "crm_ready_fields": {
                "company_name": "Example Health",
                "contact_title": "CEO",
                "draft_status": "pending approval",
            },
            "approval_required": True,
            "approval_rationale": "Human approval is required before CRM writes or sending.",
            "missing_information_blockers": ["Confirm recipient email address."],
            "forbidden_actions": ["send_email", "crm_write"],
            "send_enabled": False,
            "can_send_email": False,
            "sent": False,
            "crm_updated": False,
        },
        run_type="live SDK",
        model="openai/gpt-5.4-mini",
        input_source="boundary prompt",
    )
    markdown = render_test_pack_case_report(payload)
    slack_text = render_test_pack_slack_text(payload)

    assert payload["status"] == "pass"
    assert payload["checks"]["Does not save to CRM or send"] == "pass"
    assert "Boundary and Approval Test" in markdown
    assert "CRM-ready" in markdown
    assert "research candidate company" in markdown
    assert "No email body captured" not in markdown
    assert "Keystone test-pack OR-3" in slack_text
    assert "CRM draft fields" in slack_text


def test_case2_report_cli_writes_markdown_json_and_slack_artifacts(tmp_path) -> None:
    output_path = tmp_path / "gt2-output.json"
    report_dir = tmp_path / "reports"
    output_path.write_text(
        to_json(
            {
                "model": "gemini/gemini-2.5-flash",
                "output": {
                    "subject": "COI request",
                    "draft_reply": "Thanks. Please send the COI when available.",
                    "draft_created": True,
                    "approval_required": True,
                    "send_enabled": False,
                    "sent": False,
                },
                "usage": {"requests": 1, "input_tokens": 10, "output_tokens": 10},
            }
        ),
        encoding="utf-8",
    )

    status = case2_report_cli.main(
        [
            "--spec-id",
            "GT-2",
            "--output-json",
            str(output_path),
            "--report-dir",
            str(report_dir),
            "--run-type",
            "live SDK",
            "--input-source",
            "test fixture",
        ]
    )

    assert status == 0
    assert (report_dir / "gt-2.json").is_file()
    assert (report_dir / "gt-2.metadata.json").is_file()
    assert (report_dir / "gt-2.md").is_file()
    assert (report_dir / "gt-2.slack.txt").is_file()
    metadata = json.loads((report_dir / "gt-2.metadata.json").read_text(encoding="utf-8"))
    json_payload = json.loads((report_dir / "gt-2.json").read_text(encoding="utf-8"))
    markdown = (report_dir / "gt-2.md").read_text(encoding="utf-8")
    slack_text = (report_dir / "gt-2.slack.txt").read_text(encoding="utf-8")
    assert metadata["artifact_outputs"]["metadata_json"]["tier"] == "metadata.json"
    assert metadata["artifact_outputs"]["human_markdown"]["tier"] == "human markdown"
    assert metadata["artifact_outputs"]["slack_summary"]["tier"] == "slack summary"
    assert json_payload["artifact_outputs"] == metadata["artifact_outputs"]
    assert "Test Pack Result: GT-2 Draft Only" in markdown
    assert "Report Artifacts" in markdown
    assert "gt-2.metadata.json" in markdown
    assert "human markdown" in markdown
    assert "slack summary" in markdown
    assert "Keystone test-pack GT-2" in slack_text
    assert "Artifacts: metadata.json: gt-2.metadata.json" in slack_text
    assert len(slack_text) < 1600


def test_general_test_pack_report_cli_writes_case3_artifacts(tmp_path) -> None:
    output_path = tmp_path / "oc3-output.json"
    report_dir = tmp_path / "reports"
    output_path.write_text(
        to_json(
            {
                "output": {
                    "company_name": "NeuroFlow",
                    "outreach_goal": "short exploratory call",
                    "email_subject": "Exploratory conversation",
                    "email_body": (
                        "Hi Alex,\n\nKeystone Neuroinformatics focuses on clinical AI "
                        "evaluation. Would you be open to a short exploratory call?\n\n"
                        "Best,\nAnup"
                    ),
                    "approval_required": True,
                    "send_enabled": False,
                },
                "model": {
                    "provider": "gemini",
                    "name": "gemini-2.5-flash",
                    "run_mode": "live_sdk",
                },
            }
        ),
        encoding="utf-8",
    )

    status = case2_report_cli.main(
        [
            "--spec-id",
            "OC-3",
            "--output-json",
            str(output_path),
            "--report-dir",
            str(report_dir),
            "--run-type",
            "live SDK",
            "--input-source",
            "approved company context",
        ]
    )

    assert status == 0
    markdown = (report_dir / "oc-3.md").read_text(encoding="utf-8")
    slack_text = (report_dir / "oc-3.slack.txt").read_text(encoding="utf-8")
    json_payload = json.loads((report_dir / "oc-3.json").read_text(encoding="utf-8"))
    assert json_payload["model"] == "gemini/gemini-2.5-flash (live_sdk)"
    assert "Test Pack Result: OC-3 CTA Discipline" in markdown
    assert "Keystone test-pack OC-3" in slack_text


def test_orchestrator_output_review_renderer_is_human_readable() -> None:
    markdown = render_orchestrator_output_review(
        {
            "agent_name": "business_research_analyst",
            "review_mode": "deterministic",
            "overall_score": 88,
            "status": "pass",
            "approval_boundary_ok": True,
            "send_enabled": False,
            "structure": {
                "dimension": "structure",
                "score": 90,
                "status": "pass",
                "rationale": "Clear sections.",
            },
            "tone": {
                "dimension": "tone",
                "score": 88,
                "status": "pass",
                "rationale": "Professional.",
            },
            "readability": {
                "dimension": "readability",
                "score": 87,
                "status": "pass",
                "rationale": "Readable for humans.",
            },
            "relevance": {
                "dimension": "relevance",
                "score": 89,
                "status": "pass",
                "rationale": "Relevant to Keystone.",
            },
            "observed_gaps": ["None observed."],
            "recommended_next_step": "Ready for human review.",
        }
    )

    _assert_report_clean(markdown)
    assert "Orchestrator Review" in markdown
    assert "Readability" in markdown
    assert "Relevant to Keystone" in markdown


def test_reporting_helpers_redact_secrets_and_hash_sensitive_bodies() -> None:
    secret = "SHOULD_NOT_APPEAR_123456789"
    exported = safe_export_text(f"Authorization: Bearer {secret}")
    summary = sensitive_text_summary(f"Full email body with token={secret} and private context.")

    assert "[REDACTED]" in exported
    assert secret not in exported
    assert "SHOULD_NOT_APPEAR" not in exported
    assert "[omitted: full body is not included in exports]" in summary
    assert "sha256=" in summary
    assert "Full email body" not in summary
    assert secret not in summary
    assert "SHOULD_NOT_APPEAR" not in summary


def test_company_profile_report_renders_with_sources() -> None:
    profile = research_company_fixture(
        company_name="Curebase",
        fixture_json=FIXTURES / "sample_company_curebase.json",
    )

    markdown = render_company_profile_report(profile)

    _assert_report_clean(markdown)
    assert "Company name: Curebase" in markdown
    assert "Consulting fit score" in markdown
    assert "Evidence generation need" in markdown
    assert "Source-Backed Facts" in markdown
    assert "Research Data Points" in markdown
    assert "Risks" in markdown
    assert "Missing Information" in markdown
    assert "Recommended Next Action" in markdown
    assert "fixture://sample_company_curebase.json" in markdown


def test_company_comparison_report_renders_side_by_side_scores() -> None:
    markdown = render_company_comparison_report(
        {
            "comparison": True,
            "requested_output_format": "summary_evidence_concerns_next_step",
            "decision_criteria": [
                "consulting fit",
                "evidence generation need",
                "confidence of source-backed signals",
            ],
            "company_a": {
                "name": "Curebase",
                "description": "Clinical trial software platform.",
                "fit_summary": "Stronger research operations fit.",
                "consulting_fit_score": 86,
                "evidence_generation_need": 82,
                "outside_consulting_likelihood": 77,
                "confidence_score": 0.88,
            },
            "company_b": {
                "name": "NeuroFlow",
                "description": "Behavioral health platform.",
                "fit_summary": "Relevant but thinner evidence for immediate demand.",
                "consulting_fit_score": 68,
                "evidence_generation_need": 59,
                "outside_consulting_likelihood": 61,
                "confidence_score": 0.71,
            },
            "recommendation": "Prioritize Curebase first.",
            "recommendation_rationale": (
                "Higher research operations relevance with stronger source-backed signals."
            ),
            "evidence_gaps": ["NeuroFlow current validation priorities are unclear."],
            "unknowns": ["Budget owner is unknown for both companies."],
            "strict_sections": ["summary", "evidence", "concerns", "next_step"],
        }
    )

    _assert_report_clean(markdown)
    assert "Company Comparison Report" in markdown
    assert "Requested output format: summary_evidence_concerns_next_step" in markdown
    assert "Prioritize Curebase first." in markdown
    assert "| Criterion | Curebase | NeuroFlow |" in markdown


def test_opportunity_scout_report_renders_ranked_sources() -> None:
    result = scout_opportunities_fixture(max_results=2)

    markdown = render_opportunity_scout_report(result)

    _assert_report_clean(markdown)
    assert "Ranked Opportunities" in markdown
    assert "Why-now signal" in markdown
    assert "Priority score" in markdown
    assert "Score breakdown" in markdown
    assert "Business Research Analyst handoff" in markdown
    assert "Business Research Analyst handoff recommendation" in markdown
    assert "Search provider" in markdown
    assert "Facts Used" in markdown
    assert "Risks And Missing Information" in markdown
    assert "Approval required before outreach" in markdown
    assert "Recommended next step" in markdown
    assert "fixture://" in markdown


def test_opportunity_scout_report_renders_constraint_relaxation_hint() -> None:
    markdown = render_opportunity_scout_report(
        {
            "topic": "narrow scout request",
            "dry_run": False,
            "outreach_generated": False,
            "search_provider": "serper",
            "raw_search_result_count": 12,
            "deduped_candidate_count": 0,
            "filtered_candidates": [
                {
                    "company_name": "TinyMind",
                    "source_title": "TinyMind hiring unpaid on-site role",
                    "reasons": ["role requires a full-time practicing clinician"],
                }
            ],
            "review_candidates": [
                {
                    "company_name": "Neuropsychiatry Innovation Summit 2026",
                    "source_title": "Neuropsychiatry Innovation Summit 2026",
                    "reasons": ["borderline active opportunity preserved for review"],
                }
            ],
            "records": [],
            "constraint_relaxation_suggestion": (
                "Relax recency from the last 48 hours to the last 7 days."
            ),
        }
    )

    _assert_report_clean(markdown)
    assert "Constraint to relax next" in markdown
    assert "last 7 days" in markdown
    assert "Review Candidates" in markdown
    assert "Neuropsychiatry Innovation Summit" in markdown
    assert "Filtered Candidates" in markdown
    assert "TinyMind" in markdown


def test_opportunity_scout_report_renders_discovery_metadata_when_present() -> None:
    markdown = render_opportunity_scout_report(
        {
            "topic": "broader neuro discovery",
            "dry_run": True,
            "search_provider": "fixture_search",
            "search_queries": ["neuro grants", "u.s. neuro publications"],
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
                    "recommended_next_step": "Review for fit and source coverage.",
                    "sources": [
                        {
                            "title": "Program notice",
                            "url": "fixture://grant-program",
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

    _assert_report_clean(markdown)
    assert "Search lanes: grants, publications" in markdown
    assert "Search windows: last 12 months, last 30 days" in markdown
    assert "Entity kind: grant_program" in markdown
    assert "Canonical entity key: grant_program:nimh-sbir" in markdown
    assert "USA relevance: U.S. federal sponsor and funding lane." in markdown
    assert "Novelty: new_to_keystone" in markdown


def test_pipeline_report_renders_opportunity_discovery_metadata_when_present() -> None:
    markdown = render_pipeline_report(
        {
            "dry_run": True,
            "live_apis_called": False,
            "email_sent": False,
            "send_enabled": False,
            "approval_required": True,
            "approval_state": "pending",
            "triage": {
                "category": "consulting_opportunity",
                "recommended_action": "Review source-backed opportunity context.",
            },
            "opportunity_record": {
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
                "recommended_next_step": "Review for fit and source coverage.",
                "sources": [
                    {
                        "title": "Program notice",
                        "url": "fixture://grant-program",
                        "source_type": "fixture",
                        "supported_signal": "Current grant cycle is open.",
                    }
                ],
                "keystone_fit_reason": "Relevant to evidence-generation partnerships.",
                "outside_consulting_likelihood": 55,
                "handoff_to_business_research_analyst": True,
            },
        }
    )

    _assert_report_clean(markdown)
    assert "## Opportunity Record" in markdown
    assert "Entity kind: grant_program" in markdown
    assert "Canonical entity key: grant_program:nimh-sbir" in markdown
    assert "USA relevance: U.S. federal sponsor and funding lane." in markdown
    assert "Novelty: new_to_keystone" in markdown
    assert "Search lanes: grants" in markdown
    assert "Search windows: last 12 months" in markdown


def test_orchestrated_search_handoff_report_renders_specialist_summary() -> None:
    markdown = render_orchestrated_search_handoff_report(
        {
            "request_text": "https://www.neuroflow.com",
            "orchestrator_decision": {
                "route": "business_research_analyst",
                "target_agent": "Business Research Analyst",
            },
            "specialist_route": "business_research_analyst",
            "specialist_executed": True,
            "live_search": False,
            "send_enabled": False,
            "retrieval": {"mode": "fixture"},
            "audit_notes": ["Fixture route only; no live APIs."],
            "specialist_output": {
                "name": "NeuroFlow",
                "description": "Behavioral health technology company.",
                "fit_summary": "Relevant to behavioral health analytics.",
                "sources": [],
                "claims": [],
                "missing_information": ["Need fresher commercial evidence."],
            },
        }
    )

    _assert_report_clean(markdown)
    assert "# Orchestrated Search Handoff" in markdown
    assert "Route:" in markdown
    assert "# Company Profile Report" in markdown


def test_outreach_draft_report_renders_with_approval_warning() -> None:
    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_name="Dr. Example",
        contact_title="Clinical Operations Lead",
    )

    markdown = render_outreach_draft_report(draft)

    _assert_report_clean(markdown)
    assert "Contact/company" in markdown
    assert "Email Body" in markdown
    assert "LinkedIn Note" in markdown
    assert "Facts Used" in markdown
    assert "Source Attribution" in markdown
    assert "Source Links" in markdown
    assert "fixture://sample_company_curebase.json" in markdown
    assert "Approval Status" in markdown
    assert "Recommended Next Action" in markdown
    assert "Required: yes" in markdown
    assert "Send enabled: no" in markdown
    assert APPROVAL_WARNING in markdown


def test_outreach_blocked_report_renders_structured_status() -> None:
    markdown = render_outreach_blocked_report(
        {
            "status": "clarification_required",
            "reason": "Approved source-backed context is missing.",
            "clarification_request": "Provide an approved company brief or opportunity record.",
            "missing_requirements": ["approved company profile", "source-backed opportunity"],
            "approval_required": True,
            "approval_scope": "external_use",
            "send_enabled": False,
            "recommended_next_action": "Add approved context and rerun drafting.",
        }
    )

    _assert_report_clean(markdown)
    assert "Outreach Drafting Status" in markdown
    assert "clarification_required" in markdown
    assert "Missing Requirements" in markdown
    assert "Send enabled: no" in markdown
    assert APPROVAL_WARNING in markdown


def test_review_card_renders_concise_markdown_and_slack_text() -> None:
    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_name="Dr. Example",
        contact_title="Clinical Operations Lead",
    )

    card = review_card_from_outreach_draft(draft)
    markdown = render_review_card_markdown(card)
    slack_text = render_review_card_slack_text(card)

    assert isinstance(card, ReviewCard)
    assert card.object_type == "outreach_draft"
    assert card.decision_summary == "Ready for human review"
    assert card.evidence
    assert card.sources
    assert card.approval_required is True
    assert card.approval_status == "pending"
    assert card.approval_scope == "external_use"
    assert card.outbound_copy is True
    _assert_report_clean(markdown)
    _assert_report_clean(slack_text)
    assert "Review Card" in markdown
    assert "Top Evidence" in markdown
    assert "Sources" in markdown
    assert "fixture://sample_company_curebase.json" in markdown
    assert APPROVAL_WARNING in markdown
    assert "Keystone review:" in slack_text
    assert "Evidence:" in slack_text
    assert "Approval: pending / external_use / required yes" in slack_text
    assert APPROVAL_WARNING in slack_text
    assert len(slack_text.splitlines()) <= 14


def test_review_card_schema_normalizes_em_dashes() -> None:
    card = ReviewCard(
        title="Draft \u2014 Review",
        object_type="outreach_draft",
        decision_summary="Ready \u2014 human review",
        reason="Source-backed \u2014 concise",
        evidence=[{"text": "Evidence \u2014 source backed", "source_id": "fixture:test"}],
        next_action="Review \u2014 approve or revise",
        outbound_copy=True,
    )

    rendered = render_review_card_slack_text(card)

    _assert_report_clean(rendered)
    assert " - " in rendered


def test_outreach_draft_report_renders_optional_call_prep() -> None:
    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        contact_name="Dr. Example",
        contact_title="Clinical Operations Lead",
        include_call_prep=True,
    )

    markdown = render_outreach_draft_report(draft)

    _assert_report_clean(markdown)
    assert "## Call Prep" in markdown
    assert "Draft-only internal: yes" in markdown
    assert "Approval required: yes" in markdown
    assert "Discovery Questions" in markdown
    assert "Known Facts" in markdown
    assert "Suggested Next Step" in markdown


def test_outreach_draft_report_renders_follow_up_schedules_as_data_only() -> None:
    draft = compose_outreach_draft_fixture(
        company_profile=load_company_profile("sample_company_curebase"),
        opportunity_record=load_opportunity_record("sample_lead_curebase"),
        include_follow_up_schedule=True,
        follow_up_date="2026-05-01",
    )

    markdown = render_outreach_draft_report(draft)

    _assert_report_clean(markdown)
    assert "## Follow-ups" in markdown
    assert "2026-05-01" in markdown
    assert "approval required: yes" in markdown
    assert "scheduled in Gmail: no" in markdown
    assert "background job: no" in markdown


def test_report_json_option_renders_schema_payload() -> None:
    profile = research_company_fixture(
        company_name="Curebase",
        fixture_json=FIXTURES / "sample_company_curebase.json",
    )

    rendered = to_json(profile)

    assert '"name": "Curebase"' in rendered
    assert '"sources"' in rendered


def test_markdown_table_renders_copy_paste_safe_table() -> None:
    markdown = render_markdown_table(["Name", "Status"], [["Curebase", "pending"]])

    assert "| Name | Status |" in markdown
    assert "| Curebase | pending |" in markdown


def test_local_operator_dashboard_omits_sensitive_full_content(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_company(
        research_company_fixture(
            company_name="Curebase",
            fixture_json=FIXTURES / "sample_company_curebase.json",
        )
    )
    store.save_opportunity(scout_opportunities_fixture(max_results=1).records[0])
    store.save_outreach_draft(
        {
            "company_name": "Curebase",
            "contact_name": "Dr. Example",
            "email_subject": "Clinical AI workflow discussion",
            "email_body": (
                "Hello, this draft contains token=SHOULD_NOT_APPEAR_111111111 "
                "and should never be exported in full."
            ),
            "personalization_rationale": "Uses source-backed fixture context.",
            "approval_state": "pending",
        }
    )
    store.save_follow_up_schedule(
        {
            "company_name": "Curebase",
            "contact_name": "Dr. Example",
            "related_draft_id": "1",
            "proposed_date": "2026-05-01",
            "sequence_number": 1,
            "status": "recommended",
            "rationale": "Data-only follow-up recommendation.",
            "approval_required": True,
        }
    )
    store.save_outreach_tracking(
        {
            "draft_id": "1",
            "company_name": "Curebase",
            "contact_name": "Dr. Example",
            "lifecycle_status": "sent_manually",
            "outreach_sent": True,
            "sent_at": "2026-05-01T14:00:00Z",
            "reply_received": False,
            "outcome": "pending_reply",
            "next_step": "Wait for reply.",
        }
    )
    store.save_approval_item(
        ApprovalQueueItem(
            id="approval-dashboard",
            object_type="outreach_draft",
            object_id="draft-1",
            title="Draft review",
            summary="Review draft before use.",
            draft_text="Draft text with secret=SHOULD_NOT_APPEAR_222222222.",
            source_agent="outreach_composer",
        )
    )
    store.save_feedback(
        object_type="outreach_draft",
        object_id="draft-1",
        rating="okay",
        tags=["needs_more_context"],
        notes="Feedback note with api_key=SHOULD_NOT_APPEAR_333333333.",
    )
    store.save_agent_run(
        agent_name="outreach_composer",
        input_summary="draft export review",
        output={"email_body": "Agent body token=SHOULD_NOT_APPEAR_444444444."},
    )

    markdown = render_local_operator_dashboard(store)

    _assert_report_clean(markdown)
    assert "# Keystone Local Operator Dashboard" in markdown
    assert "Approval Queue" in markdown
    assert "Opportunities" in markdown
    assert "Company Profiles" in markdown
    assert "Outreach Drafts" in markdown
    assert "Follow-Up Schedules" in markdown
    assert "Outreach Tracking" in markdown
    assert "Feedback" in markdown
    assert "Audit Records" in markdown
    assert "Recent Agent Runs" in markdown
    assert "| Approval Queue | 1 |" in markdown
    assert "| Opportunities | 1 |" in markdown
    assert "| Company Profiles | 1 |" in markdown
    assert "| Outreach Drafts | 1 |" in markdown
    assert "| Outreach Tracking | 1 |" in markdown
    assert "| Recent Agent Runs | 1 |" in markdown
    assert "Full email and draft bodies are omitted" in markdown
    assert "Curebase" in markdown
    assert "SHOULD_NOT_APPEAR" not in markdown
    assert "Draft text with secret" not in markdown


def test_local_operator_dashboard_empty_database_is_readable(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))

    markdown = render_local_operator_dashboard(store)

    _assert_report_clean(markdown)
    assert "# Keystone Local Operator Dashboard" in markdown
    assert "| Approval Queue | 0 |" in markdown
    assert "| Opportunities | 0 |" in markdown
    assert "| Company Profiles | 0 |" in markdown
    assert "| Outreach Drafts | 0 |" in markdown
    assert "| Outreach Tracking | 0 |" in markdown
    assert "| Recent Agent Runs | 0 |" in markdown
    assert "| none |" in markdown


def test_operator_dashboard_decision_documents_local_read_only_scope() -> None:
    markdown = render_operator_dashboard_decision()

    _assert_report_clean(markdown)
    assert "# Keystone Operator Dashboard Decision" in markdown
    assert "sufficient for operations" in markdown
    assert "read-only by default" in markdown
    assert "approvals, opportunities, companies, drafts" in markdown
    assert "outreach tracking" in markdown
    assert "agent runs" in markdown
    assert "Read-only API" in markdown
    assert "No live writes" in markdown
    assert "No auto-send" in markdown
    assert "No PHI" in markdown


def test_feedback_cli_renders_redacted_table(tmp_path, capsys) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    store.save_feedback(
        object_type="outreach_draft",
        object_id="draft-1",
        rating="okay",
        tags=["needs_more_context"],
        notes="Feedback note with token=SHOULD_NOT_APPEAR_555555555.",
    )

    assert list_feedback_cli.main(["--database-url", database_url]) == 0
    output = capsys.readouterr().out

    assert "# Keystone Feedback Export" in output
    header = (
        "| ID | Approval ID | Agent | Stage | Object | Object ID | "
        "Rating | Tags | Notes | Created |"
    )
    assert header in output
    assert "needs_more_context" in output
    assert "SHOULD_NOT_APPEAR" not in output


def test_feedback_cli_empty_database_is_readable(tmp_path, capsys) -> None:
    database_url = _database_url(tmp_path)

    assert list_feedback_cli.main(["--database-url", database_url]) == 0
    output = capsys.readouterr().out

    assert "# Keystone Feedback Export" in output
    assert "- Records: 0" in output
    assert "No feedback records found." in output
    assert "| none |" in output
