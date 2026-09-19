from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "codex-skills"
    / "kba-agent-run-diagnosis"
    / "scripts"
    / "collect_run_evidence.py"
)
SPEC = importlib.util.spec_from_file_location("collect_run_evidence", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def test_collector_proves_stale_slack_runtime_from_source_newer_than_hello(
    tmp_path: Path,
) -> None:
    slack_repo = tmp_path / "keystone-slack"
    local = slack_repo / ".local"
    source = slack_repo / "kni_integrations" / "business_agents_bridge.py"
    local.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    slack_db = local / "workflow-state.sqlite"
    slack_db.touch()
    source.write_text("# changed runtime\n", encoding="utf-8")
    source_mtime = datetime(2026, 8, 3, 18, 0, tzinfo=UTC).timestamp()
    os.utime(source, (source_mtime, source_mtime))
    health = local / "slack-socket-health.json"
    health.write_text(
        json.dumps(
            {
                "status": "healthy",
                "connected": True,
                "last_hello_at": "2026-08-03T17:00:00+00:00",
                "updated_at": "2026-08-03T18:01:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    runtime, warnings = collector._collect_slack_runtime(
        slack_db,
        source_paths=[source],
    )

    assert warnings == []
    assert runtime["health_status"] == "healthy"
    assert runtime["connected"] is True
    assert runtime["freshness"] == "stale_proven"
    assert runtime["sources_newer_than_last_hello"] == [str(source)]

    report = {
        "slack_record": {"run_id": "run-1"},
        "slack_runtime": runtime,
        "slack_attempts": [],
        "agent_runs": [],
        "tool_events": [],
        "work_item_events": [],
        "trace_events": [],
        "external_payloads": [],
    }
    args = collector._parser().parse_args([])
    findings, _gaps = collector._derive_findings(report, args)
    report["findings"] = findings
    report["evidence_gaps"] = []
    report["warnings"] = []

    assert "slack_runtime_stale" in {item["code"] for item in findings}
    assert collector._verdict(report) == "FAIL"


def test_collector_does_not_overclaim_runtime_freshness(tmp_path: Path) -> None:
    slack_repo = tmp_path / "keystone-slack"
    local = slack_repo / ".local"
    source = slack_repo / "kni_integrations" / "slack_socket_mode.py"
    local.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    slack_db = local / "workflow-state.sqlite"
    slack_db.touch()
    source.write_text("# runtime\n", encoding="utf-8")
    source_mtime = datetime(2026, 8, 3, 16, 0, tzinfo=UTC).timestamp()
    os.utime(source, (source_mtime, source_mtime))
    (local / "slack-socket-health.json").write_text(
        json.dumps(
            {
                "status": "healthy",
                "connected": True,
                "last_hello_at": "2026-08-03T17:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )

    runtime, warnings = collector._collect_slack_runtime(
        slack_db,
        source_paths=[source],
    )

    assert warnings == []
    assert runtime["freshness"] == "no_staleness_proven"
    assert "still required to prove" in runtime["freshness_note"]


def _slack_database(path: Path, *, provider: str, tool_name: str, called: bool) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE business_agent_slack_run_records (
                run_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                request_ts TEXT NOT NULL,
                request_text TEXT NOT NULL,
                work_item_id TEXT NOT NULL,
                route TEXT NOT NULL,
                status TEXT NOT NULL,
                last_error TEXT NOT NULL,
                result_title TEXT NOT NULL,
                result_text TEXT NOT NULL,
                telemetry_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE business_agent_slack_runs (
                run_id TEXT PRIMARY KEY,
                request_text TEXT NOT NULL,
                resolved_request_text TEXT NOT NULL,
                status TEXT NOT NULL,
                channel_id TEXT NOT NULL,
                thread_ts TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                work_item_id TEXT NOT NULL,
                retry_of_run_id TEXT NOT NULL,
                error TEXT NOT NULL,
                result_title TEXT NOT NULL,
                result_text TEXT NOT NULL
            );
            """
        )
        telemetry = {
            "request_cache": {
                "request_tool_scope": {
                    "agent_name": "example_agent",
                    "candidate_tool_count": 12,
                    "selected_tool_count": 1,
                    "selected_tool_names": [tool_name],
                }
            },
            "tool_execution": {
                "mode": "llm_selected_function_tools" if called else "model_tools_attached_no_call",
                "model_called_tool_names": [tool_name] if called else [],
                "model_tool_call_count": 1 if called else 0,
                "provider_receipt_count": 1 if called else 0,
            },
        }
        request = (
            f"Provider affinity: {provider}\n"
            "Current user request (authoritative): inspect the selected records"
        )
        connection.execute(
            "INSERT INTO business_agent_slack_run_records "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "run-1",
                "C1",
                "100.1",
                "100.2",
                request,
                "",
                "example_agent",
                "completed",
                "",
                "Result",
                "Completed",
                json.dumps(telemetry),
                "2026-01-01T00:00:00Z",
                "2026-01-01T00:01:00Z",
            ),
        )


def _kba_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE agent_runs (
                id INTEGER PRIMARY KEY,
                agent_name TEXT,
                output_json TEXT,
                model TEXT,
                dry_run INTEGER,
                status TEXT,
                error TEXT,
                created_at_utc TEXT,
                created_at_et TEXT,
                created_at TEXT
            );
            CREATE TABLE tool_events (
                id INTEGER PRIMARY KEY,
                tool_name TEXT,
                agent_name TEXT,
                run_id TEXT,
                status TEXT,
                error TEXT,
                created_at_utc TEXT,
                created_at_et TEXT
            );
            """
        )


def test_collector_correlates_failed_agent_run_from_slack_provenance(
    tmp_path: Path,
) -> None:
    kba_db = tmp_path / "kba.db"
    _kba_database(kba_db)
    provenance = {
        "schema": "keystone.slack.run_provenance.v1",
        "context_validated": True,
        "team_id": "T1",
        "channel_id": "C1",
        "thread_ts": "100.1",
        "request_ts": "100.2",
        "runtime_fingerprint": {
            "schema": "keystone.runtime_fingerprint.v1",
            "runtime_sha256": "a" * 64,
            "source_sha256": "b" * 64,
            "dependency_sha256": "c" * 64,
            "config_sha256": "d" * 64,
            "source_file_count": 42,
            "source_read_error_count": 0,
            "dependency_file_count": 2,
            "config_key_count": 5,
            "python_runtime": "3.13",
            "process_started_at_utc": "2026-08-05T03:12:00+00:00",
        },
    }
    with sqlite3.connect(kba_db) as connection:
        connection.execute(
            "INSERT INTO agent_runs "
            "(id, agent_name, output_json, model, dry_run, status, error, "
            "created_at_utc, created_at_et, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                1,
                "opportunity_scout",
                json.dumps(
                    {
                        "status": "timeout",
                        "slack_run_provenance": provenance,
                        "output": {"failure": {"kind": "provider_timeout"}},
                    }
                ),
                "child-error:opportunity_scout",
                0,
                "error",
                "provider_timeout",
                "2026-08-05T03:12:48Z",
                "2026-08-04T23:12:48-04:00",
                "2026-08-05 03:12:48",
            ),
        )

    runs, _tools, _work_item, _events, warnings = collector._collect_kba(
        kba_db,
        channel_id="C1",
        thread_ts="100.1",
        agent_run_id=None,
        work_item_id="",
        scan_limit=10,
    )

    assert warnings == []
    assert [(run["id"], run["agent_name"], run["status"]) for run in runs] == [
        (1, "opportunity_scout", "error")
    ]
    assert runs[0]["runtime_fingerprint"]["runtime_sha256"] == "a" * 64
    assert runs[0]["runtime_fingerprint"]["source_file_count"] == 42


def test_collector_fails_on_slack_and_child_runtime_fingerprint_mismatch() -> None:
    report = {
        "slack_record": {
            "run_id": "slack-1",
            "runtime_fingerprint": {"runtime_sha256": "a" * 64},
        },
        "slack_runtime": {},
        "slack_attempts": [],
        "agent_runs": [
            {
                "id": 1,
                "runtime_fingerprint": {"runtime_sha256": "b" * 64},
            }
        ],
        "tool_events": [],
        "work_item_events": [],
        "trace_events": [],
        "external_payloads": [],
    }
    args = collector._parser().parse_args([])

    findings, gaps = collector._derive_findings(report, args)
    report["findings"] = findings
    report["evidence_gaps"] = gaps
    report["warnings"] = []

    assert "runtime_fingerprint_mismatch" in {
        finding["code"] for finding in findings
    }
    assert collector._verdict(report) == "FAIL"


def test_collector_verdict_fails_on_authoritative_current_terminal_failure() -> None:
    report = {
        "slack_record": {"status": "failed"},
        "slack_attempts": [{"status": "failed"}],
        "agent_runs": [{"id": 1, "status": "failed"}],
        "findings": [],
        "evidence_gaps": [],
        "warnings": [],
    }

    assert collector._verdict(report) == "FAIL"

    direct_report = {
        "slack_record": {},
        "slack_attempts": [],
        "agent_runs": [{"id": 1, "status": "failed"}],
        "findings": [],
        "evidence_gaps": [],
        "warnings": [],
    }
    assert collector._verdict(direct_report) == "FAIL"


def test_collector_verdict_uses_latest_completed_attempt_over_historical_failure() -> None:
    report = {
        "slack_record": {},
        "slack_attempts": [
            {"status": "failed"},
            {"status": "completed"},
        ],
        "agent_runs": [{"id": 1, "status": "failed"}],
        "findings": [],
        "evidence_gaps": [],
        "warnings": [],
    }

    assert collector._verdict(report) == "PASS"


def test_collector_verdict_does_not_pass_current_blocked_or_unknown_status() -> None:
    blocked = {
        "slack_record": {"status": "blocked"},
        "findings": [],
        "evidence_gaps": [],
        "warnings": [],
    }
    unknown = {
        "slack_record": {"status": "provider_deferred"},
        "findings": [],
        "evidence_gaps": [],
        "warnings": [],
    }

    assert collector._verdict(blocked) == "PARTIAL"
    assert collector._verdict(unknown) == "PARTIAL"


def test_collector_flags_missing_generic_provider_execution(tmp_path: Path) -> None:
    slack_db = tmp_path / "slack.db"
    kba_db = tmp_path / "kba.db"
    _slack_database(slack_db, provider="calendar", tool_name="read_calendar", called=False)
    _kba_database(kba_db)

    slack, attempts, warnings = collector._collect_slack(
        slack_db,
        channel_id="C1",
        thread_ts="100.1",
        request_ts="",
        run_id="",
        latest=False,
    )
    report = {
        "slack_record": slack,
        "slack_attempts": attempts,
        "agent_runs": [],
        "tool_events": [],
        "warnings": warnings,
    }
    args = collector._parser().parse_args(
        [
            "--channel-id",
            "C1",
            "--thread-ts",
            "100.1",
            "--expect-attached-tool",
            "read_calendar",
            "--expect-called-tool",
            "read_calendar",
        ]
    )
    findings, gaps = collector._derive_findings(report, args)

    assert "tools_attached_without_execution_evidence" in {item["code"] for item in findings}
    assert "expected_tool_not_called" in {item["code"] for item in findings}
    assert any("provider receipt" in gap.lower() for gap in gaps)


def test_collector_generalizes_to_non_calendar_provider(tmp_path: Path) -> None:
    slack_db = tmp_path / "slack.db"
    kba_db = tmp_path / "kba.db"
    _slack_database(slack_db, provider="airtable", tool_name="airtable_read_records", called=True)
    _kba_database(kba_db)

    slack, attempts, warnings = collector._collect_slack(
        slack_db,
        channel_id="C1",
        thread_ts="100.1",
        request_ts="",
        run_id="",
        latest=False,
    )
    report = {
        "slack_record": slack,
        "slack_attempts": attempts,
        "agent_runs": [],
        "tool_events": [],
        "warnings": warnings,
    }
    args = collector._parser().parse_args(
        [
            "--channel-id",
            "C1",
            "--thread-ts",
            "100.1",
            "--expect-attached-tool",
            "airtable_read_records",
            "--expect-provider",
            "airtable",
        ]
    )
    findings, _gaps = collector._derive_findings(report, args)

    codes = {item["code"] for item in findings}
    assert "tools_attached_without_execution_evidence" not in codes
    assert "expected_tool_not_attached" not in codes
    assert "expected_provider_missing" not in codes


def test_collector_accepts_workflow_and_helper_tool_evidence() -> None:
    report = {
        "slack_record": {
            "route": "example_agent",
            "tool_scope": {"selected_names": ["provider_read", "rank_records"]},
            "tool_execution": {
                "workflow_called_names": ["provider_read"],
                "workflow_helper_names": ["rank_records"],
                "provider_receipt_count": 1,
            },
            "receipts": [
                {
                    "provider": "airtable",
                    "operation": "read_records",
                    "status": "success",
                }
            ],
        },
        "slack_attempts": [],
        "agent_runs": [],
        "tool_events": [],
        "work_item_events": [],
        "trace_events": [],
        "external_payloads": [],
    }
    args = collector._parser().parse_args(
        [
            "--expect-called-tool",
            "provider_read",
            "--expect-called-tool",
            "rank_records",
            "--expect-receipt-operation",
            "read_records",
        ]
    )

    findings, _gaps = collector._derive_findings(report, args)

    assert not {
        "expected_tool_not_called",
        "expected_receipt_missing",
    }.intersection({finding["code"] for finding in findings})


def test_collector_reads_fallback_providers_from_nested_telemetry() -> None:
    payload = {
        "retrieval": {
            "provider_attempts": [
                {"provider": "searxng", "status": "unavailable"},
                {"provider": "tavily", "status": "success"},
            ],
            "providers_used": ["tavily", "agents-web-search"],
        }
    }

    assert collector._provider_names(payload) == [
        "searxng",
        "tavily",
        "agents-web-search",
    ]


def test_collector_prefers_direct_terminal_tool_evidence_over_nested_preflight() -> None:
    row = {
        "id": 42,
        "agent_name": "airtable_context_agent",
        "input_summary": "Explain the table layout without reading rows.",
        "status": "error",
        "output_json": json.dumps(
            {
                "orchestrator_preflight": {
                    "request_cache": {
                        "request_tool_scope": {"selected_tool_names": []},
                        "tool_execution": {"model_called_tool_names": []},
                    }
                },
                "tool_execution": {
                    "scope_source": "direct_context_agent_terminal_envelope",
                    "selected_tool_count": 2,
                    "selected_tool_names": [
                        "airtable_get_base_schema",
                        "airtable_read_records",
                    ],
                    "model_tool_call_count": 1,
                    "model_called_tool_names": ["airtable_get_base_schema"],
                    "provider_request_attempt_count": 1,
                    "provider_request_success_count": 1,
                    "provider_receipt_count": 1,
                },
                "tool_receipts": [
                    {
                        "provider": "airtable",
                        "operation": "read_schema",
                        "status": "success",
                        "provider_read": True,
                        "provider_write": False,
                    }
                ],
            }
        ),
    }

    summary = collector._agent_run_summary(row)

    assert summary["request"] == "Explain the table layout without reading rows."
    assert summary["tool_scope"]["selected_names"] == [
        "airtable_get_base_schema",
        "airtable_read_records",
    ]
    assert summary["tool_execution"]["model_called_names"] == [
        "airtable_get_base_schema"
    ]
    assert summary["tool_execution"]["provider_request_success_count"] == 1
    assert summary["receipts"][0]["operation"] == "read_schema"
    assert summary["receipts"][0]["provider_read"] is True
    assert summary["receipts"][0]["provider_write"] is False

    markdown = collector._markdown(
        {
            "verdict": "PASS",
            "slack_record": {},
            "agent_runs": [summary],
            "slack_runtime": {},
            "tool_events": [],
            "trace_events": [],
            "findings": [],
            "evidence_gaps": [],
            "warnings": [],
        }
    )
    assert "Receipt operations: `airtable:read_schema`" in markdown
    assert "Receipt access proof: read=`True`, write=`False`" in markdown


def test_collector_prefers_specialist_request_cache_over_tool_free_preflight() -> None:
    row = {
        "id": 6768,
        "agent_name": "chief_of_staff",
        "input_summary": "Add the approved all-day Calendar reminder.",
        "status": "success",
        "output_json": json.dumps(
            {
                "orchestrator_preflight": {
                    "request_cache": {
                        "tool_execution": {
                            "mode": "tool_free",
                            "model_tool_call_count": 0,
                            "model_called_tool_names": [],
                        }
                    }
                },
                "script_payload": {
                    "request_cache": {
                        "request_tool_scope": {
                            "selected_tool_names": [
                                "read_google_calendar_window",
                                "create_google_calendar_event",
                            ]
                        },
                        "tool_execution": {
                            "mode": "llm_selected_function_tools_failed",
                            "model_tool_call_count": 1,
                            "model_called_tool_names": [
                                "create_google_calendar_event"
                            ],
                            "provider_request_attempt_count": 3,
                            "provider_request_success_count": 3,
                            "provider_receipt_count": 1,
                        },
                    },
                    "tool_receipts": [
                        {
                            "provider": "google_calendar",
                            "operation": "create_calendar_event",
                            "provider_read": True,
                            "provider_write": True,
                            "verification": {"passed": True},
                        }
                    ],
                },
            }
        ),
    }

    summary = collector._agent_run_summary(row)

    assert summary["tool_scope"]["selected_names"] == [
        "read_google_calendar_window",
        "create_google_calendar_event",
    ]
    assert summary["tool_execution"]["mode"] == (
        "llm_selected_function_tools_failed"
    )
    assert summary["tool_execution"]["model_called_names"] == [
        "create_google_calendar_event"
    ]
    assert summary["tool_execution"]["provider_receipt_count"] == 1
    assert summary["receipts"][0]["provider"] == "google_calendar"
    assert summary["receipts"][0]["verification_passed"] is True

    markdown = collector._markdown(
        {
            "verdict": "PARTIAL",
            "slack_record": {
                "tool_execution": {
                    "mode": "model_tools_attached_no_call",
                    "model_called_names": [],
                    "provider_receipt_count": 0,
                }
            },
            "agent_runs": [summary],
            "slack_runtime": {},
            "tool_events": [],
            "trace_events": [],
            "findings": [],
            "evidence_gaps": [],
            "warnings": [],
        }
    )
    assert "Model-called tools: `create_google_calendar_event`" in markdown
    assert "Provider receipt count: `1`" in markdown


def test_missing_expected_receipt_is_reported_as_an_evidence_gap() -> None:
    report = {
        "slack_record": {},
        "slack_attempts": [],
        "agent_runs": [],
        "tool_events": [],
        "work_item_events": [],
        "trace_events": [],
        "external_payloads": [],
    }
    args = collector._parser().parse_args(
        ["--expect-receipt-operation", "read_schema"]
    )

    findings, gaps = collector._derive_findings(report, args)

    assert "expected_receipt_missing" in {
        finding["code"] for finding in findings
    }
    assert gaps == [
        "No durable receipt recorded the expected provider operation 'read_schema'."
    ]


def test_collector_uses_workitem_and_trace_tool_evidence() -> None:
    report = {
        "slack_record": {
            "route": "orchestrator",
            "tool_scope": {"selected_names": ["inspect_receipt", "resume_stage"]},
            "tool_execution": {},
            "receipts": [],
        },
        "slack_attempts": [],
        "agent_runs": [],
        "tool_events": [],
        "work_item_events": [
            {
                "tool_execution": {
                    "workflow_called_tool_names": ["inspect_receipt"],
                },
                "receipts": [
                    {
                        "provider": "local_state",
                        "operation": "inspect_stage_receipts",
                        "status": "success",
                    }
                ],
                "providers": ["local_state"],
            }
        ],
        "trace_events": [
            {
                "tool_execution": {
                    "workflow_called_helper_names": ["resume_stage"],
                },
                "receipts": [],
                "providers": [],
            }
        ],
        "external_payloads": [],
    }
    args = collector._parser().parse_args(
        [
            "--expect-called-tool",
            "inspect_receipt",
            "--expect-called-tool",
            "resume_stage",
            "--expect-provider",
            "local_state",
            "--expect-receipt-operation",
            "inspect_stage_receipts",
        ]
    )

    findings, _gaps = collector._derive_findings(report, args)

    assert not {
        "expected_tool_not_called",
        "expected_provider_missing",
        "expected_receipt_missing",
    }.intersection({finding["code"] for finding in findings})


def test_followup_projection_is_not_a_persistence_defect_when_attempts_exist() -> None:
    report = {
        "slack_record": {
            "thread_ts": "100.1",
            "request_ts": "100.2",
            "continuity": {"current_request": "explain the prior result"},
            "tool_scope": {},
            "tool_execution": {},
            "receipts": [],
        },
        "slack_attempts": [{"attempt_run_id": "attempt-root"}],
        "agent_runs": [],
        "tool_events": [],
        "work_item_events": [],
        "trace_events": [],
        "external_payloads": [],
    }
    args = collector._parser().parse_args([])

    findings, gaps = collector._derive_findings(report, args)

    assert "mutable_thread_record" not in {finding["code"] for finding in findings}
    assert not any("original Slack attempt" in gap for gap in gaps)
