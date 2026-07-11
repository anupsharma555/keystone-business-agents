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
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
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
DEFAULT_PROMPT_CACHE_RETENTION = "24h"
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
        return SQLiteSession(session_id, str(path), session_settings=settings)
    return SQLiteSession(session_id, session_settings=settings)


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


def compose_instructions(*prompt_files: str, skill_files: Sequence[str] = ()) -> str:
    """Concatenate project, memory, and prompt context with clear boundaries."""

    sections = []
    repo_profile_name, repo_profile = load_repo_runtime_policy()
    if repo_profile:
        sections.append(f"<!-- {repo_profile_name} -->\n{repo_profile}")
    requested_files = {_prompt_filename(prompt_file) for prompt_file in prompt_files}
    for shared_prompt in SHARED_PRE_RUN_PROMPTS:
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
            model_settings=_cache_friendly_model_settings(model_settings),
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
        model_settings=_cache_friendly_model_settings(model_settings),
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
) -> Any | None:
    """Build Agents SDK model settings when the installed SDK supports them."""

    if _SDK_IMPORT_ERROR is not None:
        return _cache_friendly_model_settings(
            {
                "reasoning": {"effort": reasoning_effort} if reasoning_effort else None,
                "verbosity": verbosity,
                "max_tokens": max_tokens,
            }
        )
    reasoning = Reasoning(effort=reasoning_effort) if reasoning_effort else None
    return _cache_friendly_model_settings(
        ModelSettings(
            reasoning=reasoning,
            verbosity=verbosity,
            max_tokens=max_tokens,
        )
    )


def _cache_friendly_model_settings(model_settings: Any | None = None) -> Any:
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
        return settings
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
    return dataclass_replace(settings, **updates) if updates else settings


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
        max_retries=_live_model_max_retries(),
    )
    provider = OpenAIProvider(
        openai_client=openai_client,
        use_responses=provider_kwargs.get("use_responses"),
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


def _run_sync_with_optional_session(
    agent: AgentLike,
    prompt: Any,
    *,
    run_config: Any,
    session: Any | None = None,
    max_turns: int | None = None,
) -> Any:
    kwargs: dict[str, Any] = {"run_config": run_config}
    if session is not None:
        kwargs["session"] = session
    if max_turns is not None:
        kwargs["max_turns"] = max_turns
    return Runner.run_sync(agent, prompt, **kwargs)


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

    return raw_result, _coerce_typed_output(raw_result.final_output, output_type)
