"""Run one bounded Google Calendar event create/update/delete lifecycle."""

from __future__ import annotations

import argparse
import json
from typing import Any
from uuid import uuid4

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.tools.google_calendar_tool import (
    GoogleCalendarTool,
    create_google_calendar_event_impl,
    delete_google_calendar_event_impl,
    update_google_calendar_event_impl,
)

CALENDAR_TEST_MARKER = "KBA_TEST_EVENT"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--calendar-id", default="primary")
    parser.add_argument("--start-date", default="2026-11-04")
    parser.add_argument("--updated-date", default="2026-11-05")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--live-google-calendar", action="store_true")
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    if args.live_google_calendar:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-google-calendar",
            live_action=(
                "creating, modifying, verifying, and deleting one marked "
                "Google Calendar event"
            ),
        )
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-google-calendar.")

    suffix = uuid4().hex[:10]
    approval = args.approval_reference.strip() or f"anu-211-{suffix}"
    result = execute_google_calendar_test_lifecycle(
        suffix=suffix,
        calendar_id=args.calendar_id,
        start_date=args.start_date,
        updated_date=args.updated_date,
        approval_reference=approval,
        live=bool(args.live_google_calendar and not args.dry_run),
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in {"passed", "dry-run"} else 1


def execute_google_calendar_test_lifecycle(
    *,
    suffix: str,
    calendar_id: str,
    start_date: str,
    updated_date: str,
    approval_reference: str,
    live: bool,
    tool: GoogleCalendarTool | None = None,
) -> dict[str, Any]:
    title = f"{CALENDAR_TEST_MARKER} {suffix} paper due date"
    updated_title = f"{CALENDAR_TEST_MARKER} {suffix} revised paper due date"
    event_id = ""
    receipts: dict[str, Any] = {}
    failure = ""
    calendar = tool or GoogleCalendarTool(live=live)
    try:
        created = create_google_calendar_event_impl(
            title,
            start_date,
            description=f"{CALENDAR_TEST_MARKER} {suffix} submit final paper",
            calendar_id=calendar_id,
            approval_reference=f"{approval_reference}:create",
            live=live,
            tool=calendar,
        )
        receipts["create"] = _receipt(created)
        event_id = str(created.get("event_id") or "")
        if not live:
            return {
                "status": "dry-run",
                "openai_requests": 0,
                "event_id": event_id,
                "receipts": receipts,
            }
        if not event_id or not _verified(created):
            raise RuntimeError("Calendar create did not pass read-back verification.")

        updated = update_google_calendar_event_impl(
            event_id,
            title=updated_title,
            start_date=updated_date,
            description=f"{CALENDAR_TEST_MARKER} {suffix} submit revised final paper",
            calendar_id=calendar_id,
            approval_reference=f"{approval_reference}:update",
            live=True,
            tool=calendar,
        )
        receipts["update"] = _receipt(updated)
        if not _verified(updated):
            raise RuntimeError("Calendar update did not pass read-back verification.")
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if live and event_id:
            try:
                deleted = delete_google_calendar_event_impl(
                    event_id,
                    calendar_id=calendar_id,
                    approval_reference=f"{approval_reference}:delete",
                    live=True,
                    tool=calendar,
                )
                receipts["delete"] = _receipt(deleted)
                if not _verified(deleted) and not failure:
                    failure = "RuntimeError: Calendar delete absence check failed."
            except Exception as exc:
                cleanup_failure = f"{type(exc).__name__}: {exc}"
                failure = f"{failure}; cleanup {cleanup_failure}" if failure else cleanup_failure

    return {
        "status": "failed" if failure else "passed",
        "openai_requests": 0,
        "event_id": event_id,
        "failure": failure,
        "receipts": receipts,
    }


def _verified(result: dict[str, Any]) -> bool:
    verification = result.get("verification")
    return bool(isinstance(verification, dict) and verification.get("passed"))


def _receipt(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "status",
            "operation",
            "calendar_id",
            "event_id",
            "title",
            "start_date",
            "description_present",
            "approval_reference",
            "verification",
            "send_enabled",
        )
        if key in result
    }


if __name__ == "__main__":
    raise SystemExit(main())
