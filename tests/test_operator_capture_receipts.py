"""Scoped local capture receipts survive validation without becoming source candidates."""

import json
from types import SimpleNamespace

import pytest
from test_operator_context_tools import _capability, _invoke

from keystone_agents.receipts.journal import instrument_agent_tools
from keystone_agents.receipts.mutations import (
    operation_is_mutation,
    receipt_is_local_memory_capture,
)
from keystone_agents.receipts.normalization import normalize_tool_output_receipt
from keystone_agents.runtime.decision_validation import (
    verified_candidate_fingerprints_from_receipts,
)
from keystone_agents.runtime.durable_execution import ExecutionStore, activate_execution
from keystone_agents.runtime.scoped_memory import project_scoped_memory_facts
from keystone_agents.runtime.tool_execution import (
    ToolEvidenceGroup,
    ToolExecutionContract,
    evaluate_tool_execution_contract,
    external_write_state_from_evidence,
)
from keystone_agents.tools.operator_context_tools import (
    _MIN_CAPTURE_RESPONSE_BYTES,
    _byte_count,
)

ARGS = {"kind": "preference", "title": "Bounded review", "summary": "Prefer bounded review."}


def test_verified_local_save_is_mutation_evidence_without_external_write_or_candidate(tmp_path):
    capability = _capability(tmp_path, capture_authorized=True)
    saved = _invoke(capability.tools[1], ARGS)
    receipt = normalize_tool_output_receipt("capture_operator_memory", saved)
    assert operation_is_mutation("capture_operator_memory")
    assert receipt_is_local_memory_capture(receipt)
    assert receipt["record_id"] == str(saved["id"])
    assert receipt["verification"] == {"passed": True}
    contract = ToolExecutionContract.required(
        ToolEvidenceGroup("scoped_capture", ("capture_operator_memory",)),
    )
    assert evaluate_tool_execution_contract(None, contract, tool_receipts=[receipt]).satisfied
    assert not verified_candidate_fingerprints_from_receipts([receipt])
    assert external_write_state_from_evidence(
        receipts=[receipt], tool_names=["capture_operator_memory"], evidence_complete=True,
    ) == "not_performed"


@pytest.mark.parametrize("change", [
    {"provider": "external_system"}, {"provider_write": True}, {"provider_write": None},
    {"operation": "create_record"}, {"tool_name": "create_external_record"},
])
def test_local_capture_exemption_rejects_conflicting_or_incomplete_receipts(tmp_path, change):
    capability = _capability(tmp_path, capture_authorized=True)
    receipt = normalize_tool_output_receipt(
        "capture_operator_memory", _invoke(capability.tools[1], ARGS),
    )
    conflicting = {**receipt, **change}
    assert not receipt_is_local_memory_capture(conflicting)
    assert external_write_state_from_evidence(
        receipts=[conflicting], tool_names=["capture_operator_memory"], evidence_complete=True,
    ) == "performed"
    assert verified_candidate_fingerprints_from_receipts([conflicting])


def test_arbitrary_saved_status_with_top_level_verified_does_not_gain_generic_admission():
    contract = ToolExecutionContract.required(ToolEvidenceGroup("provider", ("other_tool",)))
    assert not evaluate_tool_execution_contract(None, contract, tool_receipts=[{
        "tool_name": "other_tool", "status": "saved", "verified": True,
    }]).satisfied


@pytest.mark.parametrize("max_bytes", [128, _MIN_CAPTURE_RESPONSE_BYTES - 1])
def test_small_capture_budget_blocks_before_any_save(tmp_path, monkeypatch, max_bytes):
    capability = _capability(tmp_path, capture_authorized=True, max_bytes=max_bytes)
    assert [tool.name for tool in capability.tools] == ["query_operator_context"]

    def forbidden(*args, **kwargs):
        pytest.fail("An undersized capture must not reach audited storage.")

    monkeypatch.setattr(capability._storage, "save_memory_item", forbidden)
    query = _invoke(capability.tools[0], {"query": "bounded review", "include_profile": False})
    denied = json.loads(capability._capture("preference", ARGS["title"], ARGS["summary"], []))
    assert query["facts"] == []
    assert denied == {"status": "blocked", "reason": "capture_budget_unavailable"}
    assert _byte_count(query) <= max_bytes and _byte_count(denied) <= max_bytes
    assert capability.usage["response_bytes"] == _byte_count(query) + _byte_count(denied)
    assert capability.evidence["capture_count"] == 0
    assert not capability._storage.store.list_memory_items()


def test_capture_receipt_fits_reserved_floor_and_repeated_outputs_are_counted(tmp_path):
    capability = _capability(
        tmp_path, capture_authorized=True, max_bytes=_MIN_CAPTURE_RESPONSE_BYTES,
    )
    assert [tool.name for tool in capability.tools] == [
        "query_operator_context", "capture_operator_memory",
    ]
    first = _invoke(capability.tools[1], ARGS)
    again = _invoke(capability.tools[1], ARGS)
    assert first == again and first["verified"] is True
    assert _byte_count(first) <= capability.max_bytes <= 2048
    assert capability.usage["response_bytes"] == 2 * _byte_count(first)
    assert capability.usage["capture_saved_count"] == 1
    assert len(capability._storage.store.list_memory_items()) == 1


def test_durable_local_capture_invalidates_same_query_admitted_fact_cache(tmp_path):
    earlier = _capability(tmp_path, capture_authorized=True)
    _invoke(earlier.tools[1], {
        **ARGS, "title": "Earlier bounded review", "summary": "Use bounded review plans.",
    })
    admitted = project_scoped_memory_facts(earlier._storage.store.list_memory_items()).model_context
    capability = _capability(
        tmp_path, capture_authorized=True, initial_facts=admitted["facts"],
        raw_request="Save a second synthetic bounded review preference.",
    )
    execution = ExecutionStore(tmp_path / "isolated-execution.sqlite3")
    row = execution.begin({"request": "Save a synthetic bounded review preference."})
    native_tools = [getattr(tool, "sdk_tool", tool) for tool in capability.tools]
    instrument_agent_tools(SimpleNamespace(tools=native_tools))
    query = {"query": "bounded review", "include_profile": False}
    with activate_execution(execution, row["id"]):
        before = _invoke(native_tools[0], query)
        saved = _invoke(native_tools[1], ARGS)
        after = _invoke(native_tools[0], query)
    assert before["facts"] == [] and saved["verified"] is True
    assert before["status"] == "already_admitted" and before["already_admitted_ids"]
    assert len(after["facts"]) == 1
    assert after["facts"][0]["memory_id"] == saved["id"]
    assert capability.usage["query_calls"] == 2
    assert capability.usage["capture_saved_count"] == 1
    [operation] = execution.operations(row["id"])
    assert operation["tool_name"] == "capture_operator_memory"
    assert operation["status"] == "verified"
    assert external_write_state_from_evidence(
        receipts=[json.loads(operation["receipt_json"])],
        tool_names=["query_operator_context", "capture_operator_memory"], evidence_complete=True,
    ) == "not_performed"
    with execution.connection() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM execution_stages WHERE execution_id=? AND stage_id=?",
            (row["id"], "tool_read:query_operator_context"),
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM execution_stages WHERE execution_id=? AND stage_id=?",
            (row["id"], "tool_read:capture_operator_memory"),
        ).fetchone()[0] == 0
