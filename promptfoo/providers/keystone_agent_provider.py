"""Promptfoo provider for Keystone Business Agents dry-run Slack asks."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from keystone_agents.structured_logging import structured_log_event

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}\b"),
    re.compile(r"\b(?:KEYSTONE_OPENAI_API_KEY|OPENAI_API_KEY|SLACK_[A-Z_]*TOKEN)\s*=\s*\S+"),
    re.compile(r"\bslack\s+token\s+\S+", re.IGNORECASE),
)
_CONTEXT_LINE_TERMS = (
    "slack_context",
    "selected_message",
    "thread_messages",
    "Original request:",
    '"user_input"',
)


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Run the repo-local ask_agent CLI and return a compact JSON output string."""

    config = options.get("config") or {}
    vars_ = context.get("vars") or {}
    agent = str(vars_.get("agent") or config.get("agent") or "orchestrator")
    request_text = str(vars_.get("user_input") or prompt).strip()
    if not request_text:
        return _provider_error("missing user_input", **_provider_log_context(vars_, agent=agent))

    python = str(config.get("python") or ".venv/bin/python")
    max_manager_steps = int(vars_.get("max_manager_steps") or config.get("max_manager_steps") or 1)
    timeout_seconds = int(vars_.get("timeout_seconds") or config.get("timeout_seconds") or 90)
    live_sdk = _truthy(vars_.get("live_sdk") if "live_sdk" in vars_ else config.get("live_sdk"))
    live_search = _truthy(
        vars_.get("live_search") if "live_search" in vars_ else config.get("live_search")
    )

    live_guard = _live_eval_guard(vars_, config, live_sdk=live_sdk, live_search=live_search)
    if live_guard:
        return _provider_error(
            live_guard,
            failure_kind="live_eval_guard",
            **_provider_log_context(vars_, agent=agent),
        )

    command = [
        _resolve_project_path(python),
        str(PROJECT_ROOT / "scripts" / "ask_agent.py"),
    ]
    if request_text.lower().startswith("@kni") or request_text.startswith("<@"):
        command.append(request_text)
    else:
        command.extend(["--input", request_text, "--agent", agent])
    command.extend(["--json", "--max-manager-steps", str(max_manager_steps)])
    if live_sdk:
        command.append("--live-sdk")
    else:
        command.append("--no-live-sdk")
    if live_search:
        command.append("--live-search")

    context_file = _write_slack_context(vars_)
    if context_file:
        command.extend(["--context-file", str(context_file)])

    started = time.monotonic()
    allowed_credential_categories = _allowed_credential_categories(
        vars_,
        config,
        live_sdk=live_sdk,
        live_search=live_search,
    )
    env = _child_env(
        vars_,
        config,
        live_sdk=live_sdk,
        live_search=live_search,
        allowed_credential_categories=allowed_credential_categories,
    )
    try:
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return _provider_error(
            f"ask_agent timed out after {timeout_seconds}s",
            failure_kind="timeout",
            stderr=str(exc),
            timeout=True,
            **_provider_log_context(vars_, agent=agent),
        )
    finally:
        _cleanup_promptfoo_context_file(context_file, keep=_truthy(config.get("keep_context_file")))

    elapsed_seconds = round(time.monotonic() - started, 3)
    if completed.returncode != 0:
        return _provider_error(
            f"ask_agent exited {completed.returncode}",
            failure_kind="non_zero_exit",
            return_code=completed.returncode,
            stderr=completed.stderr,
            stdout=completed.stdout,
            elapsed_seconds=elapsed_seconds,
            **_provider_log_context(vars_, agent=agent),
        )

    try:
        raw_payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        return _provider_error(
            f"ask_agent returned non-json output: {exc}",
            failure_kind="non_json_stdout",
            stdout=completed.stdout,
            stderr=completed.stderr,
            elapsed_seconds=elapsed_seconds,
            **_provider_log_context(vars_, agent=agent),
        )

    compact = _compact_run_payload(raw_payload)
    _attach_provider_invocation_mode(
        compact,
        live_sdk=live_sdk,
        live_search=live_search,
    )
    if context_file and not compact.get("slack_context_attached"):
        compact["slack_context_attached"] = True
    compact.update(
        {
            "provider_status": "ok",
            "elapsed_seconds": elapsed_seconds,
            "surface": str(vars_.get("surface") or "slack"),
            "provenance": _eval_provenance(
                vars_,
                config,
                raw_payload,
                live_sdk=live_sdk,
                live_search=live_search,
                allowed_credential_categories=allowed_credential_categories,
            ),
        }
    )
    return {"output": json.dumps(compact, ensure_ascii=True, sort_keys=True)}


def _resolve_project_path(value: str) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str(PROJECT_ROOT / path)


def _provider_error(message: str, **extra: Any) -> dict[str, str]:
    payload: dict[str, Any] = {
        "provider_status": "error",
        "error": _redact_text(message, max_chars=500),
        "failure_payload_redacted": True,
    }
    for key, value in extra.items():
        if value in (None, "", [], {}):
            continue
        if key in {"stdout", "stderr"}:
            excerpt, truncated = _redacted_excerpt(str(value))
            payload[f"{key}_excerpt"] = excerpt
            payload[f"{key}_truncated"] = truncated
        else:
            payload[key] = value
    payload["structured_log"] = structured_log_event(
        component="promptfoo_provider",
        event="provider_error",
        level="error",
        payload=payload,
        agent=payload.get("agent", ""),
        case_id=payload.get("case_id", ""),
        run_id=payload.get("run_id", ""),
        status="error",
        failure_kind=payload.get("failure_kind", ""),
    )
    return {"output": json.dumps(payload, ensure_ascii=True, sort_keys=True)}


def _provider_log_context(vars_: dict[str, Any], *, agent: str) -> dict[str, str]:
    slack_context = vars_.get("slack_context") if isinstance(vars_.get("slack_context"), dict) else {}
    return {
        "agent": str(agent or vars_.get("agent") or ""),
        "case_id": str(vars_.get("case_id") or ""),
        "run_id": str(vars_.get("run_id") or ""),
        "slack_channel_id": str(slack_context.get("channel_id") or ""),
        "slack_thread_ts": str(slack_context.get("thread_ts") or ""),
    }


def _write_slack_context(vars_: dict[str, Any]) -> Path | None:
    slack_context = vars_.get("slack_context")
    if not isinstance(slack_context, dict):
        return None

    selected_message = slack_context.get("selected_message")
    if not isinstance(selected_message, dict):
        selected_message = {
            "ts": slack_context.get("selected_message_ts") or "1800000000.000100",
            "user_id": slack_context.get("user_id") or "U_EVAL",
            "username": slack_context.get("username") or "eval-user",
            "text": vars_.get("user_input") or "",
            "permalink": slack_context.get("permalink") or "",
        }

    payload = {
        "schema": "keystone.slack.selected_message_context.v1",
        "channel_id": slack_context.get("channel_id") or "C0BA17Y9C01",
        "channel_name": slack_context.get("channel_name") or "evals",
        "selected_message_ts": slack_context.get("selected_message_ts")
        or selected_message.get("ts")
        or "1800000000.000100",
        "thread_ts": slack_context.get("thread_ts") or selected_message.get("ts") or "",
        "permalink": slack_context.get("permalink") or selected_message.get("permalink") or "",
        "selected_message": selected_message,
        "thread_messages": slack_context.get("thread_messages") or [],
        "metadata": {
            "source": "promptfoo",
            "promptfoo_case_id": vars_.get("case_id") or "",
            "context_scope": "selected_message_or_thread_recent_window",
            "sensitivity": "slack_context_local_only",
            "retention": "temporary_provider_context_deleted_after_run",
            "raw_text_persistence": "bounded_fixture_or_operator_selected_context",
        },
    }

    directory = Path(tempfile.mkdtemp(prefix="kba-promptfoo-slack-"))
    context_file = directory / "slack-context.json"
    context_file.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")
    return context_file


def _compact_run_payload(payload: dict[str, Any]) -> dict[str, Any]:
    work_item = payload.get("work_item") if isinstance(payload.get("work_item"), dict) else {}
    route_result = _nested(payload, "orchestrator_preflight", "route_result")
    if not isinstance(route_result, dict):
        route_result = {}
    sources = _collect_sources(payload, work_item)
    artifact_refs = _collect_artifacts(payload, work_item)
    audit_notes = (
        _as_strings(payload.get("audit_notes"))
        + _as_strings(route_result.get("audit_notes"))
        + _as_strings(work_item.get("audit_notes"))
    )
    context_sources = _extract_context_sources(audit_notes)
    nested_specialist_routes = _collect_nested_specialist_routes(payload)
    manager_efficiency = _nested(work_item, "target", "metadata", "manager_loop_efficiency")
    next_action = payload.get("next_action") or work_item.get("next_action") or {}
    selected_agent = payload.get("selected_agent")
    route = (
        payload.get("route")
        or (
            route_result.get("route")
            if payload.get("mode") == "blocked" and selected_agent == "orchestrator"
            else None
        )
        or selected_agent
        or work_item.get("current_route")
        or route_result.get("route")
        or ""
    )
    context_pack_type = _nested(payload, "context_pack", "pack_type") or _pack_type_for_route(
        route
    )
    source_types = [
        source.get("source_type") for source in sources if source.get("source_type")
    ]
    artifact_types = [
        artifact.get("artifact_type")
        for artifact in artifact_refs
        if artifact.get("artifact_type")
    ]
    live_sdk = (
        bool(manager_efficiency.get("live_sdk"))
        if isinstance(manager_efficiency, dict)
        else False
    )
    live_search = (
        bool(manager_efficiency.get("live_search"))
        if isinstance(manager_efficiency, dict)
        else False
    )
    if not live_sdk and not live_search and not any("no live APIs" in note for note in audit_notes):
        audit_notes.append("Promptfoo dry-run provider used no live APIs.")
    side_effects = _side_effect_evidence(payload, route_result)

    return {
        "status": payload.get("status") or work_item.get("status") or "",
        "route": route,
        "target_agent": route_result.get("target_agent") or "",
        "human_summary": _human_summary(payload),
        "source_count": len(sources),
        "source_urls": [source.get("url") for source in sources if source.get("url")],
        "source_titles": [source.get("title") for source in sources if source.get("title")],
        "source_types": source_types,
        "artifact_count": len(artifact_refs),
        "artifact_types": artifact_types,
        "audit_notes": audit_notes,
        "context_sources": context_sources,
        "nested_specialist_routes": nested_specialist_routes,
        "blockers": payload.get("blockers") or work_item.get("blockers") or [],
        "block_kind": _nested(payload, "orchestrator_preflight", "block_kind")
        or payload.get("block_kind")
        or "",
        "refused": bool(route_result.get("refused") or payload.get("mode") == "blocked"),
        "approval_required": bool(route_result.get("approval_required")),
        "external_use_approval_required": bool(route_result.get("external_use_approval_required")),
        "can_send_email": bool(route_result.get("can_send_email")),
        "send_enabled": bool(route_result.get("send_enabled")),
        "forbidden_actions": route_result.get("forbidden_actions") or [],
        "workflow": route_result.get("workflow") or [],
        "next_action_agent": next_action.get("agent") if isinstance(next_action, dict) else "",
        "next_action_requires_approval": bool(
            next_action.get("requires_approval") if isinstance(next_action, dict) else False
        ),
        "manager_review": _nested(work_item, "target", "metadata", "orchestrator_reviews"),
        "manager_review_scores": [
            item.get("overall_score")
            for item in _nested(work_item, "target", "metadata", "orchestrator_reviews")
            if isinstance(item, dict) and item.get("overall_score") is not None
        ],
        "final_synthesis_executed": bool(
            manager_efficiency.get("final_synthesis_executed")
            if isinstance(manager_efficiency, dict)
            else False
        ),
        "live_sdk": live_sdk,
        "live_search": live_search,
        "side_effects": side_effects,
        "external_write_performed": bool(side_effects.get("external_write_performed")),
        "side_effect_evidence_complete": bool(side_effects.get("evidence_complete")),
        "context_pack_type": context_pack_type,
        "slack_context_attached": bool(
            _nested(work_item, "target", "metadata", "slack_context")
            or _nested(payload, "context_pack", "target", "metadata", "slack_context")
        ),
    }


def _attach_provider_invocation_mode(
    compact: dict[str, Any],
    *,
    live_sdk: bool,
    live_search: bool,
) -> None:
    """Attach authoritative Promptfoo invocation mode beside agent-reported mode."""

    manager_live_sdk = bool(compact.get("live_sdk"))
    manager_live_search = bool(compact.get("live_search"))
    mismatches: list[str] = []
    if manager_live_sdk != live_sdk:
        mismatches.append("live_sdk")
    if manager_live_search != live_search:
        mismatches.append("live_search")
    compact["provider_invocation"] = {
        "schema": "keystone.promptfoo.provider_invocation.v1",
        "run_mode": "live_sdk" if live_sdk else "dry_run",
        "live_sdk": live_sdk,
        "live_search": live_search,
        "manager_live_sdk": manager_live_sdk,
        "manager_live_search": manager_live_search,
        "mode_mismatch_fields": mismatches,
    }
    compact["invocation_live_sdk"] = live_sdk
    compact["invocation_live_search"] = live_search
    compact["provider_invocation_mode_mismatch"] = bool(mismatches)
    compact["provider_invocation_mode_mismatch_fields"] = mismatches


def _pack_type_for_route(route: object) -> str:
    route_name = str(route or "").strip()
    return {
        "business_research_analyst": "research",
        "chief_of_staff": "research",
        "opportunity_scout": "opportunity",
        "outreach_composer": "outreach",
        "gmail_triage": "gmail",
    }.get(route_name, "")


def _collect_sources(payload: dict[str, Any], work_item: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    sources: list[dict[str, Any]] = []
    raw_sources = (
        list(work_item.get("sources") or [])
        + list(_nested(payload, "context_pack", "ordered_sources") or [])
        + _artifact_source_refs(payload, work_item)
    )
    for raw_source in raw_sources:
        if not isinstance(raw_source, dict):
            continue
        url = str(raw_source.get("url") or "")
        title = str(raw_source.get("title") or "")
        key = url or title
        if not key or key in seen:
            continue
        seen.add(key)
        sources.append(
            {
                "url": url,
                "title": title,
                "source_type": str(raw_source.get("source_type") or ""),
            }
        )
    return sources


def _live_eval_guard(
    vars_: dict[str, Any],
    config: dict[str, Any],
    *,
    live_sdk: bool,
    live_search: bool,
) -> str:
    ambient_live_search = _ambient_live_search_enabled()
    if live_search and not live_sdk:
        return "Promptfoo live_search requires live_sdk=true plus live eval controls"
    if ambient_live_search and live_sdk and not live_search:
        return "ambient live search is enabled, but this Promptfoo case did not set live_search=true"
    if not live_sdk:
        return ""

    case_id = str(vars_.get("case_id") or "").strip()
    allowlist = _string_list(vars_.get("case_allowlist") or config.get("case_allowlist"))
    selected_cases = _string_list(vars_.get("selected_cases") or config.get("selected_cases"))
    selected_case_count = _int_or_none(
        vars_.get("selected_case_count") or config.get("selected_case_count")
    )
    budget = _float_or_none(vars_.get("budget_usd") or config.get("budget_usd"))
    max_cases = _int_or_none(vars_.get("max_cases") or config.get("max_cases"))
    approval_ref = str(vars_.get("approval_ref") or config.get("approval_ref") or "").strip()
    run_label = str(vars_.get("run_label") or config.get("run_label") or "").strip()
    approved = _truthy(vars_.get("live_run_approved") or config.get("live_run_approved"))
    model_provider = str(vars_.get("model_provider") or config.get("model_provider") or "").strip()
    model_name = str(vars_.get("model_name") or config.get("model_name") or "").strip()
    missing: list[str] = []
    if not approved:
        missing.append("live_run_approved")
    if not approval_ref:
        missing.append("approval_ref")
    if budget is None or budget <= 0:
        missing.append("budget_usd")
    if max_cases is None or max_cases <= 0:
        missing.append("max_cases")
    elif allowlist and len(allowlist) > max_cases:
        return (
            f"Promptfoo live SDK eval allowlist contains {len(allowlist)} cases, "
            f"exceeding max_cases={max_cases}"
        )
    elif selected_case_count is not None and selected_case_count > max_cases:
        return (
            f"Promptfoo live SDK eval selected_case_count={selected_case_count} "
            f"exceeds max_cases={max_cases}"
        )
    elif selected_cases and len(selected_cases) > max_cases:
        return (
            f"Promptfoo live SDK eval selected_cases contains {len(selected_cases)} cases, "
            f"exceeding max_cases={max_cases}"
        )
    if case_id and allowlist and case_id not in allowlist:
        return f"case_id {case_id} is not in the approved live Promptfoo case allowlist"
    if selected_cases and allowlist:
        unapproved_cases = [item for item in selected_cases if item not in allowlist]
        if unapproved_cases:
            return (
                "Promptfoo live SDK eval selected_cases include unapproved cases: "
                + ", ".join(unapproved_cases[:10])
            )
    if case_id and not allowlist:
        missing.append("case_allowlist")
    if not run_label:
        missing.append("run_label")
    if not model_provider:
        missing.append("model_provider")
    if not model_name:
        missing.append("model_name")
    if live_search:
        if not str(vars_.get("search_provider") or config.get("search_provider") or "").strip():
            missing.append("search_provider")
        if _int_or_none(vars_.get("max_search_calls") or config.get("max_search_calls")) is None:
            missing.append("max_search_calls")
        if _int_or_none(vars_.get("max_search_results") or config.get("max_search_results")) is None:
            missing.append("max_search_results")
    if missing:
        return "Promptfoo live SDK eval requires explicit controls: " + ", ".join(sorted(set(missing)))
    return ""


def _ambient_live_search_enabled() -> bool:
    return (
        _truthy(os.environ.get("KEYSTONE_ENABLE_LIVE_RESEARCH"))
        and _truthy(os.environ.get("KEYSTONE_LIVE_MODE"))
        and not _truthy(os.environ.get("KEYSTONE_DRY_RUN"))
    )


def _child_env(
    vars_: dict[str, Any],
    config: dict[str, Any],
    *,
    live_sdk: bool,
    live_search: bool,
    allowed_credential_categories: list[str],
) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in (
        "PATH",
        "HOME",
        "TMPDIR",
        "PYTHONPATH",
        "VIRTUAL_ENV",
        "LANG",
        "LC_ALL",
        "KEYSTONE_HOME",
        "KEYSTONE_PROMPTFOO_HUMAN_REVIEW_DB",
        "KEYSTONE_TRACE_SUMMARY_DB",
        "KEYSTONE_TRACE_PROCESSOR",
        "KEYSTONE_TRACE_INCLUDE_SENSITIVE_DATA",
        "KEYSTONE_TRACING_DISABLED",
    ):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.update(
        {
            "KEYSTONE_PROMPTFOO_EVAL": "true",
            "KEYSTONE_EVAL_SURFACE": str(vars_.get("surface") or "slack"),
            "KEYSTONE_ENABLE_LIVE_RESEARCH": "true" if live_search else "false",
            "KEYSTONE_DRY_RUN": "false" if live_sdk else "true",
        }
    )
    model_provider = str(vars_.get("model_provider") or config.get("model_provider") or "openai").strip().lower()
    if live_sdk and "openai" in allowed_credential_categories and model_provider == "openai":
        _copy_env(env, "KEYSTONE_OPENAI_API_KEY")
        _copy_env(env, "KEYSTONE_OPENAI_BASE_URL")
        _copy_env(env, "KEYSTONE_OPENAI_MODEL")
    if live_sdk and "gemini" in allowed_credential_categories and model_provider == "gemini":
        _copy_env(env, "GEMINI_API_KEY")
    if live_sdk and "litellm" in allowed_credential_categories:
        _copy_env(env, "LITELLM_BASE_URL")
    if "slack" in allowed_credential_categories:
        _copy_env(env, "SLACK_BOT_TOKEN")
        _copy_env(env, "SLACK_APP_TOKEN")
    if "gmail" in allowed_credential_categories:
        _copy_prefix(env, "GOOGLE_")
        _copy_prefix(env, "GMAIL_")
    if live_search:
        search_provider = str(vars_.get("search_provider") or config.get("search_provider") or "").strip()
        fallback = str(vars_.get("fallback_search_provider") or config.get("fallback_search_provider") or "").strip()
        if search_provider:
            env["SEARCH_PROVIDER"] = search_provider
        if fallback:
            env["KEYSTONE_FALLBACK_SEARCH_PROVIDER"] = fallback
        if "searxng" in allowed_credential_categories:
            _copy_env(env, "SEARXNG_BASE_URL")
        if "exa" in allowed_credential_categories:
            _copy_env(env, "EXA_API_KEY")
        if "tavily" in allowed_credential_categories:
            _copy_env(env, "TAVILY_API_KEY")
        if "firecrawl" in allowed_credential_categories:
            _copy_env(env, "FIRECRAWL_API_KEY")
    return env


def _allowed_credential_categories(
    vars_: dict[str, Any],
    config: dict[str, Any],
    *,
    live_sdk: bool,
    live_search: bool,
) -> list[str]:
    categories = set(_string_list(vars_.get("allowed_credential_categories") or config.get("allowed_credential_categories")))
    model_provider = str(vars_.get("model_provider") or config.get("model_provider") or "openai").strip().lower()
    if live_sdk and model_provider == "openai":
        categories.add("openai")
    if live_search:
        for provider in _string_list(
            [
                vars_.get("search_provider") or config.get("search_provider"),
                vars_.get("fallback_search_provider") or config.get("fallback_search_provider"),
            ]
        ):
            if provider:
                categories.add(provider)
    return sorted(categories)


def _copy_env(target: dict[str, str], key: str) -> None:
    value = os.environ.get(key)
    if value:
        target[key] = value


def _copy_prefix(target: dict[str, str], prefix: str) -> None:
    for key, value in os.environ.items():
        if key.startswith(prefix) and value:
            target[key] = value


def _eval_provenance(
    vars_: dict[str, Any],
    config: dict[str, Any],
    payload: dict[str, Any],
    *,
    live_sdk: bool,
    live_search: bool,
    allowed_credential_categories: list[str],
) -> dict[str, Any]:
    manager_efficiency = _nested(payload, "work_item", "target", "metadata", "manager_loop_efficiency")
    if not isinstance(manager_efficiency, dict):
        manager_efficiency = {}
    search_provider = str(vars_.get("search_provider") or config.get("search_provider") or "").strip()
    provider_sequence = _string_list(
        vars_.get("search_provider_sequence")
        or config.get("search_provider_sequence")
        or ([search_provider] if search_provider else [])
    )
    return {
        "schema": "keystone.promptfoo.run_provenance.v1",
        "prompt_versions": _json_list(vars_.get("prompt_versions") or config.get("prompt_versions")),
        "prompt_metadata": _json_object(vars_.get("prompt_metadata") or config.get("prompt_metadata")),
        "model_provider": str(vars_.get("model_provider") or config.get("model_provider") or "").strip(),
        "model_name": str(vars_.get("model_name") or config.get("model_name") or "").strip(),
        "run_mode": "live_sdk" if live_sdk else "dry_run",
        "live_sdk": live_sdk,
        "live_search": live_search,
        "search_provider": search_provider,
        "search_provider_sequence": provider_sequence,
        "max_search_calls": _int_or_none(vars_.get("max_search_calls") or config.get("max_search_calls")),
        "max_search_results": _int_or_none(vars_.get("max_search_results") or config.get("max_search_results")),
        "budget_usd": _float_or_none(vars_.get("budget_usd") or config.get("budget_usd")),
        "max_cases": _int_or_none(vars_.get("max_cases") or config.get("max_cases")),
        "selected_case_count": _int_or_none(
            vars_.get("selected_case_count") or config.get("selected_case_count")
        ),
        "approval_ref": str(vars_.get("approval_ref") or config.get("approval_ref") or "").strip(),
        "run_label": str(vars_.get("run_label") or config.get("run_label") or "").strip(),
        "git_revision": str(vars_.get("git_revision") or config.get("git_revision") or os.environ.get("GIT_COMMIT") or "").strip(),
        "allowed_credential_categories": list(allowed_credential_categories),
        "manager_live_sdk": bool(manager_efficiency.get("live_sdk")),
        "manager_live_search": bool(manager_efficiency.get("live_search")),
    }


def _side_effect_evidence(payload: dict[str, Any], route_result: dict[str, Any]) -> dict[str, Any]:
    raw_present = isinstance(payload.get("side_effects"), dict)
    raw = payload.get("side_effects") if raw_present else {}
    approval_ref = str(raw.get("approval_ref") or route_result.get("approval_ref") or "").strip()
    flags = {
        "email_sent": _truthy(raw.get("email_sent")),
        "gmail_draft_created": _truthy(raw.get("gmail_draft_created")),
        "gmail_label_changed": _truthy(raw.get("gmail_label_changed")),
        "slack_message_posted": _truthy(raw.get("slack_message_posted")),
        "crm_write_performed": _truthy(raw.get("crm_write_performed")),
        "calendar_write_performed": _truthy(raw.get("calendar_write_performed")),
        "external_file_write_performed": _truthy(raw.get("external_file_write_performed")),
    }
    blocked = [str(item) for item in raw.get("blocked_write_attempts") or [] if str(item)]
    actions_present = any(flags.values()) or bool(blocked)
    if not raw_present:
        evidence_state = "missing"
    elif actions_present:
        evidence_state = "present_with_actions"
    else:
        evidence_state = "explicit_none"
    return {
        "schema": "keystone.promptfoo.side_effects.v1",
        "evidence_state": evidence_state,
        "instrumentation_present": raw_present,
        **flags,
        "external_write_performed": any(flags.values()),
        "blocked_write_attempts": blocked,
        "approval_ref": approval_ref,
        "evidence_complete": raw_present and bool(raw.get("evidence_complete", True)),
    }


def _extract_context_sources(audit_notes: list[str]) -> list[str]:
    seen: set[str] = set()
    sources: list[str] = []
    prefix = "Requested context sources tracked for specialist run:"
    for note in audit_notes:
        if prefix.lower() not in note.lower():
            continue
        _, _, tail = note.partition(":")
        for item in tail.split(","):
            source = item.strip()
            if source and source not in seen:
                seen.add(source)
                sources.append(source)
    return sources


def _collect_nested_specialist_routes(payload: dict[str, Any]) -> list[str]:
    seen: set[str] = set()
    routes: list[str] = []
    candidates = []
    for value in (
        payload.get("nested_specialist_results"),
        _nested(payload, "output", "nested_specialist_results"),
    ):
        if isinstance(value, list):
            candidates.extend(value)

    for item in candidates:
        if not isinstance(item, dict):
            continue
        route = str(
            item.get("specialist_route")
            or item.get("route")
            or item.get("route_name")
            or ""
        ).strip()
        if route and route not in seen:
            seen.add(route)
            routes.append(route)
    return routes


def _cleanup_promptfoo_context_file(context_file: Path | None, *, keep: bool = False) -> None:
    if keep or context_file is None:
        return
    parent = context_file.parent
    if parent.name.startswith("kba-promptfoo-slack-"):
        shutil.rmtree(parent, ignore_errors=True)


def _redacted_excerpt(value: str, *, max_chars: int = 700) -> tuple[str, bool]:
    lines = []
    for line in str(value or "").splitlines():
        if any(term in line for term in _CONTEXT_LINE_TERMS):
            lines.append("[REDACTED_CONTEXT_LINE]")
        else:
            lines.append(line)
    redacted = _redact_text("\n".join(lines), max_chars=max_chars)
    return redacted, len(redacted) >= max_chars or len(str(value or "")) > max_chars


def _redact_text(value: str, *, max_chars: int) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    if len(text) > max_chars:
        return text[:max_chars] + "...[truncated]"
    return text


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return False


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if item is not None and str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if item is not None and str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return _string_list(value)
        return parsed if isinstance(parsed, list) else []
    return []


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _collect_artifacts(payload: dict[str, Any], work_item: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    artifacts: list[dict[str, Any]] = []
    raw_artifacts = list(payload.get("artifact_refs") or []) + list(
        work_item.get("artifact_refs") or []
    )
    for raw_artifact in raw_artifacts:
        if not isinstance(raw_artifact, dict):
            continue
        key = str(raw_artifact.get("artifact_id") or raw_artifact.get("title") or raw_artifact)
        if key in seen:
            continue
        seen.add(key)
        artifacts.append(raw_artifact)
    return artifacts


def _artifact_source_refs(
    payload: dict[str, Any],
    work_item: dict[str, Any],
) -> list[dict[str, Any]]:
    source_refs: list[dict[str, Any]] = []
    for artifact in _collect_artifacts(payload, work_item):
        metadata = artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {}
        for source_ref in metadata.get("source_refs") or []:
            if isinstance(source_ref, dict):
                source_refs.append(source_ref)
    return source_refs


def _human_summary(payload: dict[str, Any]) -> str:
    output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
    route_result = _nested(payload, "orchestrator_preflight", "route_result")
    if not isinstance(route_result, dict):
        route_result = {}
    blockers = payload.get("blockers")
    if not blockers and isinstance(payload.get("work_item"), dict):
        blockers = payload["work_item"].get("blockers")
    blocker_messages = [
        str(item.get("message"))
        for item in blockers or []
        if isinstance(item, dict) and item.get("message")
    ]
    summary = str(
        payload.get("human_summary")
        or payload.get("message")
        or output.get("summary")
        or output.get("clarification_request")
        or output.get("rationale")
        or " ".join(blocker_messages)
        or ""
    )
    if (
        payload.get("mode") == "blocked"
        and str(payload.get("selected_agent") or "") == "outreach_composer"
    ):
        details = [
            "Approval is required before outreach drafting or external use."
            if route_result.get("approval_required") or output.get("approval_required")
            else "",
            str(payload.get("block_reason") or ""),
            str(route_result.get("approval_rationale") or output.get("approval_rationale") or ""),
        ]
        for detail in details:
            if detail and detail not in summary:
                summary = " ".join(part for part in (summary, detail) if part)
    elif route_result.get("approval_required") and "approval" not in summary.lower():
        summary = " ".join(
            part
            for part in (
                summary,
                "Approval is required before any live API call, external post, send, or write.",
            )
            if part
        )
    return summary


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return {} if key != keys[-1] else None
        current = current.get(key)
    return current if current is not None else {}


def _as_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item)]
