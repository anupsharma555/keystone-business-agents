from __future__ import annotations

from pathlib import Path

from keystone_agents.agents.chief_of_staff import (
    build_chief_of_staff_agent,
    plan_chief_of_staff_request,
)
from keystone_agents.automation_inventory import (
    build_automation_inventory_report,
    ensure_default_automation_inventory,
)
from keystone_agents.natural_interaction import resolve_natural_followup
from keystone_agents.schemas.automation import AutomationRun, AutomationRunStatus
from keystone_agents.slack_action_contract import (
    KBA_COS_AUDIT_AUTOMATIONS,
    KBA_COS_GENERATE_DOC,
    KBA_INTENT_AUDIT_AUTOMATIONS,
    business_agent_action_value,
)
from keystone_agents.slack_interactions import handle_slack_approval_interaction
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.operations_publisher_tool import (
    publish_document_report_impl,
    publish_internal_artifact_impl,
    publish_table_mirror_impl,
)


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'ops.db'}"


def test_automation_inventory_storage_and_report(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    ensure_default_automation_inventory(store)
    store.save_automation_run(
        AutomationRun(
            automation_id="auto_weekly_opportunity",
            automation_name="Weekly Opportunity Scan",
            stage="dry-run",
            status=AutomationRunStatus.DRY_RUN,
            approval_count=2,
            next_safe_action="Review queued approvals.",
        )
    )

    report = build_automation_inventory_report(database_url=_database_url(tmp_path))

    assert any(spec.id == "auto_weekly_opportunity" for spec in report.automation_specs)
    assert any(
        spec.id == "auto_chief_of_staff_weekly_meeting_prep" for spec in report.automation_specs
    )
    assert any(
        spec.id == "auto_announcements_weekly_research_synthesis"
        for spec in report.automation_specs
    )
    assert any(binding.channel_name == "meetings" for binding in report.channel_bindings)
    assert any(
        binding.channel_name == "announcements" and binding.purpose == "research_synthesis"
        for binding in report.channel_bindings
    )
    assert report.recent_runs[0].automation_id == "auto_weekly_opportunity"
    assert report.pending_approval_count == 0
    assert "SQLite" in " ".join(report.audit_notes)


def test_chief_of_staff_builder_exposes_operating_tools() -> None:
    agent = build_chief_of_staff_agent()
    tool_names = {getattr(tool, "name", "") for tool in agent.tools}

    assert "summarize_automation_health" in tool_names
    assert "publish_document_report" in tool_names
    assert "publish_table_mirror" in tool_names
    assert "publish_slack_summary" in tool_names


def test_chief_of_staff_automation_audit_plans_internal_writes(tmp_path: Path) -> None:
    result = plan_chief_of_staff_request(
        "audit current automations and organize findings into a Google Doc and Airtable",
        database_url=_database_url(tmp_path),
    )

    assert result.automation_report is not None
    assert result.send_enabled is False
    assert result.slack_post_allowed is False
    destinations = {request.destination.value for request in result.write_requests}
    assert "google_doc" in destinations
    assert "airtable" in destinations


def test_publishers_are_dry_run_and_keep_sqlite_canonical(tmp_path: Path) -> None:
    report = build_automation_inventory_report(database_url=_database_url(tmp_path))

    doc = publish_document_report_impl(
        report.model_dump_json(),
        destination="google_doc",
        live=False,
    )
    table = publish_table_mirror_impl(
        report.model_dump_json(),
        destination="airtable",
        live=False,
    )

    assert doc["status"] == "dry-run"
    assert doc["artifact"]["url"].startswith("dry-run://google-doc/")
    assert doc["external_sharing_enabled"] is False
    assert table["canonical_state"] == "sqlite"
    assert table["artifact"]["provider"] == "airtable"


def test_internal_artifact_publisher_mirrors_company_and_contact_refs() -> None:
    result = publish_internal_artifact_impl(
        '{"company_name":"Mentavi","contact_name":"Needs source-backed confirmation"}',
        artifact_type="contact_candidates",
        title="Mentavi contact candidates",
        destinations=["airtable", "google_doc"],
        live=False,
    )

    assert result["status"] == "dry-run"
    assert result["canonical_state"] == "sqlite"
    refs = result["artifact_refs"]
    assert len(refs) == 2
    assert {ref["provider"] for ref in refs} == {"airtable", "google_doc"}
    assert all(ref["artifact_type"] == "contact_candidates" for ref in refs)


def test_natural_followup_resolves_doc_and_failure_intents(tmp_path: Path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    ensure_default_automation_inventory(store)
    store.save_automation_run(
        AutomationRun(
            automation_id="auto_gmail_triage",
            automation_name="Gmail Triage",
            stage="label-preview",
            status=AutomationRunStatus.FAILED,
            failure_summary="missing credentials",
        )
    )

    doc = resolve_natural_followup(
        "make this a doc",
        database_url=_database_url(tmp_path),
        current_report_id="airep_123",
    )
    failed = resolve_natural_followup("what failed?", database_url=_database_url(tmp_path))

    assert doc.intent == "publish_document_report"
    assert doc.target_id == "airep_123"
    assert failed.intent == "show_automation_failures"
    assert failed.metadata["failed_run_count"] == 1


def test_slack_chief_of_staff_actions_return_dry_run_report(tmp_path: Path) -> None:
    value = business_agent_action_value(intent=KBA_INTENT_AUDIT_AUTOMATIONS)
    payload = {
        "type": "block_actions",
        "user": {"id": "U123", "username": "anup"},
        "channel": {"id": "C123", "name": "ai-agents-workflow"},
        "container": {"message_ts": "1715366400.000100"},
        "actions": [{"action_id": KBA_COS_AUDIT_AUTOMATIONS, "value": value}],
    }

    result = handle_slack_approval_interaction(payload, database_url=_database_url(tmp_path))

    assert result.stage == "chief_of_staff"
    assert result.outcome == "automation_audit_ready"
    assert result.read_only_payload is not None
    assert "automation_report" in result.read_only_payload


def test_slack_chief_of_staff_generate_doc_action_is_dry_run(tmp_path: Path) -> None:
    value = business_agent_action_value(intent="generate_doc")
    payload = {
        "type": "block_actions",
        "user": {"id": "U123", "username": "anup"},
        "channel": {"id": "C123", "name": "ai-agents-workflow"},
        "container": {"message_ts": "1715366400.000100"},
        "actions": [{"action_id": KBA_COS_GENERATE_DOC, "value": value}],
    }

    result = handle_slack_approval_interaction(payload, database_url=_database_url(tmp_path))

    assert result.outcome == "document_report_ready"
    assert result.read_only_payload is not None
    publish_result = result.read_only_payload["publish_result"]
    assert publish_result["artifact"]["url"].startswith("dry-run://google-doc/")
