# ruff: noqa: E501
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from keystone_agents.agents.opportunity_scout import scout_opportunities_fixture
from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.schemas.company_profile import (
    CompanyBriefFact,
    CompanyProfile,
    CompanyResearchFocusedBrief,
    SourceRecord,
)
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.schemas.opportunity import (
    OpportunityRecord,
    OpportunityScoutResult,
    OpportunityScoutSynthesis,
    OpportunityScoutSynthesisDecision,
)
from keystone_agents.schemas.outreach import OutreachDraft, OutreachLLMDraftPayload
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.workflows import (
    _fallback_company_name,
    _fallback_opportunity,
    _opportunity_type_from_profile,
    _pipeline_approval_scope,
    _select_opportunity,
    _should_draft,
    pipeline_markdown_report,
    run_keystone_pipeline,
    run_opportunity_to_outreach_loop,
    run_weekly_opportunity_workflow,
    save_pipeline_result,
    weekly_opportunity_workflow_markdown,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _run_cli(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "KEYSTONE_OPENAI_API_KEY": "",
            "OPENAI_API_KEY": "",
            "SERPER_API_KEY": "",
            "SLACK_BOT_TOKEN": "",
            "SLACK_WEBHOOK_URL": "",
            "APIFY_API_TOKEN": "",
            "BROWSERLESS_API_KEY": "",
        }
    )
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / script), *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result


def _run_cli_json(script: str, *args: str) -> dict[str, object]:
    result = _run_cli(script, *args, "--json")
    return json.loads(result.stdout)


def test_early_run_smoke_gmail_triage_cli_handles_html_quoted_attachment_and_style() -> None:
    html_payload = _run_cli_json(
        "run_gmail_triage.py",
        "--fixture",
        str(FIXTURES / "sample_email_html_only.txt"),
        "--sender-name",
        "Jordan",
        "--sender-email",
        "jordan@example.com",
        "--email-style-profile-fixture",
        "sample_email_style_profile_approved",
    )
    quoted_payload = _run_cli_json(
        "run_gmail_triage.py",
        "--fixture",
        str(FIXTURES / "sample_email_quoted_thread.txt"),
        "--sender-name",
        "Alex",
        "--sender-email",
        "alex@example.com",
    )
    attachment_payload = _run_cli_json(
        "run_gmail_triage.py",
        "--fixture",
        str(FIXTURES / "sample_email_attachment_legal.txt"),
        "--sender-name",
        "Morgan",
        "--sender-email",
        "morgan@example.org",
    )

    assert html_payload["category"] == "consulting_opportunity"
    assert html_payload["draft_created"] is True
    assert html_payload["approval_required"] is True
    assert html_payload.get("send_enabled", False) is False
    assert html_payload["style_profile_used"] is True
    assert html_payload["style_profile_id"] == "default"
    assert "<html" not in str(html_payload["normalized_body"]).lower()
    assert "blockquote" not in str(html_payload["draft_reply"]).lower()

    assert quoted_payload["category"] == "unrelated"
    assert quoted_payload["needs_reply"] is False
    assert quoted_payload["draft_created"] is False
    assert "Prior quoted thread" not in str(quoted_payload["normalized_body"])

    assert attachment_payload["needs_reply"] is True
    assert attachment_payload["approval_required"] is True
    assert attachment_payload.get("send_enabled", False) is False
    assert "legal_review" in attachment_payload["risk_flags"]
    assert "review it before responding further" in str(attachment_payload["draft_reply"])
    assert "contract terms" not in str(attachment_payload["draft_reply"])


def test_early_run_smoke_business_research_analyst_cli_curebase_sources_and_markdown() -> None:
    payload = _run_cli_json(
        "run_company_research.py",
        "--company",
        "Curebase",
        "--fixture",
        str(FIXTURES / "sample_company_curebase.json"),
    )
    markdown = _run_cli(
        "run_company_research.py",
        "--company",
        "Curebase",
        "--fixture",
        str(FIXTURES / "sample_company_curebase.json"),
        "--markdown",
    ).stdout

    assert payload["name"] == "Curebase"
    assert payload["sources"]
    assert payload["claims"]
    assert all(claim["source_id"] for claim in payload["claims"])
    assert payload["missing_evidence"]
    assert "source-backed company website evidence" in " ".join(payload["missing_evidence"])
    assert "Source-Backed Facts" in markdown
    assert "Missing Information" in markdown
    assert "fixture://sample_company_curebase.json" in markdown


def test_company_research_focused_brief_payload_exposes_clean_slack_summary() -> None:
    import scripts.run_company_research as run_company_research
    from keystone_agents.slack_action_contract import business_agent_result_display_text

    payload = {
        "output_type": "CompanyResearchFocusedBrief",
        "output": {
            "company_name": "Abridge",
            "product": "A clinical documentation AI workflow integrated into EHR review.",
            "customers": "Health systems and clinicians are the visible buyer fit.",
            "traction_signals": "The source-backed context reports enterprise deployments.",
            "why_it_matters": "This is relevant to Keystone because it sits in clinical AI workflow evaluation.",
            "facts": [
                {
                    "text": "Abridge describes a clinical conversation documentation platform.",
                    "source_ids": ["source:product"],
                    "confidence": 0.9,
                }
            ],
            "unknowns": ["Exact buyer titles remain unverified."],
            "sources": [
                {
                    "source_id": "source:product",
                    "title": "Abridge product",
                    "url": "https://www.abridge.com/product",
                    "source_type": "company_site",
                }
            ],
        },
        "model": {"provider": "openai", "name": "gpt-5.4-mini"},
        "message": "Business Agents Company Research Brief Ready",
        "retrieval_diagnostics": {"provider_summary": "searxng+exa"},
        "sdk_synthesis_seconds": 12.3,
    }

    summary = run_company_research._company_research_sdk_human_summary(payload)
    run_company_research._attach_company_research_display_text(payload, summary)

    assert summary.startswith("*Answer:*\n")
    assert "\n\n*Detailed Summary:*\n" in summary
    assert "\n\n*Useful references:*\n" in summary
    assert "https://www.abridge.com/product" in summary
    assert "Model:" not in summary
    assert "Retrieval diagnostics" not in summary
    assert "Timing:" not in summary
    assert payload["slack_display_text"] == summary
    assert payload["display_text"] == summary
    assert payload["summary"] == summary
    assert business_agent_result_display_text(payload) == summary


def test_company_research_visible_sources_exclude_unretained_name_collisions() -> None:
    import scripts.run_company_research as run_company_research

    profile = CompanyProfile(
        name="Cartwheel",
        website="https://www.cartwheel.org",
        sources=[
            SourceRecord(
                source_id="agents-web-search:1",
                title="Cartwheel Robotics",
                url="https://www.linkedin.com/company/cartwheel-robotics",
                source_type="google_search",
                supported_claims=["A consumer robotics company."],
                confidence=0.6,
            ),
            SourceRecord(
                source_id="website_extract:2",
                title="Cartwheel school mental-health care",
                url="https://www.cartwheel.org/",
                source_type="website",
                supported_claims=["Cartwheel provides school-based mental-health care."],
                confidence=0.9,
            ),
            SourceRecord(
                source_id="agents-web-search:3",
                title="Cartwheel student mental-health funding",
                url="https://www.prnewswire.com/example-cartwheel-funding",
                source_type="news",
                supported_claims=["Funding supports school-based mental-health services."],
                confidence=0.7,
            ),
        ],
    )
    evidence = run_company_research._verified_source_evidence_entry(
        profile,
        {
            "resolved_company_url": "https://www.cartwheel.org",
            "source_triage": {
                "retained_urls": [
                    "https://www.cartwheel.org/",
                    "https://www.prnewswire.com/example-cartwheel-funding",
                ]
            },
        },
    )
    payload = {
        "output_type": "CompanyResearchFocusedBrief",
        "verified_source_evidence": [evidence],
        "output": {
            "company_name": "Cartwheel",
            "answer": "Cartwheel has source-backed school mental-health context.",
            "product": "School-based mental-health services.",
            "sources": [],
        },
    }

    summary = run_company_research._company_research_sdk_human_summary(payload)

    assert "https://www.cartwheel.org/" in summary
    assert "https://www.prnewswire.com/example-cartwheel-funding" in summary
    assert "cartwheel-robotics" not in summary
    assert evidence["source_admission"] == {
        "candidate_count": 3,
        "admitted_count": 2,
        "rejected_count": 1,
        "triage_applied": True,
    }


def test_company_research_focused_brief_preserves_structured_answer_in_slack() -> None:
    import scripts.run_company_research as run_company_research
    from keystone_agents.presentation.public_result import attach_execution_public_result
    from keystone_agents.slack_action_contract import business_agent_result_display_text

    payload = {
        "output_type": "CompanyResearchFocusedBrief",
        "output": {
            "company_name": "Anchor Health",
            "answer": (
                "Anchor Health is multimodal. Direct competitor: Signal Health "
                "(https://signal.example/product). Adjacent tool: Notes Health "
                "(https://notes.example/product)."
            ),
            "product": "A multimodal behavioral-health assessment platform.",
            "traction_signals": "No deployment evidence was verified.",
            "why_it_matters": "Anchor Health may be relevant to Keystone.",
            "sources": [
                {
                    "title": "Anchor Health",
                    "url": "https://anchor.example/product",
                },
                {
                    "title": "Signal Health",
                    "url": "https://signal.example/product",
                },
                {
                    "title": "Notes Health",
                    "url": "https://notes.example/product",
                },
            ],
        },
    }

    summary = run_company_research._company_research_sdk_human_summary(payload)

    answer = summary.split("\n\n*Detailed Summary:*", 1)[0]
    assert "Direct competitor: Signal Health" in answer
    assert "Adjacent tool: Notes Health" in answer
    assert "Anchor Health may be relevant to Keystone" not in answer
    assert "Key signal:" not in answer
    assert "*Detailed Summary:*" in summary
    assert "*Useful references:*" in summary

    run_company_research._attach_company_research_display_text(payload, summary)
    attach_execution_public_result(payload)
    for field in ("human_summary", "slack_display_text", "display_text", "summary"):
        assert payload[field] == summary
    assert payload["public_result"]["text"] == summary
    assert business_agent_result_display_text(payload) == summary


def test_company_research_places_repeated_unknowns_only_in_limitations() -> None:
    import scripts.run_company_research as run_company_research

    payload = {
        "output_type": "CompanyResearchFocusedBrief",
        "output": {
            "company_name": "Cartwheel",
            "answer": (
                "Cartwheel appears to be a school-focused mental-health provider with "
                "public evidence for both an outcome signal and district-facing service "
                "delivery, but I could not verify a public district or payer partnership "
                "from the supplied sources. Those are useful signals, but they are company "
                "claims rather than independently verified third-party outcomes. I did not "
                "find a clearly verifiable public district partnership or payer partnership "
                "in the supplied material, so I would treat that part as unconfirmed."
            ),
            "product": "School-focused mental-health services.",
            "unknowns": [
                "A public district partnership remains unverified.",
                "A public payer partnership remains unverified.",
                "Independent third-party verification of the reported outcomes remains unverified.",
            ],
            "sources": [
                {"title": "Cartwheel", "url": "https://www.cartwheel.org/"}
            ],
        },
    }

    summary = run_company_research._company_research_sdk_human_summary(payload)
    answer, details = summary.split("\n\n*Detailed Summary:*\n", 1)

    assert "district-facing service delivery." in answer
    assert "could not verify" not in answer
    assert "did not find" not in answer
    assert "independently verified" not in answer
    assert "* Limitations / what remains unverified:" in details
    assert details.count("public district partnership") == 1
    assert details.count("public payer partnership") == 1
    assert details.count("Independent third-party verification") == 1


def test_company_research_moves_labeled_caveat_to_single_limitations_section() -> None:
    import scripts.run_company_research as run_company_research

    payload = {
        "output_type": "CompanyResearchFocusedBrief",
        "output": {
            "company_name": "Example Health",
            "answer": (
                "Example Health supports behavioral-health documentation workflows. "
                "Caveats: the outcomes signal is preliminary because the study was small."
            ),
            "product": "Behavioral-health documentation support.",
            "unknowns": ["Payer adoption remains unverified."],
            "sources": [{"title": "Example", "url": "https://example.com/evidence"}],
        },
    }

    summary = run_company_research._company_research_sdk_human_summary(payload)
    answer, details = summary.split("\n\n*Detailed Summary:*\n", 1)

    assert "Caveats:" not in answer
    assert "outcomes signal is preliminary" not in answer
    assert details.count("outcomes signal is preliminary") == 1
    assert details.count("Payer adoption remains unverified") == 1


def test_company_research_bullet_contract_preserves_answer_and_exact_official_sources() -> None:
    import scripts.run_company_research as run_company_research

    payload = {
        "output_type": "CompanyResearchFocusedBrief",
        "manual_request_plan": {
            "ask_shape": {
                "output_form": "bullets",
                "source_type_preference": ["official"],
                "output_constraints": {
                    "interpretation": "four bullets and exactly two official URLs",
                    "scope": "entire_response",
                    "item_count_mode": "exact",
                    "minimum_items": 4,
                    "maximum_items": 4,
                    "source_url_count_mode": "exact",
                    "source_url_count": 2,
                    "include_source_urls": True,
                },
            }
        },
        "output": {
            "company_name": "Callyope",
            "answer": (
                "- What it does: supports mental-health assessment "
                "(https://elion.health/products/callyope).\n"
                "- Modalities: voice and language signals.\n"
                "- Verified: the official product pages describe those inputs.\n"
                "- Uncertain: independent clinical validation remains unclear."
            ),
            "product": "This generic field must not replace the answer.",
            "facts": [
                {
                    "text": "Internal source identifiers must stay hidden.",
                    "source_ids": ["searxng:13"],
                }
            ],
            "sources": [
                {
                    "title": "Callyope FAQ",
                    "url": "https://www.callyope.com/faq",
                    "source_type": "company_site",
                },
                {
                    "title": "Callyope technology",
                    "url": "https://www.callyope.com/technology",
                    "source_type": "company_site",
                },
                {
                    "title": "Directory listing",
                    "url": "https://elion.health/products/callyope",
                    "source_type": "company_site",
                },
            ],
        },
    }

    summary = run_company_research._company_research_sdk_human_summary(payload)
    run_company_research._attach_company_research_display_text(payload, summary)
    run_company_research._attach_company_research_output_constraint_validation(payload)

    assert summary.count("\n- ") + int(summary.startswith("- ")) == 4
    assert "https://www.callyope.com/faq" in summary
    assert "https://www.callyope.com/technology" in summary
    assert "elion.health" not in summary
    assert "()." not in summary
    assert "searxng:13" not in summary
    assert "- *What it does:*" in summary
    assert "- *Modalities:*" in summary
    assert "*Detailed Summary:*" not in summary
    assert payload["output_constraint_validation"]["passed"] is True
    assert payload["output_constraint_validation"]["source_url_count"] == 2


def test_company_research_uses_resolved_official_domain_for_short_brand() -> None:
    import scripts.run_company_research as run_company_research

    payload = {
        "output_type": "CompanyResearchFocusedBrief",
        "manual_request_plan": {
            "ask_shape": {
                "output_form": "bullets",
                "source_type_preference": ["official"],
                "output_constraints": {
                    "item_count_mode": "exact",
                    "minimum_items": 2,
                    "maximum_items": 2,
                    "source_url_count_mode": "exact",
                    "source_url_count": 1,
                    "include_source_urls": True,
                },
            }
        },
        "retrieval": {"resolved_company_url": "https://hyro.ai"},
        "output": {
            "company_name": "Hyro",
            "answer": "- Product: healthcare assistant.\n- Unknown: validation evidence.",
            "sources": [
                {
                    "title": "Hyro healthcare",
                    "url": "https://www.hyro.ai/healthcare/",
                    "source_type": "company_site",
                }
            ],
        },
    }

    summary = run_company_research._company_research_sdk_human_summary(payload)

    assert "https://www.hyro.ai/healthcare/" in summary
    assert "only 0 of 1" not in summary


def test_company_research_focused_brief_honors_requested_summary_word_limit() -> None:
    import scripts.run_company_research as run_company_research

    payload = {
        "output_type": "CompanyResearchFocusedBrief",
        "manual_request_plan": {
            "ask_shape": {
                "stop_condition": "stop_after_20_word_summary",
                "output_constraints": {
                    "interpretation": "exact 20-word answer",
                    "scope": "answer",
                    "word_count_mode": "exact",
                    "word_count": 20,
                },
            }
        },
        "output": {
            "company_name": "Abridge",
            "answer": (
                "Abridge provides ambient clinical documentation AI that converts "
                "clinician-patient conversations into structured notes integrated "
                "directly with health-system electronic record workflows."
            ),
            "product": (
                "Abridge provides ambient clinical documentation software that converts "
                "patient-clinician conversations into structured notes integrated with "
                "health-system workflows and electronic health records."
            ),
            "why_it_matters": "The workflow is relevant to clinical AI evaluation.",
            "sources": [
                {
                    "title": "Abridge",
                    "url": "https://www.abridge.com",
                    "source_type": "company_site",
                }
            ],
        },
    }

    summary = run_company_research._company_research_sdk_human_summary(payload)
    run_company_research._attach_company_research_display_text(payload, summary)
    run_company_research._attach_company_research_output_constraint_validation(payload)

    answer = summary.split("\n\n", 1)[0].removeprefix("*Answer:*\n")
    assert len(answer.split()) == 20
    assert "https://www.abridge.com" in summary
    assert "*Detailed Summary:*" not in summary
    assert payload["output_constraint_validation"] == {
        "applicable": True,
        "passed": True,
        "scope": "answer",
        "word_count": 20,
        "sentence_count": None,
        "item_count": None,
        "satisfied_constraints": ["word count exact 20"],
        "violations": [],
    }


def test_company_research_style_constraint_keeps_full_research_layout() -> None:
    import scripts.run_company_research as run_company_research

    payload = {
        "output_type": "CompanyResearchFocusedBrief",
        "manual_request_plan": {
            "ask_shape": {
                "output_constraints": {
                    "interpretation": "avoid em dashes",
                    "scope": "entire_response",
                    "forbid_em_dash": True,
                }
            }
        },
        "output": {
            "company_name": "Abridge",
            "product": "Ambient clinical documentation software.",
            "why_it_matters": "Relevant to clinical AI evaluation.",
            "sources": [{"title": "Abridge", "url": "https://www.abridge.com"}],
        },
    }

    summary = run_company_research._company_research_sdk_human_summary(payload)

    assert "*Detailed Summary:*" in summary
    assert "*Useful references:*" in summary


def test_company_research_focused_brief_summary_avoids_internal_jargon_and_fragments() -> None:
    import scripts.run_company_research as run_company_research

    payload = {
        "output_type": "CompanyResearchFocusedBrief",
        "output": {
            "company_name": "Example Health",
            "product": "A clinical AI workflow.",
            "traction_signals": (
                "The approved context says the platform is embedded in clinical workflows. "
                "A separate source describes additional deployment signals that would exceed "
                "the compact answer limit when repeated in full."
            ),
            "why_it_matters": (
                "Example Health is relevant to Keystone's clinical AI evaluation work. "
                "The approved context says the platform may also support broader advisory "
                "work across several long and highly detailed implementation scenarios that "
                "should not be cut into an unfinished sentence in the Slack answer."
            ),
            "facts": [],
            "unknowns": [],
            "sources": [],
        },
    }

    summary = run_company_research._company_research_sdk_human_summary(payload)

    answer = summary.split("\n\n*Detailed Summary:*", 1)[0]
    assert "approved context" not in summary.lower()
    assert "Source evidence indicates" in summary
    assert "..." not in answer
    assert "Example Health is relevant to Keystone's clinical AI evaluation work." in answer


def test_early_run_smoke_opportunity_scout_cli_high_confidence_and_weak_stale() -> None:
    high_confidence = _run_cli_json(
        "run_opportunity_scout.py",
        "--fixture",
        str(FIXTURES / "opportunity_scout_high_confidence_sources.json"),
        "--max-results",
        "1",
    )
    weak_stale = _run_cli_json(
        "run_opportunity_scout.py",
        "--fixture",
        str(FIXTURES / "opportunity_scout_weak_stale_sources.json"),
        "--max-results",
        "1",
    )

    high_record = high_confidence["records"][0]
    high_categories = {bundle["source_category"] for bundle in high_record["source_bundles"]}
    assert high_confidence["outreach_generated"] is False
    assert high_record["company_name"] == "AffectAI Research"
    assert high_record["priority_score"] >= 80
    assert high_record["outreach_draft"] is None
    assert high_record["approval_required_before_outreach"] is True
    assert {"clinical_trial", "company_page", "job_posting"} <= high_categories

    weak_record = weak_stale["records"][0]
    assert weak_stale["outreach_generated"] is False
    assert weak_record["company_name"] == "Low Proof AI"
    assert weak_record["priority_score"] < 70
    assert weak_record["stale_signal_count"] == 1
    assert weak_record["outreach_draft"] is None
    assert "Refresh stale opportunity signals before outreach." in weak_record["missing_evidence"]


def test_early_run_smoke_outreach_composer_cli_approved_context_style_draft_only() -> None:
    payload = _run_cli_json(
        "run_outreach_draft.py",
        "--fixture",
        "sample_company_curebase",
        "--opportunity-fixture",
        "sample_lead_curebase",
        "--contact-fixture",
        "sample_contact_curebase_approved",
        "--crm-context-fixture",
        "sample_crm_context_curebase",
        "--email-style-profile-fixture",
        "sample_email_style_profile_approved",
    )
    draft = payload["draft"]

    assert payload["draft_created"] is True
    assert payload["send_enabled"] is False
    assert draft["company_name"] == "Curebase"
    assert draft["approved_context_used"] is True
    assert draft["approval_required"] is True
    assert draft["approval_scope"] == "external_use"
    assert draft["approval_state"] == "pending"
    assert draft["send_enabled"] is False
    assert draft["sent"] is False
    assert draft["can_send_email"] is False
    assert draft["style_profile_used"] is True
    assert draft["style_profile_id"] == "default"
    assert draft["unsupported_claims_flagged"] == []
    assert "Curebase" in draft["email_body"]
    assert "Dr. Priya Shah" in draft["email_body"]
    assert "major hospital contract" not in draft["email_body"]
    assert {"fixture:contact_curebase_priya", "fixture:crm_context_curebase"} <= set(
        draft["source_ids_used"]
    )


def test_early_run_smoke_cli_save_uses_sqlite_safely(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'early-run.db'}"

    gmail_payload = _run_cli_json(
        "run_gmail_triage.py",
        "--fixture",
        str(FIXTURES / "sample_email_html_only.txt"),
        "--sender-name",
        "Jordan",
        "--sender-email",
        "jordan@example.com",
        "--save",
        "--database-url",
        database_url,
    )
    company_payload = _run_cli_json(
        "run_company_research.py",
        "--company",
        "Curebase",
        "--fixture",
        str(FIXTURES / "sample_company_curebase.json"),
        "--save",
        "--database-url",
        database_url,
    )
    scout_payload = _run_cli_json(
        "run_opportunity_scout.py",
        "--fixture",
        str(FIXTURES / "opportunity_scout_high_confidence_sources.json"),
        "--max-results",
        "1",
        "--save",
        "--database-url",
        database_url,
    )
    outreach_payload = _run_cli_json(
        "run_outreach_draft.py",
        "--fixture",
        "sample_company_curebase",
        "--opportunity-fixture",
        "sample_lead_curebase",
        "--contact-fixture",
        "sample_contact_curebase_approved",
        "--crm-context-fixture",
        "sample_crm_context_curebase",
        "--email-style-profile-fixture",
        "sample_email_style_profile_approved",
        "--save",
        "--database-url",
        database_url,
    )
    store = SQLiteStore(database_url)

    assert gmail_payload["storage"]["email"]["status"] == "saved"
    assert company_payload["storage"]["company"]["status"] == "saved"
    assert scout_payload["storage"]["opportunities"]["status"] == "saved"
    assert outreach_payload["storage"]["outreach_draft"]["status"] == "saved"
    assert outreach_payload["draft"]["send_enabled"] is False
    assert scout_payload["outreach_generated"] is False
    assert (tmp_path / "early-run.db").is_file()
    assert store.count("emails") >= 1
    assert store.count("companies") >= 1
    assert store.count("opportunities") >= 1
    assert store.count("outreach_drafts") >= 1
    assert store.count("outreach_tracking") == 0
    assert store.count("agent_runs") >= 4
    assert store.count("approvals") >= 2
    assert store.count("approval_queue") >= 2


def test_company_research_natural_no_save_overrides_bridge_added_save_flag(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "no-save-company.db"
    payload = _run_cli_json(
        "run_company_research.py",
        "--company",
        "Curebase",
        "--request-text",
        (
            "Research Curebase using the supplied fixture. Read-only; do not create "
            "outreach or save anything."
        ),
        "--fixture",
        str(FIXTURES / "sample_company_curebase.json"),
        "--save",
        "--database-url",
        f"sqlite:///{database_path}",
    )

    assert "storage" not in payload
    assert payload["local_persistence_boundary"] == {
        "schema": "keystone.local_persistence_boundary.v1",
        "save_requested": True,
        "save_allowed": False,
        "local_persistence_performed": False,
        "reason": "natural_request_forbids_local_persistence",
    }
    assert database_path.exists() is False


def test_outreach_draft_cli_can_create_initial_tracking_with_save(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'outreach-tracking.db'}"
    payload = _run_cli_json(
        "run_outreach_draft.py",
        "--fixture",
        "sample_company_curebase",
        "--opportunity-fixture",
        "sample_lead_curebase",
        "--contact-fixture",
        "sample_contact_curebase_approved",
        "--crm-context-fixture",
        "sample_crm_context_curebase",
        "--email-style-profile-fixture",
        "sample_email_style_profile_approved",
        "--save",
        "--create-outreach-tracking",
        "--database-url",
        database_url,
    )
    store = SQLiteStore(database_url)
    storage_payload = payload["storage"]
    assert isinstance(storage_payload, dict)
    draft_payload = storage_payload["outreach_draft"]
    assert isinstance(draft_payload, dict)
    draft_id = draft_payload["id"]

    records = store.list_outreach_tracking(draft_id=draft_id)

    tracking_payload = storage_payload["outreach_tracking"]
    assert isinstance(tracking_payload, dict)
    assert tracking_payload["status"] == "saved"
    assert len(records) == 1
    assert records[0].lifecycle_status == "draft_pending_approval"
    assert records[0].outreach_sent is False
    assert records[0].reply_received is False
    assert records[0].outcome == "unknown"
    assert records[0].send_enabled is False
    assert records[0].sent_by_agent is False


def test_outreach_tracking_cli_flag_requires_save() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts" / "run_outreach_draft.py"),
            "--create-outreach-tracking",
            "--json",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "--create-outreach-tracking requires --save" in result.stderr


def _run_pipeline(approval_state: str = "approved_for_drafting"):
    return run_keystone_pipeline(
        email_fixture=FIXTURES / "sample_email_consulting.txt",
        company_fixture=FIXTURES / "sample_company_curebase.json",
        approval_state=approval_state,
        dry_run=True,
    )


def _triage(**overrides: object) -> EmailTriageResult:
    payload = {
        "message_id": "msg-1",
        "subject": "Consulting support",
        "sender_name": "Alex",
        "sender_email": "alex@example.com",
        "category": "consulting_opportunity",
        "confidence": 0.9,
        "priority": "high",
        "summary": "Business inquiry.",
        "reasoning": "Fixture branch test.",
        "needs_reply": True,
        "recommended_labels": ["Keystone/Triage"],
        "risk_flags": [],
        "recommended_action": "Draft a reply after approval.",
        "draft_reply": None,
        "draft_created": False,
        "approval_required": True,
        "requires_human_review": True,
    }
    payload.update(overrides)
    return EmailTriageResult.model_validate(payload)


def _profile(**overrides: object) -> CompanyProfile:
    payload = {"name": "FixtureCo", "description": "Fixture company."}
    payload.update(overrides)
    return CompanyProfile.model_validate(payload)


def _opportunity(company_name: str = "FixtureCo") -> OpportunityRecord:
    return OpportunityRecord(
        company_name=company_name,
        opportunity_type="clinical AI",
        priority_score=70,
        why_now_signal="Hiring for evidence generation.",
        recommended_next_step="Review for fit.",
        sources=[
            {
                "title": "Fixture source",
                "url": "fixture://opportunity",
                "source_type": "fixture",
                "supported_signal": "Hiring for evidence generation.",
            }
        ],
        keystone_fit_reason="Clinical AI work fits Keystone.",
        outside_consulting_likelihood=60,
        handoff_to_business_research_analyst=False,
    )


def test_full_pipeline_runs_from_fixtures() -> None:
    result = _run_pipeline()

    assert result.dry_run is True
    assert result.live_apis_called is False
    assert result.email_sent is False
    assert result.operator_feedback_requests == []


def test_pipeline_can_optionally_attach_operator_feedback_requests() -> None:
    result = run_keystone_pipeline(
        email_fixture=FIXTURES / "sample_email_consulting.txt",
        company_fixture=FIXTURES / "sample_company_curebase.json",
        approval_state="approved_for_drafting",
        dry_run=True,
        include_operator_feedback_request=True,
    )

    assert len(result.operator_feedback_requests) == 3
    outreach_request = next(
        request
        for request in result.operator_feedback_requests
        if request.object_type == "outreach_draft"
    )
    assert "weak_personalization" in outreach_request.suggested_tags
    assert outreach_request.send_enabled is False


def test_output_includes_all_structured_stages() -> None:
    result = _run_pipeline()

    assert result.triage.category == "consulting_opportunity"
    assert result.company_profile is not None
    assert result.company_profile.name == "Curebase"
    assert result.opportunity_record is not None
    assert result.opportunity_record.company_name == "Curebase"
    assert result.outreach_draft is not None
    assert result.outreach_draft.company_name == "Curebase"


def test_pipeline_preserves_approval_required() -> None:
    result = _run_pipeline()

    assert result.approval_required is True
    assert result.triage.approval_required is True
    assert result.opportunity_record is not None
    assert result.opportunity_record.approval_required_before_outreach is True
    assert result.outreach_draft is not None
    assert result.outreach_draft.approval_required is True
    assert result.approval_state == "approved_for_drafting"
    assert result.outreach_draft.approval_state == "pending"
    assert result.outreach_draft.approval_scope == "external_use"
    assert result.drafting_approval_state == "approved_for_drafting"
    assert result.external_use_approval_state == "pending"
    assert result.drafting_approved is True
    assert result.external_use_approved is False
    assert "external use" in result.approval_rationale


def test_pipeline_never_sends_email() -> None:
    result = _run_pipeline()

    assert result.email_sent is False
    assert result.send_enabled is False
    assert result.outreach_draft is not None
    assert result.outreach_draft.can_send_email is False
    assert result.outreach_draft.external_use_allowed is False


def test_pipeline_exposes_all_approval_checkpoints() -> None:
    result = _run_pipeline()

    checkpoints = {checkpoint.scope: checkpoint for checkpoint in result.approval_checkpoints}

    assert set(checkpoints) == {"research", "drafting", "external_use"}
    assert checkpoints["research"].state == "approved_for_research"
    assert checkpoints["research"].approved is True
    assert checkpoints["drafting"].state == "approved_for_drafting"
    assert checkpoints["drafting"].approved is True
    assert checkpoints["external_use"].state == "pending"
    assert checkpoints["external_use"].approved is False
    assert checkpoints["external_use"].send_enabled is False


def test_pipeline_does_not_call_live_apis() -> None:
    result = _run_pipeline()

    assert result.live_apis_called is False
    assert any("no live APIs" in note for note in result.audit_notes)


def test_pipeline_sdk_flag_constructs_agents_without_live_calls() -> None:
    result = run_keystone_pipeline(
        email_fixture=FIXTURES / "sample_email_consulting.txt",
        company_fixture=FIXTURES / "sample_company_curebase.json",
        approval_state="approved_for_drafting",
        dry_run=True,
        sdk=True,
    )

    assert result.sdk_agents_constructed is True
    assert result.live_apis_called is False


def test_pipeline_non_business_triage_skips_downstream_workflow() -> None:
    result = run_keystone_pipeline(
        email_fixture=FIXTURES / "sample_email_vendor.txt",
        approval_state="approved_for_drafting",
        dry_run=True,
    )
    report = pipeline_markdown_report(result)

    assert result.company_profile is None
    assert result.opportunity_record is None
    assert result.outreach_draft is None
    assert result.approval_required is False
    assert "Triage did not identify" in " ".join(result.audit_notes)
    assert "## Company Profile" not in report
    assert "## Opportunity Record" not in report
    assert "## Outreach Draft" not in report


def test_pipeline_save_non_business_result_skips_downstream_records(tmp_path: Path) -> None:
    result = run_keystone_pipeline(
        email_fixture=FIXTURES / "sample_email_vendor.txt",
        approval_state="approved_for_drafting",
        dry_run=True,
    )

    saved = save_pipeline_result(result, database_url=f"sqlite:///{tmp_path / 'pipeline.db'}")

    assert set(saved) == {"triage", "agent_run", "agent_run_logs"}


def test_pipeline_output_has_no_em_dashes() -> None:
    result = _run_pipeline()
    rendered = pipeline_markdown_report(result)

    assert "\u2014" not in rendered
    assert result.outreach_draft is not None
    assert "\u2014" not in result.outreach_draft.email_subject
    assert "\u2014" not in result.outreach_draft.email_body
    assert "\u2014" not in result.outreach_draft.linkedin_note


def test_pipeline_markdown_report_is_approval_gated() -> None:
    result = _run_pipeline()
    result.audit_notes.append("api_key=SHOULD_NOT_APPEAR_666666666")
    report = pipeline_markdown_report(result)

    assert "# Keystone Dry-Run Pipeline Report" in report
    assert "Approval required: true" in report
    assert "Email sent: false" in report
    assert "Send enabled: false" in report
    assert "Full inbound email body: omitted" in report
    assert "Hi Keystone" not in report
    assert "We are evaluating external consulting support" not in report
    assert "SHOULD_NOT_APPEAR" not in report


def test_pipeline_markdown_report_includes_human_review_sections() -> None:
    report = pipeline_markdown_report(_run_pipeline())

    assert "## Executive Summary" in report
    assert "## Company Profile" in report
    assert "### Facts Used" in report
    assert "### Risks" in report
    assert "### Missing Information" in report
    assert "### Source Links" in report
    assert "## Opportunity Record" in report
    assert "### Why Now" in report
    assert "### Strategic Fit" in report
    assert "### Risks And Missing Information" in report
    assert "## Outreach Draft" in report
    assert "### Email Draft" in report
    assert "### Risk Flags" in report
    assert "Recommended next action" in report


def test_weekly_opportunity_workflow_blocks_drafts_until_approval() -> None:
    result = run_weekly_opportunity_workflow(
        topic="clinical trial software",
        max_opportunities=2,
    )

    assert result.orchestrator_decision.send_enabled is False
    assert result.items
    assert result.send_enabled is False
    assert result.email_sent is False
    assert result.gmail_drafts_created is False
    assert all(item.outreach_draft is None for item in result.items)
    assert all(
        "Approve the opportunity for drafting." in item.missing_information_blockers
        for item in result.items
    )
    assert all(item.source_links for item in result.items)


def test_weekly_opportunity_workflow_can_create_approval_gated_draft_text() -> None:
    result = run_weekly_opportunity_workflow(
        topic="clinical trial software",
        max_opportunities=1,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
    )

    assert len(result.items) == 1
    item = result.items[0]
    assert item.outreach_draft is not None
    assert item.outreach_draft.email_body.endswith("Sincerely,\nAnup")
    assert item.outreach_draft.can_send_email is False
    assert item.outreach_draft.send_enabled is False
    assert item.gmail_draft_ready is False
    assert "Confirm recipient email address before Gmail draft creation." in (
        item.missing_information_blockers
    )
    assert item.orchestrator_reviews

    report = weekly_opportunity_workflow_markdown(result)

    assert "## Orchestrator" in report
    assert "#### Outreach Draft" in report
    assert item.outreach_draft.email_subject in report
    assert item.outreach_draft.email_body in report
    assert "## Feedback Question" in report


def test_weekly_opportunity_workflow_contact_override_makes_gmail_draft_ready() -> None:
    result = run_weekly_opportunity_workflow(
        topic="clinical trial software",
        max_opportunities=1,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
        contact_name="Andy Reviewer",
        contact_title="Clinical Operations Lead",
        contact_email="andy@example.com",
    )

    item = result.items[0]
    assert item.outreach_draft is not None
    assert item.contact_candidate.contact_email == "andy@example.com"
    assert item.gmail_draft_ready is True
    assert "Confirm recipient email address before Gmail draft creation." not in (
        item.missing_information_blockers
    )


def test_opportunity_to_outreach_loop_drafts_top_three_with_retrieval_telemetry() -> None:
    result = run_opportunity_to_outreach_loop(dry_run=True)

    assert result.cadence == "top_3_opportunity_to_outreach"
    assert len(result.items) == 3
    assert all(item.outreach_draft is not None for item in result.items)
    assert result.send_enabled is False
    assert result.gmail_drafts_created is False
    assert result.retrieval["provider_performance"]["providers_used"] == []
    assert result.retrieval["browser_escalation_used"] is False


def test_opportunity_to_outreach_loop_cli_outputs_json() -> None:
    payload = _run_cli_json("run_opportunity_to_outreach_loop.py", "--top-n", "1")

    assert payload["cadence"] == "top_3_opportunity_to_outreach"
    assert len(payload["items"]) == 1
    assert payload["items"][0]["outreach_draft"]["send_enabled"] is False
    assert payload["gmail_drafts_created"] is False


def test_opportunity_to_outreach_loop_cli_can_request_slack_approval_preview(
    tmp_path: Path,
) -> None:
    payload = _run_cli_json(
        "run_opportunity_to_outreach_loop.py",
        "--top-n",
        "1",
        "--save",
        "--request-approval",
        "--approval-channel",
        "#ai-agents-workflow",
        "--database-url",
        f"sqlite:///{tmp_path / 'single-loop.db'}",
    )

    posts = payload["storage"]["slack_approval_posts"]
    assert len(posts) == 1
    assert posts[0]["status"] == "dry-run"
    assert posts[0]["channel"] == "#ai-agents-workflow"
    assert posts[0]["send_enabled"] is False


def test_weekly_opportunity_workflow_uses_source_backed_org_contact_path() -> None:
    result = run_weekly_opportunity_workflow(
        topic="conference journal RFP behavioral health AI",
        max_opportunities=1,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
    )

    item = result.items[0]
    assert item.contact_candidate.contact_path_type in {
        "form",
        "conference_portal",
        "website",
        "email",
        "linkedin",
        "",
    }
    assert item.outreach_draft is not None
    assert item.outreach_draft.send_enabled is False


def test_weekly_opportunity_workflow_live_sdk_synthesis_uses_agent_outputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.workflows as workflows

    calls: list[str] = []
    synthesis_tool_counts: list[int] = []

    class FakeOutcome:
        def __init__(self, final_output):
            self.final_output = final_output

    def fake_run_retrieved_sdk_synthesis(**kwargs):
        output_type = kwargs["output_type"]
        raw = kwargs["retrieve"]()
        kwargs["normalize"](raw)
        calls.append(output_type.__name__)
        synthesis_tool_counts.append(len(kwargs["agent"].tools))
        if output_type is OpportunityScoutSynthesis:
            record = raw.records[0]
            synthesis = OpportunityScoutSynthesis(
                decisions=[
                    OpportunityScoutSynthesisDecision(
                        record_key=record.canonical_entity_key or record.company_name,
                        include=True,
                        why_now_signal=record.why_now_signal,
                        keystone_fit_reason=record.keystone_fit_reason,
                        recommended_next_step=record.recommended_next_step,
                        missing_evidence=record.missing_evidence,
                    )
                ],
                audit_summary="Weekly evidence synthesis completed.",
            )
            return FakeOutcome(kwargs["finalize_output"](raw, synthesis))
        if output_type is CompanyResearchFocusedBrief:
            return FakeOutcome(
                CompanyResearchFocusedBrief(
                    company_name=getattr(raw, "name", "Curebase"),
                    product="LLM synthesized product summary.",
                    facts=[
                        CompanyBriefFact(
                            text="LLM synthesized source-backed fact.",
                            source_ids=["fixture:weekly"],
                            confidence=0.8,
                        )
                    ],
                )
            )
        if output_type is OutreachLLMDraftPayload:
            return FakeOutcome(
                OutreachLLMDraftPayload(
                    company_name="Curebase",
                    email_subject="LLM synthesized subject",
                    email_body="Hello,\n\nLLM synthesized body.\n\nSincerely,\nAnup",
                    linkedin_note="LLM synthesized LinkedIn note.",
                    personalization_rationale="LLM synthesized rationale.",
                    source_ids_used=[],
                )
            )
        raise AssertionError(f"Unexpected output type: {output_type}")

    monkeypatch.setattr(
        workflows,
        "run_retrieved_sdk_synthesis",
        fake_run_retrieved_sdk_synthesis,
    )
    monkeypatch.setattr(
        workflows,
        "compose_outreach_draft_llm_constrained",
        lambda **_kwargs: OutreachDraft(
            company_name="Curebase",
            email_subject="LLM synthesized subject",
            email_body="Hello,\n\nLLM synthesized body.\n\nSincerely,\nAnup",
            linkedin_note="LLM synthesized LinkedIn note.",
            personalization_rationale="LLM synthesized rationale.",
            source_ids_used=[],
            drafting_mode="llm_constrained",
        ),
    )

    result = run_weekly_opportunity_workflow(
        topic="clinical trial software",
        max_opportunities=1,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
        live_sdk_synthesis=True,
    )

    item = result.items[0]
    assert result.live_sdk_synthesis is True
    assert item.company_brief is not None
    assert item.company_brief.product == "LLM synthesized product summary."
    assert item.outreach_draft is not None
    assert item.outreach_draft.drafting_mode == "llm_constrained"
    assert item.outreach_draft.email_subject == "LLM synthesized subject"
    markdown = weekly_opportunity_workflow_markdown(result)
    assert "- Live SDK synthesis: True" in markdown
    assert "#### Company Brief" in markdown
    assert "LLM synthesized product summary." in markdown
    assert result.live_sdk_synthesis is True
    assert calls == [
        "OpportunityScoutSynthesis",
        "CompanyResearchFocusedBrief",
        "OutreachLLMDraftPayload",
    ]
    assert synthesis_tool_counts == [0, 0, 0]


def test_weekly_opportunity_workflow_live_sdk_invalid_outreach_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.workflows as workflows

    class FakeOutcome:
        def __init__(self, final_output):
            self.final_output = final_output

    def fake_run_retrieved_sdk_synthesis(**kwargs):
        output_type = kwargs["output_type"]
        raw = kwargs["retrieve"]()
        kwargs["normalize"](raw)
        if output_type is OpportunityScoutSynthesis:
            record = raw.records[0]
            synthesis = OpportunityScoutSynthesis(
                decisions=[
                    OpportunityScoutSynthesisDecision(
                        record_key=record.canonical_entity_key or record.company_name,
                        include=True,
                        why_now_signal=record.why_now_signal,
                        keystone_fit_reason=record.keystone_fit_reason,
                        recommended_next_step=record.recommended_next_step,
                        missing_evidence=record.missing_evidence,
                    )
                ],
                audit_summary="Weekly evidence synthesis completed.",
            )
            return FakeOutcome(kwargs["finalize_output"](raw, synthesis))
        if output_type is CompanyResearchFocusedBrief:
            return FakeOutcome(
                CompanyResearchFocusedBrief(
                    company_name=getattr(raw, "name", "Curebase"),
                    product="LLM synthesized product summary.",
                    facts=[
                        CompanyBriefFact(
                            text="LLM synthesized source-backed fact.",
                            source_ids=["fixture:weekly"],
                            confidence=0.8,
                        )
                    ],
                )
            )
        if output_type is OutreachLLMDraftPayload:
            return FakeOutcome(
                OutreachLLMDraftPayload(
                    company_name="Curebase",
                    email_subject="Invalid synthesized subject",
                    email_body="Hello, this draft contains an em dash — so it must fall back.",
                    linkedin_note="LLM synthesized LinkedIn note.",
                    personalization_rationale="LLM synthesized rationale.",
                    source_ids_used=[],
                )
            )
        raise AssertionError(f"Unexpected output type: {output_type}")

    monkeypatch.setattr(
        workflows,
        "run_retrieved_sdk_synthesis",
        fake_run_retrieved_sdk_synthesis,
    )

    result = run_weekly_opportunity_workflow(
        topic="clinical trial software",
        max_opportunities=1,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
        live_sdk_synthesis=True,
    )

    item = result.items[0]
    assert item.outreach_draft is not None
    assert item.outreach_draft.drafting_mode == "deterministic_fixture"
    assert item.outreach_draft.send_enabled is False
    assert any("live SDK draft failed with ValueError" in note for note in item.audit_notes)


def test_weekly_opportunity_workflow_live_sdk_timeout_keeps_loop_moving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.workflows as workflows

    def fake_run_retrieved_sdk_synthesis(**kwargs):
        kwargs["normalize"](kwargs["retrieve"]())
        raise TimeoutError(f"{kwargs['output_type'].__name__} timed out")

    monkeypatch.setattr(
        workflows,
        "run_retrieved_sdk_synthesis",
        fake_run_retrieved_sdk_synthesis,
    )

    result = run_weekly_opportunity_workflow(
        topic="clinical trial software",
        max_opportunities=1,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
        live_sdk_synthesis=True,
    )

    item = result.items[0]
    assert result.live_sdk_synthesis is True
    assert item.company_brief is None
    assert item.outreach_draft is not None
    assert item.outreach_draft.drafting_mode == "deterministic_fixture"
    assert any("Opportunity Scout live SDK synthesis failed" in note for note in result.audit_notes)
    assert any("focused brief SDK synthesis failed" in note for note in item.audit_notes)
    assert any("live SDK draft failed with TimeoutError" in note for note in item.audit_notes)


def test_weekly_opportunity_workflow_keeps_retrieved_candidates_when_sdk_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.workflows as workflows

    class FakeOutcome:
        def __init__(self, final_output):
            self.final_output = final_output

    def fake_run_retrieved_sdk_synthesis(**kwargs):
        raw = kwargs["retrieve"]()
        kwargs["normalize"](raw)
        if kwargs["output_type"] is OpportunityScoutSynthesis:
            synthesis = OpportunityScoutSynthesis(
                decisions=[],
                audit_summary="No supplied record was retained.",
            )
            return FakeOutcome(kwargs["finalize_output"](raw, synthesis))
        if kwargs["output_type"] is CompanyResearchFocusedBrief:
            return FakeOutcome(
                CompanyResearchFocusedBrief(
                    company_name=getattr(raw, "name", "Curebase"),
                    product="Fallback-preserved company brief.",
                )
            )
        return FakeOutcome(raw)

    monkeypatch.setattr(
        workflows,
        "run_retrieved_sdk_synthesis",
        fake_run_retrieved_sdk_synthesis,
    )

    result = run_weekly_opportunity_workflow(
        topic="clinical trial software",
        max_opportunities=1,
        live_sdk_synthesis=True,
    )

    assert result.items
    assert result.items[0].company_name
    assert any(
        "retained source-backed Scout records" in note
        for note in result.opportunity_scout.audit_notes
    )


def test_weekly_opportunity_live_search_retrieves_more_than_final_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import keystone_agents.workflows as workflows

    captured: dict[str, int] = {}

    def fake_run_opportunity_scout_live(**kwargs):
        captured["max_results"] = kwargs["max_results"]
        return scout_opportunities_fixture(
            topic=kwargs["topic"],
            max_results=kwargs["max_results"],
        ), {}

    monkeypatch.setattr(workflows, "run_opportunity_scout_live", fake_run_opportunity_scout_live)
    monkeypatch.setattr(
        workflows,
        "retrieve_company_profile_live",
        lambda **kwargs: (CompanyProfile(name=kwargs["company"]), {}),
    )

    result = run_weekly_opportunity_workflow(
        topic="clinical trial software",
        max_opportunities=1,
        live_search=True,
        dry_run=False,
    )

    assert captured["max_results"] == 5
    assert len(result.items) == 1


def test_weekly_opportunity_workflow_save_queues_approval_review(tmp_path: Path) -> None:
    result = run_weekly_opportunity_workflow(
        topic="clinical trial software",
        max_opportunities=1,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
        save=True,
        database_url=f"sqlite:///{tmp_path / 'weekly.db'}",
    )

    assert result.storage["agent_run"]["status"] == "saved"
    assert result.storage["items"][0]["company"]["status"] == "saved"
    assert result.storage["items"][0]["opportunity"]["status"] == "saved"
    assert result.storage["items"][0]["outreach_draft"]["status"] == "saved"
    assert result.storage["items"][0]["approval_queue"]["status"] == "saved"


def test_weekly_opportunity_workflow_queues_one_selected_email_approval(
    tmp_path: Path,
) -> None:
    result = run_weekly_opportunity_workflow(
        topic="clinical trial software",
        max_opportunities=1,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
        contact_name="Andy Reviewer",
        contact_title="Clinical Operations Lead",
        contact_email="andy@example.com",
        outreach_channel="email",
        save=True,
        database_url=f"sqlite:///{tmp_path / 'weekly.db'}",
    )
    payload = result.storage["items"][0]["approval_queue_item"]

    assert payload["metadata"]["outreach_channel"] == "email"
    assert payload["metadata"]["recipient_email"] == "andy@example.com"
    assert payload["metadata"]["company_website"]
    assert payload["metadata"]["company_research_points"]
    assert "LinkedIn:" not in payload["draft_text"]
    assert payload["draft_text"].endswith("Sincerely,\nAnup")


def test_weekly_opportunity_workflow_queues_one_selected_linkedin_approval(
    tmp_path: Path,
) -> None:
    result = run_weekly_opportunity_workflow(
        topic="clinical trial software",
        max_opportunities=1,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
        contact_name="Andy Reviewer",
        contact_title="Clinical Operations Lead",
        contact_email="andy@example.com",
        contact_linkedin_url="https://www.linkedin.com/in/andy-reviewer",
        outreach_channel="linkedin",
        save=True,
        database_url=f"sqlite:///{tmp_path / 'weekly.db'}",
    )
    payload = result.storage["items"][0]["approval_queue_item"]

    assert payload["metadata"]["outreach_channel"] == "linkedin"
    assert payload["metadata"]["linkedin_url"] == "https://www.linkedin.com/in/andy-reviewer"
    assert payload["draft_text"].startswith("Hi Andy")


def test_create_gmail_draft_from_approved_weekly_approval_item(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'weekly.db'}"
    result = run_weekly_opportunity_workflow(
        topic="clinical trial software",
        max_opportunities=1,
        approval_state=ApprovalState.APPROVED_FOR_DRAFTING,
        contact_name="Andy Reviewer",
        contact_title="Clinical Operations Lead",
        contact_email="andy@example.com",
        save=True,
        database_url=database_url,
    )
    approval_id = result.storage["items"][0]["approval_queue_item"]["id"]
    SQLiteStore(database_url).update_approval_status(
        approval_id,
        "approved",
        reviewer="pytest",
        notes="Approved for Gmail draft save.",
    )

    payload = _run_cli_json(
        "create_gmail_draft_from_approval.py",
        approval_id,
        "--database-url",
        database_url,
    )

    assert payload["approval_status"] == "approved"
    assert payload["gmail_result"]["status"] == "dry-run"
    assert payload["gmail_result"]["to"] == "andy@example.com"
    assert payload["sent"] is False


def test_pipeline_only_saves_when_requested(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'pipeline.db'}"
    unsaved = run_keystone_pipeline(
        email_fixture=FIXTURES / "sample_email_consulting.txt",
        company_fixture=FIXTURES / "sample_company_curebase.json",
        approval_state="approved_for_drafting",
        dry_run=True,
        save=False,
        database_url=database_url,
    )
    saved = run_keystone_pipeline(
        email_fixture=FIXTURES / "sample_email_consulting.txt",
        company_fixture=FIXTURES / "sample_company_curebase.json",
        approval_state="approved_for_drafting",
        dry_run=True,
        save=True,
        database_url=database_url,
    )

    assert unsaved.storage == {}
    assert {
        "triage",
        "company",
        "opportunity",
        "outreach_draft",
        "approval",
        "agent_run",
        "agent_run_logs",
    } <= set(saved.storage)


def test_pipeline_save_records_tool_events_with_run_id(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'pipeline.db'}"
    result = run_keystone_pipeline(
        email_fixture=FIXTURES / "sample_email_consulting.txt",
        company_fixture=FIXTURES / "sample_company_curebase.json",
        approval_state="approved_for_drafting",
        dry_run=True,
        save=True,
        database_url=database_url,
    )
    store = SQLiteStore(database_url)

    events = store.list_tool_events(agent_name="keystone_pipeline")
    event_names = {event["tool_name"] for event in events}
    run_ids = {event["run_id"] for event in events}

    assert result.storage["agent_run"]["id"]
    assert {
        "storage_save_agent_run",
        "storage_save_email",
        "storage_save_company",
        "storage_save_opportunity",
        "storage_save_outreach_draft",
        "storage_save_approval",
    } <= event_names
    assert run_ids == {str(result.storage["agent_run"]["id"])}
    assert all(event["dry_run"] == 1 for event in events)
    assert all(event["status"] == "success" for event in events)


def test_pipeline_save_can_create_initial_outreach_tracking(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'pipeline.db'}"
    result = run_keystone_pipeline(
        email_fixture=FIXTURES / "sample_email_consulting.txt",
        company_fixture=FIXTURES / "sample_company_curebase.json",
        approval_state="approved_for_drafting",
        dry_run=True,
        save=True,
        database_url=database_url,
        create_outreach_tracking=True,
    )
    store = SQLiteStore(database_url)
    draft_id = result.storage["outreach_draft"]["id"]
    records = store.list_outreach_tracking(draft_id=draft_id)
    event_names = {event["tool_name"] for event in store.list_tool_events()}

    assert result.storage["outreach_tracking"]["status"] == "saved"
    assert len(records) == 1
    assert records[0].company_name == "Curebase"
    assert records[0].lifecycle_status == "draft_pending_approval"
    assert records[0].outreach_sent is False
    assert records[0].reply_received is False
    assert records[0].outcome == "unknown"
    assert records[0].send_enabled is False
    assert records[0].sent_by_agent is False
    assert "storage_save_initial_outreach_tracking" in event_names


def test_pipeline_cli_can_create_initial_outreach_tracking_with_save(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'pipeline-cli.db'}"
    result = _run_cli(
        "run_keystone_pipeline.py",
        "--email-fixture",
        str(FIXTURES / "sample_email_consulting.txt"),
        "--company-fixture",
        str(FIXTURES / "sample_company_curebase.json"),
        "--approval-state",
        "approved_for_drafting",
        "--save",
        "--create-outreach-tracking",
        "--database-url",
        database_url,
    )
    payload = json.loads(result.stdout)
    store = SQLiteStore(database_url)
    storage_payload = payload["storage"]
    assert isinstance(storage_payload, dict)
    draft_payload = storage_payload["outreach_draft"]
    assert isinstance(draft_payload, dict)
    draft_id = draft_payload["id"]
    records = store.list_outreach_tracking(draft_id=draft_id)

    tracking_payload = storage_payload["outreach_tracking"]
    assert isinstance(tracking_payload, dict)
    assert tracking_payload["status"] == "saved"
    assert len(records) == 1
    assert records[0].lifecycle_status == "draft_pending_approval"
    assert records[0].outreach_sent is False
    assert records[0].send_enabled is False


def test_pipeline_save_creates_reconstructable_agent_run_logs(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'pipeline.db'}"
    result = run_keystone_pipeline(
        email_fixture=FIXTURES / "sample_email_consulting.txt",
        company_fixture=FIXTURES / "sample_company_curebase.json",
        approval_state="approved_for_drafting",
        dry_run=True,
        save=True,
        database_url=database_url,
    )
    store = SQLiteStore(database_url)

    logs = store.list_agent_run_logs(run_id=result.storage["agent_run"]["id"])
    step_names = [log["step_name"] for log in logs]

    assert result.storage["agent_run_logs"]
    assert step_names == [
        "gmail_triage",
        "account_research",
        "opportunity_scout",
        "outreach_composer",
        "approval_gate",
    ]
    assert {log["run_id"] for log in logs} == {str(result.storage["agent_run"]["id"])}
    assert all(log["dry_run"] == 1 for log in logs)
    assert all(log["status"] == "success" for log in logs)
    assert all(log["input_hash"] for log in logs)
    assert all(log["output_hash"] for log in logs)


def test_pending_approval_stops_before_drafting() -> None:
    result = _run_pipeline(approval_state="pending")

    assert result.opportunity_record is not None
    assert result.outreach_draft is None
    assert result.approval_required is True
    assert result.send_enabled is False
    assert result.drafting_approved is False
    assert result.external_use_approved is False
    assert result.approval_scope == "drafting"
    assert any("does not allow drafting" in note for note in result.audit_notes)


@pytest.mark.parametrize("approval_state", ["rejected", "expired"])
def test_rejected_or_expired_approval_stops_before_drafting(approval_state: str) -> None:
    result = _run_pipeline(approval_state=approval_state)

    assert result.opportunity_record is not None
    assert result.outreach_draft is None
    assert result.approval_state == approval_state
    assert result.send_enabled is False


def test_workflow_helpers_cover_fallback_branches() -> None:
    assert _fallback_company_name(_triage(sender_email="not-an-email")) == "Inbound Opportunity"
    assert _fallback_company_name(_triage(sender_email="alex@.com")) == "Inbound Opportunity"
    assert _fallback_company_name(_triage(sender_email="alex@mental-health_ai.com")) == (
        "Mental Health Ai"
    )
    assert _opportunity_type_from_profile(_profile(behavioral_health_relevance=80)) == (
        "behavioral health AI"
    )
    assert _opportunity_type_from_profile(_profile(cns_neuro_relevance=80)) == "CNS biotech"
    assert _opportunity_type_from_profile(_profile(clinical_ai_relevance=65)) == (
        "trial technology"
    )
    assert _opportunity_type_from_profile(_profile()) == "grant or collaboration opportunity"

    fallback = _fallback_opportunity(
        _profile(
            name="NoSourceCo",
            description="",
            fit_summary="",
            consulting_fit_score=0,
            outside_consulting_likelihood=0,
        )
    )
    assert fallback.company_name == "NoSourceCo"
    assert fallback.priority_score == 50
    assert fallback.sources[0].url == "fixture://pipeline-company-profile"

    source_backed = _fallback_opportunity(
        _profile(
            name="SourcedCo",
            consulting_fit_score=100,
            sources=[
                SourceRecord(
                    source_id="fixture:sourced",
                    title="Fixture",
                    url="fixture://sourced",
                    source_type="fixture",
                    supported_claims=["SourcedCo has a relevant trial signal."],
                    confidence=0.8,
                )
            ],
        )
    )
    assert source_backed.priority_score == 100
    assert source_backed.sources[0].url == "fixture://sourced"

    selected = _select_opportunity(
        scout_result=OpportunityScoutResult(records=[_opportunity("OtherCo")]),
        company_profile=_profile(name="FixtureCo"),
    )
    assert selected.company_name == "OtherCo"
    fallback_selected = _select_opportunity(
        scout_result=OpportunityScoutResult(records=[]),
        company_profile=_profile(name="FixtureCo"),
    )
    assert fallback_selected.company_name == "FixtureCo"

    assert _should_draft(_triage(), None, ApprovalState.APPROVED_FOR_DRAFTING) is False
    assert (
        _should_draft(
            _triage(category="vendor", needs_reply=True),
            _opportunity(),
            ApprovalState.APPROVED_FOR_DRAFTING,
        )
        is False
    )
    assert (
        _should_draft(
            _triage(risk_flags=["legal_review"]),
            _opportunity(),
            ApprovalState.APPROVED_FOR_DRAFTING,
        )
        is False
    )
    assert (
        _should_draft(
            _triage(needs_reply=False, approval_required=False),
            _opportunity(),
            ApprovalState.APPROVED_FOR_DRAFTING,
        )
        is False
    )
    assert _pipeline_approval_scope(ApprovalState.APPROVED_FOR_RESEARCH) == "research"


def test_pipeline_rejects_live_mode() -> None:
    with pytest.raises(RuntimeError, match="Live Keystone pipeline execution is not implemented"):
        run_keystone_pipeline(
            email_fixture=FIXTURES / "sample_email_consulting.txt",
            company_fixture=FIXTURES / "sample_company_curebase.json",
            dry_run=False,
        )


def test_pipeline_rejects_send_approval_state() -> None:
    with pytest.raises(RuntimeError, match="sending is not implemented"):
        run_keystone_pipeline(
            email_fixture=FIXTURES / "sample_email_consulting.txt",
            company_fixture=FIXTURES / "sample_company_curebase.json",
            approval_state="approved_for_send",
            dry_run=True,
        )


def test_pipeline_rejects_external_use_approval_for_drafting_gate() -> None:
    with pytest.raises(RuntimeError, match="external-use approval"):
        run_keystone_pipeline(
            email_fixture=FIXTURES / "sample_email_consulting.txt",
            company_fixture=FIXTURES / "sample_company_curebase.json",
            approval_state="approved_for_external_use",
            dry_run=True,
        )
