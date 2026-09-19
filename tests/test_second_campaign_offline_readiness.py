"""Offline admission checks for the second 20-run live acceptance campaign.

The campaign prompts are deliberately conversational and do not name function
tools.  These tests make no model or provider calls: they run deterministic
Orchestrator preflight, build the selected SDK agent, and inspect its exact
request-scoped toolbox and formatting contract before a paid run is allowed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from keystone_agents.agent_registry import list_agent_specs
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

Builder = Callable[..., Any]


@pytest.fixture(autouse=True)
def synthetic_rag_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Construct a hosted tool for admission inspection; never query a real corpus."""

    monkeypatch.setenv(
        "KEYSTONE_RAG_RETRIEVAL_SPECIALIST_FILE_SEARCH_VECTOR_STORE_IDS",
        "vs_offline_matrix",
    )


@dataclass(frozen=True)
class CampaignCase:
    case_id: str
    route: str
    prompt: str
    builder: Builder
    plan_parameter: str
    expected_target: str
    expected_intent: str
    required_tools: frozenset[str]
    forbidden_tools: frozenset[str]
    builder_kwargs: tuple[tuple[str, Any], ...] = ()
    exact_tools: frozenset[str] | None = None
    expected_output_form: str | None = None
    exact_items: int | None = None
    desired_count: int | None = None
    maximum_words: int | None = None
    exact_words: int | None = None
    vague_but_knowable: bool = False
    allow_empty_tools: bool = False
    expected_scope_mode: str = "request_scoped"


_MUTATION_TOOLS = frozenset(
    {
        "airtable_write_record",
        "airtable_link_attachment",
        "airtable_upload_attachment",
        "airtable_create_expense_from_receipt",
        "airtable_delete_test_record",
        "airtable_test_record_lifecycle",
        "apply_gmail_labels",
        "modify_gmail_message_state",
        "create_gmail_draft_reply",
        "create_gmail_draft_with_attachment",
        "gmail_test_draft_lifecycle",
        "send_gmail_test_draft",
        "google_doc_write",
        "google_doc_trash",
        "google_doc_test_lifecycle",
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
        "create_google_calendar_event",
        "update_google_calendar_event",
        "delete_google_calendar_event",
        "save_company_profile_memory",
        "save_retrieval_tool_performance_memory",
        "save_entity_memory",
        "save_opportunity_memory",
        "save_opportunity_placeholder",
        "save_initial_outreach_tracking_record",
        "create_approval_queue_item",
        "create_approval_request_placeholder",
    }
)


CASES = (
    CampaignCase(
        "S01",
        "gmail_triage",
        (
            "Morning - could you check whether there are any unread validation emails "
            "from the last 30 days whose subject contains KBA_TEST_EMAIL? Give only the "
            "count and exact date window. Don't include senders, subjects, snippets, IDs, "
            "labels, or message contents, and don't change anything."
        ),
        build_gmail_triage_agent,
        "manual_request_plan",
        "gmail_triage",
        "gmail_triage",
        frozenset({"inspect_gmail_mailbox_schema", "query_gmail_message_summaries"}),
        _MUTATION_TOOLS,
    ),
    CampaignCase(
        "S02",
        "gmail_triage",
        (
            "Please create one approved Gmail validation draft marked KBA_TEST_DRAFT "
            "Cedar Review for the configured test recipient, read it back, revise the "
            "note so it is shorter, confirm the same draft changed, then remove that "
            "test draft. Do not send anything."
        ),
        build_gmail_triage_agent,
        "manual_request_plan",
        "gmail_triage",
        "gmail_triage",
        frozenset({"gmail_test_draft_lifecycle"}),
        _MUTATION_TOOLS - {"gmail_test_draft_lifecycle"},
        exact_tools=frozenset({"gmail_test_draft_lifecycle"}),
    ),
    CampaignCase(
        "S03",
        "business_research_analyst",
        (
            "Could you read only this NIMH page and give me exactly two concise bullets "
            "with source-supported facts, followed by one cautious sentence about why "
            "they may matter to KNI? Include the final URL and any extraction limitation; "
            "don't search elsewhere or save anything: "
            "https://www.nimh.nih.gov/health/topics/technology-and-the-future-of-mental-health-treatment"
        ),
        build_business_research_analyst_agent,
        "manual_request_plan",
        "business_research_analyst",
        "company_research",
        frozenset({"extract_selected_urls_to_source_bundle"}),
        _MUTATION_TOOLS | {"discover_public_company_contacts"},
        (("tool_tier", "deep_retrieval"),),
        expected_output_form="bullets",
        exact_items=2,
    ),
    CampaignCase(
        "S04",
        "business_research_analyst",
        (
            "Could you look into Fort Health's current youth mental-health model and whether "
            "public evidence supports payer or provider adoption? For related-source "
            "discovery, start with Exa; if coverage is incomplete, use another approved "
            "route. Give exactly three concise bullets with visible URLs, then one "
            "Limitations paragraph without repeating the same limitation elsewhere."
        ),
        build_business_research_analyst_agent,
        "manual_request_plan",
        "business_research_analyst",
        "company_research",
        frozenset({"search_web", "check_workflow_duplicate"}),
        _MUTATION_TOOLS,
        expected_output_form="bullets",
        exact_items=3,
    ),
    CampaignCase(
        "S05",
        "opportunity_scout",
        (
            "Could you find up to two opportunities that are currently open in the "
            "U.S. for funding or piloting measurement-based behavioral health care? "
            "Use official pages and keep eligibility strict. If none qualify, return "
            "zero rather than broadening. Don't save anything or draft outreach."
        ),
        build_opportunity_scout_agent,
        "manual_request_plan",
        "opportunity_scout",
        "opportunity_search",
        frozenset({"search_web", "score_opportunity"}),
        _MUTATION_TOOLS,
        (("tool_tier", "deep_retrieval"),),
        desired_count=2,
    ),
    CampaignCase(
        "S06",
        "opportunity_scout",
        (
            "Could you compare these two synthetic opportunities without searching? Cedar "
            "Youth Pilot is open to small U.S. health-data businesses, is due November 15, "
            "and funds a six-month measurement pilot. River Metrics Grant is due November "
            "30 but is limited to universities. Score eligibility, timing, effort, and KNI "
            "fit, then recommend one. Don't save anything or draft outreach."
        ),
        build_opportunity_scout_agent,
        "manual_request_plan",
        "opportunity_scout",
        "opportunity_search",
        frozenset({"score_opportunity"}),
        _MUTATION_TOOLS | {"search_web"},
        (("tool_tier", "deep_retrieval"),),
        exact_tools=frozenset({"score_opportunity"}),
        vague_but_knowable=True,
    ),
    CampaignCase(
        "S07",
        "outreach_composer",
        (
            "Using only these approved synthetic facts, draft a warm organization-level "
            "introduction under 120 words: Cedar Youth Health provides measurement "
            "dashboards to community clinics, and no individual contact is approved. Show "
            "the draft here for review, then state the contact limitation once. Do not "
            "send, post, save, create an approval, or update tracking."
        ),
        build_outreach_composer_agent,
        "manual_request_plan",
        "outreach_composer",
        "outreach_draft",
        frozenset(
            {
                "check_unsupported_claims",
                "build_approved_outreach_drafting_context",
                "compose_outreach_draft_llm_constrained",
            }
        ),
        _MUTATION_TOOLS,
        expected_output_form="draft",
        maximum_words=120,
    ),
    CampaignCase(
        "S08",
        "outreach_composer",
        (
            "I'm preparing for a first conversation with a synthetic clinic network that "
            "wants better behavioral-health measurement but has not approved a pilot. From "
            "that context only, give three discovery questions, two claims we can safely "
            "mention, and one unresolved point to verify. This is internal call prep, not "
            "an email or LinkedIn draft; don't research, save, or send anything."
        ),
        build_outreach_composer_agent,
        "manual_request_plan",
        "outreach_composer",
        "outreach_draft",
        frozenset({"check_unsupported_claims", "build_call_prep_artifact"}),
        _MUTATION_TOOLS | {"search_web"},
        vague_but_knowable=True,
    ),
    CampaignCase(
        "S09",
        "airtable_context_agent",
        (
            "Read-only schema question: which Business Expenses fields accept values, "
            "and which are computed? Show field names and types only. Do not read record "
            "values or make any provider changes."
        ),
        build_airtable_context_agent,
        "manual_plan",
        "airtable_context_agent",
        "context_lookup",
        frozenset({"airtable_get_base_schema", "airtable_read_schema_detail"}),
        _MUTATION_TOOLS | {"airtable_read_records"},
        (("tool_tier", "internal_write"),),
        exact_tools=frozenset(
            {"airtable_get_base_schema", "airtable_read_schema_detail"}
        ),
    ),
    CampaignCase(
        "S10",
        "airtable_context_agent",
        (
            "I approve one exact synthetic Airtable lifecycle in Business Expenses marked "
            "KBA_TEST_RECORD: create it, read it back, revise its description, confirm "
            "the same row changed, then remove it and confirm it is gone."
        ),
        build_airtable_context_agent,
        "manual_plan",
        "airtable_context_agent",
        "business_system_write",
        frozenset({"airtable_test_record_lifecycle"}),
        _MUTATION_TOOLS - {"airtable_test_record_lifecycle"},
        (("tool_tier", "internal_write"),),
        exact_tools=frozenset({"airtable_test_record_lifecycle"}),
    ),
    CampaignCase(
        "S11",
        "google_workspace_context_agent",
        (
            "Could you show me the title, file type, owner, and modified time for the most "
            "recently changed Google Doc in KNIOps? Metadata only; don't read its contents "
            "or change anything."
        ),
        build_google_workspace_context_agent,
        "manual_plan",
        "google_workspace_context_agent",
        "context_lookup",
        frozenset({"google_drive_search_files", "google_drive_get_file_metadata"}),
        _MUTATION_TOOLS | {"google_doc_read", "google_drive_media_ocr_read"},
        (("tool_tier", "internal_write"),),
        exact_tools=frozenset({"google_drive_search_files", "google_drive_get_file_metadata"}),
    ),
    CampaignCase(
        "S12",
        "google_workspace_context_agent",
        (
            "I approve one exact synthetic KNIOps document lifecycle titled KBA_TEST_DOC "
            "Orchard Handoff: create it with the sentence \"Orchard handoff is ready.\", "
            "read it back, replace the sentence with \"Orchard handoff is verified.\", "
            "confirm the same document changed, then move it to trash and verify it."
        ),
        build_google_workspace_context_agent,
        "manual_plan",
        "google_workspace_context_agent",
        "business_system_write",
        frozenset({"google_doc_test_lifecycle"}),
        _MUTATION_TOOLS - {"google_doc_test_lifecycle"},
        (("tool_tier", "internal_write"),),
        exact_tools=frozenset({"google_doc_test_lifecycle"}),
    ),
    CampaignCase(
        "S13",
        "zotero_context_agent",
        (
            "Read Zotero and find the most recently added journal article that has a stored "
            "abstract? Give exactly its title, publication, publication date, DOI or URL, "
            "and a one-sentence abstract gist. Don't include other items or change the "
            "library."
        ),
        build_zotero_context_agent,
        "manual_plan",
        "zotero_context_agent",
        "context_lookup",
        frozenset({"zotero_read_api_metadata"}),
        _MUTATION_TOOLS,
        (("tool_tier", "internal_write"),),
    ),
    CampaignCase(
        "S14",
        "zotero_context_agent",
        (
            "I approve one exact standalone Zotero note lifecycle marked KBA_TEST_NOTE: "
            "create it, read it back, revise the wording, confirm the same note and "
            "version changed, then remove it and confirm it is absent."
        ),
        build_zotero_context_agent,
        "manual_plan",
        "zotero_context_agent",
        "business_system_write",
        frozenset({"zotero_test_note_lifecycle"}),
        _MUTATION_TOOLS - {"zotero_test_note_lifecycle"},
        (("tool_tier", "internal_write"),),
    ),
    CampaignCase(
        "S15",
        "rss_context_agent",
        (
            "Before I rely on tomorrow's RSS digest, could you confirm whether it has a "
            "saved checkpoint and any unreviewed items about youth mental-health parity or "
            "school telehealth? If nothing has been recorded, say that plainly. Just "
            "inspect; don't prepare or advance a checkpoint or post anything."
        ),
        build_rss_context_agent,
        "manual_plan",
        "rss_context_agent",
        "context_lookup",
        frozenset(
            {
                "retrieve_rss_announcement_history",
                "read_rss_announcement_evidence",
                "inspect_signal_lifecycle",
            }
        ),
        _MUTATION_TOOLS,
        exact_tools=frozenset(
            {
                "retrieve_rss_announcement_history",
                "read_rss_announcement_evidence",
                "inspect_signal_lifecycle",
            }
        ),
        vague_but_knowable=True,
    ),
    CampaignCase(
        "S16",
        "preprints_context_agent",
        (
            "Could you confirm whether the preprint watch has a saved checkpoint and any "
            "unseen work on language-model phenotyping from mental-health records? If the "
            "history is empty, say so plainly; otherwise show at most two newest versions "
            "and label them preliminary. Don't run discovery or advance anything."
        ),
        build_preprints_context_agent,
        "manual_plan",
        "preprints_context_agent",
        "context_lookup",
        frozenset(
            {
                "retrieve_preprint_announcement_history",
                "read_preprint_announcement_evidence",
                "inspect_signal_lifecycle",
            }
        ),
        _MUTATION_TOOLS,
        exact_tools=frozenset(
            {
                "retrieve_preprint_announcement_history",
                "read_preprint_announcement_evidence",
                "inspect_signal_lifecycle",
            }
        ),
        vague_but_knowable=True,
    ),
    CampaignCase(
        "S17",
        "orchestrator",
        (
            "I am looking at WorkItem wi_f5f1758b8bec429494df59c747d8d5e2. "
            "Before anyone touches it, tell me what receipt-backed stage is complete, "
            "what must not be repeated, whether approval is pending, and the safest "
            "exact resume point. Give exactly three concise bullets. Status only: do "
            "not call a specialist or change the WorkItem."
        ),
        build_orchestrator_agent,
        "manual_request_plan",
        "orchestrator",
        "continue_work_item",
        frozenset({"inspect_work_item_execution_receipts"}),
        _MUTATION_TOOLS | {"search_web"},
        (("include_handoffs", False),),
        exact_tools=frozenset({"inspect_work_item_execution_receipts"}),
        expected_output_form="bullets",
        exact_items=3,
        vague_but_knowable=True,
    ),
    CampaignCase(
        "S18",
        "chief_of_staff",
        (
            "Chief, I approve one exact KBA_TEST_CALENDAR event lifecycle. Create "
            "KBA_TEST_CALENDAR Orchard Continuity 20261012 on October 12 at 2:00 PM "
            "Eastern for 30 minutes with the note "
            "initial validation; read it back; update the same event to 2:30 PM and "
            "change the note to revised validation; verify it; then delete only that "
            "test event and confirm it is gone."
        ),
        build_chief_of_staff_agent,
        "manual_request_plan",
        "chief_of_staff",
        "business_system_write",
        frozenset(
            {
                "read_google_calendar_window",
                "create_google_calendar_event",
                "update_google_calendar_event",
                "delete_google_calendar_event",
            }
        ),
        _MUTATION_TOOLS
        - {
            "create_google_calendar_event",
            "update_google_calendar_event",
            "delete_google_calendar_event",
        },
        exact_tools=frozenset(
            {
                "read_google_calendar_window",
                "create_google_calendar_event",
                "update_google_calendar_event",
                "delete_google_calendar_event",
            }
        ),
    ),
    CampaignCase(
        "S19",
        "chief_of_staff",
        (
            "Chief, have the business research analyst read only Mantra Health's public "
            "homepage and assess whether it supports a pilot-readiness claim for a college "
            "mental-health measurement partner, then give me the safest next step. Show "
            "the source URL, don't search beyond that page, and don't approve or change "
            "anything: https://mantrahealth.com/"
        ),
        build_chief_of_staff_agent,
        "manual_request_plan",
        "chief_of_staff",
        "route_request",
        frozenset(
            {
                "inspect_active_work_items",
                "business_research_analyst_as_specialist_tool",
            }
        ),
        _MUTATION_TOOLS | {"search_web"},
        (("include_specialist_tools", True),),
        vague_but_knowable=True,
    ),
    CampaignCase(
        "S20",
        "orchestrator",
        (
            "Please use one WorkItem to identify one pilot opportunity for KNI from "
            "Cartwheel's public homepage, research the company using only that page, then "
            "prepare a 70-word outreach draft for internal review. Show the URL. Do not "
            "send or save the draft, and do not look beyond this page: "
            "https://www.cartwheelcare.org/"
        ),
        build_orchestrator_agent,
        "manual_request_plan",
        "opportunity_scout",
        "opportunity_to_outreach_loop",
        frozenset(),
        _MUTATION_TOOLS,
        (
            ("include_handoffs", False),
            ("include_specialist_tools", True),
            ("tool_tier", "deep_retrieval"),
        ),
        vague_but_knowable=True,
        allow_empty_tools=True,
        exact_words=70,
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
    CampaignCase(
        "S-OFFLINE-RAG",
        "rag_retrieval_specialist",
        (
            "I remember an article about clinician trust in clinical AI but not its title. Find "
            "the nearest matches in our configured vector store, explain any ambiguity, and "
            "cite the supporting passages. Read only."
        ),
        build_rag_retrieval_specialist_agent,
        "manual_request_plan",
        "rag_retrieval_specialist",
        "rag_retrieval",
        frozenset({"file_search"}),
        _MUTATION_TOOLS | {"search_web"},
        exact_tools=frozenset({"file_search"}),
        expected_scope_mode="full",
    ),
)
ALL_OFFLINE_CASES = CASES + OFFLINE_SUPPLEMENTAL_CASES


def _build_case_agent(case: CampaignCase, plan: Any) -> Any:
    kwargs = dict(case.builder_kwargs)
    kwargs.update({"request_text": case.prompt, case.plan_parameter: plan})
    return case.builder(**kwargs)


def _tool_names(agent: Any) -> list[str]:
    return [tool_name_for_policy(tool) for tool in agent.tools]


@pytest.mark.parametrize("case", ALL_OFFLINE_CASES, ids=lambda case: case.case_id)
def test_second_campaign_routes_and_attaches_only_safe_tools(case: CampaignCase) -> None:
    preflight = run_orchestrator_preflight(
        case.prompt,
        requested_agent=case.route,
        live_manual_plan=False,
    )
    plan = preflight.manual_request_plan
    agent = _build_case_agent(case, plan)
    names = _tool_names(agent)
    selected = set(names)
    receipt = tool_scope_receipt_for_agent(agent)

    assert preflight.sdk_usage_events == []
    assert plan.source == "heuristic"
    assert plan.target_agent == case.expected_target
    assert plan.intent == case.expected_intent
    assert case.required_tools <= selected
    assert case.forbidden_tools.isdisjoint(selected)
    assert names or case.allow_empty_tools
    assert len(names) == len(selected)
    assert receipt["effective_mode"] == case.expected_scope_mode
    assert receipt["selected_tool_names"] == names
    assert receipt["selected_tool_count"] == len(names)
    assert receipt["selection_fingerprint"]
    if case.exact_tools is not None:
        assert selected == case.exact_tools


def test_natural_selected_page_slack_request_uses_extraction_only() -> None:
    prompt = (
        "I’m looking at this NIMH page before a planning call. Using only the page "
        "itself, give me two short bullets on what it says about how psychotherapy can "
        "be delivered or evaluated, then one sentence on what KNI should not infer from "
        "it. Include the final page URL and mention any extraction limitation. Please "
        "don’t search elsewhere, look for contacts, save anything, or change anything: "
        "<https://www.nimh.nih.gov/health/topics/psychotherapies|"
        "nimh.nih.gov/health/topics/psychotherapies>"
    )

    preflight = run_orchestrator_preflight(
        prompt,
        requested_agent="business_research_analyst",
        live_manual_plan=False,
    )
    plan = preflight.manual_request_plan
    agent = build_business_research_analyst_agent(
        request_text=prompt,
        manual_request_plan=plan,
        tool_tier="deep_retrieval",
    )

    assert plan.requires_live_search is False
    assert plan.primary_target == (
        "https://www.nimh.nih.gov/health/topics/psychotherapies"
    )
    assert _tool_names(agent) == [
        "extract_selected_urls_to_source_bundle", "read_web_source_window"
    ]


def test_second_campaign_and_supplement_are_unique_human_style_and_cover_every_agent() -> None:
    registered_specs = list_agent_specs()
    registered_routes = {spec.route_name for spec in registered_specs}
    registered_tool_names = {
        name.casefold()
        for spec in registered_specs
        for name in (*spec.tools, *spec.optional_tools)
        if "_" in name
    }
    normalized_prompts = [
        " ".join(case.prompt.casefold().split()) for case in ALL_OFFLINE_CASES
    ]

    assert len(CASES) == 20
    assert len(normalized_prompts) == len(set(normalized_prompts))
    assert {case.route for case in CASES} == CAMPAIGN_ROUTES
    assert {case.route for case in ALL_OFFLINE_CASES} == registered_routes
    assert {case.case_id for case in CASES}.isdisjoint(
        case.case_id for case in OFFLINE_SUPPLEMENTAL_CASES
    )
    assert sum(case.vague_but_knowable for case in CASES) >= 5
    for case, prompt in zip(ALL_OFFLINE_CASES, normalized_prompts, strict=True):
        assert len(prompt.split()) >= 18
        assert case.prompt[0].isupper()
        assert any(mark in case.prompt for mark in (".", "?", ";", ":"))
        assert "primary evaluation target" not in prompt
        assert "pass criteria" not in prompt
        assert "expected tool" not in prompt
        assert "call the tool" not in prompt
        assert not any(tool_name in prompt for tool_name in registered_tool_names)


@pytest.mark.parametrize(
    "case",
    [
        case
        for case in CASES
        if case.expected_output_form is not None
        or case.exact_items is not None
        or case.desired_count is not None
        or case.maximum_words is not None
        or case.exact_words is not None
    ],
    ids=lambda case: case.case_id,
)
def test_second_campaign_preserves_requested_output_shape(case: CampaignCase) -> None:
    plan = run_orchestrator_preflight(
        case.prompt,
        requested_agent=case.route,
        live_manual_plan=False,
    ).manual_request_plan
    constraints = plan.ask_shape.output_constraints

    if case.expected_output_form is not None:
        assert plan.ask_shape.output_form == case.expected_output_form
    if case.exact_items is not None:
        assert constraints.item_count_mode == "exact"
        assert constraints.minimum_items == case.exact_items
        assert constraints.maximum_items == case.exact_items
    if case.desired_count is not None:
        assert plan.desired_count == case.desired_count
    if case.maximum_words is not None:
        assert constraints.word_count_mode in {"maximum", "under"}
        assert constraints.word_count == case.maximum_words
    if case.exact_words is not None:
        assert constraints.word_count_mode == "exact"
        assert constraints.word_count == case.exact_words


def test_second_campaign_outreach_context_is_provider_free_and_traceable() -> None:
    approved_facts = next(case for case in CASES if case.case_id == "S07")
    call_prep = next(case for case in CASES if case.case_id == "S08")

    approved_preflight = run_orchestrator_preflight(
        approved_facts.prompt,
        requested_agent=approved_facts.route,
        live_manual_plan=False,
    )
    call_prep_preflight = run_orchestrator_preflight(
        call_prep.prompt,
        requested_agent=call_prep.route,
        live_manual_plan=False,
    )

    assert approved_preflight.manual_request_plan.requires_approved_context is False
    assert approved_preflight.composition_admission.composition_allowed is True
    assert approved_preflight.composition_admission.provider_action_allowed is False
    assert any(
        "operator-approved synthetic facts" in note
        for note in approved_preflight.route_result.audit_notes
    )
    assert call_prep_preflight.manual_request_plan.ask_shape.audience_scope == "internal"
    assert call_prep_preflight.manual_request_plan.outreach_channel == ""
    assert call_prep_preflight.manual_request_plan.requires_approved_context is False
    assert call_prep_preflight.manual_request_plan.provider_system == "unspecified"
    assert call_prep_preflight.manual_request_plan.provider_operations == []
    assert call_prep_preflight.route_result.refused is False
    assert call_prep_preflight.route_result.send_enabled is False


def test_second_campaign_never_invokes_the_manual_planner_agent(monkeypatch: Any) -> None:
    from keystone_agents.agents import manual_request_planner

    def fail_if_built(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("standalone manual planner agent must not be built")

    monkeypatch.setattr(
        manual_request_planner,
        "build_manual_request_planner_agent",
        fail_if_built,
    )

    preflights = [
        run_orchestrator_preflight(
            case.prompt,
            requested_agent=case.route,
            live_manual_plan=False,
        )
        for case in ALL_OFFLINE_CASES
    ]

    assert all(preflight.sdk_usage_events == [] for preflight in preflights)
    assert all(preflight.manual_request_plan.source == "heuristic" for preflight in preflights)


def test_graph_case_preserves_the_typed_specialist_order_without_running_it() -> None:
    case = next(case for case in CASES if case.case_id == "S20")
    plan = run_orchestrator_preflight(
        case.prompt,
        requested_agent=case.route,
        live_manual_plan=False,
    ).manual_request_plan

    assert plan.workflow == [
        "opportunity_scout",
        "business_research_analyst",
        "outreach_composer",
    ]
    assert plan.requires_durable_state is True
    assert plan.ask_shape.permission_state == "draft_only"
