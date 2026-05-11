"""Capture sanitized Gmail outreach examples into local SQLite."""

from __future__ import annotations

import argparse
import json
from typing import Any

from keystone_agents.config import require_cli_live_confirmation
from keystone_agents.outreach_examples import outreach_example_document_from_gmail_thread
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.gmail_tool import GmailAPIError, GmailConfigurationError, GmailTool


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read successful Gmail threads into a sanitized local example library."
    )
    parser.add_argument("--live-gmail", action="store_true", help="Required for Gmail reads.")
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Defaults to true. Capture requires --no-dry-run and --live-gmail.",
    )
    parser.add_argument(
        "--label-filter",
        required=True,
        help="Required Gmail label query, e.g. Keystone/Library/Success.",
    )
    parser.add_argument("--max-threads", type=int, default=10, help="Maximum threads to read.")
    parser.add_argument("--database-url", default=None, help="SQLite URL for local storage.")
    parser.add_argument(
        "--approved-for-drafting",
        action="store_true",
        help="Mark sanitized examples as approved for drafting retrieval.",
    )
    parser.add_argument("--company-type", default="", help="Optional company type metadata.")
    parser.add_argument(
        "--opportunity-type", default="", help="Optional opportunity type metadata."
    )
    parser.add_argument(
        "--outreach-stage",
        default="initial_outreach",
        choices=["initial_outreach", "follow_up", "reply", "meeting_request", "other"],
    )
    parser.add_argument(
        "--outcome",
        default="success",
        choices=[
            "positive_reply",
            "meeting_booked",
            "referral",
            "useful_conversation",
            "success",
            "unknown",
        ],
    )
    parser.add_argument("--template-id", default=None)
    parser.add_argument("--style-profile-id", default=None)
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def _thread_id(ref: dict[str, Any]) -> str:
    return str(ref.get("threadId") or ref.get("thread_id") or ref.get("id") or "")


def capture_examples(args: argparse.Namespace) -> dict[str, Any]:
    """Run read-only Gmail capture and store sanitized documents locally."""

    if not args.live_gmail:
        raise SystemExit(
            "Read-only Gmail outreach example capture requires --live-gmail and --no-dry-run."
        )
    try:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=args.live_gmail,
            flag_name="--live-gmail",
            live_action="read-only Gmail outreach example capture",
        )
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    if not args.label_filter.strip():
        raise SystemExit("--label-filter is required.")
    if args.max_threads < 1:
        raise SystemExit("--max-threads must be at least 1.")

    gmail = GmailTool(live=True)
    store = SQLiteStore(args.database_url)
    try:
        refs = gmail.list_threads_by_label_filter(
            label_filter=args.label_filter,
            max_results=args.max_threads,
        )
        documents = []
        for ref in refs:
            thread_id = _thread_id(ref)
            if not thread_id:
                continue
            thread = gmail.get_thread(thread_id)
            document = outreach_example_document_from_gmail_thread(
                thread,
                source_label=args.label_filter,
                outreach_stage=args.outreach_stage,
                outcome=args.outcome,
                company_type=args.company_type,
                opportunity_type=args.opportunity_type,
                template_id=args.template_id,
                style_profile_id=args.style_profile_id,
                approved_for_drafting=args.approved_for_drafting,
                metadata={"gmail_thread_ref": {"thread_id_hash_only": True}},
            )
            store.save_outreach_example_document(document)
            documents.append(document)
    except (GmailAPIError, GmailConfigurationError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    return {
        "status": "captured",
        "read_only": True,
        "label_filter": args.label_filter,
        "captured_count": len(documents),
        "approved_for_drafting": bool(args.approved_for_drafting),
        "raw_body_included": False,
        "send_enabled": False,
        "gmail_mutations_enabled": False,
        "examples": [
            {
                "example_id": document.example_id,
                "source_thread_id_hash": document.source_thread_id_hash,
                "approved_for_drafting": document.approved_for_drafting,
            }
            for document in documents
        ],
    }


def main() -> int:
    args = build_parser().parse_args()
    payload = capture_examples(args)
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            "\n".join(
                [
                    "Keystone outreach example capture",
                    f"Status: {payload['status']}",
                    f"Read only: {str(payload['read_only']).lower()}",
                    f"Captured: {payload['captured_count']}",
                    f"Approved for drafting: {str(payload['approved_for_drafting']).lower()}",
                ]
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
