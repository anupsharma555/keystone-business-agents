"""Build a bounded source bundle for a Chief of Staff weekly operations packet."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Any

from keystone_agents.privacy_minimized_synthesis import (
    PrivacyMinimizedAssertion,
    PrivacyMinimizedSynthesisPacket,
    build_concept_signal_packet,
    validate_privacy_minimized_packet,
)
from keystone_agents.schemas.weekly_ops import WeeklyOpsAssemblyInput

PACKET_FOLDER_NAME = "KNIOps"
PACKET_TITLE_PREFIX = "KNI Weekly Operations Packet"
_EXTERNAL_FACT_FIELDS = frozenset(
    {"summary", "next_action", "carry_forward", "relevance", "usage"}
)
_WEEKLY_OPS_TAXONOMY = {
    "completed_operation": ("completed", "finished", "passed"),
    "follow_up_required": ("follow-up", "follow up", "needs reply", "response needed"),
    "approval_state": ("approved", "approval", "review required"),
    "decision_or_action": ("decision", "next action", "owner", "carry forward"),
    "calendar_focus": ("calendar", "meeting", "review", "sync"),
    "recurring_cadence": ("weekly", "recurring", "cadence"),
    "blocker_or_failure": ("blocked", "failed", "failure", "missing", "incomplete"),
    "usage_evidence": ("usage", "cost", "request", "receipt"),
    "safety_boundary": ("no-send", "no send", "draft-only", "without a draft"),
}
_WORKSTREAM_TERMS = (
    ("calendar_operations", ("calendar", "meeting", "event", "schedule", "deadline")),
    ("gmail_operations", ("gmail", "email", "inbox", "reply", "draft")),
    ("finance_operations", ("finance", "expense", "income", "tax", "budget")),
    ("workspace_operations", ("drive", "document", "doc", "sheet", "folder")),
    ("research_operations", ("research", "source", "evidence", "company", "article")),
    ("outreach_operations", ("outreach", "contact", "relationship")),
    ("agent_validation", ("agent", "validation", "test", "smoke", "workflow")),
)
_OWNER_ROLE_TERMS = (
    ("chief_of_staff", ("chief", "chief_of_staff")),
    ("business_research", ("business_research", "research analyst")),
    ("opportunity_scout", ("opportunity", "scout")),
    ("outreach_composer", ("outreach", "composer")),
    ("gmail_triage", ("gmail", "triage")),
    ("workspace_agent", ("workspace", "drive")),
    ("airtable_agent", ("airtable",)),
    ("zotero_agent", ("zotero",)),
    ("operator", ("operator", "anup")),
)


def build_weekly_ops_source_bundle(payload: WeeklyOpsAssemblyInput) -> dict[str, Any]:
    """Return a synthesis-ready source bundle without accepting raw provider bodies."""

    title = f"{PACKET_TITLE_PREFIX} — {payload.window.packet_date}"
    non_recurring = [event for event in payload.calendar if not event.is_recurring]
    recurring = [event for event in payload.calendar if event.is_recurring]
    recurring_groups = _recurring_calendar_groups(recurring)
    completed_runs = sorted(
        [run for run in payload.completed_runs if run.packet_role != "operational_health_only"],
        key=lambda item: (item.completed_at, item.agent_name.lower(), item.run_id),
        reverse=True,
    )
    health_runs = sorted(
        [run for run in payload.completed_runs if run.packet_role == "operational_health_only"],
        key=lambda item: (item.completed_at, item.agent_name.lower(), item.run_id),
        reverse=True,
    )
    sources = [
        _source(
            source_id=(
                f"slack:weekly:{payload.window.packet_date}"
            ),
            title="Seven-day Slack workstream summary",
            source_type="bounded_slack_summary",
            provider="slack",
            facts=[
                _fact(
                    item.summary,
                    owner=item.action_owner,
                    identity=item.message_ts,
                    link=item.source_link,
                )
                for item in payload.slack
            ],
        ),
        _source(
            source_id=f"gmail:weekly:{payload.window.packet_date}",
            title="Seven-day Gmail thread summary",
            source_type="bounded_gmail_thread_summary",
            provider="gmail",
            facts=[
                _fact(
                    f"{item.subject}: {item.summary}",
                    owner=item.sender_label,
                    identity=item.thread_id,
                    next_action=item.follow_up,
                )
                for item in payload.gmail
            ],
        ),
        _source(
            source_id=f"agent-runs:completed:{payload.window.packet_date}",
            title="Completed agent runs from the packet window",
            source_type="bounded_completed_agent_run_summary",
            provider="agent_run_store",
            facts=[
                _fact(
                    f"{item.agent_name}: {item.outcome_summary}",
                    identity=item.run_id,
                    completed_at=item.completed_at,
                    route=item.route,
                    work_item_id=item.work_item_id,
                    usage=item.usage_summary,
                    receipt=item.receipt_reference,
                    carry_forward=item.carry_forward,
                    relevance=item.relevance_reason,
                )
                for item in completed_runs
            ],
        ),
        _source(
            source_id=f"calendar:one-time:{payload.window.packet_date}",
            title="Non-recurring Calendar focus areas",
            source_type="bounded_calendar_non_recurring_summary",
            provider="google_calendar",
            facts=[
                _fact(
                    event.title,
                    identity=event.event_id,
                    start=event.start,
                    end=event.end,
                )
                for event in non_recurring
            ],
        ),
        _source(
            source_id=f"calendar:recurring:{payload.window.packet_date}",
            title="Recurring Calendar cadence",
            source_type="bounded_calendar_recurring_summary",
            provider="google_calendar",
            facts=recurring_groups,
        ),
    ]
    return {
        "schema": "keystone.work_item.source_bundle.v1",
        "source": "weekly_ops_bounded_assembly",
        "supplied_material_only": True,
        "target": {
            "name": title,
            "object_type": "portfolio_review",
        },
        "window": payload.window.model_dump(mode="json"),
        "delivery_plan": {
            "folder_name": PACKET_FOLDER_NAME,
            "folder_match_required": "exactly_one",
            "document_title": title,
            "document_read_back_required": True,
            "slack_target": "channel",
            "slack_channel_name": "ops-finance",
            "slack_payload": "concise_summary_and_verified_doc_link",
            "workspace_write_approval_required": True,
            "slack_post_approval_required": True,
        },
        "sources": sources,
        "section_order": [
            "executive_focus_areas",
            "workstreams_and_decisions",
            "completed_runs_and_outcomes",
            "carry_forward_from_completed_runs",
            "non_recurring_calendar_focus",
            "recurring_calendar_cadence",
            "next_actions",
            "source_basis",
            "operational_health",
            "packet_metadata",
        ],
        "terminal_section_contract": {
            "operational_health": (
                "Include only relevant provider failures, incomplete runs, missing usage "
                "or receipts, and safety-boundary exceptions. Keep this succinct."
            ),
            "packet_metadata": (
                "Use one human-readable footer line containing only the packet window, "
                "source coverage, model/request usage, and verified Doc destination. "
                "Exclude schemas, routes, node paths, tool traces, and raw IDs."
            ),
        },
        "operational_health_signals": [
            _fact(
                f"{item.agent_name}: {item.outcome_summary}",
                completed_at=item.completed_at,
                usage=item.usage_summary,
                receipt=item.receipt_reference,
                relevance=item.relevance_reason,
            )
            for item in health_runs[:8]
        ],
        "safety": {
            "raw_slack_bodies_included": False,
            "raw_gmail_bodies_included": False,
            "calendar_descriptions_included": False,
            "calendar_attendees_included": False,
            "external_writes_enabled": False,
            "send_enabled": False,
        },
        "synthesis_ready": all(source["key_facts"] for source in sources[:4]),
    }


def build_weekly_ops_external_synthesis_bundle(
    payload: WeeklyOpsAssemblyInput,
    *,
    personal_redaction_terms: Sequence[str] = (),
) -> dict[str, Any]:
    """Project the local packet into a non-personal, identity-free model input."""

    local_bundle = build_weekly_ops_source_bundle(payload)
    sources: list[dict[str, Any]] = []
    for source in local_bundle["sources"]:
        facts = []
        for fact in source["key_facts"]:
            projected = {
                key: _sanitize_external_text(value, personal_redaction_terms)
                for key, value in fact.items()
                if key in _EXTERNAL_FACT_FIELDS and str(value or "").strip()
            }
            date_value = fact.get("completed_at") or fact.get("start") or fact.get(
                "first_start"
            )
            if date_value:
                projected["date"] = str(date_value)[:10]
            if projected.get("summary"):
                facts.append(projected)
        sources.append(
            {
                "title": source["title"],
                "source_type": source["source_type"],
                "supported_claim": source["supported_claim"],
                "key_facts": facts,
            }
        )

    return {
        "schema": "keystone.weekly_ops.external_synthesis_bundle.v1",
        "workflow_name": "Chief Prior Week Packet",
        "data_classification": "operator-approved non-personal business operations",
        "transmission_contract": {
            "raw_provider_content": False,
            "provider_identifiers": False,
            "personal_names": False,
            "email_addresses": False,
            "urls": False,
            "exact_timestamps": False,
            "provider_writes": False,
        },
        "target": local_bundle["target"],
        "window": {
            "date_min": payload.window.time_min[:10],
            "date_max": payload.window.time_max[:10],
            "packet_date": payload.window.packet_date,
        },
        "sources": sources,
        "section_order": local_bundle["section_order"],
        "terminal_section_contract": local_bundle["terminal_section_contract"],
        "synthesis_ready": all(source["key_facts"] for source in sources[:4]),
    }


def build_weekly_ops_privacy_minimized_packet(
    payload: WeeklyOpsAssemblyInput,
) -> PrivacyMinimizedSynthesisPacket:
    """Derive non-identifying weekly concept signals while keeping prose local."""

    local_bundle = build_weekly_ops_source_bundle(payload)
    sources: dict[str, str] = {}
    for source_index, source in enumerate(local_bundle["sources"]):
        for fact_index, fact in enumerate(source["key_facts"]):
            sources[f"source_{source_index}_fact_{fact_index}"] = " ".join(
                str(value or "")
                for value in fact.values()
                if str(value or "").strip()
            )
    for signal_index, signal in enumerate(local_bundle["operational_health_signals"]):
        sources[f"health_signal_{signal_index}"] = " ".join(
            str(value or "")
            for value in signal.values()
            if str(value or "").strip()
        )
    minimized = build_concept_signal_packet(
        workflow="weekly_ops_packet",
        sources=sources,
        taxonomy=_WEEKLY_OPS_TAXONOMY,
        constraints=(
            "abstracted_evidence_only",
            "required_sections",
            "review_only",
            "no_external_action",
            "human_review_required",
        ),
    )
    source_hash_by_ref = dict(zip(sources, minimized.source_hashes, strict=True))
    assertions = _weekly_assertion_layer(
        local_bundle,
        source_hash_by_ref=source_hash_by_ref,
    )
    return validate_privacy_minimized_packet(
        minimized.model_copy(update={"assertions": assertions}),
        forbidden_raw_values=tuple(sources) + tuple(sources.values()),
    )


def _weekly_assertion_layer(
    local_bundle: dict[str, Any],
    *,
    source_hash_by_ref: dict[str, str],
) -> tuple[PrivacyMinimizedAssertion, ...]:
    counts: Counter[tuple[str, str, str]] = Counter()
    hashes: defaultdict[tuple[str, str, str], set[str]] = defaultdict(set)
    for source_index, source in enumerate(local_bundle["sources"]):
        subject = _source_family(str(source.get("source_type") or ""))
        for fact_index, fact in enumerate(source.get("key_facts") or []):
            source_hash = source_hash_by_ref[f"source_{source_index}_fact_{fact_index}"]
            text = " ".join(str(value or "") for value in fact.values()).casefold()
            relationships = {
                (subject, "workstream", _classify_workstream(text)),
                (subject, "status", _classify_status(text)),
                (subject, "action_state", _classify_action_state(fact, text)),
            }
            owner_role = _classify_owner_role(str(fact.get("owner") or ""))
            if owner_role:
                relationships.add((subject, "owner_role", owner_role))
            for relationship in relationships:
                counts[relationship] += 1
                hashes[relationship].add(source_hash)
    return tuple(
        PrivacyMinimizedAssertion(
            subject=subject,
            predicate=predicate,
            object=object_value,
            count=count,
            source_hashes=tuple(sorted(hashes[(subject, predicate, object_value)])),
        )
        for (subject, predicate, object_value), count in sorted(counts.items())
    )


def _source_family(source_type: str) -> str:
    lowered = source_type.casefold()
    if "slack" in lowered:
        return "slack_workstream"
    if "gmail" in lowered:
        return "gmail_follow_up"
    if "completed" in lowered or "agent_run" in lowered:
        return "completed_run"
    if "non_recurring" in lowered:
        return "one_time_calendar"
    if "recurring" in lowered:
        return "recurring_calendar"
    return "weekly_context"


def _classify_workstream(text: str) -> str:
    for label, terms in _WORKSTREAM_TERMS:
        if any(term in text for term in terms):
            return label
    return "general_operations"


def _classify_status(text: str) -> str:
    if any(term in text for term in ("blocked", "failed", "missing", "incomplete")):
        return "blocked_or_partial"
    if any(term in text for term in ("completed", "finished", "passed", "done")):
        return "completed"
    if any(term in text for term in ("pending", "waiting", "review", "follow-up")):
        return "pending_review"
    return "observed"


def _classify_action_state(fact: dict[str, Any], text: str) -> str:
    if fact.get("next_action") or fact.get("carry_forward"):
        return "follow_up_required"
    if any(term in text for term in ("follow-up", "follow up", "needs reply")):
        return "follow_up_required"
    if any(term in text for term in ("approved", "approval", "review required")):
        return "approval_or_review"
    return "no_action_recorded"


def _classify_owner_role(owner: str) -> str:
    lowered = owner.casefold().strip()
    if not lowered:
        return ""
    for label, terms in _OWNER_ROLE_TERMS:
        if any(term in lowered for term in terms):
            return label
    return "assigned_other"


def _sanitize_external_text(value: Any, redaction_terms: Sequence[str]) -> str:
    text = " ".join(str(value or "").split())
    text = re.sub(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[redacted]",
        text,
        flags=re.I,
    )
    text = re.sub(r"https?://\S+", "[redacted]", text, flags=re.I)
    for term in sorted(
        {item.strip() for item in redaction_terms if item.strip()},
        key=len,
        reverse=True,
    ):
        text = re.sub(re.escape(term), "[redacted person]", text, flags=re.I)
    return text


def _source(
    *,
    source_id: str,
    title: str,
    source_type: str,
    provider: str,
    facts: list[dict[str, str]],
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "title": title,
        "source_type": source_type,
        "provider": provider,
        "extraction_status": "bounded_summary",
        "supported_claim": f"{len(facts)} bounded item(s) are available for synthesis.",
        "key_facts": facts,
    }


def _fact(summary: str, **metadata: str) -> dict[str, str]:
    fact = {"summary": " ".join(str(summary or "").split())}
    fact.update(
        {
            key: " ".join(str(value or "").split())
            for key, value in metadata.items()
            if str(value or "").strip()
        }
    )
    return fact


def _recurring_calendar_groups(events: list[Any]) -> list[dict[str, str]]:
    counts = Counter((event.recurring_event_id, event.title) for event in events)
    earliest: dict[tuple[str, str], str] = {}
    for event in events:
        key = (event.recurring_event_id, event.title)
        if not earliest.get(key) or event.start < earliest[key]:
            earliest[key] = event.start
    return [
        _fact(
            f"{title}: {count} instance(s)",
            identity=series_id,
            first_start=earliest[(series_id, title)],
        )
        for (series_id, title), count in sorted(
            counts.items(), key=lambda item: (earliest[item[0]], item[0][1].lower())
        )
    ]
