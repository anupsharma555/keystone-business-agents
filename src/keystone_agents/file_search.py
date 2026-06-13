"""Hosted file search configuration for Keystone SDK agents."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from keystone_agents.sdk import FileSearchTool

CONFIG_PATH_ENV = "KEYSTONE_FILE_SEARCH_CONFIG_PATH"
DEFAULT_CONFIG_PATH = ".local/file-search-vector-stores.json"
GLOBAL_VECTOR_STORE_IDS_ENV = "KEYSTONE_FILE_SEARCH_VECTOR_STORE_IDS"
GLOBAL_MAX_RESULTS_ENV = "KEYSTONE_FILE_SEARCH_MAX_NUM_RESULTS"
GLOBAL_INCLUDE_RESULTS_ENV = "KEYSTONE_FILE_SEARCH_INCLUDE_RESULTS"

AGENT_VECTOR_STORE_IDS_ENVS: Mapping[str, str] = {
    "business_research_analyst": "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_VECTOR_STORE_IDS",
    "chief_of_staff": "KEYSTONE_CHIEF_OF_STAFF_FILE_SEARCH_VECTOR_STORE_IDS",
    "orchestrator": "KEYSTONE_ORCHESTRATOR_FILE_SEARCH_VECTOR_STORE_IDS",
}

AGENT_MAX_RESULTS_ENVS: Mapping[str, str] = {
    "business_research_analyst": "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_MAX_NUM_RESULTS",
    "chief_of_staff": "KEYSTONE_CHIEF_OF_STAFF_FILE_SEARCH_MAX_NUM_RESULTS",
    "orchestrator": "KEYSTONE_ORCHESTRATOR_FILE_SEARCH_MAX_NUM_RESULTS",
}

AGENT_INCLUDE_RESULTS_ENVS: Mapping[str, str] = {
    "business_research_analyst": "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_INCLUDE_RESULTS",
    "chief_of_staff": "KEYSTONE_CHIEF_OF_STAFF_FILE_SEARCH_INCLUDE_RESULTS",
    "orchestrator": "KEYSTONE_ORCHESTRATOR_FILE_SEARCH_INCLUDE_RESULTS",
}


@dataclass(frozen=True)
class FileSearchConfig:
    """Validated hosted file search config for one SDK agent."""

    vector_store_ids: tuple[str, ...]
    max_num_results: int | None = None
    include_search_results: bool = False
    vector_store_env_name: str | None = None
    vector_store_config_path: str | None = None
    vector_store_scope: str = "none"
    max_results_env_name: str | None = None
    max_results_config_path: str | None = None
    include_results_env_name: str | None = None
    include_results_config_path: str | None = None

    @property
    def enabled(self) -> bool:
        """Return whether a hosted file search tool should be attached."""

        return bool(self.vector_store_ids)


def _agent_key(agent_name: str) -> str:
    return str(agent_name or "").strip().lower().replace("-", "_").replace(" ", "_")


def _env_value(env: Mapping[str, str], key: str | None) -> str | None:
    if not key:
        return None
    value = env.get(key)
    if value is None:
        return None
    return value.strip() or None


def _parse_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_vector_store_ids(raw: str | None, *, env_name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    ids = tuple(item.strip() for item in raw.split(",") if item.strip())
    if not ids:
        raise ValueError(f"{env_name} must include at least one vector store id.")
    return ids


def _parse_max_results(raw: str | None, *, env_name: str) -> int | None:
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{env_name} must be a positive integer.") from exc
    if value <= 0:
        raise ValueError(f"{env_name} must be a positive integer.")
    return value


def _file_search_config_path(env: Mapping[str, str]) -> Path:
    return Path(_env_value(env, CONFIG_PATH_ENV) or DEFAULT_CONFIG_PATH)


def _load_local_file_search_config(env: Mapping[str, str]) -> dict[str, Any]:
    path = _file_search_config_path(env)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{CONFIG_PATH_ENV} contains invalid JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{CONFIG_PATH_ENV} must contain a JSON object: {path}")
    return payload


def _config_section(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    section = payload.get(key)
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise ValueError(f"{CONFIG_PATH_ENV} section `{key}` must be a JSON object.")
    return section


def _agent_config_entry(payload: Mapping[str, Any], agent_key: str) -> Mapping[str, Any]:
    agents = _config_section(payload, "agents")
    entry = agents.get(agent_key)
    if entry is None:
        return {}
    if not isinstance(entry, Mapping):
        raise ValueError(f"{CONFIG_PATH_ENV} agents.{agent_key} must be a JSON object.")
    return entry


def _global_config_entry(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    return _config_section(payload, "global")


def _config_vector_store_ids(
    entry: Mapping[str, Any],
    *,
    label: str,
) -> tuple[str, ...]:
    raw = entry.get("vector_store_ids")
    if raw is None:
        return ()
    if isinstance(raw, str):
        return _parse_vector_store_ids(raw, env_name=label)
    if not isinstance(raw, Sequence) or isinstance(raw, (bytes, bytearray)):
        raise ValueError(f"{label}.vector_store_ids must be a string or array.")
    ids = tuple(str(item).strip() for item in raw if str(item).strip())
    if not ids:
        raise ValueError(f"{label}.vector_store_ids must include at least one vector store id.")
    return ids


def _config_max_results(entry: Mapping[str, Any], *, label: str) -> int | None:
    raw = entry.get("max_num_results")
    if raw is None:
        return None
    return _parse_max_results(str(raw), env_name=f"{label}.max_num_results")


def _config_include_results(entry: Mapping[str, Any]) -> bool | None:
    raw = entry.get("include_search_results")
    if raw is None:
        return None
    return _parse_bool(str(raw))


def file_search_config_for_agent(
    agent_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> FileSearchConfig:
    """Return hosted FileSearchTool config for an agent from explicit env settings."""

    env_map = os.environ if env is None else env
    key = _agent_key(agent_name)
    local_config = _load_local_file_search_config(env_map)
    local_config_path = str(_file_search_config_path(env_map))
    agent_config = _agent_config_entry(local_config, key) if local_config else {}
    global_config = _global_config_entry(local_config) if local_config else {}
    vector_store_env = AGENT_VECTOR_STORE_IDS_ENVS.get(key, GLOBAL_VECTOR_STORE_IDS_ENV)
    max_results_env = AGENT_MAX_RESULTS_ENVS.get(key, GLOBAL_MAX_RESULTS_ENV)
    include_results_env = AGENT_INCLUDE_RESULTS_ENVS.get(key, GLOBAL_INCLUDE_RESULTS_ENV)

    vector_store_raw = _env_value(env_map, vector_store_env)
    vector_store_config_path = None
    vector_store_env_used = vector_store_env
    vector_store_scope = "agent" if vector_store_raw is not None else "none"
    if vector_store_raw is None and vector_store_env != GLOBAL_VECTOR_STORE_IDS_ENV:
        vector_store_raw = _env_value(env_map, GLOBAL_VECTOR_STORE_IDS_ENV)
        vector_store_env_used = GLOBAL_VECTOR_STORE_IDS_ENV
        vector_store_scope = "global" if vector_store_raw is not None else "none"
    elif vector_store_env == GLOBAL_VECTOR_STORE_IDS_ENV and vector_store_raw is not None:
        vector_store_scope = "global"
    vector_store_ids_from_config: tuple[str, ...] = ()
    if vector_store_raw is None and agent_config:
        vector_store_ids_from_config = _config_vector_store_ids(
            agent_config,
            label=f"{CONFIG_PATH_ENV}.agents.{key}",
        )
        if vector_store_ids_from_config:
            vector_store_scope = "agent_config"
            vector_store_config_path = local_config_path
    if vector_store_raw is None and not vector_store_ids_from_config and global_config:
        vector_store_ids_from_config = _config_vector_store_ids(
            global_config,
            label=f"{CONFIG_PATH_ENV}.global",
        )
        if vector_store_ids_from_config:
            vector_store_scope = "global_config"
            vector_store_config_path = local_config_path

    max_results_raw = _env_value(env_map, max_results_env)
    max_results_env_used = max_results_env
    max_results_config_value: int | None = None
    max_results_config_path: str | None = None
    if max_results_raw is None and max_results_env != GLOBAL_MAX_RESULTS_ENV:
        max_results_raw = _env_value(env_map, GLOBAL_MAX_RESULTS_ENV)
        max_results_env_used = GLOBAL_MAX_RESULTS_ENV
    if max_results_raw is None and vector_store_scope == "agent_config":
        max_results_config_value = _config_max_results(
            agent_config,
            label=f"{CONFIG_PATH_ENV}.agents.{key}",
        )
        if max_results_config_value is not None:
            max_results_config_path = local_config_path
    if (
        max_results_raw is None
        and max_results_config_value is None
        and vector_store_scope == "global_config"
        and global_config
    ):
        max_results_config_value = _config_max_results(
            global_config,
            label=f"{CONFIG_PATH_ENV}.global",
        )
        if max_results_config_value is not None:
            max_results_config_path = local_config_path

    include_results_raw = _env_value(env_map, include_results_env)
    include_results_env_used = include_results_env
    include_results_config_value: bool | None = None
    include_results_config_path: str | None = None
    if include_results_raw is None and include_results_env != GLOBAL_INCLUDE_RESULTS_ENV:
        include_results_raw = _env_value(env_map, GLOBAL_INCLUDE_RESULTS_ENV)
        include_results_env_used = GLOBAL_INCLUDE_RESULTS_ENV
    if include_results_raw is None and vector_store_scope == "agent_config":
        include_results_config_value = _config_include_results(agent_config)
        if include_results_config_value is not None:
            include_results_config_path = local_config_path
    if (
        include_results_raw is None
        and include_results_config_value is None
        and vector_store_scope == "global_config"
        and global_config
    ):
        include_results_config_value = _config_include_results(global_config)
        if include_results_config_value is not None:
            include_results_config_path = local_config_path

    return FileSearchConfig(
        vector_store_ids=(
            _parse_vector_store_ids(
                vector_store_raw,
                env_name=vector_store_env_used,
            )
            if vector_store_raw is not None
            else vector_store_ids_from_config
        ),
        max_num_results=(
            _parse_max_results(
                max_results_raw,
                env_name=max_results_env_used,
            )
            if max_results_raw is not None
            else max_results_config_value
        ),
        include_search_results=(
            _parse_bool(include_results_raw)
            if include_results_raw is not None
            else bool(include_results_config_value)
        ),
        vector_store_env_name=vector_store_env_used if vector_store_raw is not None else None,
        vector_store_config_path=vector_store_config_path,
        vector_store_scope=vector_store_scope,
        max_results_env_name=max_results_env_used if max_results_raw is not None else None,
        max_results_config_path=max_results_config_path,
        include_results_env_name=(
            include_results_env_used if include_results_raw is not None else None
        ),
        include_results_config_path=include_results_config_path,
    )


def file_search_availability_for_agent(
    agent_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return sanitized runtime availability for hosted FileSearch on one agent.

    The diagnostic intentionally reports counts and environment-variable sources
    instead of raw vector store IDs so CLI cards can be shared in logs.
    """

    key = _agent_key(agent_name)
    try:
        config = file_search_config_for_agent(key, env=env)
    except ValueError as exc:
        return {
            "tool_name": "file_search",
            "agent_name": key,
            "configured": True,
            "sdk_available": FileSearchTool is not None,
            "available": False,
            "status": "invalid_config",
            "error": str(exc),
            "vector_store_id_count": 0,
            "vector_store_source": "unknown",
            "vector_store_env_name": None,
            "vector_store_config_path": None,
            "max_num_results": None,
            "max_results_env_name": None,
            "max_results_config_path": None,
            "include_search_results": False,
            "include_results_env_name": None,
            "include_results_config_path": None,
        }

    configured = config.enabled
    sdk_available = FileSearchTool is not None
    if not configured:
        status = "not_configured"
    elif not sdk_available:
        status = "sdk_unavailable"
    else:
        status = "available"
    return {
        "tool_name": "file_search",
        "agent_name": key,
        "configured": configured,
        "sdk_available": sdk_available,
        "available": configured and sdk_available,
        "status": status,
        "error": None,
        "vector_store_id_count": len(config.vector_store_ids),
        "vector_store_source": config.vector_store_scope,
        "vector_store_env_name": config.vector_store_env_name,
        "vector_store_config_path": config.vector_store_config_path,
        "max_num_results": config.max_num_results,
        "max_results_env_name": config.max_results_env_name,
        "max_results_config_path": config.max_results_config_path,
        "include_search_results": config.include_search_results,
        "include_results_env_name": config.include_results_env_name,
        "include_results_config_path": config.include_results_config_path,
    }


def local_file_search_config_summary(
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a sanitized summary of the ignored local FileSearch config file."""

    env_map = os.environ if env is None else env
    path = _file_search_config_path(env_map)
    if not path.is_file():
        return {
            "path": str(path),
            "exists": False,
            "configured": False,
            "status": "missing",
            "error": None,
            "global": None,
            "agents": [],
            "unknown_agents": [],
        }
    try:
        payload = _load_local_file_search_config(env_map)
        global_entry = _global_config_entry(payload)
        agents_entry = _config_section(payload, "agents")
        agent_summaries = [
            {
                "agent_name": agent_name,
                "known_agent": agent_name in AGENT_VECTOR_STORE_IDS_ENVS,
                **_local_config_entry_summary(
                    agent_config,
                    label=f"{CONFIG_PATH_ENV}.agents.{agent_name}",
                ),
            }
            for agent_name, agent_config in sorted(agents_entry.items())
        ]
        global_summary = (
            _local_config_entry_summary(global_entry, label=f"{CONFIG_PATH_ENV}.global")
            if global_entry
            else None
        )
    except ValueError as exc:
        return {
            "path": str(path),
            "exists": True,
            "configured": True,
            "status": "invalid_config",
            "error": str(exc),
            "global": None,
            "agents": [],
            "unknown_agents": [],
        }
    configured = bool(
        (global_summary and global_summary["vector_store_id_count"] > 0)
        or any(item["vector_store_id_count"] > 0 for item in agent_summaries)
    )
    return {
        "path": str(path),
        "exists": True,
        "configured": configured,
        "status": "ready" if configured else "empty",
        "error": None,
        "global": global_summary,
        "agents": agent_summaries,
        "unknown_agents": [
            item["agent_name"] for item in agent_summaries if not item["known_agent"]
        ],
    }


def _local_config_entry_summary(entry: Mapping[str, Any], *, label: str) -> dict[str, Any]:
    if not isinstance(entry, Mapping):
        raise ValueError(f"{label} must be a JSON object.")
    vector_store_ids = _config_vector_store_ids(entry, label=label)
    max_num_results = _config_max_results(entry, label=label)
    include_results = _config_include_results(entry)
    return {
        "vector_store_id_count": len(vector_store_ids),
        "max_num_results": max_num_results,
        "include_search_results": bool(include_results),
    }


def build_file_search_tools_for_agent(
    agent_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> list[Any]:
    """Build hosted FileSearchTool instances for an agent, or return none when disabled."""

    config = file_search_config_for_agent(agent_name, env=env)
    if not config.enabled:
        return []
    if FileSearchTool is None:
        raise RuntimeError(
            "OpenAI Agents SDK hosted file search is unavailable. Install a version of "
            "`openai-agents` that includes `agents.FileSearchTool` before setting hosted "
            "file search vector store ids."
        )
    return [
        FileSearchTool(
            vector_store_ids=list(config.vector_store_ids),
            max_num_results=config.max_num_results,
            include_search_results=config.include_search_results,
        )
    ]


def append_configured_file_search_tools(
    agent_name: str,
    tools: Sequence[Any],
    *,
    env: Mapping[str, str] | None = None,
) -> list[Any]:
    """Return tools plus hosted file search when explicit vector store config exists."""

    return [*tools, *build_file_search_tools_for_agent(agent_name, env=env)]
