from __future__ import annotations

import json

import pytest

from keystone_agents.tools.local_context_tool import (
    list_local_context_sources,
    read_local_context_file,
    search_local_context,
)
from keystone_agents.tools.zotero_context_tools import (
    zotero_import_article_with_backend,
    zotero_read_api_metadata,
    zotero_resolve_collection_context,
)


def test_local_context_tools_list_configured_sources(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source_dir = tmp_path / "context"
    source_dir.mkdir()
    monkeypatch.setenv(
        "KEYSTONE_LOCAL_CONTEXT_SOURCES_JSON",
        json.dumps({"fixture_context": str(source_dir)}),
    )

    payload = json.loads(list_local_context_sources())
    by_id = {source["source_id"]: source for source in payload["sources"]}

    assert payload["send_enabled"] is False
    assert by_id["fixture_context"]["exists"] is True
    assert by_id["fixture_context"]["readable"] is True
    assert "zotero_app_support" not in by_id
    assert "zotero_reference_archive" not in by_id


def test_local_context_search_and_read_are_allowlisted_and_capped(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source_dir = tmp_path / "context"
    source_dir.mkdir()
    (source_dir / "notes.md").write_text(
        "Keystone neuroinformatics research operations note.\n" * 20,
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "KEYSTONE_LOCAL_CONTEXT_SOURCES_JSON",
        json.dumps({"fixture_context": str(source_dir)}),
    )

    search_payload = json.loads(
        search_local_context("research operations", source_ids=["fixture_context"])
    )
    read_payload = json.loads(read_local_context_file("fixture_context", "notes.md", max_chars=40))

    assert search_payload["send_enabled"] is False
    assert search_payload["raw_file_bodies_included"] is False
    assert search_payload["matches"][0]["relative_path"] == "notes.md"
    assert read_payload["send_enabled"] is False
    assert read_payload["truncated"] is True
    assert read_payload["content"] == "Keystone neuroinformatics research opera"


def test_local_context_read_rejects_path_escape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    source_dir = tmp_path / "context"
    source_dir.mkdir()
    monkeypatch.setenv(
        "KEYSTONE_LOCAL_CONTEXT_SOURCES_JSON",
        json.dumps({"fixture_context": str(source_dir)}),
    )

    with pytest.raises(ValueError, match="escapes"):
        read_local_context_file("fixture_context", "../outside.md")


def test_zotero_context_tools_resolve_local_cache_and_preview_api_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    cache_dir = tmp_path / "zotero-cache"
    cache_dir.mkdir()
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps({"collections": {"LH 01 - REACH-tDCS": "COLL1"}}),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "data": {
                            "key": "ITEM1",
                            "title": "Remote tDCS randomized trial",
                            "url": "https://pubmed.ncbi.nlm.nih.gov/example/",
                            "DOI": "10.1000/example",
                            "abstractNote": (
                                "This randomized sham-controlled trial tested home-based "
                                "tDCS for major depressive disorder."
                            ),
                            "itemType": "journalArticle",
                            "collections": ["COLL1"],
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_CACHE", str(cache_dir))

    collection_payload = json.loads(
        zotero_resolve_collection_context(
            "LH 01",
            research_goal="prepare Chief of Staff handoff",
        )
    )
    api_payload = json.loads(
        zotero_read_api_metadata(
            library_id="12345",
            collection_key="COLL1",
            query="tdcs",
            live=False,
        )
    )

    assert collection_payload["status"] == "success"
    assert collection_payload["target_type"] == "zotero_collection"
    assert collection_payload["source_ids_used"] == ["zotero:item:ITEM1"]
    assert collection_payload["send_enabled"] is False
    assert collection_payload["zotero_write_supported"] is False
    assert api_payload["status"] == "dry-run"
    assert api_payload["planned_path"] == "/users/12345/collections/COLL1/items"
    assert api_payload["params"]["q"] == "tdcs"
    assert api_payload["zotero_write_supported"] is False


def test_zotero_importer_wrapper_previews_and_blocks_unapproved_live_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    importer = tmp_path / "run_kni_zotero_importer.py"
    importer.write_text("#!/usr/bin/env python3\nprint('should not run')\n", encoding="utf-8")
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORTER_SCRIPT", str(importer))
    monkeypatch.delenv("KEYSTONE_ZOTERO_IMPORTER_ALLOW_WRITES", raising=False)

    preview = json.loads(
        zotero_import_article_with_backend(
            "https://pubmed.ncbi.nlm.nih.gov/example/",
            "LH 01 - REACH-tDCS",
            tags="trial,tdcs",
            note="For Chief of Staff synthesis",
            create_missing_collections=True,
            write=True,
            live=False,
        )
    )

    assert preview["status"] == "dry-run"
    assert preview["write_requested"] is True
    assert "--write" in preview["command"]
    assert "--create-missing-collections" in preview["command"]
    assert preview["send_enabled"] is False

    with pytest.raises(RuntimeError, match="approval_reference"):
        zotero_import_article_with_backend(
            "https://pubmed.ncbi.nlm.nih.gov/example/",
            "LH 01 - REACH-tDCS",
            write=True,
            live=True,
        )
