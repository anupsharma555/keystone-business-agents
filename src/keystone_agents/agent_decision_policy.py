"""Canonical ownership boundaries for semantic decisions across KBA agents."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentDecisionPolicy:
    """Human- and test-readable contract for one registered agent."""

    route: str
    semantic_decisions: tuple[str, ...]
    deterministic_validators: tuple[str, ...]
    forbidden_shortcuts: tuple[str, ...]
    expected_model_read_tools: tuple[str, ...] = ()
    verified_continuation_allowed: bool = False


_POLICIES = (
    AgentDecisionPolicy(
        route="gmail_triage",
        semantic_decisions=(
            "choose the bounded Gmail query and plausible contexts to read",
            "select the matching conversation and explain excluded alternatives",
            "decide whether a reply is warranted and draft its wording",
        ),
        deterministic_validators=(
            "query and context-read ceilings",
            "selected message and thread identity belongs to the provider result set",
            "approval, no-send, exact draft scope, and provider read-back",
        ),
        forbidden_shortcuts=(
            "Python exact-time or first-result selection",
            "treating multiple candidates as an automatic blocker",
            "re-searching a phrase when an exact verified continuation exists",
        ),
        expected_model_read_tools=(
            "query_gmail_message_summaries",
            "read_gmail_context",
        ),
        verified_continuation_allowed=True,
    ),
    AgentDecisionPolicy(
        route="business_research_analyst",
        semantic_decisions=(
            "choose retrieval and bounded deepening based on the request",
            "rank sources and select claims relevant to the requested decision",
            "judge evidence sufficiency, relevance, and limitations",
        ),
        deterministic_validators=(
            "URL safety, provider budget, and fallback availability",
            "source identity, provenance, contradiction, and citation checks",
        ),
        forbidden_shortcuts=(
            "Python choosing the final sources or claims before specialist review",
            "successful synthesis from unverified or inaccessible sources",
        ),
        expected_model_read_tools=("search_web",),
    ),
    AgentDecisionPolicy(
        route="opportunity_scout",
        semantic_decisions=(
            "choose search lanes and whether focused deepening is useful",
            "classify, rank, and recommend opportunities for the operator objective",
        ),
        deterministic_validators=(
            "eligibility dates, closed or expired state, deduplication, and identity",
            "provider, retry, deepening-round, and score bounds",
        ),
        forbidden_shortcuts=(
            "Python-generated semantic ranking or deepening query",
            "fixed handoff independent of the selected opportunity evidence",
        ),
        expected_model_read_tools=("search_web",),
    ),
    AgentDecisionPolicy(
        route="outreach_composer",
        semantic_decisions=(
            "select approved claims, tone, subject, emphasis, CTA, and draft wording",
            "decide which approved evidence best supports the relationship goal",
        ),
        deterministic_validators=(
            "approved evidence and recipient identity",
            "unsupported claims, length, no-send, and approval gates",
        ),
        forbidden_shortcuts=(
            "presenting a deterministic fallback draft as successful live reasoning",
            "adding claims or recipients outside the approved candidate set",
        ),
        expected_model_read_tools=(
            "load_approved_contact_context",
            "check_unsupported_claims",
        ),
        verified_continuation_allowed=True,
    ),
    AgentDecisionPolicy(
        route="airtable_context_agent",
        semantic_decisions=(
            "inspect the bounded schema and choose the relevant table, records, and fields",
            "map the operator's requested change onto schema fields",
        ),
        deterministic_validators=(
            "base and table allowlists, field types, computed fields, and exact record identity",
            "approval, write scope, arithmetic, provider read-back, and cleanup gates",
        ),
        forbidden_shortcuts=(
            "preselecting a table or schema mapping from one prompt-specific lane",
            "first-record selection for an ambiguous record request",
        ),
        expected_model_read_tools=("airtable_get_base_schema", "airtable_read_records"),
        verified_continuation_allowed=True,
    ),
    AgentDecisionPolicy(
        route="google_workspace_context_agent",
        semantic_decisions=(
            "choose the relevant Drive file, document, sheet, slide deck, or folder",
            "interpret the requested artifact or edit against provider metadata",
        ),
        deterministic_validators=(
            "exact provider identity, MIME type, field mapping, approvals, and write scope",
            "read-back verification and bounded OCR or media limits",
        ),
        forbidden_shortcuts=(
            "silently choosing a default workbook, tab, or first duplicate folder",
            "creating a folder during a read-only validation path",
        ),
        expected_model_read_tools=(
            "google_drive_search_files",
            "google_drive_get_file_metadata",
        ),
        verified_continuation_allowed=True,
    ),
    AgentDecisionPolicy(
        route="zotero_context_agent",
        semantic_decisions=(
            "choose relevant collections and items from bounded Zotero results",
            "decide which item context, children, or attachment text merits reading",
        ),
        deterministic_validators=(
            "library, collection, item, and attachment identity",
            "read ceilings, approvals, write scope, and provider read-back",
        ),
        forbidden_shortcuts=(
            "Python ranking and selecting the final Zotero item before the specialist runs",
            "disabling tools after pre-acquiring an ambiguous item set",
        ),
        expected_model_read_tools=(
            "zotero_resolve_collection_context",
            "zotero_resolve_article_context",
        ),
        verified_continuation_allowed=True,
    ),
    AgentDecisionPolicy(
        route="rss_context_agent",
        semantic_decisions=(
            "judge feed-signal relevance and downstream follow-up value",
            "select and explain useful signals from bounded announcement history",
        ),
        deterministic_validators=(
            "feed identity, revision deduplication, lifecycle order, and checkpoint integrity",
            "idempotency, provider ceilings, and persisted stage evidence",
        ),
        forbidden_shortcuts=(
            "claiming an RSS agent decision when no model stage ran",
            "automatically completing every lifecycle stage without a decision artifact",
        ),
        expected_model_read_tools=(
            "retrieve_rss_announcement_history",
            "inspect_signal_lifecycle",
        ),
    ),
    AgentDecisionPolicy(
        route="preprints_context_agent",
        semantic_decisions=(
            "judge preprint relevance, evidentiary value, and recommended follow-up",
            "select and explain useful records from bounded preprint history",
        ),
        deterministic_validators=(
            "paper identity, revision deduplication, lifecycle order, and checkpoint integrity",
            "idempotency, provider ceilings, and persisted stage evidence",
        ),
        forbidden_shortcuts=(
            "claiming a Preprints agent decision when no model stage ran",
            "fixed downstream handoffs independent of specialist reasoning",
        ),
        expected_model_read_tools=(
            "retrieve_preprint_announcement_history",
            "inspect_signal_lifecycle",
        ),
    ),
    AgentDecisionPolicy(
        route="rag_retrieval_specialist",
        semantic_decisions=(
            "choose bounded semantic queries over the configured vector-store corpus",
            "rank retained matches and judge ambiguity, relevance, and evidence sufficiency",
            "synthesize the answer and claims from retained corpus evidence",
        ),
        deterministic_validators=(
            "configured hosted file-search availability before live model execution",
            "required file-search evidence and a maximum of two retrieval calls",
            "claim references belong to retained matches and not-found results retain no matches",
            "typed retrieval result, read-only operation, and no-send flags",
        ),
        forbidden_shortcuts=(
            "Python selecting the nearest article or composing the substantive corpus answer",
            "substituting web results or model knowledge for missing corpus evidence",
            "claiming retrieval from fixture output or a standard decision record not emitted",
        ),
        # Optional at builder configuration time; required by the live SDK wrapper.
        expected_model_read_tools=("file_search",),
    ),
    AgentDecisionPolicy(
        route="orchestrator",
        semantic_decisions=(
            "choose the owning specialist and ordered multi-agent workflow",
            "decide whether additional bounded provider context is needed",
        ),
        deterministic_validators=(
            "route ceiling, permissions, approvals, resume identity, and authority boundaries",
            "PHI, send, mutation, and provider-scope gates",
        ),
        forbidden_shortcuts=(
            "collapsing a typed multi-stage workflow to one heuristic route",
            "using phrase routing as semantic execution when the model stage fails",
        ),
        expected_model_read_tools=("load_orchestrator_workflow_state",),
        verified_continuation_allowed=True,
    ),
    AgentDecisionPolicy(
        route="chief_of_staff",
        semantic_decisions=(
            "choose manager-owned provider context and specialist delegation",
            "rank safe next actions using verified operational evidence",
        ),
        deterministic_validators=(
            "typed handoff route ceiling, permissions, approval state, and exact write scope",
            "provider identity, WorkItem receipts, and safe resume point",
        ),
        forbidden_shortcuts=(
            "inferring delegation from prose-only specialist names",
            "silently replacing failed model reasoning with Python semantic planning",
        ),
        expected_model_read_tools=("inspect_active_work_item_execution_summary",),
        verified_continuation_allowed=True,
    ),
)

_POLICY_BY_ROUTE = {policy.route: policy for policy in _POLICIES}


def list_agent_decision_policies() -> tuple[AgentDecisionPolicy, ...]:
    return _POLICIES


def get_agent_decision_policy(route: str) -> AgentDecisionPolicy:
    try:
        return _POLICY_BY_ROUTE[str(route).strip()]
    except KeyError as exc:
        raise KeyError(f"Unknown agent decision policy route: {route}") from exc


__all__ = [
    "AgentDecisionPolicy",
    "get_agent_decision_policy",
    "list_agent_decision_policies",
]
