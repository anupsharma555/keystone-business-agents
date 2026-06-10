from __future__ import annotations

import json
from pathlib import Path

from keystone_agents.sdk import (
    compose_instructions,
    list_prompt_metadata,
    list_skill_metadata,
    load_prompt_metadata,
    repo_instruction_profile_id,
    skill_metadata_for_files,
)
from keystone_agents.skill_sets import (
    AGENT_SKILL_NAMES,
    CORE_SKILL_NAMES,
    SHARED_REASONING_SKILL_NAMES,
    SPECIALIST_SKILL_NAMES,
    select_agent_skill_names,
)
from keystone_agents.writing_style import (
    KEYSTONE_DEFAULT_OUTREACH_CTA,
    KEYSTONE_WRITING_STYLE_GUIDANCE,
    load_writing_style_policy,
)

PROMPT_DIR = Path("src/keystone_agents/prompts")
SKILL_DIR = Path("src/keystone_agents/skills")
SKILL_CONTRACT_EVAL_PATH = Path("evals/local/skill_contracts.jsonl")

REQUIRED_PROMPTS = {
    "keystone_profile.md",
    "local_context.md",
    "memory_policy.md",
    "repo_runtime_policy.md",
    "agent-operating-architecture.md",
    "operator_context.md",
    "writing_style.md",
    "slack-posting-rules.md",
    "skills.md",
    "tools.md",
    "gmail_triage.md",
    "business_research_analyst.md",
    "opportunity_scout.md",
    "outreach_composer.md",
    "orchestrator.md",
    "chief_of_staff.md",
    "safety_policy.md",
}


def _read_prompt(name: str) -> str:
    return (PROMPT_DIR / name).read_text(encoding="utf-8")


def _read_skill(name: str) -> str:
    return (SKILL_DIR / name / "SKILL.md").read_text(encoding="utf-8")


def _read_repo_doc(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


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
    assert "prompt-only instruction fragments" in skills
    assert "not a runtime capability model" in skills
    assert "hidden router" in skills
    assert "dynamic tool attachment path" in skills
    assert "Treat skills as prompt guidance only" in skills
    assert "Owner agent" in skills
    assert "If runtime behavior is required" in skills
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
    assert "Google Workspace Tools" in tools
    assert "`KNIOps` Drive folder" in tools
    assert "`GOOGLE_WORKSPACE_WRITES_ENABLED=true`" in tools
    assert "`google_sheet_append_rows`" in tools
    assert "Useful future tools" in tools


def test_skill_bundles_define_reasoning_contracts_not_tools() -> None:
    all_skill_names = tuple(metadata.skill_id for metadata in list_skill_metadata())
    metadata = skill_metadata_for_files(all_skill_names)

    assert set(SHARED_REASONING_SKILL_NAMES) <= set(all_skill_names)
    assert {item["skill_id"] for item in metadata} == set(all_skill_names)
    for item in metadata:
        skill_name = item["skill_id"]
        text = _read_skill(skill_name)
        assert item["version"]
        assert item["purpose"]
        assert "evals/local/skill_contracts.jsonl" in item["eval_datasets"]
        assert item["eval_datasets"] or item["validation_paths"]
        assert "## Required Behavior" in text
        assert "## Flexible Behavior" in text
        assert "## Boundaries" in text
        assert "## Output Contract" in text
        assert "## Failure Modes" in text
        assert "## Eval Criteria" in text
        assert "## Reasoning Questions" in text
        assert "## Decision Rubric" in text
        assert "## Tie-Breakers" in text
        assert "Must not" in text or "must not" in text
        assert "never authorize" in text.lower() or "must not" in text.lower()
    assert "Must not invent source IDs" in _read_skill("evidence_attribution_and_claim_mapping")
    assert "Must not send email" in _read_skill("action_boundary_enforcement")
    assert "workflow_lifecycle_tracking" in all_skill_names
    assert "handoff_contract_packaging" in all_skill_names
    assert "gmail_triage_specialist_contracts" in all_skill_names
    assert "chief_of_staff_specialist_contracts" in all_skill_names


def test_skill_contract_eval_cases_cover_shared_and_specialist_skills() -> None:
    cases = [
        json.loads(line)
        for line in SKILL_CONTRACT_EVAL_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    covered_skills = {skill for case in cases for skill in case["skills"]}
    covered_agents = {agent for case in cases for agent in case["agents"]}
    all_declared_skills = {
        skill_name for skill_names in AGENT_SKILL_NAMES.values() for skill_name in skill_names
    }

    assert all_declared_skills <= covered_skills
    assert set(AGENT_SKILL_NAMES) <= covered_agents
    for case in cases:
        expected = case["expected"]
        assert any(key.startswith("must_") for key in expected)
        assert "must_not_auto_merge" in expected or any(
            key.startswith("must_not_") for key in expected
        )


def test_dynamic_skill_selector_includes_core_and_specialist_contracts() -> None:
    for agent_name in AGENT_SKILL_NAMES:
        selected = select_agent_skill_names(agent_name)

        assert set(CORE_SKILL_NAMES) <= set(selected)
        assert SPECIALIST_SKILL_NAMES[agent_name] in selected
        assert set(selected) <= set(AGENT_SKILL_NAMES[agent_name])
        assert len(selected) < len(AGENT_SKILL_NAMES[agent_name])


def test_dynamic_skill_selector_adds_relevant_optional_contracts() -> None:
    selected = select_agent_skill_names(
        "gmail_triage",
        request_text=(
            "Save a source-cited report to a Google Sheet after checking missing "
            "evidence and handoff status."
        ),
    )

    assert "workspace_artifact_governance" in selected
    assert "evidence_attribution_and_claim_mapping" in selected
    assert "unsupported_claim_and_gap_handling" in selected
    assert "workflow_lifecycle_tracking" in selected
    assert "chief_of_staff_specialist_contracts" not in selected


def test_draft_agents_load_operator_writing_style_by_default() -> None:
    assert "writing_style_adaptation" in select_agent_skill_names("gmail_triage")
    assert "writing_style_adaptation" in select_agent_skill_names("outreach_composer")
    assert "writing_style_adaptation" not in select_agent_skill_names("opportunity_scout")


def test_dynamic_skill_selector_supports_explicit_full_catalog_mode() -> None:
    assert (
        select_agent_skill_names("outreach_composer", include_all=True)
        == AGENT_SKILL_NAMES["outreach_composer"]
    )


def test_source_triage_skill_is_selected_for_search_heavy_requests() -> None:
    assert "source_triage_decision" in select_agent_skill_names(
        "chief_of_staff",
        request_text="Do a deeper web search with Exa and Tavily sources.",
    )
    assert "source_triage_decision" in select_agent_skill_names(
        "business_research_analyst",
        request_text="Research OpenAI mental health safety using source-backed web search.",
    )
    assert "source_triage_decision" in select_agent_skill_names(
        "opportunity_scout",
        request_text="Find grant and RFP source candidates for behavioral health AI.",
    )
    assert "source_triage_decision" in select_agent_skill_names(
        "gmail_triage",
        request_text="Use retrieved web sources to contextualize this email thread.",
    )


def test_source_triage_skill_requires_deepened_links_to_be_read_before_synthesis() -> None:
    text = _read_skill("source_triage_decision")
    normalized = " ".join(text.split())

    assert "adds, reranks, or\n  promotes additional candidate links" in text
    assert "must be read/extracted or explicitly marked snippet-only" in normalized
    assert "If a broaden/deepen/rerank pass changes the retained source set" in text
    assert (
        "newly retained links must appear in source refs with extracted/read content" in normalized
    )
    assert "Additional links introduced by specialist deepening or reranking are read" in text


def test_operating_architecture_requires_reranked_links_to_be_extracted_before_synthesis() -> None:
    text = _read_prompt("agent-operating-architecture.md")
    normalized = " ".join(text.split())

    assert "separate search planning, retrieval, source ranking, claim extraction" in text
    assert "broadens, deepens, reranks, or promotes additional links" in normalized
    assert "read/extracted or explicitly marked snippet-only before final synthesis" in normalized


def test_agent_builders_include_selected_skills_and_tools_prompts() -> None:
    from keystone_agents.agents.business_research_analyst import (
        build_business_research_analyst_agent,
    )
    from keystone_agents.agents.chief_of_staff import build_chief_of_staff_agent
    from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
    from keystone_agents.agents.opportunity_scout import build_opportunity_scout_agent
    from keystone_agents.agents.orchestrator import build_orchestrator_agent
    from keystone_agents.agents.outreach_composer import build_outreach_composer_agent

    agents = {
        "gmail_triage": build_gmail_triage_agent(),
        "business_research_analyst": build_business_research_analyst_agent(),
        "opportunity_scout": build_opportunity_scout_agent(),
        "outreach_composer": build_outreach_composer_agent(),
        "orchestrator": build_orchestrator_agent(),
        "chief_of_staff": build_chief_of_staff_agent(),
    }
    for name, agent in agents.items():
        instructions = str(agent.instructions)
        assert "<!-- repo_runtime_policy.md -->" in instructions
        assert "<!-- AGENTS.md -->" not in instructions
        assert "<!-- memory_policy.md -->" in instructions
        assert "<!-- agent-operating-architecture.md -->" in instructions
        assert "<!-- writing_style.md -->" in instructions
        assert "<!-- slack-posting-rules.md -->" in instructions
        assert "<!-- operator_context.md -->" in instructions
        assert "<!-- local_context.md -->" in instructions
        assert "<!-- skills.md -->" not in instructions
        selected_skills = select_agent_skill_names(name)
        for skill_name in selected_skills:
            assert f"<!-- {skill_name}/SKILL.md -->" in instructions
        for skill_name in set(AGENT_SKILL_NAMES[name]) - set(selected_skills):
            assert f"<!-- {skill_name}/SKILL.md -->" not in instructions
        assert "<!-- tools.md -->" in instructions
        assert "Agent Improvement Test Pack" in instructions
        assert "Memory And Pre-Run Context Policy" in instructions
        assert "Shared Agent Operating Architecture" in instructions
        assert "Schema + Tools + Helpers + Memory + Model Synthesis" in instructions
        assert "Writing Style Policy" in instructions
        assert "Slack Posting Rules" in instructions
        assert "Operator Context" in instructions
        assert "Local Context Sources" in instructions
        assert "keystone_neuroinformatics" in instructions
        assert "zotero_active" in instructions
        assert "Context Permission Gating" in instructions
        if "evidence_attribution_and_claim_mapping" in selected_skills:
            assert "Evidence Attribution And Claim Mapping" in instructions
        if "workflow_lifecycle_tracking" in selected_skills:
            assert "Workflow Lifecycle Tracking" in instructions
        if "handoff_contract_packaging" in selected_skills:
            assert "Handoff Contract Packaging" in instructions


def test_agent_builder_request_text_controls_skill_visibility() -> None:
    from keystone_agents.agents.gmail_triage import build_gmail_triage_agent

    default_instructions = str(build_gmail_triage_agent().instructions)
    request_instructions = str(
        build_gmail_triage_agent(
            request_text="Save a source-cited report to Google Drive with missing evidence gaps."
        ).instructions
    )

    assert "<!-- workspace_artifact_governance/SKILL.md -->" not in default_instructions
    assert "<!-- unsupported_claim_and_gap_handling/SKILL.md -->" not in default_instructions
    assert "<!-- workspace_artifact_governance/SKILL.md -->" in request_instructions
    assert "<!-- unsupported_claim_and_gap_handling/SKILL.md -->" in request_instructions


def test_shared_agent_operating_architecture_covers_schemas_tools_helpers() -> None:
    text = _read_prompt("agent-operating-architecture.md")

    assert "Schema + Tools + Helpers + Memory + Model Synthesis" in text
    assert "Airtable" in text
    assert "`airtable_get_base_schema` before `airtable_read_records`" in text
    assert "Gmail, Calendar, and Google Workspace" in text
    assert (
        "search planning, retrieval, source ranking, claim extraction, schema structuring, "
        "and synthesis"
    ) in text
    assert "`structure_web_data_for_schema`" in text
    assert "`airtable_write_record`" in text
    assert (
        "no deletes, schema changes, attachment uploads, bulk overwrites, or silent mutations"
        in text
    )
    assert "use Playwright only as a read-only backend/headless diagnostic rendering helper" in text
    assert "`render_page`" in text
    assert "`capture_browser_diagnostics`" in text
    assert "`summarize_rendered_page_diagnostics`" in text
    assert "console, page-error, failed-request, and response-status evidence" in text
    assert "disabled by default" in text
    assert "does not open a user-screen browser" in text
    assert "temporary non-persistent profile" in text
    assert "gpt-5.4-mini" in text
    assert (
        "no live side effects unless the relevant tool, live flag, and approval scope allow them"
        in text
    )
    assert "Schema And Helper Backlog" in text
    assert "`AgentRunContextPack`" in text
    assert "`StructuredRecordSet`" in text
    assert "`DeterministicCalculationResult`" in text
    assert "`MutationPlan`" in text
    assert "`WorkspaceArtifactPlan`" in text
    assert "`SourceRetrievalPack`" in text
    assert "`CommunicationDraftContext`" in text
    assert "`OutboundConversationTracker`" in text
    assert "`DataQualityIssue`" in text


def test_agents_guide_contains_tool_use_decision_rules() -> None:
    text = _read_repo_doc("AGENTS.md")

    assert "Tool Use Decision Rules" in text
    assert "Use schema tools first for structured systems" in text
    assert "Use deterministic helpers for exact arithmetic" in text
    assert "Use Google Workspace tools for internal artifacts" in text
    assert "Use Airtable tools for table records, not browser automation" in text
    assert "Use Gmail and Slack structured tools" in text
    assert "Use extraction providers first for web content" in text
    assert "Use Playwright as read-only backend/headless rendered-browser diagnostics" in text
    assert "must not open a user-screen browser" in text
    assert "Use `capture_browser_diagnostics`" in text
    assert "`summarize_rendered_page_diagnostics`" in text
    assert "After implementing each tool or helper, run its focused tests" in text


def test_agent_improvement_pack_tracks_future_tool_helper_backlog() -> None:
    text = _read_repo_doc("docs/AGENT_IMPROVEMENT_TEST_PACK.md")

    assert "Tool Helper Backlog" in text
    assert "Lighthouse / Chrome DevTools diagnostics" in text
    assert "HAR capture and replay helpers" in text
    assert "OCR or vision models" in text
    assert "Crawl4AI, Firecrawl, and Trafilatura" in text
    assert "OpenAI file search/vector store helpers" in text
    assert "MCP tool-search and namespace-loading helpers" in text
    assert "Sandbox/Codex workspace-review helpers" in text
    assert "Structured Airtable, Gmail, Slack, Calendar, Drive, and CRM" in text


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


def test_slack_posting_rules_include_readable_finance_example() -> None:
    text = _read_prompt("slack-posting-rules.md")
    normalized = " ".join(text.split())

    assert "Slack responses should be explainable, readable" in text
    assert "more\nuseful than a generic LLM search answer" in text
    assert "Do not compress complex results into one dense paragraph" in normalized
    assert "## Section Order" in text
    assert "1. `Answer`" in text
    assert "2. `Detailed Summary`" in text
    assert "3. `Terms`" in text
    assert "4. `Recommended actions`" in text
    assert "5. `Suggested route`" in text
    assert "6. `Metadata`" in text
    assert "`Metadata` is always last when present" in text
    assert "not a rigid visual template" in text
    assert "Follow\nexplicit user formatting instructions" in text
    assert "short brief, longer report, comparison,\ntable, numbered list, bulleted list" in text
    assert 'For narrow follow-ups such as "summarize link 1"' in text
    assert "using context-pack `ordered_sources`" in text
    assert "`Answer`: Give the shortest useful direct answer first" in text
    assert "`Detailed Summary`: Treat this as the detailed answer" in text
    assert "`Detailed Summary` must\n  start with a narrative summary paragraph" in text
    assert "A source list, provider-result list, or bullet list is not a substitute" in text
    assert "more useful than a generic web-search answer" in text
    assert "based on read/extracted\n  content from selected links" in text
    assert (
        "Do not treat search\n  snippets or provider-result titles as if they were page-level evidence"
        in text
    )
    assert "After the summary paragraph, use a short bulleted list" in normalized
    assert "most relevant or interesting details" in normalized
    assert "what the\n  strongest source pages actually say" in text
    assert (
        "Keep provider counts, lane status, credits, route labels, and workflow ids out of `Detailed Summary`"
        in normalized
    )
    assert "source links for external facts" in normalized
    assert "Avoid a bare status line" in text
    assert "`Terms`: Explain acronyms" in text
    assert "`Recommended actions`: Include concrete next steps" in text
    assert "`Suggested route`: Include workflow, command, target channel" in text
    assert "`Metadata`: Put diagnostic metadata at the end of the answer" in text
    assert "Search And Research Shape" in text
    assert "web search, document retrieval, live search" in text
    assert "* Search query: `<exact query used>`" in text
    assert "* Provider top results:" in text
    assert "`source_context_status`, `source_context_focus`, or `source_triage`" in text
    assert "`source_triage`" in text
    assert "rejected sources" in text
    assert "deepen-needed sources" in normalized
    assert "do not use provider snippets, rejected sources, deepen-needed sources" in normalized
    assert "off-focus source links as the basis for a detailed answer" in normalized
    assert "`CalMHSA`, `RFP`,\n  `MBC`, or `SDK web search`" in text
    assert "Specialist Formatting Boundaries" in text
    assert "Gmail triage should keep its email classification" in text
    assert "Outreach Composer should keep outbound email and LinkedIn drafts" in text
    assert "Web/document retrieval agents should use the search/research shape" in text
    assert "Q1 and Q2 2026 finance_tax_tracker Summary" in text
    assert "* Total income: $47,323.94" in text
    assert "* Total expenses: $12,057.51" in text
    assert "Income detail" in text
    assert "Expense detail" in text
    assert "include primary source links where" in text
    assert "`apa.org`\n  refers to the American Psychological Association" in text
    assert "one record is one row in the table" in text
    assert "`Estimated Tax Period`, `Q`, and `Quarter`" in text
    assert "* Business expenses: 16 matching records, totaling $3,287.40" in text
    assert "Do not repeat standing disclaimers" in text


def test_chief_of_staff_finance_doc_prompt_requires_analysis_not_transfer() -> None:
    text = _read_prompt("chief_of_staff.md")

    assert "pass that exact value to" in text
    assert "`google_drive_create_folder` and `google_doc_write`" in text
    assert "include actual analysis, not just data\ntransfer" in text
    assert "rolling-note assumptions" in text
    assert "Use deterministic arithmetic from\nnormalized records" in text


def test_tools_prompt_documents_shared_search_contract_for_all_search_agents() -> None:
    text = _read_prompt("tools.md")
    normalized = " ".join(text.split())

    assert "## Shared Web Search Contract" in text
    assert "Agents with `search_web` all use the same shared retrieval contract" in text
    assert "should not choose providers directly" in text
    assert (
        "SearXNG as the broad-recall lane plus a capped Agents SDK hosted web-search lane"
        in normalized
    )
    assert "Exa is a capped semantic deepening lane" in text
    assert "Tavily is a capped deeper-research lane" in text
    assert "Serper remains disabled while credits are unavailable" in normalized
    assert "Use search providers for discovery and search-result recall" in text
    assert "Use extraction providers such as Trafilatura, Firecrawl" in normalized
    assert "provider diagnostics only in the final metadata section" in text
    assert "`source_context_status`" in text
    assert "`source_context_sample`" in text
    assert "`source_context_focus`" in text
    assert "`source_triage`" in text
    assert "`ordered_sources`" in text
    assert "retained sources may support claims" in text
    assert "rejected sources must not support claims" in text
    assert "deepen sources need page\nreading/extraction" in text
    assert "resolve the ordinal reference from `ordered_sources`" in normalized
    assert "do not convert off-focus provider results into a substantive synthesis" in normalized
    assert "The quality bar is higher than generic LLM search" in text
    assert "future MCP\ntools should be integrated only when they add real retrieval" in text
    assert "budget\ncontrols, and compact diagnostics" in text

    section_names = [
        "Gmail Triage",
        "Business Research Analyst",
        "Opportunity Scout",
        "Outreach Composer",
        "Orchestrator",
        "Chief Of Staff",
    ]
    for section_name in section_names:
        start = text.index(f"## {section_name} Tools")
        end = text.find("\n## ", start + 1)
        section_text = text[start : end if end != -1 else len(text)]
        assert "`search_web`" in section_text


def test_chief_of_staff_prompt_requires_visible_source_urls_for_external_facts() -> None:
    text = _read_prompt("chief_of_staff.md")

    assert "include the source\nURL in the user-visible summary on the first answer" in text
    assert "Do not put the citation only\nin structured `sources`" in text
    assert "source\nverification is still needed" in text


def test_chief_of_staff_prompt_keeps_web_briefs_substantive() -> None:
    text = _read_prompt("chief_of_staff.md")
    normalized = " ".join(text.split())

    assert "For broad web questions or deepened search briefs" in text
    assert "answer the substantive\n  user question first" in text
    assert "Do not make the main answer a provider diagnostic" in text
    assert "keep provider/lane details for trailing metadata only" in normalized
    assert "Do not choose `reference-capture` for a question, brief, search, research" in text


def test_repo_guide_requires_visible_source_urls_for_external_facts() -> None:
    text = _read_repo_doc("AGENTS.md")

    assert "External factual confirmations must be source-visible" in text
    assert "include the relevant source URLs in the visible Slack/CLI" in text
    assert "Do not\nhide citations only in structured `sources`" in text
    assert "more useful than\nstandard generic LLM search" in text
    assert "Novel\nopen-source, free-tier, or low-cost capabilities" in text
    assert "Do not add\nprovider-specific prompt branches" in text


def test_ranked_agent_goals_include_distinctive_source_grounded_answers() -> None:
    text = _read_repo_doc("docs/AGENT_GOALS.md")

    assert "## 5. Distinctive Source-Grounded Answers" in text
    assert "more useful than standard\ngeneric LLM search" in text
    assert "`Detailed Summary` is\nthe detailed answer" in text
    assert "open-source or\nfree-tier-friendly tools such as SearXNG, Exa, Tavily" in text
    assert "MCP servers, or dynamic tool\nsurfaces" in text
    assert "budget-aware, dry-run-testable, source-attributed" in text


def test_shared_slack_rules_require_visible_source_urls() -> None:
    text = _read_prompt("slack-posting-rules.md")

    assert "When source-backed claims are present, include primary source links" in text
    assert "Put source URLs in the\n  first user-visible answer" in text
    assert "structured `sources`, hidden metadata, artifacts" in text
    assert "Detailed Summary` must\n  start with a narrative summary paragraph" in text
    assert "A source list, provider-result list, or bullet list is not a substitute" in text
    assert "more useful than a generic web-search answer" in text


def test_orchestrator_prompt_reminds_specialists_about_visible_source_urls() -> None:
    text = _read_prompt("orchestrator.md")

    assert "remind the specialist that source URLs must be" in text
    assert "visible in the first user-facing answer" in text
    assert "not only in structured `sources` or\n  artifacts" in text
    assert "For broad or deep web-search requests" in text
    assert "preserve the full search intent in the\n  specialist brief" in text
    assert "provider-diagnostics or metadata requirements" in text
    assert "Do not reduce the task to only the\n  literal search query" in text


def test_specialist_prompts_require_visible_source_urls() -> None:
    expected = {
        "business_research_analyst.md": [
            "Include source URLs in the first user-visible summary",
            "Structured source records and source IDs are required",
            "not enough by themselves for Slack-facing answers",
            "treat `Detailed Summary` as the detailed\n  answer",
            "start with a narrative summary paragraph",
            "Do not turn the detailed summary into\n  route metadata, provider counts, a source list",
            "base the narrative summary on\n  read/extracted content from selected links",
        ],
        "opportunity_scout.md": [
            "Include source URLs in the first user-visible summary",
            "Structured source records and source IDs are required",
            "not enough by themselves for Slack-facing answers",
            "treat `Detailed Summary` as\n  the detailed answer",
            "start with a narrative summary paragraph",
            "Do not turn the detailed summary into route metadata",
            "base the narrative summary on\n  read/extracted content from selected links",
            "use the extracted page text",
            "Do not treat\n  search snippets alone as full source review",
        ],
        "gmail_triage.md": [
            "Source URLs in the first user-visible summary",
            "For private\n  email-only facts, identify the email/thread context",
            "Use `search_web` only for public source checking",
            "Do not use web search as a\n  substitute for Gmail message/thread reads",
            "place provider diagnostics at the end",
        ],
        "outreach_composer.md": [
            "include\n  source URLs when approved source URLs are available",
            "Do not force raw source\n  URLs into outbound email",
            "Use `search_web` only to check public source context",
            "Live search results do not become approved\noutreach context by themselves",
            "Keep any search-provider diagnostics out of drafts",
        ],
        "chief_of_staff.md": [
            "The `Detailed Summary` should\n  start with a detailed narrative summary",
            "read/extract the strongest primary URLs",
            "cross-source narrative summary of the\n  retrieved link content and source URLs",
            "not merely list links, source titles, or provider snippets",
        ],
    }

    for filename, phrases in expected.items():
        text = _read_prompt(filename)
        for phrase in phrases:
            assert phrase in text


def test_memory_policy_contains_required_pre_run_context() -> None:
    text = _read_prompt("memory_policy.md")

    required = [
        "Every Keystone agent receives the repo runtime policy profile",
        "The full root `AGENTS.md` remains the\nhuman/developer source of truth",
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


def test_repo_runtime_policy_preserves_core_agents_guide_contract() -> None:
    text = _read_prompt("repo_runtime_policy.md")

    required = [
        "Compact runtime profile distilled from AGENTS.md",
        "OpenAI Agents SDK project",
        "structured Pydantic outputs",
        "Default to dry-run-safe behavior",
        "Do not send email",
        "Do not process PHI",
        "explicit live flags and scoped human approval",
        "Tool modules own provider boundaries",
        "Search-heavy agents use the shared `SearchProvider` contract",
        "SearXNG,\nAgents hosted web search, Exa, and Tavily",
        "Serper\nis disabled unless credits are restored",
        "visible source URLs",
        "summarize read/extracted source content first",
        "typed context packs",
        "Prompt-size and cache-stability tests",
        "Agent Improvement Test Pack",
    ]

    for phrase in required:
        assert phrase in text


def test_repo_runtime_policy_stays_compact_against_full_agents_guide() -> None:
    compact = _read_prompt("repo_runtime_policy.md")
    full = _read_repo_doc("AGENTS.md")

    assert len(compact) <= 6500
    assert len(compact) <= int(len(full) * 0.35)


def test_full_agents_guide_profile_can_be_forced(monkeypatch) -> None:
    monkeypatch.setenv("KEYSTONE_AGENTS_GUIDE_PROFILE", "full")

    instructions = compose_instructions("tools.md", "chief_of_staff.md")

    assert repo_instruction_profile_id() == "full-agents-md"
    assert "<!-- AGENTS.md -->" in instructions
    assert "<!-- repo_runtime_policy.md -->" not in instructions
    assert "Keystone Business Agents Guide" in instructions


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
    assert "default to Slack-thread-only draft text" in text
    assert "Do not create a Gmail draft unless a separate backend setting" in text
    assert "Never send automatically" in text
    assert "Approval required for any draft reply" in text
    assert "Finance" in text
    assert "Legal or contract" in text
    assert "PHI or patient-specific content" in text
    assert "Internal Workspace Artifacts" in text
    assert "`KNIOps`" in text
    assert "Google Sheets for structured triage logs" in text
    assert "Workspace artifacts do not authorize sending email" in text
    assert "Outreach Reply Handling" in text
    assert "`list_outreach_tracking_records`" in text
    assert "Treat outreach tracking rows as manual lifecycle context" in text


def test_business_research_analyst_prompt_requires_sources_and_scores() -> None:
    text = _read_prompt("business_research_analyst.md")

    assert "Use multiple sources when available" in text
    assert "source attribution" in text
    assert "confidence" in text
    assert "Keystone fit score" in text
    assert "Outside consulting likelihood score" in text
    assert "Do not hallucinate missing facts" in text
    assert "Internal Workspace Artifacts" in text
    assert "`KNIOps Structured Data`" in text
    assert "Workspace artifact content is not independent public evidence" in text


def test_opportunity_scout_prompt_defines_workspace_artifact_boundaries() -> None:
    text = _read_prompt("opportunity_scout.md")

    assert "Internal Workspace Artifacts" in text
    assert "Use Google Sheets for structured watchlists" in text
    assert "Workspace artifacts do not approve outreach" in text
    assert "`GOOGLE_WORKSPACE_WRITES_ENABLED=true`" in text


def test_outreach_prompt_forbids_em_dashes_and_sets_length_limits() -> None:
    text = _read_prompt("outreach_composer.md")

    assert "research-backed personalization" in text
    assert "non-salesy physician-scientist tone" in text
    assert "No em dashes" in text
    assert "No unsupported claims" in text
    assert "Cold email must be under 180 words" in text
    assert "LinkedIn note must be under 300 characters" in text
    assert "All drafts approval-gated" in text
    assert "Gmail reply drafts default to Slack-thread-only review text" in text
    assert "Provider-side Gmail draft creation is a separate setting-backed action" in text
    assert "Internal Workspace Artifacts" in text
    assert "Use Google Docs for internal call prep" in text
    assert "A Sheet row or Doc note does not authorize sending" in text
    assert "Optional Outreach Lifecycle Tracking" in text
    assert "`save_initial_outreach_tracking_record`" in text
    assert "`list_outreach_tracking_records`" in text
    assert "sent_manually" in text


def test_email_reply_skills_default_to_slack_thread_only_drafts() -> None:
    gmail_skill = _read_skill("gmail_triage_specialist_contracts")
    outreach_skill = _read_skill("outreach_composer_specialist_contracts")
    boundary_skill = _read_skill("action_boundary_enforcement")
    style_skill = _read_skill("writing_style_adaptation")

    assert "prepare Slack-thread-only draft replies" in gmail_skill
    assert "Must not create Gmail drafts by default" in gmail_skill
    assert "default to Slack-thread-only draft text" in outreach_skill
    assert "include one concrete source-backed detail" in outreach_skill
    assert "Gmail draft creation is not implied by reply drafting" in outreach_skill
    assert (
        "Treat inbound email reply drafting as Slack-thread-only text by default" in boundary_skill
    )
    assert "apply style to Slack-thread-only draft text" in style_skill
    assert "concrete thread-grounded detail" in style_skill


def test_orchestrator_prompt_preserves_approval_gate() -> None:
    text = _read_prompt("orchestrator.md")

    assert "Route work to the correct specialist agent" in text
    assert "Do not skip approval" in text
    assert "Do not allow outreach without approved company or opportunity context" in text
    assert "Workspace Routing" in text
    assert "only scoped internal artifact operations inside `KNIOps`" in text
    assert "does not bypass specialist ownership" in text
