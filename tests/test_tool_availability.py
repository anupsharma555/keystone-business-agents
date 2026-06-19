from __future__ import annotations

import sqlite3
from pathlib import Path

from keystone_agents.source_layer_context import (
    runtime_source_layer_policy_context,
    runtime_source_layer_policy_text,
)
from keystone_agents.tool_availability import (
    local_kni_document_availability,
    mcp_availability_for_agent,
    runtime_tool_availability_for_agent,
    search_web_availability_for_agent,
    tool_search_availability_for_agent,
)
from keystone_agents.tools.kni_document_tool import (
    KNI_DOC_AUTO_REFRESH_ENV,
    KNI_DOC_INDEX_PATH_ENV,
    KNI_DOC_MODEL_CONTEXT_ALLOWED_ENV,
    KNI_DOC_ROOT_PATH_ENV,
    KNI_DOC_SEARCH_ENABLED_ENV,
)


def _init_index(index: Path) -> None:
    with sqlite3.connect(index) as connection:
        connection.execute(
            """
            CREATE TABLE local_documents (
                relative_path TEXT PRIMARY KEY,
                document_id TEXT NOT NULL,
                absolute_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_stem TEXT NOT NULL,
                extension TEXT NOT NULL,
                top_level_folder TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                modified_at TEXT NOT NULL,
                title TEXT NOT NULL,
                preview_text TEXT NOT NULL,
                searchable_path TEXT NOT NULL,
                searchable_content TEXT NOT NULL,
                indexed_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO local_documents (
                relative_path, document_id, absolute_path, file_name, file_stem,
                extension, top_level_folder, size_bytes, modified_at, title,
                preview_text, searchable_path, searchable_content, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "00_Admin/Formation/example.pdf",
                "doc_1",
                "/tmp/example.pdf",
                "example.pdf",
                "example",
                "pdf",
                "00_Admin",
                10,
                "2026-06-11T00:00:00+00:00",
                "Example",
                "Formation context",
                "00_admin formation example",
                "formation context",
                "2026-06-11T00:00:00+00:00",
            ),
        )


def test_search_web_availability_reports_live_gated_by_default() -> None:
    status = search_web_availability_for_agent("business_research_analyst", env={})

    assert status["available"] is True
    assert status["live_enabled"] is False
    assert status["live_available"] is False
    assert status["status"] == "attached_live_gated"
    assert status["provider_sequence"] == []


def test_search_web_availability_reports_default_live_ladder() -> None:
    env = {
        "KEYSTONE_LIVE_MODE": "true",
        "KEYSTONE_ENABLE_LIVE_RESEARCH": "true",
        "SEARXNG_BASE_URL": "http://127.0.0.1:18080",
        "KEYSTONE_OPENAI_API_KEY": "secret",
        "EXA_API_KEY": "secret",
    }

    status = search_web_availability_for_agent("business_research_analyst", env=env)

    assert status["live_enabled"] is True
    assert status["live_available"] is True
    assert status["status"] == "attached_live_available"
    assert status["provider_sequence"] == ["searxng", "agents-web-search", "exa"]
    assert status["provider_prerequisites"]["openai_api_key"] is True
    assert "secret" not in repr(status)


def test_search_web_availability_reports_live_misconfiguration() -> None:
    env = {
        "KEYSTONE_LIVE_MODE": "true",
        "KEYSTONE_ENABLE_LIVE_RESEARCH": "true",
        "SEARCH_PROVIDER": "tavily",
    }

    status = search_web_availability_for_agent("business_research_analyst", env=env)

    assert status["live_enabled"] is True
    assert status["live_available"] is False
    assert status["status"] == "attached_live_misconfigured"
    assert status["provider_sequence"] == ["tavily"]


def test_local_kni_document_availability_is_sanitized(tmp_path: Path) -> None:
    root = tmp_path / "KNI"
    root.mkdir()
    index = tmp_path / "kni-docs.sqlite"
    _init_index(index)

    status = local_kni_document_availability(
        env={
            KNI_DOC_SEARCH_ENABLED_ENV: "true",
            KNI_DOC_ROOT_PATH_ENV: str(root),
            KNI_DOC_INDEX_PATH_ENV: str(index),
            KNI_DOC_AUTO_REFRESH_ENV: "false",
            KNI_DOC_MODEL_CONTEXT_ALLOWED_ENV: "true",
        }
    )

    assert status["available"] is True
    assert status["status"] == "ready"
    assert status["indexed_count"] == 1
    assert status["local_only"] is True
    assert status["send_enabled"] is False
    assert "Formation context" not in repr(status)


def test_runtime_tool_availability_includes_explicit_gaps() -> None:
    status = runtime_tool_availability_for_agent(
        "chief_of_staff",
        tools=(
            "search_web",
            "list_kni_document_sources",
            "search_kni_documents",
            "read_kni_document_file",
        ),
        optional_tools=("file_search",),
        env={},
    )

    assert status["mcp"]["status"] in {
        "sdk_available_not_configured",
        "sdk_unavailable",
    }
    assert status["mcp"]["available"] is False
    assert status["tool_search"]["status"] in {
        "sdk_available_not_configured",
        "sdk_unavailable",
    }
    assert status["tool_search"]["available"] is False
    assert status["file_search"]["status"] == "not_configured"
    assert status["search_web"]["status"] == "attached_live_gated"
    assert "local_kni_documents" in status
    assert status["source_layer_policy"]["status"] == "declared"
    layers = {
        item["layer"]: item
        for item in status["source_layer_policy"]["layers"]
        if isinstance(item, dict)
    }
    assert set(layers) == {
        "hosted_file_search",
        "local_kni_documents",
        "public_web_search",
    }
    assert layers["hosted_file_search"]["runtime_status"] == "not_configured"
    assert layers["hosted_file_search"]["runtime_available"] is False
    assert layers["local_kni_documents"]["runtime_tool"] == "local_kni_documents"
    assert layers["public_web_search"]["runtime_status"] == "attached_live_gated"
    assert "latest user question" in repr(status["source_layer_policy"])


def test_mcp_and_tool_search_readiness_reports_sdk_support_without_config() -> None:
    mcp = mcp_availability_for_agent("orchestrator")
    tool_search = tool_search_availability_for_agent("orchestrator")

    assert mcp["configured"] is False
    assert mcp["available"] is False
    assert mcp["status"] in {"sdk_available_not_configured", "sdk_unavailable"}
    assert tool_search["configured"] is False
    assert tool_search["available"] is False
    assert tool_search["status"] in {
        "sdk_available_not_configured",
        "sdk_unavailable",
    }


def test_runtime_source_layer_policy_context_is_compact_and_status_aware() -> None:
    context = runtime_source_layer_policy_context(
        "chief_of_staff",
        env={
            "KEYSTONE_LIVE_MODE": "true",
            "KEYSTONE_ENABLE_LIVE_RESEARCH": "true",
            "SEARXNG_BASE_URL": "http://127.0.0.1:18080",
            "KEYSTONE_FILE_SEARCH_VECTOR_STORE_IDS": "vs_private",
            "KEYSTONE_KNI_DOC_SEARCH_ENABLED": "false",
        },
    )

    assert context["agent_name"] == "chief_of_staff"
    assert context["status"] == "declared"
    layers = {item["layer"]: item for item in context["layers"]}
    assert layers["hosted_file_search"]["runtime_status"] in {
        "available",
        "sdk_unavailable",
    }
    assert layers["hosted_file_search"]["runtime_configured"] is True
    assert layers["local_kni_documents"]["runtime_status"] == "disabled"
    assert layers["public_web_search"]["runtime_status"] == "attached_live_available"
    assert "vs_private" not in repr(context)


def test_runtime_source_layer_policy_text_includes_reasoning_contracts() -> None:
    text = runtime_source_layer_policy_text(
        "chief_of_staff",
        env={
            "KEYSTONE_LIVE_MODE": "true",
            "KEYSTONE_ENABLE_LIVE_RESEARCH": "true",
            "SEARXNG_BASE_URL": "http://127.0.0.1:18080",
            "KEYSTONE_FILE_SEARCH_VECTOR_STORE_IDS": "vs_private",
            "KEYSTONE_KNI_DOC_SEARCH_ENABLED": "true",
        },
    )

    assert "Runtime source-layer policy:" in text
    assert "hosted_file_search" in text
    assert "local_kni_documents" in text
    assert "reasoning_contract=" in text
    assert "latest user question" in text
    assert "organizer" in text
    assert "registered agent" in text
    assert "vs_private" not in text
