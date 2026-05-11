from __future__ import annotations

import ast
import json
import re
from pathlib import Path

from pydantic import BaseModel

from keystone_agents import run as run_module
from keystone_agents import sdk as sdk_module
from keystone_agents.agents import (
    business_research_analyst,
    gmail_triage,
    opportunity_scout,
    orchestrator,
    outreach_composer,
)
from keystone_agents.company_research import research_company_fixture
from keystone_agents.schemas.company_profile import CompanyProfile
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.schemas.opportunity import OpportunityScoutResult
from keystone_agents.schemas.orchestrator import OrchestratorResult
from keystone_agents.schemas.outreach import OutreachDraft
from keystone_agents.tools import gmail_tool, memory_tool
from keystone_agents.tools.apify_tool import fetch_linkedin_or_profile_placeholder
from keystone_agents.tools.approval_tool import create_approval_queue_item
from keystone_agents.tools.browserless_tool import extract_company_signals, fetch_company_page
from keystone_agents.tools.email_style_tool import load_email_style_profile
from keystone_agents.tools.gmail_tool import GmailTool
from keystone_agents.tools.outreach_template_tool import (
    list_outreach_templates,
    load_outreach_template,
)
from keystone_agents.tools.serper_tool import search_web
from keystone_agents.tools.storage_tool import (
    load_approved_contact_context,
    load_approved_crm_context,
    load_pending_approval_items,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
KEYSTONE_AGENTS_ROOT = PROJECT_ROOT / "src" / "keystone_agents"
KEYSTONE_PROMPTS_ROOT = KEYSTONE_AGENTS_ROOT / "prompts"
FIXTURES_ROOT = PROJECT_ROOT / "tests" / "fixtures"

CANONICAL_SPECIALISTS = (
    (
        "gmail_triage",
        gmail_triage.build_gmail_triage_agent,
        EmailTriageResult,
        (
            "keystone_profile.md",
            "safety_policy.md",
            "skills.md",
            "tools.md",
            "gmail_triage.md",
        ),
    ),
    (
        "business_research_analyst",
        business_research_analyst.build_business_research_analyst_agent,
        CompanyProfile,
        (
            "keystone_profile.md",
            "safety_policy.md",
            "skills.md",
            "tools.md",
            "business_research_analyst.md",
        ),
    ),
    (
        "opportunity_scout",
        opportunity_scout.build_opportunity_scout_agent,
        OpportunityScoutResult,
        (
            "keystone_profile.md",
            "safety_policy.md",
            "skills.md",
            "tools.md",
            "opportunity_scout.md",
        ),
    ),
    (
        "outreach_composer",
        outreach_composer.build_outreach_composer_agent,
        OutreachDraft,
        (
            "keystone_profile.md",
            "safety_policy.md",
            "skills.md",
            "tools.md",
            "outreach_composer.md",
        ),
    ),
)

CANONICAL_AGENTS = CANONICAL_SPECIALISTS + (
    (
        "orchestrator",
        orchestrator.build_orchestrator_agent,
        OrchestratorResult,
        (
            "keystone_profile.md",
            "safety_policy.md",
            "skills.md",
            "tools.md",
            "orchestrator.md",
        ),
    ),
)


def test_agents_guide_exists() -> None:
    guide = PROJECT_ROOT / "AGENTS.md"

    assert guide.exists()
    text = guide.read_text(encoding="utf-8").lower()
    assert "agents sdk project" in text
    assert "not a plain scripting project" in text


def test_agents_sdk_conformance_doc_maps_structural_boundaries() -> None:
    guide = PROJECT_ROOT / "docs" / "AGENTS_SDK_CONFORMANCE.md"

    assert guide.exists()
    text = guide.read_text(encoding="utf-8")
    for required in (
        "Agent definitions",
        "Function tools",
        "Live SDK integration bridge",
        "Structured output",
        "handoff_description",
        "not separate SDK primitives",
        "MCP",
    ):
        assert required in text


def test_prompt_files_exist() -> None:
    expected_prompts = {
        "business_research_analyst.md",
        "gmail_triage.md",
        "keystone_profile.md",
        "memory_policy.md",
        "opportunity_scout.md",
        "orchestrator.md",
        "outreach_composer.md",
        "safety_policy.md",
        "skills.md",
        "tools.md",
    }

    assert expected_prompts <= {path.name for path in KEYSTONE_PROMPTS_ROOT.glob("*.md")}


def test_each_agent_module_has_builder_function() -> None:
    for module_path in (KEYSTONE_AGENTS_ROOT / "agents").glob("*.py"):
        if module_path.name == "__init__.py":
            continue
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        builders = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and re.fullmatch(r"build_.+_agent", node.name)
        ]
        assert builders, f"{module_path.relative_to(PROJECT_ROOT)} lacks a build_*_agent function"


def test_model_defaults_are_centralized_in_model_provider() -> None:
    allowed = {KEYSTONE_AGENTS_ROOT / "model_provider.py"}

    for module_path in (PROJECT_ROOT / "src").rglob("*.py"):
        if module_path in allowed:
            continue
        text = module_path.read_text(encoding="utf-8")
        assert not re.search(r"['\"]gpt-[^'\"]+['\"]", text), (
            f"{module_path.relative_to(PROJECT_ROOT)} hard-codes a model name"
        )


def test_canonical_agent_builders_do_not_hardcode_model_defaults() -> None:
    for module_path in (KEYSTONE_AGENTS_ROOT / "agents").glob("*.py"):
        if module_path.name == "__init__.py":
            continue
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            if not re.fullmatch(r"build_.+_agent", node.name):
                continue
            defaults = [*node.args.defaults, *[item for item in node.args.kw_defaults if item]]
            for default in defaults:
                if isinstance(default, ast.Constant) and isinstance(default.value, str):
                    assert not default.value.startswith("gpt-"), (
                        f"{module_path.relative_to(PROJECT_ROOT)}::{node.name} "
                        "hard-codes a model default"
                    )


def test_agent_modules_do_not_import_live_provider_sdks_directly() -> None:
    forbidden_roots = {
        "google",
        "openai",
        "requests",
        "slack_sdk",
        "smtplib",
    }

    for module_path in (KEYSTONE_AGENTS_ROOT / "agents").glob("*.py"):
        if module_path.name == "__init__.py":
            continue
        tree = ast.parse(module_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name.split(".", maxsplit=1)[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported = {node.module.split(".", maxsplit=1)[0]}
            else:
                continue
            assert not (imported & forbidden_roots), (
                f"{module_path.relative_to(PROJECT_ROOT)} imports a live provider SDK "
                "directly instead of using an approved wrapper"
            )


def test_no_file_contains_live_send_defaults() -> None:
    forbidden = (
        re.compile(r"\bAUTO_SEND_EMAIL\s*=\s*true\b", re.IGNORECASE),
        re.compile(r"\bAUTO_SEND\s*=\s*true\b", re.IGNORECASE),
        re.compile(r"\bSEND_EMAIL_BY_DEFAULT\s*=\s*true\b", re.IGNORECASE),
        re.compile(r"\bSEND_EXTERNAL_EMAIL\s*=\s*true\b", re.IGNORECASE),
    )

    for path in PROJECT_ROOT.rglob("*"):
        if path.is_dir() or ".venv" in path.parts or "__pycache__" in path.parts:
            continue
        if path.suffix not in {
            ".md",
            ".py",
            ".toml",
            ".txt",
            ".json",
            ".yaml",
            ".yml",
            ".env",
            ".example",
        }:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in forbidden:
            assert not pattern.search(text), (
                f"live send default found in {path.relative_to(PROJECT_ROOT)}"
            )


def test_runbook_documents_safe_sdk_tracing_boundaries() -> None:
    runbook = (PROJECT_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8").lower()

    assert "trace_include_sensitive_data=false" in runbook
    for required in ("api keys", "full email bodies", "phi", "draft bodies"):
        assert required in runbook


def test_runbook_documents_outreach_templates_and_private_example_rag() -> None:
    runbook = (PROJECT_ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8").lower()

    for required in (
        "src/keystone_agents/templates/outreach",
        "repo-versioned",
        "not sqlite-first",
        "capture_gmail_thread_examples.py",
        "--approved-for-drafting",
        "--template-id",
        "--use-example-rag",
        "--max-examples 0",
        "raw gmail bodies",
        "raw headers",
        "oauth tokens",
        "full private contact details",
        "local-only",
        "outreach_examples",
    ):
        assert required in runbook


def test_no_obvious_repo_secrets_present() -> None:
    secret_patterns = (
        re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
        re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----"),
    )
    text_suffixes = {
        ".env",
        ".example",
        ".json",
        ".md",
        ".py",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    }

    for path in PROJECT_ROOT.rglob("*"):
        if path.is_dir():
            continue
        if {".git", ".pytest_cache", ".venv", "__pycache__"} & set(path.parts):
            continue
        if path.suffix not in text_suffixes:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in secret_patterns:
            assert not pattern.search(text), f"secret-like value found in {path}"


def test_outbound_copy_prompts_forbid_em_dash() -> None:
    outbound_prompt_names = {
        "gmail_triage.md",
        "outreach_composer.md",
        "safety_policy.md",
    }

    for name in outbound_prompt_names:
        text = (KEYSTONE_PROMPTS_ROOT / name).read_text(encoding="utf-8").lower()
        assert (
            "no em dash" in text
            or "no em dashes" in text
            or "do not use em dash" in text
            or "do not use em dashes" in text
        ), f"{name} must forbid em dashes in generated outbound copy"


def test_outbound_prompt_files_require_approval() -> None:
    outbound_prompt_names = {
        "gmail_triage.md",
        "opportunity_scout.md",
        "outreach_composer.md",
        "safety_policy.md",
    }

    for name in outbound_prompt_names:
        text = (KEYSTONE_PROMPTS_ROOT / name).read_text(encoding="utf-8").lower()
        assert "approval" in text, f"{name} must mention approval for outbound communication"


def test_canonical_tool_wrappers_are_kept_out_of_prompt_files() -> None:
    tool_modules = {
        "apify_tool.py",
        "approval_tool.py",
        "browserless_tool.py",
        "gmail_tool.py",
        "memory_tool.py",
        "outreach_template_tool.py",
        "serper_tool.py",
        "slack_tool.py",
        "storage_tool.py",
        "web_scrape_tool.py",
    }

    assert tool_modules <= {path.name for path in (KEYSTONE_AGENTS_ROOT / "tools").glob("*.py")}
    for prompt_path in KEYSTONE_PROMPTS_ROOT.glob("*.md"):
        text = prompt_path.read_text(encoding="utf-8")
        assert "@function_tool" not in text
        assert "def " not in text


def test_external_function_tools_have_tool_guardrails_attached() -> None:
    guarded_tools = (
        gmail_tool.get_gmail_message,
        gmail_tool.apply_gmail_labels,
        gmail_tool.create_gmail_draft_reply,
        search_web,
        fetch_company_page,
        extract_company_signals,
        fetch_linkedin_or_profile_placeholder,
        load_approved_contact_context,
        load_approved_crm_context,
        load_email_style_profile,
        list_outreach_templates,
        load_outreach_template,
        memory_tool.retrieve_memory,
        memory_tool.retrieve_outreach_examples,
        memory_tool.save_company_profile_memory,
        memory_tool.save_opportunity_memory,
        memory_tool.save_outreach_dedup_memory,
        memory_tool.learn_email_style_profile,
        memory_tool.check_workflow_duplicate,
        memory_tool.record_workflow_dedup,
        load_pending_approval_items,
        create_approval_queue_item,
        opportunity_scout.search_opportunity_sources_placeholder,
        opportunity_scout.save_opportunity_placeholder,
    )

    for tool in guarded_tools:
        assert getattr(tool, "tool_input_guardrails", None), f"{tool.name} lacks input guardrails"
        assert getattr(tool, "tool_output_guardrails", None), f"{tool.name} lacks output guardrails"
        sdk_tool = getattr(tool, "sdk_tool", None)
        if sdk_tool is not None:
            assert getattr(sdk_tool, "tool_input_guardrails", None)
            assert getattr(sdk_tool, "tool_output_guardrails", None)


def test_external_tool_modules_enforce_local_tool_guardrails() -> None:
    required_modules = {
        "approval_tool.py",
        "gmail_tool.py",
        "serper_tool.py",
        "slack_tool.py",
        "storage_tool.py",
    }

    for name in required_modules:
        text = (KEYSTONE_AGENTS_ROOT / "tools" / name).read_text(encoding="utf-8")
        if name == "serper_tool.py":
            text += (KEYSTONE_AGENTS_ROOT / "tools" / "search_provider.py").read_text(
                encoding="utf-8"
            )
        assert "enforce_tool_input_guardrails" in text
        assert "enforce_tool_output_guardrails" in text


def test_openai_agents_sdk_is_imported_only_in_shared_helper() -> None:
    for path in (PROJECT_ROOT / "src").rglob("*.py"):
        if "__pycache__" in path.parts or path == KEYSTONE_AGENTS_ROOT / "sdk.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "from agents import" not in text
        assert "import agents" not in text


def test_canonical_builders_return_structured_sdk_agents_with_guardrails() -> None:
    for name, builder, output_type, prompt_files in CANONICAL_AGENTS:
        agent = builder()

        assert agent.name == name
        assert agent.output_type is output_type
        assert issubclass(agent.output_type, BaseModel)
        assert agent.handoff_description
        assert agent.tools
        assert agent.input_guardrails
        assert agent.output_guardrails
        assert "<!-- AGENTS.md -->" in agent.instructions
        assert "<!-- memory_policy.md -->" in agent.instructions
        assert "Agent Improvement Test Pack" in agent.instructions
        assert "Memory And Pre-Run Context Policy" in agent.instructions
        for prompt_file in prompt_files:
            assert f"<!-- {prompt_file} -->" in agent.instructions


def test_canonical_fixture_mode_exists_for_each_specialist() -> None:
    assert callable(gmail_triage.run_gmail_triage_fixture)
    assert callable(business_research_analyst.research_account_from_search_results)
    assert callable(research_company_fixture)
    assert callable(opportunity_scout.scout_opportunities_fixture)
    assert callable(outreach_composer.compose_outreach_draft_fixture)


def test_canonical_typed_sdk_runtime_harness_exists_for_each_specialist() -> None:
    assert callable(gmail_triage.run_gmail_triage_sdk)
    assert callable(business_research_analyst.run_business_research_analyst_sdk)
    assert callable(opportunity_scout.run_opportunity_scout_sdk)
    assert callable(orchestrator.run_orchestrator_sdk)
    assert callable(outreach_composer.run_outreach_composer_sdk)


def test_shared_retrieve_normalize_synthesis_harness_exists() -> None:
    assert callable(run_module.run_retrieved_sdk_synthesis)
    assert callable(run_module.sdk_synthesis_trace_metadata)


def test_no_send_tool_is_exposed_by_canonical_agents() -> None:
    for _name, builder, _output_type, _prompt_files in CANONICAL_AGENTS:
        agent = builder()
        tool_names = {getattr(tool, "name", getattr(tool, "__name__", "")) for tool in agent.tools}

        assert not any(name.startswith("send") or "send_email" in name for name in tool_names)


def test_external_email_send_functions_are_intentionally_unavailable() -> None:
    for call in (
        lambda: GmailTool().send_email("person@example.com", "Subject", "Body"),
        lambda: gmail_tool.send_email("person@example.com", "Subject", "Body"),
    ):
        try:
            call()
        except NotImplementedError:
            continue
        raise AssertionError("External email sending must remain unavailable.")


def test_generated_outbound_copy_contains_no_em_dash() -> None:
    triage = gmail_triage.run_gmail_triage_fixture(
        FIXTURES_ROOT / "sample_email_consulting.txt",
        sender_name="Alex",
        sender_email="alex@example.com",
    )
    draft = outreach_composer.compose_outreach_draft_fixture(
        company_profile=outreach_composer.load_company_profile("sample_company_curebase"),
        opportunity_record=outreach_composer.load_opportunity_record("sample_lead_curebase"),
    )

    payload = json.dumps([triage.model_dump(), draft.model_dump()], ensure_ascii=False)
    assert "\u2014" not in payload


def test_company_and_opportunity_outputs_include_source_attribution() -> None:
    profile = research_company_fixture(
        company_name="Curebase",
        fixture_json=FIXTURES_ROOT / "sample_company_curebase.json",
    )
    scout_result = opportunity_scout.scout_opportunities_fixture(max_results=3)

    assert profile.sources
    assert all(source.url and source.supported_claims for source in profile.sources)
    assert scout_result.records
    for record in scout_result.records:
        assert record.sources
        assert all(source.url and source.supported_signal for source in record.sources)


def test_live_integrations_are_behind_explicit_cli_flags() -> None:
    gmail_cli = (PROJECT_ROOT / "scripts" / "run_gmail_triage.py").read_text(encoding="utf-8")
    scout_cli = (PROJECT_ROOT / "scripts" / "run_opportunity_scout.py").read_text(encoding="utf-8")
    outreach_cli = (PROJECT_ROOT / "scripts" / "run_outreach_draft.py").read_text(encoding="utf-8")

    assert "--live-gmail" in gmail_cli
    assert "--no-dry-run" in gmail_cli
    assert "--apply-labels" in gmail_cli
    assert "--create-draft" in gmail_cli
    assert "--live-slack" in gmail_cli
    assert "--live-search" in scout_cli
    assert "--live-slack" in outreach_cli


def test_keystone_agents_builders_use_shared_sdk_helper(monkeypatch) -> None:
    calls: list[str] = []

    def spy_build_sdk_agent(*args, **kwargs):
        calls.append(kwargs.get("name", args[0] if args else ""))
        return sdk_module.build_sdk_agent(*args, **kwargs)

    modules_and_builders = [
        (gmail_triage, gmail_triage.build_gmail_triage_agent),
        (
            business_research_analyst,
            business_research_analyst.build_business_research_analyst_agent,
        ),
        (opportunity_scout, opportunity_scout.build_opportunity_scout_agent),
        (outreach_composer, outreach_composer.build_outreach_composer_agent),
        (orchestrator, orchestrator.build_orchestrator_agent),
    ]

    for module, _builder in modules_and_builders:
        monkeypatch.setattr(module, "build_sdk_agent", spy_build_sdk_agent)

    for _module, builder in modules_and_builders:
        builder()

    assert calls == [
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "gmail_triage",
        "business_research_analyst",
        "opportunity_scout",
        "outreach_composer",
        "orchestrator",
    ]


def test_no_langchain_or_langgraph_dependency() -> None:
    pyproject_text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()

    assert "langchain" not in pyproject_text
    assert "langgraph" not in pyproject_text
