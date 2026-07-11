"""Derive one slide copy and verify a reversible Gmail attachment-draft lifecycle."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from keystone_agents.config import require_cli_live_confirmation, with_cli_environment
from keystone_agents.context_env import context_env_value
from keystone_agents.gmail_triage.draft_actions import (
    GMAIL_DRAFT_ATTACHMENT_RECIPIENT_ENV,
    GMAIL_TEST_DRAFT_MARKER,
    delete_approved_gmail_test_draft,
    execute_approved_gmail_draft_attachment_action,
)
from keystone_agents.tools.gmail_tool import GmailTool
from keystone_agents.tools.internal_data_tools import (
    presentation_delete_test_artifact_local_impl,
    presentation_extract_slide_copy_local_impl,
)

DEFAULT_OUTPUT = Path("artifacts/test-pack/gmail-slide-attachment-lifecycle.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("relative_path")
    parser.add_argument("--slide-number", type=int, default=2)
    parser.add_argument("--approval-reference", default="")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--live-provider-write", action="store_true")
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    return parser


def execute_gmail_slide_attachment_lifecycle(
    *,
    relative_path: str,
    slide_number: int,
    account: str,
    recipient: str,
    suffix: str,
    approval_reference: str,
    live: bool,
    gmail: GmailTool | None = None,
) -> dict[str, object]:
    """Create, read, update, and clean one draft plus its derived slide copy."""

    provider = gmail or GmailTool(live=live)
    marker = f"{GMAIL_TEST_DRAFT_MARKER} {suffix}"
    artifact: dict[str, Any] = {}
    created: dict[str, Any] = {}
    updated: dict[str, Any] = {}
    draft_cleanup: dict[str, Any] = {}
    artifact_cleanup: dict[str, Any] = {}
    draft_id = ""
    failure = ""
    cleanup_errors: list[str] = []
    try:
        artifact = presentation_extract_slide_copy_local_impl(
            relative_path,
            slide_number,
            output_format="png",
            output_name=f"KBA_TEST_SLIDE-{suffix}-slide-{slide_number}.png",
            approval_reference=f"{approval_reference}:derive",
            live=live,
        )
        artifact_path = str(artifact.get("artifact_path") or "")
        if live and not _verification_passed(artifact):
            raise RuntimeError("Derived slide copy did not pass verification.")
        created = execute_approved_gmail_draft_attachment_action(
            provider,
            to=recipient,
            subject=f"{marker} Clinical AI slide review",
            body=f"{marker}\nAttached is a non-destructive slide copy for review.",
            attachment_path=artifact_path,
            expected_account=account,
            approval_reference=f"{approval_reference}:create-draft",
        )
        draft_id = str(created.get("draft_id") or "")
        if not live:
            return _payload(
                "preview",
                account=account,
                recipient=recipient,
                artifact=artifact,
                created=created,
            )
        if not draft_id or not _verification_passed(created):
            raise RuntimeError("Attachment-draft create did not pass provider verification.")
        updated = execute_approved_gmail_draft_attachment_action(
            provider,
            to=recipient,
            subject=f"{marker} Clinical AI slide review updated",
            body=f"{marker}\nUpdated note; the same verified slide copy remains attached.",
            attachment_path=artifact_path,
            expected_account=account,
            approval_reference=f"{approval_reference}:update-draft",
            draft_id=draft_id,
        )
        if not _verification_passed(updated) or str(updated.get("draft_id") or "") != draft_id:
            raise RuntimeError("Attachment-draft update did not preserve and verify identity.")
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if live and draft_id:
            try:
                draft_cleanup = delete_approved_gmail_test_draft(
                    provider,
                    draft_id=draft_id,
                    expected_account=account,
                    approval_reference=f"{approval_reference}:delete-draft",
                )
            except Exception as exc:
                cleanup_errors.append(f"draft cleanup: {type(exc).__name__}: {exc}")
        artifact_path = str(artifact.get("artifact_path") or "")
        if live and artifact_path:
            try:
                artifact_cleanup = presentation_delete_test_artifact_local_impl(
                    artifact_path,
                    approval_reference=f"{approval_reference}:delete-artifact",
                    live=True,
                )
            except Exception as exc:
                cleanup_errors.append(f"artifact cleanup: {type(exc).__name__}: {exc}")

    passed = bool(
        not failure
        and not cleanup_errors
        and _verification_passed(artifact)
        and _verification_passed(created)
        and _verification_passed(updated)
        and _verification_passed(draft_cleanup)
        and _verification_passed(artifact_cleanup)
        and str(created.get("draft_id") or "") == str(updated.get("draft_id") or "")
    )
    return _payload(
        "passed" if passed else "failed",
        account=account,
        recipient=recipient,
        artifact=artifact,
        created=created,
        updated=updated,
        draft_cleanup=draft_cleanup,
        artifact_cleanup=artifact_cleanup,
        failure=failure,
        cleanup_errors=cleanup_errors,
    )


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    live = bool(args.live_provider_write and not args.dry_run)
    if args.live_provider_write:
        require_cli_live_confirmation(
            dry_run=args.dry_run,
            live_flag=True,
            flag_name="--live-provider-write",
            live_action="creating, modifying, and deleting one marked attachment draft",
        )
    elif not args.dry_run:
        raise SystemExit("--no-dry-run requires --live-provider-write.")
    account = _required_context_value(
        "KEYSTONE_GMAIL_DRAFT_ACCOUNT",
        fallback_key="KNI_BUSINESS_AGENTS_GMAIL_DRAFT_ACCOUNT",
    )
    recipient = _required_context_value(GMAIL_DRAFT_ATTACHMENT_RECIPIENT_ENV)
    suffix = uuid4().hex[:10]
    approval = args.approval_reference.strip() or f"gmail-slide-{suffix}"
    result = execute_gmail_slide_attachment_lifecycle(
        relative_path=args.relative_path,
        slide_number=args.slide_number,
        account=account,
        recipient=recipient,
        suffix=suffix,
        approval_reference=approval,
        live=live,
    )
    _write_receipt(args.output, result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in {"passed", "preview"} else 1


def _required_context_value(key: str, *, fallback_key: str = "") -> str:
    value = context_env_value(key, "").strip()
    if not value and fallback_key:
        value = context_env_value(fallback_key, "").strip()
    if not value:
        raise SystemExit(f"Missing required private runtime setting: {key}.")
    return value


def _verification_passed(result: dict[str, Any]) -> bool:
    verification = result.get("verification")
    return bool(isinstance(verification, dict) and verification.get("passed"))


def _payload(
    status: str,
    *,
    account: str,
    recipient: str,
    artifact: dict[str, Any],
    created: dict[str, Any],
    updated: dict[str, Any] | None = None,
    draft_cleanup: dict[str, Any] | None = None,
    artifact_cleanup: dict[str, Any] | None = None,
    failure: str = "",
    cleanup_errors: list[str] | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "openai_requests": 0,
        "live_search": False,
        "sent": False,
        "send_enabled": False,
        "account_configured": bool(account),
        "recipient_configured": bool(recipient),
        "artifact": _safe_receipt(artifact),
        "create": _safe_receipt(created),
        "update": _safe_receipt(updated or {}),
        "draft_cleanup": _safe_receipt(draft_cleanup or {}),
        "artifact_cleanup": _safe_receipt(artifact_cleanup or {}),
        "failure": failure,
        "cleanup_errors": cleanup_errors or [],
    }


def _safe_receipt(result: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "status",
        "operation",
        "draft_id",
        "artifact_path",
        "attachment_filename",
        "attachment_size",
        "attachment_sha256",
        "derived_copy_created",
        "parent_modified",
        "verification",
        "sent",
        "send_enabled",
    }
    return {key: value for key, value in result.items() if key in allowed}


def _write_receipt(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
