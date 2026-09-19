"""Numeric usage survives audit persistence without making secret-field exceptions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from keystone_agents.runtime.decision_trace_harness import build_backend_decision_stage_trace
from keystone_agents.storage.sqlite_store import (
    SQLiteStore,
    _audit_safe_value,
    redact_secrets,
    stable_json,
)
from keystone_agents.trace_processor import (
    _sanitize_nested_specialist_execution,
    record_sdk_run_summary_trace_event,
)
from keystone_agents.usage_projection import project_request_usage_entries
from promptfoo.eval_database import list_eval_trace_events


@pytest.mark.parametrize("storage_first", [True, False])
def test_storage_and_runtime_import_in_either_order_in_a_fresh_process(tmp_path, storage_first):
    imports = [
        "from keystone_agents.storage.sqlite_store import SQLiteStore",
        "from keystone_agents.runtime import RequestRuntime",
    ]
    if not storage_first:
        imports.reverse()
    source_root = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source_root)
    result = subprocess.run(
        [sys.executable, "-c", "\n".join([
            *imports,
            "from keystone_agents.usage_projection import nonnegative_usage_integer",
            "assert SQLiteStore.__name__ == 'SQLiteStore'",
            "assert RequestRuntime.__name__ == 'RequestRuntime'",
            "assert nonnegative_usage_integer(None) is None",
            "assert nonnegative_usage_integer(2) == 2",
        ])],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20, check=False,
    )
    assert result.returncode == 0, result.stderr


def _usage(cache_writes):
    return {
        "available": True, "requests": 2, "input_tokens": 400000,
        "output_tokens": 15, "total_tokens": 400015, "cached_input_tokens": 384,
        "cache_write_input_tokens": cache_writes, "reasoning_output_tokens": 0,
        "request_usage_entries": [
            {"input_tokens": 199999, "output_tokens": 7, "total_tokens": 200006,
             "cached_input_tokens": 128, "cache_write_input_tokens": cache_writes,
             "reasoning_output_tokens": 0},
            {"input_tokens": 200001, "output_tokens": 8, "total_tokens": 200009,
             "cached_input_tokens": 256, "cache_write_input_tokens": 0,
             "reasoning_output_tokens": 0},
        ],
    }


def _stage(usage):
    return build_backend_decision_stage_trace(
        stage_id="synthetic-usage", agent="synthetic_agent", stage="sdk_run",
        provider="openai", model="synthetic-model", run_id="synthetic-run",
        trace_id="synthetic-trace", model_visible_inputs=[], usage=usage,
    )


@pytest.mark.parametrize("cache_writes", [None, 0, 1024])
def test_usage_keeps_unknown_zero_and_positive_values_through_redaction_and_sqlite(
    tmp_path, cache_writes,
):
    usage = _usage(cache_writes)
    payload = {"usage": usage, "sdk_run_failure": {"usage": usage}}
    assert redact_secrets(payload) == payload
    assert _audit_safe_value(payload) == payload
    assert json.loads(stable_json(payload)) == payload
    store = SQLiteStore(f"sqlite:///{tmp_path / 'business.db'}")
    identity = store.save_agent_run(
        agent_name="synthetic_agent", output=payload, status="error",
    )
    assert store.get_agent_run(identity)["output"] == payload


@pytest.mark.parametrize("cache_writes", [None, 0, 1024])
def test_backend_failure_trace_retains_per_request_boundaries_and_cache_writes(cache_writes):
    stage = _stage(_usage(cache_writes))
    retained = json.loads(stable_json({
        "decision_trace": {"stages": [stage.model_dump(mode="json")]},
    }))
    usage = retained["decision_trace"]["stages"][0]["consumption"]["usage"]
    assert usage["cache_write_input_tokens"] == cache_writes
    assert usage["request_usage_entries"] == _usage(cache_writes)["request_usage_entries"]


@pytest.mark.parametrize("cache_writes", [None, 1024])
def test_persisted_sdk_trace_and_nested_specialist_usage_keep_new_metrics(tmp_path, cache_writes):
    usage = _usage(cache_writes)
    database = tmp_path / "trace.db"
    identity = record_sdk_run_summary_trace_event(
        agent_name="synthetic_agent", usage=usage, status="failed", database_path=database,
        request_cache={"nested_specialist_executions": [
            {"route_name": "synthetic", "usage": usage},
        ]},
    )
    assert identity is not None
    row = list_eval_trace_events(database_path=database)[0]
    summary = row["metadata"]["token_summary"]
    assert summary["cache_write_input_tokens"] == cache_writes
    assert summary["request_usage_entries"] == usage["request_usage_entries"]
    nested = row["metadata"]["nested_specialist_executions"][0]["usage"]
    assert nested["cache_write_input_tokens"] == cache_writes
    assert nested["request_usage_entries"] == usage["request_usage_entries"]


@pytest.mark.parametrize("invalid", [
    "123", "SYNTHETIC_OPAQUE_SECRET", True, -1, 1.5, {"raw": "secret"},
])
def test_token_like_metric_keys_never_whitelist_strings_or_invalid_numbers(invalid):
    payload = {"usage": {"cache_write_input_tokens": invalid, "input_tokens": invalid}}
    assert redact_secrets(payload) == {
        "usage": {"cache_write_input_tokens": None, "input_tokens": None},
    }
    assert _audit_safe_value(payload) == redact_secrets(payload)
    stage = _stage(payload["usage"])
    assert stage.consumption.usage["cache_write_input_tokens"] is None
    assert stage.consumption.usage["input_tokens"] is None


def test_per_request_projection_discards_secrets_without_losing_response_boundaries(tmp_path):
    canary = "SYNTHETIC_OPAQUE_SECRET_NOT_MATCHED_BY_KEY_REGEX"
    usage = {
        "cache_write_input_tokens": canary,
        "request_usage_entries": [
            {"input_tokens": 5, "cache_write_input_tokens": canary,
             "raw_usage": {"unexpected": canary}, "provider_response": canary},
            {"input_tokens": "8", "output_tokens": True, "cached_input_tokens": -2,
             "total_tokens": None, "api_key": canary},
            canary,
        ],
    }
    expected = [
        {"input_tokens": 5, "cache_write_input_tokens": None},
        {"input_tokens": None, "output_tokens": None, "cached_input_tokens": None,
         "total_tokens": None},
        {},
    ]
    assert project_request_usage_entries(usage["request_usage_entries"]) == expected
    clean = redact_secrets({"usage": usage})
    stage = _stage(usage)
    nested = _sanitize_nested_specialist_execution({"usage": usage})
    assert clean["usage"]["request_usage_entries"] == expected
    assert stage.consumption.usage["request_usage_entries"] == expected
    assert nested["usage"]["request_usage_entries"] == expected
    assert canary not in json.dumps([clean, stage.model_dump(mode="json"), nested])
    store = SQLiteStore(f"sqlite:///{tmp_path / 'business.db'}")
    identity = store.save_agent_run(agent_name="synthetic_agent", output={"usage": usage})
    assert store.get_agent_run(identity)["output"] == clean
