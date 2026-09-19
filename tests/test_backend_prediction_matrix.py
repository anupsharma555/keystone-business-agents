"""Offline trajectory predictions for ten high-value KBA agent boundaries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal

import pytest

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
from keystone_agents.agents.orchestrator import run_orchestrator_preflight
from keystone_agents.agents.outreach_composer import build_outreach_composer_agent
from keystone_agents.agents.preprints_context import build_preprints_context_agent
from keystone_agents.agents.rss_context import build_rss_context_agent
from keystone_agents.agents.zotero_context import build_zotero_context_agent
from keystone_agents.receipts.normalization import identity_fingerprint
from keystone_agents.runtime.decision_trace_harness import (
    BackendScenarioPrediction,
    DecisionAttemptEvidence,
    HandoffEvidence,
    ModelVisibleComponent,
    ProviderAttemptEvidence,
    ProviderAttemptTrace,
    build_backend_decision_stage_trace,
    compare_backend_scenario_prediction,
)
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan


@dataclass(frozen=True)
class PredictionCase:
    scenario_id: str
    route: str
    request: str
    context_categories: tuple[str, ...]
    attached_tools: tuple[str, ...]
    attached_mode: Literal["exact", "minimum"]
    allowed_call_sequences: tuple[tuple[str, ...], ...]
    candidate_ids: tuple[str, ...]
    selected_ids: tuple[str, ...]
    excluded_ids: tuple[str, ...]
    candidate_tool_ids: tuple[tuple[str, tuple[str, ...]], ...]
    decision_stage: str
    provider_attempts: tuple[ProviderAttemptEvidence, ...] = ()
    repair_expected: bool = False
    handoffs: tuple[HandoffEvidence, ...] = ()
    structured_output_obligations: tuple[str, ...] = ()
    public_output_obligations: tuple[str, ...] = ()
    workflow_called_tools: tuple[str, ...] = ()
    preacquired_candidate_ids: tuple[str, ...] = ()
    intentional_tool_free: bool = False
    preacquired_context_verified: bool = False
    model_request_count: int = 2
    max_model_requests: int = 4
    max_latency_ms: float = 1_000.0


def _provider(
    provider: str,
    operation: str,
    status: str,
    attempt: int,
    *,
    fallback_from: str = "",
    verified: bool = True,
) -> ProviderAttemptEvidence:
    return ProviderAttemptEvidence(
        provider=provider,
        operation=operation,
        status=status,
        attempt=attempt,
        fallback_from=fallback_from,
        receipt_verified=verified,
        latency_ms=1.0,
    )


_WORKSPACE_AMBIGUOUS_TOOLS = (
    "google_drive_search_files",
    "google_drive_get_file_metadata",
    "google_doc_read",
    "google_sheet_read_table",
    "google_slide_deck_read",
)


CASES = (
    PredictionCase(
        scenario_id="airtable-schema-only",
        route="airtable_context_agent",
        request=(
            "Could you check our Airtable structure and tell me which table is for "
            "opportunities, then list only its field names? Don't read record values "
            "or change anything."
        ),
        context_categories=("typed_context_pack",),
        attached_tools=(
            "airtable_get_base_schema",
            "airtable_read_schema_detail",
        ),
        attached_mode="exact",
        allowed_call_sequences=(("airtable_get_base_schema",),),
        candidate_ids=("Opportunities",),
        selected_ids=("Opportunities",),
        excluded_ids=(),
        candidate_tool_ids=(("airtable_get_base_schema", ("Opportunities",)),),
        decision_stage="airtable_record_selection",
        provider_attempts=(_provider("airtable", "read_schema", "success", 1),),
        structured_output_obligations=(
            "summary",
            "relevant_tables",
            "relevant_fields",
            "decision",
        ),
        public_output_obligations=("field_names_only", "no_record_values", "no_write"),
        model_request_count=2,
        max_model_requests=2,
    ),
    PredictionCase(
        scenario_id="calendar-context-to-gmail-four-candidate",
        route="gmail_triage",
        request=(
            "Using the interview on my calendar tomorrow, find the related G2i email "
            "conversation, decide which current thread needs a reply, and draft a short "
            "looking-forward-to-it response here only. Don't create or send a Gmail draft."
        ),
        context_categories=("typed_context_pack", "cross_provider_context"),
        attached_tools=("query_gmail_message_summaries", "read_gmail_context"),
        attached_mode="minimum",
        allowed_call_sequences=(
            (
                "query_gmail_message_summaries",
                "read_gmail_context",
                "read_gmail_context",
                "read_gmail_context",
                "read_gmail_context",
            ),
        ),
        candidate_ids=(
            "gmail-current-invitation",
            "gmail-cancellation",
            "gmail-obsolete-time",
            "gmail-reminder",
        ),
        selected_ids=("gmail-current-invitation",),
        excluded_ids=(
            "gmail-cancellation",
            "gmail-obsolete-time",
            "gmail-reminder",
        ),
        candidate_tool_ids=(
            (
                "query_gmail_message_summaries",
                (
                    "gmail-current-invitation",
                    "gmail-cancellation",
                    "gmail-obsolete-time",
                    "gmail-reminder",
                ),
            ),
            ("read_gmail_context", ("gmail-current-invitation",)),
            ("read_gmail_context", ("gmail-cancellation",)),
            ("read_gmail_context", ("gmail-obsolete-time",)),
            ("read_gmail_context", ("gmail-reminder",)),
        ),
        decision_stage="gmail_candidate_selection",
        provider_attempts=(
            _provider("gmail", "query_message_summaries", "success", 1),
            _provider("gmail", "read_thread", "success", 2),
            _provider("gmail", "read_thread", "success", 3),
            _provider("gmail", "read_thread", "success", 4),
            _provider("gmail", "read_thread", "success", 5),
        ),
        handoffs=(
            HandoffEvidence(
                source_agent="calendar_context",
                downstream_agent="gmail_triage",
                evidence_values=("calendar-event-context",),
                provided=True,
                consumed=True,
                consumption_source="verified_cross_provider_context",
            ),
        ),
        structured_output_obligations=(
            "selected_thread",
            "candidate_assessments",
            "reply_relevance",
            "draft_reply",
            "decision",
        ),
        public_output_obligations=(
            "slack_only_copy",
            "excluded_alternatives",
            "no_private_provider_ids",
            "no_gmail_write",
        ),
        model_request_count=6,
        max_model_requests=7,
    ),
    PredictionCase(
        scenario_id="research-provider-fallback",
        route="business_research_analyst",
        request=(
            "Research Northstar Health's current behavioral-health offering and give me "
            "the strongest supported partnership signal, visible sources, and what is "
            "still unverified. Read-only."
        ),
        context_categories=("typed_context_pack",),
        attached_tools=("search_web", "extract_selected_urls_to_source_bundle"),
        attached_mode="minimum",
        allowed_call_sequences=(
            ("search_web", "extract_selected_urls_to_source_bundle"),
        ),
        candidate_ids=(
            "https://northstar.example/current-model",
            "https://payer.example/northstar-partnership",
            "https://weak.example/old-directory",
        ),
        selected_ids=(
            "https://northstar.example/current-model",
            "https://payer.example/northstar-partnership",
        ),
        excluded_ids=("https://weak.example/old-directory",),
        candidate_tool_ids=(
            (
                "search_web",
                (
                    "https://northstar.example/current-model",
                    "https://payer.example/northstar-partnership",
                    "https://weak.example/old-directory",
                ),
            ),
            (
                "extract_selected_urls_to_source_bundle",
                (
                    "https://northstar.example/current-model",
                    "https://payer.example/northstar-partnership",
                ),
            ),
        ),
        decision_stage="research_source_selection",
        provider_attempts=(
            _provider("searxng", "search", "failed", 1, verified=False),
            _provider(
                "agents-web-search",
                "search",
                "success",
                2,
                fallback_from="searxng",
            ),
            _provider("trafilatura", "extract", "success", 3),
        ),
        structured_output_obligations=(
            "facts",
            "sources_used",
            "sources_excluded",
            "limitations",
            "decision",
        ),
        public_output_obligations=("visible_source_urls", "limitations", "no_write"),
        model_request_count=3,
        max_model_requests=4,
    ),
    PredictionCase(
        scenario_id="opportunity-supplied-candidates",
        route="opportunity_scout",
        request=(
            "Compare these two supplied synthetic opportunities for a small clinical-data "
            "company, score both, and choose the better-supported fit. Don't browse, save, "
            "or contact anyone."
        ),
        context_categories=("typed_context_pack", "preacquired_evidence"),
        attached_tools=("score_opportunity",),
        attached_mode="minimum",
        allowed_call_sequences=(("score_opportunity", "score_opportunity"),),
        candidate_ids=(
            "opportunity:harborlight-rfp",
            "opportunity:shoreline-grant",
        ),
        selected_ids=("opportunity:harborlight-rfp",),
        excluded_ids=("opportunity:shoreline-grant",),
        candidate_tool_ids=(
            ("score_opportunity", ("opportunity:harborlight-rfp",)),
            ("score_opportunity", ("opportunity:shoreline-grant",)),
        ),
        decision_stage="opportunity_candidate_selection",
        structured_output_obligations=(
            "candidate_scores",
            "rank_order",
            "exclusions",
            "decision",
            "research_handoff_recommendation",
        ),
        public_output_obligations=("comparison", "assumptions", "no_write"),
        preacquired_context_verified=True,
        model_request_count=3,
        max_model_requests=3,
    ),
    PredictionCase(
        scenario_id="outreach-approved-claims",
        route="outreach_composer",
        request=(
            "Using only the approved synthetic company facts, write a concise organization "
            "introduction for internal review. Keep unapproved claims out and don't browse, "
            "save, seek approval, or send anything."
        ),
        context_categories=("typed_context_pack", "preacquired_evidence"),
        attached_tools=(),
        attached_mode="exact",
        allowed_call_sequences=((),),
        candidate_ids=("keystone-profile-claim", "source-harborlight-approved"),
        selected_ids=("keystone-profile-claim", "source-harborlight-approved"),
        excluded_ids=(),
        candidate_tool_ids=(),
        decision_stage="outreach_evidence_selection",
        structured_output_obligations=(
            "draft_copy",
            "claims_used",
            "sources_used",
            "approval_state",
            "decision",
        ),
        public_output_obligations=("internal_review_only", "contact_gap", "no_send"),
        preacquired_candidate_ids=(
            "keystone-profile-claim",
            "source-harborlight-approved",
        ),
        intentional_tool_free=True,
        preacquired_context_verified=True,
        model_request_count=1,
        max_model_requests=2,
    ),
    PredictionCase(
        scenario_id="workspace-ambiguous-artifact",
        route="google_workspace_context_agent",
        request=(
            "Find the latest Northstar planning artifact in Google Drive and summarize "
            "the decisions, owner, and unresolved risk. I don't remember whether it is a "
            "Doc, Sheet, or Slides deck. Please don't change anything."
        ),
        context_categories=("typed_context_pack",),
        attached_tools=_WORKSPACE_AMBIGUOUS_TOOLS,
        attached_mode="minimum",
        allowed_call_sequences=(
            (
                "google_drive_search_files",
                "google_drive_get_file_metadata",
                "google_doc_read",
            ),
            (
                "google_drive_search_files",
                "google_drive_get_file_metadata",
                "google_sheet_read_table",
            ),
            (
                "google_drive_search_files",
                "google_drive_get_file_metadata",
                "google_slide_deck_read",
            ),
        ),
        candidate_ids=("drive-plan-current", "drive-plan-archive"),
        selected_ids=("drive-plan-current",),
        excluded_ids=("drive-plan-archive",),
        candidate_tool_ids=(
            (
                "google_drive_search_files",
                ("drive-plan-current", "drive-plan-archive"),
            ),
            ("google_drive_get_file_metadata", ("drive-plan-current",)),
            ("google_doc_read", ("drive-plan-current",)),
        ),
        decision_stage="workspace_artifact_selection",
        provider_attempts=(
            _provider("google-drive", "search_files", "success", 1),
            _provider("google-drive", "get_file_metadata", "success", 2),
            _provider("google-docs", "read_document", "success", 3),
        ),
        structured_output_obligations=(
            "selected_artifact",
            "candidate_assessments",
            "summary",
            "limitations",
            "decision",
        ),
        public_output_obligations=("artifact_type", "limitations", "no_write"),
        model_request_count=4,
        max_model_requests=5,
    ),
    PredictionCase(
        scenario_id="zotero-latest-relevant",
        route="zotero_context_agent",
        request=(
            "In the measurement-based care collection, which recently added article is "
            "most relevant to implementation barriers? Compare the plausible items and "
            "don't create notes or change the library."
        ),
        context_categories=("typed_context_pack",),
        attached_tools=("zotero_read_api_metadata", "zotero_resolve_article_context"),
        attached_mode="minimum",
        allowed_call_sequences=(
            ("zotero_read_api_metadata", "zotero_resolve_article_context"),
        ),
        candidate_ids=("zotero-current", "zotero-older", "zotero-adjacent"),
        selected_ids=("zotero-current",),
        excluded_ids=("zotero-older", "zotero-adjacent"),
        candidate_tool_ids=(
            (
                "zotero_read_api_metadata",
                ("zotero-current", "zotero-older", "zotero-adjacent"),
            ),
            ("zotero_resolve_article_context", ("zotero-current",)),
        ),
        decision_stage="zotero_item_selection",
        provider_attempts=(
            _provider("zotero", "read_collection_metadata", "success", 1),
            _provider("zotero", "resolve_article_context", "success", 2),
        ),
        structured_output_obligations=(
            "relevant_items",
            "excluded_items",
            "selection_basis",
            "limitations",
            "decision",
        ),
        public_output_obligations=("citation_metadata", "limitations", "no_write"),
        model_request_count=3,
        max_model_requests=4,
    ),
    PredictionCase(
        scenario_id="rss-invalid-id-repair-no-reread",
        route="rss_context_agent",
        request=(
            "Which unreviewed feed signal is most worth following up this week, and why?"
        ),
        context_categories=("typed_context_pack",),
        attached_tools=("retrieve_rss_announcement_history", "inspect_signal_lifecycle"),
        attached_mode="minimum",
        allowed_call_sequences=(("retrieve_rss_announcement_history",),),
        candidate_ids=("rss-current", "rss-older"),
        selected_ids=("rss-current",),
        excluded_ids=("rss-older",),
        candidate_tool_ids=(
            ("retrieve_rss_announcement_history", ("rss-current", "rss-older")),
        ),
        decision_stage="signal_relevance_selection",
        provider_attempts=(
            _provider("slack-history-cache", "read_rss_history", "success", 1),
        ),
        repair_expected=True,
        structured_output_obligations=(
            "relevant_signals",
            "selection_basis",
            "limitations",
            "decision",
        ),
        public_output_obligations=("why_now", "limitations", "no_write"),
        model_request_count=3,
        max_model_requests=3,
    ),
    PredictionCase(
        scenario_id="preprints-relevance-checkpoint",
        route="preprints_context_agent",
        request=(
            "Which recent preprint deserves a closer evidence review for our clinical AI "
            "work, and what should the next checkpoint record?"
        ),
        context_categories=("typed_context_pack", "verified_continuation_state"),
        attached_tools=(
            "retrieve_preprint_announcement_history",
            "inspect_signal_lifecycle",
        ),
        attached_mode="minimum",
        allowed_call_sequences=(
            ("retrieve_preprint_announcement_history", "inspect_signal_lifecycle"),
        ),
        candidate_ids=("preprint-current", "preprint-older"),
        selected_ids=("preprint-current",),
        excluded_ids=("preprint-older",),
        candidate_tool_ids=(
            (
                "retrieve_preprint_announcement_history",
                ("preprint-current", "preprint-older"),
            ),
            ("inspect_signal_lifecycle", ("preprint-current",)),
        ),
        decision_stage="signal_relevance_selection",
        provider_attempts=(
            _provider("slack-history-cache", "read_preprint_history", "success", 1),
            _provider("local-checkpoint-store", "inspect_checkpoint", "success", 2),
        ),
        structured_output_obligations=(
            "relevant_preprints",
            "selection_basis",
            "checkpoint_recommendation",
            "limitations",
            "decision",
        ),
        public_output_obligations=("source_provenance", "limitations", "no_external_write"),
        workflow_called_tools=(
            "prepare_signal_lifecycle_checkpoint",
            "advance_signal_lifecycle_checkpoint",
        ),
        preacquired_context_verified=True,
        model_request_count=3,
        max_model_requests=4,
    ),
    PredictionCase(
        scenario_id="orchestrator-to-chief-cross-source",
        route="chief_of_staff",
        request=(
            "Across the current Airtable partner record and the latest Google Workspace "
            "operating plan, coordinate the read-only specialists and tell me the safest "
            "next action. Don't change or post anything."
        ),
        context_categories=("typed_context_pack", "cross_provider_context"),
        attached_tools=(
            "airtable_context_agent_as_specialist_tool",
            "google_workspace_context_agent_as_specialist_tool",
        ),
        attached_mode="minimum",
        allowed_call_sequences=(
            (
                "airtable_context_agent_as_specialist_tool",
                "google_workspace_context_agent_as_specialist_tool",
            ),
            (
                "google_workspace_context_agent_as_specialist_tool",
                "airtable_context_agent_as_specialist_tool",
            ),
        ),
        candidate_ids=("airtable_context_agent", "google_workspace_context_agent"),
        selected_ids=("airtable_context_agent", "google_workspace_context_agent"),
        excluded_ids=(),
        candidate_tool_ids=(
            (
                "airtable_context_agent_as_specialist_tool",
                ("airtable_context_agent",),
            ),
            (
                "google_workspace_context_agent_as_specialist_tool",
                ("google_workspace_context_agent",),
            ),
        ),
        decision_stage="chief_delegation_selection",
        provider_attempts=(
            _provider("airtable", "nested_read_context", "success", 1),
            _provider("google-workspace", "nested_read_context", "success", 2),
        ),
        handoffs=(
            HandoffEvidence(
                source_agent="orchestrator",
                downstream_agent="chief_of_staff",
                evidence_values=("orchestrator-cross-source-plan",),
                provided=True,
                consumed=True,
                consumption_source="validated_workflow_dispatch",
            ),
            HandoffEvidence(
                source_agent="airtable_context_agent",
                downstream_agent="chief_of_staff",
                evidence_values=("airtable_context_agent",),
                provided=True,
                consumed=True,
                consumption_source="nested_specialist_result",
            ),
            HandoffEvidence(
                source_agent="google_workspace_context_agent",
                downstream_agent="chief_of_staff",
                evidence_values=("google_workspace_context_agent",),
                provided=True,
                consumed=True,
                consumption_source="nested_specialist_result",
            ),
        ),
        structured_output_obligations=(
            "summary",
            "specialist_outcomes",
            "recommended_actions",
            "limitations",
            "decision",
        ),
        public_output_obligations=("cross_source_synthesis", "limitations", "no_write"),
        model_request_count=3,
        max_model_requests=4,
    ),
)


def _canonical_plan(
    *,
    route: str,
    intent: str,
    task_objective: str,
    expected_artifact_type: str,
    provider_system: str = "unspecified",
    provider_operations: tuple[str, ...] = (),
    requires_live_search: bool = False,
    permission_state: str = "read_only",
) -> ManualRequestPlan:
    return ManualRequestPlan.model_validate(
        {
            "source": "canonical:test",
            "requested_agent": route,
            "target_agent": route,
            "intent": intent,
            "task_objective": task_objective,
            "expected_artifact_type": expected_artifact_type,
            "provider_system": provider_system,
            "provider_operations": list(provider_operations),
            "requires_live_search": requires_live_search,
            "ask_shape": {"permission_state": permission_state},
        }
    )


def _build_agent(case: PredictionCase) -> tuple[str, Any]:
    preflight = run_orchestrator_preflight(
        case.request,
        requested_agent=case.route,
        live_manual_plan=False,
    )
    plan = preflight.manual_request_plan
    if case.route == "airtable_context_agent":
        agent = build_airtable_context_agent(
            request_text=case.request,
            manual_plan=plan,
            tool_tier="core_read",
        )
    elif case.route == "gmail_triage":
        gmail_plan = _canonical_plan(
            route=case.route,
            intent="gmail_triage",
            task_objective="gmail_triage",
            expected_artifact_type="gmail_triage_report",
            provider_system="gmail",
            provider_operations=("search", "read"),
        )
        agent = build_gmail_triage_agent(
            request_text=case.request,
            manual_request_plan=gmail_plan,
            tool_tier="core_read",
        )
    elif case.route == "business_research_analyst":
        research_plan = _canonical_plan(
            route=case.route,
            intent="company_research",
            task_objective="entity_research",
            expected_artifact_type="research_brief",
            provider_operations=("search", "read"),
            requires_live_search=True,
        )
        agent = build_business_research_analyst_agent(
            request_text=case.request,
            manual_request_plan=research_plan,
            tool_tier="deep_retrieval",
        )
    elif case.route == "opportunity_scout":
        opportunity_plan = _canonical_plan(
            route=case.route,
            intent="opportunity_search",
            task_objective="opportunity_discovery",
            expected_artifact_type="opportunity_record",
        )
        agent = build_opportunity_scout_agent(
            request_text=case.request,
            manual_request_plan=opportunity_plan,
            tool_tier="internal_write",
        )
    elif case.route == "outreach_composer":
        outreach_plan = _canonical_plan(
            route=case.route,
            intent="outreach_draft",
            task_objective="outreach_draft",
            expected_artifact_type="outreach_draft",
            permission_state="draft_only",
        )
        outreach_plan = outreach_plan.model_copy(
            update={
                "requires_approved_context": True,
                "ask_shape": AskShapePolicy(
                    permission_state="draft_only",
                    prior_context_dependency="selected_context",
                    source_type_preference=["approved_synthetic"],
                ),
            }
        )
        agent = build_outreach_composer_agent(
            request_text=case.request,
            manual_request_plan=outreach_plan,
            include_tools=False,
        )
    elif case.route == "google_workspace_context_agent":
        agent = build_google_workspace_context_agent(
            request_text=case.request,
            manual_plan=plan,
            tool_tier="core_read",
        )
    elif case.route == "zotero_context_agent":
        agent = build_zotero_context_agent(
            request_text=case.request,
            manual_plan=plan,
            tool_tier="core_read",
        )
    elif case.route == "rss_context_agent":
        agent = build_rss_context_agent(
            request_text=case.request,
            manual_plan=plan,
            tool_tier="core_read",
        )
    elif case.route == "preprints_context_agent":
        agent = build_preprints_context_agent(
            request_text=case.request,
            manual_plan=plan,
            tool_tier="core_read",
        )
    else:
        agent = build_chief_of_staff_agent(
            request_text=case.request,
            manual_request_plan=plan,
            include_specialist_tools=True,
        )
    return preflight.selected_agent, agent


def _call(call_id: str, name: str) -> dict[str, Any]:
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": json.dumps({"read_only": True, "limit": 4}),
    }


def _output(call_id: str, candidate_ids: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(
            {
                "status": "success",
                "candidates": [{"id": value} for value in candidate_ids],
                "send_enabled": False,
            }
        ),
    }


def _raw_results(case: PredictionCase) -> list[SimpleNamespace]:
    candidate_queues: dict[str, list[tuple[str, ...]]] = {}
    for tool_name, candidate_ids in case.candidate_tool_ids:
        candidate_queues.setdefault(tool_name, []).append(candidate_ids)
    items: list[dict[str, Any]] = []
    actual_sequence = case.allowed_call_sequences[0]
    for index, tool_name in enumerate(actual_sequence, start=1):
        queued = candidate_queues.get(tool_name) or []
        candidate_ids = queued.pop(0) if queued else ()
        call_id = f"{case.scenario_id}-{index}"
        items.extend((_call(call_id, tool_name), _output(call_id, candidate_ids)))
    return [SimpleNamespace(new_items=items)] if items else []


def _decision_attempts(case: PredictionCase) -> list[DecisionAttemptEvidence]:
    owner = "chief_of_staff" if case.route == "chief_of_staff" else "specialist_agent"
    attempts: list[DecisionAttemptEvidence] = []
    if case.repair_expected:
        attempts.append(
            DecisionAttemptEvidence(
                attempt=1,
                decision_owner=owner,
                decision_stage=case.decision_stage,
                candidate_values=(*case.candidate_ids, "fabricated-candidate"),
                selected_values=("fabricated-candidate",),
                assessments=(
                    ("fabricated-candidate", "selected", "Invalid synthetic selection."),
                ),
                reasoning="The first selection requires validator feedback.",
                limitations=("Synthetic fixture evidence only.",),
                validator_status="repair_required",
                validator_reason_code="unknown_identity",
                repair_requested=True,
            )
        )
    assessments = tuple(
        [
            (candidate, "selected", "Selected from the bounded evidence.")
            for candidate in case.selected_ids
        ]
        + [
            (candidate, "excluded", "A stronger or more current candidate was selected.")
            for candidate in case.excluded_ids
        ]
    )
    attempts.append(
        DecisionAttemptEvidence(
            attempt=len(attempts) + 1,
            decision_owner=owner,
            decision_stage=case.decision_stage,
            candidate_values=case.candidate_ids,
            selected_values=case.selected_ids,
            assessments=assessments,
            reasoning="Selected and excluded candidates from the bounded visible evidence.",
            limitations=("Synthetic fixture evidence only.",),
            validator_status="accepted",
            validator_reason_code="selection_verified",
        )
    )
    return attempts


def _model_visible_inputs(case: PredictionCase) -> list[dict[str, Any]]:
    context = {
        "request": case.request,
        "context_categories": list(case.context_categories),
        "candidate_ids": list(case.candidate_ids),
        "handoff_targets": [handoff.downstream_agent for handoff in case.handoffs],
    }
    inputs = [context]
    for index in range(1, case.model_request_count):
        inputs.append(
            {
                **context,
                "turn": index + 1,
                "observed_tool_sequence": list(case.allowed_call_sequences[0][0:index]),
                "validator_feedback": (
                    "unknown_identity"
                    if case.repair_expected
                    and index == case.model_request_count - 1
                    else ""
                ),
            }
        )
    return inputs


def _input_components(case: PredictionCase) -> list[ModelVisibleComponent]:
    return [
        ModelVisibleComponent(
            category=category,  # type: ignore[arg-type]
            value={
                "request": case.request,
                "context_categories": list(case.context_categories),
                "candidate_ids": list(case.candidate_ids),
                "handoff_targets": [handoff.downstream_agent for handoff in case.handoffs],
            },
            source=(
                "verified_operator_or_provider_context"
                if category in {"cross_provider_context", "preacquired_evidence"}
                else "typed_runtime_context"
            ),
            required_identity_values=(
                case.candidate_ids
                if category in {"preacquired_evidence", "verified_continuation_state"}
                else ()
            ),
            verified=category
            in {
                "cross_provider_context",
                "preacquired_evidence",
                "verified_continuation_state",
            },
        )
        for category in case.context_categories
    ]


def _receipts(case: PredictionCase) -> list[dict[str, Any]]:
    return [
        {
            "tool_name": attempt.operation,
            "provider": attempt.provider,
            "operation": attempt.operation,
            "status": attempt.status,
            "verified": attempt.receipt_verified,
            "identity_fingerprints": [identity_fingerprint(case.scenario_id)],
        }
        for attempt in case.provider_attempts
    ]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.scenario_id)
def test_backend_prediction_matches_complete_sanitized_trace(
    case: PredictionCase,
) -> None:
    actual_route, agent = _build_agent(case)
    candidate_paths = {
        tool_name: ("candidates[].id",)
        for tool_name, _candidate_ids in case.candidate_tool_ids
    }
    receipts = _receipts(case)
    trace = build_backend_decision_stage_trace(
        stage_id=case.scenario_id,
        agent=actual_route,
        stage=case.decision_stage,
        provider="fake-provider",
        model="fake-sdk-model",
        run_id=f"run-{case.scenario_id}",
        trace_id=f"trace-{case.scenario_id}",
        model_visible_inputs=_model_visible_inputs(case),
        input_components=_input_components(case),
        raw_results=_raw_results(case),
        attached_tools=list(agent.tools),
        candidate_identity_paths=candidate_paths,
        preacquired_candidate_values=case.preacquired_candidate_ids,
        decision_attempts=_decision_attempts(case),
        provider_attempts=case.provider_attempts,
        provider_request_attempt_count=len(case.provider_attempts),
        provider_request_success_count=sum(
            attempt.status == "success" for attempt in case.provider_attempts
        ),
        provider_receipt_count=len(receipts),
        receipts=receipts,
        handoffs=case.handoffs,
        structured_output_obligations=case.structured_output_obligations,
        public_output_obligations=case.public_output_obligations,
        workflow_called_tools=case.workflow_called_tools,
        external_write_state="not_performed",
        preacquired_context_verified=case.preacquired_context_verified,
        preacquired_context_trace_verified=case.preacquired_context_verified,
        intentional_tool_free_synthesis=case.intentional_tool_free,
        provider_dependent=True,
        usage={
            "available": True,
            "requests": case.model_request_count,
            "provider_request_count_confirmed": True,
        },
        cost={"available": True, "estimated_usd": 0.0},
        latency_ms=5.0,
        retries=1 if case.repair_expected else 0,
        terminal_status="completed",
    )
    prediction = BackendScenarioPrediction(
        scenario_id=case.scenario_id,
        route=case.route,
        required_input_component_categories=list(case.context_categories),
        attached_tool_names=list(case.attached_tools),
        attached_tool_match_mode=case.attached_mode,
        model_called_tool_names=list(case.allowed_call_sequences[0]),
        allowed_model_call_sequences=[list(sequence) for sequence in case.allowed_call_sequences],
        provider_attempt_sequence=[
            ProviderAttemptTrace(**attempt.__dict__) for attempt in case.provider_attempts
        ],
        candidate_identity_fingerprints=[
            identity_fingerprint(value) for value in case.candidate_ids
        ],
        selected_identity_fingerprints=[
            identity_fingerprint(value) for value in case.selected_ids
        ],
        excluded_identity_fingerprints=[
            identity_fingerprint(value) for value in case.excluded_ids
        ],
        decision_owner=(
            "chief_of_staff" if case.route == "chief_of_staff" else "specialist_agent"
        ),
        decision_stage=case.decision_stage,
        validator_status="accepted",
        repair_expected=case.repair_expected,
        max_decision_attempts=2 if case.repair_expected else 1,
        required_consumed_handoff_targets=[
            handoff.downstream_agent for handoff in case.handoffs
        ],
        structured_output_obligations=list(case.structured_output_obligations),
        public_output_obligations=list(case.public_output_obligations),
        external_write_state="not_performed",
        max_model_requests=case.max_model_requests,
        max_latency_ms=case.max_latency_ms,
        terminal_status="completed",
    )

    comparison = compare_backend_scenario_prediction(prediction, trace)

    assert trace.evaluation_status == "pass", trace.findings
    assert comparison.matched, comparison.mismatched_fields
    assert comparison.mismatched_fields == []
    assert trace.external_write_state == "not_performed"
    assert trace.consumption.model_request_count <= case.max_model_requests
    assert trace.consumption.latency_ms is not None
    assert trace.consumption.latency_ms <= case.max_latency_ms
    serialized = trace.model_dump_json()
    assert case.request not in serialized
    public_handoff_labels = {
        handoff.source_agent for handoff in case.handoffs
    } | {handoff.downstream_agent for handoff in case.handoffs}
    assert all(
        candidate not in serialized
        for candidate in case.candidate_ids
        if candidate not in public_handoff_labels
    )


def test_prediction_comparison_rejects_call_order_and_request_ceiling_regressions() -> None:
    case = next(item for item in CASES if item.scenario_id == "research-provider-fallback")
    actual_route, agent = _build_agent(case)
    trace = build_backend_decision_stage_trace(
        stage_id="research-regression",
        agent=actual_route,
        stage=case.decision_stage,
        provider="fake-provider",
        model="fake-model",
        run_id="run-regression",
        trace_id="trace-regression",
        model_visible_inputs=_model_visible_inputs(case),
        input_components=_input_components(case),
        raw_results=_raw_results(case),
        attached_tools=list(agent.tools),
        candidate_identity_paths={
            tool_name: ("candidates[].id",)
            for tool_name, _candidate_ids in case.candidate_tool_ids
        },
        decision_attempts=_decision_attempts(case),
        provider_attempts=case.provider_attempts,
        receipts=_receipts(case),
        structured_output_obligations=case.structured_output_obligations,
        public_output_obligations=case.public_output_obligations,
        external_write_state="not_performed",
        provider_dependent=True,
        usage={"requests": case.model_request_count, "provider_request_count_confirmed": True},
        cost={"available": True, "estimated_usd": 0.0},
        latency_ms=5.0,
    )
    prediction = BackendScenarioPrediction(
        scenario_id=case.scenario_id,
        route=case.route,
        attached_tool_names=list(case.attached_tools),
        attached_tool_match_mode=case.attached_mode,
        allowed_model_call_sequences=[
            ["extract_selected_urls_to_source_bundle", "search_web"]
        ],
        candidate_identity_fingerprints=[
            identity_fingerprint(value) for value in case.candidate_ids
        ],
        decision_owner="specialist_agent",
        decision_stage=case.decision_stage,
        validator_status="accepted",
        max_model_requests=case.model_request_count - 1,
        terminal_status="completed",
    )

    comparison = compare_backend_scenario_prediction(prediction, trace)

    assert comparison.matched is False
    assert "model_called_tools" in comparison.mismatched_fields
    assert "model_request_ceiling" in comparison.mismatched_fields
