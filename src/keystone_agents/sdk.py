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
from dataclasses import dataclass, field
from functools import wraps
from importlib import resources
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from keystone_agents.model_provider import (
    ModelConfig,
    TraceConfig,
    TraceMetadata,
    get_model_config,
    get_runtime_agent_model_config,
    get_trace_config,
)

try:
    from agents import (
        Agent as SDKAgent,
    )
    from agents import (
        GuardrailFunctionOutput,
        ModelSettings,
        OpenAIProvider,
        RunConfig,
        RunContextWrapper,
        Runner,
        ToolGuardrailFunctionOutput,
        ToolInputGuardrailData,
        ToolOutputGuardrailData,
        input_guardrail,
        output_guardrail,
        tool_input_guardrail,
        tool_output_guardrail,
    )
    from agents import (
        WebSearchTool as SDKWebSearchTool,
    )
    from agents import (
        function_tool as _sdk_function_tool,
    )
    from openai.types.shared.reasoning import Reasoning
except ImportError as exc:  # pragma: no cover - depends on optional local install state.
    _SDK_IMPORT_ERROR: ImportError | None = exc
    GuardrailFunctionOutput = Any  # type: ignore
    ModelSettings = Any  # type: ignore
    OpenAIProvider = Any  # type: ignore
    Reasoning = Any  # type: ignore
    RunConfig = Any  # type: ignore
    RunContextWrapper = Any  # type: ignore
    Runner = Any  # type: ignore
    _sdk_function_tool = None  # type: ignore[assignment]
    SDKWebSearchTool = None  # type: ignore[assignment]

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
PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPO_AGENT_GUIDE = "AGENTS.md"
SHARED_PRE_RUN_PROMPTS = (
    "memory_policy.md",
    "writing_style.md",
    "operator_context.md",
    "local_context.md",
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


def load_prompt(name: str) -> str:
    """Load a markdown prompt from the `keystone_agents.prompts` package."""

    filename = _prompt_filename(name)
    return resources.files(PROMPT_PACKAGE).joinpath(filename).read_text(encoding="utf-8")


def load_repo_agent_guide() -> str:
    """Load the repository AGENTS.md guide for agent pre-run context."""

    configured_path = os.getenv("KEYSTONE_AGENTS_GUIDE_PATH")
    guide_path = Path(configured_path) if configured_path else PROJECT_ROOT / REPO_AGENT_GUIDE
    if not guide_path.is_file():
        return ""
    return guide_path.read_text(encoding="utf-8").strip()


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


def prompt_metadata_for_files(prompt_files: Sequence[str]) -> list[dict[str, Any]]:
    """Return JSON-safe metadata for prompt files."""

    return [metadata.to_dict() for metadata in list_prompt_metadata(prompt_files)]


def prompt_version_references(prompt_files: Sequence[str]) -> list[str]:
    """Return compact prompt version references such as `gmail_triage@2026-04-21.1`."""

    return [metadata.reference for metadata in list_prompt_metadata(prompt_files)]


def compose_instructions(*prompt_files: str) -> str:
    """Concatenate project, memory, and prompt context with clear boundaries."""

    sections = []
    repo_guide = load_repo_agent_guide()
    if repo_guide:
        sections.append(f"<!-- {REPO_AGENT_GUIDE} -->\n{repo_guide}")
    requested_files = {_prompt_filename(prompt_file) for prompt_file in prompt_files}
    for shared_prompt in SHARED_PRE_RUN_PROMPTS:
        filename = _prompt_filename(shared_prompt)
        if filename not in requested_files:
            sections.append(f"<!-- {filename} -->\n{load_prompt(filename).strip()}")
    for prompt_file in prompt_files:
        filename = prompt_file if prompt_file.endswith(".md") else f"{prompt_file}.md"
        sections.append(f"<!-- {filename} -->\n{load_prompt(filename).strip()}")
    return "\n\n".join(sections)


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
) -> AgentLike:
    """Construct an OpenAI Agents SDK `Agent` without making a model call."""

    tools_list = list(tools or [])
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
            model_settings=model_settings,
            tools=tools_list,
            handoffs=handoffs_list,
            output_type=output_type,
            input_guardrails=input_guardrails,
            output_guardrails=output_guardrails,
        )

    return Agent(
        name=name,
        handoff_description=handoff_description,
        instructions=instructions,
        model=selected_model,
        model_settings=model_settings or ModelSettings(),
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
    )


def build_model_settings(
    *,
    reasoning_effort: str | None = None,
    verbosity: str | None = None,
    max_tokens: int | None = None,
) -> Any | None:
    """Build Agents SDK model settings when the installed SDK supports them."""

    if _SDK_IMPORT_ERROR is not None:
        return {
            "reasoning": {"effort": reasoning_effort} if reasoning_effort else None,
            "verbosity": verbosity,
            "max_tokens": max_tokens,
        }
    reasoning = Reasoning(effort=reasoning_effort) if reasoning_effort else None
    return ModelSettings(
        reasoning=reasoning,
        verbosity=verbosity,
        max_tokens=max_tokens,
    )


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
    model_config.require_live_execution_ready()
    validate_sdk_available()
    provider = OpenAIProvider(**model_config.openai_provider_kwargs())
    return RunConfig(
        model=model_config.model,
        model_provider=provider,
        workflow_name=run_trace_config.workflow_name,
        group_id=run_trace_config.group_id,
        trace_metadata=dict(run_trace_config.trace_metadata) or None,
        tracing_disabled=run_trace_config.tracing_disabled,
        trace_include_sensitive_data=run_trace_config.trace_include_sensitive_data,
        sandbox=sandbox,
    )


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


def run_sdk_sync(
    agent: AgentLike,
    prompt: str,
    config: ModelConfig | None = None,
    *,
    workflow_name: str | None = None,
    group_id: str | None = None,
    trace_metadata: TraceMetadata | None = None,
    tracing_disabled: bool | None = None,
    trace_include_sensitive_data: bool | None = None,
    trace_config: TraceConfig | None = None,
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
    return Runner.run_sync(agent, prompt, run_config=run_config)


def run_sdk_sync_with_config(agent: AgentLike, prompt: str, run_config: Any) -> Any:
    """Run an SDK agent with an explicit local/fake run config."""

    validate_sdk_available()
    return Runner.run_sync(agent, prompt, run_config=run_config)


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
    prompt: str,
    output_type: type[TOutput],
    *,
    run_config: Any | None = None,
    live: bool = False,
    config: ModelConfig | None = None,
    workflow_name: str | None = None,
    group_id: str | None = None,
    trace_metadata: TraceMetadata | None = None,
    tracing_disabled: bool | None = None,
    trace_include_sensitive_data: bool | None = None,
    trace_config: TraceConfig | None = None,
) -> tuple[Any, TOutput]:
    """Run an SDK agent and validate the final output type.

    A supplied `run_config` is treated as explicit fake/local execution and does
    not require credentials. Without a run config, callers must set `live=True`,
    which routes through `build_live_run_config` and its credential checks.
    """

    if run_config is not None:
        raw_result = run_sdk_sync_with_config(agent, prompt, run_config)
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
            workflow_name=workflow_name,
            group_id=group_id,
            trace_metadata=trace_metadata,
            tracing_disabled=tracing_disabled,
            trace_include_sensitive_data=trace_include_sensitive_data,
            trace_config=trace_config,
        )

    return raw_result, _coerce_typed_output(raw_result.final_output, output_type)
