from __future__ import annotations

import importlib.util
import json
from fnmatch import fnmatch
from pathlib import Path
from types import ModuleType

import pytest

from keystone_agents.file_search_corpus import (
    build_corpus_upload_plan,
    corpus_upload_plan_summary,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = PROJECT_ROOT / "docs/corpus/seed_manifest.json"
INGEST_SCRIPT_PATH = PROJECT_ROOT / "scripts/ingest_file_search_corpus.py"


def _load_manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text())


def _manifest_local_files(manifest: dict) -> set[str]:
    return {path for corpus in manifest["corpora"] for path in corpus.get("local_files", [])}


def _load_ingest_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "ingest_file_search_corpus",
        INGEST_SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_file_search_corpus_manifest_paths_exist_and_avoid_blocked_patterns() -> None:
    manifest = _load_manifest()
    local_files = _manifest_local_files(manifest)

    assert local_files
    for rel_path in local_files:
        path = Path(rel_path)
        assert not path.is_absolute(), rel_path
        assert (PROJECT_ROOT / path).is_file(), rel_path
        for blocked_pattern in manifest["blocked_patterns"]:
            assert not fnmatch(rel_path, blocked_pattern), rel_path


def test_vendored_source_entries_are_attributed_and_listed_for_ingestion() -> None:
    manifest = _load_manifest()
    listed_files = _manifest_local_files(manifest)
    reference_corpus = manifest["corpora"][0]

    vendored_sources = reference_corpus["external_sources_vendored"]
    assert vendored_sources
    for source in vendored_sources:
        assert source["source_url"].startswith("https://")
        assert source["license"]
        assert source["retrieved_at"] == "2026-05-14"
        assert source["local_files"]
        for rel_path in source["local_files"]:
            assert rel_path in listed_files
            assert (PROJECT_ROOT / rel_path).is_file()


def test_public_vendor_corpus_contains_only_public_vendor_files() -> None:
    manifest = _load_manifest()
    public_corpus = next(
        corpus
        for corpus in manifest["corpora"]
        if corpus["name"] == "keystone-business-agents-public-vendor-reference"
    )

    assert public_corpus["sensitivity"] == "public_reference"
    assert public_corpus["local_files"]
    assert all(
        rel_path.startswith("docs/corpus/vendor/") for rel_path in public_corpus["local_files"]
    )


def test_ingest_client_uses_keystone_openai_key_not_generic_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_ingest_script()
    captured: dict[str, str] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: str) -> None:
            captured.update(kwargs)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-xxxx")
    monkeypatch.setenv("KEYSTONE_OPENAI_BASE_URL", "https://keystone.example/v1")
    monkeypatch.setattr(module, "OpenAI", FakeOpenAI)

    module._build_openai_client(api_key="sk-keystone-repo-key")

    assert captured == {
        "api_key": "sk-keystone-repo-key",
        "base_url": "https://keystone.example/v1",
    }


def test_vendored_official_docs_contain_relevant_agent_and_integration_contracts() -> None:
    agents_tools = (PROJECT_ROOT / "docs/corpus/vendor/openai-agents-python/tools.md").read_text()
    agents_sessions = (
        PROJECT_ROOT / "docs/corpus/vendor/openai-agents-python/sessions-index.md"
    ).read_text()
    openai_openapi = (PROJECT_ROOT / "docs/corpus/vendor/openai-openapi/openapi.yaml").read_text()
    gmail_discovery = json.loads(
        (PROJECT_ROOT / "docs/corpus/vendor/google-gmail-api/gmail-v1-discovery.json").read_text()
    )
    slack_web_api = (
        PROJECT_ROOT
        / "docs/corpus/vendor/slack-api-specs/slack_web_openapi_v2_without_examples.json"
    ).read_text()
    langgraph_graph_api = (
        PROJECT_ROOT / "docs/corpus/vendor/langgraph-docs/graph-api.md"
    ).read_text()
    langgraph_persistence = (
        PROJECT_ROOT / "docs/corpus/vendor/langgraph-docs/persistence.md"
    ).read_text()
    langgraph_interrupts = (
        PROJECT_ROOT / "docs/corpus/vendor/langgraph-docs/interrupts.md"
    ).read_text()
    langgraph_notes = (
        PROJECT_ROOT / "docs/corpus/resources/langgraph_operational_notes.md"
    ).read_text()

    assert "FileSearchTool" in agents_tools
    assert "as_tool" in agents_tools
    assert "session" in agents_sessions.lower()
    assert "/vector_stores" in openai_openapi
    assert "create" in gmail_discovery["resources"]["users"]["resources"]["drafts"]["methods"]
    assert "chat.postMessage" in slack_web_api
    assert "StateGraph" in langgraph_graph_api
    assert "checkpoint" in langgraph_persistence.lower()
    assert "interrupt" in langgraph_interrupts.lower()
    assert "WorkItem" in langgraph_notes


def test_corpus_upload_plan_builds_file_attributes_for_vector_store_ingestion() -> None:
    plan = build_corpus_upload_plan(
        project_root=PROJECT_ROOT,
        manifest_path=MANIFEST_PATH,
        corpus_name="keystone-business-agents-reference",
    )

    summary = corpus_upload_plan_summary(plan)

    assert summary["file_count"] == len(plan.files)
    assert summary["total_bytes"] > 4_000_000
    assert summary["corpora"] == ["keystone-business-agents-reference"]
    assert any(item.source_url for item in plan.files)
    assert any(item.license == "MIT" for item in plan.files)
    assert all(item.attributes["source_path"] == item.relative_path for item in plan.files)


def test_corpus_upload_plan_rejects_blocked_manifest_paths(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OPENAI_API_KEY=not-real")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "corpora": [
                    {
                        "name": "unsafe",
                        "approved_for": ["chief_of_staff"],
                        "sensitivity": "internal_reference",
                        "local_files": [".env"],
                    }
                ],
                "blocked_patterns": [".env"],
            }
        )
    )

    with pytest.raises(ValueError, match="Blocked FileSearch corpus path"):
        build_corpus_upload_plan(
            project_root=tmp_path,
            manifest_path=manifest_path,
            corpus_name="unsafe",
        )
