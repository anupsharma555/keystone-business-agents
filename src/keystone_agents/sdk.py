"""Shared wrapper around the OpenAI Agents SDK.

This is the only module that imports from the `agents` package. Other modules
should import SDK types, decorators, and helper constructors from here so model
configuration and test behavior stay centralized.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy as shallow_copy
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from functools import wraps
from hashlib import sha256
from importlib import resources
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from keystone_agents.capabilities.catalog import (
    append_runtime_capability_catalog,
    instruction_profile_text,
)
from keystone_agents.model_provider import (
    ModelConfig,
    TraceConfig,
    TraceMetadata,
    get_model_config,
    get_runtime_agent_model_config,
    get_trace_config,
    uses_gpt56_cache_controls,
)
from keystone_agents.runtime.execution_deadline import current_execution_deadline
from keystone_agents.runtime.request_budget import current_model_request_budget
from keystone_agents.runtime.response_terminal import ResponseTerminalObservation

try:
    from agents import (
        Agent as SDKAgent,
    )
    from agents import (
        FileSearchTool as SDKFileSearchTool,
    )
    from agents import (
        GuardrailFunctionOutput,
        ModelSettings,
        OpenAIProvider,
        RunConfig,
        RunContextWrapper,
        Runner,
        SessionSettings,
        SQLiteSession,
        ToolGuardrailFunctionOutput,
        ToolInputGuardrailData,
        ToolOutputGuardrailData,
        input_guardrail,
        output_guardrail,
        set_tracing_export_api_key,
        tool_input_guardrail,
        tool_output_guardrail,
    )
    from agents import (
        HostedMCPTool as SDKHostedMCPTool,
    )
    from agents import (
        ToolSearchTool as SDKToolSearchTool,
    )
    from agents import (
        WebSearchTool as SDKWebSearchTool,
    )
    from agents import (
        function_tool as _sdk_function_tool,
    )
    from agents.lifecycle import RunHooksBase
    from openai import AsyncOpenAI
    from openai.types.shared.reasoning import Reasoning
except ImportError as exc:  # pragma: no cover - depends on optional local install state.
    _SDK_IMPORT_ERROR: ImportError | None = exc
    GuardrailFunctionOutput = Any  # type: ignore
    ModelSettings = Any  # type: ignore
    OpenAIProvider = Any  # type: ignore
    Reasoning = Any  # type: ignore
    RunConfig = Any  # type: ignore
    RunContextWrapper = Any  # type: ignore
    RunHooksBase = object  # type: ignore[assignment,misc]
    Runner = Any  # type: ignore
    SessionSettings = Any  # type: ignore
    SQLiteSession = Any  # type: ignore
    _sdk_function_tool = None  # type: ignore[assignment]
    set_tracing_export_api_key = None  # type: ignore[assignment]
    AsyncOpenAI = Any  # type: ignore
    SDKWebSearchTool = None  # type: ignore[assignment]
    SDKFileSearchTool = None  # type: ignore[assignment]
    SDKHostedMCPTool = None  # type: ignore[assignment]
    SDKToolSearchTool = None  # type: ignore[assignment]

    def input_guardrail(*_: Any, **__: Any) -> Any:  # type: ignore
        return lambda wrapped: wrapped

    def output_guardrail(*_: Any, **__: Any) -> Any:  # type: ignore
        return lambda wrapped: wrapped

    def tool_input_guardrail(*_: Any, **__: Any) -> Any:  # type: ignore
        return lambda wrapped: LocalToolInputGuardrail(wrapped)

    def tool_output_guardrail(*_: Any, **__: Any) -> Any:  # type: ignore
        return lambda wrapped: LocalToolOutputGuardrail(wrapped)

    SDKAgent = None  # type: ignore
else:
    _SDK_IMPORT_ERROR = None

DEFAULT_LIVE_MODEL_TIMEOUT_SECONDS = 45.0
DEFAULT_LIVE_MODEL_MAX_RETRIES = 0
LIVE_MODEL_TIMEOUT_SECONDS_ENV = "KEYSTONE_LIVE_MODEL_TIMEOUT_SECONDS"
LIVE_MODEL_MAX_RETRIES_ENV = "KEYSTONE_LIVE_MODEL_MAX_RETRIES"
SDK_INCLUDE_USAGE_ENV = "KEYSTONE_SDK_INCLUDE_USAGE"
SDK_PROMPT_CACHE_RETENTION_ENV = "KEYSTONE_SDK_PROMPT_CACHE_RETENTION"
SDK_PROMPT_CACHE_SCOPE_ENV = "KEYSTONE_SDK_PROMPT_CACHE_SCOPE"
DEFAULT_PROMPT_CACHE_RETENTION = "24h"
DEFAULT_PROMPT_CACHE_SCOPE = "local-single-operator"
_FALSE_ENV_VALUES = {"", "0", "false", "no", "off", "disabled"}


@dataclass(frozen=True)
class SDKDataHandlingProfile:
    """Request-local SDK data controls for bounded private-context execution."""

    name: str
    store: bool
    prompt_cache_retention: str | None
    tracing_disabled: bool
    trace_include_sensitive_data: bool

    def audit_metadata(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "response_store": self.store,
            "prompt_cache_retention": self.prompt_cache_retention,
            "tracing_disabled": self.tracing_disabled,
            "trace_include_sensitive_data": self.trace_include_sensitive_data,
        }


PRIVATE_CONTEXT_SDK_PROFILE = SDKDataHandlingProfile(
    name="bounded_private_context",
    store=False,
    prompt_cache_retention="in_memory",
    tracing_disabled=True,
    trace_include_sensitive_data=False,
)
_SDK_DATA_HANDLING_PROFILE: ContextVar[SDKDataHandlingProfile | None] = ContextVar(
    "keystone_sdk_data_handling_profile",
    default=None,
)
_RESPONSE_TERMINAL_OBSERVER: ContextVar[Any | None] = ContextVar(
    "keystone_response_terminal_observer", default=None,
)


@contextmanager
def private_context_sdk_profile():
    """Force non-persistent SDK settings for one bounded private-context run."""

    token = _SDK_DATA_HANDLING_PROFILE.set(PRIVATE_CONTEXT_SDK_PROFILE)
    try:
        yield PRIVATE_CONTEXT_SDK_PROFILE
    finally:
        _SDK_DATA_HANDLING_PROFILE.reset(token)


def active_sdk_data_handling_profile() -> SDKDataHandlingProfile | None:
    """Return the request-local data profile, if one is active."""

    return _SDK_DATA_HANDLING_PROFILE.get()


def agent_with_stable_prompt_cache_key(
    agent: AgentLike,
    *,
    provider: str,
    model_name: str,
) -> AgentLike:
    """Clone one OpenAI agent with a privacy-scoped static-prefix cache key.

    The key groups equivalent static execution profiles across asks, Slack
    threads, and follow-ups. It deliberately excludes request text, provider
    records, Slack identifiers, session identifiers, and other dynamic input.
    The provider still validates the exact prompt prefix before reusing cached
    tokens; this key is a grouping hint, not an execution or authorization
    cache.
    """

    if str(provider or "").strip().lower() != "openai":
        return agent
    scope = _sdk_prompt_cache_scope()
    if scope is None:
        return agent
    settings = getattr(agent, "model_settings", None)
    if _prompt_cache_key_from_model_settings(settings):
        return agent

    instructions = instruction_profile_text(agent)
    if instructions is None:
        # An arbitrary callable needs run context; do not mint a false static cache identity.
        return agent
    profile = active_sdk_data_handling_profile()
    key_payload = {
        "version": "keystone.prompt_cache_profile.v1",
        "privacy_scope_sha256": sha256(scope.encode("utf-8")).hexdigest(),
        "data_handling_profile": profile.name if profile is not None else "default",
        "repo_instruction_profile": repo_instruction_profile_id(),
        "agent_name": str(getattr(agent, "name", "") or ""),
        "model_name": str(model_name or getattr(agent, "model", "") or ""),
        "instructions_sha256": sha256(
            instructions.encode("utf-8")
        ).hexdigest(),
        "tools": _prompt_cache_tool_payload(getattr(agent, "tools", []) or []),
        "output_schema": _prompt_cache_output_schema(
            getattr(agent, "output_type", None)
        ),
    }
    digest = sha256(
        json.dumps(
            key_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:32]
    key = f"kba:prompt:v1:{digest}"
    updated_settings = _model_settings_with_explicit_prompt_cache_key(settings, key)
    clone = getattr(agent, "clone", None)
    if callable(clone):
        return clone(model_settings=updated_settings)
    if isinstance(agent, LocalAgent):
        return dataclass_replace(agent, model_settings=updated_settings)
    return agent


def agent_with_isolated_tool_state(agent: AgentLike) -> AgentLike:
    """Return a run-local agent whose mutable tool state is not shared.

    SDK ``Agent.clone()`` copies the tools list shallowly, so changing a
    ``FunctionTool`` during one retry can otherwise disable or instrument the
    module-level tool object used by later agents. Tool callbacks, schemas, and
    callable enable predicates remain shared by reference; shared tool
    containers and their mutable guardrail lists are copied for this run.
    Fresh nested-specialist tools keep their parent-bound identity because their
    callbacks intentionally disable that exact one-shot child tool.
    """

    tools = list(getattr(agent, "tools", []) or [])
    if not tools:
        return agent
    isolated_tools: list[Any] = []
    identity_bound_agent = False
    for tool in tools:
        if (
            getattr(tool, "nested_execution_contract", "")
            == "validated_child_decision_v1"
        ):
            # This run-local Chief tool intentionally closes over itself and its
            # freshly built parent agent so it can disable one completed child
            # handoff. Copying either object would sever that replay boundary.
            identity_bound_agent = True
            isolated_tools.append(tool)
            continue
        isolated_tool = shallow_copy(tool)
        for attribute in ("tool_input_guardrails", "tool_output_guardrails"):
            guardrails = getattr(tool, attribute, None)
            if isinstance(guardrails, list):
                setattr(isolated_tool, attribute, list(guardrails))
        isolated_tools.append(isolated_tool)

    isolated_agent = agent if identity_bound_agent else shallow_copy(agent)
    isolated_agent.tools = isolated_tools
    return isolated_agent


def agent_with_retry_compatible_tool_choice(
    agent: AgentLike,
    *,
    disabled_tool_names: Sequence[str],
) -> tuple[AgentLike, dict[str, Any] | None]:
    """Clear a forced tool choice when retry safety disabled that exact tool.

    Completed reads and mutations are disabled before a bounded semantic retry.
    The Agents SDK omits disabled tools from the provider request, so retaining a
    named ``tool_choice`` for one of them produces an invalid request before the
    model can use the replayed evidence. Return a run-attempt clone so the
    caller's agent configuration remains unchanged.
    """

    disabled = {
        str(name).strip() for name in disabled_tool_names if str(name).strip()
    }
    if not disabled:
        return agent, None
    settings = getattr(agent, "model_settings", None)
    if settings is None:
        return agent, None
    tool_choice = (
        settings.get("tool_choice")
        if isinstance(settings, Mapping)
        else getattr(settings, "tool_choice", None)
    )
    if tool_choice is None:
        return agent, None

    enabled_tool_names = {
        str(getattr(tool, "name", "") or "").strip()
        for tool in list(getattr(agent, "tools", []) or [])
        if str(getattr(tool, "name", "") or "").strip()
        and getattr(tool, "is_enabled", True) is not False
    }
    choice_name = _named_function_tool_choice(tool_choice)
    clear_choice = bool(choice_name and choice_name in disabled)
    normalized_choice = str(tool_choice).strip().lower()
    if not enabled_tool_names and normalized_choice not in {"", "auto", "none"}:
        # The Agents SDK may normalize a named choice to the sentinel
        # ``function`` after the first tool turn. Once retry safety disables all
        # completed tools, retaining any forced choice is invalid even when its
        # original function name is no longer recoverable.
        clear_choice = True
    if not clear_choice:
        return agent, None

    if isinstance(settings, Mapping):
        updated_settings: Any = dict(settings)
        updated_settings["tool_choice"] = None
    else:
        updated_settings = dataclass_replace(settings, tool_choice=None)
    clone = getattr(agent, "clone", None)
    if callable(clone):
        updated_agent = clone(model_settings=updated_settings)
    elif isinstance(agent, LocalAgent):
        updated_agent = dataclass_replace(agent, model_settings=updated_settings)
    else:
        updated_agent = shallow_copy(agent)
        updated_agent.model_settings = updated_settings
    return updated_agent, {
        "schema": "keystone.retry_tool_choice_adjustment.v1",
        "prior_choice": choice_name or str(tool_choice),
        "adjusted_choice": "auto",
        "reason": "forced_tool_disabled_after_completed_operation",
        "disabled_tool_names": sorted(disabled),
        "enabled_tool_names": sorted(enabled_tool_names),
    }


def _named_function_tool_choice(tool_choice: Any) -> str:
    if isinstance(tool_choice, str):
        value = tool_choice.strip()
        return "" if value.lower() in {"", "auto", "none", "required"} else value
    if not isinstance(tool_choice, Mapping):
        return ""
    direct_name = str(tool_choice.get("name") or "").strip()
    if direct_name:
        return direct_name
    function = tool_choice.get("function")
    if isinstance(function, Mapping):
        return str(function.get("name") or "").strip()
    return ""


def prompt_cache_key_audit_metadata(agent: AgentLike) -> dict[str, Any]:
    """Return audit-safe metadata for an explicit profile cache key."""

    key = _prompt_cache_key_from_model_settings(
        getattr(agent, "model_settings", None)
    )
    if not key:
        return {
            "prompt_cache_key_present": False,
            "prompt_cache_key_hash": "",
            "prompt_cache_key_source": "",
        }
    return {
        "prompt_cache_key_present": True,
        "prompt_cache_key_hash": sha256(key.encode("utf-8")).hexdigest()[:12],
        "prompt_cache_key_source": "explicit_static_profile",
    }


def _sdk_prompt_cache_scope() -> str | None:
    raw = os.getenv(SDK_PROMPT_CACHE_SCOPE_ENV)
    value = DEFAULT_PROMPT_CACHE_SCOPE if raw is None else raw.strip()
    if value.lower() in _FALSE_ENV_VALUES:
        return None
    return value


def _prompt_cache_key_from_model_settings(settings: Any) -> str:
    if isinstance(settings, Mapping):
        containers = (settings.get("extra_args"), settings.get("extra_body"))
    else:
        containers = (
            getattr(settings, "extra_args", None),
            getattr(settings, "extra_body", None),
        )
    for container in containers:
        if isinstance(container, Mapping) and container.get("prompt_cache_key"):
            return str(container["prompt_cache_key"])
    return ""


def _model_settings_with_explicit_prompt_cache_key(
    settings: Any,
    key: str,
) -> Any:
    if isinstance(settings, Mapping):
        updated = dict(settings)
        extra_args = dict(updated.get("extra_args") or {})
        extra_args["prompt_cache_key"] = key
        updated["extra_args"] = extra_args
        return updated
    resolved = settings or ModelSettings()
    extra_args = dict(getattr(resolved, "extra_args", None) or {})
    extra_args["prompt_cache_key"] = key
    return dataclass_replace(resolved, extra_args=extra_args)


def _prompt_cache_tool_payload(tools: Sequence[Any]) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for tool in tools:
        schema = getattr(tool, "params_json_schema", None)
        if schema is None:
            schema = getattr(tool, "parameters", None)
        payload.append(
            {
                "name": str(
                    getattr(tool, "name", "")
                    or getattr(tool, "__name__", type(tool).__name__)
                ),
                "schema": schema if isinstance(schema, Mapping) else {},
            }
        )
    return payload


def _prompt_cache_output_schema(output_type: Any) -> dict[str, Any]:
    schema = getattr(output_type, "model_json_schema", None)
    if callable(schema):
        try:
            value = schema()
        except (TypeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}
    return {}

if _SDK_IMPORT_ERROR is not None:
    _SANDBOX_IMPORT_ERROR: ImportError | None = _SDK_IMPORT_ERROR
    SDKManifest = None  # type: ignore
    SDKSandboxAgent = None  # type: ignore
    SDKSandboxRunConfig = None  # type: ignore
    SDKDir = None  # type: ignore
    SDKFile = None  # type: ignore
    SDKLocalDir = None  # type: ignore
    SDKLocalFile = None  # type: ignore
    SDKUnixLocalSandboxClient = None  # type: ignore
else:
    try:
        from agents.sandbox import (  # type: ignore
            Manifest as SDKManifest,
        )
        from agents.sandbox import (
            SandboxAgent as SDKSandboxAgent,
        )
        from agents.sandbox import (
            SandboxRunConfig as SDKSandboxRunConfig,
        )
        from agents.sandbox.entries import (  # type: ignore
            Dir as SDKDir,
        )
        from agents.sandbox.entries import (
            File as SDKFile,
        )
        from agents.sandbox.entries import (
            LocalDir as SDKLocalDir,
        )
        from agents.sandbox.entries import (
            LocalFile as SDKLocalFile,
        )
        from agents.sandbox.sandboxes.unix_local import (  # type: ignore
            UnixLocalSandboxClient as SDKUnixLocalSandboxClient,
        )
    except ImportError as exc:  # pragma: no cover - depends on optional SDK version.
        _SANDBOX_IMPORT_ERROR = exc
        SDKManifest = None  # type: ignore
        SDKSandboxAgent = None  # type: ignore
        SDKSandboxRunConfig = None  # type: ignore
        SDKDir = None  # type: ignore
        SDKFile = None  # type: ignore
        SDKLocalDir = None  # type: ignore
        SDKLocalFile = None  # type: ignore
        SDKUnixLocalSandboxClient = None  # type: ignore
    else:
        _SANDBOX_IMPORT_ERROR = None


class ToolGuardrailViolation(RuntimeError):
    """Raised when local direct-call tool guardrails reject a tool input or output."""


@dataclass(frozen=True)
class LocalToolContext:
    """Minimal tool context used for direct-call guardrail execution."""

    tool_name: str
    tool_arguments: str
    tool_input: Any = None


@dataclass(frozen=True)
class LocalToolInputGuardrail:
    """Fallback for SDK tool input guardrails when the SDK is unavailable."""

    guardrail_function: Any
    name: str | None = None

    def get_name(self) -> str:
        return self.name or getattr(self.guardrail_function, "__name__", "tool_input_guardrail")


@dataclass(frozen=True)
class LocalToolOutputGuardrail:
    """Fallback for SDK tool output guardrails when the SDK is unavailable."""

    guardrail_function: Any
    name: str | None = None

    def get_name(self) -> str:
        return self.name or getattr(self.guardrail_function, "__name__", "tool_output_guardrail")


if _SDK_IMPORT_ERROR is not None:

    @dataclass(frozen=True)
    class ToolInputGuardrailData:  # type: ignore[no-redef]
        context: LocalToolContext
        agent: Any = None

    @dataclass(frozen=True)
    class ToolOutputGuardrailData:  # type: ignore[no-redef]
        context: LocalToolContext
        agent: Any = None
        output: Any = None

    @dataclass(frozen=True)
    class ToolGuardrailFunctionOutput:  # type: ignore[no-redef]
        output_info: Any
        behavior: dict[str, Any] = field(default_factory=lambda: {"type": "allow"})

        @classmethod
        def allow(cls, output_info: Any = None) -> ToolGuardrailFunctionOutput:
            return cls(output_info=output_info, behavior={"type": "allow"})

        @classmethod
        def reject_content(
            cls,
            message: str,
            output_info: Any = None,
        ) -> ToolGuardrailFunctionOutput:
            return cls(
                output_info=output_info,
                behavior={"type": "reject_content", "message": message},
            )

        @classmethod
        def raise_exception(cls, output_info: Any = None) -> ToolGuardrailFunctionOutput:
            return cls(output_info=output_info, behavior={"type": "raise_exception"})


def _tool_payload(func: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(func)
        bound = signature.bind_partial(*args, **kwargs)
        bound.apply_defaults()
        return dict(bound.arguments)
    except (TypeError, ValueError):
        return {"args": args, "kwargs": kwargs}


def _tool_context(tool_name: str, payload: Any) -> LocalToolContext:
    return LocalToolContext(
        tool_name=tool_name,
        tool_arguments=json.dumps(payload, default=str, ensure_ascii=True, sort_keys=True),
        tool_input=payload,
    )


def _call_guardrail_function(guardrail: Any, data: Any) -> Any:
    guardrail_function = getattr(guardrail, "guardrail_function", guardrail)
    result = guardrail_function(data)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _reject_message(result: Any) -> str:
    behavior = getattr(result, "behavior", {"type": "allow"})
    if behavior.get("type") == "reject_content":
        return str(behavior.get("message") or "Tool guardrail rejected this operation.")
    if behavior.get("type") == "raise_exception":
        return "Tool guardrail rejected this operation."
    return ""


def enforce_local_tool_input_guardrails(
    *,
    tool_name: str,
    payload: Any,
    guardrails: Sequence[Any],
) -> None:
    """Run direct-call input guardrails and raise if any rejects the operation."""

    data = ToolInputGuardrailData(context=_tool_context(tool_name, payload), agent=None)
    for guardrail in guardrails:
        result = _call_guardrail_function(guardrail, data)
        message = _reject_message(result)
        if message:
            raise ToolGuardrailViolation(message)


def enforce_local_tool_output_guardrails(
    *,
    tool_name: str,
    payload: Any,
    output: Any,
    guardrails: Sequence[Any],
) -> Any:
    """Run direct-call output guardrails and raise if any rejects the operation."""

    data = ToolOutputGuardrailData(
        context=_tool_context(tool_name, payload),
        agent=None,
        output=output,
    )
    for guardrail in guardrails:
        result = _call_guardrail_function(guardrail, data)
        message = _reject_message(result)
        if message:
            raise ToolGuardrailViolation(message)
    return output


def function_tool(func: Any = None, **kwargs: Any) -> Any:
    """Decorate a tool while preserving direct-call fixture guardrail behavior."""

    def decorate(wrapped: Any) -> Any:
        tool_name = kwargs.get("name_override") or getattr(
            wrapped, "__name__", wrapped.__class__.__name__
        )
        input_guardrails = list(kwargs.get("tool_input_guardrails") or [])
        output_guardrails = list(kwargs.get("tool_output_guardrails") or [])

        @wraps(wrapped)
        def guarded(*args: Any, **call_kwargs: Any) -> Any:
            payload = _tool_payload(wrapped, args, call_kwargs)
            enforce_local_tool_input_guardrails(
                tool_name=tool_name,
                payload=payload,
                guardrails=input_guardrails,
            )
            output = wrapped(*args, **call_kwargs)
            return enforce_local_tool_output_guardrails(
                tool_name=tool_name,
                payload=payload,
                output=output,
                guardrails=output_guardrails,
            )

        guarded.__signature__ = inspect.signature(wrapped)  # type: ignore[attr-defined]
        guarded.name = tool_name
        guarded.tool_input_guardrails = input_guardrails
        guarded.tool_output_guardrails = output_guardrails
        if _sdk_function_tool is not None:
            guarded.sdk_tool = _sdk_function_tool(guarded, **kwargs)
        return guarded

    return decorate(func) if func is not None else decorate


PROMPT_PACKAGE = "keystone_agents.prompts"
SKILL_PACKAGE = "keystone_agents.skills"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPO_AGENT_GUIDE = "AGENTS.md"
REPO_RUNTIME_POLICY_PROMPT = "repo_runtime_policy.md"
REPO_GUIDE_PROFILE_ENV = "KEYSTONE_AGENTS_GUIDE_PROFILE"
SHARED_PRE_RUN_PROMPTS = (
    "memory_policy.md",
    "agent-operating-architecture.md",
    "writing_style.md",
    "slack-posting-rules.md",
    "operator_context.md",
    "local_context.md",
)
DIRECT_SHARED_PRE_RUN_PROMPTS = (
    "memory_policy.md",
    "writing_style.md",
)
TOutput = TypeVar("TOutput")
PROMPT_METADATA_FIELDS = frozenset(
    {
        "prompt_name",
        "prompt_version",
        "prompt_purpose",
        "prompt_safety_notes",
        "prompt_eval_datasets",
    }
)
SKILL_METADATA_FIELDS = frozenset(
    {
        "skill_id",
        "skill_version",
        "skill_purpose",
        "applies_to",
        "eval_datasets",
        "validation_paths",
        "safety_notes",
    }
)


@dataclass(frozen=True)
class PromptMetadata:
    """Version metadata parsed from a markdown prompt file."""

    filename: str
    name: str
    version: str
    purpose: str
    safety_notes: str
    eval_datasets: tuple[str, ...] = ()

    @property
    def reference(self) -> str:
        return f"{self.name}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "name": self.name,
            "version": self.version,
            "reference": self.reference,
            "purpose": self.purpose,
            "safety_notes": self.safety_notes,
            "eval_datasets": list(self.eval_datasets),
        }


@dataclass(frozen=True)
class SkillMetadata:
    """Version metadata parsed from a repo-local Keystone skill bundle."""

    skill_id: str
    version: str
    purpose: str
    applies_to: tuple[str, ...] = ()
    eval_datasets: tuple[str, ...] = ()
    validation_paths: tuple[str, ...] = ()
    safety_notes: tuple[str, ...] = ()

    @property
    def filename(self) -> str:
        return f"{self.skill_id}/SKILL.md"

    @property
    def reference(self) -> str:
        return f"{self.skill_id}@{self.version}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "reference": self.reference,
            "purpose": self.purpose,
            "applies_to": list(self.applies_to),
            "eval_datasets": list(self.eval_datasets),
            "validation_paths": list(self.validation_paths),
            "safety_notes": list(self.safety_notes),
            "filename": self.filename,
        }


class AgentLike(Protocol):
    name: str
    handoff_description: str | None
    instructions: str
    tools: list[Any]
    handoffs: list[Any]
    output_type: type[Any] | None
    input_guardrails: list[Any]
    output_guardrails: list[Any]


@dataclass
class LocalAgent:
    """Inert stand-in used only when the OpenAI Agents SDK is unavailable."""

    keystone_runtime_catalog_enabled = True

    name: str
    instructions: str
    handoff_description: str | None = None
    tools: list[Any] = field(default_factory=list)
    handoffs: list[Any] = field(default_factory=list)
    model: str | None = None
    model_settings: Any | None = None
    output_type: type[Any] | None = None
    input_guardrails: list[Any] = field(default_factory=list)
    output_guardrails: list[Any] = field(default_factory=list)

    def as_tool(
        self,
        *,
        tool_name: str,
        tool_description: str,
        max_turns: int | None = None,
        **_: Any,
    ) -> LocalAgentTool:
        """Return an inert local stand-in for `Agent.as_tool()`."""

        return LocalAgentTool(
            name=tool_name,
            description=tool_description,
            agent=self,
            max_turns=max_turns,
        )


@dataclass
class LocalAgentTool:
    """Inert stand-in for SDK agent tools when the SDK is unavailable."""

    name: str
    description: str
    agent: Any
    max_turns: int | None = None
    params_json_schema: dict[str, Any] = field(default_factory=dict)


if SDKAgent is not None:
    class _CatalogSDKAgent(SDKAgent):
        """Keep public instructions intact while refreshing scoped metadata per turn."""

        keystone_runtime_catalog_enabled = True

        async def get_system_prompt(self, run_context: Any) -> str | None:
            instructions = await super().get_system_prompt(run_context)
            return append_runtime_capability_catalog(self, str(instructions or ""))
else:
    _CatalogSDKAgent = None  # type: ignore[assignment,misc]


Agent = SDKAgent or LocalAgent
SandboxAgent = SDKSandboxAgent
Manifest = SDKManifest
SandboxRunConfig = SDKSandboxRunConfig
Dir = SDKDir
File = SDKFile
LocalDir = SDKLocalDir
LocalFile = SDKLocalFile
UnixLocalSandboxClient = SDKUnixLocalSandboxClient
WebSearchTool = SDKWebSearchTool
FileSearchTool = SDKFileSearchTool
HostedMCPTool = SDKHostedMCPTool
ToolSearchTool = SDKToolSearchTool

GuardrailSpec = (
    Mapping[str, Sequence[Any]] | tuple[Sequence[Any], Sequence[Any]] | Sequence[Any] | None
)


def validate_sdk_available() -> bool:
    """Return true when the OpenAI Agents SDK is importable, otherwise raise clearly."""

    if _SDK_IMPORT_ERROR is not None:
        raise RuntimeError(
            "OpenAI Agents SDK is not installed. Install the `openai-agents` package "
            "before constructing or running SDK agents."
        ) from _SDK_IMPORT_ERROR
    return True


def build_sqlite_session(
    session_id: str,
    database_path: str | Path | None = None,
    *,
    session_history_limit: int | None = None,
) -> Any:
    """Build an Agents SDK SQLiteSession through the centralized SDK import boundary."""

    validate_sdk_available()
    if not session_id:
        raise ValueError("session_id is required for SDK SQLite sessions.")
    settings = (
        SessionSettings(limit=session_history_limit)
        if session_history_limit is not None
        else None
    )
    if database_path:
        path = Path(database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        session = SQLiteSession(session_id, str(path))
    else:
        session = SQLiteSession(session_id)
    if settings is None:
        return session
    return _ResponseItemSafeBoundedSession(session, settings)


class _ResponseItemSafeBoundedSession:
    """Bound local history without separating dependent Responses API items.

    The SDK's raw item-count limit can start a history suffix on a
    ``function_call_output`` item while its matching ``function_call`` is just
    outside the window. A widened suffix can then start on that
    ``function_call`` while omitting the preceding ``reasoning`` item required
    by the Responses API. This adapter widens the suffix only enough to retain
    both dependencies.
    """

    def __init__(self, session: Any, settings: Any) -> None:
        self._session = session
        self.session_settings = settings

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)

    async def get_items(self, limit: int | None = None) -> list[Any]:
        resolved_limit = limit if limit is not None else self.session_settings.limit
        scan_limit = (
            None
            if resolved_limit is None
            else max(resolved_limit * 4, resolved_limit + 32)
        )
        items = await self._session.get_items(limit=scan_limit)
        if resolved_limit is None or resolved_limit >= len(items):
            return items
        return _response_item_safe_history_suffix(items, resolved_limit)


def _response_item_safe_history_suffix(items: list[Any], limit: int) -> list[Any]:
    """Return a recent suffix with complete reasoning/call/output units."""

    if limit <= 0:
        return []
    start = max(0, len(items) - limit)
    call_positions = {
        str(item.get("call_id") or ""): index
        for index, item in enumerate(items)
        if isinstance(item, Mapping)
        and item.get("type") == "function_call"
        and item.get("call_id")
    }
    while True:
        suffix_call_ids = {
            str(item.get("call_id") or "")
            for item in items[start:]
            if isinstance(item, Mapping)
            and item.get("type") == "function_call"
            and item.get("call_id")
        }
        missing_positions = [
            call_positions[call_id]
            for item in items[start:]
            if isinstance(item, Mapping)
            and item.get("type") == "function_call_output"
            and (call_id := str(item.get("call_id") or ""))
            and call_id not in suffix_call_ids
            and call_id in call_positions
        ]
        if missing_positions:
            start = min(start, min(missing_positions))
            continue

        reasoning_positions = [
            reasoning_position
            for index, item in enumerate(items[start:], start=start)
            if isinstance(item, Mapping)
            and item.get("type") == "function_call"
            and (reasoning_position := _preceding_reasoning_position(items, index))
            is not None
            and reasoning_position < start
        ]
        if reasoning_positions:
            start = min(reasoning_positions)
            continue

        return [
            item
            for item in items[start:]
            if not (
                isinstance(item, Mapping)
                and item.get("type") == "function_call_output"
                and item.get("call_id")
                and str(item["call_id"]) not in suffix_call_ids
            )
        ]


def _preceding_reasoning_position(items: list[Any], call_position: int) -> int | None:
    """Return reasoning that belongs to a function-call output group, if present."""

    position = call_position - 1
    while position >= 0:
        item = items[position]
        if not isinstance(item, Mapping):
            return None
        item_type = str(item.get("type") or "")
        if item_type == "reasoning":
            return position
        if item_type != "function_call":
            return None
        position -= 1
    return None


def validate_sandbox_sdk_available() -> bool:
    """Return true when the Agents SDK sandbox classes are importable."""

    if _SANDBOX_IMPORT_ERROR is not None:
        raise RuntimeError(
            "OpenAI Agents SDK sandbox classes are unavailable. Install a version of "
            "`openai-agents` that includes `agents.sandbox` before constructing "
            "SandboxAgent workflows."
        ) from _SANDBOX_IMPORT_ERROR
    return True


def validate_web_search_sdk_available() -> bool:
    """Return true when the Agents SDK hosted web search tool is importable."""

    validate_sdk_available()
    if WebSearchTool is None:
        raise RuntimeError(
            "OpenAI Agents SDK hosted web search is unavailable. Install a version of "
            "`openai-agents` that includes `agents.WebSearchTool` before enabling "
            "sandbox web search."
        )
    return True


def _prompt_filename(name: str) -> str:
    if "/" in name or "\\" in name:
        raise ValueError("Prompt name must be a markdown filename, not a path.")
    if "." in name and not name.endswith(".md"):
        raise ValueError("Prompt name must resolve to a markdown file.")
    filename = name if name.endswith(".md") else f"{name}.md"
    if not filename.endswith(".md"):
        raise ValueError("Prompt name must resolve to a markdown file.")
    return filename


def _skill_id(name: str) -> str:
    if not name or "/" in name or "\\" in name or name in {".", ".."}:
        raise ValueError("Skill name must be a single skill directory name.")
    if name != name.strip():
        raise ValueError("Skill name must not contain leading or trailing whitespace.")
    return name


def load_prompt(name: str) -> str:
    """Load a markdown prompt from the `keystone_agents.prompts` package."""

    filename = _prompt_filename(name)
    return resources.files(PROMPT_PACKAGE).joinpath(filename).read_text(encoding="utf-8")


def load_skill(name: str) -> str:
    """Load a repo-local Keystone skill from `keystone_agents.skills/<skill>/SKILL.md`."""

    skill_id = _skill_id(name)
    return resources.files(SKILL_PACKAGE).joinpath(skill_id, "SKILL.md").read_text(encoding="utf-8")


def load_repo_agent_guide() -> str:
    """Load the full repository AGENTS.md guide."""

    configured_path = os.getenv("KEYSTONE_AGENTS_GUIDE_PATH")
    guide_path = Path(configured_path) if configured_path else PROJECT_ROOT / REPO_AGENT_GUIDE
    if not guide_path.is_file():
        return ""
    return guide_path.read_text(encoding="utf-8").strip()


def repo_instruction_profile_id() -> str:
    """Return the configured repo instruction profile id."""

    configured = os.getenv(REPO_GUIDE_PROFILE_ENV, "").strip().lower()
    if configured in {"full", "agents", "agents.md"}:
        return "full-agents-md"
    return "compact-runtime-policy"


def load_repo_runtime_policy() -> tuple[str, str]:
    """Load the repo-level instruction profile used in composed agent prompts."""

    if repo_instruction_profile_id() == "full-agents-md":
        return REPO_AGENT_GUIDE, load_repo_agent_guide()
    return REPO_RUNTIME_POLICY_PROMPT, load_prompt(REPO_RUNTIME_POLICY_PROMPT).strip()


def _parse_metadata_comment(text: str) -> dict[str, str]:
    stripped = text.lstrip()
    if not stripped.startswith("<!--"):
        return {}
    end = stripped.find("-->")
    if end == -1:
        return {}
    raw_metadata = stripped[4:end]
    metadata: dict[str, str] = {}
    for raw_line in raw_metadata.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if key in PROMPT_METADATA_FIELDS:
            metadata[key] = value.strip()
    return metadata


def _parse_front_matter(text: str) -> dict[str, str | tuple[str, ...]]:
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return {}
    lines = stripped.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end_index: int | None = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            end_index = index
            break
    if end_index is None:
        return {}

    metadata: dict[str, str | tuple[str, ...]] = {}
    current_key = ""
    current_items: list[str] = []
    for raw_line in lines[1:end_index]:
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            item = line.strip()
            if current_key and item.startswith("- "):
                current_items.append(item[2:].strip())
            continue
        if current_key and current_items:
            metadata[current_key] = tuple(current_items)
            current_items = []
        if ":" not in line:
            current_key = ""
            continue
        key, value = line.split(":", 1)
        current_key = key.strip()
        if current_key not in SKILL_METADATA_FIELDS:
            current_key = ""
            continue
        value = value.strip()
        if value:
            metadata[current_key] = value
            current_key = ""
        else:
            metadata[current_key] = ()
    if current_key and current_items:
        metadata[current_key] = tuple(current_items)
    return metadata


def load_prompt_metadata(name: str) -> PromptMetadata:
    """Load prompt version metadata from the top markdown comment."""

    filename = _prompt_filename(name)
    text = load_prompt(filename)
    metadata = _parse_metadata_comment(text)
    required_fields = {
        "prompt_name",
        "prompt_version",
        "prompt_purpose",
        "prompt_safety_notes",
    }
    missing = sorted(required_fields - set(metadata))
    if missing:
        raise ValueError(f"{filename} missing prompt metadata fields: {', '.join(missing)}")
    eval_datasets = tuple(
        item.strip() for item in metadata.get("prompt_eval_datasets", "").split(",") if item.strip()
    )
    return PromptMetadata(
        filename=filename,
        name=metadata["prompt_name"],
        version=metadata["prompt_version"],
        purpose=metadata["prompt_purpose"],
        safety_notes=metadata["prompt_safety_notes"],
        eval_datasets=eval_datasets,
    )


def _metadata_tuple(metadata: Mapping[str, str | tuple[str, ...]], key: str) -> tuple[str, ...]:
    value = metadata.get(key, ())
    if isinstance(value, tuple):
        return tuple(item for item in value if item)
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    return ()


def load_skill_metadata(name: str) -> SkillMetadata:
    """Load skill version metadata from a skill bundle's front matter."""

    skill_id = _skill_id(name)
    text = load_skill(skill_id)
    metadata = _parse_front_matter(text)
    required_fields = {"skill_id", "skill_version", "skill_purpose", "safety_notes"}
    missing = sorted(required_fields - set(metadata))
    if missing:
        raise ValueError(f"{skill_id}/SKILL.md missing skill metadata fields: {', '.join(missing)}")
    declared_id = str(metadata["skill_id"])
    if declared_id != skill_id:
        raise ValueError(
            f"{skill_id}/SKILL.md declares skill_id {declared_id!r}; expected {skill_id!r}"
        )
    return SkillMetadata(
        skill_id=skill_id,
        version=str(metadata["skill_version"]),
        purpose=str(metadata["skill_purpose"]),
        applies_to=_metadata_tuple(metadata, "applies_to"),
        eval_datasets=_metadata_tuple(metadata, "eval_datasets"),
        validation_paths=_metadata_tuple(metadata, "validation_paths"),
        safety_notes=_metadata_tuple(metadata, "safety_notes"),
    )


def list_prompt_metadata(prompt_files: Sequence[str] | None = None) -> list[PromptMetadata]:
    """Return metadata for all prompt files or the selected prompt filenames."""

    if prompt_files is None:
        files = sorted(
            path.name
            for path in resources.files(PROMPT_PACKAGE).iterdir()
            if path.name.endswith(".md")
        )
    else:
        files = [_prompt_filename(prompt_file) for prompt_file in prompt_files]
    return [load_prompt_metadata(filename) for filename in files]


def list_skill_metadata(skill_names: Sequence[str] | None = None) -> list[SkillMetadata]:
    """Return metadata for all skill bundles or the selected skill names."""

    if skill_names is None:
        files = sorted(
            path.name
            for path in resources.files(SKILL_PACKAGE).iterdir()
            if path.is_dir() and path.joinpath("SKILL.md").is_file()
        )
    else:
        files = [_skill_id(skill_name) for skill_name in skill_names]
    return [load_skill_metadata(skill_id) for skill_id in files]


def prompt_metadata_for_files(prompt_files: Sequence[str]) -> list[dict[str, Any]]:
    """Return JSON-safe metadata for prompt files."""

    return [metadata.to_dict() for metadata in list_prompt_metadata(prompt_files)]


def skill_metadata_for_files(skill_names: Sequence[str]) -> list[dict[str, Any]]:
    """Return JSON-safe metadata for skill bundles."""

    return [metadata.to_dict() for metadata in list_skill_metadata(skill_names)]


def prompt_version_references(prompt_files: Sequence[str]) -> list[str]:
    """Return compact prompt version references such as `gmail_triage@2026-04-21.1`."""

    return [metadata.reference for metadata in list_prompt_metadata(prompt_files)]


def skill_version_references(skill_names: Sequence[str]) -> list[str]:
    """Return compact skill version references such as `source_attribution@2026-05-30.1`."""

    return [metadata.reference for metadata in list_skill_metadata(skill_names)]


def compose_instructions(
    *prompt_files: str,
    skill_files: Sequence[str] = (),
    shared_prompt_files: Sequence[str] | None = None,
) -> str:
    """Concatenate project, memory, and prompt context with clear boundaries."""

    sections = []
    repo_profile_name, repo_profile = load_repo_runtime_policy()
    if repo_profile:
        sections.append(f"<!-- {repo_profile_name} -->\n{repo_profile}")
    requested_files = {_prompt_filename(prompt_file) for prompt_file in prompt_files}
    shared_prompts = (
        SHARED_PRE_RUN_PROMPTS if shared_prompt_files is None else shared_prompt_files
    )
    for shared_prompt in shared_prompts:
        filename = _prompt_filename(shared_prompt)
        if filename not in requested_files:
            sections.append(f"<!-- {filename} -->\n{load_prompt(filename).strip()}")
    leading_prompt_files = prompt_files[:-1] if skill_files and prompt_files else prompt_files
    trailing_prompt_files = prompt_files[-1:] if skill_files and prompt_files else ()
    for prompt_file in leading_prompt_files:
        filename = prompt_file if prompt_file.endswith(".md") else f"{prompt_file}.md"
        sections.append(f"<!-- {filename} -->\n{load_prompt(filename).strip()}")
    for skill_file in skill_files:
        skill_id = _skill_id(skill_file)
        sections.append(f"<!-- {skill_id}/SKILL.md -->\n{load_skill(skill_id).strip()}")
    for prompt_file in trailing_prompt_files:
        filename = prompt_file if prompt_file.endswith(".md") else f"{prompt_file}.md"
        sections.append(f"<!-- {filename} -->\n{load_prompt(filename).strip()}")
    return "\n\n".join(sections)


def compose_direct_instructions(
    *prompt_files: str,
    skill_files: Sequence[str] = (),
) -> str:
    """Compose a compact direct-call prompt while preserving shared policy."""

    return compose_instructions(
        *prompt_files,
        skill_files=skill_files,
        shared_prompt_files=DIRECT_SHARED_PRE_RUN_PROMPTS,
    )


def _split_guardrails(guardrails: GuardrailSpec) -> tuple[list[Any], list[Any]]:
    if guardrails is None:
        return [], []
    if isinstance(guardrails, Mapping):
        return list(guardrails.get("input", [])), list(guardrails.get("output", []))
    if isinstance(guardrails, tuple) and len(guardrails) == 2:
        return list(guardrails[0]), list(guardrails[1])
    return list(guardrails), []


def _sdk_tool_for(tool: Any) -> Any:
    """Return an SDK-compatible tool while preserving local direct-call functions."""

    explicit_sdk_tool = getattr(tool, "sdk_tool", None)
    if explicit_sdk_tool is not None:
        return explicit_sdk_tool
    if _sdk_function_tool is not None and callable(tool):
        return _sdk_function_tool(tool, strict_mode=False)
    return tool


def build_sdk_agent(
    name: str,
    instructions: str,
    output_type: type[Any] | None,
    tools: Sequence[Any] | None = None,
    guardrails: GuardrailSpec = None,
    handoffs: Sequence[Any] | None = None,
    *,
    model: str | None = None,
    model_settings: Any | None = None,
    handoff_description: str | None = None,
    policy_agent_name: str | None = None,
    enforce_tool_policy: bool = True,
) -> AgentLike:
    """Construct an OpenAI Agents SDK `Agent` without making a model call."""

    tools_list = list(tools or [])
    if policy_agent_name:
        from keystone_agents.agent_tool_policy import validate_agent_tool_policy

        validate_agent_tool_policy(
            policy_agent_name,
            tools_list,
            strict=enforce_tool_policy,
        )
    sdk_tools_list = [_sdk_tool_for(tool) for tool in tools_list]
    handoffs_list = list(handoffs or [])
    input_guardrails, output_guardrails = _split_guardrails(guardrails)
    selected_model = model or get_runtime_agent_model_config(name).model

    if _SDK_IMPORT_ERROR is not None:
        return LocalAgent(
            name=name,
            instructions=instructions,
            handoff_description=handoff_description,
            model=selected_model,
            model_settings=_cache_friendly_model_settings(model_settings, model=selected_model),
            tools=tools_list,
            handoffs=handoffs_list,
            output_type=output_type,
            input_guardrails=input_guardrails,
            output_guardrails=output_guardrails,
        )

    from keystone_agents.agent_registry import AGENT_REGISTRY

    agent_type = _CatalogSDKAgent if name in AGENT_REGISTRY else Agent
    return agent_type(
        name=name,
        handoff_description=handoff_description,
        instructions=instructions,
        model=selected_model,
        model_settings=_cache_friendly_model_settings(model_settings, model=selected_model),
        tools=sdk_tools_list,
        handoffs=handoffs_list,
        output_type=output_type,
        input_guardrails=input_guardrails,
        output_guardrails=output_guardrails,
    )


def create_agent(
    *,
    name: str,
    instructions: str,
    model: str | None = None,
    tools: Sequence[Any] | None = None,
    handoffs: Sequence[Any] | None = None,
    output_type: type[Any] | None = None,
    guardrails: GuardrailSpec = None,
    handoff_description: str | None = None,
    model_settings: Any | None = None,
    policy_agent_name: str | None = None,
    enforce_tool_policy: bool = True,
) -> AgentLike:
    """Backward-compatible alias for older scaffold builders."""

    return build_sdk_agent(
        name=name,
        instructions=instructions,
        output_type=output_type,
        tools=tools,
        guardrails=guardrails,
        handoffs=handoffs,
        model=model,
        model_settings=model_settings,
        handoff_description=handoff_description,
        policy_agent_name=policy_agent_name,
        enforce_tool_policy=enforce_tool_policy,
    )


def build_model_settings(
    *,
    reasoning_effort: str | None = None,
    verbosity: str | None = None,
    max_tokens: int | None = None,
    tool_choice: str | None = None,
) -> Any | None:
    """Build Agents SDK model settings when the installed SDK supports them."""

    if _SDK_IMPORT_ERROR is not None:
        return _cache_friendly_model_settings(
            {
                "reasoning": {"effort": reasoning_effort} if reasoning_effort else None,
                "verbosity": verbosity,
                "max_tokens": max_tokens,
                "tool_choice": tool_choice,
            }
        )
    reasoning = Reasoning(effort=reasoning_effort) if reasoning_effort else None
    return _cache_friendly_model_settings(
        ModelSettings(
            reasoning=reasoning,
            verbosity=verbosity,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
        )
    )


def _cache_friendly_model_settings(
    model_settings: Any | None = None, *, model: str | None = None,
) -> Any:
    """Apply repo-wide cache/cost telemetry defaults to SDK model settings."""

    include_usage = _sdk_include_usage_enabled()
    profile = active_sdk_data_handling_profile()
    retention = (
        profile.prompt_cache_retention if profile else _sdk_prompt_cache_retention()
    )
    if _SDK_IMPORT_ERROR is not None:
        settings = dict(model_settings or {})
        settings.setdefault("include_usage", include_usage)
        if profile is not None:
            settings["store"] = profile.store
        if retention is not None:
            if profile is not None:
                settings["prompt_cache_retention"] = retention
            else:
                settings.setdefault("prompt_cache_retention", retention)
        return _gpt56_cache_settings(settings) if uses_gpt56_cache_controls(model) else settings
    settings = model_settings or ModelSettings()
    updates: dict[str, Any] = {}
    if getattr(settings, "include_usage", None) is None:
        updates["include_usage"] = include_usage
    if profile is not None:
        updates["store"] = profile.store
    if profile is not None and retention is not None:
        updates["prompt_cache_retention"] = retention
    elif retention is not None and getattr(settings, "prompt_cache_retention", None) is None:
        updates["prompt_cache_retention"] = retention
    settings = dataclass_replace(settings, **updates) if updates else settings
    return _gpt56_cache_settings(settings) if uses_gpt56_cache_controls(model) else settings


def _gpt56_cache_settings(settings: Any) -> Any:
    """Use reviewed GPT-5.6 controls without changing older models' cache policy.

    https://developers.openai.com/api/docs/guides/prompt-caching
    No explicit TTL is needed: the model defaults to its supported 30m lifetime.
    """
    updates: dict[str, Any] = {"prompt_cache_retention": None, "preserve_raw_usage": True}
    profile = active_sdk_data_handling_profile()
    if profile is not None and profile.prompt_cache_retention == "in_memory":
        # KBA does not insert explicit breakpoints; this prevents implicit writes
        # for the existing non-persistent private-context profile.
        updates["prompt_cache_options"] = {"mode": "explicit"}
    return (
        {**settings, **updates} if isinstance(settings, Mapping)
        else dataclass_replace(settings, **updates)
    )


def _sdk_include_usage_enabled() -> bool:
    raw = os.getenv(SDK_INCLUDE_USAGE_ENV)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSE_ENV_VALUES


def _sdk_prompt_cache_retention() -> str | None:
    raw = os.getenv(SDK_PROMPT_CACHE_RETENTION_ENV)
    value = DEFAULT_PROMPT_CACHE_RETENTION if raw is None else raw.strip().lower()
    if value in _FALSE_ENV_VALUES:
        return None
    if value in {"in_memory", "24h"}:
        return value
    return DEFAULT_PROMPT_CACHE_RETENTION


def _live_model_timeout_seconds() -> float:
    raw = os.getenv(LIVE_MODEL_TIMEOUT_SECONDS_ENV)
    if raw is None:
        return DEFAULT_LIVE_MODEL_TIMEOUT_SECONDS
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIVE_MODEL_TIMEOUT_SECONDS
    return max(5.0, value)


def live_model_timeout_seconds() -> float:
    """Return the configured per-request model timeout for deadline headroom."""

    return _live_model_timeout_seconds()


def _live_model_max_retries() -> int:
    raw = os.getenv(LIVE_MODEL_MAX_RETRIES_ENV)
    if raw is None:
        return DEFAULT_LIVE_MODEL_MAX_RETRIES
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_LIVE_MODEL_MAX_RETRIES
    return max(0, value)


def _sdk_export_trace_metadata(metadata: Mapping[str, Any]) -> dict[str, str]:
    """Convert safe Keystone trace scalars to the SDK exporter's string contract."""

    exported: dict[str, str] = {}
    for key, value in metadata.items():
        if isinstance(value, bool):
            exported[key] = "true" if value else "false"
        else:
            exported[key] = str(value)
    return exported


def build_live_run_config(
    config: ModelConfig | None = None,
    *,
    workflow_name: str | None = None,
    group_id: str | None = None,
    trace_metadata: TraceMetadata | None = None,
    tracing_disabled: bool | None = None,
    trace_include_sensitive_data: bool | None = None,
    trace_config: TraceConfig | None = None,
    sandbox: Any | None = None,
) -> Any:
    """Build a run config for live execution after validating credentials.

    Tests and dry-run construction should not call this. It exists so live paths
    fail with the Keystone error before the SDK attempts a network request.
    """

    model_config = config or get_model_config()
    run_trace_config = (trace_config or get_trace_config()).with_overrides(
        workflow_name=workflow_name,
        group_id=group_id,
        trace_metadata=trace_metadata,
        tracing_disabled=tracing_disabled,
        trace_include_sensitive_data=trace_include_sensitive_data,
    )
    profile = active_sdk_data_handling_profile()
    if profile is not None:
        run_trace_config = run_trace_config.with_overrides(
            tracing_disabled=profile.tracing_disabled,
            trace_include_sensitive_data=profile.trace_include_sensitive_data,
        )
    model_config.require_live_execution_ready()
    validate_sdk_available()
    from keystone_agents.trace_processor import register_configured_trace_processor

    register_configured_trace_processor()
    provider_kwargs = model_config.openai_provider_kwargs()
    canary_active = bool(os.environ.get("KEYSTONE_CANARY_ACCEPTANCE_PROFILE"))
    if canary_active:
        from keystone_agents.canary_acceptance import validate_live_config

        validate_live_config(model_config.provider, model_config.model, provider_kwargs)
        run_trace_config = run_trace_config.with_overrides(
            tracing_disabled=True, trace_include_sensitive_data=False,
        )
    if (
        not run_trace_config.tracing_disabled
        and model_config.provider == "openai"
        and provider_kwargs.get("api_key")
    ):
        set_tracing_export_api_key(str(provider_kwargs["api_key"]))
    openai_client = AsyncOpenAI(
        api_key=provider_kwargs.get("api_key"),
        base_url=provider_kwargs.get("base_url") or None,
        timeout=_live_model_timeout_seconds(),
        max_retries=0 if canary_active else _live_model_max_retries(),
    )
    _observe_client_terminal_responses(openai_client)
    provider = OpenAIProvider(
        openai_client=openai_client,
        use_responses=True if canary_active else provider_kwargs.get("use_responses"),
        **({"use_responses_websocket": False} if canary_active else {}),
    )
    return RunConfig(
        model=model_config.model,
        model_provider=provider,
        workflow_name=run_trace_config.workflow_name,
        group_id=run_trace_config.group_id,
        trace_metadata=_sdk_export_trace_metadata(run_trace_config.trace_metadata) or None,
        tracing_disabled=run_trace_config.tracing_disabled,
        trace_include_sensitive_data=run_trace_config.trace_include_sensitive_data,
        sandbox=sandbox,
    )


def _observe_client_terminal_responses(client: Any) -> None:
    """Observe only terminal failures on this KBA-owned nonstreaming client."""
    responses = getattr(client, "responses", None)
    create = getattr(responses, "create", None)
    if not callable(create) or getattr(create, "_keystone_terminal_observer", False):
        return

    @wraps(create)
    async def create_with_terminal_observation(*args: Any, **kwargs: Any) -> Any:
        observer = _RESPONSE_TERMINAL_OBSERVER.get()
        dispatch_receipt = None
        if os.environ.get("KEYSTONE_CANARY_ACCEPTANCE_PROFILE"):
            from keystone_agents.canary_acceptance import (
                guarded_response_kwargs,
                observe_canary_response_reasoning,
            )

            kwargs = guarded_response_kwargs(
                client, args, kwargs, observer=observer,
            )
            dispatch_receipt = observer._keystone_canary_dispatches[-1]
        response = await create(*args, **kwargs)
        if dispatch_receipt is not None:
            observe_canary_response_reasoning(
                response, observer=observer, dispatch_receipt=dispatch_receipt,
            )
        if observer is not None and kwargs.get("stream") is not True:
            observation = ResponseTerminalObservation.from_response(
                response, max_output_tokens=kwargs.get("max_output_tokens"),
            )
            if observation is not None:
                observer.observe_terminal_response(response, observation)
        return response

    create_with_terminal_observation._keystone_terminal_observer = True
    responses.create = create_with_terminal_observation
    if os.environ.get("KEYSTONE_CANARY_ACCEPTANCE_PROFILE"):
        async def blocked_chat_completion(*args: Any, **kwargs: Any) -> Any:
            from keystone_agents.canary_acceptance import CanaryPolicyError

            raise CanaryPolicyError("canary_chat_completions_not_allowed")

        client.chat.completions.create = blocked_chat_completion


def _live_config_for_agent(agent: AgentLike, config: ModelConfig | None) -> ModelConfig | None:
    if config is not None:
        return config
    return get_runtime_agent_model_config(
        getattr(agent, "name", None),
        model_override=getattr(agent, "model", None),
    )


def build_local_run_config(
    model_provider: Any,
    *,
    model: str | None = None,
    tracing_disabled: bool = True,
    workflow_name: str = "Keystone local SDK test run",
    group_id: str | None = None,
    trace_metadata: TraceMetadata | None = None,
    trace_include_sensitive_data: bool = False,
) -> Any:
    """Build a RunConfig for local fake-model execution without credentials."""

    validate_sdk_available()
    run_trace_config = TraceConfig(
        workflow_name=workflow_name,
        group_id=group_id,
        trace_metadata=trace_metadata or {},
        tracing_disabled=tracing_disabled,
        trace_include_sensitive_data=trace_include_sensitive_data,
    )
    return RunConfig(
        model=model,
        model_provider=model_provider,
        workflow_name=run_trace_config.workflow_name,
        group_id=run_trace_config.group_id,
        trace_metadata=dict(run_trace_config.trace_metadata) or None,
        tracing_disabled=run_trace_config.tracing_disabled,
        trace_include_sensitive_data=run_trace_config.trace_include_sensitive_data,
    )


_RequestBudgetHooksBase = (
    RunHooksBase[Any, Any] if _SDK_IMPORT_ERROR is None else object
)


class _ExecutionBoundaryRunHooks(_RequestBudgetHooksBase):
    """Enforce request and time budgets at SDK model/tool boundaries."""

    def __init__(
        self,
        request_ledger: Any | None,
        deadline_ledger: Any | None,
        *,
        capture_usage: bool = False,
    ) -> None:
        self._request_ledger = request_ledger
        self._deadline_ledger = deadline_ledger
        # Per Runner invocation, never context-wrapper totals or shared context state:
        # those can include another invocation's usage when an agent calls a child.
        self._capture_usage = capture_usage
        self._requests_started = 0
        self._usage_observations: list[dict[str, int | None]] = []
        self._terminal_observations: list[dict[str, Any]] = []

    def numeric_usage(self) -> dict[str, Any]:
        return {
            "requests_started": self._requests_started,
            "responses": [dict(item) for item in self._usage_observations],
        }

    def terminal_diagnostics(self) -> dict[str, Any]:
        return {
            "schema": "keystone.response_terminal.v1",
            "observations": [dict(item) for item in self._terminal_observations],
        }

    def observe_terminal_response(
        self, response: Any, observation: ResponseTerminalObservation,
    ) -> None:
        if not self._capture_usage or len(self._usage_observations) >= self._requests_started:
            return
        # No ModelResponse/on_llm_end exists for this response: the installed SDK
        # rejects it before conversion. Never inspect its output or error message.
        self._terminal_observations.append(observation.snapshot())
        usage = getattr(response, "usage", None)
        raw_usage = usage if isinstance(usage, Mapping) else (
            usage.model_dump() if callable(getattr(usage, "model_dump", None)) else None
        )
        self._usage_observations.append({
            "request_ordinal": len(self._usage_observations) + 1,
            **numeric_sdk_request_usage(usage, raw_usage=raw_usage),
        })

    def _observe_usage(self, response: Any) -> None:
        if not self._capture_usage or len(self._usage_observations) >= self._requests_started:
            # Resuming an SDK interruption can emit an end hook for an old response.
            return
        self._usage_observations.append({
            "request_ordinal": len(self._usage_observations) + 1,
            **numeric_sdk_request_usage(
                getattr(response, "usage", None), raw_usage=getattr(response, "raw_usage", None),
            ),
        })

    @staticmethod
    def _agent_name(agent: Any) -> str:
        return str(getattr(agent, "name", "") or "unknown_agent")

    @staticmethod
    def _tool_name(tool: Any) -> str:
        return str(getattr(tool, "name", "") or "unknown_tool")

    async def on_llm_start(
        self,
        _context: Any,
        agent: Any,
        _system_prompt: str | None,
        _input_items: list[Any],
    ) -> None:
        agent_name = self._agent_name(agent)
        if os.environ.get("KEYSTONE_CANARY_ACCEPTANCE_PROFILE"):
            from keystone_agents.canary_acceptance import admit_agent

            admit_agent(agent_name, observer=self)
        stage = f"{agent_name}:llm_start"
        from keystone_agents.runtime.durable_execution import current_execution

        if self._deadline_ledger is not None:
            self._deadline_ledger.admit(stage=stage, boundary="llm")
        if self._request_ledger is not None:
            try:
                self._request_ledger.consume(stage=stage)
            except Exception:
                if self._deadline_ledger is not None:
                    self._deadline_ledger.checkpoint(
                        stage=stage,
                        boundary="llm",
                        phase="cancelled_before_dispatch",
                    )
                raise
        execution = current_execution()
        if execution is not None:
            execution.store.reserve_model(execution.execution_id, stage)
        if self._capture_usage:
            self._requests_started += 1

    async def on_llm_end(
        self,
        _context: Any,
        agent: Any,
        _response: Any,
    ) -> None:
        self._observe_usage(_response)
        if self._deadline_ledger is not None:
            self._deadline_ledger.checkpoint(
                stage=f"{self._agent_name(agent)}:llm_end",
                boundary="llm",
            )

    async def on_tool_start(
        self,
        _context: Any,
        agent: Any,
        tool: Any,
    ) -> None:
        if self._deadline_ledger is not None:
            self._deadline_ledger.admit(
                stage=f"{self._agent_name(agent)}:tool:{self._tool_name(tool)}",
                boundary="tool",
            )

    async def on_tool_end(
        self,
        _context: Any,
        agent: Any,
        tool: Any,
        _result: object,
    ) -> None:
        if self._deadline_ledger is not None:
            self._deadline_ledger.checkpoint(
                stage=f"{self._agent_name(agent)}:tool:{self._tool_name(tool)}",
                boundary="tool",
            )


def numeric_sdk_request_usage(usage: Any, *, raw_usage: Any = None) -> dict[str, int | None]:
    """Keep numeric per-response usage, never equating omitted cache writes to zero."""
    def value(source: Any, field: str) -> Any:
        return source.get(field) if isinstance(source, Mapping) else getattr(source, field, None)

    def number(source: Any, field: str) -> int | None:
        candidate = value(source, field)
        return candidate if type(candidate) is int and candidate >= 0 else None

    original = isinstance(raw_usage, Mapping)
    source = raw_usage if original else usage
    inputs = value(source, "input_tokens_details") or value(source, "prompt_tokens_details")
    outputs = value(source, "output_tokens_details") or value(source, "completion_tokens_details")
    cache_writes = number(inputs, "cache_write_tokens")
    if cache_writes is None:
        cache_writes = number(source, "cache_write_input_tokens")
    if not original and cache_writes == 0:
        # Agents SDK fills missing cache-write details with zero during normalization.
        cache_writes = None
    cached = number(inputs, "cached_tokens")
    if cached is None:
        cached = number(source, "cached_input_tokens")
    return {
        "requests": 1,
        "input_tokens": number(source, "input_tokens"),
        "output_tokens": number(source, "output_tokens"),
        "total_tokens": number(source, "total_tokens"),
        "cached_input_tokens": cached,
        "cache_write_input_tokens": cache_writes,
        "reasoning_output_tokens": number(outputs, "reasoning_tokens"),
    }


def execution_boundary_hooks(*, capture_usage: bool = False) -> Any | None:
    """Return SDK-valid composite budget hooks when either ledger is active."""

    request_ledger = current_model_request_budget()
    deadline_ledger = current_execution_deadline()
    from keystone_agents.runtime.durable_execution import current_execution

    if (
        not capture_usage
        and request_ledger is None
        and deadline_ledger is None
        and current_execution() is None
    ):
        return None
    return _ExecutionBoundaryRunHooks(request_ledger, deadline_ledger, capture_usage=capture_usage)


def model_request_budget_hooks() -> Any | None:
    """Compatibility entrypoint for the composite SDK boundary hooks."""

    return execution_boundary_hooks()


def _run_sync_with_optional_session(
    agent: AgentLike,
    prompt: Any,
    *,
    run_config: Any,
    session: Any | None = None,
    max_turns: int | None = None,
) -> Any:
    selected_model = getattr(run_config, "model", None) or getattr(agent, "model", None)
    if uses_gpt56_cache_controls(selected_model):
        agent = agent.clone(model_settings=_cache_friendly_model_settings(
            agent.model_settings, model=selected_model,
        ))
        if getattr(run_config, "model_settings", None) is not None:
            run_config = dataclass_replace(
                run_config,
                model_settings=_cache_friendly_model_settings(
                    run_config.model_settings, model=selected_model,
                ),
            )
    kwargs: dict[str, Any] = {"run_config": run_config}
    if session is not None:
        kwargs["session"] = session
    if max_turns is not None:
        kwargs["max_turns"] = max_turns
    boundary_hooks = execution_boundary_hooks(capture_usage=True)
    if boundary_hooks is not None:
        kwargs["hooks"] = boundary_hooks
    observer_token = _RESPONSE_TERMINAL_OBSERVER.set(boundary_hooks)
    terminal_error: Exception | None = None
    try:
        result = Runner.run_sync(agent, prompt, **kwargs)
    except Exception as exc:
        if boundary_hooks.terminal_diagnostics()["observations"]:
            # Terminal transport failures precede the SDK's output privacy
            # redactor. Preserve its public exception type without error prose,
            # RunErrorDetails, response objects, or the original exception chain.
            from agents.exceptions import ModelBehaviorError

            terminal_error = ModelBehaviorError(
                "Responses API returned a terminal failure; inspect the safe terminal diagnostics."
            )
            terminal_error.keystone_response_terminal = boundary_hooks.terminal_diagnostics()
            _attach_numeric_usage(terminal_error, boundary_hooks.numeric_usage())
        else:
            _attach_numeric_usage(exc, boundary_hooks.numeric_usage())
            raise
    finally:
        _RESPONSE_TERMINAL_OBSERVER.reset(observer_token)
    if terminal_error is not None:
        # Raise outside the original except block so no provider error is chained.
        raise terminal_error from None
    _attach_numeric_usage(result, boundary_hooks.numeric_usage())
    return result


def _attach_numeric_usage(target: Any, usage: Mapping[str, Any]) -> None:
    try:
        target.keystone_sdk_numeric_usage = dict(usage)
    except (AttributeError, TypeError):
        pass


def sdk_numeric_usage_observations(value: Any) -> dict[str, Any]:
    """Read the content-free numeric snapshot owned by one Runner invocation."""

    snapshot = getattr(value, "keystone_sdk_numeric_usage", None)
    return dict(snapshot) if isinstance(snapshot, Mapping) else {}


def run_sdk_sync(
    agent: AgentLike,
    prompt: Any,
    config: ModelConfig | None = None,
    *,
    session: Any | None = None,
    workflow_name: str | None = None,
    group_id: str | None = None,
    trace_metadata: TraceMetadata | None = None,
    tracing_disabled: bool | None = None,
    trace_include_sensitive_data: bool | None = None,
    trace_config: TraceConfig | None = None,
    max_turns: int | None = None,
) -> Any:
    """Run an SDK agent only after an explicit live-readiness check."""

    resolved_config = _live_config_for_agent(agent, config)
    run_config = build_live_run_config(
        resolved_config,
        workflow_name=workflow_name,
        group_id=group_id,
        trace_metadata=trace_metadata,
        tracing_disabled=tracing_disabled,
        trace_include_sensitive_data=trace_include_sensitive_data,
        trace_config=trace_config,
    )
    try:
        return _run_sync_with_optional_session(
            agent,
            prompt,
            run_config=run_config,
            session=session,
            max_turns=max_turns,
        )
    finally:
        _close_run_config_openai_client(run_config)


def _close_run_config_openai_client(run_config: Any) -> None:
    """Best-effort close for AsyncOpenAI clients embedded in live SDK configs."""

    provider = getattr(run_config, "model_provider", None)
    client = getattr(provider, "_client", None)
    close = getattr(client, "close", None)
    if client is None or not callable(close):
        return
    is_closed = getattr(client, "is_closed", None)
    if callable(is_closed) and is_closed():
        return
    result = close()
    if not inspect.isawaitable(result):
        return
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop is not None and running_loop.is_running():
        running_loop.create_task(result)
        return
    asyncio.run(result)


def run_sdk_sync_with_config(
    agent: AgentLike,
    prompt: Any,
    run_config: Any,
    *,
    session: Any | None = None,
    max_turns: int | None = None,
) -> Any:
    """Run an SDK agent with an explicit local/fake run config."""

    validate_sdk_available()
    return _run_sync_with_optional_session(
        agent,
        prompt,
        run_config=run_config,
        session=session,
        max_turns=max_turns,
    )


def _coerce_typed_output(output: Any, output_type: type[TOutput]) -> TOutput:
    if isinstance(output, output_type):
        return output
    if isinstance(output, str) and issubclass(output_type, BaseModel):
        return output_type.model_validate_json(output)
    if isinstance(output, Mapping) and issubclass(output_type, BaseModel):
        return output_type.model_validate(dict(output))
    raise TypeError(f"SDK agent returned {type(output).__name__}; expected {output_type.__name__}.")


def run_typed_sdk_sync(
    agent: AgentLike,
    prompt: Any,
    output_type: type[TOutput],
    *,
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
) -> tuple[Any, TOutput]:
    """Run an SDK agent and validate the final output type.

    A supplied `run_config` is treated as explicit fake/local execution and does
    not require credentials. Without a run config, callers must set `live=True`,
    which routes through `build_live_run_config` and its credential checks.
    """

    if run_config is not None:
        raw_result = run_sdk_sync_with_config(
            agent,
            prompt,
            run_config,
            session=session,
            max_turns=max_turns,
        )
    else:
        if not live:
            raise RuntimeError(
                "Typed SDK execution requires an explicit fake/local run_config "
                "or live=True for credential-gated model execution."
            )
        raw_result = run_sdk_sync(
            agent,
            prompt,
            _live_config_for_agent(agent, config),
            session=session,
            workflow_name=workflow_name,
            group_id=group_id,
            trace_metadata=trace_metadata,
            tracing_disabled=tracing_disabled,
            trace_include_sensitive_data=trace_include_sensitive_data,
            trace_config=trace_config,
            max_turns=max_turns,
        )

    try:
        output = _coerce_typed_output(raw_result.final_output, output_type)
    except Exception as exc:
        _attach_numeric_usage(exc, sdk_numeric_usage_observations(raw_result))
        raise
    return raw_result, output
