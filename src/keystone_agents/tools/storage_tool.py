"""Storage tool wrapper around the SQLite audit store."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from keystone_agents.guardrails import (
    enforce_tool_input_guardrails,
    enforce_tool_output_guardrails,
    keystone_tool_guardrail_kwargs,
)
from keystone_agents.schemas.approval import ApprovalQueueItem, ApprovalQueueStatus
from keystone_agents.schemas.outreach import (
    OutreachChannel,
    build_initial_outreach_tracking_record,
)
from keystone_agents.sdk import function_tool
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env


@dataclass
class StorageTool:
    """Safe local storage boundary.

    SQLite is local-only and explicitly invoked by CLI `--save` flags or tests.
    No live provider calls are made here.
    """

    database_url: str | None = None
    agent_name: str | None = None
    run_id: str | int | None = None
    dry_run: bool = True
    audit_events: bool = True
    _store: SQLiteStore | None = field(default=None, init=False, repr=False)

    @property
    def store(self) -> SQLiteStore:
        if self._store is None:
            self._store = SQLiteStore(self.database_url or database_url_from_env())
        return self._store

    def _guard_input(self, action: str, payload: Any) -> None:
        enforce_tool_input_guardrails(f"storage_{action}", payload)

    def _guard_output(self, action: str, output: dict[str, Any]) -> dict[str, Any]:
        return enforce_tool_output_guardrails(f"storage_{action}", output)

    def _record_tool_event(
        self,
        action: str,
        *,
        payload: Any,
        output: Any | None = None,
        status: str = "success",
        error: str | None = None,
        agent_name: str | None = None,
        run_id: str | int | None = None,
        dry_run: bool | None = None,
    ) -> int | None:
        if not self.audit_events:
            return None
        return self.store.save_tool_event(
            tool_name=f"storage_{action}",
            agent_name=agent_name if agent_name is not None else self.agent_name,
            run_id=run_id if run_id is not None else self.run_id,
            input_payload=payload,
            output=output,
            dry_run=self.dry_run if dry_run is None else dry_run,
            status=status,
            error=error,
        )

    def _audited_mutation(
        self,
        action: str,
        payload: Any,
        callback: Callable[[], dict[str, Any]],
        *,
        agent_name: str | None = None,
        run_id: str | int | None = None,
        dry_run: bool | None = None,
    ) -> dict[str, Any]:
        try:
            self._guard_input(action, payload)
            output = self._guard_output(action, callback())
        except Exception as exc:
            try:
                self._record_tool_event(
                    action,
                    payload=payload,
                    output={"error_type": type(exc).__name__},
                    status="error",
                    error=str(exc),
                    agent_name=agent_name,
                    run_id=run_id,
                    dry_run=dry_run,
                )
            except Exception:
                pass
            raise
        self._record_tool_event(
            action,
            payload=payload,
            output=output,
            status="success",
            agent_name=agent_name,
            run_id=run_id,
            dry_run=dry_run,
        )
        return output

    def init_db(self) -> dict[str, Any]:
        self.store.initialize()
        output = {
            "status": "initialized",
            "path": self.store.path,
            "schema_version": self.store.current_schema_version(),
            "migrations": self.store.migration_versions(),
            "tables": sorted(self.store.table_names()),
        }
        self._record_tool_event("init_db", payload={}, output=output)
        return output

    def save_agent_run(self, **kwargs: Any) -> dict[str, Any]:
        event_agent_name = str(kwargs.get("agent_name") or self.agent_name or "")
        event_dry_run = bool(kwargs.get("dry_run", self.dry_run))
        try:
            self._guard_input("save_agent_run", kwargs)
            row_id = self.store.save_agent_run(**kwargs)
            output = self._guard_output(
                "save_agent_run",
                {"status": "saved", "table": "agent_runs", "id": row_id},
            )
        except Exception as exc:
            try:
                self._record_tool_event(
                    "save_agent_run",
                    payload=kwargs,
                    output={"error_type": type(exc).__name__},
                    status="error",
                    error=str(exc),
                    agent_name=event_agent_name,
                    dry_run=event_dry_run,
                )
            except Exception:
                pass
            raise
        self._record_tool_event(
            "save_agent_run",
            payload=kwargs,
            output=output,
            agent_name=event_agent_name,
            run_id=self.run_id if self.run_id is not None else row_id,
            dry_run=event_dry_run,
        )
        return output

    def save_email(self, triage: Any, **kwargs: Any) -> dict[str, Any]:
        payload = {"triage": triage, **kwargs}
        return self._audited_mutation(
            "save_email",
            payload,
            lambda: {
                "status": "saved",
                "table": "emails",
                "id": self.store.save_email(triage, **kwargs),
            },
        )

    def save_contact(self, contact: Any) -> dict[str, Any]:
        return self._audited_mutation(
            "save_contact",
            contact,
            lambda: {
                "status": "saved",
                "table": "contacts",
                "id": self.store.save_contact(contact),
            },
        )

    def list_contacts(
        self,
        *,
        company_name: str | None = None,
        approved_only: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            contact.model_dump(mode="json")
            for contact in self.store.list_contacts(
                company_name=company_name,
                approved_only=approved_only,
            )
        ]

    def save_crm_account_context(self, context: Any) -> dict[str, Any]:
        return self._audited_mutation(
            "save_crm_account_context",
            context,
            lambda: {
                "status": "saved",
                "table": "crm_contexts",
                "id": self.store.save_crm_account_context(context),
            },
        )

    def list_crm_account_contexts(
        self,
        *,
        company_name: str | None = None,
        approved_only: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            context.model_dump(mode="json")
            for context in self.store.list_crm_account_contexts(
                company_name=company_name,
                approved_only=approved_only,
            )
        ]

    def save_email_style_profile(self, profile: Any) -> dict[str, Any]:
        return self._audited_mutation(
            "save_email_style_profile",
            profile,
            lambda: {
                "status": "saved",
                "table": "email_style_profiles",
                "id": self.store.save_email_style_profile(profile),
            },
        )

    def list_email_style_profiles(
        self,
        *,
        profile_id: str | None = None,
        approved_only: bool = False,
    ) -> list[dict[str, Any]]:
        return [
            profile.model_dump(mode="json")
            for profile in self.store.list_email_style_profiles(
                profile_id=profile_id,
                approved_only=approved_only,
            )
        ]

    def save_memory_item(self, item: Any) -> dict[str, Any]:
        return self._audited_mutation(
            "save_memory_item",
            item,
            lambda: {
                "status": "saved",
                "table": "memory_items",
                "id": self.store.save_memory_item(item),
            },
        )

    def save_work_item(self, work_item: Any) -> dict[str, Any]:
        return self._audited_mutation(
            "save_work_item",
            work_item,
            lambda: {
                "status": "saved",
                "table": "work_items",
                "id": self.store.save_work_item(work_item),
            },
        )

    def save_work_item_event(self, work_item_id: str, event: Any) -> dict[str, Any]:
        payload = {"work_item_id": work_item_id, "event": event}
        return self._audited_mutation(
            "save_work_item_event",
            payload,
            lambda: {
                "status": "saved",
                "table": "work_item_events",
                "id": self.store.save_work_item_event(work_item_id, event),
            },
        )

    def save_work_item_artifact(self, work_item_id: str, artifact: Any) -> dict[str, Any]:
        payload = {"work_item_id": work_item_id, "artifact": artifact}
        return self._audited_mutation(
            "save_work_item_artifact",
            payload,
            lambda: {
                "status": "saved",
                "table": "work_item_artifacts",
                "id": self.store.save_work_item_artifact(work_item_id, artifact),
            },
        )

    def list_work_items(
        self,
        *,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self.store.list_work_items(status=status, kind=kind, limit=limit)
        ]

    def list_memory_items(
        self,
        *,
        memory_type: str | None = None,
        object_type: str | None = None,
        object_key: str | None = None,
        approved_only: bool = False,
        safe_for_prompt: bool | None = None,
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self.store.list_memory_items(
                memory_type=memory_type,
                object_type=object_type,
                object_key=object_key,
                approved_only=approved_only,
                safe_for_prompt=safe_for_prompt,
            )
        ]

    def retrieve_memory(
        self,
        query: str = "",
        *,
        object_key: str | None = None,
        memory_types: list[str] | None = None,
        limit: int = 5,
        approved_only: bool = True,
        safe_for_prompt: bool = True,
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self.store.retrieve_memory(
                query=query,
                object_key=object_key,
                memory_types=memory_types,
                limit=limit,
                approved_only=approved_only,
                safe_for_prompt=safe_for_prompt,
            )
        ]

    def save_outreach_example_document(self, document: Any) -> dict[str, Any]:
        return self._audited_mutation(
            "save_outreach_example_document",
            document,
            lambda: {
                "status": "saved",
                "table": "outreach_examples",
                "id": self.store.save_outreach_example_document(document),
            },
        )

    def list_outreach_example_documents(
        self,
        *,
        approved_only: bool = False,
        company_type: str | None = None,
        opportunity_type: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return [
            document.model_dump(mode="json")
            for document in self.store.list_outreach_example_documents(
                approved_only=approved_only,
                company_type=company_type,
                opportunity_type=opportunity_type,
                limit=limit,
            )
        ]

    def retrieve_outreach_examples(
        self,
        query: str = "",
        *,
        company_type: str | None = None,
        opportunity_type: str | None = None,
        limit: int = 5,
    ) -> dict[str, Any]:
        return self.store.retrieve_outreach_examples(
            query=query,
            company_type=company_type,
            opportunity_type=opportunity_type,
            limit=limit,
            approved_only=True,
        ).model_dump(mode="json")

    def save_follow_up_schedule(self, schedule: Any) -> dict[str, Any]:
        return self._audited_mutation(
            "save_follow_up_schedule",
            schedule,
            lambda: {
                "status": "saved",
                "table": "follow_up_schedules",
                "id": self.store.save_follow_up_schedule(schedule),
            },
        )

    def list_follow_up_schedules(
        self,
        *,
        company_name: str | None = None,
        related_draft_id: str | int | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            schedule.model_dump(mode="json")
            for schedule in self.store.list_follow_up_schedules(
                company_name=company_name,
                related_draft_id=related_draft_id,
                status=status,
            )
        ]

    def save_outreach_tracking(self, tracking: Any) -> dict[str, Any]:
        return self._audited_mutation(
            "save_outreach_tracking",
            tracking,
            lambda: {
                "status": "saved",
                "table": "outreach_tracking",
                "id": self.store.save_outreach_tracking(tracking),
            },
        )

    def save_initial_outreach_tracking(
        self,
        *,
        draft_id: str | int,
        draft: Any,
        channel: OutreachChannel = "email",
    ) -> dict[str, Any]:
        record = build_initial_outreach_tracking_record(
            draft_id=draft_id,
            draft=draft,
            channel=channel,
        )
        return self._audited_mutation(
            "save_initial_outreach_tracking",
            record,
            lambda: {
                "status": "saved",
                "table": "outreach_tracking",
                "id": self.store.save_outreach_tracking(record),
            },
        )

    def list_outreach_tracking(
        self,
        *,
        draft_id: str | int | None = None,
        company_name: str | None = None,
        lifecycle_status: str | None = None,
        outcome: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        return [
            record.model_dump(mode="json")
            for record in self.store.list_outreach_tracking(
                draft_id=draft_id,
                company_name=company_name,
                lifecycle_status=lifecycle_status,
                outcome=outcome,
                limit=limit,
            )
        ]

    def save_company(self, profile: Any) -> dict[str, Any]:
        return self._audited_mutation(
            "save_company",
            profile,
            lambda: {
                "status": "saved",
                "table": "companies",
                "id": self.store.save_company(profile),
            },
        )

    def save_opportunity(self, opportunity: Any, *, status: str = "candidate") -> dict[str, Any]:
        payload = {"opportunity": opportunity, "status": status}
        return self._audited_mutation(
            "save_opportunity",
            payload,
            lambda: {
                "status": "saved",
                "table": "opportunities",
                "id": self.store.save_opportunity(opportunity, status=status),
            },
        )

    def save_opportunity_scout_result(
        self,
        result: Any,
        *,
        status: str = "candidate",
    ) -> dict[str, Any]:
        payload = {"result": result, "status": status}
        return self._audited_mutation(
            "save_opportunity_scout_result",
            payload,
            lambda: {
                "status": "saved",
                "table": "opportunities",
                "ids": self.store.save_opportunity_scout_result(result, status=status),
            },
        )

    def save_orchestrator_result(self, result: Any, **kwargs: Any) -> dict[str, Any]:
        payload = {"result": result, **kwargs}
        return self._audited_mutation(
            "save_orchestrator_result",
            payload,
            lambda: {
                "status": "saved",
                "table": "agent_runs",
                "id": self.store.save_orchestrator_result(result, **kwargs),
            },
        )

    def save_outreach_draft(self, draft: Any) -> dict[str, Any]:
        return self._audited_mutation(
            "save_outreach_draft",
            draft,
            lambda: {
                "status": "saved",
                "table": "outreach_drafts",
                "id": self.store.save_outreach_draft(draft),
            },
        )

    def save_approval(
        self,
        *,
        object_type: str,
        object_id: str | int,
        decision: str | None = None,
        scope: str = "send",
        reviewer: str = "",
        timestamp: str | None = None,
        notes: str = "",
        previous_state: str | None = None,
        approval_status: str | None = None,
        risk_flags: list[str] | None = None,
        source_agent: str = "",
    ) -> dict[str, Any]:
        payload = {
            "object_type": object_type,
            "object_id": object_id,
            "decision": decision,
            "scope": scope,
            "reviewer": reviewer,
            "timestamp": timestamp,
            "notes": notes,
            "previous_state": previous_state,
            "approval_status": approval_status,
            "risk_flags": risk_flags,
            "source_agent": source_agent,
        }
        return self._audited_mutation(
            "save_approval",
            payload,
            lambda: {
                "status": "saved",
                "table": "approvals",
                "id": self.store.save_approval(
                    object_type=object_type,
                    object_id=object_id,
                    decision=decision,
                    scope=scope,
                    reviewer=reviewer,
                    timestamp=timestamp,
                    notes=notes,
                    previous_state=previous_state,
                    approval_status=approval_status,
                    risk_flags=risk_flags,
                    source_agent=source_agent,
                ),
            },
        )

    def save_approval_item(self, item: ApprovalQueueItem | dict[str, Any]) -> dict[str, Any]:
        return self._audited_mutation(
            "save_approval_item",
            item,
            lambda: {
                "status": "saved",
                "table": "approval_queue",
                "id": self.store.save_approval_item(item),
            },
        )

    def get_pending_approvals(
        self,
        status: ApprovalQueueStatus | str | None = ApprovalQueueStatus.PENDING,
    ) -> list[dict[str, Any]]:
        return [item.model_dump(mode="json") for item in self.store.get_pending_approvals(status)]

    def list_approval_items(
        self,
        status: ApprovalQueueStatus | str | None = ApprovalQueueStatus.PENDING,
        *,
        object_type: str | None = None,
        source_agent: str | None = None,
    ) -> list[dict[str, Any]]:
        return [
            item.model_dump(mode="json")
            for item in self.store.list_approval_items(
                status=status,
                object_type=object_type,
                source_agent=source_agent,
            )
        ]

    def get_approval_item(self, approval_id: str) -> dict[str, Any] | None:
        item = self.store.get_approval_item(approval_id)
        if item is None:
            return None
        return item.model_dump(mode="json")

    def update_approval_status(
        self,
        approval_id: str,
        status: ApprovalQueueStatus | str,
        *,
        reviewer: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "approval_id": approval_id,
            "status": status,
            "reviewer": reviewer,
            "notes": notes,
        }
        return self._audited_mutation(
            "update_approval_status",
            payload,
            lambda: self.store.update_approval_status(
                approval_id,
                status,
                reviewer=reviewer,
                notes=notes,
            ).model_dump(mode="json"),
        )

    def archive_approval_item(
        self,
        approval_id: str,
        *,
        reviewer: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        return self.update_approval_status(
            approval_id,
            ApprovalQueueStatus.ARCHIVED,
            reviewer=reviewer,
            notes=notes,
        )

    def save_source(
        self,
        *,
        object_type: str,
        object_id: int,
        title: str,
        url: str,
        snippet: str = "",
    ) -> dict[str, Any]:
        payload = {
            "object_type": object_type,
            "object_id": object_id,
            "title": title,
            "url": url,
            "snippet": snippet,
        }
        return self._audited_mutation(
            "save_source",
            payload,
            lambda: {
                "status": "saved",
                "table": "sources",
                "id": self.store.save_source(
                    object_type=object_type,
                    object_id=object_id,
                    title=title,
                    url=url,
                    snippet=snippet,
                ),
            },
        )

    def save(self, record: dict[str, Any]) -> dict[str, Any]:
        return self._audited_mutation(
            "save",
            record,
            lambda: {
                "status": "saved",
                "table": "agent_runs",
                "id": self.store.save("record", record),
            },
        )

    def save_tool_event(self, **kwargs: Any) -> dict[str, Any]:
        self._guard_input("save_tool_event", kwargs)
        row_id = self.store.save_tool_event(**kwargs)
        return self._guard_output(
            "save_tool_event",
            {"status": "saved", "table": "tool_events", "id": row_id},
        )

    def list_tool_events(
        self,
        *,
        tool_name: str | None = None,
        agent_name: str | None = None,
        run_id: str | int | None = None,
        status: str | None = None,
        dry_run: bool | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list_tool_events(
            tool_name=tool_name,
            agent_name=agent_name,
            run_id=run_id,
            status=status,
            dry_run=dry_run,
        )

    def save_agent_run_log(self, **kwargs: Any) -> dict[str, Any]:
        self._guard_input("save_agent_run_log", kwargs)
        row_id = self.store.save_agent_run_log(**kwargs)
        return self._guard_output(
            "save_agent_run_log",
            {"status": "saved", "table": "agent_run_logs", "id": row_id},
        )

    def list_agent_run_logs(
        self,
        *,
        run_id: str | int | None = None,
        step_name: str | None = None,
        agent_name: str | None = None,
        status: str | None = None,
        dry_run: bool | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list_agent_run_logs(
            run_id=run_id,
            step_name=step_name,
            agent_name=agent_name,
            status=status,
            dry_run=dry_run,
        )

    def list_records(self, table: str = "agent_runs") -> list[dict[str, Any]]:
        return self.store.fetch_all(table)

    def list_records_by_created_date_et(
        self,
        table: str,
        date_et: str,
    ) -> list[dict[str, Any]]:
        return self.store.fetch_by_created_date_et(table, date_et)


def _bounded_max_results(max_results: int, *, default: int = 5, upper: int = 10) -> int:
    try:
        resolved = int(max_results)
    except (TypeError, ValueError):
        resolved = default
    return max(0, min(resolved, upper))


def _require_lookup_company(company_name: str) -> str:
    resolved = str(company_name or "").strip()
    if not resolved:
        raise ValueError("company_name is required for approved context lookup.")
    return resolved


def _json_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def _json_mapping(value: str | dict[str, Any], *, field_name: str) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        loaded = json.loads(str(value or "{}"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be a JSON object") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return loaded


@function_tool(**keystone_tool_guardrail_kwargs())
def load_approved_contact_context(
    company_name: str,
    max_results: int = 5,
    database_url: str | None = None,
) -> str:
    """Load approved local contact context from SQLite without live CRM calls."""

    resolved_company = _require_lookup_company(company_name)
    rows = StorageTool(database_url=database_url, agent_name="context_loader").list_contacts(
        company_name=resolved_company,
        approved_only=True,
    )
    return _json_payload(
        {
            "mode": "local_storage",
            "object_type": "contact_context",
            "company_name": resolved_company,
            "approved_only": True,
            "send_enabled": False,
            "contacts": rows[: _bounded_max_results(max_results)],
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def load_approved_crm_context(
    company_name: str,
    max_results: int = 5,
    database_url: str | None = None,
) -> str:
    """Load approved local CRM/account context from SQLite without live CRM calls."""

    resolved_company = _require_lookup_company(company_name)
    rows = StorageTool(
        database_url=database_url,
        agent_name="context_loader",
    ).list_crm_account_contexts(company_name=resolved_company, approved_only=True)
    return _json_payload(
        {
            "mode": "local_storage",
            "object_type": "crm_account_context",
            "company_name": resolved_company,
            "approved_only": True,
            "send_enabled": False,
            "contexts": rows[: _bounded_max_results(max_results)],
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def save_initial_outreach_tracking_record(
    draft_id: str,
    draft_json: str,
    channel: OutreachChannel = "email",
    approval_reference: str = "",
    database_url: str | None = None,
) -> str:
    """Save a local manual-only outreach lifecycle row for an approved draft.

    This does not send email, schedule follow-ups, or mark outreach as agent-sent.
    """

    draft = _json_mapping(draft_json, field_name="draft_json")
    result = StorageTool(
        database_url=database_url,
        agent_name="outreach_tracking",
    ).save_initial_outreach_tracking(
        draft_id=draft_id,
        draft=draft,
        channel=channel,
    )
    return _json_payload(
        {
            **result,
            "object_type": "outreach_tracking",
            "approval_reference": str(approval_reference or "").strip(),
            "send_enabled": False,
            "sent_by_agent": False,
            "manual_update_only": True,
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def list_outreach_tracking_records(
    draft_id: str | None = None,
    company_name: str | None = None,
    lifecycle_status: str | None = None,
    outcome: str | None = None,
    max_results: int = 10,
    database_url: str | None = None,
) -> str:
    """List local outreach lifecycle rows so future replies can be matched safely."""

    rows = StorageTool(
        database_url=database_url,
        agent_name="outreach_tracking",
    ).list_outreach_tracking(
        draft_id=draft_id,
        company_name=company_name,
        lifecycle_status=lifecycle_status,
        outcome=outcome,
        limit=_bounded_max_results(max_results, default=10, upper=25),
    )
    return _json_payload(
        {
            "mode": "local_storage",
            "object_type": "outreach_tracking",
            "send_enabled": False,
            "sent_by_agent": False,
            "manual_update_only": True,
            "records": rows,
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def load_pending_approval_items(
    status: str | None = "pending",
    object_type: str | None = None,
    source_agent: str | None = None,
    max_results: int = 10,
    database_url: str | None = None,
) -> str:
    """Load local approval queue items for routing context without sending anything."""

    rows = StorageTool(
        database_url=database_url,
        agent_name="approval_context_loader",
    ).list_approval_items(status=status, object_type=object_type, source_agent=source_agent)
    return _json_payload(
        {
            "mode": "local_storage",
            "object_type": "approval_queue",
            "status": status,
            "filter_object_type": object_type,
            "filter_source_agent": source_agent,
            "send_enabled": False,
            "items": rows[: _bounded_max_results(max_results, default=10, upper=25)],
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def load_approved_outreach_examples(
    query: str,
    company_type: str | None = None,
    opportunity_type: str | None = None,
    max_results: int = 5,
    database_url: str | None = None,
) -> str:
    """Retrieve approved sanitized outreach examples from local SQLite only."""

    result = StorageTool(
        database_url=database_url,
        agent_name="outreach_example_loader",
    ).retrieve_outreach_examples(
        query=query,
        company_type=company_type,
        opportunity_type=opportunity_type,
        limit=_bounded_max_results(max_results),
    )
    return _json_payload(
        {
            "mode": "local_sqlite_fts",
            "object_type": "outreach_examples",
            "approved_only": True,
            "raw_body_included": False,
            "send_enabled": False,
            **result,
        }
    )
