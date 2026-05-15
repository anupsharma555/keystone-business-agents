"""Deterministic natural follow-up resolution for Slack and CLI."""

from __future__ import annotations

from typing import Any

from keystone_agents.schemas.automation import NaturalInteractionResolution
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env


def resolve_natural_followup(
    text: str,
    *,
    database_url: str | None = None,
    current_work_item_id: str = "",
    current_report_id: str = "",
    active_automation_id: str = "",
    pending_approval_id: str = "",
    last_artifact_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> NaturalInteractionResolution:
    """Resolve common operator follow-ups against current Keystone state."""

    raw = str(text or "").strip()
    lowered = raw.lower()
    store = SQLiteStore(database_url or database_url_from_env())
    target_id = (
        current_report_id
        or last_artifact_id
        or active_automation_id
        or current_work_item_id
        or _latest_context_id(store)
    )
    if any(marker in lowered for marker in ("make this a doc", "google doc", "create doc")):
        target_type = (
            "automation_report" if current_report_id or last_artifact_id else "latest_context"
        )
        return NaturalInteractionResolution(
            input_text=raw,
            intent="publish_document_report",
            target_type=target_type,
            target_id=target_id,
            confidence=0.86 if target_id else 0.4,
            explanation="Resolved follow-up to an internal document report request.",
            requires_clarification=not bool(target_id),
            metadata=metadata or {},
        )
    if any(marker in lowered for marker in ("sync to airtable", "airtable", "table mirror")):
        target_type = (
            "automation_report" if current_report_id or last_artifact_id else "latest_context"
        )
        return NaturalInteractionResolution(
            input_text=raw,
            intent="publish_table_mirror",
            target_type=target_type,
            target_id=target_id,
            confidence=0.86 if target_id else 0.4,
            explanation="Resolved follow-up to an Airtable-shaped review mirror request.",
            requires_clarification=not bool(target_id),
            metadata=metadata or {},
        )
    if any(marker in lowered for marker in ("what failed", "failures", "failed runs")):
        failed_runs = store.list_automation_runs(status="failed", limit=10)
        return NaturalInteractionResolution(
            input_text=raw,
            intent="show_automation_failures",
            target_type="automation_run",
            target_id=failed_runs[0].id if failed_runs else "",
            confidence=0.9,
            explanation="Resolved follow-up to recent failed automation runs.",
            requires_clarification=False,
            metadata={"failed_run_count": len(failed_runs), **(metadata or {})},
        )
    if any(marker in lowered for marker in ("show blockers", "only blockers", "blocked")):
        return NaturalInteractionResolution(
            input_text=raw,
            intent="show_blockers",
            target_type="work_item_or_automation",
            target_id=target_id,
            confidence=0.82 if target_id else 0.45,
            explanation="Resolved follow-up to blockers for the current context.",
            requires_clarification=not bool(target_id),
            metadata=metadata or {},
        )
    if lowered in {"continue", "resume"} or "continue this" in lowered:
        return NaturalInteractionResolution(
            input_text=raw,
            intent="continue_work_item",
            target_type="work_item",
            target_id=current_work_item_id or _latest_work_item_id(store),
            confidence=0.9 if current_work_item_id else 0.6,
            explanation="Resolved follow-up to continue the current or latest WorkItem.",
            requires_clarification=not bool(current_work_item_id or _latest_work_item_id(store)),
            metadata=metadata or {},
        )
    if "post" in lowered and "summary" in lowered:
        return NaturalInteractionResolution(
            input_text=raw,
            intent="publish_slack_summary",
            target_type="automation_report",
            target_id=target_id,
            confidence=0.75 if target_id else 0.4,
            explanation="Resolved follow-up to an internal Slack summary request.",
            requires_clarification=not bool(target_id),
            metadata=metadata or {},
        )
    return NaturalInteractionResolution(
        input_text=raw,
        intent="clarification",
        target_type="",
        target_id="",
        confidence=0.2,
        explanation="No deterministic follow-up intent matched.",
        requires_clarification=True,
        metadata=metadata or {},
    )


def _latest_work_item_id(store: SQLiteStore) -> str:
    items = store.list_work_items(status="all", limit=1)
    return items[0].id if items else ""


def _latest_context_id(store: SQLiteStore) -> str:
    runs = store.list_automation_runs(limit=1)
    if runs:
        return runs[0].id
    return _latest_work_item_id(store)
