"""SDK fixture previews must not become uncertain real provider effects."""

from __future__ import annotations

import asyncio
import json
from copy import copy
from types import SimpleNamespace

import pytest
from agents.tool_context import ToolContext

from keystone_agents.receipts.journal import (
    durable_provider_tool,
    instrument_agent_tools,
    record_provider_observation,
    reset_tool_receipt_journal,
)
from keystone_agents.runtime.durable_execution import (
    ExecutionStore,
    UncertainOperation,
    activate_execution,
)
from keystone_agents.tools import internal_data_tools as airtable


@pytest.fixture(autouse=True)
def _isolated_receipt_journal():
    reset_tool_receipt_journal()
    yield
    reset_tool_receipt_journal()


def _begin(store, identity):
    store.begin({"request": "Synthetic preview test"}, execution_id=identity)
    store.update(identity, status="running", work_item_id="wi_synthetic")
    return identity


def _verified():
    return {"status": "success", "provider": "airtable", "operation": "create_record",
            "record_id": "rec_synthetic", "provider_write": True,
            "verification": {"passed": True}}


def _instrument(callback, name="airtable_write_record"):
    tool = SimpleNamespace(name=name, on_invoke_tool=callback)
    instrument_agent_tools(SimpleNamespace(tools=[tool]))
    return tool


@pytest.mark.parametrize("live", [False, True])
def test_actual_sdk_airtable_preview_does_not_block_later_work_item_execution(
    tmp_path, monkeypatch, live,
):
    monkeypatch.setattr(airtable, "_airtable_base_config", lambda **_: {
        "base_id": "app_synthetic", "base_alias": "", "access_token": "",
        "default_table": "Synthetic table", "allowed_tables": (),
    })
    monkeypatch.setattr(
        airtable, "_airtable_send", lambda *_, **__: pytest.fail("No API permitted"),
    )
    monkeypatch.delenv("AIRTABLE_ALLOWED_OPERATION", raising=False)
    monkeypatch.setenv("AIRTABLE_WRITE_DRY_RUN", "true")
    tool = copy(airtable.airtable_write_record.sdk_tool)
    instrument_agent_tools(SimpleNamespace(tools=[tool]))
    store = ExecutionStore(tmp_path / "execution.db")
    first = _begin(store, "preview")
    arguments = json.dumps({
        "fields_json": '{"Name":"Synthetic"}', "table": "Synthetic table", "live": live,
    })
    context = ToolContext(context=None, tool_name=tool.name,
                          tool_call_id="synthetic-preview", tool_arguments=arguments)
    with activate_execution(store, first):
        output = json.loads(asyncio.run(tool.on_invoke_tool(context, arguments)))
    assert output["status"] == "dry-run"
    assert store.operations(first)[0]["status"] == "no_effect"

    effects = []

    async def fake_write(_context, _arguments):
        effects.append("created")
        return json.dumps(_verified())

    with activate_execution(store, _begin(store, "actual")):
        actual = asyncio.run(_instrument(fake_write).on_invoke_tool(None, "{}"))
        assert json.loads(actual) == _verified()
    assert effects == ["created"]


def test_same_identity_after_preview_runs_actual_write_and_keeps_verified_raw_output(tmp_path):
    store = ExecutionStore(tmp_path / "execution.db")
    identity = _begin(store, "one-execution")
    live = False
    effects = []

    @durable_provider_tool("airtable_write_record")
    def helper(*, live=False):
        if not live:
            return {"status": "dry-run", "preview": "Synthetic plan"}
        effects.append("created")
        return {**_verified(), "original_envelope": {"field": "synthetic"}}

    async def callback(_context, _arguments):
        return json.dumps(helper(live=live))

    tool = _instrument(callback)
    with activate_execution(store, identity):
        preview = asyncio.run(tool.on_invoke_tool(None, "{}"))
        assert store.operations(identity)[0]["status"] == "no_effect"
        # A retained preview cache must not shadow the later verified raw result.
        store.save_stage(
            identity, "tool_result:airtable_write_record", {}, {"tool_output": preview},
        )
        live = True
        actual = asyncio.run(tool.on_invoke_tool(None, "{}"))
        replay = asyncio.run(tool.on_invoke_tool(None, "{}"))
    assert actual == replay
    assert json.loads(actual)["original_envelope"] == {"field": "synthetic"}
    assert effects == ["created"]
    assert store.operations(identity)[0]["status"] == "verified"


@pytest.mark.parametrize("receipt", [
    {"status": "error", "provider_write": False},
    {"status": "error", "dry_run": True, "provider_write": False},
    {"status": "success", "provider_write": False},
    {"status": "dry-run"},  # No decorated helper or explicit no-effect evidence.
])
def test_generic_failure_or_unproven_preview_remains_unresolved(tmp_path, receipt):
    store = ExecutionStore(tmp_path / "execution.db")
    first = _begin(store, "first")
    tool = _instrument(lambda _context, _arguments: json.dumps(receipt))
    with activate_execution(store, first):
        asyncio.run(tool.on_invoke_tool(None, "{}"))
    assert store.operations(first)[0]["status"] == "unknown"
    with pytest.raises(UncertainOperation):
        store.before_operation(_begin(store, "next"), "google_doc_write", {})


@pytest.mark.parametrize("verified", [False, True])
def test_early_actual_provider_identity_cannot_be_cleared_by_misleading_dry_run(tmp_path, verified):
    store = ExecutionStore(tmp_path / "execution.db")
    first = _begin(store, "first")

    @durable_provider_tool("airtable_write_record")
    def misleading_helper(*, live=False):
        actual = {**_verified(), "verification": {"passed": verified}}
        record_provider_observation(actual)
        return {"status": "dry-run", "dry_run": True, "provider_write": False}

    tool = _instrument(lambda _context, _arguments: json.dumps(misleading_helper()))
    with activate_execution(store, first):
        asyncio.run(tool.on_invoke_tool(None, "{}"))
    operation = store.operations(first)[0]
    assert operation["status"] == ("verified" if verified else "observed")
    assert json.loads(operation["receipt_json"])["record_id"] == "rec_synthetic"
    if not verified:
        with pytest.raises(UncertainOperation):
            store.before_operation(_begin(store, "next"), "google_doc_write", {})


def test_explicit_no_effect_receipt_is_not_replayed_as_a_real_mutation(tmp_path):
    store = ExecutionStore(tmp_path / "execution.db")
    identity = _begin(store, "first")
    calls = []

    async def callback(_context, _arguments):
        calls.append("invoked")
        return json.dumps({"status": "success", "dry_run": True, "provider_write": False})

    with activate_execution(store, identity):
        tool = _instrument(callback)
        asyncio.run(tool.on_invoke_tool(None, "{}"))
        asyncio.run(tool.on_invoke_tool(None, "{}"))
    assert calls == ["invoked", "invoked"]
    assert store.operations(identity)[0]["status"] == "no_effect"


def test_nested_preview_remains_an_intent_until_outer_tool_finishes(tmp_path):
    store = ExecutionStore(tmp_path / "execution.db")
    first = _begin(store, "first")
    second = _begin(store, "second")

    @durable_provider_tool("airtable_write_record")
    def preview(*, live=False):
        return {"status": "dry-run"}

    async def callback(_context, _arguments):
        result = preview()
        assert store.operations(first)[0]["status"] == "intent"
        with pytest.raises(UncertainOperation):
            store.before_operation(second, "google_doc_write", {})
        return json.dumps(result)

    with activate_execution(store, first):
        asyncio.run(_instrument(callback).on_invoke_tool(None, "{}"))
    assert store.operations(first)[0]["status"] == "no_effect"
    assert store.before_operation(second, "google_doc_write", {}) is None


def test_preview_attestation_does_not_leak_to_the_next_sdk_invocation(tmp_path):
    store = ExecutionStore(tmp_path / "execution.db")
    identity = _begin(store, "first")

    @durable_provider_tool("airtable_write_record")
    def preview(*, live=False):
        return {"status": "dry-run"}

    with activate_execution(store, identity):
        proven = _instrument(lambda _context, _arguments: json.dumps(preview()))
        asyncio.run(proven.on_invoke_tool(None, '{"value":"first"}'))
        unproven = _instrument(lambda _context, _arguments: '{"status":"dry-run"}')
        asyncio.run(unproven.on_invoke_tool(None, '{"value":"second"}'))
    assert [operation["status"] for operation in store.operations(identity)] == [
        "no_effect", "unknown",
    ]


def test_preview_identity_cannot_be_reconciled_into_a_verified_write(tmp_path):
    store = ExecutionStore(tmp_path / "execution.db")
    identity = _begin(store, "preview")
    store.before_operation(identity, "airtable_create_record", {})
    store.observe_operation(identity, "airtable_create_record", {}, {
        **_verified(), "status": "dry-run", "dry_run": True, "provider_write": False,
        "verification": {"passed": False},
    })
    assert store.operations(identity)[0]["status"] == "no_effect"
    with pytest.raises(UncertainOperation, match="No observed object"):
        store.reconcile_operation(identity, "airtable_create_record", {}, _verified())
    assert store.operations(identity)[0]["status"] == "no_effect"
    assert store.before_operation(identity, "airtable_create_record", {}) is None
