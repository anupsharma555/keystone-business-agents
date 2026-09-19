"""Accepted preflight cost metadata must survive the real context boundary."""

from __future__ import annotations

import json

import pytest

from keystone_agents.costing import estimate_usage_cost
from keystone_agents.runtime.context_snapshot import (
    freeze_workflow_context,
    restore_workflow_context,
)
from keystone_agents.runtime.durable_execution import (
    ExecutionConflict,
    ExecutionStore,
    execution_database_path,
)
from keystone_agents.schemas.work_item import WorkflowRunRequest, WorkItemRoute
from keystone_agents.storage.sqlite_store import _audit_safe_value, redact_secrets, stable_json


def _preflight(model="gpt-5.4-mini", *, cache_writes=None):
    entry = {
        "input_tokens": 21000, "output_tokens": 1700, "total_tokens": 22700,
        "cached_input_tokens": 1000, "cache_write_input_tokens": cache_writes,
        "reasoning_output_tokens": 0,
    }
    usage = {
        **entry, "available": True, "complete": True, "requests": 1,
        "attempt_count": 1, "provider_request_count_confirmed": True,
        "request_usage_entries": [entry],
        "prompt_cache_key_present": True, "prompt_cache_key_hash": "a" * 64,
    }
    cost = estimate_usage_cost(provider="openai", model=model, usage=usage)
    assert cost["estimated_usd"] > 0
    return {
        "mode": "llm", "status": "accepted", "target_agent": "business_research_analyst",
        "planner_rationale": "Review one supplied public source before drafting the answer.",
        "sdk_usage_events": [{
            "agent_name": "orchestrator", "stage": "orchestrator_preflight",
            "model_provider": "openai", "model_name": model,
            "usage": usage, "cost": cost,
            "request_cache": {
                "structured_output_retries": 0,
                "model_attempt_usage": [{"attempt_index": 1, "usage": usage}],
            },
        }],
    }


@pytest.mark.parametrize(("model", "cache_writes"), [
    ("gpt-5.4-mini", None), ("gpt-5.6-terra", 500),
])
def test_accepted_preflight_cost_survives_workflow_freeze_restore_and_journal(
    tmp_path, model, cache_writes,
):
    context = {"sources": [{"source_id": "synthetic-source", "claim": "A supplied public fact."}]}
    context_path = tmp_path / "context.json"
    context_path.write_text(json.dumps(context))
    preflight = _preflight(model, cache_writes=cache_writes)
    request = WorkflowRunRequest(
        request_text="Review the supplied public source and give a bounded answer.",
        requested_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        database_url=f"sqlite:///{tmp_path / 'business.db'}",
        context_file_path=str(context_path), orchestrator_preflight=preflight,
        live_sdk=False, live_search=False,
    )

    frozen = freeze_workflow_context(request)
    assert frozen.context_file_snapshot == context
    assert frozen.orchestrator_preflight == preflight
    serialized = stable_json(frozen.model_dump(mode="python"))
    assert "[REDACTED]" not in serialized
    restored = restore_workflow_context(request, json.loads(serialized))
    assert restored == frozen
    journal = ExecutionStore(execution_database_path(request.database_url))
    row = journal.begin({"request": restored.model_dump(mode="json"), "backend": "direct"})
    saved = json.loads(row["request_json"])["request"]["orchestrator_preflight"]
    assert saved == preflight
    assert _audit_safe_value(preflight) == preflight


def test_billable_numeric_map_preserves_missing_counts_without_string_allowance():
    payload = {"billable_tokens": {
        "input_tokens": None, "cached_input_tokens": 0,
        "cache_write_input_tokens": 5, "output_tokens": 10,
    }}
    assert redact_secrets(payload) == payload
    assert _audit_safe_value(payload) == payload


@pytest.mark.parametrize("billable", [
    "SYNTHETIC_OPAQUE_SECRET",
    {"input_tokens": "SYNTHETIC_OPAQUE_SECRET"},
    {"input_tokens": "123"},
    {"input_tokens": True},
    {"input_tokens": -1},
    {"input_tokens": 1.5},
    {"input_tokens": {"nested": "SYNTHETIC_OPAQUE_SECRET"}},
    {"input_tokens": 3, "unknown_field": "SYNTHETIC_OPAQUE_SECRET"},
    {"input_tokens": 3, "unknown_numeric_field": 5},
])
def test_invalid_billable_values_or_unknown_fields_still_block_workflow_freeze(billable):
    preflight = _preflight()
    preflight["sdk_usage_events"][0]["cost"]["billable_tokens"] = billable
    request = WorkflowRunRequest(
        request_text="Review one supplied synthetic source.", orchestrator_preflight=preflight,
        live_sdk=False, live_search=False,
    )
    safe = redact_secrets({"billable_tokens": billable})
    assert "SYNTHETIC_OPAQUE_SECRET" not in json.dumps(safe)
    assert "unknown_field" not in json.dumps(safe)
    assert "unknown_numeric_field" not in json.dumps(safe)
    with pytest.raises(ExecutionConflict, match="requires redaction"):
        freeze_workflow_context(request)
