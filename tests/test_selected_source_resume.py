"""Exact record identity can restore a selected read, without selecting a new URL."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from keystone_agents.entrypoints.cli_impl import (
    _linked_work_item_admission_failure,
    _manual_plan_retries_linked_blocked_work_item,
    _manual_plan_reuses_linked_work_item,
)
from keystone_agents.operator_failures import OperatorReadableFailureError
from keystone_agents.planning.selected_source_resume import bind_selected_source_resume
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemApprovalGate,
    WorkItemBlocker,
    WorkItemKind,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
    WorkItemTarget,
)
from keystone_agents.tools import website_extraction_tool as web

URL = "https://example.org/selected-study"
ITEM_ID = "wi_synthetic_selected_source"


def _inputs():
    source = WorkItemSourceRef(
        source_id="selected-study", url=URL, title="Selected study",
        source_type="company_site", provider="selected-url", extraction_status="extracted",
        evidence_excerpt="A bounded observational study cannot establish causation.",
    )
    item = WorkItem(
        id=ITEM_ID, kind=WorkItemKind.RESEARCH_BRIEF,
        title="Saved source review",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(name="Prior resume prose", object_type="url", url=URL),
        sources=[source],
    )
    plan = ManualRequestPlan(
        source="heuristic", target_agent="business_research_analyst", intent="research_brief",
        task_objective="source_research", primary_target="Resume the prior evidence summary",
        target_type="unknown", expected_artifact_type="source_summary",
        requires_durable_state=True, requires_live_search=False,
        side_effect_policy="draft_or_read_only", workflow=["business_research_analyst"],
        ask_shape=AskShapePolicy(
            prior_context_dependency="selected_context", permission_state="read_only",
        ),
    )
    return {
        "plan": plan, "work_item": item, "work_item_id": ITEM_ID,
        "current_request": f"Resume WorkItem {ITEM_ID} and finish its saved source summary.",
        "route_result": {
            "route": "business_research_analyst", "routing_mode": "llm",
            "workflow": ["business_research_analyst"], "selected_source_url": "",
        },
    }


def test_saved_record_identity_restores_exact_read_without_changing_request_or_source():
    args = _inputs()
    original_item = args["work_item"].model_dump(mode="json")
    original_plan = args["plan"].model_dump(mode="json")
    binding = bind_selected_source_resume(**args)
    assert binding is not None
    assert binding.plan.primary_target == URL
    assert binding.plan.target_type == "url"
    assert binding.plan.provider_operations == ["read"]
    assert binding.plan.ask_shape.strict_filter_mode == "exact"
    assert binding.plan.requires_durable_state
    assert not binding.plan.requires_live_search
    assert binding.source == args["work_item"].sources[0]
    assert binding.provenance()["new_source_selected"] is False
    assert args["work_item"].model_dump(mode="json") == original_item
    assert args["plan"].model_dump(mode="json") == original_plan


def test_unrelated_context_url_does_not_replace_or_ambiguate_selected_identity():
    args = _inputs()
    args["work_item"].sources.insert(0, WorkItemSourceRef(
        source_id="context-handoff", url="https://example.org/context",
        source_type="dry_run_context_handoff", provider="zotero_context_agent",
    ))
    binding = bind_selected_source_resume(**args)
    assert binding.source.source_id == "selected-study"


@pytest.mark.parametrize("mutation", [
    "different_model_url", "different_plan_url", "different_request_url", "multi_url_target",
    "conflicting_source_identity", "source_absent", "write", "broad_search",
    "model_broad_search",
])
def test_conflicting_source_or_operation_fails_before_any_provider(mutation):
    args = _inputs()
    other_url = "https://example.org/other-study"
    if mutation == "different_model_url":
        args["route_result"]["selected_source_url"] = other_url
    elif mutation == "different_plan_url":
        args["plan"] = args["plan"].model_copy(update={
            "primary_target": other_url, "target_type": "url",
        })
    elif mutation == "different_request_url":
        args["current_request"] += f" Use {other_url}."
    elif mutation == "multi_url_target":
        args["work_item"].target.url += " " + other_url
    elif mutation == "conflicting_source_identity":
        args["work_item"].sources.append(args["work_item"].sources[0].model_copy(
            update={"source_id": "another-identity"},
        ))
    elif mutation == "source_absent":
        args["work_item"].sources = []
    elif mutation == "write":
        args["plan"] = args["plan"].model_copy(update={"provider_operations": ["update"]})
    elif mutation == "broad_search":
        args["plan"] = args["plan"].model_copy(update={"requires_live_search": True})
    else:
        args["route_result"]["requires_live_search"] = True
    with pytest.raises(OperatorReadableFailureError):
        bind_selected_source_resume(**args)


@pytest.mark.parametrize("missing", ["exact_current_id", "model_owned_route"])
def test_record_adaptation_does_not_grant_a_route_without_current_proof(missing):
    args = _inputs()
    if missing == "exact_current_id":
        args["current_request"] = "Summarize the prior selected study."
    else:
        args["route_result"]["routing_mode"] = "deterministic"
    assert bind_selected_source_resume(**args) is None


def test_historical_envelope_urls_do_not_replace_the_current_operator_request():
    args = _inputs()
    args["current_request"] = (
        "Continue this prior Slack thread. "
        "Previous request: Read an older source. "
        "Previous result: https://example.org/historical-source "
        f"User follow-up: {args['current_request']}"
    )
    binding = bind_selected_source_resume(**args)
    assert binding is not None
    assert binding.source.url == URL


@pytest.mark.parametrize("intent", ["internal_review_handoff", "internal_review"])
@pytest.mark.parametrize("scope", ["non_resume", "other_owner", "no_model_route"])
def test_unrelated_internal_handoff_plans_are_ignored_before_public_validation(intent, scope):
    args = _inputs()
    args["plan"] = {"intent": intent, "target_agent": "chief_of_staff"}
    if scope == "non_resume":
        args["current_request"] = "Review the project and hand off the next safe task."
    elif scope == "other_owner":
        args["route_result"]["route"] = "chief_of_staff"
    else:
        args["route_result"]["routing_mode"] = "deterministic"
    assert bind_selected_source_resume(**args) is None


def test_targeted_research_resume_keeps_public_plan_validation():
    args = _inputs()
    args["plan"] = {
        "intent": "internal_review_handoff", "target_agent": "business_research_analyst",
    }
    with pytest.raises(ValidationError):
        bind_selected_source_resume(**args)


@pytest.fixture
def cached_source(monkeypatch, tmp_path):
    monkeypatch.setenv("KEYSTONE_RUNTIME_STATE_DIR", str(tmp_path))
    args = _inputs()
    preview, access = web.project_web_source(
        web.WebsiteExtractionResult(
            url=URL, provider="trafilatura", status="success",
            text_or_markdown="Synthetic retained study evidence. " * 100,
        ),
        selected_url=URL, source_id="selected-study", max_chars=1200,
    )
    args["work_item"].sources[0].web_source_access = access
    args["work_item"].sources[0].evidence_excerpt = preview
    args["plan"] = args["plan"].model_copy(update={
        "ask_shape": args["plan"].ask_shape.model_copy(update={
            "prior_context_dependency": "unspecified", "strict_filter_mode": "exact",
        }),
    })
    args["route_result"]["selected_source_url"] = URL
    return args, tmp_path / "web-source-snapshots" / f"{access.snapshot_sha256}.json"


def test_exact_model_selected_cached_source_binds_unspecified_dependency(
    cached_source, monkeypatch,
):
    args, _ = cached_source
    before = args["work_item"].model_dump(mode="json")

    def forbidden(*args, **kwargs):
        raise AssertionError("Identity validation must not fetch or emit model read coverage.")

    monkeypatch.setattr(web, "read_web_source_window_impl", forbidden)
    monkeypatch.setattr(web, "extract_website_content", forbidden)
    binding = bind_selected_source_resume(**args)
    assert binding is not None
    assert binding.plan.ask_shape.prior_context_dependency == "selected_context"
    assert binding.plan.primary_target == URL
    assert binding.plan.provider_operations == ["read"]
    assert binding.provenance()["new_extraction_permitted"] is False
    assert args["work_item"].model_dump(mode="json") == before


def test_exact_work_item_cached_source_binds_when_model_omits_optional_url(
    cached_source, monkeypatch,
):
    args, _ = cached_source
    args["route_result"]["selected_source_url"] = ""
    args["work_item"].status = WorkItemStatus.DONE
    before = args["work_item"].model_dump(mode="json")

    def forbidden(*args, **kwargs):
        raise AssertionError("Binding must not fetch or read the retained source.")

    monkeypatch.setattr(web, "read_web_source_window_impl", forbidden)
    monkeypatch.setattr(web, "extract_website_content", forbidden)
    binding = bind_selected_source_resume(**args)
    assert binding is not None
    assert binding.plan.primary_target == URL
    assert _manual_plan_reuses_linked_work_item(
        binding.plan, work_item=args["work_item"], source_resume_binding=binding,
    )
    assert args["work_item"].model_dump(mode="json") == before


@pytest.mark.parametrize("failure", ["missing_handle", "different_current_id", "write"])
def test_omitted_model_url_does_not_relax_cache_identity_or_permission(
    cached_source, failure,
):
    args, _ = cached_source
    args["route_result"]["selected_source_url"] = ""
    if failure == "missing_handle":
        args["work_item"].sources[0].web_source_access = None
        with pytest.raises(OperatorReadableFailureError):
            bind_selected_source_resume(**args)
    elif failure == "different_current_id":
        args["current_request"] = "Resume WorkItem wi_other and summarize it."
        assert bind_selected_source_resume(**args) is None
    else:
        args["plan"] = args["plan"].model_copy(update={"provider_operations": ["update"]})
        with pytest.raises(OperatorReadableFailureError):
            bind_selected_source_resume(**args)


def test_authenticated_thread_link_can_bind_a_selected_context_without_repeating_id(
    cached_source, tmp_path,
):
    args, _ = cached_source
    item = args["work_item"]
    item.status = WorkItemStatus.DONE
    item.target.metadata["slack_context"] = {
        "channel_id": "C_SYNTHETIC", "thread_ts": "123.456",
    }
    context_path = tmp_path / "current-slack-context.json"
    context_path.write_text(json.dumps({
        "schema": "keystone.slack.history_context.v1",
        "channel_id": "C_SYNTHETIC", "thread_ts": "123.456",
    }))
    args["current_request"] = "Continue this selected source summary."
    args["route_result"]["selected_source_url"] = ""
    args["route_result"]["linked_work_item_relation"] = "continue_existing"
    args["plan"] = args["plan"].model_copy(update={
        "ask_shape": args["plan"].ask_shape.model_copy(update={
            "prior_context_dependency": "selected_context",
        }),
    })
    args["slack_context_path"] = str(context_path)
    binding = bind_selected_source_resume(**args)
    assert binding is not None
    assert _manual_plan_reuses_linked_work_item(
        binding.plan, work_item=item, source_resume_binding=binding,
    )

    args["current_request"] = "Summarize this selected source as a new task."
    args["plan"] = args["plan"].model_copy(update={
        "ask_shape": args["plan"].ask_shape.model_copy(update={
            "prior_context_dependency": "unspecified",
        }),
    })
    args["route_result"]["linked_work_item_relation"] = "new_request"
    assert bind_selected_source_resume(**args) is None

    args["current_request"] = "Continue this selected source summary."
    args["route_result"]["linked_work_item_relation"] = "continue_existing"
    args["plan"] = args["plan"].model_copy(update={
        "ask_shape": args["plan"].ask_shape.model_copy(update={
            "prior_context_dependency": "selected_context",
        }),
    })
    context_path.write_text(json.dumps({
        "schema": "keystone.slack.history_context.v1",
        "channel_id": "C_OTHER", "thread_ts": "123.456",
    }))
    assert bind_selected_source_resume(**args) is None

    context_path.write_text(json.dumps({
        "schema": "keystone.slack.history_context.v1",
        "channel_id": "C_SYNTHETIC", "thread_ts": "123.456",
    }))
    for relation in ("branch_existing", "new_request", "clarify", "unspecified"):
        args["route_result"]["linked_work_item_relation"] = relation
        assert bind_selected_source_resume(**args) is None


def test_model_continuation_restores_only_saved_read_scope_from_heuristic_short_reply(
    cached_source, tmp_path,
):
    args, _ = cached_source
    item = args["work_item"]
    item.status = WorkItemStatus.DONE
    item.target.metadata["slack_context"] = {
        "channel_id": "C_SYNTHETIC", "thread_ts": "123.456",
    }
    saved = bind_selected_source_resume(**args)
    item.target.metadata["manual_request_plan"] = saved.plan.model_dump(mode="json")
    path = tmp_path / "current-slack-context.json"
    path.write_text(json.dumps({
        "schema": "keystone.slack.history_context.v1",
        "channel_id": "C_SYNTHETIC", "thread_ts": "123.456",
    }))
    args["current_request"] = "Try again."
    args["slack_context_path"] = str(path)
    args["route_result"].update({
        "selected_source_url": "", "linked_work_item_relation": "continue_existing",
        "requires_live_search": False,
    })
    args["plan"] = args["plan"].model_copy(update={
        "primary_target": "Try", "target_type": "company",
        "expected_artifact_type": "research_brief", "requires_live_search": True,
        "ask_shape": args["plan"].ask_shape.model_copy(update={
            "prior_context_dependency": "unspecified", "permission_state": "unspecified",
        }),
    })
    binding = bind_selected_source_resume(**args)
    assert binding is not None
    assert binding.plan.primary_target == URL
    assert binding.plan.provider_operations == ["read"]
    assert not binding.plan.requires_live_search
    assert binding.plan.ask_shape.permission_state == "read_only"
    del item.target.metadata["manual_request_plan"]
    assert bind_selected_source_resume(**args) is None


def test_blocked_exact_cached_retry_requires_intact_snapshot(cached_source):
    args, _ = cached_source
    item = args["work_item"]
    item.status = WorkItemStatus.BLOCKED
    item.blockers = [WorkItemBlocker(
        code="business_research_analyst_execution_failed", message="Previous read failed.",
    )]
    item.next_action = WorkItemNextAction(
        action="review_business_research_analyst_execution_failure",
        agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )
    plan = bind_selected_source_resume(**args).plan
    assert _manual_plan_retries_linked_blocked_work_item(
        plan, work_item=item, authenticated_thread_link=True,
    )
    item.sources[0].web_source_access = None
    assert not _manual_plan_retries_linked_blocked_work_item(
        plan, work_item=item, authenticated_thread_link=True,
    )


def test_blocked_research_brief_retry_keeps_saved_artifact_and_source(cached_source):
    args, _ = cached_source
    item = args["work_item"]
    item.status = WorkItemStatus.BLOCKED
    item.target.metadata["manual_expected_artifact_type"] = "research_brief"
    item.blockers = [WorkItemBlocker(
        code="business_research_analyst_agent_decision_invalid",
        message="Prior source decision failed.",
    )]
    item.next_action = WorkItemNextAction(
        action="retry_business_research_analyst_agent_decision",
        agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
    )
    args["plan"] = args["plan"].model_copy(update={
        "intent": "research_brief", "expected_artifact_type": "research_brief",
    })
    before = item.model_dump(mode="json")
    binding = bind_selected_source_resume(**args)
    assert binding is not None
    assert binding.plan.intent == "research_brief"
    assert binding.plan.expected_artifact_type == "research_brief"
    assert binding.plan.primary_target == URL
    assert binding.plan.provider_operations == ["read"]
    assert _manual_plan_retries_linked_blocked_work_item(
        binding.plan, work_item=item, authenticated_thread_link=True,
    )
    assert item.model_dump(mode="json") == before

    item.target.metadata["manual_expected_artifact_type"] = "source_summary"
    assert bind_selected_source_resume(**args) is None
    item.target.metadata["manual_expected_artifact_type"] = "research_brief"
    item.approval_gates.append(WorkItemApprovalGate(scope="external_use", state="pending"))
    assert not _manual_plan_retries_linked_blocked_work_item(
        binding.plan, work_item=item, authenticated_thread_link=True,
    )


def test_short_retry_restores_saved_research_brief_plan_without_new_source(cached_source, tmp_path):
    args, _ = cached_source
    item = args["work_item"]
    item.target.metadata["slack_context"] = {
        "channel_id": "C_SYNTHETIC", "thread_ts": "123.456",
    }
    saved_plan = args["plan"].model_copy(update={
        "source": "canonical:orchestrator_selected_source",
        "intent": "research_brief", "expected_artifact_type": "research_brief",
        "primary_target": URL, "target_type": "url", "provider_operations": ["read"],
        "requires_live_search": False,
        "ask_shape": args["plan"].ask_shape.model_copy(update={
            "permission_state": "read_only", "prior_context_dependency": "selected_context",
        }),
    })
    item.target.metadata["manual_request_plan"] = saved_plan.model_dump(mode="json")
    item.target.metadata["manual_expected_artifact_type"] = "research_brief"
    path = tmp_path / "current-slack-context.json"
    path.write_text(json.dumps({
        "schema": "keystone.slack.history_context.v1",
        "channel_id": "C_SYNTHETIC", "thread_ts": "123.456",
    }))
    args["current_request"] = "Try again."
    args["slack_context_path"] = str(path)
    args["route_result"].update({
        "selected_source_url": "", "linked_work_item_relation": "continue_existing",
    })
    args["plan"] = args["plan"].model_copy(update={
        "expected_artifact_type": "source_summary", "requires_live_search": True,
        "ask_shape": args["plan"].ask_shape.model_copy(update={
            "prior_context_dependency": "unspecified", "permission_state": "unspecified",
        }),
    })
    binding = bind_selected_source_resume(**args)
    assert binding is not None
    assert binding.plan.intent == "research_brief"
    assert binding.plan.expected_artifact_type == "research_brief"
    assert binding.plan.primary_target == URL
    assert not binding.plan.requires_live_search


def test_failed_continuation_reports_host_gate_without_exposing_task_identity():
    args = _inputs()
    args["work_item"].target.metadata["manual_expected_artifact_type"] = "research_brief"
    error = _linked_work_item_admission_failure(
        args["plan"], work_item=args["work_item"],
        source_resume_binding=None, linked_relation="continue_existing",
    )
    assert error.failure.kind == "guardrail_block"
    diagnostic = error.keystone_host_continuation_admission
    assert diagnostic == {
        "layer": "host_continuation_admission",
        "plan_artifact": "source_summary",
        "task_artifact": "research_brief",
        "source_binding": "not_admitted",
        "relation": "continue_existing",
        "failed_gate_code": "linked_reuse_gate_denied",
    }
    assert "host_continuation_admission" not in error.failure.reason
    assert "failed_gate_code" not in error.failure.reason
    assert ITEM_ID not in error.failure.reason
    assert URL not in error.failure.reason


@pytest.mark.parametrize("failure", [
    "missing_handle", "unavailable", "missing_file", "hash_mismatch", "different_source_id",
    "different_model_url", "approved_context_required",
])
def test_unspecified_dependency_never_falls_back_to_fresh_acquisition(cached_source, failure):
    args, path = cached_source
    source = args["work_item"].sources[0]
    if failure == "missing_handle":
        source.web_source_access = None
    elif failure == "unavailable":
        source.web_source_access = source.web_source_access.model_copy(update={"available": False})
    elif failure == "missing_file":
        path.unlink()
    elif failure == "hash_mismatch":
        payload = json.loads(path.read_text())
        payload["text"] += " Changed cached material."
        path.write_text(json.dumps(payload))
    elif failure == "different_source_id":
        source.web_source_access = source.web_source_access.model_copy(
            update={"source_id": "other"},
        )
    elif failure == "different_model_url":
        args["route_result"]["selected_source_url"] = "https://example.org/other-source"
    else:
        args["plan"] = args["plan"].model_copy(update={"requires_approved_context": True})
    with pytest.raises(OperatorReadableFailureError):
        bind_selected_source_resume(**args)


@pytest.mark.parametrize("ineligible", ["refused", "context_only"])
def test_cache_identity_cannot_override_a_refused_or_context_only_route(cached_source, ineligible):
    args, _ = cached_source
    if ineligible == "refused":
        args["route_result"]["refused"] = True
    else:
        args["route_result"]["context_only_response"] = True
    assert bind_selected_source_resume(**args) is None


def test_cached_identity_validation_is_metadata_only(cached_source):
    args, _ = cached_source
    access = args["work_item"].sources[0].web_source_access
    before = access.model_dump()
    assert web.validate_retained_web_source_access(access) is True
    assert access.model_dump() == before


@pytest.mark.parametrize("status", [WorkItemStatus.DONE, WorkItemStatus.IN_PROGRESS])
def test_verified_source_resume_reuses_exact_record_without_artifact_type_assumptions(
    cached_source, status,
):
    args, _ = cached_source
    item = args["work_item"]
    item.status = status
    assert not item.artifact_refs
    binding = bind_selected_source_resume(**args)
    assert _manual_plan_reuses_linked_work_item(
        binding.plan, work_item=item, source_resume_binding=binding,
    )
    assert not _manual_plan_reuses_linked_work_item(binding.plan, work_item=item)
    assert not _manual_plan_reuses_linked_work_item(
        binding.plan, work_item=item, source_resume_binding=binding.provenance(),
    )


@pytest.mark.parametrize("gate", [
    "archived", "needs_context", "pending_approval", "unresolved_blocker", "changed_plan",
])
def test_verified_source_binding_does_not_bypass_other_record_gates(cached_source, gate):
    args, _ = cached_source
    item = args["work_item"]
    item.status = WorkItemStatus.DONE
    binding = bind_selected_source_resume(**args)
    plan = binding.plan
    if gate == "archived":
        item.status = WorkItemStatus.ARCHIVED
    elif gate == "needs_context":
        item.status = WorkItemStatus.NEEDS_CONTEXT
    elif gate == "pending_approval":
        item.approval_gates.append(WorkItemApprovalGate(scope="external_use", state="pending"))
    elif gate == "unresolved_blocker":
        item.blockers.append(WorkItemBlocker(
            code="source_sufficiency_required", message="Still open.",
        ))
    else:
        plan = plan.model_copy(update={"provider_operations": ["search"]})
    assert not _manual_plan_reuses_linked_work_item(
        plan, work_item=item, source_resume_binding=binding,
    )
