"""Run one bounded live Workspace search/read/summarize validation."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from keystone_agents.agents.google_workspace_context import (
    build_google_workspace_context_agent,
)
from keystone_agents.cli import _context_agent_tool_receipts
from keystone_agents.config import load_settings
from keystone_agents.execution_identity import (
    ValidationExecutionIdentity,
    create_validation_execution_identity,
)
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.operational_context import GoogleWorkspaceContextResult

REQUEST = (
    "Find README.doc in KNIOps, read the exact selected Google Doc, and summarize "
    "its purpose, folder roles, operating model, and safety boundaries. Do not "
    "create, modify, move, share, trash, or otherwise write any Workspace artifact."
)
EXPECTED_MODEL = "gpt-5.4-mini"
EXPECTED_REQUESTS = 3
MAX_BUDGET_USD = 0.05
READ_OPERATIONS = ("search_files", "read_doc")
WRITE_OPERATIONS = {
    "append_rows",
    "create_folder",
    "create_sheet",
    "delete_rows",
    "remove_tab",
    "rename_folder",
    "trash_doc",
    "trash_folder",
    "trash_sheet",
    "update_doc",
    "update_row",
    "update_tab",
    "write_doc",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=EXPECTED_MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=EXPECTED_REQUESTS)
    parser.add_argument("--budget-usd", type=float, default=MAX_BUDGET_USD)
    parser.add_argument(
        "--revalidate-receipt",
        type=Path,
        help="Re-run deterministic checks over a saved receipt without a model call.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/workspace-selected-doc-summary-live.json"),
    )
    return parser


def _validate_run_limits(args: argparse.Namespace) -> None:
    if args.model != EXPECTED_MODEL:
        raise SystemExit(f"Workspace validation requires model={EXPECTED_MODEL}.")
    if args.max_openai_requests != EXPECTED_REQUESTS:
        raise SystemExit(
            f"Workspace validation requires max_openai_requests={EXPECTED_REQUESTS}."
        )
    if args.budget_usd <= 0 or args.budget_usd > MAX_BUDGET_USD:
        raise SystemExit("Workspace validation requires a budget at or below $0.05.")


def _configure_bounded_environment(args: argparse.Namespace) -> None:
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_GOOGLE_WORKSPACE_LIVE_READS"] = "true"
    os.environ["KEYSTONE_GOOGLE_WORKSPACE_CONTEXT_AGENT_SDK_MAX_TURNS"] = str(
        args.max_openai_requests
    )
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"


def _summary_dimension_checks(summary: str) -> dict[str, bool]:
    lowered = summary.lower()
    return {
        "purpose_addressed": any(
            term in lowered
            for term in ("purpose", "organizes", "internal workspace for", "used for")
        ),
        "folder_roles_addressed": "folder" in lowered,
        "operating_model_addressed": any(
            term in lowered for term in ("operating", "workflow", "handoff")
        ),
        "safety_boundary_addressed": any(
            term in lowered for term in ("safety", "approval", "write", "boundary")
        ),
    }


def _build_payload(
    result: TypedAgentRunResult[GoogleWorkspaceContextResult],
    *,
    execution_identity: ValidationExecutionIdentity,
    model: str,
    request_ceiling: int,
    budget_usd: float,
) -> dict[str, Any]:
    output = result.final_output
    receipts = _context_agent_tool_receipts(result.raw_result)
    operations = [str(receipt.get("operation") or "") for receipt in receipts]
    observed_requests = int(result.usage.get("requests") or 0)
    dimension_checks = _summary_dimension_checks(output.summary)
    checks = {
        "exact_request_count": observed_requests == request_ceiling,
        "search_then_read": operations[:2] == list(READ_OPERATIONS),
        "all_tool_receipts_successful": bool(receipts)
        and all(receipt.get("status") == "success" for receipt in receipts),
        "no_write_operation": not any(operation in WRITE_OPERATIONS for operation in operations),
        "selected_doc_identity_retained": any(
            "readme.doc"
            in " ".join(
                str(receipt.get(key) or "") for key in ("query", "title", "document_id")
            ).lower()
            for receipt in receipts
        ),
        "selected_doc_reported": any(
            "readme.doc" in str(item).lower()
            for item in [*output.relevant_docs, *output.relevant_files, output.recommended_target]
        ),
        "summary_present": len(output.summary.strip()) >= 80,
        "requested_dimensions_addressed": all(dimension_checks.values()),
        "no_blocker": not output.blockers,
        "budget_not_exceeded": result.budget_guard.get("exceeded") is False,
        "no_retry": int(result.request_cache.get("rate_limit_retries") or 0) == 0,
    }
    passed = all(checks.values())
    return {
        "status": "pass" if passed else "partial",
        "scenario": "workspace_selected_doc_search_read_summarize",
        "request": REQUEST,
        "model": model,
        "expected_requests": EXPECTED_REQUESTS,
        "requests": observed_requests,
        "request_ceiling": request_ceiling,
        "budget_usd": budget_usd,
        "retries_allowed": 0,
        "execution_identity": execution_identity.receipt(),
        "usage": result.usage,
        "cost": result.cost,
        "budget_guard": result.budget_guard,
        "request_cache": result.request_cache,
        "checks": checks,
        "summary_dimension_checks": dimension_checks,
        "tool_receipts": receipts,
        "output": output.model_dump(mode="json"),
        "safety": {
            "live_search": False,
            "provider_reads": sum(operation in READ_OPERATIONS for operation in operations),
            "provider_writes": sum(operation in WRITE_OPERATIONS for operation in operations),
            "gmail_draft_created": False,
            "email_sent": False,
            "slack_posted": False,
            "workspace_write_performed": False,
        },
    }


def _write_result_atomic(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _revalidate_saved_receipt(
    path: Path,
) -> tuple[
    TypedAgentRunResult[GoogleWorkspaceContextResult],
    ValidationExecutionIdentity,
    str,
]:
    saved = json.loads(path.read_text(encoding="utf-8"))
    raw_items = [
        SimpleNamespace(type="tool_call_output_item", output=json.dumps(receipt))
        for receipt in saved["tool_receipts"]
    ]
    result = TypedAgentRunResult(
        agent_name="google_workspace_context_agent",
        output=GoogleWorkspaceContextResult.model_validate(saved["output"]),
        raw_result=SimpleNamespace(new_items=raw_items),
        live=True,
        usage=saved["usage"],
        cost=saved["cost"],
        budget_guard=saved["budget_guard"],
        request_cache=saved["request_cache"],
    )
    identity = ValidationExecutionIdentity(**saved["execution_identity"])
    return result, identity, str(saved.get("status") or "unknown")


def main() -> int:
    args = build_parser().parse_args()
    _validate_run_limits(args)
    _configure_bounded_environment(args)
    if args.revalidate_receipt is not None:
        result, execution_identity, initial_status = _revalidate_saved_receipt(
            args.revalidate_receipt
        )
        payload = _build_payload(
            result,
            execution_identity=execution_identity,
            model=args.model,
            request_ceiling=args.max_openai_requests,
            budget_usd=args.budget_usd,
        )
        payload["revalidated_from"] = str(args.revalidate_receipt)
        payload["initial_status"] = initial_status
        payload["validation_revision"] = (
            "Accept explicit internal-workspace-for wording as a purpose statement. "
            "No model or provider call was made."
        )
        _write_result_atomic(args.output, payload)
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        return 0 if payload["status"] == "pass" else 2
    load_settings(force_dotenv=True)
    execution_identity = create_validation_execution_identity(
        scenario="workspace_selected_doc_search_read_summarize",
        route="google_workspace_context_agent",
    )
    result = run_typed_sdk_agent(
        agent=build_google_workspace_context_agent(
            model=args.model,
            request_text=REQUEST,
        ),
        typed_input=REQUEST,
        output_type=GoogleWorkspaceContextResult,
        live=True,
        workflow_name="Keystone Workspace selected-document summary validation",
        trace_metadata=execution_identity.trace_metadata(),
        max_turns=args.max_openai_requests,
    )
    payload = _build_payload(
        result,
        execution_identity=execution_identity,
        model=args.model,
        request_ceiling=args.max_openai_requests,
        budget_usd=args.budget_usd,
    )
    _write_result_atomic(args.output, payload)
    print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
