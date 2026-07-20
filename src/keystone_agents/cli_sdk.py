"""Shared CLI helpers for explicit SDK synthesis paths."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

from keystone_agents.config import load_settings
from keystone_agents.costing import (
    fetch_provider_cost_window,
    gemini_free_tier_usage_context,
    provider_cost_window_unqueried,
)
from keystone_agents.run import SDKSynthesisOutcome
from keystone_agents.sdk_sessions import (
    apply_sdk_session_env,
    build_sdk_session,
    build_sdk_session_from_env,
    resolve_sdk_session_spec,
)

SDKRunConfigFactory = Callable[[], Any]


def add_sdk_run_arguments(parser: argparse.ArgumentParser) -> None:
    """Add explicit SDK execution flags without changing existing `--sdk` behavior."""

    parser.add_argument(
        "--run-sdk",
        action="store_true",
        help=(
            "Run SDK synthesis with an injected fake/local run_config. "
            "Used by local harnesses and tests; no live credentials are required."
        ),
    )
    parser.add_argument(
        "--live-sdk",
        action="store_true",
        help=(
            "Run live SDK synthesis. Requires selected provider credentials and does "
            "not invoke outbound email, Slack, CRM, LinkedIn, scheduling, or publishing "
            "actions."
        ),
    )
    parser.add_argument(
        "--compact-instructions",
        action="store_true",
        help=(
            "Use the bounded direct-agent instruction profile. This preserves core, "
            "specialist, request-triggered, memory, writing, and safety contracts while "
            "omitting graph-oriented shared prompt material."
        ),
    )
    parser.add_argument(
        "--trace-include-sensitive-data",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Rejected for SDK synthesis; prompts and artifacts must not be traced.",
    )
    parser.add_argument(
        "--include-provider-cost-window",
        action="store_true",
        help=(
            "Include an explicit read-only provider admin cost-window lookup in SDK "
            "JSON output. Currently supports OpenAI organization Costs API and requires "
            "OPENAI_ADMIN_KEY."
        ),
    )
    parser.add_argument(
        "--provider-cost-window-seconds",
        type=int,
        default=600,
        help="Seconds to pad around the SDK run when querying provider aggregate costs.",
    )
    parser.add_argument(
        "--openai-cost-project-id",
        default=None,
        help="Optional OpenAI project ID filter for --include-provider-cost-window.",
    )
    add_sdk_session_arguments(parser)


def add_sdk_session_arguments(parser: argparse.ArgumentParser) -> None:
    """Add optional local SDK conversation-session flags."""

    parser.add_argument(
        "--sdk-session",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "Use a local Agents SDK SQLite conversation session for this SDK run. "
            "Defaults depend on the calling workflow; use --no-sdk-session to disable."
        ),
    )
    parser.add_argument(
        "--sdk-session-id",
        default="",
        help=(
            "Optional logical session label. It is hashed before use and is not stored "
            "as the raw session id."
        ),
    )
    parser.add_argument(
        "--sdk-session-db",
        default="",
        help="Optional SQLite path for local SDK session history.",
    )
    parser.add_argument(
        "--sdk-session-history-limit",
        type=int,
        default=None,
        help=(
            "Maximum recent SDK session items to retrieve for a run. Defaults to the "
            "central session policy."
        ),
    )


def sdk_execution_requested(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "run_sdk", False) or getattr(args, "live_sdk", False))


def resolve_sdk_execution(
    args: argparse.Namespace,
    *,
    run_config_factory: SDKRunConfigFactory | None,
) -> tuple[Any | None, bool]:
    """Return `(run_config, live)` for explicit SDK synthesis."""

    if getattr(args, "run_sdk", False) and getattr(args, "live_sdk", False):
        raise SystemExit("Use either --run-sdk or --live-sdk, not both.")
    if getattr(args, "trace_include_sensitive_data", False):
        raise SystemExit("trace_include_sensitive_data=true is rejected for SDK synthesis.")
    configure_sdk_session_from_args(
        args,
        scope="cli",
        components=("direct-script",),
        default_enabled=False,
    )
    if getattr(args, "live_sdk", False):
        load_settings(force_dotenv=True)
        return None, True
    if getattr(args, "run_sdk", False):
        if run_config_factory is None:
            raise SystemExit(
                "--run-sdk requires an explicit fake/local SDK run_config supplied by "
                "the caller. Use --live-sdk for credential-gated model execution."
            )
        run_config = run_config_factory()
        if run_config is None:
            raise SystemExit("--run-sdk run_config factory returned no run_config.")
        return run_config, False
    raise SystemExit("SDK execution was not requested.")


def sdk_session_from_args(
    args: argparse.Namespace,
    *,
    scope: str,
    components: tuple[str, ...] = (),
    default_enabled: bool = False,
) -> Any | None:
    """Build a local SDK session from CLI flags or inherited session env."""

    explicit = (
        getattr(args, "sdk_session", None) is not None
        or bool(getattr(args, "sdk_session_id", ""))
        or bool(getattr(args, "sdk_session_db", ""))
        or getattr(args, "sdk_session_history_limit", None) is not None
    )
    if explicit:
        spec = resolve_sdk_session_spec(
            scope=scope,
            components=components,
            enabled=getattr(args, "sdk_session", None),
            explicit_session_id=str(getattr(args, "sdk_session_id", "") or ""),
            database_path=str(getattr(args, "sdk_session_db", "") or ""),
            history_limit=getattr(args, "sdk_session_history_limit", None),
            default_enabled=default_enabled,
        )
        apply_sdk_session_env(spec)
        return build_sdk_session(spec)
    inherited_session = build_sdk_session_from_env()
    if inherited_session is not None:
        return inherited_session
    spec = resolve_sdk_session_spec(
        scope=scope,
        components=components,
        enabled=None,
        explicit_session_id="",
        database_path="",
        history_limit=getattr(args, "sdk_session_history_limit", None),
        default_enabled=default_enabled,
    )
    apply_sdk_session_env(spec)
    return build_sdk_session(spec)


def configure_sdk_session_from_args(
    args: argparse.Namespace,
    *,
    scope: str,
    components: tuple[str, ...] = (),
    default_enabled: bool = False,
) -> None:
    """Apply CLI SDK session flags to env for downstream centralized SDK runs."""

    if (
        getattr(args, "sdk_session", None) is None
        and not getattr(args, "sdk_session_id", "")
        and not getattr(args, "sdk_session_db", "")
        and getattr(args, "sdk_session_history_limit", None) is None
    ):
        return
    spec = resolve_sdk_session_spec(
        scope=scope,
        components=components,
        enabled=getattr(args, "sdk_session", None),
        explicit_session_id=str(getattr(args, "sdk_session_id", "") or ""),
        database_path=str(getattr(args, "sdk_session_db", "") or ""),
        history_limit=getattr(args, "sdk_session_history_limit", None),
        default_enabled=default_enabled,
    )
    apply_sdk_session_env(spec)


def reject_sdk_side_effect_flags(
    args: argparse.Namespace,
    flags: Mapping[str, str],
) -> None:
    """Block outbound or live-provider flags on SDK synthesis routes."""

    enabled = [flag for attr, flag in flags.items() if bool(getattr(args, attr, False))]
    if enabled:
        raise SystemExit(
            "SDK synthesis does not invoke live provider or outbound side effects. "
            f"Remove {' '.join(enabled)} and rerun."
        )


def sdk_agent_description(agent: Any) -> dict[str, Any]:
    """Return a non-executing SDK agent descriptor for CLI `--sdk` output."""

    return {
        "name": agent.name,
        "output_type": getattr(agent.output_type, "__name__", str(agent.output_type)),
        "tools": [getattr(tool, "name", getattr(tool, "__name__", "")) for tool in agent.tools],
        "sdk_run_invoked": False,
    }


def jsonable(value: Any) -> Any:
    """Convert Pydantic and nested values into JSON-serializable data."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(item) for item in value]
    return value


def sdk_synthesis_payload(
    outcome: SDKSynthesisOutcome,
    *,
    include_provider_cost_window: bool = False,
    provider_cost_window_seconds: int = 600,
    openai_cost_project_id: str | None = None,
) -> dict[str, Any]:
    """Render a safe, explicit SDK synthesis envelope for CLI output."""

    sdk_seconds = None
    if outcome.started_at_unix is not None and outcome.ended_at_unix is not None:
        sdk_seconds = round(max(0.0, outcome.ended_at_unix - outcome.started_at_unix), 3)
    provider_cost_window = (
        fetch_provider_cost_window(
            provider=outcome.model_provider,
            run_started_at=outcome.started_at_unix,
            run_ended_at=outcome.ended_at_unix,
            window_seconds=provider_cost_window_seconds,
            openai_project_id=openai_cost_project_id,
        )
        if include_provider_cost_window
        else provider_cost_window_unqueried()
    )
    return {
        "mode": "sdk-synthesis",
        "agent_name": outcome.agent_name,
        "dry_run": not outcome.live,
        "live_sdk": outcome.live,
        "sdk_run_invoked": True,
        "model": {
            "provider": outcome.model_provider,
            "name": outcome.model_name,
            "run_mode": outcome.model_run_mode,
        },
        "sdk_synthesis_seconds": sdk_seconds,
        "usage": jsonable(outcome.usage),
        "cost": jsonable(outcome.cost),
        "request_cache": jsonable(outcome.request_cache),
        "budget_guard": jsonable(outcome.budget_guard),
        "gemini_free_tier_usage": jsonable(
            outcome.provider_usage_context
            or gemini_free_tier_usage_context(
                provider=outcome.model_provider,
                model=outcome.model_name,
                usage=outcome.usage,
            )
        ),
        "provider_cost_window": jsonable(provider_cost_window),
        "output_type": type(outcome.final_output).__name__,
        "output": jsonable(outcome.final_output),
        "storage": jsonable(outcome.storage),
        "audit_notes": list(outcome.audit_notes),
    }
