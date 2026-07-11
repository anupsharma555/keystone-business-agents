"""Run one bounded Zotero test collection/item lifecycle with zero model calls."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from uuid import uuid4

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.tools.zotero_context_tools import (
    ZOTERO_TEST_COLLECTION_MARKER,
    ZOTERO_TEST_ITEM_MARKER,
    zotero_delete_test_collection_impl,
    zotero_delete_test_item_impl,
    zotero_write_test_collection_impl,
    zotero_write_test_item_impl,
)

DEFAULT_OUTPUT = Path("artifacts/test-pack/zotero-test-library-lifecycle.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--library-id", default="")
    parser.add_argument("--library-type", choices=("user", "group"), default="user")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live-zotero", action="store_true")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    if args.live_zotero:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-zotero",
            live_action=(
                "creating, modifying, and deleting one marked Zotero collection "
                "and one marked webpage item"
            ),
        )
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-zotero.")

    suffix = uuid4().hex[:10]
    approval = args.approval_reference.strip() or f"anu-200-library-{suffix}"
    live = bool(args.live_zotero and not args.dry_run)
    collection_key = ""
    item_key = ""
    results: dict[str, dict[str, object]] = {}
    cleanup_errors: list[str] = []
    failure = ""
    try:
        results["create_collection"] = zotero_write_test_collection_impl(
            f"{ZOTERO_TEST_COLLECTION_MARKER} {suffix} created",
            library_id=args.library_id,
            library_type=args.library_type,
            approval_reference=f"{approval}:create-collection",
            live=live,
        )
        collection_key = str(results["create_collection"].get("collection_key") or "")
        if not live:
            payload = _payload("preview", results, collection_key, item_key, cleanup_errors)
            _write_receipt(args.output, payload)
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        _require_pass(results["create_collection"], "collection create")

        results["update_collection"] = zotero_write_test_collection_impl(
            f"{ZOTERO_TEST_COLLECTION_MARKER} {suffix} updated",
            collection_key=collection_key,
            library_id=args.library_id,
            library_type=args.library_type,
            approval_reference=f"{approval}:update-collection",
            operation="update",
            live=True,
        )
        _require_pass(results["update_collection"], "collection update")

        results["create_item"] = zotero_write_test_item_impl(
            f"{ZOTERO_TEST_ITEM_MARKER} {suffix} created",
            collection_key=collection_key,
            tags=["kba-validation", "created"],
            library_id=args.library_id,
            library_type=args.library_type,
            approval_reference=f"{approval}:create-item",
            live=True,
        )
        item_key = str(results["create_item"].get("item_key") or "")
        _require_pass(results["create_item"], "item create")

        results["update_item"] = zotero_write_test_item_impl(
            f"{ZOTERO_TEST_ITEM_MARKER} {suffix} updated",
            item_key=item_key,
            collection_key=collection_key,
            tags=["kba-validation", "updated"],
            library_id=args.library_id,
            library_type=args.library_type,
            approval_reference=f"{approval}:update-item",
            operation="update",
            live=True,
        )
        _require_pass(results["update_item"], "item update")
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if live and item_key:
            try:
                results["delete_item"] = zotero_delete_test_item_impl(
                    item_key,
                    library_id=args.library_id,
                    library_type=args.library_type,
                    approval_reference=f"{approval}:delete-item",
                    live=True,
                )
            except Exception as exc:
                cleanup_errors.append(f"item cleanup: {type(exc).__name__}: {exc}")
        if live and collection_key:
            try:
                results["delete_collection"] = zotero_delete_test_collection_impl(
                    collection_key,
                    library_id=args.library_id,
                    library_type=args.library_type,
                    approval_reference=f"{approval}:delete-collection",
                    live=True,
                )
            except Exception as exc:
                cleanup_errors.append(f"collection cleanup: {type(exc).__name__}: {exc}")

    expected = {
        "create_collection",
        "update_collection",
        "create_item",
        "update_item",
        "delete_item",
        "delete_collection",
    }
    passed = not failure and not cleanup_errors and expected <= set(results)
    passed = passed and all(_verification_passed(results[name]) for name in expected)
    payload = _payload(
        "passed" if passed else "failed",
        results,
        collection_key,
        item_key,
        cleanup_errors,
        failure=failure,
    )
    _write_receipt(args.output, payload)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if passed else 1


def _require_pass(result: dict[str, object], label: str) -> None:
    if not _verification_passed(result):
        raise RuntimeError(f"Zotero {label} did not pass provider read-back verification.")


def _verification_passed(result: dict[str, object]) -> bool:
    verification = result.get("verification")
    return bool(isinstance(verification, dict) and verification.get("passed"))


def _receipt(result: dict[str, object]) -> dict[str, object]:
    allowed = (
        "status",
        "operation",
        "collection_key",
        "item_key",
        "name",
        "title",
        "tags",
        "required_marker",
        "approval_reference",
        "before",
        "after",
        "verification",
        "send_enabled",
    )
    return {key: result.get(key) for key in allowed if key in result}


def _payload(
    status: str,
    results: dict[str, dict[str, object]],
    collection_key: str,
    item_key: str,
    cleanup_errors: list[str],
    *,
    failure: str = "",
) -> dict[str, object]:
    return {
        "status": status,
        "openai_requests": 0,
        "collection_key": collection_key,
        "item_key": item_key,
        "steps": {name: _receipt(result) for name, result in results.items()},
        "failure": failure,
        "cleanup_errors": cleanup_errors,
        "no_send_or_post": True,
    }


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
