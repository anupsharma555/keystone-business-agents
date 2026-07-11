#!/usr/bin/env python3
"""Run a reversible zero-model mailbox-state lifecycle on one real marked email."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.context_env import context_env_value
from keystone_agents.tools.gmail_tool import GmailTool

MARKER = "KBA_TEST_EMAIL"
TEST_LABEL = "KBA_TEST_LABEL"


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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default=f'"{MARKER}"')
    parser.add_argument("--expected-account", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--live-gmail", action="store_true")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--output", type=Path)
    return parser


def _operation(
    gmail: GmailTool,
    message_id: str,
    name: str,
    *,
    expected_account: str,
    approval_reference: str,
    label: str = "",
) -> dict[str, Any]:
    result = gmail.modify_message_state(
        message_id,
        name,  # type: ignore[arg-type]
        label=label,
        expected_account=expected_account,
        approval_reference=f"{approval_reference}:{name}",
    )
    return {
        "operation": name,
        "status": result["status"],
        "verification_passed": bool(result["verification"]["passed"]),
        "provider_write": bool(result["provider_write"]),
        "send_enabled": bool(result["send_enabled"]),
        "sent": bool(result["sent"]),
    }


def execute_lifecycle(
    gmail: GmailTool,
    *,
    query: str,
    expected_account: str,
    approval_reference: str,
) -> dict[str, Any]:
    if not approval_reference.strip():
        raise ValueError("A scoped approval reference is required.")
    candidates = gmail.search_message_summaries(max_results=5, query=query)
    marked = [
        item
        for item in candidates
        if MARKER in str(item.get("subject") or "")
        or MARKER in str(item.get("snippet") or "")
    ]
    if not marked:
        raise RuntimeError("No real Gmail message with the required KBA_TEST_EMAIL marker matched.")
    selected = marked[0]
    message_id = str(selected.get("id") or "").strip()
    if not message_id:
        raise RuntimeError("The selected marked Gmail message has no provider identity.")

    initial = gmail.get_message(message_id)
    initial_labels = set(str(value) for value in initial.get("labelIds") or [])
    receipts: list[dict[str, Any]] = []

    def run(name: str, *, label: str = "") -> None:
        receipts.append(
            _operation(
                gmail,
                message_id,
                name,
                label=label,
                expected_account=expected_account,
                approval_reference=approval_reference,
            )
        )

    try:
        run("add_label", label=TEST_LABEL)
        run("remove_label", label=TEST_LABEL)
        run("mark_unread" if "UNREAD" not in initial_labels else "mark_read")
        run("mark_read" if "UNREAD" not in initial_labels else "mark_unread")
        run("star" if "STARRED" not in initial_labels else "unstar")
        run("unstar" if "STARRED" not in initial_labels else "star")
        run("mark_important" if "IMPORTANT" not in initial_labels else "mark_not_important")
        run("mark_not_important" if "IMPORTANT" not in initial_labels else "mark_important")
        run("archive" if "INBOX" in initial_labels else "unarchive")
        run("unarchive" if "INBOX" in initial_labels else "archive")
        run("trash")
        run("restore")
    finally:
        current_labels = set(
            str(value) for value in gmail.get_message(message_id).get("labelIds") or []
        )
        for label_id, add_name, remove_name in (
            ("INBOX", "unarchive", "archive"),
            ("UNREAD", "mark_unread", "mark_read"),
            ("STARRED", "star", "unstar"),
            ("IMPORTANT", "mark_important", "mark_not_important"),
        ):
            if (label_id in current_labels) != (label_id in initial_labels):
                run(add_name if label_id in initial_labels else remove_name)
        current_labels = set(
            str(value) for value in gmail.get_message(message_id).get("labelIds") or []
        )
        if "TRASH" in current_labels and "TRASH" not in initial_labels:
            run("restore")
        if "TRASH" not in current_labels and "TRASH" in initial_labels:
            run("trash")

    final = gmail.get_message(message_id)
    final_labels = set(str(value) for value in final.get("labelIds") or [])
    state_labels = {"INBOX", "UNREAD", "STARRED", "IMPORTANT", "TRASH"}
    final_state_restored = (initial_labels & state_labels) == (final_labels & state_labels)
    test_label_absent = TEST_LABEL not in final_labels and "Label_test" not in final_labels
    status = "pass" if final_state_restored and test_label_absent else "partial"
    return {
        "schema": "keystone.gmail_mailbox_state_lifecycle.v1",
        "status": status,
        "provider": "gmail",
        "real_provider_message": True,
        "message_id_sha256": _hash(message_id),
        "thread_id_sha256": _hash(str(final.get("threadId") or "")),
        "candidate_count": len(marked),
        "operation_count": len(receipts),
        "receipts": receipts,
        "final_state_restored": final_state_restored,
        "test_label_absent": test_label_absent,
        "openai_requests": 0,
        "email_sent": False,
        "message_body_persisted": False,
    }


@with_cli_environment()
def main() -> None:
    args = _parser().parse_args()
    if args.live_gmail:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-gmail",
            live_action="reversibly modifying one exact marked Gmail message",
        )
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-gmail.")
    live = bool(args.live_gmail and not args.dry_run)
    if live and os.getenv("KEYSTONE_GMAIL_ALLOW_MAILBOX_WRITES", "").lower() != "true":
        raise SystemExit("Live mode requires KEYSTONE_GMAIL_ALLOW_MAILBOX_WRITES=true.")
    account = args.expected_account.strip() or context_env_value(
        "KNI_BUSINESS_AGENTS_GMAIL_DRAFT_ACCOUNT",
        context_env_value("GMAIL_ACCOUNT", ""),
    ).strip()
    payload = execute_lifecycle(
        GmailTool(live=live),
        query=args.query,
        expected_account=account,
        approval_reference=args.approval_reference,
    )
    if args.output:
        _write_atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
