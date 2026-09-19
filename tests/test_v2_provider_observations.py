from __future__ import annotations

import json
from functools import partial
from types import SimpleNamespace

import pytest

from keystone_agents.runtime.durable_execution import (
    ExecutionStore,
    UncertainOperation,
    activate_execution,
)
from keystone_agents.tools import (
    gmail_tool,
    google_calendar_tool,
    internal_data_tools,
    zotero_context_tools,
)


@pytest.fixture
def execution(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    execution_id = store.begin({"request": "One synthetic provider operation"})["id"]
    with activate_execution(store, execution_id):
        yield store, execution_id


def _observed(execution, key, identity):
    store, execution_id = execution
    rows = store.operations(execution_id)
    assert len(rows) == 1
    assert rows[0]["status"] == "observed"
    payload = json.loads(rows[0]["receipt_json"])
    assert payload[key] == identity
    assert payload.get("verification", {}).get("passed") is not True


def _reopen_blocks_replay(execution, operation):
    store, execution_id = execution
    with activate_execution(ExecutionStore(store.path), execution_id):
        with pytest.raises(UncertainOperation):
            operation()


@pytest.mark.parametrize("operation_name", ["create", "update", "delete"])
def test_calendar_identity_survives_readback_failure(execution, monkeypatch, operation_name):
    monkeypatch.setenv("KEYSTONE_GOOGLE_CALENDAR_ALLOW_WRITES", "true")
    effects = []

    class Calendar:
        event_id = "synthetic-event"

        def get_event(self, calendar_id, event_id):
            if effects:
                _observed(execution, "event_id", self.event_id)
                raise RuntimeError("synthetic Calendar readback failure")
            if operation_name == "create":
                return {"_not_found": True}
            return {
                "id": event_id,
                "summary": "Synthetic event",
                "status": "confirmed",
                "start": {"date": "2026-09-10"},
                "end": {"date": "2026-09-11"},
            }

        def create_event(self, calendar_id, event_id, payload):
            self.event_id = event_id
            effects.append("create")
            return {"id": event_id}

        def update_event(self, *args):
            effects.append("update")

        def delete_event(self, *args):
            effects.append("delete")

    client = Calendar()
    common = {
        "calendar_id": "synthetic-calendar",
        "approval_reference": "synthetic",
        "live": True,
        "tool": client,
    }
    if operation_name == "create":
        operation = partial(
            google_calendar_tool.create_google_calendar_event_impl,
            "Synthetic event",
            "2026-09-10",
            **common,
        )
    elif operation_name == "update":
        operation = partial(
            google_calendar_tool.update_google_calendar_event_impl,
            "synthetic-event",
            title="Updated synthetic event",
            **common,
        )
    else:
        operation = partial(
            google_calendar_tool.delete_google_calendar_event_impl, "synthetic-event", **common
        )
    with pytest.raises(RuntimeError, match="readback"):
        operation()
    _reopen_blocks_replay(execution, operation)
    assert effects == [operation_name]


@pytest.mark.parametrize("failure_stage", ["folder", "readback"])
def test_sheet_identity_survives_post_create_failure(execution, monkeypatch, failure_stage):
    effects = []

    def fail_at(stage):
        _observed(execution, "spreadsheet_id", "synthetic-sheet")
        if stage == failure_stage:
            raise RuntimeError(f"synthetic {stage} failure")
        return "synthetic-folder"

    class Sheets:
        def spreadsheets(self):
            return self

        def create(self, **kwargs):
            def execute():
                effects.append("create")
                return {"spreadsheetId": "synthetic-sheet"}

            return SimpleNamespace(execute=execute)

    monkeypatch.setattr(
        internal_data_tools, "_require_google_workspace_write_approval", lambda *_: None
    )
    monkeypatch.setattr(
        internal_data_tools,
        "_google_workspace_services",
        lambda: {"drive": object(), "sheets": Sheets()},
    )
    monkeypatch.setattr(internal_data_tools, "_assert_configured_google_account", lambda *_: None)
    monkeypatch.setattr(
        internal_data_tools, "_ensure_drive_folder_path", lambda *_: fail_at("folder")
    )
    monkeypatch.setattr(internal_data_tools, "_move_drive_file_to_folder", lambda *_: None)
    monkeypatch.setattr(
        internal_data_tools, "_verify_google_sheet_file", lambda *a, **k: fail_at("readback")
    )
    operation = partial(
        internal_data_tools.google_sheet_create_impl,
        "Synthetic sheet",
        approval_reference="synthetic",
        live=True,
    )
    with pytest.raises(RuntimeError, match=failure_stage):
        operation()
    _reopen_blocks_replay(execution, operation)
    assert effects == ["create"]


def test_https_airtable_attachment_records_identity_before_readback(execution, monkeypatch):
    for key, value in {
        "AIRTABLE_BASE_ID": "app_synthetic",
        "AIRTABLE_ACCESS_TOKEN": "synthetic-token",
        "AIRTABLE_ALLOWED_TABLES": "Business Expenses",
        "AIRTABLE_WRITE_DRY_RUN": "false",
        "AIRTABLE_ALLOW_WRITES": "true",
        "AIRTABLE_ALLOW_ATTACHMENT_UPLOADS": "true",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        internal_data_tools,
        "airtable_get_base_schema_impl",
        lambda **k: {
            "schema": {
                "tables": [
                    {
                        "name": "Business Expenses",
                        "fields": [
                            {
                                "name": "Attachments",
                                "field_type": "multipleAttachments",
                                "field_id": "fld_attachment",
                            },
                        ],
                    }
                ]
            },
        },
    )
    effects = []

    def read(*args, **kwargs):
        if effects:
            _observed(execution, "record_id", "rec_synthetic")
            raise RuntimeError("synthetic attachment readback failure")
        return {"records": [{"id": "rec_synthetic", "fields": {"Attachments": []}}]}

    def write(*args, **kwargs):
        effects.append("patch")
        return {"id": "rec_synthetic"}

    monkeypatch.setattr(internal_data_tools, "airtable_read_records_impl", read)
    monkeypatch.setattr(internal_data_tools, "_airtable_send", write)
    operation = partial(
        internal_data_tools.airtable_link_attachment_impl,
        "https://example.test/synthetic-receipt.pdf",
        table="Business Expenses",
        record_id="rec_synthetic",
        approval_reference="synthetic",
        live=True,
    )
    with pytest.raises(RuntimeError, match="readback"):
        operation()
    _reopen_blocks_replay(execution, operation)
    assert effects == ["patch"]


@pytest.mark.parametrize(
    "operation_name",
    ["create", "reply", "update", "create_attachment", "update_attachment", "delete"],
)
def test_gmail_draft_identity_survives_post_response_failure(
    execution, tmp_path, monkeypatch, operation_name
):
    client = gmail_tool.GmailTool(live=True)
    effects = []
    attachment = tmp_path / "synthetic.pdf"
    attachment.write_bytes(b"%PDF-1.4\nsynthetic")
    monkeypatch.setenv("KEYSTONE_PRESENTATION_DERIVED_ROOT", str(tmp_path))

    class Payload(dict):
        id_reads = 0

        def get(self, key, default=None):
            if key == "id" and self.id_reads == 0:
                self.id_reads += 1
                return "synthetic-draft"
            _observed(execution, "draft_id", "synthetic-draft")
            raise RuntimeError("synthetic Gmail response normalization failure")

    def request(method, endpoint, **kwargs):
        if method in {"POST", "PUT", "DELETE"}:
            effects.append(method)
            return {} if method == "DELETE" else Payload()
        _observed(execution, "draft_id", "synthetic-draft")
        raise RuntimeError("synthetic Gmail readback failure")

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(
        client,
        "get_message",
        lambda *_: {
            "from": "sender@example.test",
            "subject": "Synthetic discussion",
            "threadId": "synthetic-thread",
        },
    )
    content = ("recipient@example.test", "Synthetic draft", "Synthetic content")
    if operation_name == "create":
        operation = partial(client.create_draft, *content)
    elif operation_name == "reply":
        operation = partial(client.create_draft_reply, "synthetic-message", "Synthetic reply")
    elif operation_name == "update":
        operation = partial(client.update_draft, "synthetic-draft", *content)
    elif operation_name == "create_attachment":
        operation = partial(client.create_draft_with_attachment, *content, str(attachment))
    elif operation_name == "update_attachment":
        operation = partial(
            client.update_draft_with_attachment, "synthetic-draft", *content, str(attachment)
        )
    else:
        operation = partial(client.delete_draft, "synthetic-draft")
    with pytest.raises(RuntimeError, match="synthetic Gmail"):
        operation()
        client.draft_exists("synthetic-draft")
    _reopen_blocks_replay(execution, operation)
    assert len(effects) == 1


@pytest.mark.parametrize("operation_name", ["create", "update", "delete"])
def test_zotero_note_identity_survives_readback_failure(execution, monkeypatch, operation_name):
    monkeypatch.setenv("KEYSTONE_ZOTERO_ALLOW_TEST_NOTE_WRITES", "true")
    monkeypatch.setenv("ZOTERO_API_KEY", "synthetic-token")
    effects = []
    before = {
        "key": "SYNTH001",
        "version": 1,
        "data": {
            "key": "SYNTH001",
            "version": 1,
            "itemType": "note",
            "note": "KBA_TEST_NOTE original",
        },
    }

    def read_item(**kwargs):
        if effects:
            _observed(execution, "item_key", "SYNTH001")
            raise RuntimeError("synthetic Zotero readback failure")
        return before

    def request(method, path, **kwargs):
        if method in {"POST", "PATCH", "DELETE"}:
            effects.append(method)
            return 200, {"successful": {"0": {"key": "SYNTH001"}}}, {}
        return read_item()

    monkeypatch.setattr(zotero_context_tools, "_zotero_get_item", read_item)
    monkeypatch.setattr(zotero_context_tools, "_zotero_http_request", request)
    if operation_name == "delete":
        operation = partial(
            zotero_context_tools.zotero_delete_test_note_impl,
            "SYNTH001",
            library_id="123",
            approval_reference="synthetic",
            live=True,
        )
    else:
        operation = partial(
            zotero_context_tools.zotero_write_test_note_impl,
            "KBA_TEST_NOTE revised",
            item_key="SYNTH001" if operation_name == "update" else "",
            operation=operation_name,
            library_id="123",
            approval_reference="synthetic",
            live=True,
        )
    with pytest.raises(RuntimeError, match="readback"):
        operation()
    _reopen_blocks_replay(execution, operation)
    assert len(effects) == 1


def test_dry_run_methods_do_not_create_mutation_intents(execution):
    google_calendar_tool.create_google_calendar_event_impl(
        "Synthetic",
        "2026-09-10",
        approval_reference="synthetic",
        live=False,
    )
    gmail_tool.GmailTool(live=False).create_draft("recipient@example.test", "Synthetic", "Preview")
    zotero_context_tools.zotero_write_test_note_impl("KBA_TEST_NOTE preview", live=False)
    store, execution_id = execution
    assert store.operations(execution_id) == []


@pytest.mark.parametrize("resource", ["collection", "item"])
@pytest.mark.parametrize("operation_name", ["create", "update", "delete"])
def test_zotero_single_resource_identity_survives_readback_failure(
    execution,
    monkeypatch,
    resource,
    operation_name,
):
    monkeypatch.setenv("KEYSTONE_ZOTERO_ALLOW_TEST_LIBRARY_WRITES", "true")
    monkeypatch.setenv("ZOTERO_API_KEY", "synthetic-token")
    effects = []
    identity = "COLLS001" if resource == "collection" else "ITEMS001"
    identity_key = "collection_key" if resource == "collection" else "item_key"
    collection = {
        "key": "COLLS001",
        "version": 1,
        "data": {"key": "COLLS001", "version": 1, "name": "KBA_TEST_COLLECTION original"},
    }
    item = {
        "key": "ITEMS001",
        "version": 1,
        "data": {
            "key": "ITEMS001",
            "version": 1,
            "title": "KBA_TEST_ITEM original",
            "itemType": "webpage",
            "tags": [{"tag": "KBA_TEST_ITEM"}],
            "collections": ["COLLS001"],
        },
    }

    def readback():
        _observed(execution, identity_key, identity)
        raise RuntimeError("synthetic Zotero resource readback failure")

    def read_collection(**kwargs):
        if resource == "collection" and effects:
            return readback()
        return collection

    def read_item(**kwargs):
        return readback() if effects else item

    def request(method, path, **kwargs):
        if method in {"POST", "PATCH", "DELETE"}:
            effects.append(method)
            return 200, {"successful": {"0": {"key": identity}}}, {}
        if not effects and "/items?" in path:
            return 200, [], {}
        return readback()

    monkeypatch.setattr(zotero_context_tools, "_zotero_get_collection", read_collection)
    monkeypatch.setattr(zotero_context_tools, "_zotero_get_item", read_item)
    monkeypatch.setattr(zotero_context_tools, "_zotero_http_request", request)
    common = {"library_id": "123", "approval_reference": "synthetic", "live": True}
    if resource == "collection":
        if operation_name == "delete":
            operation = partial(
                zotero_context_tools.zotero_delete_test_collection_impl, identity, **common
            )
        else:
            operation = partial(
                zotero_context_tools.zotero_write_test_collection_impl,
                "KBA_TEST_COLLECTION revised",
                collection_key=identity if operation_name == "update" else "",
                operation=operation_name,
                **common,
            )
    elif operation_name == "delete":
        operation = partial(zotero_context_tools.zotero_delete_test_item_impl, identity, **common)
    else:
        operation = partial(
            zotero_context_tools.zotero_write_test_item_impl,
            "KBA_TEST_ITEM revised",
            item_key=identity if operation_name == "update" else "",
            collection_key="COLLS001",
            operation=operation_name,
            **common,
        )
    with pytest.raises(RuntimeError, match="readback"):
        operation()
    _reopen_blocks_replay(execution, operation)
    assert len(effects) == 1
