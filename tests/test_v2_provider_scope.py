from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from keystone_agents.receipts.journal import durable_provider_tool, instrument_agent_tools
from keystone_agents.runtime.durable_execution import (
    ExecutionConflict,
    ExecutionStore,
    activate_execution,
)


@pytest.fixture(autouse=True)
def isolated_context_config(monkeypatch):
    monkeypatch.delenv("KEYSTONE_CONTEXT_CONFIG_REPO", raising=False)
    monkeypatch.delenv("KEYSTONE_SLACK_REPO", raising=False)
    monkeypatch.delenv("KEYSTONE_CONTEXT_CONFIG_OVERRIDE", raising=False)
    monkeypatch.delenv("KEYSTONE_CONTEXT_CONFIG_OVERRIDE_KEYS", raising=False)


def verified_result(tool_name="airtable_write_record"):
    provider, operation, identity_key = {
        "google_sheet_create": ("google_workspace", "create_sheet", "spreadsheet_id"),
        "create_google_calendar_event": ("google_calendar", "create_event", "event_id"),
        "create_gmail_draft": ("gmail", "create_draft", "draft_id"),
        "zotero_write_test_note": ("zotero", "create", "item_key"),
    }.get(tool_name, ("airtable", "create_record", "record_id"))
    return {
        "status": "success",
        "provider": provider,
        "operation": operation,
        identity_key: "synthetic-record",
        "verification": {"passed": True},
    }


@pytest.mark.parametrize("lane", ["direct", "sdk"])
@pytest.mark.parametrize(
    ("tool_name", "config_key"),
    [
        ("airtable_write_record", "AIRTABLE_FINANCE_TAX_TRACKER_BASE_ID"),
        ("google_sheet_create", "GOOGLE_DRIVE_ACCOUNT"),
        ("create_google_calendar_event", "GOOGLE_CALENDAR_ID"),
        ("create_gmail_draft", "KEYSTONE_GMAIL_DRAFT_ACCOUNT"),
        ("zotero_write_test_note", "ZOTERO_LIBRARY_ID"),
    ],
)
def test_changed_default_scope_blocks_cached_replay_and_new_write_after_reopen(
    tmp_path, monkeypatch, lane, tool_name, config_key
):
    monkeypatch.setenv(config_key, "synthetic-original-scope")
    effects = []

    @durable_provider_tool(tool_name)
    def direct(value, *, live=True):
        effects.append(value)
        return verified_result(tool_name)

    async def callback(_context, serialized):
        effects.append(json.loads(serialized)["value"])
        return json.dumps(verified_result(tool_name))

    tool = SimpleNamespace(name=tool_name, on_invoke_tool=callback)
    instrument_agent_tools(SimpleNamespace(tools=[tool]))

    def invoke(value):
        if lane == "direct":
            return direct(value)
        return asyncio.run(tool.on_invoke_tool(None, json.dumps({"value": value})))

    store = ExecutionStore(tmp_path / "execution.sqlite3")
    execution_id = store.begin({"request": "One synthetic write"})["id"]
    with activate_execution(store, execution_id):
        invoke("first")
        invoke("first")
    assert effects == ["first"]
    monkeypatch.setenv(config_key, "synthetic-changed-scope")
    with activate_execution(ExecutionStore(store.path), execution_id):
        for value in ("first", "different-arguments"):
            with pytest.raises(ExecutionConflict, match="scope changed"):
                invoke(value)
    assert effects == ["first"]
    assert len(store.operations(execution_id)) == 1
    database_bytes = store.path.read_bytes()
    assert b"synthetic-original-scope" not in database_bytes
    assert b"synthetic-changed-scope" not in database_bytes


@pytest.mark.parametrize("nested_sdk", [False, True])
@pytest.mark.parametrize("field", ["api_base_url", "expected_account", "token_file"])
def test_injected_owner_scope_change_blocks_mutation_before_provider(tmp_path, nested_sdk, field):
    effects = []

    class Provider:
        live = True

        def __init__(self, scope):
            setattr(self, field, scope)

        @durable_provider_tool("create_gmail_draft")
        def create(self, value):
            effects.append(value)
            return verified_result()

    owner = Provider("https://first.example.test/api")

    async def callback(_context, serialized):
        return json.dumps(owner.create(json.loads(serialized)["value"]))

    tool = SimpleNamespace(name="create_gmail_draft", on_invoke_tool=callback)
    instrument_agent_tools(SimpleNamespace(tools=[tool]))

    def invoke(value):
        if nested_sdk:
            return asyncio.run(tool.on_invoke_tool(None, json.dumps({"value": value})))
        return owner.create(value)

    store = ExecutionStore(tmp_path / "execution.sqlite3")
    execution_id = store.begin({"request": "One synthetic draft"})["id"]
    with activate_execution(store, execution_id):
        invoke("first")
    owner = Provider("https://changed.example.test/api")
    with activate_execution(ExecutionStore(store.path), execution_id):
        with pytest.raises(ExecutionConflict, match="scope changed"):
            invoke("changed-arguments")
    assert effects == ["first"]
    assert b"first.example.test" not in store.path.read_bytes()


def test_linked_configuration_resolution_is_guarded_without_reading_env_files(
    tmp_path, monkeypatch
):
    from keystone_agents import context_env

    linked = {"AIRTABLE_BASE_ID": "synthetic-linked-base"}
    monkeypatch.setattr(
        context_env, "context_env_value", lambda key, default="": linked.get(key, default)
    )
    effects = []

    @durable_provider_tool("airtable_write_record")
    def write(value, *, live=True):
        effects.append(value)
        return verified_result()

    store = ExecutionStore(tmp_path / "execution.sqlite3")
    execution_id = store.begin({"request": "One synthetic linked-config write"})["id"]
    with activate_execution(store, execution_id):
        write("first")
    linked["AIRTABLE_BASE_ID"] = "synthetic-changed-linked-base"
    with activate_execution(ExecutionStore(store.path), execution_id):
        with pytest.raises(ExecutionConflict, match="scope changed"):
            write("changed-arguments")
    assert effects == ["first"]


def test_scope_guard_does_not_read_credentials_or_arbitrary_environment(tmp_path, monkeypatch):
    from keystone_agents import context_env

    looked_up = []

    def resolve(key, default=""):
        looked_up.append(key)
        assert "TOKEN" not in key and "ACCESS" not in key and "SECRET" not in key
        return default

    monkeypatch.setattr(context_env, "context_env_value", resolve)
    effects = []

    @durable_provider_tool("airtable_write_record")
    def write(value, *, live=True):
        effects.append(value)
        return verified_result()

    store = ExecutionStore(tmp_path / "execution.sqlite3")
    execution_id = store.begin({"request": "One synthetic guarded write"})["id"]
    with activate_execution(store, execution_id):
        write("first")
        monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "synthetic-rotated-credential")
        monkeypatch.setenv("UNRELATED_SETTING", "synthetic-change")
        write("first")
    assert effects == ["first"]
    assert "AIRTABLE_BASE_ID" in looked_up
    assert b"synthetic-rotated-credential" not in store.path.read_bytes()


def test_legacy_operation_without_scope_requires_reconciliation(tmp_path):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    execution_id = store.begin({"request": "One legacy synthetic write"})["id"]
    store.before_operation(execution_id, "airtable_write_record", {"value": "first"})
    store.observe_operation(
        execution_id, "airtable_write_record", {"value": "first"}, verified_result()
    )
    effects = []

    @durable_provider_tool("airtable_write_record")
    def write(value, *, live=True):
        effects.append(value)
        return verified_result()

    with activate_execution(ExecutionStore(store.path), execution_id):
        with pytest.raises(ExecutionConflict, match="no recorded provider scope"):
            write("changed-arguments")
    assert effects == []
    assert len(store.operations(execution_id)) == 1


def test_scope_change_requires_new_execution_not_new_operation_key(tmp_path, monkeypatch):
    effects = []

    @durable_provider_tool("zotero_write_test_note")
    def write(value, *, live=True):
        effects.append(value)
        return verified_result()

    store = ExecutionStore(tmp_path / "execution.sqlite3")
    for scope in ("synthetic-library-one", "synthetic-library-two"):
        monkeypatch.setenv("ZOTERO_LIBRARY_ID", scope)
        execution_id = store.begin({"request": "An independent approved write"})["id"]
        with activate_execution(store, execution_id):
            write(scope)
    assert effects == ["synthetic-library-one", "synthetic-library-two"]
