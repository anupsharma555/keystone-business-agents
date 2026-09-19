from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

from keystone_agents import workflow_runner
from keystone_agents.agent_tool_policy import tool_name_for_policy
from keystone_agents.agents.preprints_context import build_preprints_context_agent
from keystone_agents.agents.rss_context import build_rss_context_agent
from keystone_agents.schemas.signal_lifecycle import (
    SIGNAL_LIFECYCLE_ARTIFACT_TYPE,
    SignalLifecycleStatus,
    SignalTriggerContext,
)
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
    WorkItemStatus,
)
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools.signal_lifecycle_tools import (
    advance_signal_lifecycle_checkpoint_impl,
    inspect_signal_lifecycle_impl,
    prepare_signal_lifecycle_checkpoint_impl,
)
from keystone_agents.work_items import build_context_pack_for_route
from keystone_agents.workflow_runner import advance_work_item


def _concurrent_prepare_worker(
    database_url: str,
    work_item_id: str,
    start_event: object,
    result_queue: object,
) -> None:
    start_event.wait()  # type: ignore[attr-defined]
    result = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=work_item_id,
        source_kind="rss",
        trigger_ref=f"concurrent-{work_item_id}",
        dedupe_scope="concurrent-rss-monitor",
        candidate_items_json=json.dumps(
            [{"feed_item_id": "rss-shared", "url": "https://example.com/shared"}]
        ),
        dry_run=False,
        database_url=database_url,
    )
    result_queue.put(  # type: ignore[attr-defined]
        (work_item_id, result["status"], result["admission_guard"])
    )


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'signals.db'}"


def _work_item(store: SQLiteStore, *, kind: str, suffix: str) -> WorkItem:
    route = (
        WorkItemRoute.PREPRINTS_CONTEXT_AGENT
        if kind == "preprints"
        else WorkItemRoute.RSS_CONTEXT_AGENT
    )
    item = WorkItem(
        id=f"wi_signal_{suffix}",
        kind=WorkItemKind.RESEARCH_BRIEF,
        title=f"{kind} signal {suffix}",
        current_route=route,
        signal_trigger=SignalTriggerContext(
            source_kind=kind,
            trigger_ref=f"trigger-{suffix}",
            dedupe_scope=f"weekly-{kind}",
            trigger_label=f"Weekly {kind}",
            scheduled=True,
        ),
    )
    store.save_work_item(item)
    return item


def _preprint(version: int) -> dict[str, object]:
    return {
        "feed_item_id": f"medrxiv:10.1101/2026.01.01.123456v{version}",
        "title": "A behavioral health model evaluation",
        "url": f"https://www.medrxiv.org/content/10.1101/2026.01.01.123456v{version}",
        "published_at": f"2026-07-0{version}",
        "publication_ids": [f"10.1101/2026.01.01.123456v{version}"],
    }


def _complete_checkpoint(
    *,
    database_url: str,
    work_item_id: str,
    trigger_id: str,
) -> None:
    for stage in (
        "context_retrieved",
        "handoff_prepared",
        "review_completed",
        "completed",
    ):
        result = advance_signal_lifecycle_checkpoint_impl(
            work_item_id=work_item_id,
            trigger_id=trigger_id,
            completed_stage=stage,
            dry_run=False,
            database_url=database_url,
        )
        assert result["status"] in {"ready", "completed"}


def test_signal_lifecycle_dry_run_is_inspectable_without_work_item_mutation(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    item = _work_item(store, kind="rss", suffix="dry")

    result = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=item.id,
        source_kind="rss",
        trigger_ref="rss-event-2026-08-02",
        dedupe_scope="weekly-rss",
        candidate_items_json=json.dumps(
            [
                {
                    "feed_item_id": "rss-1",
                    "url": "https://example.com/article?utm_source=test",
                    "published_at": "2026-08-02",
                }
            ]
        ),
        scheduled=True,
        dry_run=True,
        database_url=database_url,
    )

    assert result["status"] == "dry_run"
    assert result["checkpoint"]["status"] == "planned"
    assert result["checkpoint"]["admitted_revision_ids"]
    assert result["provider_calls_made"] == 0
    assert result["external_writes_performed"] is False
    loaded = store.get_work_item(item.id)
    assert loaded is not None
    assert loaded.artifact_refs == []
    assert store.list_work_item_events(item.id) == []


def test_signal_lifecycle_inspection_resolves_latest_checkpoint_by_source(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    older = _work_item(store, kind="rss", suffix="source-a-older")
    prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=older.id,
        source_kind="rss",
        trigger_ref="rss-source-older",
        dedupe_scope="rss-source-scope",
        candidate_items_json="[]",
        dry_run=False,
        database_url=database_url,
    )
    newer = _work_item(store, kind="rss", suffix="source-z-newer")
    prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=newer.id,
        source_kind="rss",
        trigger_ref="rss-source-newer",
        dedupe_scope="rss-source-scope",
        candidate_items_json="[]",
        dry_run=False,
        database_url=database_url,
    )

    result = inspect_signal_lifecycle_impl(
        source_kind="rss",
        database_url=database_url,
    )

    assert result["status"] == "success"
    assert result["resolution"] == "latest_source_checkpoint"
    assert result["work_item_id"] == newer.id
    assert result["source_kind"] == "rss"
    assert result["checkpoint_count"] == 1
    assert result["external_writes_performed"] is False


def test_signal_lifecycle_source_inspection_reports_no_saved_checkpoint(
    tmp_path: Path,
) -> None:
    result = inspect_signal_lifecycle_impl(
        source_kind="preprints",
        database_url=_database_url(tmp_path),
    )

    assert result["status"] == "no_checkpoint"
    assert result["reason"] == "no_saved_checkpoint_for_source"
    assert result["source_kind"] == "preprints"
    assert result["checkpoint_count"] == 0
    assert result["external_writes_performed"] is False


def test_preprint_lifecycle_deduplicates_versions_and_resumes_failed_stage(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    item = _work_item(store, kind="preprints", suffix="resume")

    prepared = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=item.id,
        source_kind="preprints",
        trigger_ref=item.signal_trigger.trigger_ref,  # type: ignore[union-attr]
        dedupe_scope=item.signal_trigger.dedupe_scope,  # type: ignore[union-attr]
        candidate_items_json=json.dumps([_preprint(1), _preprint(2)]),
        trigger_label="Weekly preprint scan",
        scheduled=True,
        dry_run=False,
        database_url=database_url,
    )
    checkpoint = prepared["checkpoint"]
    trigger_id = checkpoint["trigger_id"]

    assert prepared["status"] == "checkpointed"
    assert [decision["disposition"] for decision in checkpoint["decisions"]] == [
        "duplicate",
        "admitted",
    ]
    assert len(checkpoint["admitted_revision_ids"]) == 1
    assert len(checkpoint["duplicate_revision_ids"]) == 1

    first_stage = advance_signal_lifecycle_checkpoint_impl(
        work_item_id=item.id,
        trigger_id=trigger_id,
        completed_stage="context_retrieved",
        provider_calls_made=1,
        dry_run=False,
        database_url=database_url,
    )
    failed = advance_signal_lifecycle_checkpoint_impl(
        work_item_id=item.id,
        trigger_id=trigger_id,
        completed_stage="handoff_prepared",
        outcome="failed",
        failure_code="SyntheticProviderFailure",
        failure_summary="Injected after context retrieval.",
        dry_run=False,
        database_url=database_url,
    )

    assert first_stage["checkpoint"]["completed_stages"] == ["context_retrieved"]
    assert failed["status"] == "partial_failure"
    assert failed["checkpoint"]["next_stage"] == "handoff_prepared"
    assert failed["checkpoint"]["safe_next_action"] == (
        "Retry the same WorkItem at handoff_prepared."
    )

    resumed = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=item.id,
        source_kind="preprints",
        trigger_ref=item.signal_trigger.trigger_ref,  # type: ignore[union-attr]
        dedupe_scope=item.signal_trigger.dedupe_scope,  # type: ignore[union-attr]
        candidate_items_json=json.dumps([_preprint(2)]),
        dry_run=False,
        database_url=database_url,
    )
    assert resumed["status"] == "resumed"
    assert resumed["checkpoint"]["next_stage"] == "handoff_prepared"
    assert resumed["checkpoint"]["completed_stages"] == ["context_retrieved"]

    for stage in ("handoff_prepared", "review_completed", "completed"):
        final = advance_signal_lifecycle_checkpoint_impl(
            work_item_id=item.id,
            trigger_id=trigger_id,
            completed_stage=stage,
            dry_run=False,
            database_url=database_url,
        )
    assert final["status"] == "completed"
    assert final["checkpoint"]["completed_stages"] == [
        "context_retrieved",
        "handoff_prepared",
        "review_completed",
        "completed",
    ]

    reloaded = SQLiteStore(database_url)
    inspected = inspect_signal_lifecycle_impl(
        work_item_id=item.id,
        trigger_id=trigger_id,
        database_url=database_url,
    )
    assert inspected["checkpoints"][0]["status"] == SignalLifecycleStatus.COMPLETED
    assert any(
        artifact.artifact_type == SIGNAL_LIFECYCLE_ARTIFACT_TYPE
        for artifact in reloaded.get_work_item(item.id).artifact_refs  # type: ignore[union-attr]
    )


def test_signal_lifecycle_suppresses_completed_revisions_and_duplicate_triggers(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    first = _work_item(store, kind="preprints", suffix="one")
    first_result = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=first.id,
        source_kind="preprints",
        trigger_ref="preprint-trigger-one",
        dedupe_scope="preprint-monitor",
        candidate_items_json=json.dumps([_preprint(2)]),
        dry_run=False,
        database_url=database_url,
    )
    _complete_checkpoint(
        database_url=database_url,
        work_item_id=first.id,
        trigger_id=first_result["checkpoint"]["trigger_id"],
    )

    second = _work_item(store, kind="preprints", suffix="two")
    second_result = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=second.id,
        source_kind="preprints",
        trigger_ref="preprint-trigger-two",
        dedupe_scope="preprint-monitor",
        candidate_items_json=json.dumps([_preprint(2), _preprint(3)]),
        dry_run=False,
        database_url=database_url,
    )
    dispositions = [
        decision["disposition"] for decision in second_result["checkpoint"]["decisions"]
    ]
    assert dispositions == ["duplicate", "updated"]
    assert second_result["checkpoint"]["decisions"][0]["matched_work_item_id"] == first.id

    duplicate = _work_item(store, kind="preprints", suffix="duplicate")
    duplicate_result = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=duplicate.id,
        source_kind="preprints",
        trigger_ref="preprint-trigger-two",
        dedupe_scope="preprint-monitor",
        candidate_items_json=json.dumps([_preprint(3)]),
        dry_run=False,
        database_url=database_url,
    )
    assert duplicate_result["status"] == "duplicate_trigger"
    assert duplicate_result["checkpoint"]["canonical_work_item_id"] == second.id
    assert duplicate_result["checkpoint"]["next_stage"] is None


def test_signal_lifecycle_suppresses_same_revision_while_canonical_work_is_active(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    first = _work_item(store, kind="preprints", suffix="active-one")
    prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=first.id,
        source_kind="preprints",
        trigger_ref="active-trigger-one",
        dedupe_scope="active-preprint-monitor",
        candidate_items_json=json.dumps([_preprint(2)]),
        dry_run=False,
        database_url=database_url,
    )

    second = _work_item(store, kind="preprints", suffix="active-two")
    duplicate = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=second.id,
        source_kind="preprints",
        trigger_ref="active-trigger-two",
        dedupe_scope="active-preprint-monitor",
        candidate_items_json=json.dumps([_preprint(2)]),
        dry_run=False,
        database_url=database_url,
    )

    assert duplicate["status"] == "duplicate_trigger"
    assert duplicate["checkpoint"]["canonical_work_item_id"] == first.id
    assert duplicate["checkpoint"]["admitted_revision_ids"] == []


def test_signal_lifecycle_deduplication_does_not_expire_after_500_work_items(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    first = _work_item(store, kind="rss", suffix="old-canonical")
    prepared = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=first.id,
        source_kind="rss",
        trigger_ref="old-trigger",
        dedupe_scope="long-running-rss-monitor",
        candidate_items_json=json.dumps(
            [{"feed_item_id": "rss-old", "url": "https://example.com/old"}]
        ),
        dry_run=False,
        database_url=database_url,
    )
    _complete_checkpoint(
        database_url=database_url,
        work_item_id=first.id,
        trigger_id=prepared["checkpoint"]["trigger_id"],
    )
    for index in range(501):
        store.save_work_item(
            WorkItem(
                id=f"wi_filler_{index:03d}",
                kind=WorkItemKind.RESEARCH_BRIEF,
                title=f"Filler {index}",
                current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            )
        )

    duplicate_item = _work_item(store, kind="rss", suffix="new-duplicate")
    duplicate = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=duplicate_item.id,
        source_kind="rss",
        trigger_ref="new-trigger",
        dedupe_scope="long-running-rss-monitor",
        candidate_items_json=json.dumps(
            [{"feed_item_id": "rss-old", "url": "https://example.com/old"}]
        ),
        dry_run=False,
        database_url=database_url,
    )

    assert duplicate["status"] == "duplicate_trigger"
    assert duplicate["checkpoint"]["canonical_work_item_id"] == first.id


def test_signal_lifecycle_blocks_wrong_owner_and_out_of_order_transition(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    wrong = WorkItem(
        id="wi_signal_wrong",
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Wrong route",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )
    store.save_work_item(wrong)
    blocked = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=wrong.id,
        source_kind="rss",
        trigger_ref="rss-wrong",
        dedupe_scope="rss-monitor",
        candidate_items_json="[]",
        dry_run=False,
        database_url=database_url,
    )
    assert blocked["reason"] == "work_item_route_does_not_own_signal_kind"

    owned = _work_item(store, kind="rss", suffix="ordered")
    prepared = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=owned.id,
        source_kind="rss",
        trigger_ref="rss-ordered",
        dedupe_scope="rss-monitor",
        candidate_items_json=json.dumps(
            [{"feed_item_id": "rss-1", "url": "https://example.com/a"}]
        ),
        dry_run=False,
        database_url=database_url,
    )
    out_of_order = advance_signal_lifecycle_checkpoint_impl(
        work_item_id=owned.id,
        trigger_id=prepared["checkpoint"]["trigger_id"],
        completed_stage="review_completed",
        dry_run=False,
        database_url=database_url,
    )
    assert out_of_order["status"] == "blocked"
    assert out_of_order["expected_stage"] == "context_retrieved"
    assert store.list_work_item_events(owned.id)[-1].event_type == (
        "signal_trigger_checkpointed"
    )


def test_signal_trigger_is_preserved_in_context_packs_and_agents_own_tools(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    item = _work_item(store, kind="rss", suffix="pack")
    pack = build_context_pack_for_route(item, WorkItemRoute.RSS_CONTEXT_AGENT, store=store)

    assert pack.signal_trigger == item.signal_trigger
    rss_tools = {tool_name_for_policy(tool) for tool in build_rss_context_agent().tools}
    preprint_tools = {
        tool_name_for_policy(tool) for tool in build_preprints_context_agent().tools
    }
    expected = {
        "inspect_signal_lifecycle",
        "prepare_signal_lifecycle_checkpoint",
        "advance_signal_lifecycle_checkpoint",
    }
    assert expected <= rss_tools
    assert expected <= preprint_tools


def test_zero_admission_is_explicit_for_invalid_and_multi_owner_duplicates(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    invalid_item = _work_item(store, kind="rss", suffix="invalid")
    invalid = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=invalid_item.id,
        source_kind="rss",
        trigger_ref="invalid-trigger",
        dedupe_scope="zero-admission-rss",
        candidate_items_json=json.dumps([{"title": "No stable id"}]),
        dry_run=False,
        database_url=database_url,
    )
    assert invalid["status"] == "no_action"
    assert invalid["checkpoint"]["status"] == "no_action"
    assert invalid["checkpoint"]["next_stage"] is None

    first = _work_item(store, kind="rss", suffix="owner-one")
    second = _work_item(store, kind="rss", suffix="owner-two")
    for item, url in (
        (first, "https://example.com/one"),
        (second, "https://example.com/two"),
    ):
        prepare_signal_lifecycle_checkpoint_impl(
            work_item_id=item.id,
            source_kind="rss",
            trigger_ref=f"owner-{item.id}",
            dedupe_scope="multi-owner-rss",
            candidate_items_json=json.dumps([{"feed_item_id": item.id, "url": url}]),
            dry_run=False,
            database_url=database_url,
        )
    duplicate_item = _work_item(store, kind="rss", suffix="multi-owner-duplicate")
    duplicate = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=duplicate_item.id,
        source_kind="rss",
        trigger_ref="multi-owner-duplicate",
        dedupe_scope="multi-owner-rss",
        candidate_items_json=json.dumps(
            [
                {"feed_item_id": first.id, "url": "https://example.com/one"},
                {"feed_item_id": second.id, "url": "https://example.com/two"},
            ]
        ),
        dry_run=False,
        database_url=database_url,
    )

    assert duplicate["status"] == "duplicate_trigger"
    assert duplicate["checkpoint"]["canonical_work_item_id"] == ""
    assert duplicate["checkpoint"]["canonical_work_item_ids"] == sorted(
        [first.id, second.id]
    )
    assert duplicate["checkpoint"]["admitted_revision_ids"] == []


def test_signal_lifecycle_advance_rejects_checkpoint_after_route_changes(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    item = _work_item(store, kind="rss", suffix="route-change")
    prepared = prepare_signal_lifecycle_checkpoint_impl(
        work_item_id=item.id,
        source_kind="rss",
        trigger_ref="route-change",
        dedupe_scope="route-change-rss",
        candidate_items_json=json.dumps(
            [{"feed_item_id": "rss-route", "url": "https://example.com/route"}]
        ),
        dry_run=False,
        database_url=database_url,
    )
    changed = (store.get_work_item(item.id) or item).model_copy(
        update={"current_route": WorkItemRoute.PREPRINTS_CONTEXT_AGENT}
    )
    store.save_work_item(changed)

    result = advance_signal_lifecycle_checkpoint_impl(
        work_item_id=item.id,
        trigger_id=prepared["checkpoint"]["trigger_id"],
        completed_stage="context_retrieved",
        dry_run=False,
        database_url=database_url,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "signal_checkpoint_kind_does_not_match_work_item_route"


def test_single_host_admission_guard_serializes_concurrent_revision_claims(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    first = _work_item(store, kind="rss", suffix="concurrent-one")
    second = _work_item(store, kind="rss", suffix="concurrent-two")
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    result_queue = context.Queue()
    processes = [
        context.Process(
            target=_concurrent_prepare_worker,
            args=(database_url, item.id, start_event, result_queue),
        )
        for item in (first, second)
    ]
    for process in processes:
        process.start()
    start_event.set()
    results = [result_queue.get(timeout=15) for _ in processes]
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert sorted(status for _work_item_id, status, _guard in results) == [
        "checkpointed",
        "duplicate_trigger",
    ]
    assert {guard for _work_item_id, _status, guard in results} == {
        "sqlite_path_advisory_file_lock"
    }


def test_triggered_workflow_checkpoints_before_done_and_reuses_terminal_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    item = _work_item(store, kind="rss", suffix="workflow-terminal")
    calls = 0
    source_items = [
        {
            "feed_item_id": "rss-workflow-1",
            "title": "Clinical AI implementation update",
            "url": "https://example.com/workflow-1",
            "source": "fixture",
            "published_at": "2026-08-02",
            "tags": ["clinical-ai"],
            "summary": "A bounded implementation signal.",
        }
    ]

    def retrieve(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return {"status": "success", "items": source_items}

    monkeypatch.setattr(
        workflow_runner,
        "retrieve_announcement_feed_history_impl",
        retrieve,
    )
    request = WorkflowRunRequest(
        request_text="Rank the triggered RSS signal read-only.",
        work_item_id=item.id,
        requested_route=WorkItemRoute.RSS_CONTEXT_AGENT,
        database_url=database_url,
        save=True,
    )

    first = workflow_runner.advance_work_item_manager_loop(request, max_steps=5)
    second = advance_work_item(request)

    assert first.status == WorkItemStatus.DONE
    assert first.artifact_refs[0].metadata["signal_lifecycle_route"] == (
        WorkItemRoute.RSS_CONTEXT_AGENT.value
    )
    assert second.status == WorkItemStatus.DONE
    assert second.advanced is False
    assert calls == 1
    events = store.list_work_item_events(item.id)
    event_types = [event.event_type for event in events]
    trigger_event = next(
        event for event in events if event.event_type == "signal_trigger_checkpointed"
    )
    assert trigger_event.metadata["admission_guard"] == (
        "sqlite_path_advisory_file_lock"
    )
    assert event_types.index("signal_trigger_checkpointed") < event_types.index(
        "artifact_attached"
    )
    assert event_types.index("artifact_attached") < event_types.index(
        "signal_lifecycle_stage_completed"
    )
    checkpoint = inspect_signal_lifecycle_impl(
        work_item_id=item.id,
        database_url=database_url,
    )["checkpoints"][0]
    assert checkpoint["completed_stages"] == [
        "context_retrieved",
        "handoff_prepared",
        "review_completed",
        "completed",
    ]


def test_triggered_workflow_resumes_after_partial_progress_without_provider_reread(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    item = _work_item(store, kind="preprints", suffix="workflow-resume")
    retrieval_calls = 0

    def retrieve(*_args, **_kwargs):
        nonlocal retrieval_calls
        retrieval_calls += 1
        return {"status": "success", "items": [_preprint(2)]}

    original_advance = workflow_runner.advance_signal_lifecycle_checkpoint_impl
    injected = False

    def fail_once(**kwargs):
        nonlocal injected
        stage = str(kwargs["completed_stage"])
        if stage.endswith("handoff_prepared") and not injected:
            injected = True
            return original_advance(
                **{
                    **kwargs,
                    "outcome": "failed",
                    "failure_summary": "Injected after context persistence.",
                }
            )
        return original_advance(**kwargs)

    monkeypatch.setattr(
        workflow_runner,
        "retrieve_announcement_feed_history_impl",
        retrieve,
    )
    monkeypatch.setattr(
        workflow_runner,
        "advance_signal_lifecycle_checkpoint_impl",
        fail_once,
    )
    request = WorkflowRunRequest(
        request_text="Review the triggered preprint signal read-only.",
        work_item_id=item.id,
        requested_route=WorkItemRoute.PREPRINTS_CONTEXT_AGENT,
        database_url=database_url,
        save=True,
    )

    paused = workflow_runner.advance_work_item_manager_loop(request, max_steps=5)
    resumed = workflow_runner.advance_work_item_manager_loop(request, max_steps=5)

    assert paused.status == WorkItemStatus.IN_PROGRESS
    assert paused.next_action is not None
    assert paused.next_action.action == "resume_signal_handoff_prepared"
    assert resumed.status == WorkItemStatus.DONE
    assert retrieval_calls == 1
    checkpoint = inspect_signal_lifecycle_impl(
        work_item_id=item.id,
        database_url=database_url,
    )["checkpoints"][0]
    assert checkpoint["status"] == "completed"
    assert checkpoint["completed_stages"].count("context_retrieved") == 1
    context_rows = [
        row
        for row in store.fetch_all("work_item_artifacts")
        if row["work_item_id"] == item.id
        and row["artifact_type"] == "preprints_context"
    ]
    assert len(context_rows) == 1
