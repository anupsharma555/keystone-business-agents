from __future__ import annotations

import scripts.check_airtable_structural_readiness as readiness


def test_airtable_structural_readiness_is_sanitized_and_ready(monkeypatch) -> None:
    monkeypatch.setenv("AIRTABLE_WORKSPACE_ID", "wsp-secret")
    monkeypatch.setenv("AIRTABLE_TEST_BASE_CLEANUP_MODE", "manual_ui_confirmed")
    monkeypatch.setattr(
        readiness,
        "_airtable_base_config",
        lambda **_kwargs: {"access_token": "pat-secret", "base_id": "app-configured"},
    )
    monkeypatch.setattr(
        readiness,
        "_airtable_send",
        lambda request, **_kwargs: (
            {"bases": [{"id": "app-configured", "name": "private"}]}
            if request["url"].endswith("/meta/bases")
            else {
                "id": "usr-secret",
                "email": "private@example.com",
                "scopes": sorted(readiness.REQUIRED_STRUCTURAL_SCOPES),
            }
        ),
    )

    result = readiness.check_airtable_structural_readiness()

    assert result["status"] == "ready"
    assert result["provider_call"] == "read_only_whoami"
    assert result["scope_status"] == "reported"
    assert result["blockers"] == []
    assert result["openai_requests"] == 0
    assert result["writes_performed"] == 0
    assert result["configured_base_visible"] is True
    assert "wsp-secret" not in str(result)
    assert "pat-secret" not in str(result)
    assert "usr-secret" not in str(result)
    assert "private@example.com" not in str(result)


def test_airtable_structural_readiness_reports_exact_missing_prerequisites(monkeypatch) -> None:
    monkeypatch.delenv("AIRTABLE_WORKSPACE_ID", raising=False)
    monkeypatch.delenv("AIRTABLE_TEST_BASE_CLEANUP_MODE", raising=False)
    monkeypatch.setattr(
        readiness,
        "_airtable_base_config",
        lambda **_kwargs: {"access_token": "pat-secret", "base_id": "app-configured"},
    )
    monkeypatch.setattr(
        readiness,
        "_airtable_send",
        lambda request, **_kwargs: (
            {"bases": [{"id": "app-configured"}]}
            if request["url"].endswith("/meta/bases")
            else {"scopes": ["schema.bases:read"]}
        ),
    )

    result = readiness.check_airtable_structural_readiness()

    assert result["status"] == "blocked"
    assert result["configured_required_scopes"] == ["schema.bases:read"]
    assert result["missing_required_scopes"] == [
        "schema.bases:write",
        "workspacesAndBases:read",
    ]
    assert result["blockers"] == [
        "airtable_structural_scopes_missing",
        "airtable_workspace_id_missing",
        "airtable_test_base_cleanup_plan_missing",
    ]


def test_airtable_structural_readiness_does_not_leak_provider_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        readiness,
        "_airtable_base_config",
        lambda **_kwargs: {"access_token": "pat-secret", "base_id": "app-configured"},
    )
    monkeypatch.setattr(
        readiness,
        "_airtable_send",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("token pat-secret rejected for private@example.com")
        ),
    )

    result = readiness.check_airtable_structural_readiness()

    assert result["status"] == "blocked"
    assert result["diagnostic"] == "whoami_failed:RuntimeError"
    assert "pat-secret" not in str(result)
    assert "private@example.com" not in str(result)


def test_airtable_structural_readiness_does_not_treat_absent_scope_field_as_empty(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AIRTABLE_WORKSPACE_ID", "wsp-secret")
    monkeypatch.setenv("AIRTABLE_TEST_BASE_CLEANUP_MODE", "manual_ui_confirmed")
    monkeypatch.setattr(
        readiness,
        "_airtable_base_config",
        lambda **_kwargs: {"access_token": "pat-secret", "base_id": "app-configured"},
    )
    monkeypatch.setattr(
        readiness,
        "_airtable_send",
        lambda request, **_kwargs: (
            {"bases": [{"id": "app-configured"}]}
            if request["url"].endswith("/meta/bases")
            else {"id": "usr-secret"}
        ),
    )

    result = readiness.check_airtable_structural_readiness()

    assert result["scope_status"] == "not_reported_by_provider"
    assert result["status"] == "preflight_ready_write_unverified"
    assert result["configured_required_scopes"] == []
    assert result["missing_required_scopes"] == []
    assert result["scope_verification"].endswith(
        "write_scope_requires_marked_create"
    )
    assert "airtable_structural_scope_introspection_unavailable" not in result["blockers"]
    assert "airtable_structural_scopes_missing" not in result["blockers"]
