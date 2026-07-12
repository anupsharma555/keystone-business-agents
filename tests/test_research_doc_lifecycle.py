from __future__ import annotations

import pytest

import keystone_agents.research_doc_lifecycle as lifecycle
from keystone_agents.schemas.research import (
    ResearchBrief,
    ResearchBriefFact,
    ResearchSourceCitation,
)


def _focused_result() -> dict[str, object]:
    return {
        "output_type": "CompanyResearchFocusedBrief",
        "output": {
            "company_name": "Example Health",
            "product": "A source-backed clinical operations platform.",
            "customers": "Health organizations.",
            "why_it_matters": "Relevant to clinical AI evaluation work.",
            "facts": [
                {"text": "The company operates a platform.", "source_ids": ["src-1"]}
            ],
            "source_ids_used": ["src-1"],
            "sources": [
                {
                    "source_id": "src-1",
                    "title": "Official site",
                    "url": "https://example.test",
                    "source_type": "company_site",
                }
            ],
            "raw_source_content_included": False,
            "send_enabled": False,
        },
        "usage": {"requests": 1},
        "cost": {"estimated_usd": 0.02},
    }


def test_research_doc_content_requires_sources() -> None:
    payload = _focused_result()
    payload["output"]["sources"] = []

    with pytest.raises(ValueError, match="source-backed"):
        lifecycle.build_research_doc_content(payload, suffix="abc")


def test_research_doc_dry_run_preserves_source_provenance(monkeypatch) -> None:
    monkeypatch.setattr(
        lifecycle,
        "google_doc_write_impl",
        lambda title, body, **_kwargs: {
            "status": "dry-run",
            "title": title,
            "body_hash": "bounded",
            "send_enabled": False,
        },
    )

    result = lifecycle.execute_research_doc_lifecycle(
        _focused_result(),
        suffix="abc",
        folder_path="KNIOps",
        approval_reference="approval",
        live=False,
    )

    assert result["status"] == "dry-run"
    assert result["source_url_count"] == 1
    assert result["openai_requests"] == 0
    assert "clinical operations" not in str(result)


def test_research_doc_live_lifecycle_verifies_same_id_and_cleanup(monkeypatch) -> None:
    content = lifecycle.build_research_doc_content(_focused_result(), suffix="abc")
    write_count = 0

    def fake_write(*_args, **_kwargs):
        nonlocal write_count
        write_count += 1
        return {"status": "success", "document_id": "doc-1", "operation": "write"}

    monkeypatch.setattr(lifecycle, "google_doc_write_impl", fake_write)
    read_count = 0

    def fake_read(*_args, **_kwargs):
        nonlocal read_count
        read_count += 1
        body = content["original_body"] if read_count == 1 else content["updated_body"]
        return {
            "status": "success",
            "document_id": "doc-1",
            "title": content["title"],
            "text": body,
            "char_count": len(body),
            "truncated": False,
        }

    monkeypatch.setattr(lifecycle, "google_doc_read_impl", fake_read)
    monkeypatch.setattr(
        lifecycle,
        "google_doc_trash_impl",
        lambda *_args, **_kwargs: {
            "status": "success",
            "document_id": "doc-1",
            "trashed": True,
        },
    )
    monkeypatch.setattr(
        lifecycle,
        "google_drive_get_file_metadata_impl",
        lambda *_args, **_kwargs: {"status": "success", "trashed": True},
    )

    result = lifecycle.execute_research_doc_lifecycle(
        _focused_result(),
        suffix="abc",
        folder_path="KNIOps",
        approval_reference="approval",
        live=True,
    )

    assert result["status"] == "passed"
    assert write_count == 2
    assert result["same_document_identity"] is True
    assert result["receipts"]["create_readback"]["passed"] is True
    assert result["receipts"]["update_readback"]["passed"] is True
    assert result["receipts"]["trash_readback"]["passed"] is True
    assert result["openai_requests"] == 0


def test_research_doc_cleanup_runs_after_update_failure(monkeypatch) -> None:
    content = lifecycle.build_research_doc_content(_focused_result(), suffix="abc")
    write_count = 0

    def fake_write(*_args, **_kwargs):
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise RuntimeError("update failed")
        return {"status": "success", "document_id": "doc-1"}

    monkeypatch.setattr(lifecycle, "google_doc_write_impl", fake_write)
    monkeypatch.setattr(
        lifecycle,
        "google_doc_read_impl",
        lambda *_args, **_kwargs: {
            "status": "success",
            "document_id": "doc-1",
            "title": content["title"],
            "text": content["original_body"],
            "char_count": len(content["original_body"]),
            "truncated": False,
        },
    )
    trashed: list[str] = []
    monkeypatch.setattr(
        lifecycle,
        "google_doc_trash_impl",
        lambda document_id, **_kwargs: trashed.append(document_id)
        or {"status": "success", "document_id": document_id, "trashed": True},
    )
    monkeypatch.setattr(
        lifecycle,
        "google_drive_get_file_metadata_impl",
        lambda *_args, **_kwargs: {"status": "success", "trashed": True},
    )

    result = lifecycle.execute_research_doc_lifecycle(
        _focused_result(),
        suffix="abc",
        folder_path="KNIOps",
        approval_reference="approval",
        live=True,
    )

    assert result["status"] == "failed"
    assert "update failed" in result["failure"]
    assert trashed == ["doc-1"]
    assert result["receipts"]["trash_readback"]["passed"] is True


def test_research_doc_content_supports_general_source_backed_brief() -> None:
    brief = ResearchBrief(
        target_name="Selected research note",
        summary="A concise summary of the selected internal research note.",
        key_findings=["The note identifies one evidence-quality priority."],
        facts=[
            ResearchBriefFact(
                text="The note identifies an evidence-quality priority.",
                source_ids=["google-doc:doc-1"],
                confidence=0.9,
            )
        ],
        sources=[
            ResearchSourceCitation(
                source_id="google-doc:doc-1",
                title="Selected research note",
                url="https://docs.google.com/document/d/doc-1/edit",
                source_type="google_workspace_document",
            )
        ],
    )
    content = lifecycle.build_research_doc_content(
        {"output_type": "ResearchBrief", "output": brief.model_dump(mode="json")},
        suffix="abc",
    )

    assert content["title"].startswith("KBA_TEST_DOC_abc_")
    assert "concise research brief" in content["original_body"]
    assert "## Executive summary" in content["original_body"]
    assert "## Sources" in content["original_body"]
