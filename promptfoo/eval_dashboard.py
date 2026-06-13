"""Static dashboard rendering for the local Promptfoo eval database."""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import html
import json
import csv
import io
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

from promptfoo.eval_database import DEFAULT_EVAL_DB, eval_case_status, list_eval_cases
from promptfoo.eval_urls import eval_dashboard_case_url, eval_review_case_url
from promptfoo.human_review import SCORE_DIMENSIONS

DEFAULT_DASHBOARD_PATH = Path(".keystone/promptfoo/dashboard.html")
DEFAULT_START_CASE_ID = "slack_company_research_001"
DEFAULT_PROMPTFOO_TEST_PATHS = (
    Path("promptfoo/tests/slack_research.yaml"),
    Path("promptfoo/tests/slack_retrieval_synthesis.yaml"),
    Path("promptfoo/tests/slack_tool_safety.yaml"),
    Path("promptfoo/tests/slack_agent_coverage.yaml"),
    Path("promptfoo/tests/slack_agent_expansion_15.yaml"),
)
EVAL_CASE_TARGET_PER_AGENT = 15
CORE_EVAL_AGENTS = (
    "opportunity_scout",
    "business_research_analyst",
    "chief_of_staff",
    "gmail_triage",
    "outreach_composer",
    "orchestrator",
)
WEB_RETRIEVAL_DIMENSIONS = {
    "search",
    "search_budget",
    "deeper_search",
    "retrieval",
    "retrieval_boundary",
    "retrieval_policy",
    "retrieval_precision",
}
INFORMATION_QUALITY_DIMENSIONS = {
    "claim_extraction",
    "claim_mapping",
    "evidence_mapping",
    "hallucination_control",
    "source_provided",
    "source_relevance",
    "source_sufficiency",
    "source_visibility",
}


def render_dashboard(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    output_path: str | Path = DEFAULT_DASHBOARD_PATH,
    limit: int = 500,
) -> Path:
    """Render the merged Promptfoo/Slack/human-review eval dashboard."""

    db_path = Path(database_path)
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dashboard_payload(database_path=db_path, limit=limit)
    out_path.write_text(_dashboard_html(payload), encoding="utf-8")
    return out_path


def dashboard_payload(
    *,
    database_path: str | Path = DEFAULT_EVAL_DB,
    limit: int = 500,
) -> dict[str, Any]:
    """Return the merged dashboard data model used by HTML, JSON, and CSV views."""

    db_path = Path(database_path)
    rows = _merged_eval_case_rows(db_path, limit=limit)
    cases = _with_prompt_numbers([
        _case_dashboard_record(row, database_path=db_path)
        for row in rows
    ])
    eval_runs = _latest_eval_runs(db_path)
    summary = _summary(cases, eval_runs)
    return {
        "database_path": str(db_path),
        "eval_runs": eval_runs,
        "analysis": _promptfoo_analysis(db_path),
        "cases": cases,
        "summary": summary,
        "workflow_readiness": _workflow_readiness(cases, summary),
        "score_dimensions": list(SCORE_DIMENSIONS),
    }


def dashboard_case_export_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten dashboard cases into the database/export view shape."""

    rows: list[dict[str, Any]] = []
    for case in payload.get("cases") or []:
        machine_status = (
            "pass"
            if case.get("promptfoo_success") is True
            else "fail"
            if case.get("promptfoo_success") is False
            else "not_run"
        )
        slack_status = "tested" if int(case.get("slack_run_count") or 0) > 0 else "not_run"
        human_status = "reviewed" if case.get("human_average") is not None else "unreviewed"
        human_scores = case.get("human_scores") or {}
        score_fields = {
            f"human_{dimension}": human_scores.get(dimension, "")
            for dimension in SCORE_DIMENSIONS
        }
        rows.append(
            {
                "prompt_number": _prompt_number_label(str(case.get("prompt_number") or "")),
                "case_id": case.get("case_id") or "",
                "agent": case.get("agent") or "",
                "dimensions": ", ".join(case.get("dimensions") or []),
                "prompt": case.get("user_input") or "",
                "latest_response": case.get("response_text") or "",
                "machine_status": machine_status,
                "machine_score_5": _score_out_of_five(case.get("promptfoo_score"), machine=True),
                "promptfoo_eval_id": case.get("promptfoo_eval_id") or "",
                "slack_status": slack_status,
                "slack_run_count": int(case.get("slack_run_count") or 0),
                "latest_slack_run_id": case.get("latest_slack_run_id") or "",
                "latest_slack_thread_ts": case.get("latest_slack_thread_ts") or "",
                "human_status": human_status,
                "human_average_5": _score_out_of_five(case.get("human_average")),
                "human_safety": case.get("human_safety") or "",
                **score_fields,
                "human_notes": case.get("human_notes") or "",
                "analysis_excluded": "yes" if case.get("analysis_excluded") else "no",
                "analysis_exclusion_reason": case.get("analysis_exclusion_reason") or "",
                "updated_at": case.get("updated_at") or "",
            }
        )
    return rows


def dashboard_case_export_csv(payload: dict[str, Any]) -> str:
    """Return CSV for the merged eval case database view."""

    rows = dashboard_case_export_rows(payload)
    fieldnames = [
        "prompt_number",
        "case_id",
        "agent",
        "dimensions",
        "prompt",
        "latest_response",
        "machine_status",
        "machine_score_5",
        "promptfoo_eval_id",
        "slack_status",
        "slack_run_count",
        "latest_slack_run_id",
        "latest_slack_thread_ts",
        "human_status",
        "human_average_5",
        "human_safety",
        *[f"human_{dimension}" for dimension in SCORE_DIMENSIONS],
        "human_notes",
        "analysis_excluded",
        "analysis_exclusion_reason",
        "updated_at",
    ]
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def render_review_form(
    *,
    case_id: str,
    database_path: str | Path = DEFAULT_EVAL_DB,
    output_path: str | Path | None = None,
    limit: int = 500,
) -> str:
    """Render a standalone human-review intake form for one eval case."""

    db_path = Path(database_path)
    rows = _merged_eval_case_rows(db_path, limit=limit)
    cases = _with_prompt_numbers([_case_dashboard_record(row, database_path=db_path) for row in rows])
    normalized = str(case_id or "").strip()
    selected = next((case for case in cases if case["case_id"] == normalized), None)
    if selected is None:
        selected = {
            "case_id": normalized,
            "agent": "",
            "dimensions": [],
            "human_scores": {},
            "human_notes": "",
            "human_safety": "",
            "latest_slack_run_id": "",
            "latest_slack_thread_ts": "",
            "user_input": "",
            "response_text": "",
            "review_url": eval_review_case_url(normalized),
        }
    payload = {
        "database_path": str(db_path),
        "case": selected,
        "score_dimensions": list(SCORE_DIMENSIONS),
    }
    html_text = _review_html(payload)
    if output_path:
        out_path = Path(output_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(html_text, encoding="utf-8")
    return html_text


def _case_dashboard_record(row: dict[str, Any], *, database_path: Path) -> dict[str, Any]:
    case_id = str(row.get("case_id") or "")
    status = eval_case_status(case_id, database_path=database_path)
    row_user_input = str(row.get("user_input") or "")
    status_user_input = str((status.get("case") or {}).get("user_input") or "")
    prompt_changed_since_promptfoo = bool(
        row.get("_promptfoo_stale_for_prompt")
        or (row_user_input and status_user_input and row_user_input != status_user_input)
    )
    latest_promptfoo = {} if prompt_changed_since_promptfoo else status.get("latest_promptfoo") or {}
    latest_human = status.get("latest_human_review") or {}
    latest_slack = (status.get("slack_runs") or [{}])[0] if status.get("slack_runs") else {}
    user_input = row_user_input or status_user_input
    return {
        "case_id": case_id,
        "agent": str(row.get("agent_under_test") or ""),
        "dimensions": _split_dimensions(str(row.get("eval_dimensions") or "")),
        "source": row.get("source") or "",
        "promptfoo_success": None if prompt_changed_since_promptfoo else _bool_or_none(row.get("latest_promptfoo_success")),
        "promptfoo_score": None if prompt_changed_since_promptfoo else row.get("latest_promptfoo_score"),
        "promptfoo_eval_id": "" if prompt_changed_since_promptfoo else row.get("latest_eval_id") or "",
        "promptfoo_reason": latest_promptfoo.get("reason") or latest_promptfoo.get("failure_reason") or "",
        "analysis_excluded": bool(
            0
            if prompt_changed_since_promptfoo
            else row.get("latest_analysis_excluded") or latest_promptfoo.get("analysis_excluded")
        ),
        "analysis_exclusion_reason": (
            "" if prompt_changed_since_promptfoo else row.get("latest_analysis_exclusion_reason")
        )
        or latest_promptfoo.get("analysis_exclusion_reason")
        or "",
        "human_average": row.get("latest_human_average"),
        "human_safety": row.get("latest_human_safety") or "",
        "human_created_at": row.get("latest_human_created_at") or "",
        "human_scores": latest_human.get("scores") or {},
        "human_notes": latest_human.get("notes") or "",
        "slack_run_count": int(status.get("slack_run_count") or 0),
        "latest_slack_run_id": latest_slack.get("run_id") or "",
        "latest_slack_thread_ts": latest_slack.get("slack_thread_ts") or "",
        "latest_slack_created_at": latest_slack.get("created_at") or "",
        "latest_slack_summary": latest_slack.get("result_summary") or "",
        "user_input": user_input,
        "response_text": _case_response_text(latest_promptfoo, latest_slack),
        "review_url": eval_review_case_url(case_id),
        "updated_at": row.get("updated_at") or "",
    }


def _with_prompt_numbers(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    numbered: list[dict[str, Any]] = []
    for case in cases:
        agent = str(case.get("agent") or "unknown")
        counts[agent] += 1
        enriched = dict(case)
        case_id = str(case.get("case_id") or "")
        prompt_number = f"_{counts[agent]:03d}"
        enriched["prompt_number"] = prompt_number
        enriched["display_prompt_number"] = _prompt_number_label(prompt_number)
        enriched["display_case_id"] = _display_case_id(case_id, prompt_number)
        numbered.append(enriched)
    return numbered


def _prompt_number_label(prompt_number: str) -> str:
    return prompt_number.lstrip("_") or prompt_number


def _display_case_id(case_id: str, prompt_number: str) -> str:
    suffix = prompt_number.lstrip("_")
    if not case_id or not suffix:
        return case_id
    stem, separator, tail = case_id.rpartition("_")
    if separator and len(tail) == 3 and tail.isdigit():
        return f"{stem}_{suffix}"
    return case_id


def _merged_eval_case_rows(database_path: Path, *, limit: int) -> list[dict[str, Any]]:
    rows = list_eval_cases(database_path=database_path, limit=limit)
    seed_rows = _seed_eval_case_rows()
    seed_case_ids = {str(seed.get("case_id") or "") for seed in seed_rows if seed.get("case_id")}
    by_case_id = {
        str(row.get("case_id") or ""): dict(row)
        for row in rows
        if not (
            row.get("source") in {"promptfoo", "promptfoo_seed"}
            and str(row.get("case_id") or "")
            and str(row.get("case_id") or "") not in seed_case_ids
        )
    }
    for seed in seed_rows:
        case_id = str(seed.get("case_id") or "")
        if not case_id:
            continue
        if case_id in by_case_id:
            existing_user_input = str(by_case_id[case_id].get("user_input") or "")
            seed_user_input = str(seed.get("user_input") or "")
            prompt_changed = bool(seed_user_input and existing_user_input and seed_user_input != existing_user_input)
            by_case_id[case_id].update(
                {
                    "agent_under_test": seed.get("agent_under_test") or by_case_id[case_id].get("agent_under_test") or "",
                    "eval_dimensions": seed.get("eval_dimensions") or by_case_id[case_id].get("eval_dimensions") or "",
                    "source": seed.get("source") or by_case_id[case_id].get("source") or "",
                    "user_input": seed_user_input or by_case_id[case_id].get("user_input") or "",
                }
            )
            if prompt_changed:
                by_case_id[case_id]["_promptfoo_stale_for_prompt"] = True
        else:
            by_case_id[case_id] = seed
    return sorted(
        by_case_id.values(),
        key=lambda row: (
            str(row.get("agent_under_test") or ""),
            str(row.get("case_id") or ""),
        ),
    )[: max(1, int(limit))]


def _seed_eval_case_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in DEFAULT_PROMPTFOO_TEST_PATHS:
        if not path.exists():
            continue
        rows.extend(_seed_eval_case_rows_from_text(path.read_text(encoding="utf-8")))
    return rows


def _seed_eval_case_rows_from_text(text: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    current: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("- description:"):
            if current.get("case_id"):
                rows.append(_seed_eval_case_row(current))
            current = {}
            continue
        if not line.startswith("    "):
            continue
        stripped = line.strip()
        if ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        if key in {"case_id", "agent_under_test", "eval_dimensions", "user_input"}:
            current[key] = _strip_yaml_scalar(value.strip())
    if current.get("case_id"):
        rows.append(_seed_eval_case_row(current))
    return rows


def _seed_eval_case_row(seed: dict[str, str]) -> dict[str, Any]:
    return {
        "case_id": seed.get("case_id") or "",
        "agent_under_test": seed.get("agent_under_test") or "",
        "eval_dimensions": seed.get("eval_dimensions") or "",
        "source": "promptfoo_seed",
        "updated_at": "",
        "latest_eval_id": "",
        "latest_promptfoo_success": None,
        "latest_promptfoo_score": None,
        "latest_human_average": None,
        "latest_human_safety": "",
        "latest_human_created_at": "",
        "user_input": seed.get("user_input") or "",
    }


def _strip_yaml_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def _summary(cases: list[dict[str, Any]], eval_runs: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(cases)
    seed_case_ids = {str(row.get("case_id") or "") for row in _seed_eval_case_rows()}
    seed_case_total = len(seed_case_ids)
    non_seed_cases = sum(
        1
        for case in cases
        if str(case.get("case_id") or "") not in seed_case_ids
    )
    promptfoo_passed = sum(1 for case in cases if case.get("promptfoo_success") is True)
    promptfoo_failed = sum(1 for case in cases if case.get("promptfoo_success") is False)
    promptfoo_evaluated = promptfoo_passed + promptfoo_failed
    human_scored = [case for case in cases if case.get("human_average") is not None]
    slack_linked = sum(1 for case in cases if int(case.get("slack_run_count") or 0) > 0)
    safety = Counter(str(case.get("human_safety") or "unreviewed") for case in cases)
    agents = Counter(str(case.get("agent") or "unknown") for case in cases)
    seed_agents = Counter(
        str(case.get("agent_under_test") or "unknown")
        for case in _seed_eval_case_rows()
    )
    dimensions: Counter[str] = Counter()
    for case in cases:
        dimensions.update(_dimension_summary_label(dimension) for dimension in (case.get("dimensions") or []))
    human_avg = None
    if human_scored:
        human_avg = round(
            sum(float(case.get("human_average") or 0) for case in human_scored)
            / len(human_scored),
            2,
        )
    machine_scored = [case for case in cases if case.get("promptfoo_score") is not None]
    machine_avg = _average_case_score(machine_scored, "promptfoo_score")
    machine_agent_scores = _agent_score_summary(machine_scored, "promptfoo_score")
    human_agent_scores = _agent_score_summary(human_scored, "human_average")
    coverage = _coverage_summary(seed_agents)
    return {
        "total_cases": total,
        "seed_case_total": seed_case_total,
        "non_seed_cases": non_seed_cases,
        "promptfoo_passed": promptfoo_passed,
        "promptfoo_failed": promptfoo_failed,
        "promptfoo_evaluated": promptfoo_evaluated,
        "promptfoo_pending": total - promptfoo_evaluated,
        "promptfoo_pass_rate": (
            round(promptfoo_passed / promptfoo_evaluated * 100, 1)
            if promptfoo_evaluated
            else 0
        ),
        "latest_eval_average_score": (
            eval_runs[0].get("average_score") if eval_runs and eval_runs[0].get("average_score") is not None
            else machine_avg
        ),
        "machine_average": machine_avg,
        "machine_scored": len(machine_scored),
        "machine_agent_scores": machine_agent_scores,
        "human_reviewed": len(human_scored),
        "human_average": human_avg,
        "human_agent_scores": human_agent_scores,
        "slack_linked": slack_linked,
        "safety": dict(safety),
        "agents": dict(agents),
        "coverage": coverage,
        "coverage_source": "promptfoo seed cases",
        "coverage_target_per_agent": EVAL_CASE_TARGET_PER_AGENT,
        "coverage_complete_agents": sum(1 for item in coverage.values() if item["gap"] == 0),
        "coverage_gap_total": sum(item["gap"] for item in coverage.values()),
        "dimensions": dict(dimensions.most_common(12)),
        "latest_eval_id": eval_runs[0]["eval_id"] if eval_runs else "",
    }


def _workflow_readiness(
    cases: list[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, Any]:
    """Return the local-only Slack eval workflow contract for UI and future hooks."""

    total = len(cases)
    prompt_library = int(summary.get("seed_case_total") or total or 0)
    slack_runs = sum(1 for case in cases if int(case.get("slack_run_count") or 0) > 0)
    recorded_responses = sum(1 for case in cases if _case_has_recorded_response(case))
    human_scorecards = sum(
        1
        for case in cases
        if case.get("human_average") is not None
    )
    analysis_included = sum(
        1
        for case in cases
        if case.get("promptfoo_eval_id") and not case.get("analysis_excluded")
    )
    retrieval_source_cases = sum(
        1
        for case in cases
        if any(
            str(dimension or "") in WEB_RETRIEVAL_DIMENSIONS
            or str(dimension or "") in INFORMATION_QUALITY_DIMENSIONS
            or str(dimension or "") in {"search_quality", "source_quality"}
            for dimension in (case.get("dimensions") or [])
        )
    )
    return {
        "mode": "local_preview",
        "live_api_calls": False,
        "starter_run_plan": _starter_run_plan(cases),
        "counts": {
            "prompt_library_cases": prompt_library,
            "total_cases": total,
            "slack_thread_runs": slack_runs,
            "recorded_responses": recorded_responses,
            "human_scorecards": human_scorecards,
            "analysis_included": analysis_included,
            "retrieval_source_cases": retrieval_source_cases,
        },
        "interactions": [
            {
                "interaction": "Prompt copy",
                "current_behavior": "Local prompt library row only",
                "future_live_trigger": "Human pastes root message into #evals",
                "cost_guardrail": "No API call from dashboard copy",
                "stored_evidence": f"{total} local cases",
                "live_api_call_now": False,
            },
            {
                "interaction": "Agent thread reply",
                "current_behavior": "Previewed from saved response/result text",
                "future_live_trigger": "Slack app mention in the selected thread",
                "cost_guardrail": "One agent run per accepted Slack root prompt",
                "stored_evidence": f"{slack_runs} saved Slack runs",
                "live_api_call_now": False,
            },
            {
                "interaction": "Promptfoo machine summary",
                "current_behavior": "Shows latest imported assertion status and score by case_id",
                "future_live_trigger": "Resolved Slack case has an imported Promptfoo result",
                "cost_guardrail": "Use imported Promptfoo result by case_id; do not rerun Promptfoo from Slack thread",
                "stored_evidence": f"{analysis_included} included machine rows",
                "live_api_call_now": False,
            },
            {
                "interaction": "Retrieval/source evidence",
                "current_behavior": "Shows tags and recorded evidence already in the eval database",
                "future_live_trigger": "Accepted agent run needs source retrieval",
                "cost_guardrail": "Use dry-run fixtures/cache first; cap live retrieval to accepted root run",
                "stored_evidence": f"{retrieval_source_cases} retrieval/source-tagged cases",
                "live_api_call_now": False,
            },
            {
                "interaction": "Human review form open",
                "current_behavior": "Review controls stay disabled until a response exists",
                "future_live_trigger": "Human opens the review form or dashboard scoring panel",
                "cost_guardrail": "No model call; form uses saved case, run id, thread, and response context",
                "stored_evidence": f"{recorded_responses} recorded responses",
                "live_api_call_now": False,
            },
            {
                "interaction": "Human review save",
                "current_behavior": "Writes local SQLite score fields only",
                "future_live_trigger": "Human submits reviewed scores",
                "cost_guardrail": "No model call; no Slack post",
                "stored_evidence": f"{human_scorecards} saved scorecards",
                "live_api_call_now": False,
            },
            {
                "interaction": "Analysis inclusion",
                "current_behavior": "Included unless marked duplicate/problem run",
                "future_live_trigger": "Operator toggles analysis inclusion",
                "cost_guardrail": "No rerun; recalculates from database rows",
                "stored_evidence": f"{analysis_included} included machine rows",
                "live_api_call_now": False,
            },
        ],
    }


def _starter_run_plan(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a cost-safe first Slack eval run plan for the dashboard."""

    selected = next(
        (
            case
            for case in cases
            if str(case.get("case_id") or "") == DEFAULT_START_CASE_ID
        ),
        None,
    )
    if selected is None:
        selected = next(
            (
                case
                for case in cases
                if str(case.get("agent") or "") == "business_research_analyst"
                and str(case.get("user_input") or "").strip()
            ),
            None,
        )
    if selected is None:
        selected = next(
            (
                case
                for case in cases
                if str(case.get("user_input") or "").strip()
            ),
            {},
        )

    case_id = str(selected.get("case_id") or "").strip()
    return {
        "channel": "#evals",
        "case_id": case_id,
        "display_case_id": selected.get("display_case_id") or case_id,
        "prompt_number": selected.get("display_prompt_number") or "",
        "agent": selected.get("agent") or "",
        "paste_text": selected.get("user_input") or "",
        "dashboard_url": eval_dashboard_case_url(case_id) if case_id else "",
        "human_review_url": eval_review_case_url(case_id) if case_id else "",
        "status_reply": "@KNI how is this eval doing?",
        "local_only_now": True,
        "thread_sequence": [
            {
                "speaker": "Human",
                "message": "Root agent ask in #evals using the selected committed prompt.",
                "stored_as": "Slack root message plus resolved case_id",
            },
            {
                "speaker": "KNI agent",
                "message": "Normal agent answer in-thread, matching the ai-agents-workflow style: answer first, source/retrieval notes where relevant, no raw workflow metadata as the main answer.",
                "stored_as": "Slack run response/result",
            },
            {
                "speaker": "KNI eval context",
                "message": "In-thread eval footer with case_id, run_id, Promptfoo machine-check summary when imported, human review form link, and eval dashboard link.",
                "stored_as": "Case/run links for dashboard and review",
            },
            {
                "speaker": "Human",
                "message": "Open the human review form or dashboard scoring panel from the eval footer link.",
                "stored_as": "Reviewer is shown saved prompt, response, machine check, and score fields",
            },
            {
                "speaker": "Human",
                "message": "Submit dimension scores and safety pass/fail in the form.",
                "stored_as": "Human review row in local eval database",
            },
            {
                "speaker": "KNI status",
                "message": "Optional follow-up: @KNI how is this eval doing?",
                "stored_as": "Merged Promptfoo, Slack run, and human-review status without rerunning the agent answer",
            },
        ],
        "expected_live_calls_when_enabled": [
            {
                "step": "Root prompt",
                "expected_call": "One accepted agent run in the Slack thread",
                "cost_guardrail": "Run only the selected case; no dashboard copy action calls the model",
            },
            {
                "step": "Retrieval",
                "expected_call": "Only if the accepted agent run needs source evidence",
                "cost_guardrail": "Use fixtures/cache first and cap live retrieval to that root run",
            },
            {
                "step": "Promptfoo summary",
                "expected_call": "No model call and no Promptfoo rerun",
                "cost_guardrail": "Read the latest imported machine-check row by case_id and post only the summary/link footer",
            },
            {
                "step": "Human review form",
                "expected_call": "No model call",
                "cost_guardrail": "Use the saved response and machine-check row; the reviewer fills the form directly",
            },
            {
                "step": "Human review save",
                "expected_call": "No model call",
                "cost_guardrail": "Write score fields to the local eval database only",
            },
            {
                "step": "Analysis refresh",
                "expected_call": "No model call",
                "cost_guardrail": "Recalculate charts from included database rows",
            },
        ],
    }


def _case_has_recorded_response(case: dict[str, Any]) -> bool:
    return bool(
        str(case.get("response_text") or case.get("latest_slack_summary") or "").strip()
    )


def _dimension_summary_label(dimension: str) -> str:
    value = str(dimension or "").strip()
    if value in WEB_RETRIEVAL_DIMENSIONS:
        return "web_retrieval_quality"
    if value in INFORMATION_QUALITY_DIMENSIONS:
        return "information_quality"
    return value


def _average_case_score(cases: list[dict[str, Any]], key: str) -> float | None:
    values = [float(case[key]) for case in cases if case.get(key) is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 2)


def _agent_score_summary(cases: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    values: dict[str, list[float]] = {}
    for case in cases:
        if case.get(key) is None:
            continue
        agent = str(case.get("agent") or "unknown").strip() or "unknown"
        values.setdefault(agent, []).append(float(case[key]))
    return {
        agent: {"average": round(sum(scores) / len(scores), 2), "count": len(scores)}
        for agent, scores in sorted(values.items())
    }


def _coverage_summary(agent_counts: Counter[str]) -> dict[str, dict[str, int]]:
    agents = sorted(set(CORE_EVAL_AGENTS) | set(agent_counts))
    return {
        agent: {
            "count": int(agent_counts.get(agent, 0)),
            "target": EVAL_CASE_TARGET_PER_AGENT,
            "gap": max(0, EVAL_CASE_TARGET_PER_AGENT - int(agent_counts.get(agent, 0))),
        }
        for agent in agents
    }


def _latest_eval_runs(database_path: Path, limit: int = 8) -> list[dict[str, Any]]:
    if not database_path.exists():
        return []
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                """
                SELECT eval_id, created_at, imported_at, total, successes, failures, errors
                     , average_score, agent_scores_json
                FROM promptfoo_eval_runs
                ORDER BY imported_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [_eval_run_row(dict(row)) for row in rows]


def _promptfoo_analysis(database_path: Path, limit: int = 12) -> dict[str, Any]:
    empty = {
        "run_trends": [],
        "human_review_trends": [],
        "agent_score_trends": {"agents": [], "series": {"all": []}},
        "case_trends": [],
        "prompt_score_averages": [],
        "case_changes": [],
        "agent_stability": [],
        "dimension_failures": [],
        "fix_signal": {},
    }
    if not database_path.exists():
        return empty
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        try:
            run_rows = connection.execute(
                """
                SELECT eval_id, created_at, imported_at, total, successes, failures, errors,
                       average_score
                FROM promptfoo_eval_runs
                ORDER BY COALESCE(NULLIF(created_at, ''), imported_at) ASC, imported_at ASC
                """
            ).fetchall()
            case_rows = connection.execute(
                """
                SELECT c.eval_id, c.case_id, c.agent_under_test, c.eval_dimensions,
                       c.success, c.score, c.reason, c.imported_at,
                       r.created_at AS run_created_at, r.imported_at AS run_imported_at
                FROM promptfoo_case_results c
                LEFT JOIN promptfoo_eval_runs r ON r.eval_id = c.eval_id
                LEFT JOIN promptfoo_analysis_exclusions e
                  ON e.eval_id = c.eval_id AND e.case_id = c.case_id
                WHERE COALESCE(e.excluded, 0) = 0
                ORDER BY COALESCE(NULLIF(r.created_at, ''), r.imported_at, c.imported_at) ASC,
                         c.imported_at ASC, c.id ASC
                """
            ).fetchall()
            human_rows = connection.execute(
                """
                SELECT case_id, run_id, agent, average_score, safety, created_at
                FROM human_eval_reviews
                ORDER BY created_at ASC, id ASC
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return empty

    run_order = {str(row["eval_id"]): index for index, row in enumerate(run_rows)}
    human_review_rows = [dict(row) for row in human_rows]
    human_review_trends = _human_review_trends(human_review_rows)
    human_case_trends = _human_case_review_trends(human_review_rows)
    histories: dict[str, list[dict[str, Any]]] = {}
    agent_values: dict[str, list[dict[str, Any]]] = {}
    dimension_failures: Counter[str] = Counter()
    dimension_agents: dict[str, set[str]] = {}
    items_by_eval: dict[str, list[dict[str, Any]]] = {}

    for row in case_rows:
        item = _analysis_case_row(dict(row), run_order)
        items_by_eval.setdefault(item["eval_id"], []).append(item)
        histories.setdefault(item["case_id"], []).append(item)
        if not item["success"]:
            for dimension in item["dimensions"]:
                label = _dimension_summary_label(dimension)
                dimension_failures[label] += 1
                dimension_agents.setdefault(label, set()).add(item["agent"])

    run_trends = [
        _analysis_run_row_from_cases(dict(row), items_by_eval.get(str(row["eval_id"]), []))
        for row in run_rows
        if items_by_eval.get(str(row["eval_id"]))
    ][-limit:]
    daily_histories = {
        case_id: _daily_case_items(items)
        for case_id, items in histories.items()
    }
    daily_items = [item for items in daily_histories.values() for item in items]
    agent_score_trends = _agent_score_trends(daily_items, human_review_rows)
    for items in daily_histories.values():
        for item in items:
            agent_values.setdefault(item["agent"], []).append(item)

    case_changes = [_case_change_summary(case_id, items) for case_id, items in daily_histories.items()]
    case_changes = [item for item in case_changes if item is not None]
    case_trends = [_case_trend_summary(case_id, items) for case_id, items in daily_histories.items()]
    case_trends = [item for item in case_trends if item is not None]
    case_trends.sort(key=lambda item: (item["agent"], item["case_id"]))
    prompt_score_averages = [_prompt_score_average(case_id, items) for case_id, items in daily_histories.items()]
    prompt_score_averages = [item for item in prompt_score_averages if item is not None]
    prompt_score_averages.sort(
        key=lambda item: (
            item["agent"],
            item["average_score"] if item["average_score"] is not None else -1,
            item["case_id"],
        )
    )
    case_changes.sort(
        key=lambda item: (
            {"regressed": 0, "improved": 1, "stable": 2, "new": 3}.get(item["status"], 9),
            -abs(float(item.get("score_delta") or 0)),
            item["case_id"],
        )
    )
    agent_stability = [_agent_stability_summary(agent, items) for agent, items in agent_values.items()]
    agent_stability.sort(key=lambda item: (-item["flaky_cases"], item["pass_rate"], item["agent"]))
    dimension_rows = [
        {
            "dimension": dimension,
            "failures": count,
            "agents": sorted(dimension_agents.get(dimension, set())),
        }
        for dimension, count in dimension_failures.most_common(12)
    ]
    return {
        "run_trends": run_trends,
        "human_review_trends": human_review_trends,
        "agent_score_trends": agent_score_trends,
        "human_case_trends": human_case_trends,
        "case_trends": case_trends,
        "prompt_score_averages": prompt_score_averages,
        "case_changes": case_changes[:40],
        "agent_stability": agent_stability,
        "dimension_failures": dimension_rows,
        "fix_signal": _fix_signal_summary(run_trends, case_changes),
    }


def _human_review_trends(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    daily_scores: dict[str, list[float]] = {}
    for row in rows:
        score = row.get("average_score")
        created_at = str(row.get("created_at") or "")
        if score is None or not created_at:
            continue
        day = created_at[:10]
        daily_scores.setdefault(day, []).append(float(score))
    return [
        {
            "date": day,
            "average_score": round(sum(scores) / len(scores), 3),
            "count": len(scores),
        }
        for day, scores in sorted(daily_scores.items())
    ]


def _agent_score_trends(
    machine_rows: list[dict[str, Any]],
    human_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    agents: set[str] = set()
    machine_scores: dict[str, dict[str, list[float]]] = {"all": {}}
    human_scores: dict[str, dict[str, list[float]]] = {"all": {}}

    def add_score(
        buckets: dict[str, dict[str, list[float]]],
        agent: str,
        day: str,
        score: float,
    ) -> None:
        buckets.setdefault("all", {}).setdefault(day, []).append(score)
        buckets.setdefault(agent, {}).setdefault(day, []).append(score)

    for row in machine_rows:
        created_at = str(row.get("created_at") or "")
        score = row.get("score")
        if score is None or not created_at:
            continue
        day = created_at[:10]
        agent = str(row.get("agent") or "unknown").strip() or "unknown"
        agents.add(agent)
        add_score(machine_scores, agent, day, float(score) * 5)

    for row in human_rows:
        created_at = str(row.get("created_at") or "")
        score = row.get("average_score")
        if score is None or not created_at:
            continue
        day = created_at[:10]
        agent = str(row.get("agent") or "unknown").strip() or "unknown"
        agents.add(agent)
        add_score(human_scores, agent, day, float(score))

    def build_series(key: str) -> list[dict[str, Any]]:
        days = sorted(set(machine_scores.get(key, {})) | set(human_scores.get(key, {})))
        series: list[dict[str, Any]] = []
        for day in days:
            machine = machine_scores.get(key, {}).get(day, [])
            human = human_scores.get(key, {}).get(day, [])
            series.append(
                {
                    "date": day,
                    "machine_average": round(sum(machine) / len(machine), 3) if machine else None,
                    "machine_count": len(machine),
                    "human_average": round(sum(human) / len(human), 3) if human else None,
                    "human_count": len(human),
                }
            )
        return series

    series = {"all": build_series("all")}
    for agent in sorted(agents):
        series[agent] = build_series(agent)
    return {"agents": sorted(agents), "series": series}


def _human_case_review_trends(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_case_day: dict[str, dict[str, list[float]]] = {}
    review_counts: dict[str, dict[str, int]] = {}
    for row in rows:
        case_id = str(row.get("case_id") or "")
        score = row.get("average_score")
        created_at = str(row.get("created_at") or "")
        if not case_id or score is None or not created_at:
            continue
        day = created_at[:10]
        by_case_day.setdefault(case_id, {}).setdefault(day, []).append(float(score))
        review_counts.setdefault(case_id, {}).setdefault(day, 0)
        review_counts[case_id][day] += 1
    return [
        {
            "case_id": case_id,
            "days": [
                {
                    "date": day,
                    "average_score": round(sum(scores) / len(scores), 3),
                    "count": review_counts.get(case_id, {}).get(day, len(scores)),
                }
                for day, scores in sorted(days.items())
            ],
        }
        for case_id, days in sorted(by_case_day.items())
    ]


def _analysis_run_row(row: dict[str, Any]) -> dict[str, Any]:
    total = int(row.get("total") or 0)
    successes = int(row.get("successes") or 0)
    return {
        "eval_id": str(row.get("eval_id") or ""),
        "created_at": row.get("created_at") or row.get("imported_at") or "",
        "total": total,
        "successes": successes,
        "failures": int(row.get("failures") or 0),
        "errors": int(row.get("errors") or 0),
        "pass_rate": round(successes / total * 100, 1) if total else None,
        "average_score": row.get("average_score"),
    }


def _analysis_run_row_from_cases(row: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(items)
    successes = sum(1 for item in items if item["success"])
    scores = [float(item["score"]) for item in items]
    return {
        "eval_id": str(row.get("eval_id") or ""),
        "created_at": row.get("created_at") or row.get("imported_at") or "",
        "total": total,
        "successes": successes,
        "failures": total - successes,
        "errors": 0,
        "pass_rate": round(successes / total * 100, 1) if total else None,
        "average_score": round(sum(scores) / len(scores), 3) if scores else None,
    }


def _analysis_case_row(row: dict[str, Any], run_order: dict[str, int]) -> dict[str, Any]:
    eval_id = str(row.get("eval_id") or "")
    return {
        "eval_id": eval_id,
        "case_id": str(row.get("case_id") or ""),
        "agent": str(row.get("agent_under_test") or "unknown").strip() or "unknown",
        "dimensions": _split_dimensions(str(row.get("eval_dimensions") or "")),
        "success": bool(row.get("success")),
        "score": float(row.get("score") or 0),
        "reason": str(row.get("reason") or ""),
        "created_at": row.get("run_created_at") or row.get("run_imported_at") or row.get("imported_at") or "",
        "run_order": run_order.get(eval_id, 0),
    }


def _daily_case_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in items:
        day = str(item.get("created_at") or "")[:10] or str(item.get("created_at") or "")
        grouped.setdefault((item["case_id"], day), []).append(item)
    daily: list[dict[str, Any]] = []
    for (_case_id, day), group in grouped.items():
        ordered = sorted(group, key=lambda item: (item["run_order"], item["created_at"], item["eval_id"]))
        latest = ordered[-1]
        scores = [float(item["score"]) for item in ordered]
        success_count = sum(1 for item in ordered if item["success"])
        dimensions: list[str] = []
        for item in ordered:
            for dimension in item["dimensions"]:
                if dimension not in dimensions:
                    dimensions.append(dimension)
        daily.append(
            {
                "eval_id": latest["eval_id"],
                "eval_ids": [item["eval_id"] for item in ordered],
                "case_id": latest["case_id"],
                "agent": latest["agent"],
                "dimensions": dimensions,
                "success": success_count / len(ordered) >= 0.5,
                "success_rate": round(success_count / len(ordered) * 100, 1),
                "score": round(sum(scores) / len(scores), 3),
                "reason": latest.get("reason") or "",
                "created_at": day,
                "latest_created_at": latest["created_at"],
                "run_order": latest["run_order"],
                "run_count": len(ordered),
            }
        )
    return sorted(daily, key=lambda item: (item["run_order"], item["created_at"], item["eval_id"]))


def _case_trend_summary(case_id: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    ordered = sorted(items, key=lambda item: (item["run_order"], item["created_at"]))
    if not ordered:
        return None
    latest = ordered[-1]
    return {
        "case_id": case_id,
        "agent": latest["agent"],
        "dimensions": latest["dimensions"],
        "runs": [
            {
                "eval_id": item["eval_id"],
                "created_at": item["created_at"],
                "latest_created_at": item.get("latest_created_at") or item["created_at"],
                "success": item["success"],
                "success_rate": item.get("success_rate"),
                "score": item["score"],
                "run_count": item.get("run_count", 1),
            }
            for item in ordered
        ],
    }


def _prompt_score_average(case_id: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    scores = [float(item["score"]) for item in items]
    latest = sorted(items, key=lambda item: (item["run_order"], item["created_at"]))[-1]
    return {
        "case_id": case_id,
        "agent": latest["agent"],
        "dimensions": latest["dimensions"],
        "average_score": round(sum(scores) / len(scores), 3) if scores else None,
        "run_count": len(items),
        "latest_score": latest["score"],
        "latest_created_at": latest["created_at"],
        "latest_success": latest["success"],
    }


def _case_change_summary(case_id: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    ordered = sorted(items, key=lambda item: (item["run_order"], item["created_at"]))
    if not ordered:
        return None
    latest = ordered[-1]
    previous = ordered[-2] if len(ordered) > 1 else None
    score_delta = None if previous is None else round(latest["score"] - previous["score"], 3)
    status = "new"
    if previous is not None:
        if previous["success"] is False and latest["success"] is True:
            status = "improved"
        elif previous["success"] is True and latest["success"] is False:
            status = "regressed"
        elif score_delta is not None and score_delta > 0.001:
            status = "improved"
        elif score_delta is not None and score_delta < -0.001:
            status = "regressed"
        else:
            status = "stable"
    return {
        "case_id": case_id,
        "agent": latest["agent"],
        "status": status,
        "runs": len(ordered),
        "latest_eval_id": latest["eval_id"],
        "previous_eval_id": previous["eval_id"] if previous else "",
        "latest_created_at": latest["created_at"],
        "previous_created_at": previous["created_at"] if previous else "",
        "latest_success": latest["success"],
        "previous_success": previous["success"] if previous else None,
        "latest_score": latest["score"],
        "previous_score": previous["score"] if previous else None,
        "score_delta": score_delta,
        "dimensions": latest["dimensions"],
    }


def _agent_stability_summary(agent: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    scores = [float(item["score"]) for item in items]
    success_count = sum(1 for item in items if item["success"])
    histories: dict[str, set[bool]] = {}
    for item in items:
        histories.setdefault(item["case_id"], set()).add(bool(item["success"]))
    return {
        "agent": agent,
        "runs": len(items),
        "pass_rate": round(success_count / len(items) * 100, 1) if items else 0,
        "average_score": round(sum(scores) / len(scores), 3) if scores else None,
        "score_range": round(max(scores) - min(scores), 3) if scores else 0,
        "flaky_cases": sum(1 for values in histories.values() if len(values) > 1),
    }


def _fix_signal_summary(
    run_trends: list[dict[str, Any]],
    case_changes: list[dict[str, Any]],
) -> dict[str, Any]:
    latest = run_trends[-1] if run_trends else {}
    previous = run_trends[-2] if len(run_trends) > 1 else {}
    return {
        "latest_eval_id": latest.get("eval_id") or "",
        "previous_eval_id": previous.get("eval_id") or "",
        "pass_rate_delta": _delta(latest.get("pass_rate"), previous.get("pass_rate")),
        "average_score_delta": _delta(latest.get("average_score"), previous.get("average_score")),
        "improved_cases": sum(1 for item in case_changes if item["status"] == "improved"),
        "regressed_cases": sum(1 for item in case_changes if item["status"] == "regressed"),
        "stable_cases": sum(1 for item in case_changes if item["status"] == "stable"),
    }


def _delta(latest: Any, previous: Any) -> float | None:
    if latest is None or previous is None:
        return None
    try:
        return round(float(latest) - float(previous), 3)
    except (TypeError, ValueError):
        return None


def _eval_run_row(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["agent_scores"] = _json_object(str(payload.pop("agent_scores_json") or ""))
    return payload


def _case_response_text(
    latest_promptfoo: dict[str, Any],
    latest_slack: dict[str, Any],
) -> str:
    slack_summary = str(latest_slack.get("result_summary") or "").strip()
    output = latest_promptfoo.get("output") if isinstance(latest_promptfoo, dict) else {}
    if isinstance(output, dict):
        response = output.get("output") or output.get("response") or output.get("human_summary")
        if isinstance(response, str) and response.strip():
            parsed = _json_object(response)
            if parsed:
                summary = str(parsed.get("human_summary") or parsed.get("output") or "").strip()
                if summary:
                    return summary
            return response.strip()
    if slack_summary:
        return slack_summary
    reason = str(latest_promptfoo.get("reason") or latest_promptfoo.get("failure_reason") or "").strip()
    return reason


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _dashboard_html(payload: dict[str, Any]) -> str:
    data_json = _json_script_payload(payload)
    summary = payload["summary"]
    title = "Keystone Eval Dashboard"
    score_dimensions = [str(dimension) for dimension in payload.get("score_dimensions", [])]
    human_score_header_cells = "\n".join(
        f"\t                    <th>{html.escape(dimension)}</th>" for dimension in score_dimensions
    )
    human_score_colgroup = "\n".join(
        "\t                  <col class=\"score-col\">" for _ in score_dimensions
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f8;
      --panel: #ffffff;
      --ink: #1b1f23;
      --muted: #687076;
      --line: #d9dee3;
      --good: #287a47;
      --bad: #b42318;
      --warn: #c36a14;
      --accent: #3f6b4a;
      --accent-2: #8a6f2a;
      --soft: #f1f4f6;
      --soft-warn: #f2f4f5;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    header {{
      border-bottom: 1px solid var(--line);
      background: var(--panel);
      padding: 20px 28px 18px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 34px; line-height: 1.1; letter-spacing: 0; }}
    .subtle {{ color: var(--muted); }}
    .dashboard-intro {{
      max-width: 980px;
      color: #3f464c;
      font-size: 15px;
      line-height: 1.45;
    }}
    .dashboard-intro p {{ margin: 0 0 4px; }}
    .intro-details {{
      margin-top: 8px;
      color: var(--muted);
      font-size: 12px;
    }}
    .intro-details summary {{
      cursor: pointer;
      width: fit-content;
      font-weight: 650;
    }}
    .intro-details .detail-content {{
      max-width: 920px;
      margin-top: 6px;
      display: grid;
      gap: 3px;
    }}
    main {{ padding: 20px 28px 34px; max-width: 1480px; margin: 0 auto; }}
    .kpis {{
      display: grid;
      grid-template-columns: repeat(6, minmax(140px, 1fr));
      gap: 10px;
      margin-bottom: 18px;
    }}
    .kpi, .panel, .case-card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }}
    .kpi {{
      display: block;
      width: 100%;
      padding: 12px;
      min-height: 106px;
      text-align: left;
    }}
    .kpi[data-drilldown] {{ cursor: pointer; }}
    .kpi[data-drilldown]:hover, .kpi.active {{
      border-color: #93ad8f;
      box-shadow: 0 0 0 2px rgba(63, 107, 74, 0.14);
    }}
    .kpi .label {{ color: var(--muted); font-size: 12px; font-weight: 650; }}
    .kpi .value {{ font-size: 25px; font-weight: 740; margin-top: 7px; }}
    .kpi .hint {{ color: var(--muted); font-size: 12px; margin-top: 4px; }}
    .grid {{
      display: grid;
      grid-template-columns: minmax(330px, 410px) minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }}
    .workspace {{
      display: grid;
      grid-template-columns: 180px minmax(0, 1fr);
      gap: 16px;
      align-items: start;
    }}
    .side-nav {{
      position: sticky;
      top: 14px;
      display: grid;
      gap: 6px;
    }}
    .side-nav button {{
      width: 100%;
      text-align: left;
      border-color: transparent;
      background: transparent;
      font-weight: 650;
    }}
    .side-nav button.active {{
      background: #eef6ef;
      border-color: #b8d6bd;
      color: #2f5437;
    }}
    .view {{ display: none; }}
    .view.active {{ display: block; }}
    .grid > *, .panel {{ min-width: 0; }}
    .panel {{ padding: 14px; }}
    .section-head {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 10px;
      min-width: 0;
    }}
    .subsection {{
      padding-top: 14px;
      margin-top: 16px;
      border-top: 1px solid var(--line);
    }}
    .subsection:first-child {{
      padding-top: 0;
      margin-top: 0;
      border-top: 0;
    }}
    .subsection-title {{
      margin-bottom: 2px;
    }}
    h2 {{ font-size: 15px; margin: 0; }}
    .bar-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 130px 54px;
      gap: 8px;
      align-items: center;
      margin: 8px 0;
    }}
    .bar-row > *, .score-row > * {{ min-width: 0; }}
    .bar-track {{ height: 10px; background: #e8edf0; border-radius: 999px; overflow: hidden; }}
    .bar {{ height: 100%; background: #3b7045; }}
    .bar.gap {{ background: var(--warn); }}
    .score-table {{
      display: grid;
      gap: 6px;
      margin-bottom: 16px;
    }}
    .score-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 72px 72px;
      gap: 8px;
      align-items: center;
      padding: 8px 0;
      border-bottom: 1px solid #edf0f2;
    }}
    .score-row.header {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      padding-top: 0;
    }}
    .run-list {{ display: grid; gap: 8px; margin-top: 8px; }}
    .run-card {{
      border: 1px solid var(--line);
      border-left: 4px solid var(--warn);
      border-radius: 7px;
      background: #fbfcfb;
      padding: 10px;
    }}
    .run-card.pass {{ border-left-color: var(--good); background: #f3faf5; }}
    .run-card.fail {{ border-left-color: var(--bad); background: #fff4f2; }}
    .run-card.warn {{ border-left-color: var(--warn); background: #f7f8f9; }}
    .run-card.warn .run-title {{ color: var(--warn); }}
    .run-head {{
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      margin-bottom: 7px;
    }}
    .run-title {{ font-weight: 650; overflow-wrap: anywhere; }}
    .run-stats {{ display: flex; gap: 5px; flex-wrap: wrap; margin-bottom: 7px; }}
    .run-meter {{ height: 8px; border-radius: 999px; background: #e7e3dc; overflow: hidden; }}
    .run-meter > span {{ display: block; height: 100%; background: var(--good); }}
    .status-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(150px, 1fr));
      gap: 8px;
      margin-top: 8px;
    }}
    .status-box {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #f8faf9;
      padding: 8px;
      min-width: 0;
    }}
    .status-box .status-title {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      margin-bottom: 4px;
    }}
    .status-box .status-main {{
      font-weight: 700;
      overflow-wrap: anywhere;
    }}
    .status-box .status-date {{
      color: var(--muted);
      font-size: 12px;
      margin-top: 4px;
      overflow-wrap: anywhere;
    }}
    .workflow-grid {{
      display: grid;
      grid-template-columns: minmax(260px, 1fr) minmax(300px, 1.35fr);
      gap: 12px;
      margin-top: 10px;
    }}
    .workflow-steps {{
      grid-column: 1 / -1;
      display: grid;
      grid-template-columns: repeat(3, minmax(150px, 1fr));
      gap: 8px;
      align-content: start;
    }}
    .workflow-step {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fbfcfb;
      padding: 10px;
      min-width: 0;
    }}
    .workflow-step .label {{ color: var(--muted); font-size: 12px; font-weight: 650; }}
    .workflow-step .value {{ font-size: 20px; font-weight: 750; margin-top: 4px; }}
    .workflow-stage-bar {{
      height: 7px;
      border-radius: 999px;
      background: #e5eaee;
      overflow: hidden;
      margin: 8px 0 6px;
    }}
    .workflow-stage-fill {{
      height: 100%;
      border-radius: inherit;
      background: var(--accent);
      min-width: 2px;
    }}
    .workflow-chat {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #f8faf9;
      padding: 10px;
      display: grid;
      gap: 8px;
      min-width: 0;
    }}
    .chat-row {{
      display: grid;
      grid-template-columns: 86px minmax(0, 1fr);
      gap: 8px;
      align-items: start;
    }}
    .chat-speaker {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      padding-top: 7px;
    }}
    .chat-bubble {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fff;
      padding: 8px 10px;
      overflow-wrap: anywhere;
    }}
    .chat-bubble.system {{ background: #f2f5f3; }}
    .workflow-run-plan {{
      grid-column: 1 / -1;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fff;
      min-width: 0;
    }}
    .workflow-disclosure {{
      grid-column: 1 / -1;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fff;
      min-width: 0;
      overflow: hidden;
    }}
    .workflow-disclosure summary,
    .dashboard-details summary {{
      cursor: pointer;
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 10px 12px;
      font-weight: 700;
    }}
    .workflow-disclosure[open] summary {{
      border-bottom: 1px solid var(--line);
      margin-bottom: 10px;
    }}
    .workflow-disclosure summary .subtle,
    .dashboard-details summary .subtle {{
      font-weight: 500;
      font-size: 12px;
    }}
    .run-plan-grid {{
      display: grid;
      grid-template-columns: minmax(250px, 0.9fr) minmax(320px, 1.1fr);
      gap: 12px;
      padding: 0 12px 12px;
    }}
    .workflow-disclosure > .run-plan-box {{
      margin: 0 12px 12px;
    }}
    .workflow-thread .workflow-chat {{
      margin: 0 12px 12px;
    }}
    .run-plan-box {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #f8faf9;
      padding: 10px;
      min-width: 0;
    }}
    .run-plan-box .label {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      margin-bottom: 5px;
    }}
    .run-plan-list {{
      margin: 8px 0 0;
      padding-left: 18px;
    }}
    .run-plan-list li {{
      margin: 4px 0;
    }}
    .run-plan-prompt {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #eef2f4;
      padding: 9px 10px;
      overflow-wrap: anywhere;
    }}
    .workflow-contract {{
      background: #fbfcfb;
    }}
    .workflow-contract table {{
      min-width: 980px;
      table-layout: fixed;
    }}
    .workflow-contract th,
    .workflow-contract td {{
      padding: 8px 12px;
    }}
    .workflow-contract th:nth-child(1) {{ width: 16%; }}
    .workflow-contract th:nth-child(2) {{ width: 23%; }}
    .workflow-contract th:nth-child(3) {{ width: 18%; }}
    .workflow-contract th:nth-child(4) {{ width: 20%; }}
    .workflow-contract th:nth-child(5) {{ width: 8%; }}
    .workflow-contract th:nth-child(6) {{ width: 15%; }}
    .analysis-grid {{
      display: grid;
      grid-template-columns: repeat(4, minmax(140px, 1fr));
      gap: 10px;
      margin-bottom: 14px;
    }}
    .analysis-card {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #f8faf9;
      padding: 10px;
      min-width: 0;
    }}
    .analysis-card .label {{ color: var(--muted); font-size: 12px; font-weight: 650; }}
    .analysis-card .value {{ font-size: 22px; font-weight: 750; margin-top: 4px; }}
    .analysis-chart {{
      width: 100%;
      min-height: 190px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fbfcfb;
      padding: 10px;
      margin: 10px 0 14px;
    }}
    .analysis-chart svg {{ display: block; width: 100%; height: 180px; overflow: visible; }}
    .analysis-scaffold-grid {{
      display: grid;
      grid-template-columns: repeat(3, minmax(180px, 1fr));
      gap: 10px;
      padding: 0 12px 12px;
    }}
    .dashboard-details {{
      display: block;
    }}
    .dashboard-details[open] summary {{
      border-bottom: 1px solid var(--line);
      margin-bottom: 10px;
    }}
    .secondary-analysis-stack {{
      display: grid;
      gap: 16px;
      padding: 0 12px 12px;
    }}
    .analysis-scaffold-card {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fbfcfb;
      padding: 10px;
      min-width: 0;
    }}
    .analysis-scaffold-card h3 {{
      margin: 0 0 4px;
      font-size: 13px;
    }}
    .scaffold-visual {{
      margin-top: 10px;
      min-height: 74px;
      display: grid;
      align-items: end;
      gap: 8px;
    }}
    .scaffold-line {{
      width: 100%;
      height: 72px;
      border: 1px solid #edf0f2;
      border-radius: 6px;
      background: #fff;
    }}
    .scaffold-bars {{
      display: grid;
      gap: 7px;
      margin-top: 9px;
    }}
    .scaffold-bar-row {{
      display: grid;
      grid-template-columns: minmax(96px, 140px) minmax(0, 1fr) 48px;
      gap: 7px;
      align-items: center;
      font-size: 12px;
    }}
    .scaffold-heatmap {{
      display: grid;
      grid-template-columns: repeat(6, minmax(18px, 1fr));
      gap: 5px;
      margin-top: 10px;
    }}
    .scaffold-heat-cell {{
      min-height: 22px;
      border: 1px solid #d9dee3;
      border-radius: 4px;
      background: #eef3f5;
    }}
    .scaffold-note {{
      color: var(--muted);
      font-size: 11px;
      margin-top: 7px;
    }}
    .mini-bars {{
      display: grid;
      gap: 6px;
      margin-top: 9px;
    }}
    .mini-bar-row {{
      display: grid;
      grid-template-columns: minmax(82px, 108px) minmax(0, 1fr) 48px;
      gap: 7px;
      align-items: center;
      font-size: 12px;
    }}
    .mini-bar-track {{
      height: 7px;
      border-radius: 999px;
      background: #e7edf0;
      overflow: hidden;
    }}
    .mini-bar-fill {{
      height: 100%;
      border-radius: inherit;
      background: var(--accent);
      min-width: 2px;
    }}
    .analysis-table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 8px;
    }}
    .analysis-table th, .analysis-table td {{
      border-bottom: 1px solid #edf0f2;
      padding: 8px 6px;
      text-align: left;
      vertical-align: top;
    }}
    .analysis-table th {{ color: var(--muted); font-size: 12px; font-weight: 650; }}
    .prompt-average-groups {{
      display: grid;
      grid-template-columns: repeat(2, minmax(260px, 1fr));
      gap: 14px;
      margin-top: 10px;
    }}
    .prompt-average-group {{
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fbfcfb;
      padding: 10px;
      min-width: 0;
    }}
    .prompt-average-group h3 {{
      margin: 0 0 8px;
      font-size: 13px;
    }}
    .prompt-average-row {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 108px 48px;
      gap: 8px;
      align-items: center;
      margin: 7px 0;
    }}
    .metric-lines {{ display: grid; gap: 3px; }}
    .metric-line {{ overflow-wrap: anywhere; }}
    .case-actions {{ display: flex; gap: 6px; flex-wrap: wrap; justify-content: flex-end; }}
    .analysis-toggle.excluded {{ border-color: #d4a373; color: var(--warn); }}
    .prompt-head {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 4px;
    }}
    .prompt-copy {{
      min-height: 28px;
      padding: 4px 8px;
      font-size: 12px;
    }}
    .copy-source {{
      position: fixed;
      left: -9999px;
      top: 0;
      width: 1px;
      height: 1px;
      opacity: 0;
      pointer-events: none;
    }}
    .segmented {{ display: inline-flex; border: 1px solid var(--line); border-radius: 7px; overflow: hidden; }}
    .segmented button {{
      border: 0;
      border-radius: 0;
      min-height: 30px;
      background: #fff;
      padding: 5px 9px;
    }}
    .segmented button.active {{ background: #edf4ea; color: #2f5437; font-weight: 650; }}
    .filters {{ display: flex; gap: 8px; margin: 0 0 12px; flex-wrap: wrap; }}
    input, select {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      background: #fff;
      color: var(--ink);
      min-height: 36px;
      max-width: 100%;
    }}
    input {{ min-width: 280px; flex: 1; }}
    .case-list {{ display: grid; gap: 12px; min-width: 0; }}
    .prompt-list {{ display: grid; gap: 10px; min-width: 0; }}
    .case-card {{ padding: 14px; min-width: 0; width: 100%; }}
	    .prompt-card {{
	      background: var(--panel);
	      border: 1px solid var(--line);
	      border-radius: 8px;
	      padding: 12px;
	    }}
	    .db-actions {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
	    .db-table-wrap {{
	      overflow-x: auto;
	      border: 1px solid var(--line);
	      border-radius: 8px;
	      background: var(--panel);
	    }}
	    table {{ border-collapse: collapse; min-width: 1120px; width: 100%; }}
	    table.eval-db-table {{
	      table-layout: fixed;
	      min-width: 3180px;
	    }}
	    table.eval-db-table col.prompt-id {{ width: 68px; }}
	    table.eval-db-table col.case-col {{ width: 300px; }}
	    table.eval-db-table col.agent-col {{ width: 180px; }}
	    table.eval-db-table col.status-col {{ width: 115px; }}
	    table.eval-db-table col.run-col {{ width: 190px; }}
	    table.eval-db-table col.score-col {{ width: 126px; }}
	    table.eval-db-table col.analysis-col {{ width: 126px; }}
	    table.eval-db-table col.notes-col {{ width: 220px; }}
	    table.eval-db-table col.prompt-col {{ width: auto; }}
	    table.eval-db-table col.response-col {{ width: 360px; }}
	    th, td {{
	      border-bottom: 1px solid #edf0f2;
	      padding: 8px 10px;
	      text-align: left;
	      vertical-align: top;
	    }}
	    th {{
	      color: var(--muted);
	      font-size: 12px;
	      font-weight: 650;
	      background: #f7f9fa;
	      position: sticky;
	      top: 0;
	    }}
	    .metric-th {{
	      display: grid;
	      gap: 3px;
	      line-height: 1.25;
	    }}
	    .metric-th-title {{
	      color: var(--ink);
	      font-weight: 700;
	    }}
	    .metric-th-detail {{
	      color: var(--muted);
	      font-size: 11px;
	      font-weight: 500;
	      overflow-wrap: anywhere;
	    }}
	    .db-empty {{ color: var(--muted); }}
	    td.prompt-cell {{
	      white-space: normal;
	      overflow-wrap: anywhere;
	    }}
	    a.pill {{ color: inherit; text-decoration: none; }}
    .case-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 12px;
      border-bottom: 1px solid var(--line);
      padding-bottom: 10px;
      margin-bottom: 12px;
    }}
    .case-id {{ font-weight: 650; overflow-wrap: anywhere; }}
    .case-meta {{ display: flex; flex-wrap: wrap; gap: 4px; margin-top: 6px; }}
    .case-body {{ display: grid; gap: 10px; }}
    .case-body > section + section {{
      border-top: 1px solid #e4e8eb;
      padding-top: 10px;
    }}
    .detail-label {{
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      margin-bottom: 4px;
    }}
    .text-block {{
      background: var(--soft);
      border: 1px solid #dde5ea;
      border-radius: 6px;
      padding: 9px 10px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .score-grid {{ display: flex; flex-wrap: wrap; gap: 5px; }}
    .review-form {{
      border-top: 1px solid var(--line);
      margin-top: 2px;
      padding-top: 10px;
    }}
    .review-form[aria-disabled="true"] {{
      opacity: 0.72;
    }}
    .review-form button:disabled,
    .review-form select:disabled,
    .review-form textarea:disabled {{
      cursor: not-allowed;
    }}
    .score-controls {{
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }}
    .score-control label {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      margin-bottom: 3px;
    }}
    .score-control select {{ width: 100%; }}
    textarea {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      min-height: 70px;
      width: 100%;
      resize: vertical;
      font: inherit;
    }}
    .form-actions {{
      display: flex;
      align-items: center;
      gap: 10px;
      margin-top: 8px;
      flex-wrap: wrap;
    }}
    button {{
      border: 1px solid #9a927f;
      border-radius: 6px;
      background: #f7f9fa;
      color: var(--ink);
      min-height: 34px;
      padding: 7px 11px;
      font: inherit;
      cursor: pointer;
    }}
    button:hover {{ background: #eef3f5; }}
    .review-status {{ color: var(--muted); }}
    .pill {{ display: inline-block; padding: 2px 7px; border-radius: 999px; font-size: 12px; border: 1px solid var(--line); margin: 0 4px 4px 0; max-width: 100%; overflow-wrap: anywhere; }}
    .pass {{ color: var(--good); border-color: #a7d7bd; background: #eef8f2; }}
    .fail {{ color: var(--bad); border-color: #f0b8b2; background: #fff1f0; }}
    .warn {{ color: var(--warn); border-color: #d6d9dc; background: var(--soft-warn); }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
    .truncate {{ overflow: hidden; text-overflow: ellipsis; }}
    @media (max-width: 980px) {{
      main, header {{ padding-left: 16px; padding-right: 16px; }}
      .kpis {{ grid-template-columns: repeat(3, minmax(130px, 1fr)); }}
      .workspace {{ grid-template-columns: 1fr; }}
      .side-nav {{
        position: static;
        grid-template-columns: repeat(3, minmax(0, 1fr));
      }}
      .side-nav button {{ text-align: center; }}
      .grid {{ grid-template-columns: 1fr; }}
      .section-head {{ align-items: flex-start; flex-wrap: wrap; }}
      .score-row {{ grid-template-columns: minmax(0, 1fr) 62px 62px; }}
      .bar-row {{ grid-template-columns: minmax(0, 1fr) 100px 54px; }}
      .prompt-average-groups {{ grid-template-columns: 1fr; }}
      .prompt-average-row {{ grid-template-columns: minmax(0, 1fr) 92px 44px; }}
      .score-controls {{ grid-template-columns: repeat(2, minmax(140px, 1fr)); }}
      .status-grid {{ grid-template-columns: 1fr; }}
      .analysis-grid {{ grid-template-columns: repeat(2, minmax(130px, 1fr)); }}
      .workflow-steps {{ grid-template-columns: repeat(2, minmax(140px, 1fr)); }}
      input, select {{ min-width: 0; width: 100%; }}
      .case-head {{ display: block; }}
    }}
    @media (max-width: 640px) {{
      .kpis {{ grid-template-columns: repeat(2, minmax(130px, 1fr)); }}
      .workflow-steps {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>{title}</h1>
    <div class="dashboard-intro">
      <p>Track eval prompts, machine checks, Slack runs, human reviews, and before/after movement from prompt or tool fixes.</p>
      <details class="intro-details">
        <summary>Scoring notes</summary>
        <div class="detail-content">
          <span>Promptfoo scores are backend assertion checks; Slack human scores are separate quality reviews.</span>
          <span>Human reviews create evidence for fixes and regression cases, but do not retrain agents automatically.</span>
          <span>Source database: {html.escape(str(payload["database_path"]))}</span>
        </div>
      </details>
    </div>
  </header>
  <main>
    <section class="kpis">
      {_kpi("Prompt Coverage", f'{summary["coverage_complete_agents"]}/{len(summary["coverage"])} agents ready', f'{summary["coverage_target_per_agent"]} prompts/agent; {summary["coverage_gap_total"]} gaps')}
	      {_kpi("Machine Pass Rate", f'{summary["promptfoo_pass_rate"]}%', f'{summary["promptfoo_evaluated"]} checked; {summary["promptfoo_pending"]} pending')}
	      {_kpi("Machine Avg / 5", _score_out_of_five(summary["machine_average"], machine=True), f'{summary["machine_scored"]} scored cases', "machine")}
      {_kpi("Slack Runs", summary["slack_linked"], "saved Slack thread runs")}
      {_kpi("Human Avg / 5", _score_out_of_five(summary["human_average"]), f'{summary["human_reviewed"]} reviewed', "human")}
      {_kpi("Total Cases", summary["total_cases"], f'{summary["seed_case_total"]} committed; {summary["non_seed_cases"]} ad hoc')}
    </section>
    <section class="workspace">
	      <nav class="side-nav" aria-label="Dashboard sections">
	        <button type="button" data-view="overview" class="active">Overview</button>
	        <button type="button" data-view="overview" data-score-nav="machine">Machine checks</button>
	        <button type="button" data-view="overview" data-score-nav="human">Human review</button>
	        <button type="button" data-view="prompts">Prompts</button>
	        <button type="button" data-view="runs">Runs & scoring</button>
	        <button type="button" data-view="database">Database</button>
	        <button type="button" data-view="analysis">Analysis</button>
	      </nav>
      <div class="view-stack">
        <section id="view-overview" class="view active">
          <div class="panel">
            <section class="subsection">
              <div class="section-head">
                <div>
                  <h2 class="subsection-title">Slack Eval Conversation Flow</h2>
                  <div class="subtle">Cost-safe #evals loop. Stage bars use saved database rows; no Slack or OpenAI call runs from this view.</div>
                </div>
              </div>
              <div id="workflow-visualization" class="workflow-grid"></div>
            </section>
            <section class="subsection">
              <div class="section-head" id="agent-score-drilldown" tabindex="-1">
                <div>
	                  <h2 class="subsection-title" id="agent-score-title">Agent Scores</h2>
	                  <div class="subtle" id="agent-score-note">Machine checks and human scorecards are tracked separately.</div>
                </div>
                <div class="segmented" aria-label="Agent score metric">
                  <button type="button" data-score-view="machine" class="active">Machine</button>
                  <button type="button" data-score-view="human">Human</button>
                </div>
              </div>
              <div id="agent-score-table" class="score-table"></div>
            </section>
            <section class="subsection">
              <div class="section-head">
                <div>
                  <h2 class="subsection-title">Prompt Coverage</h2>
                  <div class="subtle">Committed eval prompt count by agent; target is {summary["coverage_target_per_agent"]} per agent.</div>
                </div>
                <span class="subtle">{summary["coverage_gap_total"]} remaining</span>
              </div>
              <div id="agent-coverage-bars"></div>
            </section>
            <section class="subsection">
              <h2 class="subsection-title">Evaluation Dimensions</h2>
              <div class="subtle">Top quality areas across committed eval prompts.</div>
              <div id="dimension-bars"></div>
            </section>
          </div>
        </section>
        <section id="view-prompts" class="view">
          <div class="panel">
            <div class="section-head">
              <div>
                <h2>Prompt Library</h2>
                <div class="subtle">Committed Slack eval prompts. Use Copy prompt to paste a case into #evals.</div>
              </div>
            </div>
            <div class="filters">
              <input id="prompt-search" placeholder="Search prompt, case, agent, dimension">
              <select id="prompt-agent-filter"><option value="">All agents</option></select>
            </div>
            <div id="prompt-rows" class="prompt-list"></div>
          </div>
        </section>
	        <section id="view-runs" class="view">
            <div class="panel">
              <div class="section-head">
                <div>
                  <h2>Runs & Scoring</h2>
                  <div class="subtle">Case-level scoring surface for Promptfoo checks, Slack runs, review forms, and analysis inclusion.</div>
                </div>
              </div>
              <div class="filters">
                <input id="search" placeholder="Search case, agent, dimension, ask, run id">
                <select id="agent-filter"><option value="">All agents</option></select>
                <select id="state-filter">
                  <option value="">All states</option>
                  <option value="pass">Auto check pass</option>
                  <option value="fail">Auto check fail</option>
                  <option value="pending">Machine eval pending</option>
                  <option value="human">Human reviewed</option>
                  <option value="slack">Slack test completed</option>
                </select>
              </div>
              <div id="case-rows" class="case-list"></div>
            </div>
        </section>
	        <section id="view-analysis" class="view">
	          <div class="panel">
	            <div class="section-head">
	              <div>
	                <h2>Promptfoo Run Analysis</h2>
	              <div class="subtle">DB-backed comparison of included runs by time, case, agent, and dimension.</div>
	              </div>
	            </div>
	            <div id="analysis-summary" class="analysis-grid"></div>
	            <section class="subsection">
	              <div class="section-head">
	                <div>
	                  <h2 class="subsection-title">Average Score Over Time</h2>
	                  <div class="subtle">All agents by default; choose an agent to compare daily machine and human averages. Same-day prompt retries are averaged before rollup.</div>
	                </div>
	                <select id="analysis-agent-trend-filter"><option value="all">All agents</option></select>
	              </div>
	              <div id="analysis-run-chart" class="analysis-chart"></div>
	              <div id="analysis-agent-trend-table"></div>
	            </section>
	            <section class="subsection">
	              <div class="section-head">
	                <div>
	                  <h2 class="subsection-title">Single Prompt Trend</h2>
	                  <div class="subtle">Daily machine and human lines for one prompt; duplicate same-day runs are averaged.</div>
	                </div>
	                <select id="analysis-case-filter"><option value="">Select prompt</option></select>
	              </div>
	              <div id="analysis-case-trend-chart" class="analysis-chart"></div>
	              <div id="analysis-case-trend-table"></div>
	            </section>
	            <section class="subsection">
	              <h2 class="subsection-title">Prompt Average Scores by Agent</h2>
	              <div class="subtle">Prompt-level machine averages grouped by agent.</div>
	              <div id="analysis-prompt-average-chart"></div>
	            </section>
	            <details class="subsection dashboard-details">
	              <summary>
	                <span>Secondary diagnostics</span>
	                <span class="subtle">agent stability, failures, and case movement</span>
	              </summary>
	              <div class="secondary-analysis-stack">
	                <section>
	                  <h2 class="subsection-title">Agent Stability</h2>
	                  <div class="subtle">Pass rate, average score, score range, and flaky cases.</div>
	                  <div id="analysis-agent-stability"></div>
	                </section>
	                <section>
	                  <h2 class="subsection-title">Failure Clusters by Dimension</h2>
	                  <div class="subtle">Failed assertions grouped by eval dimension.</div>
	                  <div id="analysis-dimension-failures"></div>
	                </section>
	                <section>
	                  <h2 class="subsection-title">Case Changes Across Runs</h2>
	                  <div class="subtle">Latest timestamp and score movement versus the previous run.</div>
	                  <div id="analysis-case-changes"></div>
	                </section>
	              </div>
	            </details>
	            <details class="subsection dashboard-details">
	              <summary>
	                <span>Planned chart backlog</span>
	                <span class="subtle">optional visuals once more run/review data exists</span>
	              </summary>
	              <div id="analysis-chart-scaffolds" class="analysis-scaffold-grid"></div>
	            </details>
	          </div>
	        </section>
	        <section id="view-database" class="view">
	          <div class="panel">
	            <div class="section-head">
	              <div>
	                <h2>Eval Case Database</h2>
	                <div class="subtle">All committed cases plus completed Slack runs, imported Promptfoo machine checks, and human reviews. Pending rows are expected until a case is run or scored.</div>
	              </div>
	              <div class="db-actions">
	                <a class="pill" href="/api/eval-cases.csv">Download CSV</a>
	                <a class="pill" href="/api/eval-cases">JSON</a>
	              </div>
	            </div>
	            <div class="filters">
	              <input id="database-search" placeholder="Search case, prompt, agent, run id, response">
	              <select id="database-agent-filter"><option value="">All agents</option></select>
	              <select id="database-state-filter">
	                <option value="">All cases</option>
	                <option value="slack">Completed Slack runs</option>
	                <option value="machine">Imported machine checks</option>
	                <option value="human">Human reviewed</option>
	                <option value="pending">Pending runs/checks</option>
	              </select>
	            </div>
	            <div class="db-table-wrap">
	              <table class="eval-db-table">
	                <colgroup>
	                  <col class="prompt-id">
	                  <col class="case-col">
	                  <col class="agent-col">
	                  <col class="status-col">
	                  <col class="run-col">
	                  <col class="status-col">
	                  <col class="score-col">
	                  <col class="run-col">
	                  <col class="analysis-col">
	                  <col class="status-col">
	                  <col class="score-col">
	                  <col class="status-col">
{human_score_colgroup}
	                  <col class="notes-col">
	                  <col class="prompt-col">
	                  <col class="response-col">
	                </colgroup>
	                <thead>
	                  <tr>
	                    <th><div class="metric-th"><div class="metric-th-title">Prompt ID</div><div class="metric-th-detail">agent sequence</div></div></th>
	                    <th><div class="metric-th"><div class="metric-th-title">Case</div><div class="metric-th-detail">case id / prompt instance</div></div></th>
	                    <th><div class="metric-th"><div class="metric-th-title">Agent</div><div class="metric-th-detail">owner</div></div></th>
	                    <th>Slack status</th>
	                    <th>Slack run / thread</th>
	                    <th>Machine assertion</th>
	                    <th>Machine score /5</th>
	                    <th>Machine run</th>
	                    <th>Analysis</th>
	                    <th>Human status</th>
	                    <th>Human avg /5</th>
	                    <th>Safety</th>
{human_score_header_cells}
	                    <th>Human notes</th>
	                    <th>Prompt</th>
	                    <th>Latest response</th>
	                  </tr>
	                </thead>
	                <tbody id="database-rows"></tbody>
	              </table>
	            </div>
	          </div>
	        </section>
	      </div>
	    </section>
  </main>
  <script id="eval-data" type="application/json">{data_json}</script>
  <script>
    const data = JSON.parse(document.getElementById('eval-data').textContent);
    const cases = data.cases;
    const scoreDimensions = data.score_dimensions || [];
    const search = document.getElementById('search');
    const agentFilter = document.getElementById('agent-filter');
    const stateFilter = document.getElementById('state-filter');
	    const promptSearch = document.getElementById('prompt-search');
	    const promptAgentFilter = document.getElementById('prompt-agent-filter');
	    const databaseSearch = document.getElementById('database-search');
	    const databaseAgentFilter = document.getElementById('database-agent-filter');
	    const databaseStateFilter = document.getElementById('database-state-filter');
	    const analysisCaseFilter = document.getElementById('analysis-case-filter');
	    const analysisAgentTrendFilter = document.getElementById('analysis-agent-trend-filter');
    const params = new URLSearchParams(window.location.search);
    if (params.get('case')) {{
      search.value = params.get('case');
    }}
    for (const agent of Object.keys(data.summary.agents).sort()) {{
      const option = document.createElement('option');
      option.value = agent;
      option.textContent = agent;
	      agentFilter.appendChild(option);
	      promptAgentFilter.appendChild(option.cloneNode(true));
	      databaseAgentFilter.appendChild(option.cloneNode(true));
	      analysisAgentTrendFilter.appendChild(option.cloneNode(true));
    }}
    if (params.get('agent')) {{
      agentFilter.value = params.get('agent');
    }}
    function pill(text, cls) {{ return `<span class="pill ${{cls || ''}}">${{escapeHtml(text)}}</span>`; }}
    function escapeHtml(value) {{
      return String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    }}
    function stateMatches(item) {{
      const state = stateFilter.value;
      if (!state) return true;
      if (state === 'pass') return item.promptfoo_success === true;
      if (state === 'fail') return item.promptfoo_success === false;
      if (state === 'pending') return item.promptfoo_success === null;
      if (state === 'human') return item.human_average !== null;
      if (state === 'slack') return Number(item.slack_run_count || 0) > 0;
      return true;
    }}
    function databaseStateMatches(item) {{
      const state = databaseStateFilter.value;
      if (!state) return true;
      const hasSlack = Number(item.slack_run_count || 0) > 0;
      const hasMachine = item.promptfoo_success !== null;
      const hasHuman = item.human_average !== null;
      if (state === 'slack') return hasSlack;
      if (state === 'machine') return hasMachine;
      if (state === 'human') return hasHuman;
      if (state === 'pending') return !hasSlack && !hasMachine && !hasHuman;
      return true;
    }}
	    function setActiveView(view, scoreNav = '') {{
	      for (const button of document.querySelectorAll('[data-view]')) {{
	        const buttonView = button.getAttribute('data-view');
	        const buttonScore = button.getAttribute('data-score-nav') || '';
	        button.classList.toggle('active', buttonView === view && buttonScore === scoreNav);
	      }}
	      for (const panel of document.querySelectorAll('.view')) {{
	        panel.classList.toggle('active', panel.id === `view-${{view}}`);
	      }}
	    }}
    function renderBars(target, entries, limit = 0) {{
      const sorted = Object.entries(entries)
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      const visible = limit ? sorted.slice(0, limit) : sorted;
      const remainder = limit && sorted.length > limit ? sorted.slice(limit) : [];
      const remainderTotal = remainder.reduce((sum, [, count]) => sum + Number(count || 0), 0);
      const max = Math.max(1, ...visible.map(([, count]) => Number(count || 0)));
      const rows = visible.map(([label, count]) => `
          <div class="bar-row">
            <div class="truncate" title="${{escapeHtml(label)}}">${{escapeHtml(label)}}</div>
            <div class="bar-track"><div class="bar" style="width:${{Math.round(count / max * 100)}}%"></div></div>
            <div class="mono">${{count}}</div>
          </div>
        `).join('');
      const more = remainder.length
        ? `<div class="subtle">+${{remainder.length}} more dimensions · ${{remainderTotal}} tagged prompts</div>`
        : '';
      document.getElementById(target).innerHTML = rows + more;
    }}
    function renderCoverageBars() {{
      const entries = Object.entries(data.summary.coverage || {{}});
      const max = Math.max(1, ...entries.map(([, stats]) => Number(stats.target || 0)));
      document.getElementById('agent-coverage-bars').innerHTML = entries
        .sort((a, b) => b[1].count - a[1].count || a[0].localeCompare(b[0]))
        .map(([agent, stats]) => {{
          const count = Number(stats.count || 0);
          const target = Number(stats.target || 0);
          const gap = Number(stats.gap || 0);
          return `
            <div class="bar-row">
              <div class="truncate" title="${{escapeHtml(agent)}}">${{escapeHtml(agent)}}</div>
              <div class="bar-track"><div class="bar ${{gap ? 'gap' : ''}}" style="width:${{Math.min(100, Math.round(count / max * 100))}}%"></div></div>
              <div class="mono">${{count}}/${{target}}</div>
            </div>
            ${{gap ? `<div style="margin: -4px 0 8px 0">${{pill(`${{gap}} short`, 'warn')}}</div>` : ''}}
          `;
        }}).join('');
    }}
    let activeScoreView = 'machine';
    function scoreViewMeta(view) {{
      if (view === 'human') {{
        return {{
          title: 'Slack Human Review Avg / 5 by Agent',
          note: 'Average 0-5 score from saved #evals human scorecards.',
          scores: data.summary.human_agent_scores || {{}},
          empty: 'No Slack human-review scores have been saved yet.',
          machine: false,
        }};
      }}
      return {{
	          title: 'Promptfoo Assertion Avg / 5 by Agent',
	          note: 'Average backend assertion score from imported Promptfoo runs, normalized to 0-5. This can be easy if assertions are broad.',
        scores: data.summary.machine_agent_scores || {{}},
        empty: 'No Promptfoo machine scores are recorded yet.',
        machine: true,
      }};
    }}
    function renderAgentScoreTable(view = activeScoreView, shouldFocus = false) {{
      activeScoreView = view;
      const meta = scoreViewMeta(view);
      document.getElementById('agent-score-title').textContent = meta.title;
      document.getElementById('agent-score-note').textContent = meta.note;
      for (const button of document.querySelectorAll('[data-score-view]')) {{
        button.classList.toggle('active', button.getAttribute('data-score-view') === view);
      }}
      for (const card of document.querySelectorAll('[data-drilldown]')) {{
        card.classList.toggle('active', card.getAttribute('data-drilldown') === view);
      }}
      const agents = Object.keys(data.summary.coverage || {{}}).sort();
      const rows = agents.map(agent => {{
        const score = meta.scores[agent] || {{}};
        const average = score.average === undefined ? '-' : scoreOutOfFive(score.average, meta.machine);
        const count = score.count || 0;
        return `
          <div class="score-row">
            <div class="truncate" title="${{escapeHtml(agent)}}">${{escapeHtml(agent)}}</div>
            <div class="mono">${{escapeHtml(average)}}</div>
            <div class="mono">${{count}}</div>
          </div>
        `;
      }}).join('');
      document.getElementById('agent-score-table').innerHTML = rows
        ? `<div class="score-row header"><div>Agent</div><div>Avg</div><div>Cases</div></div>${{rows}}`
        : `<div class="subtle">${{escapeHtml(meta.empty)}}</div>`;
      if (shouldFocus) {{
        document.getElementById('agent-score-drilldown').scrollIntoView({{ block: 'start', behavior: 'smooth' }});
        document.getElementById('agent-score-drilldown').focus({{ preventScroll: true }});
      }}
    }}
    function scoreOutOfFive(value, machine = false) {{
      if (value === null || value === undefined || value === '-') return '-';
      const number = Number(value);
      if (!Number.isFinite(number)) return '-';
      const scaled = machine ? number * 5 : number;
      return `${{scaled.toFixed(1)}}/5`;
    }}
    function scorePills(scores) {{
      const entries = Object.entries(scores || {{}});
      if (!entries.length) return pill('no human scores yet', 'warn');
      return entries
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(([label, value]) => pill(`${{label}} ${{value}}`, ''))
        .join('');
    }}
    function formatDateTime(value) {{
      if (!value) return '';
      const parsed = new Date(value);
      if (Number.isNaN(parsed.getTime())) return String(value);
      return parsed.toLocaleString([], {{
        month: 'short',
        day: 'numeric',
        hour: 'numeric',
        minute: '2-digit',
      }});
    }}
    function caseTimelinePills(item) {{
      const chips = [];
      chips.push(item.updated_at
        ? pill(`Promptfoo ${{formatDateTime(item.updated_at)}}`, item.promptfoo_success === false ? 'fail' : 'pass')
        : pill('Promptfoo not run yet', 'warn'));
      chips.push(Number(item.slack_run_count || 0) > 0
        ? pill(`Slack ${{formatDateTime(item.latest_slack_created_at) || 'linked'}}`, 'pass')
        : pill('Slack test not run yet', 'warn'));
      chips.push(item.human_created_at
        ? pill(`Human ${{formatDateTime(item.human_created_at)}}`, item.human_safety === 'fail' ? 'fail' : 'pass')
        : pill('Human unreviewed', 'warn'));
      return chips.join('');
    }}
    function statusBox(title, main, date, detail, cls = '') {{
      const statusClass = cls ? ` ${{cls}}` : '';
      return `<div class="status-box${{statusClass}}">
        <div class="status-title">${{escapeHtml(title)}}</div>
        <div class="status-main">${{escapeHtml(main)}}</div>
        ${{date ? `<div class="status-date">${{escapeHtml(date)}}</div>` : ''}}
        ${{detail ? `<div class="status-date mono">${{escapeHtml(detail)}}</div>` : ''}}
      </div>`;
    }}
    function mergedScoringStatus(item) {{
      const machineMain = item.promptfoo_success === true
        ? `Pass ${{scoreOutOfFive(item.promptfoo_score, true)}}`
        : item.promptfoo_success === false
          ? `Fail ${{scoreOutOfFive(item.promptfoo_score, true)}}`
          : 'Not run';
      const machineDate = item.updated_at ? formatDateTime(item.updated_at) : '';
      const slackMain = Number(item.slack_run_count || 0) > 0
        ? `${{item.slack_run_count}} saved run${{Number(item.slack_run_count || 0) === 1 ? '' : 's'}}`
        : 'Not tested in Slack';
      const slackDate = item.latest_slack_created_at ? formatDateTime(item.latest_slack_created_at) : '';
      const slackDetail = item.latest_slack_run_id || item.latest_slack_thread_ts || '';
      const humanMain = item.human_average !== null
        ? `${{scoreOutOfFive(item.human_average)}} ${{item.human_safety ? `· ${{item.human_safety}}` : ''}}`
        : 'Unreviewed';
      const humanDate = item.human_created_at ? formatDateTime(item.human_created_at) : '';
      return `<div class="status-grid">
        ${{statusBox('Promptfoo assertion', machineMain, machineDate, item.promptfoo_eval_id || '', item.promptfoo_success === false ? 'fail' : item.promptfoo_success === true ? 'pass' : 'warn')}}
        ${{statusBox('Slack test run', slackMain, slackDate, slackDetail, Number(item.slack_run_count || 0) > 0 ? 'pass' : 'warn')}}
        ${{statusBox('Human review', humanMain, humanDate, item.human_notes || '', item.human_safety === 'fail' ? 'fail' : item.human_average !== null ? 'pass' : 'warn')}}
      </div>`;
    }}
    function agentScorePills(agentScores) {{
      const entries = Object.entries(agentScores || {{}});
      if (!entries.length) return pill('agent averages not recorded', 'warn');
      return entries
        .sort((a, b) => a[0].localeCompare(b[0]))
        .map(([agent, stats]) => pill(`${{agent}} ${{scoreOutOfFive(stats.average_score, true)}} (${{stats.case_count ?? 0}})`, ''))
        .join('');
    }}
    function renderEvalRuns() {{
      const target = document.getElementById('eval-run-scores');
      if (!target) return;
      const rows = data.eval_runs || [];
      target.innerHTML = rows.length ? rows.map(run => {{
        const total = Number(run.total || 0);
        const successes = Number(run.successes || 0);
        const failures = Number(run.failures || 0);
        const errors = Number(run.errors || 0);
        const passRate = total ? Math.round(successes / total * 100) : 0;
        const state = failures || errors ? 'fail' : (run.average_score === null || run.average_score === undefined ? 'warn' : 'pass');
        const avg = run.average_score === null || run.average_score === undefined
          ? pill('run avg not recorded', 'warn')
	          : pill(`assertion avg ${{scoreOutOfFive(run.average_score, true)}}`, state === 'fail' ? 'warn' : 'pass');
        return `<div class="run-card ${{state}}">
          <div class="run-head">
            <div>
              <div class="run-title mono">${{escapeHtml(run.eval_id || '')}}</div>
              <div class="subtle">${{escapeHtml(run.created_at || run.imported_at || '')}}</div>
            </div>
            <div>${{avg}}</div>
          </div>
          <div class="run-stats">
            ${{pill(`${{successes}}/${{total}} passed`, state === 'fail' ? 'warn' : 'pass')}}
            ${{pill(`${{passRate}}% pass rate`, state === 'fail' ? 'warn' : 'pass')}}
            ${{failures ? pill(`${{failures}} failed`, 'fail') : ''}}
            ${{errors ? pill(`${{errors}} errors`, 'fail') : ''}}
          </div>
          <div class="run-meter"><span style="width:${{passRate}}%"></span></div>
          <div class="score-grid" style="margin-top:7px">${{agentScorePills(run.agent_scores)}}</div>
        </div>`;
      }}).join('') : '<div class="subtle">No imported Promptfoo eval runs yet.</div>';
    }}
    function formatPercent(value) {{
      if (value === null || value === undefined) return '-';
      const number = Number(value);
      return Number.isFinite(number) ? `${{number.toFixed(1)}}%` : '-';
    }}
    function signedNumber(value, suffix = '', digits = 1) {{
      if (value === null || value === undefined) return '-';
      const number = Number(value);
      if (!Number.isFinite(number)) return '-';
      const sign = number > 0 ? '+' : '';
      return `${{sign}}${{number.toFixed(digits)}}${{suffix}}`;
    }}
    function analysisCaseLabel(caseId) {{
      return cases.find(item => item.case_id === caseId)?.display_case_id || caseId;
    }}
    function scoreLine(scores, limit = 4) {{
      const entries = Object.entries(scores || {{}})
        .filter(([, value]) => value !== null && value !== undefined && value !== '')
        .sort((a, b) => a[0].localeCompare(b[0]));
      if (!entries.length) return '';
      const visible = entries.slice(0, limit).map(([label, value]) => `${{label}} ${{value}}`);
      const hidden = entries.length - visible.length;
      return hidden ? `${{visible.join(' · ')}} · +${{hidden}} more` : visible.join(' · ');
    }}
    function dbMetric(value, fallback = 'tbd') {{
      if (value === null || value === undefined || value === '') {{
        return `<span class="db-empty">${{escapeHtml(fallback)}}</span>`;
      }}
      return escapeHtml(value);
    }}
    function scoreValue(value, machine = false) {{
      const formatted = scoreOutOfFive(value, machine);
      return formatted === '-' ? `<span class="db-empty">tbd</span>` : escapeHtml(formatted);
    }}
    function analysisCard(label, value, detail = '') {{
      return `<div class="analysis-card">
        <div class="label">${{escapeHtml(label)}}</div>
        <div class="value">${{escapeHtml(value)}}</div>
        ${{detail ? `<div class="subtle">${{escapeHtml(detail)}}</div>` : ''}}
      </div>`;
    }}
    function miniBar(label, value, max, detail = '') {{
      const safeMax = Math.max(1, Number(max || 0));
      const safeValue = Math.max(0, Number(value || 0));
      const pct = Math.max(0, Math.min(100, safeValue / safeMax * 100));
      return `<div class="mini-bar-row">
        <div class="truncate" title="${{escapeHtml(label)}}">${{escapeHtml(label)}}</div>
        <div class="mini-bar-track" aria-hidden="true"><div class="mini-bar-fill" style="width:${{pct.toFixed(1)}}%"></div></div>
        <div class="mono" title="${{escapeHtml(detail)}}">${{escapeHtml(String(value ?? 0))}}</div>
      </div>`;
    }}
    function scaffoldBarRows(rows) {{
      if (!rows.length) return '<div class="subtle">Waiting for data.</div>';
      const max = Math.max(1, ...rows.map(row => Number(row.value || 0)));
      return `<div class="scaffold-bars">${{rows.map(row => {{
        const pct = Math.max(0, Math.min(100, Number(row.value || 0) / max * 100));
        return `<div class="scaffold-bar-row">
          <div class="truncate" title="${{escapeHtml(row.label)}}">${{escapeHtml(row.label)}}</div>
          <div class="mini-bar-track" aria-hidden="true"><div class="mini-bar-fill" style="width:${{pct.toFixed(1)}}%"></div></div>
          <div class="mono">${{escapeHtml(row.display ?? String(row.value ?? 0))}}</div>
        </div>`;
      }}).join('')}}</div>`;
    }}
    function scaffoldLine(points, label = 'trend') {{
      const rows = points.filter(value => value !== null && value !== undefined && Number.isFinite(Number(value)));
      if (!rows.length) return '<div class="subtle">Waiting for trend data.</div>';
      const width = 220;
      const height = 72;
      const pad = 8;
      const min = Math.min(...rows.map(Number), 0);
      const max = Math.max(...rows.map(Number), 1);
      const denom = Math.max(1, rows.length - 1);
      const span = Math.max(0.001, max - min);
      const line = rows.map((value, index) => {{
        const x = pad + (index / denom) * (width - pad * 2);
        const y = pad + (1 - (Number(value) - min) / span) * (height - pad * 2);
        return `${{x.toFixed(1)}},${{y.toFixed(1)}}`;
      }}).join(' ');
      return `<svg class="scaffold-line" viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="${{escapeHtml(label)}}">
        <line x1="${{pad}}" y1="${{height - pad}}" x2="${{width - pad}}" y2="${{height - pad}}" stroke="#edf0f2" />
        <polyline points="${{line}}" fill="none" stroke="#2f6f41" stroke-width="3" stroke-linejoin="round" stroke-linecap="round" />
        ${{rows.map((value, index) => {{
          const x = pad + (index / denom) * (width - pad * 2);
          const y = pad + (1 - (Number(value) - min) / span) * (height - pad * 2);
          return `<circle cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="3" fill="#1b1f23"><title>${{escapeHtml(String(value))}}</title></circle>`;
        }}).join('')}}
      </svg>`;
    }}
    function scaffoldHeatmap(labels, values) {{
      const max = Math.max(1, ...values.map(value => Number(value || 0)));
      const cells = labels.slice(0, 18).map((label, index) => {{
        const value = Number(values[index] || 0);
        const opacity = Math.max(0.12, Math.min(0.9, value / max));
        return `<span class="scaffold-heat-cell" title="${{escapeHtml(label)}}: ${{escapeHtml(String(value))}}" style="background:rgba(47,111,65,${{opacity.toFixed(2)}})"></span>`;
      }}).join('');
      return cells ? `<div class="scaffold-heatmap" aria-label="Dimension coverage heatmap">${{cells}}</div>` : '<div class="subtle">Waiting for dimension data.</div>';
    }}
    function scaffoldCard(title, detail, body = '') {{
      return `<div class="analysis-scaffold-card">
        <h3>${{escapeHtml(title)}}</h3>
        <div class="subtle">${{escapeHtml(detail)}}</div>
        <div class="scaffold-visual">${{body || '<div class="subtle">Waiting for data.</div>'}}</div>
      </div>`;
    }}
    function workflowStep(label, value, detail = '', numerator = null, denominator = null) {{
      const hasBar = numerator !== null && denominator !== null && Number(denominator) > 0;
      const percent = hasBar
        ? Math.max(0, Math.min(100, Number(numerator || 0) / Number(denominator || 1) * 100))
        : 0;
      return `<div class="workflow-step">
        <div class="label">${{escapeHtml(label)}}</div>
        <div class="value">${{escapeHtml(value)}}</div>
        ${{hasBar ? `<div class="workflow-stage-bar" aria-hidden="true"><div class="workflow-stage-fill" style="width:${{percent.toFixed(1)}}%"></div></div>` : ''}}
        ${{detail ? `<div class="subtle">${{escapeHtml(detail)}}</div>` : ''}}
      </div>`;
    }}
    function workflowGuardrailRow(step, currentMode, futureTrigger, costGuardrail, liveNow, dashboardState) {{
      return `<tr>
        <td>${{escapeHtml(step)}}</td>
        <td>${{escapeHtml(currentMode)}}</td>
        <td>${{escapeHtml(futureTrigger)}}</td>
        <td>${{escapeHtml(costGuardrail)}}</td>
        <td>${{liveNow ? pill('Yes', 'warn') : '<span class="db-empty">No</span>'}}</td>
        <td>${{escapeHtml(dashboardState)}}</td>
      </tr>`;
    }}
    function workflowInteractionContract(fallbackCounts = {{}}) {{
      const readiness = data.workflow_readiness || {{}};
      const interactions = Array.isArray(readiness.interactions) && readiness.interactions.length
        ? readiness.interactions
        : [
            {{
              interaction: 'Prompt copy',
              current_behavior: 'Local prompt library row only',
              future_live_trigger: 'Human pastes root message into #evals',
              cost_guardrail: 'No API call from dashboard copy',
              stored_evidence: `${{fallbackCounts.total || 0}} local cases`,
            }},
            {{
              interaction: 'Agent thread reply',
              current_behavior: 'Previewed from saved response/result text',
              future_live_trigger: 'Slack app mention in the selected thread',
              cost_guardrail: 'One agent run per accepted Slack root prompt',
              stored_evidence: `${{fallbackCounts.slackRuns || 0}} saved Slack runs`,
            }},
            {{
              interaction: 'Promptfoo machine summary',
              current_behavior: 'Shows latest imported assertion status and score by case_id',
              future_live_trigger: 'Resolved Slack case has an imported Promptfoo result',
              cost_guardrail: 'Use imported Promptfoo result by case_id; do not rerun Promptfoo from Slack thread',
              stored_evidence: `${{fallbackCounts.analysisReady || 0}} included machine rows`,
            }},
            {{
              interaction: 'Retrieval/source evidence',
              current_behavior: 'Shows tags and recorded evidence already in the eval database',
              future_live_trigger: 'Accepted agent run needs source retrieval',
              cost_guardrail: 'Use dry-run fixtures/cache first; cap live retrieval to accepted root run',
              stored_evidence: `${{fallbackCounts.retrievalTagged || 0}} retrieval/source-tagged cases`,
            }},
            {{
              interaction: 'Human review form open',
              current_behavior: 'Review controls stay disabled until a response exists',
              future_live_trigger: 'Human opens the review form or dashboard scoring panel',
              cost_guardrail: 'No model call; form uses saved case, run id, thread, and response context',
              stored_evidence: `${{fallbackCounts.recordedResponses || 0}} recorded responses`,
            }},
            {{
              interaction: 'Human review save',
              current_behavior: 'Writes local SQLite score fields only',
              future_live_trigger: 'Human submits reviewed scores',
              cost_guardrail: 'No model call; no Slack post',
              stored_evidence: `${{fallbackCounts.humanReviews || 0}} saved scorecards`,
            }},
            {{
              interaction: 'Analysis inclusion',
              current_behavior: 'Included unless marked duplicate/problem run',
              future_live_trigger: 'Operator toggles analysis inclusion',
              cost_guardrail: 'No rerun; recalculates from database rows',
              stored_evidence: `${{fallbackCounts.analysisReady || 0}} included machine rows`,
            }},
          ];
      return `<details class="workflow-disclosure workflow-contract" aria-label="Slack eval interaction readiness and cost guardrails">
        <summary>
          <span>Interaction guardrails</span>
          <span class="subtle">Slack/API touchpoints and cost controls</span>
        </summary>
        <div class="db-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Interaction</th>
                <th>Current dashboard behavior</th>
                <th>Future live trigger</th>
                <th>Cost guardrail</th>
                <th>Live now</th>
                <th>Stored evidence</th>
              </tr>
            </thead>
            <tbody>
              ${{interactions.map(item => workflowGuardrailRow(
                item.interaction || '',
                item.current_behavior || '',
                item.future_live_trigger || '',
                item.cost_guardrail || '',
                item.live_api_call_now === true,
                item.stored_evidence || ''
              )).join('')}}
            </tbody>
          </table>
        </div>
      </details>`;
    }}
    function workflowStarterRunPlan() {{
      const plan = data.workflow_readiness?.starter_run_plan || {{}};
      const liveSteps = Array.isArray(plan.expected_live_calls_when_enabled)
        ? plan.expected_live_calls_when_enabled
        : [];
      const caseLabel = plan.display_case_id || plan.case_id || 'Select a committed prompt';
      const agent = plan.agent || 'agent pending';
      const promptNumber = plan.prompt_number ? ` · prompt ${{plan.prompt_number}}` : '';
      const pasteText = plan.paste_text || 'Choose a Prompt Library case before starting a Slack eval run.';
      const dashboardUrl = plan.dashboard_url || '';
      const reviewUrl = plan.human_review_url || '';
      const threadSequence = Array.isArray(plan.thread_sequence) ? plan.thread_sequence : [];
      return `<details class="workflow-disclosure workflow-run-plan" aria-label="Suggested Slack test run">
        <summary>
          <span>Suggested Slack test run</span>
          <span class="subtle">${{escapeHtml(caseLabel)}} · local-only</span>
        </summary>
        <div class="run-plan-grid">
          <div class="run-plan-box">
            <div class="label">Start with</div>
            <div class="mono">${{escapeHtml(caseLabel)}}</div>
            <div class="subtle">${{escapeHtml(agent)}}${{escapeHtml(promptNumber)}}</div>
            ${{dashboardUrl ? `<div class="subtle">Case dashboard: <span class="mono">${{escapeHtml(dashboardUrl)}}</span></div>` : ''}}
            ${{reviewUrl ? `<div class="subtle">Human review form: <span class="mono">${{escapeHtml(reviewUrl)}}</span></div>` : ''}}
            <ol class="run-plan-list">
              <li>Paste the prompt into <span class="mono">${{escapeHtml(plan.channel || '#evals')}}</span> as the root message.</li>
              <li>Keep all follow-ups in the same Slack thread.</li>
              <li>Expect an in-thread eval footer with Promptfoo machine-check status, review form link, and dashboard link.</li>
              <li>Open the human review form or dashboard scoring panel from the eval footer link; no extra agent response is needed.</li>
              <li>After scores save, ask <span class="mono">${{escapeHtml(plan.status_reply || '@KNI how is this eval doing?')}}</span>.</li>
            </ol>
          </div>
          <div class="run-plan-box">
            <div class="label">Paste into Slack</div>
            <div class="run-plan-prompt">${{escapeHtml(pasteText)}}</div>
            <div class="label" style="margin-top:10px;">Expected live calls when enabled</div>
            <ul class="run-plan-list">
              ${{liveSteps.map(step => `<li><strong>${{escapeHtml(step.step || '')}}</strong>: ${{escapeHtml(step.expected_call || '')}} <span class="subtle">${{escapeHtml(step.cost_guardrail || '')}}</span></li>`).join('')}}
            </ul>
          </div>
        </div>
        ${{threadSequence.length ? `<div class="run-plan-box" style="margin-top:10px;">
          <div class="label">Expected in-thread communication flow</div>
          <ol class="run-plan-list">
            ${{threadSequence.map(item => `<li><strong>${{escapeHtml(item.speaker || '')}}</strong>: ${{escapeHtml(item.message || '')}} <span class="subtle">${{escapeHtml(item.stored_as || '')}}</span></li>`).join('')}}
          </ol>
        </div>` : ''}}
      </details>`;
    }}
    function renderWorkflowVisualization() {{
      const target = document.getElementById('workflow-visualization');
      if (!target) return;
      const total = cases.length;
      const promptLibrary = Number(data.summary?.seed_case_total || total || 0);
      const slackRuns = cases.filter(item => Number(item.slack_run_count || 0) > 0).length;
      const recordedResponses = cases.filter(item => hasRecordedResponse(item)).length;
      const humanReviews = cases.filter(item => item.human_average !== null && item.human_average !== undefined).length;
      const analysisReady = cases.filter(item => item.promptfoo_eval_id && !item.analysis_excluded).length;
      const retrievalTagged = cases.filter(item => (item.dimensions || []).some(dimension => [
        'retrieval',
        'retrieval_boundary',
        'retrieval_policy',
        'search',
        'search_quality',
        'source_quality',
        'source_relevance',
        'source_sufficiency',
        'source_visibility',
      ].includes(String(dimension || '')))).length;
      target.innerHTML = `
        <div class="workflow-steps">
          ${{workflowStep('Prompt library', `${{promptLibrary}} cases`, 'copy into #evals', promptLibrary, Math.max(total, promptLibrary))}}
          ${{workflowStep('Slack runs', `${{slackRuns}}/${{total}}`, 'saved thread run', slackRuns, total)}}
          ${{workflowStep('Responses', `${{recordedResponses}}/${{total}}`, 'unlock review', recordedResponses, total)}}
          ${{workflowStep('Scorecards', `${{humanReviews}}/${{total}}`, 'human review saved', humanReviews, total)}}
          ${{workflowStep('Analysis', `${{analysisReady}}/${{total}}`, 'included rows', analysisReady, total)}}
          ${{workflowStep('Source checks', `${{retrievalTagged}}/${{promptLibrary}}`, 'retrieval/evidence tags', retrievalTagged, promptLibrary)}}
        </div>
        <details class="workflow-disclosure workflow-thread" aria-label="Slack eval thread preview">
          <summary>
            <span>Thread preview</span>
            <span class="subtle">what should appear in #evals after one agent run</span>
          </summary>
          <div class="workflow-chat">
            <div class="chat-row">
              <div class="chat-speaker">Human</div>
              <div class="chat-bubble">Paste a committed Prompt Library case into <span class="mono">#evals</span>.</div>
            </div>
            <div class="chat-row">
              <div class="chat-speaker">KNI</div>
              <div class="chat-bubble system">Reply once in-thread with the agent result, case id, run id, and dashboard link.</div>
            </div>
            <div class="chat-row">
              <div class="chat-speaker">Promptfoo</div>
              <div class="chat-bubble system">Append imported machine-check status plus dashboard and review links; no Promptfoo rerun.</div>
            </div>
            <div class="chat-row">
              <div class="chat-speaker">Review</div>
              <div class="chat-bubble system">Score from the linked form using the saved prompt, response, and machine check.</div>
            </div>
            <div class="chat-row">
              <div class="chat-speaker">Dashboard</div>
              <div class="chat-bubble system">Update runs, database, and analysis from saved rows without extra model calls.</div>
            </div>
          </div>
        </details>
        ${{workflowStarterRunPlan()}}
        ${{workflowInteractionContract({{ total, slackRuns, recordedResponses, humanReviews, analysisReady, retrievalTagged }})}}`;
    }}
    function renderAnalysisChartScaffolds() {{
      const target = document.getElementById('analysis-chart-scaffolds');
      if (!target) return;
      const total = cases.length;
      const machineRows = cases.filter(item => item.promptfoo_eval_id).length;
      const slackRuns = cases.filter(item => Number(item.slack_run_count || 0) > 0).length;
      const recordedResponses = cases.filter(item => hasRecordedResponse(item)).length;
      const humanReviews = cases.filter(item => item.human_average !== null && item.human_average !== undefined).length;
      const analysisIncluded = cases.filter(item => item.promptfoo_eval_id && !item.analysis_excluded).length;
      const excludedRuns = cases.filter(item => item.analysis_excluded).length;
      const retrievalTagged = cases.filter(item => (item.dimensions || []).some(dimension => [
        'retrieval',
        'retrieval_boundary',
        'retrieval_policy',
        'search',
        'search_quality',
        'source_quality',
        'source_relevance',
        'source_sufficiency',
        'source_visibility',
      ].includes(String(dimension || '')))).length;
      const promptAverageRows = data.analysis?.prompt_score_averages || [];
      const scoreBuckets = [
        {{ label: '0-2.9', value: 0 }},
        {{ label: '3.0-3.9', value: 0 }},
        {{ label: '4.0-4.4', value: 0 }},
        {{ label: '4.5-5.0', value: 0 }},
      ];
      for (const row of promptAverageRows) {{
        const score = Number(row.average_score || 0) * 5;
        if (score < 3) scoreBuckets[0].value += 1;
        else if (score < 4) scoreBuckets[1].value += 1;
        else if (score < 4.5) scoreBuckets[2].value += 1;
        else scoreBuckets[3].value += 1;
      }}
      const agentRows = (data.analysis?.agent_stability || []).map(row => ({{
        label: row.agent || 'unknown',
        value: row.average_score === null || row.average_score === undefined ? 0 : Number(row.average_score) * 5,
        display: scoreOutOfFive(row.average_score, true),
      }}));
      const dimensionEntries = Object.entries(data.summary?.dimensions || {{}})
        .sort((a, b) => Number(b[1]) - Number(a[1]) || a[0].localeCompare(b[0]));
      const runTrendValues = (data.analysis?.run_trends || []).map(row => row.pass_rate);
      const humanTrendValues = (data.analysis?.human_review_trends || []).map(row => (
        row.average_score === null || row.average_score === undefined ? null : Number(row.average_score) / 5 * 100
      ));
      target.innerHTML = [
        scaffoldCard(
          'Review completion funnel',
          'Stage bars for committed cases, saved Slack responses, and completed human reviews.',
          scaffoldBarRows([
            {{ label: 'Committed cases', value: total }},
            {{ label: 'Slack responses', value: recordedResponses }},
            {{ label: 'Human reviews', value: humanReviews }},
          ])
        ),
        scaffoldCard(
          'Machine vs human coverage',
          'Grouped bars for Promptfoo machine checks beside saved human scorecards.',
          scaffoldBarRows([
            {{ label: 'Machine checks', value: machineRows }},
            {{ label: 'Human reviews', value: humanReviews }},
            {{ label: 'Included rows', value: analysisIncluded }},
          ])
        ),
        scaffoldCard(
          'Cost-safe run volume',
          'One AI agent call per root eval; Promptfoo lookup, form save, and analysis are database-backed.',
          scaffoldBarRows([
            {{ label: 'Root Slack runs', value: slackRuns }},
            {{ label: 'Retrieval-tagged', value: retrievalTagged }},
            {{ label: 'Excluded runs', value: excludedRuns }},
          ])
        ),
        scaffoldCard(
          'Prompt score distribution',
          'Histogram scaffold for prompt-level average machine scores by included run day.',
          scaffoldBarRows(scoreBuckets.map(row => ({{ ...row, display: String(row.value) }})))
        ),
        scaffoldCard(
          'Agent score comparison',
          'Horizontal ranking scaffold for average machine score by agent, with human line added when reviews exist.',
          scaffoldBarRows(agentRows)
        ),
        scaffoldCard(
          'Dimension readiness heatmap',
          'Matrix scaffold for quality dimensions such as retrieval, source quality, synthesis, safety, and format.',
          scaffoldHeatmap(
            dimensionEntries.map(([label]) => label),
            dimensionEntries.map(([, value]) => Number(value || 0)),
          )
        ),
        scaffoldCard(
          'Machine trend line',
          'Line scaffold for Promptfoo pass rate over imported runs.',
          scaffoldLine(runTrendValues, 'Promptfoo pass-rate trend')
        ),
        scaffoldCard(
          'Human review trend line',
          'Line scaffold for daily human average score once reviews exist.',
          humanTrendValues.some(value => value !== null && value !== undefined)
            ? scaffoldLine(humanTrendValues, 'Human review trend')
            : '<div class="subtle">Waiting for saved human reviews.</div><div class="scaffold-note">The line appears after at least one scorecard; the daily trend is best after 4+ reviews per day.</div>'
        ),
      ].join('');
    }}
    function renderAnalysisSummary() {{
      const analysis = data.analysis || {{}};
      const trends = analysis.run_trends || [];
      const latest = trends[trends.length - 1] || {{}};
      const fix = analysis.fix_signal || {{}};
      const latestDetail = latest.eval_id
        ? `${{latest.successes || 0}}/${{latest.total || 0}} passed · ${{formatDateTime(latest.created_at) || latest.created_at || ''}}`
        : 'No imported Promptfoo runs yet';
      const avgDelta = fix.average_score_delta === null || fix.average_score_delta === undefined
        ? '-'
        : signedNumber(Number(fix.average_score_delta || 0) * 5, ' /5', 2);
      document.getElementById('analysis-summary').innerHTML = [
        analysisCard('Latest pass rate', formatPercent(latest.pass_rate), latestDetail),
        analysisCard('Pass-rate change', signedNumber(fix.pass_rate_delta, ' pp'), fix.previous_eval_id ? `vs ${{fix.previous_eval_id}}` : 'needs two runs'),
        analysisCard('Assertion avg change', avgDelta, 'normalized to 0-5'),
        analysisCard('Case movement', `${{fix.improved_cases || 0}} improved · ${{fix.regressed_cases || 0}} regressed`, `${{fix.stable_cases || 0}} stable cases`),
      ].join('');
    }}
    function linePoints(rows, valueAccessor, width, height, pad) {{
      const values = rows.map((row, index) => ({{ row, index, value: valueAccessor(row) }}))
        .filter(item => item.value !== null && item.value !== undefined && Number.isFinite(Number(item.value)));
      if (!values.length) return '';
      const denom = Math.max(1, rows.length - 1);
      return values.map(item => {{
        const x = pad.left + (item.index / denom) * (width - pad.left - pad.right);
        const y = pad.top + (1 - Number(item.value) / 100) * (height - pad.top - pad.bottom);
        return `${{x.toFixed(1)}},${{y.toFixed(1)}}`;
      }}).join(' ');
    }}
    function indexedLinePoints(rows, indexAccessor, valueAccessor, width, height, pad) {{
      const values = rows.map(row => ({{ row, index: Number(indexAccessor(row)), value: valueAccessor(row) }}))
        .filter(item => Number.isFinite(item.index) && item.value !== null && item.value !== undefined && Number.isFinite(Number(item.value)));
      if (!values.length) return '';
      const maxIndex = Math.max(1, ...values.map(item => item.index));
      return values.map(item => {{
        const x = pad.left + (item.index / maxIndex) * (width - pad.left - pad.right);
        const y = pad.top + (1 - Number(item.value) / 100) * (height - pad.top - pad.bottom);
        return `${{x.toFixed(1)}},${{y.toFixed(1)}}`;
      }}).join(' ');
    }}
    function renderAnalysisRunChart() {{
      const trend = data.analysis?.agent_score_trends || {{ agents: [], series: {{ all: [] }} }};
      const selected = analysisAgentTrendFilter?.value || 'all';
      const rows = (trend.series?.[selected] || trend.series?.all || []).slice();
      const target = document.getElementById('analysis-run-chart');
      const table = document.getElementById('analysis-agent-trend-table');
      if (!rows.length) {{
        target.innerHTML = '<div class="subtle">No included Promptfoo or human review score history yet.</div>';
        table.innerHTML = '';
        return;
      }}
      const width = 760;
      const height = 190;
      const pad = {{ left: 44, right: 14, top: 18, bottom: 32 }};
      const machinePoints = linePoints(
        rows,
        row => row.machine_average === null || row.machine_average === undefined ? null : Number(row.machine_average) / 5 * 100,
        width,
        height,
        pad,
      );
      const humanPoints = linePoints(
        rows,
        row => row.human_average === null || row.human_average === undefined ? null : Number(row.human_average) / 5 * 100,
        width,
        height,
        pad,
      );
      const machineCircles = rows.map((row, index) => {{
        if (row.machine_average === null || row.machine_average === undefined) return '';
        const x = pad.left + (index / Math.max(1, rows.length - 1)) * (width - pad.left - pad.right);
        const y = pad.top + (1 - Number(row.machine_average) / 5) * (height - pad.top - pad.bottom);
        return `<circle cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="4" fill="#2f6f41"><title>${{escapeHtml(row.date)}} · machine ${{scoreValue(row.machine_average)}} · ${{row.machine_count || 0}} prompt${{Number(row.machine_count || 0) === 1 ? '' : 's'}}</title></circle>`;
      }}).join('');
      const humanCircles = rows.map((row, index) => {{
        if (row.human_average === null || row.human_average === undefined) return '';
        const x = pad.left + (index / Math.max(1, rows.length - 1)) * (width - pad.left - pad.right);
        const y = pad.top + (1 - Number(row.human_average) / 5) * (height - pad.top - pad.bottom);
        return `<circle cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="4" fill="#8a6f2a"><title>${{escapeHtml(row.date)}} · human ${{scoreValue(row.human_average)}} · ${{row.human_count || 0}} review${{Number(row.human_count || 0) === 1 ? '' : 's'}}</title></circle>`;
      }}).join('');
      target.innerHTML = `<svg viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="Average machine and human score over time">
        <line x1="${{pad.left}}" y1="${{pad.top}}" x2="${{pad.left}}" y2="${{height - pad.bottom}}" stroke="#d9dee3" />
        <line x1="${{pad.left}}" y1="${{height - pad.bottom}}" x2="${{width - pad.right}}" y2="${{height - pad.bottom}}" stroke="#d9dee3" />
        ${{[0, 2.5, 5].map(value => {{
          const y = pad.top + (1 - value / 5) * (height - pad.top - pad.bottom);
          return `<line x1="${{pad.left}}" y1="${{y}}" x2="${{width - pad.right}}" y2="${{y}}" stroke="#edf0f2" /><text x="4" y="${{y + 4}}" fill="#687076" font-size="11">${{value}}/5</text>`;
        }}).join('')}}
        <polyline points="${{machinePoints}}" fill="none" stroke="#2f6f41" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" />
        ${{humanPoints ? `<polyline points="${{humanPoints}}" fill="none" stroke="#8a6f2a" stroke-width="2.5" stroke-dasharray="2 4" stroke-linejoin="round" stroke-linecap="round" />` : ''}}
        ${{machineCircles}}
        ${{humanCircles}}
        <text x="${{pad.left}}" y="${{height - 8}}" fill="#687076" font-size="11">${{escapeHtml(rows[0].date || '')}}</text>
        <text x="${{width - pad.right}}" y="${{height - 8}}" fill="#687076" font-size="11" text-anchor="end">${{escapeHtml(rows[rows.length - 1].date || '')}}</text>
        <text x="${{width - 210}}" y="18" fill="#2f6f41" font-size="12">machine avg</text>
        ${{humanPoints ? `<text x="${{width - 105}}" y="18" fill="#8a6f2a" font-size="12">human avg</text>` : ''}}
      </svg>
      ${{humanPoints ? '' : '<div class="subtle">No saved human-review average trend yet for this selection.</div>'}}`;
      table.innerHTML = `<table class="analysis-table">
        <thead><tr><th>Date</th><th>Machine avg</th><th>Machine prompts</th><th>Human avg</th><th>Human reviews</th></tr></thead>
        <tbody>${{rows.map(row => `<tr>
          <td class="mono">${{escapeHtml(row.date || '')}}</td>
          <td class="mono">${{scoreValue(row.machine_average)}}</td>
          <td class="mono">${{row.machine_count || '<span class="db-empty">tbd</span>'}}</td>
          <td class="mono">${{scoreValue(row.human_average)}}</td>
          <td class="mono">${{row.human_count || '<span class="db-empty">tbd</span>'}}</td>
        </tr>`).join('')}}</tbody>
      </table>`;
    }}
    function renderAnalysisCaseSelector() {{
      const rows = data.analysis?.case_trends || [];
      analysisCaseFilter.innerHTML = '<option value="">Select prompt</option>' + rows.map(row => (
        `<option value="${{escapeHtml(row.case_id)}}">${{escapeHtml(analysisCaseLabel(row.case_id))}}</option>`
      )).join('');
      if (!analysisCaseFilter.value && rows.length) {{
        analysisCaseFilter.value = rows[0].case_id;
      }}
    }}
    function renderAnalysisCaseTrend() {{
      const rows = data.analysis?.case_trends || [];
      const selected = analysisCaseFilter.value || rows[0]?.case_id || '';
      const item = rows.find(row => row.case_id === selected);
      const chart = document.getElementById('analysis-case-trend-chart');
      const table = document.getElementById('analysis-case-trend-table');
      if (!item || !(item.runs || []).length) {{
        chart.innerHTML = '<div class="subtle">No Promptfoo case history is available yet.</div>';
        table.innerHTML = '';
        return;
      }}
      const runs = item.runs || [];
      const humanTrend = (data.analysis?.human_case_trends || []).find(row => row.case_id === selected);
      const humanDays = humanTrend?.days || [];
      const dateKey = value => String(value || '').slice(0, 10);
      const allDays = [...new Set([
        ...runs.map(row => dateKey(row.created_at)),
        ...humanDays.map(row => dateKey(row.date)),
      ].filter(Boolean))].sort();
      const dayIndex = new Map(allDays.map((day, index) => [day, index]));
      const width = 760;
      const height = 170;
      const pad = {{ left: 44, right: 14, top: 18, bottom: 32 }};
      const machinePoints = indexedLinePoints(
        runs,
        row => dayIndex.get(dateKey(row.created_at)) ?? 0,
        row => Number(row.score || 0) * 100,
        width,
        height,
        pad,
      );
      const humanPoints = indexedLinePoints(
        humanDays,
        row => dayIndex.get(dateKey(row.date)) ?? 0,
        row => row.average_score === null || row.average_score === undefined ? null : Number(row.average_score) / 5 * 100,
        width,
        height,
        pad,
      );
      const xForDay = day => {{
        const maxIndex = Math.max(1, allDays.length - 1);
        return pad.left + ((dayIndex.get(day) ?? 0) / maxIndex) * (width - pad.left - pad.right);
      }};
      const circles = runs.map((row, index) => {{
        const x = xForDay(dateKey(row.created_at));
        const y = pad.top + (1 - Number(row.score || 0)) * (height - pad.top - pad.bottom);
        const fill = row.success ? '#2f6f41' : '#b42318';
        return `<circle cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="4" fill="${{fill}}"><title>${{escapeHtml(row.eval_id)}} · ${{scoreOutOfFive(row.score, true)}} · ${{row.success ? 'pass' : 'fail'}} · ${{row.run_count || 1}} run${{Number(row.run_count || 1) === 1 ? '' : 's'}}</title></circle>`;
      }}).join('');
      const humanCircles = humanDays.map(row => {{
        const x = xForDay(dateKey(row.date));
        const y = pad.top + (1 - Number(row.average_score || 0) / 5) * (height - pad.top - pad.bottom);
        return `<circle cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="4" fill="#8a6f2a"><title>human avg ${{scoreOutOfFive(row.average_score)}} · ${{row.count || 1}} review${{Number(row.count || 1) === 1 ? '' : 's'}}</title></circle>`;
      }}).join('');
      chart.innerHTML = `<svg viewBox="0 0 ${{width}} ${{height}}" role="img" aria-label="Single prompt score over time">
        <line x1="${{pad.left}}" y1="${{pad.top}}" x2="${{pad.left}}" y2="${{height - pad.bottom}}" stroke="#d9dee3" />
        <line x1="${{pad.left}}" y1="${{height - pad.bottom}}" x2="${{width - pad.right}}" y2="${{height - pad.bottom}}" stroke="#d9dee3" />
        ${{[0, 2.5, 5].map(value => {{
          const y = pad.top + (1 - value / 5) * (height - pad.top - pad.bottom);
          return `<line x1="${{pad.left}}" y1="${{y}}" x2="${{width - pad.right}}" y2="${{y}}" stroke="#edf0f2" /><text x="4" y="${{y + 4}}" fill="#687076" font-size="11">${{value}}/5</text>`;
        }}).join('')}}
        <polyline points="${{machinePoints}}" fill="none" stroke="#2f6f41" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" />
        ${{humanPoints ? `<polyline points="${{humanPoints}}" fill="none" stroke="#8a6f2a" stroke-width="2.5" stroke-dasharray="4 4" stroke-linejoin="round" stroke-linecap="round" />` : ''}}
        ${{circles}}
        ${{humanCircles}}
        <text x="${{pad.left}}" y="${{height - 8}}" fill="#687076" font-size="11">${{escapeHtml(allDays[0] || runs[0].created_at || '')}}</text>
        <text x="${{width - pad.right}}" y="${{height - 8}}" fill="#687076" font-size="11" text-anchor="end">${{escapeHtml(allDays[allDays.length - 1] || runs[runs.length - 1].created_at || '')}}</text>
        <text x="${{width - 210}}" y="18" fill="#2f6f41" font-size="12">machine avg</text>
        ${{humanPoints ? `<text x="${{width - 105}}" y="18" fill="#8a6f2a" font-size="12">human avg</text>` : ''}}
      </svg>
      ${{humanPoints ? '' : '<div class="subtle">No saved human-review trend for this prompt yet.</div>'}}`;
      const machineByDay = new Map(runs.map(row => [dateKey(row.created_at), row]));
      const humanByDay = new Map(humanDays.map(row => [dateKey(row.date), row]));
      table.innerHTML = `<table class="analysis-table">
        <thead><tr><th>Run date</th><th>Eval run instance</th><th>Machine runs</th><th>Machine check</th><th>Machine avg</th><th>Human reviews</th><th>Human avg</th></tr></thead>
        <tbody>${{allDays.map(day => {{
          const machine = machineByDay.get(day) || {{}};
          const human = humanByDay.get(day) || {{}};
          return `<tr>
          <td>${{escapeHtml(day)}}</td>
          <td class="mono">${{dbMetric(machine.eval_id)}}</td>
          <td class="mono">${{machine.run_count || '<span class="db-empty">tbd</span>'}}</td>
          <td>${{machine.eval_id ? pill(machine.success ? 'pass' : 'fail', machine.success ? 'pass' : 'fail') : '<span class="db-empty">tbd</span>'}}</td>
          <td class="mono">${{scoreValue(machine.score, true)}}</td>
          <td class="mono">${{human.count || '<span class="db-empty">tbd</span>'}}</td>
          <td class="mono">${{scoreValue(human.average_score)}}</td>
        </tr>`;
        }}).join('')}}</tbody>
      </table>`;
    }}
    function renderAnalysisPromptAverages() {{
      const rows = data.analysis?.prompt_score_averages || [];
      const target = document.getElementById('analysis-prompt-average-chart');
      if (!rows.length) {{
        target.innerHTML = '<div class="subtle">No included Promptfoo prompt scores are available yet.</div>';
        return;
      }}
      const grouped = new Map();
      for (const row of rows) {{
        const agent = row.agent || 'unknown';
        if (!grouped.has(agent)) grouped.set(agent, []);
        grouped.get(agent).push(row);
      }}
      target.innerHTML = `<div class="prompt-average-groups">${{Array.from(grouped.entries()).map(([agent, items]) => {{
        const sorted = items.slice().sort((a, b) => Number(a.average_score || 0) - Number(b.average_score || 0) || String(a.case_id).localeCompare(String(b.case_id)));
        return `<section class="prompt-average-group">
          <h3>${{escapeHtml(agent)}}</h3>
          ${{sorted.map(row => {{
            const score = Number(row.average_score || 0);
            return `<div class="prompt-average-row">
              <div class="truncate" title="${{escapeHtml(row.case_id)}}">${{escapeHtml(analysisCaseLabel(row.case_id))}}</div>
              <div class="bar-track"><div class="bar ${{score < 1 ? 'gap' : ''}}" style="width:${{Math.max(0, Math.min(100, score * 100))}}%"></div></div>
              <div class="mono">${{scoreOutOfFive(row.average_score, true)}}</div>
              <div class="subtle" style="grid-column: 1 / -1">${{row.run_count || 0}} day${{Number(row.run_count || 0) === 1 ? '' : 's'}} averaged · latest ${{escapeHtml(row.latest_created_at || '')}}</div>
            </div>`;
          }}).join('')}}
        </section>`;
      }}).join('')}}</div>`;
    }}
    function renderAnalysisAgentStability() {{
      const rows = data.analysis?.agent_stability || [];
      const target = document.getElementById('analysis-agent-stability');
      if (!rows.length) {{
        target.innerHTML = '<div class="subtle">No agent-level Promptfoo history yet.</div>';
        return;
      }}
      target.innerHTML = `<table class="analysis-table">
        <thead><tr><th>Agent</th><th>Pass rate</th><th>Avg / 5</th><th>Score range</th><th>Flaky cases</th></tr></thead>
        <tbody>${{rows.map(row => `<tr>
          <td class="mono">${{escapeHtml(row.agent)}}</td>
          <td>
            <div class="bar-row" style="grid-template-columns:minmax(58px, 72px) 1fr; margin:0">
              <span class="mono">${{formatPercent(row.pass_rate)}}</span>
              <span class="bar-track"><span class="bar ${{Number(row.pass_rate || 0) < 100 ? 'gap' : ''}}" style="width:${{Math.max(0, Math.min(100, Number(row.pass_rate || 0)))}}%"></span></span>
            </div>
          </td>
          <td class="mono">${{scoreOutOfFive(row.average_score, true)}}</td>
          <td class="mono">${{(Number(row.score_range || 0) * 5).toFixed(2)}}/5</td>
          <td class="mono">${{row.flaky_cases || 0}}</td>
        </tr>`).join('')}}</tbody>
      </table>`;
    }}
    function renderAnalysisDimensionFailures() {{
      const rows = data.analysis?.dimension_failures || [];
      const target = document.getElementById('analysis-dimension-failures');
      if (!rows.length) {{
        target.innerHTML = '<div class="subtle">No failed Promptfoo assertions are recorded in the imported runs.</div>';
        return;
      }}
      const max = Math.max(1, ...rows.map(row => Number(row.failures || 0)));
      target.innerHTML = rows.map(row => `
        <div class="bar-row">
          <div class="truncate" title="${{escapeHtml((row.agents || []).join(', '))}}">${{escapeHtml(row.dimension)}}</div>
          <div class="bar-track"><div class="bar gap" style="width:${{Math.round(Number(row.failures || 0) / max * 100)}}%"></div></div>
          <div class="mono">${{row.failures || 0}}</div>
        </div>
      `).join('');
    }}
    function renderAnalysisCaseChanges() {{
      const rows = data.analysis?.case_changes || [];
      const target = document.getElementById('analysis-case-changes');
      if (!rows.length) {{
        target.innerHTML = '<div class="subtle">No repeated case history yet. Run Promptfoo more than once on the same cases to see improvements and regressions.</div>';
        return;
      }}
      target.innerHTML = `<table class="analysis-table">
        <thead><tr><th>Last run</th><th>Case</th><th>Agent</th><th>Previous</th><th>Latest</th><th>Delta</th><th>Signal</th><th>Dimensions</th></tr></thead>
        <tbody>${{rows.slice(0, 30).map(row => {{
          const cls = row.status === 'regressed' ? 'fail' : row.status === 'improved' ? 'pass' : row.status === 'stable' ? '' : 'warn';
          const delta = row.score_delta === null || row.score_delta === undefined ? '-' : signedNumber(Number(row.score_delta || 0) * 5, '', 2);
          return `<tr>
            <td>${{escapeHtml(formatDateTime(row.latest_created_at) || row.latest_created_at || '')}}</td>
            <td class="mono" title="${{escapeHtml(row.case_id)}}">${{escapeHtml(analysisCaseLabel(row.case_id))}}</td>
            <td class="mono">${{escapeHtml(row.agent || '')}}</td>
            <td class="mono">${{row.previous_score === null || row.previous_score === undefined ? '-' : scoreOutOfFive(row.previous_score, true)}}</td>
            <td class="mono">${{scoreOutOfFive(row.latest_score, true)}}</td>
            <td class="mono">${{escapeHtml(delta)}}</td>
            <td>${{pill(row.status || 'unknown', cls)}}</td>
            <td>${{(row.dimensions || []).map(d => pill(d)).join('')}}</td>
          </tr>`;
        }}).join('')}}</tbody>
      </table>`;
    }}
    function renderAnalysis() {{
      renderAnalysisSummary();
      renderAnalysisChartScaffolds();
      renderAnalysisRunChart();
      renderAnalysisCaseSelector();
      renderAnalysisCaseTrend();
      renderAnalysisPromptAverages();
      renderAnalysisAgentStability();
      renderAnalysisDimensionFailures();
      renderAnalysisCaseChanges();
    }}
    function scoreOptions(selected, pending = false) {{
      if (pending) {{
        return '<option value="" selected>tbd</option>' + [0, 1, 2, 3, 4, 5].map(value => `<option value="${{value}}">${{value}}</option>`).join('');
      }}
      return [0, 1, 2, 3, 4, 5].map(value => {{
        const isSelected = Number(selected ?? 4) === value ? ' selected' : '';
        return `<option value="${{value}}"${{isSelected}}>${{value}}</option>`;
      }}).join('');
    }}
    function hasRecordedResponse(item) {{
      return Boolean(String(item.response_text || item.latest_slack_summary || '').trim());
    }}
    function scoreForm(item) {{
      const reviewReady = hasRecordedResponse(item);
      const disabled = reviewReady ? '' : ' disabled';
      const disabledReason = reviewReady
        ? ''
        : '<div class="subtle">Score saving is disabled until this case has a recorded Promptfoo or Slack response.</div>';
      const controls = scoreDimensions.map(dimension => `
        <div class="score-control">
          <label>${{escapeHtml(dimension)}}</label>
          <select data-score="${{escapeHtml(dimension)}}"${{disabled}}>${{scoreOptions(item.human_scores?.[dimension], !reviewReady)}}</select>
        </div>
      `).join('');
      return `<form class="review-form" data-case-id="${{escapeHtml(item.case_id)}}" aria-disabled="${{reviewReady ? 'false' : 'true'}}">
        <div class="detail-label">Score This Case</div>
        ${{disabledReason}}
        <div class="score-controls">
          ${{controls}}
          <div class="score-control">
            <label>safety</label>
            <select data-safety${{disabled}}>
              ${{!reviewReady ? '<option value="" selected>tbd</option>' : ''}}
              <option value="pass"${{reviewReady && item.human_safety !== 'fail' ? ' selected' : ''}}>pass</option>
              <option value="fail"${{reviewReady && item.human_safety === 'fail' ? ' selected' : ''}}>fail</option>
            </select>
          </div>
        </div>
        <textarea data-notes placeholder="Notes for this review"${{disabled}}>${{escapeHtml(item.human_notes || '')}}</textarea>
        <div class="form-actions">
          <button type="submit"${{disabled}}>Save human review</button>
          <span class="review-status" data-review-status></span>
        </div>
      </form>`;
    }}
    function analysisToggle(item) {{
      if (!item.promptfoo_eval_id) {{
        return '<span class="subtle">Analysis inclusion appears after a Promptfoo machine-check run.</span>';
      }}
      const excluded = Boolean(item.analysis_excluded);
      const label = excluded ? 'Include in analysis' : 'Exclude from analysis';
      const state = excluded ? 'analysis excluded' : 'analysis included';
      return `<div class="case-actions">
        ${{pill(state, excluded ? 'warn' : 'pass')}}
        <button
          type="button"
          class="analysis-toggle ${{excluded ? 'excluded' : ''}}"
          data-analysis-toggle
          data-case-id="${{escapeHtml(item.case_id || '')}}"
          data-eval-id="${{escapeHtml(item.promptfoo_eval_id || '')}}"
          data-analysis-excluded="${{excluded ? 'true' : 'false'}}"
        >${{escapeHtml(label)}}</button>
      </div>`;
    }}
    async function copyPrompt(button) {{
      const original = button.textContent;
      const source = button.closest('article, section')?.querySelector('[data-copy-source]');
      const promptText = source?.value || '';
      if (!promptText.trim()) {{
        button.textContent = 'No prompt';
        setTimeout(() => {{ button.textContent = original; }}, 1200);
        return;
      }}
      try {{
        source.focus();
        source.select();
        const copied = document.execCommand('copy');
        if (!copied) {{
          await navigator.clipboard.writeText(promptText || '');
        }}
        button.textContent = 'Copied';
      }} catch (error) {{
        try {{
          await navigator.clipboard.writeText(promptText || '');
          button.textContent = 'Copied';
        }} catch (fallbackError) {{
          button.textContent = 'Copy failed';
        }}
      }}
      setTimeout(() => {{ button.textContent = original; }}, 1200);
    }}
    function renderPromptRows() {{
      const query = promptSearch.value.trim().toLowerCase();
      const rows = cases.filter(item => {{
        const haystack = [
          item.prompt_number, item.case_id, item.display_case_id, item.agent, (item.dimensions || []).join(' '), item.user_input
        ].join(' ').toLowerCase();
        return (!query || haystack.includes(query))
          && (!promptAgentFilter.value || item.agent === promptAgentFilter.value);
      }});
      document.getElementById('prompt-rows').innerHTML = rows.map(item => `
        <article class="prompt-card">
          <div class="case-head" style="border-bottom:0; padding-bottom:0; margin-bottom:8px">
            <div>
              <div class="case-id" title="${{escapeHtml(item.case_id)}}">${{escapeHtml(item.display_case_id || item.case_id)}}</div>
              <div class="case-meta">
                ${{pill(item.agent || 'agent?')}}
                ${{(item.dimensions || []).map(d => pill(d)).join('')}}
              </div>
            </div>
            <button class="prompt-copy" type="button" data-copy-prompt="${{escapeHtml(item.case_id)}}">Copy prompt</button>
          </div>
          <textarea class="copy-source" data-copy-source readonly>${{escapeHtml(item.user_input || '')}}</textarea>
          <div class="text-block">${{escapeHtml(item.user_input || 'No prompt recorded.')}}</div>
        </article>
      `).join('');
      for (const button of document.querySelectorAll('#prompt-rows [data-copy-prompt]')) {{
        button.addEventListener('click', () => copyPrompt(button));
      }}
    }}
	    function renderRows() {{
      const query = search.value.trim().toLowerCase();
      const rows = cases.filter(item => {{
        const haystack = [
          item.case_id, item.display_case_id, item.agent, (item.dimensions || []).join(' '),
          item.user_input, item.response_text, item.latest_slack_run_id, item.human_notes
        ].join(' ').toLowerCase();
        return (!query || haystack.includes(query))
          && (!agentFilter.value || item.agent === agentFilter.value)
          && stateMatches(item);
      }});
      document.getElementById('case-rows').innerHTML = rows.map(item => {{
        return `<article class="case-card">
          <div class="case-head">
            <div>
              <div class="case-id" title="${{escapeHtml(item.case_id)}}">${{escapeHtml(item.display_case_id || item.case_id)}}</div>
              <div class="case-meta">
                ${{pill(item.agent || 'agent?')}}
                ${{(item.dimensions || []).map(d => pill(d)).join('')}}
              </div>
            </div>
            <div>${{analysisToggle(item)}}</div>
          </div>
          ${{mergedScoringStatus(item)}}
          <div class="case-body">
            <section>
              <div class="prompt-head">
                <div class="detail-label">Prompt</div>
                <button class="prompt-copy" type="button" data-copy-prompt="${{escapeHtml(item.case_id)}}">Copy prompt</button>
              </div>
              <textarea class="copy-source" data-copy-source readonly>${{escapeHtml(item.user_input || '')}}</textarea>
              <div class="text-block">${{escapeHtml(item.user_input || 'No prompt recorded.')}}</div>
            </section>
            <section>
              <div class="detail-label">Latest Response / Result</div>
              <div class="text-block">${{escapeHtml(item.response_text || item.latest_slack_summary || item.promptfoo_reason || 'No response recorded.')}}</div>
            </section>
            <section>
              <div class="detail-label">Human Quality Review</div>
              <div class="score-grid">${{scorePills(item.human_scores)}} ${{item.human_notes ? pill(`notes: ${{item.human_notes}}`, '') : ''}}</div>
            </section>
            <section>
              ${{scoreForm(item)}}
            </section>
          </div>
        </article>`;
      }}).join('');
      for (const form of document.querySelectorAll('.review-form')) {{
        form.addEventListener('submit', event => saveReview(event, form));
	    }}
	      for (const button of document.querySelectorAll('[data-copy-prompt]')) {{
	        button.addEventListener('click', () => copyPrompt(button));
	      }}
	      for (const button of document.querySelectorAll('[data-analysis-toggle]')) {{
	        button.addEventListener('click', () => toggleAnalysisExclusion(button));
	      }}
	    }}
	    function renderDatabaseRows() {{
	      const query = databaseSearch.value.trim().toLowerCase();
	      const rows = cases.filter(item => {{
	        const haystack = [
	          item.prompt_number, item.display_prompt_number, item.case_id, item.display_case_id, item.agent, (item.dimensions || []).join(' '),
	          item.user_input, item.response_text, item.latest_slack_run_id, item.promptfoo_eval_id, item.human_notes
	        ].join(' ').toLowerCase();
	        return (!query || haystack.includes(query))
	          && (!databaseAgentFilter.value || item.agent === databaseAgentFilter.value)
	          && databaseStateMatches(item);
	      }});
	      document.getElementById('database-rows').innerHTML = rows.map(item => {{
	        const slackStatus = Number(item.slack_run_count || 0) > 0
	          ? `${{item.slack_run_count}} saved run${{Number(item.slack_run_count || 0) === 1 ? '' : 's'}}`
	          : 'not tested';
	        const slackRun = item.latest_slack_run_id || item.latest_slack_thread_ts || '';
	        const machineAssertion = item.promptfoo_success === true
	          ? pill('pass', 'pass')
	          : item.promptfoo_success === false
	            ? pill('fail', 'fail')
	            : '<span class="db-empty">pending</span>';
	        const analysisState = item.promptfoo_eval_id
	          ? `${{item.analysis_excluded ? 'excluded' : 'included'}}${{item.analysis_exclusion_reason ? `: ${{item.analysis_exclusion_reason}}` : ''}}`
	          : '';
	        const humanStatus = item.human_average !== null ? 'reviewed' : 'unreviewed';
	        const humanScoreCells = scoreDimensions.map(dimension => (
	          `<td class="mono">${{dbMetric(item.human_scores?.[dimension])}}</td>`
	        )).join('');
	        return `<tr>
	          <td class="mono">${{escapeHtml(item.display_prompt_number || item.prompt_number || '000')}}</td>
	          <td class="mono" title="${{escapeHtml(item.case_id || '')}}">${{escapeHtml(item.display_case_id || item.case_id || '')}}</td>
	          <td>${{escapeHtml(item.agent || '')}}</td>
	          <td>${{escapeHtml(slackStatus)}}</td>
	          <td class="mono">${{dbMetric(slackRun)}}</td>
	          <td>${{machineAssertion}}</td>
	          <td class="mono">${{scoreValue(item.promptfoo_score, true)}}</td>
	          <td class="mono">${{dbMetric(item.promptfoo_eval_id)}}</td>
	          <td>${{dbMetric(analysisState, item.promptfoo_eval_id ? 'included' : 'tbd')}}</td>
	          <td>${{escapeHtml(humanStatus)}}</td>
	          <td class="mono">${{scoreValue(item.human_average)}}</td>
	          <td>${{dbMetric(item.human_safety)}}</td>
	          ${{humanScoreCells}}
	          <td>${{dbMetric(item.human_notes)}}</td>
	          <td class="prompt-cell">${{escapeHtml(item.user_input || '')}}</td>
	          <td class="prompt-cell"><span class="subtle">${{escapeHtml(item.response_text || '')}}</span></td>
	        </tr>`;
	      }}).join('');
	    }}
    async function toggleAnalysisExclusion(button) {{
      const caseId = button.getAttribute('data-case-id') || '';
      const evalId = button.getAttribute('data-eval-id') || '';
      const currentlyExcluded = button.getAttribute('data-analysis-excluded') === 'true';
      const excluded = !currentlyExcluded;
      const original = button.textContent;
      button.disabled = true;
      button.textContent = excluded ? 'Excluding...' : 'Including...';
      try {{
        const response = await fetch('/api/analysis-exclusion', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify({{
            case_id: caseId,
            eval_id: evalId,
            excluded,
            reason: excluded ? 'manual dashboard exclusion' : '',
          }}),
        }});
        const result = await response.json().catch(() => ({{}}));
        if (!response.ok) throw new Error(result.error || `HTTP ${{response.status}}`);
        setTimeout(() => window.location.reload(), 250);
      }} catch (error) {{
        button.disabled = false;
        button.textContent = `Failed: ${{error.message}}`;
        setTimeout(() => {{ button.textContent = original; }}, 1800);
      }}
    }}
    async function saveReview(event, form) {{
      event.preventDefault();
      const status = form.querySelector('[data-review-status]');
      const caseId = form.getAttribute('data-case-id');
      const item = cases.find(candidate => candidate.case_id === caseId) || {{}};
      if (!hasRecordedResponse(item)) {{
        status.textContent = 'Save disabled until this case has a recorded response.';
        return;
      }}
      const scores = {{}};
      for (const select of form.querySelectorAll('[data-score]')) {{
        scores[select.getAttribute('data-score')] = Number(select.value);
      }}
      const payload = {{
        case_id: caseId,
        run_id: item.latest_slack_run_id || '',
        agent: item.agent || '',
        slack_thread_ts: item.latest_slack_thread_ts || '',
        safety: form.querySelector('[data-safety]').value,
        notes: form.querySelector('[data-notes]').value,
        scores,
      }};
      status.textContent = 'Saving...';
      try {{
        const response = await fetch('/api/human-review', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify(payload),
        }});
        const result = await response.json().catch(() => ({{}}));
        if (!response.ok) throw new Error(result.error || `HTTP ${{response.status}}`);
        status.textContent = `Saved average ${{result.average_score}}/5`;
        setTimeout(() => window.location.reload(), 500);
      }} catch (error) {{
        status.textContent = `Save failed: ${{error.message}}. Start the dashboard server to save scores.`;
      }}
    }}
    renderAgentScoreTable('machine');
    renderWorkflowVisualization();
    renderCoverageBars();
    renderBars('dimension-bars', data.summary.dimensions, 8);
    renderEvalRuns();
    renderAnalysis();
	    for (const card of document.querySelectorAll('[data-drilldown]')) {{
	      card.addEventListener('click', () => {{
	        setActiveView('overview', card.getAttribute('data-drilldown'));
	        renderAgentScoreTable(card.getAttribute('data-drilldown'), true);
	      }});
	    }}
	    for (const button of document.querySelectorAll('[data-score-view]')) {{
	      button.addEventListener('click', () => {{
	        setActiveView('overview', button.getAttribute('data-score-view'));
	        renderAgentScoreTable(button.getAttribute('data-score-view'), true);
	      }});
	    }}
	    for (const button of document.querySelectorAll('[data-view]')) {{
	      button.addEventListener('click', () => {{
	        const scoreNav = button.getAttribute('data-score-nav') || '';
	        setActiveView(button.getAttribute('data-view'), scoreNav);
	        if (scoreNav) {{
	          renderAgentScoreTable(scoreNav, true);
	        }}
	      }});
	    }}
    search.addEventListener('input', renderRows);
    agentFilter.addEventListener('change', renderRows);
    stateFilter.addEventListener('change', renderRows);
	    promptSearch.addEventListener('input', renderPromptRows);
	    promptAgentFilter.addEventListener('change', renderPromptRows);
	    databaseSearch.addEventListener('input', renderDatabaseRows);
	    databaseAgentFilter.addEventListener('change', renderDatabaseRows);
	    databaseStateFilter.addEventListener('change', renderDatabaseRows);
	    analysisAgentTrendFilter.addEventListener('change', renderAnalysisRunChart);
	    analysisCaseFilter.addEventListener('change', renderAnalysisCaseTrend);
    if (params.get('case')) {{
      setActiveView('runs');
    }}
	    renderPromptRows();
	    renderRows();
	    renderDatabaseRows();
  </script>
</body>
</html>
"""


def _review_html(payload: dict[str, Any]) -> str:
    data_json = _json_script_payload(payload)
    case = payload["case"]
    title = "Keystone Eval Review"
    review_ready = bool(
        str(case.get("response_text") or case.get("latest_slack_summary") or "").strip()
    )
    disabled = "" if review_ready else " disabled"
    disabled_note = (
        ""
        if review_ready
        else '<div class="subtle">Score saving is disabled until this case has a recorded Promptfoo or Slack response.</div>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f6f7f8;
      --panel: #ffffff;
      --ink: #1b1f23;
      --muted: #687076;
      --line: #d9dee3;
      --good: #287a47;
      --bad: #b42318;
      --warn: #b54708;
      --soft: #f1f4f6;
      --soft-warn: #f2f4f5;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }}
    main {{ max-width: 900px; margin: 0 auto; padding: 24px 18px 36px; }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
    }}
    h1 {{ margin: 0 0 6px; font-size: 24px; letter-spacing: 0; }}
    h2 {{ font-size: 15px; margin: 18px 0 8px; }}
    .subtle {{ color: var(--muted); }}
    .mono {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }}
    .pill {{ display: inline-block; padding: 2px 7px; border-radius: 999px; font-size: 12px; border: 1px solid var(--line); margin: 0 4px 4px 0; max-width: 100%; overflow-wrap: anywhere; }}
    .warn {{ color: var(--warn); border-color: #d6d9dc; background: var(--soft-warn); }}
    .text-block {{
      background: var(--soft);
      border: 1px solid #dde5ea;
      border-radius: 6px;
      padding: 9px 10px;
      white-space: pre-wrap;
      overflow-wrap: anywhere;
    }}
    .score-controls {{
      display: grid;
      grid-template-columns: repeat(3, minmax(160px, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }}
    label {{
      display: block;
      color: var(--muted);
      font-size: 12px;
      font-weight: 650;
      margin-bottom: 3px;
    }}
    select, textarea {{
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      width: 100%;
      font: inherit;
      background: #fff;
      color: var(--ink);
    }}
    textarea {{ min-height: 100px; resize: vertical; }}
    button {{
      border: 1px solid #9a927f;
      border-radius: 6px;
      background: #f7f9fa;
      color: var(--ink);
      min-height: 34px;
      padding: 7px 11px;
      font: inherit;
      cursor: pointer;
    }}
    .form-actions {{ display: flex; align-items: center; gap: 10px; margin-top: 10px; flex-wrap: wrap; }}
    @media (max-width: 760px) {{
      .score-controls {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>
  <main>
    <div class="panel">
      <h1>{title}</h1>
      <div class="subtle">Standalone intake for one eval case. Scores are 0-5; safety is pass/fail.</div>
      <h2>Case</h2>
      <div class="mono">{html.escape(str(case.get("case_id") or ""))}</div>
      <div style="margin-top:8px">
        <span class="pill">{html.escape(str(case.get("agent") or "agent?"))}</span>
        {"".join(f'<span class="pill">{html.escape(str(dimension))}</span>' for dimension in case.get("dimensions") or [])}
      </div>
      <h2>Prompt</h2>
      <div class="text-block">{html.escape(str(case.get("user_input") or "No prompt recorded."))}</div>
      <h2>Latest Response / Result</h2>
      <div class="text-block">{html.escape(str(case.get("response_text") or "No response recorded."))}</div>
      <h2>Human Review</h2>
      <form id="review-form">
        {disabled_note}
        <div id="score-controls" class="score-controls"></div>
        <textarea id="notes" placeholder="Notes for this review"{disabled}>{html.escape(str(case.get("human_notes") or ""))}</textarea>
        <div class="form-actions">
          <button type="submit"{disabled}>Save human review</button>
          <span id="status" class="subtle"></span>
        </div>
      </form>
    </div>
  </main>
  <script id="eval-data" type="application/json">{data_json}</script>
  <script>
    const data = JSON.parse(document.getElementById('eval-data').textContent);
    const item = data.case || {{}};
    const scoreDimensions = data.score_dimensions || [];
    const reviewReady = Boolean(String(item.response_text || item.latest_slack_summary || '').trim());
    const controls = document.getElementById('score-controls');
    function escapeHtml(value) {{
      return String(value ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c]));
    }}
    function scoreOptions(selected) {{
      if (!reviewReady) {{
        return '<option value="" selected>tbd</option>' + [0, 1, 2, 3, 4, 5].map(value => `<option value="${{value}}">${{value}}</option>`).join('');
      }}
      return [0, 1, 2, 3, 4, 5].map(value => {{
        const isSelected = Number(selected ?? 4) === value ? ' selected' : '';
        return `<option value="${{value}}"${{isSelected}}>${{value}}</option>`;
      }}).join('');
    }}
    controls.innerHTML = scoreDimensions.map(dimension => `
      <div>
        <label>${{escapeHtml(dimension)}}</label>
        <select data-score="${{escapeHtml(dimension)}}"${{reviewReady ? '' : ' disabled'}}>${{scoreOptions(item.human_scores?.[dimension])}}</select>
      </div>
    `).join('') + `
      <div>
        <label>safety</label>
        <select data-safety${{reviewReady ? '' : ' disabled'}}>
          ${{!reviewReady ? '<option value="" selected>tbd</option>' : ''}}
          <option value="pass"${{reviewReady && item.human_safety !== 'fail' ? ' selected' : ''}}>pass</option>
          <option value="fail"${{reviewReady && item.human_safety === 'fail' ? ' selected' : ''}}>fail</option>
        </select>
      </div>
    `;
    document.getElementById('review-form').addEventListener('submit', async event => {{
      event.preventDefault();
      const status = document.getElementById('status');
      if (!reviewReady) {{
        status.textContent = 'Save disabled until this case has a recorded response.';
        return;
      }}
      const scores = {{}};
      for (const select of document.querySelectorAll('[data-score]')) {{
        scores[select.getAttribute('data-score')] = Number(select.value);
      }}
      const payload = {{
        case_id: item.case_id || '',
        run_id: item.latest_slack_run_id || '',
        agent: item.agent || '',
        slack_thread_ts: item.latest_slack_thread_ts || '',
        safety: document.querySelector('[data-safety]').value,
        notes: document.getElementById('notes').value,
        scores,
      }};
      status.textContent = 'Saving...';
      try {{
        const response = await fetch('/api/human-review', {{
          method: 'POST',
          headers: {{'Content-Type': 'application/json'}},
          body: JSON.stringify(payload),
        }});
        const result = await response.json().catch(() => ({{}}));
        if (!response.ok) throw new Error(result.error || `HTTP ${{response.status}}`);
        status.textContent = `Saved average ${{result.average_score}}/5`;
      }} catch (error) {{
        status.textContent = `Save failed: ${{error.message}}`;
      }}
    }});
  </script>
</body>
</html>
"""


def _json_script_payload(payload: dict[str, Any]) -> str:
    return (
        json.dumps(payload, ensure_ascii=True, sort_keys=True)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _kpi(label: str, value: Any, hint: str = "", drilldown: str = "") -> str:
    data_attr = f' data-drilldown="{html.escape(drilldown)}"' if drilldown else ""
    return (
        f'<button class="kpi" type="button"{data_attr}>'
        f'<div class="label">{html.escape(label)}</div>'
        f'<div class="value">{html.escape(str(value))}</div>'
        f'<div class="hint">{html.escape(hint)}</div>'
        "</button>"
    )


def _score_out_of_five(value: Any, *, machine: bool = False) -> str:
    if value is None or value == "":
        return "-"
    try:
        score = float(value)
    except (TypeError, ValueError):
        return "-"
    if machine:
        score *= 5
    return f"{score:.1f}/5"


def _split_dimensions(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Render the local Promptfoo eval dashboard.")
    parser.add_argument("--database-path", default=str(DEFAULT_EVAL_DB))
    parser.add_argument("--output", default=str(DEFAULT_DASHBOARD_PATH))
    parser.add_argument("--limit", type=int, default=500)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    output = render_dashboard(
        database_path=args.database_path,
        output_path=args.output,
        limit=args.limit,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
