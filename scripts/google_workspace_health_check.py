"""Check Google OAuth readiness for Keystone Workspace, Gmail, and Calendar.

The default mode is offline and never calls Google APIs. Use --live for read-only
API probes after the operator has approved a live credential check.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from keystone_agents.tools.gmail_tool import GMAIL_SCOPES as KBA_GOOGLE_SCOPES

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dependency is declared for the repo.
    load_dotenv = None

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"

WORKSPACE_SCOPES = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
)
GMAIL_SCOPES = tuple(scope for scope in KBA_GOOGLE_SCOPES if "/auth/gmail." in scope)
CALENDAR_SCOPES = tuple(scope for scope in KBA_GOOGLE_SCOPES if "/auth/calendar." in scope)

FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
DOC_MIME_TYPE = "application/vnd.google-apps.document"
SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"


@dataclass
class HealthCheck:
    name: str
    status: str
    details: dict[str, Any] = field(default_factory=dict)


def _utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _env(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get(name) or default).strip()


def _truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _path(value: str) -> Path | None:
    return Path(value).expanduser() if value.strip() else None


def _json_file_status(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"path": "", "present": False, "valid_json": False}
    details: dict[str, Any] = {"path": str(path), "present": path.is_file(), "valid_json": False}
    if not path.is_file():
        return details
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        details["error"] = "invalid_json"
        return details
    except OSError:
        details["error"] = "unreadable"
        return details
    if not isinstance(payload, dict):
        details["error"] = "not_object"
        return details
    raw_scopes = payload.get("scopes")
    if isinstance(raw_scopes, list | tuple | set):
        granted_scopes = [str(scope).strip() for scope in raw_scopes if str(scope).strip()]
    else:
        granted_scopes = str(payload.get("scope") or "").split()
    details.update(
        {
            "valid_json": True,
            "has_refresh_token": bool(str(payload.get("refresh_token") or "").strip()),
            "has_access_token": bool(
                str(payload.get("token") or payload.get("access_token") or "").strip()
            ),
            "scope_field_present": bool(granted_scopes),
            "granted_scopes": sorted(set(granted_scopes)),
        }
    )
    return details


def _client_secret_status(path: Path | None) -> dict[str, Any]:
    details = _json_file_status(path)
    details.pop("has_access_token", None)
    details.pop("has_refresh_token", None)
    details.pop("scope_field_present", None)
    details.pop("granted_scopes", None)
    details["has_client_config"] = False
    if not details.get("valid_json") or path is None:
        return details
    payload = json.loads(path.read_text(encoding="utf-8"))
    client = payload.get("installed") or payload.get("web") or {}
    details["has_client_config"] = bool(
        isinstance(client, dict)
        and str(client.get("client_id") or "").strip()
        and str(client.get("client_secret") or "").strip()
    )
    return details


def _offline_oauth_check(
    *,
    name: str,
    token_path: Path | None,
    client_secret_path: Path | None,
    account: str,
    live_enabled: bool,
    required_scopes: tuple[str, ...],
    extra: dict[str, Any] | None = None,
) -> HealthCheck:
    token = _json_file_status(token_path)
    secret = _client_secret_status(client_secret_path)
    issues: list[str] = []
    if live_enabled or token_path or client_secret_path:
        if not token["present"]:
            issues.append("oauth_token_missing")
        elif not token["valid_json"]:
            issues.append("oauth_token_invalid")
        elif not token.get("has_refresh_token"):
            issues.append("refresh_token_missing")
        if token.get("valid_json"):
            granted_scopes = set(token.get("granted_scopes") or [])
            if not granted_scopes:
                issues.append("oauth_scope_metadata_missing")
            else:
                issues.extend(
                    f"oauth_scope_missing:{scope}"
                    for scope in required_scopes
                    if scope not in granted_scopes
                )
        if client_secret_path and not secret["present"]:
            issues.append("oauth_client_secret_missing")
        elif client_secret_path and not secret["valid_json"]:
            issues.append("oauth_client_secret_invalid")
        elif client_secret_path and not secret.get("has_client_config"):
            issues.append("oauth_client_config_missing")
    status = STATUS_WARNING if issues else STATUS_OK
    return HealthCheck(
        name=name,
        status=status,
        details={
            "configured_account": account,
            "live_enabled": live_enabled,
            "token": token,
            "client_secret": secret,
            "required_scopes": list(required_scopes),
            "issues": issues,
            **(extra or {}),
        },
    )


def _google_imports():
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
    except ModuleNotFoundError as exc:  # pragma: no cover - environment dependent.
        raise RuntimeError(
            "Missing Google dependencies. Install google-api-python-client, "
            "google-auth, google-auth-httplib2, and google-auth-oauthlib."
        ) from exc
    return Credentials, Request, RefreshError, build, HttpError


def _credentials(token_path: Path, scopes: tuple[str, ...]):
    Credentials, Request, RefreshError, _build, _HttpError = _google_imports()
    creds = Credentials.from_authorized_user_file(str(token_path), scopes)
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        except RefreshError as exc:
            raise RuntimeError(f"OAuth refresh failed: {exc}") from exc
    if not creds.valid:
        raise RuntimeError("OAuth token is not valid after refresh.")
    return creds


def _service(api: str, version: str, token_path: Path, scopes: tuple[str, ...]):
    _Credentials, _Request, _RefreshError, build, _HttpError = _google_imports()
    return build(api, version, credentials=_credentials(token_path, scopes), cache_discovery=False)


def _google_error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _find_child_folder(drive_service: Any, parent_id: str, name: str) -> str:
    escaped = name.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        f"name = '{escaped}' and mimeType = '{FOLDER_MIME_TYPE}' "
        f"and trashed = false and '{parent_id}' in parents"
    )
    response = (
        drive_service.files()
        .list(q=query, spaces="drive", fields="files(id,name)", pageSize=5)
        .execute()
    )
    files = response.get("files", []) if isinstance(response, dict) else []
    return str(files[0].get("id", "")) if files else ""


def _find_folder_path(drive_service: Any, folder_path: str) -> tuple[str, list[str]]:
    parent = "root"
    resolved: list[str] = []
    for part in [item.strip() for item in folder_path.split("/") if item.strip()]:
        folder_id = _find_child_folder(drive_service, parent, part)
        if not folder_id:
            return "", resolved
        parent = folder_id
        resolved.append(part)
    return ("" if parent == "root" else parent), resolved


def _live_workspace_checks(env: Mapping[str, str]) -> list[HealthCheck]:
    token_path = _path(_env(env, "GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH"))
    if token_path is None:
        return [
            HealthCheck(
                name="google_workspace_live",
                status=STATUS_WARNING,
                details={"error": "GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH is not configured."},
            )
        ]
    folder_path = _env(env, "GOOGLE_DRIVE_KNI_OPS_FOLDER", "KNIOps") or "KNIOps"
    expected_account = _env(env, "GOOGLE_DRIVE_ACCOUNT")
    require_account = _truthy(_env(env, "GOOGLE_WORKSPACE_REQUIRE_ACCOUNT_MATCH", "true"))
    checks: list[HealthCheck] = []

    try:
        drive = _service("drive", "v3", token_path, WORKSPACE_SCOPES)
        about = drive.about().get(fields="user(emailAddress,displayName)").execute()
        user = about.get("user", {}) if isinstance(about, dict) else {}
        actual_account = str(user.get("emailAddress", "")).strip().lower()
        account_ok = (
            not require_account
            or not expected_account
            or actual_account == expected_account.lower()
        )
        root_id, resolved = _find_folder_path(drive, folder_path)
        checks.append(
            HealthCheck(
                name="google_drive",
                status=STATUS_OK if account_ok and root_id else STATUS_WARNING,
                details={
                    "account": actual_account,
                    "expected_account": expected_account,
                    "account_match": account_ok,
                    "folder_path": folder_path,
                    "folder_found": bool(root_id),
                    "resolved_parts": resolved,
                },
            )
        )
    except Exception as exc:
        return [
            HealthCheck(
                name="google_drive",
                status=STATUS_ERROR,
                details={"error": _google_error(exc), "folder_path": folder_path},
            )
        ]

    if not root_id:
        return checks

    try:
        docs = _service("docs", "v1", token_path, WORKSPACE_SCOPES)
        response = (
            drive.files()
            .list(
                q=(f"'{root_id}' in parents and mimeType = '{DOC_MIME_TYPE}' and trashed = false"),
                spaces="drive",
                fields="files(id,name,webViewLink)",
                pageSize=10,
            )
            .execute()
        )
        files = response.get("files", []) if isinstance(response, dict) else []
        doc = files[0] if files else None
        if doc:
            metadata = (
                docs.documents().get(documentId=str(doc["id"]), fields="documentId,title").execute()
            )
            checks.append(
                HealthCheck(
                    name="google_docs",
                    status=STATUS_OK,
                    details={
                        "read_test": "documents.get",
                        "document_found": True,
                        "document_title": metadata.get("title", doc.get("name", "")),
                    },
                )
            )
        else:
            checks.append(
                HealthCheck(
                    name="google_docs",
                    status=STATUS_WARNING,
                    details={
                        "document_found": False,
                        "message": "No Google Docs found in KNIOps root.",
                    },
                )
            )
    except Exception as exc:
        checks.append(
            HealthCheck(
                name="google_docs",
                status=STATUS_ERROR,
                details={"error": _google_error(exc)},
            )
        )

    try:
        sheets = _service("sheets", "v4", token_path, WORKSPACE_SCOPES)
        artifacts_id = _find_child_folder(drive, root_id, "Artifacts") or root_id
        response = (
            drive.files()
            .list(
                q=(
                    f"'{artifacts_id}' in parents and mimeType = '{SHEET_MIME_TYPE}' "
                    "and trashed = false"
                ),
                spaces="drive",
                fields="files(id,name,webViewLink)",
                pageSize=10,
            )
            .execute()
        )
        files = response.get("files", []) if isinstance(response, dict) else []
        sheet = files[0] if files else None
        if sheet:
            metadata = (
                sheets.spreadsheets()
                .get(
                    spreadsheetId=str(sheet["id"]),
                    fields="spreadsheetId,properties.title,sheets.properties.title",
                )
                .execute()
            )
            title = metadata.get("properties", {}).get("title", sheet.get("name", ""))
            checks.append(
                HealthCheck(
                    name="google_sheets",
                    status=STATUS_OK,
                    details={
                        "read_test": "spreadsheets.get",
                        "spreadsheet_found": True,
                        "spreadsheet_title": title,
                        "tabs": [
                            item.get("properties", {}).get("title", "")
                            for item in metadata.get("sheets", [])
                            if isinstance(item, dict)
                        ],
                    },
                )
            )
        else:
            checks.append(
                HealthCheck(
                    name="google_sheets",
                    status=STATUS_WARNING,
                    details={
                        "spreadsheet_found": False,
                        "message": "No Google Sheets found in KNIOps/Artifacts.",
                    },
                )
            )
    except Exception as exc:
        checks.append(
            HealthCheck(
                name="google_sheets",
                status=STATUS_ERROR,
                details={"error": _google_error(exc)},
            )
        )

    return checks


def _live_gmail_check(env: Mapping[str, str]) -> HealthCheck:
    token_path = _path(_env(env, "GMAIL_OAUTH_TOKEN_PATH") or _env(env, "GOOGLE_TOKEN_FILE"))
    if token_path is None:
        return HealthCheck(
            name="gmail",
            status=STATUS_WARNING,
            details={"error": "No Gmail token path configured."},
        )
    try:
        service = _service("gmail", "v1", token_path, GMAIL_SCOPES)
        profile = service.users().getProfile(userId="me").execute()
        actual = str(profile.get("emailAddress", "")).strip().lower()
        expected = (_env(env, "GMAIL_ACCOUNT") or _env(env, "KEYSTONE_GMAIL_DRAFT_ACCOUNT")).lower()
        account_ok = not expected or actual == expected
        return HealthCheck(
            name="gmail",
            status=STATUS_OK if account_ok else STATUS_WARNING,
            details={
                "account": actual,
                "expected_account": expected,
                "account_match": account_ok,
                "messages_total_present": "messagesTotal" in profile,
            },
        )
    except Exception as exc:
        return HealthCheck(name="gmail", status=STATUS_ERROR, details={"error": _google_error(exc)})


def _live_calendar_check(env: Mapping[str, str]) -> HealthCheck:
    token_path = _path(
        _env(env, "CALENDAR_OAUTH_TOKEN_PATH")
        or _env(env, "GMAIL_OAUTH_TOKEN_PATH")
        or _env(env, "GOOGLE_TOKEN_FILE")
    )
    if token_path is None:
        return HealthCheck(
            name="google_calendar",
            status=STATUS_WARNING,
            details={"error": "CALENDAR_OAUTH_TOKEN_PATH is not configured."},
        )
    calendar_id = _env(env, "GOOGLE_CALENDAR_ID", "primary") or "primary"
    try:
        service = _service("calendar", "v3", token_path, CALENDAR_SCOPES)
        metadata = service.calendarList().get(calendarId=calendar_id).execute()
        return HealthCheck(
            name="google_calendar",
            status=STATUS_OK,
            details={
                "calendar_id": calendar_id,
                "summary": metadata.get("summary", ""),
                "access_role": metadata.get("accessRole", ""),
            },
        )
    except Exception as exc:
        return HealthCheck(
            name="google_calendar",
            status=STATUS_ERROR,
            details={"calendar_id": calendar_id, "error": _google_error(exc)},
        )


def run_google_health_check(
    env: Mapping[str, str] | None = None,
    *,
    live: bool = False,
) -> dict[str, Any]:
    source = os.environ if env is None else env
    workspace_token = _path(_env(source, "GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH"))
    workspace_secret = _path(_env(source, "GOOGLE_WORKSPACE_OAUTH_CLIENT_SECRET_PATH"))
    gmail_token = _path(_env(source, "GMAIL_OAUTH_TOKEN_PATH") or _env(source, "GOOGLE_TOKEN_FILE"))
    gmail_secret = _path(
        _env(source, "GMAIL_OAUTH_CLIENT_SECRET_PATH") or _env(source, "GOOGLE_CREDENTIALS_FILE")
    )
    calendar_token = _path(
        _env(source, "CALENDAR_OAUTH_TOKEN_PATH")
        or _env(source, "GMAIL_OAUTH_TOKEN_PATH")
        or _env(source, "GOOGLE_TOKEN_FILE")
    )
    calendar_secret = _path(
        _env(source, "CALENDAR_OAUTH_CLIENT_SECRET_PATH")
        or _env(source, "GMAIL_OAUTH_CLIENT_SECRET_PATH")
        or _env(source, "GOOGLE_CREDENTIALS_FILE")
    )

    checks = [
        _offline_oauth_check(
            name="google_workspace_oauth",
            token_path=workspace_token,
            client_secret_path=workspace_secret,
            account=_env(source, "GOOGLE_DRIVE_ACCOUNT"),
            live_enabled=_truthy(_env(source, "GOOGLE_WORKSPACE_WRITES_ENABLED")),
            required_scopes=WORKSPACE_SCOPES,
            extra={
                "folder": _env(source, "GOOGLE_DRIVE_KNI_OPS_FOLDER", "KNIOps") or "KNIOps",
                "require_account_match": _truthy(
                    _env(source, "GOOGLE_WORKSPACE_REQUIRE_ACCOUNT_MATCH", "true")
                ),
            },
        ),
        _offline_oauth_check(
            name="gmail_oauth",
            token_path=gmail_token,
            client_secret_path=gmail_secret,
            account=_env(source, "GMAIL_ACCOUNT") or _env(source, "KEYSTONE_GMAIL_DRAFT_ACCOUNT"),
            live_enabled=_truthy(
                _env(source, "GMAIL_LIVE_READ_ENABLED")
                or _env(source, "KEYSTONE_ENABLE_LIVE_GMAIL")
            ),
            required_scopes=GMAIL_SCOPES,
        ),
        _offline_oauth_check(
            name="calendar_oauth",
            token_path=calendar_token,
            client_secret_path=calendar_secret,
            account=_env(source, "GOOGLE_CALENDAR_ACCOUNT"),
            live_enabled=_truthy(_env(source, "CALENDAR_LIVE_READ_ENABLED")),
            required_scopes=CALENDAR_SCOPES,
            extra={"calendar_id": _env(source, "GOOGLE_CALENDAR_ID", "primary") or "primary"},
        ),
    ]
    if live:
        checks.extend(_live_workspace_checks(source))
        checks.append(_live_gmail_check(source))
        checks.append(_live_calendar_check(source))

    if any(check.status == STATUS_ERROR for check in checks):
        overall = STATUS_ERROR
    elif any(check.status == STATUS_WARNING for check in checks):
        overall = STATUS_WARNING
    else:
        overall = STATUS_OK
    return {
        "generated_at": _utc_now(),
        "live": live,
        "overall_status": overall,
        "checks": [asdict(check) for check in checks],
    }


def _print_text(report: dict[str, Any]) -> None:
    print(f"Google integration health: {report['overall_status']}")
    print(f"Live probes: {'enabled' if report['live'] else 'disabled'}")
    for check in report["checks"]:
        print(f"- {check['name']}: {check['status']}")
        issues = check["details"].get("issues") or []
        if issues:
            print(f"  issues: {', '.join(issues)}")
        account = check["details"].get("account") or check["details"].get("configured_account")
        if account:
            print(f"  account: {account}")
        folder = check["details"].get("folder") or check["details"].get("folder_path")
        if folder:
            print(f"  folder: {folder}")
        error = check["details"].get("error")
        if error:
            print(f"  error: {error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Google integration OAuth health.")
    parser.add_argument(
        "--env-file",
        action="append",
        default=[],
        help="Dotenv file to load. May be repeated; later files override earlier files.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run read-only live API probes for Drive, Docs, Sheets, Gmail, and Calendar.",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    env_files = args.env_file or [".env"]
    if load_dotenv is not None:
        for env_file in env_files:
            load_dotenv(env_file, override=True)
    report = run_google_health_check(live=args.live)
    if args.json:
        print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        _print_text(report)
    return 1 if report["overall_status"] == STATUS_ERROR else 0


if __name__ == "__main__":
    raise SystemExit(main())
