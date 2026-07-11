"""Create and verify one disposable Airtable base for structural validation."""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from keystone_agents.config import with_cli_environment
from keystone_agents.tools.internal_data_tools import _airtable_base_config, _airtable_send

MARKER = "KBA_TEST_BASE"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--approval-reference", default="")
    parser.add_argument(
        "--output",
        default="artifacts/test-pack/airtable-test-base-lifecycle.json",
    )
    return parser


def _marked_name(now: datetime) -> str:
    return f"{MARKER} {now.astimezone(UTC).strftime('%Y%m%dT%H%M%SZ')}"


def _create_payload(*, name: str, workspace_id: str) -> dict[str, Any]:
    return {
        "name": name,
        "workspaceId": workspace_id,
        "tables": [
            {
                "name": "Validation Records",
                "description": "Disposable KBA structural validation table.",
                "fields": [
                    {"name": "Name", "type": "singleLineText"},
                    {"name": "Notes", "type": "multilineText"},
                    {
                        "name": "Amount",
                        "type": "currency",
                        "options": {"precision": 2, "symbol": "$"},
                    },
                    {
                        "name": "Complete",
                        "type": "checkbox",
                        "options": {"color": "greenBright", "icon": "check"},
                    },
                ],
            }
        ],
    }


def _schema_summary(payload: dict[str, Any]) -> dict[str, Any]:
    tables = payload.get("tables") if isinstance(payload, dict) else []
    return {
        "table_count": len(tables) if isinstance(tables, list) else 0,
        "tables": [
            {
                "id": str(table.get("id") or ""),
                "name": str(table.get("name") or ""),
                "field_names": [
                    str(field.get("name") or "")
                    for field in table.get("fields", [])
                    if isinstance(field, dict)
                ],
                "view_names": [
                    str(view.get("name") or "")
                    for view in table.get("views", [])
                    if isinstance(view, dict)
                ],
            }
            for table in (tables if isinstance(tables, list) else [])
            if isinstance(table, dict)
        ],
    }


def run_lifecycle(
    *,
    live: bool,
    approval_reference: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    name = _marked_name(now or datetime.now(UTC))
    workspace_id = str(os.getenv("AIRTABLE_WORKSPACE_ID") or "").strip()
    allow_writes = os.getenv("KEYSTONE_AIRTABLE_ALLOW_TEST_BASE_WRITES") == "true"
    cleanup_mode = str(os.getenv("AIRTABLE_TEST_BASE_CLEANUP_MODE") or "").strip()
    config = _airtable_base_config(base_alias="finance_tax_tracker")
    token = str(config.get("access_token") or "")
    blockers = []
    if live and not allow_writes:
        blockers.append("airtable_test_base_write_gate_disabled")
    if live and not workspace_id:
        blockers.append("airtable_workspace_id_missing")
    if live and not approval_reference.strip():
        blockers.append("approval_reference_missing")
    if live and cleanup_mode != "manual_ui_confirmed":
        blockers.append("manual_ui_cleanup_not_confirmed")
    if live and not token:
        blockers.append("airtable_access_token_missing")
    if blockers:
        return _result(name=name, status="blocked", blockers=blockers)
    if not live:
        preview = _create_payload(name=name, workspace_id="configured-workspace")
        return _result(
            name=name,
            status="dry-run",
            checks={
                "marked_name": name.startswith(MARKER),
                "one_initial_table": len(preview["tables"]) == 1,
                "typed_fields_present": len(preview["tables"][0]["fields"]) == 4,
                "writes_performed": False,
            },
        )

    try:
        created = _airtable_send(
            {
                "method": "POST",
                "url": "https://api.airtable.com/v0/meta/bases",
                "params": {},
                "payload": _create_payload(name=name, workspace_id=workspace_id),
            },
            access_token=token,
        )
    except RuntimeError as exc:
        detail = str(exc).lower()
        blocker = (
            "airtable_structural_write_permission_denied"
            if "invalid_permissions_or_model_not_found" in detail
            else "airtable_test_base_create_failed"
        )
        return _result(name=name, status="blocked", blockers=[blocker])
    base_id = str(created.get("id") or "")
    if not base_id:
        return _result(name=name, status="partial", blockers=["provider_base_id_missing"])
    first_schema = _airtable_send(
        {
            "method": "GET",
            "url": f"https://api.airtable.com/v0/meta/bases/{base_id}/tables",
            "params": {},
        },
        access_token=token,
    )
    second_table = _airtable_send(
        {
            "method": "POST",
            "url": f"https://api.airtable.com/v0/meta/bases/{base_id}/tables",
            "params": {},
            "payload": {
                "name": "Follow-up Records",
                "description": "Second disposable KBA validation table.",
                "fields": [
                    {"name": "Name", "type": "singleLineText"},
                    {"name": "Status", "type": "singleLineText"},
                ],
            },
        },
        access_token=token,
    )
    final_schema = _airtable_send(
        {
            "method": "GET",
            "url": f"https://api.airtable.com/v0/meta/bases/{base_id}/tables",
            "params": {},
        },
        access_token=token,
    )
    first_summary = _schema_summary(first_schema)
    final_summary = _schema_summary(final_schema)
    first_table = first_summary["tables"][0] if first_summary["tables"] else {}
    final_names = {table["name"] for table in final_summary["tables"]}
    checks = {
        "provider_base_id_present": bool(base_id),
        "marked_name": name.startswith(MARKER),
        "initial_table_verified": first_table.get("name") == "Validation Records",
        "typed_fields_verified": {"Name", "Notes", "Amount", "Complete"}.issubset(
            set(first_table.get("field_names") or [])
        ),
        "default_view_verified": bool(first_table.get("view_names")),
        "second_table_created": bool(second_table.get("id")),
        "second_table_verified": "Follow-up Records" in final_names,
        "manual_cleanup_required": True,
    }
    return _result(
        name=name,
        status="provider-write-verified" if all(checks.values()) else "partial",
        base_id=base_id,
        checks=checks,
        schema=final_summary,
        writes_performed=2,
    )


def _result(
    *,
    name: str,
    status: str,
    blockers: list[str] | None = None,
    base_id: str = "",
    checks: dict[str, bool] | None = None,
    schema: dict[str, Any] | None = None,
    writes_performed: int = 0,
) -> dict[str, Any]:
    return {
        "status": status,
        "operation": "airtable_test_base_structural_lifecycle",
        "base_name": name,
        "base_id": base_id,
        "checks": checks or {},
        "schema": schema or {},
        "blockers": blockers or [],
        "writes_performed": writes_performed,
        "cleanup": {
            "required": bool(base_id),
            "method": "move_exact_marked_base_to_airtable_trash",
            "verified": False,
        },
        "openai_requests": 0,
        "secrets_included": False,
    }


def _write_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    temporary.replace(path)


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    result = run_lifecycle(
        live=args.live,
        approval_reference=args.approval_reference,
    )
    _write_atomic(Path(args.output), result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in {"dry-run", "provider-write-verified"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
