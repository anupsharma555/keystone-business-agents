from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from keystone_agents.receipts.journal import instrument_agent_tools, reset_tool_receipt_journal
from keystone_agents.runtime.durable_execution import (
    ExecutionStore,
    UncertainOperation,
    activate_execution,
)
from keystone_agents.tools import internal_data_tools as workspace


@pytest.mark.parametrize("failure_stage", ["folder", "body", "readback"])
def test_created_doc_identity_survives_later_stage_failure(tmp_path, monkeypatch, failure_stage):
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    execution_id = store.begin({"request": "Create one synthetic document"})["id"]
    creates = []

    def observe_then_fail(stage):
        row = store.operations(execution_id)[0]
        assert row["status"] == "observed"
        assert json.loads(row["receipt_json"])["document_id"] == "synthetic-doc"
        if stage == failure_stage:
            raise RuntimeError(f"synthetic {stage} failure")
        return {}

    class Documents:
        def documents(self):
            return self

        def create(self, **kwargs):
            def execute():
                creates.append("create")
                return {"documentId": "synthetic-doc"}

            return SimpleNamespace(execute=execute)

        def batchUpdate(self, **kwargs):
            return SimpleNamespace(execute=lambda: observe_then_fail("body"))

        def get(self, **kwargs):
            return SimpleNamespace(execute=lambda: observe_then_fail("readback"))

    monkeypatch.setattr(workspace, "_require_google_workspace_write_approval", lambda *_args: None)
    monkeypatch.setattr(
        workspace,
        "_google_workspace_services",
        lambda: {
            "docs": Documents(),
            "drive": object(),
        },
    )
    monkeypatch.setattr(workspace, "_assert_configured_google_account", lambda *_args: None)
    monkeypatch.setattr(
        workspace,
        "_ensure_drive_folder_path",
        lambda *_args: observe_then_fail("folder") or "synthetic-folder",
    )
    monkeypatch.setattr(workspace, "_move_drive_file_to_folder", lambda *_args: None)

    def invoke(_context, _arguments):
        return workspace.google_doc_write_impl(
            "Synthetic doc",
            "Synthetic content",
            approval_reference="synthetic-approval",
            live=True,
        )

    tool = SimpleNamespace(name="google_doc_write", on_invoke_tool=invoke)
    instrument_agent_tools(SimpleNamespace(tools=[tool]))
    arguments = '{"title":"Synthetic doc","body_text":"Synthetic content"}'
    with activate_execution(store, execution_id):
        reset_tool_receipt_journal()
        with pytest.raises(RuntimeError, match=failure_stage):
            asyncio.run(tool.on_invoke_tool(None, arguments))

    reopened = ExecutionStore(store.path)
    row = reopened.operations(execution_id)[0]
    assert row["status"] == "observed"
    assert json.loads(row["receipt_json"])["document_id"] == "synthetic-doc"
    with activate_execution(reopened, execution_id):
        with pytest.raises(UncertainOperation):
            asyncio.run(tool.on_invoke_tool(None, arguments))
    assert creates == ["create"]
