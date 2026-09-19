"""Shared helpers for exposing Keystone specialist agents as SDK tools."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Mapping, Sequence
from copy import copy
from hashlib import sha256
from typing import Any, Literal

from pydantic import BaseModel

from keystone_agents.agent_decision_contracts import context_agent_decision_contract
from keystone_agents.agent_registry import SPECIALIST_AGENT_SPECS, AgentSpec
from keystone_agents.agent_tool_policy import (
    GOOGLE_WORKSPACE_READ_TOOLS,
    ToolTier,
    filter_tools_for_tier,
)
from keystone_agents.planning.compatibility import (
    infer_selected_public_url_specialist_plan,
)
from keystone_agents.receipts.mutations import operation_is_mutation
from keystone_agents.receipts.normalization import identity_fingerprint
from keystone_agents.run import run_typed_sdk_agent, sdk_run_failure_metadata
from keystone_agents.runtime.decision_validation import (
    AgentDecisionContract,
    AgentDecisionValidationError,
    SpecialistDecisionEvidence,
    build_decision_repair_evidence_replay,
    decision_repair_prompt,
    decision_validation_telemetry,
    is_provider_read_tool_name,
    validate_specialist_decision,
)
from keystone_agents.runtime.tool_execution import (
    ToolEvidenceGroup,
    ToolExecutionContract,
    ToolExecutionMode,
    sdk_tool_output_payloads,
)
from keystone_agents.schemas.chief_of_staff import (
    ChiefNestedSpecialistResult,
    ChiefNestedSpecialistSourceRef,
    ChiefSpecialistContextEntry,
    ChiefSpecialistToolInput,
)
from keystone_agents.schemas.handoff_types import HandoffTypeContract, build_handoff_type_contract
from keystone_agents.specialist_tool_names import specialist_agent_tool_name

SpecialistToolMode = Literal["read_plan", "approved_write"]

_CHIEF_VALIDATED_CONTEXT_ROUTES = frozenset(
    {
        "gmail_triage",
        "airtable_context_agent",
        "google_workspace_context_agent",
        "zotero_context_agent",
        "rss_context_agent",
        "preprints_context_agent",
    }
)

SPECIALIST_CONTEXT_GUIDANCE: dict[str, str] = {
    "gmail_triage": (
        "Return thread/message context, label recommendations, priority, risk flags, "
        "reply/draft need, approval gates, missing context, and the safest next action. "
        "Include human work context: who should review, what decision is needed, "
        "what follow-up work exists, and which systems need integration. Do not send email."
    ),
    "business_research_analyst": (
        "Return source-backed facts, evidence gaps, decision-useful findings, "
        "buyer/stakeholder context, human validation needs, and handoff-ready "
        "context for opportunity, outreach, or artifact work."
    ),
    "rag_retrieval_specialist": (
        "Return ranked vector-store matches, grounded claims, ambiguity, and corpus "
        "limitations. Use only when a typed plan explicitly requests the RAG specialist."
    ),
    "opportunity_scout": (
        "Return ranked candidates, scoring rationale, deduplication context, "
        "human prioritization needs, follow-up research needs, and handoff-ready "
        "context for research or outreach."
    ),
    "outreach_composer": (
        "Return draft-only copy, approved context used, unsupported-claim checks, "
        "approval gates, human review needs, follow-up timing, and integration "
        "notes for tracking systems. Do not send."
    ),
    "airtable_context_agent": (
        "Return relevant base/table/field context, candidate record identity, mapping "
        "assumptions, blockers, approval needs, human work context, and a reviewable "
        "Airtable write plan for specialist/action-handler execution. Do not execute writes."
    ),
    "google_workspace_context_agent": (
        "Return relevant Drive folder/file, Doc, Sheet, and tab context, target-selection "
        "rationale, blockers, approval needs, human work context, and a reviewable "
        "Workspace write plan for specialist/action-handler execution. Do not execute writes."
    ),
    "zotero_context_agent": (
        "Return relevant Zotero library, collection, item, article, source ID, "
        "evidence-gap, and human work context plus a reviewable artifact plan. "
        "Do not mutate Zotero libraries, collections, notes, tags, attachments, or metadata."
    ),
    "rss_context_agent": (
        "Return historical RSS/#announcements articles, source IDs, recurring themes, "
        "opportunity signals, future-direction guidance, and human work context. "
        "Treat this as read-only historical context, not current source verification."
    ),
    "preprints_context_agent": (
        "Return historical preprint/#knowledge-hub records, source IDs, recurring "
        "psychiatry research themes, opportunity signals, future-direction guidance, "
        "and human work context. Mark preprints as preliminary evidence."
    ),
}


def _tool_name(tool: Any) -> str:
    return str(getattr(tool, "name", getattr(tool, "__name__", "")) or "")


def _safe_setattr(target: Any, name: str, value: Any) -> None:
    try:
        setattr(target, name, value)
    except Exception:
        pass


_NESTED_PARENT_REPLAY_BLOCKS_ATTR = "keystone_nested_parent_replay_blocks"
_NESTED_PARENT_REPLAY_BOUND_ATTR = "keystone_nested_parent_replay_bound"
_NESTED_PARENT_AGENT_ATTR = "keystone_nested_parent_agent"


def bind_validated_nested_context_replay_boundary(parent_agent: Any) -> Any:
    """Bind validated context tools to one replay-aware Chief agent instance.

    A context specialist is a one-shot provider reader within a parent run. Once
    it returns a validated, sanitized envelope, later Chief turns (including a
    validator-driven parent repair) read that envelope from the parent system
    instructions while the child tool remains disabled.
    """

    validated_tools = [
        tool
        for tool in list(getattr(parent_agent, "tools", []) or [])
        if getattr(tool, "nested_execution_contract", "")
        == "validated_child_decision_v1"
    ]
    if not validated_tools:
        return parent_agent
    for tool in validated_tools:
        _safe_setattr(tool, _NESTED_PARENT_AGENT_ATTR, parent_agent)
    if getattr(parent_agent, _NESTED_PARENT_REPLAY_BOUND_ATTR, False):
        return parent_agent

    original_instructions = getattr(parent_agent, "instructions", None)
    replay_blocks: dict[str, str] = {}
    _safe_setattr(parent_agent, _NESTED_PARENT_REPLAY_BLOCKS_ATTR, replay_blocks)

    async def replay_aware_instructions(context: Any, agent: Any) -> str:
        if callable(original_instructions):
            base = original_instructions(context, agent)
            if inspect.isawaitable(base):
                base = await base
        else:
            base = original_instructions
        resolved_base = str(base or "")
        usage_requests = getattr(getattr(context, "usage", None), "requests", None)
        # Within the initial parent SDK run, the ordinary tool output is already
        # present in generated_items. Inject the replay only into a fresh parent
        # attempt (the validator-driven repair), avoiding duplicate prompt weight
        # and latency on the post-tool turn.
        fresh_parent_attempt = not isinstance(usage_requests, int) or usage_requests <= 0
        blocks = list(replay_blocks.values()) if fresh_parent_attempt else []
        return resolved_base + ("" if not blocks else "\n\n" + "\n\n".join(blocks))

    _safe_setattr(replay_aware_instructions, "keystone_static_instructions", original_instructions)
    parent_agent.instructions = replay_aware_instructions
    _safe_setattr(parent_agent, _NESTED_PARENT_REPLAY_BOUND_ATTR, True)
    return parent_agent


def _install_nested_live_read_boundaries(agent: Any, *, live: bool) -> None:
    """Force live mode only on declared read tools that expose a live field."""

    if not live:
        return
    wrapped_tools: list[Any] = []
    for shared_tool in list(getattr(agent, "tools", []) or []):
        # Agent builders can reuse module-level FunctionTool objects. Copy each
        # instance before wrapping so one live nested run cannot change later
        # offline agents in the same worker process.
        tool = copy(shared_tool)
        wrapped_tools.append(tool)
        tool_name = _tool_name(tool)
        if (
            not tool_name
            or operation_is_mutation(tool_name)
            or not is_provider_read_tool_name(tool_name)
            or getattr(tool, "_keystone_nested_live_read_wrapped", False)
        ):
            continue
        schema = getattr(tool, "params_json_schema", None)
        properties = schema.get("properties") if isinstance(schema, Mapping) else None
        if not isinstance(properties, Mapping) or "live" not in properties:
            continue
        original = getattr(tool, "on_invoke_tool", None)
        if not callable(original):
            continue

        async def invoke_live_read(
            context: Any,
            input_json: str,
            *,
            bound_original: Any = original,
            bound_tool_name: str = tool_name,
        ) -> Any:
            try:
                payload = json.loads(input_json)
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = None
            requested_live = payload.get("live") if isinstance(payload, dict) else None
            effective_input = input_json
            if isinstance(payload, dict):
                payload["live"] = True
                effective_input = json.dumps(payload, ensure_ascii=True, sort_keys=True)
            result = bound_original(context, effective_input)
            if inspect.isawaitable(result):
                result = await result
            custom_data = getattr(context, "_custom_data", None)
            custom_data = dict(custom_data) if isinstance(custom_data, Mapping) else {}
            prior = custom_data.get("keystone_nested_live_read_enforcement")
            records = list(prior) if isinstance(prior, list | tuple) else []
            records.append(
                {
                    "schema": "keystone.nested_live_read_enforcement.v1",
                    "origin": "nested_specialist_tool_wrapper",
                    "tool_name": bound_tool_name,
                    "requested_live": requested_live,
                    "effective_live": True,
                    "enforced": requested_live is not True,
                    "read_only": True,
                    "mutation_capability_enabled": False,
                }
            )
            custom_data["keystone_nested_live_read_enforcement"] = records
            context._custom_data = custom_data  # noqa: SLF001 - documented SDK hook
            return result

        tool.on_invoke_tool = invoke_live_read
        _safe_setattr(tool, "_keystone_nested_live_read_wrapped", True)
    agent.tools = wrapped_tools


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(item) for item in value]
    return value


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _context_entries_from_mapping(value: Any) -> list[ChiefSpecialistContextEntry]:
    mapping = _as_mapping(value)
    entries: list[ChiefSpecialistContextEntry] = []
    for key, item in mapping.items():
        if isinstance(item, list | tuple):
            text = ", ".join(str(part) for part in item)
        else:
            text = str(item)
        entries.append(ChiefSpecialistContextEntry(key=str(key), value=text))
    return entries[:20]


def _compact_text(value: Any, *, limit: int = 800) -> str:
    text = " ".join(str(value or "").split()).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 3].rstrip()}..."


def _as_list(value: Any, *, limit: int = 20) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list | tuple | set) else [value]
    cleaned = [_compact_text(item, limit=240) for item in values]
    return [item for item in cleaned if item][:limit]


def _summary_from_output(payload: dict[str, Any], raw_output: Any) -> str:
    for field in ("summary", "synthesis", "detailed_summary", "executive_summary", "rationale"):
        if payload.get(field):
            return _compact_text(payload.get(field))
    if isinstance(raw_output, str):
        return _compact_text(raw_output)
    return _compact_text(json.dumps(_jsonable(payload), ensure_ascii=True, sort_keys=True))


def _spec_for_route(route_name: str) -> AgentSpec | None:
    return next((spec for spec in SPECIALIST_AGENT_SPECS if spec.route_name == route_name), None)


def _chief_nested_type_contract(
    *,
    route_name: str,
    raw_output: Any,
    parsed_output_status: str,
) -> HandoffTypeContract:
    spec = _spec_for_route(route_name)
    source_output_type = spec.output_schema if spec is not None else type(raw_output).__name__
    return build_handoff_type_contract(
        source_agent=route_name,
        target_agent="chief_of_staff",
        source_output_type=source_output_type if raw_output is not None else "",
        target_input_type="keystone_agents.schemas.chief_of_staff.ChiefNestedSpecialistResult",
        target_output_type="keystone_agents.schemas.chief_of_staff.ChiefOfStaffResult",
        payload_mode="adapted" if parsed_output_status != "malformed" else "malformed",
        parsed_output_status=parsed_output_status,  # type: ignore[arg-type]
        compatibility_notes=[
            "Nested specialist output is adapted into a Chief review envelope."
        ],
    )


def _source_refs_from_output(payload: dict[str, Any]) -> list[ChiefNestedSpecialistSourceRef]:
    refs: list[ChiefNestedSpecialistSourceRef] = []
    for value in payload.get("sources") or payload.get("source_refs") or []:
        item = _as_mapping(value)
        source_id = (
            item.get("source_id")
            or item.get("id")
            or item.get("sourceId")
            or item.get("url")
            or item.get("title")
            or ""
        )
        refs.append(
            ChiefNestedSpecialistSourceRef(
                source_id=str(source_id or ""),
                title=str(item.get("title") or item.get("name") or ""),
                url=str(item.get("url") or ""),
                source_type=str(item.get("source_type") or item.get("type") or ""),
                note=str(item.get("note") or item.get("snippet") or ""),
            )
        )
    return refs[:20]


def _source_ids_from_output(
    payload: dict[str, Any],
    refs: Sequence[ChiefNestedSpecialistSourceRef],
) -> list[str]:
    ids: list[str] = []
    for field in ("source_ids", "source_ids_used", "local_context_source_ids"):
        ids.extend(_as_list(payload.get(field), limit=40))
    ids.extend(ref.source_id for ref in refs if ref.source_id)
    return list(dict.fromkeys(ids))[:40]


def build_chief_specialist_tool_input(options: Mapping[str, Any]) -> str:
    """Render typed Chief specialist input into a nested-agent prompt."""

    params = _as_mapping(options.get("params"))
    if not params:
        params = ChiefSpecialistToolInput().model_dump(mode="json")
    canonical_plan = _as_mapping(params.get("canonical_manual_request_plan"))
    structured_context = {
        key: value
        for key, value in params.items()
        if key != "canonical_manual_request_plan"
    }
    summary = str(options.get("summary") or "").strip()
    sections = [
        "You are being called by Chief of Staff as an advisory specialist tool.",
        "Use the structured context below as the bounded task contract.",
        "Do not send, publish, schedule, mutate provider state, or execute nested live writes.",
        "",
        "## Specialist Task",
        _compact_text(params.get("specialist_task") or params.get("raw_operator_request")),
        "",
        "## Raw Operator Request",
        _compact_text(params.get("raw_operator_request")),
    ]
    if canonical_plan:
        sections.extend(
            [
                "",
                "## Canonical Manual Request Plan",
                json.dumps(canonical_plan, ensure_ascii=True, sort_keys=True, indent=2),
                (
                    "Use canonical provider, operation, tool, query, scope, field, "
                    "selection, output, and safety fields as execution authority. "
                    "You may report a non-safety inconsistency within your admitted "
                    "capability, but do not silently rewrite this plan."
                ),
            ]
        )
    sections.extend(
        [
            "",
            "## Structured Chief Context",
            json.dumps(structured_context, ensure_ascii=True, sort_keys=True, indent=2),
        ]
    )
    if summary:
        sections.extend(["", "## Input Schema Summary", summary])
    sections.extend(
        [
            "",
            "Return your normal structured output. Preserve source IDs, blockers, "
            "approval needs, human work context, and advisory-vs-write boundaries.",
        ]
    )
    return "\n".join(sections).strip()


def build_bound_chief_specialist_tool_input(
    options: Mapping[str, Any],
    *,
    raw_operator_request: str = "",
    manual_request_plan: Mapping[str, Any] | BaseModel | None = None,
) -> str:
    """Bind canonical request authority after model tool arguments are validated.

    Chief may refine ``specialist_task`` and the bounded context fields, but it
    must not rewrite the immutable operator ask or erase the reconciled plan
    before the selected specialist sees them.
    """

    params = _as_mapping(options.get("params"))
    immutable_request = str(raw_operator_request or "").strip()
    if immutable_request:
        params["raw_operator_request"] = immutable_request
    canonical_plan = _jsonable(manual_request_plan)
    if isinstance(canonical_plan, Mapping) and canonical_plan:
        params["canonical_manual_request_plan"] = {
            str(key): value for key, value in canonical_plan.items()
        }
    return build_chief_specialist_tool_input(
        {
            **_as_mapping(options),
            "params": params,
        }
    )


def _nested_output_payload(output: Any) -> tuple[dict[str, Any], str]:
    if output is None:
        return {}, "missing"
    if isinstance(output, BaseModel):
        return output.model_dump(mode="json"), "parsed"
    if isinstance(output, Mapping):
        return {str(key): item for key, item in output.items()}, "parsed"
    if isinstance(output, str):
        text = output.strip()
        if not text:
            return {}, "missing"
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError:
            return {"summary": text}, "text"
        if isinstance(decoded, Mapping):
            return {str(key): item for key, item in decoded.items()}, "parsed"
        return {"summary": text}, "text"
    return {"summary": str(output)}, "text"


def extract_nested_specialist_result(
    *,
    run_result: Any,
    route_name: str,
    tool_name: str,
) -> ChiefNestedSpecialistResult:
    """Normalize one nested specialist SDK result into a Chief review envelope."""

    raw_output = getattr(run_result, "final_output", None)
    try:
        payload, status = _nested_output_payload(raw_output)
        refs = _source_refs_from_output(payload)
        blockers = [
            *_as_list(payload.get("blockers"), limit=20),
            *_as_list(payload.get("missing_context"), limit=20),
        ]
        approval_needs = _as_list(payload.get("approval_needs"), limit=20)
        if payload.get("approval_required") is True and not approval_needs:
            approval_needs.append("Specialist output requires approval before action.")
        validation_status: Literal["ok", "needs_review", "blocked"]
        if status in {"missing", "malformed"} or blockers:
            validation_status = "blocked"
        elif approval_needs:
            validation_status = "needs_review"
        else:
            validation_status = "ok"
        type_contract = _chief_nested_type_contract(
            route_name=route_name,
            raw_output=raw_output,
            parsed_output_status=status,
        )
        human_context = _context_entries_from_mapping(payload.get("human_work_context"))
        # Preserve child-authored evidence and qualifications through the same
        # identity-sanitized context lane. Do not manufacture evidence from tools
        # after the child has made its decision.
        for field_name, raw_values in (
            ("relevant_evidence", payload.get("relevant_evidence")),
            ("decision_limitations", _as_mapping(payload.get("decision")).get("limitations")),
        ):
            if not isinstance(raw_values, list | tuple) or not raw_values:
                continue
            values = [" ".join(str(value or "").split()) for value in raw_values]
            retained = values[:10]
            truncated_count = sum(len(value) > 1000 for value in retained)
            human_context.extend(
                ChiefSpecialistContextEntry(
                    key=f"{field_name}_{index + 1}",
                    value=_compact_text(value, limit=1000),
                    note="Child-authored evidence; provider identity sanitization applies.",
                )
                for index, value in enumerate(retained)
            )
            human_context.append(ChiefSpecialistContextEntry(
                key=f"{field_name}_coverage",
                value=json.dumps({
                    "total_entries": len(values), "retained_entries": len(retained),
                    "omitted_entries": len(values) - len(retained),
                    "truncated_entries": truncated_count,
                    "complete": len(values) == len(retained) and not truncated_count,
                }, sort_keys=True),
            ))
        return ChiefNestedSpecialistResult(
            route_name=route_name,
            tool_name=tool_name,
            parsed_output_status=status,  # type: ignore[arg-type]
            output_type=type(raw_output).__name__ if raw_output is not None else "",
            type_contract=type_contract,
            source_output_type=type_contract.source_output_type,
            target_input_type=type_contract.target_input_type,
            payload_mode=type_contract.payload_mode,
            type_compatibility_status=type_contract.compatibility_status,
            summary=_summary_from_output(payload, raw_output),
            source_ids=_source_ids_from_output(payload, refs),
            source_refs=refs,
            blockers=blockers,
            approval_needs=approval_needs,
            human_work_context=human_context,
            validation_status=validation_status,
            diagnostics={
                "advisory_only": True,
                "final_output_present": raw_output is not None,
            },
        )
    except Exception as exc:
        type_contract = _chief_nested_type_contract(
            route_name=route_name,
            raw_output=raw_output,
            parsed_output_status="malformed",
        )
        return ChiefNestedSpecialistResult(
            route_name=route_name,
            tool_name=tool_name,
            parsed_output_status="malformed",
            output_type=type(raw_output).__name__ if raw_output is not None else "",
            type_contract=type_contract,
            source_output_type=type_contract.source_output_type,
            target_input_type=type_contract.target_input_type,
            payload_mode=type_contract.payload_mode,
            type_compatibility_status=type_contract.compatibility_status,
            summary="Nested specialist output could not be normalized.",
            blockers=[f"Nested specialist output normalization failed: {exc}"],
            validation_status="blocked",
            diagnostics={"advisory_only": True, "error_type": type(exc).__name__},
        )


def _nested_provider_candidate_ids(
    route_name: str,
    raw_result: Any,
) -> tuple[str, ...]:
    """Extract exact provider identities from child tool outputs, never model prose."""

    identities: list[str] = []

    def add(value: object) -> None:
        cleaned = " ".join(str(value or "").split())
        if cleaned:
            identities.append(cleaned)

    for entry in sdk_tool_output_payloads(raw_result):
        payload = entry.get("output")
        if not isinstance(payload, Mapping):
            continue
        if route_name == "airtable_context_agent":
            add(payload.get("record_id"))
            add(payload.get("duplicate_record_id"))
            row_groups = ("records", "items", "matching_record_summaries")
            identity_keys = ("record_id", "id")
        elif route_name == "google_workspace_context_agent":
            for key in (
                "document_id",
                "file_id",
                "folder_id",
                "spreadsheet_id",
                "presentation_id",
            ):
                add(payload.get(key))
            row_groups = ("items", "files", "folders", "documents", "spreadsheets")
            identity_keys = (
                "file_id",
                "folder_id",
                "document_id",
                "spreadsheet_id",
                "presentation_id",
                "id",
            )
            file_payload = payload.get("file")
            if isinstance(file_payload, Mapping):
                add(file_payload.get("file_id") or file_payload.get("id"))
        elif route_name == "zotero_context_agent":
            for key in (
                "item_key",
                "selected_item_key",
                "parent_item_key",
                "attachment_item_key",
                "collection_key",
            ):
                add(payload.get(key))
            row_groups = ("items", "collections", "results", "children")
            identity_keys = ("item_key", "collection_key", "key")
        else:
            continue
        for group in row_groups:
            rows = payload.get(group)
            if not isinstance(rows, list | tuple):
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                for key in identity_keys:
                    value = row.get(key)
                    if value:
                        add(value)
                        break
        result_scope = payload.get("result_scope")
        if isinstance(result_scope, Mapping):
            for item_ref in result_scope.get("item_refs") or ():
                add(item_ref)
    return tuple(dict.fromkeys(identities))


def _nested_context_read_contract(route_name: str, agent: Any) -> ToolExecutionContract:
    tool_names = tuple(
        dict.fromkeys(
            _tool_name(tool)
            for tool in list(getattr(agent, "tools", []) or [])
            if _tool_name(tool)
            and (
                _tool_name(tool) in GOOGLE_WORKSPACE_READ_TOOLS
                if route_name == "google_workspace_context_agent"
                else is_provider_read_tool_name(_tool_name(tool))
            )
        )
    )
    if not tool_names:
        raise ValueError(
            f"{route_name} has no bounded provider-read tool for nested execution."
        )
    return ToolExecutionContract.required(
        ToolEvidenceGroup("nested_provider_read", tool_names),
        stage=f"{route_name}_chief_nested_provider_read",
    )


def _nested_tool_free_agent(agent: Any) -> Any:
    repair_agent = copy(agent)
    repair_agent.tools = []
    return repair_agent


def _nested_replay_result(raw_result: Any) -> dict[str, Any]:
    """Project paired SDK tool outputs into the replay helper's public shape."""

    items: list[dict[str, Any]] = []
    for entry in sdk_tool_output_payloads(raw_result):
        tool_name = str(entry.get("tool_name") or "").strip()
        call_id = str(entry.get("call_id") or "").strip()
        if not tool_name or not call_id:
            continue
        items.extend(
            [
                {
                    "type": "function_call",
                    "call_id": call_id,
                    "name": tool_name,
                    "status": "completed",
                },
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": entry.get("output"),
                    "status": "completed",
                },
            ]
        )
    return {"new_items": items}


def _nested_decision_envelope(
    *,
    route_name: str,
    contract: AgentDecisionContract,
    evidence: SpecialistDecisionEvidence,
    outcome: Any,
    attempts: Sequence[Mapping[str, Any]],
    repair_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    terminal = dict(attempts[-1]) if attempts else {}
    events = [
        event
        for attempt in attempts
        for event in list(attempt.get("events") or [])
        if isinstance(event, Mapping)
    ]
    return {
        "schema": "keystone.agent_decision_run.v1",
        "route": route_name,
        "decision_owner": str(terminal.get("decision_owner") or "specialist_agent"),
        "decision_stage": contract.decision_stage,
        "attempt_count": len(attempts),
        "repair_attempted": len(attempts) > 1,
        "attempts": [dict(value) for value in attempts],
        "candidate_ids": list(evidence.candidate_ids),
        "selected_candidate_ids": list(terminal.get("selected_candidate_ids") or []),
        "excluded_candidate_ids": list(terminal.get("excluded_candidate_ids") or []),
        "reasoning": str(terminal.get("reasoning") or ""),
        "limitations": list(terminal.get("limitations") or []),
        "validator_outcome": outcome.model_dump(mode="json"),
        "events": events,
        **({"repair_evidence": dict(repair_evidence)} if repair_evidence else {}),
    }


def _nested_combine_receipts(results: Sequence[Any]) -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        for receipt in list(getattr(result, "tool_receipts", []) or []):
            if not isinstance(receipt, Mapping):
                continue
            payload = dict(receipt)
            fingerprint = json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                default=str,
                separators=(",", ":"),
            )
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            receipts.append(payload)
    return receipts


def _nested_usage(results: Sequence[Any]) -> dict[str, Any]:
    rows = [dict(getattr(result, "usage", {}) or {}) for result in results]
    available = [row for row in rows if row.get("available") is True]

    def total(field: str) -> int | None:
        values = [row.get(field) for row in available]
        if len(values) != len(rows) or any(
            not isinstance(value, int) or isinstance(value, bool) for value in values
        ):
            return None
        return sum(values)

    return {
        "available": bool(available),
        "complete": len(available) == len(rows),
        "attempt_count": len(rows),
        "requests": total("requests"),
        "input_tokens": total("input_tokens"),
        "output_tokens": total("output_tokens"),
        "total_tokens": total("total_tokens"),
        "cached_input_tokens": total("cached_input_tokens"),
        "reasoning_output_tokens": total("reasoning_output_tokens"),
    }


def _nested_internal_execution_record(
    *,
    route_name: str,
    tool_name: str,
    tool_call_id: str,
    results: Sequence[Any],
    decision_ownership: Mapping[str, Any],
    terminal_status: str,
) -> dict[str, Any]:
    final_output = getattr(results[-1], "final_output", None) if results else None
    output_payload = _as_mapping(final_output)
    candidate_ids = list(decision_ownership.get("candidate_ids") or [])
    selected_ids = list(decision_ownership.get("selected_candidate_ids") or [])
    request_caches = [
        dict(getattr(result, "request_cache", {}) or {}) for result in results
    ]
    return {
        "schema": "keystone.chief_nested_specialist_execution.v1",
        "route_name": route_name,
        "tool_name": tool_name,
        "tool_call_id": tool_call_id,
        "execution_state": "executed_nested_specialist",
        "advisory_only": True,
        "recommendation_only": False,
        "provider_write_executed": False,
        "nested_execution_mode": str(
            decision_ownership.get("nested_execution_mode") or "offline_read_only"
        ),
        "raw_structured_decision": _jsonable(output_payload.get("decision")),
        "decision_ownership": dict(decision_ownership),
        "candidate_universe": candidate_ids,
        "candidate_identity_fingerprints": [
            identity_fingerprint(value) for value in candidate_ids
        ],
        "selected_identity_fingerprints": [
            identity_fingerprint(value) for value in selected_ids
        ],
        "tool_origins": [
            dict(cache.get("tool_execution") or {}) for cache in request_caches
        ],
        "tool_execution_attempts": [
            dict(value)
            for cache in request_caches
            for value in list(cache.get("tool_execution_attempts") or [])
            if isinstance(value, Mapping)
        ],
        "nested_live_read_enforcements": [
            dict(value)
            for cache in request_caches
            for value in list(cache.get("nested_live_read_enforcements") or [])
            if isinstance(value, Mapping)
        ],
        "receipts": _nested_combine_receipts(results),
        "usage": _nested_usage(results),
        "repair_attempts": max(
            max(0, len(results) - 1),
            max(0, int(decision_ownership.get("attempt_count") or 1) - 1),
        ),
        "limitations": list(
            _as_mapping(output_payload.get("decision")).get("limitations") or []
        ),
        "handoff": {
            "state": "executed",
            "consumption_status": "returned_to_chief_model",
            "terminal_status": terminal_status,
        },
    }


def _run_validated_nested_context_child(
    *,
    route_name: str,
    agent: Any,
    output_type: Any,
    prompt: str,
    run_config: Any,
    live: bool,
    max_turns: int,
    manual_request_plan: Mapping[str, Any] | BaseModel | None = None,
) -> tuple[Any, dict[str, Any], list[Any]]:
    """Run one context child with provider-bound decision validation and one repair."""

    mutation_tools = sorted(
        tool_name
        for tool in list(getattr(agent, "tools", []) or [])
        if (tool_name := _tool_name(tool))
        and operation_is_mutation(tool_name)
        and not is_provider_read_tool_name(tool_name)
    )
    if mutation_tools:
        raise ValueError(
            "Chief nested specialist execution must be read-only; mutation tools were "
            f"attached: {', '.join(mutation_tools)}"
        )
    execution_prompt = prompt + (
        "\n\nNested execution boundary:\n"
        "- The parent is running in live provider mode. Bounded read-only provider "
        "calls may set live=true when needed.\n"
        "- No mutation tool is attached and no write, checkpoint advance, post, or "
        "send is authorized."
        if live
        else (
            "\n\nNested execution boundary:\n"
            "- This is an offline or fixture execution. Keep provider calls live=false.\n"
            "- No mutation, checkpoint advance, post, or send is authorized."
        )
    )
    if route_name == "gmail_triage":
        from keystone_agents.agents.gmail_triage import (
            GmailAgentDecisionError,
            run_gmail_triage_sdk,
        )

        # Use the owning Gmail runner rather than raw Agent.as_tool execution.
        # Chief-authored object hints remain evidence, not verified identities;
        # the child must query/read before selecting and may repair only against
        # that same provider universe. The tier excludes all mailbox mutations.
        try:
            result = run_gmail_triage_sdk(
                execution_prompt,
                run_config=run_config,
                live=live,
                tool_tier=ToolTier.DIAGNOSTIC,
                max_turns=max_turns,
                manual_request_plan=manual_request_plan,
                provider_selection_required=True,
                prepared_agent=agent,
            )
        except GmailAgentDecisionError as exc:
            failed_result = exc.result
            failed_cache = dict(getattr(failed_result, "request_cache", {}) or {})
            ownership = {
                **dict(exc.telemetry),
                "attempt_count": 1 + int(failed_cache.get("decision_repairs") or 0),
                "nested_execution_mode": "live_read_only" if live else "offline_read_only",
            }
            exc.keystone_sdk_run_failure = {  # type: ignore[attr-defined]
                "schema": "keystone.sdk_run_failure.v1",
                "agent_name": route_name,
                "failure_kind": "gmailagentdecisionerror",
                "usage": dict(getattr(failed_result, "usage", {}) or {}),
                "request_cache": {"decision_ownership": ownership},
                "tool_receipts": list(getattr(failed_result, "tool_receipts", []) or []),
                "tool_execution": dict(
                    failed_cache.get("tool_execution") or {}
                ),
            }
            raise
        ownership = dict(result.request_cache.get("decision_ownership") or {})
        ownership.update(
            {
                "selected_candidate_ids": list(result.final_output.decision.selected_candidate_ids),
                "attempt_count": 1 + int(result.request_cache.get("decision_repairs") or 0),
                "nested_execution_mode": "live_read_only" if live else "offline_read_only",
            }
        )
        result.request_cache["decision_ownership"] = ownership
        return result, ownership, [result]
    if route_name in {"rss_context_agent", "preprints_context_agent"}:
        from keystone_agents.runtime.signal_context import run_signal_context_sdk

        result = run_signal_context_sdk(
            "preprints" if route_name == "preprints_context_agent" else "rss",
            execution_prompt,
            run_config=run_config,
            live=live,
            max_turns=max_turns,
            agent=agent,
        )
        ownership = dict(result.request_cache.get("decision_ownership") or {})
        ownership["nested_execution_mode"] = (
            "live_read_only" if live else "offline_read_only"
        )
        return result, ownership, [result]

    tool_contract = _nested_context_read_contract(route_name, agent)
    initial = run_typed_sdk_agent(
        agent=agent,
        typed_input=execution_prompt,
        output_type=output_type,
        run_config=run_config,
        live=live,
        max_turns=max_turns,
        tool_execution_contract=tool_contract,
    )
    provider_candidates = _nested_provider_candidate_ids(
        route_name,
        initial.raw_result,
    )
    contract = context_agent_decision_contract(
        route_name,
        provider_candidate_ids=provider_candidates,
    )
    outcome, evidence = validate_specialist_decision(initial.final_output, contract)
    attempts = [
        decision_validation_telemetry(
            initial.final_output,
            contract,
            evidence,
            outcome,
            attempt=1,
            tool_mode="model_called",
        )
    ]
    results = [initial]
    repair_evidence: dict[str, Any] | None = None
    if not provider_candidates:
        outcome = outcome.model_copy(
            update={
                "status": "rejected",
                "reason_code": "nested_provider_candidate_universe_missing",
                "feedback": (
                    "The child did not expose a bounded provider candidate universe; "
                    "Chief cannot treat its selection as verified."
                ),
            }
        )
        attempts[-1]["validator_outcome"] = outcome.model_dump(mode="json")
    elif outcome.status != "accepted":
        verified_fingerprints = tuple(
            identity_fingerprint(value) for value in provider_candidates
        )
        replay = build_decision_repair_evidence_replay(
            _nested_replay_result(initial.raw_result),
            evidence,
            verified_candidate_fingerprints=verified_fingerprints,
        )
        if replay.ready:
            repair_agent = _nested_tool_free_agent(agent)
            repair_evidence = replay.telemetry(
                disabled_read_tool_names=[
                    _tool_name(tool)
                    for tool in list(getattr(agent, "tools", []) or [])
                    if _tool_name(tool)
                ]
            )
            repair = run_typed_sdk_agent(
                agent=repair_agent,
                typed_input=execution_prompt
                + decision_repair_prompt(
                    contract,
                    evidence,
                    outcome,
                    evidence_replay=replay,
                ),
                output_type=output_type,
                run_config=run_config,
                live=live,
                session=None,
                max_turns=max(2, min(max_turns, 6)),
                tool_execution_contract=ToolExecutionContract(
                    mode=ToolExecutionMode.FORBIDDEN,
                    stage=f"{route_name}_chief_nested_decision_repair",
                ),
            )
            results.append(repair)
            outcome, evidence = validate_specialist_decision(
                repair.final_output,
                contract,
                verified_candidate_fingerprints=verified_fingerprints,
            )
            outcome = outcome.model_copy(update={"repair_attempted": True})
            attempts.append(
                decision_validation_telemetry(
                    repair.final_output,
                    contract,
                    evidence,
                    outcome,
                    attempt=2,
                    tool_mode="verified_context_tool_free",
                )
            )
        else:
            outcome = outcome.model_copy(
                update={
                    "status": "rejected",
                    "reason_code": replay.reason_code,
                    "feedback": replay.feedback,
                }
            )
            attempts[-1]["validator_outcome"] = outcome.model_dump(mode="json")
            repair_evidence = replay.telemetry()

    ownership = _nested_decision_envelope(
        route_name=route_name,
        contract=contract,
        evidence=evidence,
        outcome=outcome,
        attempts=attempts,
        repair_evidence=repair_evidence,
    )
    ownership["nested_execution_mode"] = (
        "live_read_only" if live else "offline_read_only"
    )
    if outcome.status != "accepted":
        error = AgentDecisionValidationError(outcome)
        error.keystone_sdk_run_failure = {  # type: ignore[attr-defined]
            "schema": "keystone.sdk_run_failure.v1",
            "agent_name": route_name,
            "failure_kind": "agentdecisionvalidationerror",
            "attempt_count": len(results),
            "usage": _nested_usage(results),
            "request_cache": {"decision_ownership": ownership},
            "tool_receipts": _nested_combine_receipts(results),
            "tool_execution": dict(initial.request_cache.get("tool_execution") or {}),
        }
        raise error
    final = results[-1]
    final.request_cache["decision_ownership"] = ownership
    if repair_evidence:
        final.request_cache["decision_repair_evidence"] = repair_evidence
        final.request_cache["decision_repairs"] = 1
    final.request_cache["nested_child_attempts"] = [
        {
            "attempt": index,
            "request_cache": dict(result.request_cache or {}),
            "usage": dict(result.usage or {}),
            "cost": dict(result.cost or {}),
            "tool_receipts": list(result.tool_receipts or []),
        }
        for index, result in enumerate(results, start=1)
    ]
    return final, ownership, results


async def extract_nested_specialist_result_json(
    run_result: Any,
    *,
    route_name: str,
    tool_name: str,
) -> str:
    """SDK custom output extractor returning a model-visible envelope."""

    envelope = extract_nested_specialist_result(
        run_result=run_result,
        route_name=route_name,
        tool_name=tool_name,
    )
    return json.dumps(envelope.model_dump(mode="json"), ensure_ascii=True, sort_keys=True)


def _nested_public_diagnostics(
    internal_record: Mapping[str, Any],
) -> list[ChiefSpecialistContextEntry]:
    ownership = _as_mapping(internal_record.get("decision_ownership"))
    outcome = _as_mapping(ownership.get("validator_outcome"))
    usage = _as_mapping(internal_record.get("usage"))
    handoff = _as_mapping(internal_record.get("handoff"))
    tool_origins = [
        value
        for value in list(internal_record.get("tool_origins") or [])
        if isinstance(value, Mapping)
    ]
    model_called = sorted(
        {
            str(name)
            for origin in tool_origins
            for name in list(origin.get("model_called_tool_names") or [])
            if str(name).strip()
        }
    )
    values = {
        "advisory_only": "True",
        "nested_execution_state": str(
            internal_record.get("execution_state") or "unknown"
        ),
        "nested_terminal_status": str(handoff.get("terminal_status") or "unknown"),
        "nested_consumption_status": str(
            handoff.get("consumption_status") or "unknown"
        ),
        "nested_execution_mode": str(
            internal_record.get("nested_execution_mode") or "unknown"
        ),
        "decision_owner": str(ownership.get("decision_owner") or "unknown"),
        "decision_stage": str(ownership.get("decision_stage") or "unknown"),
        "validator_status": str(outcome.get("status") or "unknown"),
        "validator_reason": str(outcome.get("reason_code") or "unknown"),
        "candidate_count": str(len(ownership.get("candidate_ids") or [])),
        "repair_attempts": str(internal_record.get("repair_attempts") or 0),
        "model_called_tools": ", ".join(model_called) or "none",
        "provider_receipt_count": str(len(internal_record.get("receipts") or [])),
        "model_request_count": str(usage.get("requests") or "unknown"),
        "private_identity_fingerprints_retained": "True",
        "raw_provider_content_publicly_exposed": "False",
    }
    return [ChiefSpecialistContextEntry(key=key, value=value) for key, value in values.items()]


def _redact_nested_provider_identities(
    text: object,
    *,
    identities: Sequence[object],
) -> str:
    cleaned = _compact_text(text, limit=1_200)
    for identity in sorted(
        {
            str(value).strip()
            for value in identities
            if len(str(value).strip()) >= 3
        },
        key=len,
        reverse=True,
    ):
        cleaned = cleaned.replace(identity, "[verified provider object]")
    return cleaned


def _sanitize_nested_public_envelope(
    envelope: ChiefNestedSpecialistResult,
    *,
    route_name: str,
    internal_record: Mapping[str, Any],
) -> ChiefNestedSpecialistResult:
    identities = list(internal_record.get("candidate_universe") or [])
    signal_route = route_name in {"rss_context_agent", "preprints_context_agent"}
    source_refs = [
        ref.model_copy(
            update={
                "source_id": "",
                "url": (
                    ref.url
                    if signal_route and ref.url.startswith(("https://", "http://"))
                    else ""
                ),
                "note": _redact_nested_provider_identities(
                    ref.note,
                    identities=identities,
                ),
            }
        )
        for ref in envelope.source_refs
    ]
    human_context = [
        item.model_copy(
            update={
                "value": _redact_nested_provider_identities(
                    item.value,
                    identities=identities,
                )
            }
        )
        for item in envelope.human_work_context
    ]
    public_source_ids = [
        ref.url for ref in source_refs if signal_route and ref.url
    ]
    return envelope.model_copy(
        update={
            "summary": _redact_nested_provider_identities(
                envelope.summary,
                identities=identities,
            ),
            "source_ids": public_source_ids,
            "source_refs": source_refs,
            "blockers": [
                _redact_nested_provider_identities(value, identities=identities)
                for value in envelope.blockers
            ],
            "approval_needs": [
                _redact_nested_provider_identities(value, identities=identities)
                for value in envelope.approval_needs
            ],
            "human_work_context": human_context,
            "diagnostics": [
                *envelope.diagnostics,
                *_nested_public_diagnostics(internal_record),
            ],
        }
    )


def _blocked_nested_specialist_envelope(
    *,
    route_name: str,
    tool_name: str,
    reason: str,
    reason_code: str,
) -> ChiefNestedSpecialistResult:
    base = extract_nested_specialist_result(
        run_result=type("BlockedNestedRun", (), {"final_output": None})(),
        route_name=route_name,
        tool_name=tool_name,
    )
    return base.model_copy(
        update={
            "summary": "Nested specialist execution was blocked before a verified result.",
            "blockers": [_compact_text(reason, limit=500)],
            "validation_status": "blocked",
            "diagnostics": [
                ChiefSpecialistContextEntry(key="advisory_only", value="True"),
                ChiefSpecialistContextEntry(
                    key="nested_execution_state",
                    value="blocked",
                ),
                ChiefSpecialistContextEntry(
                    key="nested_terminal_status",
                    value="blocked",
                ),
                ChiefSpecialistContextEntry(
                    key="validator_status",
                    value="blocked",
                ),
                ChiefSpecialistContextEntry(
                    key="validator_reason",
                    value=reason_code or "unknown",
                ),
                ChiefSpecialistContextEntry(
                    key="raw_provider_content_publicly_exposed",
                    value="False",
                ),
            ],
        }
    )


def _install_validated_nested_context_invoker(
    tool: Any,
    *,
    spec: AgentSpec,
    agent: Any,
    input_builder: Any,
    tool_name: str,
    live_execution: bool,
    max_turns: int,
    manual_request_plan: Mapping[str, Any] | BaseModel | None = None,
) -> Any:
    """Replace raw Agent.as_tool execution with a validated child adapter."""

    execution_records: dict[str, dict[str, Any]] = {}
    validated_replay: dict[str, Any] = {}
    child_execution_in_progress = False
    _install_nested_live_read_boundaries(agent, live=live_execution)

    def retain_internal_record(
        context: Any,
        tool_call_id: str,
        record: Mapping[str, Any],
    ) -> None:
        payload = dict(record)
        execution_records[tool_call_id] = payload
        # SDK-only custom data is retained on the ToolCallOutputItem and is not
        # replayed to the model. This makes the child decision and receipts
        # available to the parent trace/session without adding private provider
        # identities to the model-visible or public tool output.
        context._custom_data = {  # noqa: SLF001 - documented SDK adapter hook
            "keystone_nested_specialist_execution": payload
        }

    async def invoke(context: Any, input_json: str) -> str:
        nonlocal child_execution_in_progress
        tool_call_id = str(
            getattr(context, "tool_call_id", "")
            or f"nested-{sha256(input_json.encode('utf-8')).hexdigest()[:16]}"
        )
        run_config = getattr(context, "run_config", None)
        if run_config is None:
            envelope = _blocked_nested_specialist_envelope(
                route_name=spec.route_name,
                tool_name=tool_name,
                reason=(
                    "The parent execution context did not provide an SDK run_config, so "
                    "the child was not run through an unvalidated fallback."
                ),
                reason_code="nested_run_config_missing",
            )
            retain_internal_record(context, tool_call_id, {
                "schema": "keystone.chief_nested_specialist_execution.v1",
                "route_name": spec.route_name,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "execution_state": "blocked",
                "handoff": {
                    "state": "not_executed",
                    "consumption_status": "blocked_before_child_execution",
                    "terminal_status": "blocked",
                },
                "reason_code": "nested_run_config_missing",
            })
            return json.dumps(envelope.model_dump(mode="json"), sort_keys=True)
        try:
            parsed = ChiefSpecialistToolInput.model_validate_json(input_json)
            replayed_envelope = str(validated_replay.get("envelope_json") or "")
            if replayed_envelope:
                source_record = _as_mapping(validated_replay.get("internal_record"))
                source_tool_call_id = str(source_record.get("tool_call_id") or "")
                replay_record = {
                    "schema": "keystone.chief_nested_specialist_execution.v1",
                    "route_name": spec.route_name,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "execution_state": "replayed_validated_nested_specialist",
                    "advisory_only": True,
                    "provider_write_executed": False,
                    "provider_read_executed": False,
                    "child_execution_reused": True,
                    "child_rerun_allowed": False,
                    "source_tool_call_id": source_tool_call_id,
                    "source_envelope_fingerprint": str(
                        validated_replay.get("envelope_fingerprint") or ""
                    ),
                    "candidate_identity_fingerprints": list(
                        source_record.get("candidate_identity_fingerprints") or []
                    ),
                    "selected_identity_fingerprints": list(
                        source_record.get("selected_identity_fingerprints") or []
                    ),
                    "tool_origins": [],
                    "tool_execution_attempts": [],
                    "receipts": [],
                    "usage": {
                        "available": True,
                        "complete": True,
                        "attempt_count": 0,
                        "requests": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "total_tokens": 0,
                        "cached_input_tokens": 0,
                        "reasoning_output_tokens": 0,
                    },
                    "repair_attempts": 0,
                    "parent_repair_replay": {
                        "schema": "keystone.chief_parent_decision_replay.v1",
                        "status": "replayed",
                        "mode": "sanitized_child_envelope",
                        "provider_calls_during_replay": 0,
                        "child_model_requests_during_replay": 0,
                        "child_rerun_allowed": False,
                    },
                    "handoff": {
                        "state": "replayed",
                        "consumption_status": "returned_to_chief_model",
                        "terminal_status": "completed",
                    },
                }
                retain_internal_record(context, tool_call_id, replay_record)
                return replayed_envelope
            if child_execution_in_progress:
                envelope = _blocked_nested_specialist_envelope(
                    route_name=spec.route_name,
                    tool_name=tool_name,
                    reason=(
                        "The same one-shot context specialist is already running for this "
                        "Chief request; a parallel duplicate provider read was blocked."
                    ),
                    reason_code="nested_child_duplicate_in_progress",
                )
                retain_internal_record(context, tool_call_id, {
                    "schema": "keystone.chief_nested_specialist_execution.v1",
                    "route_name": spec.route_name,
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "execution_state": "blocked_duplicate",
                    "provider_read_executed": False,
                    "provider_write_executed": False,
                    "child_rerun_allowed": False,
                    "usage": {"available": True, "complete": True, "requests": 0},
                    "reason_code": "nested_child_duplicate_in_progress",
                    "handoff": {
                        "state": "not_executed",
                        "consumption_status": "duplicate_blocked",
                        "terminal_status": "blocked",
                    },
                })
                return json.dumps(
                    envelope.model_dump(mode="json"),
                    ensure_ascii=True,
                    sort_keys=True,
                )
            child_execution_in_progress = True
            prompt = input_builder(
                {"params": parsed.model_dump(mode="json")}
            )
            result, ownership, results = await asyncio.to_thread(
                _run_validated_nested_context_child,
                route_name=spec.route_name,
                agent=agent,
                output_type=spec.resolve_output_schema(),
                prompt=prompt,
                run_config=run_config,
                live=live_execution,
                max_turns=max_turns,
                manual_request_plan=manual_request_plan,
            )
            internal_record = _nested_internal_execution_record(
                route_name=spec.route_name,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                results=results,
                decision_ownership=ownership,
                terminal_status="completed",
            )
            envelope = extract_nested_specialist_result(
                run_result=result,
                route_name=spec.route_name,
                tool_name=tool_name,
            )
            envelope = _sanitize_nested_public_envelope(
                envelope,
                route_name=spec.route_name,
                internal_record=internal_record,
            )
            envelope = envelope.model_copy(
                update={
                    "diagnostics": [
                        *envelope.diagnostics,
                        ChiefSpecialistContextEntry(
                            key="parent_repair_replay",
                            value="armed",
                        ),
                        ChiefSpecialistContextEntry(
                            key="child_rerun_allowed",
                            value="False",
                        ),
                    ]
                }
            )
            envelope_json = json.dumps(
                envelope.model_dump(mode="json"),
                ensure_ascii=True,
                sort_keys=True,
            )
            envelope_fingerprint = sha256(envelope_json.encode("utf-8")).hexdigest()
            parent_agent = getattr(tool, _NESTED_PARENT_AGENT_ATTR, None)
            replay_blocks = getattr(
                parent_agent,
                _NESTED_PARENT_REPLAY_BLOCKS_ATTR,
                None,
            )
            replay_armed = isinstance(replay_blocks, dict)
            if replay_armed:
                replay_blocks[tool_name] = (
                    "Validated nested specialist evidence replay (provider-read-free):\n"
                    f"- specialist_route: {spec.route_name}\n"
                    f"- source_tool: {tool_name}\n"
                    "- This sanitized envelope came from one completed, validated child "
                    "execution. Reuse it for the Chief decision and any validator-driven "
                    "repair. Do not request or infer fresh provider context.\n"
                    "- The child tool is disabled; no second child model run or provider "
                    "read is permitted.\n"
                    f"Sanitized child envelope: {envelope_json}"
                )
                tool.is_enabled = False
            replay_telemetry = {
                "schema": "keystone.chief_parent_decision_replay.v1",
                "status": "armed" if replay_armed else "parent_boundary_unavailable",
                "mode": "sanitized_child_envelope_in_parent_instructions",
                "injection_policy": "fresh_parent_attempt_only",
                "source_tool_call_id": tool_call_id,
                "source_envelope_fingerprint": envelope_fingerprint,
                "source_envelope_serialized_chars": len(envelope_json),
                "provider_calls_during_replay": 0,
                "child_model_requests_during_replay": 0,
                "child_tool_disabled": bool(replay_armed),
                "child_rerun_allowed": not replay_armed,
            }
            internal_record["parent_repair_replay"] = replay_telemetry
            internal_record["child_rerun_allowed"] = not replay_armed
            retain_internal_record(context, tool_call_id, internal_record)
            if replay_armed:
                validated_replay.update(
                    {
                        "envelope_json": envelope_json,
                        "envelope_fingerprint": envelope_fingerprint,
                        "internal_record": dict(internal_record),
                    }
                )
            child_execution_in_progress = False
            return envelope_json
        except Exception as exc:
            child_execution_in_progress = False
            failure = sdk_run_failure_metadata(exc)
            failure_cache = _as_mapping(failure.get("request_cache"))
            ownership = _as_mapping(failure_cache.get("decision_ownership"))
            outcome = _as_mapping(ownership.get("validator_outcome"))
            reason_code = str(
                outcome.get("reason_code")
                or failure.get("failure_kind")
                or type(exc).__name__
            ).lower()
            retain_internal_record(context, tool_call_id, {
                "schema": "keystone.chief_nested_specialist_execution.v1",
                "route_name": spec.route_name,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "execution_state": "blocked",
                "raw_structured_decision": {},
                "decision_ownership": ownership,
                "candidate_universe": list(ownership.get("candidate_ids") or []),
                "candidate_identity_fingerprints": [
                    identity_fingerprint(value)
                    for value in list(ownership.get("candidate_ids") or [])
                ],
                "tool_origins": [dict(failure.get("tool_execution") or {})],
                "receipts": [
                    dict(value)
                    for value in list(failure.get("tool_receipts") or [])
                    if isinstance(value, Mapping)
                ],
                "usage": dict(failure.get("usage") or {}),
                "repair_attempts": max(
                    0,
                    int(ownership.get("attempt_count") or 1) - 1,
                ),
                "handoff": {
                    "state": "executed_then_blocked",
                    "consumption_status": "blocked_result_returned_to_chief_model",
                    "terminal_status": "blocked",
                },
                "reason_code": reason_code,
            })
            envelope = _blocked_nested_specialist_envelope(
                route_name=spec.route_name,
                tool_name=tool_name,
                reason=(
                    "The nested specialist did not produce a provider-bound validated "
                    "decision. Chief must not use its prose as a verified result."
                ),
                reason_code=reason_code,
            )
            return json.dumps(
                envelope.model_dump(mode="json"),
                ensure_ascii=True,
                sort_keys=True,
            )

    tool.on_invoke_tool = invoke
    _safe_setattr(tool, "nested_execution_records", execution_records)
    _safe_setattr(tool, "nested_execution_contract", "validated_child_decision_v1")
    return tool


def _builder_accepts(builder: Any, parameter_name: str) -> bool:
    try:
        signature = inspect.signature(builder)
    except (TypeError, ValueError):
        return False
    return parameter_name in signature.parameters


def _build_specialist_agent(
    spec: AgentSpec,
    *,
    read_plan_tier: ToolTier | str = ToolTier.DIAGNOSTIC,
    raw_operator_request: str = "",
    manual_request_plan: Mapping[str, Any] | BaseModel | None = None,
    live_execution: bool = False,
) -> Any:
    builder = spec.resolve_builder()
    kwargs: dict[str, Any] = {}
    specialist_plan: Mapping[str, Any] | BaseModel | None = manual_request_plan
    if spec.route_name == "business_research_analyst" and raw_operator_request:
        selected_url_plan = infer_selected_public_url_specialist_plan(
            raw_operator_request
        )
        if selected_url_plan is not None:
            specialist_plan = selected_url_plan
    if _builder_accepts(builder, "request_text"):
        kwargs["request_text"] = raw_operator_request
    if _builder_accepts(builder, "manual_request_plan"):
        kwargs["manual_request_plan"] = specialist_plan
    if _builder_accepts(builder, "tool_tier"):
        kwargs["tool_tier"] = read_plan_tier
    if spec.route_name == "gmail_triage":
        kwargs["provider_tools_live"] = live_execution
        kwargs["provider_selection_mode"] = True
    agent = builder(**kwargs)
    if "tool_tier" not in kwargs:
        agent.tools = filter_tools_for_tier(spec.route_name, list(agent.tools or []), read_plan_tier)
    return agent


def build_specialist_agent_tools(
    *,
    manager_agent_name: str,
    mode: SpecialistToolMode = "read_plan",
    raw_operator_request: str = "",
    manual_request_plan: Mapping[str, Any] | BaseModel | None = None,
    approval_context: Mapping[str, Any] | None = None,
    specs: Sequence[AgentSpec] = SPECIALIST_AGENT_SPECS,
    route_tool_name_overrides: Mapping[str, str] | None = None,
    route_descriptions: Mapping[str, str] | None = None,
    include_routes: set[str] | frozenset[str] | None = None,
    exclude_routes: set[str] | frozenset[str] | None = None,
    live_execution: bool = False,
    max_turns: int = 6,
) -> list[Any]:
    """Build SDK `Agent.as_tool()` tools for registered Keystone specialists."""

    _ = approval_context
    tool_name_overrides = dict(route_tool_name_overrides or {})
    descriptions = dict(route_descriptions or {})
    include = set(include_routes or ())
    exclude = set(exclude_routes or ())
    tools: list[Any] = []
    for spec in specs:
        if include and spec.route_name not in include:
            continue
        if spec.route_name == "rag_retrieval_specialist" and not include:
            # Keep RAG opt-in during its initial rollout. Direct named-agent and
            # explicit graph routes remain available; managers must request this
            # specialist deliberately before it is exposed as an agent tool.
            continue
        if spec.route_name in exclude or spec.route_name == manager_agent_name:
            continue
        agent = _build_specialist_agent(
            spec,
            raw_operator_request=raw_operator_request,
            manual_request_plan=manual_request_plan,
            live_execution=live_execution,
        )
        as_tool = getattr(agent, "as_tool", None)
        if not callable(as_tool):
            continue
        tool_name = tool_name_overrides.get(
            spec.route_name,
            specialist_agent_tool_name(spec.route_name),
        )
        async def output_extractor(
            run_result: Any,
            *,
            route_name: str = spec.route_name,
            resolved_tool_name: str = tool_name,
        ) -> str:
            return await extract_nested_specialist_result_json(
                run_result,
                route_name=route_name,
                tool_name=resolved_tool_name,
            )

        def input_builder(
            options: Mapping[str, Any],
            *,
            bound_raw_operator_request: str = raw_operator_request,
            bound_manual_request_plan: Mapping[str, Any] | BaseModel | None = (
                manual_request_plan
            ),
        ) -> str:
            return build_bound_chief_specialist_tool_input(
                options,
                raw_operator_request=bound_raw_operator_request,
                manual_request_plan=bound_manual_request_plan,
            )

        mode_note = (
            "approved-write planning helper; Chief of Staff owns any actual write tools"
            if mode == "approved_write"
            else "read/plan-only; write and publish tools are not exposed"
        )
        safety_note = (
            "Nested output is advisory to the manager agent; Python approval, source, "
            "and side-effect gates remain authoritative."
        )
        context_note = (
            "Use decision_context for objective/success criteria, target_context for "
            "system objects and entities, and coordination_context for sibling "
            "specialist dependencies or prior nested outputs. Use provider_call_context "
            "to preserve exact provider/tool-call hints such as query, ID, date window, "
            "folder path, sheet tab, field mapping, label action, source basis, and "
            "approval reference."
        )
        description = descriptions.get(
            spec.route_name,
            f"Run {spec.agent_name} as a {mode_note} helper for {manager_agent_name}. "
            f"{SPECIALIST_CONTEXT_GUIDANCE.get(spec.route_name, '').strip()} "
            f"{context_note} {safety_note}",
        )
        specialist_tool = as_tool(
            tool_name=tool_name,
            tool_description=description,
            parameters=ChiefSpecialistToolInput,
            input_builder=input_builder,
            custom_output_extractor=output_extractor,
            max_turns=max_turns,
        )
        if spec.route_name in _CHIEF_VALIDATED_CONTEXT_ROUTES:
            specialist_tool = _install_validated_nested_context_invoker(
                specialist_tool,
                spec=spec,
                agent=agent,
                input_builder=input_builder,
                tool_name=tool_name,
                live_execution=live_execution,
                max_turns=max_turns,
                manual_request_plan=manual_request_plan,
            )
        tools.append(specialist_tool)
        _safe_setattr(tools[-1], "specialist_route_name", spec.route_name)
        _safe_setattr(tools[-1], "specialist_tool_mode", mode)
        _safe_setattr(tools[-1], "specialist_write_authorized", False)
        _safe_setattr(tools[-1], "nested_tool_names", tuple(_tool_name(tool) for tool in agent.tools))
        _safe_setattr(tools[-1], "specialist_input_model", ChiefSpecialistToolInput)
        _safe_setattr(tools[-1], "specialist_result_model", ChiefNestedSpecialistResult)
        _safe_setattr(tools[-1], "specialist_input_builder", input_builder)
        _safe_setattr(tools[-1], "specialist_output_extractor", output_extractor)
        _safe_setattr(
            tools[-1],
            "specialist_type_contract",
            build_handoff_type_contract(
                source_agent=manager_agent_name,
                target_agent=spec.route_name,
                source_output_type="keystone_agents.schemas.chief_of_staff.ChiefSpecialistToolInput",
                target_input_type=getattr(spec, "input_contract_schema", ""),
                target_output_type=spec.output_schema,
                payload_mode="adapted",
                parsed_output_status="parsed",
                compatibility_notes=[
                    "Chief specialist tool input is adapted into the selected specialist input contract."
                ],
            ).model_dump(mode="json"),
        )
    return tools
