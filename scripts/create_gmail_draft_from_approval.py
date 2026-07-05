"""Create a standalone Gmail draft from an approved outreach approval item."""

from __future__ import annotations

import argparse
import json
import os

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.schemas.approval import ApprovalQueueObjectType, ApprovalQueueStatus
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env
from keystone_agents.tools.gmail_tool import GmailTool

DEFAULT_GMAIL_DRAFT_ACCOUNT = "operator@example.com"
GMAIL_DRAFT_ACCOUNT_ENV_KEYS = (
    "KEYSTONE_GMAIL_DRAFT_ACCOUNT",
    "KNI_BUSINESS_AGENTS_GMAIL_DRAFT_ACCOUNT",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create a Gmail draft only after a local approval queue item is approved."
    )
    parser.add_argument("approval_id", help="Approved outreach_draft approval queue item id.")
    parser.add_argument("--to", default=None, help="Recipient email override.")
    parser.add_argument("--subject", default=None, help="Subject override.")
    parser.add_argument("--database-url", default=None, help="SQLite URL.")
    parser.add_argument(
        "--live-gmail",
        action="store_true",
        help="Create the draft in Gmail. Without this, returns a dry-run preview.",
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use dry-run mode. Pass --no-dry-run with --live-gmail for Gmail draft creation.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON.")
    return parser


def _parse_draft_text(draft_text: str) -> tuple[str, str]:
    text = draft_text.strip()
    subject = ""
    if text.lower().startswith("subject:"):
        first, _, rest = text.partition("\n")
        subject = first.split(":", 1)[1].strip()
        text = rest.strip()
    body, _, _linkedin = text.partition("\n\nLinkedIn:")
    return subject, body.strip()


def _target_gmail_draft_account(metadata: dict[str, object]) -> str:
    for key in ("gmail_draft_account", "target_gmail_account"):
        value = str(metadata.get(key) or "").strip()
        if value:
            return value
    for key in GMAIL_DRAFT_ACCOUNT_ENV_KEYS:
        value = os.getenv(key, "").strip()
        if value:
            return value
    return DEFAULT_GMAIL_DRAFT_ACCOUNT


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    if args.live_gmail:
        try:
            require_cli_live_confirmation(
                dry_run=args.dry_run,
                live_flag=True,
                flag_name="--live-gmail",
                live_action="creating a Gmail draft from an approved outreach item",
            )
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-gmail.")

    store = SQLiteStore(args.database_url or database_url_from_env())
    item = store.get_approval_item(args.approval_id)
    if item is None:
        raise SystemExit(f"Approval queue item not found: {args.approval_id}")
    if item.approval_status != ApprovalQueueStatus.APPROVED:
        raise SystemExit(
            f"Approval item {item.id} is {item.approval_status.value}; expected approved."
        )
    if item.object_type != ApprovalQueueObjectType.OUTREACH_DRAFT:
        raise SystemExit(
            f"Approval item {item.id} is {item.object_type.value}, not outreach_draft."
        )
    if not item.draft_text:
        raise SystemExit(f"Approval item {item.id} has no draft text.")
    if (item.metadata or {}).get("outreach_channel") != "email":
        raise SystemExit(
            f"Approval item {item.id} is not an email approval item; no Gmail draft created."
        )

    parsed_subject, parsed_body = _parse_draft_text(item.draft_text)
    metadata = item.metadata or {}
    recipient = (args.to or metadata.get("recipient_email") or "").strip()
    subject = (args.subject or metadata.get("email_subject") or parsed_subject).strip()
    target_account = _target_gmail_draft_account(metadata)
    if not recipient:
        raise SystemExit("Recipient email is required. Pass --to or save recipient_email metadata.")
    if not subject:
        raise SystemExit(
            "Email subject is required. Pass --subject or approve a draft with a subject."
        )
    if not parsed_body:
        raise SystemExit(f"Approval item {item.id} has no email body.")

    result = GmailTool(live=args.live_gmail).create_draft(
        to=recipient,
        subject=subject,
        body=parsed_body,
        expected_account=target_account,
    )
    payload = {
        "approval_id": item.id,
        "approval_status": item.approval_status.value,
        "gmail_result": result,
        "gmail_account": result.get("gmail_account") or target_account,
        "sent": False,
        "send_enabled": False,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(
            f"{result.get('status')}\tapproval={item.id}\t"
            f"draft_id={result.get('draft_id', '')}\tsent=false"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
