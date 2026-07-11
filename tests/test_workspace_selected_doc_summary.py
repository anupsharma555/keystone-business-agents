from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from keystone_agents.execution_identity import create_validation_execution_identity
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.schemas.operational_context import GoogleWorkspaceContextResult
from scripts.run_workspace_selected_doc_summary import (
    EXPECTED_MODEL,
    EXPECTED_REQUESTS,
    REQUEST,
    _build_payload,
    _configure_bounded_environment,
    _revalidate_saved_receipt,
    _summary_dimension_checks,
    _validate_run_limits,
    _write_result_atomic,
    build_parser,
)


def _tool_output(payload: dict[str, object]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_call_output_item", output=json.dumps(payload))


def _passing_result() -> TypedAgentRunResult[GoogleWorkspaceContextResult]:
    output = GoogleWorkspaceContextResult.model_validate(
        {
            "summary": (
                "README.doc explains the purpose of KNIOps, the roles of its folders, "
                "the operating handoff workflow, and safety boundaries for approved writes."
            ),
            "relevant_folders": ["KNIOps"],
            "relevant_files": ["README.doc"],
            "relevant_docs": ["README.doc"],
            "recommended_target": "KNIOps / README.doc",
            "blockers": [],
        }
    )
    raw_result = SimpleNamespace(
        new_items=[
            _tool_output(
                {
                    "status": "success",
                    "operation": "search_files",
                    "query": "README.doc",
                    "folder_path": "KNIOps",
                    "item_count": 1,
                    "send_enabled": False,
                }
            ),
            _tool_output(
                {
                    "status": "success",
                    "operation": "read_doc",
                    "document_id": "README.doc:provider-id",
                    "title": "README.doc",
                    "char_count": 1096,
                    "truncated": False,
                    "send_enabled": False,
                }
            ),
        ]
    )
    return TypedAgentRunResult(
        agent_name="google_workspace_context_agent",
        output=output,
        raw_result=raw_result,
        live=True,
        usage={"available": True, "requests": 3},
        cost={"available": True, "estimated_usd": 0.02},
        budget_guard={"enforced": True, "exceeded": False, "budget_usd": 0.05},
        request_cache={"rate_limit_retries": 0},
    )


def _identity():
    return create_validation_execution_identity(
        scenario="workspace_selected_doc_search_read_summarize",
        route="google_workspace_context_agent",
        now=datetime(2026, 7, 11, 12, 30, tzinfo=UTC),
        nonce="a1b2c3d4",
    )


def test_runner_defaults_to_three_requests_and_five_cent_budget(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["run_workspace_selected_doc_summary.py"])
    args = build_parser().parse_args()

    assert args.model == EXPECTED_MODEL
    assert args.max_openai_requests == EXPECTED_REQUESTS
    assert args.budget_usd == 0.05
    assert "README.doc" in REQUEST
    _validate_run_limits(args)
    _configure_bounded_environment(args)
    assert args.max_openai_requests == 3


def test_passing_receipt_requires_search_read_summary_and_no_write() -> None:
    payload = _build_payload(
        _passing_result(),
        execution_identity=_identity(),
        model=EXPECTED_MODEL,
        request_ceiling=EXPECTED_REQUESTS,
        budget_usd=0.05,
    )

    assert payload["status"] == "pass"
    assert payload["checks"]["search_then_read"] is True
    assert payload["checks"]["requested_dimensions_addressed"] is True
    assert payload["safety"]["provider_reads"] == 2
    assert payload["safety"]["provider_writes"] == 0
    assert payload["execution_identity"]["run_id"].startswith("kba_workspace_")


def test_summary_accepts_explicit_internal_workspace_purpose_wording() -> None:
    checks = _summary_dimension_checks(
        "KNIOps is Keystone's internal workspace for operations and research, with "
        "clear folder roles, a Slack-to-Drive workflow, and strict safety boundaries."
    )

    assert all(checks.values())


def test_receipt_fails_when_a_write_operation_or_request_overrun_occurs() -> None:
    result = _passing_result()
    result.raw_result.new_items.append(
        _tool_output({"status": "success", "operation": "write_doc"})
    )
    object.__setattr__(result, "usage", {"available": True, "requests": 4})

    payload = _build_payload(
        result,
        execution_identity=_identity(),
        model=EXPECTED_MODEL,
        request_ceiling=EXPECTED_REQUESTS,
        budget_usd=0.05,
    )

    assert payload["status"] == "partial"
    assert payload["checks"]["exact_request_count"] is False
    assert payload["checks"]["no_write_operation"] is False


def test_receipt_is_written_atomically(tmp_path) -> None:
    receipt = tmp_path / "workspace.json"
    _write_result_atomic(receipt, {"status": "pass", "requests": 3})

    assert json.loads(receipt.read_text(encoding="utf-8"))["requests"] == 3
    assert not receipt.with_suffix(".json.tmp").exists()


def test_saved_receipt_can_be_revalidated_without_model_or_provider_call(tmp_path) -> None:
    result = _passing_result()
    identity = _identity()
    payload = _build_payload(
        result,
        execution_identity=identity,
        model=EXPECTED_MODEL,
        request_ceiling=EXPECTED_REQUESTS,
        budget_usd=0.05,
    )
    receipt = tmp_path / "saved.json"
    receipt.write_text(json.dumps(payload), encoding="utf-8")

    restored, restored_identity, initial_status = _revalidate_saved_receipt(receipt)

    assert restored.final_output.recommended_target.startswith("KNIOps")
    assert restored_identity.run_id == identity.run_id
    assert initial_status == "pass"


def test_persisted_live_plan_requires_fresh_approval_with_current_billing_refresh() -> None:
    plan = json.loads(
        Path("artifacts/test-pack/next-live-workspace-selected-doc-plan.json").read_text(
            encoding="utf-8"
        )
    )

    assert plan["approval_status"] == "completed_within_eight_sequential_run_allowance"
    assert plan["model"] == EXPECTED_MODEL
    assert plan["expected_openai_requests"] == EXPECTED_REQUESTS
    assert plan["max_openai_requests"] == EXPECTED_REQUESTS
    assert plan["budget_usd"] == 0.05
    assert plan["provider_writes"] == 0
    assert plan["live_search"] is False
    assert plan["retries_allowed"] == 0
    assert "SDK trace metadata" in plan["execution_identity"]
    assert plan["billing_baseline"]["chrome_billing_refresh"] == "completed"
    assert plan["billing_baseline"]["chrome_billing_credit_balance_usd"] == 2.88
    assert plan["result"]["status"] == "pass"
    assert plan["result"]["openai_requests"] == 3
    assert plan["result"]["provider_reads"] == 2


def test_main_uses_same_execution_identity_for_trace_and_receipt(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    import scripts.run_workspace_selected_doc_summary as runner

    captured: dict[str, object] = {}

    def fake_run_typed_sdk_agent(**kwargs):
        captured.update(kwargs)
        return _passing_result()

    output = tmp_path / "workspace-live.json"
    monkeypatch.setattr(runner, "run_typed_sdk_agent", fake_run_typed_sdk_agent)
    monkeypatch.setattr(runner, "load_settings", lambda **_kwargs: None)
    monkeypatch.setattr(runner, "build_google_workspace_context_agent", lambda **_kwargs: object())
    monkeypatch.setattr(
        "sys.argv",
        ["run_workspace_selected_doc_summary.py", "--output", str(output)],
    )

    assert runner.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert captured["trace_metadata"]["run_id"] == payload["execution_identity"]["run_id"]
    assert captured["trace_metadata"]["case_id"] == payload["execution_identity"]["case_id"]
    assert json.loads(output.read_text(encoding="utf-8")) == payload
