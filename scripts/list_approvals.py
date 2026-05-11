"""List local Keystone approval queue items."""

from __future__ import annotations

import argparse
import json

from keystone_agents.reporting import (
    render_markdown_table,
    render_operator_feedback_question,
    sensitive_text_summary,
)
from keystone_agents.schemas.approval import ApprovalQueueItem, ApprovalQueueStatus
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="List Keystone approval queue items.")
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLite URL. Defaults to DATABASE_URL or local DB.",
    )
    parser.add_argument(
        "--status",
        choices=[status.value for status in ApprovalQueueStatus] + ["all"],
        default=ApprovalQueueStatus.PENDING.value,
        help="Approval status to list. Defaults to pending.",
    )
    parser.add_argument("--object-type", default=None, help="Optional queue object type filter.")
    parser.add_argument("--source-agent", default=None, help="Optional source agent filter.")
    parser.add_argument("--json", action="store_true", help="Print sanitized JSON.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = SQLiteStore(args.database_url or database_url_from_env())
    status = None if args.status == "all" else args.status
    items = store.list_approval_items(
        status=status,
        object_type=args.object_type,
        source_agent=args.source_agent,
    )

    if args.json:
        print(
            json.dumps(
                [_approval_item_export_dict(item) for item in items],
                ensure_ascii=True,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(_render_approvals_markdown(items, status_label=args.status))
    return 0


def _approval_item_export_dict(item: ApprovalQueueItem) -> dict[str, object]:
    payload = item.model_dump(mode="json", exclude={"draft_text"})
    payload["draft_text_summary"] = sensitive_text_summary(item.draft_text)
    return payload


def _render_approvals_markdown(
    items: list[ApprovalQueueItem],
    *,
    status_label: str,
) -> str:
    lines = [
        "# Keystone Approval Queue",
        "",
        f"- Status filter: {status_label}",
        f"- Items: {len(items)}",
        "- Full draft text is omitted from this export.",
        "",
    ]
    if not items:
        lines.append(f"No {status_label} approvals found.")
        return "\n".join(lines)
    rows = [
        [
            item.id,
            item.approval_status.value,
            item.object_type.value,
            item.object_id or "",
            item.source_agent,
            item.reviewer or "",
            item.title,
            item.summary,
            ", ".join(item.risk_flags) if item.risk_flags else "none",
            _feedback_question(item),
            sensitive_text_summary(item.draft_text),
        ]
        for item in items
    ]
    lines.append(
        render_markdown_table(
            [
                "ID",
                "Status",
                "Object",
                "Object ID",
                "Agent",
                "Reviewer",
                "Title",
                "Summary",
                "Risk Flags",
                "Feedback Question",
                "Draft Text",
            ],
            rows,
        )
    )
    return "\n".join(lines)


def _feedback_question(item: ApprovalQueueItem) -> str:
    request = item.metadata.get("operator_feedback_request")
    if not isinstance(request, dict):
        return "none"
    return render_operator_feedback_question(request, max_tags=4)


if __name__ == "__main__":
    raise SystemExit(main())
