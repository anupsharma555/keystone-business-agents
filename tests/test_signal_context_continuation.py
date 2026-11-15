"""Verified RSS/Preprints evidence survives one same-thread owner change."""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from keystone_agents.agents import orchestrator as orchestrator_module
from keystone_agents.agents.preprints_context import build_preprints_context_agent
from keystone_agents.agents.rss_context import build_rss_context_agent
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.instruction_following import InstructionFollowingRepairOutput
from keystone_agents.langgraph_workflow import (
    advance_work_item_manager_loop_with_optional_langgraph,
)
from keystone_agents.planning.composition_admission import (
    resolve_provider_free_composition_admission,
)
from keystone_agents.schemas.execution_request import DirectAgentResponse
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.schemas.orchestrator import OrchestratorResult
from keystone_agents.schemas.work_item import WorkflowRunRequest, WorkItemRoute, WorkItemSourceRef
from keystone_agents.slack_actions import _verified_signal_context_from_agent_run
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import announcement_context_tools
from keystone_agents.work_items import create_or_load_work_item
from keystone_agents.workflow_runner import (
    _dedupe_work_item_sources,
    _manager_review_failure_is_authoritative,
    prepare_work_item_step,
)

TEAM_ID = "T-SIGNAL"
CHANNEL_ID = "C-SIGNAL"
THREAD_TS = "1789000000.000100"
ROOT_REQUEST_TS = "1789000001.000100"
FOLLOWUP_REQUEST_TS = "1789000002.000100"


def _context_only_plan(request: str = "Should we pursue that or monitor it?") -> ManualRequestPlan:
    return ManualRequestPlan(
        source="canonical:orchestrator_context_only",
        requested_agent="opportunity_scout",
        target_agent="opportunity_scout",
        intent="route_request",
        task_objective="route_or_continue",
        expected_artifact_type="none",
        objective=request,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            prior_context_dependency="selected_context",
            permission_state="read_only",
            audience_scope="internal",
        ),
    )


def _route() -> OrchestratorResult:
    return OrchestratorResult(
        route="opportunity_scout",
        workflow=["opportunity_scout"],
        routing_mode="llm",
        context_only_response=True,
        rationale=(
            "Assess the already selected public signal without another provider read."
        ),
    )


def _source(kind: str, index: int = 1) -> dict[str, str]:
    prefix = "preprint" if kind == "preprints" else "rss"
    return {
        "feed_item_id": f"{prefix}-clinical-ai-{index}",
        "title": f"Clinical-AI evaluation signal {index}",
        "url": f"https://public.example/{prefix}/clinical-ai-{index}",
        "source": f"{prefix}-history",
        "published_at": f"2026-09-{13 + index:02d}",
        "summary": "The public item describes a possible independent evaluation need.",
        "selection_reason": "It directly matches the bounded clinical-AI monitoring ask.",
        "relevance_to_keystone": "KNI could assess evidence quality if the signal matures.",
        "evidence_status": "verified_signal_history",
        "source_basis": "Synthetic bounded provider evidence for offline contract tests.",
        "evidence_notes": ["Stored public history; no fresh web verification."],
    }


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'signal-context.db'}"


def _signal_evidence(
    *,
    tool_name: str,
    candidates: list[dict[str, str]],
) -> dict[str, Any]:
    safe_keys = (
        "feed_item_id",
        "title",
        "url",
        "published_at",
        "source",
        "summary",
        "tags",
        "selected",
        "relevance_status",
        "selection_reason",
        "source_basis",
        "evidence_status",
        "publication_ids",
        "evidence_notes",
    )
    sanitized = [
        {
            ("candidate_id" if key == "feed_item_id" else key): candidate[key]
            for key in safe_keys
            if key in candidate
        }
        for candidate in candidates
    ]
    payload: dict[str, Any] = {
        "schema": "keystone.signal_decision_evidence.v1",
        "source": "first_attempt_model_called_history_tool",
        "tool_name": tool_name,
        "tool_call_count": 1,
        "model_tool_call_count": 1,
        "blocked_tool_call_count": 0,
        "unresolved_tool_call_count": 0,
        "tool_output_count": 1,
        "candidate_count": len(sanitized),
        "candidate_ids": [item["candidate_id"] for item in sanitized],
        "candidates": sanitized,
        "raw_provider_payload_retained": False,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    payload["evidence_fingerprint"] = sha256(canonical.encode("utf-8")).hexdigest()
    return payload


def _rehash_signal_evidence(evidence: dict[str, Any]) -> None:
    evidence.pop("evidence_fingerprint", None)
    canonical = json.dumps(
        evidence,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    evidence["evidence_fingerprint"] = sha256(canonical.encode("utf-8")).hexdigest()


def _add_exact_signal_read(
    evidence: dict[str, Any], *, selected_id: str
) -> None:
    """Model one history call followed by a retained, candidate-bound exact read."""

    candidate = next(
        item for item in evidence["candidates"] if item["candidate_id"] == selected_id
    )
    snapshot = "a" * 64
    evidence_id = f"evidence:{'b' * 24}"
    candidate.update(
        {
            "selected_evidence_read_required": True,
            "source_snapshot_sha256": snapshot,
            "evidence_ids": [evidence_id],
        }
    )
    evidence.update(
        {
            "tool_call_count": 2,
            "history_tool_call_count": 1,
            "evidence_read_call_count": 1,
            "evidence_tool_name": (
                "read_preprint_announcement_evidence"
                if evidence["tool_name"] == "retrieve_preprint_announcement_history"
                else "read_rss_announcement_evidence"
            ),
            "model_tool_call_count": 2,
            "tool_output_count": 2,
            "tool_result_item_counts": [len(evidence["candidates"])],
            "bounded_reformulation": {"used": False, "valid": True},
            "evidence_reads": [
                {
                    "status": "success",
                    "read_mode": "evidence_content",
                    "feed_item_id": selected_id,
                    "evidence_id": evidence_id,
                    "content_status": "saved_content_available",
                    "content_complete": True,
                    "text_char_count": 47,
                    "text_sha256": "c" * 64,
                    "snapshot_sha256": snapshot,
                }
            ],
        }
    )
    _rehash_signal_evidence(evidence)


def _save_signal_run(
    database_url: str,
    *,
    kind: str = "rss",
    sources: list[dict[str, str]] | None = None,
    public_status: str = "completed",
    completion_confirmed: bool = True,
    validator_status: str = "accepted",
    team_id: str = TEAM_ID,
    channel_id: str = CHANNEL_ID,
    thread_ts: str = THREAD_TS,
    request_ts: str = ROOT_REQUEST_TS,
    run_status: str = "success",
    dry_run: bool = False,
) -> str:
    route = "preprints_context_agent" if kind == "preprints" else "rss_context_agent"
    tool_name = (
        "retrieve_preprint_announcement_history"
        if kind == "preprints"
        else "retrieve_rss_announcement_history"
    )
    selected_sources = sources or [_source(kind)]
    selected_ids = [source["feed_item_id"] for source in selected_sources]
    output = {
        "status": "done",
        "route": route,
        "output": {
            "retrieved_item_ids": selected_ids,
            "articles": selected_sources,
            "decision": {
                "decision_owner": "specialist_agent",
                "decision_stage": "signal_relevance_selection",
                "selected_candidate_ids": selected_ids,
                "needs_more_context": False,
                "limitations": ["Stored public history; no fresh web verification."],
            },
        },
        "public_result": {
            "status": public_status,
            "text": f"Selected {len(selected_sources)} public signal(s).",
            "completion_confirmed": completion_confirmed,
        },
        "slack_run_provenance": {
            "schema": "keystone.slack.run_provenance.v1",
            "context_validated": True,
            "team_id": team_id,
            "channel_id": channel_id,
            "thread_ts": thread_ts,
            "request_ts": request_ts,
        },
        "internal_decision_ownership": {
            "validator_outcome": {"status": validator_status}
        },
        "_sdk_request_cache": {
            "signal_decision_evidence": _signal_evidence(
                tool_name=tool_name,
                candidates=selected_sources,
            ),
            "decision_ownership": {
                "validator_outcome": {"status": validator_status}
            },
        },
    }
    run_id = SQLiteStore(database_url).save_agent_run(
        agent_name=route,
        input_summary="Select the most relevant public signal.",
        output=output,
        model="offline-test-double",
        dry_run=dry_run,
        status=run_status,
    )
    return str(run_id)


def _write_slack_context(
    tmp_path: Path,
    *,
    request_ts: str = FOLLOWUP_REQUEST_TS,
    prior_agent_runs: list[dict[str, Any]] | None = None,
    injected_signal_context: dict[str, Any] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "schema": "keystone.slack.history_context.v1",
        "team_id": TEAM_ID,
        "channel_id": CHANNEL_ID,
        "channel_name": "clinical-ai",
        "thread_ts": THREAD_TS,
        "request_ts": request_ts,
        "thread_root_request": "Which public clinical-AI signal is most relevant?",
    }
    if prior_agent_runs is not None:
        payload["prior_agent_runs"] = prior_agent_runs
    if injected_signal_context is not None:
        payload["verified_signal_context"] = injected_signal_context
    path = tmp_path / f"slack-context-{request_ts.replace('.', '-')}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _rehydrate_state(
    tmp_path: Path,
    database_url: str,
    *,
    request: str,
    request_ts: str = FOLLOWUP_REQUEST_TS,
    prior_agent_runs: list[dict[str, Any]] | None = None,
    injected_signal_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context_path = _write_slack_context(
        tmp_path,
        request_ts=request_ts,
        prior_agent_runs=prior_agent_runs,
        injected_signal_context=injected_signal_context,
    )
    return cli._orchestrator_workflow_state_from_cli_context(
        context_file_path=str(context_path),
        request_text=request,
        database_url=database_url,
    )


def _run_preflight(
    monkeypatch: pytest.MonkeyPatch,
    request: str,
    state: dict[str, Any],
):
    monkeypatch.setattr(
        orchestrator_module,
        "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: _route(),
    )
    return orchestrator_module.run_orchestrator_preflight(
        request,
        requested_agent="opportunity_scout",
        live_orchestrator=True,
        workflow_state=state,
    )


@pytest.mark.parametrize("use_langgraph", [False, True], ids=("manager", "graph"))
@pytest.mark.parametrize("summary_kind", ["valid", "missing", "wrong", "extra"])
@pytest.mark.parametrize("no_action", [False, True], ids=("record", "no-action"))
def test_work_item_selected_signal_keeps_attested_source_in_scout_packet(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    supplied_opportunity_fake_sdk: Any,
    use_langgraph: bool,
    summary_kind: str,
    no_action: bool,
) -> None:
    database_url = _database_url(tmp_path)
    source = _source("preprints")
    source["summary"] = "Saved preliminary methods summary."
    source["limitations"] = ["Full article text was not read."]
    initial_run_id = _save_signal_run(database_url, kind="preprints", sources=[source])
    context_path = _write_slack_context(tmp_path)
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["read_context"] = "An unrelated thread link: https://untrusted.example/decoy"
    context_path.write_text(json.dumps(context), encoding="utf-8")
    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_preprint_announcement_history_impl",
        lambda **_kwargs: pytest.fail("WorkItem handoff must not reread Preprints"),
    )
    plan = ManualRequestPlan(
        source="llm", requested_agent="opportunity_scout",
        target_agent="opportunity_scout", workflow=["opportunity_scout"],
        intent="opportunity_search", task_objective="route_or_continue",
        expected_artifact_type="none" if no_action else "opportunity_record",
        requires_durable_state=True,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            prior_context_dependency="selected_context", permission_state="read_only",
        ),
    )
    supplied_opportunity_fake_sdk.subject = "Synthetic preprint"
    supplied_opportunity_fake_sdk.no_action = no_action
    if summary_kind == "valid" and not no_action:
        supplied_opportunity_fake_sdk.record_url_suffix = "/"
    if summary_kind == "valid":
        from keystone_agents import workflow_runner

        monkeypatch.setattr(
            workflow_runner,
            "synthesize_user_facing_work_item_response_sdk_result",
            lambda *_args, **_kwargs: pytest.fail(
                "Canonical supplied Scout summary must not be rewritten."
            ),
        )
    if summary_kind == "valid" and not no_action and not use_langgraph:
        from keystone_agents import workflow_runner

        original_sdk = workflow_runner.run_opportunity_scout_sdk

        def sdk_with_newer_signal(*args: Any, **kwargs: Any) -> Any:
            output = original_sdk(*args, **kwargs)
            _save_signal_run(
                database_url, kind="preprints", sources=[_source("preprints", 2)],
                request_ts="1789000001.500100",
            )
            return output

        monkeypatch.setattr(workflow_runner, "run_opportunity_scout_sdk", sdk_with_newer_signal)
    if summary_kind == "missing":
        supplied_opportunity_fake_sdk.summary_override = (
            "The selected source suggests a review-only direction."
        )
    elif summary_kind == "wrong":
        supplied_opportunity_fake_sdk.summary_override = (
            "The selected source suggests a review-only direction. "
            "Source: https://untrusted.example/wrong"
        )
    elif summary_kind == "extra":
        supplied_opportunity_fake_sdk.summary_override = (
            f"The selected source suggests a review-only direction. Source: {source['url']} "
            "and https://untrusted.example/extra"
        )
    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text=(
                "Assess that selected source as one KNI opportunity; "
                "do not research externally."
            ),
            database_url=database_url, context_file_path=str(context_path),
            requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
            manual_request_plan=plan.model_dump(mode="json"),
            live_sdk=True, live_search=False, save=True,
        ),
        max_steps=1, use_langgraph=use_langgraph,
    )

    assert supplied_opportunity_fake_sdk.calls
    packet = json.loads(supplied_opportunity_fake_sdk.calls[0]["typed_input"].context)
    assert packet["supplied_sources"][0]["provider_candidate_id"] == source["feed_item_id"]
    assert len(supplied_opportunity_fake_sdk.calls) == 1
    assert result.work_item.sources[0].source_id == source["feed_item_id"]
    assert len([
        item for item in result.work_item.sources
        if item.source_id == source["feed_item_id"]
    ]) == 1
    binding = result.work_item.target.metadata["verified_signal_source_binding"]
    assert binding["source_run_id"] == initial_run_id
    assert result.work_item.sources[0].url == source["url"]
    assert result.work_item.sources[0].supported_claim == source["summary"]
    assert packet["supplied_sources"][0]["url"] == source["url"]
    assert packet["context_pack"]["source_refs"][0]["source_id"] == source["feed_item_id"]
    assert "model-reported limitation (unverified): full article text was not read" in (
        str(packet["supplied_sources"][0]).lower()
    )
    stored = SQLiteStore(database_url).get_work_item(result.work_item.id)
    assert stored is not None
    assert len([
        item for item in stored.sources
        if item.source_id == source["feed_item_id"]
    ]) == 1
    cli._print_work_item_result(result, json_output=True)
    public_payload = json.loads(capsys.readouterr().out)
    assert public_payload["slack_display_text"] == result.human_summary
    assert public_payload["completion_confirmed"] is (summary_kind == "valid")
    if summary_kind == "valid":
        assert source["url"] in public_payload["public_result"]["text"]
        assert "https://untrusted.example/" not in public_payload["public_result"]["text"]
    if summary_kind == "valid" and not no_action and use_langgraph:
        resumed = prepare_work_item_step(WorkflowRunRequest(
            request_text="resume", database_url=database_url,
            work_item_id=result.work_item.id,
            requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
            manual_request_plan=plan.model_dump(mode="json"), save=True,
        ))
        resumed_binding = resumed.work_item.target.metadata["verified_signal_source_binding"]
        assert resumed_binding["source_run_id"] == initial_run_id
        third_context = _write_slack_context(tmp_path, request_ts="1789000003.000100")
        third = prepare_work_item_step(WorkflowRunRequest(
            request_text="Should we pursue that same selected source?",
            database_url=database_url, context_file_path=str(third_context),
            requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
            manual_request_plan=plan.model_dump(mode="json"), save=True,
        ))
        third_binding = third.work_item.target.metadata["verified_signal_source_binding"]
        assert third_binding["source_run_id"] == initial_run_id
    if summary_kind == "valid":
        assert source["url"] in result.human_summary
        if not no_action:
            assert result.artifact_refs[0].metadata["source_provided"] is True
            assert stored.status.value == "done"
        else:
            assert not result.artifact_refs
            assert stored.status.value == "done"
            assert "verified_signal_source_url_not_retained" not in {
                blocker.code for blocker in result.blockers
            }
            if not use_langgraph:
                review = dict(result.work_item.target.metadata["orchestrator_reviews"][-1])
                review["observed_gaps"] = ["Unsupported claim in the source-backed answer."]
                assert _manager_review_failure_is_authoritative(
                    review, result,
                    original_request=WorkflowRunRequest(
                        request_text="Assess the selected source.",
                        manual_request_plan=plan.model_dump(mode="json"),
                    ),
                )
    else:
        assert result.status.value != "done"
        assert stored.status.value != "done"
        assert not result.artifact_refs
        assert not stored.artifact_refs
    assert result.tool_execution.get("provider_request_attempt_count", 0) == 0


@pytest.mark.parametrize("use_langgraph", [False, True], ids=("manager", "graph"))
@pytest.mark.parametrize(
    ("source_repeat", "message_covered"),
    [(40, True), (75, True), (100, False), (140, True)],
    ids=("duplicate-repair", "thread-span-repair", "uncovered-message", "distinct-oversize"),
)
def test_long_thread_opportunity_context_repairs_or_blocks_before_scout(
    tmp_path: Path,
    supplied_opportunity_fake_sdk: Any,
    use_langgraph: bool,
    source_repeat: int,
    message_covered: bool,
) -> None:
    database_url = _database_url(tmp_path)
    source = _source("preprints")
    source["summary"] = "Saved preliminary methods summary."
    source["limitations"] = ["Full article text was not read."]
    root_run_id = _save_signal_run(database_url, kind="preprints", sources=[source])
    context_path = _write_slack_context(tmp_path)
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["thread_messages"] = [
        {
            "ts": f"1789000001.{index:06d}", "role": "user",
            "text": f"Prior synthetic turn {index}: keep this read-only and source-backed.",
        }
        for index in range(8)
    ]
    retained_messages = (
        context["thread_messages"] if message_covered else context["thread_messages"][:-1]
    )
    context["read_context"] = (
        "\n".join(item["text"] for item in retained_messages)
        + "\nEarlier supplemental status. " * 100
    )
    context_path.write_text(json.dumps(context), encoding="utf-8")
    plan = ManualRequestPlan(
        source="llm", requested_agent="opportunity_scout",
        target_agent="opportunity_scout", workflow=["opportunity_scout"],
        intent="opportunity_search", task_objective="route_or_continue",
        expected_artifact_type="none", requires_durable_state=True,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            prior_context_dependency="selected_context", permission_state="read_only",
        ),
    )
    request = WorkflowRunRequest(
        request_text="Assess that selected source; do not research externally.",
        database_url=database_url, context_file_path=str(context_path),
        requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        manual_request_plan=plan.model_dump(mode="json"),
        live_sdk=True, live_search=False, save=True,
    )
    prepared = prepare_work_item_step(request)
    item = prepared.work_item
    item.sources.extend(
        WorkItemSourceRef(
            source_id=f"supplemental-{index}",
            title=f"Supplemental source {index}",
            url=f"https://public.example/supplemental-{index}",
            source_type="public_web", provider="supplied",
            extraction_status="supplied_material",
            evidence_excerpt="Supplemental bounded context. " * source_repeat,
        )
        for index in range(2)
    )
    SQLiteStore(database_url).save_work_item(item)
    supplied_opportunity_fake_sdk.subject = "Synthetic preprint"
    supplied_opportunity_fake_sdk.no_action = True
    result = advance_work_item_manager_loop_with_optional_langgraph(
        request.model_copy(update={"work_item_id": item.id}),
        max_steps=1, use_langgraph=use_langgraph,
    )
    if source_repeat == 140 or not message_covered:
        assert supplied_opportunity_fake_sdk.calls == []
        assert result.status.value == "blocked"
        assert not result.artifact_refs
        assert result.tool_execution.get("provider_request_attempt_count", 0) == 0
        stored = SQLiteStore(database_url).get_work_item(item.id)
        assert stored is not None and stored.status.value == "blocked"
        insufficient = [
            event for event in SQLiteStore(database_url).list_work_item_events(item.id)
            if event.event_type == "opportunity_context_repair_insufficient"
        ]
        assert len(insufficient) == 1
        assert insufficient[0].metadata["after_chars"] > 48_000
        assert insufficient[0].metadata["source_identity_retained"] is True
        assert insufficient[0].metadata["within_bound"] is False
        return
    assert len(supplied_opportunity_fake_sdk.calls) == 1
    packet = json.loads(supplied_opportunity_fake_sdk.calls[0]["typed_input"].context)
    binding = packet["context_pack"]["target"]["metadata"]["verified_signal_source_binding"]
    assert binding["source_run_id"] == root_run_id
    assert packet["supplied_sources"][0]["source_id"] == source["feed_item_id"]
    assert packet["supplied_sources"][0]["url"] == source["url"]
    assert packet["supplied_sources"][0]["supported_claim"].startswith(source["summary"])
    assert "model-reported limitation (unverified): full article text was not read" in (
        str(packet["supplied_sources"][0]).lower()
    )
    assert "slack_context" not in packet
    assert "retrieved_sources" not in packet["context_pack"]
    retained_thread = packet["context_pack"]["target"]["metadata"]["slack_context"]
    assert len(retained_thread["thread_messages"]) == 8
    assert retained_thread["thread_root_request"]
    assert retained_thread["latest_user_follow_up"]
    if source_repeat == 75:
        assert "read_context" not in retained_thread
        assert "thread_transcript" not in retained_thread
    else:
        assert retained_thread["read_context"]
    repairs = [
        event for event in SQLiteStore(database_url).list_work_item_events(item.id)
        if event.event_type == "opportunity_context_repaired"
    ]
    assert len(repairs) == 1
    assert repairs[0].metadata["before_chars"] > 48_000
    assert repairs[0].metadata["after_chars"] <= 48_000
    assert repairs[0].metadata["source_identity_retained"] is True
    assert repairs[0].metadata["within_bound"] is True
    if source_repeat == 75:
        assert repairs[0].metadata["omitted_supplemental_thread_fields"] == [
            "read_context", "thread_transcript",
        ]
        assert repairs[0].metadata["omitted_supplemental_thread_chars"] > 0
    assert result.tool_execution.get("provider_request_attempt_count", 0) == 0
    assert result.status.value == "done"
    stored = SQLiteStore(database_url).get_work_item(item.id)
    assert stored is not None and stored.status.value == "done"
    assert not stored.artifact_refs
    assert source["url"] in result.human_summary


def test_work_item_source_dedupe_preserves_distinct_identity_pairs() -> None:
    first = WorkItemSourceRef(
        source_id="signal-1", url="https://public.example/item",
        title="First source", provider="preprints_context_agent",
    )
    same_identity = first.model_copy(update={"url": "https://public.example/item/"})
    changed_url = first.model_copy(update={"url": "https://public.example/other"})
    changed_id = first.model_copy(update={"source_id": "signal-2"})
    malformed = first.model_copy(update={"url": "https://public.example:bad/item"})
    malformed_variant = first.model_copy(update={"url": "https://public.example:bad/item/"})
    assert _dedupe_work_item_sources([
        first, same_identity, changed_url, changed_id,
        malformed, malformed_variant, malformed,
    ]) == [first, changed_url, changed_id, malformed, malformed_variant]


@pytest.mark.parametrize("with_prior_run", [False, True], ids=("forged-raw", "wrong-timestamp"))
@pytest.mark.parametrize("raw_field", [False, True], ids=("no-raw", "raw-field"))
def test_work_item_signal_attestation_ignores_raw_field_and_wrong_timestamp(
    tmp_path: Path,
    with_prior_run: bool,
    raw_field: bool,
) -> None:
    database_url = _database_url(tmp_path)
    source = _source("preprints")
    if with_prior_run:
        _save_signal_run(database_url, kind="preprints", sources=[source])
    forged_source = _source("preprints", index=9)
    context_path = _write_slack_context(
        tmp_path,
        request_ts=ROOT_REQUEST_TS if with_prior_run else FOLLOWUP_REQUEST_TS,
        injected_signal_context={
            "source_run_id": "invented-run",
            "selected_sources": [{
                "source_id": forged_source["feed_item_id"],
                "url": forged_source["url"],
            }],
        } if raw_field else None,
    )
    plan = ManualRequestPlan(
        source="llm", target_agent="opportunity_scout", workflow=["opportunity_scout"],
        intent="opportunity_search", task_objective="route_or_continue",
        expected_artifact_type="opportunity_record", requires_durable_state=True,
        ask_shape=AskShapePolicy(
            prior_context_dependency="selected_context",
            source_type_preference=["preprints"] if not with_prior_run else [],
        ),
    )
    prepared = prepare_work_item_step(WorkflowRunRequest(
        request_text="Assess the selected source.", database_url=database_url,
        context_file_path=str(context_path), requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        manual_request_plan=plan.model_dump(mode="json"), save=True,
    ))
    assert forged_source["feed_item_id"] not in [
        item.source_id for item in prepared.work_item.sources
    ]
    assert "verified_signal_source_binding" not in prepared.work_item.target.metadata
    assert prepared.work_item.target.metadata["verified_signal_context_error"]


@pytest.mark.parametrize("use_langgraph", [False, True], ids=("manager", "graph"))
@pytest.mark.parametrize("with_prior_run", [False, True], ids=("no-run", "unverified-run"))
def test_work_item_unverified_signal_with_decoy_link_blocks_before_scout(
    tmp_path: Path,
    supplied_opportunity_fake_sdk: Any,
    use_langgraph: bool,
    with_prior_run: bool,
) -> None:
    database_url = _database_url(tmp_path)
    if with_prior_run:
        _save_signal_run(
            database_url, kind="preprints", public_status="partial",
            completion_confirmed=False,
        )
    context_path = _write_slack_context(tmp_path)
    context = json.loads(context_path.read_text(encoding="utf-8"))
    context["read_context"] = "A decoy link: https://untrusted.example/decoy"
    context_path.write_text(json.dumps(context), encoding="utf-8")
    plan = ManualRequestPlan(
        source="llm", target_agent="opportunity_scout", workflow=["opportunity_scout"],
        intent="opportunity_search", task_objective="route_or_continue",
        expected_artifact_type="opportunity_record", requires_durable_state=True,
        ask_shape=AskShapePolicy(prior_context_dependency="selected_context"),
    )
    result = advance_work_item_manager_loop_with_optional_langgraph(
        WorkflowRunRequest(
            request_text="Assess that selected source without new research.",
            database_url=database_url, context_file_path=str(context_path),
            requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
            manual_request_plan=plan.model_dump(mode="json"),
            live_sdk=True, live_search=False, save=True,
        ),
        max_steps=1, use_langgraph=use_langgraph,
    )
    assert not supplied_opportunity_fake_sdk.calls
    assert not result.artifact_refs
    assert result.status.value != "done"
    stored = SQLiteStore(database_url).get_work_item(result.work_item.id)
    assert stored is not None and stored.status.value != "done"


@pytest.mark.parametrize("source_mode", ["typed-source", "live-search"])
def test_work_item_non_signal_context_keeps_authorized_source_path(
    tmp_path: Path,
    source_mode: str,
) -> None:
    database_url = _database_url(tmp_path)
    store = SQLiteStore(database_url)
    work_item = create_or_load_work_item(
        store=store, request_text="Assess an existing supplied source.",
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
    )
    if source_mode == "typed-source":
        work_item = work_item.model_copy(update={
            "sources": [WorkItemSourceRef(
                source_id="typed-record-1", title="Existing supplied note",
                url="https://public.example/typed-record-1", provider="operator_supplied",
                evidence_excerpt="An independently typed source excerpt for this WorkItem.",
            )]
        })
        store.save_work_item(work_item)
    plan = ManualRequestPlan(
        source="llm", target_agent="opportunity_scout", workflow=["opportunity_scout"],
        intent="opportunity_search", task_objective="route_or_continue",
        expected_artifact_type="opportunity_record", requires_durable_state=True,
        requires_live_search=source_mode == "live-search",
        ask_shape=AskShapePolicy(prior_context_dependency="selected_context"),
    )
    prepared = prepare_work_item_step(WorkflowRunRequest(
        request_text="Assess the selected context.", database_url=database_url,
        context_file_path=str(_write_slack_context(tmp_path)),
        work_item_id=work_item.id, requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        manual_request_plan=plan.model_dump(mode="json"),
        live_search=source_mode == "live-search", save=True,
    ))
    assert "verified_signal_context_error" not in prepared.work_item.target.metadata
    if source_mode == "typed-source":
        assert prepared.work_item.sources[0].source_id == "typed-record-1"


@pytest.mark.parametrize("explicit_source", [False, True], ids=("ambiguous", "exact-source"))
@pytest.mark.parametrize("newer_confirmed", [False, True], ids=("unconfirmed", "confirmed"))
@pytest.mark.parametrize("lineage_kind", ["unrelated", "match", "wrong-url"])
def test_work_item_signal_attestation_checks_newer_completed_thread_result(
    tmp_path: Path,
    explicit_source: bool,
    newer_confirmed: bool,
    lineage_kind: str,
) -> None:
    database_url = _database_url(tmp_path)
    source = _source("preprints")
    source_run_id = _save_signal_run(database_url, kind="preprints", sources=[source])
    store = SQLiteStore(database_url)
    wrapper_output: dict[str, Any] = {}
    if lineage_kind != "unrelated":
        linked = create_or_load_work_item(
            store=store, request_text="Assess the selected source.",
            route=WorkItemRoute.OPPORTUNITY_SCOUT,
        )
        binding = {
            "source_run_id": source_run_id,
            "source_request_ts": ROOT_REQUEST_TS,
            "source_id": source["feed_item_id"],
            "source_url": (
                source["url"] if lineage_kind == "match"
                else "https://untrusted.example/other-source"
            ),
            "team_id": TEAM_ID,
            "channel_id": CHANNEL_ID,
            "thread_ts": THREAD_TS,
            "request_ts": "1789000001.500100",
        }
        linked = linked.model_copy(update={
            "target": linked.target.model_copy(update={
                "metadata": {**linked.target.metadata, "verified_signal_source_binding": binding}
            })
        })
        wrapper_output["work_item"] = linked.model_dump(mode="json")
    store.save_agent_run(
        agent_name=(
            "business_research_analyst" if lineage_kind == "unrelated"
            else "opportunity_scout"
        ),
        input_summary="A later same-thread wrapper result.",
        output={
            **wrapper_output,
            "public_result": {"status": "completed", "completion_confirmed": newer_confirmed},
            "slack_run_provenance": {
                "schema": "keystone.slack.run_provenance.v1", "context_validated": True,
                "team_id": TEAM_ID, "channel_id": CHANNEL_ID, "thread_ts": THREAD_TS,
                "request_ts": "1789000001.500100",
            },
        },
        model="offline-test-double", dry_run=False, status="success",
    )
    plan = ManualRequestPlan(
        source="llm", target_agent="opportunity_scout", workflow=["opportunity_scout"],
        intent="opportunity_search", task_objective="route_or_continue",
        expected_artifact_type="opportunity_record", requires_durable_state=True,
        ask_shape=AskShapePolicy(prior_context_dependency="selected_context"),
    )
    request_text = (
        f"Assess selected preprint {source['feed_item_id']} from {source['url']}."
        if explicit_source else "Assess that selected context."
    )
    prepared = prepare_work_item_step(WorkflowRunRequest(
        request_text=request_text, database_url=database_url,
        context_file_path=str(_write_slack_context(tmp_path)),
        requested_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        manual_request_plan=plan.model_dump(mode="json"), save=True,
    ))
    metadata = prepared.work_item.target.metadata
    if explicit_source or not newer_confirmed or lineage_kind == "match":
        assert metadata["verified_signal_source_binding"]["source_id"] == source["feed_item_id"]
        assert "verified_signal_context_error" not in metadata
    else:
        assert metadata["verified_signal_context_error"]
        assert "verified_signal_source_binding" not in metadata


def test_verified_single_rss_source_admits_tool_free_owner_switch() -> None:
    admission = resolve_provider_free_composition_admission(
        _context_only_plan(),
        workflow_state={
            "prior_agent_runs": [
                {
                    "id": "run-rss-1",
                    "route": "rss_context_agent",
                    "status": "completed",
                    "thread_correlation": "same_thread",
                    "summary": "Selected one public clinical-AI signal.",
                }
            ],
            "verified_signal_context": {
                "source_run_id": "run-rss-1",
                "source_route": "rss_context_agent",
                "verification_status": "verified",
                "public_result_status": "completed",
                "source_request_ts": ROOT_REQUEST_TS,
                "selected_sources": [
                    {
                        "source_id": "rss-clinical-ai-1",
                        "provider_candidate_id": "rss-clinical-ai-1",
                        "title": "Clinical-AI evaluation partnership",
                        "url": "https://public.example/clinical-ai-partnership",
                        "source_type": "rss_historical_context",
                        "supported_claim": (
                            "The partnership seeks independent evaluation design."
                        ),
                        "provider": "rss_context_agent",
                        "extraction_status": "verified_signal_history",
                        "retrieved_at": "2026-09-14",
                    }
                ],
                "source_dates": {"rss-clinical-ai-1": "2026-09-14"},
                "limitations": ["Stored public history; no fresh web verification."],
            },
        },
    )

    assert admission.composition_allowed is True
    assert admission.context_kind == "verified_signal_context"
    assert admission.source_run_id == "run-rss-1"


@pytest.mark.parametrize(
    ("kind", "followup", "answer_fragment"),
    [
        (
            "rss",
            "Opportunity Scout, is that something we should pursue or just monitor?",
            "A bounded discovery call may be reasonable",
        ),
        (
            "preprints",
            "Does it suggest a concrete KNI opportunity or only something to monitor?",
            "The evidence is preliminary, so monitor it",
        ),
        (
            "rss",
            "Given that signal, should we take no action for now?",
            "No action is warranted yet",
        ),
    ],
    ids=("rss-pursue", "preprint-uncertain-monitor", "rss-no-action"),
)
def test_persisted_signal_owner_switch_is_model_visible_and_tool_free(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    kind: str,
    followup: str,
    answer_fragment: str,
) -> None:
    database_url = _database_url(tmp_path)
    source = _source(kind)
    run_id = _save_signal_run(database_url, kind=kind, sources=[source])
    state = _rehydrate_state(tmp_path, database_url, request=followup)

    assert state["verified_signal_context"]["source_run_id"] == run_id
    assert state["verified_signal_context"]["selected_sources"][0]["source_id"] == source[
        "feed_item_id"
    ]
    preflight = _run_preflight(monkeypatch, followup, state)
    assert preflight.execution_allowed is True
    assert preflight.composition_admission.composition_allowed is True
    assert preflight.composition_admission.source_route == (
        "preprints_context_agent" if kind == "preprints" else "rss_context_agent"
    )
    estimate = cli._estimate_ask_openai_requests(
        SimpleNamespace(
            context_file="",
            agent="opportunity_scout",
            max_manager_steps=3,
            live_search=False,
        ),
        input_text=followup,
        live_sdk=True,
        live_manual_plan=False,
        requested_route="opportunity_scout",
        manual_plan=preflight.manual_request_plan,
        effective_live_search=False,
        provider_free_composition_allowed=True,
        observed_orchestrator_requests=1,
    )
    assert estimate["min"] == estimate["max"] == 2
    assert estimate["stages"] == [
        "orchestrator_preflight",
        "opportunity_scout_direct_supplied_response_sdk",
    ]

    execution_context = cli._direct_specialist_execution_context(
        followup,
        workflow_state=state,
        force_thread_context=True,
        verified_signal_context=(
            preflight.composition_admission.verified_signal_context
        ),
    )
    answer = f"{answer_fragment}; this assessment uses {source['url']}"
    calls: list[dict[str, Any]] = []

    def fake_sdk(**kwargs: Any) -> SimpleNamespace:
        calls.append(kwargs)
        assert kwargs["agent"].tools == []
        assert kwargs["agent"].handoffs == []
        assert kwargs["max_turns"] == 1
        assert kwargs["typed_input"].original_request == followup
        selected_context = kwargs["typed_input"].selected_context
        assert source["feed_item_id"] in selected_context
        assert source["url"] in selected_context
        assert source["published_at"] in selected_context
        assert "no fresh web verification" in selected_context.lower()
        return SimpleNamespace(
            output=DirectAgentResponse(answer=answer),
            usage={"requests": 1},
            cost={"estimated_usd": 0.001},
            budget_guard={"status": "passed"},
            request_cache={},
        )

    monkeypatch.setattr(cli, "run_typed_sdk_agent", fake_sdk)
    result = cli._run_direct_supplied_context_response_live(
        "opportunity_scout",
        followup,
        json_output=True,
        manual_plan=preflight.manual_request_plan,
        orchestrator_preflight=preflight,
        sdk_session_spec=None,
        database_url=database_url,
        execution_context=execution_context,
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0 and len(calls) == 1
    assert payload["status"] == "completed"
    assert payload["public_result"]["completion_confirmed"] is True
    assert payload["public_result"]["text"] == answer
    assert source["url"] in payload["slack_display_text"]
    assert payload["tool_admission"]["tool_count"] == 0
    assert payload["tool_execution"]["model_tool_call_count"] == 0
    assert payload["tool_execution"]["workflow_called_tool_names"] == []
    assert payload["tool_execution"]["provider_request_attempt_count"] == 0
    assert payload["tool_execution"]["provider_receipt_count"] == 0
    assert payload["side_effects"] == {
        "external_write_performed": False,
        "email_sent": False,
        "slack_message_posted": False,
    }
    assert payload["retained_signal_validation"]["passed"] is True


def test_unnamed_same_thread_opportunity_followup_keeps_scout_owner_through_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    source = _source("preprints")
    _save_signal_run(database_url, kind="preprints", sources=[source])
    followup = (
        "Using that preprint, what opportunity, if any, is worth considering "
        "for Keystone? Explain why or why not, cite the same source."
    )
    context_path = _write_slack_context(tmp_path)
    envelope = "\n".join(
        [
            "business agents continue this prior Slack thread.",
            f"Current user request (authoritative): {followup}",
            "Prior task owner (advisory): preprints_context_agent",
            "Previous request: Find the saved preprint most worth research review.",
            f"Previous result: Selected one preprint. Source: {source['url']}",
            f"User follow-up: {followup}",
            "Continue the same agent task.",
        ]
    )
    preflights: list[Any] = []

    def fake_preflight(request_text: str, **kwargs: Any) -> Any:
        assert request_text == followup
        assert kwargs["requested_agent"] is None
        preflight = orchestrator_module.run_orchestrator_preflight(
            request_text,
            requested_agent=None,
            live_orchestrator=True,
            workflow_state=kwargs["workflow_state"],
        )
        assert preflight.manual_request_plan.source == "canonical:orchestrator_context_only"
        assert preflight.manual_request_plan.target_agent == "opportunity_scout"
        assert preflight.composition_admission.context_kind == "verified_signal_context"
        preflights.append(preflight)
        return preflight

    monkeypatch.setattr(
        orchestrator_module,
        "_route_ambiguous_with_llm",
        lambda *_a, **_k: _route(),
    )
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight)
    monkeypatch.setattr(
        cli, "_run_live_ask_with_environment",
        lambda args: cli._run_ask_with_current_environment(args),
    )
    monkeypatch.setattr(
        cli, "_run_ask_work_item",
        lambda *_a, **_k: pytest.fail("One-owner supplied answer must not create a WorkItem"),
    )
    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_preprint_announcement_history_impl",
        lambda **_k: pytest.fail("Retained source must not be reread"),
    )
    sdk_calls: list[Any] = []

    def fake_sdk(**kwargs: Any) -> SimpleNamespace:
        sdk_calls.append(kwargs)
        assert kwargs["agent"].name == "opportunity_scout"
        assert kwargs["agent"].tools == []
        assert kwargs["agent"].handoffs == []
        selected_context = kwargs["typed_input"].selected_context
        assert source["feed_item_id"] in selected_context
        assert source["url"] in selected_context
        assert source["summary"] in selected_context
        assert "no fresh web verification" in selected_context.lower()
        return SimpleNamespace(
            output=DirectAgentResponse(
                answer=(
                    "Only a preliminary research opportunity is supported. "
                    f"Source: {source['url']}"
                )
            ),
            usage={"requests": 1},
            cost={"estimated_usd": 0.001},
            budget_guard={"status": "passed"},
            request_cache={},
        )

    monkeypatch.setattr(cli, "run_typed_sdk_agent", fake_sdk)
    args = cli.build_parser().parse_args(
        [
            "ask", "--live-sdk", "--json", "--database-url", database_url,
            "--context-file", str(context_path), "--input", envelope,
        ]
    )

    assert cli._run_ask(args) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(preflights) == len(sdk_calls) == 1
    assert payload["selected_agent"] == "opportunity_scout"
    assert payload["status"] == "completed"
    assert payload["retained_signal_validation"]["passed"] is True
    assert payload["tool_admission"]["tool_count"] == 0
    assert payload["tool_execution"]["model_tool_call_count"] == 0
    assert payload["side_effects"]["external_write_performed"] is False


def test_multiple_signal_sources_require_exact_operator_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    sources = [_source("rss", 1), _source("rss", 2)]
    _save_signal_run(database_url, sources=sources)
    generic_request = "Does that suggest an opportunity?"
    state = _rehydrate_state(tmp_path, database_url, request=generic_request)

    ambiguous = _run_preflight(monkeypatch, generic_request, state)
    assert ambiguous.execution_allowed is False
    assert ambiguous.composition_admission.reason == "selected_context_ambiguous"

    exact_request = f"Assess only this exact signal: {sources[1]['url']}"
    selected = _run_preflight(monkeypatch, exact_request, state)
    assert selected.execution_allowed is True
    selected_context = selected.composition_admission.verified_signal_context
    assert selected_context is not None
    assert [source.source_id for source in selected_context.selected_sources] == [
        sources[1]["feed_item_id"]
    ]
    projected = cli._direct_specialist_execution_context(
        exact_request,
        workflow_state=state,
        force_thread_context=True,
        verified_signal_context=selected_context,
    )
    encoded = json.dumps(projected, sort_keys=True)
    assert sources[1]["url"] in encoded
    assert sources[0]["url"] not in encoded


@pytest.mark.parametrize(
    ("context_update", "reason"),
    [
        ({"verified_signal_context": None}, "selected_context_source_missing"),
        (
            {
                "verified_signal_context": {
                    "source_run_id": "run-rss-1",
                    "source_route": "rss_context_agent",
                    "verification_status": "unverified",
                    "public_result_status": "partial",
                }
            },
            "selected_context_source_unverified",
        ),
        (
            {
                "verified_signal_context": {
                    "source_run_id": "different-run",
                    "source_route": "rss_context_agent",
                    "verification_status": "verified",
                    "public_result_status": "completed",
                    "source_request_ts": ROOT_REQUEST_TS,
                    "selected_sources": [
                        {
                            "source_id": "rss-clinical-ai-1",
                            "title": "Signal",
                            "url": "https://public.example/rss/clinical-ai-1",
                            "source_type": "rss_historical_context",
                            "supported_claim": "Bounded claim.",
                            "provider": "rss_context_agent",
                        }
                    ],
                    "source_dates": {"rss-clinical-ai-1": "2026-09-14"},
                }
            },
            "selected_context_source_mismatch",
        ),
    ],
    ids=("missing", "unverified", "mismatched-run"),
)
def test_signal_admission_fails_closed_for_missing_unverified_or_mismatched_context(
    context_update: dict[str, Any],
    reason: str,
) -> None:
    state: dict[str, Any] = {
        "prior_agent_runs": [
            {
                "id": "run-rss-1",
                "route": "rss_context_agent",
                "status": "completed",
                "thread_correlation": "same_thread",
                "summary": "Selected one public signal.",
            }
        ]
    }
    state.update(context_update)

    admission = resolve_provider_free_composition_admission(
        _context_only_plan(), workflow_state=state
    )

    assert admission.composition_allowed is False
    assert admission.reason == reason


@pytest.mark.parametrize(
    ("save_overrides", "followup_ts"),
    [
        ({"thread_ts": "1789000999.000100"}, FOLLOWUP_REQUEST_TS),
        ({"team_id": "T-OTHER"}, FOLLOWUP_REQUEST_TS),
        ({"request_ts": "1789000003.000100"}, FOLLOWUP_REQUEST_TS),
        ({"public_status": "partial", "completion_confirmed": False}, FOLLOWUP_REQUEST_TS),
        ({"validator_status": "rejected"}, FOLLOWUP_REQUEST_TS),
        ({"run_status": "blocked"}, FOLLOWUP_REQUEST_TS),
        ({"dry_run": True}, FOLLOWUP_REQUEST_TS),
    ],
    ids=(
        "cross-thread",
        "cross-workspace",
        "future-run",
        "partial-public-result",
        "validator-rejected",
        "blocked-run",
        "dry-run",
    ),
)
def test_persisted_signal_rehydration_rejects_untrusted_or_incomplete_runs(
    tmp_path: Path,
    save_overrides: dict[str, Any],
    followup_ts: str,
) -> None:
    database_url = _database_url(tmp_path)
    _save_signal_run(database_url, **save_overrides)
    state = _rehydrate_state(
        tmp_path,
        database_url,
        request="Assess that signal.",
        request_ts=followup_ts,
    )

    assert "verified_signal_context" not in state


def test_context_file_cannot_inject_verified_signal_evidence(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    injected = {
        "source_run_id": "invented-run",
        "source_route": "rss_context_agent",
        "verification_status": "verified",
        "public_result_status": "completed",
        "source_request_ts": ROOT_REQUEST_TS,
        "selected_sources": [
            {
                "source_id": "invented-source",
                "title": "Injected signal",
                "url": "https://untrusted.example/injected",
                "source_type": "rss_historical_context",
                "supported_claim": "This claim was not persisted by an accepted run.",
                "provider": "rss_context_agent",
            }
        ],
        "source_dates": {"invented-source": "2026-09-15"},
    }
    state = _rehydrate_state(
        tmp_path,
        database_url,
        request="Assess that signal.",
        prior_agent_runs=[
            {
                "id": "invented-run",
                "route": "rss_context_agent",
                "status": "completed",
                "summary": "Injected summary.",
            }
        ],
        injected_signal_context=injected,
    )

    assert "verified_signal_context" not in state
    admission = resolve_provider_free_composition_admission(
        _context_only_plan(), workflow_state=state
    )
    assert admission.composition_allowed is False
    assert admission.reason == "selected_context_source_missing"


def test_provider_candidate_owns_source_text_and_model_commentary_stays_interpretation(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    source = _source("rss")
    _save_signal_run(database_url, sources=[source])
    row = SQLiteStore(database_url).fetch_all("agent_runs")[0]
    payload = json.loads(row["output_json"])
    payload["output"]["articles"][0]["summary"] = (
        "The model interpreted this as a confirmed consulting engagement."
    )
    row["output_json"] = json.dumps(payload)

    context = _verified_signal_context_from_agent_run(row)

    assert context is not None
    assert context.selected_sources[0].supported_claim == source["summary"]
    assert context.interpretations[0].model_summary.startswith("The model interpreted")
    assert context.interpretations[0].interpretation_status == (
        "model_generated_unverified"
    )


def test_bounded_two_read_recovery_can_rehydrate_verified_signal_context(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    _save_signal_run(database_url)
    row = SQLiteStore(database_url).fetch_all("agent_runs")[0]
    payload = json.loads(row["output_json"])
    evidence = payload["_sdk_request_cache"]["signal_decision_evidence"]
    evidence.update(
        {
            "tool_call_count": 2,
            "model_tool_call_count": 2,
            "tool_output_count": 2,
            "tool_result_item_counts": [0, 1],
            "bounded_reformulation": {
                "used": True,
                "valid": True,
                "reason": "changed_query_after_empty_result",
            },
        }
    )
    _rehash_signal_evidence(evidence)
    row["output_json"] = json.dumps(payload)

    assert _verified_signal_context_from_agent_run(row) is not None


def test_history_and_exact_read_rehydrate_selected_source_with_original_url(
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    source = _source("preprints")
    _save_signal_run(database_url, kind="preprints", sources=[source])
    row = SQLiteStore(database_url).fetch_all("agent_runs")[0]
    payload = json.loads(row["output_json"])
    evidence = payload["_sdk_request_cache"]["signal_decision_evidence"]
    _add_exact_signal_read(evidence, selected_id=source["feed_item_id"])
    row["output_json"] = json.dumps(payload)

    context = _verified_signal_context_from_agent_run(row)

    assert context is not None
    assert context.selected_sources[0].provider_candidate_id == source["feed_item_id"]
    assert context.selected_sources[0].url == source["url"]
    assert context.source_request_ts == ROOT_REQUEST_TS


def test_empty_first_history_then_exact_read_can_rehydrate(tmp_path: Path) -> None:
    database_url = _database_url(tmp_path)
    source = _source("rss")
    _save_signal_run(database_url, sources=[source])
    row = SQLiteStore(database_url).fetch_all("agent_runs")[0]
    payload = json.loads(row["output_json"])
    evidence = payload["_sdk_request_cache"]["signal_decision_evidence"]
    _add_exact_signal_read(evidence, selected_id=source["feed_item_id"])
    evidence.update(
        {
            "tool_call_count": 3,
            "history_tool_call_count": 2,
            "model_tool_call_count": 3,
            "tool_output_count": 3,
            "tool_result_item_counts": [0, 1],
            "bounded_reformulation": {"used": True, "valid": True},
        }
    )
    _rehash_signal_evidence(evidence)
    row["output_json"] = json.dumps(payload)

    assert _verified_signal_context_from_agent_run(row) is not None


@pytest.mark.parametrize(
    "corruption",
    [
        "wrong-candidate-id",
        "wrong-evidence-id",
        "wrong-snapshot",
        "missing-handle",
        "missing-read",
        "wrong-read-tool",
        "wrong-total",
        "wrong-history-count",
        "partial-status",
        "partial-content",
        "empty-content",
        "bad-fingerprint",
        "missing-provenance",
        "incomplete-public-result",
    ],
)
def test_exact_read_context_rejects_unbound_or_incomplete_receipts(
    tmp_path: Path,
    corruption: str,
) -> None:
    database_url = _database_url(tmp_path)
    source = _source("rss")
    _save_signal_run(database_url, sources=[source])
    row = SQLiteStore(database_url).fetch_all("agent_runs")[0]
    payload = json.loads(row["output_json"])
    evidence = payload["_sdk_request_cache"]["signal_decision_evidence"]
    _add_exact_signal_read(evidence, selected_id=source["feed_item_id"])
    read = evidence["evidence_reads"][0]
    candidate = evidence["candidates"][0]
    if corruption == "wrong-candidate-id":
        read["feed_item_id"] = "other-candidate"
    elif corruption == "wrong-evidence-id":
        read["evidence_id"] = f"evidence:{'d' * 24}"
    elif corruption == "wrong-snapshot":
        read["snapshot_sha256"] = "d" * 64
    elif corruption == "missing-handle":
        candidate.pop("evidence_ids")
    elif corruption == "missing-read":
        evidence["evidence_reads"] = []
        evidence["evidence_read_call_count"] = 0
        evidence["tool_call_count"] = 1
        evidence["model_tool_call_count"] = 1
        evidence["tool_output_count"] = 1
    elif corruption == "wrong-read-tool":
        evidence["evidence_tool_name"] = "read_unrelated_evidence"
    elif corruption == "wrong-total":
        evidence["tool_call_count"] = 3
    elif corruption == "wrong-history-count":
        evidence["history_tool_call_count"] = 2
    elif corruption == "partial-status":
        read["status"] = "partial"
    elif corruption == "partial-content":
        read["content_complete"] = False
    elif corruption == "empty-content":
        read["content_status"] = "empty_saved_record"
    elif corruption == "bad-fingerprint":
        evidence["evidence_fingerprint"] = "0" * 64
    elif corruption == "missing-provenance":
        payload["slack_run_provenance"].pop("request_ts")
    elif corruption == "incomplete-public-result":
        payload["public_result"]["completion_confirmed"] = False
    if corruption != "bad-fingerprint":
        _rehash_signal_evidence(evidence)
    row["output_json"] = json.dumps(payload)

    assert _verified_signal_context_from_agent_run(row) is None


@pytest.mark.parametrize(
    ("result_counts", "reformulation"),
    [
        ([1, 1], {"used": True, "valid": True}),
        ([0, 1], {"used": True, "valid": False}),
        ([0], {"used": True, "valid": True}),
    ],
)
def test_invalid_two_read_recovery_cannot_rehydrate_verified_signal_context(
    tmp_path: Path,
    result_counts: list[int],
    reformulation: dict[str, bool],
) -> None:
    database_url = _database_url(tmp_path)
    _save_signal_run(database_url)
    row = SQLiteStore(database_url).fetch_all("agent_runs")[0]
    payload = json.loads(row["output_json"])
    evidence = payload["_sdk_request_cache"]["signal_decision_evidence"]
    evidence.update(
        {
            "tool_call_count": 2,
            "model_tool_call_count": 2,
            "tool_output_count": 2,
            "tool_result_item_counts": result_counts,
            "bounded_reformulation": reformulation,
        }
    )
    _rehash_signal_evidence(evidence)
    row["output_json"] = json.dumps(payload)

    assert _verified_signal_context_from_agent_run(row) is None


@pytest.mark.parametrize("corruption", ["missing-candidates", "bad-fingerprint"])
def test_missing_or_corrupt_provider_candidate_evidence_cannot_verify_context(
    tmp_path: Path,
    corruption: str,
) -> None:
    database_url = _database_url(tmp_path)
    _save_signal_run(database_url)
    row = SQLiteStore(database_url).fetch_all("agent_runs")[0]
    payload = json.loads(row["output_json"])
    evidence = payload["_sdk_request_cache"]["signal_decision_evidence"]
    if corruption == "missing-candidates":
        evidence["candidates"] = []
        evidence["candidate_count"] = 0
    else:
        evidence["evidence_fingerprint"] = "0" * 64
    row["output_json"] = json.dumps(payload)

    assert _verified_signal_context_from_agent_run(row) is None


@pytest.mark.parametrize("missing_field", ["summary", "published_at"])
def test_selected_candidate_missing_substantive_evidence_remains_unverified(
    tmp_path: Path,
    missing_field: str,
) -> None:
    database_url = _database_url(tmp_path)
    selected = _source("rss")
    selected[missing_field] = ""
    _save_signal_run(database_url, sources=[selected])
    row = SQLiteStore(database_url).fetch_all("agent_runs")[0]

    assert _verified_signal_context_from_agent_run(row) is None


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("title", "Different model-written title"),
        ("url", "https://other.example/model-written"),
        ("published_at", "2026-09-01"),
        ("source", "different-model-source"),
    ],
)
def test_selected_article_identity_must_match_provider_candidate(
    tmp_path: Path,
    field: str,
    replacement: str,
) -> None:
    database_url = _database_url(tmp_path)
    _save_signal_run(database_url)
    row = SQLiteStore(database_url).fetch_all("agent_runs")[0]
    payload = json.loads(row["output_json"])
    payload["output"]["articles"][0][field] = replacement
    row["output_json"] = json.dumps(payload)

    assert _verified_signal_context_from_agent_run(row) is None


@pytest.mark.parametrize(
    "citation",
    [
        "{url}",
        "[source]({url})",
        "<{url}|source>",
        "Source: {url}.",
        "Source: ({url}),",
    ],
    ids=("bare", "markdown", "slack", "period", "parenthesis-comma"),
)
def test_equivalent_visible_source_citations_are_accepted(
    tmp_path: Path,
    citation: str,
) -> None:
    database_url = _database_url(tmp_path)
    source = _source("rss")
    _save_signal_run(database_url, sources=[source])
    state = _rehydrate_state(tmp_path, database_url, request="Assess that signal.")
    context = cli._verified_signal_context_from_execution_context(state)

    validation = cli._validate_direct_signal_context_response(
        f"Monitor it. {citation.format(url=source['url'])}",
        context,
    )

    assert validation["passed"] is True


def test_newer_partial_signal_blocks_pronoun_but_exact_older_source_is_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    older = _source("rss", 1)
    newer = _source("rss", 2)
    older_run_id = _save_signal_run(database_url, sources=[older])
    newer_run_id = _save_signal_run(
        database_url,
        sources=[newer],
        public_status="partial",
        completion_confirmed=False,
        request_ts="1789000002.000100",
    )

    pronoun_request = "What went wrong with that last signal?"
    pronoun_state = _rehydrate_state(
        tmp_path,
        database_url,
        request=pronoun_request,
        request_ts="1789000003.000100",
    )
    pronoun = _run_preflight(monkeypatch, pronoun_request, pronoun_state)
    assert "verified_signal_context" not in pronoun_state
    assert pronoun_state["signal_run_history"][0]["id"] == newer_run_id
    assert pronoun.composition_admission.reason == (
        "selected_context_latest_signal_incomplete"
    )
    assert pronoun.execution_allowed is False

    exact_request = f"Assess the earlier source exactly: {older['url']}"
    exact_state = _rehydrate_state(
        tmp_path,
        database_url,
        request=exact_request,
        request_ts="1789000003.000100",
    )
    exact = _run_preflight(monkeypatch, exact_request, exact_state)
    context = exact.composition_admission.verified_signal_context
    assert exact.execution_allowed is True
    assert context is not None
    assert context.source_run_id == older_run_id
    assert context.selection_basis == "explicit_older_signal"
    assert context.newer_signal_run_id == newer_run_id
    assert context.newer_signal_status == "partial"
    assert context.newer_signal_request_ts == "1789000002.000100"


def test_exact_older_source_remains_selectable_after_newer_completed_signal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    older = _source("rss", 1)
    newer = _source("rss", 2)
    older_run_id = _save_signal_run(database_url, sources=[older])
    newer_run_id = _save_signal_run(
        database_url,
        sources=[newer],
        request_ts="1789000002.000100",
    )

    generic_request = "Assess that latest signal."
    generic_state = _rehydrate_state(
        tmp_path,
        database_url,
        request=generic_request,
        request_ts="1789000003.000100",
    )
    generic = _run_preflight(monkeypatch, generic_request, generic_state)
    assert generic.composition_admission.source_run_id == newer_run_id

    exact_request = f"Assess the earlier source exactly: {older['url']}"
    exact_state = _rehydrate_state(
        tmp_path,
        database_url,
        request=exact_request,
        request_ts="1789000003.000100",
    )
    exact = _run_preflight(monkeypatch, exact_request, exact_state)
    context = exact.composition_admission.verified_signal_context
    assert exact.execution_allowed is True
    assert context is not None
    assert context.source_run_id == older_run_id
    assert context.selection_basis == "explicit_older_signal"
    assert context.newer_signal_run_id == newer_run_id
    assert context.newer_signal_status == "completed"


def test_intervening_non_signal_owner_keeps_signal_history_in_causal_position(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    database_url = _database_url(tmp_path)
    source = _source("rss")
    _save_signal_run(database_url, sources=[source])
    non_signal_id = SQLiteStore(database_url).save_agent_run(
        agent_name="opportunity_scout",
        input_summary="Assess the retained signal.",
        dry_run=False,
        status="success",
        output={
            "public_result": {
                "status": "completed",
                "text": "The opportunity remains monitor-only.",
                "completion_confirmed": True,
            },
            "slack_run_provenance": {
                "schema": "keystone.slack.run_provenance.v1",
                "context_validated": True,
                "team_id": TEAM_ID,
                "channel_id": CHANNEL_ID,
                "thread_ts": THREAD_TS,
                "request_ts": "1789000002.000100",
            },
        },
    )
    followup = "Make that assessment one sentence."
    state = _rehydrate_state(
        tmp_path,
        database_url,
        request=followup,
        request_ts="1789000003.000100",
        prior_agent_runs=[
            {
                "id": str(non_signal_id),
                "route": "opportunity_scout",
                "status": "completed",
                "summary": "The opportunity remains monitor-only.",
            }
        ],
    )

    preflight = _run_preflight(monkeypatch, followup, state)

    assert preflight.execution_allowed is True
    assert preflight.composition_admission.context_kind == "selected_thread_response"
    assert preflight.composition_admission.source_route == "opportunity_scout"
    assert state["prior_agent_runs"][-1]["id"] == str(non_signal_id)
    assert state["signal_run_history"][0]["request_ts"] == ROOT_REQUEST_TS

    model_inputs: list[str] = []

    def fake_sdk(**kwargs: Any) -> SimpleNamespace:
        model_inputs.append(kwargs["typed_input"].selected_context)
        return SimpleNamespace(
            output=DirectAgentResponse(
                answer="The event invitation lacks a confirmed registration deadline."
            ),
            usage={"requests": 1},
            cost={"estimated_usd": 0.001},
            budget_guard={"status": "passed"},
            request_cache={},
        )

    execution_context = cli._direct_specialist_execution_context(
        followup,
        workflow_state=state,
        force_thread_context=True,
        verified_signal_context=(
            preflight.composition_admission.verified_signal_context
        ),
    )
    monkeypatch.setattr(cli, "run_typed_sdk_agent", fake_sdk)
    result = cli._run_direct_supplied_context_response_live(
        "opportunity_scout",
        followup,
        json_output=True,
        manual_plan=preflight.manual_request_plan,
        orchestrator_preflight=preflight,
        sdk_session_spec=None,
        database_url=database_url,
        execution_context=execution_context,
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert source["url"] not in model_inputs[0]
    assert "verified_signal_context" not in execution_context
    assert payload["status"] == "completed"
    assert payload["public_result"]["completion_confirmed"] is True
    assert "retained_signal_validation" not in payload


@pytest.mark.parametrize(
    "answer",
    [
        "The evidence is preliminary, so monitor it without taking action.",
        (
            "Monitor the signal at https://public.example/rss/clinical-ai-1 and also "
            "rely on https://untrusted.example/extra"
        ),
        "Monitor the different item at https://public.example/rss/clinical-ai-999",
    ],
    ids=("missing-link", "extra-link", "mismatched-link"),
)
def test_direct_signal_response_blocks_missing_or_unverified_visible_links(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    answer: str,
) -> None:
    database_url = _database_url(tmp_path)
    source = _source("rss")
    _save_signal_run(database_url, sources=[source])
    request = "Should we pursue that signal?"
    state = _rehydrate_state(tmp_path, database_url, request=request)
    execution_context = cli._direct_specialist_execution_context(
        request,
        workflow_state=state,
        force_thread_context=True,
    )
    monkeypatch.setattr(
        cli,
        "run_typed_sdk_agent",
        lambda **_kwargs: SimpleNamespace(
            output=DirectAgentResponse(answer=answer),
            usage={"requests": 1},
            cost={"estimated_usd": 0.001},
            budget_guard={"status": "passed"},
            request_cache={},
        ),
    )
    monkeypatch.setattr(
        "keystone_agents.instruction_following.run_typed_sdk_agent",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("Offline test did not supply a citation repair")
        ),
    )

    result = cli._run_direct_supplied_context_response_live(
        "opportunity_scout",
        request,
        json_output=True,
        manual_plan=_context_only_plan(request),
        orchestrator_preflight=None,
        sdk_session_spec=None,
        database_url=database_url,
        execution_context=execution_context,
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["status"] == "blocked"
    assert payload["block_kind"] == "signal_context_identity_validation_failed"
    assert payload["completion_confirmed"] is False
    assert payload["public_result"]["completion_confirmed"] is False
    assert payload["retained_signal_validation"]["passed"] is False
    assert payload["tool_execution"]["provider_request_attempt_count"] == 0
    assert payload["side_effects"]["external_write_performed"] is False


@pytest.mark.parametrize(
    ("repair_kind", "source_scope", "completed"),
    [
        ("exact", "unspecified", True),
        ("exact", "answer", True),
        ("wrong", "unspecified", False),
        ("extra", "unspecified", False),
        ("failed", "unspecified", False),
    ],
)
def test_verified_signal_missing_citation_gets_one_tool_free_repair(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    repair_kind: str,
    source_scope: str,
    completed: bool,
) -> None:
    database_url = _database_url(tmp_path)
    source = _source("preprints")
    _save_signal_run(database_url, kind="preprints", sources=[source])
    request = "Should we cite and monitor that saved signal?"
    state = _rehydrate_state(tmp_path, database_url, request=request)
    execution_context = cli._direct_specialist_execution_context(
        request, workflow_state=state, force_thread_context=True,
    )
    plan = _context_only_plan(request)
    plan = plan.model_copy(update={
        "ask_shape": plan.ask_shape.model_copy(update={
            "output_constraints": plan.ask_shape.output_constraints.model_copy(
                update={"source_scope": source_scope}
            )
        })
    })
    assert plan.ask_shape.output_constraints.include_source_urls is False
    first_calls: list[dict[str, Any]] = []
    repair_calls: list[dict[str, Any]] = []

    def first_answer(**kwargs: Any) -> SimpleNamespace:
        first_calls.append(kwargs)
        assert kwargs["agent"].tools == []
        assert kwargs["typed_input"].original_request == request
        assert kwargs["typed_input"].output_constraints["include_source_urls"] is True
        assert kwargs["typed_input"].output_constraints["source_scope"] == source_scope
        assert source["url"] in kwargs["typed_input"].selected_context
        return SimpleNamespace(
            output=DirectAgentResponse(
                answer="The saved evidence is preliminary, so monitor it for now."
            ),
            usage={"requests": 1}, cost={"estimated_usd": 0.001},
            budget_guard={"status": "passed"}, request_cache={},
        )

    def repair_answer(**kwargs: Any) -> SimpleNamespace:
        repair_calls.append(kwargs)
        assert kwargs["agent"].tools == []
        assert kwargs["typed_input"].original_request == request
        assert kwargs["typed_input"].interpreted_constraints.include_source_urls is True
        assert kwargs["typed_input"].interpreted_constraints.source_scope == source_scope
        assert source["url"] in kwargs["typed_input"].bounded_evidence
        if repair_kind == "failed":
            raise RuntimeError("Offline repair failed")
        url = (
            source["url"]
            if repair_kind != "wrong"
            else "https://public.example/preprints/unrelated"
        )
        answer = f"The evidence is preliminary; monitor it. {url}"
        if repair_kind == "extra":
            answer += " https://untrusted.example/extra"
        return SimpleNamespace(
            output=InstructionFollowingRepairOutput(response_text=answer),
            usage={"requests": 1}, cost={"estimated_usd": 0.001},
            request_cache={},
        )

    monkeypatch.setattr(cli, "run_typed_sdk_agent", first_answer)
    monkeypatch.setattr(
        "keystone_agents.instruction_following.run_typed_sdk_agent",
        repair_answer,
    )
    result = cli._run_direct_supplied_context_response_live(
        "opportunity_scout", request, json_output=True, manual_plan=plan,
        orchestrator_preflight=None, sdk_session_spec=None,
        database_url=database_url, execution_context=execution_context,
    )
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert len(first_calls) == len(repair_calls) == 1
    assert payload["manual_request_plan"]["ask_shape"]["output_constraints"][
        "include_source_urls"
    ] is False
    assert payload["instruction_following"]["repair_attempted"] is True
    if repair_kind in {"wrong", "extra"}:
        assert payload["instruction_following"]["validation"]["passed"] is True
    assert payload["status"] == ("completed" if completed else "blocked")
    assert payload["public_result"]["completion_confirmed"] is completed
    assert payload["retained_signal_validation"]["passed"] is completed
    assert payload["tool_admission"]["tool_count"] == 0
    assert payload["tool_execution"]["provider_request_attempt_count"] == 0
    assert payload["side_effects"]["external_write_performed"] is False
    if completed:
        assert source["url"] in payload["public_result"]["text"]
    else:
        assert source["url"] not in payload["public_result"]["text"]


def _signal_owner_route(kind: str) -> str:
    return "preprints_context_agent" if kind == "preprints" else "rss_context_agent"


def _direct_signal_owner_state(
    database_url: str,
    *,
    kind: str,
    source: dict[str, str],
) -> dict[str, Any]:
    run_id = _save_signal_run(database_url, kind=kind, sources=[source])
    row = SQLiteStore(database_url).fetch_all("agent_runs")[0]
    context = _verified_signal_context_from_agent_run(row)
    assert context is not None
    return {
        "prior_agent_runs": [
            {
                "id": run_id,
                "route": _signal_owner_route(kind),
                "status": "completed",
                "thread_correlation": "same_thread",
                "summary": "Selected one bounded public signal.",
            }
        ],
        "verified_signal_context": context.model_dump(mode="json"),
    }


@pytest.mark.parametrize("kind", ("rss", "preprints"))
@pytest.mark.parametrize("state_path", ("direct", "persisted_handoff"))
def test_signal_owner_context_only_response_preserves_verified_source_without_reread(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    kind: str,
    state_path: str,
) -> None:
    database_url = _database_url(tmp_path)
    source = _source(kind)
    source["summary"] = (
        "The retained source defines iTBS as intermittent theta-burst stimulation "
        "and reports preliminary evidence only."
    )
    request = "Define iTBS from the retained source and keep its link."
    if state_path == "direct":
        state = _direct_signal_owner_state(
            database_url,
            kind=kind,
            source=source,
        )
    else:
        _save_signal_run(database_url, kind=kind, sources=[source])
        state = _rehydrate_state(tmp_path, database_url, request=request)

    route = _signal_owner_route(kind)
    monkeypatch.setattr(
        orchestrator_module,
        "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: OrchestratorResult(
            route=route,
            workflow=[route],
            routing_mode="llm",
            context_only_response=True,
            rationale=(
                "Answer from the verified same-thread signal without another provider read."
            ),
        ),
    )
    preflight = orchestrator_module.run_orchestrator_preflight(
        request,
        requested_agent=route,
        live_orchestrator=True,
        workflow_state=state,
    )

    assert preflight.execution_allowed is True
    assert preflight.manual_request_plan.source == "canonical:orchestrator_context_only"
    assert preflight.manual_request_plan.target_agent == route
    assert preflight.composition_admission.composition_allowed is True
    assert preflight.composition_admission.context_kind == "verified_signal_context"
    retained = preflight.composition_admission.verified_signal_context
    assert retained is not None
    assert retained.selected_sources[0].source_id == source["feed_item_id"]
    assert retained.selected_sources[0].supported_claim == source["summary"]
    assert retained.selected_sources[0].url == source["url"]
    assert retained.source_dates[source["feed_item_id"]] == source["published_at"]

    execution_context = cli._direct_specialist_execution_context(
        request,
        workflow_state=state,
        force_thread_context=True,
        verified_signal_context=retained,
    )
    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_preprint_announcement_history_impl",
        lambda **_kwargs: pytest.fail("retained-context response must not reread preprints"),
    )
    monkeypatch.setattr(
        announcement_context_tools,
        "retrieve_rss_announcement_history_impl",
        lambda **_kwargs: pytest.fail("retained-context response must not reread RSS"),
    )
    sdk_calls: list[dict[str, Any]] = []
    answer = (
        "iTBS means intermittent theta-burst stimulation. "
        f"Source: {source['url']}"
    )

    def fake_sdk(**kwargs: Any) -> SimpleNamespace:
        sdk_calls.append(kwargs)
        assert kwargs["agent"].name == route
        assert kwargs["agent"].tools == []
        assert kwargs["agent"].handoffs == []
        assert kwargs["max_turns"] == 1
        typed_input = kwargs["typed_input"]
        assert typed_input.original_request == request
        model_context = typed_input.selected_context
        assert source["feed_item_id"] in model_context
        assert source["url"] in model_context
        assert source["published_at"] in model_context
        assert "intermittent theta-burst stimulation" in model_context
        assert "preliminary evidence only" in model_context
        return SimpleNamespace(
            output=DirectAgentResponse(answer=answer),
            usage={"requests": 1},
            cost={"estimated_usd": 0.0},
            budget_guard={"status": "passed"},
            request_cache={},
        )

    monkeypatch.setattr(cli, "run_typed_sdk_agent", fake_sdk)
    exit_code = cli._run_ask_specialist_live(
        route,
        request,
        json_output=True,
        manual_plan=preflight.manual_request_plan,
        orchestrator_preflight=preflight,
        execution_context=execution_context,
        database_url=database_url,
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert len(sdk_calls) == 1
    assert payload["status"] == "completed"
    assert payload["selected_agent"] == route
    assert payload["public_result"]["text"] == answer
    assert payload["retained_signal_validation"]["passed"] is True
    assert payload["tool_admission"]["tool_count"] == 0
    assert payload["tool_execution"]["provider_request_attempt_count"] == 0
    assert payload["tool_execution"]["provider_receipt_count"] == 0
    assert payload["side_effects"]["external_write_performed"] is False


@pytest.mark.parametrize("kind", ("rss", "preprints"))
@pytest.mark.parametrize(
    ("context_state", "expected_reason"),
    (
        ("missing", "selected_context_source_missing"),
        ("unverified", "selected_context_source_unverified"),
        ("mismatched", "selected_context_source_mismatch"),
    ),
)
def test_signal_owner_context_only_response_fails_closed_for_bad_source_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    kind: str,
    context_state: str,
    expected_reason: str,
) -> None:
    route = _signal_owner_route(kind)
    source = _source(kind)
    state: dict[str, Any] = {
        "prior_agent_runs": [
            {
                "id": "run-signal-1",
                "route": route,
                "status": "completed",
                "thread_correlation": "same_thread",
                "summary": "Selected one bounded source.",
            }
        ]
    }
    if context_state != "missing":
        state["verified_signal_context"] = {
            "source_run_id": (
                "different-run" if context_state == "mismatched" else "run-signal-1"
            ),
            "source_route": route,
            "verification_status": (
                "unverified" if context_state == "unverified" else "verified"
            ),
            "public_result_status": (
                "partial" if context_state == "unverified" else "completed"
            ),
            "source_request_ts": ROOT_REQUEST_TS,
            "selected_sources": [
                {
                    "source_id": source["feed_item_id"],
                    "title": source["title"],
                    "url": source["url"],
                    "source_type": f"{kind}_historical_context",
                    "supported_claim": source["summary"],
                    "provider": route,
                }
            ],
            "source_dates": {source["feed_item_id"]: source["published_at"]},
        }
    monkeypatch.setattr(
        orchestrator_module,
        "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: OrchestratorResult(
            route=route,
            workflow=[route],
            routing_mode="llm",
            context_only_response=True,
            rationale="Answer only from verified same-thread evidence.",
        ),
    )

    preflight = orchestrator_module.run_orchestrator_preflight(
        "Explain the retained source in one sentence.",
        requested_agent=route,
        live_orchestrator=True,
        workflow_state=state,
    )

    assert preflight.execution_allowed is False
    assert preflight.block_kind == "context_only_response_unavailable"
    assert preflight.composition_admission.reason == expected_reason


@pytest.mark.parametrize("kind", ("rss", "preprints"))
def test_signal_owner_new_source_request_keeps_retrieval_tools_available(kind: str) -> None:
    route = _signal_owner_route(kind)
    request = "Find a new current source about behavioral-health evaluation."
    plan = ManualRequestPlan(
        source="canonical:test_new_signal_source",
        requested_agent=route,
        target_agent=route,
        intent="context_lookup",
        task_objective="context_lookup",
        expected_artifact_type="context_summary",
        objective=request,
        provider_system="unspecified",
        provider_operations=["read"],
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(permission_state="read_only"),
    )
    agent = (
        build_preprints_context_agent(
            request_text=request,
            manual_plan=plan,
            tool_tier="core_read",
        )
        if kind == "preprints"
        else build_rss_context_agent(
            request_text=request,
            manual_plan=plan,
            tool_tier="core_read",
        )
    )
    tool_names = {str(getattr(tool, "name", "")) for tool in agent.tools}

    assert cli._should_run_direct_supplied_response(
        request,
        requested_route=route,
        manual_plan=plan,
        provider_free_composition_allowed=False,
    ) is False
    assert (
        "retrieve_preprint_announcement_history"
        if kind == "preprints"
        else "retrieve_rss_announcement_history"
    ) in tool_names
