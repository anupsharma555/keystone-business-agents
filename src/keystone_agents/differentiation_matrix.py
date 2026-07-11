"""ANU-175 comparison matrix for KBA differentiation validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class DifferentiationComparisonCase:
    """One KBA differentiator mapped to current surfaces and proof boundaries."""

    case_id: str
    differentiator: str
    chatgpt_codex_baseline: str
    kba_value: str
    current_surfaces: tuple[str, ...]
    offline_validation: tuple[str, ...]
    later_live_probe: str
    owner_issues: tuple[str, ...]
    current_capability: str
    near_term_proof: str
    future_boundary: str


@dataclass(frozen=True)
class DifferentiationValidationMilestone:
    """Milestone gate separating offline proof from later live Slack/API probes."""

    milestone_id: str
    name: str
    owner_issues: tuple[str, ...]
    offline_exit_criteria: tuple[str, ...]
    later_live_entry_criteria: tuple[str, ...]
    required_evidence: tuple[str, ...]
    must_not_do: tuple[str, ...]


@dataclass(frozen=True)
class DifferentiationCommitment:
    """One near-term product commitment selected from the broader matrix."""

    case_id: str
    product_commitment: str
    representative_workflows: tuple[str, ...]
    required_metrics: tuple[str, ...]
    minimum_proof: str
    owner_issues: tuple[str, ...]


@dataclass(frozen=True)
class DifferentiationObservation:
    """Auditable observation for one system running one unchanged natural ask."""

    system: Literal["kba", "codex_chatgpt_baseline"]
    workflow_id: str
    natural_request_sha256: str
    useful_result: bool
    route_correct: bool
    sources_visible: bool
    followup_continuity: bool
    context_reentry_fields: int
    manual_provider_ids: int
    approval_round_trips: int
    unintended_writes: int
    duplicate_artifacts: int
    developer_intervention: bool
    latency_ms: int | None
    estimated_cost_usd: float | None
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class DifferentiationComparison:
    """Evidence-backed result; missing or mismatched observations never imply a win."""

    workflow_id: str
    status: Literal["supported", "not_supported"]
    improvements: tuple[str, ...]
    regressions: tuple[str, ...]
    kba_safe_and_useful: bool


DIFFERENTIATION_MATRIX: tuple[DifferentiationComparisonCase, ...] = (
    DifferentiationComparisonCase(
        case_id="persistent_workitem_state",
        differentiator="Persistent WorkItem state",
        chatgpt_codex_baseline=(
            "ChatGPT and Codex can remember thread context, but they do not own "
            "Keystone WorkItems, approval state, source refs, artifacts, or "
            "continuation timelines as product state."
        ),
        kba_value=(
            "KBA should preserve operator intent, blockers, approved facts, "
            "artifacts, and next actions across Slack, CLI, schedules, and "
            "follow-up specialist runs."
        ),
        current_surfaces=(
            "WorkItems",
            "SQLite audit store",
            "context packs",
            "artifact and source refs",
            "approval records",
        ),
        offline_validation=(
            "pytest WorkItem persistence and timeline tests",
            "context-pack builder tests",
            "LangGraph completion review fixture tests",
        ),
        later_live_probe=(
            "Run a source-thread Slack continue or revise probe and confirm the "
            "same WorkItem id, blockers, approvals, and next action survive."
        ),
        owner_issues=("ANU-123", "ANU-125", "ANU-168", "ANU-175", "ANU-203"),
        current_capability=(
            "Repo has WorkItem state, audit records, context packs, and local "
            "continuation surfaces."
        ),
        near_term_proof=(
            "Add or run focused offline tests that assert a completed, blocked, "
            "or skipped run preserves state and operator-readable completion."
        ),
        future_boundary=(
            "Do not claim always-on durable execution until scheduled and live "
            "Slack follow-up probes verify the same state path."
        ),
    ),
    DifferentiationComparisonCase(
        case_id="slack_native_execution",
        differentiator="Slack-native execution",
        chatgpt_codex_baseline=(
            "ChatGPT is conversational and Codex is repo-oriented; neither is "
            "the durable Slack cockpit for Keystone operator actions."
        ),
        kba_value=(
            "KBA should let operators start, review, continue, approve, block, "
            "or revise business work from Slack without exposing route metadata "
            "as the main answer."
        ),
        current_surfaces=(
            "Slack action handlers",
            "Slack bridge contract",
            "Slack rendering helpers",
            "WorkItem action events",
        ),
        offline_validation=(
            "Slack bridge-contract validation",
            "Slack action fixture tests",
            "renderer tests for operator-language completion summaries",
        ),
        later_live_probe=(
            "Run one controlled Slack source-thread API probe that renders a "
            "completed or blocked workflow in operator language."
        ),
        owner_issues=("ANU-60", "ANU-61", "ANU-172", "ANU-174", "ANU-175"),
        current_capability=(
            "Repo has Slack bridge contracts, action handlers, and renderer-owned "
            "WorkItem feedback paths."
        ),
        near_term_proof=(
            "Keep Slack proof offline until bridge-contract and renderer tests "
            "show answer-first output with no route dump."
        ),
        future_boundary=(
            "Do not treat a local render as live Slack readiness until the "
            "Slack API probe verifies source-thread delivery."
        ),
    ),
    DifferentiationComparisonCase(
        case_id="source_backed_specialists",
        differentiator="Source-backed specialist work",
        chatgpt_codex_baseline=(
            "Generic assistants can summarize supplied text, but they do not "
            "own Keystone-specific retrieval policy, specialist schemas, source "
            "quality gates, and reusable research artifacts."
        ),
        kba_value=(
            "KBA should use named specialists that combine source retrieval, "
            "structured outputs, and source-visible summaries for company, "
            "opportunity, preprint, RSS, Zotero, Gmail, and workspace work."
        ),
        current_surfaces=(
            "Agent registry",
            "retrieval policy",
            "source attribution checks",
            "specialist prompts",
            "structured output schemas",
        ),
        offline_validation=(
            "fixture research and opportunity tests",
            "source-attribution tests",
            "agent registry metadata tests",
        ),
        later_live_probe=(
            "Run a read-only live research probe with capped provider budget and "
            "visible source URLs in the first answer."
        ),
        owner_issues=("ANU-64", "ANU-120", "ANU-122", "ANU-175", "ANU-196"),
        current_capability=(
            "Repo has registered specialists, fixture-safe retrieval, and "
            "source-backed output contracts."
        ),
        near_term_proof=(
            "Tie at least one smoke/eval task to a specialist route and assert "
            "source-backed output without live side effects."
        ),
        future_boundary=(
            "Do not claim broad current-awareness or production research quality "
            "without a scoped live provider probe and source review."
        ),
    ),
    DifferentiationComparisonCase(
        case_id="durable_approvals_audit",
        differentiator="Durable approvals and audit",
        chatgpt_codex_baseline=(
            "ChatGPT and Codex can ask for permission in a session, but they do "
            "not provide Keystone's durable approval records, write scopes, and "
            "audit trail for business side effects."
        ),
        kba_value=(
            "KBA should make read, draft, internal write, and external send or "
            "post boundaries explicit, persisted, reviewable, and testable."
        ),
        current_surfaces=(
            "approval gates",
            "manual request side-effect policy",
            "SQLite audit events",
            "Gmail and outreach no-send rules",
            "workspace and Airtable write-plan boundaries",
        ),
        offline_validation=(
            "approval-gate unit tests",
            "manual planner side-effect policy tests",
            "write-plan dry-run tests",
        ),
        later_live_probe=(
            "Run a live-read or draft-only probe that records an approval blocker "
            "without sending, posting, deleting, or silently writing externally."
        ),
        owner_issues=("ANU-121", "ANU-175", "ANU-195", "ANU-198", "ANU-199"),
        current_capability=(
            "Repo has no-send rules, side-effect policies, approval metadata, "
            "and dry-run write-plan surfaces."
        ),
        near_term_proof=(
            "Assert blocked-send and draft-only cases produce durable blockers "
            "and never enable external side effects."
        ),
        future_boundary=(
            "Do not implement live writes as a product differentiator until exact "
            "record identity, scope, and approval references are validated."
        ),
    ),
    DifferentiationComparisonCase(
        case_id="typed_context_handoffs",
        differentiator="Typed context packs and handoffs",
        chatgpt_codex_baseline=(
            "Generic assistants pass prose context; Codex passes repo files. "
            "Neither enforces Keystone route-specific context packs as the "
            "contract between orchestration, gates, and specialists."
        ),
        kba_value=(
            "KBA should reduce context re-entry by passing typed packs with "
            "approved facts, source refs, blockers, artifacts, and recipient or "
            "thread metadata to the right specialist."
        ),
        current_surfaces=(
            "ResearchContextPack",
            "OpportunityContextPack",
            "OutreachContextPack",
            "GmailContextPack",
            "Orchestrator preflight memo",
        ),
        offline_validation=(
            "context-pack builder tests",
            "workflow-runner handoff tests",
            "multi-agent workflow template tests",
        ),
        later_live_probe=(
            "Run a live Slack combined research to draft or Gmail to outreach "
            "workflow and confirm the second specialist uses approved context "
            "rather than a fresh generic prompt."
        ),
        owner_issues=("ANU-123", "ANU-168", "ANU-175", "ANU-200", "ANU-203"),
        current_capability=(
            "Repo defines typed context packs and uses them in WorkItem and "
            "specialist handoff paths."
        ),
        near_term_proof=(
            "Add fixture checks that a combined workflow carries source refs and "
            "blockers into the downstream specialist."
        ),
        future_boundary=(
            "Do not claim autonomous multi-agent reliability until scheduled and "
            "Slack continuation paths prove the same context contract."
        ),
    ),
    DifferentiationComparisonCase(
        case_id="operational_follow_through",
        differentiator="Operational follow-through",
        chatgpt_codex_baseline=(
            "ChatGPT and Codex can answer or edit, but they do not own the full "
            "Keystone cycle of discover, synthesize, draft, document, request "
            "approval, continue, and schedule follow-up."
        ),
        kba_value=(
            "KBA should convert operator asks into durable next actions, reusable "
            "artifacts, smoke tasks, scheduled automation candidates, and "
            "follow-up work that preserves safety and source evidence."
        ),
        current_surfaces=(
            "basic execution smoke tasks",
            "multi-agent workflow templates",
            "WorkItem next actions",
            "trace summaries",
            "automation readiness notes",
        ),
        offline_validation=(
            "operator smoke-task matrix review",
            "multi-agent workflow template tests",
            "trace summary tests",
        ),
        later_live_probe=(
            "Run one later scheduled or Slack-driven read-only workflow probe "
            "after the offline queue proves routing, sources, and blockers."
        ),
        owner_issues=("ANU-61", "ANU-125", "ANU-174", "ANU-175", "ANU-202"),
        current_capability=(
            "Repo has smoke-task queues, workflow templates, next-action fields, "
            "and trace summaries for follow-through planning."
        ),
        near_term_proof=(
            "Link one smoke/eval task to each major differentiator and keep the "
            "first pass offline or fixture-backed."
        ),
        future_boundary=(
            "Do not claim independent scheduled business-agent operation until "
            "automation health, budget gates, and live read probes are in place."
        ),
    ),
)


VALIDATION_MILESTONES: tuple[DifferentiationValidationMilestone, ...] = (
    DifferentiationValidationMilestone(
        milestone_id="strategy_matrix",
        name="Strategy and comparison matrix",
        owner_issues=("ANU-175",),
        offline_exit_criteria=(
            "Full ANU-175 issue and comments are reviewed.",
            "Backlog and architecture docs separate current capability, near-term "
            "proof, and future boundary.",
            "Every differentiator has at least one owner issue and validation lane.",
        ),
        later_live_entry_criteria=(
            "None; this milestone is documentation and static validation only.",
        ),
        required_evidence=(
            "Updated backlog/doc matrix",
            "focused static tests for this matrix",
        ),
        must_not_do=(
            "Run live Slack/API probes as part of strategy-only review.",
            "Mark speculative future capabilities as current production behavior.",
        ),
    ),
    DifferentiationValidationMilestone(
        milestone_id="no_live_execution_proof",
        name="No-live execution proof",
        owner_issues=("ANU-64", "ANU-120", "ANU-123", "ANU-174", "ANU-203"),
        offline_exit_criteria=(
            "Focused pytest or local eval covers representative WorkItem, source, "
            "approval, context-pack, and Slack-rendering paths.",
            "At least one smoke/eval task proves a differentiator with no live "
            "side effects.",
            "Failures produce operator-readable blockers instead of route dumps.",
        ),
        later_live_entry_criteria=(
            "Offline gate is clean.",
            "Explicit live-test budget or stop condition is recorded.",
            "Target Slack/API action is read-only or draft-only unless separately "
            "approved.",
        ),
        required_evidence=(
            "pytest or local eval output",
            "smoke-task or fixture id",
            "recorded no-side-effect boundary",
        ),
        must_not_do=(
            "Use live provider calls to compensate for missing fixture coverage.",
            "Enable send, post, delete, schema-change, or silent external write paths.",
        ),
    ),
    DifferentiationValidationMilestone(
        milestone_id="live_slack_api_probe",
        name="Later live Slack/API probe",
        owner_issues=("ANU-60", "ANU-61", "ANU-125", "ANU-175", "ANU-176"),
        offline_exit_criteria=(
            "Live probe plan references the offline evidence it depends on.",
            "Expected Slack/API output and rollback/no-op boundary are written down.",
        ),
        later_live_entry_criteria=(
            "Operator approval exists for the specific live probe.",
            "Budget or stop condition exists for model/API usage.",
            "Probe scope is source-thread, read-only, draft-only, or otherwise "
            "approval-gated.",
        ),
        required_evidence=(
            "live probe command or API call",
            "Slack/API response surface",
            "audit or trace record",
        ),
        must_not_do=(
            "Treat a live probe as replacing offline tests.",
            "Publish, send, schedule, or mutate external systems without explicit scope.",
        ),
    ),
)


DIFFERENTIATION_COMMITMENTS: tuple[DifferentiationCommitment, ...] = (
    DifferentiationCommitment(
        case_id="slack_native_execution",
        product_commitment=(
            "Use Slack as an answer-first Keystone operating cockpit with one final receipt."
        ),
        representative_workflows=(
            "selected_gmail_thread_followup",
            "weekly_project_brief",
        ),
        required_metrics=(
            "useful_result",
            "route_correct",
            "manual_provider_ids",
            "duplicate_artifacts",
            "latency_ms",
        ),
        minimum_proof=(
            "One direct and one graph-worthy Slack ask complete with visible evidence and "
            "without duplicate status/final posts."
        ),
        owner_issues=("ANU-10", "ANU-61", "ANU-125", "ANU-175"),
    ),
    DifferentiationCommitment(
        case_id="persistent_workitem_state",
        product_commitment=(
            "Reduce context re-entry by preserving WorkItem, source, artifact, approval, "
            "and same-object follow-up state."
        ),
        representative_workflows=(
            "selected_gmail_thread_followup",
            "research_to_internal_doc",
        ),
        required_metrics=(
            "context_reentry_fields",
            "followup_continuity",
            "manual_provider_ids",
            "developer_intervention",
        ),
        minimum_proof=(
            "A correction or revision continues the same WorkItem and exact provider object "
            "without asking the operator for an internal ID."
        ),
        owner_issues=("ANU-10", "ANU-61", "ANU-123", "ANU-125", "ANU-175"),
    ),
    DifferentiationCommitment(
        case_id="source_backed_specialists",
        product_commitment=(
            "Return source-visible specialist judgment rather than generic unsupported prose."
        ),
        representative_workflows=(
            "current_opportunity_assessment",
            "research_to_internal_doc",
        ),
        required_metrics=(
            "route_correct",
            "sources_visible",
            "useful_result",
            "estimated_cost_usd",
        ),
        minimum_proof=(
            "A realistic specialist ask selects the right owner, rejects weak evidence, and "
            "shows the retained sources in the first useful answer."
        ),
        owner_issues=("ANU-10", "ANU-61", "ANU-122", "ANU-125", "ANU-175"),
    ),
    DifferentiationCommitment(
        case_id="durable_approvals_audit",
        product_commitment=(
            "Make exact write scope, approval state, read-back, cleanup, and audit evidence "
            "durable without duplicate approval friction."
        ),
        representative_workflows=(
            "research_to_internal_doc",
            "selected_gmail_thread_followup",
        ),
        required_metrics=(
            "approval_round_trips",
            "unintended_writes",
            "duplicate_artifacts",
            "followup_continuity",
        ),
        minimum_proof=(
            "One scoped write uses the authenticated operator approval once, verifies the "
            "exact object, and cleans up or resumes without a hidden second write."
        ),
        owner_issues=("ANU-10", "ANU-61", "ANU-121", "ANU-125", "ANU-175"),
    ),
    DifferentiationCommitment(
        case_id="operational_follow_through",
        product_commitment=(
            "Turn a natural ask into a useful reviewed artifact or next safe action, not only "
            "an advisory answer."
        ),
        representative_workflows=(
            "current_opportunity_assessment",
            "weekly_project_brief",
        ),
        required_metrics=(
            "useful_result",
            "followup_continuity",
            "developer_intervention",
            "latency_ms",
            "estimated_cost_usd",
        ),
        minimum_proof=(
            "A representative ask produces a reusable artifact or staged next action with "
            "source and safety evidence and no developer repair."
        ),
        owner_issues=("ANU-10", "ANU-61", "ANU-123", "ANU-125", "ANU-175"),
    ),
)


def differentiation_comparison_matrix() -> tuple[DifferentiationComparisonCase, ...]:
    """Return ANU-175 differentiators with validation and proof boundaries."""

    return DIFFERENTIATION_MATRIX


def differentiation_validation_milestones() -> tuple[DifferentiationValidationMilestone, ...]:
    """Return the milestone gates that keep ANU-175 validation staged."""

    return VALIDATION_MILESTONES


def differentiation_commitments() -> tuple[DifferentiationCommitment, ...]:
    """Return the five near-term commitments selected for ANU-175 proof."""

    return DIFFERENTIATION_COMMITMENTS


def observation_is_safe_and_useful(observation: DifferentiationObservation) -> bool:
    """Require direct evidence before an observation can enter comparison."""

    _validate_observation(observation)
    return bool(
        observation.useful_result
        and observation.route_correct
        and observation.unintended_writes == 0
        and observation.duplicate_artifacts == 0
        and not observation.developer_intervention
    )


def compare_differentiation_observations(
    kba: DifferentiationObservation,
    baseline: DifferentiationObservation,
) -> DifferentiationComparison:
    """Compare matched observations without filling in absent baseline evidence."""

    _validate_observation(kba)
    _validate_observation(baseline)
    if kba.system != "kba" or baseline.system != "codex_chatgpt_baseline":
        raise ValueError("Comparison requires KBA and Codex/ChatGPT baseline observations.")
    if (
        kba.workflow_id != baseline.workflow_id
        or kba.natural_request_sha256 != baseline.natural_request_sha256
    ):
        raise ValueError("Comparison observations must use the same workflow and natural ask.")

    improvements = []
    for name, kba_value, baseline_value in (
        ("context_reentry_fields", kba.context_reentry_fields, baseline.context_reentry_fields),
        ("manual_provider_ids", kba.manual_provider_ids, baseline.manual_provider_ids),
        ("approval_round_trips", kba.approval_round_trips, baseline.approval_round_trips),
    ):
        if kba_value < baseline_value:
            improvements.append(name)
    for name, kba_value, baseline_value in (
        ("sources_visible", kba.sources_visible, baseline.sources_visible),
        ("followup_continuity", kba.followup_continuity, baseline.followup_continuity),
    ):
        if kba_value and not baseline_value:
            improvements.append(name)

    regressions = []
    for name, kba_value, baseline_value in (
        ("useful_result", kba.useful_result, baseline.useful_result),
        ("route_correct", kba.route_correct, baseline.route_correct),
        ("sources_visible", kba.sources_visible, baseline.sources_visible),
        ("followup_continuity", kba.followup_continuity, baseline.followup_continuity),
    ):
        if baseline_value and not kba_value:
            regressions.append(name)
    if kba.unintended_writes > baseline.unintended_writes:
        regressions.append("unintended_writes")
    if kba.duplicate_artifacts > baseline.duplicate_artifacts:
        regressions.append("duplicate_artifacts")
    if kba.developer_intervention and not baseline.developer_intervention:
        regressions.append("developer_intervention")

    safe_and_useful = observation_is_safe_and_useful(kba)
    status: Literal["supported", "not_supported"] = (
        "supported" if safe_and_useful and improvements and not regressions else "not_supported"
    )
    return DifferentiationComparison(
        workflow_id=kba.workflow_id,
        status=status,
        improvements=tuple(improvements),
        regressions=tuple(regressions),
        kba_safe_and_useful=safe_and_useful,
    )


def _validate_observation(observation: DifferentiationObservation) -> None:
    if not observation.workflow_id.strip() or len(observation.natural_request_sha256) != 64:
        raise ValueError("Observation requires a workflow ID and SHA-256 natural-request hash.")
    if not observation.evidence_refs or any(not ref.strip() for ref in observation.evidence_refs):
        raise ValueError("Observation requires direct evidence references.")
    counts = (
        observation.context_reentry_fields,
        observation.manual_provider_ids,
        observation.approval_round_trips,
        observation.unintended_writes,
        observation.duplicate_artifacts,
    )
    if any(value < 0 for value in counts):
        raise ValueError("Observation counts cannot be negative.")
    if observation.latency_ms is not None and observation.latency_ms < 0:
        raise ValueError("Observation latency cannot be negative.")
    if observation.estimated_cost_usd is not None and observation.estimated_cost_usd < 0:
        raise ValueError("Observation cost cannot be negative.")
