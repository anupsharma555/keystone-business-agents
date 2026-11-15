"""Opted-in host scope omits raw history while retaining the actual local reader."""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from types import SimpleNamespace

import pytest
from agents.models.interface import Model, ModelProvider, ModelResponse
from agents.usage import Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)
from pydantic import BaseModel

from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.orchestrator.preflight_context import SPECIALIST_EXECUTION_CONTEXT_ENV
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.runtime.continuity_policy import (
    CHANNEL_IDS_ENV,
    ENABLED_ENV,
    RAW_HISTORY_OMITTED_REASON,
    TEAM_IDS_ENV,
    activate_continuity_policy,
    current_continuity_policy,
    resolve_slack_compact_continuity_policy,
)
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.schemas.work_item import WorkflowRunRequest, WorkItem, WorkItemKind
from keystone_agents.sdk import build_local_run_config, build_sdk_agent, build_sqlite_session
from keystone_agents.sdk_sessions import (
    SDK_SESSION_DB_ENV,
    SDK_SESSION_ID_ENV,
    SDK_SESSIONS_ENABLED_ENV,
    SDKSessionSpec,
    build_sdk_session,
    build_sdk_session_from_env,
    sdk_session_env,
)
from keystone_agents.tools import website_extraction_tool as web
from keystone_agents.workflow_runner import _sdk_session_spec_for_work_item

CHANNEL = "C111TEST"
CURRENT_REQUEST = "Read only the exact saved source and summarize its evidence limits."
OLD_ANSWER = "UNACCEPTED_PRIOR_ASSISTANT_SENTINEL"
ORPHAN_INPUT = "ORPHAN_FINAL_SYNTHESIS_USER_SENTINEL"


def host_context():
    return {
        "schema": "keystone.slack.history_context.v1",
        "team_id": "T111TEST", "channel_id": CHANNEL, "user_id": "U111TEST",
        "thread_ts": "1700604700.000001", "request_ts": "1700604800.000001",
        "channel_context_capability": {
            "schema_version": "keystone.slack.channel_context.v1", "channel_id": CHANNEL,
            "request_ts": "1700604800.000001", "oldest_ts": "1700000000.000001",
        },
    }


@pytest.fixture
def pilot(monkeypatch):
    monkeypatch.setenv(ENABLED_ENV, "true")
    monkeypatch.setenv(CHANNEL_IDS_ENV, CHANNEL)
    monkeypatch.delenv(TEAM_IDS_ENV, raising=False)
    monkeypatch.delenv("KEYSTONE_CONTEXT_CONFIG_REPO", raising=False)
    monkeypatch.delenv("KEYSTONE_SLACK_REPO", raising=False)
    return resolve_slack_compact_continuity_policy(host_context())


@pytest.mark.parametrize("case", [
    "default_off", "other_channel", "missing_user", "missing_team", "missing_descriptor",
    "mismatched_channel", "mismatched_request", "future_thread", "too_many_channels",
    "prose_only",
])
def test_only_current_matching_host_scope_qualifies(case):
    context = host_context()
    env = {ENABLED_ENV: "true", CHANNEL_IDS_ENV: CHANNEL}
    if case == "default_off":
        env = {}
    elif case == "other_channel":
        env[CHANNEL_IDS_ENV] = "C222OTHER"
    elif case == "missing_user":
        context.pop("user_id")
    elif case == "missing_team":
        context.pop("team_id")
    elif case == "missing_descriptor":
        context.pop("channel_context_capability")
    elif case == "mismatched_channel":
        context["channel_context_capability"]["channel_id"] = "C222OTHER"
    elif case == "mismatched_request":
        context["channel_context_capability"]["request_ts"] = "1700604801.000001"
    elif case == "future_thread":
        context["thread_ts"] = "1700604801.000001"
    elif case == "too_many_channels":
        env[CHANNEL_IDS_ENV] = ",".join(f"C{i:03}TEST" for i in range(17))
    else:
        context = {"raw_request": json.dumps(context)}
    assert not resolve_slack_compact_continuity_policy(context, environ=env).eligible


def test_workspace_enrollment_admits_public_and_private_channels_only_in_bound_team():
    env = {
        ENABLED_ENV: "true", TEAM_IDS_ENV: "T111TEST, T222OTHER",
        CHANNEL_IDS_ENV: "CLEGACYPILOT",
    }
    for prefix in ("C", "G"):
        for index in range(24):
            channel = f"{prefix}{index:03}TEST"
            context = host_context()
            context["channel_id"] = channel
            context["channel_context_capability"]["channel_id"] = channel
            context["channel_context_capability"]["team_id"] = "T111TEST"
            policy = resolve_slack_compact_continuity_policy(context, environ=env)
            assert policy.eligible and policy.channel_id == channel

            context["team_id"] = "T333FOREIGN"
            assert not resolve_slack_compact_continuity_policy(context, environ=env).eligible


def test_workspace_enrollment_requires_current_bound_team_and_valid_configuration():
    context = host_context()
    env = {ENABLED_ENV: "true", TEAM_IDS_ENV: "T111TEST", CHANNEL_IDS_ENV: CHANNEL}
    assert not resolve_slack_compact_continuity_policy(context, environ=env).eligible
    context["channel_context_capability"]["team_id"] = "T222OTHER"
    assert not resolve_slack_compact_continuity_policy(context, environ=env).eligible
    context["channel_context_capability"]["team_id"] = "T111TEST"
    assert resolve_slack_compact_continuity_policy(context, environ=env).eligible

    for invalid in ("*", "T111TEST,OTHER", "T111TEST/", ",".join(
        f"T{i:03}TEST" for i in range(17)
    )):
        assert resolve_slack_compact_continuity_policy(
            context, environ={**env, TEAM_IDS_ENV: invalid},
        ).reason == "invalid_team_configuration"

    context["channel_id"] = "D111DIRECT"
    context["channel_context_capability"]["channel_id"] = "D111DIRECT"
    assert not resolve_slack_compact_continuity_policy(context, environ=env).eligible


def test_empty_team_enrollment_preserves_legacy_public_channel_pilot():
    context = host_context()
    env = {ENABLED_ENV: "true", TEAM_IDS_ENV: "  ", CHANNEL_IDS_ENV: CHANNEL}
    assert resolve_slack_compact_continuity_policy(context, environ=env).eligible
    context["channel_id"] = "G111PRIVATE"
    context["channel_context_capability"]["channel_id"] = "G111PRIVATE"
    assert not resolve_slack_compact_continuity_policy(context, environ=env).eligible


def test_workspace_enrollment_can_read_allowlisted_linked_repo_config(monkeypatch, tmp_path):
    linked_repo = tmp_path / "slack-host"
    linked_repo.mkdir()
    (linked_repo / ".env").write_text(f"{TEAM_IDS_ENV}=T111TEST\n")
    monkeypatch.setenv("KEYSTONE_CONTEXT_CONFIG_REPO", str(linked_repo))
    monkeypatch.delenv(TEAM_IDS_ENV, raising=False)
    monkeypatch.setenv(ENABLED_ENV, "true")
    monkeypatch.setenv(CHANNEL_IDS_ENV, "C222OTHER")
    context = host_context()
    context["channel_context_capability"]["team_id"] = "T111TEST"
    assert resolve_slack_compact_continuity_policy(context).eligible

    (linked_repo / ".env").write_text(f"{TEAM_IDS_ENV}=*\n")
    assert resolve_slack_compact_continuity_policy(context).reason == "invalid_team_configuration"


def test_saved_scope_cannot_override_current_workspace_capability():
    context = host_context()
    context["channel_context_capability"]["team_id"] = "T111TEST"
    context["slack_scope"] = {
        "team_id": "T222OTHER", "channel_id": "G222OTHER", "user_id": "U111TEST",
        "thread_ts": context["thread_ts"], "request_ts": context["request_ts"],
    }
    context["current_work_item"] = {
        "team_id": "T222OTHER", "channel_id": "G222OTHER",
    }
    env = {ENABLED_ENV: "true", TEAM_IDS_ENV: "T111TEST,T222OTHER"}
    assert not resolve_slack_compact_continuity_policy(context, environ=env).eligible


def test_policy_scope_is_request_local_and_metadata_excludes_identity(pilot):
    assert pilot.eligible
    before = current_continuity_policy()
    with activate_continuity_policy(pilot):
        assert current_continuity_policy() == pilot
        assert CHANNEL not in json.dumps(pilot.metadata())
        assert "U111TEST" not in repr(pilot)
        # A nested host entry cannot inherit another entry's eligible identity.
        with activate_continuity_policy(resolve_slack_compact_continuity_policy({})):
            assert not current_continuity_policy().eligible
        assert current_continuity_policy() == pilot
    assert current_continuity_policy() is before


def test_direct_and_work_item_session_specs_deny_even_explicit_sessions(pilot, tmp_path):
    path = tmp_path / "context.json"
    path.write_text(json.dumps(host_context()))
    args = Namespace(
        sdk_session=True, sdk_session_id="explicit-test-session",
        sdk_session_db=str(tmp_path / "sessions.sqlite3"), sdk_session_history_limit=6,
    )
    direct = cli._sdk_session_spec_for_ask(
        args, route="chief_of_staff", default_enabled=True, context_file_path=str(path),
    )
    request = WorkflowRunRequest(
        request_text=CURRENT_REQUEST, context_file_snapshot=host_context(),
        live_sdk=True, sdk_session_enabled=True, sdk_session_id="explicit-test-session",
    )
    item = WorkItem(kind=WorkItemKind.RESEARCH_BRIEF, title="Synthetic selected study")
    workflow = _sdk_session_spec_for_work_item(request, item)
    for spec in [direct, workflow]:
        assert not spec.enabled and spec.history_limit == 0
        assert spec.history_admission_reason == RAW_HISTORY_OMITTED_REASON
        assert sdk_session_env(spec)[SDK_SESSIONS_ENABLED_ENV] == "false"
        assert build_sdk_session(spec) is None
    assert not (tmp_path / "sessions.sqlite3").exists()


class Answer(BaseModel):
    summary: str


class ReaderModel(Model):
    def __init__(self, access, expect_history):
        self.access, self.expect_history, self.inputs = access, expect_history, []

    async def get_response(self, system_instructions, input, model_settings, tools,
                           output_schema, handoffs, tracing, **kwargs):
        text = json.dumps(input)
        self.inputs.append(text)
        assert CURRENT_REQUEST in text
        assert (OLD_ANSWER in text) == self.expect_history
        assert (ORPHAN_INPUT in text) == self.expect_history
        assert [tool.name for tool in tools] == ["read_web_source_window"]
        if len(self.inputs) == 1:
            output = [ResponseFunctionToolCall(
                type="function_call", name="read_web_source_window", call_id="read-current-source",
                arguments=json.dumps({
                    "source_id": self.access.source_id, "selected_url": self.access.selected_url,
                    "expected_snapshot_sha256": self.access.snapshot_sha256,
                    "start_char": self.access.next_start_char, "max_chars": 8000,
                }),
            )]
        else:
            assert "Current local source continuation" in text
            output = [ResponseOutputMessage(
                type="message", id="answer", role="assistant", status="completed",
                content=[ResponseOutputText(type="output_text", annotations=[], text=json.dumps({
                    "summary": "Summarized the current selected source with its coverage limits.",
                }))],
            )]
        return ModelResponse(output=output, usage=Usage(requests=1), response_id="offline-test")

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


class Provider(ModelProvider):
    def __init__(self, model):
        self.model = model

    def get_model(self, model_name):
        return self.model


@pytest.mark.parametrize("session_mode", ["explicit", "environment"])
@pytest.mark.parametrize("admission", ["pilot", "child_environment", "nonpilot"])
def test_actual_sdk_omits_old_outputs_but_keeps_scoped_reader(
    monkeypatch, tmp_path, pilot, session_mode, admission,
):
    monkeypatch.setenv("KEYSTONE_RUNTIME_STATE_DIR", str(tmp_path / "sources"))
    session_path = tmp_path / "history.sqlite3"
    session = build_sqlite_session("synthetic-history", session_path, session_history_limit=6)
    history = [
        {"role": "assistant", "content": OLD_ANSWER},
        {"role": "user", "content": ORPHAN_INPUT},
    ]
    asyncio.run(session.add_items(history))
    monkeypatch.setenv(SDK_SESSIONS_ENABLED_ENV, "true")
    monkeypatch.setenv(SDK_SESSION_ID_ENV, "synthetic-history")
    monkeypatch.setenv(SDK_SESSION_DB_ENV, str(session_path))
    if admission == "child_environment":
        context = host_context()
        monkeypatch.setenv(SPECIALIST_EXECUTION_CONTEXT_ENV, json.dumps({
            "slack_scope": {k: v for k, v in context.items()
                            if k not in {"schema", "channel_context_capability"}},
            "channel_context_capability": context["channel_context_capability"],
        }))
    elif admission == "nonpilot":
        monkeypatch.setenv(ENABLED_ENV, "false")
    _, access = web.project_web_source(web.WebsiteExtractionResult(
        url="https://example.org/current-source", provider="trafilatura", status="success",
        text_or_markdown="Preview. " * 200 + " Current local source continuation. " * 40,
    ), selected_url="https://example.org/current-source", max_chars=1200)
    model = ReaderModel(access, expect_history=admission == "nonpilot")
    agent = build_sdk_agent(
        "business_research_analyst",
        "Answer only the current raw request from scoped source evidence.",
        Answer, tools=[web.scoped_web_source_read_tool([access])],
    )
    from contextlib import nullcontext

    policy_scope = activate_continuity_policy(pilot) if admission == "pilot" else nullcontext()
    with policy_scope:
        if admission == "pilot":
            assert build_sdk_session_from_env() is None
        result = run_typed_sdk_agent(
            agent=agent, typed_input={"raw_request": CURRENT_REQUEST}, output_type=Answer,
            run_config=build_local_run_config(Provider(model)), max_turns=2,
            session=session if session_mode == "explicit" else None,
        )
    assert len(model.inputs) == 2
    if admission != "nonpilot":
        assert result.request_cache["session_attached"] is False
        assert (
            result.request_cache["raw_sdk_history_admission"]["reason"]
            == RAW_HISTORY_OMITTED_REASON
        )
        assert asyncio.run(session.get_items()) == history
    else:
        assert result.request_cache["session_attached"] is True
        assert "raw_sdk_history_admission" not in result.request_cache


def test_chief_child_receives_disabled_session_without_parent_environment_mutation(
    monkeypatch, tmp_path, pilot, capsys,
):
    captured = {}

    def child(*args, **kwargs):
        captured.update(kwargs["env"])
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "send_enabled": False, "human_summary": "Current bounded evidence summary.",
            "output": {"summary": "Current bounded evidence summary."},
        }))

    monkeypatch.setattr(cli, "run_isolated_child_process", child)
    monkeypatch.setenv(SDK_SESSIONS_ENABLED_ENV, "true")
    context = host_context()
    context = {
        "slack_scope": {k: v for k, v in context.items()
                        if k not in {"schema", "channel_context_capability"}},
        "channel_context_capability": context["channel_context_capability"],
    }
    code = cli._run_ask_script_live(
        "chief_of_staff", CURRENT_REQUEST, ["offline-child"], json_output=True,
        manual_plan=ManualRequestPlan(target_agent="chief_of_staff"),
        execution_context=context, database_url=f"sqlite:///{tmp_path / 'child.db'}",
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert captured[SDK_SESSIONS_ENABLED_ENV] == "false"
    assert payload["sdk_session"]["history_admission_reason"] == RAW_HISTORY_OMITTED_REASON
    assert payload["sdk_session"]["enabled"] is False
    import os

    assert os.environ[SDK_SESSIONS_ENABLED_ENV] == "true"


def test_child_host_scope_blocks_session_construction_without_contextvar(monkeypatch, pilot):
    monkeypatch.setenv(SPECIALIST_EXECUTION_CONTEXT_ENV, json.dumps(host_context()))
    monkeypatch.setenv(SDK_SESSIONS_ENABLED_ENV, "true")
    monkeypatch.setenv(SDK_SESSION_ID_ENV, "synthetic-session")

    def forbidden(*args, **kwargs):
        raise AssertionError("A denied session must not open or modify its database.")

    monkeypatch.setattr("keystone_agents.sdk_sessions.build_sqlite_session", forbidden)
    assert build_sdk_session(SDKSessionSpec(enabled=True, session_id="synthetic-session")) is None
    assert build_sdk_session_from_env() is None
