from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import keystone_agents.run as sdk_run
from keystone_agents.trace_processor import (
    KeystoneEvalTraceProcessor,
    record_sdk_run_summary_trace_event,
    register_configured_trace_processor,
)
from promptfoo.eval_database import list_eval_trace_events, summarize_eval_trace_events


def test_trace_processor_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("KEYSTONE_TRACE_PROCESSOR", raising=False)

    assert register_configured_trace_processor() is None


def test_trace_processor_disabled_import_does_not_require_repo_root_package(tmp_path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = str(repo_root / "src")
    env.pop("KEYSTONE_TRACE_PROCESSOR", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from keystone_agents.trace_processor import "
                "register_configured_trace_processor; "
                "assert register_configured_trace_processor() is None"
            ),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_trace_processor_records_safe_summary_and_rejects_unsafe_metadata() -> None:
    processor = KeystoneEvalTraceProcessor()
    trace = SimpleNamespace(
        trace_id="trace_123",
        workflow_name="Keystone eval",
        group_id="group_1",
        metadata={"agent_name": "business_research_analyst"},
    )
    unsafe_span = SimpleNamespace(
        trace_id="trace_123",
        span_id="span_1",
        parent_id="trace_123",
        span_data=SimpleNamespace(type="model"),
        metadata={"prompt": "raw model prompt should not be stored"},
    )

    processor.on_trace_start(trace)
    processor.on_span_start(unsafe_span)
    processor.on_span_end(unsafe_span)
    processor.on_trace_end(trace)

    events = processor.events
    assert events[0]["event_type"] == "trace_start"
    assert events[0]["metadata"] == {"agent_name": "business_research_analyst"}
    assert events[1]["event_type"] == "span_start"
    assert events[1]["metadata"] == {"metadata_status": "rejected_unsafe"}
    assert "raw model prompt" not in str(events)
    assert events[2]["duration_ms"] >= 0


def test_trace_processor_persists_safe_summary(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    processor = KeystoneEvalTraceProcessor(database_path=database_path)
    trace = SimpleNamespace(
        trace_id="trace_abc",
        workflow_name="Keystone eval",
        group_id="case_123",
        metadata={"agent_name": "orchestrator", "case_id": "case_123"},
    )

    processor.on_trace_start(trace)
    processor.on_trace_end(trace)

    rows = list_eval_trace_events(database_path=database_path)
    assert len(rows) == 2
    assert rows[0]["event_type"] == "trace_end"
    assert rows[0]["trace_id"] == "trace_abc"
    assert rows[0]["metadata"] == {"agent_name": "orchestrator", "case_id": "case_123"}
    assert "prompt" not in str(rows).lower()


def test_trace_processor_redacts_unsafe_scalar_fields(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    processor = KeystoneEvalTraceProcessor(database_path=database_path)
    trace = SimpleNamespace(
        trace_id="trace jane@example.com sk-SECRETSECRETSECRET",
        workflow_name="raw model prompt for jane@example.com",
        group_id="case jane@example.com",
        metadata={"agent_name": "orchestrator"},
    )

    processor.on_trace_start(trace)

    rows = list_eval_trace_events(database_path=database_path)
    serialized = str(rows)
    assert len(rows) == 1
    assert rows[0]["trace_id"].startswith("redacted_")
    assert rows[0]["name"].startswith("redacted_")
    assert rows[0]["group_id"].startswith("redacted_")
    assert "jane@example.com" not in serialized
    assert "sk-SECRET" not in serialized


def test_sdk_run_summary_trace_event_is_joinable_and_redacted(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"

    row_id = record_sdk_run_summary_trace_event(
        agent_name="business_research_analyst",
        route="business_research_analyst",
        live=True,
        run_mode="live_sdk",
        model_provider="openai",
        model_name="gpt-5.4-mini",
        request_cache={
            "static_prefix_sha256": "a" * 64,
            "dynamic_prompt_sha256": "b" * 64,
            "tool_names_sha256": "c" * 64,
            "tool_count": 3,
            "max_turns": 2,
            "max_turns_source": "quality_budget",
            "session_scope": "work_item",
            "session_source": "env",
            "session_id_hash": "abc123def456",
            "session_history_mode": "recent_items",
            "session_history_limit": 8,
            "session_truncation_configured": True,
        },
        usage={
            "available": True,
            "requests": 2,
            "input_tokens": 1200,
            "cached_input_tokens": 400,
            "output_tokens": 300,
            "total_tokens": 1500,
            "cache_hit_rate": 0.3333,
            "prompt_cache_key_present": True,
            "prompt_cache_key_hash": "d" * 12,
        },
        cost={
            "source": "local_pricing_table",
            "estimated_usd": 0.0042,
            "pricing_provider": "openai",
            "pricing_model": "gpt-5.4-mini",
        },
        budget_guard={"status": "ok", "exceeded": False},
        search_diagnostics={
            "provider_summary": "searxng+agents-web-search",
            "providers_used": ["searxng", "agents-web-search"],
            "search_providers_attempted": ["searxng", "agents-web-search"],
        },
        raw_result={
            "new_items": [
                {
                    "type": "function_call",
                    "name": "search_web",
                    "status": "completed",
                    "duration_ms": 8.25,
                    "arguments": {"query": "private query must not persist"},
                },
                {"type": "handoff_call", "name": "handoff_to_business_research_analyst"},
                {"type": "message_output_item", "output": "private answer must not persist"},
            ]
        },
        trace_metadata={
            "case_id": "case_123",
            "eval_id": "eval_456",
            "work_item_id": "wi_789",
            "run_id": "run_abc",
            "slack_channel_id": "C123",
            "slack_thread_ts": "1715366400.000100",
        },
        duration_ms=12.5,
        database_path=database_path,
    )

    rows = list_eval_trace_events(database_path=database_path)
    metadata = rows[0]["metadata"]
    serialized = str(rows)
    assert row_id == rows[0]["id"]
    assert rows[0]["event_type"] == "sdk_run_summary"
    assert rows[0]["trace_id"] == "run_abc"
    assert rows[0]["group_id"] == "case_123"
    assert metadata["schema"] == "keystone.sdk_run_summary.v1"
    assert metadata["correlation"]["work_item_id"] == "wi_789"
    assert metadata["max_turns"] == 2
    assert metadata["turns_used"] == 2
    assert metadata["turns_used_source"] == "usage_requests"
    assert metadata["tool_call_counts"] == {"search_web": 1}
    assert metadata["handoff_count"] == 1
    assert metadata["child_step_summary"] == [
        {
            "step_index": 1,
            "category": "tool",
            "name": "search_web",
            "status": "completed",
            "duration_ms": 8.25,
            "error_kind": "",
        },
        {
            "step_index": 2,
            "category": "handoff",
            "name": "handoff_to_business_research_analyst",
            "status": "observed",
            "duration_ms": None,
            "error_kind": "",
        },
        {
            "step_index": 3,
            "category": "model",
            "name": "model",
            "status": "observed",
            "duration_ms": None,
            "error_kind": "",
        },
    ]
    assert metadata["retrieval_provider_summary"]["providers_used"] == [
        "searxng",
        "agents-web-search",
    ]
    assert metadata["diagnostic_contract"]["schema"] == "keystone.eval_run_diagnostics.v1"
    assert metadata["diagnostic_contract"]["logs_hold_verbose_details"] is True
    assert metadata["model"]["provider"] == "openai"
    assert metadata["tooling"]["tool_call_count"] == 1
    assert metadata["tooling"]["tool_names"] == ["search_web"]
    assert metadata["tooling"]["child_step_summary"] == metadata["child_step_summary"]
    assert metadata["retrieval"]["search_provider"] == "searxng+agents-web-search"
    assert metadata["retrieval"]["search_provider_sequence"] == ["searxng", "agents-web-search"]
    assert metadata["cost"]["sdk_estimated_cost_usd"] == 0.0042
    assert metadata["approval"]["send_enabled"] is False
    assert metadata["side_effects"]["external_write_performed"] is False
    assert metadata["diagnostic_summary"]["has_model_metadata"] is True
    assert metadata["diagnostic_summary"]["has_tool_metadata"] is True
    assert metadata["diagnostic_summary"]["has_retrieval_metadata"] is True
    assert metadata["diagnostic_summary"]["has_error_or_retry"] is False
    assert metadata["prompt_cache"]["dynamic_prompt_sha256"] == "b" * 64
    assert metadata["token_summary"]["input_tokens"] == 1200
    assert metadata["cost_summary"]["estimated_usd"] == 0.0042
    assert metadata["redaction"]["raw_prompt_included"] is False
    assert metadata["redaction"]["raw_response_included"] is False
    assert "raw model prompt" not in serialized
    assert "private query" not in serialized
    assert "private answer" not in serialized
    assert "sk-" not in serialized
    summary = summarize_eval_trace_events(database_path=database_path)
    assert summary["diagnostic_category_counts"] == []


def test_sdk_run_summary_trace_event_classifies_failures_for_trace_analysis(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"

    record_sdk_run_summary_trace_event(
        agent_name="opportunity_scout",
        route="opportunity_scout",
        live=True,
        run_mode="live_sdk",
        model_provider="openai",
        model_name="gpt-5.4-mini",
        request_cache={"tool_count": 1},
        usage={"requests": 1},
        cost={"estimated_usd": 0.001},
        budget_guard={"status": "approval_required", "approval_required": True},
        search_diagnostics={
            "provider_summary": "searxng",
            "providers_used": ["searxng"],
            "search_provider_errors": [
                {"provider": "searxng", "error_type": "TimeoutError", "code": "timeout"}
            ],
            "web_extraction_issue_count": 1,
            "web_extraction_issues": [{"type": "empty_extract"}, {"type": "paywall"}],
            "failed_tool_call_count": 1,
        },
        orchestrator_diagnostics={
            "has_preflight": True,
            "has_review": True,
            "selected_route": "opportunity_scout",
            "route_confidence": 0.82,
            "feedback_count": 2,
            "blocker_count": 1,
            "review_status": "repair_recommended",
        },
        raw_result={"new_items": [{"type": "function_call", "name": "search_web"}]},
        trace_metadata={"case_id": "case_failure_001", "run_id": "run_failure_001"},
        status="error",
        failure_kind="provider_timeout",
        retry_count=2,
        repair_loop_count=1,
        database_path=database_path,
    )

    rows = list_eval_trace_events(database_path=database_path)
    metadata = rows[0]["metadata"]
    summary = summarize_eval_trace_events(database_path=database_path)
    categories = {item["key"]: item["count"] for item in summary["diagnostic_category_counts"]}
    trend_categories = {item["key"] for item in summary["diagnostic_category_trends"]}

    assert metadata["error_retry"]["warning_types"] == [
        "retrieval_provider_errors",
        "web_extraction_issues",
        "provider_timeout",
        "repair_loop",
        "orchestrator_blockers",
    ]
    assert metadata["retrieval"]["search_provider_error_count"] == 1
    assert metadata["retrieval"]["search_provider_error_types"] == ["TimeoutError"]
    assert metadata["retrieval"]["web_extraction_issue_types"] == ["empty_extract", "paywall"]
    assert metadata["orchestrator"]["has_preflight"] is True
    assert metadata["orchestrator"]["has_review"] is True
    assert metadata["orchestrator"]["selected_route"] == "opportunity_scout"
    assert metadata["orchestrator"]["route_confidence"] == 0.82
    assert metadata["orchestrator"]["feedback_count"] == 2
    assert metadata["orchestrator"]["blocker_count"] == 1
    assert metadata["orchestrator"]["review_status"] == "repair_recommended"
    assert metadata["diagnostic_summary"]["has_orchestrator_feedback"] is True
    assert metadata["diagnostic_summary"]["has_error_or_retry"] is True
    assert categories["error_or_retry"] == 1
    assert categories["web_extraction_issues"] == 1
    assert categories["tool_failures"] == 1
    assert categories["approval_gate"] == 1
    assert categories["orchestrator_feedback"] == 1
    assert trend_categories >= {
        "error_or_retry",
        "web_extraction_issues",
        "tool_failures",
        "approval_gate",
        "orchestrator_feedback",
    }
    assert summary["diagnostic_followups"][0]["join_key"] == "case_failure_001"
    assert summary["diagnostic_case_rollups"][0]["join_key"] == "case_failure_001"
    assert summary["diagnostic_case_rollups"][0]["event_count"] == 1
    assert {item["key"] for item in summary["diagnostic_case_rollups"][0]["categories"]} >= {
        "error_or_retry",
        "web_extraction_issues",
        "tool_failures",
        "approval_gate",
        "orchestrator_feedback",
    }


def test_trace_diagnostic_trends_group_categories_by_day(tmp_path) -> None:
    database_path = tmp_path / "evals.sqlite"
    for case_id in ("case_trend_older_001", "case_trend_newer_001"):
        record_sdk_run_summary_trace_event(
            agent_name="business_research_analyst",
            route="business_research_analyst",
            live=True,
            run_mode="live_sdk",
            model_provider="openai",
            model_name="gpt-5.4-mini",
            request_cache={"tool_count": 1},
            usage={"requests": 1},
            cost={"estimated_usd": 0.001},
            budget_guard={"status": "ok"},
            search_diagnostics={
                "provider_summary": "searxng",
                "providers_used": ["searxng"],
                "search_provider_errors": [{"provider": "searxng", "code": "timeout"}],
            },
            trace_metadata={"case_id": case_id, "run_id": case_id.replace("case", "run")},
            status="error",
            failure_kind="provider_timeout",
            database_path=database_path,
        )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE eval_trace_events SET created_at = ? WHERE group_id = ?",
            ("2026-06-13T10:00:00Z", "case_trend_older_001"),
        )
        connection.execute(
            "UPDATE eval_trace_events SET created_at = ? WHERE group_id = ?",
            ("2026-06-14T10:00:00Z", "case_trend_newer_001"),
        )
        connection.commit()

    trends = summarize_eval_trace_events(database_path=database_path)["diagnostic_category_trends"]
    error_trends = [
        (item["date"], item["key"], item["count"], item["severity"])
        for item in trends
        if item["key"] == "error_or_retry"
    ]

    assert error_trends == [
        ("2026-06-13", "error_or_retry", 1, "fail"),
        ("2026-06-14", "error_or_retry", 1, "fail"),
    ]


def test_sdk_run_recorder_maps_safe_orchestrator_trace_metadata(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "evals.sqlite"
    monkeypatch.setenv("KEYSTONE_TRACE_SUMMARY_DB", str(database_path))

    sdk_run._record_sdk_run_summary_safely(
        agent_name="business_research_analyst",
        model_provider="openai",
        model_name="gpt-5.4-mini",
        model_run_mode="live_sdk",
        live=True,
        request_cache={"tool_count": 1},
        usage={"requests": 1},
        cost={"estimated_usd": 0.001},
        budget_guard={"status": "ok"},
        search_telemetry=[],
        search_diagnostics={"provider_summary": "dry-run"},
        raw_result=None,
        trace_metadata={
            "case_id": "case_orchestrator_trace_001",
            "run_id": "run_orchestrator_trace_001",
            "route": "business_research_analyst",
            "orchestrator_has_preflight": True,
            "orchestrator_has_review": True,
            "orchestrator_feedback_count": 3,
            "orchestrator_blocker_count": 1,
            "orchestrator_review_status": "partial",
        },
        status="ok",
    )

    rows = list_eval_trace_events(database_path=database_path)
    metadata = rows[0]["metadata"]
    categories = {
        item["key"]: item["count"]
        for item in summarize_eval_trace_events(database_path=database_path)[
            "diagnostic_category_counts"
        ]
    }

    assert metadata["orchestrator"]["has_preflight"] is True
    assert metadata["orchestrator"]["has_review"] is True
    assert metadata["orchestrator"]["feedback_count"] == 3
    assert metadata["orchestrator"]["blocker_count"] == 1
    assert metadata["orchestrator"]["review_status"] == "partial"
    assert metadata["diagnostic_summary"]["has_orchestrator_feedback"] is True
    assert categories["orchestrator_feedback"] == 1
    assert categories["error_or_retry"] == 1
