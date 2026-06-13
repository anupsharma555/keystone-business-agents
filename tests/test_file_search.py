from __future__ import annotations

import json

import pytest

from keystone_agents import file_search
from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_agent,
    build_business_research_analyst_comparison_agent,
    build_business_research_analyst_focused_brief_agent,
    build_business_research_analyst_research_brief_agent,
)
from keystone_agents.agents.chief_of_staff import build_chief_of_staff_agent
from keystone_agents.agents.orchestrator import build_orchestrator_agent

FILE_SEARCH_ENV_KEYS = (
    file_search.CONFIG_PATH_ENV,
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
    monkeypatch.setenv(
        file_search.CONFIG_PATH_ENV,
        "/__missing_keystone_file_search_vector_stores__.json",
    )


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
    assert config.vector_store_scope == "agent"
    assert (
        config.vector_store_env_name
        == "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_VECTOR_STORE_IDS"
    )
    assert (
        config.max_results_env_name
        == "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_MAX_NUM_RESULTS"
    )
    assert (
        config.include_results_env_name
        == "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_INCLUDE_RESULTS"
    )


def test_file_search_config_supports_orchestrator_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_file_search_env(monkeypatch)
    monkeypatch.setenv(file_search.GLOBAL_VECTOR_STORE_IDS_ENV, "vs_global")
    monkeypatch.setenv("KEYSTONE_ORCHESTRATOR_FILE_SEARCH_VECTOR_STORE_IDS", "vs_route")
    monkeypatch.setenv("KEYSTONE_ORCHESTRATOR_FILE_SEARCH_MAX_NUM_RESULTS", "2")

    config = file_search.file_search_config_for_agent("orchestrator")

    assert config.vector_store_ids == ("vs_route",)
    assert config.max_num_results == 2
    assert config.include_search_results is False
    assert config.vector_store_scope == "agent"


def test_file_search_config_records_global_fallback_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_file_search_env(monkeypatch)
    monkeypatch.setenv(file_search.GLOBAL_VECTOR_STORE_IDS_ENV, "vs_global")
    monkeypatch.setenv(file_search.GLOBAL_MAX_RESULTS_ENV, "4")
    monkeypatch.setenv(file_search.GLOBAL_INCLUDE_RESULTS_ENV, "yes")

    config = file_search.file_search_config_for_agent("chief_of_staff")

    assert config.vector_store_ids == ("vs_global",)
    assert config.vector_store_scope == "global"
    assert config.vector_store_env_name == file_search.GLOBAL_VECTOR_STORE_IDS_ENV
    assert config.max_num_results == 4
    assert config.max_results_env_name == file_search.GLOBAL_MAX_RESULTS_ENV
    assert config.include_search_results is True
    assert config.include_results_env_name == file_search.GLOBAL_INCLUDE_RESULTS_ENV


def test_file_search_config_uses_agent_config_file_fallback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clear_file_search_env(monkeypatch)
    config_path = tmp_path / "file-search-vector-stores.json"
    config_path.write_text(
        json.dumps(
            {
                "schema": "keystone.file_search_vector_stores.v1",
                "agents": {
                    "business_research_analyst": {
                        "vector_store_ids": ["vs_public_agents_sdk"],
                        "max_num_results": 6,
                        "include_search_results": True,
                    }
                },
            }
        )
    )
    monkeypatch.setenv(file_search.CONFIG_PATH_ENV, str(config_path))

    config = file_search.file_search_config_for_agent("business_research_analyst")

    assert config.vector_store_ids == ("vs_public_agents_sdk",)
    assert config.vector_store_scope == "agent_config"
    assert config.vector_store_env_name is None
    assert config.vector_store_config_path == str(config_path)
    assert config.max_num_results == 6
    assert config.max_results_config_path == str(config_path)
    assert config.include_search_results is True
    assert config.include_results_config_path == str(config_path)


def test_file_search_config_env_overrides_config_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clear_file_search_env(monkeypatch)
    config_path = tmp_path / "file-search-vector-stores.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {
                    "vector_store_ids": ["vs_from_config"],
                    "max_num_results": 2,
                },
            }
        )
    )
    monkeypatch.setenv(file_search.CONFIG_PATH_ENV, str(config_path))
    monkeypatch.setenv(file_search.GLOBAL_VECTOR_STORE_IDS_ENV, "vs_from_env")
    monkeypatch.setenv(file_search.GLOBAL_MAX_RESULTS_ENV, "4")

    config = file_search.file_search_config_for_agent("chief_of_staff")

    assert config.vector_store_ids == ("vs_from_env",)
    assert config.vector_store_scope == "global"
    assert config.vector_store_env_name == file_search.GLOBAL_VECTOR_STORE_IDS_ENV
    assert config.vector_store_config_path is None
    assert config.max_num_results == 4
    assert config.max_results_env_name == file_search.GLOBAL_MAX_RESULTS_ENV
    assert config.max_results_config_path is None


def test_file_search_config_does_not_mix_env_vector_ids_with_config_options(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clear_file_search_env(monkeypatch)
    config_path = tmp_path / "file-search-vector-stores.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {
                    "vector_store_ids": ["vs_from_config"],
                    "max_num_results": 2,
                    "include_search_results": True,
                },
            }
        )
    )
    monkeypatch.setenv(file_search.CONFIG_PATH_ENV, str(config_path))
    monkeypatch.setenv(file_search.GLOBAL_VECTOR_STORE_IDS_ENV, "vs_from_env")

    config = file_search.file_search_config_for_agent("chief_of_staff")

    assert config.vector_store_ids == ("vs_from_env",)
    assert config.vector_store_scope == "global"
    assert config.max_num_results is None
    assert config.max_results_config_path is None
    assert config.include_search_results is False
    assert config.include_results_config_path is None


def test_file_search_availability_reports_config_file_without_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clear_file_search_env(monkeypatch)
    monkeypatch.setattr(file_search, "FileSearchTool", FakeFileSearchTool)
    config_path = tmp_path / "file-search-vector-stores.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "orchestrator": {
                        "vector_store_ids": ["vs_orchestrator_public"],
                    }
                }
            }
        )
    )
    monkeypatch.setenv(file_search.CONFIG_PATH_ENV, str(config_path))

    status = file_search.file_search_availability_for_agent("orchestrator")

    assert status["available"] is True
    assert status["vector_store_id_count"] == 1
    assert status["vector_store_source"] == "agent_config"
    assert status["vector_store_config_path"] == str(config_path)
    assert "vs_orchestrator_public" not in repr(status)


def test_local_file_search_config_summary_reports_counts_without_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clear_file_search_env(monkeypatch)
    config_path = tmp_path / "file-search-vector-stores.json"
    config_path.write_text(
        json.dumps(
            {
                "global": {
                    "vector_store_ids": ["vs_global_public_docs"],
                    "max_num_results": 5,
                },
                "agents": {
                    "chief_of_staff": {
                        "vector_store_ids": ["vs_chief_docs"],
                        "include_search_results": True,
                    },
                    "unknown_helper": {
                        "vector_store_ids": ["vs_unknown_docs"],
                    },
                },
            }
        )
    )
    monkeypatch.setenv(file_search.CONFIG_PATH_ENV, str(config_path))

    summary = file_search.local_file_search_config_summary()

    assert summary["status"] == "ready"
    assert summary["configured"] is True
    assert summary["global"]["vector_store_id_count"] == 1
    assert summary["global"]["max_num_results"] == 5
    assert summary["unknown_agents"] == ["unknown_helper"]
    agent_by_name = {item["agent_name"]: item for item in summary["agents"]}
    assert agent_by_name["chief_of_staff"]["known_agent"] is True
    assert agent_by_name["chief_of_staff"]["vector_store_id_count"] == 1
    assert agent_by_name["chief_of_staff"]["include_search_results"] is True
    assert agent_by_name["unknown_helper"]["known_agent"] is False
    assert "vs_global_public_docs" not in repr(summary)
    assert "vs_chief_docs" not in repr(summary)
    assert "vs_unknown_docs" not in repr(summary)


def test_local_file_search_config_summary_reports_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_file_search_env(monkeypatch)

    summary = file_search.local_file_search_config_summary()

    assert summary["status"] == "missing"
    assert summary["exists"] is False
    assert summary["configured"] is False


def test_local_file_search_config_summary_reports_invalid_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    _clear_file_search_env(monkeypatch)
    config_path = tmp_path / "file-search-vector-stores.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "chief_of_staff": {
                        "vector_store_ids": [],
                    },
                },
            }
        )
    )
    monkeypatch.setenv(file_search.CONFIG_PATH_ENV, str(config_path))

    summary = file_search.local_file_search_config_summary()

    assert summary["status"] == "invalid_config"
    assert summary["exists"] is True
    assert "must include at least one vector store id" in str(summary["error"])


def test_file_search_availability_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_file_search_env(monkeypatch)
    monkeypatch.setattr(file_search, "FileSearchTool", FakeFileSearchTool)
    monkeypatch.setenv(
        "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_VECTOR_STORE_IDS",
        "vs_private_one,vs_private_two",
    )

    status = file_search.file_search_availability_for_agent("business_research_analyst")

    assert status["available"] is True
    assert status["configured"] is True
    assert status["sdk_available"] is True
    assert status["status"] == "available"
    assert status["vector_store_id_count"] == 2
    assert status["vector_store_source"] == "agent"
    assert "vs_private_one" not in repr(status)
    assert "vs_private_two" not in repr(status)


def test_file_search_availability_reports_invalid_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_file_search_env(monkeypatch)
    monkeypatch.setenv(file_search.GLOBAL_VECTOR_STORE_IDS_ENV, "vs_global")
    monkeypatch.setenv(file_search.GLOBAL_MAX_RESULTS_ENV, "not-an-int")

    status = file_search.file_search_availability_for_agent("orchestrator")

    assert status["available"] is False
    assert status["configured"] is True
    assert status["status"] == "invalid_config"
    assert "positive integer" in str(status["error"])


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


@pytest.mark.parametrize(
    "builder",
    [
        build_business_research_analyst_focused_brief_agent,
        build_business_research_analyst_comparison_agent,
        build_business_research_analyst_research_brief_agent,
    ],
)
def test_business_research_analyst_specialist_variants_attach_file_search(
    monkeypatch: pytest.MonkeyPatch,
    builder,
) -> None:
    _clear_file_search_env(monkeypatch)
    monkeypatch.setattr(file_search, "FileSearchTool", FakeFileSearchTool)
    monkeypatch.setenv(
        "KEYSTONE_BUSINESS_RESEARCH_ANALYST_FILE_SEARCH_VECTOR_STORE_IDS",
        "vs_research",
    )

    agent = builder()

    hosted_tools = [
        tool for tool in agent.tools if getattr(tool, "name", "") == "file_search"
    ]
    assert len(hosted_tools) == 1
    assert hosted_tools[0].vector_store_ids == ["vs_research"]


def test_chief_of_staff_attaches_file_search_from_global_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_file_search_env(monkeypatch)
    monkeypatch.setattr(file_search, "FileSearchTool", FakeFileSearchTool)
    monkeypatch.setenv(file_search.GLOBAL_VECTOR_STORE_IDS_ENV, "vs_ops")

    agent = build_chief_of_staff_agent()

    assert "file_search" in {getattr(tool, "name", "") for tool in agent.tools}


def test_orchestrator_attaches_file_search_from_agent_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_file_search_env(monkeypatch)
    monkeypatch.setattr(file_search, "FileSearchTool", FakeFileSearchTool)
    monkeypatch.setenv("KEYSTONE_ORCHESTRATOR_FILE_SEARCH_VECTOR_STORE_IDS", "vs_router")

    agent = build_orchestrator_agent(include_handoffs=False)

    hosted_tools = [
        tool for tool in agent.tools if getattr(tool, "name", "") == "file_search"
    ]
    assert len(hosted_tools) == 1
    assert hosted_tools[0].vector_store_ids == ["vs_router"]
