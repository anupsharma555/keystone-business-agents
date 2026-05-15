"""Hosted file search configuration for Keystone SDK agents."""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from keystone_agents.sdk import FileSearchTool

GLOBAL_VECTOR_STORE_IDS_ENV = "KEYSTONE_FILE_SEARCH_VECTOR_STORE_IDS"
GLOBAL_MAX_RESULTS_ENV = "KEYSTONE_FILE_SEARCH_MAX_NUM_RESULTS"
GLOBAL_INCLUDE_RESULTS_ENV = "KEYSTONE_FILE_SEARCH_INCLUDE_RESULTS"

AGENT_VECTOR_STORE_IDS_ENVS: Mapping[str, str] = {
    "business_research_analyst": "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_VECTOR_STORE_IDS",
    "chief_of_staff": "KEYSTONE_CHIEF_OF_STAFF_FILE_SEARCH_VECTOR_STORE_IDS",
}

AGENT_MAX_RESULTS_ENVS: Mapping[str, str] = {
    "business_research_analyst": "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_MAX_NUM_RESULTS",
    "chief_of_staff": "KEYSTONE_CHIEF_OF_STAFF_FILE_SEARCH_MAX_NUM_RESULTS",
}

AGENT_INCLUDE_RESULTS_ENVS: Mapping[str, str] = {
    "business_research_analyst": "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_INCLUDE_RESULTS",
    "chief_of_staff": "KEYSTONE_CHIEF_OF_STAFF_FILE_SEARCH_INCLUDE_RESULTS",
}


@dataclass(frozen=True)
class FileSearchConfig:
    """Validated hosted file search config for one SDK agent."""

    vector_store_ids: tuple[str, ...]
    max_num_results: int | None = None
    include_search_results: bool = False

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


def file_search_config_for_agent(
    agent_name: str,
    *,
    env: Mapping[str, str] | None = None,
) -> FileSearchConfig:
    """Return hosted FileSearchTool config for an agent from explicit env settings."""

    env_map = os.environ if env is None else env
    key = _agent_key(agent_name)
    vector_store_env = AGENT_VECTOR_STORE_IDS_ENVS.get(key, GLOBAL_VECTOR_STORE_IDS_ENV)
    max_results_env = AGENT_MAX_RESULTS_ENVS.get(key, GLOBAL_MAX_RESULTS_ENV)
    include_results_env = AGENT_INCLUDE_RESULTS_ENVS.get(key, GLOBAL_INCLUDE_RESULTS_ENV)

    vector_store_raw = _env_value(env_map, vector_store_env)
    vector_store_env_used = vector_store_env
    if vector_store_raw is None and vector_store_env != GLOBAL_VECTOR_STORE_IDS_ENV:
        vector_store_raw = _env_value(env_map, GLOBAL_VECTOR_STORE_IDS_ENV)
        vector_store_env_used = GLOBAL_VECTOR_STORE_IDS_ENV

    max_results_raw = _env_value(env_map, max_results_env)
    max_results_env_used = max_results_env
    if max_results_raw is None and max_results_env != GLOBAL_MAX_RESULTS_ENV:
        max_results_raw = _env_value(env_map, GLOBAL_MAX_RESULTS_ENV)
        max_results_env_used = GLOBAL_MAX_RESULTS_ENV

    include_results_raw = _env_value(env_map, include_results_env)
    if include_results_raw is None and include_results_env != GLOBAL_INCLUDE_RESULTS_ENV:
        include_results_raw = _env_value(env_map, GLOBAL_INCLUDE_RESULTS_ENV)

    return FileSearchConfig(
        vector_store_ids=_parse_vector_store_ids(
            vector_store_raw,
            env_name=vector_store_env_used,
        ),
        max_num_results=_parse_max_results(
            max_results_raw,
            env_name=max_results_env_used,
        ),
        include_search_results=_parse_bool(include_results_raw),
    )


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
