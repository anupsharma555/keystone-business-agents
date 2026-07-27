from __future__ import annotations

from typing import Any

from keystone_agents import capability_profile
from keystone_agents.capabilities import profile
from keystone_agents.runtime import RequestRuntime
from keystone_agents.schemas.work_item import WorkflowRunRequest
from keystone_agents.workflow_runner import answer_work_item_state_followup


def test_request_runtime_reuses_store_after_request_normalization(
    monkeypatch,
) -> None:
    created_urls: list[str] = []

    class FakeStore:
        def __init__(self, database_url: str) -> None:
            created_urls.append(database_url)

    monkeypatch.setattr(
        "keystone_agents.runtime.request.SQLiteStore",
        FakeStore,
    )
    request = WorkflowRunRequest(
        request_text="research Example Health",
        database_url="sqlite:////tmp/kba-runtime-a.sqlite3",
        save=True,
    )

    runtime = RequestRuntime.from_workflow_request(request)
    normalized = request.model_copy(update={"request_text": "research Example Health"})

    assert runtime.with_request(normalized) is runtime
    assert created_urls == ["sqlite:////tmp/kba-runtime-a.sqlite3"]


def test_request_runtime_rebuilds_store_when_storage_scope_changes(
    monkeypatch,
) -> None:
    created_urls: list[str] = []

    class FakeStore:
        def __init__(self, database_url: str) -> None:
            created_urls.append(database_url)

    monkeypatch.setattr(
        "keystone_agents.runtime.request.SQLiteStore",
        FakeStore,
    )
    first = WorkflowRunRequest(
        request_text="first",
        database_url="sqlite:////tmp/kba-runtime-a.sqlite3",
        save=True,
    )
    second = first.model_copy(
        update={"database_url": "sqlite:////tmp/kba-runtime-b.sqlite3"}
    )

    runtime = RequestRuntime.from_workflow_request(first)
    replacement = runtime.with_request(second)

    assert replacement is not runtime
    assert created_urls == [
        "sqlite:////tmp/kba-runtime-a.sqlite3",
        "sqlite:////tmp/kba-runtime-b.sqlite3",
    ]


def test_request_runtime_builds_named_service_once() -> None:
    runtime = RequestRuntime.from_workflow_request(
        WorkflowRunRequest(request_text="fixture", save=False)
    )
    calls = 0

    def build_service() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"service": "fixture"}

    first = runtime.service("sdk-session:fixture", build_service)
    second = runtime.service("sdk-session:fixture", build_service)

    assert first is second
    assert calls == 1


def test_state_followup_normalization_builds_one_store_per_request(
    monkeypatch,
) -> None:
    created_urls: list[str] = []

    class FakeStore:
        def __init__(self, database_url: str) -> None:
            created_urls.append(database_url)

    monkeypatch.setattr(
        "keystone_agents.runtime.request.SQLiteStore",
        FakeStore,
    )
    monkeypatch.setattr(
        "keystone_agents.workflow_runner._maybe_answer_manager_loop_state_followup",
        lambda _request, *, store: None,
    )

    result = answer_work_item_state_followup(
        WorkflowRunRequest(
            request_text="what is the current status?",
            database_url="sqlite:////tmp/kba-runtime-followup.sqlite3",
            save=True,
        )
    )

    assert result is None
    assert created_urls == ["sqlite:////tmp/kba-runtime-followup.sqlite3"]


def test_legacy_capability_profile_import_is_a_compatibility_facade() -> None:
    assert (
        capability_profile.RequestCapabilityProfile
        is profile.RequestCapabilityProfile
    )
    assert (
        capability_profile.compile_request_capability_profile
        is profile.compile_request_capability_profile
    )
