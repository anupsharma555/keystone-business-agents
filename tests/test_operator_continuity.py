"""Independent boundaries for optional graph and operator continuity."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from keystone_agents.runtime.operator_continuity import (
    MAX_OPTIONAL_CONTEXT_BYTES,
    MAX_TASK_CONTEXT_BYTES,
    capture_request_authorized,
    compact_optional_context,
    compact_task_context,
    operator_capability_from_state,
    serialized_context_bytes,
)
from keystone_agents.schemas.web_source import WebSourceAccess
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemArtifactRef,
    WorkItemKind,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemTarget,
)

CHANNEL = "C111TEST"
OLD_ANSWER = "UNACCEPTED_OLD_ANSWER_SENTINEL"


def _host_state() -> dict:
    return {
        "team_id": "T111TEST", "channel_id": CHANNEL, "user_id": "U111TEST",
        "thread_ts": "1794675600.000001", "request_ts": "1794675600.000002",
        "channel_context_capability": {
            "schema_version": "keystone.slack.channel_context.v1",
            "channel_id": CHANNEL,
            "request_ts": "1794675600.000002",
            "oldest_ts": "1794070800.000002",
        },
    }


def _work_item(*, target_url: str = "https://example.org/target") -> WorkItem:
    return WorkItem(
        id="wi_synthetic", kind=WorkItemKind.RESEARCH_BRIEF,
        title="Synthetic selected source",
        request_text=OLD_ANSWER,
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(
            name=OLD_ANSWER, url=target_url,
            metadata={"old_answer": OLD_ANSWER},
        ),
        next_action=WorkItemNextAction(
            action="review_research_brief", description=OLD_ANSWER,
        ),
        artifact_refs=[WorkItemArtifactRef(
            artifact_type="research_brief", artifact_id="brief-synthetic",
            summary=OLD_ANSWER, selected=True,
        )],
        sources=[WorkItemSourceRef(
            source_id="selected-synthetic", url="https://example.org/source",
            evidence_excerpt=OLD_ANSWER,
        )],
        audit_notes=[OLD_ANSWER],
    )


def test_canonical_task_projection_is_whole_bounded_and_excludes_old_prose() -> None:
    work_item = _work_item()
    task = compact_task_context(work_item)
    assert task["id"] == work_item.id
    assert task["sources"] == [
        {"source_id": "selected-synthetic", "url": "https://example.org/source"}
    ]
    assert serialized_context_bytes(task) <= MAX_TASK_CONTEXT_BYTES
    assert OLD_ANSWER not in json.dumps(task)
    assert "request_text" not in task and "audit_notes" not in task


def test_oversize_target_is_omitted_without_changing_source_identity() -> None:
    long_url = "https://example.org/" + "ü" * 1_000
    task = compact_task_context(_work_item(target_url=long_url))
    assert task and "target" not in task
    assert task["sources"][0]["url"] == "https://example.org/source"
    assert serialized_context_bytes(task) <= MAX_TASK_CONTEXT_BYTES
    assert long_url not in json.dumps(task, ensure_ascii=False)


def test_optional_packet_reprojects_mapping_and_caps_utf8_whole_facts() -> None:
    hostile_task = {
        "id": "wi_synthetic", "kind": "research_brief", "status": "blocked",
        "route": "business_research_analyst", "old_answer": OLD_ANSWER,
        "target": {"url": "https://example.org/target", "old_answer": OLD_ANSWER},
        "sources": [{"source_id": "selected", "url": "https://example.org/source",
                     "old_answer": OLD_ANSWER}],
    }
    large_url = "https://example.org/" + "é" * 1_000
    facts = [
        {"memory_id": 1, "summary": "Approved short preference", "source_urls": []},
        {"memory_id": 2, "summary": "Large source", "source_urls": [large_url]},
    ]
    packet = compact_optional_context(hostile_task, {"model_context": {"facts": facts}})
    assert serialized_context_bytes(packet) <= MAX_OPTIONAL_CONTEXT_BYTES
    assert OLD_ANSWER not in json.dumps(packet)
    assert packet["current_work_item"]["target"] == {"url": "https://example.org/target"}
    admitted = packet["approved_operator_context"]["facts"]
    assert facts[0] in admitted
    assert all(fact in facts for fact in admitted)
    if facts[1] in admitted:
        assert admitted[1]["source_urls"] == [large_url]
    else:
        assert large_url not in json.dumps(packet, ensure_ascii=False)


def test_web_source_handle_survives_second_compaction_when_it_fits() -> None:
    url = "https://example.org/selected-study"
    handle = WebSourceAccess(
        source_id="selected", selected_url=url, resolved_url=url,
        snapshot_sha256="a" * 64, total_chars=100,
        start_char=0, end_char=20, next_start_char=20,
    )
    item = _work_item().model_copy(update={
        "sources": [WorkItemSourceRef(source_id="selected", url=url,
                                      web_source_access=handle)],
    })
    task = compact_task_context(item)
    assert task["sources"][0]["web_source_access"]["snapshot_sha256"] == "a" * 64
    packet = compact_optional_context(task, None)
    forwarded = packet["current_work_item"]["sources"][0]["web_source_access"]
    for key in (
        "source_id", "selected_url", "resolved_url", "snapshot_sha256",
        "total_chars", "start_char", "end_char", "next_start_char", "read_tool",
    ):
        assert forwarded[key] == getattr(handle, key)
    assert "limitation" not in forwarded
    assert serialized_context_bytes(packet) <= MAX_OPTIONAL_CONTEXT_BYTES


@pytest.mark.parametrize("raw,allowed", [
    ("Remember this preference for future reviews.", True),
    ("Please save this as a preference in memory.", True),
    ("Summarize the source without saving it.", False),
    ("Previous request: remember this preference.", False),
    ("The old answer said remember this preference.", False),
    ("Remember not to save this reference.", False),
    ("“Remember this preference” is quoted source text.", False),
])
def test_capture_needs_current_affirmative_instruction_and_typed_plan(raw, allowed) -> None:
    plan = SimpleNamespace(intent="reference_capture")
    assert capture_request_authorized(raw, plan) is allowed
    assert not capture_request_authorized(raw, SimpleNamespace(intent="research_brief"))


def test_factory_requires_current_pilot_scope_and_keeps_exact_request_url(
    tmp_path, monkeypatch,
) -> None:
    state = _host_state()
    raw = "Remember https://example.org/article_(2) as my reference."
    database_url = f"sqlite:///{tmp_path / 'operator.db'}"
    monkeypatch.delenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED", raising=False)
    assert operator_capability_from_state(
        state, raw_request=raw, database_url=database_url,
    ) is None
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED", "true")
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_CHANNEL_IDS", CHANNEL)
    for mutation in ("missing_user", "mismatched_request", "missing_descriptor"):
        broken = json.loads(json.dumps(state))
        if mutation == "missing_user":
            broken.pop("user_id")
        elif mutation == "mismatched_request":
            broken["channel_context_capability"]["request_ts"] = "1794675601.000002"
        else:
            broken.pop("channel_context_capability")
        assert operator_capability_from_state(
            broken, raw_request=raw, database_url=database_url,
        ) is None
    capability = operator_capability_from_state(
        state, raw_request=raw, database_url=database_url,
    )
    assert capability is not None
    assert capability._source_refs["request-url:1"] == "https://example.org/article_(2)"
    assert capability.max_bytes <= 4096


def test_profile_factory_requires_current_user_in_host_allowlist(
    tmp_path, monkeypatch,
) -> None:
    state = _host_state()
    database_url = f"sqlite:///{tmp_path / 'operator.db'}"
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED", "true")
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_CHANNEL_IDS", CHANNEL)
    monkeypatch.setenv("KEYSTONE_SLACK_COMPACT_CONTINUITY_PROFILE_USER_IDS", "U111TEST")
    allowed = operator_capability_from_state(
        state, raw_request="Summarize my approved professional background.",
        database_url=database_url,
    )
    assert allowed is not None and allowed.include_approved_profile
    assert json.loads(allowed._query(
        "approved professional background", include_profile=True,
    ))["facts"]

    other_state = {**state, "user_id": "U222TEST"}
    other = operator_capability_from_state(
        other_state, raw_request="Summarize my approved professional background.",
        database_url=database_url,
    )
    assert other is not None and not other.include_approved_profile
    assert json.loads(other._query(
        "approved professional background", include_profile=True,
    ))["facts"] == []


def test_linked_thread_context_bounds_prior_exchange_and_excludes_current_and_future(monkeypatch):
    from keystone_agents.runtime.operator_continuity import (
        MAX_LINKED_THREAD_CONTEXT_BYTES,
        compact_linked_thread_context,
    )
    monkeypatch.setenv('KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED', 'true')
    monkeypatch.setenv('KEYSTONE_SLACK_COMPACT_CONTINUITY_CHANNEL_IDS', CHANNEL)
    host = _host_state()
    host['request_ts'] = '1794675700.000001'
    host['channel_context_capability']['request_ts'] = host['request_ts']
    host['channel_context_capability']['oldest_ts'] = '1794070900.000001'
    host['thread_messages'] = [
        {'ts': '1794675600.000001', 'role': 'user', 'text': 'Review the selected study.'},
        {'ts': '1794675601.000001', 'role': 'assistant',
         'text': 'The review failed before reading.'},
        {'ts': host['request_ts'], 'role': 'user', 'text': 'CURRENT_TURN_SENTINEL'},
        {'ts': '1794675800.000001', 'role': 'assistant', 'text': 'FUTURE_SENTINEL'},
    ]
    packet = compact_linked_thread_context(host, linked_work_item_id='wi_synthetic')
    assert [item['role'] for item in packet['messages']] == ['user', 'assistant']
    assert packet['thread_started_at'] == '2026-11-14T12:00:00.000001-05:00'
    assert packet['messages'][0]['sent_at'] == packet['thread_started_at']
    assert 'review failed' in json.dumps(packet)
    assert 'CURRENT_TURN_SENTINEL' not in json.dumps(packet)
    assert 'FUTURE_SENTINEL' not in json.dumps(packet)
    assert serialized_context_bytes(packet) <= MAX_LINKED_THREAD_CONTEXT_BYTES
    assert 'not evidence' in packet['scope']
    assert compact_linked_thread_context(host, linked_work_item_id='') == {}
    host['channel_id'] = 'COTHER'
    assert compact_linked_thread_context(host, linked_work_item_id='wi_synthetic') == {}


def test_linked_thread_context_caps_unicode_and_falls_back_to_host_root(monkeypatch):
    from keystone_agents.runtime.operator_continuity import (
        MAX_LINKED_THREAD_CONTEXT_BYTES,
        compact_linked_thread_context,
    )
    monkeypatch.setenv('KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED', 'true')
    monkeypatch.setenv('KEYSTONE_SLACK_COMPACT_CONTINUITY_CHANNEL_IDS', CHANNEL)
    host = _host_state()
    host['thread_root_request'] = 'Review selected source ' + ('x' * 1600)
    packet = compact_linked_thread_context(host, linked_work_item_id='wi_synthetic')
    assert len(packet['messages']) == 1 and packet['messages'][0]['truncated']
    host['thread_messages'] = [
        {'ts': host['thread_ts'], 'role': 'user', 'text': '\u4e2d' * 800},
        {'ts': host['thread_ts'], 'role': 'assistant', 'text': 'Saved source unavailable.'},
    ]
    packet = compact_linked_thread_context(host, linked_work_item_id='wi_synthetic')
    assert serialized_context_bytes(packet) <= MAX_LINKED_THREAD_CONTEXT_BYTES
    assert len(packet.get('messages', [])) <= 2


@pytest.mark.parametrize('mutation', ['valid', 'other_task', 'other_thread', 'future', 'completed'])
def test_latest_thread_attempt_is_current_host_scoped_and_not_task_status(monkeypatch, mutation):
    from keystone_agents.runtime.operator_continuity import compact_latest_thread_attempt

    monkeypatch.setenv('KEYSTONE_SLACK_COMPACT_CONTINUITY_ENABLED', 'true')
    monkeypatch.setenv('KEYSTONE_SLACK_COMPACT_CONTINUITY_CHANNEL_IDS', CHANNEL)
    host = _host_state()
    attempt = {
        'attempt_run_id': 'attempt_synthetic', 'work_item_id': 'wi_synthetic',
        'channel_id': CHANNEL, 'thread_ts': host['thread_ts'],
        'request_ts': host['thread_ts'], 'status': 'failed',
        'old_answer': OLD_ANSWER,
    }
    host['latest_thread_attempt'] = attempt
    if mutation == 'other_task':
        attempt['work_item_id'] = 'wi_other'
    elif mutation == 'other_thread':
        attempt['thread_ts'] = '1794675500.000001'
    elif mutation == 'future':
        attempt['request_ts'] = host['request_ts']
    elif mutation == 'completed':
        attempt['status'] = 'completed'
    packet = compact_latest_thread_attempt(host, linked_work_item_id='wi_synthetic')
    if mutation == 'valid':
        assert packet['status'] == 'failed' and packet['work_item_id'] == 'wi_synthetic'
        assert OLD_ANSWER not in json.dumps(packet)
    else:
        assert packet == {}
