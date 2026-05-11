from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.update_outreach_tracking as update_outreach_tracking_cli
from keystone_agents.agents.opportunity_scout import scout_opportunities_fixture
from keystone_agents.company_research import research_company_fixture
from keystone_agents.interfaces.table_mirror import (
    AirtableMirrorProvider,
    DryRunTableMirrorProvider,
    GoogleSheetsMirrorProvider,
    LocalTableMirrorCRMProvider,
    build_crm_provider,
    build_table_mirror_provider,
)
from keystone_agents.schemas.approval import ApprovalQueueItem
from keystone_agents.schemas.contact_context import ContactRecord, CRMAccountContext
from keystone_agents.schemas.crm import CRMProviderName, CRMWriteOperation, CRMWriteStatus
from keystone_agents.schemas.table_mirror import (
    TableMirrorExportResult,
    TableMirrorObjectType,
    TableMirrorProviderName,
)
from keystone_agents.storage.sqlite_store import SQLiteStore

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures"


def _approved_contact() -> ContactRecord:
    return ContactRecord(
        company_name="Curebase",
        contact_name="Dr. Priya Shah",
        role_title="Clinical Operations Lead",
        source="fixture",
        source_id="fixture:contact_curebase_priya",
        source_url="fixture://sample_contact_curebase_approved.json",
        confidence=0.85,
        approval_state="approved_for_drafting",
        notes="Approved local fixture contact.",
    )


def _pending_contact() -> ContactRecord:
    return ContactRecord(
        company_name="Curebase",
        contact_name="Unreviewed Prospect",
        role_title="Operations",
        source="fixture",
        source_id="fixture:contact_curebase_pending",
        source_url="fixture://sample_contact_curebase_pending.json",
        confidence=0.4,
        approval_state="pending",
        notes="Pending local fixture contact.",
    )


def _approved_crm_context() -> CRMAccountContext:
    return CRMAccountContext(
        company_name="Curebase",
        account_stage="research",
        account_owner="Keystone",
        last_touchpoint="No outbound contact made.",
        next_step="Review draft-only outreach.",
        source="fixture",
        source_id="fixture:crm_context_curebase",
        source_url="fixture://sample_crm_context_curebase.json",
        confidence=0.75,
        approval_state="approved_for_drafting",
        notes="Approved local CRM context.",
    )


def test_dry_run_export_works_for_opportunities() -> None:
    records = scout_opportunities_fixture(max_results=2).records
    result = DryRunTableMirrorProvider().export_records("opportunities", records)

    assert result.provider == TableMirrorProviderName.DRY_RUN
    assert result.object_type == TableMirrorObjectType.OPPORTUNITIES
    assert result.table_name == "Opportunities"
    assert result.dry_run is True
    assert len(result.records) == 2
    assert result.records[0].fields["Company Name"]
    assert result.records[0].fields["Source URLs"]
    assert result.field_to_column["company_name"] == "Company Name"
    assert any("no live provider APIs" in note for note in result.audit_notes)


def test_dry_run_export_supports_all_object_types() -> None:
    from scripts.export_pipeline_table import fixture_records_for_object_type

    provider = DryRunTableMirrorProvider()

    for object_type in TableMirrorObjectType:
        records = fixture_records_for_object_type(object_type)
        result = provider.export_records(object_type, records)

        assert result.object_type == object_type
        assert result.columns
        assert result.records


def test_unsupported_provider_fails_clearly() -> None:
    with pytest.raises(ValueError, match="Unsupported table mirror provider"):
        build_table_mirror_provider("not-a-provider")


def test_live_providers_do_not_run_without_explicit_flag() -> None:
    with pytest.raises(RuntimeError, match="explicit live=True"):
        AirtableMirrorProvider().export_records("opportunities", [])

    with pytest.raises(RuntimeError, match="explicit live=True"):
        GoogleSheetsMirrorProvider().export_records("opportunities", [])


def test_live_providers_require_credentials_after_explicit_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AIRTABLE_API_KEY",
        "AIRTABLE_BASE_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_SHEETS_SPREADSHEET_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(RuntimeError, match="AIRTABLE_API_KEY and AIRTABLE_BASE_ID"):
        AirtableMirrorProvider(live=True).export_records("opportunities", [])

    with pytest.raises(
        RuntimeError,
        match="GOOGLE_APPLICATION_CREDENTIALS and GOOGLE_SHEETS_SPREADSHEET_ID",
    ):
        GoogleSheetsMirrorProvider(live=True).export_records("opportunities", [])


def test_local_crm_provider_lists_leads_and_fetches_account_context() -> None:
    provider = build_crm_provider(
        leads=[_approved_contact(), _pending_contact()],
        account_contexts=[_approved_crm_context()],
    )

    leads = provider.list_leads(company_name="Curebase")
    approved_leads = provider.list_leads(company_name="Curebase", approved_only=True)
    context = provider.fetch_account_context("Curebase")

    assert provider.name == CRMProviderName.LOCAL_TABLE_MIRROR
    assert provider.dry_run is True
    assert [item.value for item in CRMProviderName] == ["local-table-mirror"]
    assert [lead.id for lead in leads] == [
        "fixture:contact_curebase_priya",
        "fixture:contact_curebase_pending",
    ]
    assert [lead.id for lead in approved_leads] == ["fixture:contact_curebase_priya"]
    assert context.company_name == "Curebase"
    assert [contact.contact_name for contact in context.contacts] == ["Dr. Priya Shah"]
    assert context.crm_context is not None
    assert context.crm_context.source_url == "fixture://sample_crm_context_curebase.json"


def test_local_crm_write_operations_are_dry_run_and_approval_aware() -> None:
    provider = LocalTableMirrorCRMProvider(leads=[_approved_contact()])

    blocked = provider.update_status("fixture:contact_curebase_priya", "qualified")
    approved = provider.update_status(
        "fixture:contact_curebase_priya",
        "qualified",
        approval_state="approved_for_drafting",
        reviewer="operator",
        note="Human reviewed status change.",
    )
    attached = provider.attach_report_link_or_note(
        "fixture:contact_curebase_priya",
        report_link="local://reviewed-report/curebase",
        note="Attach reviewed research brief.",
        approval_state="approved_for_drafting",
        reviewer="operator",
    )

    assert blocked.operation == CRMWriteOperation.UPDATE_STATUS
    assert blocked.status == CRMWriteStatus.BLOCKED_PENDING_APPROVAL
    assert blocked.approval_required is True
    assert blocked.dry_run is True
    assert blocked.table_preview is None
    assert approved.status == CRMWriteStatus.DRY_RUN_READY
    assert approved.approval_required is False
    assert approved.table_preview is not None
    assert approved.table_preview.dry_run is True
    assert approved.table_preview.records[0].fields["CRM Status"] == "qualified"
    assert "Human reviewed" in str(approved.table_preview.records[0].fields["Notes"])
    assert provider.list_leads()[0].status == "approved_for_drafting"
    assert attached.operation == CRMWriteOperation.ATTACH_REPORT_LINK_OR_NOTE
    assert attached.table_preview is not None
    assert "local://reviewed-report/curebase" in str(
        attached.table_preview.records[0].fields["Notes"]
    )
    assert any("no live CRM API" in note for note in attached.audit_notes)


def test_local_crm_provider_rejects_live_or_unsupported_providers() -> None:
    with pytest.raises(ValueError, match="Unsupported CRM provider"):
        build_crm_provider("hubspot")

    with pytest.raises(RuntimeError, match="Live CRM providers are not implemented"):
        build_crm_provider(dry_run=False)

    with pytest.raises(RuntimeError, match="Live CRM integrations are not implemented"):
        LocalTableMirrorCRMProvider(
            leads=[_approved_contact()],
            dry_run=False,
        ).update_status(
            "fixture:contact_curebase_priya",
            "qualified",
            approval_state="approved_for_drafting",
        )


def test_schema_serializes() -> None:
    records = scout_opportunities_fixture(max_results=1).records
    result = DryRunTableMirrorProvider().export_records("opportunities", records)

    encoded = result.model_dump_json()
    decoded = TableMirrorExportResult.model_validate_json(encoded)

    assert decoded.provider == TableMirrorProviderName.DRY_RUN
    assert decoded.records[0].fields["Company Name"] == result.records[0].fields["Company Name"]
    assert json.loads(encoded)["columns"][0]["column_name"] == "Company Name"


def test_cli_json_export_outputs_column_mapping() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/export_pipeline_table.py",
            "--provider",
            "dry-run",
            "--object-type",
            "companies",
            "--json",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["provider"] == "dry-run"
    assert payload["object_type"] == "companies"
    assert payload["columns"][0] == {
        "source_field": "name",
        "column_name": "Company Name",
        "description": "",
    }
    assert payload["records"][0]["fields"]["Company Name"] == "Curebase"


def test_draft_table_mirror_omits_full_email_body() -> None:
    result = DryRunTableMirrorProvider().export_records(
        "drafts",
        [
            {
                "company_name": "Curebase",
                "contact_name": "Dr. Example",
                "email_subject": "Clinical AI workflow discussion",
                "email_body": "Full body with token=SHOULD_NOT_APPEAR_111111111.",
                "approval_state": "pending",
            }
        ],
    )
    encoded = result.model_dump_json()

    assert result.records[0].fields["Email Body Summary"].startswith("[omitted:")
    assert "sha256=" in result.records[0].fields["Email Body Summary"]
    assert "Outreach Sent" in result.records[0].fields
    assert "Reply Received" in result.records[0].fields
    assert "Outcome" in result.records[0].fields
    assert "Full body with token" not in encoded
    assert "SHOULD_NOT_APPEAR" not in encoded


def test_outreach_tracking_table_mirror_exports_lifecycle_fields() -> None:
    result = DryRunTableMirrorProvider().export_records(
        "outreach_tracking",
        [
            {
                "draft_id": "draft-1",
                "company_name": "Curebase",
                "contact_name": "Dr. Example",
                "channel": "email",
                "lifecycle_status": "reply_received",
                "outreach_sent": True,
                "reply_received": True,
                "reply_summary": "Positive reply asking for times.",
                "outcome": "meeting_booked",
                "next_step": "Prepare call notes.",
                "manual_update_only": True,
                "send_enabled": False,
                "sent_by_agent": False,
            }
        ],
    )

    fields = result.records[0].fields
    assert result.table_name == "Outreach Tracking"
    assert fields["Draft ID"] == "draft-1"
    assert fields["Lifecycle Status"] == "reply_received"
    assert fields["Outreach Sent"] is True
    assert fields["Reply Received"] is True
    assert fields["Outcome"] == "meeting_booked"
    assert fields["Send Enabled"] is False


def test_cli_dashboard_export_reads_sqlite_and_omits_sensitive_content(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'dashboard.db'}"
    store = SQLiteStore(database_url)
    store.save_company(
        research_company_fixture(
            company_name="Curebase",
            fixture_json=FIXTURE_ROOT / "sample_company_curebase.json",
        )
    )
    store.save_opportunity(scout_opportunities_fixture(max_results=1).records[0])
    store.save_outreach_draft(
        {
            "company_name": "Curebase",
            "contact_name": "Dr. Example",
            "email_subject": "Clinical AI workflow discussion",
            "email_body": "Draft body with secret=SHOULD_NOT_APPEAR_222222222.",
            "personalization_rationale": "Uses approved fixture context.",
            "approval_state": "pending",
        }
    )
    store.save_outreach_tracking(
        {
            "draft_id": "1",
            "company_name": "Curebase",
            "contact_name": "Dr. Example",
            "lifecycle_status": "sent_manually",
            "outreach_sent": True,
            "sent_at": "2026-05-01T14:00:00Z",
            "reply_received": False,
            "outcome": "pending_reply",
            "next_step": "Wait for reply.",
        }
    )
    store.save_approval_item(
        ApprovalQueueItem(
            id="approval-cli-dashboard",
            object_type="outreach_draft",
            object_id="draft-1",
            title="Draft review",
            summary="Review draft before use.",
            draft_text="Approval text with token=SHOULD_NOT_APPEAR_333333333.",
            source_agent="outreach_composer",
        )
    )
    store.save_agent_run(
        agent_name="outreach_composer",
        input_summary="dashboard review",
        output={"email_body": "Agent body with token=SHOULD_NOT_APPEAR_444444444."},
    )

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/export_pipeline_table.py",
            "--dashboard",
            "--database-url",
            database_url,
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "# Keystone Local Operator Dashboard" in completed.stdout
    assert "Approval Queue" in completed.stdout
    assert "Opportunities" in completed.stdout
    assert "Company Profiles" in completed.stdout
    assert "Outreach Drafts" in completed.stdout
    assert "Outreach Tracking" in completed.stdout
    assert "Recent Agent Runs" in completed.stdout
    assert "| Approval Queue | 1 |" in completed.stdout
    assert "| Opportunities | 1 |" in completed.stdout
    assert "| Company Profiles | 1 |" in completed.stdout
    assert "| Outreach Drafts | 1 |" in completed.stdout
    assert "| Outreach Tracking | 1 |" in completed.stdout
    assert "| Recent Agent Runs | 1 |" in completed.stdout
    assert "Curebase" in completed.stdout
    assert "SHOULD_NOT_APPEAR" not in completed.stdout
    assert "Approval text with token" not in completed.stdout


def test_cli_dashboard_empty_database_output_is_readable(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'empty-dashboard.db'}"
    SQLiteStore(database_url)

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/export_pipeline_table.py",
            "--dashboard",
            "--database-url",
            database_url,
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "# Keystone Local Operator Dashboard" in completed.stdout
    assert "| Approval Queue | 0 |" in completed.stdout
    assert "| Opportunities | 0 |" in completed.stdout
    assert "| Company Profiles | 0 |" in completed.stdout
    assert "| Outreach Drafts | 0 |" in completed.stdout
    assert "| Outreach Tracking | 0 |" in completed.stdout
    assert "| Recent Agent Runs | 0 |" in completed.stdout
    assert "| none |" in completed.stdout


def test_cli_dashboard_decision_is_read_only_and_future_scoped() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/export_pipeline_table.py",
            "--dashboard-decision",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "# Keystone Operator Dashboard Decision" in completed.stdout
    assert "read-only by default" in completed.stdout
    assert "outreach tracking" in completed.stdout
    assert "recent agent runs" in completed.stdout
    assert "Read-only API" in completed.stdout
    assert "No live writes" in completed.stdout
    assert "No auto-send" in completed.stdout
    assert "No secrets in repo" in completed.stdout


def test_cli_sqlite_table_export_outputs_markdown_table(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'table.db'}"
    store = SQLiteStore(database_url)
    store.save_approval_item(
        ApprovalQueueItem(
            id="approval-table",
            object_type="outreach_draft",
            object_id="draft-1",
            title="Draft review",
            summary="Review draft before use.",
            draft_text="Approval body with secret=SHOULD_NOT_APPEAR_444444444.",
            source_agent="outreach_composer",
        )
    )

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/export_pipeline_table.py",
            "--provider",
            "dry-run",
            "--object-type",
            "approvals",
            "--source",
            "sqlite",
            "--database-url",
            database_url,
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "| Record ID | Object Type | Object ID | Summary |" in completed.stdout
    assert "approval-table" in completed.stdout
    assert "SHOULD_NOT_APPEAR" not in completed.stdout


def test_cli_sqlite_draft_export_includes_latest_tracking_fields(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'draft-tracking.db'}"
    store = SQLiteStore(database_url)
    draft_id = store.save_outreach_draft(
        {
            "company_name": "Curebase",
            "contact_name": "Dr. Example",
            "email_subject": "Clinical AI workflow discussion",
            "email_body": "Draft body requiring review.",
            "personalization_rationale": "Uses approved fixture context.",
            "approval_state": "pending",
        }
    )
    store.save_outreach_tracking(
        {
            "draft_id": str(draft_id),
            "company_name": "Curebase",
            "contact_name": "Dr. Example",
            "lifecycle_status": "reply_received",
            "reply_received": True,
            "reply_received_at": "2026-05-02T14:00:00Z",
            "reply_summary": "Positive reply asking for availability.",
            "outcome": "meeting_booked",
            "next_step": "Prepare call notes.",
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/export_pipeline_table.py",
            "--provider",
            "dry-run",
            "--object-type",
            "drafts",
            "--source",
            "sqlite",
            "--database-url",
            database_url,
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Lifecycle Status" in completed.stdout
    assert "reply_received" in completed.stdout
    assert "Reply Received" in completed.stdout
    assert "meeting_booked" in completed.stdout
    assert "Prepare call notes." in completed.stdout


def test_cli_sqlite_outreach_tracking_export_outputs_markdown_table(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'tracking.db'}"
    store = SQLiteStore(database_url)
    store.save_outreach_tracking(
        {
            "draft_id": "draft-1",
            "company_name": "Curebase",
            "contact_name": "Dr. Example",
            "lifecycle_status": "sent_manually",
            "outreach_sent": True,
            "outcome": "pending_reply",
            "next_step": "Wait for reply.",
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/export_pipeline_table.py",
            "--provider",
            "dry-run",
            "--object-type",
            "outreach_tracking",
            "--source",
            "sqlite",
            "--database-url",
            database_url,
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "| Record ID | Draft ID | Company Name |" in completed.stdout
    assert "draft-1" in completed.stdout
    assert "sent_manually" in completed.stdout
    assert "pending_reply" in completed.stdout


def test_update_outreach_tracking_cli_appends_manual_snapshot(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_url = f"sqlite:///{tmp_path / 'manual-tracking.db'}"

    assert (
        update_outreach_tracking_cli.main(
            [
                "--draft-id",
                "draft-1",
                "--company-name",
                "Curebase",
                "--contact-name",
                "Dr. Example",
                "--reply-received",
                "--reply-received-at",
                "2026-05-02T14:00:00Z",
                "--outcome",
                "meeting_booked",
                "--next-step",
                "Prepare call notes.",
                "--database-url",
                database_url,
                "--json",
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    store = SQLiteStore(database_url)
    records = store.list_outreach_tracking(draft_id="draft-1")

    assert payload["lifecycle_status"] == "reply_received"
    assert payload["outreach_sent"] is True
    assert records[0].reply_received is True
    assert records[0].sent_by_agent is False
    assert records[0].send_enabled is False


def test_cli_sqlite_contacts_export_reads_local_contacts_without_sensitive_notes(
    tmp_path: Path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'contacts.db'}"
    store = SQLiteStore(database_url)
    store.save_contact(
        {
            "company": "Curebase",
            "name": "Dr. Local Contact",
            "title": "Clinical Operations Lead",
            "profile_url": "fixture://profile/local-contact",
            "source": "fixture",
            "confidence": 0.8,
            "approval_status": "approved_for_drafting",
            "notes": (
                "Patient Jane Doe was diagnosed with depression. token=SHOULD_NOT_APPEAR_555555555."
            ),
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            "scripts/export_pipeline_table.py",
            "--provider",
            "dry-run",
            "--object-type",
            "contacts",
            "--source",
            "sqlite",
            "--database-url",
            database_url,
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert "| Record ID | Contact Name | Contact Email |" in completed.stdout
    assert "Dr. Local Contact" in completed.stdout
    assert "Clinical Operations Lead" in completed.stdout
    assert "fixture://profile/local-contact" in completed.stdout
    assert "Patient Jane Doe" not in completed.stdout
    assert "SHOULD_NOT_APPEAR" not in completed.stdout
