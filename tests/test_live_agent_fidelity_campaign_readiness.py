"""Offline admission gate for the 20-run Slack agent-fidelity campaign.

The prompts are intentionally novel and human-written. This file proves the
plannerless deterministic preflight, direct agent ownership, and exact attached
tool surface before any paid Slack run is allowed to use them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import pytest

from keystone_agents.agent_tool_policy import tool_name_for_policy
from keystone_agents.agents.airtable_context import build_airtable_context_agent
from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_agent,
)
from keystone_agents.agents.chief_of_staff import build_chief_of_staff_agent
from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
from keystone_agents.agents.google_workspace_context import (
    build_google_workspace_context_agent,
)
from keystone_agents.agents.opportunity_scout import build_opportunity_scout_agent
from keystone_agents.agents.orchestrator import (
    build_orchestrator_agent,
    run_orchestrator_preflight,
)
from keystone_agents.agents.outreach_composer import build_outreach_composer_agent
from keystone_agents.agents.preprints_context import build_preprints_context_agent
from keystone_agents.agents.rag_retrieval_specialist import (
    build_rag_retrieval_specialist_agent,
)
from keystone_agents.agents.rss_context import build_rss_context_agent
from keystone_agents.agents.zotero_context import build_zotero_context_agent
from keystone_agents.capabilities.tool_scope import tool_scope_receipt_for_agent
from keystone_agents.entrypoints import cli_impl
from keystone_agents.runtime.tool_execution import evaluate_tool_execution_contract

Builder = Callable[..., Any]


@pytest.fixture(autouse=True)
def synthetic_rag_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Construct a hosted tool for admission inspection; never query a real corpus."""

    monkeypatch.setenv(
        "KEYSTONE_RAG_RETRIEVAL_SPECIALIST_FILE_SEARCH_VECTOR_STORE_IDS",
        "vs_offline_matrix",
    )


@dataclass(frozen=True)
class LiveCampaignCase:
    case_id: str
    route: str
    prompt: str
    builder: Builder
    plan_parameter: str
    expected_target: str
    expected_intent: str
    required_tools: frozenset[str]
    forbidden_tools: frozenset[str] = frozenset()
    builder_kwargs: tuple[tuple[str, Any], ...] = ()


_WRITE_TOOLS = frozenset(
    {
        "airtable_write_record",
        "airtable_link_attachment",
        "airtable_upload_attachment",
        "airtable_create_expense_from_receipt",
        "create_gmail_draft_reply",
        "create_gmail_draft_with_attachment",
        "modify_gmail_message_state",
        "apply_gmail_labels",
        "send_gmail_test_draft",
        "google_doc_write",
        "google_doc_trash",
        "google_drive_create_folder",
        "google_drive_rename_folder",
        "google_drive_remove_folder",
        "google_slide_deck_write",
        "google_sheet_create",
        "google_sheet_append_rows",
        "google_sheet_update_row",
        "google_sheet_delete_rows",
        "google_sheet_create_tab",
        "google_sheet_update_tab",
        "google_sheet_remove_tab",
        "google_sheet_trash",
        "zotero_import_article_with_backend",
        "zotero_write_test_note",
        "zotero_delete_test_note",
        "zotero_test_note_lifecycle",
        "zotero_write_test_collection",
        "zotero_delete_test_collection",
        "zotero_write_test_item",
        "zotero_delete_test_item",
        "prepare_signal_lifecycle_checkpoint",
        "advance_signal_lifecycle_checkpoint",
    }
)


CASES = (
    LiveCampaignCase(
        "T01",
        "gmail_triage",
        (
            "Could you inspect the Gmail integration's available mailbox schema and explain "
            "which field names are safe for a future date-bounded search? Return only field "
            "names and their purpose. Do not query or read messages, and do not return any "
            "mailbox-derived data, senders, subjects, snippets, or body text."
        ),
        build_gmail_triage_agent,
        "manual_request_plan",
        "gmail_triage",
        "gmail_triage",
        frozenset({"inspect_gmail_mailbox_schema"}),
        _WRITE_TOOLS,
    ),
    LiveCampaignCase(
        "T02",
        "business_research_analyst",
        (
            "Can you get me up to speed on Spring Health's current employer mental-health "
            "platform? I need one independently checkable outcome or evidence signal and "
            "one recent customer or payer adoption signal, with direct URLs and company "
            "claims labeled as such. If the first search source is unavailable or thin, "
            "use another approved source before saying nothing was found. Read-only; "
            "don't save research or find contacts."
        ),
        build_business_research_analyst_agent,
        "manual_request_plan",
        "business_research_analyst",
        "company_research",
        frozenset({"search_web", "check_workflow_duplicate"}),
        _WRITE_TOOLS,
    ),
    LiveCampaignCase(
        "T03",
        "opportunity_scout",
        (
            "Could you look for two open U.S. funding or pilot opportunities in 2026 "
            "that a small behavioral-health analytics company could realistically pursue, "
            "ideally involving measurement-based care or clinical data infrastructure? "
            "Confirm eligibility and deadline from official pages and include the links. "
            "Don't save anything or draft an application."
        ),
        build_opportunity_scout_agent,
        "manual_request_plan",
        "opportunity_scout",
        "opportunity_search",
        frozenset({"search_web", "check_workflow_duplicate"}),
        _WRITE_TOOLS,
    ),
    LiveCampaignCase(
        "T04",
        "outreach_composer",
        (
            "We already have a reviewed Deliberate AI profile in our records. Please use "
            "only approved claims and whatever approved contact context is available to "
            "write a warm, concise introduction for internal review about a possible "
            "behavioral-health AI collaboration. If no named contact is approved, address "
            "the organization generally and say so once. Keep it here: no Gmail draft, "
            "tracking update, post, or send."
        ),
        build_outreach_composer_agent,
        "manual_request_plan",
        "outreach_composer",
        "outreach_draft",
        frozenset({"load_approved_contact_context", "check_unsupported_claims"}),
        _WRITE_TOOLS,
    ),
    LiveCampaignCase(
        "T05",
        "airtable_context_agent",
        (
            "In the finance base, could you tell me how many reimbursable expenses were "
            "recorded in July 2026 and their total amount, broken out by category? Please "
            "check the table shape first and only read records; don't create, update, "
            "attach, or delete anything."
        ),
        build_airtable_context_agent,
        "manual_plan",
        "airtable_context_agent",
        "context_lookup",
        frozenset(
            {
                "airtable_get_base_schema",
                "airtable_read_schema_detail",
                "airtable_read_records",
                "airtable_aggregate_records",
            }
        ),
        _WRITE_TOOLS,
    ),
    LiveCampaignCase(
        "T06",
        "google_workspace_context_agent",
        (
            "Could you find the most recently modified PDF in the Drive folder used for "
            "Keystone operations and read just its first page? Tell me the file name, "
            "modified time, and a three-sentence summary, noting if OCR or extraction is "
            "incomplete. Please don't edit, move, share, or create anything."
        ),
        build_google_workspace_context_agent,
        "manual_plan",
        "google_workspace_context_agent",
        "context_lookup",
        frozenset(
            {
                "google_drive_search_files",
                "google_drive_get_file_metadata",
                "google_drive_media_ocr_read",
            }
        ),
        _WRITE_TOOLS,
        (("tool_tier", "internal_write"),),
    ),
    LiveCampaignCase(
        "T07",
        "zotero_context_agent",
        (
            "In Zotero, find the two newest saved items about digital biomarkers for "
            "psychosis relapse and give me title, authors, year, DOI or URL, and whether "
            "an abstract is available. Please only read the library: no notes, imports, "
            "edits, or deletes."
        ),
        build_zotero_context_agent,
        "manual_plan",
        "zotero_context_agent",
        "context_lookup",
        frozenset({"zotero_resolve_article_context", "zotero_read_api_metadata"}),
        _WRITE_TOOLS,
        (("tool_tier", "internal_write"),),
    ),
    LiveCampaignCase(
        "T08",
        "rss_context_agent",
        (
            "Before I start today's reading, can you compare the saved RSS checkpoint "
            "with the unreviewed feed history and tell me whether anything new mentions "
            "Medicaid behavioral-health quality measures? Give the checkpoint age, unseen "
            "count, and at most two newest matches with dates and links. Leave the feed "
            "and checkpoint untouched."
        ),
        build_rss_context_agent,
        "manual_plan",
        "rss_context_agent",
        "context_lookup",
        frozenset({"retrieve_rss_announcement_history", "inspect_signal_lifecycle"}),
        _WRITE_TOOLS,
    ),
    LiveCampaignCase(
        "T09",
        "preprints_context_agent",
        (
            "Could you compare the saved preprint checkpoint with unseen items about "
            "speech or smartphone signals for depression relapse? Show the unseen count "
            "and up to two newest relevant papers, with repository, version date, and "
            "link, and label them preliminary. Don't prepare or advance a checkpoint."
        ),
        build_preprints_context_agent,
        "manual_plan",
        "preprints_context_agent",
        "context_lookup",
        frozenset(
            {"retrieve_preprint_announcement_history", "inspect_signal_lifecycle"}
        ),
        _WRITE_TOOLS,
    ),
    LiveCampaignCase(
        "T10",
        "orchestrator",
        (
            "I'm looking at WorkItem wi_506f354a486244ffb079f653759182b9. Before anyone "
            "touches it, tell me what receipt-backed stage is complete, what must not be "
            "repeated, whether an approval is pending, and the safest exact resume point. "
            "Status only: do not call a specialist or change the WorkItem."
        ),
        build_orchestrator_agent,
        "manual_request_plan",
        "orchestrator",
        "continue_work_item",
        frozenset({"inspect_work_item_execution_receipts"}),
        _WRITE_TOOLS | frozenset({"search_web"}),
        (("include_handoffs", False),),
    ),
    LiveCampaignCase(
        "T11",
        "chief_of_staff",
        (
            "Could you review the verified active WorkItems and show me the three that "
            "have been waiting longest since their last proven stage? For each, give route, "
            "last verified stage, blockers, approval queue status, and safe resume "
            "disposition, then pick the least risky one to revisit. Inspection only: do "
            "not resume, approve, rerun, or change anything."
        ),
        build_chief_of_staff_agent,
        "manual_request_plan",
        "chief_of_staff",
        "continue_work_item",
        frozenset({"inspect_active_work_item_execution_summary"}),
        _WRITE_TOOLS,
    ),
    LiveCampaignCase(
        "T12",
        "business_research_analyst",
        (
            "Please investigate Little Otter's current pediatric mental-health model and "
            "look for one payer or employer adoption signal and one outcome or evidence "
            "signal. Start with Exa for semantic coverage, then use another approved search "
            "provider if the evidence is incomplete. Show the URLs, separate company claims "
            "from independent evidence, and keep this read-only."
        ),
        build_business_research_analyst_agent,
        "manual_request_plan",
        "business_research_analyst",
        "company_research",
        frozenset({"search_web", "check_workflow_duplicate"}),
        _WRITE_TOOLS,
    ),
    LiveCampaignCase(
        "T13",
        "business_research_analyst",
        (
            "Can you assess Osmind's current platform for psychiatry practices? Find one "
            "public adoption signal and one independently supported evidence or outcome "
            "signal, with direct source URLs. If one search route fails, try a different "
            "approved provider before returning a limitation. Don't save a profile or look "
            "for contacts."
        ),
        build_business_research_analyst_agent,
        "manual_request_plan",
        "business_research_analyst",
        "company_research",
        frozenset({"search_web", "check_workflow_duplicate"}),
        _WRITE_TOOLS,
    ),
    LiveCampaignCase(
        "T14",
        "opportunity_scout",
        (
            "Please find two currently open NIMH or NSF SBIR/STTR opportunities relevant "
            "to AI-assisted measurement-based mental-health care. Verify phase eligibility "
            "and the deadline from the official notice, include the links, and explain any "
            "small-business fit caveat. Don't save a lead or draft outreach."
        ),
        build_opportunity_scout_agent,
        "manual_request_plan",
        "opportunity_scout",
        "opportunity_search",
        frozenset({"search_web", "check_workflow_duplicate"}),
        _WRITE_TOOLS,
    ),
    LiveCampaignCase(
        "T15",
        "gmail_triage",
        (
            "Could you inspect the Gmail connector schema and tell me whether it exposes a "
            "stable thread identifier and a mailbox-category field? Give only the exact field "
            "names and types. Do not query or read messages, and do not include mailbox data, "
            "addresses, subjects, snippets, or body text."
        ),
        build_gmail_triage_agent,
        "manual_request_plan",
        "gmail_triage",
        "gmail_triage",
        frozenset({"inspect_gmail_mailbox_schema"}),
        _WRITE_TOOLS,
    ),
    LiveCampaignCase(
        "T16",
        "airtable_context_agent",
        (
            "In the Partner Leads table, could you count active leads by stage and identify "
            "records missing either an owner or a next-step date? Read the schema first and "
            "return only record names and the missing-field reason. Please don't modify, "
            "create, attach, or delete anything."
        ),
        build_airtable_context_agent,
        "manual_plan",
        "airtable_context_agent",
        "context_lookup",
        frozenset(
            {
                "airtable_get_base_schema",
                "airtable_read_schema_detail",
                "airtable_read_records",
                "airtable_aggregate_records",
            }
        ),
        _WRITE_TOOLS,
    ),
    LiveCampaignCase(
        "T17",
        "google_workspace_context_agent",
        (
            "Please find the most recently modified Drive spreadsheet whose title includes "
            "cash flow or runway. Read its visible table headers and newest non-empty row, "
            "then report the file owner and modified time. Keep this read-only: don't edit, "
            "append, move, share, or create anything."
        ),
        build_google_workspace_context_agent,
        "manual_plan",
        "google_workspace_context_agent",
        "context_lookup",
        frozenset(
            {
                "google_drive_search_files",
                "google_drive_get_file_metadata",
                "google_sheet_list",
                "google_sheet_read_table",
            }
        ),
        _WRITE_TOOLS,
        (("tool_tier", "internal_write"),),
    ),
    LiveCampaignCase(
        "T18",
        "zotero_context_agent",
        (
            "Could you find up to three saved Zotero articles from 2024 onward on ecological "
            "momentary assessment for bipolar disorder? Give title, year, abstract status, "
            "tags, collection location, and DOI or URL. Only inspect the library; don't "
            "import, annotate, edit, move, or delete anything."
        ),
        build_zotero_context_agent,
        "manual_plan",
        "zotero_context_agent",
        "context_lookup",
        frozenset({"zotero_resolve_article_context", "zotero_read_api_metadata"}),
        _WRITE_TOOLS,
        (("tool_tier", "internal_write"),),
    ),
    LiveCampaignCase(
        "T19",
        "outreach_composer",
        (
            "Using only approved records for Deliberate AI, turn the existing collaboration "
            "idea into a LinkedIn note of no more than 90 words for the organization account. "
            "If approved evidence or contact context is missing, state that once and keep the "
            "copy appropriately general. Return it for review only: don't post, save, create "
            "a task, update tracking, or send anything."
        ),
        build_outreach_composer_agent,
        "manual_request_plan",
        "outreach_composer",
        "outreach_draft",
        frozenset({"load_approved_contact_context", "check_unsupported_claims"}),
        _WRITE_TOOLS,
    ),
    LiveCampaignCase(
        "T20",
        "orchestrator",
        (
            "I need an evidence-backed vendor landscape and, after review, perhaps a short "
            "partnership note. I do not know which specialist should own the first step, and "
            "there is no selected company or recipient. Give me the safe order and the first "
            "clarification you need. Do not run tools, create artifacts, or draft anything."
        ),
        build_orchestrator_agent,
        "manual_request_plan",
        "clarification",
        "clarification",
        frozenset({"route_request_placeholder"}),
        _WRITE_TOOLS | frozenset({"search_web"}),
        (("include_handoffs", False),),
    ),
)


# The original 20 prompts retain their historical campaign scope. Supplements
# are offline admission cases only, not additional authorized or completed live runs.
CAMPAIGN_ROUTES = frozenset({
    "gmail_triage",
    "business_research_analyst",
    "opportunity_scout",
    "outreach_composer",
    "airtable_context_agent",
    "google_workspace_context_agent",
    "zotero_context_agent",
    "rss_context_agent",
    "preprints_context_agent",
    "orchestrator",
    "chief_of_staff",
})
OFFLINE_SUPPLEMENTAL_CASES = (
    LiveCampaignCase(
        "T-OFFLINE-RAG",
        "rag_retrieval_specialist",
        (
            "Could you find the closest articles in our configured vector store about clinician "
            "trust in clinical AI? Explain relevance from retrieved passages, flag missing "
            "evidence, and keep the corpus unchanged."
        ),
        build_rag_retrieval_specialist_agent,
        "manual_request_plan",
        "rag_retrieval_specialist",
        "rag_retrieval",
        frozenset({"file_search"}),
        _WRITE_TOOLS | {"search_web"},
    ),
)
ALL_OFFLINE_CASES = CASES + OFFLINE_SUPPLEMENTAL_CASES


@pytest.mark.parametrize("case", ALL_OFFLINE_CASES, ids=lambda case: case.case_id)
def test_live_campaign_prompt_has_safe_plannerless_tool_admission(
    case: LiveCampaignCase,
) -> None:
    preflight = run_orchestrator_preflight(
        case.prompt,
        requested_agent=case.route,
        live_manual_plan=False,
    )
    plan = preflight.manual_request_plan
    kwargs = dict(case.builder_kwargs)
    kwargs.update({"request_text": case.prompt, case.plan_parameter: plan})
    agent = case.builder(**kwargs)
    names = [tool_name_for_policy(tool) for tool in agent.tools]
    selected = set(names)

    assert preflight.sdk_usage_events == []
    assert plan.source == "heuristic"
    assert plan.target_agent == case.expected_target
    assert plan.intent == case.expected_intent
    assert case.required_tools <= selected
    assert case.forbidden_tools.isdisjoint(selected)
    assert names
    assert len(names) == len(selected)


def test_campaign_and_offline_supplement_cover_every_registered_agent() -> None:
    from keystone_agents.agent_registry import list_agent_specs

    registered = {spec.route_name for spec in list_agent_specs()}
    covered = {case.route for case in CASES}

    assert len(CASES) == 20
    assert covered == CAMPAIGN_ROUTES
    assert {case.route for case in ALL_OFFLINE_CASES} == registered
    assert all(sum(case.route == route for case in CASES) >= 1 for route in CAMPAIGN_ROUTES)
    assert {case.case_id for case in CASES}.isdisjoint(
        case.case_id for case in OFFLINE_SUPPLEMENTAL_CASES
    )


_EXPECTED_CONTEXT_EXECUTION_TOOLS = {
    "T01": {"inspect_gmail_mailbox_schema"},
    "T05": {"airtable_get_base_schema", "airtable_aggregate_records"},
    "T06": {
        "google_drive_search_files",
        "google_drive_get_file_metadata",
        "google_drive_media_ocr_read",
    },
    "T07": {"zotero_resolve_article_context", "zotero_read_api_metadata"},
    "T08": {"retrieve_rss_announcement_history", "inspect_signal_lifecycle"},
    "T09": {"retrieve_preprint_announcement_history", "inspect_signal_lifecycle"},
    "T15": {"inspect_gmail_mailbox_schema"},
    "T16": {
        "airtable_get_base_schema",
        "airtable_read_records",
        "airtable_aggregate_records",
    },
    "T17": {
        "google_drive_search_files",
        "google_drive_get_file_metadata",
        "google_sheet_read_table",
    },
    "T18": {"zotero_resolve_article_context", "zotero_read_api_metadata"},
}


@pytest.mark.parametrize(
    "case",
    [case for case in CASES if case.case_id in _EXPECTED_CONTEXT_EXECUTION_TOOLS],
    ids=lambda case: case.case_id,
)
def test_live_context_prompt_requires_exact_successful_functional_tool_groups(
    case: LiveCampaignCase,
) -> None:
    preflight = run_orchestrator_preflight(
        case.prompt,
        requested_agent=case.route,
        live_manual_plan=False,
    )
    plan = preflight.manual_request_plan
    kwargs = dict(case.builder_kwargs)
    kwargs.update({"request_text": case.prompt, case.plan_parameter: plan})
    agent = case.builder(**kwargs)
    attached = [tool_name_for_policy(tool) for tool in agent.tools]
    scope_receipt = tool_scope_receipt_for_agent(agent)
    selected = [
        str(name)
        for name in scope_receipt.get("selected_tool_names", [])
        if str(name).strip()
    ]
    assert selected == attached
    assert scope_receipt["selected_tool_count"] == len(attached)
    contract = cli_impl._context_agent_tool_execution_contract(
        case.route,
        input_text=case.prompt,
        selected_tool_names=selected,
        manual_plan=plan,
    )

    assert contract is not None
    required = {
        tool_name
        for group in contract.required_groups
        for tool_name in group.any_of_tool_names
    }
    assert required == _EXPECTED_CONTEXT_EXECUTION_TOOLS[case.case_id]
    zero_call_outcome = evaluate_tool_execution_contract(
        SimpleNamespace(new_items=[]),
        contract,
    )
    assert zero_call_outcome.satisfied is False
    assert set(zero_call_outcome.missing_groups)
