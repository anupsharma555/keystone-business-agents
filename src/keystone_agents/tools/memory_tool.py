"""Local memory and workflow dedup tools."""

from __future__ import annotations

import json
from typing import Any

from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.memory import (
    WorkflowDedupAction,
    approval_decision_memory_item,
    build_chief_of_staff_memory_context,
    build_email_style_profile_from_feedback,
    build_workflow_dedup_memory,
    company_profile_memory_items,
    email_style_memory_item,
    feedback_memory_item,
    opportunity_entity_memory_items,
    opportunity_memory_items,
    outreach_dedup_memory_items,
    retrieval_tool_performance_memory_item,
    workflow_dedup_key,
)
from keystone_agents.memory import (
    check_workflow_duplicate as check_workflow_duplicate_impl,
)
from keystone_agents.outreach_examples import retrieve_outreach_examples_local
from keystone_agents.schemas.email_style import EmailStyleProfile
from keystone_agents.schemas.feedback import FeedbackRecord
from keystone_agents.schemas.outreach import OutreachDraft
from keystone_agents.sdk import function_tool
from keystone_agents.storage.sqlite_store import database_url_from_env
from keystone_agents.tools.storage_tool import StorageTool


def _json_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)


def _load_json(value: str | dict[str, Any] | list[Any]) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


@function_tool(**keystone_tool_guardrail_kwargs())
def retrieve_memory(
    query: str = "",
    object_key: str | None = None,
    memory_types: list[str] | None = None,
    limit: int = 5,
    approved_only: bool = True,
    database_url: str | None = None,
) -> str:
    """Retrieve approved, prompt-safe local memory without live API calls."""

    records = StorageTool(
        database_url=database_url or database_url_from_env(),
        agent_name="memory",
    ).retrieve_memory(
        query=query,
        object_key=object_key,
        memory_types=memory_types,
        limit=limit,
        approved_only=approved_only,
        safe_for_prompt=True,
    )
    return _json_payload(
        {
            "mode": "local_memory",
            "query": query,
            "object_key": object_key or "",
            "approved_only": approved_only,
            "send_enabled": False,
            "records": records,
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def retrieve_chief_of_staff_memory(
    query: str = "",
    object_key: str | None = None,
    route: str = "",
    limit: int = 8,
    database_url: str | None = None,
) -> str:
    """Retrieve approved prompt-safe Chief of Staff strategic memory."""

    context = build_chief_of_staff_memory_context(
        query=query,
        object_key=object_key,
        route=route,
        limit=limit,
        database_url=database_url or database_url_from_env(),
    )
    return _json_payload(
        {
            "mode": "chief_of_staff_memory",
            **context.model_dump(mode="json"),
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def retrieve_outreach_examples(
    outreach_goal: str = "",
    company_type: str = "",
    opportunity_type: str = "",
    selected_template: str = "",
    outreach_stage: str = "",
    limit: int = 3,
    database_url: str | None = None,
) -> str:
    """Retrieve 1-3 approved sanitized outreach examples from local SQLite."""

    result = retrieve_outreach_examples_local(
        outreach_goal=outreach_goal,
        company_type=company_type,
        opportunity_type=opportunity_type,
        selected_template=selected_template,
        outreach_stage=outreach_stage,
        limit=limit,
        database_url=database_url or database_url_from_env(),
    )
    return _json_payload(result.model_dump(mode="json"))


@function_tool(**keystone_tool_guardrail_kwargs())
def save_company_profile_memory(
    profile_json: str,
    approval_state: str = "approved_for_research",
    database_url: str | None = None,
) -> str:
    """Save source-backed company profile memory and a research dedup marker."""

    items = company_profile_memory_items(
        _load_json(profile_json),
        approval_state=approval_state,
    )
    storage = StorageTool(database_url=database_url or database_url_from_env(), agent_name="memory")
    ids = [storage.save_memory_item(item.model_dump(mode="json"))["id"] for item in items]
    return _json_payload(
        {
            "mode": "local_memory",
            "object_type": "company_profile",
            "saved_ids": ids,
            "send_enabled": False,
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def save_opportunity_memory(
    opportunity_json: str,
    approval_state: str = "approved_for_research",
    database_url: str | None = None,
) -> str:
    """Save source-backed opportunity memory and an opportunity dedup marker."""

    items = opportunity_memory_items(
        _load_json(opportunity_json),
        approval_state=approval_state,
    )
    storage = StorageTool(database_url=database_url or database_url_from_env(), agent_name="memory")
    ids = [storage.save_memory_item(item.model_dump(mode="json"))["id"] for item in items]
    return _json_payload(
        {
            "mode": "local_memory",
            "object_type": "opportunity",
            "saved_ids": ids,
            "send_enabled": False,
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def save_entity_memory(
    entity_json: str,
    approval_state: str = "approved_for_research",
    database_url: str | None = None,
) -> str:
    """Save bounded source-backed memory for generalized opportunity entities."""

    items = opportunity_entity_memory_items(
        _load_json(entity_json),
        approval_state=approval_state,
    )
    storage = StorageTool(database_url=database_url or database_url_from_env(), agent_name="memory")
    ids = [storage.save_memory_item(item.model_dump(mode="json"))["id"] for item in items]
    return _json_payload(
        {
            "mode": "local_memory",
            "object_type": "opportunity_entity",
            "saved_ids": ids,
            "send_enabled": False,
            "raw_body_included": False,
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def save_retrieval_tool_performance_memory(
    retrieval_metadata_json: str,
    object_id: str = "company_research",
    approval_state: str = "approved_for_research",
    database_url: str | None = None,
) -> str:
    """Save prompt-safe retrieval ladder performance memory for future tool selection."""

    item = retrieval_tool_performance_memory_item(
        _load_json(retrieval_metadata_json),
        object_id=object_id,
        approval_state=approval_state,
    )
    if item is None:
        return _json_payload(
            {
                "mode": "local_memory",
                "object_type": "retrieval_tool_performance",
                "saved_id": None,
                "send_enabled": False,
            }
        )
    output = StorageTool(
        database_url=database_url or database_url_from_env(),
        agent_name="memory",
    ).save_memory_item(item.model_dump(mode="json"))
    return _json_payload(
        {
            "mode": "local_memory",
            "object_type": "retrieval_tool_performance",
            "saved_id": output["id"],
            "send_enabled": False,
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def save_outreach_dedup_memory(
    draft_json: str,
    approval_state: str = "pending",
    database_url: str | None = None,
) -> str:
    """Save data-only dedup markers for outreach drafts without sending or scheduling."""

    items = outreach_dedup_memory_items(
        OutreachDraft.model_validate(_load_json(draft_json)),
        approval_state=approval_state,
    )
    storage = StorageTool(database_url=database_url or database_url_from_env(), agent_name="memory")
    ids = [storage.save_memory_item(item.model_dump(mode="json"))["id"] for item in items]
    return _json_payload(
        {
            "mode": "local_memory",
            "object_type": "outreach_draft",
            "saved_ids": ids,
            "send_enabled": False,
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def save_human_feedback_memory(
    feedback_json: str,
    approval_item_json: str = "{}",
    approval_state: str = "approved_for_drafting",
    database_url: str | None = None,
) -> str:
    """Save prompt-safe human feedback memory without raw sensitive bodies."""

    approval_item_payload = _load_json(approval_item_json) if approval_item_json else {}
    item = feedback_memory_item(
        _load_json(feedback_json),
        approval_item=approval_item_payload or None,
        approval_state=approval_state,
    )
    output = StorageTool(
        database_url=database_url or database_url_from_env(),
        agent_name="memory",
    ).save_memory_item(item.model_dump(mode="json"))
    return _json_payload(
        {
            "mode": "local_memory",
            "object_type": "feedback",
            "saved_id": output["id"],
            "send_enabled": False,
            "raw_body_included": False,
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def save_approval_decision_memory(
    decision_json: str,
    outcome_json: str = "{}",
    approval_item_json: str = "{}",
    approval_state: str = "approved_for_drafting",
    database_url: str | None = None,
) -> str:
    """Save approval decision and sanitized outcome memory without enabling sends."""

    approval_item_payload = _load_json(approval_item_json) if approval_item_json else {}
    outcome_payload = _load_json(outcome_json) if outcome_json else {}
    item = approval_decision_memory_item(
        _load_json(decision_json),
        outcome=outcome_payload or None,
        approval_item=approval_item_payload or None,
        approval_state=approval_state,
    )
    output = StorageTool(
        database_url=database_url or database_url_from_env(),
        agent_name="memory",
    ).save_memory_item(item.model_dump(mode="json"))
    return _json_payload(
        {
            "mode": "local_memory",
            "object_type": "approval",
            "saved_id": output["id"],
            "send_enabled": False,
            "raw_body_included": False,
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def learn_email_style_profile(
    profile_id: str,
    drafts_json: str,
    feedback_json: str = "[]",
    database_url: str | None = None,
) -> str:
    """Create an approved aggregate style profile from approved drafts and feedback."""

    drafts = [OutreachDraft.model_validate(item) for item in _load_json(drafts_json)]
    feedback = [FeedbackRecord.model_validate(item) for item in _load_json(feedback_json)]
    profile = build_email_style_profile_from_feedback(
        profile_id=profile_id,
        drafts=drafts,
        feedback_records=feedback,
    )
    storage = StorageTool(database_url=database_url or database_url_from_env(), agent_name="memory")
    profile_id_saved = storage.save_email_style_profile(profile.model_dump(mode="json"))["id"]
    memory_id = storage.save_memory_item(email_style_memory_item(profile).model_dump(mode="json"))[
        "id"
    ]
    return _json_payload(
        {
            "mode": "local_memory",
            "object_type": "email_style_profile",
            "profile_id": profile.profile_id,
            "style_profile_row_id": profile_id_saved,
            "memory_id": memory_id,
            "send_enabled": False,
            "raw_sent_email_bodies_included": False,
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def check_workflow_duplicate(
    action: WorkflowDedupAction,
    company_name: str,
    contact: str | None = None,
    subject: str | None = None,
    body_hash: str | None = None,
    database_url: str | None = None,
) -> str:
    """Check local memory for duplicate research, opportunity, outreach, or email work."""

    return _json_payload(
        check_workflow_duplicate_impl(
            action=action,
            company_name=company_name,
            contact=contact,
            subject=subject,
            body_hash=body_hash,
            database_url=database_url or database_url_from_env(),
        )
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def record_workflow_dedup(
    action: WorkflowDedupAction,
    company_name: str,
    contact: str | None = None,
    subject: str | None = None,
    body_hash: str | None = None,
    source_ids: list[str] | None = None,
    approval_state: str = "pending",
    rationale: str = "",
    database_url: str | None = None,
) -> str:
    """Record a local workflow dedup marker without sending or scheduling anything."""

    item = build_workflow_dedup_memory(
        action=action,
        company_name=company_name,
        contact=contact,
        subject=subject,
        body_hash=body_hash,
        source_ids=source_ids or [],
        approval_state=approval_state,
        rationale=rationale,
    )
    output = StorageTool(
        database_url=database_url or database_url_from_env(),
        agent_name="memory",
    ).save_memory_item(item.model_dump(mode="json"))
    return _json_payload(
        {
            "mode": "local_memory",
            "dedup_key": workflow_dedup_key(
                action=action,
                company_name=company_name,
                contact=contact,
                subject=subject,
                body_hash=body_hash,
            ),
            "saved_id": output["id"],
            "send_enabled": False,
        }
    )


def load_email_style_profile_from_memory(
    profile_id: str,
    *,
    database_url: str | None = None,
) -> EmailStyleProfile | None:
    """Load an approved aggregate style profile previously learned into storage."""

    rows = StorageTool(database_url or database_url_from_env()).list_email_style_profiles(
        profile_id=profile_id,
        approved_only=True,
    )
    if not rows:
        return None
    return EmailStyleProfile.model_validate(rows[0])
