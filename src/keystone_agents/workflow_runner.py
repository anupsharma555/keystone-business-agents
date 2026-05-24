"""Deterministic WorkItem workflow runner for safe single-step advancement."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from keystone_agents.agents.business_research_analyst import (
    run_business_research_analyst_research_brief_sdk,
)
from keystone_agents.agents.chief_of_staff import (
    plan_chief_of_staff_request,
    run_chief_of_staff_sdk,
)
from keystone_agents.agents.opportunity_scout import scout_opportunities_fixture
from keystone_agents.agents.opportunity_search_planner import resolve_opportunity_search_plan
from keystone_agents.agents.orchestrator import route_request
from keystone_agents.agents.outreach_composer import (
    build_approved_outreach_drafting_context,
    build_outreach_composer_compact_synthesis_agent,
    compose_outreach_draft_fixture,
    compose_outreach_draft_llm_constrained,
    load_style_profile,
)
from keystone_agents.cli_sdk import jsonable
from keystone_agents.company_research import research_company_fixture
from keystone_agents.contact_enrichment import build_contact_enrichment_artifact
from keystone_agents.live_retrieval import (
    retrieve_company_profile_live,
    run_opportunity_scout_live,
)
from keystone_agents.memory import retrieval_tool_performance_memory_item
from keystone_agents.models import OutreachComposerSDKInput, ResearchSDKInput
from keystone_agents.run import run_retrieved_sdk_synthesis
from keystone_agents.schemas.approval import ApprovalScope, ApprovalState
from keystone_agents.schemas.opportunity import OpportunityRecord as ScoutOpportunityRecord
from keystone_agents.schemas.outreach import (
    OpportunityRecord as OutreachOpportunityRecord,
)
from keystone_agents.schemas.outreach import (
    OutreachLLMDraftPayload,
)
from keystone_agents.schemas.research import ResearchBrief
from keystone_agents.schemas.work_item import (
    WorkflowRunRequest,
    WorkflowRunResult,
    WorkItem,
    WorkItemApprovalGate,
    WorkItemArtifactRef,
    WorkItemBlocker,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
    utc_now_iso,
)
from keystone_agents.sdk_sessions import (
    build_sdk_session,
    context_file_session_components,
    resolve_sdk_session_spec,
)
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env
from keystone_agents.tools.approval_tool import build_approval_queue_item
from keystone_agents.work_items import (
    add_blocker,
    attach_artifact,
    build_context_pack_for_route,
    create_or_load_work_item,
    derive_case_status,
    drafting_ready,
    infer_route_for_continue,
    normalize_target_text,
    opportunity_ready,
    record_event,
    research_ready,
    selected_artifacts,
    set_next_action,
)
from keystone_agents.zotero_research import (
    build_zotero_article_research_brief,
    build_zotero_collection_research_brief,
    build_zotero_live_source_context,
    extract_zotero_article_query,
    extract_zotero_collection_hint,
    looks_like_zotero_article_request,
    looks_like_zotero_collection_request,
)


def advance_work_item(request: WorkflowRunRequest) -> WorkflowRunResult:
    """Advance a WorkItem by one deterministic, side-effect-safe step."""

    store = SQLiteStore(request.database_url or database_url_from_env()) if request.save else None
    input_text = request.request_text.strip()
    route = _select_route(request, input_text, store)
    work_item = create_or_load_work_item(
        store=store,
        request_text=input_text,
        work_item_id=request.work_item_id,
        route=route,
    )
    if input_text and input_text.lower() not in {"continue", "resume"}:
        work_item = work_item.model_copy(update={"request_text": input_text})
    work_item = _apply_manual_request_plan(work_item, request.manual_request_plan)
    external_context = _load_external_context(request)
    work_item = _apply_external_context(
        work_item,
        external_context,
        context_file_path=request.context_file_path,
    )
    sdk_session_spec = _sdk_session_spec_for_work_item(request, work_item)
    if route != WorkItemRoute.ORCHESTRATOR:
        work_item = work_item.model_copy(
            update={
                "current_route": route,
                "kind": create_or_load_work_item(
                    store=None,
                    request_text=work_item.request_text or input_text,
                    route=route,
                ).kind,
            }
        )

    context_pack = build_context_pack_for_route(work_item, route, store=store)
    record_event(
        work_item,
        event_type="advance_started",
        summary=f"Advancing via {route.value}.",
        metadata={
            "live_search": request.live_search,
            "live_sdk": request.live_sdk,
            "sdk_session": sdk_session_spec.log_metadata(),
            "context_pack": {
                "pack_type": context_pack.pack_type,
                "ready": context_pack.ready,
                "readiness_gates": [
                    {
                        "name": gate.name,
                        "ready": gate.ready,
                        "required": gate.required,
                    }
                    for gate in context_pack.readiness_gates
                ],
            },
            "manual_request_plan": _manual_plan_event_payload(request.manual_request_plan),
            "external_context": _external_context_event_payload(
                external_context,
                context_file_path=request.context_file_path,
            ),
        },
        store=store,
    )

    if route == WorkItemRoute.BUSINESS_RESEARCH_ANALYST:
        result = _advance_research(
            work_item,
            request=request,
            store=store,
            sdk_session=build_sdk_session(sdk_session_spec) if request.live_sdk else None,
        )
    elif route == WorkItemRoute.OPPORTUNITY_SCOUT:
        result = _advance_opportunity(work_item, request=request, store=store)
    elif route == WorkItemRoute.OUTREACH_COMPOSER:
        result = _advance_outreach(work_item, request=request, store=store)
    elif route == WorkItemRoute.CHIEF_OF_STAFF:
        result = _advance_chief_of_staff(
            work_item,
            request=request,
            store=store,
            sdk_session=build_sdk_session(sdk_session_spec) if request.live_sdk else None,
        )
    else:
        result = _block_unsupported_route(work_item, route=route, store=store)

    if store is not None:
        store.save_work_item(result.work_item)
    final_context_pack = build_context_pack_for_route(result.work_item, result.route, store=store)
    return result.model_copy(
        update={
            "manual_request_plan": request.manual_request_plan,
            "context_pack": final_context_pack.model_dump(mode="json"),
        }
    )


def _select_route(
    request: WorkflowRunRequest,
    input_text: str,
    store: SQLiteStore | None,
) -> WorkItemRoute:
    if request.requested_route is not None:
        return request.requested_route
    planned_route = _route_from_manual_plan(request.manual_request_plan)
    if planned_route is not None:
        return planned_route
    existing: WorkItem | None = None
    if request.work_item_id and store is not None:
        existing = store.get_work_item(request.work_item_id)
    if existing is not None and input_text.lower() in {"", "continue", "resume"}:
        continued = infer_route_for_continue(existing)
        if continued is not None:
            return continued
        return existing.current_route
    if input_text and _looks_like_outreach_request(input_text):
        return WorkItemRoute.OUTREACH_COMPOSER
    decision = route_request(input_text)
    try:
        return WorkItemRoute(decision.route)
    except ValueError:
        return WorkItemRoute.CLARIFICATION


def _route_from_manual_plan(plan: dict | None) -> WorkItemRoute | None:
    if not isinstance(plan, dict):
        return None
    target_agent = str(plan.get("target_agent") or "").strip()
    if target_agent in {"", WorkItemRoute.ORCHESTRATOR.value, WorkItemRoute.CLARIFICATION.value}:
        return None
    try:
        return WorkItemRoute(target_agent)
    except ValueError:
        return None


def _sdk_session_spec_for_work_item(request: WorkflowRunRequest, work_item: WorkItem) -> Any:
    context_scope = context_file_session_components(request.context_file_path)
    if context_scope is not None:
        scope, components = context_scope
    else:
        scope = "workitem"
        components = (work_item.id,)
    return resolve_sdk_session_spec(
        scope=scope,
        components=components,
        enabled=request.sdk_session_enabled,
        explicit_session_id=request.sdk_session_id,
        database_path=request.sdk_session_db_path,
        default_enabled=request.live_sdk,
    )


def _apply_manual_request_plan(work_item: WorkItem, plan: dict | None) -> WorkItem:
    if not isinstance(plan, dict) or not plan:
        return work_item
    metadata = {
        **work_item.target.metadata,
        "manual_request_plan": _manual_plan_event_payload(plan),
    }
    for key in (
        "constraints",
        "desired_count",
        "gmail_query",
        "lookback_days",
        "draft_policy",
        "recipient",
        "outreach_channel",
        "tone",
    ):
        value = plan.get(key)
        if value not in (None, "", []):
            metadata[f"manual_{key}"] = value
    primary_target = str(plan.get("primary_target") or "").strip()
    target_type = str(plan.get("target_type") or "").strip()
    target = work_item.target
    if primary_target and not target.name:
        target = target.model_copy(update={"name": primary_target})
    if target_type and target_type != "unknown":
        target = target.model_copy(update={"object_type": target_type})
    return work_item.model_copy(
        update={"target": target.model_copy(update={"metadata": metadata})}
    ).touch()


def _manual_primary_target(work_item: WorkItem) -> str:
    plan = work_item.target.metadata.get("manual_request_plan")
    if not isinstance(plan, dict):
        return ""
    return " ".join(str(plan.get("primary_target") or "").strip().split())


def _manual_plan_event_payload(plan: dict | None) -> dict:
    if not isinstance(plan, dict):
        return {}
    allowed = {
        "source",
        "requested_agent",
        "target_agent",
        "intent",
        "primary_target",
        "target_type",
        "objective",
        "desired_count",
        "constraints",
        "requires_live_search",
        "requires_approved_context",
        "side_effect_policy",
        "planner_warnings",
        "gmail_query",
        "lookback_days",
        "draft_policy",
        "recipient",
        "outreach_channel",
        "tone",
    }
    return {key: plan[key] for key in allowed if key in plan and plan[key] not in (None, "")}


def _load_external_context(request: WorkflowRunRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if isinstance(request.external_context, dict):
        payload.update(request.external_context)
    if request.context_file_path:
        file_payload = json.loads(Path(request.context_file_path).read_text(encoding="utf-8"))
        if not isinstance(file_payload, dict):
            raise ValueError("context_file_path must contain a JSON object.")
        payload.update(file_payload)
    return payload


def _apply_external_context(
    work_item: WorkItem,
    context: dict[str, Any],
    *,
    context_file_path: str,
) -> WorkItem:
    if not context:
        return work_item
    if str(context.get("schema") or "") == "keystone.slack.selected_message_context.v1":
        return _apply_slack_selected_context(
            work_item,
            context,
            context_file_path=context_file_path,
        )
    metadata = {
        **work_item.target.metadata,
        "external_context": _bounded_context_metadata(context),
    }
    if context_file_path:
        metadata["external_context_file_path"] = context_file_path
    return work_item.model_copy(
        update={"target": work_item.target.model_copy(update={"metadata": metadata})}
    ).touch()


def _apply_slack_selected_context(
    work_item: WorkItem,
    context: dict[str, Any],
    *,
    context_file_path: str,
) -> WorkItem:
    selected = (
        context.get("selected_message") if isinstance(context.get("selected_message"), dict) else {}
    )
    selected_text = _compact_context_text(selected.get("text"), max_chars=900)
    channel_id = _compact_context_text(context.get("channel_id"), max_chars=80)
    selected_ts = _compact_context_text(context.get("selected_message_ts"), max_chars=80)
    permalink = _compact_context_text(context.get("permalink"), max_chars=500)
    source_id = f"slack:{channel_id}:{selected_ts}".strip(":")
    warnings = _context_string_list(context.get("warnings"), max_items=5, max_chars=320)
    slack_metadata = {
        "schema": str(context.get("schema") or ""),
        "source": str(context.get("source") or "slack_message_action"),
        "team_id": _compact_context_text(context.get("team_id"), max_chars=80),
        "team_domain": _compact_context_text(context.get("team_domain"), max_chars=120),
        "channel_id": channel_id,
        "channel_name": _compact_context_text(context.get("channel_name"), max_chars=120),
        "selected_message_ts": selected_ts,
        "thread_ts": _compact_context_text(context.get("thread_ts"), max_chars=80),
        "permalink": permalink,
        "thread_fetch_status": _compact_context_text(
            context.get("thread_fetch_status"),
            max_chars=40,
        ),
        "warnings": warnings,
        "selected_message": {
            "ts": _compact_context_text(selected.get("ts"), max_chars=80),
            "user_id": _compact_context_text(selected.get("user_id"), max_chars=80),
            "username": _compact_context_text(selected.get("username"), max_chars=120),
            "text": selected_text,
            "permalink": _compact_context_text(
                selected.get("permalink") or permalink,
                max_chars=500,
            ),
        },
        "thread_messages": _context_messages_metadata(context.get("thread_messages")),
    }
    if context_file_path:
        slack_metadata["context_file_path"] = context_file_path
    metadata = {**work_item.target.metadata, "slack_context": slack_metadata}
    sources = list(work_item.sources)
    if source_id and all(source.source_id != source_id for source in sources):
        sources.append(
            WorkItemSourceRef(
                title="Selected Slack message",
                url=permalink,
                source_type="slack_message",
                source_id=source_id,
                supported_claim="Selected Slack context supplied by the user for this run.",
                provider="slack",
                extraction_status=str(context.get("thread_fetch_status") or "not_requested"),
                retrieved_at=utc_now_iso(),
                key_facts=[selected_text] if selected_text else [],
            )
        )
    audit_notes = list(work_item.audit_notes)
    for warning in warnings:
        note = f"Slack context warning: {warning}"
        if note not in audit_notes:
            audit_notes.append(note)
    return work_item.model_copy(
        update={
            "target": work_item.target.model_copy(update={"metadata": metadata}),
            "sources": sources,
            "audit_notes": audit_notes,
        }
    ).touch()


def _external_context_event_payload(
    context: dict[str, Any],
    *,
    context_file_path: str,
) -> dict[str, Any]:
    if not context:
        return {}
    payload = {
        "schema": str(context.get("schema") or ""),
        "source": str(context.get("source") or ""),
    }
    if context_file_path:
        payload["context_file_path"] = context_file_path
    if str(context.get("schema") or "") == "keystone.slack.selected_message_context.v1":
        payload.update(
            {
                "channel_id": _compact_context_text(context.get("channel_id"), max_chars=80),
                "selected_message_ts": _compact_context_text(
                    context.get("selected_message_ts"),
                    max_chars=80,
                ),
                "thread_ts": _compact_context_text(context.get("thread_ts"), max_chars=80),
                "permalink": _compact_context_text(context.get("permalink"), max_chars=500),
                "thread_fetch_status": _compact_context_text(
                    context.get("thread_fetch_status"),
                    max_chars=40,
                ),
                "warnings": _context_string_list(
                    context.get("warnings"),
                    max_items=5,
                    max_chars=320,
                ),
            }
        )
    return {key: value for key, value in payload.items() if value not in ("", [], {})}


def _bounded_context_metadata(context: dict[str, Any]) -> dict[str, Any]:
    allowed = {}
    for key in ("schema", "source", "permalink", "warnings"):
        if key in context:
            allowed[key] = context[key]
    return allowed


def _context_messages_metadata(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    messages: list[dict[str, str]] = []
    for item in value[:20]:
        if not isinstance(item, dict):
            continue
        messages.append(
            {
                "ts": _compact_context_text(item.get("ts"), max_chars=80),
                "user_id": _compact_context_text(item.get("user_id"), max_chars=80),
                "username": _compact_context_text(item.get("username"), max_chars=120),
                "text": _compact_context_text(item.get("text"), max_chars=900),
                "permalink": _compact_context_text(item.get("permalink"), max_chars=500),
            }
        )
    return messages


def _context_string_list(value: Any, *, max_items: int, max_chars: int) -> list[str]:
    if value is None:
        return []
    raw_values = value if isinstance(value, list) else [value]
    return [
        text
        for text in (
            _compact_context_text(item, max_chars=max_chars) for item in raw_values[:max_items]
        )
        if text
    ]


def _compact_context_text(value: Any, *, max_chars: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_chars:
        return text
    return f"{text[: max_chars - 3].rstrip()}..."


def _effective_max_results(request: WorkflowRunRequest) -> int:
    count = request.max_results
    plan = request.manual_request_plan if isinstance(request.manual_request_plan, dict) else {}
    desired = plan.get("desired_count")
    if isinstance(desired, int):
        count = max(count, desired)
    return max(1, min(20, count))


def _advance_chief_of_staff(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
    sdk_session: Any | None = None,
) -> WorkflowRunResult:
    request_text = request.request_text.strip() or work_item.request_text
    sdk_input = {
        "request": request_text,
        "work_item": {
            "id": work_item.id,
            "route": WorkItemRoute.CHIEF_OF_STAFF.value,
            "target": work_item.target.model_dump(mode="json"),
            "sources": [source.model_dump(mode="json") for source in work_item.sources[:8]],
        },
        "slack_context": work_item.target.metadata.get("slack_context", {}),
        "side_effect_policy": (
            "read-only interpretation; no Slack post, Gmail send, calendar write, "
            "Airtable write, or repo write without explicit approval gates"
        ),
    }
    if request.live_sdk:
        typed_result = run_chief_of_staff_sdk(
            sdk_input,
            live=True,
            session=sdk_session,
            force_sdk_interpretation=True,
        )
        output = typed_result.output
        mode_note = "Chief of Staff live SDK interpretation executed for this WorkItem."
    else:
        output = plan_chief_of_staff_request(request_text, database_url=request.database_url)
        mode_note = "Chief of Staff deterministic dry-run planner executed for this WorkItem."

    artifact_id = ""
    output_payload = output.model_dump(mode="json")
    if store is not None:
        artifact_id = str(
            store.save_agent_run(
                agent_name=WorkItemRoute.CHIEF_OF_STAFF.value,
                input_payload=sdk_input,
                input_summary=request_text[:240],
                output=output_payload,
                model="sdk-live" if request.live_sdk else "fixture",
                dry_run=not request.live_sdk,
                status="success",
            )
        )
    artifact = WorkItemArtifactRef(
        artifact_type="chief_of_staff_plan",
        artifact_id=artifact_id or f"unsaved:{work_item.id}:chief_of_staff_plan",
        source_agent=WorkItemRoute.CHIEF_OF_STAFF.value,
        approval_state=ApprovalState.PENDING.value,
        title="Chief of Staff plan",
        summary=output.summary[:240],
        metadata={
            "workflow_type": output.recommended_route.workflow_type,
            "target_channel": output.recommended_route.target_channel,
            "slack_post_allowed": output.slack_post_allowed,
            "send_enabled": output.send_enabled,
        },
    )
    updated = attach_artifact(
        work_item.model_copy(
            update={
                "last_agent": WorkItemRoute.CHIEF_OF_STAFF.value,
                "status": WorkItemStatus.DONE,
                "confidence": max(work_item.confidence, 0.7),
                "audit_notes": [*work_item.audit_notes, mode_note, *output.audit_notes],
                "next_action": WorkItemNextAction(
                    action="review_chief_of_staff_plan",
                    agent=WorkItemRoute.CHIEF_OF_STAFF,
                    description=(
                        "Review the Chief of Staff plan before any live internal write or post."
                    ),
                    requires_approval=bool(output.approval_required),
                ),
            }
        ).touch(),
        artifact,
    )
    _persist_artifact_and_event(
        updated,
        artifact,
        summary="Attached Chief of Staff plan.",
        store=store,
    )
    return WorkflowRunResult(
        work_item=updated,
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=updated.status,
        advanced=True,
        artifact_refs=[artifact],
        next_action=updated.next_action,
        human_summary=output.summary,
        audit_notes=[mode_note, *output.audit_notes],
    )


def _advance_research(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
    sdk_session: Any | None = None,
) -> WorkflowRunResult:
    request_text = request.request_text.strip()
    selected_opportunity = selected_artifacts(work_item, "opportunity")
    target = ""
    if selected_opportunity and _should_research_selected_opportunity(request_text):
        target = selected_opportunity[0].title
    target = (
        target
        or _manual_primary_target(work_item)
        or work_item.target.name
        or normalize_target_text(
            request.request_text or work_item.request_text,
            WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        )
    )
    work_item = work_item.model_copy(
        update={
            "target": work_item.target.model_copy(update={"name": target}),
            "last_agent": WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        }
    )
    ready = research_ready(work_item)
    if not ready.ready:
        return _blocked_result(work_item, ready.blockers, ready.next_action, store=store)

    if _should_run_zotero_article_brief(work_item, request.request_text):
        return _advance_zotero_article_research(
            work_item,
            request=request,
            store=store,
            sdk_session=sdk_session,
        )

    if _should_run_zotero_collection_brief(work_item, request.request_text):
        return _advance_zotero_collection_research(work_item, request=request, store=store)

    metadata: dict[str, object] = {}
    if request.live_search:
        profile, metadata = retrieve_company_profile_live(
            company=target,
            max_results=_effective_max_results(request),
        )
        audit_notes = ["Live company retrieval executed.", *metadata.get("debug_notes", [])]
    else:
        profile = research_company_fixture(company_name=target)
        audit_notes = ["Fixture company research executed; no live APIs were called."]
    retrieval_memory_id = _persist_retrieval_tool_memory(
        metadata,
        object_id=f"work_item_company_research:{target}",
        store=store,
    )
    if retrieval_memory_id is not None:
        audit_notes.append(f"Retrieval tool performance memory saved: {retrieval_memory_id}.")

    source_refs = [
        _work_item_source_ref_from_company_source(source) for source in profile.sources[:12]
    ]
    contact_enrichment = build_contact_enrichment_artifact(
        company_name=profile.name,
        sources=list(profile.sources),
    )
    artifact_id = ""
    if store is not None:
        artifact_id = str(store.save_company(profile))
    artifact = WorkItemArtifactRef(
        artifact_type="company_profile",
        artifact_id=artifact_id or f"unsaved:{profile.name}",
        source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        approval_state="approved_for_research",
        title=profile.name,
        summary=(profile.fit_summary or profile.description)[:240],
        metadata={
            "consulting_fit_score": profile.consulting_fit_score,
            "confidence_score": profile.confidence_score,
            "source_refs": [source.model_dump(mode="json") for source in profile.sources[:8]],
            "retrieval_diagnostics": metadata.get("retrieval_diagnostics"),
        },
    )
    contact_artifact_id = ""
    if store is not None:
        contact_artifact_id = str(
            store.save_agent_run(
                agent_name=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                input_payload={"company_name": profile.name, "artifact": "contact_enrichment"},
                input_summary=f"Contact enrichment candidates for {profile.name}",
                output=contact_enrichment.model_dump(mode="json"),
                dry_run=True,
                status="success",
            )
        )
    contact_artifact = WorkItemArtifactRef(
        artifact_type="contact_candidates",
        artifact_id=contact_artifact_id or f"unsaved:{profile.name}:contacts",
        source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        approval_state="needs_confirmation",
        title=f"Contact candidates for {profile.name}",
        summary=(
            f"{len(contact_enrichment.candidates)} source-backed candidate(s); "
            + "; ".join(contact_enrichment.missing[:2])
        )[:240],
        metadata=contact_enrichment.model_dump(mode="json"),
    )
    work_item = attach_artifact(
        attach_artifact(
            work_item.model_copy(update={"sources": [*work_item.sources, *source_refs]}),
            artifact,
        ),
        contact_artifact,
    )
    work_item = work_item.model_copy(
        update={
            "confidence": float(profile.confidence_score or 0),
            "audit_notes": [*work_item.audit_notes, *audit_notes],
            "next_action": WorkItemNextAction(
                action="review_company_profile",
                agent=WorkItemRoute.OPPORTUNITY_SCOUT,
                description=(
                    "Review the source-backed profile, then scout matching opportunities if useful."
                ),
                command_hint=f"keystone work-items advance {work_item.id}",
            ),
        }
    )
    work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
    _persist_artifact_and_event(
        work_item,
        artifact,
        summary=f"Attached company profile for {profile.name}.",
        store=store,
    )
    _persist_artifact_and_event(
        work_item,
        contact_artifact,
        summary=f"Attached contact enrichment candidates for {profile.name}.",
        store=store,
    )
    return WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=work_item.status,
        advanced=True,
        artifact_refs=[artifact, contact_artifact],
        next_action=work_item.next_action,
        human_summary=(
            "Business Research Analyst attached a source-backed company profile "
            f"for {profile.name}. Contact enrichment found "
            f"{len(contact_enrichment.candidates)} candidate channel(s)."
        ),
        audit_notes=audit_notes,
    )


def _advance_zotero_collection_research(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
) -> WorkflowRunResult:
    hint = (
        extract_zotero_collection_hint(request.request_text)
        or extract_zotero_collection_hint(work_item.target.name)
        or work_item.target.name
    )
    try:
        brief, paragraphs = build_zotero_collection_research_brief(
            hint,
            research_goal=request.request_text or work_item.request_text,
        )
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        blocker = WorkItemBlocker(
            code="zotero_collection_resolution_failed",
            message=(
                "Business Research Analyst could not resolve the requested Zotero "
                f"collection from local cache: {exc}"
            ),
        )
        return _blocked_result(
            work_item,
            (blocker,),
            WorkItemNextAction(
                action="provide_zotero_collection",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description=(
                    "Provide a unique Zotero collection prefix such as `LH 01`, "
                    "or refresh the Zotero import cache."
                ),
            ),
            store=store,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            audit_notes=["Zotero collection research brief blocked before artifact creation."],
        )
    source_refs = [
        _work_item_source_ref_from_research_source(
            source,
            supported_claim=f"Source included in {brief.target_name}.",
        )
        for source in brief.sources[:12]
    ]
    artifact_id = ""
    if store is not None:
        artifact_id = str(
            store.save_agent_run(
                agent_name=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                input_payload={"request_text": request.request_text, "collection_hint": hint},
                input_summary=f"Zotero collection research brief: {brief.target_name}",
                output=brief.model_dump(mode="json"),
                dry_run=True,
                status="success",
            )
        )
    artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id=artifact_id or f"unsaved:{brief.target_name}",
        source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        approval_state=ApprovalState.APPROVED_FOR_RESEARCH.value,
        title=brief.target_name,
        summary=brief.summary[:240],
        metadata={
            "target_type": brief.target_type,
            "source_count": len(brief.sources),
            "article_summary_count": len(brief.article_summaries),
            "source_refs": [source.model_dump(mode="json") for source in brief.sources[:8]],
        },
    )
    work_item = attach_artifact(
        work_item.model_copy(
            update={
                "sources": [*work_item.sources, *source_refs],
                "target": work_item.target.model_copy(
                    update={"name": brief.target_name, "object_type": brief.target_type}
                ),
            }
        ),
        artifact,
    )
    audit_notes = [
        "Local Zotero collection research brief executed; no live APIs were called.",
        "Business Research Analyst used ResearchBrief output instead of CompanyProfile.",
    ]
    work_item = work_item.model_copy(
        update={
            "confidence": 0.9,
            "audit_notes": [*work_item.audit_notes, *audit_notes],
            "next_action": WorkItemNextAction(
                action="review_research_brief",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description=(
                    "Review the source-backed Zotero research brief and request live/page "
                    "extraction if it will be used externally."
                ),
                command_hint=f"keystone work-items show {work_item.id}",
            ),
        }
    )
    work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
    _persist_artifact_and_event(
        work_item,
        artifact,
        summary=f"Attached Zotero research brief for {brief.target_name}.",
        store=store,
    )
    return WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=work_item.status,
        advanced=True,
        artifact_refs=[artifact],
        next_action=work_item.next_action,
        human_summary=_research_brief_human_summary(brief, paragraphs),
        audit_notes=audit_notes,
    )


def _advance_zotero_article_research(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
    sdk_session: Any | None = None,
) -> WorkflowRunResult:
    query = (
        extract_zotero_article_query(request.request_text)
        or extract_zotero_article_query(work_item.target.name)
        or work_item.target.name
    )
    try:
        brief, paragraphs = build_zotero_article_research_brief(
            query,
            research_goal=request.request_text or work_item.request_text,
        )
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        blocker = WorkItemBlocker(
            code="zotero_article_resolution_failed",
            message=(
                "Business Research Analyst could not resolve the requested Zotero "
                f"article or item from local cache: {exc}"
            ),
        )
        return _blocked_result(
            work_item,
            (blocker,),
            WorkItemNextAction(
                action="provide_zotero_article_query",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description=(
                    "Provide a DOI, PMID, NCT identifier, title fragment, or refresh "
                    "the Zotero import cache."
                ),
            ),
            store=store,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            audit_notes=["Zotero article search blocked before artifact creation."],
        )
    audit_notes = [
        "Local Zotero article search executed.",
        "Business Research Analyst used ResearchBrief output instead of CompanyProfile.",
    ]
    retrieval_metadata: dict[str, object] = {}
    if request.live_sdk:
        try:
            source_context, extraction_notes, retrieval_metadata = build_zotero_live_source_context(
                brief,
                live=request.live_search,
                max_sources=min(max(request.max_results, 1), 4),
            )
            sdk_result = run_business_research_analyst_research_brief_sdk(
                ResearchSDKInput(
                    target_name=brief.target_name,
                    target_type=brief.target_type,
                    research_goal=request.request_text or work_item.request_text,
                    source_context=source_context,
                    local_context_source_ids=("zotero_import_cache",),
                ),
                live=True,
                session=sdk_session,
            )
        except Exception as exc:
            blocker = WorkItemBlocker(
                code="zotero_article_live_synthesis_failed",
                message=(
                    "Business Research Analyst resolved the Zotero item, but live SDK "
                    f"synthesis failed: {type(exc).__name__}: {exc}"
                ),
            )
            return _blocked_result(
                work_item,
                (blocker,),
                WorkItemNextAction(
                    action="retry_zotero_article_live_synthesis",
                    agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                    description=(
                        "Retry with --live-search --live-sdk after confirming model and "
                        "page-extraction credentials."
                    ),
                ),
                store=store,
                route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                audit_notes=[
                    *audit_notes,
                    "Zotero article live SDK synthesis failed before artifact creation.",
                ],
            )
        brief = _merge_research_brief_sources(sdk_result.output, fallback=brief)
        paragraphs = _article_summary_lines_from_brief(brief) or paragraphs
        audit_notes.extend(
            [
                "Live SDK synthesis executed for the Zotero article research brief.",
                *extraction_notes,
            ]
        )
    elif request.live_search:
        source_context, extraction_notes, retrieval_metadata = build_zotero_live_source_context(
            brief,
            live=True,
            max_sources=min(max(request.max_results, 1), 4),
        )
        if source_context:
            audit_notes.append("Live source extraction executed without SDK synthesis.")
        audit_notes.extend(extraction_notes)
    source_refs = [
        _work_item_source_ref_from_research_source(
            source,
            supported_claim=f"Source matched for {brief.target_name}.",
        )
        for source in brief.sources[:8]
    ]
    artifact_id = ""
    if store is not None:
        artifact_id = str(
            store.save_agent_run(
                agent_name=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
                input_payload={"request_text": request.request_text, "zotero_query": query},
                input_summary=f"Zotero article research brief: {brief.target_name}",
                output=brief.model_dump(mode="json"),
                dry_run=True,
                status="success",
            )
        )
    artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id=artifact_id or f"unsaved:{brief.target_name}",
        source_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
        approval_state=ApprovalState.APPROVED_FOR_RESEARCH.value,
        title=brief.target_name,
        summary=brief.summary[:240],
        metadata={
            "target_type": brief.target_type,
            "source_count": len(brief.sources),
            "article_summary_count": len(brief.article_summaries),
            "retrieval": retrieval_metadata if request.live_search or request.live_sdk else {},
            "source_refs": [source.model_dump(mode="json") for source in brief.sources[:8]],
        },
    )
    work_item = attach_artifact(
        work_item.model_copy(
            update={
                "sources": [*work_item.sources, *source_refs],
                "target": work_item.target.model_copy(
                    update={"name": brief.target_name, "object_type": brief.target_type}
                ),
            }
        ),
        artifact,
    )
    if not request.live_search and not request.live_sdk:
        audit_notes.append("No live APIs were called.")
    work_item = work_item.model_copy(
        update={
            "confidence": 0.85,
            "audit_notes": [*work_item.audit_notes, *audit_notes],
            "next_action": WorkItemNextAction(
                action="review_research_brief",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description=(
                    "Review the source-backed Zotero research brief and request live/page "
                    "extraction if detailed methods or eligibility fields are needed."
                ),
                command_hint=f"keystone work-items show {work_item.id}",
            ),
        }
    )
    work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
    _persist_artifact_and_event(
        work_item,
        artifact,
        summary=f"Attached Zotero article research brief for {brief.target_name}.",
        store=store,
    )
    return WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=work_item.status,
        advanced=True,
        artifact_refs=[artifact],
        next_action=work_item.next_action,
        human_summary=_research_brief_human_summary(brief, paragraphs),
        audit_notes=audit_notes,
    )


def _should_research_selected_opportunity(request_text: str) -> bool:
    normalized = " ".join(str(request_text or "").strip().lower().split())
    if normalized in {"", "continue", "resume"}:
        return True
    return normalized in {
        "run deeper source-backed business research for this workitem.",
        "find a better source-backed contact or destination for this outreach workitem.",
    }


def _advance_opportunity(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
) -> WorkflowRunResult:
    request_text = request.request_text.strip()
    planned_topic = _manual_primary_target(work_item)
    topic_input = (
        work_item.target.name
        if request_text.lower() in {"", "continue", "resume"}
        else planned_topic or request_text or work_item.request_text or work_item.target.name
    )
    topic = normalize_target_text(topic_input, WorkItemRoute.OPPORTUNITY_SCOUT)
    work_item = work_item.model_copy(
        update={
            "target": work_item.target.model_copy(update={"name": topic, "object_type": "topic"}),
            "last_agent": WorkItemRoute.OPPORTUNITY_SCOUT.value,
        }
    )
    ready = opportunity_ready(work_item)
    if not ready.ready:
        return _blocked_result(work_item, ready.blockers, ready.next_action, store=store)

    metadata: dict[str, object] = {}
    if request.live_search:
        search_plan = (
            resolve_opportunity_search_plan(
                topic,
                desired_count=_effective_max_results(request),
                live=bool(request.live_sdk),
            )
            if request.live_sdk
            else None
        )
        scout_result, metadata = run_opportunity_scout_live(
            topic=topic,
            max_results=_effective_max_results(request),
            search_plan=search_plan,
        )
        audit_notes = ["Live opportunity retrieval executed.", *metadata.get("debug_notes", [])]
        if search_plan is not None:
            audit_notes.append("Opportunity Scout used the named-agent live search planning path.")
        retrieval_memory_id = _persist_retrieval_tool_memory(
            metadata,
            object_id=f"work_item_opportunity_scout:{topic}",
            store=store,
        )
        if retrieval_memory_id is not None:
            audit_notes.append(f"Retrieval tool performance memory saved: {retrieval_memory_id}.")
    else:
        scout_result = scout_opportunities_fixture(
            topic=topic,
            max_results=_effective_max_results(request),
        )
        audit_notes = ["Fixture opportunity scout executed; no live APIs were called."]

    saved_ids: list[str] = []
    if store is not None:
        saved_ids = [str(item_id) for item_id in store.save_opportunity_scout_result(scout_result)]
    artifacts: list[WorkItemArtifactRef] = []
    for index, record in enumerate(scout_result.records):
        artifact = WorkItemArtifactRef(
            artifact_type="opportunity",
            artifact_id=saved_ids[index] if index < len(saved_ids) else f"unsaved:{index + 1}",
            source_agent=WorkItemRoute.OPPORTUNITY_SCOUT.value,
            approval_state="pending",
            title=record.company_name,
            summary=record.why_now_signal[:240],
            metadata={
                "priority_score": record.priority_score,
                "opportunity_type": record.opportunity_type,
                "recommended_next_step": record.recommended_next_step,
                "retrieval_diagnostics": metadata.get("retrieval_diagnostics"),
            },
        )
        artifacts.append(artifact)
        work_item = attach_artifact(work_item, artifact)

    if not artifacts:
        blocker = WorkItemBlocker(
            code="no_opportunities_found",
            message="Opportunity Scout did not produce a source-backed opportunity record.",
        )
        return _blocked_result(
            work_item,
            (blocker,),
            WorkItemNextAction(
                action="broaden_opportunity_search",
                agent=WorkItemRoute.OPPORTUNITY_SCOUT,
                description="Broaden search terms or enable live search for a wider scan.",
            ),
            store=store,
            audit_notes=audit_notes,
        )

    work_item = work_item.model_copy(
        update={
            "confidence": max(record.priority_score for record in scout_result.records) / 100,
            "audit_notes": [*work_item.audit_notes, *audit_notes],
            "next_action": WorkItemNextAction(
                action="review_opportunities",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description=(
                    "Review the opportunity records and run company research for any "
                    "priority target."
                ),
                command_hint=f"keystone work-items advance {work_item.id}",
            ),
        }
    )
    work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
    for artifact in artifacts:
        _persist_artifact_and_event(
            work_item,
            artifact,
            summary=f"Attached opportunity for {artifact.title}.",
            store=store,
        )
    return WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        status=work_item.status,
        advanced=True,
        artifact_refs=artifacts,
        next_action=work_item.next_action,
        human_summary=(
            f"Opportunity Scout attached {len(artifacts)} source-backed opportunity record(s)."
        ),
        audit_notes=audit_notes,
    )


def _should_run_zotero_collection_brief(work_item: WorkItem, request_text: str) -> bool:
    if work_item.target.object_type == "zotero_collection":
        return True
    text = " ".join(
        part
        for part in (request_text, work_item.request_text, work_item.target.name)
        if str(part or "").strip()
    )
    return looks_like_zotero_collection_request(text)


def _should_run_zotero_article_brief(work_item: WorkItem, request_text: str) -> bool:
    if work_item.target.object_type == "zotero_article":
        return True
    text = " ".join(
        part
        for part in (request_text, work_item.request_text, work_item.target.name)
        if str(part or "").strip()
    )
    return looks_like_zotero_article_request(text)


def _work_item_source_ref_from_company_source(source) -> WorkItemSourceRef:
    quality = getattr(source, "source_quality", None)
    quality_label = ""
    if quality is not None:
        quality_label = str(
            getattr(quality, "quality_label", "")
            or getattr(quality, "tier", "")
            or getattr(quality, "score", "")
        )
    claims = list(getattr(source, "supported_claims", []) or [])
    source_id = str(getattr(source, "source_id", "") or "")
    return WorkItemSourceRef(
        title=str(getattr(source, "title", "") or ""),
        url=str(getattr(source, "url", "") or ""),
        source_type=str(getattr(source, "source_type", "") or ""),
        source_id=source_id,
        supported_claim=claims[0] if claims else "",
        provider=_provider_from_source_id_or_type(
            source_id,
            str(getattr(source, "source_type", "") or ""),
        ),
        extraction_status="extracted" if claims else "",
        source_quality=quality_label,
        retrieved_at=utc_now_iso(),
        key_facts=claims[:5],
        zotero_key=_zotero_item_key(source_id),
    )


def _work_item_source_ref_from_research_source(
    source,
    *,
    supported_claim: str,
) -> WorkItemSourceRef:
    source_id = str(getattr(source, "source_id", "") or "")
    source_type = str(getattr(source, "source_type", "") or "")
    return WorkItemSourceRef(
        title=str(getattr(source, "title", "") or ""),
        url=str(getattr(source, "url", "") or ""),
        source_type=source_type,
        source_id=source_id,
        supported_claim=supported_claim,
        provider=_provider_from_source_id_or_type(source_id, source_type),
        extraction_status="source_linked",
        retrieved_at=utc_now_iso(),
        zotero_key=_zotero_item_key(source_id),
    )


def _provider_from_source_id_or_type(source_id: str, source_type: str) -> str:
    if source_id.startswith("zotero:item:"):
        return "zotero"
    if ":" in source_id:
        return source_id.split(":", maxsplit=1)[0]
    if source_type.startswith("local_zotero"):
        return "zotero"
    return source_type


def _research_brief_human_summary(brief: ResearchBrief, paragraphs: list[str]) -> str:
    lines = [
        "Business Research Analyst attached a source-backed Zotero research brief.",
        "",
        "*Summary:*",
        brief.summary,
    ]
    if brief.article_summaries:
        article = brief.article_summaries[0]
        lines.extend(
            [
                "",
                "*Requested details:*",
                (
                    "- Methods/design: "
                    f"{article.methods_or_design or 'Not available from local metadata.'}"
                ),
                ("- Inclusion/exclusion: " + _eligibility_summary_from_brief(brief)),
                f"- Relevance: {article.relevance_to_goal or 'Not available from local metadata.'}",
            ]
        )
        if article.limitations:
            lines.append(f"- Limitations: {'; '.join(article.limitations[:3])}")
    if paragraphs:
        lines.extend(["", "*Source summaries:*"])
        for index, paragraph in enumerate(paragraphs[:12]):
            lines.append(_paragraph_with_direct_source_links(paragraph, index, brief))
            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()
    if brief.sources:
        lines.extend(["", "*Sources:*"])
        _append_spaced_lines(lines, _research_source_summary_entries(brief.sources[:6]))
    if brief.unknowns:
        lines.extend(["", "*Missing details:*"])
        lines.extend(f"- {item}" for item in brief.unknowns[:6])
    if brief.next_steps:
        lines.extend(["", "*Next steps:*"])
        lines.extend(f"- {item}" for item in brief.next_steps[:4])
    return "\n".join(lines)


def _append_spaced_lines(lines: list[str], items: list[str]) -> None:
    """Append list entries with blank-line separation for Slack chunk stability."""

    for item in items:
        lines.append(item)
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()


def _research_source_summary_entries(sources) -> list[str]:
    entries: list[str] = []
    for source in sources:
        title = _single_line(source.title)
        url = _single_line(source.url)
        if not title and not url:
            continue
        rows: list[str] = []
        if title:
            rows.append(f"- {title}")
        else:
            rows.append("- Source")
        if url:
            rows.append(f"  Link: {url}")
        source_id = _single_line(source.source_id)
        if source_id:
            rows.append(f"  Source ID: {source_id}")
        zotero_key = _zotero_item_key(source_id)
        if zotero_key:
            rows.append(f"  Zotero key: {zotero_key}")
        entries.append("\n".join(rows))
    return entries


def _single_line(value: str) -> str:
    return " ".join(str(value or "").split())


def _merge_research_brief_sources(
    brief: ResearchBrief,
    *,
    fallback: ResearchBrief,
) -> ResearchBrief:
    source_ids = {source.source_id for source in brief.sources}
    sources = [*brief.sources]
    for source in fallback.sources:
        if source.source_id not in source_ids:
            sources.append(source)
            source_ids.add(source.source_id)
    unknowns = list(dict.fromkeys([*brief.unknowns, *fallback.unknowns]))
    limitations = list(dict.fromkeys([*brief.limitations, *fallback.limitations]))
    return brief.model_copy(
        update={
            "sources": sources,
            "unknowns": unknowns,
            "limitations": limitations,
        }
    )


def _article_summary_lines_from_brief(brief: ResearchBrief) -> list[str]:
    lines: list[str] = []
    source_by_id = {source.source_id: source for source in brief.sources}
    for index, article in enumerate(brief.article_summaries[:8], start=1):
        parts = [article.title.rstrip(".")]
        if article.research_question:
            parts.append(article.research_question.rstrip("."))
        if article.methods_or_design:
            parts.append(f"Methods/design: {article.methods_or_design.rstrip('.')}")
        if article.key_findings:
            parts.append("Key signal: " + article.key_findings[0].rstrip("."))
        if article.limitations:
            parts.append("Limitation: " + article.limitations[0].rstrip("."))
        line = f"{index}. " + ". ".join(part for part in parts if part) + "."
        source_lines = _direct_source_link_lines(
            source_by_id[source_id] for source_id in article.source_ids if source_id in source_by_id
        )
        if source_lines:
            line = "\n".join([line, *source_lines])
        lines.append(line)
    return lines


def _paragraph_with_direct_source_links(
    paragraph: str,
    index: int,
    brief: ResearchBrief,
) -> str:
    source = brief.sources[index] if index < len(brief.sources) else None
    if source is None:
        return paragraph
    source_lines = _direct_source_link_lines((source,))
    missing_lines = [
        line
        for line in source_lines
        if _source_line_value(line) and _source_line_value(line) not in paragraph
    ]
    if not missing_lines:
        return paragraph
    return "\n".join([paragraph, *missing_lines])


def _direct_source_link_lines(sources) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for source in sources:
        if source.url and source.url not in seen:
            lines.append(f"   Source link: {source.url}")
            seen.add(source.url)
        if source.source_id and source.source_id not in seen:
            lines.append(f"   Source ID: {source.source_id}")
            seen.add(source.source_id)
        zotero_key = _zotero_item_key(source.source_id)
        if zotero_key and zotero_key not in seen:
            lines.append(f"   Zotero key: {zotero_key}")
            seen.add(zotero_key)
    return lines


def _source_line_value(line: str) -> str:
    return line.split(":", 1)[1].strip() if ":" in line else line.strip()


def _zotero_item_key(source_id: str) -> str:
    prefix = "zotero:item:"
    if source_id.startswith(prefix):
        return source_id.removeprefix(prefix).strip()
    return ""


def _eligibility_summary_from_brief(brief: ResearchBrief) -> str:
    for unknown in brief.unknowns:
        if any(marker in unknown.lower() for marker in ("inclusion", "exclusion", "eligibility")):
            return unknown
    return "Not available from local metadata."


def _advance_outreach(
    work_item: WorkItem,
    *,
    request: WorkflowRunRequest,
    store: SQLiteStore | None,
) -> WorkflowRunResult:
    if store is None:
        blocker = WorkItemBlocker(
            code="outreach_requires_saved_artifacts",
            message=(
                "Outreach drafting from WorkItems requires saved company/opportunity artifacts."
            ),
        )
        return _blocked_result(
            work_item,
            (blocker,),
            WorkItemNextAction(
                action="save_work_item_context",
                agent=WorkItemRoute.OUTREACH_COMPOSER,
                description="Save the WorkItem and its selected artifacts before drafting.",
            ),
            store=store,
            route=WorkItemRoute.OUTREACH_COMPOSER,
        )
    ready = drafting_ready(work_item)
    if not ready.ready:
        return _blocked_result(
            work_item,
            ready.blockers,
            ready.next_action,
            store=store,
            route=WorkItemRoute.OUTREACH_COMPOSER,
        )

    company_ref = selected_artifacts(work_item, "company_profile")[0]
    try:
        company_profile = store.load_company_profile(int(company_ref.artifact_id))
    except (KeyError, TypeError, ValueError) as exc:
        blocker = WorkItemBlocker(
            code="selected_company_profile_unavailable",
            message=f"Selected company profile could not be loaded: {company_ref.artifact_id}.",
        )
        return _blocked_result(
            work_item,
            (blocker,),
            WorkItemNextAction(
                action="select_valid_company_profile",
                agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description="Select a saved company profile artifact before drafting.",
            ),
            store=store,
            route=WorkItemRoute.OUTREACH_COMPOSER,
            audit_notes=[str(exc)],
        )

    opportunity_record: OutreachOpportunityRecord | None = None
    opportunity_refs = selected_artifacts(work_item, "opportunity")
    if opportunity_refs and opportunity_refs[0].approval_state in {
        ApprovalState.APPROVED_FOR_DRAFTING.value,
        ApprovalState.APPROVED_FOR_EXTERNAL_USE.value,
    }:
        try:
            opportunity_record = _to_outreach_opportunity(
                store.load_opportunity_record(int(opportunity_refs[0].artifact_id))
            )
        except (KeyError, TypeError, ValueError):
            opportunity_record = None

    draft, draft_audit_note = _compose_outreach_draft_for_work_item(
        company_profile=company_profile,
        opportunity_record=opportunity_record,
        request=request,
    )
    draft_id = str(store.save_outreach_draft(draft))
    approval_item = build_approval_queue_item(
        draft,
        context={
            "object_type": "outreach_draft",
            "object_id": draft_id,
            "source_agent": WorkItemRoute.OUTREACH_COMPOSER.value,
            "decision": ApprovalState.PENDING.value,
            "scope": ApprovalScope.EXTERNAL_USE.value,
            "metadata": {
                "work_item_id": work_item.id,
                "company_profile_artifact_id": company_ref.artifact_id,
                "opportunity_artifact_id": opportunity_refs[0].artifact_id
                if opportunity_refs
                else "",
            },
        },
    )
    store.save_approval_item(approval_item)
    artifact = WorkItemArtifactRef(
        artifact_type="outreach_draft",
        artifact_id=draft_id,
        source_agent=WorkItemRoute.OUTREACH_COMPOSER.value,
        approval_state=ApprovalState.PENDING.value,
        title=draft.email_subject,
        summary=draft.personalization_rationale[:240],
        selected=True,
        metadata={"approval_queue_id": approval_item.id, "selected": True},
    )
    work_item = attach_artifact(work_item, artifact)
    work_item = work_item.model_copy(
        update={
            "last_agent": WorkItemRoute.OUTREACH_COMPOSER.value,
            "approval_gates": [
                *work_item.approval_gates,
                WorkItemApprovalGate(
                    scope=ApprovalScope.EXTERNAL_USE.value,
                    state=ApprovalState.PENDING.value,
                    required=True,
                    rationale="Outreach draft requires human approval before external use.",
                    approval_id=approval_item.id,
                ),
            ],
            "next_action": WorkItemNextAction(
                action="review_outreach_draft",
                agent=WorkItemRoute.OUTREACH_COMPOSER,
                description="Review the draft approval item before any external use.",
                requires_approval=True,
            ),
            "audit_notes": [
                *work_item.audit_notes,
                draft_audit_note,
            ],
        }
    )
    work_item = work_item.model_copy(update={"status": derive_case_status(work_item)}).touch()
    _persist_artifact_and_event(
        work_item,
        artifact,
        summary=f"Attached outreach draft for {company_profile.name}.",
        store=store,
    )
    return WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.OUTREACH_COMPOSER,
        status=work_item.status,
        advanced=True,
        artifact_refs=[artifact],
        next_action=work_item.next_action,
        human_summary=(
            f"Outreach Composer created a draft-only approval item for {company_profile.name}; "
            "no external message was sent."
        ),
        audit_notes=[draft_audit_note],
    )


def _compose_outreach_draft_for_work_item(
    *,
    company_profile: Any,
    opportunity_record: OutreachOpportunityRecord | None,
    request: WorkflowRunRequest,
) -> tuple[Any, str]:
    objective = _outreach_goal(request.request_text)
    if not request.live_sdk:
        return (
            compose_outreach_draft_fixture(
                company_profile=company_profile,
                opportunity_record=opportunity_record,
                outreach_goal=objective,
            ),
            "Draft-only outreach artifact created; no send or live side effects occurred.",
        )

    try:
        style_profile = load_style_profile("sample_email_style_profile_anup_approved")
        approved_context = build_approved_outreach_drafting_context(
            company_profile=company_profile,
            opportunity_record=opportunity_record,
            email_style_profile=style_profile,
            objective=objective,
            revision_request=request.request_text,
        )

        def retrieve() -> dict[str, Any]:
            return {
                "company_profile": company_profile,
                "opportunity_record": opportunity_record,
                "email_style_profile": style_profile,
                "approved_context": approved_context,
                "context_policy": "Approved source-backed WorkItem context only.",
            }

        def normalize(context: dict[str, Any]) -> OutreachComposerSDKInput:
            return OutreachComposerSDKInput(
                company_name=company_profile.name,
                recent_signal=opportunity_record.rationale if opportunity_record else "",
                outreach_goal=objective,
                approved_context=(
                    "Approved source-backed WorkItem outreach context:\n"
                    f"{json.dumps(jsonable(context), ensure_ascii=True, sort_keys=True)}"
                ),
                email_style_profile=json.dumps(
                    jsonable(style_profile),
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            )

        outcome = run_retrieved_sdk_synthesis(
            agent=build_outreach_composer_compact_synthesis_agent(),
            output_type=OutreachLLMDraftPayload,
            retrieve=retrieve,
            normalize=normalize,
            input_summary=f"WorkItem outreach SDK synthesis for {company_profile.name}",
            input_audit_payload={
                "company": company_profile.name,
                "sdk_synthesis": True,
                "workflow": "work_item_outreach_composer",
            },
            live=True,
            save=False,
            model_label="sdk-live",
        )
        compact_payload = jsonable(outcome.final_output)
        if not isinstance(compact_payload, dict):
            raise RuntimeError("Outreach SDK synthesis did not return a JSON object.")
        source_ids_used = compact_payload.get("source_ids_used")
        if isinstance(source_ids_used, list) and "keystone_profile" not in source_ids_used:
            compact_payload["source_ids_used"] = [*source_ids_used, "keystone_profile"]
        draft = compose_outreach_draft_llm_constrained(
            approved_context=approved_context,
            llm_draft_payload=compact_payload,
            fallback_to_fixture=False,
        )
        return (
            draft.model_copy(update={"drafting_mode": "llm_constrained"}),
            "Outreach Composer live SDK draft created; no send or live Gmail side effect occurred.",
        )
    except Exception as exc:
        return (
            compose_outreach_draft_fixture(
                company_profile=company_profile,
                opportunity_record=opportunity_record,
                outreach_goal=objective,
            ),
            (
                "Outreach Composer live SDK drafting failed with "
                f"{type(exc).__name__}; deterministic draft-only fallback created."
            ),
        )


def _block_unsupported_route(
    work_item: WorkItem,
    *,
    route: WorkItemRoute,
    store: SQLiteStore | None,
) -> WorkflowRunResult:
    if route == WorkItemRoute.OUTREACH_COMPOSER:
        ready = drafting_ready(work_item)
        return _blocked_result(
            work_item,
            ready.blockers,
            ready.next_action,
            store=store,
            route=route,
        )
    blocker = WorkItemBlocker(
        code="route_not_supported_in_workitem_phase",
        message=(
            "WorkItem Phase II supports Business Research Analyst and Opportunity "
            f"Scout only; {route.value} is not enabled here."
        ),
    )
    return _blocked_result(
        work_item,
        (blocker,),
        WorkItemNextAction(
            action="use_existing_agent_path",
            agent=route,
            description=(
                "Use the existing specialist path until this route is enabled for WorkItems."
            ),
        ),
        store=store,
        route=route,
    )


def _blocked_result(
    work_item: WorkItem,
    blockers: tuple[WorkItemBlocker, ...],
    next_action: WorkItemNextAction | None,
    *,
    store: SQLiteStore | None,
    route: WorkItemRoute | None = None,
    audit_notes: list[str] | None = None,
) -> WorkflowRunResult:
    updated = work_item
    for blocker in blockers:
        updated = add_blocker(updated, blocker)
    updated = set_next_action(updated, next_action)
    updated = updated.model_copy(
        update={
            "status": derive_case_status(updated),
            "audit_notes": [*updated.audit_notes, *(audit_notes or [])],
        }
    ).touch()
    record_event(
        updated,
        event_type="advance_blocked",
        summary=blockers[0].message if blockers else "WorkItem advance blocked.",
        metadata={"blockers": [blocker.model_dump(mode="json") for blocker in blockers]},
        store=store,
    )
    return WorkflowRunResult(
        work_item=updated,
        route=route or updated.current_route,
        status=updated.status,
        advanced=False,
        blockers=list(blockers),
        next_action=next_action,
        human_summary=_blocked_human_summary(route or updated.current_route, blockers),
        audit_notes=audit_notes or [],
    )


def _blocked_human_summary(
    route: WorkItemRoute,
    blockers: tuple[WorkItemBlocker, ...],
) -> str:
    if not blockers:
        return "WorkItem advance blocked."
    requirements = [blocker.message.strip() for blocker in blockers if blocker.message.strip()]
    if not requirements:
        return "WorkItem advance blocked."
    agent_name = {
        WorkItemRoute.BUSINESS_RESEARCH_ANALYST: "Business Research Analyst",
        WorkItemRoute.OPPORTUNITY_SCOUT: "Opportunity Scout",
        WorkItemRoute.OUTREACH_COMPOSER: "Outreach Composer",
        WorkItemRoute.GMAIL_TRIAGE: "Gmail Triage",
        WorkItemRoute.CHIEF_OF_STAFF: "Chief of Staff",
        WorkItemRoute.ORCHESTRATOR: "Orchestrator",
        WorkItemRoute.CLARIFICATION: "Orchestrator",
    }.get(route, route.value)
    return f"{agent_name} did not have the required context to complete synthesis: " + "; ".join(
        dict.fromkeys(requirements)
    )


def _persist_artifact_and_event(
    work_item: WorkItem,
    artifact: WorkItemArtifactRef,
    *,
    summary: str,
    store: SQLiteStore | None,
) -> None:
    if store is None:
        return
    store.save_work_item_artifact(work_item.id, artifact)
    record_event(
        work_item,
        event_type="artifact_attached",
        summary=summary,
        metadata={"artifact": artifact.model_dump(mode="json")},
        store=store,
    )


def _persist_retrieval_tool_memory(
    metadata: dict[str, object],
    *,
    object_id: str,
    store: SQLiteStore | None,
) -> int | None:
    if store is None or not metadata:
        return None
    item = retrieval_tool_performance_memory_item(dict(metadata), object_id=object_id)
    if item is None:
        return None
    return int(store.save_memory_item(item))


def _looks_like_outreach_request(text: str) -> bool:
    lower = text.lower()
    return any(
        marker in lower
        for marker in (
            "draft outreach",
            "write outreach",
            "compose outreach",
            "draft email",
            "write email",
            "linkedin note",
        )
    )


def _to_outreach_opportunity(record: ScoutOpportunityRecord) -> OutreachOpportunityRecord:
    source = record.sources[0] if record.sources else None
    return OutreachOpportunityRecord(
        company_name=record.company_name,
        title=f"{record.opportunity_type} opportunity",
        source=source.url if source else "fixture",
        source_id=source.source_id if source else "fixture:work-item-opportunity",
        notes=record.why_now_signal,
        score=record.priority_score / 100,
        rationale=record.keystone_fit_reason,
        next_step=record.recommended_next_step,
        claims=record.claims,
        unsupported_claims_flagged=record.unsupported_claims_flagged,
        approved_for_outreach=True,
    )


def _outreach_goal(text: str) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned or cleaned.lower() in {"continue", "resume"}:
        return "compare notes on clinical AI evaluation and research operations"
    return cleaned[:180]
