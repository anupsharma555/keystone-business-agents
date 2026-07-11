"""Join a persisted source-backed company result to a reversible Google Doc lifecycle."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from keystone_agents.schemas.company_profile import CompanyProfile, CompanyResearchFocusedBrief
from keystone_agents.schemas.research import ResearchBrief
from keystone_agents.tools.internal_data_tools import (
    google_doc_read_impl,
    google_doc_trash_impl,
    google_doc_write_impl,
    google_drive_get_file_metadata_impl,
)

RESEARCH_DOC_TEST_MARKER = "KBA_TEST_DOC"


def execute_research_doc_lifecycle(
    persisted_result: dict[str, Any],
    *,
    suffix: str,
    folder_path: str,
    approval_reference: str,
    live: bool,
) -> dict[str, Any]:
    """Create, verify, modify, verify, and trash one source-backed marked Doc."""

    document = build_research_doc_content(persisted_result, suffix=suffix)
    document_id = ""
    receipts: dict[str, Any] = {}
    failure = ""
    try:
        created = google_doc_write_impl(
            document["title"],
            document["original_body"],
            folder_path=folder_path,
            approval_reference=f"{approval_reference}:create",
            live=live,
        )
        receipts["create"] = _bounded_write_receipt(created)
        document_id = str(created.get("document_id") or "")
        if not live:
            return _result(
                status="dry-run",
                document=document,
                document_id=document_id,
                receipts=receipts,
            )
        if not document_id:
            raise RuntimeError("Research Google Doc create returned no document ID.")

        create_read = google_doc_read_impl(document_id, folder_path=folder_path, live=True)
        create_verified = _read_matches(
            create_read,
            document_id=document_id,
            title=document["title"],
            body=document["original_body"],
        )
        receipts["create_readback"] = _bounded_read_receipt(create_read, create_verified)
        if not create_verified:
            raise RuntimeError("Research Google Doc create read-back failed.")

        updated = google_doc_write_impl(
            document["title"],
            document["updated_body"],
            document_id=document_id,
            folder_path=folder_path,
            approval_reference=f"{approval_reference}:update",
            live=True,
        )
        receipts["update"] = _bounded_write_receipt(updated)
        update_read = google_doc_read_impl(document_id, folder_path=folder_path, live=True)
        update_verified = _read_matches(
            update_read,
            document_id=document_id,
            title=document["title"],
            body=document["updated_body"],
        )
        receipts["update_readback"] = _bounded_read_receipt(update_read, update_verified)
        if not update_verified:
            raise RuntimeError("Research Google Doc update read-back failed.")
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        if live and document_id:
            try:
                trashed = google_doc_trash_impl(
                    document_id,
                    folder_path=folder_path,
                    approval_reference=f"{approval_reference}:trash",
                    live=True,
                )
                receipts["trash"] = _bounded_write_receipt(trashed)
                metadata = google_drive_get_file_metadata_impl(
                    document_id,
                    folder_path=folder_path,
                    live=True,
                )
                trash_verified = bool(
                    metadata.get("status") == "success" and metadata.get("trashed") is True
                )
                receipts["trash_readback"] = {
                    "passed": trash_verified,
                    "trashed": bool(metadata.get("trashed")),
                }
                if not trash_verified and not failure:
                    failure = "RuntimeError: Research Google Doc trash read-back failed."
            except Exception as exc:
                cleanup_failure = f"{type(exc).__name__}: {exc}"
                failure = f"{failure}; cleanup {cleanup_failure}" if failure else cleanup_failure

    return _result(
        status="failed" if failure else "passed",
        document=document,
        document_id=document_id,
        receipts=receipts,
        failure=failure,
    )


def build_research_doc_content(
    persisted_result: dict[str, Any], *, suffix: str
) -> dict[str, Any]:
    """Validate provenance and render existing structured research without new reasoning."""

    output_type = str(persisted_result.get("output_type") or "").strip()
    raw_output = persisted_result.get("output")
    if not isinstance(raw_output, dict):
        raw_output = persisted_result
    if output_type == "CompanyResearchFocusedBrief":
        brief = CompanyResearchFocusedBrief.model_validate(raw_output)
        company = brief.company_name
        sections = [
            ("Product", brief.product),
            ("Customers", brief.customers),
            ("Traction signals", brief.traction_signals),
            ("Leadership", brief.leadership),
            ("Why it matters", brief.why_it_matters),
        ]
        sources = [(source.title, source.url) for source in brief.sources]
        if not brief.sources or not any(value for _, value in sections):
            raise ValueError(
                "Focused Business Research result lacks source-backed summary content."
            )
    elif output_type == "ResearchBrief":
        brief = ResearchBrief.model_validate(raw_output)
        company = brief.target_name
        sections = [
            ("Executive summary", brief.summary),
            ("Key findings", "\n".join(f"- {item}" for item in brief.key_findings[:8])),
            ("Interpretation", "\n".join(f"- {item}" for item in brief.inferences[:6])),
            ("Limitations", "\n".join(f"- {item}" for item in brief.limitations[:6])),
            ("Next steps", "\n".join(f"- {item}" for item in brief.next_steps[:6])),
        ]
        sources = [(source.title, source.url) for source in brief.sources]
        if not brief.sources or not brief.summary or not brief.key_findings:
            raise ValueError("Research brief lacks source-backed summary content.")
    elif output_type in {"", "CompanyProfile"}:
        profile = CompanyProfile.model_validate(raw_output)
        company = profile.name
        sections = [
            ("Company summary", profile.description),
            ("KNI relevance", profile.fit_summary),
            ("Evidence", "\n".join(f"- {claim}" for claim in profile.evidence[:8])),
            ("Evidence gaps", "\n".join(f"- {gap}" for gap in profile.missing_evidence[:6])),
        ]
        sources = [(source.title, source.url) for source in profile.sources]
        if not profile.sources or not any(value for _, value in sections[:2]):
            raise ValueError("Company profile lacks source-backed summary content.")
    else:
        raise ValueError(f"Unsupported Business Research output type: {output_type}")

    valid_sources = [(title.strip(), url.strip()) for title, url in sources if url.strip()]
    if not valid_sources:
        raise ValueError("Business Research result has no usable source URLs.")
    clean_company = " ".join(company.split())
    safe_company = re.sub(r"[^A-Za-z0-9_-]+", "_", clean_company).strip("_")[:50]
    title = f"{RESEARCH_DOC_TEST_MARKER}_{suffix}_{safe_company}"
    body_parts = [RESEARCH_DOC_TEST_MARKER, f"# {clean_company} — concise company summary"]
    for heading, value in sections:
        clean_value = str(value or "").strip()
        if clean_value:
            body_parts.extend([f"## {heading}", clean_value])
    body_parts.append("## Sources")
    body_parts.extend(f"- {title}: {url}" for title, url in valid_sources[:8])
    original_body = "\n\n".join(body_parts)
    updated_body = (
        f"{original_body}\n\n## Review note\nInternal validation draft. Verify current facts "
        "and approve before external use."
    )
    return {
        "title": title,
        "original_body": original_body,
        "updated_body": updated_body,
        "source_urls": [url for _, url in valid_sources[:8]],
        "company": clean_company,
    }


def _result(
    *,
    status: str,
    document: dict[str, Any],
    document_id: str,
    receipts: dict[str, Any],
    failure: str = "",
) -> dict[str, Any]:
    return {
        "status": status,
        "failure": failure,
        "openai_requests": 0,
        "live_search": False,
        "document_id_present": bool(document_id),
        "same_document_identity": _same_document_identity(receipts, document_id),
        "title_marker_present": document["title"].startswith(RESEARCH_DOC_TEST_MARKER),
        "original_body_sha256": hashlib.sha256(document["original_body"].encode()).hexdigest(),
        "updated_body_sha256": hashlib.sha256(document["updated_body"].encode()).hexdigest(),
        "source_url_count": len(document["source_urls"]),
        "receipts": receipts,
        "send_enabled": False,
    }


def _same_document_identity(receipts: dict[str, Any], document_id: str) -> bool:
    if not document_id:
        return False
    ids = [
        str(receipt.get("document_id") or "")
        for receipt in receipts.values()
        if isinstance(receipt, dict) and receipt.get("document_id")
    ]
    return bool(ids and all(value == document_id for value in ids))


def _read_matches(
    result: dict[str, Any], *, document_id: str, title: str, body: str
) -> bool:
    return bool(
        result.get("status") == "success"
        and result.get("document_id") == document_id
        and result.get("title") == title
        and str(result.get("text") or "").strip() == body
    )


def _bounded_read_receipt(result: dict[str, Any], passed: bool) -> dict[str, Any]:
    return {
        "passed": passed,
        "document_id": result.get("document_id", ""),
        "char_count": int(result.get("char_count") or 0),
        "truncated": bool(result.get("truncated")),
    }


def _bounded_write_receipt(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result.get(key)
        for key in (
            "status",
            "operation",
            "document_id",
            "trashed",
            "approval_reference",
            "send_enabled",
        )
        if key in result
    }
