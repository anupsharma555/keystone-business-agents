"""No-live architecture acceptance matrix for diverse and deterministic asks."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from keystone_agents.agent_registry import list_agent_specs

AskFamily = Literal["diverse", "deterministic"]
CoverageStatus = Literal["automated", "partial", "planned"]
ToolTier = Literal["none", "read_only", "draft_only", "write_gated"]

MAJOR_AGENT_ROUTES: tuple[str, ...] = tuple(spec.route_name for spec in list_agent_specs())


class DiverseAskAcceptanceCase(BaseModel):
    """One architecture-level ask shape and its expected execution boundary."""

    case_id: str
    ask_family: AskFamily
    prompt: str
    expected_route: str
    required_context: list[str] = Field(min_length=1)
    allowed_tool_tier: ToolTier
    side_effect_boundary: str
    expected_failure_mode: str
    stop_condition: str
    output_form: str
    coverage_status: CoverageStatus
    proof_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_coverage_claim(self) -> DiverseAskAcceptanceCase:
        if self.coverage_status == "automated" and not self.proof_refs:
            raise ValueError("Automated rows require at least one proof ref.")
        return self


def _case(
    case_id: str,
    family: AskFamily,
    prompt: str,
    route: str,
    context: tuple[str, ...],
    tier: ToolTier,
    boundary: str,
    failure: str,
    stop: str,
    output: str,
    status: CoverageStatus,
    *proof_refs: str,
) -> DiverseAskAcceptanceCase:
    return DiverseAskAcceptanceCase(
        case_id=case_id,
        ask_family=family,
        prompt=prompt,
        expected_route=route,
        required_context=list(context),
        allowed_tool_tier=tier,
        side_effect_boundary=boundary,
        expected_failure_mode=failure,
        stop_condition=stop,
        output_form=output,
        coverage_status=status,
        proof_refs=list(proof_refs),
    )


DIVERSE_ASK_ACCEPTANCE_CASES: tuple[DiverseAskAcceptanceCase, ...] = (
    _case(
        "DA-RAG-1",
        "diverse",
        "Use the RAG retrieval specialist to find corpus evidence about clinician trust in AI.",
        "rag_retrieval_specialist",
        ("explicit specialist request", "configured vector-store corpus", "retrieved matches"),
        "read_only",
        "hosted corpus retrieval only; no web substitution, external writes, or sends",
        "report absent or off-topic evidence without retaining irrelevant matches or claims",
        "stop after at most two file-search calls and a source-grounded retrieval result",
        "typed ranked matches, grounded claims, answer, and limitations",
        "automated",
        "tests/test_rag_retrieval_specialist.py::test_explicit_named_route_does_not_capture_basic_queries",
        "tests/test_rag_retrieval_specialist.py::test_live_wrapper_requires_file_search_evidence",
        "tests/test_rag_retrieval_specialist.py::test_grounded_claims_must_reference_retained_matches",
        "tests/test_rag_retrieval_specialist.py::test_not_found_cannot_retain_off_topic_matches_or_claims",
    ),
    _case(
        "DA-RAG-2",
        "deterministic",
        "Run this explicit corpus query only when hosted file search is configured.",
        "rag_retrieval_specialist",
        ("explicit corpus query", "hosted file-search availability"),
        "read_only",
        "no model or provider call when the configured corpus is unavailable",
        "block before live model execution when hosted file search is unavailable",
        "stop at the availability gate; fixture mode must not claim retrieval",
        "exact configuration blocker or fixture-not-queried result",
        "automated",
        "tests/test_rag_retrieval_specialist.py::test_live_wrapper_fails_before_model_without_vector_store",
        "tests/test_rag_retrieval_specialist.py::test_fixture_does_not_claim_retrieval",
    ),
    _case(
        "DA-OR-1",
        "diverse",
        "Review KBA and prioritize the next implementation work.",
        "orchestrator",
        ("raw request", "repo state", "backlog"),
        "read_only",
        "no external writes",
        "return a scoped plan when evidence is incomplete",
        "stop after prioritized safe next actions",
        "prioritized plan",
        "automated",
        "tests/test_manager_rwm_acceptance.py::test_orchestrator_read_contract_preserves_request_shape_and_selects_owner",
    ),
    _case(
        "DA-OR-2",
        "deterministic",
        "Continue this exact WorkItem but do not run live tools.",
        "orchestrator",
        ("WorkItem id", "current state"),
        "none",
        "no live tools",
        "block when WorkItem identity is missing",
        "stop before specialist execution",
        "continuation decision",
        "automated",
        "tests/test_manual_request_plan.py::test_manual_plan_still_routes_explicit_continuations_to_orchestrator",
    ),
    _case(
        "DA-COS-1",
        "diverse",
        "Audit current operations and tell me what matters next.",
        "chief_of_staff",
        ("operating context", "recent run state"),
        "read_only",
        "recommendations only",
        "request the missing operating scope",
        "stop after ranked recommendations",
        "operational brief",
        "automated",
        "tests/test_chief_of_staff_operating_layer.py::test_diverse_current_operations_audit_ranks_failures_and_stays_read_only",
    ),
    _case(
        "DA-COS-2",
        "deterministic",
        "Review these WorkItems for blockers without continuing them.",
        "chief_of_staff",
        ("selected WorkItem summaries",),
        "read_only",
        "no WorkItem continuation",
        "name missing owners or evidence",
        "stop after blocker and next-safe-action review",
        "blocker table",
        "automated",
        "tests/test_test_pack_specs.py::test_chief_specs_are_no_live_and_exclude_private_or_outbound_memory",
    ),
    _case(
        "DA-GT-1",
        "diverse",
        "Review recent email and tell me what needs follow-up this week.",
        "gmail_triage",
        ("bounded Gmail scope", "date window"),
        "read_only",
        "no draft or send unless requested",
        "gmail_context_required",
        "stop after prioritized triage",
        "priority summary",
        "automated",
        "tests/test_gmail_triage.py::test_bounded_weekly_gmail_fixture_grouping_prioritizes_every_message_without_mutation",
    ),
    _case(
        "DA-GT-2",
        "deterministic",
        "Summarize this selected thread; do not draft or send.",
        "gmail_triage",
        ("selected thread identity", "thread messages"),
        "read_only",
        "no Gmail mutation",
        "block if selected thread is absent",
        "stop after selected-thread summary",
        "thread summary",
        "automated",
        "tests/test_manual_request_plan.py::test_manual_plan_preserves_quick_selected_thread_read_only_shape",
    ),
    _case(
        "DA-BR-1",
        "diverse",
        "Research this company for KNI partnership relevance.",
        "business_research_analyst",
        ("company identity", "source policy", "KNI objective"),
        "read_only",
        "no Doc or CRM write",
        "state thin or conflicting evidence",
        "stop when source sufficiency is unresolved",
        "source-backed brief",
        "automated",
        "tests/test_business_research_analyst.py::test_diverse_company_partnership_fixture_returns_complete_source_backed_brief",
        "tests/test_source_triage.py::test_source_triage_thin_company_research_requires_more_evidence",
    ),
    _case(
        "DA-BR-2",
        "deterministic",
        "Compare two companies in a table using official sources only.",
        "business_research_analyst",
        ("two exact entities", "official sources"),
        "read_only",
        "no search broadening beyond named entities",
        "show missing cells instead of inference",
        "stop if entity identity is ambiguous",
        "comparison table",
        "automated",
        "tests/test_manual_request_plan.py::test_manual_plan_preserves_exact_source_table_and_no_broadening_shape",
    ),
    _case(
        "DA-OS-1",
        "diverse",
        "Find behavioral-health AI opportunities relevant to KNI.",
        "opportunity_scout",
        ("opportunity domain", "KNI fit"),
        "read_only",
        "no CRM save or outreach",
        "return weak-adjacent caveats when exact evidence is thin",
        "stop before weak padding",
        "ranked opportunity set",
        "automated",
        "tests/test_opportunity_scout.py::test_diverse_behavioral_health_ai_fixture_returns_ranked_sources_without_padding",
    ),
    _case(
        "DA-OS-2",
        "deterministic",
        "Find exact active remote U.S. roles; return zero if none.",
        "opportunity_scout",
        ("active status", "remote", "United States", "official source"),
        "read_only",
        "no broadening or save",
        "return zero exact matches",
        "stop when no source-backed exact match remains",
        "strict filtered list",
        "automated",
        "tests/test_manual_request_plan.py::test_manual_plan_preserves_exact_source_table_and_no_broadening_shape",
    ),
    _case(
        "DA-OC-1",
        "diverse",
        "Draft a warm founder note from this approved research brief.",
        "outreach_composer",
        ("approved facts", "recipient persona", "channel"),
        "draft_only",
        "draft only; never send",
        "approved_context_required",
        "stop before external use",
        "reviewable outreach draft",
        "automated",
        "tests/test_outreach_composer.py::test_diverse_approved_founder_note_is_source_backed_reviewable_and_never_sendable",
    ),
    _case(
        "DA-OC-2",
        "deterministic",
        "Revise this approved draft to 80 words and keep the same CTA.",
        "outreach_composer",
        ("selected approved draft", "approved claims", "CTA"),
        "draft_only",
        "no send or recipient change",
        "block if selected draft is absent",
        "stop after one bounded revision",
        "plain-text draft",
        "automated",
        "tests/test_outreach_composer.py::test_selected_draft_revision_preserves_identity_cta_recipient_and_word_limit",
    ),
    _case(
        "DA-AT-1",
        "diverse",
        "Read the bounded Airtable records for this operating question and summarize them.",
        "airtable_context_agent",
        ("exact base and table scope", "live schema", "bounded record query"),
        "read_only",
        "no Airtable mutation",
        "block when the base, table, or readable field scope is unavailable",
        "stop after the bounded schema and record packet",
        "source-bounded Airtable summary",
        "automated",
        "tests/test_context_agent_read_calls.py::test_airtable_context_read_tools_return_bounded_context_packets",
    ),
    _case(
        "DA-AT-2",
        "deterministic",
        "Update this exact approved Airtable record and verify the same record.",
        "airtable_context_agent",
        ("exact record identity", "typed update operation", "approval reference", "live schema"),
        "write_gated",
        "one exact approved update plus provider read-back only",
        "block when identity, operation, approval, schema, or live gate is missing",
        "stop after same-record read-back or the first blocker",
        "verified Airtable mutation receipt",
        "automated",
        "tests/test_cli.py::test_live_semantic_airtable_operations_override_raw_verb_noise",
    ),
    _case(
        "DA-ZO-1",
        "diverse",
        (
            "For the same Zotero article, summarize its stored metadata and notes, "
            "then use the attached PDF only if full text is permitted."
        ),
        "zotero_context_agent",
        ("exact parent item", "child notes", "attachment identity", "full-text permission"),
        "read_only",
        "no Zotero mutation or web substitution",
        "block or clarify when the parent or PDF attachment is ambiguous",
        "stop after the bounded article evidence packet and synthesis",
        "article metadata, note, and optional PDF summary",
        "automated",
        "tests/test_agent_registry.py::test_direct_zotero_read_catalog_is_request_scoped",
        "tests/test_context_agent_read_calls.py::test_zotero_pdf_read_verifies_parent_and_returns_bounded_text",
    ),
    _case(
        "DA-ZO-2",
        "deterministic",
        "Add a note to this exact Zotero article.",
        "zotero_context_agent",
        ("exact parent item", "requested note content", "supported mutation policy"),
        "write_gated",
        "ordinary native Zotero mutation remains unsupported",
        "return the exact unsupported-operation boundary without selecting test tools",
        "stop before any provider mutation",
        "blocked mutation result or approved importer/test lifecycle receipt",
        "automated",
        "tests/test_agent_registry.py::test_direct_zotero_write_catalog_preserves_native_mutation_boundary",
    ),
    _case(
        "DA-GW-1",
        "diverse",
        "Read one exact Google Doc and summarize the requested sections.",
        "google_workspace_context_agent",
        ("exact Drive file identity", "requested content scope"),
        "read_only",
        "no Workspace mutation",
        "block when the file identity is missing or ambiguous",
        "stop after bounded content read and synthesis",
        "source-bounded document summary",
        "automated",
        "tests/test_agent_registry.py::test_direct_google_workspace_read_catalog_is_request_scoped",
    ),
    _case(
        "DA-GW-2",
        "deterministic",
        "Append one approved row to an exact Google Sheet and verify it.",
        "google_workspace_context_agent",
        ("exact Sheet identity", "live schema", "field mapping", "approval reference"),
        "write_gated",
        "one exact internal write plus provider read-back only",
        "block when identity, schema mapping, approval, or live gate is missing",
        "stop after append and read-back receipt",
        "verified Sheet mutation receipt",
        "automated",
        "tests/test_agent_registry.py::test_direct_google_workspace_write_catalog_is_target_and_operation_scoped",
    ),
    _case(
        "DA-RSS-1",
        "diverse",
        (
            "What recent RSS announcements matter to Keystone? Rank the themes, "
            "link each source, and distinguish stored signals from verified current facts."
        ),
        "rss_context_agent",
        ("bounded announcement history", "topic relevance", "source URLs"),
        "read_only",
        "no Slack post or source mutation",
        "state when stored feed history does not verify current status",
        "stop before weak or unrelated result padding",
        "ranked source-linked monitoring brief",
        "automated",
        "tests/test_workflow_runner.py::test_natural_rss_request_executes_explicit_read_only_slack_provider",
    ),
    _case(
        "DA-RSS-2",
        "deterministic",
        "Read matching RSS history only when the explicit provider-read gate is enabled.",
        "rss_context_agent",
        ("bounded query", "configured Slack source", "explicit live-read gate"),
        "read_only",
        "read-only provider access; never post",
        "return no matches without calling Slack when the process gate is absent",
        "stop before any provider call when the gate is absent",
        "bounded feed-history packet or exact blocker",
        "automated",
        "tests/test_announcement_context_tools.py::test_rss_context_live_slack_fallback_requires_process_gate",
    ),
    _case(
        "DA-PP-1",
        "diverse",
        (
            "Find three recent psychiatry or clinical AI preprints relevant to Keystone, "
            "rank them, link each source, and label preliminary evidence."
        ),
        "preprints_context_agent",
        ("bounded preprint history", "topic relevance", "publication identity"),
        "read_only",
        "no source mutation, post, or unsupported current-fact claim",
        "state when evidence is preliminary or only a stored discovery candidate",
        "stop after the requested unique ranked set",
        "ranked source-linked preliminary-evidence brief",
        "automated",
        "tests/test_workflow_runner.py::test_natural_preprints_request_executes_ranked_read_only_context_provider",
    ),
    _case(
        "DA-PP-2",
        "deterministic",
        "Return selected matching preprints only; do not substitute unselected discovery records.",
        "preprints_context_agent",
        ("selected-only constraint", "bounded preprint store"),
        "read_only",
        "no fallback broadening or source mutation",
        "return zero matches when the selected-only store has no result",
        "stop without consulting the linked discovery fallback",
        "strict filtered preprint packet",
        "automated",
        "tests/test_announcement_context_tools.py::test_preprint_context_does_not_fallback_for_selected_only_query",
    ),
)


def build_diverse_ask_acceptance_report() -> dict[str, object]:
    """Validate matrix completeness without upgrading partial rows to passes."""

    ids = [case.case_id for case in DIVERSE_ASK_ACCEPTANCE_CASES]
    route_family_counts = Counter(
        (case.expected_route, case.ask_family) for case in DIVERSE_ASK_ACCEPTANCE_CASES
    )
    missing_pairs = [
        f"{route}:{family}"
        for route in MAJOR_AGENT_ROUTES
        for family in ("diverse", "deterministic")
        if route_family_counts[(route, family)] != 1
    ]
    duplicate_ids = sorted(case_id for case_id, count in Counter(ids).items() if count > 1)
    expected_routes = set(MAJOR_AGENT_ROUTES)
    observed_routes = {case.expected_route for case in DIVERSE_ASK_ACCEPTANCE_CASES}
    unexpected_routes = sorted(observed_routes - expected_routes)
    structurally_complete = not missing_pairs and not duplicate_ids and not unexpected_routes
    return {
        "schema": "keystone.diverse_ask_acceptance.v1",
        "status": "complete" if structurally_complete else "incomplete",
        "structurally_complete": structurally_complete,
        "case_count": len(DIVERSE_ASK_ACCEPTANCE_CASES),
        "agent_count": len({case.expected_route for case in DIVERSE_ASK_ACCEPTANCE_CASES}),
        "coverage_counts": dict(
            sorted(Counter(case.coverage_status for case in DIVERSE_ASK_ACCEPTANCE_CASES).items())
        ),
        "missing_route_family_pairs": missing_pairs,
        "unexpected_routes": unexpected_routes,
        "duplicate_case_ids": duplicate_ids,
        "behavioral_pass_claimed": False,
        "cases": [case.model_dump(mode="json") for case in DIVERSE_ASK_ACCEPTANCE_CASES],
    }


def automated_proof_nodeids() -> tuple[str, ...]:
    """Return unique executable proof nodes for rows claiming automation."""

    refs = {
        ref
        for case in DIVERSE_ASK_ACCEPTANCE_CASES
        if case.coverage_status == "automated"
        for ref in case.proof_refs
        if ref.startswith("tests/") and "::" in ref
    }
    return tuple(sorted(refs))


__all__ = [
    "DIVERSE_ASK_ACCEPTANCE_CASES",
    "MAJOR_AGENT_ROUTES",
    "DiverseAskAcceptanceCase",
    "automated_proof_nodeids",
    "build_diverse_ask_acceptance_report",
]
