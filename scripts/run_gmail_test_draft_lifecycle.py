"""Run one bounded Gmail test-draft create/read/update/read/delete lifecycle."""

from __future__ import annotations

import argparse
import json
from uuid import uuid4

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.gmail_triage.draft_actions import (
    GMAIL_TEST_DRAFT_MARKER,
    delete_approved_gmail_test_draft,
    execute_approved_gmail_draft_action,
)
from keystone_agents.tools.gmail_tool import GmailTool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True, help="Expected authenticated Gmail account.")
    parser.add_argument("--to", required=True, help="Recipient for the test draft; it is not sent.")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--live-gmail", action="store_true")
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


def execute_gmail_test_draft_lifecycle(
    *,
    account: str,
    recipient: str,
    suffix: str,
    approval_reference: str,
    live: bool,
    gmail: GmailTool | None = None,
) -> dict[str, object]:
    """Create, verify, update, verify, and delete one marked test draft."""

    marker = f"{GMAIL_TEST_DRAFT_MARKER} {suffix}"
    provider = gmail or GmailTool(live=live)
    draft_id = ""
    create_result: dict[str, object] = {}
    update_result: dict[str, object] = {}
    delete_result: dict[str, object] = {}
    failure = ""
    try:
        create_result = execute_approved_gmail_draft_action(
            provider,
            to=recipient,
            subject=f"{marker} operational validation",
            body=f"{marker}\nCreated for bounded Gmail draft lifecycle validation.",
            expected_account=account,
            approval_reference=f"{approval_reference}:create",
        )
        draft_id = str(create_result.get("draft_id") or "")
        if not live:
            return {
                "status": "dry-run",
                "openai_requests": 0,
                "sent": False,
                "send_enabled": False,
                "create": _receipt(create_result),
            }
        if not draft_id or not _verification_passed(create_result):
            raise RuntimeError("Gmail test-draft create did not pass read-back verification.")

        update_result = execute_approved_gmail_draft_action(
            provider,
            to=recipient,
            subject=f"{marker} operational validation updated",
            body=f"{marker}\nModified in place and ready for verified cleanup.",
            expected_account=account,
            approval_reference=f"{approval_reference}:update",
            draft_id=draft_id,
        )
        if not _verification_passed(update_result):
            raise RuntimeError("Gmail test-draft update did not pass read-back verification.")
    except Exception as exc:  # Cleanup must still run after a partial live lifecycle.
        failure = str(exc)
    finally:
        if live and draft_id:
            try:
                delete_result = delete_approved_gmail_test_draft(
                    provider,
                    draft_id=draft_id,
                    expected_account=account,
                    approval_reference=f"{approval_reference}:delete",
                )
            except Exception as exc:
                failure = f"{failure}; cleanup failed: {exc}".strip("; ")

    passed = (
        not failure
        and _verification_passed(create_result)
        and _verification_passed(update_result)
        and _verification_passed(delete_result)
    )
    return {
        "status": "passed" if passed else "failed",
        "failure": failure,
        "openai_requests": 0,
        "draft_id": draft_id,
        "sent": False,
        "send_enabled": False,
        "create": _receipt(create_result),
        "update": _receipt(update_result),
        "delete": _receipt(delete_result),
    }


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    if args.live_gmail:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-gmail",
            live_action="creating, modifying, and deleting one marked Gmail test draft",
        )
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-gmail.")

    suffix = uuid4().hex[:10]
    approval_reference = args.approval_reference.strip() or f"gmail-lifecycle-{suffix}"
    result = execute_gmail_test_draft_lifecycle(
        account=args.account.strip(),
        recipient=args.to.strip(),
        suffix=suffix,
        approval_reference=approval_reference,
        live=bool(args.live_gmail and not args.dry_run),
    )
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result["status"] in {"passed", "dry-run"} else 1


def _verification_passed(result: dict[str, object]) -> bool:
    verification = result.get("verification")
    return bool(isinstance(verification, dict) and verification.get("passed"))


def _receipt(result: dict[str, object]) -> dict[str, object]:
    return {
        key: result.get(key)
        for key in (
            "status",
            "operation",
            "draft_id",
            "approval_reference",
            "before",
            "after",
            "verification",
            "sent",
            "send_enabled",
        )
        if key in result
    }


if __name__ == "__main__":
    raise SystemExit(main())
