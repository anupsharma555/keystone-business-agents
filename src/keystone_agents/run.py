"""Dry-run execution helpers."""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256
from json import JSONDecodeError, dumps, loads
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from keystone_agents.costing import (
    AgentRunBudgetExceededError,
    enforce_agent_run_budget,
    estimate_usage_cost,
    gemini_free_tier_usage_context,
)
from keystone_agents.model_provider import (
    GEMINI_PROVIDER,
    MissingOpenAIAPIKeyError,
    ModelConfig,
    ModelProviderConfigurationError,
    TraceConfig,
    TraceMetadata,
    get_gemini_fallback_model_config,
    get_openai_fallback_model_config,
    get_runtime_agent_model_config,
)
from keystone_agents.models import AgentRunRequest, AgentRunResult, RunMode, TypedAgentRunResult
from keystone_agents.operator_failures import known_exception_to_operator_failure
from keystone_agents.sdk import AgentLike, repo_instruction_profile_id, run_typed_sdk_sync
from keystone_agents.sdk_sessions import build_sdk_session_from_env, session_audit_metadata
from keystone_agents.tools.serper_tool import (
    consume_sdk_search_telemetry,
    reset_sdk_search_telemetry,
    sdk_search_diagnostics_from_telemetry,
    set_sdk_search_request_context,
)

TOutput = TypeVar("TOutput")
TRaw = TypeVar("TRaw")


@dataclass(frozen=True)
class SDKSynthesisOutcome:
    """Result envelope for retrieve-normalize-synthesize SDK workflows."""

    agent_name: str
    raw_context: Any
    typed_input: Any
    result: TypedAgentRunResult[Any]
    storage: dict[str, Any] = field(default_factory=dict)
    audit_notes: tuple[str, ...] = ()
    model_provider: str = ""
    model_name: str = ""
    model_run_mode: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    cost: dict[str, Any] = field(default_factory=dict)
    budget_guard: dict[str, Any] = field(default_factory=dict)
    request_cache: dict[str, Any] = field(default_factory=dict)
    provider_usage_context: dict[str, Any] = field(default_factory=dict)
    started_at_unix: float | None = None
    ended_at_unix: float | None = None

    @property
    def output(self) -> Any:
        return self.result.output

    @property
    def final_output(self) -> Any:
        return self.result.final_output

    @property
    def live(self) -> bool:
        return self.result.live


def run_agent_dry(agent: AgentLike, request: AgentRunRequest | None = None) -> AgentRunResult:
    """Return a deterministic result without invoking model or tool APIs."""

    request = request or AgentRunRequest()
    if request.mode != RunMode.DRY_RUN:
        raise RuntimeError("Live execution is not implemented in this scaffold.")

    return AgentRunResult(
        agent_name=agent.name,
        mode=RunMode.DRY_RUN,
        summary=f"{agent.name} constructed successfully in dry-run mode.",
        data={"payload": request.payload},
    )


def prompt_from_typed_input(value: Any) -> str:
    """Render typed SDK input objects to an agent prompt."""

    to_prompt = getattr(value, "to_prompt", None)
    if callable(to_prompt):
        return str(to_prompt())
    if isinstance(value, Mapping):
        return dumps(value, ensure_ascii=True, sort_keys=True, default=str)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return dumps(value, ensure_ascii=True, default=str)
    return str(value)


def run_typed_sdk_agent(
    *,
    agent: AgentLike,
    typed_input: Any,
    output_type: type[TOutput],
    run_config: Any | None = None,
    live: bool = False,
    config: ModelConfig | None = None,
    session: Any | None = None,
    workflow_name: str | None = None,
    group_id: str | None = None,
    trace_metadata: TraceMetadata | None = None,
    tracing_disabled: bool | None = None,
    trace_include_sensitive_data: bool | None = None,
    trace_config: TraceConfig | None = None,
    max_turns: int | None = None,
) -> TypedAgentRunResult[TOutput]:
    """Run an SDK agent through a typed, credential-safe execution path."""

    if run_config is not None:
        if config is not None:
            model_provider = config.provider
            model_name = config.model
        else:
            model_provider = "local"
            model_name = str(getattr(run_config, "model", "") or "sdk-local")
    else:
        model_config = config or get_runtime_agent_model_config(
            getattr(agent, "name", None),
            model_override=getattr(agent, "model", None),
        )
        model_provider = model_config.provider
        model_name = model_config.model
    resolved_session = session or build_sdk_session_from_env()
    prompt = prompt_from_typed_input(typed_input)
    request_cache = _sdk_request_cache_metadata(
        agent=agent,
        prompt=prompt,
        session=resolved_session,
    )
    retry_count = 0
    max_rate_limit_retries = _sdk_rate_limit_max_retries(live=live, run_config=run_config)
    while True:
        reset_sdk_search_telemetry()
        set_sdk_search_request_context(prompt)
        try:
            raw_result, output = run_typed_sdk_sync(
                agent,
                prompt,
                output_type,
                run_config=run_config,
                live=live,
                config=config,
                session=resolved_session,
                workflow_name=workflow_name,
                group_id=group_id,
                trace_metadata=trace_metadata,
                tracing_disabled=tracing_disabled,
                trace_include_sensitive_data=trace_include_sensitive_data,
                trace_config=trace_config,
                max_turns=max_turns,
            )
            search_telemetry = consume_sdk_search_telemetry()
            break
        except Exception as exc:
            consume_sdk_search_telemetry()
            if retry_count >= max_rate_limit_retries or not _is_sdk_rate_limit_error(exc):
                raise
            retry_count += 1
            time.sleep(_sdk_rate_limit_retry_delay_seconds(exc, retry_count))
    output = _attach_retrieval_diagnostics(output, search_telemetry)
    usage = _extract_sdk_usage(raw_result)
    cost = estimate_usage_cost(provider=model_provider, model=model_name, usage=usage)
    budget_guard = enforce_agent_run_budget(
        agent_name=agent.name,
        provider=model_provider,
        model=model_name,
        cost=cost,
        strict_unknown_cost=live and run_config is None,
    )
    return TypedAgentRunResult(
        agent_name=agent.name,
        output=output,
        raw_result=raw_result,
        live=live and run_config is None,
        usage=usage,
        cost=cost,
        budget_guard=budget_guard,
        request_cache={
            **request_cache,
            **({"rate_limit_retries": retry_count} if retry_count else {}),
        },
    )


def _sdk_rate_limit_max_retries(*, live: bool, run_config: Any | None) -> int:
    if not live or run_config is not None:
        return 0
    raw = os.getenv("KEYSTONE_SDK_RATE_LIMIT_MAX_RETRIES", "").strip()
    try:
        return max(0, min(3, int(raw) if raw else 1))
    except ValueError:
        return 1


def _is_sdk_rate_limit_error(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return True
    text = f"{type(exc).__name__} {exc}".lower()
    return "rate limit" in text or "429" in text or "rate_limit_exceeded" in text


def _sdk_rate_limit_retry_delay_seconds(exc: BaseException, retry_count: int) -> float:
    retry_after = getattr(exc, "retry_after", None)
    if retry_after is None:
        headers = getattr(exc, "headers", None)
        if isinstance(headers, Mapping):
            retry_after = headers.get("retry-after") or headers.get("Retry-After")
    try:
        delay = float(retry_after) if retry_after is not None else 0.0
    except (TypeError, ValueError):
        delay = 0.0
    if delay <= 0:
        match = re.search(r"try again in\s+([0-9]+(?:\.[0-9]+)?)s", str(exc), flags=re.I)
        if match:
            delay = float(match.group(1))
    if delay <= 0:
        delay = min(30.0, 4.0 * retry_count)
    return min(30.0, max(1.0, delay + 0.5))


def _attach_retrieval_diagnostics(
    output: TOutput, telemetry_packets: list[dict[str, Any]]
) -> TOutput:
    diagnostics = sdk_search_diagnostics_from_telemetry(telemetry_packets)
    if not diagnostics:
        return output
    model_fields = getattr(output, "model_fields", {})
    if isinstance(model_fields, dict) and "retrieval_diagnostics" in model_fields:
        model_copy = getattr(output, "model_copy", None)
        if callable(model_copy):
            return model_copy(update={"retrieval_diagnostics": diagnostics})
    if isinstance(output, dict):
        return {**output, "retrieval_diagnostics": diagnostics}  # type: ignore[return-value]
    return output


def sdk_synthesis_trace_metadata(
    *,
    agent_name: str,
    live: bool,
    save: bool,
    workflow: str | None = None,
    extra: TraceMetadata | None = None,
) -> dict[str, Any]:
    """Build trace metadata for live SDK synthesis without carrying prompt content."""

    metadata: dict[str, Any] = {
        "agent_name": agent_name,
        "run_kind": "sdk_synthesis",
        "live": bool(live),
        "save_requested": bool(save),
    }
    if workflow:
        metadata["workflow"] = workflow
    if extra:
        metadata.update(dict(extra))
    return dict(TraceConfig(trace_metadata=metadata).trace_metadata)


def _validate_synthesis_output(output: Any, output_type: type[TOutput]) -> TOutput:
    model_validate = getattr(output_type, "model_validate", None)
    if callable(model_validate):
        return model_validate(output)
    if isinstance(output, output_type):
        return output
    return output_type(output)


def _audit_payload(
    *,
    input_summary: str,
    input_audit_payload: Mapping[str, Any] | None,
) -> dict[str, Any]:
    if input_audit_payload is None:
        return {"input_summary": input_summary}
    return dict(input_audit_payload)


def _usage_attr(value: Any, name: str, default: int = 0) -> int:
    raw = value.get(name, default) if isinstance(value, Mapping) else getattr(value, name, default)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return default


def _extract_token_detail(value: Any, field_name: str) -> int:
    if value is None:
        return 0
    if isinstance(value, Mapping):
        raw = value.get(field_name, 0)
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            return 0
    return _usage_attr(value, field_name)


def _extract_sdk_usage(raw_result: Any) -> dict[str, Any]:
    """Return safe request/token usage metadata when the SDK exposes it."""

    prompt_cache_metadata = _prompt_cache_metadata(raw_result)
    usage = getattr(raw_result, "usage", None)
    if usage is None and isinstance(raw_result, Mapping):
        usage = raw_result.get("usage")
    context_wrapper = getattr(raw_result, "context_wrapper", None)
    if usage is None and context_wrapper is not None:
        usage = getattr(context_wrapper, "usage", None)
    if usage is None:
        return {
            "available": False,
            "requests": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
            "cached_input_tokens": None,
            "reasoning_output_tokens": None,
            "cache_hit_rate": None,
            **prompt_cache_metadata,
        }

    input_details = _first_attr(
        usage,
        "input_tokens_details",
        "prompt_tokens_details",
    )
    output_details = _first_attr(
        usage,
        "output_tokens_details",
        "completion_tokens_details",
    )
    input_tokens = _first_usage_int(
        usage,
        ("input_tokens", "prompt_tokens", "prompt_token_count", "promptTokenCount"),
    )
    cached_input_tokens = max(
        _extract_token_detail(input_details, "cached_tokens"),
        _first_usage_int(
            usage,
            (
                "input_cached_tokens",
                "cached_input_tokens",
                "cached_content_token_count",
                "cachedContentTokenCount",
            ),
        ),
    )
    return {
        "available": True,
        "requests": _first_usage_int(usage, ("requests", "num_model_requests"), default=1),
        "input_tokens": input_tokens,
        "output_tokens": _first_usage_int(
            usage,
            (
                "output_tokens",
                "completion_tokens",
                "candidates_token_count",
                "candidatesTokenCount",
            ),
        ),
        "total_tokens": _first_usage_int(
            usage,
            ("total_tokens", "total_token_count", "totalTokenCount"),
        ),
        "cached_input_tokens": cached_input_tokens,
        "reasoning_output_tokens": max(
            _extract_token_detail(output_details, "reasoning_tokens"),
            _first_usage_int(usage, ("thoughts_token_count", "thoughtsTokenCount")),
        ),
        "cache_hit_rate": _cache_hit_rate(input_tokens, cached_input_tokens),
        **prompt_cache_metadata,
    }


def _cache_hit_rate(input_tokens: int | None, cached_input_tokens: int | None) -> float | None:
    try:
        total = int(input_tokens or 0)
        cached = int(cached_input_tokens or 0)
    except (TypeError, ValueError):
        return None
    if total <= 0:
        return 0.0
    return round(min(total, max(0, cached)) / total, 4)


def _prompt_cache_metadata(raw_result: Any) -> dict[str, Any]:
    key = getattr(raw_result, "_generated_prompt_cache_key", None)
    if not key and isinstance(raw_result, Mapping):
        key = raw_result.get("generated_prompt_cache_key")
    if not key:
        state = getattr(raw_result, "state", None)
        key = getattr(state, "_generated_prompt_cache_key", None)
    if not key:
        return {
            "prompt_cache_key_present": False,
            "prompt_cache_key_hash": "",
        }
    digest = sha256(str(key).encode("utf-8")).hexdigest()[:12]
    return {
        "prompt_cache_key_present": True,
        "prompt_cache_key_hash": digest,
    }


def _sdk_request_cache_metadata(
    *,
    agent: AgentLike,
    prompt: str,
    session: Any | None,
) -> dict[str, Any]:
    """Return audit-safe fingerprints for cache-sensitive SDK request layout."""

    instructions = str(getattr(agent, "instructions", "") or "")
    tool_names = _ordered_tool_names(getattr(agent, "tools", []) or [])
    output_schema = _output_schema_payload(getattr(agent, "output_type", None))
    static_payload = {
        "instructions_sha256": _sha256(instructions),
        "tool_names": tool_names,
        "output_schema_sha256": _sha256_dumps(output_schema),
    }
    session_metadata = _session_audit_metadata(session)
    return {
        "request_layout": "static_agent_prefix_then_dynamic_typed_input",
        "repo_instruction_profile": repo_instruction_profile_id(),
        "static_prefix_sha256": _sha256_dumps(static_payload),
        "instructions_sha256": static_payload["instructions_sha256"],
        "tool_names_sha256": _sha256_dumps(tool_names),
        "tool_count": len(tool_names),
        "output_schema_sha256": static_payload["output_schema_sha256"],
        "dynamic_prompt_sha256": _sha256(prompt),
        "dynamic_prompt_chars": len(prompt),
        "session_attached": session is not None,
        **session_metadata,
        "note": (
            "Fingerprints are audit-safe diagnostics for prompt-cache behavior; raw "
            "instructions, tool schemas, session ids, and prompt text are not stored here."
        ),
    }


def _session_audit_metadata(session: Any | None) -> dict[str, Any]:
    if session is None:
        return {
            "session_scope": "",
            "session_source": "",
            "session_id_hash": "",
        }
    metadata = session_audit_metadata(session)
    return {
        "session_scope": str(metadata.get("scope") or ""),
        "session_source": str(metadata.get("source") or ""),
        "session_id_hash": str(metadata.get("session_id_hash") or ""),
    }


def _ordered_tool_names(tools: Sequence[Any]) -> list[str]:
    return [
        str(getattr(tool, "name", getattr(tool, "__name__", type(tool).__name__)) or "")
        for tool in tools
    ]


def _output_schema_payload(output_type: Any) -> Any:
    if hasattr(output_type, "model_json_schema"):
        try:
            return output_type.model_json_schema()
        except Exception:
            return str(output_type)
    return str(output_type or "")


def _sha256(value: str) -> str:
    return sha256(str(value or "").encode("utf-8")).hexdigest()


def _sha256_dumps(value: Any) -> str:
    return sha256(
        dumps(value, ensure_ascii=True, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _first_attr(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        item = getattr(value, name, None)
        if item is not None:
            return item
    return None


def _first_usage_int(
    usage: Any,
    names: tuple[str, ...],
    *,
    default: int = 0,
) -> int:
    for name in names:
        if isinstance(usage, Mapping):
            raw = usage.get(name)
        else:
            raw = getattr(usage, name, None)
        if raw is None:
            continue
        try:
            return int(raw or 0)
        except (TypeError, ValueError):
            continue
    return default


def _sdk_audit_output(
    *,
    output: Any,
    model_provider: str,
    model_name: str,
    model_run_mode: str,
    usage: dict[str, Any],
    cost: dict[str, Any],
    budget_guard: dict[str, Any],
    request_cache: dict[str, Any],
) -> dict[str, Any]:
    return {
        "result": output,
        "_sdk_model": {
            "provider": model_provider,
            "name": model_name,
            "run_mode": model_run_mode,
        },
        "_sdk_usage": usage,
        "_sdk_cost": cost,
        "_sdk_budget_guard": budget_guard,
        "_sdk_request_cache": request_cache,
    }


def _today_et() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


def _observed_daily_gemini_requests(
    storage: Any,
    *,
    model_provider: str,
    model_name: str,
) -> int | None:
    if model_provider != GEMINI_PROVIDER:
        return None
    try:
        if hasattr(storage, "list_records_by_created_date_et"):
            rows = storage.list_records_by_created_date_et("agent_runs", _today_et())
        elif hasattr(storage, "list_records"):
            rows = storage.list_records("agent_runs")
        else:
            return None
    except Exception:
        return None

    total = 0
    matched = False
    for row in rows:
        output_json = row.get("output_json") if isinstance(row, Mapping) else None
        if not output_json:
            continue
        try:
            output_payload = loads(str(output_json))
        except (JSONDecodeError, TypeError, ValueError):
            continue
        if not isinstance(output_payload, Mapping):
            continue
        sdk_model = output_payload.get("_sdk_model")
        sdk_usage = output_payload.get("_sdk_usage")
        if not isinstance(sdk_model, Mapping) or not isinstance(sdk_usage, Mapping):
            continue
        if sdk_model.get("provider") != GEMINI_PROVIDER:
            continue
        if sdk_model.get("name") != model_name:
            continue
        matched = True
        total += _usage_attr(sdk_usage, "requests", default=0)
    return total if matched else None


def run_retrieved_sdk_synthesis(
    *,
    agent: AgentLike,
    output_type: type[TOutput],
    retrieve: Callable[[], TRaw],
    normalize: Callable[[TRaw], Any],
    input_summary: str,
    input_audit_payload: Mapping[str, Any] | None = None,
    run_config: Any | None = None,
    live: bool = False,
    config: ModelConfig | None = None,
    session: Any | None = None,
    workflow_name: str | None = None,
    group_id: str | None = None,
    trace_metadata: TraceMetadata | None = None,
    tracing_disabled: bool | None = None,
    trace_include_sensitive_data: bool | None = None,
    trace_config: TraceConfig | None = None,
    save: bool = False,
    storage: Any | None = None,
    persist_output: Callable[[TOutput], Mapping[str, Any] | None] | None = None,
    model_label: str | None = None,
) -> SDKSynthesisOutcome:
    """
    Retrieve and normalize context in Python, synthesize with an SDK agent, then audit.

    The helper keeps live model execution behind the existing SDK credential gate. It
    never invokes outbound integrations itself; callers may only pass local storage
    persistence for explicitly requested `save` runs.
    """

    if save and storage is None:
        raise RuntimeError("storage is required when save=True for SDK synthesis.")
    if trace_include_sensitive_data is True or (
        trace_config is not None and trace_config.trace_include_sensitive_data
    ):
        raise RuntimeError("SDK synthesis traces must not include sensitive data.")

    if run_config is not None:
        if config is not None:
            resolved_model_provider = config.provider
            resolved_model_name = config.model
        else:
            resolved_model_provider = "local"
            resolved_model_name = str(
                getattr(run_config, "model", "") or model_label or "sdk-local"
            )
        resolved_model_run_mode = "local_sdk"
    else:
        resolved_model_config = config or get_runtime_agent_model_config(
            getattr(agent, "name", None),
            model_override=getattr(agent, "model", None),
        )
        resolved_model_provider = resolved_model_config.provider
        resolved_model_name = resolved_model_config.model
        resolved_model_run_mode = "live_sdk" if live else "sdk"
    fallback_model_configs = (
        [
            fallback_config
            for fallback_config in (
                get_openai_fallback_model_config(
                    getattr(agent, "name", None),
                    model_override=None,
                ),
                get_gemini_fallback_model_config(
                    getattr(agent, "name", None),
                    model_override=None,
                ),
            )
            if fallback_config is not None
        ]
        if live and run_config is None and config is None
        else []
    )

    workflow = workflow_name or f"Keystone {agent.name} SDK synthesis"
    resolved_trace_metadata = sdk_synthesis_trace_metadata(
        agent_name=agent.name,
        live=live and run_config is None,
        save=save,
        workflow=workflow,
        extra=trace_metadata,
    )
    raw_context = retrieve()
    typed_input = normalize(raw_context)
    audit_payload = _audit_payload(
        input_summary=input_summary,
        input_audit_payload=input_audit_payload,
    )
    storage_results: dict[str, Any] = {}
    started_at = time.time()
    audit_notes = [
        "Retrieved and normalized context in Python before SDK execution.",
        "Validated structured output before optional persistence.",
        "No outbound side effects were invoked by the synthesis harness.",
    ]

    try:
        live_attempts: list[tuple[str, ModelConfig | None]] = [(resolved_model_provider, config)]
        for fallback_model_config in fallback_model_configs:
            live_attempts.append((fallback_model_config.provider, fallback_model_config))

        last_exc: Exception | None = None
        for attempt_index, (attempt_provider, attempt_config) in enumerate(live_attempts):
            try:
                typed_result = run_typed_sdk_agent(
                    agent=agent,
                    typed_input=typed_input,
                    output_type=output_type,
                    run_config=run_config,
                    live=live,
                    config=attempt_config,
                    session=session,
                    workflow_name=workflow,
                    group_id=group_id,
                    trace_metadata=resolved_trace_metadata,
                    tracing_disabled=tracing_disabled,
                    trace_include_sensitive_data=trace_include_sensitive_data,
                    trace_config=trace_config,
                )
                if attempt_config is not None:
                    resolved_model_provider = attempt_config.provider
                    resolved_model_name = attempt_config.model
                    resolved_model_run_mode = "live_sdk" if live else "sdk"
                break
            except Exception as exc:
                last_exc = exc
                if isinstance(
                    exc,
                    AgentRunBudgetExceededError | ModelProviderConfigurationError,
                ):
                    raise
                if isinstance(exc, MissingOpenAIAPIKeyError) and not fallback_model_configs:
                    raise
                if attempt_index >= len(live_attempts) - 1:
                    raise
                fallback_provider = live_attempts[attempt_index + 1][0]
                fallback_label = {"openai": "OpenAI", "gemini": "Gemini"}.get(
                    fallback_provider,
                    fallback_provider,
                )
                audit_notes.append(
                    "Primary live SDK provider "
                    f"{attempt_provider} failed with {type(exc).__name__}; "
                    f"retried once with {fallback_label} fallback."
                )
        else:
            raise RuntimeError("SDK synthesis did not execute a model attempt.") from last_exc

        validated_output = _validate_synthesis_output(typed_result.output, output_type)
        typed_result = TypedAgentRunResult(
            agent_name=typed_result.agent_name,
            output=validated_output,
            raw_result=typed_result.raw_result,
            live=typed_result.live,
            usage=typed_result.usage,
            cost=typed_result.cost,
            budget_guard=typed_result.budget_guard,
            request_cache=typed_result.request_cache,
        )
        usage = typed_result.usage or _extract_sdk_usage(typed_result.raw_result)
        cost = typed_result.cost or estimate_usage_cost(
            provider=resolved_model_provider,
            model=resolved_model_name,
            usage=usage,
        )
        provider_usage_context = gemini_free_tier_usage_context(
            provider=resolved_model_provider,
            model=resolved_model_name,
            usage=usage,
        )

        if save:
            if persist_output is not None:
                persisted = persist_output(validated_output)
                if persisted:
                    storage_results["artifact"] = dict(persisted)
            audit_output = _sdk_audit_output(
                output=validated_output,
                model_provider=resolved_model_provider,
                model_name=resolved_model_name,
                model_run_mode=resolved_model_run_mode,
                usage=usage,
                cost=cost,
                budget_guard=typed_result.budget_guard,
                request_cache=typed_result.request_cache,
            )
            storage_results["agent_run"] = storage.save_agent_run(
                agent_name=agent.name,
                input_payload=audit_payload,
                input_summary=input_summary,
                output=audit_output,
                model=model_label or ("sdk-live" if typed_result.live else "sdk-local"),
                dry_run=not typed_result.live,
                status="success",
            )
            observed_daily_requests = _observed_daily_gemini_requests(
                storage,
                model_provider=resolved_model_provider,
                model_name=resolved_model_name,
            )
            if observed_daily_requests is not None:
                provider_usage_context = gemini_free_tier_usage_context(
                    provider=resolved_model_provider,
                    model=resolved_model_name,
                    usage=usage,
                    observed_daily_requests=observed_daily_requests,
                    daily_usage_source="local_sqlite_agent_runs_current_et_day",
                )
    except Exception as exc:
        operator_failure = known_exception_to_operator_failure(
            exc,
            context=f"{agent.name} SDK run",
        )
        if save and storage is not None:
            storage_results["agent_run"] = storage.save_agent_run(
                agent_name=agent.name,
                input_payload=audit_payload,
                input_summary=input_summary,
                output={
                    "failure": operator_failure.to_dict(),
                    "summary": operator_failure.summary,
                    "next_step": operator_failure.next_step,
                    "send_enabled": False,
                },
                model=model_label or ("sdk-live" if live and run_config is None else "sdk-local"),
                dry_run=not (live and run_config is None),
                status="error",
                error=type(exc).__name__,
            )
        raise

    return SDKSynthesisOutcome(
        agent_name=agent.name,
        raw_context=raw_context,
        typed_input=typed_input,
        result=typed_result,
        storage=storage_results,
        audit_notes=tuple(audit_notes),
        model_provider=resolved_model_provider,
        model_name=resolved_model_name,
        model_run_mode=resolved_model_run_mode,
        usage=usage,
        cost=cost,
        budget_guard=typed_result.budget_guard,
        request_cache=typed_result.request_cache,
        provider_usage_context=provider_usage_context,
        started_at_unix=started_at,
        ended_at_unix=time.time(),
    )
