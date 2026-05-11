"""Run the first end-to-end Keystone dry-run pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from keystone_agents.schemas.approval import ApprovalState
from keystone_agents.workflows import pipeline_markdown_report, run_keystone_pipeline

APPROVAL_STATES = [
    state.value
    for state in ApprovalState
    if state
    not in {
        ApprovalState.APPROVED_FOR_EXTERNAL_USE,
        ApprovalState.APPROVED_FOR_SEND,
    }
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Keystone end-to-end dry-run workflow.")
    parser.add_argument(
        "--email-fixture", required=True, help="Path to a local inbound email fixture."
    )
    parser.add_argument(
        "--company-fixture", default=None, help="Optional local company fixture JSON path."
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use deterministic fixture mode. Defaults to true.",
    )
    parser.add_argument(
        "--sdk", action="store_true", help="Construct specialist SDK agents without running them."
    )
    parser.add_argument("--save", action="store_true", help="Save pipeline artifacts to SQLite.")
    parser.add_argument(
        "--create-outreach-tracking",
        action="store_true",
        help=(
            "With --save, create an initial draft_pending_approval outreach_tracking row "
            "when a draft is saved. Does not send or schedule outreach."
        ),
    )
    parser.add_argument(
        "--ask-feedback",
        action="store_true",
        help="Attach optional structured operator feedback requests to review outputs.",
    )
    parser.add_argument("--database-url", default=None, help="SQLite URL for --save.")
    parser.add_argument(
        "--approval-state",
        choices=APPROVAL_STATES,
        default=ApprovalState.PENDING.value,
        help="Approval state for the opportunity-to-draft gate.",
    )
    parser.add_argument("--markdown", action="store_true", help="Print markdown report.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.dry_run:
        raise SystemExit("Keystone pipeline CLI is dry-run only. Re-run with --dry-run.")
    if args.create_outreach_tracking and not args.save:
        raise SystemExit("--create-outreach-tracking requires --save.")

    result = run_keystone_pipeline(
        email_fixture=Path(args.email_fixture),
        company_fixture=Path(args.company_fixture) if args.company_fixture else None,
        approval_state=args.approval_state,
        dry_run=args.dry_run,
        sdk=args.sdk,
        save=args.save,
        database_url=args.database_url,
        create_outreach_tracking=args.create_outreach_tracking,
        include_operator_feedback_request=args.ask_feedback,
    )
    if args.markdown:
        print(pipeline_markdown_report(result))
    else:
        print(json.dumps(result.model_dump(), ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
