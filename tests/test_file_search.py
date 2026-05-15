from __future__ import annotations

import pytest

from keystone_agents import file_search
from keystone_agents.agents.business_research_analyst import build_business_research_analyst_agent
from keystone_agents.agents.chief_of_staff import build_chief_of_staff_agent

FILE_SEARCH_ENV_KEYS = (
    file_search.GLOBAL_VECTOR_STORE_IDS_ENV,
    file_search.GLOBAL_MAX_RESULTS_ENV,
    file_search.GLOBAL_INCLUDE_RESULTS_ENV,
    *file_search.AGENT_VECTOR_STORE_IDS_ENVS.values(),
    *file_search.AGENT_MAX_RESULTS_ENVS.values(),
    *file_search.AGENT_INCLUDE_RESULTS_ENVS.values(),
)


class FakeFileSearchTool:
    name = "file_search"

    def __init__(
        self,
        *,
        vector_store_ids: list[str],
        max_num_results: int | None = None,
        include_search_results: bool = False,
    ) -> None:
        self.vector_store_ids = vector_store_ids
        self.max_num_results = max_num_results
        self.include_search_results = include_search_results


def _clear_file_search_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in FILE_SEARCH_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_file_search_config_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_file_search_env(monkeypatch)

    config = file_search.file_search_config_for_agent("business_research_analyst")

    assert config.enabled is False
    assert file_search.build_file_search_tools_for_agent("business_research_analyst") == []


def test_file_search_config_uses_agent_vector_store_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_file_search_env(monkeypatch)
    monkeypatch.setenv(file_search.GLOBAL_VECTOR_STORE_IDS_ENV, "vs_global")
    monkeypatch.setenv(
        "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_VECTOR_STORE_IDS",
        "vs_one, vs_two",
    )
    monkeypatch.setenv(
        "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_MAX_NUM_RESULTS",
        "7",
    )
    monkeypatch.setenv(
        "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_INCLUDE_RESULTS",
        "true",
    )

    config = file_search.file_search_config_for_agent("business_research_analyst")

    assert config.vector_store_ids == ("vs_one", "vs_two")
    assert config.max_num_results == 7
    assert config.include_search_results is True


def test_file_search_tools_require_sdk_support_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_file_search_env(monkeypatch)
    monkeypatch.setenv(file_search.GLOBAL_VECTOR_STORE_IDS_ENV, "vs_configured")
    monkeypatch.setattr(file_search, "FileSearchTool", None)

    with pytest.raises(RuntimeError, match="hosted file search is unavailable"):
        file_search.build_file_search_tools_for_agent("chief_of_staff")


def test_business_research_analyst_attaches_file_search_only_with_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_file_search_env(monkeypatch)

    disabled_agent = build_business_research_analyst_agent()
    assert "file_search" not in {getattr(tool, "name", "") for tool in disabled_agent.tools}

    monkeypatch.setattr(file_search, "FileSearchTool", FakeFileSearchTool)
    monkeypatch.setenv(
        "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_VECTOR_STORE_IDS",
        "vs_research",
    )
    monkeypatch.setenv(
        "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_MAX_NUM_RESULTS",
        "3",
    )

    enabled_agent = build_business_research_analyst_agent()
    hosted_tools = [
        tool for tool in enabled_agent.tools if getattr(tool, "name", "") == "file_search"
    ]

    assert len(hosted_tools) == 1
    assert hosted_tools[0].vector_store_ids == ["vs_research"]
    assert hosted_tools[0].max_num_results == 3


def test_chief_of_staff_attaches_file_search_from_global_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_file_search_env(monkeypatch)
    monkeypatch.setattr(file_search, "FileSearchTool", FakeFileSearchTool)
    monkeypatch.setenv(file_search.GLOBAL_VECTOR_STORE_IDS_ENV, "vs_ops")

    agent = build_chief_of_staff_agent()

    assert "file_search" in {getattr(tool, "name", "") for tool in agent.tools}
