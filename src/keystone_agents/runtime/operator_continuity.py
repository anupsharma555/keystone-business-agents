"""Compact optional continuity from current host scope and canonical task state.

Task references and approved operator guidance share a strict byte budget. Source
text belongs to the separate evidence contract; old answers never become memory.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from keystone_agents.runtime.continuity_policy import resolve_slack_compact_continuity_policy
from keystone_agents.runtime.scoped_memory import memory_capture_fingerprint
from keystone_agents.schemas.memory_scope import MemoryCaptureProvenance, SlackMemoryScope
from keystone_agents.schemas.web_source import WebSourceAccess

MAX_OPTIONAL_CONTEXT_BYTES = 6144
MAX_TASK_CONTEXT_BYTES = 2048
MAX_LINKED_THREAD_CONTEXT_BYTES = 8192


def compact_latest_thread_attempt(
    host_context: Mapping[str, Any], *, linked_work_item_id: str,
) -> dict[str, str]:
    """Admit one earlier host-recorded attempt status, distinct from task status."""
    policy = resolve_slack_compact_continuity_policy(host_context)
    value = host_context.get("latest_thread_attempt")
    if not policy.eligible or not linked_work_item_id or not isinstance(value, Mapping):
        return {}
    if (
        value.get("work_item_id") != linked_work_item_id
        or value.get("channel_id") != policy.channel_id
        or value.get("thread_ts") != policy.thread_ts
        or value.get("status") not in {"failed", "blocked"}
    ):
        return {}
    try:
        if not Decimal(policy.thread_ts) <= Decimal(str(value.get("request_ts"))) < Decimal(
            policy.request_ts
        ):
            return {}
    except InvalidOperation:
        return {}
    result = {key: str(value.get(key) or "") for key in (
        "attempt_run_id", "work_item_id", "request_ts", "status",
    )}
    if not result["attempt_run_id"] or any(len(item) > 160 for item in result.values()):
        return {}
    return result


def compact_linked_thread_context(
    host_context: Mapping[str, Any], *, linked_work_item_id: str,
) -> dict[str, Any]:
    """Bound recent host thread excerpts for intent, never source or write authority."""
    policy = resolve_slack_compact_continuity_policy(host_context)
    if not policy.eligible or not linked_work_item_id:
        return {}
    from keystone_agents.storage.sqlite_store import redact_secrets

    try:
        budget = max(1024, min(32768, int(os.environ.get(
            "KEYSTONE_SLACK_LINKED_THREAD_CONTEXT_MAX_BYTES", MAX_LINKED_THREAD_CONTEXT_BYTES,
        ))))
    except ValueError:
        budget = MAX_LINKED_THREAD_CONTEXT_BYTES
    try:
        start, end = Decimal(policy.thread_ts), Decimal(policy.request_ts)
    except InvalidOperation:
        return {}
    messages = host_context.get("thread_messages")
    timezone = ZoneInfo("America/New_York")

    def sent_at(timestamp: Decimal) -> str:
        try:
            return datetime.fromtimestamp(float(timestamp), timezone).isoformat()
        except (OverflowError, OSError, ValueError):
            return ""

    candidates = []
    for raw in messages if isinstance(messages, list) else []:
        if not isinstance(raw, Mapping) or not isinstance(raw.get("text"), str):
            continue
        try:
            ts = Decimal(str(raw.get("ts") or ""))
            if not start <= ts < end:
                continue
        except InvalidOperation:
            continue
        role = str(raw.get("role") or "").lower()
        role = {"operator": "user", "agent": "assistant"}.get(role, role)
        if role not in {"user", "assistant"}:
            continue
        cleaned = " ".join(str(redact_secrets(raw["text"])).split())
        if cleaned:
            candidates.append((ts, {
                "role": role, "ts": str(ts), "sent_at": sent_at(ts),
                "excerpt": cleaned[:1200],
                "truncated": len(cleaned) > 1200,
            }))
    selected = [entry for _, entry in sorted(candidates, key=lambda item: item[0])][-6:]
    packet: dict[str, Any] = {
        "scope": "reply_interpretation_only; not evidence, approval or permission",
        "thread_started_at": sent_at(start),
        "messages": [],
    }
    if not selected:
        root = host_context.get("thread_root_request")
        if isinstance(root, str) and root.strip():
            root = " ".join(str(redact_secrets(root)).split())
            selected = [{
                "role": "user", "ts": policy.thread_ts, "sent_at": sent_at(start),
                "excerpt": root[:1200], "truncated": len(root) > 1200,
            }]
    # Admit the most recent turns first, then restore conversational ordering.
    for entry in reversed(selected):
        trial = {**packet, "messages": [entry, *packet["messages"]]}
        if serialized_context_bytes(trial) <= budget:
            packet = trial
    return packet if packet["messages"] else {}


def serialized_context_bytes(value: Any) -> int:
    # Match run.serialize_input, including escaped Unicode and normal spacing.
    return len(json.dumps(value, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8"))


def compact_task_context(work_item: Any) -> dict[str, Any]:
    """Project whole canonical handles, never artifact summaries or audit prose."""
    task = {
        "id": work_item.id,
        "kind": work_item.kind.value,
        "status": work_item.status.value,
        "route": work_item.current_route.value,
    }
    candidates: list[tuple[str, Any]] = []
    target = {
        key: value
        for key, value in {
            "url": work_item.target.url,
            "external_id": work_item.target.external_id,
            "object_type": work_item.target.object_type,
        }.items()
        if value
    }
    if target:
        candidates.append(("target", target))
    if work_item.next_action:
        candidates.append(
            (
                "next_action",
                {
                    "action": work_item.next_action.action,
                    "agent": work_item.next_action.agent.value
                    if work_item.next_action.agent
                    else "",
                    "requires_approval": work_item.next_action.requires_approval,
                },
            )
        )
    candidates.append(("blockers", [b.code for b in work_item.blockers if not b.resolved][:3]))
    candidates.append(
        (
            "selected_artifacts",
            [
                {"artifact_id": a.artifact_id, "artifact_type": a.artifact_type}
                for a in work_item.artifact_refs
                if a.selected
            ][-2:],
        )
    )
    # Each source is atomic. An oversized URL or immutable snapshot handle is
    # omitted, never shortened into a different identity.
    sources = []
    for source in work_item.sources[:2]:
        entry = {k: v for k, v in {"source_id": source.source_id, "url": source.url}.items() if v}
        if source.web_source_access:
            entry["web_source_access"] = source.web_source_access.model_dump(
                mode="json",
                exclude_none=True,
                exclude={"limitation"},
            )
        trial = {**task, "sources": [*sources, entry]}
        if entry and serialized_context_bytes(trial) <= MAX_TASK_CONTEXT_BYTES:
            sources.append(entry)
    if sources:
        candidates.append(("sources", sources))
    for key, value in candidates:
        if value and serialized_context_bytes({**task, key: value}) <= MAX_TASK_CONTEXT_BYTES:
            task[key] = value
    return task if serialized_context_bytes(task) <= MAX_TASK_CONTEXT_BYTES else {}


def compact_optional_context(
    work_item_context: Mapping[str, Any] | None,
    operator_evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    packet: dict[str, Any] = {}
    if isinstance(work_item_context, Mapping) and work_item_context:
        # Re-project the transport mapping: accepting a small arbitrary mapping
        # would let old narrative fields masquerade as canonical task state.
        task = {
            key: work_item_context[key]
            for key in ("id", "kind", "status", "route")
            if isinstance(work_item_context.get(key), str)
        }
        for key, fields in (
            ("target", ("url", "external_id", "object_type")),
            ("next_action", ("action", "agent", "requires_approval")),
        ):
            value = work_item_context.get(key)
            if isinstance(value, Mapping):
                task[key] = {
                    field: value[field]
                    for field in fields
                    if isinstance(value.get(field), str | bool)
                }
        for key, fields in (
            ("selected_artifacts", ("artifact_id", "artifact_type")),
            ("sources", ("source_id", "url")),
        ):
            values = work_item_context.get(key)
            if isinstance(values, list):
                task[key] = [
                    {field: value[field] for field in fields if isinstance(value.get(field), str)}
                    for value in values[:2]
                    if isinstance(value, Mapping)
                ]
                if key == "sources":
                    for entry, value in zip(
                        task[key], [v for v in values[:2] if isinstance(v, Mapping)], strict=True
                    ):
                        try:
                            access = WebSourceAccess.model_validate(value.get("web_source_access"))
                        except (TypeError, ValidationError):
                            continue
                        if access.source_id == entry.get(
                            "source_id"
                        ) and access.selected_url == entry.get("url"):
                            entry["web_source_access"] = access.model_dump(
                                mode="json",
                                exclude_none=True,
                                exclude={"limitation"},
                            )
        candidate = {"current_work_item": task} if task.get("id") else {}
        if serialized_context_bytes(candidate) <= MAX_TASK_CONTEXT_BYTES + 24:
            packet = candidate
    context = (operator_evidence or {}).get("model_context")
    facts = context.get("facts") if isinstance(context, Mapping) else None
    admitted: list[dict[str, Any]] = []
    for fact in facts[:4] if isinstance(facts, list) else []:
        if not isinstance(fact, dict):
            continue
        candidate = {**packet, "approved_operator_context": {"facts": [*admitted, fact]}}
        if serialized_context_bytes(candidate) > MAX_OPTIONAL_CONTEXT_BYTES:
            # A root run may create task state AFTER the preflight queried its
            # facts. Preserve those model-seen facts; the typed context pack
            # still carries canonical task state outside this optional packet.
            facts_only = {"approved_operator_context": {"facts": [*admitted, fact]}}
            if serialized_context_bytes(facts_only) <= MAX_OPTIONAL_CONTEXT_BYTES:
                packet.pop("current_work_item", None)
                candidate = facts_only
        if serialized_context_bytes(candidate) <= MAX_OPTIONAL_CONTEXT_BYTES:
            admitted.append(fact)
    if admitted:
        packet["approved_operator_context"] = {"facts": admitted}
    return packet


def capture_request_authorized(raw_request: str, manual_request_plan: Any) -> bool:
    """An affirmative current capture ask AND a typed owner plan are necessary."""
    intent = getattr(manual_request_plan, "intent", "")
    if isinstance(manual_request_plan, Mapping):
        intent = manual_request_plan.get("intent", "")
    if intent != "reference_capture":
        return False
    text = str(raw_request).strip()
    if "\n" in text or re.search(
        r"[\"`“”]|"
        r"\b(?:don't|do not|not to|never|avoid|without)\s+"
        r"(?:(?:ever|actually|also|to)\s+){0,2}(?:save|saving|remember|store|capture)\b",
        text,
        re.I,
    ):
        return False
    return bool(
        re.match(
            r"^(?:@KNI\s+)?(?:(?:CoS|chief of staff)\s+)?(?:please\s+)?"
            r"(?:remember\b|save\b.{0,60}\b(?:memory|preference|reference)\b)",
            text,
            re.I,
        )
    )


def operator_capability_from_state(
    workflow_state: Mapping[str, Any] | None,
    *,
    raw_request: str,
    database_url: str | None,
    capture_authorized: bool = False,
):
    """Bind tools only to validated CURRENT transport identity; no prose scope."""
    from keystone_agents.tools.operator_context_tools import OperatorContextCapability

    policy = resolve_slack_compact_continuity_policy(workflow_state)
    if not policy.eligible or not database_url:
        return None
    scope = SlackMemoryScope(
        team_id=policy.team_id,
        channel_id=policy.channel_id,
        user_id=policy.user_id,
        project_key="keystone-business-agents",
    )
    fingerprint = memory_capture_fingerprint(
        scope,
        thread_ts=policy.thread_ts,
        request_ts=policy.request_ts,
        raw_request=raw_request,
    )
    try:
        provenance = MemoryCaptureProvenance(
            thread_ts=policy.thread_ts,
            request_ts=policy.request_ts,
            request_fingerprint=fingerprint,
        )
    except ValidationError:
        return None
    initial_context = (workflow_state or {}).get("approved_operator_context")
    initial_context = initial_context if isinstance(initial_context, Mapping) else {}
    packet = compact_optional_context(
        (workflow_state or {}).get("current_work_item"), {"model_context": initial_context},
    )
    packet.pop("approved_operator_context", None)
    # Reserve the outer facts key as well as the task packet. Tool envelopes
    # consume some of this budget, so forwarding the facts always fits too.
    remaining = MAX_OPTIONAL_CONTEXT_BYTES - serialized_context_bytes(
        {
            **packet,
            "approved_operator_context": {"facts": []},
        }
    )
    source_refs = {}
    for index, url in enumerate(re.findall(r"https?://[^\s<>]+", raw_request)):
        source_refs[f"request-url:{index + 1}"] = url
    profile_users = str(os.environ.get("KEYSTONE_SLACK_COMPACT_CONTINUITY_PROFILE_USER_IDS", ""))
    profile_ids = tuple(dict.fromkeys(re.split(r"[,\s]+", profile_users.strip())))
    profile_enabled = bool(
        profile_users.strip() and len(profile_users) <= 2048 and len(profile_ids) <= 16
        and all(re.fullmatch(r"[UW][A-Z0-9]{2,30}", user) for user in profile_ids)
        and policy.user_id in profile_ids
    )
    return OperatorContextCapability(
        scope=scope,
        provenance=provenance,
        database_url=database_url,
        raw_request=raw_request,
        source_refs=source_refs,
        capture_authorized=capture_authorized,
        max_bytes=min(4096, max(0, remaining)),
        initial_facts=initial_context.get("facts"),
        include_approved_profile=profile_enabled,
    )


def revalidated_operator_context_evidence(
    host_context: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
    *,
    raw_request: str,
    database_url: str,
) -> dict[str, Any] | None:
    """Rebind forwarded facts before another model receives their text.

    A compact fact projection intentionally omits private scope metadata. Its
    shape therefore cannot authorize reuse: the capability rechecks exact local
    records against the current destination and validates profile eligibility.
    This does not query new facts or consume a model/tool acquisition slot.
    """
    model_context = evidence.get("model_context") if isinstance(evidence, Mapping) else None
    if not isinstance(model_context, Mapping) or not model_context.get("facts"):
        return None
    try:
        capability = operator_capability_from_state(
            {**host_context, "approved_operator_context": model_context},
            raw_request=raw_request,
            database_url=database_url,
        )
    except (TypeError, ValueError):
        return None
    return dict(capability.evidence) if capability is not None else None
