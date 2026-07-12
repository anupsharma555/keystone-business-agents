"""Deterministic quality markers for LangGraph WorkItem comparisons."""
# ruff: noqa: E501

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

QUALITY_MARKERS_SCHEMA = "keystone.langgraph.quality_markers.v1"
QUALITY_COMPARISON_SCHEMA = "keystone.langgraph.quality_comparison.v1"
LIVE_OUTPUT_REVIEW_RUBRIC_SCHEMA = "keystone.langgraph.live_output_review_rubric.v1"
LIVE_OPEN_SMOKE_CHECKPOINT_SCHEMA = "keystone.langgraph.live_open_smoke_checkpoint.v1"
LIVE_OUTPUT_REVIEW_DECISION_SCHEMA = "keystone.langgraph.live_output_review_decision.v1"
USER_FACING_OUTPUT_SCORECARD_SCHEMA = "keystone.langgraph.user_facing_output_scorecard.v1"
USER_FACING_OUTPUT_COMPARISON_SCHEMA = "keystone.langgraph.user_facing_output_comparison.v1"
LIVE_SMOKE_PLAN_SCHEMA = "keystone.langgraph.live_smoke_plan.v1"
LLM_REASONING_TOUCHPOINTS_SCHEMA = "keystone.langgraph.llm_reasoning_touchpoints.v1"
APPROVED_MAX_LIVE_SDK_CALLS = 5
FORCED_COMPARISON_MIN_LIVE_SDK_CALLS = 2
INTERNAL_SDK_REQUEST_REVIEW_THRESHOLD_PER_RUN = 4
INTERNAL_SDK_REQUEST_COMPLEX_GRAPH_CANDIDATE_THRESHOLD = 8
LIVE_OUTPUT_GRAPH_EVIDENCE_MODES = {"forced_langgraph_true"}
LIVE_OUTPUT_GRAPH_EVENT_SCHEMA = "keystone.langgraph.orchestration.v1"
LIVE_OUTPUT_GRAPH_OFF_EVIDENCE_MODES = {"forced_langgraph_false_control"}
LIVE_OUTPUT_REQUIRED_EVIDENCE_MODES = {
    "forced_langgraph_false_control",
    "forced_langgraph_true",
}
EDGE_PROGRAM_INVENTORY_SCHEMA = "keystone.langgraph.edge_program_inventory.v1"
EDGE_PROGRAM_VALIDATION_SCHEMA = "keystone.langgraph.edge_program_validation.v1"
LIVE_OUTPUT_REVIEW_WINNERS = {"graph", "control", "tie", "unreviewed"}
_LIVE_PROMPT_STRIP_PATTERNS = (
    r"^\s*@KNI\s+",
    r"\b(?:business agents\s+)?(?:open/default|forced true|forced false|forced-on|forced-off)\s+LangGraph smoke:?\s*",
    r"\bLangGraph smoke\s*\d*:?\s*",
    r"\bLive SDK is approved only for this bounded[^.]*\.",
    r"\blive web search is not approved\.",
    r"\bUse local/dry-run retrieval where possible\.",
    r"\bDo not send, create Gmail drafts, post outside this thread, schedule, publish, create files, update Airtable/CRM/Drive/Sheets, or write external systems\.",
    r"\bAlso keep track of this run costs\.",
)
_LIVE_PROMPT_FORBIDDEN_PATTERNS = (
    ("harness_label", r"\b(?:open/default|forced true|forced false|forced-on|forced-off|LangGraph(?:\s+smoke)?)\b"),
    ("live_sdk_control", r"\bLive SDK is approved\b"),
    ("live_search_control", r"\blive web search is not approved\b"),
    ("dry_run_control", r"\bUse local/dry-run retrieval\b"),
    ("no_side_effect_inventory", r"\bDo not send, create Gmail drafts\b"),
    ("cost_tracking_control", r"\bkeep track of this run costs\b"),
)

LIVE_OUTPUT_REVIEW_RUBRIC = [
    {
        "criterion": "usefulness",
        "question": (
            "Does the graph run give the operator a clearer decision, next action, "
            "or work product than the graph-off run?"
        ),
    },
    {
        "criterion": "detail",
        "question": (
            "Does it include enough specific evidence, assumptions, missing facts, "
            "and stage outcomes without dumping raw route metadata?"
        ),
    },
    {
        "criterion": "relevance",
        "question": (
            "Does it stay focused on the requested company/topic and avoid generic "
            "or off-target opportunities?"
        ),
    },
    {
        "criterion": "evidence_quality",
        "question": (
            "Are source-backed claims visible, are weak/stale/unsupported claims "
            "flagged, and are evidence gaps preserved across handoffs?"
        ),
    },
    {
        "criterion": "safety_and_permissions",
        "question": (
            "Does it keep draft/send/post/write boundaries clear and avoid implying "
            "that review artifacts were externally delivered?"
        ),
    },
    {
        "criterion": "efficiency",
        "question": (
            "Did the graph improve the work without adding unnecessary live SDK "
            "calls or final prose synthesis just for verbosity?"
        ),
    },
]

_USER_FACING_REQUIRED_HEADINGS = (
    "recommendation",
    "strongest evidence",
    "main uncertainty",
    "next step",
    "draft for review",
)
_USER_FACING_BACKEND_TERMS = (
    "workitem",
    "artifact_id",
    "node_path",
    "completed_routes",
    "manager_loop",
    "gmail-thread:",
    "gmail:selected",
    "fixture:",
)
_ACTION_VERBS = (
    "review",
    "ask",
    "send",
    "confirm",
    "prepare",
    "schedule",
    "compare",
    "decide",
    "hold",
    "proceed",
)

_CONTEXT_PROVIDERS = {
    "rss_context_agent",
    "preprints_context_agent",
    "zotero_context_agent",
    "airtable_context_agent",
    "google_workspace_context_agent",
}
_DURABLE_ARTIFACT_TYPES = {
    "chief_of_staff_plan",
    "rss_context_summary",
    "preprints_context_summary",
    "zotero_context_summary",
    "airtable_context_summary",
    "google_workspace_context_summary",
    "company_profile",
    "opportunity",
    "outreach_draft",
    "gmail_thread_summary",
    "workspace_artifact_plan",
    "airtable_write_plan",
}
_REQUIRED_EDGE_INVARIANTS = (
    "work_items_sqlite_canonical_state",
    "typed_context_packs_preserved",
    "python_readiness_and_approval_gates_authoritative",
    "no_send_no_write_defaults",
    "source_attribution_visible_or_preserved",
    "offline_first_cost_aware_validation",
)
_LLM_REASONING_TOUCHPOINTS: tuple[dict[str, Any], ...] = (
    {
        "touchpoint": "relevance_usefulness_review",
        "owning_node": "manager_loop_finalize",
        "may_use_llm": True,
        "default_model_call": False,
        "purpose": (
            "Judge whether completed graph output is specific, useful, and relevant "
            "after deterministic stage and safety review."
        ),
        "deterministic_authority": (
            "status, approval, no-send/no-write, artifact/source presence, and blocker gates"
        ),
    },
    {
        "touchpoint": "repair_routing",
        "owning_node": "manager_loop_continue",
        "may_use_llm": True,
        "default_model_call": False,
        "purpose": (
            "Convert review feedback into observed gaps, repair route, requested output "
            "type, source issue, and next safe action."
        ),
        "deterministic_authority": (
            "repair eligibility, max repair attempts, side-effect blockers, and approval gates"
        ),
    },
    {
        "touchpoint": "source_sufficiency_judgment",
        "owning_node": "finalize_step",
        "may_use_llm": True,
        "default_model_call": False,
        "purpose": (
            "Assess whether available sources are actually adequate for the requested "
            "answer when deterministic source checks are ambiguous."
        ),
        "deterministic_authority": (
            "missing source refs, source count thresholds, provider reachability, "
            "recipient readiness, and live-search approval"
        ),
    },
    {
        "touchpoint": "final_answer_satisfaction",
        "owning_node": "manager_loop_finalize",
        "may_use_llm": True,
        "default_model_call": False,
        "purpose": (
            "Decide whether the final answer satisfies the latest user ask or needs "
            "a targeted rewrite/deepen/stop recommendation."
        ),
        "deterministic_authority": (
            "terminal status, checkpoint state, side-effect safety, and renderer contract"
        ),
    },
)
_DETERMINISTIC_GRAPH_NODES = (
    "normalize_request",
    "state_followup",
    "prepare_work_item",
    "stage_feed_context",
    "stage_zotero_context",
    "stage_google_workspace_context",
    "stage_airtable_context",
    "approval_checkpoint",
)
_LLM_REVIEW_TRIGGER_POLICY = (
    "deterministic_review_ambiguous_or_failed_without_hard_blocker",
    "high_value_multistep_route_research_opportunity_outreach",
    "high_value_multistep_route_gmail_research_outreach",
    "chief_led_coordination_with_final_output_quality_risk",
    "source_bundle_quality_or_answer_satisfaction_risk",
    "explicit_fake_or_live_review_mode_with_cost_budget",
)
_LLM_REVIEW_EVIDENCE_FIELDS = (
    "review_mode",
    "llm_review_used",
    "cost_guard",
    "deterministic_gates_authoritative",
)
_LLM_REPAIR_CONTEXT_FIELDS = (
    "observed_gaps",
    "recommended_next_step",
    "target_output_type",
    "source_issue",
    "repair_route",
    "qualitative_feedback",
    "review_mode",
    "llm_review_used",
    "cost_guard",
)
_LIVE_COMPARISON_MODES = (
    "open_default_backend_selected",
    "forced_langgraph_false_control",
    "forced_langgraph_true",
)
_LIVE_MODE_BASE_EVIDENCE_FIELDS = (
    "actual_environment",
    "slack_permalink",
    "work_item_id",
    "route",
    "status",
    "operator_status",
    "slack_display_title",
    "visible_output",
    "source_urls",
    "side_effects",
    "side_effects_reviewed",
    "workflow_sdk_usage_event",
)
_LIVE_MODE_GRAPH_EVIDENCE_FIELDS = ("langgraph_orchestration_event",)
_LIVE_SIDE_EFFECT_FLAGS = (
    "send_attempted",
    "gmail_draft_created",
    "external_post_created",
    "schedule_created",
    "external_write_performed",
)
_LIVE_WORKFLOW_SDK_USAGE_REQUIREMENTS = {
    "schema": "keystone.workflow_sdk_usage.v1",
    "usage": ("requests",),
    "cost": ("estimated_usd_or_amount_usd_or_source",),
    "request_cache": (
        "static_prefix_sha256_or_dynamic_prompt_sha256_or_prompt_cache_key_hash",
    ),
}
_SELECTED_EDGE_PROGRAM_ENTRIES: tuple[dict[str, Any], ...] = (
    {
        "edge_id": "business_research_opportunity_outreach_checkpoint",
        "edge": "business_research -> opportunity_scout -> outreach_composer -> approval_checkpoint",
        "status": "implemented_and_tested",
        "quality_scenarios": ("research-opportunity", "outreach-checkpoint"),
        "test_paths": ("tests/test_langgraph_workflow.py", "tests/test_langgraph_quality.py"),
        "doc_refs": ("docs/LANGGRAPH_OPTION.md",),
        "preserved_invariants": _REQUIRED_EDGE_INVARIANTS,
        "live_smoke_candidate": True,
    },
    {
        "edge_id": "gmail_research_outreach_checkpoint",
        "edge": "gmail_triage -> business_research -> outreach_composer -> approval_checkpoint",
        "status": "implemented_and_tested",
        "quality_scenarios": ("gmail-research-outreach", "gmail-research-thread-draft"),
        "test_paths": ("tests/test_langgraph_workflow.py", "tests/test_langgraph_quality.py"),
        "doc_refs": ("docs/LANGGRAPH_OPTION.md",),
        "preserved_invariants": _REQUIRED_EDGE_INVARIANTS,
        "live_smoke_candidate": True,
    },
    {
        "edge_id": "rss_preprints_zotero_signal_to_research_or_opportunity",
        "edge": "rss/preprints/zotero context -> business_research/opportunity_scout -> artifact planning",
        "status": "implemented_and_tested",
        "quality_scenarios": ("rss-opportunity", "preprints-zotero-research"),
        "test_paths": ("tests/test_langgraph_workflow.py", "tests/test_langgraph_quality.py"),
        "doc_refs": (
            "docs/LANGGRAPH_OPTION.md",
            "docs/corpus/resources/langgraph_operational_notes.md",
        ),
        "preserved_invariants": _REQUIRED_EDGE_INVARIANTS,
        "live_smoke_candidate": False,
    },
    {
        "edge_id": "chief_managed_coordination",
        "edge": "chief_of_staff -> selected context/specialist nodes -> review/finalize/checkpoint",
        "status": "implemented_and_tested",
        "handoff_contracts": (
            "ChiefOfStaffResult.durable_handoff.agent selects canonical WorkItem specialist",
            "ChiefOfStaffResult.context_handoffs selects read-only context staging agents",
            "legacy Chief of Staff -> Agent prose remains compatibility fallback only",
            "agents_as_tools advisory output must not replace durable graph specialist nodes",
        ),
        "quality_scenarios": (
            "chief-context-opportunity",
            "research-opportunity",
            "gmail-research-outreach",
        ),
        "test_paths": ("tests/test_langgraph_workflow.py", "tests/test_agent_registry.py"),
        "doc_refs": (
            "docs/LANGGRAPH_OPTION.md",
            "docs/assets/kba-current-agent-architecture.svg",
        ),
        "preserved_invariants": _REQUIRED_EDGE_INVARIANTS,
        "live_smoke_candidate": True,
    },
)
_FUTURE_EDGE_PROGRAM_ENTRIES: tuple[dict[str, Any], ...] = (
    {
        "edge_id": "approved_external_action_execution",
        "edge": "approval_checkpoint -> scoped approved provider mutation",
        "status": "future_not_selected_for_current_graph_program",
        "reason": (
            "External sends, Gmail drafts, posts, Airtable/Workspace writes, and "
            "Zotero imports require separate specialist/action-handler execution "
            "paths after scoped approval."
        ),
    },
)


def langgraph_edge_program_inventory() -> dict[str, Any]:
    """Return the machine-readable inventory for selected LangGraph edge work."""

    return {
        "schema": EDGE_PROGRAM_INVENTORY_SCHEMA,
        "selected_edges": [_copy_inventory_entry(item) for item in _SELECTED_EDGE_PROGRAM_ENTRIES],
        "future_edges": [_copy_inventory_entry(item) for item in _FUTURE_EDGE_PROGRAM_ENTRIES],
        "required_invariants": list(_REQUIRED_EDGE_INVARIANTS),
        "llm_reasoning_policy": langgraph_llm_reasoning_touchpoints(),
        "live_smoke_boundary": {
            "max_live_sdk_calls": APPROVED_MAX_LIVE_SDK_CALLS,
            "live_api_test_budget": APPROVED_MAX_LIVE_SDK_CALLS,
            "internal_sdk_request_review_threshold_per_run": (
                INTERNAL_SDK_REQUEST_REVIEW_THRESHOLD_PER_RUN
            ),
            "internal_sdk_request_complex_graph_candidate_threshold": (
                INTERNAL_SDK_REQUEST_COMPLEX_GRAPH_CANDIDATE_THRESHOLD
            ),
            "serial_only": True,
            "offline_first": True,
            "comparison_modes": list(_LIVE_COMPARISON_MODES),
            "mode_evidence_requirements": {
                "open_default_backend_selected": list(_LIVE_MODE_BASE_EVIDENCE_FIELDS),
                "forced_langgraph_false_control": list(_LIVE_MODE_BASE_EVIDENCE_FIELDS),
                "forced_langgraph_true": list(
                    _LIVE_MODE_BASE_EVIDENCE_FIELDS + _LIVE_MODE_GRAPH_EVIDENCE_FIELDS
                ),
            },
            "workflow_sdk_usage_requirements": {
                "schema": _LIVE_WORKFLOW_SDK_USAGE_REQUIREMENTS["schema"],
                "usage": list(_LIVE_WORKFLOW_SDK_USAGE_REQUIREMENTS["usage"]),
                "cost": list(_LIVE_WORKFLOW_SDK_USAGE_REQUIREMENTS["cost"]),
                "request_cache": list(_LIVE_WORKFLOW_SDK_USAGE_REQUIREMENTS["request_cache"]),
            },
            "side_effect_requirements": {
                "all_flags_present": True,
                "all_flags_false": True,
                "reviewed_field": "side_effects_reviewed",
                "reviewed_value": True,
                "flags": list(_LIVE_SIDE_EFFECT_FLAGS),
            },
            "first_run_gate": "open_default_backend_selected must pass before forced modes",
            "live_search_default": False,
            "external_writes_enabled": False,
            "send_enabled": False,
        },
        "notes": [
            "This inventory is structural. It proves coverage surfaces exist, not that live prose is better.",
            "Use the live output review packet before claiming graph output is more useful than control.",
        ],
    }


def langgraph_llm_reasoning_touchpoints() -> dict[str, Any]:
    """Return the minimal contained LLM-review contract for graph workflows.

    This contract documents where manager-style model judgment may be plugged in
    without changing default graph execution. It is deliberately a policy shape:
    model calls remain opt-in/fake/live-review specific, and Python gates keep
    authority over safety, approvals, source presence, and side effects.
    """

    return {
        "schema": LLM_REASONING_TOUCHPOINTS_SCHEMA,
        "default_api_calls": False,
        "model_reasoning_may_run_at": [_copy_inventory_entry(item) for item in _LLM_REASONING_TOUCHPOINTS],
        "deterministic_only_nodes": list(_DETERMINISTIC_GRAPH_NODES),
        "trigger_policy": list(_LLM_REVIEW_TRIGGER_POLICY),
        "evidence_fields": list(_LLM_REVIEW_EVIDENCE_FIELDS),
        "manager_loop_repair_context_fields": list(_LLM_REPAIR_CONTEXT_FIELDS),
        "deterministic_gates_authoritative": True,
        "live_validation_policy": (
            "Live validation is outside normal graph execution and must use the "
            "KBA cost-aware live SDK boundary with a separate budget."
        ),
    }


def validate_langgraph_edge_program_inventory(
    inventory: dict[str, Any] | None = None,
    *,
    known_quality_scenarios: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate that selected edge inventory entries name coverage and invariants."""

    data = inventory or langgraph_edge_program_inventory()
    blockers: list[str] = []
    known_scenarios = (
        {str(item) for item in known_quality_scenarios}
        if known_quality_scenarios is not None
        else None
    )
    selected_edges = data.get("selected_edges")
    if not isinstance(selected_edges, list) or not selected_edges:
        blockers.append("missing_selected_edges")
        selected_edges = []
    required_invariants = set(_string_list(data.get("required_invariants")))
    if required_invariants != set(_REQUIRED_EDGE_INVARIANTS):
        blockers.append("required_invariants_changed")
    if data.get("llm_reasoning_policy") != langgraph_llm_reasoning_touchpoints():
        blockers.append("llm_reasoning_policy_changed")
    for item in selected_edges:
        if not isinstance(item, dict):
            blockers.append("invalid_edge_entry")
            continue
        edge_id = str(item.get("edge_id") or "unknown")
        if item.get("status") != "implemented_and_tested":
            blockers.append(f"{edge_id}:status_not_implemented_and_tested")
        if not _string_list(item.get("test_paths")):
            blockers.append(f"{edge_id}:missing_test_paths")
        if not _string_list(item.get("doc_refs")):
            blockers.append(f"{edge_id}:missing_doc_refs")
        quality_scenarios = _string_list(item.get("quality_scenarios"))
        if not quality_scenarios:
            blockers.append(f"{edge_id}:missing_quality_scenarios")
        if known_scenarios is not None:
            missing_scenarios = sorted(
                scenario for scenario in quality_scenarios if scenario not in known_scenarios
            )
            if missing_scenarios:
                blockers.append(
                    f"{edge_id}:unknown_quality_scenarios:{','.join(missing_scenarios)}"
                )
        invariants = set(_string_list(item.get("preserved_invariants")))
        missing = sorted(required_invariants - invariants)
        if missing:
            blockers.append(f"{edge_id}:missing_invariants:{','.join(missing)}")
    boundary = data.get("live_smoke_boundary")
    if not isinstance(boundary, dict):
        blockers.append("missing_live_smoke_boundary")
        boundary = {}
    if int(boundary.get("max_live_sdk_calls") or 0) != APPROVED_MAX_LIVE_SDK_CALLS:
        blockers.append("live_smoke_boundary:max_live_sdk_calls_not_approved_cap")
    if tuple(_string_list(boundary.get("comparison_modes"))) != _LIVE_COMPARISON_MODES:
        blockers.append("live_smoke_boundary:comparison_modes_changed")
    mode_requirements = boundary.get("mode_evidence_requirements")
    if not isinstance(mode_requirements, dict):
        blockers.append("live_smoke_boundary:missing_mode_evidence_requirements")
        mode_requirements = {}
    for mode in _LIVE_COMPARISON_MODES:
        expected_fields = list(_LIVE_MODE_BASE_EVIDENCE_FIELDS)
        if mode in LIVE_OUTPUT_GRAPH_EVIDENCE_MODES:
            expected_fields.extend(_LIVE_MODE_GRAPH_EVIDENCE_FIELDS)
        if _string_list(mode_requirements.get(mode)) != expected_fields:
            blockers.append(f"live_smoke_boundary:{mode}:evidence_requirements_changed")
    usage_requirements = boundary.get("workflow_sdk_usage_requirements")
    expected_usage_requirements = {
        "schema": _LIVE_WORKFLOW_SDK_USAGE_REQUIREMENTS["schema"],
        "usage": list(_LIVE_WORKFLOW_SDK_USAGE_REQUIREMENTS["usage"]),
        "cost": list(_LIVE_WORKFLOW_SDK_USAGE_REQUIREMENTS["cost"]),
        "request_cache": list(_LIVE_WORKFLOW_SDK_USAGE_REQUIREMENTS["request_cache"]),
    }
    if usage_requirements != expected_usage_requirements:
        blockers.append("live_smoke_boundary:workflow_sdk_usage_requirements_changed")
    side_effect_requirements = boundary.get("side_effect_requirements")
    expected_side_effect_requirements = {
        "all_flags_present": True,
        "all_flags_false": True,
        "reviewed_field": "side_effects_reviewed",
        "reviewed_value": True,
        "flags": list(_LIVE_SIDE_EFFECT_FLAGS),
    }
    if side_effect_requirements != expected_side_effect_requirements:
        blockers.append("live_smoke_boundary:side_effect_requirements_changed")
    for flag in ("offline_first", "serial_only"):
        if boundary.get(flag) is not True:
            blockers.append(f"live_smoke_boundary:{flag}_not_true")
    for flag in ("external_writes_enabled", "send_enabled", "live_search_default"):
        if boundary.get(flag) is not False:
            blockers.append(f"live_smoke_boundary:{flag}_not_false")
    return {
        "schema": EDGE_PROGRAM_VALIDATION_SCHEMA,
        "valid": not blockers,
        "blockers": blockers,
        "selected_edge_count": len(selected_edges),
        "future_edge_count": len(data.get("future_edges") or []),
    }


def render_langgraph_edge_program_inventory(inventory: dict[str, Any] | None = None) -> str:
    """Render a compact operator-facing summary of the edge inventory."""

    data = inventory or langgraph_edge_program_inventory()
    validation = validate_langgraph_edge_program_inventory(data)
    boundary = data.get("live_smoke_boundary") if isinstance(data, dict) else {}
    if not isinstance(boundary, dict):
        boundary = {}
    lines = [
        "LangGraph edge program inventory",
        f"- Selected durable edges: {int(validation.get('selected_edge_count') or 0)}",
        f"- Inventory valid: {_yes_no(validation.get('valid'))}",
        f"- Max live SDK calls: {int(boundary.get('max_live_sdk_calls') or 0)}",
        "- Internal SDK request review threshold per run: "
        f"{int(boundary.get('internal_sdk_request_review_threshold_per_run') or 0)}",
        f"- Offline first: {_yes_no(boundary.get('offline_first'))}",
    ]
    policy = data.get("llm_reasoning_policy")
    if isinstance(policy, dict):
        touchpoints = policy.get("model_reasoning_may_run_at")
        evidence_fields = _string_list(policy.get("evidence_fields"))
        lines.append(
            "- Default LLM manager-review API calls: "
            f"{_yes_no(bool(policy.get('default_api_calls')))}"
        )
        lines.append(
            "- Optional LLM reasoning touchpoints: "
            f"{len(touchpoints) if isinstance(touchpoints, list) else 0}"
        )
        if evidence_fields:
            lines.append("- LLM review evidence fields: " + ", ".join(evidence_fields))
    mode_requirements = boundary.get("mode_evidence_requirements")
    if isinstance(mode_requirements, dict):
        for mode in _LIVE_COMPARISON_MODES:
            fields = _string_list(mode_requirements.get(mode))
            extra_fields = [
                field for field in fields if field not in _LIVE_MODE_BASE_EVIDENCE_FIELDS
            ]
            detail = f"{len(fields)} fields"
            if extra_fields:
                detail += f"; extra: {', '.join(extra_fields)}"
            lines.append(f"- Evidence {mode}: {detail}")
    usage_requirements = boundary.get("workflow_sdk_usage_requirements")
    if isinstance(usage_requirements, dict):
        usage_fields = _string_list(usage_requirements.get("usage"))
        cost_fields = _string_list(usage_requirements.get("cost"))
        cache_fields = _string_list(usage_requirements.get("request_cache"))
        if usage_fields or cost_fields or cache_fields:
            lines.append(
                "- Workflow SDK usage evidence: "
                f"usage={','.join(usage_fields) or 'n/a'}; "
                f"cost={','.join(cost_fields) or 'n/a'}; "
                f"request_cache={','.join(cache_fields) or 'n/a'}"
            )
    side_effect_requirements = boundary.get("side_effect_requirements")
    if isinstance(side_effect_requirements, dict):
        side_effect_flags = _string_list(side_effect_requirements.get("flags"))
        if side_effect_flags:
            lines.append(
                "- Side-effect evidence: reviewed=true and all flags present and false: "
                + ", ".join(side_effect_flags)
            )
    for item in data.get("selected_edges") or []:
        if not isinstance(item, dict):
            continue
        lines.append(
            "- "
            f"{str(item.get('edge_id') or 'unknown')}: "
            f"{str(item.get('status') or 'unknown')}"
        )
    blockers = _string_list(validation.get("blockers"))
    if blockers:
        lines.append(f"- Blockers: {', '.join(blockers)}")
    return "\n".join(lines)


def user_facing_output_quality_scorecard(
    summary: str,
    *,
    expected_target_terms: Iterable[str] = (),
    openai_requests: int = 0,
    total_tokens: int = 0,
    max_openai_requests: int = 1,
    max_total_tokens: int = 50000,
    side_effects: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Score visible operator output with bounded deterministic signals."""

    text = str(summary or "").strip()
    lowered = text.lower()
    heading_counts = {
        heading: len(re.findall(rf"(?im)^\*{re.escape(heading)}:\*", text))
        for heading in _USER_FACING_REQUIRED_HEADINGS
    }
    present_headings = sum(count > 0 for count in heading_counts.values())
    completeness = 2 if present_headings == len(heading_counts) else 1 if present_headings >= 3 else 0
    evidence_section = _visible_section(text, "strongest evidence")
    evidence_bullets = len(re.findall(r"(?m)^-\s+\S", evidence_section))
    evidence_use = 2 if evidence_bullets >= 2 else 1 if evidence_section.strip() else 0
    target_terms = [str(term).strip().lower() for term in expected_target_terms if str(term).strip()]
    matched_terms = [term for term in target_terms if term in lowered]
    relevance = 2 if target_terms and len(matched_terms) == len(target_terms) else 1 if matched_terms else 0
    paragraphs = [" ".join(part.lower().split()) for part in re.split(r"\n\s*\n", text) if part.strip()]
    duplicate_paragraphs = len(paragraphs) - len(set(paragraphs))
    duplication = (
        2
        if duplicate_paragraphs == 0 and all(count <= 1 for count in heading_counts.values())
        else 1
        if duplicate_paragraphs <= 1
        else 0
    )
    next_step = _visible_section(text, "next step")
    actionability = (
        2
        if next_step and any(re.search(rf"\b{verb}\b", next_step, flags=re.I) for verb in _ACTION_VERBS)
        else 1
        if next_step
        else 0
    )
    backend_terms = [term for term in _USER_FACING_BACKEND_TERMS if term in lowered]
    slack_readability = (
        2
        if text and len(text) <= 3000 and present_headings >= 3 and not backend_terms
        else 1
        if text and len(text) <= 5000
        else 0
    )
    efficiency = (
        2
        if openai_requests <= max_openai_requests and total_tokens <= max_total_tokens
        else 1
        if openai_requests <= max_openai_requests + 1
        else 0
    )
    side_effect_flags = dict(side_effects or {})
    side_effect_names = [name for name, occurred in side_effect_flags.items() if occurred]
    side_effect_score = 2 if not side_effect_names else 0
    dimensions = {
        "answer_completeness": completeness,
        "evidence_use": evidence_use,
        "relevance": relevance,
        "duplication_control": duplication,
        "actionable_next_step": actionability,
        "slack_readability": slack_readability,
        "request_token_efficiency": efficiency,
        "side_effect_safety": side_effect_score,
    }
    return {
        "schema": USER_FACING_OUTPUT_SCORECARD_SCHEMA,
        "dimensions": dimensions,
        "total_score": sum(dimensions.values()),
        "max_score": 16,
        "passed": sum(dimensions.values()) >= 14 and all(score > 0 for score in dimensions.values()),
        "diagnostics": {
            "present_headings": present_headings,
            "matched_target_terms": matched_terms,
            "duplicate_paragraph_count": duplicate_paragraphs,
            "backend_terms": backend_terms,
            "side_effects": side_effect_names,
            "openai_requests": int(openai_requests),
            "total_tokens": int(total_tokens),
        },
    }


def compare_user_facing_output_quality(
    control: dict[str, Any],
    graph: dict[str, Any],
) -> dict[str, Any]:
    """Compare matched visible-output scorecards without changing safety authority."""

    control_dimensions = dict(control.get("dimensions") or {})
    graph_dimensions = dict(graph.get("dimensions") or {})
    dimension_deltas = {
        name: int(graph_dimensions.get(name) or 0) - int(control_dimensions.get(name) or 0)
        for name in sorted(set(control_dimensions) | set(graph_dimensions))
    }
    return {
        "schema": USER_FACING_OUTPUT_COMPARISON_SCHEMA,
        "control_score": int(control.get("total_score") or 0),
        "graph_score": int(graph.get("total_score") or 0),
        "score_delta": int(graph.get("total_score") or 0) - int(control.get("total_score") or 0),
        "dimension_deltas": dimension_deltas,
        "graph_quality_improved": bool(
            int(graph.get("total_score") or 0) > int(control.get("total_score") or 0)
            and graph.get("passed") is True
            and int(graph_dimensions.get("side_effect_safety") or 0) == 2
            and int(graph_dimensions.get("request_token_efficiency") or 0)
            >= int(control_dimensions.get("request_token_efficiency") or 0)
        ),
    }


def _visible_section(text: str, heading: str) -> str:
    match = re.search(
        rf"(?ims)^\*{re.escape(heading)}:\*\s*(?P<body>.*?)"
        r"(?=^\*[^\n]+:\*|\Z)",
        str(text or ""),
    )
    return match.group("body").strip() if match is not None else ""


def langgraph_quality_markers(result: Any, events: Iterable[Any]) -> dict[str, Any]:
    """Summarize deterministic output-quality markers for one WorkItem run.

    These markers are deliberately structural. They do not claim the prose is
    better; they prove whether a graph run preserved route/safety while adding
    durable context, source, artifact, and explainability surfaces.
    """

    work_item = getattr(result, "work_item", None)
    artifact_refs = list(getattr(work_item, "artifact_refs", []) or [])
    source_refs = list(getattr(work_item, "sources", []) or [])
    artifact_types = {str(getattr(artifact, "artifact_type", "") or "") for artifact in artifact_refs}
    source_providers = {str(getattr(source, "provider", "") or "") for source in source_refs}
    graph_metadata = _latest_event_metadata(events, "langgraph_orchestration")
    completion_review = graph_metadata.get("graph_completion_review")
    if not isinstance(completion_review, dict):
        completion_review = {}
    stage_statuses = _stage_statuses(completion_review)
    safety_violations = _safety_violations(artifact_refs)
    return {
        "schema": QUALITY_MARKERS_SCHEMA,
        "route": _enum_value(getattr(result, "route", "")),
        "status": _enum_value(getattr(result, "status", "")),
        "artifact_types": sorted(item for item in artifact_types if item),
        "source_providers": sorted(item for item in source_providers if item),
        "context_evidence_count": len(_CONTEXT_PROVIDERS & source_providers),
        "durable_stage_count": len(_DURABLE_ARTIFACT_TYPES & artifact_types),
        "graph_explainability": bool(
            graph_metadata.get("node_path")
            and completion_review.get("schema") == "keystone.langgraph.completion_review.v1"
        ),
        "stage_statuses": stage_statuses,
        "completed_requested_stage_count": sum(
            1 for status in stage_statuses.values() if status == "completed"
        ),
        "missing_required_stages": list(completion_review.get("missing_required_stages") or []),
        "checkpoint_required": bool(completion_review.get("checkpoint_required")),
        "side_effect_safe": not safety_violations,
        "safety_violations": safety_violations,
    }


def compare_langgraph_quality(
    control: dict[str, Any],
    graph: dict[str, Any],
    *,
    allow_route_change: bool = False,
    expected_graph_route: str = "",
) -> dict[str, Any]:
    """Compare graph-off/control markers with backend-selected graph markers."""

    context_delta = int(graph.get("context_evidence_count") or 0) - int(
        control.get("context_evidence_count") or 0
    )
    durable_delta = int(graph.get("durable_stage_count") or 0) - int(
        control.get("durable_stage_count") or 0
    )
    same_route = bool(graph.get("route") == control.get("route"))
    graph_route_matches_expected = bool(
        expected_graph_route and graph.get("route") == expected_graph_route
    )
    route_ok = same_route or bool(allow_route_change and graph_route_matches_expected)
    same_status = bool(graph.get("status") == control.get("status"))
    status_improved = bool(
        str(control.get("status") or "") in {"blocked", "needs_approval", "in_progress"}
        and str(graph.get("status") or "") == "done"
    )
    completed_stage_delta = int(graph.get("completed_requested_stage_count") or 0) - int(
        control.get("completed_requested_stage_count") or 0
    )
    graph_explainability_added = bool(
        graph.get("graph_explainability") and not control.get("graph_explainability")
    )
    side_effect_safe_both = bool(graph.get("side_effect_safe") and control.get("side_effect_safe"))
    improvement_markers: list[str] = []
    regression_markers: list[str] = []
    if context_delta > 0:
        improvement_markers.append("context_evidence_added")
    if durable_delta > 0:
        improvement_markers.append("durable_stages_added")
    if graph_explainability_added:
        improvement_markers.append("graph_explainability_added")
    if completed_stage_delta > 0:
        improvement_markers.append("requested_stage_trace_added")
    if allow_route_change and graph_route_matches_expected and not same_route:
        improvement_markers.append("graph_enabled_expected_route")
    if status_improved:
        improvement_markers.append("status_improved")
    if not route_ok:
        regression_markers.append("route_changed")
    if not same_status and not status_improved:
        regression_markers.append("status_changed")
    if not side_effect_safe_both:
        regression_markers.append("side_effect_safety_regressed")
    if graph.get("missing_required_stages"):
        regression_markers.append("missing_required_graph_stage")
    ready_for_live_smoke = bool(
        route_ok
        and side_effect_safe_both
        and not regression_markers
        and (
            context_delta > 0
            or durable_delta > 0
            or graph_explainability_added
            or completed_stage_delta > 0
        )
    )
    return {
        "schema": QUALITY_COMPARISON_SCHEMA,
        "same_route": same_route,
        "route_change_allowed": bool(allow_route_change),
        "expected_graph_route": expected_graph_route,
        "graph_route_matches_expected": graph_route_matches_expected,
        "same_status": same_status,
        "status_improved": status_improved,
        "context_evidence_delta": context_delta,
        "durable_stage_delta": durable_delta,
        "completed_requested_stage_delta": completed_stage_delta,
        "graph_explainability_added": graph_explainability_added,
        "side_effect_safe_both": side_effect_safe_both,
        "improvement_markers": improvement_markers,
        "regression_markers": regression_markers,
        "ready_for_live_smoke": ready_for_live_smoke,
    }


def render_langgraph_quality_comparison(comparison: dict[str, Any]) -> str:
    """Render a compact operator-facing summary of a graph quality comparison."""

    expected_route = str(comparison.get("expected_graph_route") or "")
    graph_route_match = (
        _yes_no(comparison.get("graph_route_matches_expected")) if expected_route else "n/a"
    )
    lines = [
        "LangGraph quality comparison",
        f"- Same route: {_yes_no(comparison.get('same_route'))}",
        f"- Route change allowed: {_yes_no(comparison.get('route_change_allowed'))}",
        f"- Graph route matches expected: {graph_route_match}",
        f"- Same status: {_yes_no(comparison.get('same_status'))}",
        f"- Context evidence delta: {int(comparison.get('context_evidence_delta') or 0):+d}",
        f"- Durable stage delta: {int(comparison.get('durable_stage_delta') or 0):+d}",
        "- Completed requested-stage delta: "
        f"{int(comparison.get('completed_requested_stage_delta') or 0):+d}",
        f"- Graph explainability added: {_yes_no(comparison.get('graph_explainability_added'))}",
        f"- Side-effect safe in both runs: {_yes_no(comparison.get('side_effect_safe_both'))}",
        f"- Ready for bounded live smoke: {_yes_no(comparison.get('ready_for_live_smoke'))}",
    ]
    improvements = _string_list(comparison.get("improvement_markers"))
    regressions = _string_list(comparison.get("regression_markers"))
    if improvements:
        lines.append(f"- Improvement markers: {', '.join(improvements)}")
    if regressions:
        lines.append(f"- Regression markers: {', '.join(regressions)}")
    return "\n".join(lines)


def langgraph_live_output_review_rubric() -> dict[str, Any]:
    """Return the human review rubric for live graph-vs-control output comparisons."""

    return {
        "schema": LIVE_OUTPUT_REVIEW_RUBRIC_SCHEMA,
        "use_after": "offline_quality_comparison_ready",
        "criteria": [dict(item) for item in LIVE_OUTPUT_REVIEW_RUBRIC],
        "minimum_better_than_control": [
            "usefulness",
            "relevance",
            "evidence_quality",
            "safety_and_permissions",
        ],
        "note": (
            "The offline graph-quality markers are necessary but not sufficient; "
            "live Slack/API output still needs this usefulness review."
        ),
    }


def langgraph_live_output_review_packet(
    *,
    request_text: str,
    control_output: dict[str, Any],
    graph_output: dict[str, Any],
    quality_target_terms: Iterable[str] = (),
    all_scenarios_ready: bool | None = None,
    all_scenarios_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the review packet for judging visible live output quality.

    The packet includes a bounded deterministic visible-output scorecard when
    complete forced-mode evidence is attached. Human usefulness and judgment
    review remains authoritative.
    """

    rubric = langgraph_live_output_review_rubric()
    packet = {
        "schema": "keystone.langgraph.live_output_review_packet.v1",
        "request_text": request_text,
        "rubric": rubric,
        "minimum_better_than_control": rubric["minimum_better_than_control"],
        "all_scenarios_ready": all_scenarios_ready,
        "all_scenarios_summary": dict(all_scenarios_summary or {}),
        "control_output": control_output,
        "graph_output": graph_output,
        "quality_target_terms": [
            str(term).strip() for term in quality_target_terms if str(term).strip()
        ],
        "mode_evidence": [
            _live_output_mode_evidence_template(
                mode="open_default_backend_selected",
                expected_environment={"KEYSTONE_WORKITEM_LANGGRAPH": "unset"},
            ),
            _live_output_mode_evidence_template(
                mode="forced_langgraph_false_control",
                expected_environment={"KEYSTONE_WORKITEM_LANGGRAPH": "false"},
            ),
            _live_output_mode_evidence_template(
                mode="forced_langgraph_true",
                expected_environment={"KEYSTONE_WORKITEM_LANGGRAPH": "true"},
            ),
        ],
        "internal_sdk_request_policy": {
            "review_threshold_per_run": INTERNAL_SDK_REQUEST_REVIEW_THRESHOLD_PER_RUN,
            "complex_graph_candidate_threshold": (
                INTERNAL_SDK_REQUEST_COMPLEX_GRAPH_CANDIDATE_THRESHOLD
            ),
            "threshold_type": "per_run_internal_sdk_requests",
            "separate_from_live_api_test_budget": True,
            "review_blocker": "internal_sdk_request_threshold_exceeded",
            "migration_requirement": (
                "Raising the bounded-smoke threshold toward 8 requires trace evidence "
                "that added calls improve relevance, detail, planning quality, or "
                "execution quality."
            ),
        },
        "review_questions": [
            {
                "criterion": item["criterion"],
                "question": item["question"],
                "control_observation": "",
                "graph_observation": "",
                "winner": "unreviewed",
            }
            for item in rubric["criteria"]
        ],
        "decision": "unreviewed",
        "user_facing_output_quality": {
            "status": "pending_live_mode_evidence",
            "control": {},
            "graph": {},
            "comparison": {},
        },
        "note": (
            "Use this packet after the live Slack/API run output is available. "
            "The graph should be considered better only if it improves the "
            "minimum criteria without safety or permission regressions."
        ),
    }
    return packet


def langgraph_live_user_facing_output_quality(packet: dict[str, Any]) -> dict[str, Any]:
    """Score complete forced-mode visible output without replacing human review."""

    control = _mode_evidence(packet, "forced_langgraph_false_control")
    graph = _mode_evidence(packet, "forced_langgraph_true")
    missing: list[str] = []
    for label, evidence in (("control", control), ("graph", graph)):
        if not str(evidence.get("visible_output") or "").strip():
            missing.append(f"{label}.visible_output")
        if evidence.get("side_effects_reviewed") is not True:
            missing.append(f"{label}.side_effects_reviewed")
        if not _workflow_sdk_usage_events(evidence):
            missing.append(f"{label}.workflow_sdk_usage_event")
    if missing:
        return {
            "status": "pending_live_mode_evidence",
            "missing": missing,
            "control": {},
            "graph": {},
            "comparison": {},
        }
    control_requests, control_tokens = _workflow_sdk_usage_totals(control)
    graph_requests, graph_tokens = _workflow_sdk_usage_totals(graph)
    target_terms = _string_list(packet.get("quality_target_terms"))
    control_score = user_facing_output_quality_scorecard(
        str(control.get("visible_output") or ""),
        expected_target_terms=target_terms,
        openai_requests=control_requests,
        total_tokens=control_tokens,
        max_openai_requests=max(1, control_requests),
        max_total_tokens=max(1, control_tokens),
        side_effects=_boolean_mapping(control.get("side_effects")),
    )
    graph_score = user_facing_output_quality_scorecard(
        str(graph.get("visible_output") or ""),
        expected_target_terms=target_terms,
        openai_requests=graph_requests,
        total_tokens=graph_tokens,
        max_openai_requests=max(1, control_requests),
        max_total_tokens=max(1, control_tokens),
        side_effects=_boolean_mapping(graph.get("side_effects")),
    )
    return {
        "status": "scored",
        "missing": [],
        "control": control_score,
        "graph": graph_score,
        "comparison": compare_user_facing_output_quality(control_score, graph_score),
    }


def finalize_langgraph_live_output_review(packet: dict[str, Any]) -> dict[str, Any]:
    """Return a deterministic decision from a completed live output review packet."""

    automated_output_quality = langgraph_live_user_facing_output_quality(packet)
    minimum = set(_string_list(packet.get("minimum_better_than_control")))
    review_questions = packet.get("review_questions")
    if not isinstance(review_questions, list):
        review_questions = []
    winners: dict[str, str] = {}
    unreviewed: list[str] = []
    invalid: list[str] = []
    missing_observations: list[str] = []
    for item in review_questions:
        if not isinstance(item, dict):
            continue
        criterion = str(item.get("criterion") or "").strip()
        if not criterion:
            continue
        winner = str(item.get("winner") or "unreviewed").strip().lower()
        if winner not in LIVE_OUTPUT_REVIEW_WINNERS:
            invalid.append(criterion)
            winner = "unreviewed"
        winners[criterion] = winner
        if winner == "unreviewed":
            unreviewed.append(criterion)
        else:
            for field in ("control_observation", "graph_observation"):
                if not str(item.get(field) or "").strip():
                    missing_observations.append(f"{criterion}.{field}")

    missing_minimum = sorted(criterion for criterion in minimum if criterion not in winners)
    minimum_graph_wins = sorted(
        criterion for criterion in minimum if winners.get(criterion) == "graph"
    )
    minimum_control_wins = sorted(
        criterion for criterion in minimum if winners.get(criterion) == "control"
    )
    minimum_ties = sorted(criterion for criterion in minimum if winners.get(criterion) == "tie")
    blockers = list(missing_minimum)
    if unreviewed:
        blockers.extend(f"unreviewed:{criterion}" for criterion in unreviewed)
    if invalid:
        blockers.extend(f"invalid_winner:{criterion}" for criterion in invalid)
    if missing_observations:
        blockers.extend(
            f"missing_review_observation:{item}" for item in sorted(missing_observations)
        )
    evidence_blockers = _live_output_evidence_blockers(packet)
    blockers.extend(evidence_blockers)
    live_sdk_request_count = _live_sdk_request_count(packet)
    live_api_test_attempt_count = _live_api_test_attempt_count(packet)
    internal_threshold_blockers = _internal_sdk_request_threshold_blockers(packet)
    efficiency_winner = winners.get("efficiency", "unreviewed")
    safety_winner = winners.get("safety_and_permissions", "unreviewed")
    graph_meets_minimum = bool(
        minimum
        and not blockers
        and not minimum_control_wins
        and not minimum_ties
        and len(minimum_graph_wins) == len(minimum)
    )
    if blockers:
        decision = "unreviewed"
    elif graph_meets_minimum and efficiency_winner != "control":
        decision = "graph_better"
    elif minimum_control_wins or safety_winner == "control":
        decision = "control_better"
    else:
        decision = "inconclusive"

    return {
        "schema": LIVE_OUTPUT_REVIEW_DECISION_SCHEMA,
        "decision": decision,
        "winners": winners,
        "minimum_better_than_control": sorted(minimum),
        "minimum_graph_wins": minimum_graph_wins,
        "minimum_control_wins": minimum_control_wins,
        "minimum_ties": minimum_ties,
        "unreviewed_criteria": unreviewed,
        "invalid_winner_criteria": invalid,
        "missing_observations": sorted(missing_observations),
        "evidence_blockers": evidence_blockers,
        "blockers": blockers,
        "live_sdk_request_count": live_sdk_request_count,
        "approved_max_live_sdk_calls": APPROVED_MAX_LIVE_SDK_CALLS,
        "live_api_test_attempt_count": live_api_test_attempt_count,
        "approved_live_api_test_budget": APPROVED_MAX_LIVE_SDK_CALLS,
        "internal_sdk_request_review_threshold_per_run": (
            INTERNAL_SDK_REQUEST_REVIEW_THRESHOLD_PER_RUN
        ),
        "internal_sdk_request_complex_graph_candidate_threshold": (
            INTERNAL_SDK_REQUEST_COMPLEX_GRAPH_CANDIDATE_THRESHOLD
        ),
        "internal_sdk_request_threshold_blockers": internal_threshold_blockers,
        "graph_meets_minimum": graph_meets_minimum,
        "efficiency_winner": efficiency_winner,
        "safety_winner": safety_winner,
        "user_facing_output_quality": automated_output_quality,
        "note": (
            "graph_better requires graph to win every minimum criterion and avoid "
            "an efficiency loss; otherwise the architecture is not yet proven "
            "better for user-visible work."
        ),
    }


def langgraph_open_smoke_checkpoint(packet: dict[str, Any]) -> dict[str, Any]:
    """Return whether the first open/default live run is ready for forced comparison."""

    blockers = _mode_evidence_blockers(
        packet,
        mode="open_default_backend_selected",
        require_workflow_sdk_usage=True,
    )
    mode_evidence = _mode_evidence(packet, "open_default_backend_selected")
    route = str(mode_evidence.get("route") or "").strip() if mode_evidence else ""
    status = str(mode_evidence.get("status") or "").strip().lower() if mode_evidence else ""
    visible_output = (
        str(mode_evidence.get("visible_output") or "").strip() if mode_evidence else ""
    )
    if status and status not in {"done", "completed", "success"}:
        blockers.append(f"open_default_status_not_done:{status}")
    if mode_evidence:
        blockers.extend(
            _blocked_operator_display_blockers(
                mode_evidence,
                mode="open_default_backend_selected",
            )
        )
    if _visible_output_mentions_side_effect(visible_output):
        blockers.append("open_default_visible_output_implies_external_side_effect")
    graph_expected_route = ""
    graph_output = packet.get("graph_output")
    if isinstance(graph_output, dict):
        graph_expected_route = str(graph_output.get("route") or "").strip()
    if graph_expected_route and route and route != graph_expected_route:
        blockers.append(f"open_default_route_mismatch:{route}!={graph_expected_route}")
    live_sdk_request_count = _live_sdk_request_count(packet)
    live_api_test_attempt_count = _live_api_test_attempt_count(packet)
    remaining_live_api_test_attempts = max(
        0,
        APPROVED_MAX_LIVE_SDK_CALLS - live_api_test_attempt_count,
    )
    threshold_blockers = _internal_sdk_request_threshold_blockers(packet)
    blockers.extend(threshold_blockers)
    if remaining_live_api_test_attempts < FORCED_COMPARISON_MIN_LIVE_SDK_CALLS:
        blockers.append(
            "open_default_live_api_test_budget_too_low_for_forced_modes:"
            f"{remaining_live_api_test_attempts}<{FORCED_COMPARISON_MIN_LIVE_SDK_CALLS}"
        )
    return {
        "schema": LIVE_OPEN_SMOKE_CHECKPOINT_SCHEMA,
        "mode": "open_default_backend_selected",
        "ready_for_forced_comparison": not blockers,
        "blockers": blockers,
        "route": route,
        "status": status,
        "operator_status": (
            str(mode_evidence.get("operator_status") or "").strip() if mode_evidence else ""
        ),
        "slack_display_title": (
            str(mode_evidence.get("slack_display_title") or "").strip()
            if mode_evidence
            else ""
        ),
        "expected_route": graph_expected_route,
        "live_sdk_request_count": live_sdk_request_count,
        "approved_max_live_sdk_calls": APPROVED_MAX_LIVE_SDK_CALLS,
        "live_api_test_attempt_count": live_api_test_attempt_count,
        "approved_live_api_test_budget": APPROVED_MAX_LIVE_SDK_CALLS,
        "remaining_live_api_test_attempts": remaining_live_api_test_attempts,
        "remaining_live_sdk_calls": remaining_live_api_test_attempts,
        "forced_comparison_min_live_sdk_calls": FORCED_COMPARISON_MIN_LIVE_SDK_CALLS,
        "internal_sdk_request_review_threshold_per_run": (
            INTERNAL_SDK_REQUEST_REVIEW_THRESHOLD_PER_RUN
        ),
        "internal_sdk_request_threshold_blockers": threshold_blockers,
        "requires_next_step": (
            "run_forced_langgraph_false_control"
            if not blockers
            else "inspect_or_fix_open_default_run"
        ),
        "note": (
            "Forced comparison runs should not start until the open/default Slack "
            "run has visible output, WorkItem route/status, and workflow SDK usage evidence."
        ),
    }


def langgraph_live_smoke_plan(
    *,
    scenario: str,
    request_text: str,
    comparison: dict[str, Any],
    max_live_sdk_calls: int = APPROVED_MAX_LIVE_SDK_CALLS,
    all_scenarios_ready: bool | None = None,
    all_scenarios_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the bounded live-smoke plan after offline graph quality is ready."""

    ready = bool(comparison.get("ready_for_live_smoke"))
    blockers: list[str] = []
    if not ready:
        blockers.append("offline_comparison_not_ready")
    if comparison.get("regression_markers"):
        blockers.append("offline_regression_markers_present")
    if not comparison.get("side_effect_safe_both"):
        blockers.append("side_effect_safety_not_proven_offline")
    if all_scenarios_ready is not True:
        blockers.append("all_scenarios_offline_gate_not_ready")
    if max_live_sdk_calls <= 0:
        blockers.append("live_sdk_call_budget_missing_or_nonpositive")
    if max_live_sdk_calls > APPROVED_MAX_LIVE_SDK_CALLS:
        blockers.append("live_sdk_call_budget_exceeds_approved_cap")
    slack_prompt_template = _slack_live_smoke_prompt_template(request_text)
    prompt_cleanliness_blockers = _live_prompt_cleanliness_blockers(
        slack_prompt_template
    )
    blockers.extend(prompt_cleanliness_blockers)
    return {
        "schema": LIVE_SMOKE_PLAN_SCHEMA,
        "scenario": scenario,
        "request_text": request_text,
        "slack_prompt_template": slack_prompt_template,
        "prompt_cleanliness_blockers": prompt_cleanliness_blockers,
        "ready_for_live_smoke": ready and not blockers,
        "blockers": blockers,
        "all_scenarios_ready": all_scenarios_ready,
        "all_scenarios_summary": dict(all_scenarios_summary or {}),
        "recommended_next_run": (
            "open_default_backend_selected_only" if ready and not blockers else "do_not_run_live"
        ),
        "max_live_sdk_calls": max_live_sdk_calls,
        "approved_max_live_sdk_calls": APPROVED_MAX_LIVE_SDK_CALLS,
        "live_api_test_budget": max_live_sdk_calls,
        "approved_live_api_test_budget": APPROVED_MAX_LIVE_SDK_CALLS,
        "internal_sdk_request_review_threshold_per_run": (
            INTERNAL_SDK_REQUEST_REVIEW_THRESHOLD_PER_RUN
        ),
        "internal_sdk_request_complex_graph_candidate_threshold": (
            INTERNAL_SDK_REQUEST_COMPLEX_GRAPH_CANDIDATE_THRESHOLD
        ),
        "internal_sdk_request_policy": {
            "review_threshold_per_run": INTERNAL_SDK_REQUEST_REVIEW_THRESHOLD_PER_RUN,
            "complex_graph_candidate_threshold": (
                INTERNAL_SDK_REQUEST_COMPLEX_GRAPH_CANDIDATE_THRESHOLD
            ),
            "separate_from_live_api_test_budget": True,
            "threshold_type": "per_run_internal_sdk_requests",
            "migration_requirement": (
                "Raising the threshold toward 8 requires trace evidence that added "
                "calls improve relevance, detail, planning quality, or execution quality."
            ),
        },
        "max_live_workflow_runs_before_review": 1,
        "run_sequence": [
            {
                "mode": "open_default_backend_selected",
                "when": "first",
                "environment": {"KEYSTONE_WORKITEM_LANGGRAPH": "unset"},
                "purpose": (
                    "Confirm the backend-selected path completes and produces "
                    "useful visible output before spending on forced comparisons."
                ),
            },
            {
                "mode": "forced_langgraph_false_control",
                "when": "only_after_open_run_passes_and_budget_remains",
                "environment": {"KEYSTONE_WORKITEM_LANGGRAPH": "false"},
                "purpose": "Control comparison for output usefulness and stage coverage.",
            },
            {
                "mode": "forced_langgraph_true",
                "when": "only_after_control_run_passes_and_budget_remains",
                "environment": {"KEYSTONE_WORKITEM_LANGGRAPH": "true"},
                "purpose": "Confirm the graph path is responsible for any observed value change.",
            },
        ],
        "live_controls": {
            "live_sdk": True,
            "live_search": False,
            "cost_profile": "slack_smoke_limited",
            "hosted_web_search_max_calls": 0,
            "allow_manager_loop_repair": False,
            "include_contact_enrichment": False,
            "external_writes_enabled": False,
            "send_enabled": False,
            "gmail_drafts_enabled": False,
            "post_outside_current_thread_enabled": False,
        },
        "prompt_guidance": [
            "Keep the visible Slack prompt as a natural operator ask.",
            "Do not include harness labels such as open/default, forced true, forced false, or LangGraph.",
            "Apply graph mode selection through backend configuration or test harness state, not user-facing wording.",
            "Keep live SDK, live-search, cost, and no-side-effect controls in live_controls or harness state, not the visible prompt.",
        ],
        "output_quality_success_criteria": [
            "visible answer gives a clearer operator decision or next action than control",
            "visible answer is specific to the requested company/topic and does not drift to generic leads",
            "visible answer preserves source-backed claims, evidence gaps, and uncertainty",
            "visible answer includes the requested draft/work product when safety gates allow it",
            "visible answer avoids route/debug metadata as the main substance",
            "visible answer does not add verbosity or API calls without user-facing value",
        ],
        "expected_evidence": [
            "full Slack/API visible output captured for review",
            "Slack operator status and display title captured from the rendered thread",
            "WorkItem id and route/status captured",
            "langgraph_orchestration event inspected when graph is selected",
            "workflow_sdk_usage event inspected for model, usage, cache, and cost data",
            "live output review packet completed against the rubric",
        ],
        "stop_conditions": [
            "route/status differs from offline-ready expectation",
            "requested stages are missing without a clear blocker",
            "any send, Gmail draft, external post, schedule, publish, or write is attempted",
            "workflow_sdk_usage is missing after a live SDK run",
            "forced graph run lacks langgraph_orchestration_event evidence",
            "live API test attempt count would exceed the approved budget",
            f"live API test budget must stay at or below {APPROVED_MAX_LIVE_SDK_CALLS}",
            "per-run internal SDK requests exceed the bounded-smoke review threshold",
            "visible output is less useful, less detailed, less relevant, or less source-grounded than control",
            "visible prompt wording includes harness labels that change backend routing",
            "all-scenarios offline readiness gate is missing or not ready",
            "open/default evidence checkpoint is not ready for forced comparison",
        ],
    }


def langgraph_live_smoke_plan_from_packet(packet: dict[str, Any]) -> dict[str, Any]:
    """Build the next live-smoke step from an existing review packet."""

    checkpoint = langgraph_open_smoke_checkpoint(packet)
    comparison = {
        "ready_for_live_smoke": True,
        "regression_markers": [],
        "side_effect_safe_both": True,
    }
    plan = langgraph_live_smoke_plan(
        scenario="saved_review_packet",
        request_text=str(packet.get("request_text") or ""),
        comparison=comparison,
        all_scenarios_ready=packet.get("all_scenarios_ready"),
        all_scenarios_summary=(
            packet.get("all_scenarios_summary")
            if isinstance(packet.get("all_scenarios_summary"), dict)
            else None
        ),
    )
    if plan.get("blockers"):
        plan["recommended_next_run"] = "do_not_run_live"
        plan["max_live_workflow_runs_before_review"] = 0
        return plan
    if checkpoint.get("ready_for_forced_comparison") is not True:
        plan["recommended_next_run"] = "open_default_backend_selected_only"
        plan["max_live_workflow_runs_before_review"] = 1
        return plan
    if _mode_evidence_blockers(
        packet,
        mode="forced_langgraph_false_control",
        require_workflow_sdk_usage=True,
    ):
        plan["recommended_next_run"] = "forced_langgraph_false_control"
        plan["max_live_workflow_runs_before_review"] = 1
        return plan
    if _mode_evidence_blockers(
        packet,
        mode="forced_langgraph_true",
        require_workflow_sdk_usage=True,
    ):
        plan["recommended_next_run"] = "forced_langgraph_true"
        plan["max_live_workflow_runs_before_review"] = 1
        return plan
    decision = finalize_langgraph_live_output_review(packet)
    if decision.get("decision") == "graph_better":
        plan["recommended_next_run"] = "no_live_run_required"
        plan["ready_for_live_smoke"] = False
        plan["max_live_workflow_runs_before_review"] = 0
    else:
        plan["recommended_next_run"] = "score_live_output_review"
        plan["ready_for_live_smoke"] = False
        plan["max_live_workflow_runs_before_review"] = 0
    return plan


def render_langgraph_live_output_review_rubric() -> str:
    """Render the live output usefulness rubric in compact operator-facing text."""

    rubric = langgraph_live_output_review_rubric()
    lines = [
        "Live output review rubric",
        "Use after the offline comparison is ready; compare graph-off/control and graph output.",
    ]
    for item in rubric["criteria"]:
        lines.append(f"- {item['criterion']}: {item['question']}")
    return "\n".join(lines)


def render_langgraph_live_smoke_plan(plan: dict[str, Any]) -> str:
    """Render the bounded live-smoke plan in compact operator-facing text."""

    controls = plan.get("live_controls") if isinstance(plan, dict) else {}
    if not isinstance(controls, dict):
        controls = {}
    blockers = _string_list(plan.get("blockers"))
    lines = [
        "Bounded live-smoke plan",
        f"- Ready for live smoke: {_yes_no(plan.get('ready_for_live_smoke'))}",
        f"- Recommended next run: {str(plan.get('recommended_next_run') or 'n/a')}",
        f"- Max live SDK calls: {int(plan.get('max_live_sdk_calls') or 0)}",
        "- Internal SDK request threshold/run: "
        f"{int(plan.get('internal_sdk_request_review_threshold_per_run') or 0)}",
        "- Complex-route candidate threshold/run: "
        f"{int(plan.get('internal_sdk_request_complex_graph_candidate_threshold') or 0)}",
        "- Max live workflow runs before review: "
        f"{int(plan.get('max_live_workflow_runs_before_review') or 0)}",
        f"- Cost profile: {str(controls.get('cost_profile') or 'n/a')}",
        f"- Live search: {_yes_no(controls.get('live_search'))}",
        "- Hosted web-search calls: "
        f"{int(controls.get('hosted_web_search_max_calls') or 0)}",
        f"- External writes enabled: {_yes_no(controls.get('external_writes_enabled'))}",
        f"- Send enabled: {_yes_no(controls.get('send_enabled'))}",
    ]
    summary = plan.get("all_scenarios_summary")
    scenario_count = int(summary.get("scenario_count") or 0) if isinstance(summary, dict) else 0
    if plan.get("all_scenarios_ready") is None:
        lines.append("- All-scenarios offline gate: missing")
    else:
        lines.append(
            "- All-scenarios offline gate: "
            f"{_yes_no(plan.get('all_scenarios_ready'))}"
            + (f" ({scenario_count} scenarios)" if scenario_count else "")
        )
    prompt = str(plan.get("slack_prompt_template") or "").strip()
    if prompt:
        lines.append(f"- Slack prompt template: {prompt}")
    prompt_blockers = _string_list(plan.get("prompt_cleanliness_blockers"))
    if prompt_blockers:
        lines.append(f"- Prompt cleanliness blockers: {', '.join(prompt_blockers)}")
    run_sequence = plan.get("run_sequence")
    if isinstance(run_sequence, list):
        for item in run_sequence:
            if not isinstance(item, dict):
                continue
            env = item.get("environment") if isinstance(item.get("environment"), dict) else {}
            env_bits = ", ".join(f"{key}={value}" for key, value in sorted(env.items()))
            lines.append(
                "- Mode "
                f"{str(item.get('mode') or 'n/a')}: {env_bits or 'default environment'}"
            )
    if blockers:
        lines.append(f"- Blockers: {', '.join(blockers)}")
    lines.append(
        "Inspect full Slack/API output quality, workflow_sdk_usage, and graph-event "
        "evidence before spending on the next run."
    )
    return "\n".join(lines)


def render_langgraph_live_output_review_packet(packet: dict[str, Any]) -> str:
    """Render a compact side-by-side worksheet for visible output review."""

    control = packet.get("control_output") if isinstance(packet, dict) else {}
    graph = packet.get("graph_output") if isinstance(packet, dict) else {}
    if not isinstance(control, dict):
        control = {}
    if not isinstance(graph, dict):
        graph = {}
    lines = [
        "Live output usefulness comparison",
        "- Decision: unreviewed until Slack/API output is inspected",
        f"- All-scenarios offline gate: {_yes_no(packet.get('all_scenarios_ready'))}",
        "- Minimum graph wins needed: "
        + ", ".join(_string_list(packet.get("minimum_better_than_control"))),
        f"- Control output chars: {int(control.get('output_char_count') or 0)}",
        f"- Graph output chars: {int(graph.get('output_char_count') or 0)}",
        f"- Control artifact summaries: {int(control.get('artifact_summary_count') or 0)}",
        f"- Graph artifact summaries: {int(graph.get('artifact_summary_count') or 0)}",
        "- Required evidence: permalink, WorkItem, route/status, Slack display, "
        "visible output, source URLs, side_effects=false, "
        "side_effects_reviewed=true, workflow_sdk_usage; "
        "forced graph also needs langgraph_orchestration_event.",
        "Review against: usefulness, detail, relevance, evidence_quality, "
        "safety_and_permissions, efficiency.",
    ]
    return "\n".join(lines)


def render_langgraph_live_output_review_decision(decision: dict[str, Any]) -> str:
    """Render the deterministic decision from a completed live output review."""

    lines = [
        "Live output review decision",
        f"- Decision: {str(decision.get('decision') or 'unreviewed')}",
        f"- Graph meets minimum: {_yes_no(decision.get('graph_meets_minimum'))}",
        "- Minimum graph wins: " + _join_or_none(decision.get("minimum_graph_wins")),
        "- Minimum control wins: " + _join_or_none(decision.get("minimum_control_wins")),
        "- Minimum ties: " + _join_or_none(decision.get("minimum_ties")),
        f"- Efficiency winner: {str(decision.get('efficiency_winner') or 'unreviewed')}",
        f"- Safety winner: {str(decision.get('safety_winner') or 'unreviewed')}",
    ]
    automated = decision.get("user_facing_output_quality")
    if isinstance(automated, dict):
        comparison = automated.get("comparison")
        if not isinstance(comparison, dict):
            comparison = {}
        lines.extend(
            [
                "- Automated visible-output score: "
                f"{str(automated.get('status') or 'pending_live_mode_evidence')}",
                "- Automated control/graph score: "
                f"{int(comparison.get('control_score') or 0)}/"
                f"{int(comparison.get('graph_score') or 0)}",
                "- Automated graph score delta: "
                f"{int(comparison.get('score_delta') or 0):+d}",
                "- Automated graph quality improved: "
                f"{_yes_no(comparison.get('graph_quality_improved'))}",
            ]
        )
    blockers = _string_list(decision.get("blockers"))
    if blockers:
        lines.append(f"- Blockers: {', '.join(blockers)}")
    return "\n".join(lines)


def render_langgraph_open_smoke_checkpoint(checkpoint: dict[str, Any]) -> str:
    """Render the first-run checkpoint before forced graph/control comparisons."""

    lines = [
        "Open/default live smoke checkpoint",
        "- Ready for forced comparison: "
        f"{_yes_no(checkpoint.get('ready_for_forced_comparison'))}",
        f"- Route: {str(checkpoint.get('route') or 'n/a')}",
        f"- Status: {str(checkpoint.get('status') or 'n/a')}",
        f"- Operator status: {str(checkpoint.get('operator_status') or 'n/a')}",
        f"- Slack display title: {str(checkpoint.get('slack_display_title') or 'n/a')}",
        f"- Expected route: {str(checkpoint.get('expected_route') or 'n/a')}",
        "- Internal SDK requests observed: "
        f"{int(checkpoint.get('live_sdk_request_count') or 0)}",
        "- Internal SDK request threshold/run: "
        f"{int(checkpoint.get('internal_sdk_request_review_threshold_per_run') or 0)}",
        "- Live API test attempts used: "
        f"{int(checkpoint.get('live_api_test_attempt_count') or 0)}",
        "- Live API test attempts remaining: "
        f"{int(checkpoint.get('remaining_live_api_test_attempts') or 0)}",
        "- Forced-mode live-test reserve: "
        f"{int(checkpoint.get('forced_comparison_min_live_sdk_calls') or 0)}",
        f"- Required next step: {str(checkpoint.get('requires_next_step') or 'n/a')}",
    ]
    blockers = _string_list(checkpoint.get("blockers"))
    if blockers:
        lines.append(f"- Blockers: {', '.join(blockers)}")
    return "\n".join(lines)



def _latest_event_metadata(events: Iterable[Any], event_type: str) -> dict[str, Any]:
    for event in reversed(list(events)):
        if str(getattr(event, "event_type", "") or "") != event_type:
            continue
        metadata = getattr(event, "metadata", None)
        if isinstance(metadata, dict):
            return metadata
    return {}


def _stage_statuses(completion_review: dict[str, Any]) -> dict[str, str]:
    requested_stages = completion_review.get("requested_stages") or []
    if not isinstance(requested_stages, list):
        return {}
    statuses: dict[str, str] = {}
    for stage in requested_stages:
        if not isinstance(stage, dict):
            continue
        name = str(stage.get("stage") or "").strip()
        status = str(stage.get("status") or "").strip()
        if name and status:
            statuses[name] = status
    return statuses


def _safety_violations(artifact_refs: list[Any]) -> list[str]:
    violations: list[str] = []
    for artifact in artifact_refs:
        metadata = getattr(artifact, "metadata", None)
        if not isinstance(metadata, dict):
            continue
        artifact_type = str(getattr(artifact, "artifact_type", "") or "artifact")
        for key in (
            "send_enabled",
            "external_writes_enabled",
            "gmail_draft_created",
            "approval_queue_created",
        ):
            if metadata.get(key) is True:
                violations.append(f"{artifact_type}:{key}")
    return violations


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _yes_no(value: Any) -> str:
    return "yes" if bool(value) else "no"


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item or "").strip()]


def _join_or_none(value: Any) -> str:
    items = _string_list(value)
    return ", ".join(items) if items else "none"


def _live_output_mode_evidence_template(
    *,
    mode: str,
    expected_environment: dict[str, str],
) -> dict[str, Any]:
    return {
        "mode": mode,
        "expected_environment": dict(expected_environment),
        "actual_environment": {},
        "slack_message_ts": "",
        "slack_thread_ts": "",
        "slack_permalink": "",
        "run_id": "",
        "work_item_id": "",
        "route": "",
        "status": "",
        "operator_status": "",
        "slack_display_title": "",
        "workflow_sdk_usage_event": {},
        "workflow_sdk_usage_events": [],
        "langgraph_orchestration_event": {},
        "visible_output": "",
        "source_urls": [],
        "side_effects": {},
        "side_effects_reviewed": False,
        "notes": "",
    }


def _copy_inventory_entry(item: dict[str, Any]) -> dict[str, Any]:
    copied: dict[str, Any] = {}
    for key, value in item.items():
        if isinstance(value, tuple):
            copied[key] = list(value)
        elif isinstance(value, list):
            copied[key] = list(value)
        elif isinstance(value, dict):
            copied[key] = dict(value)
        else:
            copied[key] = value
    return copied


def _live_output_evidence_blockers(packet: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    if packet.get("all_scenarios_ready") is not True:
        blockers.append("all_scenarios_offline_gate_not_ready")
    blockers.extend(_live_sdk_request_budget_blockers(packet))
    for mode in sorted(LIVE_OUTPUT_REQUIRED_EVIDENCE_MODES):
        blockers.extend(
            _mode_evidence_blockers(
                packet,
                mode=mode,
                require_workflow_sdk_usage=True,
            )
        )
    return blockers


def _live_sdk_request_budget_blockers(packet: dict[str, Any]) -> list[str]:
    return _internal_sdk_request_threshold_blockers(packet)


def _live_sdk_request_count(packet: dict[str, Any]) -> int:
    evidence = packet.get("mode_evidence")
    if not isinstance(evidence, list):
        return 0
    total = 0
    for item in evidence:
        if not isinstance(item, dict):
            continue
        for usage_event in _workflow_sdk_usage_events(item):
            usage = usage_event.get("usage")
            if not isinstance(usage, dict):
                continue
            try:
                total += int(usage.get("requests") or 0)
            except (TypeError, ValueError):
                continue
    return total


def _live_api_test_attempt_count(packet: dict[str, Any]) -> int:
    evidence = packet.get("mode_evidence")
    if not isinstance(evidence, list):
        return 0
    total = 0
    for item in evidence:
        if not isinstance(item, dict):
            continue
        if _workflow_sdk_usage_events(item) or str(item.get("work_item_id") or "").strip():
            total += 1
    return total


def _internal_sdk_request_threshold_blockers(packet: dict[str, Any]) -> list[str]:
    threshold = _internal_sdk_request_review_threshold(packet)
    blockers: list[str] = []
    evidence = packet.get("mode_evidence")
    if not isinstance(evidence, list):
        return blockers
    for item in evidence:
        if not isinstance(item, dict):
            continue
        mode = str(item.get("mode") or "unknown").strip() or "unknown"
        count = _workflow_sdk_request_count_for_mode(item)
        if count > threshold:
            blockers.append(
                f"internal_sdk_request_threshold_exceeded:{mode}:{count}>{threshold}"
            )
    return blockers


def _internal_sdk_request_review_threshold(packet: dict[str, Any]) -> int:
    policy = packet.get("internal_sdk_request_policy")
    if isinstance(policy, dict):
        try:
            value = int(policy.get("review_threshold_per_run") or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    return INTERNAL_SDK_REQUEST_REVIEW_THRESHOLD_PER_RUN


def _workflow_sdk_request_count_for_mode(item: dict[str, Any]) -> int:
    total = 0
    for usage_event in _workflow_sdk_usage_events(item):
        usage = usage_event.get("usage")
        if not isinstance(usage, dict):
            continue
        try:
            total += int(usage.get("requests") or 0)
        except (TypeError, ValueError):
            continue
    return total


def _workflow_sdk_usage_events(item: dict[str, Any]) -> list[dict[str, Any]]:
    events = item.get("workflow_sdk_usage_events")
    if isinstance(events, list):
        normalized = [event for event in events if isinstance(event, dict) and event]
        if normalized:
            return normalized
    usage_event = item.get("workflow_sdk_usage_event")
    if isinstance(usage_event, dict) and usage_event:
        return [usage_event]
    return []


def _workflow_sdk_usage_totals(item: dict[str, Any]) -> tuple[int, int]:
    requests = 0
    total_tokens = 0
    for event in _workflow_sdk_usage_events(item):
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        try:
            requests += int(usage.get("requests") or 0)
        except (TypeError, ValueError):
            pass
        try:
            event_total = int(usage.get("total_tokens") or 0)
        except (TypeError, ValueError):
            event_total = 0
        if event_total <= 0:
            for key in ("input_tokens", "output_tokens"):
                try:
                    event_total += int(usage.get(key) or 0)
                except (TypeError, ValueError):
                    continue
        total_tokens += event_total
    return requests, total_tokens


def _boolean_mapping(value: Any) -> dict[str, bool]:
    if not isinstance(value, Mapping):
        return {}
    return {str(key): bool(item) for key, item in value.items()}


def _mode_evidence(
    packet: dict[str, Any],
    mode: str,
) -> dict[str, Any]:
    evidence = packet.get("mode_evidence")
    if not isinstance(evidence, list):
        return {}
    for item in evidence:
        if not isinstance(item, dict):
            continue
        if str(item.get("mode") or "").strip() == mode:
            return item
    return {}


def _mode_evidence_blockers(
    packet: dict[str, Any],
    *,
    mode: str,
    require_workflow_sdk_usage: bool,
) -> list[str]:
    evidence = packet.get("mode_evidence")
    if not isinstance(evidence, list):
        return ["missing_mode_evidence"]
    item = _mode_evidence(packet, mode)
    if not item:
        return [f"missing_mode_evidence:{mode}"]
    blockers: list[str] = []
    for field in (
        "slack_permalink",
        "work_item_id",
        "route",
        "status",
        "operator_status",
        "slack_display_title",
        "visible_output",
    ):
        if not str(item.get(field) or "").strip():
            blockers.append(f"missing_mode_evidence:{mode}.{field}")
    source_urls = item.get("source_urls")
    if not isinstance(source_urls, list) or not [
        url for url in source_urls if str(url or "").strip()
    ]:
        blockers.append(f"missing_mode_evidence:{mode}.source_urls")
    side_effects = item.get("side_effects")
    if not isinstance(side_effects, dict) or not side_effects:
        blockers.append(f"missing_mode_evidence:{mode}.side_effects")
    else:
        for key in _LIVE_SIDE_EFFECT_FLAGS:
            if key not in side_effects:
                blockers.append(f"missing_mode_evidence:{mode}.side_effects.{key}")
        for key, value in side_effects.items():
            if bool(value):
                blockers.append(f"side_effect_detected:{mode}.{key}")
    if item.get("side_effects_reviewed") is not True:
        blockers.append(f"missing_mode_evidence:{mode}.side_effects_reviewed")
    blockers.extend(_actual_environment_blockers(item, mode=mode))
    usage_events = _workflow_sdk_usage_events(item)
    if require_workflow_sdk_usage and not usage_events:
        blockers.append(f"missing_mode_evidence:{mode}.workflow_sdk_usage_event")
    elif require_workflow_sdk_usage:
        for usage_event in usage_events:
            blockers.extend(_workflow_sdk_usage_blockers(usage_event, mode=mode))
    graph_event = item.get("langgraph_orchestration_event")
    if mode in LIVE_OUTPUT_GRAPH_EVIDENCE_MODES and (
        not isinstance(graph_event, dict) or not graph_event
    ):
        blockers.append(f"missing_mode_evidence:{mode}.langgraph_orchestration_event")
    elif (
        mode in LIVE_OUTPUT_GRAPH_OFF_EVIDENCE_MODES
        and isinstance(graph_event, dict)
        and graph_event
    ):
        blockers.append(
            f"unexpected_mode_evidence:{mode}.langgraph_orchestration_event"
        )
    elif isinstance(graph_event, dict) and graph_event:
        if not _valid_langgraph_orchestration_event(graph_event):
            blockers.append(
                f"invalid_mode_evidence:{mode}.langgraph_orchestration_event.schema"
            )
    blockers.extend(_blocked_operator_display_blockers(item, mode=mode))
    return blockers


def _actual_environment_blockers(item: dict[str, Any], *, mode: str) -> list[str]:
    if mode not in LIVE_OUTPUT_REQUIRED_EVIDENCE_MODES:
        return []
    expected = item.get("expected_environment")
    actual = item.get("actual_environment")
    if not isinstance(expected, dict) or not expected:
        return [f"missing_mode_evidence:{mode}.expected_environment"]
    if not isinstance(actual, dict) or not actual:
        return [f"missing_mode_evidence:{mode}.actual_environment"]
    blockers: list[str] = []
    for key, expected_value in expected.items():
        actual_value = str(actual.get(key) or "").strip()
        if actual_value != str(expected_value):
            blockers.append(f"invalid_mode_evidence:{mode}.actual_environment.{key}")
    return blockers


def _valid_langgraph_orchestration_event(event: dict[str, Any]) -> bool:
    schema = str(event.get("schema") or "").strip()
    if schema:
        return schema == LIVE_OUTPUT_GRAPH_EVENT_SCHEMA
    runtime = str(event.get("runtime") or "").strip().lower()
    node_path = event.get("node_path")
    return runtime == "langgraph" and isinstance(node_path, list) and bool(node_path)


def _workflow_sdk_usage_blockers(event: dict[str, Any], *, mode: str) -> list[str]:
    blockers: list[str] = []
    if str(event.get("schema") or "").strip() != "keystone.workflow_sdk_usage.v1":
        blockers.append(f"invalid_mode_evidence:{mode}.workflow_sdk_usage_event.schema")
    usage = event.get("usage")
    if not isinstance(usage, dict) or not usage:
        blockers.append(f"missing_mode_evidence:{mode}.workflow_sdk_usage_event.usage")
    elif int(usage.get("requests") or 0) <= 0:
        blockers.append(f"invalid_mode_evidence:{mode}.workflow_sdk_usage_event.usage.requests")
    cost = event.get("cost")
    if not isinstance(cost, dict) or not cost:
        blockers.append(f"missing_mode_evidence:{mode}.workflow_sdk_usage_event.cost")
    request_cache = event.get("request_cache")
    if not isinstance(request_cache, dict) or not request_cache:
        blockers.append(f"missing_mode_evidence:{mode}.workflow_sdk_usage_event.request_cache")
    elif not any(
        str(request_cache.get(key) or "").strip()
        for key in ("static_prefix_sha256", "dynamic_prompt_sha256", "prompt_cache_key_hash")
    ):
        blockers.append(
            f"missing_mode_evidence:{mode}.workflow_sdk_usage_event.request_cache_hash"
        )
    return blockers


def _blocked_operator_display_blockers(item: dict[str, Any], *, mode: str) -> list[str]:
    status = str(item.get("status") or "").strip().lower()
    if status != "blocked":
        return []
    blockers: list[str] = []
    operator_status = str(item.get("operator_status") or "").strip().lower()
    display_title = str(item.get("slack_display_title") or "").strip()
    display_title_lower = display_title.lower()
    visible_output = str(item.get("visible_output") or "").strip().lower()
    if operator_status != "needs_input":
        blockers.append(f"blocked_mode_without_needs_input_operator_status:{mode}")
    if not display_title:
        blockers.append(f"blocked_mode_missing_slack_display_title:{mode}")
    elif "blocked" in display_title_lower:
        blockers.append(f"blocked_mode_title_says_blocked:{mode}")
    elif "need input" not in display_title_lower and "needs input" not in display_title_lower:
        blockers.append(f"blocked_mode_title_not_needs_input:{mode}")
    if not re.search(
        r"(?:\?|can you|could you|please provide|please share|reply with|what i need|"
        r"what should|provide the|share the|which)",
        visible_output,
    ):
        blockers.append(f"blocked_mode_visible_output_lacks_next_input_request:{mode}")
    return blockers


def _visible_output_mentions_side_effect(visible_output: str) -> bool:
    normalized = " ".join(str(visible_output or "").lower().split())
    return bool(
        re.search(
            r"\b(?:sent|posted|scheduled|published|created gmail draft|"
            r"gmail draft created|updated airtable|updated crm|wrote external)\b",
            normalized,
        )
        and not re.search(
            r"\b(?:no|not|was not|were not|without)\b[^.\n]{0,80}"
            r"\b(?:sent|posted|scheduled|published|created gmail draft|"
            r"gmail draft created|updated airtable|updated crm|wrote external)\b",
            normalized,
        )
    )


def _slack_live_smoke_prompt_template(request_text: str) -> str:
    """Return natural Slack wording that keeps harness labels out of the ask."""

    cleaned = " ".join(str(request_text or "").split()).strip()
    for pattern in _LIVE_PROMPT_STRIP_PATTERNS:
        cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()
    cleaned = " ".join(cleaned.split()).strip(" :")
    if not cleaned:
        cleaned = "run the approved agent smoke"
    return f"@KNI {cleaned}"


def _live_prompt_cleanliness_blockers(prompt: str) -> list[str]:
    """Return blockers for visible Slack prompts that still expose harness state."""

    normalized = " ".join(str(prompt or "").split()).strip()
    blockers: list[str] = []
    if not normalized.startswith("@KNI "):
        blockers.append("live_smoke_prompt_missing_kni_prefix")
    if len(re.findall(r"@KNI\b", normalized, flags=re.IGNORECASE)) != 1:
        blockers.append("live_smoke_prompt_duplicate_kni_prefix")
    for label, pattern in _LIVE_PROMPT_FORBIDDEN_PATTERNS:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            blockers.append(f"live_smoke_prompt_contains:{label}")
    return blockers
