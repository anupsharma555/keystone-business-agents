from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from keystone_agents.context_env import context_env_path
from keystone_agents.schemas.operational_context import AirtableContextResult
from keystone_agents.tools import internal_data_tools, local_context_tool
from keystone_agents.tools.internal_data_tools import (
    airtable_get_base_schema,
    airtable_read_records,
    google_doc_read,
    google_drive_get_file_metadata,
    google_drive_list_folder,
    google_drive_search_files,
    google_sheet_list,
    google_sheet_read_table,
)
from keystone_agents.tools.zotero_context_tools import (
    project_zotero_item_metadata,
    read_latest_zotero_journal_abstract_metadata,
    read_latest_zotero_journal_metadata,
    zotero_list_cached_items,
    zotero_read_api_metadata,
    zotero_read_item_children,
    zotero_read_pdf_attachment_text,
    zotero_resolve_collection_context,
)


def _loads(value: str) -> dict[str, object]:
    payload = json.loads(value)
    assert isinstance(payload, dict)
    return payload


def test_airtable_context_read_tools_return_bounded_context_packets() -> None:
    schema = _loads(airtable_get_base_schema(base_name="2026 Finance & Tax Tracker"))
    records = _loads(
        airtable_read_records(
            table="Eval tracker",
            base_alias="eval_tracker",
            view="Grid view",
            filter_formula="{Promptfoo case id} = 'case_1'",
            max_records=7,
        )
    )

    assert schema["status"] == "dry-run"
    assert schema["send_enabled"] is False
    assert schema["request"]["method"] == "GET"  # type: ignore[index]
    assert schema["schema"]["base_name"] == "2026 Finance & Tax Tracker"  # type: ignore[index]
    assert schema["schema"]["allowed_tables"]  # type: ignore[index]
    assert schema["schema"]["missing_allowed_tables"]  # type: ignore[index]
    assert "No Airtable metadata API call was made." in schema["audit_notes"]  # type: ignore[operator]

    assert records["status"] == "dry-run"
    assert records["send_enabled"] is False
    assert records["request"]["table"] == "Eval tracker"  # type: ignore[index]
    assert records["request"]["params"]["maxRecords"] == 7  # type: ignore[index]
    assert records["request"]["params"]["view"] == "Grid view"  # type: ignore[index]
    assert records["request"]["params"]["filterByFormula"] == (  # type: ignore[index]
        "{Promptfoo case id} = 'case_1'"
    )


def test_airtable_read_tools_honor_live_read_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, bool] = {}

    def fake_schema_impl(**kwargs: object) -> dict[str, object]:
        captured["schema"] = bool(kwargs.get("live"))
        return {"status": "captured", "send_enabled": False}

    def fake_records_impl(*args: object, **kwargs: object) -> dict[str, object]:
        captured["records"] = bool(kwargs.get("live"))
        return {"status": "captured", "send_enabled": False}

    monkeypatch.setattr(internal_data_tools, "airtable_get_base_schema_impl", fake_schema_impl)
    monkeypatch.setattr(internal_data_tools, "airtable_read_records_impl", fake_records_impl)
    monkeypatch.setenv(internal_data_tools.AIRTABLE_LIVE_READS_ENV, "true")

    _loads(airtable_get_base_schema(base_name="2026 Finance & Tax Tracker"))
    _loads(airtable_read_records(table="Business Income", max_records=1))

    assert captured == {"schema": True, "records": True}


def test_airtable_context_finance_base_name_uses_finance_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AIRTABLE_BASE_ID", "appKniOps")
    monkeypatch.setenv("AIRTABLE_ALLOWED_TABLES", "Ops Queue,Automation Review")
    monkeypatch.setenv("AIRTABLE_FINANCE_TAX_TRACKER_BASE_ID", "appFinance")
    monkeypatch.setenv("AIRTABLE_FINANCE_TAX_TRACKER_ALLOWED_TABLES", "Tax Payments")

    schema = _loads(
        airtable_get_base_schema(
            base_name="2026 Finance & Tax Tracker",
            live=False,
        )
    )
    records = _loads(airtable_read_records(table="Tax Payments", live=False))
    resolved = internal_data_tools._airtable_base_config(base_name="2026 Finance & Tax Tracker")

    assert schema["schema"]["base_id"] == "appFinance"  # type: ignore[index]
    assert schema["schema"]["allowed_tables"] == ["Tax Payments"]  # type: ignore[index]
    assert resolved["base_id"] == "appFinance"
    assert records["request"]["url"] == "https://api.airtable.com/v0/<base_id>/Tax%20Payments"  # type: ignore[index]
    assert records["request"]["table"] == "Tax Payments"  # type: ignore[index]


def test_airtable_context_result_preserves_bounded_record_summaries() -> None:
    result = AirtableContextResult(
        summary="Period 2 Tax Payments were visible.",
        record_summaries=[
            {
                "key": "IRS Estimated Taxes: Q2 2026 (pending)",
                "value": "Tax Type: Federal; Amount: $4,705.00; Payment Date: 6/1/2026",
                "note": "Period 2",
            }
        ],
    )

    assert result.record_summaries[0].key == "IRS Estimated Taxes: Q2 2026 (pending)"
    assert result.record_summaries[0].value == (
        "Tax Type: Federal; Amount: $4,705.00; Payment Date: 6/1/2026"
    )
    assert result.record_summaries[0].note == "Period 2"


def test_context_read_tools_can_use_linked_slack_env_without_copying_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    slack_repo = tmp_path / "keystone-slack"
    local_dir = slack_repo / ".local"
    local_dir.mkdir(parents=True)
    workspace_token = local_dir / "google-workspace-oauth-token.json"
    workspace_token.write_text("{}", encoding="utf-8")
    (slack_repo / ".env").write_text(
        "\n".join(
            [
                "AIRTABLE_BASE_ID=appSlackShared",
                "AIRTABLE_ACCESS_TOKEN=pat_secret_shared",
                "AIRTABLE_ALLOWED_TABLES=Business Income,Business Expenses",
                "GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH=.local/google-workspace-oauth-token.json",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("KEYSTONE_CONTEXT_CONFIG_REPO", str(slack_repo))
    monkeypatch.setenv(
        "KEYSTONE_CONTEXT_CONFIG_OVERRIDE_KEYS",
        "AIRTABLE_BASE_ID,AIRTABLE_ACCESS_TOKEN,GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH",
    )
    monkeypatch.setenv("AIRTABLE_BASE_ID", "appStaleLocal")
    monkeypatch.setenv("AIRTABLE_ACCESS_TOKEN", "pat_stale_local")
    monkeypatch.delenv("AIRTABLE_ALLOWED_TABLES", raising=False)

    schema = _loads(airtable_get_base_schema(live=False))
    records = _loads(airtable_read_records(table="Business Income", live=False))
    token_path = context_env_path("GOOGLE_WORKSPACE_OAUTH_TOKEN_PATH")

    assert schema["schema"]["base_id"] == "appSlackShared"  # type: ignore[index]
    assert records["request"]["table"] == "Business Income"  # type: ignore[index]
    assert "pat_secret_shared" not in json.dumps(schema)
    assert "pat_secret_shared" not in json.dumps(records)
    assert token_path == workspace_token


def test_google_workspace_context_read_tools_return_drive_docs_sheets_metadata() -> None:
    folder = _loads(google_drive_list_folder(folder_path="", max_items=12))
    search = _loads(
        google_drive_search_files(
            query="eval diagram",
            folder_path="Research",
            mime_type="image/",
            max_items=9,
        )
    )
    metadata = _loads(
        google_drive_get_file_metadata(
            file_id_or_url="https://drive.google.com/file/d/file123/view",
            folder_path="Research",
        )
    )
    doc = _loads(
        google_doc_read(
            document_id_or_url="https://docs.google.com/document/d/doc123/edit",
            folder_path="Research",
            max_chars=1500,
        )
    )
    sheet = _loads(
        google_sheet_read_table(
            spreadsheet_id_or_url="https://docs.google.com/spreadsheets/d/sheet123/edit",
            folder_path="Research",
            sheet_name="Eval Runs",
            range_a1="Eval Runs!A1:D25",
            max_rows=25,
        )
    )

    assert folder["status"] == "dry-run"
    assert folder["send_enabled"] is False
    assert folder["folder_path"] == "KNIOps"
    assert folder["max_items"] == 12

    assert search["status"] == "dry-run"
    assert search["query"] == "eval diagram"
    assert search["mime_type"] == "image/"
    assert "does not download file bytes" in " ".join(search["notes"])  # type: ignore[arg-type]

    assert metadata["status"] == "dry-run"
    assert metadata["file_id"] == "file123"
    assert "imageMediaMetadata" in metadata["metadata_fields"]  # type: ignore[operator]
    assert "does not download file bytes" in " ".join(metadata["notes"])  # type: ignore[arg-type]

    assert doc["status"] == "dry-run"
    assert doc["document_id"] == "doc123"
    assert doc["folder_path"] == "KNIOps / Research"
    assert doc["max_chars"] == 1500

    assert sheet["status"] == "dry-run"
    assert sheet["spreadsheet_id"] == "sheet123"
    assert sheet["sheet_name"] == "Eval Runs"
    assert sheet["range"] == "Eval Runs!A1:D25"
    assert sheet["rows"] == []
    assert sheet["send_enabled"] is False


def test_google_workspace_read_tools_honor_live_read_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, bool] = {}

    def fake_folder_impl(*args: object, live: bool = False, **kwargs: object) -> dict[str, object]:
        captured["folder"] = live
        return {"status": "captured", "send_enabled": False}

    def fake_search_impl(*args: object, live: bool = False, **kwargs: object) -> dict[str, object]:
        captured["search"] = live
        return {"status": "captured", "send_enabled": False}

    def fake_metadata_impl(*args: object, live: bool = False, **kwargs: object) -> dict[str, object]:
        captured["metadata"] = live
        return {"status": "captured", "send_enabled": False}

    def fake_doc_impl(*args: object, live: bool = False, **kwargs: object) -> dict[str, object]:
        captured["doc"] = live
        return {"status": "captured", "send_enabled": False}

    def fake_sheet_list_impl(*args: object, live: bool = False, **kwargs: object) -> dict[str, object]:
        captured["sheet_list"] = live
        return {"status": "captured", "send_enabled": False}

    def fake_sheet_read_impl(*args: object, live: bool = False, **kwargs: object) -> dict[str, object]:
        captured["sheet_read"] = live
        return {"status": "captured", "send_enabled": False}

    monkeypatch.setattr(internal_data_tools, "google_drive_list_folder_impl", fake_folder_impl)
    monkeypatch.setattr(internal_data_tools, "google_drive_search_files_impl", fake_search_impl)
    monkeypatch.setattr(internal_data_tools, "google_drive_get_file_metadata_impl", fake_metadata_impl)
    monkeypatch.setattr(internal_data_tools, "google_doc_read_impl", fake_doc_impl)
    monkeypatch.setattr(internal_data_tools, "google_sheet_list_impl", fake_sheet_list_impl)
    monkeypatch.setattr(internal_data_tools, "google_sheet_read_table_impl", fake_sheet_read_impl)
    monkeypatch.setenv(internal_data_tools.GOOGLE_WORKSPACE_LIVE_READS_ENV, "true")

    _loads(google_drive_list_folder())
    _loads(google_drive_search_files())
    _loads(google_drive_get_file_metadata("file123"))
    _loads(google_doc_read("doc123"))
    _loads(google_sheet_list())
    _loads(google_sheet_read_table())

    assert captured == {
        "folder": True,
        "search": True,
        "metadata": True,
        "doc": True,
        "sheet_list": True,
        "sheet_read": True,
    }


def test_zotero_context_read_tools_return_collection_and_api_handoff_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "zotero-cache"
    cache_dir.mkdir()
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps(
            {
                "collections": {
                    "KNI Collections - Behavioral Health AI Validation": "COLL1",
                }
            }
        ),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "key": "ITEM1",
                        "data": {
                            "key": "ITEM1",
                            "title": "Measurement-based care AI evaluation",
                            "creators": [
                                {
                                    "creatorType": "author",
                                    "firstName": "A.",
                                    "lastName": "Researcher",
                                }
                            ],
                            "date": "2026",
                            "DOI": "10.1000/context",
                            "url": "https://example.org/context-paper",
                            "abstractNote": (
                                "A validation study for behavioral health AI measurement workflows."
                            ),
                            "itemType": "journalArticle",
                            "collections": ["COLL1"],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_CACHE", str(cache_dir))

    listed = _loads(zotero_list_cached_items(limit=1))
    assert listed["status"] == "success"
    assert listed["item_count"] == 1
    assert listed["items"] == [
        {
            "item_key": "ITEM1",
            "title": "Measurement-based care AI evaluation",
            "item_type": "journalArticle",
            "date": "2026",
            "doi": "10.1000/context",
            "url": "https://example.org/context-paper",
        }
    ]

    collection = _loads(
        zotero_resolve_collection_context(
            "KNI Collections",
            research_goal="prepare Chief of Staff context handoff",
        )
    )
    api = _loads(
        zotero_read_api_metadata(
            library_id="12345",
            library_type="user",
            collection_key="COLL1",
            query="measurement-based care",
            limit=5,
            live=False,
        )
    )

    assert collection["status"] == "success"
    assert collection["target_type"] == "zotero_collection"
    assert collection["target_name"] == "KNI Collections - Behavioral Health AI Validation"
    assert collection["source_ids_used"] == ["zotero:item:ITEM1"]
    assert collection["article_summaries"][0]["title"] == (  # type: ignore[index]
        "Measurement-based care AI evaluation"
    )
    assert collection["send_enabled"] is False
    assert collection["zotero_write_supported"] is False

    assert api["status"] == "dry-run"
    assert api["planned_path"] == "/users/12345/collections/COLL1/items"
    assert api["params"] == {"limit": 5, "q": "measurement-based care"}
    assert "collection items" in api["supported_reads"]  # type: ignore[operator]
    assert api["send_enabled"] is False
    assert api["zotero_write_supported"] is False


def test_zotero_api_metadata_can_plan_latest_top_level_item_read() -> None:
    api = _loads(
        zotero_read_api_metadata(
            library_id="12345",
            library_type="user",
            limit=5,
            sort="dateAdded",
            direction="desc",
            top_level_only=True,
            item_type="journalArticle",
            require_abstract=True,
            live=False,
        )
    )

    assert api["status"] == "dry-run"
    assert api["planned_path"] == "/users/12345/items/top"
    assert api["params"] == {
        "limit": 5,
        "sort": "dateAdded",
        "direction": "desc",
        "itemType": "journalArticle",
    }
    assert api["selection_rule"] == "first_nonempty_abstract_in_provider_order"
    assert api["provider_order"] == {
        "sort": "dateAdded",
        "direction": "desc",
        "top_level_only": True,
        "item_type": "journalArticle",
    }
    assert api["require_abstract"] is True
    assert api["send_enabled"] is False


def test_zotero_api_metadata_selects_first_ordered_item_with_stored_abstract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                [
                    {
                        "key": "NO_ABSTRACT",
                        "data": {
                            "title": "Newest without an abstract",
                            "dateAdded": "2026-07-13T12:00:00Z",
                            "abstractNote": "",
                        },
                    },
                    {
                        "key": "WITH_ABSTRACT",
                        "data": {
                            "title": "Newest article with a stored abstract",
                            "dateAdded": "2026-07-12T12:00:00Z",
                            "abstractNote": "Stored abstract evidence.",
                        },
                    },
                ]
            ).encode("utf-8")

    captured: dict[str, object] = {}

    def _urlopen(request: object, timeout: int) -> _Response:
        captured["request"] = request
        captured["timeout"] = timeout
        return _Response()

    monkeypatch.setenv("ZOTERO_API_KEY", "test-key")
    monkeypatch.setattr(
        "keystone_agents.tools.zotero_context_tools.urlopen",
        _urlopen,
    )

    api = _loads(
        zotero_read_api_metadata(
            library_id="12345",
            sort="dateAdded",
            direction="desc",
            top_level_only=True,
            item_type="journalArticle",
            require_abstract=True,
            live=True,
        )
    )

    assert api["status"] == "success"
    assert api["provider_read"] is True
    assert api["item_count"] == 1
    assert api["selected_item_title"] == "Newest article with a stored abstract"
    assert api["selected_item_has_abstract"] is True
    assert api["selected_item_date_added"] == "2026-07-12T12:00:00Z"
    assert api["items"][0]["key"] == "WITH_ABSTRACT"  # type: ignore[index]
    assert captured["timeout"] == 30


def test_zotero_api_metadata_recovers_stale_configured_user_library_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _Response:
        def __init__(self, payload: object) -> None:
            self.payload = payload

        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(self.payload).encode("utf-8")

    def _urlopen(request: Request, timeout: int) -> _Response:
        assert timeout == 30
        url = request.full_url
        calls.append(url)
        if "/users/stale/items" in url:
            raise HTTPError(url, 403, "Forbidden", hdrs=None, fp=None)
        if url.endswith("/keys/current"):
            return _Response(
                {
                    "userID": 24680,
                    "access": {"user": {"library": True, "write": False}},
                }
            )
        assert "/users/24680/items" in url
        return _Response(
            [
                {
                    "key": "ITEM1",
                    "data": {"title": "Authorized library item", "abstractNote": "Evidence."},
                }
            ]
        )

    monkeypatch.setenv("ZOTERO_API_KEY", "test-key")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "stale")
    monkeypatch.setattr("keystone_agents.tools.zotero_context_tools.urlopen", _urlopen)

    payload = _loads(zotero_read_api_metadata(live=True))

    assert payload["status"] == "success"
    assert payload["selected_item_title"] == "Authorized library item"
    assert payload["library_id_resolution"] == (
        "api_key_current_user_after_configured_403"
    )
    assert len(calls) == 3


def test_zotero_api_metadata_does_not_redirect_explicit_or_group_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def _urlopen(request: Request, timeout: int) -> object:
        assert timeout == 30
        url = request.full_url
        calls.append(url)
        raise HTTPError(url, 403, "Forbidden", hdrs=None, fp=None)

    monkeypatch.setenv("ZOTERO_API_KEY", "test-key")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "configured")
    monkeypatch.setattr("keystone_agents.tools.zotero_context_tools.urlopen", _urlopen)

    with pytest.raises(HTTPError):
        zotero_read_api_metadata(library_id="explicit", live=True)
    with pytest.raises(HTTPError):
        zotero_read_api_metadata(library_type="group", live=True)

    assert len(calls) == 2
    assert not any("/keys/current" in url for url in calls)


def test_zotero_api_metadata_requires_verified_user_library_access_on_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps(
                {"userID": 24680, "access": {"user": {"library": False}}}
            ).encode("utf-8")

    def _urlopen(request: Request, timeout: int) -> _Response:
        assert timeout == 30
        url = request.full_url
        if url.endswith("/keys/current"):
            return _Response()
        raise HTTPError(url, 403, "Forbidden", hdrs=None, fp=None)

    monkeypatch.setenv("ZOTERO_API_KEY", "test-key")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "stale")
    monkeypatch.setattr("keystone_agents.tools.zotero_context_tools.urlopen", _urlopen)

    with pytest.raises(RuntimeError, match="cannot read its user library"):
        zotero_read_api_metadata(live=True)


def test_zotero_api_metadata_rejects_unbounded_sort_controls() -> None:
    with pytest.raises(ValueError, match="sort must be one of"):
        zotero_read_api_metadata(library_id="12345", sort="arbitrary", live=False)

    with pytest.raises(ValueError, match="direction requires"):
        zotero_read_api_metadata(library_id="12345", direction="desc", live=False)


def test_latest_zotero_abstract_helper_uses_exact_provider_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_read(**kwargs: object) -> str:
        captured.update(kwargs)
        return json.dumps({"status": "success", "provider_read": True, "items": []})

    monkeypatch.setattr(
        "keystone_agents.tools.zotero_context_tools.zotero_read_api_metadata",
        fake_read,
    )

    payload = read_latest_zotero_journal_abstract_metadata()

    assert payload["provider_read"] is True
    assert captured == {
        "limit": 100,
        "sort": "dateAdded",
        "direction": "desc",
        "top_level_only": True,
        "item_type": "journalArticle",
        "require_abstract": True,
        "live": True,
    }


def test_latest_zotero_journal_helper_does_not_require_abstract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_read(**kwargs: object) -> str:
        captured.update(kwargs)
        return json.dumps({"status": "success", "provider_read": True, "items": []})

    monkeypatch.setattr(
        "keystone_agents.tools.zotero_context_tools.zotero_read_api_metadata",
        fake_read,
    )

    payload = read_latest_zotero_journal_metadata()

    assert payload["provider_read"] is True
    assert captured == {
        "limit": 1,
        "sort": "dateAdded",
        "direction": "desc",
        "top_level_only": True,
        "item_type": "journalArticle",
        "require_abstract": False,
        "live": True,
    }

    with pytest.raises(ValueError, match="item_type must be one of"):
        zotero_read_api_metadata(library_id="12345", item_type="attachment", live=False)


def test_zotero_metadata_projection_preserves_requested_provider_fields() -> None:
    projection = project_zotero_item_metadata(
        {
            "key": "ITEM1",
            "version": 7,
            "data": {
                "itemType": "journalArticle",
                "title": "A structured article",
                "creators": [{"firstName": "Ada", "lastName": "Lovelace"}],
                "publicationTitle": "Journal of Structured Context",
                "abstractNote": "Stored abstract.",
                "DOI": "10.1000/example",
            },
        },
        request_text="Return the title, authors, publication title, abstract, and DOI.",
    )

    assert projection["fields"] == {
        "title": "A structured article",
        "authors": ["Ada Lovelace"],
        "abstract": "Stored abstract.",
        "publication_title": "Journal of Structured Context",
        "doi": "10.1000/example",
    }
    assert projection["missing_requested_fields"] == []
    assert projection["provider_field_map"]["authors"] == "creators"
    assert projection["provider_field_map"]["publication_title"] == "publicationTitle"


def test_zotero_broad_metadata_projection_retains_bounded_unknown_provider_fields() -> None:
    projection = project_zotero_item_metadata(
        {
            "key": "ITEM1",
            "data": {
                "itemType": "journalArticle",
                "title": "A structured article",
                "customFutureField": "Provider value",
            },
        },
        request_text="Return all available metadata for this article.",
    )

    assert "customFutureField" in projection["available_provider_field_names"]
    assert projection["provider_fields"]["customFutureField"] == "Provider value"


def test_zotero_item_children_dry_run_is_parent_scoped() -> None:
    payload = _loads(zotero_read_item_children(parent_item_key="PARENT1", live=False))

    assert payload["status"] == "dry-run"
    assert payload["parent_item_key"] == "PARENT1"
    assert payload["supported_child_types"] == ["note", "attachment"]
    assert payload["zotero_write_supported"] is False


def test_zotero_item_children_projects_notes_and_pdf_attachments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZOTERO_API_KEY", "test-key")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "123")
    monkeypatch.setattr(
        "keystone_agents.tools.zotero_context_tools._read_zotero_api_json",
        lambda *_args, **_kwargs: [
            {
                "key": "NOTE1",
                "data": {
                    "itemType": "note",
                    "parentItem": "PARENT1",
                    "note": "<p>Important stored note.</p>",
                },
            },
            {
                "key": "PDF1",
                "data": {
                    "itemType": "attachment",
                    "parentItem": "PARENT1",
                    "filename": "article.pdf",
                    "contentType": "application/pdf",
                },
            },
        ],
    )

    payload = _loads(zotero_read_item_children(parent_item_key="PARENT1", live=True))

    assert payload["status"] == "success"
    assert payload["note_count"] == 1
    assert payload["attachment_count"] == 1
    assert payload["children"][0]["note"] == "<p>Important stored note.</p>"  # type: ignore[index]
    assert payload["children"][0]["note_text"] == "Important stored note."  # type: ignore[index]
    assert payload["children"][1]["item_key"] == "PDF1"  # type: ignore[index]


def test_zotero_pdf_read_verifies_parent_and_returns_bounded_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Page:
        def __init__(self, text: str) -> None:
            self._text = text

        def extract_text(self) -> str:
            return self._text

    class _Reader:
        def __init__(self, _stream: object) -> None:
            self.pages = [_Page("First page."), _Page("Second page.")]

    monkeypatch.setenv("ZOTERO_API_KEY", "test-key")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "123")
    monkeypatch.setattr(
        "keystone_agents.tools.zotero_context_tools._read_zotero_api_json",
        lambda *_args, **_kwargs: {
            "data": {
                "itemType": "attachment",
                "parentItem": "PARENT1",
                "filename": "article.pdf",
                "contentType": "application/pdf",
            }
        },
    )
    monkeypatch.setattr(
        "keystone_agents.tools.zotero_context_tools._read_zotero_api_bytes",
        lambda *_args, **_kwargs: (b"%PDF-test", "application/pdf"),
    )
    fake_pypdf = ModuleType("pypdf")
    fake_pypdf.PdfReader = _Reader  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pypdf", fake_pypdf)

    payload = _loads(
        zotero_read_pdf_attachment_text(
            parent_item_key="PARENT1",
            attachment_item_key="PDF1",
            max_pages=1,
            max_chars=1000,
            live=True,
        )
    )

    assert payload["status"] == "success"
    assert payload["text"] == "First page."
    assert payload["page_count"] == 2
    assert payload["pages_read"] == 1
    assert payload["truncated"] is True
    assert payload["file_persisted"] is False


def test_zotero_pdf_read_rejects_parent_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZOTERO_API_KEY", "test-key")
    monkeypatch.setenv("ZOTERO_LIBRARY_ID", "123")
    monkeypatch.setattr(
        "keystone_agents.tools.zotero_context_tools._read_zotero_api_json",
        lambda *_args, **_kwargs: {
            "data": {
                "itemType": "attachment",
                "parentItem": "OTHER",
                "filename": "article.pdf",
                "contentType": "application/pdf",
            }
        },
    )

    with pytest.raises(RuntimeError, match="does not belong"):
        zotero_read_pdf_attachment_text(
            parent_item_key="PARENT1",
            attachment_item_key="PDF1",
            live=True,
        )


def test_zotero_context_can_resolve_kni_cache_from_linked_slack_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    slack_repo = tmp_path / "keystone-slack"
    cache_dir = slack_repo / ".local"
    cache_dir.mkdir(parents=True)
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps({"collections": {"KNI Collections": "COLL1"}}),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "key": "ITEM1",
                        "data": {
                            "key": "ITEM1",
                            "title": "KNI collection context paper",
                            "date": "2026",
                            "collections": ["COLL1"],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (slack_repo / ".env").write_text(
        "ZOTERO_COLLECTION_CACHE=.local/zotero_collections.json\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_CONTEXT_CONFIG_REPO", str(slack_repo))
    monkeypatch.delenv("KEYSTONE_ZOTERO_IMPORT_CACHE", raising=False)
    monkeypatch.delenv("ZOTERO_COLLECTION_CACHE", raising=False)

    collection = _loads(
        zotero_resolve_collection_context(
            "KNI Collections",
            research_goal="prepare Chief of Staff context handoff",
        )
    )

    assert collection["status"] == "success"
    assert collection["target_name"] == "KNI Collections"
    assert collection["source_ids_used"] == ["zotero:item:ITEM1"]


def test_zotero_context_resolves_generic_kni_collection_from_import_repo_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import_repo = tmp_path / "zotero-import"
    cache_dir = import_repo / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps(
            {
                "collections": {
                    "CHAI 01 - AI Governance": "OTHER1",
                    "KNI 00 - Foundational Texts & Reviews": "KNI00",
                    "KNI 10 - AI, Digital Phenotyping & Psychiatric Monitoring": "KNI10",
                }
            }
        ),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "key": "ITEM1",
                        "data": {
                            "key": "ITEM1",
                            "title": "Foundational KNI review",
                            "date": "2026",
                            "collections": ["KNI00"],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_REPO", str(import_repo))
    monkeypatch.delenv("KEYSTONE_ZOTERO_IMPORT_CACHE", raising=False)
    monkeypatch.delenv("ZOTERO_COLLECTION_CACHE", raising=False)
    monkeypatch.delenv("ZOTERO_ITEMS_CACHE", raising=False)

    collection = _loads(
        zotero_resolve_collection_context(
            "default KNI collection",
            research_goal="diagnostic context handoff",
        )
    )

    assert collection["status"] == "success"
    assert collection["target_name"] == "KNI 00 - Foundational Texts & Reviews"
    assert collection["source_ids_used"] == ["zotero:item:ITEM1"]
    assert "Foundational Texts" in str(collection["summary"])
    assert "Lindus/Sooma" not in str(collection["summary"])


def test_zotero_context_resolves_foundational_texts_reviews_alias(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import_repo = tmp_path / "zotero-import"
    cache_dir = import_repo / ".cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps(
            {
                "collections": {
                    "KNI 00 - Foundational Texts & Reviews": "KNI00",
                    "KNI 10 - AI, Digital Phenotyping & Psychiatric Monitoring": "KNI10",
                }
            }
        ),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "key": "ITEM1",
                        "data": {
                            "key": "ITEM1",
                            "title": "Digital psychiatry and psychiatric diagnosis review",
                            "itemType": "journalArticle",
                            "collections": ["KNI00"],
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_REPO", str(import_repo))
    monkeypatch.delenv("KEYSTONE_ZOTERO_IMPORT_CACHE", raising=False)
    monkeypatch.delenv("ZOTERO_COLLECTION_CACHE", raising=False)

    collection = _loads(
        zotero_resolve_collection_context(
            "KNI foundational texts/reviews collection",
            research_goal="Find digital psychiatry, depression, and diagnosis background sources.",
        )
    )

    assert collection["status"] == "success"
    assert collection["target_name"] == "KNI 00 - Foundational Texts & Reviews"
    assert collection["source_ids_used"] == ["zotero:item:ITEM1"]


def test_local_context_lists_default_zotero_import_cache_source(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import_repo = tmp_path / "zotero-import"
    cache_dir = import_repo / ".cache"
    cache_dir.mkdir(parents=True)
    monkeypatch.setattr(local_context_tool, "DEFAULT_LOCAL_ZOTERO_IMPORT_REPO", import_repo)
    monkeypatch.delenv("KEYSTONE_ZOTERO_IMPORT_CACHE", raising=False)
    monkeypatch.delenv("ZOTERO_COLLECTION_CACHE", raising=False)
    monkeypatch.delenv("KEYSTONE_ZOTERO_IMPORT_REPO", raising=False)
    monkeypatch.delenv("KEYSTONE_LOCAL_CONTEXT_SOURCES_JSON", raising=False)
    monkeypatch.delenv("KEYSTONE_LOCAL_CONTEXT_SOURCES", raising=False)

    payload = _loads(local_context_tool.list_local_context_sources())
    by_id = {
        str(source["source_id"]): source
        for source in payload["sources"]  # type: ignore[index]
        if isinstance(source, dict)
    }

    assert by_id["zotero_import_cache"]["path"] == str(cache_dir)
    assert by_id["zotero_import_cache"]["exists"] is True
    assert by_id["zotero_import_cache"]["readable"] is True
