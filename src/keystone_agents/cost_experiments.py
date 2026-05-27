"""Utilities for comparing repeated SDK run cache and cost behavior."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from keystone_agents.costing import compare_estimated_to_actual_cost, summarize_cache_experiment
from keystone_agents.schemas.work_item import WorkItemEvent
from keystone_agents.storage.sqlite_store import SQLiteStore, database_url_from_env

SLACK_COST_CONTROLLED_PROFILES = frozenset(
    {
        "slack_conservative",
        "slack-cost-conservative",
        "slack_context_light",
        "slack_manager_balanced",
        "slack_research_balanced",
        "slack_opportunity_balanced",
        "slack_research_deep",
    }
)


def sdk_cost_record_from_payload(
    payload: dict[str, Any],
    *,
    run_id: str = "",
    agent_name: str = "",
    actual_usd: float | str | None = None,
) -> dict[str, Any]:
    """Extract comparable SDK usage/cost/cache metadata from a saved payload."""

    usage = _dict_at(payload, "usage") or _dict_at(payload, "_sdk_usage")
    cost = dict(_dict_at(payload, "cost") or _dict_at(payload, "_sdk_cost"))
    request_cache = _dict_at(payload, "request_cache") or _dict_at(payload, "_sdk_request_cache")
    model = _dict_at(payload, "model") or _dict_at(payload, "_sdk_model")
    if actual_usd is not None:
        cost["estimate_vs_actual"] = compare_estimated_to_actual_cost(
            cost=cost,
            actual_usd=actual_usd,
        )
        cost["actual_usd"] = cost["estimate_vs_actual"].get("actual_usd")
    return {
        "run_id": str(run_id or payload.get("run_id") or payload.get("id") or ""),
        "agent_name": str(agent_name or payload.get("agent_name") or ""),
        "model": model,
        "usage": usage,
        "cost": cost,
        "request_cache": request_cache,
    }


def sdk_cost_record_from_agent_run_row(
    row: dict[str, Any],
    *,
    actual_usd: float | str | None = None,
) -> dict[str, Any]:
    """Extract comparable SDK metadata from one SQLite `agent_runs` row."""

    payload = _loads_json_object(row.get("output_json"))
    record = sdk_cost_record_from_payload(
        payload,
        run_id=str(row.get("id") or ""),
        agent_name=str(row.get("agent_name") or ""),
        actual_usd=actual_usd,
    )
    record["input_summary"] = str(row.get("input_summary") or "")
    record["created_at_et"] = str(row.get("created_at_et") or "")
    record["dry_run"] = bool(row.get("dry_run"))
    record["status"] = str(row.get("status") or "")
    if not record["model"]:
        record["model"] = str(row.get("model") or "")
    return record


def compare_sdk_cost_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare repeated SDK run records for cache, cost, and request-prefix stability."""

    cache_summary = summarize_cache_experiment(records)
    request_cache_summary = _request_cache_summary(records)
    actual_comparisons = [
        {
            "run_id": record.get("run_id", ""),
            **comparison,
        }
        for record in records
        for comparison in [_dict_at(record.get("cost"), "estimate_vs_actual")]
        if comparison
    ]
    diagnosis = _cache_readiness_diagnosis(
        records=records,
        cache_summary=cache_summary,
        request_cache_summary=request_cache_summary,
    )
    return {
        "available": bool(records),
        "run_count": len(records),
        "cache_summary": cache_summary,
        "request_cache_summary": request_cache_summary,
        "cache_readiness": diagnosis,
        "actual_cost_comparisons": actual_comparisons,
        "runs": [_compact_record(record) for record in records],
        "note": (
            "For repeated Slack-thread tests, expect stable static_prefix_sha256, "
            "session_attached=true, append-only dynamic prompt growth, and higher "
            "cached_input_tokens on the follow-up run."
        ),
    }


def render_sdk_cost_comparison_markdown(summary: dict[str, Any]) -> str:
    """Render a concise operator-facing cache/cost comparison report."""

    cache = _dict_at(summary, "cache_summary")
    request_cache = _dict_at(summary, "request_cache_summary")
    readiness = _dict_at(summary, "cache_readiness")
    runs = summary.get("runs") if isinstance(summary.get("runs"), list) else []
    lines = [
        "# SDK Cost Cache Comparison",
        "",
        f"- Runs compared: {_clean(summary.get('run_count', 0))}",
        f"- Cache summary available: {_yes_no(bool(cache.get('available')))}",
        f"- Request-cache diagnostics available: {_yes_no(bool(request_cache.get('available')))}",
    ]
    if cache.get("available"):
        lines.extend(
            [
                "",
                "## Cache And Cost",
                "",
                f"- First cache hit rate: {_clean(cache.get('first_cache_hit_rate'))}",
                f"- Last cache hit rate: {_clean(cache.get('last_cache_hit_rate'))}",
                f"- Cache hit rate delta: {_clean(cache.get('cache_hit_rate_delta'))}",
                f"- First estimated USD: {_money(cache.get('first_estimated_usd'))}",
                f"- Last estimated USD: {_money(cache.get('last_estimated_usd'))}",
                f"- Estimated USD delta: {_money(cache.get('estimated_usd_delta'))}",
            ]
        )
    if request_cache.get("available"):
        lines.extend(
            [
                "",
                "## Request Cache Diagnostics",
                "",
                f"- Static prefix stable: {_yes_no(bool(request_cache.get('static_prefix_stable')))}",
                f"- Instructions stable: {_yes_no(bool(request_cache.get('instructions_stable')))}",
                f"- Tool order stable: {_yes_no(bool(request_cache.get('tool_order_stable')))}",
                f"- Output schema stable: {_yes_no(bool(request_cache.get('output_schema_stable')))}",
                f"- Session attached for all runs: {_yes_no(bool(request_cache.get('session_attached_all')))}",
                f"- Session hash stable: {_yes_no(bool(request_cache.get('session_hash_stable')))}",
                (
                    "- Dynamic prompt chars: "
                    f"{_clean(request_cache.get('first_dynamic_prompt_chars'))} to "
                    f"{_clean(request_cache.get('last_dynamic_prompt_chars'))} "
                    f"(delta {_clean(request_cache.get('dynamic_prompt_chars_delta'))})"
                ),
            ]
        )
    if runs:
        lines.extend(["", "## Runs", "", "| Run | Cache Hit | Input | Cached Input | Output | Estimated | Actual | Session |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"])
        for run in runs:
            if not isinstance(run, dict):
                continue
            lines.append(
                "| "
                + " | ".join(
                    [
                        _clean(run.get("run_id") or run.get("agent_name") or "run"),
                        _clean(run.get("cache_hit_rate")),
                        _clean(run.get("input_tokens")),
                        _clean(run.get("cached_input_tokens")),
                        _clean(run.get("output_tokens")),
                        _money(run.get("estimated_usd")),
                        _money(run.get("actual_usd")),
                        _yes_no(bool(run.get("session_attached"))),
                    ]
                )
                + " |"
            )
    if readiness:
        lines.extend(
            [
                "",
                "## Cache Readiness",
                "",
                f"- Status: {_clean(readiness.get('status')) or 'unknown'}",
                f"- Cache-friendly: {_yes_no(bool(readiness.get('cache_friendly')))}",
            ]
        )
        issues = readiness.get("issues") if isinstance(readiness.get("issues"), list) else []
        recommendations = (
            readiness.get("recommendations")
            if isinstance(readiness.get("recommendations"), list)
            else []
        )
        if issues:
            lines.extend(["", "Issues:"])
            lines.extend(f"- {_clean(issue)}" for issue in issues)
        if recommendations:
            lines.extend(["", "Recommended fixes:"])
            lines.extend(f"- {_clean(item)}" for item in recommendations)
    comparisons = summary.get("actual_cost_comparisons")
    if isinstance(comparisons, list) and comparisons:
        lines.extend(["", "## OpenAI Platform Comparison", ""])
        for comparison in comparisons:
            if not isinstance(comparison, dict):
                continue
            lines.append(
                "- "
                f"{_clean(comparison.get('run_id') or 'run')}: "
                f"estimated {_money(comparison.get('estimated_usd'))}, "
                f"actual {_money(comparison.get('actual_usd'))}, "
                f"delta {_money(comparison.get('delta_usd'))}"
            )
    note = _clean(summary.get("note"))
    if note:
        lines.extend(["", "## Interpretation", "", note])
    return "\n".join(lines).strip()


def _cache_readiness_diagnosis(
    *,
    records: list[dict[str, Any]],
    cache_summary: dict[str, Any],
    request_cache_summary: dict[str, Any],
) -> dict[str, Any]:
    issues: list[str] = []
    recommendations: list[str] = []
    if len(records) < 2:
        issues.append("Need at least two comparable runs to evaluate repeated-run caching.")
        recommendations.append("Run the same thread once, append a follow-up, then compare both runs.")
    if not cache_summary.get("available"):
        issues.append("SDK token usage was unavailable, so cached input cannot be measured.")
        recommendations.append("Use a live SDK/model path that returns input and cached-input usage.")
    if not request_cache_summary.get("available"):
        issues.append("Request-cache fingerprints were unavailable for one or more runs.")
        recommendations.append("Ensure runs use the centralized run_typed_sdk_agent wrapper.")
    if request_cache_summary.get("available"):
        if not request_cache_summary.get("static_prefix_stable"):
            issues.append("Static prompt prefix changed between runs.")
            recommendations.append(
                "Keep agent instructions, tool order, and output schema stable across follow-ups."
            )
        if not request_cache_summary.get("tool_order_stable"):
            issues.append("Tool ordering changed between runs.")
            recommendations.append("Sort or otherwise stabilize dynamic tool assembly.")
        if not request_cache_summary.get("output_schema_stable"):
            issues.append("Structured output schema changed between runs.")
            recommendations.append("Avoid run-specific schema generation or unordered schema fields.")
        if not request_cache_summary.get("session_attached_all"):
            issues.append("At least one run did not attach an SDK session.")
            recommendations.append("Use the same thread-derived SDK session for repeated Slack runs.")
        if not request_cache_summary.get("session_hash_stable"):
            issues.append("Runs used different SDK session hashes.")
            recommendations.append(
                "Compare runs from the same Slack thread/session, or pass explicit run IDs from the same thread."
            )
    if cache_summary.get("available"):
        first_rate = _float_value(cache_summary.get("first_cache_hit_rate"))
        last_rate = _float_value(cache_summary.get("last_cache_hit_rate"))
        if last_rate is not None and len(records) >= 2 and last_rate < 0.10:
            issues.append("Follow-up run had very low cached input.")
            recommendations.append(
                "Check for volatile metadata, timestamps, retrieved context, or changing ids before the stable prefix."
            )
        elif (
            first_rate is not None
            and last_rate is not None
            and len(records) >= 2
            and last_rate <= first_rate
        ):
            issues.append("Cached input did not improve on the follow-up run.")
            recommendations.append(
                "Confirm the follow-up request is append-only and reuses the same prior thread text."
            )
    status = "pass" if not issues else "warn"
    return {
        "status": status,
        "cache_friendly": not issues,
        "issues": issues,
        "recommendations": list(dict.fromkeys(recommendations)),
    }


def load_sdk_cost_records_from_database(
    *,
    database_url: str | None = None,
    run_ids: list[str] | None = None,
    limit: int = 2,
    actual_usd: list[str] | None = None,
    session_hash: str = "",
    same_session_as_run_id: str = "",
    latest_session: bool = False,
    latest_repeated_session: bool = False,
    min_session_runs: int = 2,
) -> list[dict[str, Any]]:
    """Load comparable SDK records from local `agent_runs` audit rows."""

    store = SQLiteStore(database_url or database_url_from_env())
    rows = store.fetch_all("agent_runs")
    selected_rows = _select_rows(
        rows,
        run_ids=run_ids,
        limit=limit,
        session_hash=session_hash,
        same_session_as_run_id=same_session_as_run_id,
        latest_session=latest_session,
        latest_repeated_session=latest_repeated_session,
        min_session_runs=min_session_runs,
    )
    actuals = actual_usd or []
    return [
        sdk_cost_record_from_agent_run_row(
            row,
            actual_usd=actuals[index] if index < len(actuals) else None,
        )
        for index, row in enumerate(selected_rows)
    ]


def annotate_actual_costs_for_database_selection(
    *,
    database_url: str | None = None,
    actual_usd: list[str] | None = None,
    run_ids: list[str] | None = None,
    limit: int = 2,
    session_hash: str = "",
    same_session_as_run_id: str = "",
    latest_session: bool = False,
    latest_repeated_session: bool = False,
    min_session_runs: int = 2,
    actual_source: str = "operator_openai_platform",
    actual_reference_id: str = "",
) -> list[dict[str, Any]]:
    """Persist operator-provided actual platform costs onto selected agent runs."""

    actuals = actual_usd or []
    if not actuals:
        return []
    store = SQLiteStore(database_url or database_url_from_env())
    rows = _select_rows(
        store.fetch_all("agent_runs"),
        run_ids=run_ids,
        limit=limit,
        session_hash=session_hash,
        same_session_as_run_id=same_session_as_run_id,
        latest_session=latest_session,
        latest_repeated_session=latest_repeated_session,
        min_session_runs=min_session_runs,
    )
    if len(actuals) != len(rows):
        raise ValueError(
            "Number of --actual-usd values must match selected agent_runs rows "
            f"({len(actuals)} actuals for {len(rows)} rows)."
        )
    return [
        store.annotate_agent_run_actual_cost(
            row.get("id"),
            actual_usd=actuals[index],
            source=actual_source,
            reference_id=actual_reference_id,
        )
        for index, row in enumerate(rows)
    ]


def load_sdk_cost_records_from_payload_files(
    payload_files: list[str | Path],
    *,
    actual_usd: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Load comparable SDK records from saved CLI JSON payload files."""

    actuals = actual_usd or []
    records: list[dict[str, Any]] = []
    for index, payload_file in enumerate(payload_files):
        path = Path(payload_file)
        payload = _loads_json_object(path.read_text(encoding="utf-8"))
        records.append(
            sdk_cost_record_from_payload(
                payload,
                run_id=path.stem,
                actual_usd=actuals[index] if index < len(actuals) else None,
            )
        )
    return records


def list_sdk_session_run_groups(
    *,
    database_url: str | None = None,
    limit: int = 10,
    min_runs: int = 1,
) -> list[dict[str, Any]]:
    """Return audit-safe recent SDK session groups from local `agent_runs` rows."""

    store = SQLiteStore(database_url or database_url_from_env())
    groups: dict[str, dict[str, Any]] = {}
    for row in store.fetch_all("agent_runs"):
        payload = _loads_json_object(row.get("output_json"))
        request_cache = _dict_at(payload, "_sdk_request_cache") or _dict_at(
            payload,
            "request_cache",
        )
        session_hash = str(request_cache.get("session_id_hash") or "").strip()
        if not session_hash:
            continue
        usage = _dict_at(payload, "_sdk_usage") or _dict_at(payload, "usage")
        cost = _dict_at(payload, "_sdk_cost") or _dict_at(payload, "cost")
        model = _dict_at(payload, "_sdk_model") or _dict_at(payload, "model")
        group = groups.setdefault(
            session_hash,
            {
                "session_id_hash": session_hash,
                "session_scope": str(request_cache.get("session_scope") or ""),
                "run_count": 0,
                "first_run_id": "",
                "last_run_id": "",
                "last_created_at_et": "",
                "agents": set(),
                "models": set(),
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "estimated_usd": 0.0,
                "last_cache_hit_rate": None,
                "_last_row_index": 0,
            },
        )
        group["run_count"] += 1
        run_id = str(row.get("id") or "")
        group["first_run_id"] = group["first_run_id"] or run_id
        group["last_run_id"] = run_id
        group["last_created_at_et"] = str(row.get("created_at_et") or row.get("created_at") or "")
        group["agents"].add(str(row.get("agent_name") or ""))
        model_label = _model_label(model) or str(row.get("model") or "")
        if model_label:
            group["models"].add(model_label)
        group["input_tokens"] += _int_value(usage.get("input_tokens"))
        group["cached_input_tokens"] += _int_value(usage.get("cached_input_tokens"))
        group["output_tokens"] += _int_value(usage.get("output_tokens"))
        group["estimated_usd"] += (
            _float_value(cost.get("estimated_usd", cost.get("amount_usd"))) or 0.0
        )
        group["last_cache_hit_rate"] = usage.get("cache_hit_rate")
        group["_last_row_index"] = _int_value(run_id)

    session_groups: list[dict[str, Any]] = []
    for group in groups.values():
        input_tokens = group["input_tokens"]
        cache_hit_rate = (
            round(group["cached_input_tokens"] / input_tokens, 4) if input_tokens else None
        )
        session_groups.append(
            {
                "session_id_hash": group["session_id_hash"],
                "session_scope": group["session_scope"],
                "run_count": group["run_count"],
                "first_run_id": group["first_run_id"],
                "last_run_id": group["last_run_id"],
                "last_created_at_et": group["last_created_at_et"],
                "agents": sorted(value for value in group["agents"] if value),
                "models": sorted(value for value in group["models"] if value),
                "aggregate_cache_hit_rate": cache_hit_rate,
                "last_cache_hit_rate": group["last_cache_hit_rate"],
                "input_tokens": group["input_tokens"],
                "cached_input_tokens": group["cached_input_tokens"],
                "output_tokens": group["output_tokens"],
                "estimated_usd": group["estimated_usd"],
                "_last_row_index": group["_last_row_index"],
            }
        )
    minimum_runs = max(1, int(min_runs or 1))
    session_groups = [
        group for group in session_groups if int(group.get("run_count") or 0) >= minimum_runs
    ]
    session_groups.sort(
        key=lambda group: (
            _int_value(group.get("_last_row_index")),
            str(group.get("last_created_at_et") or ""),
        ),
        reverse=True,
    )
    bounded_limit = max(1, int(limit or 10))
    return [
        {key: value for key, value in group.items() if key != "_last_row_index"}
        for group in session_groups[:bounded_limit]
    ]


def render_sdk_session_groups_markdown(groups: list[dict[str, Any]]) -> str:
    """Render recent SDK session groups without raw session or Slack identifiers."""

    lines = [
        "# SDK Session Groups",
        "",
        "| Session Hash | Scope | Runs | First Run | Last Run | Agents | Cache Hit | Estimated | Last Created |",
        "| --- | --- | ---: | ---: | ---: | --- | ---: | ---: | --- |",
    ]
    if not groups:
        lines.append("| none |  |  |  |  |  |  |  |  |")
        return "\n".join(lines)
    for group in groups:
        lines.append(
            "| "
            + " | ".join(
                [
                    _clean(group.get("session_id_hash")),
                    _clean(group.get("session_scope")),
                    _clean(group.get("run_count")),
                    _clean(group.get("first_run_id")),
                    _clean(group.get("last_run_id")),
                    ", ".join(_clean(value) for value in group.get("agents", [])),
                    _clean(group.get("aggregate_cache_hit_rate")),
                    _money(group.get("estimated_usd")),
                    _clean(group.get("last_created_at_et")),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def load_work_item_cost_summary(
    *,
    database_url: str | None = None,
    work_item_id: str = "",
    actual_usd: float | str | None = None,
    actual_source: str = "operator_openai_platform",
    actual_reference_id: str = "",
) -> dict[str, Any]:
    """Summarize WorkItem-level retrieval and SDK usage events for Slack cost checks."""

    store = SQLiteStore(database_url or database_url_from_env())
    rows = store.fetch_all("work_item_events")
    selected_work_item_id = str(work_item_id or "").strip() or _latest_usage_work_item_id(rows)
    selected = [
        row
        for row in rows
        if str(row.get("work_item_id") or "") == selected_work_item_id
        and str(row.get("event_type") or "")
        in {"workflow_retrieval_usage", "workflow_sdk_usage", "manager_loop_efficiency"}
    ]
    run_control_rows = [
        row
        for row in rows
        if str(row.get("work_item_id") or "") == selected_work_item_id
        and str(row.get("event_type") or "") == "advance_started"
    ]
    actual_rows = [
        row
        for row in rows
        if str(row.get("work_item_id") or "") == selected_work_item_id
        and str(row.get("event_type") or "") == "workflow_actual_cost"
    ]
    retrieval_events = [
        _loads_json_object(row.get("metadata_json"))
        for row in selected
        if str(row.get("event_type") or "") == "workflow_retrieval_usage"
    ]
    sdk_events = [
        _loads_json_object(row.get("metadata_json"))
        for row in selected
        if str(row.get("event_type") or "") == "workflow_sdk_usage"
    ]
    manager_events = [
        _loads_json_object(row.get("metadata_json"))
        for row in selected
        if str(row.get("event_type") or "") == "manager_loop_efficiency"
    ]
    run_control_events = [_loads_json_object(row.get("metadata_json")) for row in run_control_rows]
    retrieval = _summarize_retrieval_usage_events(retrieval_events)
    sdk = _summarize_workflow_sdk_usage_events(sdk_events)
    run_controls = _summarize_workflow_run_controls(run_control_events)
    estimated = round(
        float(retrieval.get("estimated_usd") or 0.0)
        + float(sdk.get("estimated_usd") or 0.0),
        8,
    )
    comparison = {}
    if actual_usd is not None:
        comparison = compare_estimated_to_actual_cost(
            cost={
                "estimated_usd": estimated,
                "source": "workflow_usage_events",
            },
            actual_usd=actual_usd,
            source=actual_source,
            reference_id=actual_reference_id,
        )
    elif actual_rows:
        comparison = _loads_json_object(actual_rows[-1].get("metadata_json"))
    return {
        "available": bool(selected_work_item_id and (retrieval_events or sdk_events)),
        "work_item_id": selected_work_item_id,
        "event_count": len(selected),
        "retrieval_event_count": len(retrieval_events),
        "sdk_event_count": len(sdk_events),
        "actual_event_count": len(actual_rows),
        "manager_loop": manager_events[-1] if manager_events else {},
        "run_controls": run_controls,
        "retrieval": retrieval,
        "sdk": sdk,
        "estimated_usd": estimated,
        "actual_comparison": comparison,
        "diagnosis": _workflow_cost_diagnosis(
            retrieval=retrieval,
            sdk=sdk,
            manager_loop=manager_events[-1] if manager_events else {},
            run_controls=run_controls,
            actual_comparison=comparison,
        ),
    }


def annotate_actual_cost_for_work_item(
    *,
    database_url: str | None = None,
    work_item_id: str = "",
    actual_usd: float | str,
    actual_source: str = "operator_openai_platform",
    actual_reference_id: str = "",
) -> dict[str, Any]:
    """Persist an operator-provided platform actual cost on a WorkItem."""

    summary = load_work_item_cost_summary(
        database_url=database_url,
        work_item_id=work_item_id,
        actual_usd=actual_usd,
        actual_source=actual_source,
        actual_reference_id=actual_reference_id,
    )
    selected_work_item_id = str(summary.get("work_item_id") or "").strip()
    if not selected_work_item_id:
        raise ValueError("No WorkItem with usage events was found for actual-cost annotation.")
    comparison = _dict_at(summary, "actual_comparison")
    if not comparison:
        raise ValueError("Could not compute actual-cost comparison for WorkItem.")
    store = SQLiteStore(database_url or database_url_from_env())
    store.save_work_item_event(
        selected_work_item_id,
        WorkItemEvent(
            event_type="workflow_actual_cost",
            actor="operator",
            summary="Recorded OpenAI Platform actual cost for WorkItem cost comparison.",
            metadata=comparison,
        ),
    )
    return {
        "work_item_id": selected_work_item_id,
        "actual_comparison": comparison,
    }


def render_work_item_cost_summary_markdown(summary: dict[str, Any]) -> str:
    """Render a concise WorkItem-level cost and usage report."""

    retrieval = _dict_at(summary, "retrieval")
    sdk = _dict_at(summary, "sdk")
    manager = _dict_at(summary, "manager_loop")
    run_controls = _dict_at(summary, "run_controls")
    diagnosis = _dict_at(summary, "diagnosis")
    lines = [
        "# WorkItem Cost Summary",
        "",
        f"- WorkItem: {_clean(summary.get('work_item_id')) or 'n/a'}",
        f"- Usage events available: {_yes_no(bool(summary.get('available')))}",
        f"- Retrieval events: {_clean(summary.get('retrieval_event_count'))}",
        f"- SDK events: {_clean(summary.get('sdk_event_count'))}",
        f"- Estimated workflow USD: {_money(summary.get('estimated_usd'))}",
    ]
    if manager:
        lines.extend(
            [
                f"- Manager steps: {_clean(manager.get('step_count'))}",
                f"- Repair count: {_clean(manager.get('repair_count'))}",
                f"- Final synthesis executed: {_yes_no(bool(manager.get('final_synthesis_executed')))}",
            ]
        )
    if run_controls.get("available"):
        lines.extend(
            [
                f"- Cost profile: {_clean(run_controls.get('latest_cost_profile'))}",
                f"- Hosted web-search cap: {_clean(run_controls.get('latest_hosted_web_search_max_calls'))}",
                f"- Repair allowed: {_yes_no(bool(run_controls.get('latest_allow_manager_loop_repair')))}",
                f"- Contact enrichment: {_yes_no(bool(run_controls.get('latest_include_contact_enrichment')))}",
                f"- Reuse existing research: {_yes_no(bool(run_controls.get('latest_reuse_existing_research')))}",
            ]
        )
    lines.extend(
        [
            "",
            "## Retrieval",
            "",
            f"- Query count: {_clean(retrieval.get('query_count'))}",
            f"- Raw result count: {_clean(retrieval.get('raw_search_result_count'))}",
            f"- Hosted web-search calls: {_clean(retrieval.get('agents_web_search_calls'))}",
            f"- SearXNG requests: {_clean(retrieval.get('searxng_requests'))}",
            f"- Retrieval estimated USD: {_money(retrieval.get('estimated_usd'))}",
        ]
    )
    lines.extend(
        [
            "",
            "## SDK",
            "",
            f"- Agents: {_clean(', '.join(sdk.get('agents') or []))}",
            f"- Providers: {_clean(', '.join(sdk.get('providers') or []))}",
            f"- Models: {_clean(', '.join(sdk.get('models') or []))}",
            f"- Input tokens: {_clean(sdk.get('input_tokens'))}",
            f"- Cached input tokens: {_clean(sdk.get('cached_input_tokens'))}",
            f"- Cache hit rate: {_clean(sdk.get('cache_hit_rate'))}",
            f"- Output tokens: {_clean(sdk.get('output_tokens'))}",
            f"- Reasoning output tokens: {_clean(sdk.get('reasoning_output_tokens'))}",
            f"- SDK estimated USD: {_money(sdk.get('estimated_usd'))}",
        ]
    )
    comparison = _dict_at(summary, "actual_comparison")
    if comparison:
        lines.extend(
            [
                "",
                "## Platform Comparison",
                "",
                f"- Estimated USD: {_money(comparison.get('estimated_usd'))}",
                f"- Actual USD: {_money(comparison.get('actual_usd'))}",
                f"- Actual minus estimate USD: {_money(comparison.get('actual_minus_estimate_usd'))}",
                f"- Estimate coverage: {_format_percent(comparison.get('estimate_coverage_rate'))}",
                f"- Source: {_clean(comparison.get('source'))}",
                f"- Reference: {_clean(comparison.get('reference_id'))}",
            ]
        )
    if diagnosis:
        lines.extend(["", "## Diagnosis", "", f"- Status: {_clean(diagnosis.get('status'))}"])
        for item in diagnosis.get("notes") or []:
            lines.append(f"- {_clean(item)}")
    return "\n".join(lines).strip()


def _latest_usage_work_item_id(rows: list[dict[str, Any]]) -> str:
    for row in reversed(rows):
        if str(row.get("event_type") or "") in {
            "workflow_retrieval_usage",
            "workflow_sdk_usage",
            "manager_loop_efficiency",
        }:
            return str(row.get("work_item_id") or "")
    return ""


def _summarize_retrieval_usage_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    provider_usage: dict[str, dict[str, Any]] = {}
    query_count = 0
    raw_result_count = 0
    for event in events:
        query_count += _int_value(event.get("query_count"))
        raw_result_count += _int_value(event.get("raw_search_result_count"))
        raw_usage = event.get("provider_usage")
        if not isinstance(raw_usage, dict):
            continue
        for provider_name, usage in raw_usage.items():
            if not isinstance(usage, dict):
                continue
            target = provider_usage.setdefault(
                str(provider_name),
                {
                    "requests_attempted": 0,
                    "requests_succeeded": 0,
                    "raw_result_count": 0,
                    "credits_used": 0,
                    "input_tokens": 0,
                    "cached_input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_output_tokens": 0,
                    "estimated_usd": 0.0,
                },
            )
            for key in (
                "requests_attempted",
                "requests_succeeded",
                "raw_result_count",
                "credits_used",
                "input_tokens",
                "cached_input_tokens",
                "output_tokens",
                "reasoning_output_tokens",
            ):
                target[key] += _int_value(usage.get(key))
            target["estimated_usd"] = round(
                float(target["estimated_usd"]) + (_float_value(usage.get("estimated_usd")) or 0.0),
                8,
            )
    aggregate = _provider_usage_aggregate(provider_usage)
    return {
        "query_count": query_count,
        "raw_search_result_count": raw_result_count,
        "provider_usage": provider_usage,
        "searxng_requests": _int_value(
            provider_usage.get("searxng", {}).get("requests_attempted")
        ),
        "agents_web_search_calls": _int_value(
            provider_usage.get("agents-web-search", {}).get("requests_succeeded")
        ),
        **aggregate,
    }


def _summarize_workflow_sdk_usage_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "total_tokens": 0,
        "estimated_usd": 0.0,
    }
    agents: set[str] = set()
    providers: set[str] = set()
    models: set[str] = set()
    cache_rates: list[float] = []
    for event in events:
        agent_name = str(event.get("agent_name") or "").strip()
        if agent_name:
            agents.add(agent_name)
        event_usage = event.get("usage") if isinstance(event.get("usage"), dict) else {}
        event_cost = event.get("cost") if isinstance(event.get("cost"), dict) else {}
        pricing_provider = str(event_cost.get("pricing_provider") or "").strip()
        pricing_model = str(event_cost.get("pricing_model") or "").strip()
        if pricing_provider:
            providers.add(pricing_provider)
        if pricing_model:
            models.add(pricing_model)
        for key in (
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        ):
            usage[key] += _int_value(event_usage.get(key))
        rate = _float_value(event_usage.get("cache_hit_rate"))
        if rate is not None:
            cache_rates.append(rate)
        usage["estimated_usd"] = round(
            float(usage["estimated_usd"])
            + (
                _float_value(event_cost.get("estimated_usd", event_cost.get("amount_usd")))
                or 0.0
            ),
            8,
        )
    input_tokens = int(usage["input_tokens"])
    cached_input_tokens = int(usage["cached_input_tokens"])
    usage["cache_hit_rate"] = (
        round(cached_input_tokens / input_tokens, 4)
        if input_tokens
        else (cache_rates[-1] if cache_rates else None)
    )
    usage["agents"] = sorted(agents)
    usage["providers"] = sorted(providers)
    usage["models"] = sorted(models)
    return usage


def _summarize_workflow_run_controls(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {"available": False, "advance_started_count": 0}
    latest = events[-1]
    cost_profiles = sorted(
        {
            str(event.get("cost_profile") or "").strip()
            for event in events
            if str(event.get("cost_profile") or "").strip()
        }
    )
    hosted_caps = [
        event.get("hosted_web_search_max_calls")
        for event in events
        if "hosted_web_search_max_calls" in event
    ]
    return {
        "available": True,
        "advance_started_count": len(events),
        "cost_profiles": cost_profiles,
        "latest_cost_profile": str(latest.get("cost_profile") or "").strip(),
        "latest_allow_manager_loop_repair": bool(latest.get("allow_manager_loop_repair")),
        "latest_include_contact_enrichment": bool(latest.get("include_contact_enrichment")),
        "latest_hosted_web_search_max_calls": latest.get("hosted_web_search_max_calls"),
        "latest_reuse_existing_research": bool(latest.get("reuse_existing_research")),
        "hosted_web_search_max_calls_values": hosted_caps,
    }


def _provider_usage_aggregate(provider_usage: dict[str, dict[str, Any]]) -> dict[str, Any]:
    aggregate = {
        "requests_attempted": 0,
        "requests_succeeded": 0,
        "raw_result_count": 0,
        "credits_used": 0,
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
        "estimated_usd": 0.0,
    }
    for usage in provider_usage.values():
        for key in (
            "requests_attempted",
            "requests_succeeded",
            "raw_result_count",
            "credits_used",
            "input_tokens",
            "cached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
        ):
            aggregate[key] += _int_value(usage.get(key))
        aggregate["estimated_usd"] = round(
            float(aggregate["estimated_usd"]) + (_float_value(usage.get("estimated_usd")) or 0.0),
            8,
        )
    input_tokens = int(aggregate["input_tokens"])
    cached_input_tokens = int(aggregate["cached_input_tokens"])
    aggregate["cache_hit_rate"] = (
        round(cached_input_tokens / input_tokens, 4) if input_tokens else None
    )
    return aggregate


def _workflow_cost_diagnosis(
    *,
    retrieval: dict[str, Any],
    sdk: dict[str, Any],
    manager_loop: dict[str, Any],
    run_controls: dict[str, Any],
    actual_comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    notes: list[str] = []
    if run_controls.get("available"):
        latest_cost_profile = str(run_controls.get("latest_cost_profile") or "").strip()
        if latest_cost_profile not in SLACK_COST_CONTROLLED_PROFILES:
            notes.append(
                "Run did not use a Slack cost-controlled profile; restart or inspect the invoking path."
            )
        if run_controls.get("latest_hosted_web_search_max_calls") is None:
            notes.append("Hosted web-search cap was not set for this run.")
        non_mini_models = _non_mini_openai_models(sdk)
        if latest_cost_profile in SLACK_COST_CONTROLLED_PROFILES and non_mini_models:
            notes.append(
                "Slack cost-controlled run used non-mini OpenAI model(s): "
                f"{', '.join(non_mini_models)}."
            )
    if _int_value(retrieval.get("query_count")) > 20:
        notes.append("Search query fanout is still high for a normal Slack run.")
    if (
        str(run_controls.get("latest_cost_profile") or "")
        in {"slack_research_balanced", "slack_opportunity_balanced", "slack_research_deep"}
        and _int_value(retrieval.get("query_count")) == 0
        and not _float_value(retrieval.get("estimated_usd"))
    ):
        notes.append("No retrieval usage was recorded for a research/search-oriented run.")
    if _int_value(retrieval.get("agents_web_search_calls")) > 1:
        notes.append("Hosted web-search calls exceeded the conservative Slack target of 0-1.")
    if _int_value(manager_loop.get("repair_count")) > 0:
        notes.append("A manager-loop repair pass ran; this can roughly double retrieval cost.")
    if not sdk.get("input_tokens"):
        notes.append("No workflow SDK token usage was recorded.")
    elif _float_value(sdk.get("cache_hit_rate")) in {None, 0.0}:
        notes.append("SDK usage was recorded but cached input was low or unavailable.")
    comparison = actual_comparison if isinstance(actual_comparison, dict) else {}
    coverage = _float_value(comparison.get("estimate_coverage_rate"))
    actual_gap = _float_value(comparison.get("actual_minus_estimate_usd"))
    if comparison.get("available") and coverage is not None and coverage < 0.8:
        notes.append(
            "Local estimate covered less than 80% of the OpenAI Platform actual; "
            "inspect built-in tool charges, retries, pricing-table drift, or platform "
            "billing-window mismatch."
        )
    elif comparison.get("available") and actual_gap is not None and actual_gap > 0.02:
        notes.append(
            "OpenAI Platform actual exceeded the local estimate by more than two cents."
        )
    if not notes:
        notes.append("Workflow usage is within the conservative Slack cost expectations.")
    return {
        "status": "pass" if len(notes) == 1 and notes[0].startswith("Workflow usage") else "warn",
        "notes": notes,
    }


def _non_mini_openai_models(sdk: dict[str, Any]) -> list[str]:
    providers = {str(provider).strip().lower() for provider in (sdk.get("providers") or [])}
    if "openai" not in providers:
        return []
    models = sorted(
        {
            str(model).strip()
            for model in (sdk.get("models") or [])
            if str(model).strip()
        }
    )
    return [
        model
        for model in models
        if model.lower().startswith("gpt-") and "-mini" not in model.lower()
    ]


def _request_cache_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    caches = [
        record.get("request_cache")
        for record in records
        if isinstance(record.get("request_cache"), dict)
    ]
    if not caches:
        return {
            "available": False,
            "note": "No request-cache fingerprints were present in the supplied records.",
        }
    static_prefixes = _distinct(caches, "static_prefix_sha256")
    instructions = _distinct(caches, "instructions_sha256")
    tools = _distinct(caches, "tool_names_sha256")
    schemas = _distinct(caches, "output_schema_sha256")
    session_hashes = _distinct(caches, "session_id_hash")
    dynamic_chars = [_int_value(cache.get("dynamic_prompt_chars")) for cache in caches]
    return {
        "available": True,
        "static_prefix_stable": len(static_prefixes) <= 1,
        "instructions_stable": len(instructions) <= 1,
        "tool_order_stable": len(tools) <= 1,
        "output_schema_stable": len(schemas) <= 1,
        "session_attached_all": all(bool(cache.get("session_attached")) for cache in caches),
        "session_hash_stable": len(session_hashes) <= 1,
        "session_hash_count": len(session_hashes),
        "first_dynamic_prompt_chars": dynamic_chars[0] if dynamic_chars else 0,
        "last_dynamic_prompt_chars": dynamic_chars[-1] if dynamic_chars else 0,
        "dynamic_prompt_chars_delta": (
            dynamic_chars[-1] - dynamic_chars[0] if len(dynamic_chars) >= 2 else 0
        ),
        "static_prefix_count": len(static_prefixes),
        "note": (
            "A static-prefix count above 1 means the cacheable prefix changed between "
            "records; inspect prompts, tool ordering, schemas, or agent selection."
        ),
    }


def _compact_record(record: dict[str, Any]) -> dict[str, Any]:
    usage = _dict_at(record, "usage")
    cost = _dict_at(record, "cost")
    request_cache = _dict_at(record, "request_cache")
    return {
        "run_id": record.get("run_id", ""),
        "agent_name": record.get("agent_name", ""),
        "model": record.get("model", ""),
        "input_tokens": _int_value(usage.get("input_tokens")),
        "cached_input_tokens": _int_value(usage.get("cached_input_tokens")),
        "cache_hit_rate": usage.get("cache_hit_rate"),
        "output_tokens": _int_value(usage.get("output_tokens")),
        "reasoning_output_tokens": _int_value(usage.get("reasoning_output_tokens")),
        "estimated_usd": cost.get("estimated_usd", cost.get("amount_usd")),
        "actual_usd": cost.get("actual_usd"),
        "static_prefix_sha256": request_cache.get("static_prefix_sha256", ""),
        "session_id_hash": request_cache.get("session_id_hash", ""),
        "session_scope": request_cache.get("session_scope", ""),
        "dynamic_prompt_chars": request_cache.get("dynamic_prompt_chars"),
        "session_attached": bool(request_cache.get("session_attached")),
    }


def _select_rows(
    rows: list[dict[str, Any]],
    *,
    run_ids: list[str] | None,
    limit: int,
    session_hash: str = "",
    same_session_as_run_id: str = "",
    latest_session: bool = False,
    latest_repeated_session: bool = False,
    min_session_runs: int = 2,
) -> list[dict[str, Any]]:
    if run_ids:
        wanted = {str(run_id) for run_id in run_ids}
        return [row for row in rows if str(row.get("id") or "") in wanted]
    bounded_limit = max(1, int(limit or 2))
    resolved_session_hash = str(session_hash or "").strip()
    if same_session_as_run_id and not resolved_session_hash:
        anchor = next(
            (row for row in rows if str(row.get("id") or "") == str(same_session_as_run_id)),
            None,
        )
        resolved_session_hash = _row_session_hash(anchor) if anchor else ""
    if latest_session and not resolved_session_hash:
        for row in reversed(rows):
            resolved_session_hash = _row_session_hash(row)
            if resolved_session_hash:
                break
    if latest_repeated_session and not resolved_session_hash:
        resolved_session_hash = _latest_session_hash_with_min_runs(
            rows,
            min_runs=max(2, int(min_session_runs or 2)),
        )
    if resolved_session_hash:
        matching_rows = [row for row in rows if _row_session_hash(row) == resolved_session_hash]
        return matching_rows[-bounded_limit:]
    return rows[-bounded_limit:]


def _row_session_hash(row: dict[str, Any] | None) -> str:
    if not row:
        return ""
    payload = _loads_json_object(row.get("output_json"))
    request_cache = _dict_at(payload, "_sdk_request_cache") or _dict_at(payload, "request_cache")
    return str(request_cache.get("session_id_hash") or "").strip()


def _latest_session_hash_with_min_runs(rows: list[dict[str, Any]], *, min_runs: int) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        session_hash = _row_session_hash(row)
        if session_hash:
            counts[session_hash] = counts.get(session_hash, 0) + 1
    for row in reversed(rows):
        session_hash = _row_session_hash(row)
        if session_hash and counts.get(session_hash, 0) >= min_runs:
            return session_hash
    return ""


def _distinct(values: list[dict[str, Any]], key: str) -> list[str]:
    return sorted({str(value.get(key) or "") for value in values if value.get(key)})


def _dict_at(value: Any, key: str) -> dict[str, Any]:
    if isinstance(value, dict):
        item = value.get(key)
        return item if isinstance(item, dict) else {}
    return {}


def _loads_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _int_value(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _float_value(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").strip()


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _money(value: Any) -> str:
    if value is None or value == "":
        return "n/a"
    try:
        return f"${float(value):.6f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return _clean(value)


def _format_percent(value: Any) -> str:
    number = _float_value(value)
    if number is None:
        return "n/a"
    percent = number * 100 if -1 <= number <= 1 else number
    return f"{percent:.2f}".rstrip("0").rstrip(".") + "%"


def _model_label(model: Any) -> str:
    if not isinstance(model, dict):
        return ""
    provider = str(model.get("provider") or "").strip()
    name = str(model.get("name") or model.get("model") or "").strip()
    if provider and name:
        return f"{provider}/{name}"
    return name or provider
