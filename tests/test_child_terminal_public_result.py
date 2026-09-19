from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from keystone_agents.entrypoints import cli_impl
from keystone_agents.entrypoints.cli_impl import _persisted_agent_run_status
from keystone_agents.presentation.public_result import (
    attach_execution_public_result,
    public_result_storage_status,
)
from keystone_agents.storage.sqlite_store import SQLiteStore


@pytest.mark.parametrize(
    ("child_status", "expected_status"),
    [
        ("failed", "failed"),
        ("error", "failed"),
        ("timeout", "failed"),
        ("partial", "partial"),
        ("needs_approval", "needs_approval"),
        ("blocked", "blocked"),
        ("clarification_required", "needs_input"),
        ("needs_input", "needs_input"),
    ],
)
def test_child_terminal_status_cannot_be_promoted_to_parent_success(
    child_status: str,
    expected_status: str,
) -> None:
    payload = {
        "mode": "live_sdk",
        "human_summary": "The parent wrapper says the result is ready.",
        "script_payload": {
            "status": child_status,
            "human_summary": "The child reported a non-success terminal result.",
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == expected_status
    assert result.completion_confirmed is False
    assert result.text == "The child reported a non-success terminal result."
    assert payload["status"] == expected_status
    assert payload["public_result"]["status"] == expected_status
    assert "Result Ready" not in payload["slack_display_title"]


def test_existing_parent_public_success_is_downgraded_by_child_failure() -> None:
    payload = {
        "mode": "live_sdk",
        "script_payload": {
            "status": "failed",
            "output": {
                "failure": {"summary": "The specialist child did not complete."}
            },
        },
        "public_result": {
            "status": "completed",
            "title": "Business Agents Result Ready",
            "text": "The requested work completed.",
            "completion_confirmed": True,
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "failed"
    assert result.completion_confirmed is False
    assert result.text == "The specialist child did not complete."
    assert result.failure_code == "child_failed"
    assert result.failure_summary == "The specialist child did not complete."
    assert payload["status"] == "failed"
    assert payload["slack_display_title"] == "Business Agents Run Failed"


def test_child_failure_text_overrides_actionable_parent_preflight_summary() -> None:
    payload = {
        "mode": "blocked",
        "status": "blocked",
        "human_summary": (
            "Provide the vendor target. Research comes first, and approved findings "
            "are required before outreach drafting."
        ),
        "script_payload": {
            "status": "failed",
            "human_summary": "The downstream provider failed after preflight.",
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "failed"
    assert result.completion_confirmed is False
    assert result.text == "The downstream provider failed after preflight."
    assert payload["status"] == "failed"


def test_failed_child_execution_telemetry_prevents_completion_without_status() -> None:
    payload = {
        "mode": "live_sdk",
        "human_summary": "A stale wrapper summary claimed completion.",
        "script_payload": {
            "execution_telemetry": {"status": "failed"},
            "output": {"summary": "Unvalidated child output."},
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "failed"
    assert result.completion_confirmed is False
    assert result.text == "Business Agents did not complete this request."
    assert result.failure_code == "child_execution_telemetry_failed"
    assert payload["status"] == "failed"


@pytest.mark.parametrize("validator_status", ["rejected", "repair_required"])
def test_unaccepted_final_validator_outcome_prevents_completion(
    validator_status: str,
) -> None:
    payload = {
        "mode": "live_sdk",
        "human_summary": "A stale wrapper summary claimed completion.",
        "script_payload": {
            "request_cache": {
                "decision_ownership": {
                    "validator_outcome": {
                        "status": validator_status,
                        "reason_code": "selected_identity_not_in_provider_evidence",
                    }
                }
            }
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "blocked"
    assert result.completion_confirmed is False
    assert result.text == "Business Agents could not validate a safe completed result."
    assert result.failure_code == "selected_identity_not_in_provider_evidence"
    assert payload["status"] == "blocked"


def test_historical_repair_required_does_not_override_final_validator_acceptance() -> None:
    payload = {
        "mode": "live_sdk",
        "human_summary": "The repaired specialist result is ready.",
        "script_payload": {
            "request_cache": {
                "decision_ownership": {
                    "attempts": [
                        {"validator_outcome": {"status": "repair_required"}}
                    ],
                    "validator_outcome": {"status": "accepted"},
                }
            }
        },
    }

    result = attach_execution_public_result(payload)

    assert result.status == "completed"
    assert result.completion_confirmed is True


@pytest.mark.parametrize(
    ("public_status", "storage_status"),
    [
        ("verified", "success"),
        ("completed", "success"),
        ("recovered", "success"),
        ("partial", "partial"),
        ("needs_approval", "needs_approval"),
        ("needs_input", "needs_input"),
        ("blocked", "blocked"),
        ("failed", "error"),
        ("canceled", "error"),
    ],
)
def test_storage_status_is_derived_from_canonical_public_result(
    public_status: str,
    storage_status: str,
) -> None:
    payload = {
        "status": public_status,
        "title": "Terminal result",
        "text": "Reader-facing terminal result.",
        "completion_confirmed": public_status in {"verified", "completed", "recovered"},
        "recovery_used": public_status == "recovered",
        "recovery_notice": (
            "Recovered from verified evidence." if public_status == "recovered" else ""
        ),
    }

    assert public_result_storage_status(payload) == storage_status
    assert _persisted_agent_run_status({"public_result": payload}) == storage_status


@pytest.mark.parametrize(
    ("child_payload", "expected_public_status", "expected_storage_status"),
    [
        ({"status": "failed"}, "failed", "error"),
        ({"status": "error"}, "failed", "error"),
        ({"status": "timeout"}, "failed", "error"),
        ({"status": "partial"}, "partial", "partial"),
        ({"status": "needs_approval"}, "needs_approval", "needs_approval"),
        ({"status": "blocked"}, "blocked", "blocked"),
        ({"status": "clarification_required"}, "needs_input", "needs_input"),
        ({"status": "needs_input"}, "needs_input", "needs_input"),
        (
            {"execution_telemetry": {"status": "failed"}},
            "failed",
            "error",
        ),
        (
            {
                "request_cache": {
                    "decision_ownership": {
                        "validator_outcome": {
                            "status": "rejected",
                            "reason_code": "candidate_identity_not_verified",
                        }
                    }
                }
            },
            "blocked",
            "blocked",
        ),
    ],
)
def test_direct_child_production_wrapper_persists_canonical_non_success(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
    child_payload: dict[str, object],
    expected_public_status: str,
    expected_storage_status: str,
) -> None:
    payload = {
        "human_summary": "The child returned a bounded terminal result.",
        "output": {"summary": "The child returned a bounded terminal result."},
        **child_payload,
    }
    monkeypatch.setattr(
        cli_impl,
        "run_isolated_child_process",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )
    database_url = f"sqlite:///{tmp_path / 'terminal-child.sqlite'}"

    exit_code = cli_impl._run_ask_script_live(
        "opportunity_scout",
        "Review one bounded opportunity and keep the result read-only.",
        ["unused-child-command"],
        json_output=True,
        manual_plan=None,
        database_url=database_url,
    )

    rendered = json.loads(capsys.readouterr().out)
    stored = SQLiteStore(database_url).fetch_all("agent_runs")
    assert exit_code == (1 if expected_public_status == "failed" else 0)
    assert rendered["public_result"]["status"] == expected_public_status
    assert rendered["public_result"]["completion_confirmed"] is False
    assert rendered["status"] == expected_public_status
    assert stored[0]["status"] == expected_storage_status
