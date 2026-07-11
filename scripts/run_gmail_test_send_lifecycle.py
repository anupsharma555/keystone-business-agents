#!/usr/bin/env python3
"""Create, revise, send, and verify one exact marked Gmail validation draft."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from uuid import uuid4

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.context_env import context_env_value
from keystone_agents.gmail_triage.draft_actions import (
    GMAIL_TEST_DRAFT_MARKER,
    GMAIL_TEST_EMAIL_MARKER,
    delete_approved_gmail_test_draft,
    execute_approved_gmail_draft_action,
    send_approved_gmail_test_draft,
)
from keystone_agents.tools.gmail_tool import GmailTool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--to", default="")
    parser.add_argument("--expected-account", default="")
    parser.add_argument("--approval-reference", required=True)
    parser.add_argument("--send-number", type=int, default=1)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/gmail-test-send-lifecycle.json"),
    )
    parser.add_argument("--live-gmail", action="store_true")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    if args.live_gmail:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-gmail",
            live_action="sending one exact marked Gmail validation draft",
        )
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-gmail.")

    live = bool(args.live_gmail and not args.dry_run)
    recipient = args.to.strip() or os.getenv("KEYSTONE_GMAIL_TEST_SEND_RECIPIENT", "").strip()
    account = args.expected_account.strip() or context_env_value(
        "KNI_BUSINESS_AGENTS_GMAIL_DRAFT_ACCOUNT",
        context_env_value("GMAIL_ACCOUNT", ""),
    ).strip()
    if not recipient or not account:
        raise SystemExit(
            "Gmail test send requires private runtime recipient and expected-account settings."
        )
    suffix = uuid4().hex[:10]
    subject = (
        f"{GMAIL_TEST_EMAIL_MARKER} {GMAIL_TEST_DRAFT_MARKER} "
        f"operational validation {suffix}"
    )
    initial_body = (
        f"{GMAIL_TEST_EMAIL_MARKER} {GMAIL_TEST_DRAFT_MARKER}\n\n"
        "This is a synthetic Keystone Business Agents delivery validation message.\n\n"
        "Sincerely,\nAnup"
    )
    revised_body = (
        f"{GMAIL_TEST_EMAIL_MARKER} {GMAIL_TEST_DRAFT_MARKER}\n\n"
        "This revised synthetic message verifies draft modification and delivery.\n\n"
        "Sincerely,\nAnup"
    )
    gmail = GmailTool(live=live)
    draft_id = ""
    sent = False
    create_result: dict[str, object] = {}
    update_result: dict[str, object] = {}
    send_result: dict[str, object] = {}
    cleanup_result: dict[str, object] = {}
    failure = ""
    cleanup_failure = ""
    try:
        create_result = execute_approved_gmail_draft_action(
            gmail,
            to=recipient,
            subject=subject,
            body=initial_body,
            expected_account=account,
            approval_reference=f"{args.approval_reference}:create",
        )
        draft_id = str(create_result.get("draft_id") or "")
        if not live:
            payload = _result_payload(create=create_result)
            payload.update(status="dry-run", openai_requests=0, provider_send_count=0)
            _write_result_atomic(args.output, payload)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        if not draft_id or not _verified(create_result):
            raise RuntimeError("Marked Gmail validation draft creation did not verify.")
        update_result = execute_approved_gmail_draft_action(
            gmail,
            to=recipient,
            subject=subject,
            body=revised_body,
            expected_account=account,
            approval_reference=f"{args.approval_reference}:update",
            draft_id=draft_id,
        )
        if not _verified(update_result):
            raise RuntimeError("Marked Gmail validation draft update did not verify.")
        send_result = send_approved_gmail_test_draft(
            gmail,
            draft_id=draft_id,
            expected_account=account,
            approval_reference=f"{args.approval_reference}:send",
            send_number=args.send_number,
        )
        sent = bool(send_result.get("sent") and _verified(send_result))
        if not sent:
            raise RuntimeError("Marked Gmail validation draft send did not verify.")
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            if live and draft_id and not sent and gmail.draft_exists(draft_id):
                cleanup_result = delete_approved_gmail_test_draft(
                    gmail,
                    draft_id=draft_id,
                    expected_account=account,
                    approval_reference=f"{args.approval_reference}:cleanup",
                )
        except Exception as exc:
            cleanup_failure = f"{type(exc).__name__}: {exc}"

    payload = _result_payload(
        create=create_result,
        update=update_result,
        send=send_result,
        cleanup=cleanup_result,
    )
    payload["status"] = "passed" if sent and not failure else "failed"
    payload["provider_send_count"] = 1 if sent else 0
    payload["openai_requests"] = 0
    payload["failure"] = failure
    payload["cleanup_failure"] = cleanup_failure
    _write_result_atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "passed" else 1


def _verified(result: dict[str, object]) -> bool:
    verification = result.get("verification")
    return bool(isinstance(verification, dict) and verification.get("passed"))


def _result_payload(**results: dict[str, object]) -> dict[str, object]:
    return {
        "marker": GMAIL_TEST_EMAIL_MARKER,
        **{
            name: {
                key: result.get(key)
                for key in (
                    "status",
                    "operation",
                    "draft_id",
                    "message_id",
                    "thread_id",
                    "approval_reference",
                    "send_number",
                    "sent",
                    "send_enabled",
                    "verification",
                )
                if key in result
            }
            for name, result in results.items()
            if result
        },
    }


def _write_result_atomic(path: Path, result: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
