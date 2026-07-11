"""Check Airtable structural-write prerequisites without creating or changing anything."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from keystone_agents.config import with_cli_environment
from keystone_agents.tools.internal_data_tools import _airtable_base_config, _airtable_send

REQUIRED_STRUCTURAL_SCOPES = {
    "schema.bases:read",
    "schema.bases:write",
    "workspacesAndBases:read",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="artifacts/test-pack/airtable-structural-readiness.json",
    )
    return parser


@with_cli_environment()
def main() -> int:
    args = build_parser().parse_args()
    result = check_airtable_structural_readiness()
    _write_result_atomic(Path(args.output), result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in {"ready", "preflight_ready_write_unverified"} else 2


def check_airtable_structural_readiness() -> dict[str, Any]:
    config = _airtable_base_config(base_alias="finance_tax_tracker")
    token = str(config.get("access_token") or "")
    configured_base_id = str(config.get("base_id") or "")
    workspace_id = str(os.getenv("AIRTABLE_WORKSPACE_ID") or "").strip()
    cleanup_mode = str(os.getenv("AIRTABLE_TEST_BASE_CLEANUP_MODE") or "").strip()
    if not token:
        return _readiness_result(
            scopes=set(),
            scopes_reported=False,
            token_configured=False,
            workspace_id_configured=bool(workspace_id),
            cleanup_mode=cleanup_mode,
            diagnostic="airtable_access_token_missing",
            configured_base_visible=False,
            base_visibility_checked=False,
        )
    try:
        payload = _airtable_send(
            {
                "method": "GET",
                "url": "https://api.airtable.com/v0/meta/whoami",
                "params": {},
            },
            access_token=token,
        )
    except Exception as exc:
        return _readiness_result(
            scopes=set(),
            scopes_reported=False,
            token_configured=True,
            workspace_id_configured=bool(workspace_id),
            cleanup_mode=cleanup_mode,
            diagnostic=f"whoami_failed:{type(exc).__name__}",
            configured_base_visible=False,
            base_visibility_checked=False,
        )
    scopes_value = payload.get("scopes") if isinstance(payload, dict) else None
    scopes_reported = isinstance(scopes_value, list)
    scopes = {
        str(scope)
        for scope in (scopes_value if isinstance(scopes_value, list) else [])
        if str(scope).strip()
    }
    configured_base_visible = False
    base_visibility_checked = False
    try:
        bases_payload = _airtable_send(
            {
                "method": "GET",
                "url": "https://api.airtable.com/v0/meta/bases",
                "params": {},
            },
            access_token=token,
        )
        bases = bases_payload.get("bases") if isinstance(bases_payload, dict) else None
        if isinstance(bases, list):
            base_visibility_checked = True
            configured_base_visible = any(
                isinstance(base, dict) and str(base.get("id") or "") == configured_base_id
                for base in bases
            )
    except Exception:
        pass
    return _readiness_result(
        scopes=scopes,
        scopes_reported=scopes_reported,
        token_configured=True,
        workspace_id_configured=bool(workspace_id),
        cleanup_mode=cleanup_mode,
        diagnostic="whoami_succeeded",
        configured_base_visible=configured_base_visible,
        base_visibility_checked=base_visibility_checked,
    )


def _readiness_result(
    *,
    scopes: set[str],
    scopes_reported: bool,
    token_configured: bool,
    workspace_id_configured: bool,
    cleanup_mode: str,
    diagnostic: str,
    configured_base_visible: bool,
    base_visibility_checked: bool,
) -> dict[str, Any]:
    missing_scopes = sorted(REQUIRED_STRUCTURAL_SCOPES - scopes) if scopes_reported else []
    cleanup_ready = cleanup_mode in {"manual_ui_confirmed", "enterprise_api"}
    blockers: list[str] = []
    if not token_configured:
        blockers.append("airtable_access_token_missing")
    if scopes_reported and missing_scopes:
        blockers.append("airtable_structural_scopes_missing")
    if token_configured and not base_visibility_checked:
        blockers.append("airtable_base_visibility_check_failed")
    elif token_configured and not configured_base_visible:
        blockers.append("airtable_configured_base_not_visible")
    if not workspace_id_configured:
        blockers.append("airtable_workspace_id_missing")
    if not cleanup_ready:
        blockers.append("airtable_test_base_cleanup_plan_missing")
    ready = not blockers
    status = (
        "ready"
        if ready and scopes_reported
        else "preflight_ready_write_unverified"
        if ready
        else "blocked"
    )
    return {
        "status": status,
        "operation": "airtable_structural_readiness",
        "provider_call": "read_only_whoami" if diagnostic == "whoami_succeeded" else "none",
        "token_configured": token_configured,
        "required_scopes": sorted(REQUIRED_STRUCTURAL_SCOPES),
        "configured_required_scopes": sorted(REQUIRED_STRUCTURAL_SCOPES & scopes),
        "missing_required_scopes": missing_scopes,
        "scope_status": "reported" if scopes_reported else "not_reported_by_provider",
        "scope_verification": (
            "reported_by_provider"
            if scopes_reported
            else "read_scope_proven_by_base_visibility;write_scope_requires_marked_create"
        ),
        "configured_base_visible": configured_base_visible,
        "base_visibility_checked": base_visibility_checked,
        "workspace_id_configured": workspace_id_configured,
        "cleanup_mode_configured": cleanup_ready,
        "cleanup_modes_supported": ["manual_ui_confirmed", "enterprise_api"],
        "diagnostic": diagnostic,
        "blockers": blockers,
        "openai_requests": 0,
        "writes_performed": 0,
        "secrets_included": False,
    }


def _write_result_atomic(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    temporary_path.write_text(
        json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
