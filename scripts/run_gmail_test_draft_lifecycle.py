"""Run one bounded Gmail test-draft create/read/update/read/delete lifecycle."""

from __future__ import annotations

import argparse
import json
from uuid import uuid4

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.gmail_triage.draft_actions import (
    GMAIL_TEST_DRAFT_MARKER,
    execute_gmail_test_draft_lifecycle,
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
        GmailTool(live=bool(args.live_gmail and not args.dry_run)),
        marker=f"{GMAIL_TEST_DRAFT_MARKER} {suffix}",
        expected_account=args.account.strip(),
        recipient=args.to.strip(),
        approval_reference=approval_reference,
    )
    print(json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True))
    return 0 if result["status"] in {"success", "dry-run"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
