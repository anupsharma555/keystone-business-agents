from __future__ import annotations

import importlib
import json
import sys
from argparse import Namespace
from pathlib import Path
from typing import Any

import pytest

from keystone_agents.guardrails import assess_tool_payload_guardrails
from keystone_agents.models import (
    BusinessResearchFocusedBriefSDKInput,
    BusinessResearchSDKInput,
    GmailPriorityGroupingSDKInput,
    GmailTriageSDKInput,
    OpportunityScoutSDKInput,
    OutreachComposerSDKInput,
    TypedAgentRunResult,
)
from keystone_agents.schemas.company_profile import CompanyProfile, CompanyResearchFocusedBrief
from keystone_agents.schemas.email_triage import EmailTriageResult, GmailPriorityGroupingResult
from keystone_agents.schemas.opportunity import OpportunityScoutResult
from keystone_agents.schemas.orchestrator import OrchestratorOutputReview
from keystone_agents.schemas.outreach import OutreachDraft
from keystone_agents.sdk import ToolGuardrailViolation
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.search_provider import SearchProviderError, SearchResult

FIXTURES = Path(__file__).resolve().parent / "fixtures"
LOCAL_RUN_CONFIG = object()
RUNTIME_MODEL_ENV_VARS = (
    "MODEL_PROVIDER",
    "KEYSTONE_OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "KEYSTONE_OPENAI_BASE_URL",
    "KEYSTONE_OPENAI_MODEL",
    "KEYSTONE_ORCHESTRATOR_MODEL",
    "KEYSTONE_ORCHESTRATOR_MODEL_PROVIDER",
    "KEYSTONE_ORCHESTRATOR_BASE_URL",
    "KEYSTONE_GMAIL_TRIAGE_MODEL",
    "KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER",
    "KEYSTONE_GMAIL_TRIAGE_BASE_URL",
    "KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL",
    "KEYSTONE_BUSINESS_RESEARCH_ANALYST_MODEL_PROVIDER",
    "KEYSTONE_BUSINESS_RESEARCH_ANALYST_BASE_URL",
    "KEYSTONE_OPPORTUNITY_SCOUT_MODEL",
    "KEYSTONE_OPPORTUNITY_SCOUT_MODEL_PROVIDER",
    "KEYSTONE_OPPORTUNITY_SCOUT_BASE_URL",
    "KEYSTONE_OUTREACH_COMPOSER_MODEL",
    "KEYSTONE_OUTREACH_COMPOSER_MODEL_PROVIDER",
    "KEYSTONE_OUTREACH_COMPOSER_BASE_URL",
    "GEMINI_API_KEY",
    "LITELLM_BASE_URL",
)


def _disable_dotenv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("keystone_agents.config.load_dotenv", lambda *_args, **_kwargs: None)


def _clear_runtime_model_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in RUNTIME_MODEL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _email_triage_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "message_id": "sdk-message-1",
        "subject": "Potential consulting project",
        "sender_name": "Example Sender",
        "sender_email": "sender@example.com",
        "category": "consulting_opportunity",
        "confidence": 0.91,
        "priority": "high",
        "summary": "Consulting inquiry relevant to Keystone.",
        "reasoning": "Fake local SDK response.",
        "needs_reply": True,
        "recommended_labels": ["Keystone/Triage"],
        "risk_flags": [],
        "recommended_action": "Create a draft for human approval.",
        "draft_reply": "Thanks for reaching out. Reference sk-12345678.",
        "draft_created": True,
        "approval_required": True,
        "requires_human_review": True,
    }
    payload.update(overrides)
    return payload


def _orchestrator_review_result(**overrides: Any) -> OrchestratorOutputReview:
    payload = {
        "agent_name": "gmail_triage",
        "output_type": "dict",
        "review_mode": "llm",
        "overall_score": 90,
        "status": "pass",
        "structure": {
            "dimension": "structure",
            "score": 90,
            "status": "pass",
            "rationale": "Human-facing structure is clear.",
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
            "rationale": "Readable for humans.",
        },
        "relevance": {
            "dimension": "relevance",
            "score": 90,
            "status": "pass",
            "rationale": "Relevant to Keystone review.",
        },
        "human_readable": True,
        "metadata_relevance_ok": True,
        "approval_boundary_ok": True,
        "send_enabled": False,
        "can_send_email": False,
        "llm_review_used": True,
        "deterministic_baseline": {"status": "pass", "overall_score": 90},
        "cost_guard": {"scope": "structure, relevance, human readability"},
        "observed_gaps": ["None observed."],
        "recommended_next_step": "Ready for human review.",
        "test_pack_checks": {
            "Structure": "pass",
            "Tone": "pass",
            "Readability": "pass",
            "Relevance": "pass",
            "Preserves no-send behavior": "pass",
        },
    }
    payload.update(overrides)
    return OrchestratorOutputReview.model_validate(payload)


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
        "confidence": 0.9,
        "priority": priority,
        "summary": f"{subject} grouped as {bucket}.",
        "reasoning": "Fake local SDK grouped the sanitized batch context.",
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
        "request_summary": "GT-1 unread Gmail priority grouping.",
        "source_label": "UNREAD",
        "lookback_days": 3,
        "source_message_count": 4,
        "urgent": [
            _priority_grouping_item(
                message_id="fixture-1-sample_email_consulting",
                bucket="urgent",
                subject="Consulting support for clinical operations workflow",
                category="consulting_opportunity",
                priority="urgent",
                needs_reply=True,
                draft_reply=(
                    "Hi Alex,\n\nThanks for reaching out. I can review the "
                    "non-sensitive project context after approval.\n\nBest,\nKeystone"
                ),
            )
        ],
        "important": [
            _priority_grouping_item(
                message_id="fixture-2-sample_email_collaboration",
                bucket="important",
                subject="Potential collaboration on behavioral health research",
                category="collaboration_opportunity",
                priority="high",
            )
        ],
        "can_wait": [
            _priority_grouping_item(
                message_id="fixture-3-sample_email_newsletter",
                bucket="can_wait",
                subject="Weekly digital health funding digest",
                category="newsletter",
                priority="low",
            )
        ],
        "ignore": [
            _priority_grouping_item(
                message_id="fixture-4-sample_email_vendor",
                bucket="ignore",
                subject="Increase your pipeline with our automated lead platform",
                category="vendor",
                priority="low",
            )
        ],
        "draft_count": 1,
        "send_enabled": False,
        "sent": False,
        "live_side_effects_enabled": False,
        "audit_notes": ["Fake local SDK response."],
    }
    payload.update(overrides)
    return payload


def _company_profile_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "name": "Curebase",
        "website": "https://www.curebase.com",
        "description": "Clinical trial software company.",
        "fit_summary": "Relevant for decentralized clinical trial workflows.",
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
                "supported_claims": ["Clinical trial software company."],
                "confidence": 0.7,
            }
        ],
        "evidence": ["Clinical trial software company."],
        "risks": [],
        "missing_information": [],
    }
    payload.update(overrides)
    return payload


def _company_focused_brief_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "company_name": "Curebase",
        "product": "Clinical trial software platform.",
        "customers": "Clinical research teams and trial sponsors.",
        "traction_signals": "Research workflow and validation signals.",
        "leadership": "Unknown from provided sources.",
        "why_it_matters": "Inference: evidence generation needs may matter to Keystone.",
        "facts": [
            {
                "text": "Curebase is a clinical trial software company.",
                "source_ids": ["fixture:curebase_company"],
                "confidence": 0.82,
            }
        ],
        "inferences": ["Potential advisory relevance is an inference."],
        "unknowns": ["Leadership was not source-backed."],
        "source_ids_used": ["fixture:curebase_company"],
        "sources": [
            {
                "source_id": "fixture:curebase_company",
                "title": "Curebase fixture company profile",
                "url": "fixture://sample_company_curebase.json",
                "source_type": "fixture",
            }
        ],
    }
    payload.update(overrides)
    return payload


def _opportunity_scout_payload(**overrides: Any) -> dict[str, Any]:
    payload = {
        "topic": "clinical AI",
        "dry_run": True,
        "records": [
            {
                "company_name": "NeuroFlow",
                "opportunity_type": "behavioral health AI",
                "priority_score": 76,
                "why_now_signal": "Payer partnership and outcomes evidence.",
                "recommended_next_step": "Hand off to Business Research Analyst.",
                "sources": [
                    {
                        "source_id": "fixture:neuroflow",
                        "title": "Fixture payer partnership announcement",
                        "url": "fixture://neuroflow-payer-partnership",
                        "source_type": "fixture",
                        "supported_signal": "Payer partnership and outcomes evidence.",
                    }
                ],
                "source_signals": ["payer partnership"],
                "keystone_fit_reason": "Signal intersects Keystone focus areas.",
                "outside_consulting_likelihood": 77,
                "handoff_to_business_research_analyst": True,
                "outreach_draft": None,
                "approval_required_before_outreach": True,
            }
        ],
        "audit_notes": ["Fake local SDK response."],
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
            "I noticed Curebase's work around decentralized trial operations. "
            "Reference sk-12345678. "
            "I would welcome a brief introductory conversation."
        ),
        "linkedin_note": "Hi Dr. Example, open to a brief exchange?",
        "personalization_rationale": "Draft references approved source-backed context.",
        "facts_used": [
            {
                "claim_text": "Company name: Curebase",
                "source_id": "fixture:curebase",
                "confidence": 0.8,
                "claim_type": "company_identity",
            }
        ],
        "unsupported_claims_flagged": [],
        "approval_required": True,
        "approval_state": "pending",
        "approval_scope": "send",
    }
    payload.update(overrides)
    return payload


def _sdk_cli_cases() -> list[dict[str, Any]]:
    email_fixture = str(FIXTURES / "sample_email_consulting.txt")
    return [
        {
            "module": "scripts.run_gmail_triage",
            "default_argv": ["run_gmail_triage.py", "--fixture", email_fixture, "--json"],
            "sdk_argv": ["run_gmail_triage.py", "--sdk", "--json"],
            "run_argv": ["run_gmail_triage.py", "--fixture", email_fixture, "--run-sdk", "--json"],
            "live_argv": [
                "run_gmail_triage.py",
                "--fixture",
                email_fixture,
                "--live-sdk",
                "--json",
            ],
            "typed_input": GmailTriageSDKInput,
            "output": lambda: EmailTriageResult.model_validate(_email_triage_payload()),
            "missing_live_key_message": "KEYSTONE_OPENAI_API_KEY is required",
        },
        {
            "module": "scripts.run_company_research",
            "default_argv": ["run_company_research.py", "--company", "Curebase", "--json"],
            "sdk_argv": [
                "run_company_research.py",
                "--company",
                "Curebase",
                "--sdk",
                "--json",
            ],
            "run_argv": [
                "run_company_research.py",
                "--company",
                "Curebase",
                "--run-sdk",
                "--json",
            ],
            "live_argv": [
                "run_company_research.py",
                "--company",
                "Curebase",
                "--live-sdk",
                "--json",
            ],
            "typed_input": BusinessResearchSDKInput,
            "output": lambda: CompanyProfile.model_validate(_company_profile_payload()),
            "missing_live_key_message": "KEYSTONE_OPENAI_API_KEY is required",
        },
        {
            "module": "scripts.run_opportunity_scout",
            "default_argv": [
                "run_opportunity_scout.py",
                "--topic",
                "clinical AI",
                "--max-results",
                "1",
                "--json",
            ],
            "sdk_argv": [
                "run_opportunity_scout.py",
                "--topic",
                "clinical AI",
                "--max-results",
                "1",
                "--sdk",
                "--json",
            ],
            "run_argv": [
                "run_opportunity_scout.py",
                "--topic",
                "clinical AI",
                "--max-results",
                "1",
                "--run-sdk",
                "--json",
            ],
            "live_argv": [
                "run_opportunity_scout.py",
                "--topic",
                "clinical AI",
                "--max-results",
                "1",
                "--live-sdk",
                "--json",
            ],
            "typed_input": OpportunityScoutSDKInput,
            "output": lambda: OpportunityScoutResult.model_validate(_opportunity_scout_payload()),
            "missing_live_key_message": "KEYSTONE_OPENAI_API_KEY is required",
        },
        {
            "module": "scripts.run_outreach_draft",
            "default_argv": ["run_outreach_draft.py", "--json"],
            "sdk_argv": ["run_outreach_draft.py", "--sdk", "--json"],
            "run_argv": ["run_outreach_draft.py", "--run-sdk", "--json"],
            "live_argv": ["run_outreach_draft.py", "--live-sdk", "--json"],
            "typed_input": OutreachComposerSDKInput,
            "output": lambda: OutreachDraft.model_validate(_outreach_draft_payload()),
            "missing_live_key_message": "KEYSTONE_OPENAI_API_KEY is required",
        },
    ]


def _import_cli(case: dict[str, Any]) -> Any:
    return importlib.import_module(str(case["module"]))


def test_company_research_dry_run_works_without_search_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_company_research as cli

    _disable_dotenv(monkeypatch)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_company_research.py", "--company", "Curebase", "--json"],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["name"] == "Curebase"


def test_company_live_search_refuses_dry_run_before_provider_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_company_research as cli

    monkeypatch.setattr(
        cli,
        "build_search_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("provider setup must not run while dry-run is true")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_company_research.py", "--company", "Curebase", "--live-search"],
    )

    with pytest.raises(SystemExit, match="--no-dry-run"):
        cli.main()


def test_company_sdk_live_search_refuses_dry_run_before_provider_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_company_research as cli

    monkeypatch.setattr(
        cli,
        "build_search_provider",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("provider setup must not run while dry-run is true")
        ),
    )
    monkeypatch.setattr(cli, "SDK_RUN_CONFIG_FACTORY", lambda: LOCAL_RUN_CONFIG)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_company_research.py",
            "--company",
            "Curebase",
            "--improvement-case",
            "br-1",
            "--run-sdk",
            "--live-search",
            "--json",
        ],
    )

    with pytest.raises(SystemExit, match="--no-dry-run"):
        cli.main()


def test_gmail_live_gmail_refuses_dry_run_before_tool_setup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_gmail_triage as cli

    monkeypatch.setattr(
        cli,
        "GmailTool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("GmailTool must not be constructed while dry-run is true")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_gmail_triage.py", "--live-gmail", "--allow-inbox"],
    )

    with pytest.raises(SystemExit, match="--no-dry-run"):
        cli.main()


def test_gmail_live_slack_refuses_dry_run_before_approval_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_gmail_triage as cli

    monkeypatch.setattr(
        cli,
        "post_approval_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Slack approval post must not run while dry-run is true")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--fixture",
            str(FIXTURES / "sample_email_consulting.txt"),
            "--request-approval",
            "--live-slack",
        ],
    )

    with pytest.raises(SystemExit, match="--no-dry-run"):
        cli.main()


def test_gmail_live_slack_requires_credentials_when_confirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_gmail_triage as cli

    _disable_dotenv(monkeypatch)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("SLACK_CHANNEL_APPROVALS", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--fixture",
            str(FIXTURES / "sample_email_consulting.txt"),
            "--request-approval",
            "--live-slack",
            "--no-dry-run",
        ],
    )

    with pytest.raises(SystemExit, match="SLACK_BOT_TOKEN.*SLACK_CHANNEL_APPROVALS"):
        cli.main()


def test_gmail_sdk_flag_constructs_agent_without_live_model_call(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_gmail_triage as cli

    _disable_dotenv(monkeypatch)
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["run_gmail_triage.py", "--sdk", "--json"])

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["mode"] == "sdk-agent"
    assert payload["live_sdk"] is False
    assert payload["agent"]["sdk_run_invoked"] is False


@pytest.mark.parametrize("case", _sdk_cli_cases(), ids=lambda case: case["module"])
def test_specialist_cli_default_path_does_not_invoke_sdk_synthesis(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _import_cli(case)

    monkeypatch.setattr(
        cli,
        "run_retrieved_sdk_synthesis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("SDK synthesis must not run on the default CLI path")
        ),
    )
    monkeypatch.setattr(sys, "argv", case["default_argv"])

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload.get("mode") != "sdk-synthesis"
    assert payload.get("sdk_run_invoked") is not True


@pytest.mark.parametrize("case", _sdk_cli_cases(), ids=lambda case: case["module"])
def test_specialist_cli_can_attach_orchestrator_review_to_default_output(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _import_cli(case)
    argv = list(case["default_argv"])
    argv.insert(max(len(argv) - 1, 1), "--orchestrator-review")

    _disable_dotenv(monkeypatch)
    monkeypatch.setattr(sys, "argv", argv)

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    review = payload["orchestrator_review"]

    assert review["reviewed_by"] == "orchestrator"
    assert review["review_mode"] == "deterministic"
    assert review["status"] in {"pass", "partial"}
    assert review["approval_boundary_ok"] is True
    assert review["send_enabled"] is False
    assert set(review["test_pack_checks"]) >= {
        "structure",
        "tone",
        "readability",
        "relevance",
        "preserves_no_send_behavior",
    }


@pytest.mark.parametrize("case", _sdk_cli_cases(), ids=lambda case: case["module"])
def test_specialist_cli_can_attach_llm_orchestrator_review_to_default_output(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import keystone_agents.cli_orchestrator_review as review_cli

    cli = _import_cli(case)
    calls: list[dict[str, Any]] = []
    argv = list(case["default_argv"])
    json_index = argv.index("--json")
    argv[json_index:json_index] = [
        "--orchestrator-review",
        "--orchestrator-review-mode",
        "llm",
    ]

    def fake_llm_review(**kwargs: Any) -> OrchestratorOutputReview:
        calls.append(kwargs)
        assert kwargs["run_config"] is LOCAL_RUN_CONFIG
        assert kwargs["live"] is False
        assert kwargs["fallback_to_deterministic"] is False
        return _orchestrator_review_result(agent_name=kwargs["agent_name"])

    _disable_dotenv(monkeypatch)
    monkeypatch.setattr(cli, "ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY", lambda: LOCAL_RUN_CONFIG)
    monkeypatch.setattr(review_cli, "review_specialist_output_llm", fake_llm_review)
    monkeypatch.setattr(sys, "argv", argv)

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    review = payload["orchestrator_review"]

    assert calls
    assert review["review_mode"] == "llm"
    assert review["llm_review_used"] is True
    assert review["human_readable"] is True
    assert review["metadata_relevance_ok"] is True
    assert review["send_enabled"] is False


@pytest.mark.parametrize("case", _sdk_cli_cases(), ids=lambda case: case["module"])
def test_specialist_cli_sdk_flag_is_non_executing(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _import_cli(case)

    monkeypatch.setattr(
        cli,
        "run_retrieved_sdk_synthesis",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("--sdk must construct only")
        ),
    )
    monkeypatch.setattr(sys, "argv", case["sdk_argv"])

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["agent"]["sdk_run_invoked"] is False


def test_live_test_env_defaults_search_and_gmail_clis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_company_research as company_cli
    import scripts.run_gmail_triage as gmail_cli
    import scripts.run_opportunity_scout as scout_cli

    _disable_dotenv(monkeypatch)
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")

    scout_args = scout_cli.build_parser().parse_args([])
    company_args = company_cli.build_parser().parse_args(["--company", "Curebase"])
    gmail_args = gmail_cli.build_parser().parse_args([])

    assert scout_args.dry_run is False
    assert scout_args.live_search is True
    assert company_args.dry_run is False
    assert company_args.live_search is True
    assert gmail_args.dry_run is False
    assert gmail_args.live_gmail is True
    assert gmail_args.allow_inbox is True


def test_serper_is_not_offered_as_live_search_fallback() -> None:
    import scripts.run_opportunity_scout as scout_cli
    import scripts.run_opportunity_to_outreach_loop as loop_cli
    import scripts.run_orchestrated_search_handoff as handoff_cli
    import scripts.run_weekly_opportunity_workflow as weekly_cli

    for parser in (
        scout_cli.build_parser(),
        handoff_cli.build_parser(),
        weekly_cli.build_parser(),
        loop_cli.build_parser(),
    ):
        action = next(
            item
            for item in parser._actions
            if item.option_strings == ["--fallback-search-provider"]
        )
        assert "serper" not in action.choices


def test_live_test_env_auto_promotes_sdk_only_specialist_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_company_research as company_cli
    import scripts.run_gmail_triage as gmail_cli
    import scripts.run_opportunity_scout as scout_cli
    import scripts.run_outreach_draft as outreach_cli

    _disable_dotenv(monkeypatch)
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_GMAIL", "true")

    scout_args = scout_cli._apply_live_test_defaults(
        Namespace(
            improvement_case="os-1",
            sdk=False,
            run_sdk=False,
            live_sdk=False,
        )
    )
    company_args = company_cli._apply_live_test_defaults(
        Namespace(
            focused_brief=True,
            improvement_case=None,
            sdk=False,
            run_sdk=False,
            live_sdk=False,
        )
    )
    gmail_args = gmail_cli._apply_live_test_defaults(
        Namespace(
            priority_grouping=True,
            sdk=False,
            run_sdk=False,
            live_sdk=False,
        )
    )
    outreach_args = outreach_cli._apply_live_test_defaults(
        Namespace(
            sdk=False,
            run_sdk=False,
            live_sdk=False,
        )
    )

    assert scout_args.live_sdk is True
    assert company_args.live_sdk is True
    assert gmail_args.live_sdk is True
    assert outreach_args.live_sdk is True


def test_live_test_env_does_not_override_explicit_sdk_construct_only_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_outreach_draft as outreach_cli

    _disable_dotenv(monkeypatch)
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")

    args = outreach_cli._apply_live_test_defaults(
        Namespace(
            sdk=True,
            run_sdk=False,
            live_sdk=False,
        )
    )

    assert args.live_sdk is False


def test_company_cli_main_loads_repo_dotenv_before_parser_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_company_research as company_cli

    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "KEYSTONE_LIVE_MODE=true",
                "KEYSTONE_DRY_RUN=false",
                "KEYSTONE_ENABLE_LIVE_RESEARCH=true",
                "SEARCH_PROVIDER=searxng",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)
    monkeypatch.delenv("KEYSTONE_LIVE_MODE", raising=False)
    monkeypatch.delenv("KEYSTONE_DRY_RUN", raising=False)
    monkeypatch.delenv("KEYSTONE_ENABLE_LIVE_RESEARCH", raising=False)
    monkeypatch.delenv("SEARCH_PROVIDER", raising=False)

    def fake_retrieve(args: Namespace) -> tuple[CompanyProfile, dict[str, Any]]:
        assert args.live_search is True
        assert args.dry_run is False
        return (
            CompanyProfile(
                name="Curebase",
                website="https://www.curebase.com",
                description="Clinical trial software platform.",
                fit_summary="Relevant for evidence-generation work.",
            ),
            {"mode": "live_search", "search_provider": "searxng"},
        )

    monkeypatch.setattr(company_cli, "_retrieve_company_profile", fake_retrieve)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_company_research.py", "--company", "Curebase", "--json"],
    )

    assert company_cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["retrieval"]["mode"] == "live_search"
    assert payload["retrieval"]["search_provider"] == "searxng"


def _patch_no_side_effects(cli: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("SDK synthesis must not invoke outbound side effects")

    for name in ("GmailTool", "post_approval_request", "build_search_provider"):
        if hasattr(cli, name):
            monkeypatch.setattr(cli, name, fail)


def _patch_local_sdk_run(
    monkeypatch: pytest.MonkeyPatch,
    *,
    output_factory: Any,
    expected_input_type: type[Any],
    calls: list[dict[str, Any]],
) -> None:
    import keystone_agents.run as run_module

    def fake_run_typed_sdk_agent(**kwargs: Any) -> TypedAgentRunResult[Any]:
        typed_input = kwargs["typed_input"]
        assert isinstance(typed_input, expected_input_type)
        assert kwargs["run_config"] is LOCAL_RUN_CONFIG
        assert kwargs["live"] is False
        calls.append(kwargs)
        return TypedAgentRunResult(
            agent_name=kwargs["agent"].name,
            output=output_factory(),
            raw_result={"local": True},
            live=False,
        )

    monkeypatch.setattr(run_module, "run_typed_sdk_agent", fake_run_typed_sdk_agent)


@pytest.mark.parametrize("case", _sdk_cli_cases(), ids=lambda case: case["module"])
def test_specialist_cli_run_sdk_uses_typed_input_and_local_run_config(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _import_cli(case)
    calls: list[dict[str, Any]] = []

    _disable_dotenv(monkeypatch)
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _patch_no_side_effects(cli, monkeypatch)
    monkeypatch.setattr(cli, "SDK_RUN_CONFIG_FACTORY", lambda: LOCAL_RUN_CONFIG)
    _patch_local_sdk_run(
        monkeypatch,
        output_factory=case["output"],
        expected_input_type=case["typed_input"],
        calls=calls,
    )
    monkeypatch.setattr(sys, "argv", case["run_argv"])

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["mode"] == "sdk-synthesis"
    assert payload["sdk_run_invoked"] is True
    assert payload["dry_run"] is True
    assert payload["live_sdk"] is False
    assert calls
    assert hasattr(calls[0]["typed_input"], "to_prompt")


def test_gmail_gt1_priority_grouping_cli_uses_llm_batch_pipeline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    import keystone_agents.cli_orchestrator_review as review_cli
    import scripts.run_gmail_triage as cli

    calls: list[dict[str, Any]] = []
    review_calls: list[dict[str, Any]] = []

    _disable_dotenv(monkeypatch)
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _patch_no_side_effects(cli, monkeypatch)
    monkeypatch.setattr(cli, "SDK_RUN_CONFIG_FACTORY", lambda: LOCAL_RUN_CONFIG)
    monkeypatch.setattr(cli, "ORCHESTRATOR_REVIEW_RUN_CONFIG_FACTORY", lambda: LOCAL_RUN_CONFIG)
    monkeypatch.setattr(
        review_cli,
        "review_specialist_output_llm",
        lambda **kwargs: (
            review_calls.append(kwargs)
            or _orchestrator_review_result(agent_name=kwargs["agent_name"])
        ),
    )
    monkeypatch.setattr(
        cli,
        "triage_gmail_message_envelope",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("GT-1 SDK path should not use deterministic message triage")
        ),
    )
    _patch_local_sdk_run(
        monkeypatch,
        output_factory=lambda: GmailPriorityGroupingResult.model_validate(
            _priority_grouping_payload()
        ),
        expected_input_type=GmailPriorityGroupingSDKInput,
        calls=calls,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_gmail_triage.py",
            "--priority-grouping",
            "--fixtures",
            str(FIXTURES / "sample_email_consulting.txt"),
            str(FIXTURES / "sample_email_collaboration.txt"),
            str(FIXTURES / "sample_email_newsletter.txt"),
            str(FIXTURES / "sample_email_vendor.txt"),
            "--request",
            "Prioritize emails for partnership follow-up and ignore vendor newsletters.",
            "--run-sdk",
            "--test-pack-report-dir",
            str(tmp_path),
            "--founder-fit-profile",
            str(FIXTURES / "founder_fit_profile_approved.json"),
            "--orchestrator-review",
            "--orchestrator-review-mode",
            "llm",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    typed_input = calls[0]["typed_input"]
    prompt = typed_input.to_prompt()

    assert payload["mode"] == "sdk-synthesis"
    assert payload["priority_grouping"] is True
    assert payload["output_type"] == "GmailPriorityGroupingResult"
    assert len(typed_input.messages) == 4
    assert "Operator request: Prioritize emails for partnership follow-up" in prompt
    assert "Review my emails from the last 3 days" in prompt
    assert "Draft replies only for urgent items" in prompt
    assert "founder_fit_test" in prompt
    assert payload["output"]["urgent"][0]["draft_reply"]
    assert payload["output"]["important"][0]["draft_reply"] is None
    assert payload["output"]["can_wait"][0]["draft_reply"] is None
    assert payload["output"]["ignore"][0]["draft_reply"] is None
    assert payload["output"]["send_enabled"] is False
    assert payload["output"]["sent"] is False
    assert payload["output"]["live_side_effects_enabled"] is False
    assert payload["test_pack_report"]["status"] == "pass"
    assert payload["orchestrator_review"]["status"] == "pass"
    assert payload["orchestrator_review"]["review_mode"] == "llm"
    assert review_calls

    markdown_path = Path(payload["test_pack_report"]["markdown_path"])
    json_path = Path(payload["test_pack_report"]["json_path"])
    markdown = markdown_path.read_text(encoding="utf-8")
    report_payload = json.loads(json_path.read_text(encoding="utf-8"))

    assert markdown_path.parent == tmp_path
    assert json_path.parent == tmp_path
    assert "# Test Pack Result: GT-1 Priority Grouping" in markdown
    assert "Status: pass" in markdown
    assert "Drafts only urgent items" in markdown
    assert "Orchestrator Review" in markdown
    assert "Draft Outputs" in markdown
    assert "Thanks for reaching out" in markdown
    assert report_payload["draft_outputs"][0]["draft_reply"]
    assert report_payload["status"] == "pass"
    assert report_payload["safety"]["send_enabled"] is False
    assert report_payload["orchestrator_review"]["status"] == "pass"
    assert report_payload["orchestrator_review"]["review_mode"] == "llm"


def test_company_research_focused_brief_requires_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_company_research as cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_company_research.py",
            "--company",
            "Curebase",
            "--focused-brief",
            "--json",
        ],
    )

    with pytest.raises(SystemExit, match="--focused-brief requires --run-sdk or --live-sdk"):
        cli.main()


def test_company_research_cr1_improvement_case_requires_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_company_research as cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_company_research.py",
            "--company",
            "Curebase",
            "--improvement-case",
            "br-1",
            "--json",
        ],
    )

    with pytest.raises(SystemExit, match="--improvement-case requires --run-sdk"):
        cli.main()


def test_company_research_focused_brief_run_sdk_uses_brief_typed_input(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_company_research as cli

    calls: list[dict[str, Any]] = []

    _disable_dotenv(monkeypatch)
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _patch_no_side_effects(cli, monkeypatch)
    monkeypatch.setattr(cli, "SDK_RUN_CONFIG_FACTORY", lambda: LOCAL_RUN_CONFIG)
    _patch_local_sdk_run(
        monkeypatch,
        output_factory=lambda: CompanyResearchFocusedBrief.model_validate(
            _company_focused_brief_payload()
        ),
        expected_input_type=BusinessResearchFocusedBriefSDKInput,
        calls=calls,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_company_research.py",
            "--company",
            "Curebase",
            "--fixture",
            str(FIXTURES / "sample_company_curebase.json"),
            "--focused-brief",
            "--founder-fit-profile",
            str(FIXTURES / "founder_fit_profile_approved.json"),
            "--run-sdk",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["mode"] == "sdk-synthesis"
    assert payload["output_type"] == "CompanyResearchFocusedBrief"
    assert payload["output"]["facts"][0]["source_ids"] == ["fixture:curebase_company"]
    assert calls
    prompt = calls[0]["typed_input"].to_prompt()
    assert "BR-1 focused brief" in prompt
    assert "fixture:curebase_company" in prompt
    assert "founder_fit_test" in prompt


def test_company_research_cr1_improvement_case_uses_pack_prompt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_company_research as cli

    calls: list[dict[str, Any]] = []

    _disable_dotenv(monkeypatch)
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _patch_no_side_effects(cli, monkeypatch)
    monkeypatch.setattr(cli, "SDK_RUN_CONFIG_FACTORY", lambda: LOCAL_RUN_CONFIG)
    _patch_local_sdk_run(
        monkeypatch,
        output_factory=lambda: CompanyResearchFocusedBrief.model_validate(
            _company_focused_brief_payload()
        ),
        expected_input_type=BusinessResearchFocusedBriefSDKInput,
        calls=calls,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_company_research.py",
            "--company",
            "Curebase",
            "--fixture",
            str(FIXTURES / "sample_company_curebase.json"),
            "--improvement-case",
            "br-1",
            "--run-sdk",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    prompt = calls[0]["typed_input"].to_prompt()

    assert payload["mode"] == "sdk-synthesis"
    assert payload["improvement_case"] == "br-1"
    assert payload["output_type"] == "CompanyResearchFocusedBrief"
    assert "Use clear sections." in payload["acceptance_criteria"]
    assert "possible partnership or advisory relevance" in prompt
    assert "Focus on product, customers, traction signals, leadership" in prompt
    assert "fixture:curebase_company" in prompt


def test_company_research_cr1_improvement_case_run_sdk_supports_live_search(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_company_research as cli

    calls: list[dict[str, Any]] = []
    seen_queries: list[tuple[str, int]] = []

    class FakeSearchProvider:
        def search_web(self, query: str, num_results: int = 5) -> list[SearchResult]:
            seen_queries.append((query, num_results))
            return [
                SearchResult(
                    title="Curebase company site",
                    link="https://www.curebase.ai/",
                    snippet=(
                        "Curebase is the AI-native eClinical platform for modern clinical research."
                    ),
                    source="serper",
                ),
                SearchResult(
                    title="Curebase LinkedIn",
                    link="https://www.linkedin.com/company/curebase",
                    snippet=(
                        "Curebase builds end-to-end software for sponsors and sites "
                        "on a unified clinical research workflow."
                    ),
                    source="serper",
                ),
            ]

    _disable_dotenv(monkeypatch)
    monkeypatch.setenv("KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", "false")
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(cli, "SDK_RUN_CONFIG_FACTORY", lambda: LOCAL_RUN_CONFIG)
    monkeypatch.setattr(
        cli,
        "build_search_provider",
        lambda *_args, **_kwargs: FakeSearchProvider(),
    )
    _patch_local_sdk_run(
        monkeypatch,
        output_factory=lambda: CompanyResearchFocusedBrief.model_validate(
            _company_focused_brief_payload(
                facts=[
                    {
                        "text": "Curebase describes itself as an AI-native eClinical platform.",
                        "source_ids": ["serper:1"],
                        "confidence": 0.82,
                    }
                ],
                source_ids_used=["serper:1", "serper:2"],
                sources=[
                    {
                        "source_id": "serper:1",
                        "title": "Curebase company site",
                        "url": "https://www.curebase.ai/",
                        "source_type": "google_search",
                    },
                    {
                        "source_id": "serper:2",
                        "title": "Curebase LinkedIn",
                        "url": "https://www.linkedin.com/company/curebase",
                        "source_type": "google_search",
                    },
                ],
            )
        ),
        expected_input_type=BusinessResearchFocusedBriefSDKInput,
        calls=calls,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_company_research.py",
            "--company",
            "Curebase",
            "--improvement-case",
            "br-1",
            "--run-sdk",
            "--live-search",
            "--search-provider",
            "serper",
            "--no-dry-run",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    prompt = calls[0]["typed_input"].to_prompt()

    assert payload["mode"] == "sdk-synthesis"
    assert payload["improvement_case"] == "br-1"
    assert payload["output_type"] == "CompanyResearchFocusedBrief"
    assert payload["retrieval"]["mode"] == "live_search"
    assert payload["retrieval"]["search_provider"] == "serper"
    assert payload["retrieval"]["raw_search_result_count"] == 8
    assert len(payload["retrieval"]["search_queries"]) == 4
    assert len(seen_queries) == 4
    assert all(num_results == 5 for _, num_results in seen_queries)
    assert "https://www.curebase.ai/" in prompt
    assert "linkedin.com/company/curebase" in prompt
    assert "serper:1" in prompt


def test_opportunity_scout_os1_improvement_case_uses_llm_synthesis_context(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_opportunity_scout as cli

    calls: list[dict[str, Any]] = []

    def output() -> OpportunityScoutResult:
        return OpportunityScoutResult.model_validate(
            {
                "topic": cli.OS1_IMPROVEMENT_PROMPT,
                "dry_run": True,
                "records": [
                    {
                        "company_name": "NeuroMeasure Health",
                        "opportunity_type": "clinical AI",
                        "role_title": "Medical Director, Clinical AI Research",
                        "role_location": "Remote, United States",
                        "role_remote": True,
                        "role_country": "United States",
                        "role_posted_at": "2026-04-20",
                        "role_active": True,
                        "role_fit_reason": (
                            "Matches physician-scientist, behavioral health, clinical "
                            "research, and AI validation experience."
                        ),
                        "priority_score": 86,
                        "why_now_signal": (
                            "Active remote U.S. role posted 2026-04-20 with behavioral "
                            "health, clinical research, and AI validation fit."
                        ),
                        "recommended_next_step": (
                            "Verify posting freshness and save only after human approval."
                        ),
                        "sources": [
                            {
                                "source_id": "fixture:os1-role",
                                "title": "Medical Director, Clinical AI Research",
                                "url": ("https://jobs.example.test/neuro-measure-medical-director"),
                                "source_type": "job_posting",
                                "supported_signal": (
                                    "Remote U.S. role posted 2026-04-20 for a physician-scientist."
                                ),
                            }
                        ],
                        "keystone_fit_reason": (
                            "Role fits Keystone clinical AI and behavioral health "
                            "research experience."
                        ),
                        "outside_consulting_likelihood": 80,
                        "handoff_to_business_research_analyst": True,
                        "outreach_draft": None,
                        "approval_required_before_outreach": True,
                    }
                ],
                "audit_notes": ["LLM synthesized OS-1 from role source context."],
                "outreach_generated": False,
            }
        )

    _disable_dotenv(monkeypatch)
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _patch_no_side_effects(cli, monkeypatch)
    monkeypatch.setattr(cli, "SDK_RUN_CONFIG_FACTORY", lambda: LOCAL_RUN_CONFIG)
    monkeypatch.setattr(
        cli,
        "scout_opportunities_fixture",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("OS-1 SDK path should not use deterministic Scout fixture")
        ),
    )
    _patch_local_sdk_run(
        monkeypatch,
        output_factory=output,
        expected_input_type=OpportunityScoutSDKInput,
        calls=calls,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_opportunity_scout.py",
            "--improvement-case",
            "os-1",
            "--fixture",
            str(FIXTURES / "opportunity_scout_os1_role_sources.json"),
            "--founder-fit-profile",
            str(FIXTURES / "founder_fit_profile_approved.json"),
            "--run-sdk",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    prompt = calls[0]["typed_input"].to_prompt()

    assert payload["mode"] == "sdk-synthesis"
    assert payload["model"] == {
        "provider": "local",
        "name": "sdk-local",
        "run_mode": "local_sdk",
    }
    assert payload["usage"]["available"] is False
    assert payload["cost"]["source"] == "usage_not_available"
    assert payload["gemini_free_tier_usage"]["source"] == "unsupported_provider"
    assert payload["provider_cost_window"]["source"] == "not_queried"
    assert payload["output"]["records"][0]["role_title"] == (
        "Medical Director, Clinical AI Research"
    )
    assert cli.OS1_IMPROVEMENT_PROMPT in prompt
    assert "candidate_role_sources" in prompt
    assert "Exclude AI tutor roles" in prompt
    assert "neuro-measure-medical-director" in prompt
    assert "founder_fit_test" in prompt


def test_opportunity_scout_os1_live_search_queries_pass_search_guardrails() -> None:
    import scripts.run_opportunity_scout as cli

    for query in cli._os1_role_search_queries():
        assessment = assess_tool_payload_guardrails(
            "serper_search",
            {"query": query, "num_results": 10, "live": True},
        )

        assert assessment.allowed, query


def test_opportunity_scout_os1_live_search_falls_back_to_searxng(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_opportunity_scout as cli

    calls: list[str | None] = []

    class FailingSerperProvider:
        def validate_configuration(self) -> None:
            return None

        def search_web(self, _query: str, num_results: int = 5) -> list[SearchResult]:
            raise SearchProviderError("Serper unavailable")

    class BackupSearxngProvider:
        def validate_configuration(self) -> None:
            return None

        def search_web(self, _query: str, num_results: int = 5) -> list[SearchResult]:
            return [
                SearchResult(
                    title="Remote Clinical AI Medical Director",
                    link="https://jobs.example.test/clinical-ai-medical-director",
                    snippet="Remote United States role posted this week.",
                    source="searxng",
                )
            ]

    def fake_build_search_provider(
        provider: str | None = None,
        *,
        live: bool = False,
    ) -> FailingSerperProvider | BackupSearxngProvider:
        assert live is True
        calls.append(provider)
        if provider == "searxng":
            return BackupSearxngProvider()
        return FailingSerperProvider()

    monkeypatch.setattr(cli, "build_search_provider", fake_build_search_provider)
    monkeypatch.setattr(cli, "_os1_role_search_queries", lambda: ["remote clinical AI"])
    args = Namespace(
        dry_run=False,
        live_search=True,
        search_provider="serper",
        fallback_search_provider="searxng",
        max_results=2,
    )

    hits, metadata = cli._search_role_sources_live(args)

    assert calls == ["serper", "searxng"]
    assert hits == [
        {
            "title": "Remote Clinical AI Medical Director",
            "url": "https://jobs.example.test/clinical-ai-medical-director",
            "snippet": "Remote United States role posted this week.",
            "source": "searxng",
            "source_type": "job_posting",
            "query": "remote clinical AI",
        }
    ]
    assert metadata["search_provider_used"] == "searxng"
    assert metadata["primary_search_provider"] == "serper"
    assert metadata["fallback_search_provider"] == "searxng"
    assert metadata["search_provider_fallback_used"] is True
    assert metadata["search_provider_errors"][0]["error_type"] == "SearchProviderError"
    assert metadata["serper_estimated_credits_used"] == 0


def test_opportunity_scout_os1_live_search_fallback_preserves_guardrails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_opportunity_scout as cli

    calls: list[str | None] = []

    class GuardrailBlockedProvider:
        def validate_configuration(self) -> None:
            return None

        def search_web(self, _query: str, num_results: int = 5) -> list[SearchResult]:
            raise ToolGuardrailViolation("blocked by safety guardrail")

    class UnexpectedFallbackProvider:
        def validate_configuration(self) -> None:
            return None

        def search_web(self, _query: str, num_results: int = 5) -> list[SearchResult]:
            raise AssertionError("fallback must not bypass guardrail failures")

    def fake_build_search_provider(
        provider: str | None = None,
        *,
        live: bool = False,
    ) -> GuardrailBlockedProvider | UnexpectedFallbackProvider:
        assert live is True
        calls.append(provider)
        if provider == "searxng":
            return UnexpectedFallbackProvider()
        return GuardrailBlockedProvider()

    monkeypatch.setattr(cli, "build_search_provider", fake_build_search_provider)
    monkeypatch.setattr(cli, "_os1_role_search_queries", lambda: ["remote clinical AI"])
    args = Namespace(
        dry_run=False,
        live_search=True,
        search_provider="serper",
        fallback_search_provider="searxng",
        max_results=2,
    )

    with pytest.raises(ToolGuardrailViolation, match="blocked by safety guardrail"):
        cli._search_role_sources_live(args)

    assert calls == ["serper"]


def test_opportunity_scout_improvement_case_requires_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_opportunity_scout as cli

    monkeypatch.setattr(
        sys,
        "argv",
        ["run_opportunity_scout.py", "--improvement-case", "os-1", "--json"],
    )

    with pytest.raises(SystemExit, match="requires --run-sdk or --live-sdk"):
        cli.main()


@pytest.mark.parametrize("case", _sdk_cli_cases(), ids=lambda case: case["module"])
def test_specialist_cli_live_sdk_requires_openai_key_before_model_execution(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _import_cli(case)

    _disable_dotenv(monkeypatch)
    _clear_runtime_model_env(monkeypatch)
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", case["live_argv"])

    with pytest.raises(SystemExit, match=case["missing_live_key_message"]):
        cli.main()


def test_live_sdk_resolution_loads_dotenv_keystone_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse

    import keystone_agents.config as config_module
    from keystone_agents.cli_sdk import resolve_sdk_execution
    from keystone_agents.model_provider import openai_api_key_from_env

    _clear_runtime_model_env(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    calls: list[bool] = []

    def fake_load_dotenv(*_args: Any, **_kwargs: Any) -> None:
        calls.append(True)
        monkeypatch.setenv("KEYSTONE_OPENAI_API_KEY", "dotenv-keystone-unit-test-key")

    monkeypatch.setattr(config_module, "load_dotenv", fake_load_dotenv)
    args = argparse.Namespace(
        run_sdk=False,
        live_sdk=True,
        trace_include_sensitive_data=False,
    )

    run_config, live = resolve_sdk_execution(args, run_config_factory=None)

    assert run_config is None
    assert live is True
    assert calls == [True]
    assert openai_api_key_from_env() == "dotenv-keystone-unit-test-key"


@pytest.mark.parametrize("case", _sdk_cli_cases(), ids=lambda case: case["module"])
def test_specialist_cli_sdk_synthesis_rejects_sensitive_trace_payloads(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli = _import_cli(case)

    monkeypatch.setattr(cli, "SDK_RUN_CONFIG_FACTORY", lambda: LOCAL_RUN_CONFIG)
    monkeypatch.setattr(
        sys,
        "argv",
        [*case["run_argv"][:-1], "--trace-include-sensitive-data", "--json"],
    )

    with pytest.raises(SystemExit, match="trace_include_sensitive_data=true"):
        cli.main()


@pytest.mark.parametrize("case", _sdk_cli_cases(), ids=lambda case: case["module"])
def test_specialist_cli_run_sdk_save_records_sanitized_agent_run_only(
    case: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli = _import_cli(case)
    calls: list[dict[str, Any]] = []
    database_url = f"sqlite:///{tmp_path / 'sdk-audit.db'}"

    _disable_dotenv(monkeypatch)
    monkeypatch.delenv("KEYSTONE_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _patch_no_side_effects(cli, monkeypatch)
    monkeypatch.setattr(cli, "SDK_RUN_CONFIG_FACTORY", lambda: LOCAL_RUN_CONFIG)
    _patch_local_sdk_run(
        monkeypatch,
        output_factory=case["output"],
        expected_input_type=case["typed_input"],
        calls=calls,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [*case["run_argv"][:-1], "--save", "--database-url", database_url, "--json"],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    store = SQLiteStore(database_url)
    rows = store.fetch_all("agent_runs")

    assert payload["storage"]["agent_run"]["table"] == "agent_runs"
    assert len(rows) == 1
    assert rows[0]["agent_name"]
    assert rows[0]["dry_run"] == 1
    assert rows[0]["model"] == "sdk-local"
    assert rows[0]["status"] == "success"
    assert '"email_body":' not in rows[0]["output_json"]
    assert '"draft_reply":' not in rows[0]["output_json"]
    assert "sk-12345678" not in json.dumps(rows, sort_keys=True)
    for table in (
        "emails",
        "companies",
        "opportunities",
        "outreach_drafts",
        "approvals",
        "approval_queue",
    ):
        assert store.count(table) == 0


def test_outreach_live_slack_refuses_dry_run_before_approval_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_outreach_draft as cli

    monkeypatch.setattr(
        cli,
        "post_approval_request",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Slack approval post must not run while dry-run is true")
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_outreach_draft.py", "--request-approval", "--live-slack"],
    )

    with pytest.raises(SystemExit, match="--no-dry-run"):
        cli.main()


def test_outreach_live_slack_can_be_confirmed_with_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_outreach_draft as cli

    class FakeApprovalRequest:
        slack_ts = "captured-ts"

        def model_dump(self) -> dict[str, Any]:
            return {"status": "captured", "live": True}

    calls: list[bool] = []

    def fake_post_approval_request(
        draft: object,
        context: dict[str, object] | None = None,
        live: bool = False,
    ) -> FakeApprovalRequest:
        calls.append(live)
        return FakeApprovalRequest()

    _disable_dotenv(monkeypatch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "fake-slack-unit-test-token")
    monkeypatch.setenv("SLACK_CHANNEL_APPROVALS", "CUNITTEST")
    monkeypatch.setattr(cli, "post_approval_request", fake_post_approval_request)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_outreach_draft.py",
            "--request-approval",
            "--live-slack",
            "--no-dry-run",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert calls == [True]
    assert payload["approval_request"] == {"status": "captured", "live": True}
    assert payload["send_enabled"] is False


def test_outreach_no_dry_run_without_live_flag_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_outreach_draft as cli

    monkeypatch.setattr(sys, "argv", ["run_outreach_draft.py", "--no-dry-run"])

    with pytest.raises(SystemExit, match="requires --live-slack"):
        cli.main()


def test_pipeline_cli_refuses_no_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.run_keystone_pipeline as cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_keystone_pipeline.py",
            "--email-fixture",
            str(FIXTURES / "sample_email_consulting.txt"),
            "--no-dry-run",
        ],
    )

    with pytest.raises(SystemExit, match="dry-run only"):
        cli.main()
