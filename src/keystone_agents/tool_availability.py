"""Sanitized runtime tool availability diagnostics for agent cards."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any

from keystone_agents.agent_tool_policy import source_layer_policy_for_tools
from keystone_agents.config import parse_bool
from keystone_agents.file_search import file_search_availability_for_agent
from keystone_agents.sdk import HostedMCPTool, ToolSearchTool
from keystone_agents.tools.kni_document_tool import list_kni_document_sources_impl
from keystone_agents.tools.search_provider import SearchProviderName, normalize_search_provider_name

KNI_DOCUMENT_TOOL_NAMES = frozenset(
    {
        "list_kni_document_sources",
        "search_kni_documents",
        "read_kni_document_file",
    }
)


def runtime_tool_availability_for_agent(
    agent_name: str,
    *,
    tools: Sequence[str],
    optional_tools: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return JSON-safe runtime tool availability for one agent card.

    This reports only configuration state, counts, and non-secret paths/statuses.
    It does not expose API keys, vector store IDs, document snippets, or search
    result content.
    """

    declared = set(tools) | set(optional_tools)
    availability: dict[str, Any] = {
        "mcp": mcp_availability_for_agent(agent_name),
        "tool_search": tool_search_availability_for_agent(agent_name),
    }
    if "file_search" in declared:
        availability["file_search"] = file_search_availability_for_agent(agent_name, env=env)
    if "search_web" in declared:
        availability["search_web"] = search_web_availability_for_agent(agent_name, env=env)
    if KNI_DOCUMENT_TOOL_NAMES <= declared:
        availability["local_kni_documents"] = local_kni_document_availability(env=env)
    availability["source_layer_policy"] = source_layer_policy_availability(
        agent_name,
        declared,
        tool_statuses=availability,
    )
    return availability


def source_layer_policy_availability(
    agent_name: str,
    declared_tools: Sequence[str] | frozenset[str],
    *,
    tool_statuses: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return advisory evidence-layer guidance for this agent's tool surface."""

    statuses = tool_statuses or {}
    layers = [
        _source_layer_runtime_summary(layer, statuses)
        for layer in source_layer_policy_for_tools(declared_tools)
    ]
    return {
        "tool_name": "source_layer_policy",
        "agent_name": str(agent_name or "").strip(),
        "configured": bool(layers),
        "available": any(bool(layer.get("runtime_available")) for layer in layers),
        "status": "declared" if layers else "none",
        "layers": layers,
        "reason": (
            "Advisory source-layer guidance only; the model must still answer from "
            "the latest user question and actual tool outputs."
        ),
    }


def _source_layer_runtime_summary(
    layer: Mapping[str, Any],
    tool_statuses: Mapping[str, Any],
) -> dict[str, Any]:
    tools = [str(name) for name in layer.get("tools", ()) if str(name).strip()]
    backing_tool_name = _source_layer_backing_tool_name(str(layer.get("layer") or ""))
    backing_status = tool_statuses.get(backing_tool_name)
    runtime_status = "not_attached"
    runtime_available = False
    runtime_configured = False
    if isinstance(backing_status, Mapping):
        runtime_status = str(backing_status.get("status") or "unknown")
        runtime_available = bool(backing_status.get("available"))
        runtime_configured = bool(backing_status.get("configured"))
    return {
        **dict(layer),
        "tools": tools,
        "runtime_tool": backing_tool_name,
        "runtime_status": runtime_status,
        "runtime_available": runtime_available,
        "runtime_configured": runtime_configured,
    }


def _source_layer_backing_tool_name(layer_name: str) -> str:
    if layer_name == "local_kni_documents":
        return "local_kni_documents"
    if layer_name == "hosted_file_search":
        return "file_search"
    if layer_name == "public_web_search":
        return "search_web"
    return layer_name


def mcp_availability_for_agent(agent_name: str) -> dict[str, Any]:
    """Return current MCP status for an agent.

    Keystone currently prefers reviewed SDK function tools for owned provider
    boundaries. MCP should become available only after a dedicated provider
    boundary is added with tests and live flags.
    """

    sdk_available = HostedMCPTool is not None
    return {
        "tool_name": "mcp",
        "agent_name": str(agent_name or "").strip(),
        "configured": False,
        "sdk_available": sdk_available,
        "available": False,
        "status": "sdk_available_not_configured" if sdk_available else "sdk_unavailable",
        "reason": (
            "Agents SDK HostedMCPTool is installed, but Keystone has no reviewed "
            "MCP server/client boundary configured for this agent."
            if sdk_available
            else "Installed Agents SDK does not expose HostedMCPTool."
        ),
    }


def tool_search_availability_for_agent(agent_name: str) -> dict[str, Any]:
    """Return current dynamic tool-search status for an agent."""

    sdk_available = ToolSearchTool is not None
    return {
        "tool_name": "tool_search",
        "agent_name": str(agent_name or "").strip(),
        "configured": False,
        "sdk_available": sdk_available,
        "available": False,
        "status": "sdk_available_not_configured" if sdk_available else "sdk_unavailable",
        "reason": (
            "Agents SDK ToolSearchTool is installed, but Keystone has no reviewed "
            "dynamic tool-search loader configured for this agent."
            if sdk_available
            else "Installed Agents SDK does not expose ToolSearchTool."
        ),
    }


def search_web_availability_for_agent(
    agent_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return sanitized availability for the shared public `search_web` tool."""

    env_map = os.environ if env is None else env
    live_provider = _sdk_live_search_provider_name(env_map)
    configured_provider = (env_map.get("SEARCH_PROVIDER") or "").strip() or "default"
    provider_sequence = _search_provider_sequence(env_map, live_provider)
    live_enabled = live_provider is not None
    live_available = live_enabled and any(
        _provider_prerequisite_configured(env_map, provider) for provider in provider_sequence
    )
    if not live_enabled:
        status = "attached_live_gated"
    elif live_available:
        status = "attached_live_available"
    else:
        status = "attached_live_misconfigured"
    return {
        "tool_name": "search_web",
        "agent_name": str(agent_name or "").strip(),
        "attached": True,
        "available": True,
        "status": status,
        "configured_provider": configured_provider,
        "live_enabled": live_enabled,
        "live_available": live_available,
        "provider_sequence": provider_sequence,
        "provider_prerequisites": {
            "searxng_base_url": bool(_env_value(env_map, "SEARXNG_BASE_URL")),
            "openai_api_key": bool(_env_value(env_map, "KEYSTONE_OPENAI_API_KEY")),
            "exa_api_key": bool(_env_value(env_map, "EXA_API_KEY")),
            "tavily_api_key": bool(_env_value(env_map, "TAVILY_API_KEY")),
            "firecrawl_api_key": bool(_env_value(env_map, "FIRECRAWL_API_KEY")),
            "serper_enabled": _env_bool(env_map, "KEYSTONE_SERPER_ENABLED", default=False),
            "serper_api_key": bool(_env_value(env_map, "SERPER_API_KEY")),
        },
    }


def local_kni_document_availability(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return sanitized availability for local-only KNI document tools."""

    try:
        payload = list_kni_document_sources_impl(env=dict(env) if env is not None else None)
    except Exception as exc:  # pragma: no cover - defensive for local filesystem drift.
        return {
            "tool_name": "local_kni_documents",
            "configured": False,
            "available": False,
            "status": "error",
            "error": f"{exc.__class__.__name__}: {exc}",
            "local_only": True,
            "send_enabled": False,
        }
    status = str(payload.get("status") or "")
    enabled = bool(payload.get("enabled"))
    return {
        "tool_name": "local_kni_documents",
        "configured": enabled,
        "available": enabled and status == "ready",
        "status": status or ("ready" if enabled else "disabled"),
        "indexed_count": int(payload.get("indexed_count") or 0),
        "indexed_at": str(payload.get("indexed_at") or ""),
        "root_path": str(payload.get("root_path") or ""),
        "index_path": str(payload.get("index_path") or ""),
        "local_only": True,
        "model_context_allowed": bool(payload.get("model_context_allowed")),
        "send_enabled": False,
    }


def _sdk_live_search_provider_name(env: Mapping[str, str]) -> str | None:
    raw_live_mode = env.get("KEYSTONE_LIVE_MODE")
    if raw_live_mode is None or not parse_bool(raw_live_mode):
        return None
    raw_dry_run = env.get("KEYSTONE_DRY_RUN")
    if raw_dry_run is not None and parse_bool(raw_dry_run):
        return None
    raw_live_research = env.get("KEYSTONE_ENABLE_LIVE_RESEARCH")
    if raw_live_research is None and raw_dry_run is None:
        return None
    if raw_live_research is not None and not parse_bool(raw_live_research):
        return None
    raw_provider = env.get("SEARCH_PROVIDER")
    if raw_provider is None or not raw_provider.strip():
        return SearchProviderName.SEARXNG.value
    try:
        provider_name = normalize_search_provider_name(raw_provider)
    except ValueError:
        return str(raw_provider or "").strip().lower() or None
    if provider_name == SearchProviderName.DRY_RUN:
        return None
    return provider_name.value


def _search_provider_sequence(env: Mapping[str, str], live_provider: str | None) -> list[str]:
    if live_provider is None:
        return []
    sequence = [live_provider]
    if live_provider == SearchProviderName.SEARXNG.value:
        if _env_bool(env, "KEYSTONE_AGENTS_WEB_SEARCH_FALLBACK", default=True):
            sequence.append(SearchProviderName.AGENTS_WEB_SEARCH.value)
        if _env_bool(env, "KEYSTONE_EXA_SEARCH_FALLBACK", default=bool(_env_value(env, "EXA_API_KEY"))):
            sequence.append(SearchProviderName.EXA.value)
        if _env_bool(env, "KEYSTONE_TAVILY_SEARCH_FALLBACK", default=False):
            sequence.append(SearchProviderName.TAVILY.value)
    return list(dict.fromkeys(sequence))


def _provider_prerequisite_configured(env: Mapping[str, str], provider: str) -> bool:
    if provider == SearchProviderName.SEARXNG.value:
        return bool(_env_value(env, "SEARXNG_BASE_URL"))
    if provider == SearchProviderName.AGENTS_WEB_SEARCH.value:
        return bool(_env_value(env, "KEYSTONE_OPENAI_API_KEY"))
    if provider == SearchProviderName.EXA.value:
        return bool(_env_value(env, "EXA_API_KEY"))
    if provider == SearchProviderName.TAVILY.value:
        return bool(_env_value(env, "TAVILY_API_KEY"))
    if provider == SearchProviderName.FIRECRAWL.value:
        return bool(_env_value(env, "FIRECRAWL_API_KEY"))
    if provider == SearchProviderName.SERPER.value:
        return _env_bool(env, "KEYSTONE_SERPER_ENABLED", default=False) and bool(
            _env_value(env, "SERPER_API_KEY")
        )
    return False


def _env_value(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    return value.strip() or None


def _env_bool(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    value = env.get(name)
    if value is None:
        return default
    return parse_bool(value)
