from __future__ import annotations

from datetime import UTC, datetime

import scripts.run_airtable_test_base_lifecycle as lifecycle


def test_test_base_lifecycle_is_dry_run_by_default(monkeypatch) -> None:
    monkeypatch.setattr(
        lifecycle,
        "_airtable_base_config",
        lambda **_kwargs: {"access_token": "pat-secret"},
    )
    monkeypatch.setattr(
        lifecycle,
        "_airtable_send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network")),
    )

    result = lifecycle.run_lifecycle(
        live=False,
        approval_reference="",
        now=datetime(2026, 7, 11, tzinfo=UTC),
    )

    assert result["status"] == "dry-run"
    assert result["base_name"] == "KBA_TEST_BASE 20260711T000000Z"
    assert result["writes_performed"] == 0


def test_live_lifecycle_creates_and_verifies_structural_objects(monkeypatch) -> None:
    monkeypatch.setenv("AIRTABLE_WORKSPACE_ID", "wsp-secret")
    monkeypatch.setenv("KEYSTONE_AIRTABLE_ALLOW_TEST_BASE_WRITES", "true")
    monkeypatch.setenv("AIRTABLE_TEST_BASE_CLEANUP_MODE", "manual_ui_confirmed")
    monkeypatch.setattr(
        lifecycle,
        "_airtable_base_config",
        lambda **_kwargs: {"access_token": "pat-secret"},
    )
    calls: list[dict[str, object]] = []

    def fake_send(request, **_kwargs):
        calls.append(request)
        if request["url"].endswith("/meta/bases"):
            return {"id": "app-test"}
        if request["method"] == "POST":
            return {"id": "tbl-follow-up"}
        tables = [
            {
                "id": "tbl-validation",
                "name": "Validation Records",
                "fields": [
                    {"name": "Name"},
                    {"name": "Notes"},
                    {"name": "Amount"},
                    {"name": "Complete"},
                ],
                "views": [{"name": "Grid view"}],
            }
        ]
        if len([call for call in calls if call["method"] == "GET"]) > 1:
            tables.append(
                {
                    "id": "tbl-follow-up",
                    "name": "Follow-up Records",
                    "fields": [{"name": "Name"}, {"name": "Status"}],
                    "views": [{"name": "Grid view"}],
                }
            )
        return {"tables": tables}

    monkeypatch.setattr(lifecycle, "_airtable_send", fake_send)
    result = lifecycle.run_lifecycle(
        live=True,
        approval_reference="approval-test-base",
        now=datetime(2026, 7, 11, tzinfo=UTC),
    )

    assert result["status"] == "provider-write-verified"
    assert result["checks"]["default_view_verified"] is True
    assert result["checks"]["second_table_verified"] is True
    assert result["cleanup"]["required"] is True
    assert calls[0]["payload"]["workspaceId"] == "wsp-secret"
    assert "wsp-secret" not in str(result)
    assert "pat-secret" not in str(result)


def test_live_lifecycle_requires_gates_and_cleanup_plan(monkeypatch) -> None:
    monkeypatch.delenv("AIRTABLE_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("KEYSTONE_AIRTABLE_ALLOW_TEST_BASE_WRITES", raising=False)
    monkeypatch.delenv("AIRTABLE_TEST_BASE_CLEANUP_MODE", raising=False)
    monkeypatch.setattr(
        lifecycle,
        "_airtable_base_config",
        lambda **_kwargs: {"access_token": "pat-secret"},
    )

    result = lifecycle.run_lifecycle(live=True, approval_reference="")

    assert result["status"] == "blocked"
    assert result["writes_performed"] == 0
    assert result["blockers"] == [
        "airtable_test_base_write_gate_disabled",
        "airtable_workspace_id_missing",
        "approval_reference_missing",
        "manual_ui_cleanup_not_confirmed",
    ]


def test_live_lifecycle_reports_structural_permission_denial(monkeypatch) -> None:
    monkeypatch.setenv("AIRTABLE_WORKSPACE_ID", "wsp-secret")
    monkeypatch.setenv("KEYSTONE_AIRTABLE_ALLOW_TEST_BASE_WRITES", "true")
    monkeypatch.setenv("AIRTABLE_TEST_BASE_CLEANUP_MODE", "manual_ui_confirmed")
    monkeypatch.setattr(
        lifecycle,
        "_airtable_base_config",
        lambda **_kwargs: {"access_token": "pat-secret"},
    )
    monkeypatch.setattr(
        lifecycle,
        "_airtable_send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError(
                "Airtable API request failed with HTTP 403: "
                "INVALID_PERMISSIONS_OR_MODEL_NOT_FOUND"
            )
        ),
    )

    result = lifecycle.run_lifecycle(
        live=True,
        approval_reference="approval-test-base",
    )

    assert result["status"] == "blocked"
    assert result["blockers"] == ["airtable_structural_write_permission_denied"]
    assert result["base_id"] == ""
    assert result["cleanup"]["required"] is False
