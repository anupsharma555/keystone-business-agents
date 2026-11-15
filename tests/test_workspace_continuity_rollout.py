"""Offline acceptance for host-enrolled Slack workspace continuity."""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace

import pytest
from agents.models.interface import Model
from agents.tool_context import ToolContext
from pydantic import BaseModel

from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.memory import chief_of_staff_memory_item
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.runtime.continuity_policy import (
    CHANNEL_IDS_ENV,
    ENABLED_ENV,
    RAW_HISTORY_OMITTED_REASON,
    activate_continuity_policy,
    resolve_slack_compact_continuity_policy,
)
from keystone_agents.runtime.operator_continuity import (
    compact_linked_thread_context,
    compact_task_context,
    operator_capability_from_state,
)
from keystone_agents.runtime.scoped_memory import (
    memory_capture_fingerprint,
    project_scoped_memory_facts,
)
from keystone_agents.schemas.memory_scope import MemoryCaptureProvenance, SlackMemoryScope
from keystone_agents.schemas.work_item import WorkItem, WorkItemKind, WorkItemRoute
from keystone_agents.sdk import build_local_run_config, build_sdk_agent, build_sqlite_session
from keystone_agents.sdk_sessions import SDKSessionSpec, build_sdk_session
from keystone_agents.storage.sqlite_store import SQLiteStore

TEAM_IDS_ENV = "KEYSTONE_SLACK_COMPACT_CONTINUITY_TEAM_IDS"
TEAM = "T111TEST"
OTHER_TEAM = "T222OTHER"
USER = "U111TEST"
REQUEST_TS = "1794675601.000001"
THREAD_TS = "1794675600.000001"


def _host(channel: str, *, team: str = TEAM, user: str = USER,
          request_ts: str = REQUEST_TS) -> dict:
    return {
        "slack_scope": {
            "team_id": team, "channel_id": channel, "user_id": user,
            "thread_ts": THREAD_TS, "request_ts": request_ts,
        },
        "channel_context_capability": {
            "schema_version": "keystone.slack.channel_context.v1",
            "team_id": team, "channel_id": channel, "request_ts": request_ts,
            "oldest_ts": "1794670000.000001",
        },
    }


def _workspace_env() -> dict[str, str]:
    # A configured team enrolls every current C/G channel, including channels
    # outside the old pilot list. The legacy list must not widen another team.
    return {ENABLED_ENV: "true", TEAM_IDS_ENV: TEAM, CHANNEL_IDS_ENV: "C999LEGACY"}


def test_twenty_four_public_and_private_channels_are_enrolled() -> None:
    env = _workspace_env()
    channels = [f"C{i:03}TEST" for i in range(12)] + [
        f"G{i:03}TEST" for i in range(12)
    ]
    policies = [resolve_slack_compact_continuity_policy(_host(channel), environ=env)
                for channel in channels]
    assert len(policies) == 24
    assert all(policy.eligible for policy in policies)
    assert {policy.channel_id for policy in policies} == set(channels)
    assert all(policy.reason == RAW_HISTORY_OMITTED_REASON for policy in policies)


@pytest.mark.parametrize("case", [
    "foreign_team", "legacy_channel_foreign_team", "dm", "missing_team",
    "missing_user", "missing_request", "stale_channel_capability",
    "stale_request_capability", "foreign_team_capability", "missing_capability",
])
def test_workspace_enrollment_requires_current_matching_host_scope(case: str) -> None:
    host = _host("G123PRIVATE")
    env = _workspace_env()
    if case == "foreign_team":
        host = _host("G123PRIVATE", team=OTHER_TEAM)
    elif case == "legacy_channel_foreign_team":
        host = _host("C999LEGACY", team=OTHER_TEAM)
    elif case == "dm":
        host = _host("D123DIRECT")
    elif case == "missing_team":
        host["slack_scope"].pop("team_id")
    elif case == "missing_user":
        host["slack_scope"].pop("user_id")
    elif case == "missing_request":
        host["slack_scope"].pop("request_ts")
    elif case == "stale_channel_capability":
        host["channel_context_capability"]["channel_id"] = "G999OTHER"
    elif case == "stale_request_capability":
        host["channel_context_capability"]["request_ts"] = "1794675602.000001"
    elif case == "foreign_team_capability":
        host["channel_context_capability"]["team_id"] = OTHER_TEAM
    else:
        host.pop("channel_context_capability")
    assert not resolve_slack_compact_continuity_policy(host, environ=env).eligible


@pytest.mark.parametrize("configured", ["T111TEST,invalid", "T111TEST,,T222OTHER;", "D123BAD"])
def test_malformed_nonempty_team_configuration_fails_closed(configured: str) -> None:
    env = {**_workspace_env(), TEAM_IDS_ENV: configured}
    assert not resolve_slack_compact_continuity_policy(
        _host("C999LEGACY"), environ=env,
    ).eligible


def test_unset_team_configuration_keeps_legacy_c_channel_pilot() -> None:
    env = {ENABLED_ENV: "true", CHANNEL_IDS_ENV: "C999LEGACY"}
    assert resolve_slack_compact_continuity_policy(
        _host("C999LEGACY"), environ=env,
    ).eligible
    assert not resolve_slack_compact_continuity_policy(
        _host("G999LEGACY"), environ=env,
    ).eligible


@pytest.mark.parametrize("channel", ["C123PUBLIC", "G456PRIVATE"])
@pytest.mark.parametrize("is_reply", [False, True], ids=["root", "reply"])
def test_cli_context_for_each_channel_shape_omits_old_output(
    tmp_path, monkeypatch, channel: str, is_reply: bool,
) -> None:
    monkeypatch.setenv(ENABLED_ENV, "true")
    monkeypatch.setenv(TEAM_IDS_ENV, TEAM)
    request_ts = REQUEST_TS if is_reply else THREAD_TS
    host = _host(channel, request_ts=request_ts)
    payload = {
        "schema": "keystone.slack.history_context.v1",
        **host["slack_scope"],
        "channel_context_capability": host["channel_context_capability"],
        "prior_agent_runs": [{"summary": "OLD_OUTPUT_SENTINEL"}],
    }
    path = tmp_path / "slack-context.json"
    path.write_text(json.dumps(payload))
    args = Namespace(
        sdk_session=True, sdk_session_id="explicit-old-history",
        sdk_session_db=str(tmp_path / "history.sqlite3"),
        sdk_session_history_limit=6,
    )
    spec = cli._sdk_session_spec_for_ask(
        args, route="chief_of_staff", default_enabled=True,
        context_file_path=str(path),
    )
    assert not spec.enabled and spec.history_limit == 0
    assert spec.history_admission_reason == RAW_HISTORY_OMITTED_REASON

    state = {
        "slack_context": host["slack_scope"],
        "channel_context_capability": host["channel_context_capability"],
        "operator_context_evidence": {"model_context": {"facts": []}},
        "prior_agent_runs": payload["prior_agent_runs"],
    }
    direct = cli._direct_specialist_execution_context(
        "Summarize this current request.", workflow_state=state,
        include_slack_scope=True,
    )
    assert direct["slack_scope"]["team_id"] == TEAM
    assert direct["slack_scope"]["channel_id"] == channel
    assert direct["channel_context_capability"]["channel_id"] == channel
    assert "OLD_OUTPUT_SENTINEL" not in json.dumps(direct)


def _query(capability, query: str) -> dict:
    tool = capability.sdk_tools[0]
    arguments = json.dumps({"query": query, "include_profile": True})
    context = ToolContext(
        context=None, tool_name=tool.name, tool_call_id="offline-query",
        tool_arguments=arguments,
    )
    return json.loads(asyncio.run(tool.on_invoke_tool(context, arguments)))


def test_profile_can_cross_channels_but_saved_memory_cannot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(ENABLED_ENV, "true")
    monkeypatch.setenv(TEAM_IDS_ENV, TEAM)
    monkeypatch.setenv(CHANNEL_IDS_ENV, "C999LEGACY")
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_PROFILE_USER_IDS", USER)
    database = f"sqlite:///{tmp_path / 'workspace-memory.sqlite3'}"
    channel_a = "C123PUBLIC"
    channel_b = "G456PRIVATE"
    scope_a = SlackMemoryScope(
        team_id=TEAM, channel_id=channel_a, user_id=USER,
        project_key="keystone-business-agents",
    )
    capture = MemoryCaptureProvenance(
        thread_ts=THREAD_TS, request_ts=REQUEST_TS,
        request_fingerprint=memory_capture_fingerprint(
            scope_a, thread_ts=THREAD_TS, request_ts=REQUEST_TS,
            raw_request="Remember this bounded experiment preference.",
        ),
    )
    record = chief_of_staff_memory_item(
        memory_type="operator_strategy", title="Bounded experiment preference",
        summary="Prefer a bounded experiment with a reviewable result.",
        source_ids=["synthetic:operator"],
    )
    SQLiteStore(database).save_memory_item(
        record, trusted_scope=scope_a, capture_provenance=capture,
    )
    capability_a = operator_capability_from_state(
        _host(channel_a), raw_request="Review background and bounded experiment.",
        database_url=database,
    )
    capability_b = operator_capability_from_state(
        _host(channel_b), raw_request="Review background and bounded experiment.",
        database_url=database,
    )
    capability_other_user = operator_capability_from_state(
        _host(channel_b, user="U222OTHER"),
        raw_request="Review background and bounded experiment.",
        database_url=database,
    )
    assert capability_a is not None and capability_b is not None
    assert capability_other_user is not None
    facts_a = _query(capability_a, "background bounded experiment")["facts"]
    facts_b = _query(capability_b, "background bounded experiment")["facts"]
    ids_a = {fact["memory_id"] for fact in facts_a}
    ids_b = {fact["memory_id"] for fact in facts_b}
    assert "approved_profile:background" in ids_a & ids_b
    assert any(isinstance(item, int) for item in ids_a)
    assert all(not isinstance(item, int) for item in ids_b)
    assert not _query(capability_other_user, "background skills")["facts"]


def test_stale_approved_fact_cannot_seed_another_channel(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(ENABLED_ENV, "true")
    monkeypatch.setenv(TEAM_IDS_ENV, TEAM)
    database = f"sqlite:///{tmp_path / 'seed-isolation.sqlite3'}"
    monkeypatch.setenv("DATABASE_URL", database)
    old_scope = SlackMemoryScope(
        team_id=TEAM, channel_id="C123PUBLIC", user_id=USER,
        project_key="keystone-business-agents",
    )
    old_provenance = MemoryCaptureProvenance(
        thread_ts=THREAD_TS, request_ts=REQUEST_TS,
        request_fingerprint=memory_capture_fingerprint(
            old_scope, thread_ts=THREAD_TS, request_ts=REQUEST_TS,
            raw_request="Remember this old channel preference.",
        ),
    )
    store = SQLiteStore(database)
    store.save_memory_item(chief_of_staff_memory_item(
        memory_type="operator_strategy", title="Old channel preference",
        summary="PRIVATE_OLD_CHANNEL_MEMORY_SENTINEL",
        source_ids=["synthetic:operator"],
    ), trusted_scope=old_scope, capture_provenance=old_provenance)
    old_fact = project_scoped_memory_facts(
        store.retrieve_memory(admission_scope=old_scope),
    ).model_context["facts"][0]
    new_host = _host("G456PRIVATE")
    # A reused workflow state's model-visible facts are not current transport
    # authority. The new channel must query its own admitted local memory.
    new_host["approved_operator_context"] = {"facts": [old_fact]}
    new_host["operator_context_evidence"] = {"model_context": {"facts": [old_fact]}}
    capability = operator_capability_from_state(
        new_host, raw_request="Review current channel guidance.",
        database_url=database,
    )
    assert capability is not None
    assert old_fact not in (capability.evidence.get("model_context") or {}).get("facts", [])
    assert old_fact not in _query(capability, "old channel preference")["facts"]
    direct = cli._direct_specialist_execution_context(
        "Review current channel guidance.",
        workflow_state={
            **new_host,
            "slack_context": new_host["slack_scope"],
        },
        include_slack_scope=True,
        database_url=database,
    )
    assert old_fact["summary"] not in json.dumps(direct)


def test_private_channel_omits_old_sdk_history_and_old_task_prose(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(ENABLED_ENV, "true")
    monkeypatch.setenv(TEAM_IDS_ENV, TEAM)
    policy = resolve_slack_compact_continuity_policy(_host("G123PRIVATE"))
    assert policy.eligible
    session_path = tmp_path / "sdk-history.sqlite3"
    session = build_sqlite_session("workspace-test-history", session_path)
    old_answer = "UNACCEPTED_OLD_ANSWER_SENTINEL"
    asyncio.run(session.add_items([{"role": "assistant", "content": old_answer}]))

    class Answer(BaseModel):
        summary: str

    class LocalModel(Model):
        inputs: list[str] = []

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            from test_lazy_slack_context_backend_matrix import _message, _response

            self.inputs.append(json.dumps(input))
            assert old_answer not in self.inputs[-1]
            assert not tools and not handoffs
            return _response([_message(Answer(summary="Current task only."))])

        def stream_response(self, *args, **kwargs):
            raise AssertionError("Offline acceptance uses one native non-streaming turn.")

    from test_compact_continuity_sessions import Provider

    model = LocalModel()
    agent = build_sdk_agent("chief_of_staff", "Answer current request only.", Answer)
    with activate_continuity_policy(policy):
        assert build_sdk_session(SDKSessionSpec(
            enabled=True, session_id="workspace-test-history",
            database_path=str(session_path), scope="slack",
        )) is None
        result = run_typed_sdk_agent(
            agent=agent, typed_input={"raw_request": "Summarize this current task."},
            output_type=Answer, run_config=build_local_run_config(Provider(model)),
            session=session,
        )
    assert result.output.summary == "Current task only."
    assert len(model.inputs) == 1
    assert result.request_cache["session_attached"] is False
    assert asyncio.run(session.get_items()) == [
        {"role": "assistant", "content": old_answer},
    ]

    item = WorkItem(
        kind=WorkItemKind.RESEARCH_BRIEF, title="Current task",
        request_text=old_answer, current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        audit_notes=[old_answer],
    )
    assert old_answer not in json.dumps(compact_task_context(item))


def test_linked_thread_excludes_earlier_thread_messages() -> None:
    host = _host("G123PRIVATE")
    host["thread_messages"] = [
        {"role": "assistant", "ts": "1794675599.000001",
         "text": "EARLIER_THREAD_SENTINEL"},
        {"role": "user", "ts": THREAD_TS,
         "text": "Current linked task."},
    ]
    env = _workspace_env()
    policy = resolve_slack_compact_continuity_policy(host, environ=env)
    assert policy.eligible
    # This helper uses process configuration because real callers run inside
    # the host process, so install the same effective enrollment for the call.
    from unittest.mock import patch

    with patch.dict("os.environ", env):
        context = compact_linked_thread_context(host, linked_work_item_id="wi_current")
    assert "Current linked task." in json.dumps(context)
    assert "EARLIER_THREAD_SENTINEL" not in json.dumps(context)
