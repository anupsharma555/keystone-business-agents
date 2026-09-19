from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from keystone_agents.finance_expense_receipts import FinanceReceiptEvidence
from keystone_agents.receipts.journal import instrument_agent_tools, reset_tool_receipt_journal
from keystone_agents.runtime.durable_execution import (
    ExecutionStore,
    UncertainOperation,
    activate_execution,
)
from keystone_agents.tools import internal_data_tools as airtable


@pytest.mark.parametrize("composite", [False, True])
@pytest.mark.parametrize("failure", ["exception", "missing_record"])
def test_airtable_created_identity_survives_failed_readback(
    tmp_path, monkeypatch, composite, failure
):
    for key, value in {
        "AIRTABLE_BASE_ID": "app_synthetic",
        "AIRTABLE_ACCESS_TOKEN": "synthetic-token",
        "AIRTABLE_ALLOWED_TABLES": "Business Expenses",
        "AIRTABLE_ALLOW_WRITES": "true",
        "AIRTABLE_WRITE_DRY_RUN": "false",
        "AIRTABLE_ALLOW_ATTACHMENT_UPLOADS": "true",
    }.items():
        monkeypatch.setenv(key, value)
    receipt = tmp_path / "receipt.pdf"
    receipt.write_bytes(b"%PDF-1.4\nsynthetic receipt")
    monkeypatch.setattr(
        airtable,
        "extract_finance_receipt_evidence",
        lambda path: FinanceReceiptEvidence(
            source_path=str(path),
            filename="receipt.pdf",
            content_read=True,
            vendor="Synthetic vendor",
            description="Synthetic service",
            total="10.00",
            currency="USD",
        ),
    )
    monkeypatch.setattr(
        airtable,
        "airtable_get_base_schema_impl",
        lambda **kwargs: {
            "schema": {
                "tables": [
                    {
                        "name": "Business Expenses",
                        "fields": [
                            {"name": "Item", "field_id": "fld_item", "field_type": "multilineText"},
                            {
                                "name": "Attachments",
                                "field_id": "fld_attachment",
                                "field_type": "multipleAttachments",
                            },
                        ],
                    }
                ]
            },
        },
    )
    store = ExecutionStore(tmp_path / "execution.sqlite3")
    execution_id = store.begin({"request": "Create one synthetic receipt record"})["id"]
    writes = []

    def provider_write(request, *, access_token):
        writes.append(request["method"])
        return {"id": "rec_synthetic"}

    def readback(*args, **kwargs):
        observed = store.operations(execution_id)
        assert len(observed) == 1
        assert observed[0]["status"] == "observed"
        assert json.loads(observed[0]["receipt_json"])["record_id"] == "rec_synthetic"
        if failure == "exception":
            raise RuntimeError("synthetic provider read-back failed")
        return {"records": []}

    monkeypatch.setattr(airtable, "_airtable_send", provider_write)
    monkeypatch.setattr(airtable, "airtable_read_records_impl", readback)
    tool_name = "airtable_create_expense_from_receipt" if composite else "airtable_write_record"

    def invoke(_context, _arguments):
        if composite:
            return airtable.airtable_create_expense_from_receipt_impl(
                str(receipt),
                table="Business Expenses",
                base_id="app_synthetic",
                approval_reference="synthetic-approval",
                live=True,
            )
        return airtable.airtable_write_record_impl(
            '{"Item":"Synthetic service"}',
            table="Business Expenses",
            base_id="app_synthetic",
            approval_reference="synthetic-approval",
            live=True,
        )

    tool = SimpleNamespace(name=tool_name, on_invoke_tool=invoke)
    instrument_agent_tools(SimpleNamespace(tools=[tool]))
    arguments = json.dumps({"local_file_path": str(receipt), "table": "Business Expenses"})
    with activate_execution(store, execution_id):
        reset_tool_receipt_journal()
        if failure == "exception":
            with pytest.raises(RuntimeError, match="read-back"):
                asyncio.run(tool.on_invoke_tool(None, arguments))
        else:
            result = asyncio.run(tool.on_invoke_tool(None, arguments))
            assert result["verification"]["passed"] is False

    reopened = ExecutionStore(store.path)
    observed = reopened.operations(execution_id)[0]
    assert observed["status"] == "observed"
    assert json.loads(observed["receipt_json"])["record_id"] == "rec_synthetic"
    with activate_execution(reopened, execution_id):
        with pytest.raises(UncertainOperation):
            asyncio.run(tool.on_invoke_tool(None, arguments))
    assert writes == ["POST"]
