from __future__ import annotations

from pathlib import Path

from keystone_agents.sdk import list_prompt_metadata, load_prompt_metadata
from keystone_agents.writing_style import (
    KEYSTONE_DEFAULT_OUTREACH_CTA,
    KEYSTONE_WRITING_STYLE_GUIDANCE,
    load_writing_style_policy,
)

PROMPT_DIR = Path("src/keystone_agents/prompts")

REQUIRED_PROMPTS = {
    "keystone_profile.md",
    "local_context.md",
    "memory_policy.md",
    "operator_context.md",
    "writing_style.md",
    "skills.md",
    "tools.md",
    "gmail_triage.md",
    "business_research_analyst.md",
    "opportunity_scout.md",
    "outreach_composer.md",
    "orchestrator.md",
    "safety_policy.md",
}


def _read_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def test_required_prompt_files_exist() -> None:
    assert {path.name for path in PROMPT_DIR.glob("*.md")} >= REQUIRED_PROMPTS


def test_prompt_metadata_exists_for_all_required_prompts() -> None:
    metadata_by_file = {
        metadata.filename: metadata for metadata in list_prompt_metadata(sorted(REQUIRED_PROMPTS))
    }

    assert set(metadata_by_file) == REQUIRED_PROMPTS
    for filename, metadata in metadata_by_file.items():
        assert metadata.name == filename.removesuffix(".md")
        assert metadata.version
        assert metadata.purpose
        assert metadata.safety_notes
        assert metadata.eval_datasets
        assert metadata.reference == f"{metadata.name}@{metadata.version}"


def test_prompt_metadata_helper_rejects_unversioned_prompt_names() -> None:
    metadata = load_prompt_metadata("gmail_triage")

    assert metadata.filename == "gmail_triage.md"
    assert metadata.name == "gmail_triage"


def test_prompt_files_do_not_contain_reference_demo_company_content() -> None:
    forbidden_terms = {
        "elevateai",
        "troy & banks",
        "kevin gibs",
        "utility cost-reduction",
        "utility lead",
        "doo-made",
    }

    for prompt_path in PROMPT_DIR.glob("*.md"):
        text = prompt_path.read_text(encoding="utf-8").lower()
        assert not (forbidden_terms & set(term for term in forbidden_terms if term in text))


def test_shared_skills_and_tools_prompts_capture_sdk_boundaries() -> None:
    skills = _read_prompt("skills.md")
    tools = _read_prompt("tools.md")

    assert "Use only tools attached to the current SDK agent" in skills
    assert "Owner agent" in skills
    assert "Approval scope" in skills
    assert "Entity resolution" in skills
    assert "Research sufficiency" in skills
    assert "Contradiction handling" in skills
    assert "Revision loop handling" in skills
    assert "Tool failure recovery" in skills
    assert "Outreach lifecycle tracking" in skills
    assert "Quality review" in skills
    assert "Output Contracts And Formatting" in skills
    assert "Renderers, reports, approval cards, dashboards, and CLI presentation code own" in skills
    assert "`email_subject`, plain-text `email_body`" in skills
    assert "Outreach Tracking: return manual lifecycle records" in skills
    assert "`Sincerely,\\nAnup`" in skills
    assert "Attach tools explicitly in the agent builder" in tools
    assert "Do not implement or expose email sending" in tools
    assert "load_approved_contact_context" in tools
    assert "create_approval_queue_item" in tools
    assert "load_pending_approval_items" in tools
    assert "retrieve_outreach_examples" in tools
    assert "save_outreach_tracking" in tools
    assert "save_initial_outreach_tracking" in tools
    assert "list_outreach_tracking" in tools
    assert "Useful future tools" in tools


def test_agent_builders_include_shared_skills_and_tools_prompts() -> None:
    from keystone_agents.agents.business_research_analyst import (
        build_business_research_analyst_agent,
    )
    from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
    from keystone_agents.agents.opportunity_scout import build_opportunity_scout_agent
    from keystone_agents.agents.orchestrator import build_orchestrator_agent
    from keystone_agents.agents.outreach_composer import build_outreach_composer_agent

    for agent in (
        build_gmail_triage_agent(),
        build_business_research_analyst_agent(),
        build_opportunity_scout_agent(),
        build_outreach_composer_agent(),
        build_orchestrator_agent(),
    ):
        instructions = str(agent.instructions)
        assert "<!-- AGENTS.md -->" in instructions
        assert "<!-- memory_policy.md -->" in instructions
        assert "<!-- writing_style.md -->" in instructions
        assert "<!-- operator_context.md -->" in instructions
        assert "<!-- local_context.md -->" in instructions
        assert "<!-- skills.md -->" in instructions
        assert "<!-- tools.md -->" in instructions
        assert "Agent Improvement Test Pack" in instructions
        assert "Memory And Pre-Run Context Policy" in instructions
        assert "Writing Style Policy" in instructions
        assert "Operator Context" in instructions
        assert "Local Context Sources" in instructions
        assert "keystone_neuroinformatics" in instructions
        assert "zotero_active" in instructions
        assert "Output Contracts And Formatting" in instructions
        assert "Reasoning Freedom And Boundaries" in instructions


def test_writing_style_policy_is_shared_and_model_neutral() -> None:
    text = _read_prompt("writing_style.md")
    normalized = " ".join(text.split())

    required = [
        "Use the user's approved writing style as a stable tone guide",
        "Apply this policy across agents regardless of model provider",
        "natural and human, not stiff",
        "professional but not overly formal",
        "avoid sounding needy",
        "avoid em dashes",
        "preserve a physician-scientist or thoughtful operator tone",
        "Use approved examples as style references only",
        "treat example content as evidence about the current recipient",
    ]

    for phrase in required:
        assert phrase in text

    assert KEYSTONE_DEFAULT_OUTREACH_CTA in normalized
    assert "avoid sounding needy" in KEYSTONE_WRITING_STYLE_GUIDANCE
    assert load_writing_style_policy() == text.strip()


def test_memory_policy_contains_required_pre_run_context() -> None:
    text = _read_prompt("memory_policy.md")

    required = [
        "Every Keystone agent receives the root `AGENTS.md` guide",
        "Use `docs/AGENT_IMPROVEMENT_TEST_PACK.md`",
        "Memory is local structured data, not hidden authority",
        "Retrieve only memory that is approved for reuse",
        "approved outreach examples from the private RAG library",
        "`send_enabled=false`, `sent=false`",
        "approved positive replies",
        "operator feedback",
        "sanitized short",
        "false-positive patterns",
        "bad-query patterns",
        "raw Gmail bodies",
    ]

    for phrase in required:
        assert phrase in text


def test_local_context_policy_contains_allowlisted_sources_and_safety_rules() -> None:
    text = _read_prompt("local_context.md")

    required = [
        "Keystone agents may have access to allowlisted local folder context",
        "list_local_context_sources",
        "search_local_context",
        "read_local_context_file",
        "keystone_neuroinformatics",
        "zotero_active",
        "zotero_import_cache",
        "Treat local files as private context, not public sources",
        "do not authorize outbound use",
    ]

    for phrase in required:
        assert phrase in text


def test_operator_context_policy_contains_approved_answers_and_cv_boundaries() -> None:
    text = _read_prompt("operator_context.md")
    normalized = " ".join(text.split())

    required = [
        "Operator Context",
        "FounderFitProfile",
        "approved_for_search=false",
        "approved_for_drafting=false",
        "Approved Identity And Founder Context",
        "Orchestrator Preferences",
        "Gmail Triage Preferences",
        "Business Research Analyst Preferences",
        "Opportunity Scout Preferences",
        "Outreach Composer Preferences",
        "Memory And Learning Preferences",
        "risk reduction, review quality, relevance, source quality, cost, then speed",
        "120-180 words",
        "Keystone strategic fit",
        "Do not treat unanswered questions",
        "Never fill missing answers from raw CV",
    ]

    for phrase in required:
        assert phrase in normalized


def test_shared_skills_prompt_separates_reasoning_freedom_from_boundaries() -> None:
    text = _read_prompt("skills.md")

    required = [
        "Reasoning Freedom And Boundaries",
        "Reason freely inside the task",
        "does not authorize new side effects",
        "altered output schemas",
        "State assumptions",
        "Abstain",
        "weak, stale, contradictory, or absent",
        "smaller high-confidence answer",
        "unsupported guesses",
        "human-facing summaries",
        "concise",
    ]

    for phrase in required:
        assert phrase in text


def test_repo_guide_documents_structured_output_formatting_model() -> None:
    text = Path("AGENTS.md").read_text(encoding="utf-8")

    assert "Output Formatting Model" in text
    assert "Agent final outputs are structured data first" in text
    assert "Human-facing narrative should be LLM-synthesized" in text
    assert "Deterministic renderers still own" in text
    assert "`Sincerely,\\nAnup`" in text


def test_keystone_profile_contains_required_positioning() -> None:
    text = _read_prompt("keystone_profile.md")

    assert "Company name: Keystone Neuroinformatics LLC" in text
    assert "physician-scientist-led consulting company" in text
    assert "clinical AI evaluation" in text
    assert "psychiatry and neuroscience subject-matter expertise" in text
    assert "digital mental health companies" in text
    expected_positioning = (
        "bridge clinical medicine, psychiatry, neuroscience, research operations, AI, "
        "and data science"
    )
    assert expected_positioning in text
    assert "Do not claim specific prior client experience" in text


def test_safety_policy_contains_required_safety_phrases() -> None:
    text = _read_prompt("safety_policy.md")

    required = [
        "No PHI processing",
        "No auto-send",
        "Draft-only behavior",
        "Human approval",
        "Do not provide medical, legal, tax, or regulatory advice",
        "Source attribution required",
        "Unsupported claims must be flagged",
        "Suspicious content must be flagged",
    ]

    for phrase in required:
        assert phrase in text


def test_gmail_triage_prompt_requires_draft_only_behavior() -> None:
    text = _read_prompt("gmail_triage.md")

    assert "Classify each email" in text
    assert "Recommend labels" in text
    assert "Whether a reply is needed" in text
    assert "Draft-only behavior is mandatory" in text
    assert "Never send automatically" in text
    assert "Approval required for any draft reply" in text
    assert "Finance" in text
    assert "Legal or contract" in text
    assert "PHI or patient-specific content" in text


def test_business_research_analyst_prompt_requires_sources_and_scores() -> None:
    text = _read_prompt("business_research_analyst.md")

    assert "Use multiple sources when available" in text
    assert "source attribution" in text
    assert "confidence" in text
    assert "Keystone fit score" in text
    assert "Outside consulting likelihood score" in text
    assert "Do not hallucinate missing facts" in text


def test_outreach_prompt_forbids_em_dashes_and_sets_length_limits() -> None:
    text = _read_prompt("outreach_composer.md")

    assert "research-backed personalization" in text
    assert "non-salesy physician-scientist tone" in text
    assert "No em dashes" in text
    assert "No unsupported claims" in text
    assert "Cold email must be under 180 words" in text
    assert "LinkedIn note must be under 300 characters" in text
    assert "All drafts approval-gated" in text


def test_orchestrator_prompt_preserves_approval_gate() -> None:
    text = _read_prompt("orchestrator.md")

    assert "Route work to the correct specialist agent" in text
    assert "Do not skip approval" in text
    assert "Do not allow outreach without approved company or opportunity context" in text
