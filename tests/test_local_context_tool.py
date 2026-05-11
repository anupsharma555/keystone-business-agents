from __future__ import annotations

import json

import pytest

from keystone_agents.tools.local_context_tool import (
    list_local_context_sources,
    read_local_context_file,
    search_local_context,
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
