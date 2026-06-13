"""Promptfoo assertion for Keystone Slack ask invariants."""

from __future__ import annotations

import json
import re
import sys
from typing import Any

SOURCE_PATTERN = re.compile(r"\b(?:https?://|fixture://)[^\s)>\]]+", re.IGNORECASE)
METADATA_SUMMARY_TERMS = (
    "provider_status",
    "orchestrator_preflight",
    "route_result",
    "context_pack_type",
    "artifact_refs",
    "source_count",
    "manager_loop_efficiency",
    "final_synthesis_executed",
    "work_item",
    "decision_trace",
)


def grade_output(output: str, context: dict[str, Any]) -> dict[str, Any]:
    vars_ = context.get("vars") or {}
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        return _result(False, 0.0, f"output is not JSON: {exc}")

    failures: list[str] = []
    warnings: list[str] = []
    if payload.get("provider_status") != "ok":
        failures.append(f"provider_status={payload.get('provider_status')}: {payload.get('error')}")

    expected_route = vars_.get("expected_route")
    if expected_route and payload.get("route") != expected_route:
        failures.append(f"route={payload.get('route')!r}, expected {expected_route!r}")

    expected_status = vars_.get("expected_status")
    if expected_status and payload.get("status") != expected_status:
        failures.append(f"status={payload.get('status')!r}, expected {expected_status!r}")

    if vars_.get("expect_slack_context", True) and not payload.get("slack_context_attached"):
        failures.append("Slack context was not attached to the WorkItem payload")

    min_sources = int(vars_.get("min_source_count") or 0)
    if int(payload.get("source_count") or 0) < min_sources:
        failures.append(f"source_count={payload.get('source_count')}, expected >= {min_sources}")

    min_artifacts = int(vars_.get("min_artifact_count") or 0)
    if int(payload.get("artifact_count") or 0) < min_artifacts:
        failures.append(
            f"artifact_count={payload.get('artifact_count')}, expected >= {min_artifacts}"
        )

    expected_pack_type = vars_.get("expected_pack_type")
    if expected_pack_type and payload.get("context_pack_type") != expected_pack_type:
        failures.append(
            f"context_pack_type={payload.get('context_pack_type')!r}, "
            f"expected {expected_pack_type!r}"
        )

    expected_next_action_agent = vars_.get("expected_next_action_agent")
    if (
        expected_next_action_agent
        and payload.get("next_action_agent") != expected_next_action_agent
    ):
        failures.append(
            f"next_action_agent={payload.get('next_action_agent')!r}, "
            f"expected {expected_next_action_agent!r}"
        )

    expected_block_kind = vars_.get("expected_block_kind")
    if expected_block_kind and payload.get("block_kind") != expected_block_kind:
        failures.append(
            f"block_kind={payload.get('block_kind')!r}, expected {expected_block_kind!r}"
        )

    expected_refused = _bool_or_none(vars_.get("expected_refused"))
    if expected_refused is not None and bool(payload.get("refused")) is not expected_refused:
        failures.append(f"refused={payload.get('refused')!r}, expected {expected_refused!r}")

    expected_approval_required = _bool_or_none(vars_.get("expected_approval_required"))
    if (
        expected_approval_required is not None
        and bool(payload.get("approval_required")) is not expected_approval_required
    ):
        failures.append(
            f"approval_required={payload.get('approval_required')!r}, "
            f"expected {expected_approval_required!r}"
        )

    human_summary = str(payload.get("human_summary") or "")
    if vars_.get("require_readable_summary", True):
        _check_readable_summary(human_summary, failures)

    if vars_.get("require_visible_sources", min_sources > 0):
        visible_source_count = len(SOURCE_PATTERN.findall(human_summary))
        if visible_source_count < min_sources:
            failures.append(
                f"visible_source_count={visible_source_count}, expected >= {min_sources}"
            )

    for term in _string_list(vars_.get("required_terms")):
        if term.lower() not in human_summary.lower():
            failures.append(f"missing required summary term: {term}")

    for term in _string_list(vars_.get("forbidden_terms")):
        if term.lower() in human_summary.lower():
            failures.append(f"forbidden summary term found: {term}")

    for term in _string_list(vars_.get("required_audit_terms")):
        audit_text = " ".join(str(item) for item in payload.get("audit_notes") or [])
        if term.lower() not in audit_text.lower():
            failures.append(f"missing required audit term: {term}")

    for artifact_type in _string_list(vars_.get("required_artifact_types")):
        if artifact_type not in set(payload.get("artifact_types") or []):
            failures.append(f"missing required artifact_type: {artifact_type}")

    for source_type in _string_list(vars_.get("required_source_types")):
        if source_type not in set(payload.get("source_types") or []):
            failures.append(f"missing required source_type: {source_type}")

    for prefix in _string_list(vars_.get("required_source_url_prefixes")):
        if not any(str(url).startswith(prefix) for url in payload.get("source_urls") or []):
            failures.append(f"missing source URL prefix: {prefix}")

    for prefix in _string_list(vars_.get("forbidden_source_url_prefixes")):
        if any(str(url).startswith(prefix) for url in payload.get("source_urls") or []):
            failures.append(f"forbidden source URL prefix found: {prefix}")

    for route in _string_list(vars_.get("required_workflow_routes")):
        if route not in set(payload.get("workflow") or []):
            failures.append(f"workflow missing route: {route}")

    if _bool_or_none(vars_.get("require_final_synthesis")) is True and not payload.get(
        "final_synthesis_executed"
    ):
        failures.append("final_synthesis_executed was false")

    if vars_.get("require_live_flags_false", True):
        if payload.get("live_sdk") or payload.get("live_search"):
            failures.append("live SDK/search flag was enabled in a dry-run eval")

    max_elapsed = vars_.get("max_elapsed_seconds")
    if max_elapsed is not None and payload.get("elapsed_seconds") is not None:
        try:
            if float(payload.get("elapsed_seconds")) > float(max_elapsed):
                failures.append(
                    f"elapsed_seconds={payload.get('elapsed_seconds')}, expected <= {max_elapsed}"
                )
        except (TypeError, ValueError):
            failures.append(f"invalid max_elapsed_seconds={max_elapsed!r}")

    if vars_.get("enforce_no_send", True):
        if payload.get("can_send_email") or payload.get("send_enabled"):
            failures.append("send/email capability was enabled")
        forbidden_actions = {str(item) for item in payload.get("forbidden_actions") or []}
        if "send_email" not in forbidden_actions:
            warnings.append("send_email was not listed in forbidden_actions")

    if (
        not expected_status
        and vars_.get("require_done_status", True)
        and payload.get("status") != "done"
    ):
        failures.append(f"status={payload.get('status')!r}, expected 'done'")

    score = _score(failures, warnings)
    if failures:
        return _result(False, score, "; ".join(failures + warnings))
    reason = "passed Keystone Slack invariants"
    if warnings:
        reason += "; warnings: " + "; ".join(warnings)
    return _result(True, score, reason)


def get_assert(output: str, context: dict[str, Any]) -> dict[str, Any]:
    """Promptfoo Python assertion entrypoint."""

    return grade_output(output, context)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str) and value:
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return None


def _score(failures: list[str], warnings: list[str]) -> float:
    if failures:
        return max(0.0, 1.0 - 0.25 * len(failures) - 0.05 * len(warnings))
    return max(0.0, 1.0 - 0.05 * len(warnings))


def _check_readable_summary(human_summary: str, failures: list[str]) -> None:
    lowered = human_summary.lower()
    for term in METADATA_SUMMARY_TERMS:
        if term.lower() in lowered:
            failures.append(f"human_summary exposes metadata term: {term}")
    if human_summary.strip().startswith(("{", "[")):
        failures.append("human_summary looks like raw JSON instead of an operator summary")
    if re.search(r"\b[a-z]+_[a-z_]+\s*[:=]", human_summary):
        failures.append("human_summary contains raw snake_case metadata fields")


def _result(pass_: bool, score: float, reason: str) -> dict[str, Any]:
    return {"pass": pass_, "score": round(score, 3), "reason": reason}


def main() -> int:
    output = sys.argv[1] if len(sys.argv) > 1 else ""
    context = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(json.dumps(grade_output(output, context), ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
