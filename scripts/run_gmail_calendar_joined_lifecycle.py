#!/usr/bin/env python3
"""Select one bounded Gmail event and verify reversible Calendar creation."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from keystone_agents.calendar_actions import (
    EmailCalendarCandidate,
    extract_email_calendar_candidate,
)
from keystone_agents.config import load_settings
from keystone_agents.tools.gmail_tool import GmailTool
from keystone_agents.tools.google_calendar_tool import (
    GoogleCalendarTool,
    create_google_calendar_event_impl,
    delete_google_calendar_event_impl,
)

TEST_MARKER = "KBA_TEST_EVENT"
DEFAULT_QUERY = 'newer_than:30d {meeting event appointment calendar "due date" deadline}'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--max-messages", type=int, default=10)
    parser.add_argument("--calendar-id", default="primary")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/test-pack/gmail-calendar-joined-live.json"),
    )
    return parser


def _hash(value: str) -> str:
    return hashlib.sha256(str(value or "").encode()).hexdigest()[:12]


def _select_candidate(
    gmail: GmailTool,
    *,
    query: str,
    max_messages: int,
) -> tuple[dict[str, Any], EmailCalendarCandidate, int]:
    summaries = gmail.search_message_summaries(query=query, max_results=max_messages)
    inspected = 0
    for summary in summaries:
        message_id = str(summary.get("message_id") or summary.get("id") or "")
        if not message_id:
            continue
        message = gmail.get_message(message_id)
        inspected += 1
        candidate = extract_email_calendar_candidate(
            subject=str(message.get("subject") or ""),
            body=str(message.get("normalized_body") or message.get("body") or ""),
            sender_email=str(message.get("from") or ""),
            recipient_text=str(message.get("to") or ""),
        )
        if candidate.complete:
            return message, candidate, inspected
    raise RuntimeError("No uniquely parseable future Gmail event was found in the bounded search.")


def execute_joined_lifecycle(
    *,
    gmail: GmailTool,
    calendar: GoogleCalendarTool,
    query: str,
    max_messages: int,
    calendar_id: str,
) -> dict[str, Any]:
    suffix = uuid4().hex[:10]
    approval = f"operator-command:l174-01:{suffix}"
    event_id = ""
    failure = ""
    create: dict[str, Any] = {}
    delete: dict[str, Any] = {}
    message: dict[str, Any] = {}
    candidate: EmailCalendarCandidate | None = None
    inspected = 0
    try:
        message, candidate, inspected = _select_candidate(
            gmail,
            query=query,
            max_messages=max_messages,
        )
        title = f"{TEST_MARKER} {suffix} {candidate.title}"[:240]
        create = create_google_calendar_event_impl(
            title,
            candidate.start_date,
            start_time=candidate.start_time,
            end_time=candidate.end_time,
            description=f"{TEST_MARKER} joined Gmail-to-Calendar validation; no invitations sent.",
            calendar_id=calendar_id,
            approval_reference=f"{approval}:create",
            live=True,
            tool=calendar,
        )
        event_id = str(create.get("event_id") or "")
        if not event_id or not create.get("verification", {}).get("passed"):
            raise RuntimeError("Joined Calendar create did not pass provider read-back.")
    except Exception as exc:
        failure = str(exc)
    finally:
        if event_id:
            try:
                delete = delete_google_calendar_event_impl(
                    event_id,
                    calendar_id=calendar_id,
                    approval_reference=f"{approval}:delete",
                    live=True,
                    tool=calendar,
                )
            except Exception as exc:
                failure = f"{failure}; cleanup failed: {exc}".strip("; ")

    delete_verified = bool(delete.get("verification", {}).get("passed"))
    passed = bool(
        not failure
        and candidate is not None
        and candidate.complete
        and create.get("verification", {}).get("passed")
        and delete_verified
        and delete.get("verification", {}).get("event_absent_after")
    )
    return {
        "status": "pass" if passed else "partial",
        "failure": failure,
        "scenario": "gmail_event_to_calendar_joined_lifecycle",
        "openai_requests": 0,
        "gmail": {
            "query_hash": _hash(query),
            "messages_inspected": inspected,
            "message_id_hash": _hash(str(message.get("id") or "")) if message else "",
            "thread_id_hash": _hash(str(message.get("threadId") or "")) if message else "",
            "raw_body_persisted": False,
            "writes": 0,
        },
        "extracted_event": {
            "subject_hash": candidate.source_subject_hash if candidate else "",
            "start_date": candidate.start_date if candidate else "",
            "start_time": candidate.start_time if candidate else "",
            "end_time": candidate.end_time if candidate else "",
            "all_day": candidate.all_day if candidate else True,
            "attendee_count": len(candidate.attendee_hashes) if candidate else 0,
            "attendee_hashes": list(candidate.attendee_hashes) if candidate else [],
            "blockers": list(candidate.blockers) if candidate else [],
        },
        "calendar": {
            "calendar_id": calendar_id,
            "event_id_present": bool(event_id),
            "create_verified": bool(create.get("verification", {}).get("passed")),
            "delete_verified": delete_verified,
            "event_absent_after": bool(
                delete.get("verification", {}).get("event_absent_after")
            ),
            "invitations_sent": False,
        },
        "safety": {
            "marked_test_event": True,
            "gmail_modified": False,
            "send_enabled": False,
            "calendar_cleanup_attempted": bool(event_id),
        },
    }


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> int:
    args = build_parser().parse_args()
    if args.max_messages < 1 or args.max_messages > 10:
        raise SystemExit("--max-messages must be from 1 to 10.")
    load_settings(force_dotenv=True)
    payload = execute_joined_lifecycle(
        gmail=GmailTool(live=True),
        calendar=GoogleCalendarTool(live=True),
        query=args.query,
        max_messages=args.max_messages,
        calendar_id=args.calendar_id,
    )
    _write_atomic(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
