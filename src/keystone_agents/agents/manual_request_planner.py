"""Manual request planner SDK agent."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from keystone_agents.manual_request import (
    infer_manual_request_plan,
    merge_manual_request_plan,
    normalize_manual_agent,
)
from keystone_agents.model_provider import ModelConfig, get_runtime_agent_model_config
from keystone_agents.run import run_typed_sdk_agent
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
from keystone_agents.sdk import Agent, build_sdk_agent, compose_instructions

_MAX_PLANNER_THREAD_MESSAGES = 8
_MAX_PLANNER_PRIOR_RUNS = 5
_MAX_PLANNER_TRANSCRIPT_CHARS = 6000
_TRANSCRIPT_COMPACTION_MARKER = (
    "\n[... middle of Slack thread omitted; newest context follows ...]\n"
)


@dataclass(frozen=True)
class ManualRequestPlannerInput:
    request_text: str
    requested_agent: str | None = None
    fallback_plan: ManualRequestPlan | None = None
    workflow_context: dict[str, Any] | None = None

    def to_prompt(self) -> str:
        requested = self.requested_agent or "not specified"
        context = self.workflow_context or {}
        marked_lifecycle_attention = ""
        if re.search(
            r"\bKBA_TEST_(?:RECORD|DOC|DRAFT)(?:_[A-Za-z0-9]+)*\b",
            self.request_text,
            re.IGNORECASE,
        ):
            marked_lifecycle_attention = (
                "\nMarked provider lifecycle attention:\n"
                "This request names one guarded KBA_TEST provider object. Interpret "
                "the full requested operation sequence by meaning, not by requiring "
                "canonical verbs. If it requests a lifecycle, restate its stages "
                "explicitly in objective using create, verify, update when requested, "
                "verify again, and delete or trash when requested. Use "
                "intent=business_system_write, record the ordered normalized stages "
                "in provider_operations, and select the single provider owner. Preserve "
                "all no-send, exact-object, and cleanup restrictions. This planning "
                "hint does not approve or execute any provider write.\n"
            )
        return (
            "Plan this manual Keystone agent request before execution.\n\n"
            f"Requested agent mention: {requested}\n"
            f"Operator request: {self.request_text}\n\n"
            f"{marked_lifecycle_attention}"
            "Bounded prior thread/work-item context (reference evidence only):\n"
            f"{json.dumps(context, ensure_ascii=True, sort_keys=True, indent=2)}\n\n"
            "Context authority rule: the current operator request is authoritative; "
            "the thread-root operator request may resolve references; historical agent "
            "outputs may be wrong and must never select an owner by themselves.\n\n"
            "Interpret the request independently from local keyword or phrase "
            "classifiers. A local fallback is used only if this model call is "
            "unavailable and is intentionally not included as planning evidence.\n\n"
            "Return only a valid ManualRequestPlan."
        )


def build_manual_request_planner_agent(model: str | None = None) -> Agent:
    return build_sdk_agent(
        name="manual_request_planner",
        instructions=compose_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "manual_request_planner.md",
        ),
        output_type=ManualRequestPlan,
        tools=[],
        model=model,
        handoff_description=(
            "Use to turn a manual Slack or CLI request into a safe structured agent-execution plan."
        ),
    )


def resolve_manual_request_plan(
    request_text: str | None,
    *,
    requested_agent: str | None = None,
    live: bool = False,
    run_config: Any | None = None,
    model: str | None = None,
    session: Any | None = None,
    workflow_state: Mapping[str, Any] | None = None,
    cost_callback: Callable[[Any], None] | None = None,
) -> ManualRequestPlan:
    """Return an LLM manual-request plan when requested, otherwise local fallback."""

    fallback = infer_manual_request_plan(request_text, requested_agent=requested_agent)
    workflow_context = _compact_manual_planner_context(workflow_state)
    fallback = _apply_contextual_route_hint(
        fallback,
        request_text=str(request_text or ""),
        workflow_context=workflow_context,
    )
    if not live and run_config is None:
        return fallback
    errors: list[str] = []
    for config in _planner_model_configs(requested_agent=requested_agent, model=model):
        stdout_capture = io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout_capture):
                result = run_typed_sdk_agent(
                    agent=build_manual_request_planner_agent(model=config.model),
                    typed_input=ManualRequestPlannerInput(
                        request_text=str(request_text or ""),
                        requested_agent=requested_agent,
                        fallback_plan=fallback,
                        workflow_context=workflow_context,
                    ),
                    output_type=ManualRequestPlan,
                    run_config=run_config,
                    live=live,
                    config=config,
                    session=session,
                    workflow_name="Keystone manual request planning",
                    tracing_disabled=True,
                )
        except Exception as exc:
            captured = " ".join(stdout_capture.getvalue().split())
            suffix = f" ({captured})" if captured else ""
            errors.append(f"{config.provider}/{config.model}: {exc}{suffix}")
            continue
        if cost_callback is not None:
            cost_callback(result)
        return merge_manual_request_plan(
            fallback,
            result.output.model_copy(update={"source": "llm"}),
            allow_contextual_delegation=bool(workflow_context),
        )
    return fallback.model_copy(
        update={
            "planner_warnings": [
                *fallback.planner_warnings,
                "Live manual planner unavailable; used local fallback: " + " | ".join(errors),
            ]
        }
    )


def _compact_manual_planner_context(
    workflow_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return bounded context that helps resolve thread-local references."""

    if not workflow_state:
        return {}
    state = dict(workflow_state)
    slack_context = state.get("slack_context")
    nested_slack_context = dict(slack_context) if isinstance(slack_context, Mapping) else {}
    transcript = str(
        state.get("slack_thread_transcript")
        or nested_slack_context.get("thread_transcript")
        or ""
    ).strip()
    thread_root = str(
        state.get("slack_thread_root")
        or nested_slack_context.get("thread_root_request")
        or ""
    ).strip()
    recent = state.get("recent_slack_thread") or nested_slack_context.get("thread_messages") or []
    prior_runs = state.get("prior_agent_runs") or nested_slack_context.get("prior_agent_runs") or []
    current_work_item = state.get("current_work_item")
    execution_continuation = state.get("execution_continuation")
    compact: dict[str, Any] = {}
    receipt: dict[str, Any] = {
        "policy": "preserve_root_and_latest",
    }
    if thread_root:
        compact["slack_thread_root"] = thread_root[:2400]
        receipt["thread_root_retained"] = True
    if transcript:
        bounded_transcript = _bounded_planner_thread_transcript(transcript)
        compact["slack_thread_transcript"] = bounded_transcript
        receipt.update(
            {
                "transcript_original_chars": len(transcript),
                "transcript_retained_chars": len(bounded_transcript),
                "transcript_compacted": len(bounded_transcript) < len(transcript),
            }
        )
    if isinstance(recent, list):
        retained_recent = recent[-_MAX_PLANNER_THREAD_MESSAGES:]
        compact["recent_slack_thread"] = [
            {
                key: str(value or "")[:600]
                for key, value in (
                    ("id", item.get("id") or item.get("ts")),
                    ("role", item.get("role")),
                    (
                        "source_agent",
                        item.get("source_agent")
                        or item.get("user_id")
                        or item.get("username")
                        or item.get("user"),
                    ),
                    ("summary", item.get("summary") or item.get("text")),
                )
                if str(value or "").strip()
            }
            for item in retained_recent
            if isinstance(item, Mapping)
        ]
        receipt.update(
            {
                "thread_messages_original": len(recent),
                "thread_messages_retained": len(retained_recent),
                "thread_messages_dropped_oldest": max(
                    0,
                    len(recent) - len(retained_recent),
                ),
            }
        )
    if isinstance(prior_runs, list):
        retained_prior_runs = prior_runs[-_MAX_PLANNER_PRIOR_RUNS:]
        compact["prior_agent_runs"] = [
            {
                key: str(item.get(key) or "")[:600]
                for key in ("id", "route", "status", "object_id", "title", "summary")
                if str(item.get(key) or "").strip()
            }
            for item in retained_prior_runs
            if isinstance(item, Mapping)
        ]
        receipt.update(
            {
                "prior_runs_original": len(prior_runs),
                "prior_runs_retained": len(retained_prior_runs),
                "prior_runs_dropped_oldest": max(
                    0,
                    len(prior_runs) - len(retained_prior_runs),
                ),
            }
        )
    if nested_slack_context:
        compact["slack_context"] = {
            key: str(nested_slack_context.get(key) or "")[:300]
            for key in ("channel_id", "channel_name", "selected_message_ts", "thread_ts")
            if str(nested_slack_context.get(key) or "").strip()
        }
    if isinstance(current_work_item, Mapping):
        compact_work_item = _compact_current_work_item(current_work_item)
        if compact_work_item:
            compact["current_work_item"] = compact_work_item
            target = compact_work_item.get("target")
            selected = compact_work_item.get("selected_artifacts")
            receipt.update(
                {
                    "work_item_identity_expanded": bool(target or selected),
                    "selected_artifacts_retained": (
                        len(selected) if isinstance(selected, list) else 0
                    ),
                }
            )
    if isinstance(execution_continuation, Mapping):
        compact_continuation = {
            key: str(execution_continuation.get(key) or "")[:limit]
            for key, limit in (
                ("work_item_id", 100),
                ("provider_affinity", 100),
                ("prior_request", 2400),
            )
            if str(execution_continuation.get(key) or "").strip()
        }
        if compact_continuation:
            compact["execution_continuation"] = compact_continuation
    if len(receipt) > 1:
        compact["context_compaction"] = receipt
    return {key: value for key, value in compact.items() if value}


def _bounded_planner_thread_transcript(text: str) -> str:
    """Keep the thread root and newest context when the transcript exceeds its cap."""

    normalized = str(text or "").strip()
    if len(normalized) <= _MAX_PLANNER_TRANSCRIPT_CHARS:
        return normalized
    marker_chars = len(_TRANSCRIPT_COMPACTION_MARKER)
    available = _MAX_PLANNER_TRANSCRIPT_CHARS - marker_chars
    head_chars = min(2400, available // 2)
    tail_chars = available - head_chars
    return (
        normalized[:head_chars].rstrip()
        + _TRANSCRIPT_COMPACTION_MARKER
        + normalized[-tail_chars:].lstrip()
    )


def _compact_current_work_item(value: Mapping[str, Any]) -> dict[str, Any]:
    """Retain only canonical identity and next-action fields from one WorkItem."""

    compact: dict[str, Any] = {
        key: str(value.get(key) or "")[:1200]
        for key in (
            "id",
            "kind",
            "status",
            "route",
            "title",
            "prior_request",
        )
        if str(value.get(key) or "").strip()
    }
    target = value.get("target")
    if isinstance(target, Mapping):
        compact_target = {
            key: str(target.get(key) or "")[:240]
            for key in ("name", "object_type", "external_id")
            if str(target.get(key) or "").strip()
        }
        if compact_target:
            compact["target"] = compact_target
    artifacts = value.get("selected_artifacts")
    if isinstance(artifacts, list):
        compact_artifacts = [
            {
                key: str(item.get(key) or "")[:480]
                for key in (
                    "artifact_type",
                    "artifact_id",
                    "source_agent",
                    "title",
                    "summary",
                )
                if str(item.get(key) or "").strip()
            }
            for item in artifacts[-3:]
            if isinstance(item, Mapping)
        ]
        compact_artifacts = [item for item in compact_artifacts if item]
        if compact_artifacts:
            compact["selected_artifacts"] = compact_artifacts
    next_action = value.get("next_action")
    if isinstance(next_action, Mapping):
        compact_next_action = {
            key: str(next_action.get(key) or "")[:480]
            for key in ("action", "agent", "description")
            if str(next_action.get(key) or "").strip()
        }
        if compact_next_action:
            compact["next_action"] = compact_next_action
    return compact


def _apply_contextual_route_hint(
    fallback: ManualRequestPlan,
    *,
    request_text: str,
    workflow_context: Mapping[str, Any],
) -> ManualRequestPlan:
    """Use typed source context as a routing hint, never as write authority."""

    if not workflow_context:
        return fallback
    continuation = workflow_context.get("execution_continuation")
    provider_affinity = (
        str(continuation.get("provider_affinity") or "").strip().lower()
        if isinstance(continuation, Mapping)
        else ""
    )
    affinity_owners = {
        "airtable": "airtable_context_agent",
        "calendar": "chief_of_staff",
        "google_calendar": "chief_of_staff",
        "google calendar": "chief_of_staff",
        "gmail": "gmail_triage",
        "google_workspace": "google_workspace_context_agent",
        "google workspace": "google_workspace_context_agent",
        "zotero": "zotero_context_agent",
    }
    affinity_owner = affinity_owners.get(provider_affinity)
    if not _depends_on_prior_context(request_text) and affinity_owner is None:
        return fallback
    requested = fallback.requested_agent
    if requested not in {None, "orchestrator", "chief_of_staff"}:
        return fallback
    if _explicit_source_owner_from_text(request_text) is not None:
        return fallback
    owner_candidates = _context_owner_candidates_from_workflow_context(
        workflow_context
    )
    if affinity_owner is None and len(owner_candidates) > 1:
        return fallback.model_copy(
            update={
                "target_agent": "clarification",
                "intent": "clarification",
                "task_objective": "clarification",
                "expected_artifact_type": "none",
                "side_effect_policy": "draft_or_read_only",
                "planner_warnings": [
                    *[
                        warning
                        for warning in fallback.planner_warnings
                        if "did not contain enough information" not in warning
                    ],
                    (
                        "Prior context identifies multiple source owners; resolve the "
                        "referenced object before execution."
                    ),
                ],
            }
        )
    target = affinity_owner or (owner_candidates[0] if owner_candidates else None)
    if target is None:
        return fallback
    write_requested = bool(
        re.search(
            r"\b(?:add|append|attach|change|create|delete|edit|label|mark|modify|move|"
            r"remove|rename|replace|revise|save|set|shorten|tag|update|write)\b",
            request_text,
            flags=re.I,
        )
    )
    updates: dict[str, Any] = {
        "target_agent": target,
        "workflow": [],
        "rationale": (
            "Prior thread/work-item context identifies the source-owning specialist; "
            "the live planner must interpret the requested change before tool execution."
        ),
        "planner_warnings": [
            warning
            for warning in fallback.planner_warnings
            if "did not contain enough information" not in warning
        ],
    }
    if target in {
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
    }:
        updates.update(
            {
                "intent": "business_system_write" if write_requested else "context_lookup",
                "provider_system": {
                    "airtable_context_agent": "airtable",
                    "google_workspace_context_agent": "google_workspace",
                    "zotero_context_agent": "zotero",
                }[target],
                "target_type": "business_system_context",
                "task_objective": (
                    "business_system_write" if write_requested else "context_lookup"
                ),
                "expected_artifact_type": (
                    "business_system_write_plan" if write_requested else "context_summary"
                ),
                "side_effect_policy": (
                    "internal_write_approval_required"
                    if write_requested
                    else "draft_or_read_only"
                ),
            }
        )
    elif target == "gmail_triage":
        updates.update(
            {
                "intent": "gmail_triage",
                "provider_system": "gmail",
                "target_type": "gmail_thread",
                "task_objective": "gmail_triage",
                "expected_artifact_type": "gmail_triage_report",
            }
        )
    elif target == "chief_of_staff":
        if provider_affinity in {"calendar", "google_calendar", "google calendar"}:
            updates.update(
                {
                    "intent": (
                        "business_system_write" if write_requested else "context_lookup"
                    ),
                    "provider_system": "google_calendar",
                    "target_type": "business_system_context",
                    "task_objective": (
                        "business_system_write" if write_requested else "context_lookup"
                    ),
                    "expected_artifact_type": (
                        "business_system_write_plan"
                        if write_requested
                        else "context_summary"
                    ),
                    "side_effect_policy": (
                        "internal_write_approval_required"
                        if write_requested
                        else "draft_or_read_only"
                    ),
                }
            )
        else:
            updates.update(
                {
                    "intent": "slack_operations",
                    "provider_system": "slack",
                    "target_type": "slack_channel",
                    "task_objective": "slack_operations",
                    "expected_artifact_type": "slack_ops_summary",
                }
            )
    elif target == "business_research_analyst":
        updates.update(
            {
                "intent": "research_brief",
                "task_objective": "source_research",
                "expected_artifact_type": "source_summary",
            }
        )
    return fallback.model_copy(update=updates)


def _context_owner_candidates_from_workflow_context(
    workflow_context: Mapping[str, Any],
) -> list[str]:
    """Infer an owner only from typed state or operator-authored context."""

    candidates: list[str] = []
    current_work_item = workflow_context.get("current_work_item")
    if isinstance(current_work_item, Mapping):
        route = str(current_work_item.get("route") or "").strip()
        if route in {
            "airtable_context_agent",
            "google_workspace_context_agent",
            "zotero_context_agent",
            "gmail_triage",
            "chief_of_staff",
            "business_research_analyst",
        }:
            candidates.append(route)
        target = current_work_item.get("target")
        if isinstance(target, Mapping):
            candidates.extend(
                _context_owner_candidates(
                    json.dumps(dict(target), ensure_ascii=True, sort_keys=True).lower()
                )
            )

    nested = workflow_context.get("slack_context")
    nested_slack = dict(nested) if isinstance(nested, Mapping) else {}
    recent = (
        workflow_context.get("recent_slack_thread")
        or nested_slack.get("thread_messages")
        or []
    )
    operator_fragments: list[str] = []
    if isinstance(recent, list):
        for item in recent:
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("role") or "").strip().lower()
            if role in {"agent", "assistant", "system", "tool"}:
                continue
            summary = str(item.get("summary") or item.get("text") or "").strip()
            if summary:
                operator_fragments.append(summary)

    thread_root = str(
        workflow_context.get("slack_thread_root")
        or nested_slack.get("thread_root_request")
        or ""
    ).strip()
    if thread_root:
        operator_fragments.insert(0, thread_root)
    continuation = workflow_context.get("execution_continuation")
    if isinstance(continuation, Mapping):
        prior_request = str(continuation.get("prior_request") or "").strip()
        if prior_request:
            operator_fragments.insert(0, prior_request)
    if operator_fragments:
        candidates.extend(
            _context_owner_candidates(
                json.dumps(
                    operator_fragments,
                    ensure_ascii=True,
                    sort_keys=True,
                ).lower()
            )
        )
    elif not recent:
        legacy_transcript = str(
            workflow_context.get("slack_thread_transcript")
            or nested_slack.get("thread_transcript")
            or ""
        ).strip()
        if legacy_transcript:
            candidates.extend(
                _context_owner_candidates(legacy_transcript.lower())
            )
    return list(dict.fromkeys(candidates))


def _depends_on_prior_context(request_text: str) -> bool:
    text = " ".join(str(request_text or "").lower().split())
    if not text:
        return False
    explicit_dependency = bool(
        re.search(
            r"\b(?:above|current|earlier|it|prior|previous|same|that|these|this|those)\b"
            r"|\blink\s+\d+\b|\b(?:prior|previous)\s+results?\b",
            text,
        )
    )
    contextual_action = bool(
        re.search(
            r"\b(?:add|append|attach|change|delete|edit|label|mark|modify|move|remove|"
            r"rename|replace|revise|save|set|shorten|tag|update)\b",
            text,
        )
    )
    return explicit_dependency or contextual_action


def _explicit_source_owner_from_text(text: str) -> str | None:
    """Return a source owner only when the current request names the system."""

    checks = (
        (r"\bairtable\b", "airtable_context_agent"),
        (
            r"\b(?:google\s+docs?|google\s+sheets?|google\s+drive|google\s+workspace)\b",
            "google_workspace_context_agent",
        ),
        (r"\bzotero\b", "zotero_context_agent"),
        (r"\b(?:gmail|gmail\s+draft)\b", "gmail_triage"),
        (r"\bslack\b", "chief_of_staff"),
    )
    for pattern, target in checks:
        if re.search(pattern, str(text or ""), flags=re.I):
            return target
    return None


def _context_owner_from_text(context_text: str) -> str | None:
    """Choose one unambiguous source owner from bounded context."""

    candidates = _context_owner_candidates(context_text)
    return candidates[0] if len(candidates) == 1 else None


def _context_owner_candidates(context_text: str) -> list[str]:
    """Return bounded source-owner candidates without guessing across providers."""

    provider_checks = (
        (r"\bairtable\b", "airtable_context_agent"),
        (
            r"\b(?:google\s+docs?|google\s+sheets?|google\s+drive|google\s+workspace|"
            r"spreadsheet|document_id|sheet_id)\b",
            "google_workspace_context_agent",
        ),
        (r"\bzotero\b", "zotero_context_agent"),
        (r"\b(?:gmail|email\s+draft|gmail\s+draft|draft_id)\b", "gmail_triage"),
    )
    provider_owners = list(
        dict.fromkeys(
            target
            for pattern, target in provider_checks
            if re.search(pattern, context_text, flags=re.I)
        )
    )
    if provider_owners:
        return provider_owners
    if re.search(
        r"\b(?:source\s+result|search\s+result|research\s+result|article|paper)\b",
        context_text,
        flags=re.I,
    ):
        return ["business_research_analyst"]
    if re.search(
        r"\b(?:slack\s+message|slack\s+post|selected\s+message|message_ts)\b",
        context_text,
        flags=re.I,
    ):
        return ["chief_of_staff"]
    return []


def _planner_model_configs(
    *,
    requested_agent: str | None,
    model: str | None = None,
) -> list[ModelConfig]:
    """Return provider-aware planner configs with bounded fallback."""

    policy = os.getenv("KEYSTONE_MANUAL_PLANNER_PROVIDER_POLICY", "target_with_openai_fallback")
    policy = policy.strip().lower() or "target_with_openai_fallback"
    target_agent = normalize_manual_agent(requested_agent) or "orchestrator"
    target_config = get_runtime_agent_model_config(target_agent, model_override=model)
    openai_config = get_runtime_agent_model_config(
        "orchestrator",
        model_override=model,
        provider_override="openai",
    )
    if policy == "openai":
        return [openai_config]
    if policy == "target":
        return [target_config]
    configs = [target_config]
    if (
        target_config.provider,
        target_config.model,
        target_config.base_url,
    ) != (
        openai_config.provider,
        openai_config.model,
        openai_config.base_url,
    ):
        configs.append(openai_config)
    return configs
