from __future__ import annotations

import asyncio
import json
import os
from argparse import Namespace
from pathlib import Path

from keystone_agents.cli import _ask_route_session_default, _sdk_session_spec_for_ask
from keystone_agents.cli_sdk import sdk_session_from_args
from keystone_agents.schemas.work_item import WorkflowRunRequest
from keystone_agents.sdk_sessions import (
    DEFAULT_SESSION_HISTORY_LIMIT,
    SDK_SESSION_DB_ENV,
    SDK_SESSION_HISTORY_LIMIT_ENV,
    SDK_SESSION_ID_ENV,
    SDK_SESSIONS_ENABLED_ENV,
    build_sdk_session,
    build_sdk_session_from_env,
    context_file_session_components,
    derive_sdk_session_id,
    resolve_sdk_session_spec,
    sdk_session_env,
    session_audit_metadata,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.workflow_runner import advance_work_item


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'sdk_sessions.db'}"


def test_derive_sdk_session_id_hashes_raw_components() -> None:
    session_id = derive_sdk_session_id("slack", ("T123", "C456", "1715366400.000100"))

    assert session_id.startswith("kba_slack_")
    assert "T123" not in session_id
    assert "C456" not in session_id
    assert "1715366400" not in session_id
    assert session_id == derive_sdk_session_id("slack", ("T123", "C456", "1715366400.000100"))


def test_slack_context_file_can_scope_session_without_raw_values(tmp_path: Path) -> None:
    context_path = tmp_path / "slack-context.json"
    context_path.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.selected_message_context.v1",
                "team_id": "T123",
                "channel_id": "C456",
                "thread_ts": "1715366400.000100",
            }
        ),
        encoding="utf-8",
    )

    scope, components = context_file_session_components(context_path) or ("", ())
    spec = resolve_sdk_session_spec(scope=scope, components=components, default_enabled=True)

    assert scope == "slack"
    assert spec.enabled is True
    assert spec.scope == "slack"
    assert "T123" not in spec.session_id
    assert "C456" not in spec.session_id
    assert spec.history_limit == DEFAULT_SESSION_HISTORY_LIMIT


def test_slack_history_context_file_scopes_session_to_thread(tmp_path: Path) -> None:
    context_path = tmp_path / "slack-history.json"
    context_path.write_text(
        json.dumps(
            {
                "schema": "keystone.slack.history_context.v1",
                "channel_id": "C456",
                "thread_ts": "1715366400.000100",
                "request_ts": "1715366460.000200",
            }
        ),
        encoding="utf-8",
    )

    scope, components = context_file_session_components(context_path) or ("", ())
    spec = resolve_sdk_session_spec(scope=scope, components=components, default_enabled=True)

    assert scope == "slack"
    assert components == ("", "C456", "1715366400.000100")
    assert spec.enabled is True
    assert spec.scope == "slack"
    assert "C456" not in spec.session_id
    assert "1715366400" not in spec.session_id


def test_build_sdk_session_from_env_uses_explicit_env(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, str, int | None]] = []

    def fake_build_sqlite_session(
        session_id: str,
        database_path: str,
        *,
        session_history_limit: int | None = None,
    ) -> object:
        calls.append((session_id, database_path, session_history_limit))
        return object()

    monkeypatch.setattr(
        "keystone_agents.sdk_sessions.build_sqlite_session",
        fake_build_sqlite_session,
    )
    monkeypatch.setenv(SDK_SESSIONS_ENABLED_ENV, "true")
    session_id = derive_sdk_session_id("ask", ("abc123",))
    monkeypatch.setenv(SDK_SESSION_ID_ENV, session_id)
    monkeypatch.setenv(SDK_SESSION_DB_ENV, str(tmp_path / "sessions.sqlite3"))
    monkeypatch.setenv(SDK_SESSION_HISTORY_LIMIT_ENV, "12")

    session = build_sdk_session_from_env()

    assert session is not None
    assert calls == [(session_id, str(tmp_path / "sessions.sqlite3"), 12)]
    metadata = session_audit_metadata(session)
    assert metadata["scope"] == "ask"
    assert metadata["source"] == "env"
    assert metadata["session_id_hash"]
    assert metadata["session_history_mode"] == "recent_items"
    assert metadata["session_history_limit"] == 12
    assert metadata["session_truncation_configured"] is True
    assert "abc123" not in json.dumps(metadata)


def test_sdk_session_from_args_honors_default_enabled_without_flags(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, int | None]] = []

    def fake_build_sqlite_session(
        session_id: str,
        database_path: str,
        *,
        session_history_limit: int | None = None,
    ) -> object:
        calls.append((session_id, database_path, session_history_limit))
        return object()

    monkeypatch.setattr(
        "keystone_agents.sdk_sessions.build_sqlite_session",
        fake_build_sqlite_session,
    )
    monkeypatch.setenv(SDK_SESSION_DB_ENV, str(tmp_path / "sessions.sqlite3"))
    monkeypatch.delenv(SDK_SESSION_ID_ENV, raising=False)
    monkeypatch.delenv(SDK_SESSIONS_ENABLED_ENV, raising=False)
    monkeypatch.delenv(SDK_SESSION_HISTORY_LIMIT_ENV, raising=False)
    args = Namespace(
        sdk_session=None,
        sdk_session_id="",
        sdk_session_db="",
        sdk_session_history_limit=None,
    )

    session = sdk_session_from_args(
        args,
        scope="chief_of_staff",
        components=("direct-script", "tester", "/repo"),
        default_enabled=True,
    )

    assert session is not None
    assert calls == [
        (
            derive_sdk_session_id("chief_of_staff", ("direct-script", "tester", "/repo")),
            str(tmp_path / "sessions.sqlite3"),
            DEFAULT_SESSION_HISTORY_LIMIT,
        )
    ]
    assert os.environ[SDK_SESSIONS_ENABLED_ENV] == "true"
    assert os.environ[SDK_SESSION_HISTORY_LIMIT_ENV] == str(DEFAULT_SESSION_HISTORY_LIMIT)
    for key in (
        SDK_SESSIONS_ENABLED_ENV,
        SDK_SESSION_ID_ENV,
        SDK_SESSION_DB_ENV,
        SDK_SESSION_HISTORY_LIMIT_ENV,
    ):
        os.environ.pop(key, None)


def test_sdk_session_from_args_honors_explicit_history_limit(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[tuple[str, str, int | None]] = []

    def fake_build_sqlite_session(
        session_id: str,
        database_path: str,
        *,
        session_history_limit: int | None = None,
    ) -> object:
        calls.append((session_id, database_path, session_history_limit))
        return object()

    monkeypatch.setattr(
        "keystone_agents.sdk_sessions.build_sqlite_session",
        fake_build_sqlite_session,
    )
    db_path = str(tmp_path / "sessions.sqlite3")
    args = Namespace(
        sdk_session=True,
        sdk_session_id="operator-thread",
        sdk_session_db=db_path,
        sdk_session_history_limit=7,
    )

    session = sdk_session_from_args(
        args,
        scope="business_research_analyst",
        components=("direct-script",),
        default_enabled=False,
    )

    assert session is not None
    expected_id = derive_sdk_session_id(
        "business_research_analyst",
        ("explicit", "operator-thread"),
    )
    assert calls == [(expected_id, db_path, 7)]
    assert os.environ[SDK_SESSION_HISTORY_LIMIT_ENV] == "7"
    for key in (
        SDK_SESSIONS_ENABLED_ENV,
        SDK_SESSION_ID_ENV,
        SDK_SESSION_DB_ENV,
        SDK_SESSION_HISTORY_LIMIT_ENV,
    ):
        os.environ.pop(key, None)


def test_work_item_records_sdk_session_metadata(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    result = advance_work_item(
        WorkflowRunRequest(
            request_text="research NeuroFlow",
            save=True,
            database_url=database_url,
            live_sdk=True,
            sdk_session_db_path=str(tmp_path / "workitem-sessions.sqlite3"),
            sdk_session_history_limit=6,
        )
    )
    store = SQLiteStore(database_url)
    events = store.list_work_item_events(result.work_item.id)
    metadata = events[0].metadata["sdk_session"]

    assert metadata["enabled"] is True
    assert metadata["scope"] == "workitem"
    assert metadata["session_id_hash"]
    assert metadata["session_history_mode"] == "recent_items"
    assert metadata["session_history_limit"] == 6
    assert metadata["session_truncation_configured"] is True
    assert result.work_item.id not in json.dumps(metadata)


def test_session_env_disables_child_runs_when_spec_disabled() -> None:
    spec = resolve_sdk_session_spec(scope="ask", components=("default",), enabled=False)

    assert sdk_session_env(spec) == {SDK_SESSIONS_ENABLED_ENV: "false"}


def test_session_env_includes_history_limit_when_enabled() -> None:
    spec = resolve_sdk_session_spec(
        scope="ask",
        components=("default",),
        enabled=True,
        history_limit=5,
    )

    assert sdk_session_env(spec)[SDK_SESSION_HISTORY_LIMIT_ENV] == "5"


def test_build_sdk_session_applies_sdk_session_settings() -> None:
    spec = resolve_sdk_session_spec(
        scope="ask",
        components=("default",),
        enabled=True,
        history_limit=3,
    )

    session = build_sdk_session(spec)

    assert session is not None
    assert session.session_settings.limit == 3


def test_chief_of_staff_requires_scoped_context_for_default_session(monkeypatch) -> None:
    monkeypatch.delenv(SDK_SESSIONS_ENABLED_ENV, raising=False)
    monkeypatch.delenv(SDK_SESSION_ID_ENV, raising=False)
    monkeypatch.delenv(SDK_SESSION_DB_ENV, raising=False)
    monkeypatch.delenv(SDK_SESSION_HISTORY_LIMIT_ENV, raising=False)
    args = Namespace(
        sdk_session=None,
        sdk_session_id="",
        sdk_session_db="",
        sdk_session_history_limit=None,
    )

    chief_spec = _sdk_session_spec_for_ask(
        args,
        route="chief_of_staff",
        default_enabled=_ask_route_session_default("chief_of_staff"),
    )
    research_spec = _sdk_session_spec_for_ask(
        args,
        route="business_research_analyst",
        default_enabled=_ask_route_session_default("business_research_analyst"),
    )

    assert chief_spec.enabled is False
    assert research_spec.enabled is False


def test_bounded_sdk_session_keeps_function_call_with_output(tmp_path: Path) -> None:
    spec = resolve_sdk_session_spec(
        scope="slack",
        components=("T123", "C456", "thread-1"),
        enabled=True,
        database_path=str(tmp_path / "sessions.sqlite3"),
        history_limit=3,
    )
    session = build_sdk_session(spec)
    assert session is not None
    asyncio.run(
        session.add_items(
            [
                {
                    "type": "function_call",
                    "name": "calendar_lookup",
                    "call_id": "call_calendar_1",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_calendar_1",
                    "output": "{}",
                },
                {"role": "user", "content": "follow up one"},
                {"role": "user", "content": "follow up two"},
            ]
        )
    )

    items = asyncio.run(session.get_items())

    assert len(items) == 4
    assert items[0]["type"] == "function_call"
    assert items[1]["type"] == "function_call_output"
    assert items[0]["call_id"] == items[1]["call_id"]


def test_bounded_sdk_session_keeps_reasoning_with_function_call_and_output(
    tmp_path: Path,
) -> None:
    spec = resolve_sdk_session_spec(
        scope="slack",
        components=("T123", "C456", "thread-reasoning"),
        enabled=True,
        database_path=str(tmp_path / "sessions.sqlite3"),
        history_limit=6,
    )
    session = build_sdk_session(spec)
    assert session is not None
    asyncio.run(
        session.add_items(
            [
                {"role": "user", "content": "check the calendar"},
                {"type": "reasoning", "id": "rs_calendar_1", "summary": []},
                {
                    "type": "function_call",
                    "id": "fc_calendar_1",
                    "name": "calendar_lookup",
                    "call_id": "call_calendar_1",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_calendar_1",
                    "output": "{}",
                },
                {"type": "reasoning", "id": "rs_calendar_2", "summary": []},
                {"type": "message", "role": "assistant", "content": []},
                {"role": "user", "content": "is it there now?"},
                {"type": "reasoning", "id": "rs_calendar_3", "summary": []},
                {"type": "message", "role": "assistant", "content": []},
            ]
        )
    )

    items = asyncio.run(session.get_items())

    assert [item.get("type") or item.get("role") for item in items] == [
        "reasoning",
        "function_call",
        "function_call_output",
        "reasoning",
        "message",
        "user",
        "reasoning",
        "message",
    ]


def test_non_chief_ask_can_still_opt_into_session() -> None:
    args = Namespace(
        sdk_session=True,
        sdk_session_id="",
        sdk_session_db="",
        sdk_session_history_limit=8,
    )

    spec = _sdk_session_spec_for_ask(
        args,
        route="business_research_analyst",
        default_enabled=_ask_route_session_default("business_research_analyst"),
    )

    assert spec.enabled is True
    assert spec.history_limit == 8
