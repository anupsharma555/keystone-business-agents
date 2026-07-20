from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    target = Path(path)
    if path == "LINEAR_BACKLOG.MD" and not target.is_file():
        pytest.skip("local-only Linear backlog is unavailable in this clean clone")
    return target.read_text(encoding="utf-8")


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
    assert "data_schema_mapping" in all_skill_names


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
        if set(selected) == set(AGENT_SKILL_NAMES[agent_name]):
            assert agent_name.endswith("_context_agent")
        else:
            assert len(selected) < len(AGENT_SKILL_NAMES[agent_name])


def test_compact_read_only_context_skills_omit_generic_duplicate_contracts() -> None:
    selected = select_agent_skill_names(
        "zotero_context_agent",
        request_text="Read one exact Zotero article and summarize its abstract.",
        compact=True,
    )

    assert "zotero_context_specialist_contracts" in selected
    assert "evidence_attribution_and_claim_mapping" in selected
    assert "context_permission_gating" not in selected
    assert "action_boundary_enforcement" not in selected
    assert "tool_result_resilience" not in selected
    assert "structured_output_quality_review" not in selected


def test_compact_mutation_skills_keep_permission_and_action_contracts() -> None:
    selected = select_agent_skill_names(
        "google_workspace_context_agent",
        request_text="Update one exact Google Doc and verify it.",
        compact=True,
    )

    assert "google_workspace_context_specialist_contracts" in selected
    assert "context_permission_gating" in selected
    assert "action_boundary_enforcement" in selected


def test_ask_to_target_resolution_skill_selected_for_chief_and_airtable() -> None:
    request = (
        "Add a business expense to Airtable business expenses based on receipt details "
        "in /tmp/example-business-cards-receipt.pdf"
    )

    chief_selected = select_agent_skill_names("chief_of_staff", request_text=request)
    airtable_selected = select_agent_skill_names("airtable_context_agent", request_text=request)

    assert "ask_to_target_resolution" in chief_selected
    assert "artifact_evidence_handling" in chief_selected
    assert "data_schema_mapping" in chief_selected
    assert "ask_to_target_resolution" in airtable_selected
    assert "artifact_evidence_handling" in airtable_selected
    assert "data_schema_mapping" in airtable_selected

    skill_text = _read_skill("ask_to_target_resolution")
    assert "Airtable Finance Receipt Mapping" in skill_text
    assert "Infer `base_alias=\"finance_tax_tracker\"`" in skill_text
    assert "Business Expenses" in skill_text
    assert "Do not block by asking for base/table" in skill_text

    mapping_text = _read_skill("data_schema_mapping")
    assert "Read the input evidence before proposing field values" in mapping_text
    assert "Use exact destination field names" in mapping_text
    assert "model-extracted source facts separately from\n  exact target field values" in mapping_text


def test_artifact_evidence_skill_selected_for_local_file_requests() -> None:
    request = "Read the attached invoice image at /tmp/vendor-receipt.png and update the record."

    for agent_name in (
        "chief_of_staff",
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
        "gmail_triage",
    ):
        assert "artifact_evidence_handling" in select_agent_skill_names(
            agent_name,
            request_text=request,
        )

    skill_text = _read_skill("artifact_evidence_handling")
    assert "Do not infer values from\n   filename" in skill_text
    assert "Use operator-supplied local files, PDFs, images" in skill_text
    assert "Must not send, publish, upload, attach, or share private artifacts" in skill_text


def test_data_schema_mapping_skill_selected_for_mapping_and_field_requests() -> None:
    request = (
        "Map the attached invoice PDF to the Airtable expense schema, infer the "
        "estimated tax period, and populate the matching fields."
    )

    for agent_name in (
        "chief_of_staff",
        "airtable_context_agent",
        "google_workspace_context_agent",
        "gmail_triage",
        "business_research_analyst",
    ):
        assert "data_schema_mapping" in select_agent_skill_names(
            agent_name,
            request_text=request,
        )

    skill_text = _read_skill("data_schema_mapping")
    assert "schema-read, model-map, helper-validate, then bounded-write" in skill_text
    assert "Must not encode one-off natural-language shortcuts" in skill_text


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


def test_operating_architecture_resolves_noncritical_uncertainty_before_blocking() -> None:
    text = _read_prompt("agent-operating-architecture.md")
    normalized = " ".join(text.split())

    assert "use an uncertainty-resolution ladder before blocking" in normalized
    assert "apply configured account, calendar, timezone" in normalized
    assert "ask one targeted question only if the remaining alternatives would materially change" in normalized
    assert "Do not turn every omitted optional field into a blocker" in normalized


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


def test_compact_outreach_builder_preserves_request_aware_skill_visibility() -> None:
    from keystone_agents.agents.outreach_composer import (
        build_outreach_composer_compact_synthesis_agent,
    )

    default_instructions = str(build_outreach_composer_compact_synthesis_agent().instructions)
    request_instructions = str(
        build_outreach_composer_compact_synthesis_agent(
            request_text=(
                "prepare a source-cited outreach draft and save report to Google Drive "
                "with missing evidence gaps"
            )
        ).instructions
    )

    assert "<!-- outreach_composer.md -->" in request_instructions
    assert "<!-- tools.md -->" not in request_instructions
    assert "<!-- action_boundary_enforcement/SKILL.md -->" in request_instructions
    assert "<!-- context_permission_gating/SKILL.md -->" in request_instructions
    assert "<!-- workspace_artifact_governance/SKILL.md -->" not in default_instructions
    assert "<!-- workspace_artifact_governance/SKILL.md -->" not in request_instructions
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
    assert "`airtable_create_expense_from_receipt`" in text
    assert "`airtable_write_record`" in text
    assert "`airtable_upload_attachment`" in text
    assert (
        "No ordinary deletes, schema changes, generic attachment uploads, bulk "
        "overwrites, or silent mutations"
        in text
    )
    assert "## Direct Write Execution Semantics" in text
    assert "authenticated operator\ncommand as approval" in text
    assert "Operator approval is operation-specific, not global" in text
    assert "call the relevant typed write tool with `live=true`" in text
    assert "nested context agent or agent-as-tool" in text
    assert "`airtable_link_attachment` for credential-free HTTPS receipt URLs" in text
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


def test_context_agent_prompts_require_live_read_tools_for_live_read_only_invocation() -> None:
    airtable = _read_prompt("airtable_context.md")
    workspace = _read_prompt("google_workspace_context.md")
    zotero = _read_prompt("zotero_context.md")

    assert "directly invoked in live SDK mode for a read-only lookup" in airtable
    assert "airtable_get_base_schema" in airtable
    assert "live=true" in airtable
    assert "directly invoked in live SDK mode for a read-only lookup" in workspace
    assert "tool's live-read equivalent" in workspace
    assert "directly invoked in live SDK mode for a read-only lookup" in zotero
    assert "one KNI collection" in zotero
    assert "KNI foundational texts/reviews" in zotero
    assert "KNI 00 - Foundational Texts & Reviews" in zotero
    assert "zotero_resolve_collection_context" in zotero
    assert "Do not import or mutate Zotero" in zotero


def test_workspace_prompt_uses_direct_scoped_write_authority() -> None:
    text = _read_prompt("google_workspace_context.md")

    assert "authenticated operator command" in text
    assert "scoped approval for only that" in text
    assert "action. Call the matching typed tool with `live=true`" in text
    assert "Nested/advisory calls" in text


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
    normalized = " ".join(text.split())

    assert "preserve that exact value for downstream execution" in text
    assert "Google Workspace Context or the\napproved Workspace action handler" in text
    assert "include actual analysis, not just data transfer" in normalized
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


def test_chief_of_staff_prompt_handles_business_expense_receipt_periods() -> None:
    text = _read_prompt("chief_of_staff.md")
    normalized = " ".join(text.split())

    assert "Map explicit business-expense requests to `Business Expenses`" in normalized
    assert "explicit personal-expense requests to `Personal Expenses`" in normalized
    assert "Reason from the receipt date and the tracker period rules" in normalized
    assert "do not use the current calendar date" in normalized
    assert "`airtable_create_expense_from_receipt` for the final approved" in normalized
    assert "lower-level record-write plus attachment operations only when the bounded receipt tool" in normalized


def test_chief_of_staff_prompt_keeps_web_briefs_substantive() -> None:
    text = _read_prompt("chief_of_staff.md")
    normalized = " ".join(text.split())

    assert "For broad web questions or deepened search briefs" in text
    assert "answer the substantive\n  user question first" in text
    assert "Do not make the main answer a provider diagnostic" in text
    assert "keep provider/lane details for trailing metadata only" in normalized
    assert "Do not choose `reference-capture` for a question, brief, search, research" in text


def test_chief_of_staff_prompt_defines_local_kni_document_guardrails() -> None:
    text = _read_prompt("chief_of_staff.md")
    normalized = " ".join(text.split())

    assert "local-source Keystone Neuroinformatics document" in text
    assert "model_context_allowed=true" in text
    assert "bank/payment account details" in normalized
    assert "PHI or patient identifiers" in text
    assert "Legal, contract, finance, tax, insurance" in normalized
    assert "must not be presented as legal, tax, insurance, or coverage advice" in normalized
    assert "not approval to send, post, publish, submit, or share externally" in normalized


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


def test_agent_capability_boundaries_define_orchestrator_and_chief_rwm() -> None:
    text = _read_repo_doc("docs/AGENT_CAPABILITY_BOUNDARIES.md")

    assert "This note is the repo-local contract for ANU-193, ANU-194, and ANU-124" in text
    assert "## Orchestrator R/W/M" in text
    assert "preflight route advice, missing-context blockers" in text
    assert "revise route advice and specialist briefs" in text
    assert "## Chief Of Staff R/W/M" in text
    assert "manager workflow plans with objective, selected specialists" in text
    assert "`durable_handoff.agent` for the specialist" in text
    assert "`context_handoffs` for read-only context agents" in text
    assert "## Agents-As-Tools Boundary" in text
    assert "Nested specialist calls must" in text
    assert "advisory context to Chief" in text
    assert "## Durable Graph Handoff Boundary" in text
    assert "Durable graph handoffs are WorkItem state transitions" in text
    assert "## Backend Graph Selector Boundary" in text
    assert "It does not own business routing, provider permissions" in text
    assert "## Unresolved Architecture Decisions" in text
    assert "## Test Acceptance Criteria" in text


def test_control_plane_runtime_skills_point_to_capability_boundaries() -> None:
    orchestrator = _read_repo_doc(
        "src/keystone_agents/skills/orchestrator_specialist_contracts/SKILL.md"
    )
    chief = _read_repo_doc(
        "src/keystone_agents/skills/chief_of_staff_specialist_contracts/SKILL.md"
    )

    assert "`docs/AGENT_CAPABILITY_BOUNDARIES.md`" in orchestrator
    assert "write planning/review metadata" in orchestrator
    assert "WorkItem/storage layer records them" in orchestrator
    assert "`docs/AGENT_CAPABILITY_BOUNDARIES.md`" in chief
    assert "write internal\n  manager plans/handoffs" in chief
    assert "agents-as-tools nested specialist result" in chief
    assert "structured `durable_handoff.agent`" in chief


def test_major_milestones_include_rwm_acceptance_criteria() -> None:
    backlog = _read_repo_doc("LINEAR_BACKLOG.MD")
    goals = _read_repo_doc("docs/AGENT_GOALS.md")

    assert "Milestone definition:" in backlog
    assert "`docs/AGENT_CAPABILITY_BOUNDARIES.md` now defines" in backlog
    assert "Whether Chief needs a dedicated first-class `ManagerWorkflowPlan`" in backlog
    assert "`ANU-193`: route correction updates route plans/review notes" in backlog
    assert "`ANU-194`: Chief broad asks produce internal plans" in backlog
    assert "`ANU-124`: broad/project/ops/cross-agent asks default to Chief" in backlog
    assert "ANU-193 is the Orchestrator read/write/modify contract" in goals
    assert "ANU-124 and ANU-194 make Chief of Staff" in goals
    assert "backend graph selection belongs to WorkItem execution\npolicy" in goals


def test_anu60_live_slack_proof_plan_preserves_acceptance_boundary() -> None:
    proof_plan = _read_repo_doc("docs/ANU60_LIVE_SLACK_PROOF_PLAN.md")
    evidence_template = _read_repo_doc("docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md")
    normalized_proof_plan = " ".join(proof_plan.split())
    index = _read_repo_doc("docs/INDEX.md")
    backlog = _read_repo_doc("docs/ORCHESTRATOR_BRIDGE_BACKLOG.md")
    linear_backlog = _read_repo_doc("LINEAR_BACKLOG.MD")
    workflow_status = _read_repo_doc("docs/AI_AGENTS_WORKFLOW_TEST_STATUS.md")
    package = json.loads(_read_repo_doc("package.json"))

    assert "explicit approval for live Slack posting" in proof_plan
    assert "The strict readiness harness validates the local `#evals` flow" in proof_plan
    assert "ANU-60 acceptance still needs visible output proof in `#ai-agents-workflow`" in proof_plan
    assert "Do not treat the `#evals` readiness pass as the final ANU-60 proof" in proof_plan
    assert "npm run eval:slack:strict-readiness -- --json" in proof_plan
    assert "npm run eval:slack:strict-live-readiness -- --json" in proof_plan
    assert "../keystone-slack/scripts/manage_slack_socket.sh status" in proof_plan
    assert "## Current No-Live Evidence" in proof_plan
    assert "tests/test_prompt_contracts.py\n  tests/test_slack_action_contract.py -q" in proof_plan
    assert "111 passed" in proof_plan
    assert "validate_slack_bridge_contract.py" in proof_plan
    assert "validate_slack_result_rendering_examples.py" in proof_plan
    assert "npm run eval:slack:anu60-proof" in proof_plan
    assert "npm run eval:slack:anu60-preflight" in proof_plan
    assert "acceptance map, and doc-contract guard were added" in proof_plan
    assert "artifacts/anu60_expansion_gate_after_acceptance_map.json" in proof_plan
    assert "36/36" in proof_plan
    assert "sibling `keystone-slack`" in proof_plan
    assert "focused no-live bridge suite passed `8`" in proof_plan
    assert "focused sibling timeout fixture pair below also passed independently" in proof_plan
    assert "(`2` tests)" in proof_plan
    assert "It still does not prove the live visible Slack output" in proof_plan

    for probe_marker in (
        "slack_rss_context_announcement_history_001",
        "slack_preprints_context_preliminary_evidence_001",
        "Suki AI",
        "Nabla",
        "slack_gmail_missing_thread_identity_001",
    ):
        assert probe_marker in proof_plan

    for approval_boundary in (
        "No external sends, writes, drafts, schedules, file creation",
        "Stop after the first failed visible render",
        "No Gmail draft is created and no email is sent",
        "Expected: no",
    ):
        assert approval_boundary in proof_plan

    for evidence_field in (
        "Slack permalink",
        "Local run id or WorkItem id",
        "Route and output type",
        "external write/send/draft/feed-refresh",
        "Visible body starts with answer-first human_summary",
        "Provider/model/timing metadata appears before answer",
        "Required source URLs or source-limit language present",
        "External write/send/draft/feed-refresh observed",
    ):
        assert evidence_field in proof_plan

    assert "`scripts/sync_slack_eval_thread.py` is a read-only importer" in proof_plan
    assert "do not use a saved `#evals` row as a substitute" in normalized_proof_plan
    assert "answer-first `human_summary`" in proof_plan
    assert "No provider/model/timing/retrieval metadata appears before the answer" in proof_plan
    assert "Gmail Triage needs email context" in proof_plan
    assert "## Acceptance Coverage Map" in proof_plan
    assert "Direct Business Research Slack probes render the KBA answer first" in proof_plan
    assert "Conversational Business Research Slack probes render the KBA answer first" in proof_plan
    assert "RSS context-agent Slack probes display `RssContextResult` summaries" in proof_plan
    assert "Preprints context-agent Slack probes display `PreprintsContextResult` summaries" in proof_plan
    assert "Blocked preflights render accurate, redacted, actionable statuses" in proof_plan
    assert "Timeouts render accurate, redacted, actionable statuses" in proof_plan
    assert "test_business_agents_run_command_timeout_returns_structured_failure_and_kills_group" in proof_plan
    assert "test_business_agents_streaming_run_command_timeout_kills_group" in proof_plan
    assert "record this as fixture-backed proof, not live Slack acceptance evidence" in proof_plan
    assert "ANU-60 can move to Done only when the live evidence above is captured" in proof_plan
    assert "`docs/ANU60_LIVE_SLACK_PROOF_PLAN.md`" in index
    assert "`docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md`" in index
    assert "`docs/ANU60_LIVE_SLACK_PROOF_PLAN.md`" in backlog
    assert "`docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md` as the capture form" in backlog
    assert "Current ANU-60 handoff: `docs/ANU60_LIVE_SLACK_PROOF_PLAN.md`, with" in linear_backlog
    assert "`docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md` as the capture form" in linear_backlog
    assert "ANU-60 should remain short of Done" in linear_backlog
    assert "Use `docs/ANU60_LIVE_SLACK_PROOF_PLAN.md` as\n" "the current handoff packet" in workflow_status
    assert "`docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md`" in workflow_status
    assert "the capture form before any additional `#ai-agents-workflow` live probe" in workflow_status
    assert "npm run eval:slack:anu60-proof" in workflow_status
    assert "npm run eval:slack:anu60-preflight" in workflow_status
    assert "ANU-60 is complete through the no-live/pre-live boundary" in workflow_status
    assert "Status: pre-live complete; fixed locally in sibling `keystone-slack`" in backlog
    assert "do not move ANU-60 to Done until fresh live" in workflow_status
    assert (
        package["scripts"]["eval:slack:anu60-proof"]
        == ".venv/bin/python scripts/validate_anu60_live_slack_proof_packet.py"
    )
    assert (
        package["scripts"]["eval:slack:anu60-preflight"]
        == ".venv/bin/python scripts/run_anu60_no_live_preflight.py"
    )

    assert "`docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md`" in proof_plan
    assert "Do not fill this from\n`#evals` rows alone" in evidence_template
    for section in (
        "## Run Boundary",
        "### RSS Context",
        "### Preprints Context",
        "### Direct Business Research",
        "### Conversational Business Research",
        "### Gmail Missing Context",
        "### Timeout / Failure Fixture Boundary",
        "## Acceptance Coverage Map",
        "## Completion Summary",
    ):
        assert section in evidence_template
    for required_field in (
        "Slack permalink:",
        "Local run id or WorkItem id:",
        "Visible body starts with answer-first `human_summary`: yes/no",
        "Provider/model/timing metadata appears before answer: yes/no",
        "External write/send/draft/feed-refresh observed: yes/no",
        "Visible body says `Gmail Triage needs email context`: yes/no",
        "Failure output is redacted and actionable: yes/no",
        "Timeout/failure done criterion covered by fixture or approved live probe: yes/no",
        "test_business_agents_run_command_timeout_returns_structured_failure_and_kills_group",
        "test_business_agents_streaming_run_command_timeout_kills_group",
        "All required probes passed: yes/no",
        "Recommendation for ANU-60 state:",
    ):
        assert required_field in evidence_template


def test_anu60_live_slack_proof_packet_validator_passes() -> None:
    from scripts.validate_anu60_live_slack_proof_packet import (
        validate_anu60_live_slack_proof_packet,
    )

    evidence = Path("artifacts/anu60_expansion_gate_after_acceptance_map.json")
    if not evidence.is_file():
        pytest.skip("generated ANU-60 acceptance-map evidence is unavailable in clean CI")
    assert validate_anu60_live_slack_proof_packet() == []


def test_shared_slack_rules_require_visible_source_urls() -> None:
    text = _read_prompt("slack-posting-rules.md")

    assert "When source-backed claims are present, include primary source links" in text
    assert "Put source URLs in the\n  first user-visible answer" in text
    assert "structured `sources`, hidden metadata, artifacts" in text
    assert "Detailed Summary` must\n  start with a narrative summary paragraph" in text
    assert "A source list, provider-result list, or bullet list is not a substitute" in text
    assert "more useful than a generic web-search answer" in text


def test_file_search_prompt_contract_separates_reference_local_and_live_sources() -> None:
    tools = _read_prompt("tools.md")
    research = _read_prompt("business_research_analyst.md")
    orchestrator = _read_prompt("orchestrator.md")
    normalized_research = " ".join(research.split())
    normalized_orchestrator = " ".join(orchestrator.split())

    assert ".local/file-search-vector-stores.json" in tools
    assert "Hosted FileSearch is not local KNI document search and is not fresh web search" in tools
    assert "Use local KNI document tools for Keystone Neuroinformatics folder evidence" in tools
    assert "use `search_web` for current public facts" in tools
    assert "when configured by the harness or local FileSearch config" in normalized_research
    assert "not treat it as a substitute for source-backed company research" in normalized_research
    assert "Use hosted `file_search`, when configured, only for stable approved reference" in orchestrator
    assert "not a substitute for current public web search" in normalized_orchestrator
    assert "route to Chief of Staff and preserve the local-doc requirement" in normalized_orchestrator
    assert (
        "Do not replace local KNI evidence with hosted FileSearch or generic web search"
        in normalized_orchestrator
    )


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
    assert "Approval scope must remain `external_use`" in text
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
