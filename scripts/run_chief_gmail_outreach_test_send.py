#!/usr/bin/env python3
"""Run a two-request Chief/Gmail/Outreach flow and one approved test send."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from keystone_agents.agents.chief_of_staff import run_chief_of_staff_sdk
from keystone_agents.config import load_settings
from keystone_agents.gmail_triage.draft_actions import (
    GMAIL_TEST_DRAFT_MARKER,
    GMAIL_TEST_EMAIL_MARKER,
    delete_approved_gmail_test_draft,
    execute_approved_gmail_draft_action,
    send_approved_gmail_test_draft,
)
from keystone_agents.schemas.outreach import OutreachDraft
from keystone_agents.tools.gmail_tool import GmailTool
from scripts.run_outreach_selected_gmail_draft_lifecycle import (
    EXPECTED_MODEL,
    _account_and_recipient,
    _approved_context,
    _draft_checks,
    _run_model,
    _select_synthetic_thread,
    _typed_input,
)

MAX_REQUESTS = 2
MAX_BUDGET_USD = 0.10


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=EXPECTED_MODEL)
    parser.add_argument("--max-openai-requests", type=int, default=MAX_REQUESTS)
    parser.add_argument("--budget-usd", type=float, default=MAX_BUDGET_USD)
    parser.add_argument("--approval-reference", required=True)
    parser.add_argument("--send-number", type=int, default=2)
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/chief-gmail-outreach-test-send-live.json"),
    )
    return parser


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def _chief_request(selected: dict[str, Any]) -> dict[str, Any]:
    return {
        "request": (
            "Bounded live SDK smoke validation. Coordinate one already selected exact real "
            "Gmail KBA_TEST_EMAIL thread. Gmail Triage owns the provider thread and draft "
            "action. Outreach Composer should write one concise receipt-confirmation reply "
            "using only the supplied synthetic thread facts and approved style. After both "
            "specialists are ready, the deterministic Gmail test-send gate may create, "
            "update, and send the exact marked draft to the explicitly approved test "
            "recipient. Return the ordered ownership and safety plan. Do not call tools, "
            "search, post to Slack, broaden recipients, or claim that Chief sends directly."
        ),
        "provider_call_context": {
            "gmail": {
                "selected_exact_thread": True,
                "thread_ref": str(selected["thread_id"]),
                "message_ref": str(selected["message_id"]),
                "synthetic_markers_verified": True,
                "recipient_scope": "configured_exact_test_recipient",
            }
        },
        "approval_reference": "operator-approved:chief-gmail-outreach-test-send",
    }


def _chief_checks(output: Any) -> dict[str, bool]:
    payload = output.model_dump(mode="json")
    text = json.dumps(payload, sort_keys=True).lower()
    return {
        "gmail_ownership": "gmail" in text and "draft" in text,
        "outreach_ownership": "outreach" in text,
        "approval_retained": output.approval_required is True,
        "send_disabled": output.send_enabled is False,
        "no_slack_post": output.slack_post_allowed is False,
    }


def main() -> int:
    args = _parser().parse_args()
    if args.model != EXPECTED_MODEL or args.max_openai_requests != MAX_REQUESTS:
        raise SystemExit("This validation requires exactly two gpt-5.4-mini requests.")
    if args.budget_usd <= 0 or args.budget_usd > MAX_BUDGET_USD:
        raise SystemExit("Budget must be greater than zero and at most $0.10.")
    os.environ["KEYSTONE_AGENT_RUN_BUDGET_USD"] = str(args.budget_usd)
    os.environ["KEYSTONE_LIVE_MODEL_MAX_RETRIES"] = "0"
    os.environ["KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES"] = "0"
    load_settings(force_dotenv=True)
    provider = GmailTool(live=True)
    selected = _select_synthetic_thread(provider)
    account, recipient = _account_and_recipient(provider, selected)
    if recipient.casefold() != "att44409@gmail.com":
        raise RuntimeError("approved_test_recipient_mismatch")
    if args.preflight_only:
        payload = {
            "status": "pass",
            "scenario": "chief_gmail_outreach_test_send_preflight",
            "real_provider_thread": True,
            "recipient_verified": True,
            "subject_marker_verified": True,
            "body_marker_verified": True,
            "openai_requests": 0,
            "provider_writes": 0,
            "email_sent": False,
        }
        _write_atomic(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0

    chief_result = run_chief_of_staff_sdk(
        _chief_request(selected),
        live=True,
        model=args.model,
        quality_mode="fast",
        force_sdk_interpretation=True,
        include_specialist_tools=False,
        attach_tools=False,
    )
    chief_checks = _chief_checks(chief_result.final_output)
    chief_requests = int(chief_result.usage.get("requests") or 0)
    if chief_requests != 1 or not all(chief_checks.values()):
        payload = {
            "status": "partial",
            "stage": "chief",
            "requests": chief_requests,
            "checks": chief_checks,
            "usage": chief_result.usage,
            "cost": chief_result.cost,
            "email_sent": False,
        }
        _write_atomic(args.output, payload)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 2

    context = _approved_context(selected)
    outreach_result = _run_model(
        _typed_input(selected, context),
        context=context,
        model=args.model,
        trace_metadata={"scenario": "chief_gmail_outreach_test_send"},
    )
    draft = OutreachDraft.model_validate(outreach_result.final_output)
    outreach_checks = _draft_checks(draft, allowed_source_ids=context.allowed_source_ids)
    outreach_requests = int(outreach_result.usage.get("requests") or 0)
    total_requests = chief_requests + outreach_requests
    estimated_cost = float(chief_result.cost.get("estimated_usd") or 0.0) + float(
        outreach_result.cost.get("estimated_usd") or 0.0
    )
    draft_id = ""
    create_receipt: dict[str, Any] = {}
    update_receipt: dict[str, Any] = {}
    send_receipt: dict[str, Any] = {}
    cleanup_receipt: dict[str, Any] = {}
    failure = ""
    suffix = uuid4().hex[:10]
    marker = f"{GMAIL_TEST_EMAIL_MARKER} {GMAIL_TEST_DRAFT_MARKER}"
    approval = args.approval_reference
    try:
        if outreach_requests != 1 or total_requests != MAX_REQUESTS:
            raise RuntimeError("api_request_ceiling_mismatch")
        if estimated_cost > args.budget_usd or not all(outreach_checks.values()):
            raise RuntimeError("outreach_or_budget_acceptance_failed")
        create_receipt = execute_approved_gmail_draft_action(
            provider,
            to=recipient,
            subject=f"{marker} Chief coordinated response {suffix}",
            body=f"{marker}\n\nDraft prepared for exact provider update verification.",
            expected_account=account,
            approval_reference=f"{approval}:create",
        )
        draft_id = str(create_receipt.get("draft_id") or "")
        update_receipt = execute_approved_gmail_draft_action(
            provider,
            to=recipient,
            subject=f"{marker} Chief coordinated response {suffix}",
            body=f"{marker}\n\n{draft.email_body.strip()}",
            expected_account=account,
            approval_reference=f"{approval}:update",
            draft_id=draft_id,
        )
        send_receipt = send_approved_gmail_test_draft(
            provider,
            draft_id=draft_id,
            expected_account=account,
            approval_reference=f"{approval}:send",
            send_number=args.send_number,
        )
    except Exception as exc:
        failure = str(exc)
    finally:
        if draft_id and not send_receipt:
            try:
                cleanup_receipt = delete_approved_gmail_test_draft(
                    provider,
                    draft_id=draft_id,
                    expected_account=account,
                    approval_reference=f"{approval}:cleanup",
                )
            except Exception as exc:
                failure = f"{failure}; cleanup failed: {exc}".strip("; ")
    checks = {
        **{f"chief_{key}": value for key, value in chief_checks.items()},
        **{f"outreach_{key}": value for key, value in outreach_checks.items()},
        "exact_request_count": total_requests == MAX_REQUESTS,
        "budget_not_exceeded": estimated_cost <= args.budget_usd,
        "draft_create_verified": bool(create_receipt.get("verification", {}).get("passed")),
        "draft_update_verified": bool(update_receipt.get("verification", {}).get("passed")),
        "test_send_verified": bool(send_receipt.get("verification", {}).get("passed")),
        "provider_send_count_one": send_receipt.get("status") == "sent"
        and send_receipt.get("sent") is True,
    }
    payload = {
        "status": "pass" if not failure and all(checks.values()) else "partial",
        "scenario": "chief_gmail_outreach_test_send",
        "model": args.model,
        "requests": total_requests,
        "request_ceiling": args.max_openai_requests,
        "estimated_usd": estimated_cost,
        "budget_usd": args.budget_usd,
        "chief_usage": chief_result.usage,
        "outreach_usage": outreach_result.usage,
        "checks": checks,
        "failure": failure,
        "provider": {
            "create_verified": bool(create_receipt.get("verification", {}).get("passed")),
            "update_verified": bool(update_receipt.get("verification", {}).get("passed")),
            "send_verified": bool(send_receipt.get("verification", {}).get("passed")),
            "draft_absent_after_send": bool(
                send_receipt.get("verification", {}).get("draft_absent_after_send")
            ),
            "cleanup_absent": bool(cleanup_receipt.get("verification", {}).get("absent")),
        },
        "safety": {
            "recipient_persisted": False,
            "draft_copy_persisted": False,
            "real_selected_thread": True,
            "exact_test_recipient_verified": True,
            "ordinary_send_enabled": False,
            "test_email_sent": bool(send_receipt),
            "slack_posted": False,
            "live_search": False,
        },
    }
    _write_atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
