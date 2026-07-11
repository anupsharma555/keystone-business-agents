"""Create, verify, update, and delete one exact marked Slack test message."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.tools.slack_tool import SLACK_TEST_MESSAGE_MARKER, SlackTool

DEFAULT_OUTPUT = Path("artifacts/test-pack/slack-test-message-lifecycle.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live-slack", action="store_true")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    return parser


def execute_slack_test_message_lifecycle(
    *,
    channel: str,
    suffix: str,
    approval_reference: str,
    live: bool,
    slack: SlackTool | None = None,
) -> dict[str, object]:
    """Post, read, update, read, delete, and verify one exact Slack message."""

    provider = slack or SlackTool(live=live, approvals_channel=channel)
    initial = f"{SLACK_TEST_MESSAGE_MARKER} {suffix} initial operational validation"
    revised = f"{SLACK_TEST_MESSAGE_MARKER} {suffix} revised operational validation"
    message_ts = ""
    create: dict[str, Any] = {}
    update: dict[str, Any] = {}
    delete: dict[str, Any] = {}
    recovery: dict[str, Any] = {}
    failure = ""
    cleanup_failure = ""
    try:
        create = provider.post_test_message(
            channel,
            initial,
            approval_reference=f"{approval_reference}:create",
        )
        message_ts = str(create.get("ts") or "")
        if not live:
            return _payload("preview", create=create)
        if not message_ts or not _verified(create):
            raise RuntimeError("Slack test-message create did not pass provider verification.")
        update = provider.update_test_message(
            channel,
            message_ts,
            revised,
            approval_reference=f"{approval_reference}:update",
        )
        if not _verified(update):
            raise RuntimeError("Slack test-message update did not pass provider verification.")
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if live and not message_ts:
            try:
                matches = provider.find_test_messages(channel, initial, limit=20)
                recovery = {
                    "status": "verified" if len(matches) <= 1 else "ambiguous",
                    "matching_message_count": len(matches),
                    "passed": len(matches) <= 1,
                }
                if len(matches) == 1:
                    message_ts = matches[0]
                    recovery["recovered_message"] = True
                elif len(matches) > 1:
                    raise RuntimeError("Slack create recovery found multiple exact test messages.")
            except Exception as exc:
                cleanup_failure = f"recovery {type(exc).__name__}: {exc}"
        if live and message_ts:
            try:
                delete = provider.delete_test_message(
                    channel,
                    message_ts,
                    approval_reference=f"{approval_reference}:delete",
                )
                if not _verified(delete) and not cleanup_failure:
                    cleanup_failure = "Slack test-message absence verification failed."
            except Exception as exc:
                cleanup_failure = f"delete {type(exc).__name__}: {exc}"

    passed = bool(
        not failure
        and not cleanup_failure
        and _verified(create)
        and _verified(update)
        and _verified(delete)
    )
    return _payload(
        "passed" if passed else "failed",
        create=create,
        update=update,
        delete=delete,
        recovery=recovery,
        failure=failure,
        cleanup_failure=cleanup_failure,
    )


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    live = bool(args.live_slack and not args.dry_run)
    if args.live_slack:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-slack",
            live_action="posting, modifying, and deleting one exact marked Slack test message",
        )
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-slack.")
    channel = args.channel.strip() or os.getenv("KEYSTONE_SLACK_TEST_CHANNEL", "").strip()
    if not channel:
        raise SystemExit("Slack test lifecycle requires an exact channel ID.")
    suffix = uuid4().hex[:10]
    approval = args.approval_reference.strip() or f"slack-test-{suffix}"
    result = execute_slack_test_message_lifecycle(
        channel=channel,
        suffix=suffix,
        approval_reference=approval,
        live=live,
    )
    _write_receipt(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in {"passed", "preview"} else 1


def _verified(result: dict[str, Any]) -> bool:
    verification = result.get("verification")
    return bool(isinstance(verification, dict) and verification.get("passed"))


def _payload(status: str, **parts: object) -> dict[str, object]:
    return {
        "status": status,
        "openai_requests": 0,
        "live_search": False,
        "message_present_after": False if status == "passed" else None,
        **{
            key: _safe_receipt(value) if isinstance(value, dict) else value
            for key, value in parts.items()
        },
    }


def _safe_receipt(result: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "status",
        "operation",
        "channel",
        "ts",
        "approval_reference",
        "verification",
        "matching_message_count",
        "passed",
        "recovered_message",
    }
    return {key: value for key, value in result.items() if key in allowed}


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
