"""Offline acceptance matrix for plannerless, request-scoped agent admission.

These tests do not call a model or a live provider. They run the real
``run_orchestrator_preflight(..., live_manual_plan=False)`` path, pass its typed
deterministic plan to the selected direct agent, and inspect the exact SDK tool
surface. The matrix therefore protects the removal of the standalone
manual-planner model hop without manufacturing stronger plan authority in the
test.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest

from keystone_agents.agent_decision_policy import list_agent_decision_policies
from keystone_agents.agent_registry import list_agent_specs
from keystone_agents.agent_tool_policy import tool_name_for_policy
from keystone_agents.agents.airtable_context import build_airtable_context_agent
from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_agent,
)
from keystone_agents.agents.calendar_action_interpreter import resolve_calendar_action_plan
from keystone_agents.agents.chief_of_staff import build_chief_of_staff_agent
from keystone_agents.agents.gmail_triage import build_gmail_triage_agent
from keystone_agents.agents.google_workspace_context import (
    build_google_workspace_context_agent,
)
from keystone_agents.agents.opportunity_scout import build_opportunity_scout_agent
from keystone_agents.agents.orchestrator import (
    OrchestratorPreflight,
    build_orchestrator_agent,
    run_orchestrator_preflight,
)
from keystone_agents.agents.outreach_composer import build_outreach_composer_agent
from keystone_agents.agents.preprints_context import build_preprints_context_agent
from keystone_agents.agents.rag_retrieval_specialist import (
    build_rag_retrieval_specialist_agent,
    rag_retrieval_fixture,
)
from keystone_agents.agents.rss_context import build_rss_context_agent
from keystone_agents.agents.zotero_context import build_zotero_context_agent
from keystone_agents.authority.semantic import is_bounded_provider_read_plan
from keystone_agents.calendar_actions import infer_calendar_action_plan
from keystone_agents.capabilities.tool_scope import tool_scope_receipt_for_agent
from keystone_agents.live_retrieval import build_shared_search_provider_config
from keystone_agents.retrieval_policy import resolve_requested_search_provider
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan

Builder = Callable[..., Any]


@pytest.fixture(autouse=True)
def synthetic_rag_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    """Construct a hosted tool for admission inspection; never query a real corpus."""

    monkeypatch.setenv(
        "KEYSTONE_RAG_RETRIEVAL_SPECIALIST_FILE_SEARCH_VECTOR_STORE_IDS",
        "vs_offline_matrix",
    )


@dataclass(frozen=True)
class PlannerlessAgentCase:
    route: str
    prompt: str
    builder: Builder
    plan_parameter: str
    expected_target: str
    expected_intent: str
    required_tools: frozenset[str]
    forbidden_tools: frozenset[str]
    builder_kwargs: tuple[tuple[str, Any], ...] = ()


_PROVIDER_MUTATION_TOOLS = frozenset(
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
        "create_google_calendar_event",
        "update_google_calendar_event",
        "delete_google_calendar_event",
    }
)


CASES = (
    PlannerlessAgentCase(
        route="rag_retrieval_specialist",
        prompt=(
            "Find the nearest articles in the configured vector store about clinician "
            "trust in clinical AI. Cite only retrieved evidence and keep this read-only."
        ),
        builder=build_rag_retrieval_specialist_agent,
        plan_parameter="manual_request_plan",
        expected_target="rag_retrieval_specialist",
        expected_intent="rag_retrieval",
        required_tools=frozenset({"file_search"}),
        forbidden_tools=_PROVIDER_MUTATION_TOOLS | {"search_web"},
    ),
    PlannerlessAgentCase(
        route="gmail_triage",
        prompt=(
            "Morning - could you look over the five newest messages from the last "
            "two days, group them by urgency, and tell me which need a reply? Just "
            "read them; please do not draft, label, archive, or change anything."
        ),
        builder=build_gmail_triage_agent,
        plan_parameter="manual_request_plan",
        expected_target="gmail_triage",
        expected_intent="gmail_triage",
        required_tools=frozenset({"inspect_gmail_mailbox_schema", "query_gmail_message_summaries"}),
        forbidden_tools=_PROVIDER_MUTATION_TOOLS,
    ),
    PlannerlessAgentCase(
        route="business_research_analyst",
        prompt=(
            "Can you research Northstar Youth Health's current school mental-health "
            "offering and "
            "find one district adoption signal? Please begin with Tavily if it is "
            "available, then use another public search source if coverage is thin. "
            "Cite the URLs and keep this read-only."
        ),
        builder=build_business_research_analyst_agent,
        plan_parameter="manual_request_plan",
        expected_target="business_research_analyst",
        expected_intent="company_research",
        required_tools=frozenset({"search_web", "check_workflow_duplicate"}),
        forbidden_tools=_PROVIDER_MUTATION_TOOLS,
    ),
    PlannerlessAgentCase(
        route="opportunity_scout",
        prompt=(
            "Please find three currently open 2026 partnership or pilot opportunities "
            "for a small behavioral-health data company. Use official opportunity "
            "pages, keep the eligibility filters strict, and do not draft outreach or "
            "save records."
        ),
        builder=build_opportunity_scout_agent,
        plan_parameter="manual_request_plan",
        expected_target="opportunity_scout",
        expected_intent="opportunity_search",
        required_tools=frozenset({"search_web", "check_workflow_duplicate"}),
        forbidden_tools=frozenset(
            {
                *_PROVIDER_MUTATION_TOOLS,
                "save_entity_memory",
                "save_opportunity_memory",
                "save_opportunity_placeholder",
            }
        ),
    ),
    PlannerlessAgentCase(
        route="outreach_composer",
        prompt=(
            "Using only the approved HarborMind profile and approved contact context, "
            "write a short introductory email here for my review. Do not create a "
            "Gmail draft, send it, post it, or update tracking."
        ),
        builder=build_outreach_composer_agent,
        plan_parameter="manual_request_plan",
        expected_target="outreach_composer",
        expected_intent="outreach_draft",
        required_tools=frozenset({"load_approved_contact_context", "check_unsupported_claims"}),
        forbidden_tools=frozenset(
            {
                *_PROVIDER_MUTATION_TOOLS,
                "save_initial_outreach_tracking_record",
                "create_approval_queue_item",
                "create_approval_request_placeholder",
            }
        ),
    ),
    PlannerlessAgentCase(
        route="airtable_context_agent",
        prompt=(
            "Could you first show me the schema for the Partner Leads table, then list "
            "the three most recently updated active records? This is read-only; do not "
            "create, update, attach, or delete anything."
        ),
        builder=build_airtable_context_agent,
        plan_parameter="manual_plan",
        expected_target="airtable_context_agent",
        expected_intent="context_lookup",
        required_tools=frozenset({"airtable_get_base_schema", "airtable_read_records"}),
        forbidden_tools=_PROVIDER_MUTATION_TOOLS,
    ),
    PlannerlessAgentCase(
        route="google_workspace_context_agent",
        prompt=(
            "Please find the latest Google Drive document named Clinical AI Partner "
            "Notes and summarize its title, modified date, and owner. Read only; do not "
            "edit, move, share, or create files."
        ),
        builder=build_google_workspace_context_agent,
        plan_parameter="manual_plan",
        expected_target="google_workspace_context_agent",
        expected_intent="context_lookup",
        required_tools=frozenset({"google_drive_search_files", "google_drive_get_file_metadata"}),
        forbidden_tools=_PROVIDER_MUTATION_TOOLS,
        builder_kwargs=(("tool_tier", "internal_write"),),
    ),
    PlannerlessAgentCase(
        route="zotero_context_agent",
        prompt=(
            "In Zotero, find the newest two items in the Digital Phenotyping collection "
            "and return each title, authors, publication, and abstract. Do not add "
            "notes, import, edit, or delete anything."
        ),
        builder=build_zotero_context_agent,
        plan_parameter="manual_plan",
        expected_target="zotero_context_agent",
        expected_intent="context_lookup",
        required_tools=frozenset({"zotero_resolve_article_context", "zotero_read_api_metadata"}),
        forbidden_tools=_PROVIDER_MUTATION_TOOLS,
        builder_kwargs=(("tool_tier", "internal_write"),),
    ),
    PlannerlessAgentCase(
        route="rss_context_agent",
        prompt=(
            "Before tomorrow's digest, show me the RSS lifecycle checkpoint and the "
            "last three processed feed identities. Just inspect the state; do not "
            "prepare or advance a checkpoint."
        ),
        builder=build_rss_context_agent,
        plan_parameter="manual_plan",
        expected_target="rss_context_agent",
        expected_intent="context_lookup",
        required_tools=frozenset({"retrieve_rss_announcement_history", "inspect_signal_lifecycle"}),
        forbidden_tools=frozenset(
            {"prepare_signal_lifecycle_checkpoint", "advance_signal_lifecycle_checkpoint"}
        ),
    ),
    PlannerlessAgentCase(
        route="preprints_context_agent",
        prompt=(
            "Could you check the preprint lifecycle state and tell me which receipt last "
            "verified the checkpoint? Keep it read-only; do not run discovery, prepare "
            "a checkpoint, or advance anything."
        ),
        builder=build_preprints_context_agent,
        plan_parameter="manual_plan",
        expected_target="preprints_context_agent",
        expected_intent="context_lookup",
        required_tools=frozenset(
            {"retrieve_preprint_announcement_history", "inspect_signal_lifecycle"}
        ),
        forbidden_tools=frozenset(
            {"prepare_signal_lifecycle_checkpoint", "advance_signal_lifecycle_checkpoint"}
        ),
    ),
    PlannerlessAgentCase(
        route="orchestrator",
        prompt=(
            "I need a sourced competitor brief and, after that is reviewed, a short "
            "draft introduction. I'm not sure which specialist should go first. Lay "
            "out the safe sequence only; don't run tools, create artifacts, or contact "
            "anyone."
        ),
        builder=build_orchestrator_agent,
        plan_parameter="manual_request_plan",
        expected_target="clarification",
        expected_intent="clarification",
        required_tools=frozenset({"route_request_placeholder"}),
        forbidden_tools=frozenset({*_PROVIDER_MUTATION_TOOLS, "search_web"}),
        builder_kwargs=(("include_handoffs", False),),
    ),
    PlannerlessAgentCase(
        route="chief_of_staff",
        prompt=(
            "Hey Chief of Staff - before I plan tomorrow, look across the three blocked, "
            "non-archived WorkItems updated most recently. For each, give me its current "
            "owner or route and last verified stage. Then tell me which one has the "
            "clearest safe next step and whether any approval is already waiting. Keep "
            "this read-only; do not continue, rerun, approve, or change anything."
        ),
        builder=build_chief_of_staff_agent,
        plan_parameter="manual_request_plan",
        expected_target="chief_of_staff",
        expected_intent="continue_work_item",
        required_tools=frozenset(
            {
                "inspect_active_work_item_execution_summary",
            }
        ),
        forbidden_tools=_PROVIDER_MUTATION_TOOLS,
    ),
)


def _deterministic_preflight(case: PlannerlessAgentCase) -> OrchestratorPreflight:
    return run_orchestrator_preflight(
        case.prompt,
        requested_agent=case.route,
        live_manual_plan=False,
    )


def _deterministic_plan(case: PlannerlessAgentCase) -> ManualRequestPlan:
    return _deterministic_preflight(case).manual_request_plan


def _tool_names(agent: Any) -> list[str]:
    return [tool_name_for_policy(tool) for tool in agent.tools]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.route)
def test_plannerless_registered_agent_matrix(case: PlannerlessAgentCase) -> None:
    """Every registered agent gets a useful, bounded toolbox without a planner call."""

    preflight = _deterministic_preflight(case)
    plan = preflight.manual_request_plan
    kwargs = dict(case.builder_kwargs)
    kwargs.update({"request_text": case.prompt, case.plan_parameter: plan})
    agent = case.builder(**kwargs)
    names = _tool_names(agent)
    selected = set(names)

    assert plan.target_agent == case.expected_target
    assert plan.intent == case.expected_intent
    assert plan.source == "heuristic"
    assert preflight.sdk_usage_events == []
    assert case.required_tools <= selected
    assert case.forbidden_tools.isdisjoint(selected)
    assert names
    assert len(names) == len(selected)


def test_rag_matrix_fixture_preserves_query_without_claiming_retrieval() -> None:
    case = next(case for case in CASES if case.route == "rag_retrieval_specialist")
    preflight = _deterministic_preflight(case)
    result = rag_retrieval_fixture(case.prompt)

    assert preflight.manual_request_plan.provider_system == "openai_vector_store"
    assert set(preflight.manual_request_plan.provider_operations) <= {"read"}
    assert preflight.manual_request_plan.side_effect_policy == "draft_or_read_only"
    assert result.query == case.prompt
    assert result.match_status == "fixture_not_queried"
    assert result.matches == []
    assert result.claims == []
    assert result.limitations
    assert result.file_search_performed is False
    assert result.external_write_performed is False
    assert result.send_enabled is False


def test_matrix_covers_every_registered_agent_exactly_once() -> None:
    matrix_routes = [case.route for case in CASES]
    registered_routes = [spec.route_name for spec in list_agent_specs()]

    assert len(matrix_routes) == len(set(matrix_routes))
    assert set(matrix_routes) == set(registered_routes)


def test_decision_ownership_matrix_covers_every_registered_agent() -> None:
    """Every agent names model decisions, Python checks, and forbidden shortcuts."""

    policies = list_agent_decision_policies()
    policy_routes = [policy.route for policy in policies]
    registered_routes = [spec.route_name for spec in list_agent_specs()]

    assert len(policy_routes) == len(set(policy_routes)) == len(registered_routes)
    assert set(policy_routes) == set(registered_routes)
    for policy in policies:
        assert policy.semantic_decisions
        assert policy.deterministic_validators
        assert policy.forbidden_shortcuts
        assert policy.expected_model_read_tools


def test_provider_dependent_agents_require_model_visible_read_tools() -> None:
    matrix_tools = {
        case.route: set(_tool_names(case.builder(**{
            **dict(case.builder_kwargs),
            "request_text": case.prompt,
            case.plan_parameter: _deterministic_plan(case),
        })))
        for case in CASES
    }

    for policy in list_agent_decision_policies():
        assert set(policy.expected_model_read_tools) <= matrix_tools[policy.route], (
            policy.route,
            policy.expected_model_read_tools,
            matrix_tools[policy.route],
        )


@pytest.mark.parametrize(
    ("prompt", "expected_count", "expected_query", "expected_date_scope"),
    [
        (
            "CoS, list only the two interviews I have tomorrow from Google Calendar.",
            2,
            "interviews",
            "tomorrow",
        ),
        (
            "Chief of Staff, show my three appointments today on Google Calendar.",
            3,
            "appointments",
            "today",
        ),
        (
            "What are my two calls tomorrow on my calendar?",
            2,
            "calls",
            "tomorrow",
        ),
        (
            "CoS, which four webinars are on Google Calendar for 2026-08-05?",
            4,
            "webinars",
            "specific_date",
        ),
    ],
)
def test_natural_calendar_reads_compile_to_required_typed_provider_path(
    prompt: str,
    expected_count: int,
    expected_query: str,
    expected_date_scope: str,
) -> None:
    preflight = run_orchestrator_preflight(
        prompt,
        requested_agent="chief_of_staff",
        live_manual_plan=False,
    )
    plan = preflight.manual_request_plan
    resolution = resolve_calendar_action_plan(
        prompt,
        infer_calendar_action_plan(prompt),
        manual_plan=plan,
        semantic_candidate=True,
        live=False,
        today=date(2026, 8, 3),
    )
    names = set(
        _tool_names(
            build_chief_of_staff_agent(
                request_text=prompt,
                manual_request_plan=plan,
            )
        )
    )

    assert is_bounded_provider_read_plan(
        plan,
        provider_system="google_calendar",
        allowed_agents={"chief_of_staff"},
        allowed_intents={"context_lookup"},
    )
    assert plan.desired_count == expected_count
    assert plan.desired_count_explicit is True
    assert plan.provider_operations == ["read"]
    assert resolution.plan is not None
    assert resolution.plan.operation == "read"
    assert resolution.plan.query == expected_query
    assert resolution.plan.date_scope == expected_date_scope
    assert resolution.plan.target_count == expected_count
    assert "read_google_calendar_window" in names
    assert {
        "create_google_calendar_event",
        "update_google_calendar_event",
        "delete_google_calendar_event",
    }.isdisjoint(names)


def test_schema_first_read_order_for_gmail_and_airtable() -> None:
    cases = {case.route: case for case in CASES}
    for route, schema_tool, read_tool in (
        ("gmail_triage", "inspect_gmail_mailbox_schema", "query_gmail_message_summaries"),
        ("airtable_context_agent", "airtable_get_base_schema", "airtable_read_records"),
    ):
        case = cases[route]
        plan = _deterministic_plan(case)
        kwargs = dict(case.builder_kwargs)
        kwargs.update({"request_text": case.prompt, case.plan_parameter: plan})
        names = _tool_names(case.builder(**kwargs))

        assert names.index(schema_tool) < names.index(read_tool)


def test_airtable_schema_only_request_does_not_attach_record_reader() -> None:
    prompt = (
        "Could you look at our Airtable structure and tell me which table is meant for "
        "opportunities, then list that table's field names only? Please don't read or "
        "show any record values, and don't create, update, attach, or delete anything."
    )
    preflight = run_orchestrator_preflight(
        prompt,
        requested_agent="airtable_context_agent",
        live_manual_plan=False,
    )
    agent = build_airtable_context_agent(
        request_text=prompt,
        manual_plan=preflight.manual_request_plan,
        tool_tier="internal_write",
    )

    assert _tool_names(agent) == [
        "airtable_get_base_schema",
        "airtable_read_schema_detail",
    ]


@pytest.mark.parametrize(
    ("natural_request", "expected_provider"),
    [
        ("Could you use SearXNG for this search?", "searxng"),
        ("Please use OpenAI web search for this check.", "agents-web-search"),
        ("Can you try Exa first?", "exa"),
        ("Please begin with Tavily.", "tavily"),
        ("Search with Firecrawl this time.", "firecrawl"),
        ("Use Serper for this comparison.", "serper"),
    ],
)
def test_natural_provider_specific_asks_survive_without_a_planner(
    natural_request: str,
    expected_provider: str,
) -> None:
    assert (
        resolve_requested_search_provider(
            request_text=natural_request,
            requested_provider=None,
        )
        == expected_provider
    )


def test_named_search_provider_retains_enabled_fallbacks() -> None:
    request = (
        "Could you use Tavily first for this company check, then fall back to "
        "another public web provider if coverage is thin?"
    )

    config = build_shared_search_provider_config(
        requested_provider=None,
        configured_provider="searxng",
        fallback_provider="agents-web-search",
        agents_web_search_parallel=False,
        tavily_search_fallback=True,
        exa_search_fallback=True,
        request_text=request,
    )

    assert config.provider_sequence[0] == "tavily"
    assert "agents-web-search" in config.provider_sequence
    assert "exa" in config.deepening_provider_sequence
    assert len({*config.provider_sequence, *config.deepening_provider_sequence}) >= 3


@pytest.mark.parametrize(
    "request_text",
    [
        (
            "Business Research Analyst, begin with Tavily for a current "
            "source-backed review of Northstar Youth Health. Compare two public "
            "evidence signals."
        ),
        (
            "Could you start on Exa with a concise analysis of Northstar Youth "
            "Health and then use another public provider if the evidence is thin?"
        ),
    ],
)
def test_provider_first_wording_preserves_company_target(request_text: str) -> None:
    plan = run_orchestrator_preflight(
        request_text,
        requested_agent="business_research_analyst",
        live_manual_plan=False,
    ).manual_request_plan

    assert plan.primary_target == "Northstar Youth Health"


def test_google_workspace_default_builder_is_request_scoped_without_tier_hint() -> None:
    case = next(case for case in CASES if case.route == "google_workspace_context_agent")
    plan = _deterministic_plan(case)
    names = set(
        _tool_names(
            build_google_workspace_context_agent(
                request_text=case.prompt,
                manual_plan=plan,
            )
        )
    )

    assert {"google_drive_search_files", "google_drive_get_file_metadata"} <= names
    assert _PROVIDER_MUTATION_TOOLS.isdisjoint(names)


def test_workspace_pdf_metadata_request_does_not_attach_ocr_or_folder_listing() -> None:
    prompt = (
        "Can you find the most recently modified PDF in the KNIOps folder and tell me "
        "only its file name, MIME type, modified date, and owner? Read-only: don't open "
        "its contents, edit, move, share, create, or trash anything."
    )
    plan = run_orchestrator_preflight(
        prompt,
        requested_agent="google_workspace_context_agent",
        live_manual_plan=False,
    ).manual_request_plan
    names = _tool_names(
        build_google_workspace_context_agent(
            request_text=prompt,
            manual_plan=plan,
        )
    )

    assert names == ["google_drive_search_files", "google_drive_get_file_metadata"]


def test_workspace_pdf_content_request_attaches_bounded_ocr() -> None:
    prompt = (
        "Could you find the latest PDF named KBA_TEST_MEDIA and read its first page, "
        "then give me the title and two visible headings? Keep it read-only."
    )
    plan = run_orchestrator_preflight(
        prompt,
        requested_agent="google_workspace_context_agent",
        live_manual_plan=False,
    ).manual_request_plan
    names = set(
        _tool_names(
            build_google_workspace_context_agent(
                request_text=prompt,
                manual_plan=plan,
            )
        )
    )

    assert {
        "google_drive_search_files",
        "google_drive_get_file_metadata",
        "google_drive_media_ocr_read",
    } <= names
    assert "google_drive_list_folder" not in names


def test_zotero_default_builder_is_request_scoped_without_tier_hint() -> None:
    case = next(case for case in CASES if case.route == "zotero_context_agent")
    plan = _deterministic_plan(case)
    names = set(
        _tool_names(
            build_zotero_context_agent(
                request_text=case.prompt,
                manual_plan=plan,
            )
        )
    )

    assert {"zotero_resolve_article_context", "zotero_read_api_metadata"} <= names
    assert _PROVIDER_MUTATION_TOOLS.isdisjoint(names)


@pytest.mark.parametrize(
    "route",
    [
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
    ],
)
def test_provider_context_builders_attach_exact_request_scope_receipt(route: str) -> None:
    case = next(case for case in CASES if case.route == route)
    plan = _deterministic_plan(case)
    kwargs = dict(case.builder_kwargs)
    kwargs.update({"request_text": case.prompt, case.plan_parameter: plan})
    agent = case.builder(**kwargs)
    receipt = tool_scope_receipt_for_agent(agent)

    assert receipt["effective_mode"] == "request_scoped"
    assert receipt["selected_tool_names"] == _tool_names(agent)
    assert receipt["selected_tool_count"] == len(agent.tools)
    assert receipt["selection_fingerprint"]


@pytest.mark.parametrize("title", ["Delete Word Advisory", "Update Strategy Briefing"])
def test_calendar_title_words_do_not_enlarge_create_authority(title: str) -> None:
    request = (
        f"Hey Chief, please add a calendar event called {title} on September 18 "
        "at 2 PM Eastern for 30 minutes. No attendees."
    )

    plan = run_orchestrator_preflight(
        request,
        requested_agent="chief_of_staff",
        live_manual_plan=False,
    ).manual_request_plan

    assert plan.intent == "business_system_write"
    assert plan.provider_system == "google_calendar"
    assert plan.provider_operations == ["create"]


@pytest.mark.parametrize(
    ("route", "expected_count"),
    [("gmail_triage", 5), ("rss_context_agent", 3)],
)
def test_spelled_out_bounded_counts_survive_plannerless_inference(
    route: str,
    expected_count: int,
) -> None:
    case = next(case for case in CASES if case.route == route)

    assert _deterministic_plan(case).desired_count == expected_count


@pytest.mark.parametrize(
    ("route", "prompt", "builder", "expected_tools", "plan_parameter"),
    [
        (
            "google_workspace_context_agent",
            (
                "Please update the Google Doc named HarborMind Partner Notes by "
                "replacing the Status line with Reviewed. Do not create a new "
                "document, delete it, move it, rename it, or share it."
            ),
            build_google_workspace_context_agent,
            {
                "google_drive_search_files",
                "google_drive_get_file_metadata",
                "google_doc_read",
                "google_doc_write",
            },
            "manual_plan",
        ),
        (
            "airtable_context_agent",
            (
                "Please update the exact Partner Leads record recTest123 so Status is "
                "Reviewed. First read the table schema and current record, and do not "
                "create, delete, attach, or change any other record."
            ),
            build_airtable_context_agent,
            {
                "airtable_get_base_schema",
                "airtable_read_schema_detail",
                "airtable_read_records",
                "airtable_write_record",
            },
            "manual_plan",
        ),
    ],
)
def test_mixed_intent_update_keeps_exact_tools_and_negative_scope(
    route: str,
    prompt: str,
    builder: Builder,
    expected_tools: set[str],
    plan_parameter: str,
) -> None:
    """A positive update survives narrower no-create/no-delete clauses."""

    preflight = run_orchestrator_preflight(
        prompt,
        requested_agent=route,
        live_manual_plan=False,
    )
    plan = preflight.manual_request_plan
    agent = builder(
        request_text=prompt,
        tool_tier="internal_write",
        **{plan_parameter: plan},
    )

    assert preflight.sdk_usage_events == []
    assert plan.source == "heuristic"
    assert plan.target_agent == route
    assert plan.intent == "business_system_write"
    assert plan.provider_system in {"google_workspace", "airtable"}
    assert "update" in plan.provider_operations
    assert {"create", "delete", "attach"}.isdisjoint(plan.provider_operations)
    assert plan.ask_shape.permission_state != "read_only"
    assert plan.side_effect_policy == "internal_write_approval_required"
    assert set(_tool_names(agent)) == expected_tools
