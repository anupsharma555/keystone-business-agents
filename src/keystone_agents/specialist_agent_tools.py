"""Shared helpers for exposing Keystone specialist agents as SDK tools."""

from __future__ import annotations

import inspect
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel

from keystone_agents.agent_registry import SPECIALIST_AGENT_SPECS, AgentSpec
from keystone_agents.agent_tool_policy import ToolTier, filter_tools_for_tier
from keystone_agents.schemas.chief_of_staff import (
    ChiefNestedSpecialistResult,
    ChiefNestedSpecialistSourceRef,
    ChiefSpecialistContextEntry,
    ChiefSpecialistToolInput,
)
from keystone_agents.schemas.handoff_types import HandoffTypeContract, build_handoff_type_contract
from keystone_agents.specialist_tool_names import specialist_agent_tool_name

SpecialistToolMode = Literal["read_plan", "approved_write"]

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
        "assumptions, blockers, approval needs, human work context, and a Chief-owned "
        "Airtable write plan. Do not execute writes."
    ),
    "google_workspace_context_agent": (
        "Return relevant Drive folder/file, Doc, Sheet, and tab context, target-selection "
        "rationale, blockers, approval needs, human work context, and a Chief-owned "
        "Workspace write plan. Do not execute writes."
    ),
    "zotero_context_agent": (
        "Return relevant Zotero library, collection, item, article, source ID, "
        "evidence-gap, and human work context plus a Chief-owned artifact plan. "
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
            "Nested specialist output is adapted into a Chief-owned review envelope."
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
        "",
        "## Structured Chief Context",
        json.dumps(params, ensure_ascii=True, sort_keys=True, indent=2),
    ]
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
    """Normalize one nested specialist SDK result into a Chief-owned envelope."""

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
            human_work_context=_context_entries_from_mapping(payload.get("human_work_context")),
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
) -> Any:
    builder = spec.resolve_builder()
    kwargs: dict[str, Any] = {}
    if _builder_accepts(builder, "tool_tier"):
        kwargs["tool_tier"] = read_plan_tier
    agent = builder(**kwargs)
    if "tool_tier" not in kwargs:
        agent.tools = filter_tools_for_tier(spec.route_name, list(agent.tools or []), read_plan_tier)
    return agent


def build_specialist_agent_tools(
    *,
    manager_agent_name: str,
    mode: SpecialistToolMode = "read_plan",
    approval_context: Mapping[str, Any] | None = None,
    specs: Sequence[AgentSpec] = SPECIALIST_AGENT_SPECS,
    route_tool_name_overrides: Mapping[str, str] | None = None,
    route_descriptions: Mapping[str, str] | None = None,
    include_routes: set[str] | frozenset[str] | None = None,
    exclude_routes: set[str] | frozenset[str] | None = None,
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
        if spec.route_name in exclude or spec.route_name == manager_agent_name:
            continue
        agent = _build_specialist_agent(spec)
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
        tools.append(
            as_tool(
                tool_name=tool_name,
                tool_description=description,
                parameters=ChiefSpecialistToolInput,
                input_builder=build_chief_specialist_tool_input,
                custom_output_extractor=output_extractor,
                max_turns=max_turns,
            )
        )
        _safe_setattr(tools[-1], "specialist_route_name", spec.route_name)
        _safe_setattr(tools[-1], "specialist_tool_mode", mode)
        _safe_setattr(tools[-1], "specialist_write_authorized", False)
        _safe_setattr(tools[-1], "nested_tool_names", tuple(_tool_name(tool) for tool in agent.tools))
        _safe_setattr(tools[-1], "specialist_input_model", ChiefSpecialistToolInput)
        _safe_setattr(tools[-1], "specialist_result_model", ChiefNestedSpecialistResult)
        _safe_setattr(tools[-1], "specialist_input_builder", build_chief_specialist_tool_input)
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
