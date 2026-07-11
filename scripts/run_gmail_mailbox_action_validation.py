#!/usr/bin/env python3
"""Validate one natural Gmail state ask against a real marked provider message."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from keystone_agents.agents.gmail_triage import build_gmail_mailbox_action_agent
from keystone_agents.config import load_settings
from keystone_agents.context_env import context_env_value
from keystone_agents.execution_identity import create_validation_execution_identity
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.email_triage import GmailMailboxActionPlan
from keystone_agents.tools.gmail_tool import GmailTool

MODEL = "gpt-5.4-mini"
MAX_REQUESTS = 1
MAX_BUDGET_USD = 0.05
MARKER = "KBA_TEST_EMAIL"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=MAX_REQUESTS)
    parser.add_argument("--budget-usd", type=float, default=MAX_BUDGET_USD)
    parser.add_argument("--query", default=f'"{MARKER}"')
    parser.add_argument("--approval-reference", required=True)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/gmail-mailbox-action-live.json"),
    )
    return parser


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def _select_message(gmail: GmailTool, query: str) -> dict[str, Any]:
    candidates = gmail.search_message_summaries(max_results=5, query=query)
    marked = [
        item
        for item in candidates
        if MARKER in str(item.get("subject") or "")
        or MARKER in str(item.get("snippet") or "")
    ]
    if not marked:
        raise RuntimeError("No real marked Gmail test message matched the bounded query.")
    selected = marked[0]
    message_id = str(selected.get("id") or "").strip()
    if not message_id:
        raise RuntimeError("Selected Gmail test message lacks provider identity.")
    return gmail.get_message(message_id)


def _expected_operations(initial_labels: set[str]) -> list[str]:
    return ["unstar", "star"] if "STARRED" in initial_labels else ["star", "unstar"]


def _prompt(initial_labels: set[str]) -> str:
    return (
        "Operator request: For the already selected exact real Gmail validation message, "
        "temporarily toggle its starred state, verify the change, and then restore its "
        "original starred state. Do not send, draft, archive, label, trash, search, or "
        "change any other message. The selected message is currently "
        f"{'starred' if 'STARRED' in initial_labels else 'not starred'}. "
        "Return the exact ordered mailbox operations only; Python owns provider identity, "
        "approval, execution, read-back, and cleanup."
    )


def main() -> int:
    args = _parser().parse_args()
    if args.model != MODEL or args.max_openai_requests != MAX_REQUESTS:
        raise SystemExit("This validation requires exactly one gpt-5.4-mini request.")
    if args.budget_usd <= 0 or args.budget_usd > MAX_BUDGET_USD:
        raise SystemExit("Budget must be greater than zero and at most $0.05.")
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"
    load_settings(force_dotenv=True)
    gmail = GmailTool(live=True)
    selected = _select_message(gmail, args.query)
    message_id = str(selected["id"])
    thread_id = str(selected.get("threadId") or "")
    initial_labels = set(str(value) for value in selected.get("labelIds") or [])
    expected = _expected_operations(initial_labels)
    if args.preflight_only:
        payload = {
            "status": "pass",
            "scenario": "gmail_mailbox_action_preflight",
            "real_provider_message": True,
            "message_id_sha256": _hash(message_id),
            "thread_id_sha256": _hash(thread_id),
            "expected_operation_count": len(expected),
            "openai_requests": 0,
            "provider_writes": 0,
            "email_sent": False,
        }
        _write_atomic(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    identity = create_validation_execution_identity(
        scenario="gmail_mailbox_action",
        route="gmail_triage",
    )
    result = run_typed_sdk_agent(
        agent=build_gmail_mailbox_action_agent(model=args.model),
        typed_input=_prompt(initial_labels),
        output_type=GmailMailboxActionPlan,
        live=True,
        workflow_name="Keystone Gmail mailbox action validation",
        trace_metadata=identity.trace_metadata(),
        tracing_disabled=True,
        trace_include_sensitive_data=False,
        max_turns=1,
    )
    plan = result.final_output
    observed_requests = int(result.usage.get("requests") or 0)
    plan_passed = (
        plan.operations == expected
        and not plan.label
        and plan.approval_reference_required
        and not plan.send_enabled
    )
    receipts: list[dict[str, Any]] = []
    account = context_env_value(
        "KNI_BUSINESS_AGENTS_GMAIL_DRAFT_ACCOUNT",
        context_env_value("GMAIL_ACCOUNT", ""),
    ).strip()
    if plan_passed:
        for operation in plan.operations:
            receipt = gmail.modify_message_state(
                message_id,
                operation,
                expected_account=account,
                approval_reference=f"{args.approval_reference}:{operation}",
            )
            receipts.append(
                {
                    "operation": operation,
                    "verification_passed": bool(receipt["verification"]["passed"]),
                }
            )
    final_labels = set(
        str(value) for value in gmail.get_message(message_id).get("labelIds") or []
    )
    final_state_restored = ("STARRED" in final_labels) == ("STARRED" in initial_labels)
    checks = {
        "one_request": observed_requests == 1,
        "plan_matches_request": plan_passed,
        "provider_operations_verified": len(receipts) == 2
        and all(item["verification_passed"] for item in receipts),
        "final_state_restored": final_state_restored,
        "no_send": plan.send_enabled is False and plan.sent is False,
    }
    payload = {
        "status": "pass" if all(checks.values()) else "partial",
        "scenario": "gmail_mailbox_action",
        "model": args.model,
        "requests": observed_requests,
        "request_ceiling": args.max_openai_requests,
        "budget_usd": args.budget_usd,
        "usage": result.usage,
        "cost": result.cost,
        "budget_guard": result.budget_guard,
        "execution_identity": identity.receipt(),
        "message_id_sha256": _hash(message_id),
        "thread_id_sha256": _hash(thread_id),
        "plan": plan.model_dump(mode="json"),
        "receipts": receipts,
        "checks": checks,
        "message_body_persisted": False,
        "email_sent": False,
    }
    _write_atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
