"""Local memory derivation and workflow dedup helpers."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any, Literal

from keystone_agents.schemas.approval import (
    ApprovalDecisionRecord,
    ApprovalQueueItem,
    ApprovalState,
)
from keystone_agents.schemas.company_profile import ClaimEvidenceRecord, CompanyProfile
from keystone_agents.schemas.email_style import EmailStyleProfile
from keystone_agents.schemas.feedback import FeedbackRecord
from keystone_agents.schemas.memory import (
    ChiefOfStaffMemoryContext,
    MemoryItem,
    normalize_memory_key,
)
from keystone_agents.schemas.opportunity import (
    OpportunityRecord as ScoutOpportunityRecord,
)
from keystone_agents.schemas.opportunity import OpportunityScoutResult
from keystone_agents.schemas.outreach import OutreachDraft
from keystone_agents.schemas.outreach_examples import OutreachExampleRecord
from keystone_agents.storage.sqlite_store import SQLiteStore

WorkflowDedupAction = Literal[
    "company_research",
    "opportunity",
    "outreach_company",
    "outbound_email",
]

CHIEF_OF_STAFF_MEMORY_TYPES = (
    "operator_strategy",
    "project_goal",
    "project_constraint",
    "project_decision",
    "project_status_snapshot",
    "portfolio_priority",
    "budget_assumption",
    "avoidance_rule",
    "operator_reference",
)

CHIEF_OF_STAFF_ROUTE_MEMORY_TYPES: dict[str, tuple[str, ...]] = {
    "project-context-review": (
        "project_goal",
        "project_constraint",
        "project_decision",
        "project_status_snapshot",
        "portfolio_priority",
        "operator_strategy",
        "avoidance_rule",
    ),
    "budget-resource-review": (
        "budget_assumption",
        "project_constraint",
        "project_decision",
        "project_status_snapshot",
        "operator_strategy",
    ),
    "meeting-prep": (
        "project_goal",
        "project_constraint",
        "project_decision",
        "project_status_snapshot",
        "portfolio_priority",
    ),
    "portfolio-review": (
        "portfolio_priority",
        "project_status_snapshot",
        "project_constraint",
        "project_decision",
        "operator_strategy",
        "avoidance_rule",
    ),
    "research-direction-review": (
        "operator_strategy",
        "project_goal",
        "project_constraint",
        "project_decision",
        "portfolio_priority",
        "avoidance_rule",
    ),
    "memory-review": CHIEF_OF_STAFF_MEMORY_TYPES,
}

MANAGER_LOOP_EFFICIENCY_METRIC_NAME = "keystone.manager_loop.efficiency"
MANAGER_LOOP_EFFICIENCY_METRIC_VERSION = "v1"


def company_profile_memory_items(
    profile: CompanyProfile | dict[str, Any],
    *,
    approval_state: ApprovalState | str = ApprovalState.APPROVED_FOR_RESEARCH,
) -> list[MemoryItem]:
    """Build source-backed company memory items from a company profile."""

    record = (
        profile if isinstance(profile, CompanyProfile) else CompanyProfile.model_validate(profile)
    )
    source_ids = _source_ids_from_company(record)
    if not source_ids:
        return []
    object_key = normalize_memory_key(record.name)
    items = [
        MemoryItem(
            memory_type="company_profile_snapshot",
            object_type="company_profile",
            object_id=record.name,
            object_key=object_key,
            title=f"Company profile: {record.name}",
            summary=_first_non_empty(record.fit_summary, record.description, record.name),
            content={
                "company_name": record.name,
                "website": record.website,
                "consulting_fit_score": record.consulting_fit_score,
                "confidence_score": record.confidence_score,
                "behavioral_health_relevance": record.behavioral_health_relevance,
                "clinical_ai_relevance": record.clinical_ai_relevance,
                "cns_neuro_relevance": record.cns_neuro_relevance,
                "evidence_generation_need": record.evidence_generation_need,
                "outside_consulting_likelihood": record.outside_consulting_likelihood,
                "missing_information": record.missing_information[:8],
                "risks": record.risks[:8],
            },
            source_ids=source_ids,
            approval_state=approval_state,
            confidence=record.confidence_score,
            sensitivity="internal",
            metadata={"source": "company_profile"},
        )
    ]
    for claim in _source_backed_claims(record.claims, source_ids)[:12]:
        items.append(
            MemoryItem(
                memory_type="company_fact",
                object_type="company",
                object_id=record.name,
                object_key=object_key,
                title=f"{record.name}: {claim.claim_type}",
                summary=claim.claim_text,
                content={
                    "claim_text": claim.claim_text,
                    "claim_type": claim.claim_type,
                    "source_id": claim.source_id,
                },
                source_ids=[claim.source_id],
                approval_state=approval_state,
                confidence=claim.confidence,
                sensitivity="internal",
                metadata={"source": "company_profile_claim"},
            )
        )
    items.append(
        build_workflow_dedup_memory(
            action="company_research",
            company_name=record.name,
            object_id=record.name,
            source_ids=source_ids,
            approval_state=ApprovalState.APPROVED_FOR_RESEARCH,
            rationale="Company research was completed and source-backed memory was saved.",
        )
    )
    return items


def opportunity_memory_items(
    result_or_record: OpportunityScoutResult | ScoutOpportunityRecord | dict[str, Any],
    *,
    approval_state: ApprovalState | str = ApprovalState.APPROVED_FOR_RESEARCH,
) -> list[MemoryItem]:
    """Build source-backed opportunity memory items from scout output."""

    records = _coerce_opportunity_records(result_or_record)
    items: list[MemoryItem] = []
    for record in records:
        source_ids = [source.source_id for source in record.sources if source.source_id]
        if not source_ids:
            continue
        entity_kind = (record.entity_kind or "company").strip() or "company"
        canonical_entity_key = (record.canonical_entity_key or "").strip()
        object_key = normalize_memory_key(
            canonical_entity_key
            or (
                f"{entity_kind}:{record.company_name}"
                if entity_kind != "company"
                else record.company_name
            )
        )
        items.append(
            MemoryItem(
                memory_type="opportunity_signal",
                object_type="opportunity",
                object_id=record.company_name,
                object_key=object_key,
                title=f"Opportunity: {record.company_name}",
                summary=record.why_now_signal,
                content={
                    "company_name": record.company_name,
                    "entity_name": record.company_name,
                    "entity_kind": entity_kind,
                    "canonical_entity_key": canonical_entity_key or object_key,
                    "opportunity_type": record.opportunity_type,
                    "priority_score": record.priority_score,
                    "score_rationale": record.score_rationale,
                    "keystone_fit_reason": record.keystone_fit_reason,
                    "recommended_next_step": record.recommended_next_step,
                    "handoff_to_business_research_analyst": (
                        record.handoff_to_business_research_analyst
                    ),
                    "research_needed": record.research_needed[:8],
                    "approval_required_before_outreach": (record.approval_required_before_outreach),
                },
                source_ids=source_ids,
                approval_state=approval_state,
                confidence=max(0.0, min(1.0, record.priority_score / 100)),
                sensitivity="internal",
                metadata={"source": "opportunity_scout"},
            )
        )
        items.append(
            build_workflow_dedup_memory(
                action="opportunity",
                company_name=record.company_name,
                object_id=record.company_name,
                source_ids=source_ids,
                approval_state=ApprovalState.APPROVED_FOR_RESEARCH,
                rationale="Opportunity was already identified by Opportunity Scout.",
            )
        )
    return items


def opportunity_entity_memory_items(
    entity_or_entities: dict[str, Any] | Sequence[dict[str, Any]],
    *,
    approval_state: ApprovalState | str = ApprovalState.APPROVED_FOR_RESEARCH,
) -> list[MemoryItem]:
    """Build bounded source-backed memory for generalized opportunity entities."""

    items: list[MemoryItem] = []
    for entity in _coerce_entity_records(entity_or_entities):
        entity_name = _first_non_empty(
            entity.get("entity_name"),
            entity.get("company_name"),
            entity.get("name"),
            entity.get("title"),
        )
        if entity_name == "No summary available.":
            continue
        source_ids = _source_ids_from_mapping(entity)
        if not source_ids:
            continue
        entity_kind = _bounded_text(entity.get("entity_kind") or "company", max_chars=40)
        canonical_entity_key = _bounded_text(
            entity.get("canonical_entity_key")
            or (
                f"{entity_kind}:{entity_name}"
                if entity_kind and entity_kind != "company"
                else entity_name
            ),
            max_chars=160,
        )
        why_now = _first_non_empty(
            entity.get("why_now_signal"),
            entity.get("signal"),
            entity.get("summary"),
            entity.get("description"),
            entity_name,
        )
        priority_score = _bounded_score(entity.get("priority_score"))
        items.append(
            MemoryItem(
                memory_type="opportunity_signal",
                object_type="opportunity",
                object_id=entity_name,
                object_key=canonical_entity_key,
                title=f"Opportunity entity: {entity_name}",
                summary=why_now,
                content={
                    "entity_name": entity_name,
                    "company_name": _bounded_text(
                        entity.get("company_name") or entity_name,
                        max_chars=160,
                    ),
                    "entity_kind": entity_kind or "company",
                    "canonical_entity_key": canonical_entity_key,
                    "opportunity_type": _bounded_text(
                        entity.get("opportunity_type"),
                        max_chars=120,
                    ),
                    "priority_score": priority_score,
                    "why_now_signal": why_now,
                    "keystone_fit_reason": _bounded_text(
                        entity.get("keystone_fit_reason"),
                        max_chars=320,
                    ),
                    "recommended_next_step": _bounded_text(
                        entity.get("recommended_next_step") or entity.get("next_step"),
                        max_chars=240,
                    ),
                    "research_needed": _bounded_text_list(
                        entity.get("research_needed"),
                        max_items=8,
                        max_chars=160,
                    ),
                    "search_lanes": _bounded_text_list(
                        entity.get("search_lanes"),
                        max_items=8,
                        max_chars=80,
                    ),
                    "search_time_windows": _bounded_text_list(
                        entity.get("search_time_windows"),
                        max_items=8,
                        max_chars=80,
                    ),
                    "approval_required_before_outreach": bool(
                        entity.get("approval_required_before_outreach", True)
                    ),
                    "send_enabled": False,
                },
                source_ids=source_ids,
                approval_state=approval_state,
                confidence=max(0.0, min(1.0, priority_score / 100)),
                sensitivity="internal",
                metadata={"source": "opportunity_entity"},
            )
        )
    return items


def operator_reference_memory_item(
    *,
    title: str,
    summary: str,
    url: str = "",
    request_text: str = "",
    source: str = "chief_of_staff",
    approval_state: ApprovalState | str = ApprovalState.APPROVED_FOR_RESEARCH,
) -> MemoryItem:
    """Build prompt-safe memory for an operator-supplied reference."""

    bounded_title = _bounded_text(title or url or "Operator reference", max_chars=160)
    bounded_summary = _bounded_text(summary or bounded_title, max_chars=500)
    source_ids = [url] if url else ["operator_supplied_reference"]
    return MemoryItem(
        memory_type="operator_reference",
        object_type="other",
        object_id=url or bounded_title,
        object_key=normalize_memory_key(bounded_title),
        title=bounded_title,
        summary=bounded_summary,
        content={
            "title": bounded_title,
            "summary": bounded_summary,
            "url": url,
            "request_text": _bounded_text(request_text, max_chars=500),
            "source": source,
        },
        source_ids=source_ids,
        approval_state=approval_state,
        confidence=0.8 if url else 0.6,
        sensitivity="internal",
        metadata={"source": source, "reference_url": url},
    )


def chief_of_staff_memory_item(
    *,
    memory_type: str,
    title: str,
    summary: str,
    object_id: str = "",
    object_key: str = "",
    content: dict[str, Any] | None = None,
    source_ids: Sequence[str] = (),
    approval_state: ApprovalState | str = ApprovalState.APPROVED_FOR_RESEARCH,
    confidence: float = 0.7,
    sensitivity: str = "internal",
    supersedes_memory_id: int | None = None,
    expires_at: str | None = None,
) -> MemoryItem:
    """Build one prompt-safe strategic memory item for Chief of Staff."""

    if memory_type not in CHIEF_OF_STAFF_MEMORY_TYPES:
        raise ValueError(f"unsupported Chief of Staff memory type: {memory_type}")
    bounded_title = _bounded_text(title or summary or memory_type, max_chars=160)
    bounded_summary = _bounded_text(summary or bounded_title, max_chars=500)
    resolved_object_id = _bounded_text(object_id or bounded_title, max_chars=160)
    if memory_type in {"portfolio_priority", "operator_strategy", "avoidance_rule"}:
        object_type = "portfolio" if memory_type == "portfolio_priority" else "operator_strategy"
    else:
        object_type = "project"
    return MemoryItem(
        memory_type=memory_type,  # type: ignore[arg-type]
        object_type=object_type,  # type: ignore[arg-type]
        object_id=resolved_object_id,
        object_key=normalize_memory_key(object_key or resolved_object_id),
        title=bounded_title,
        summary=bounded_summary,
        content={
            "summary": bounded_summary,
            "memory_type": memory_type,
            "source": "chief_of_staff",
            **(content or {}),
        },
        source_ids=list(
            dict.fromkeys(_bounded_text(item, max_chars=240) for item in source_ids if item)
        ),
        approval_state=approval_state,
        confidence=max(0.0, min(1.0, confidence)),
        sensitivity=sensitivity,  # type: ignore[arg-type]
        metadata={"source": "chief_of_staff", "strategic_memory": True},
        supersedes_memory_id=supersedes_memory_id,
        expires_at=expires_at,
    )


def build_chief_of_staff_memory_context(
    *,
    query: str = "",
    route: str = "",
    object_key: str | None = None,
    memory_types: Sequence[str] | None = None,
    limit: int = 8,
    database_url: str | None = None,
) -> ChiefOfStaffMemoryContext:
    """Retrieve bounded approved strategic memory for a Chief of Staff run."""

    selected_types = tuple(
        memory_types or CHIEF_OF_STAFF_ROUTE_MEMORY_TYPES.get(route, CHIEF_OF_STAFF_MEMORY_TYPES)
    )
    store = SQLiteStore(database_url)
    records = store.retrieve_memory(
        query=query,
        object_key=object_key,
        memory_types=list(selected_types),
        limit=max(1, min(int(limit or 8), 12)),
        approved_only=True,
        safe_for_prompt=True,
    )
    all_relevant = store.list_memory_items(approved_only=True, safe_for_prompt=True)
    superseded_ids = {
        item.supersedes_memory_id for item in all_relevant if item.supersedes_memory_id
    }
    filtered = [
        item
        for item in records
        if (item.id not in superseded_ids) and not _memory_item_is_expired(item)
    ]
    missing_reason = ""
    if not filtered:
        missing_reason = (
            "No approved prompt-safe Chief of Staff strategic memory matched this request."
        )
    return ChiefOfStaffMemoryContext(
        query=query,
        route=route,
        object_key=object_key or "",
        memory_types=list(selected_types),
        records=filtered,
        missing_reason=missing_reason,
        approved_only=True,
        safe_for_prompt=True,
        send_enabled=False,
    )


def outreach_dedup_memory_items(
    draft: OutreachDraft | dict[str, Any],
    *,
    approval_state: ApprovalState | str = ApprovalState.PENDING,
) -> list[MemoryItem]:
    """Build workflow dedup records for outreach drafts without enabling sending."""

    record = draft if isinstance(draft, OutreachDraft) else OutreachDraft.model_validate(draft)
    source_ids = list(record.source_ids_used)
    items = [
        build_workflow_dedup_memory(
            action="outreach_company",
            company_name=record.company_name,
            contact=record.contact_name or record.recipient,
            object_id=record.company_name,
            source_ids=source_ids,
            approval_state=approval_state,
            rationale="An outreach draft already exists for this company.",
        )
    ]
    body_hash = _stable_hash(record.email_body)
    items.append(
        build_workflow_dedup_memory(
            action="outbound_email",
            company_name=record.company_name,
            contact=record.contact_name or record.recipient,
            subject=record.email_subject,
            body_hash=body_hash,
            object_id=record.company_name,
            source_ids=source_ids,
            approval_state=approval_state,
            rationale="A matching outbound email draft already exists.",
        )
    )
    return items


def build_email_style_profile_from_feedback(
    *,
    profile_id: str,
    drafts: Sequence[OutreachDraft | dict[str, Any]],
    feedback_records: Sequence[FeedbackRecord | dict[str, Any]] = (),
    approval_state: ApprovalState | str = ApprovalState.APPROVED_FOR_DRAFTING,
) -> EmailStyleProfile:
    """Derive an aggregate style profile from approved drafts and human feedback."""

    approved_drafts = [
        draft if isinstance(draft, OutreachDraft) else OutreachDraft.model_validate(draft)
        for draft in drafts
    ]
    approved_drafts = [
        draft
        for draft in approved_drafts
        if str(draft.approval_state) == ApprovalState.APPROVED_FOR_EXTERNAL_USE.value
    ]
    if not approved_drafts:
        raise ValueError("at least one externally approved draft is required for style learning")

    feedback = [
        item if isinstance(item, FeedbackRecord) else FeedbackRecord.model_validate(item)
        for item in feedback_records
    ]
    tags = {tag for item in feedback for tag in item.tags}
    body_texts = [draft.email_body for draft in approved_drafts]
    greetings = list(dict.fromkeys(_greeting_pattern(body) for body in body_texts))
    signoffs = list(dict.fromkeys(_signoff(body) for body in body_texts if _signoff(body)))
    preferred_phrases = _preferred_phrases(body_texts, tags)
    avoided_phrases = _avoided_phrases(tags)
    sentence_words = _average_sentence_words(body_texts)

    profile_slug = normalize_memory_key(profile_id).replace(" ", "-")
    return EmailStyleProfile(
        profile_id=profile_id,
        source="local_feedback",
        source_id=f"memory:email_style:{profile_slug}",
        source_url=f"local://memory/email_style/{profile_slug}",
        confidence=min(0.95, 0.55 + 0.1 * len(approved_drafts) + 0.05 * len(feedback)),
        approval_state=approval_state,
        approval_scope="drafting",
        greeting_patterns=[item for item in greetings if item],
        signoffs=signoffs,
        sentence_length="short" if sentence_words <= 13 else "medium",
        average_sentence_words=sentence_words,
        directness="high" if "too_verbose" in tags else "medium",
        cta_style="soft_question",
        formality="professional",
        formatting_preferences=_formatting_preferences(tags),
        preferred_phrases=preferred_phrases,
        avoided_phrases=avoided_phrases,
        approved_sample_snippets=_approved_snippets(body_texts),
        notes=_style_notes(feedback),
        raw_sent_email_bodies_included=False,
        send_enabled=False,
        sent=False,
    )


def feedback_memory_item(
    feedback: FeedbackRecord | dict[str, Any],
    *,
    approval_item: ApprovalQueueItem | dict[str, Any] | None = None,
    approval_state: ApprovalState | str = ApprovalState.APPROVED_FOR_DRAFTING,
) -> MemoryItem:
    """Build one prompt-safe memory item from approval-linked human feedback."""

    record = (
        feedback
        if isinstance(feedback, FeedbackRecord)
        else FeedbackRecord.model_validate(feedback)
    )
    item = (
        approval_item
        if isinstance(approval_item, ApprovalQueueItem) or approval_item is None
        else ApprovalQueueItem.model_validate(approval_item)
    )
    object_key_source = (
        (item.object_id if item is not None and item.object_id else record.object_id)
        or (item.title if item is not None else "")
        or record.object_id
    )
    artifact_label = (
        item.title
        if item is not None and item.title
        else f"{record.object_type} {record.object_id}"
    )
    notes_summary = _feedback_notes_summary(record.notes)
    summary = f"Human feedback rated {artifact_label} as {record.rating}."
    if record.tags:
        summary = f"{summary} Tags: {', '.join(record.tags[:4])}."
    source_ids = []
    if record.approval_id:
        source_ids.append(f"approval:{record.approval_id}")
    if record.id is not None:
        source_ids.append(f"feedback:{record.id}")
    if item is not None and item.object_id:
        source_ids.append(f"{item.object_type.value}:{item.object_id}")
    return MemoryItem(
        memory_type="human_feedback",
        object_type="feedback",
        object_id=str(record.id or record.object_id),
        object_key=normalize_memory_key(object_key_source),
        title=f"Human feedback: {artifact_label}",
        summary=summary,
        content={
            "artifact_object_type": record.object_type,
            "artifact_object_id": record.object_id,
            "approval_id": record.approval_id or "",
            "approval_status": item.approval_status.value if item is not None else "",
            "artifact_title": item.title if item is not None else "",
            "artifact_summary": item.summary if item is not None else "",
            "source_agent": record.source_agent,
            "review_stage": record.review_stage,
            "rating": record.rating,
            "tags": record.tags,
            "risk_flags": record.risk_flags,
            "notes_summary": notes_summary,
        },
        source_ids=source_ids,
        approval_state=approval_state,
        confidence=0.85,
        sensitivity="internal",
        metadata={"source": "human_feedback", "feedback_id": record.id},
    )


def approval_decision_memory_item(
    decision: ApprovalDecisionRecord | dict[str, Any],
    *,
    outcome: dict[str, Any] | None = None,
    approval_item: ApprovalQueueItem | dict[str, Any] | None = None,
    approval_state: ApprovalState | str = ApprovalState.APPROVED_FOR_DRAFTING,
) -> MemoryItem:
    """Build one prompt-safe memory item from an approval decision and outcome."""

    record = (
        decision
        if isinstance(decision, ApprovalDecisionRecord)
        else ApprovalDecisionRecord.model_validate(decision)
    )
    item = (
        approval_item
        if isinstance(approval_item, ApprovalQueueItem) or approval_item is None
        else ApprovalQueueItem.model_validate(approval_item)
    )
    outcome_context = _approval_outcome_context(outcome or {})
    object_key_source = item.object_id if item is not None and item.object_id else record.object_id
    artifact_label = (
        item.title
        if item is not None and item.title
        else f"{record.object_type} {record.object_id}"
    )
    summary = (
        f"Human approval decision for {artifact_label}: "
        f"{record.decision.value} for {record.scope.value}."
    )
    if outcome_context.get("outcome_summary"):
        summary = f"{summary} Outcome: {outcome_context['outcome_summary']}"
    source_ids = [f"approval_decision:{record.object_type}:{record.object_id}"]
    if item is not None:
        source_ids.append(f"approval:{item.id}")
    return MemoryItem(
        memory_type="approval_decision",
        object_type="approval",
        object_id=record.object_id,
        object_key=object_key_source,
        title=f"Approval decision: {artifact_label}",
        summary=summary,
        content={
            "artifact_object_type": record.object_type,
            "artifact_object_id": record.object_id,
            "decision": record.decision.value,
            "scope": record.scope.value,
            "reviewer": _bounded_text(record.reviewer, max_chars=120),
            "timestamp": record.timestamp,
            "notes_summary": _feedback_notes_summary(record.notes),
            "risk_flags": _bounded_text_list(record.risk_flags, max_items=8, max_chars=120),
            "source_agent": _bounded_text(record.source_agent, max_chars=120),
            "approval_queue_id": item.id if item is not None else "",
            "approval_queue_status": (item.approval_status.value if item is not None else ""),
            "artifact_title": item.title if item is not None else "",
            "artifact_summary": item.summary if item is not None else "",
            "outcome": outcome_context,
            "send_enabled": False,
        },
        source_ids=list(dict.fromkeys(source_ids)),
        approval_state=approval_state,
        confidence=0.9,
        sensitivity="internal",
        metadata={"source": "approval_decision"},
    )


def email_style_memory_item(profile: EmailStyleProfile) -> MemoryItem:
    """Build one prompt-safe memory item from an approved aggregate style profile."""

    return MemoryItem(
        memory_type="email_style_preference",
        object_type="email_style_profile",
        object_id=profile.profile_id,
        object_key=normalize_memory_key(profile.profile_id),
        title=f"Email style profile: {profile.profile_id}",
        summary=(
            f"{profile.formality} tone, {profile.directness} directness, {profile.cta_style} CTA."
        ),
        content=profile.safe_prompt_context(),
        source_ids=[profile.source_id],
        approval_state=profile.approval_state,
        confidence=profile.confidence,
        sensitivity="internal",
        metadata={"source": "email_style_profile"},
    )


def _feedback_notes_summary(value: str, *, max_chars: int = 240) -> str:
    text = " ".join(str(value or "").replace("\u2014", "-").split()).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def outreach_example_memory_item(example: OutreachExampleRecord | dict[str, Any]) -> MemoryItem:
    """Build one prompt-safe memory item from a sanitized outreach example."""

    from keystone_agents.outreach_examples import outreach_example_memory_item as _build

    return _build(example)


def build_workflow_dedup_memory(
    *,
    action: WorkflowDedupAction,
    company_name: str,
    object_id: str = "",
    contact: str | None = None,
    subject: str | None = None,
    body_hash: str | None = None,
    source_ids: Sequence[str] = (),
    approval_state: ApprovalState | str = ApprovalState.PENDING,
    rationale: str = "",
) -> MemoryItem:
    """Create a data-only workflow dedup marker."""

    dedup_key = workflow_dedup_key(
        action=action,
        company_name=company_name,
        contact=contact,
        subject=subject,
        body_hash=body_hash,
    )
    return MemoryItem(
        memory_type="workflow_dedup",
        object_type="workflow",
        object_id=object_id or company_name,
        object_key=dedup_key,
        title=f"Workflow dedup: {action} for {company_name}",
        summary=rationale or f"Workflow action already recorded: {action}.",
        content={
            "action": action,
            "company_name": company_name,
            "contact": contact or "",
            "subject": subject or "",
            "body_hash": body_hash or "",
            "dedup_key": dedup_key,
            "send_enabled": False,
        },
        source_ids=list(dict.fromkeys(source_id for source_id in source_ids if source_id)),
        approval_state=approval_state,
        confidence=1.0,
        sensitivity="internal",
        safe_for_prompt=True,
        metadata={"source": "workflow_dedup"},
        send_enabled=False,
        sent=False,
    )


def workflow_dedup_key(
    *,
    action: WorkflowDedupAction,
    company_name: str,
    contact: str | None = None,
    subject: str | None = None,
    body_hash: str | None = None,
) -> str:
    """Return the deterministic dedup key for a workflow action."""

    parts = [action, normalize_memory_key(company_name)]
    if action in {"outreach_company", "outbound_email"}:
        parts.append(normalize_memory_key(contact or "company"))
    if action == "outbound_email":
        parts.append(normalize_memory_key(subject or "no subject"))
        if body_hash:
            parts.append(str(body_hash)[:16])
    return ":".join(parts)


def check_workflow_duplicate(
    *,
    action: WorkflowDedupAction,
    company_name: str,
    contact: str | None = None,
    subject: str | None = None,
    body_hash: str | None = None,
    database_url: str | None = None,
) -> dict[str, Any]:
    """Check local workflow memory for duplicate research, opportunity, or outreach work."""

    key = workflow_dedup_key(
        action=action,
        company_name=company_name,
        contact=contact,
        subject=subject,
        body_hash=body_hash,
    )
    store = SQLiteStore(database_url)
    matches = store.list_memory_items(
        memory_type="workflow_dedup",
        object_key=key,
        approved_only=False,
        safe_for_prompt=True,
    )
    return {
        "duplicate": bool(matches),
        "dedup_key": key,
        "matches": [item.prompt_context() for item in matches],
        "send_enabled": False,
    }


def save_memory_items(
    items: Iterable[MemoryItem],
    *,
    database_url: str | None = None,
) -> list[int]:
    """Persist local memory items and return row ids."""

    store = SQLiteStore(database_url)
    return [store.save_memory_item(item) for item in items]


def retrieval_tool_performance_memory_item(
    retrieval_metadata: dict[str, Any],
    *,
    object_id: str = "company_research",
    approval_state: ApprovalState | str = ApprovalState.APPROVED_FOR_RESEARCH,
) -> MemoryItem | None:
    """Build prompt-safe memory about which retrieval tools added useful evidence."""

    ladder = retrieval_metadata.get("retrieval_ladder")
    if not isinstance(ladder, list) or not ladder:
        return None
    useful_rungs = [
        str(rung.get("rung") or "retrieval").replace("_", " ")
        for rung in ladder
        if isinstance(rung, dict) and rung.get("useful")
    ]
    providers = _retrieval_providers(retrieval_metadata)
    summary = (
        "Useful retrieval tools: " + ", ".join(useful_rungs)
        if useful_rungs
        else "No retrieval ladder rung produced enough source evidence."
    )
    if providers:
        summary = f"{summary} Providers observed: {', '.join(providers)}."
    return MemoryItem(
        memory_type="retrieval_tool_performance",
        object_type="workflow",
        object_id=object_id,
        object_key=normalize_memory_key(f"retrieval tool performance {object_id}"),
        title=f"Retrieval tool performance: {object_id}",
        summary=summary,
        content={
            "retrieval_ladder": ladder,
            "website_extraction": retrieval_metadata.get("website_extraction") or {},
            "search_provider_sequence": retrieval_metadata.get("search_provider_sequence") or [],
            "search_provider_used": retrieval_metadata.get("search_provider_used") or "",
            "provider_usage": retrieval_metadata.get("provider_usage") or {},
            "provider_value_summary": retrieval_metadata.get("provider_value_summary") or [],
        },
        source_ids=["local:retrieval_ladder"],
        approval_state=approval_state,
        confidence=0.75 if useful_rungs else 0.4,
        sensitivity="internal",
        metadata={"source": "retrieval_metadata"},
    )


def manager_loop_efficiency_memory_item(
    metrics: dict[str, Any],
    *,
    object_id: str,
    approval_state: ApprovalState | str = ApprovalState.APPROVED_FOR_RESEARCH,
) -> MemoryItem | None:
    """Build prompt-safe memory for comparing manager-loop efficiency over time."""

    if not metrics:
        return None
    metric_name = _bounded_text(
        metrics.get("metric_name") or MANAGER_LOOP_EFFICIENCY_METRIC_NAME,
        max_chars=100,
    )
    metric_version = _bounded_text(
        metrics.get("metric_version") or MANAGER_LOOP_EFFICIENCY_METRIC_VERSION,
        max_chars=20,
    )
    final_route = _bounded_text(metrics.get("final_route"), max_chars=80)
    final_status = _bounded_text(metrics.get("final_status"), max_chars=40)
    elapsed_seconds = _bounded_float(metrics.get("elapsed_seconds"))
    step_count = _bounded_int(metrics.get("step_count"))
    repair_count = _bounded_int(metrics.get("repair_count"))
    blocker_count = _bounded_int(metrics.get("blocker_count"))
    efficiency_signal = _bounded_text(metrics.get("efficiency_signal"), max_chars=60)
    summary = (
        f"{metric_name} {metric_version}: {final_route or 'unknown route'} "
        f"ended {final_status or 'unknown'} in {elapsed_seconds:.3f}s with "
        f"{step_count} step(s), {repair_count} repair(s), "
        f"{blocker_count} blocker(s)."
    )
    if efficiency_signal:
        summary = f"{summary} Signal: {efficiency_signal}."
    return MemoryItem(
        memory_type="manager_loop_efficiency",
        object_type="workflow",
        object_id=object_id,
        object_key=normalize_memory_key(f"manager loop efficiency {object_id}"),
        title=f"Manager loop efficiency: {object_id}",
        summary=summary,
        content={
            "metric_name": metric_name,
            "metric_version": metric_version,
            "schema": _bounded_text(metrics.get("schema"), max_chars=120),
            "elapsed_seconds": elapsed_seconds,
            "latency_bucket": _bounded_text(metrics.get("latency_bucket"), max_chars=40),
            "step_count": step_count,
            "specialist_step_count": _bounded_int(metrics.get("specialist_step_count")),
            "repair_count": repair_count,
            "repair_rate": _bounded_float(metrics.get("repair_rate")),
            "repair_attempts_by_route": _bounded_mapping(metrics.get("repair_attempts_by_route")),
            "route_sequence": _bounded_text_list(
                metrics.get("route_sequence"),
                max_items=8,
                max_chars=80,
            ),
            "final_route": final_route,
            "final_status": final_status,
            "advanced": bool(metrics.get("advanced")),
            "artifact_count": _bounded_int(metrics.get("artifact_count")),
            "blocker_count": blocker_count,
            "live_search": bool(metrics.get("live_search")),
            "live_sdk": bool(metrics.get("live_sdk")),
            "final_synthesis_executed": bool(metrics.get("final_synthesis_executed")),
            "seconds_per_specialist_step": _bounded_float(
                metrics.get("seconds_per_specialist_step")
            ),
            "completion_without_blockers": bool(metrics.get("completion_without_blockers")),
            "efficiency_signal": efficiency_signal,
        },
        source_ids=["local:manager_loop_efficiency"],
        approval_state=approval_state,
        confidence=0.8 if blocker_count == 0 else 0.55,
        sensitivity="internal",
        metadata={
            "source": "manager_loop_metrics",
            "metric_name": metric_name,
            "metric_version": metric_version,
        },
    )


def _retrieval_providers(retrieval_metadata: dict[str, Any]) -> list[str]:
    providers: list[str] = []
    for provider in retrieval_metadata.get("search_providers_used") or []:
        text = str(provider).strip()
        if text:
            providers.append(text)
    website = retrieval_metadata.get("website_extraction")
    if isinstance(website, dict):
        for provider in website.get("providers_used") or []:
            text = str(provider).strip()
            if text:
                providers.append(text)
    return list(dict.fromkeys(providers))


def _bounded_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number < 0:
        return 0.0
    if number > 86_400:
        return 86_400.0
    return round(number, 3)


def _bounded_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(10_000, number))


def _bounded_mapping(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    bounded: dict[str, int] = {}
    for key, raw_value in list(value.items())[:12]:
        clean_key = _bounded_text(key, max_chars=80)
        if clean_key:
            bounded[clean_key] = _bounded_int(raw_value)
    return bounded


def _source_ids_from_company(profile: CompanyProfile) -> list[str]:
    return list(dict.fromkeys(source.source_id for source in profile.sources if source.source_id))


def _source_backed_claims(
    claims: Sequence[ClaimEvidenceRecord],
    source_ids: Sequence[str],
) -> list[ClaimEvidenceRecord]:
    source_set = set(source_ids)
    return [
        claim
        for claim in claims
        if claim.approved
        and claim.confidence > 0
        and claim.source_id in source_set
        and claim.claim_type != "unsupported"
    ]


def _coerce_opportunity_records(
    value: OpportunityScoutResult | ScoutOpportunityRecord | dict[str, Any],
) -> list[ScoutOpportunityRecord]:
    if isinstance(value, OpportunityScoutResult):
        return value.records
    if isinstance(value, ScoutOpportunityRecord):
        return [value]
    if "records" in value:
        return OpportunityScoutResult.model_validate(value).records
    return [ScoutOpportunityRecord.model_validate(value)]


def _coerce_entity_records(
    value: dict[str, Any] | Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        if isinstance(value.get("records"), list):
            return [_as_mapping(item) for item in value["records"]]
        return [_as_mapping(value)]
    return [_as_mapping(item) for item in value]


def _as_mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    raise TypeError("entity memory inputs must be dict-like records")


def _source_ids_from_mapping(value: dict[str, Any]) -> list[str]:
    source_ids: list[str] = []
    raw_source_ids = value.get("source_ids")
    if isinstance(raw_source_ids, list):
        source_ids.extend(_bounded_text(item, max_chars=180) for item in raw_source_ids)
    sources = value.get("sources")
    if isinstance(sources, list):
        for source in sources:
            if isinstance(source, dict):
                source_ids.append(_bounded_text(source.get("source_id"), max_chars=180))
    return list(dict.fromkeys(source_id for source_id in source_ids if source_id))


def _approval_outcome_context(outcome: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(outcome, dict):
        return {}
    return {
        "outcome_status": _bounded_text(
            outcome.get("outcome_status") or outcome.get("status"),
            max_chars=80,
        ),
        "outcome_summary": _bounded_text(
            outcome.get("outcome_summary") or outcome.get("summary"),
            max_chars=240,
        ),
        "next_step": _bounded_text(
            outcome.get("next_step") or outcome.get("recommended_next_step"),
            max_chars=180,
        ),
        "lessons": _bounded_text_list(
            outcome.get("lessons") or outcome.get("lessons_learned"),
            max_items=6,
            max_chars=140,
        ),
        "blocked_reason": _bounded_text(outcome.get("blocked_reason"), max_chars=180),
    }


def _bounded_text_list(
    value: Any,
    *,
    max_items: int,
    max_chars: int,
) -> list[str]:
    if value is None:
        return []
    raw_items = value if isinstance(value, list | tuple | set) else [value]
    return [
        item
        for item in (
            _bounded_text(raw_item, max_chars=max_chars) for raw_item in list(raw_items)[:max_items]
        )
        if item
    ]


def _bounded_text(value: Any, *, max_chars: int) -> str:
    text = " ".join(str(value or "").replace("\u2014", "-").split()).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def _bounded_score(value: Any) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def _memory_item_is_expired(item: MemoryItem) -> bool:
    if not item.expires_at:
        return False
    try:
        expires_at = datetime.fromisoformat(item.expires_at.replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC)


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = _bounded_text(value, max_chars=500)
        if text:
            return text
    return "No summary available."


def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _greeting_pattern(body: str) -> str:
    first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    lowered = first_line.lower()
    if lowered.startswith("hi "):
        return "Hi {name},"
    if lowered.startswith("hello "):
        return "Hello {name},"
    return first_line[:80]


def _signoff(body: str) -> str:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if len(lines) < 2:
        return ""
    tail = lines[-2:] if len(lines[-1].split()) <= 3 else lines[-1:]
    signoff = "\n".join(tail)
    if len(signoff.split()) > 12:
        return ""
    return signoff


def _preferred_phrases(body_texts: Sequence[str], tags: set[str]) -> list[str]:
    phrases: list[str] = []
    joined = "\n".join(body_texts).lower()
    if "compare notes" in joined:
        phrases.append("compare notes")
    if "excellent_personalization" in tags:
        phrases.append("source-backed personalization")
    return list(dict.fromkeys(phrases))


def _avoided_phrases(tags: set[str]) -> list[str]:
    phrases = []
    if "too_salesy" in tags:
        phrases.append("salesy language")
    if "too_generic" in tags:
        phrases.append("generic outreach")
    if "unsafe_claim" in tags:
        phrases.append("unsupported Keystone claims")
    if "missing_requested_action" in tags:
        phrases.append("omitting the requested action")
    return phrases


def _formatting_preferences(tags: set[str]) -> list[str]:
    preferences = ["Short paragraphs", "No em dash"]
    if "too_verbose" in tags:
        preferences.append("Keep drafts concise")
    return preferences


def _average_sentence_words(body_texts: Sequence[str]) -> int:
    sentences: list[str] = []
    for body in body_texts:
        sentences.extend(
            sentence.strip()
            for sentence in re.split(r"[.!?]\s+", body.replace("\n", " "))
            if sentence.strip()
        )
    if not sentences:
        return 16
    word_counts = [len(sentence.split()) for sentence in sentences]
    return max(4, min(40, round(sum(word_counts) / len(word_counts))))


def _approved_snippets(body_texts: Sequence[str]) -> list[str]:
    snippets: list[str] = []
    for body in body_texts:
        for line in body.splitlines():
            cleaned = " ".join(line.split()).strip()
            if 4 <= len(cleaned.split()) <= 30 and not cleaned.lower().startswith(
                ("hi ", "hello ")
            ):
                snippets.append(cleaned)
                break
    return list(dict.fromkeys(snippets))[:5]


def _style_notes(feedback: Sequence[FeedbackRecord]) -> str:
    notes = [
        " ".join(item.notes.replace("\u2014", "-").split()).strip()
        for item in feedback
        if item.notes.strip()
    ]
    return " | ".join(notes)[:300]
