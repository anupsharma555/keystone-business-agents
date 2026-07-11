from __future__ import annotations

import sqlite3
import zipfile
from pathlib import Path
from types import SimpleNamespace

from keystone_agents.tools import kni_document_tool
from keystone_agents.tools.kni_document_tool import (
    KNI_DOC_AUTO_REFRESH_ENV,
    KNI_DOC_INDEX_PATH_ENV,
    KNI_DOC_MODEL_CONTEXT_ALLOWED_ENV,
    KNI_DOC_PDF_TEXT_COMMAND_ENV,
    KNI_DOC_ROOT_PATH_ENV,
    KNI_DOC_SEARCH_ENABLED_ENV,
    assess_kni_document_sensitivity,
    list_kni_document_sources_impl,
    read_kni_document_file_impl,
    search_kni_documents_impl,
)


def _env(root: Path, index: Path) -> dict[str, str]:
    return {
        KNI_DOC_SEARCH_ENABLED_ENV: "true",
        KNI_DOC_ROOT_PATH_ENV: str(root),
        KNI_DOC_INDEX_PATH_ENV: str(index),
        KNI_DOC_PDF_TEXT_COMMAND_ENV: "/usr/bin/pdftotext-test",
        KNI_DOC_AUTO_REFRESH_ENV: "false",
        KNI_DOC_MODEL_CONTEXT_ALLOWED_ENV: "true",
    }


def _init_index(index: Path, rows: list[dict[str, str]]) -> None:
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
        for row in rows:
            relative_path = row["relative_path"]
            file_name = Path(relative_path).name
            connection.execute(
                """
                INSERT INTO local_documents (
                    relative_path, document_id, absolute_path, file_name, file_stem,
                    extension, top_level_folder, size_bytes, modified_at, title,
                    preview_text, searchable_path, searchable_content, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    relative_path,
                    f"doc_{len(relative_path)}",
                    f"/tmp/{relative_path}",
                    file_name,
                    Path(file_name).stem,
                    row.get("extension", Path(relative_path).suffix.lstrip(".")),
                    relative_path.split("/", 1)[0],
                    10,
                    "2026-06-11T00:00:00+00:00",
                    row.get("title", Path(file_name).stem),
                    row.get("preview_text", ""),
                    row.get("searchable_path", relative_path.lower()),
                    row.get("searchable_content", row.get("preview_text", "").lower()),
                    "2026-06-11T00:00:00+00:00",
                ),
            )


def _write_docx(path: Path, text: str) -> None:
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:p><w:r><w:t>"
        f"{text}"
        "</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)


def test_kni_document_sources_report_index_status(tmp_path: Path) -> None:
    root = tmp_path / "KNI"
    root.mkdir()
    index = tmp_path / "kni-docs.sqlite"
    _init_index(
        index,
        [
            {
                "relative_path": "07_Marketing_&_Presence/Branding/KNI_Service_Menu_v0.2.md",
                "preview_text": "Service menu context",
            }
        ],
    )

    payload = list_kni_document_sources_impl(env=_env(root, index))

    assert payload["status"] == "ready"
    assert payload["indexed_count"] == 1
    assert payload["local_only"] is True
    assert payload["model_context_allowed"] is True
    assert payload["send_enabled"] is False


def test_kni_document_sources_use_linked_legacy_context_config(
    tmp_path: Path, monkeypatch
) -> None:
    context_repo = tmp_path / "keystone-slack"
    root = tmp_path / "KNI"
    index = context_repo / ".local" / "kni-docs.sqlite"
    context_repo.mkdir()
    root.mkdir()
    index.parent.mkdir()
    _init_index(
        index,
        [
            {
                "relative_path": "07_Marketing_&_Presence/Capability_Statement.md",
                "preview_text": "KNI capability statement and service areas",
            }
        ],
    )
    (context_repo / ".env").write_text(f"KNI_DOC_ROOT_PATH={root}\n")
    monkeypatch.setenv("KEYSTONE_CONTEXT_CONFIG_REPO", str(context_repo))
    monkeypatch.delenv(KNI_DOC_SEARCH_ENABLED_ENV, raising=False)
    monkeypatch.delenv(KNI_DOC_ROOT_PATH_ENV, raising=False)
    monkeypatch.delenv(KNI_DOC_INDEX_PATH_ENV, raising=False)

    payload = list_kni_document_sources_impl()

    assert payload["status"] == "ready"
    assert payload["indexed_count"] == 1
    assert payload["root_path"] == str(root)
    assert payload["index_path"] == str(index)
    assert payload["local_only"] is True
    assert payload["send_enabled"] is False


def test_kni_document_search_filters_blocked_sensitive_paths(tmp_path: Path) -> None:
    root = tmp_path / "KNI"
    root.mkdir()
    index = tmp_path / "kni-docs.sqlite"
    _init_index(
        index,
        [
            {
                "relative_path": "07_Marketing_&_Presence/Branding/KNI_Service_Menu_v0.2.md",
                "preview_text": "Service menu for behavioral health AI evaluation",
            },
            {
                "relative_path": "01_Finance/Banking_Payments/Payment_Setup/bank-info.md",
                "preview_text": "Payment setup and account number context",
            },
            {
                "relative_path": "00_Admin/Insurance/InsurancePolicy/CFC POLICY stamped 042126.pdf",
                "preview_text": "Insurance policy coverage summary",
            },
        ],
    )

    service = search_kni_documents_impl("service menu", env=_env(root, index))
    finance = search_kni_documents_impl("payment setup", env=_env(root, index))
    insurance = search_kni_documents_impl("insurance policy", env=_env(root, index))

    assert [match["relative_path"] for match in service["matches"]] == [
        "07_Marketing_&_Presence/Branding/KNI_Service_Menu_v0.2.md"
    ]
    assert service["model_context_allowed"] is True
    assert service["matches"][0]["model_context_allowed"] is True
    assert finance["matches"] == []
    assert finance["blocked_result_count"] == 1
    assert insurance["matches"][0]["review_required"] is True
    assert "insurance_policy" in insurance["matches"][0]["review_reasons"]


def test_kni_document_search_reports_missing_index(tmp_path: Path) -> None:
    root = tmp_path / "KNI"
    root.mkdir()

    payload = search_kni_documents_impl("service menu", env=_env(root, tmp_path / "missing.sqlite"))

    assert payload["status"] == "missing_index"
    assert payload["matches"] == []
    assert payload["diagnostic"]["refresh_command"] == "python3 -m kni_integrations.cli doc-index"


def test_kni_document_search_refreshes_index_before_query(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "KNI"
    root.mkdir()
    index = tmp_path / "kni-docs.sqlite"
    _init_index(
        index,
        [
            {
                "relative_path": "README.md",
                "preview_text": "Fresh service context",
            }
        ],
    )
    calls: list[dict[str, object]] = []

    def fake_run(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(kni_document_tool, "REFRESH_WORKDIR", str(tmp_path))
    monkeypatch.setattr(kni_document_tool.subprocess, "run", fake_run)
    env = {**_env(root, index), KNI_DOC_AUTO_REFRESH_ENV: "true"}

    payload = search_kni_documents_impl("service", env=env)

    assert payload["refresh"]["attempted"] is True
    assert payload["refresh"]["ok"] is True
    assert calls[0]["kwargs"]["cwd"] == str(tmp_path)
    assert payload["matches"][0]["relative_path"] == "README.md"


def test_kni_document_search_sanitizes_readonly_refresh_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "KNI"
    root.mkdir()
    index = tmp_path / "kni-docs.sqlite"
    _init_index(
        index,
        [
            {
                "relative_path": "README.md",
                "preview_text": "Fresh service context",
            }
        ],
    )

    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stderr="sqlite3.OperationalError: attempt to write a readonly database",
        )

    monkeypatch.setattr(kni_document_tool, "REFRESH_WORKDIR", str(tmp_path))
    monkeypatch.setattr(kni_document_tool.subprocess, "run", fake_run)
    env = {**_env(root, index), KNI_DOC_AUTO_REFRESH_ENV: "true"}

    payload = search_kni_documents_impl("service", env=env)

    assert payload["status"] == "ready"
    assert payload["refresh"]["attempted"] is True
    assert payload["refresh"]["ok"] is False
    assert payload["refresh"]["reason"] == "readonly_index"
    assert "Traceback" not in payload["refresh"]["stderr_tail"]
    assert payload["matches"][0]["relative_path"] == "README.md"


def test_kni_document_sensitivity_blocks_sensitive_content() -> None:
    assert assess_kni_document_sensitivity("safe.md", "Routing number: 123").blocked
    assert assess_kni_document_sensitivity("safe.md", "W-9 tax id details").blocked
    assert assess_kni_document_sensitivity("safe.md", "Patient name: Jane Doe").blocked
    assert assess_kni_document_sensitivity("safe.md", "api_key = 'secret'").blocked

    contract = assess_kni_document_sensitivity(
        "02_Contracts_&_Clients/Templates/MSA_SOW_NDA/KNI_Master_Services_Agreement.docx",
        "Template language only",
    )
    assert contract.blocked is False
    assert contract.review_required is True
    assert "legal_contract" in contract.review_reasons

    insurance = assess_kni_document_sensitivity(
        "00_Admin/Insurance/InsurancePolicy/CFC POLICY stamped 042126.pdf",
        "Coverage summary",
    )
    assert insurance.blocked is False
    assert insurance.review_required is True
    assert "insurance_policy" in insurance.review_reasons


def test_kni_document_read_rejects_traversal_and_blocked_paths(tmp_path: Path) -> None:
    root = tmp_path / "KNI"
    root.mkdir()
    blocked = read_kni_document_file_impl(
        "01_Finance/Banking_Payments/Payment_Setup/info.md",
        env=_env(root, tmp_path / "index.sqlite"),
    )

    assert blocked["sensitivity_status"] == "blocked"
    assert blocked["content"] == ""

    try:
        read_kni_document_file_impl("../outside.md", env=_env(root, tmp_path / "index.sqlite"))
    except ValueError as exc:
        assert "escapes" in str(exc)
    else:
        raise AssertionError("path traversal was not rejected")


def test_kni_document_read_text_docx_and_pdf_with_guardrails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    root = tmp_path / "KNI"
    root.mkdir()
    text_path = root / "README.md"
    text_path.write_text("Useful service context")
    docx_path = root / "proposal.docx"
    _write_docx(docx_path, "DOCX proposal context")
    pdf_path = root / "policy.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 test")

    monkeypatch.setattr(
        kni_document_tool,
        "_read_pdf_file",
        lambda path, command: "PDF insurance policy context",
    )

    env = _env(root, tmp_path / "index.sqlite")
    text = read_kni_document_file_impl("README.md", env=env)
    docx = read_kni_document_file_impl("proposal.docx", env=env)
    pdf = read_kni_document_file_impl("policy.pdf", env=env)

    assert text["content"] == "Useful service context"
    assert "DOCX proposal context" in docx["content"]
    assert pdf["content"] == "PDF insurance policy context"
    assert pdf["local_only"] is True
    assert pdf["model_context_allowed"] is True
    assert pdf["send_enabled"] is False


def test_kni_document_model_context_allowed_reflects_env(tmp_path: Path) -> None:
    root = tmp_path / "KNI"
    root.mkdir()
    index = tmp_path / "kni-docs.sqlite"
    _init_index(
        index,
        [
            {
                "relative_path": "README.md",
                "preview_text": "Useful service context",
            }
        ],
    )
    env = {**_env(root, index), KNI_DOC_MODEL_CONTEXT_ALLOWED_ENV: "false"}
    (root / "README.md").write_text("Useful service context", encoding="utf-8")

    sources = list_kni_document_sources_impl(env=env)
    search = search_kni_documents_impl("service", env=env)
    read = read_kni_document_file_impl("README.md", env=env)

    assert sources["model_context_allowed"] is False
    assert search["model_context_allowed"] is False
    assert search["matches"][0]["model_context_allowed"] is False
    assert read["model_context_allowed"] is False
    assert search["send_enabled"] is False
    assert read["send_enabled"] is False


def test_kni_document_read_redacts_bank_payment_lines_without_exposing_raw_content(
    tmp_path: Path,
) -> None:
    root = tmp_path / "KNI"
    root.mkdir()
    policy_path = root / "00_Admin" / "Insurance" / "InsurancePolicy"
    policy_path.mkdir(parents=True)
    (policy_path / "policy.md").write_text(
        "Coverage summary for operational review.\n"
        "Routing number: 123456789\n"
        "Claims process summary.",
        encoding="utf-8",
    )

    payload = read_kni_document_file_impl(
        "00_Admin/Insurance/InsurancePolicy/policy.md",
        env=_env(root, tmp_path / "index.sqlite"),
    )

    assert payload["sensitivity_status"] == "redacted"
    assert payload["review_required"] is True
    assert "insurance_policy" in payload["review_reasons"]
    assert "bank_payment" in payload["review_reasons"]
    assert "Coverage summary" in payload["content"]
    assert "Claims process" in payload["content"]
    assert "Routing number" not in payload["content"]


def test_kni_document_read_blocks_phi_content(tmp_path: Path) -> None:
    root = tmp_path / "KNI"
    root.mkdir()
    path = root / "project.md"
    path.write_text("Project note\nPatient name: Jane Doe", encoding="utf-8")

    payload = read_kni_document_file_impl("project.md", env=_env(root, tmp_path / "index.sqlite"))

    assert payload["sensitivity_status"] == "blocked"
    assert payload["review_required"] is True
    assert "phi_patient_identifier" in payload["review_reasons"]
    assert payload["content"] == ""
