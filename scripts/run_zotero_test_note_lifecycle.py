"""Run one bounded Zotero test-note create/update/delete lifecycle."""

from __future__ import annotations

import argparse
import json
from uuid import uuid4

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.tools.zotero_context_tools import (
    ZOTERO_TEST_NOTE_MARKER,
    zotero_delete_test_note_impl,
    zotero_write_test_note_impl,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-id", default="")
    parser.add_argument("--library-type", choices=("user", "group"), default="user")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--live-zotero", action="store_true")
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    if args.live_zotero:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-zotero",
            live_action="creating, modifying, and deleting one marked Zotero test note",
        )
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-zotero.")

    suffix = uuid4().hex[:10]
    approval_reference = args.approval_reference.strip() or f"anu-200-{suffix}"
    created_html = f"<p>{ZOTERO_TEST_NOTE_MARKER} {suffix} created for validation</p>"
    updated_html = f"<p>{ZOTERO_TEST_NOTE_MARKER} {suffix} modified and verified</p>"
    live = bool(args.live_zotero and not args.dry_run)
    item_key = ""
    create_result: dict[str, object] = {}
    update_result: dict[str, object] = {}
    delete_result: dict[str, object] = {}
    try:
        create_result = zotero_write_test_note_impl(
            created_html,
            library_id=args.library_id,
            library_type=args.library_type,
            approval_reference=f"{approval_reference}:create",
            operation="create",
            live=live,
        )
        item_key = str(create_result.get("item_key") or "")
        if not live:
            print(json.dumps({"create": _receipt(create_result)}, indent=2, sort_keys=True))
            return 0
        if not item_key or not _verification_passed(create_result):
            raise RuntimeError("Zotero test-note create did not pass read-back verification.")

        update_result = zotero_write_test_note_impl(
            updated_html,
            item_key=item_key,
            library_id=args.library_id,
            library_type=args.library_type,
            approval_reference=f"{approval_reference}:update",
            operation="update",
            live=True,
        )
        if not _verification_passed(update_result):
            raise RuntimeError("Zotero test-note update did not pass read-back verification.")
    finally:
        if live and item_key:
            delete_result = zotero_delete_test_note_impl(
                item_key,
                library_id=args.library_id,
                library_type=args.library_type,
                approval_reference=f"{approval_reference}:delete",
                live=True,
            )

    result = {
        "status": (
            "passed"
            if _verification_passed(create_result)
            and _verification_passed(update_result)
            and _verification_passed(delete_result)
            else "failed"
        ),
        "openai_requests": 0,
        "item_key": item_key,
        "create": _receipt(create_result),
        "update": _receipt(update_result),
        "delete": _receipt(delete_result),
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "passed" else 1


def _verification_passed(result: dict[str, object]) -> bool:
    verification = result.get("verification")
    return bool(isinstance(verification, dict) and verification.get("passed"))


def _receipt(result: dict[str, object]) -> dict[str, object]:
    return {
        key: result.get(key)
        for key in (
            "status",
            "operation",
            "item_key",
            "parent_item_key",
            "required_marker",
            "approval_reference",
            "before",
            "after",
            "verification",
            "send_enabled",
        )
        if key in result
    }


if __name__ == "__main__":
    raise SystemExit(main())
