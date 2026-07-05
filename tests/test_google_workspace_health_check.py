from __future__ import annotations

import json
from pathlib import Path

from scripts.google_workspace_health_check import run_google_health_check


def test_google_health_check_offline_accepts_configured_tokens(tmp_path: Path) -> None:
    workspace_token = tmp_path / "workspace-token.json"
    workspace_secret = tmp_path / "workspace-secret.json"
    gmail_token = tmp_path / "gmail-token.json"
    gmail_secret = tmp_path / "gmail-secret.json"
    calendar_token = tmp_path / "calendar-token.json"
    calendar_secret = tmp_path / "calendar-secret.json"

    token_payload = {
        "refresh_token": "refresh-secret-value",
        "token": "access-secret-value",
        "scope": "scope-value",
    }
    secret_payload = {
        "installed": {
            "client_id": "client-id-value",
            "client_secret": "client-secret-value",
        }
    }
    for path in (workspace_token, gmail_token, calendar_token):
        path.write_text(json.dumps(token_payload), encoding="utf-8")
    for path in (workspace_secret, gmail_secret, calendar_secret):
        path.write_text(json.dumps(secret_payload), encoding="utf-8")

    report = run_google_health_check(
        {
            "GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH": str(workspace_token),
            "GOOGLE_WORKSPACE_OAUTH_CLIENT_SECRET_PATH": str(workspace_secret),
            "GOOGLE_WORKSPACE_WRITES_ENABLED": "true",
            "GOOGLE_DRIVE_ACCOUNT": "operator@example.com",
            "GOOGLE_DRIVE_KNI_OPS_FOLDER": "KNIOps",
            "GMAIL_OAUTH_TOKEN_PATH": str(gmail_token),
            "GMAIL_OAUTH_CLIENT_SECRET_PATH": str(gmail_secret),
            "GMAIL_LIVE_READ_ENABLED": "true",
            "GMAIL_ACCOUNT": "operator@example.com",
            "CALENDAR_OAUTH_TOKEN_PATH": str(calendar_token),
            "CALENDAR_OAUTH_CLIENT_SECRET_PATH": str(calendar_secret),
            "CALENDAR_LIVE_READ_ENABLED": "true",
            "GOOGLE_CALENDAR_ACCOUNT": "operator@example.com",
            "GOOGLE_CALENDAR_ID": "primary",
        },
        live=False,
    )

    assert report["overall_status"] == "ok"
    assert {check["name"] for check in report["checks"]} == {
        "google_workspace_oauth",
        "gmail_oauth",
        "calendar_oauth",
    }
    payload = json.dumps(report)
    assert "refresh-secret-value" not in payload
    assert "access-secret-value" not in payload
    assert "client-secret-value" not in payload


def test_google_health_check_offline_warns_for_missing_live_tokens(tmp_path: Path) -> None:
    report = run_google_health_check(
        {
            "GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH": str(tmp_path / "missing-workspace.json"),
            "GOOGLE_WORKSPACE_WRITES_ENABLED": "true",
            "GMAIL_OAUTH_TOKEN_PATH": str(tmp_path / "missing-gmail.json"),
            "GMAIL_LIVE_READ_ENABLED": "true",
            "CALENDAR_OAUTH_TOKEN_PATH": str(tmp_path / "missing-calendar.json"),
            "CALENDAR_LIVE_READ_ENABLED": "true",
        },
        live=False,
    )

    assert report["overall_status"] == "warning"
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["google_workspace_oauth"]["details"]["issues"] == ["oauth_token_missing"]
    assert checks["gmail_oauth"]["details"]["issues"] == ["oauth_token_missing"]
    assert checks["calendar_oauth"]["details"]["issues"] == ["oauth_token_missing"]
