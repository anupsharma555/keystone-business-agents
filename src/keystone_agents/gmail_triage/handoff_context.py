"""Promote only selected provider-read Gmail context into downstream evidence."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import quote

from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.schemas.gmail_query import (
    GmailReadContextResult,
    GmailSelectedContextResult,
    GmailSourceBodyEvidence,
    GmailSourceEvidencePage,
)
from keystone_agents.schemas.source_evidence import SourceEvidencePage
from keystone_agents.schemas.work_item import WorkItemSourceRef


def selected_provider_context(
    output: EmailTriageResult,
    entries: Sequence[Mapping[str, Any]],
) -> GmailSelectedContextResult | None:
    """Select exact read evidence after the owning agent's identity validation."""
    if output.decision.needs_more_context:
        return None
    candidates: list[GmailReadContextResult] = []
    for entry in reversed(entries):
        if entry.get("tool_name") != "read_gmail_context":
            continue
        payload = entry.get("output")
        if not isinstance(payload, Mapping):
            continue
        if payload.get("provider_read_performed") is not True or payload.get("status") != "read":
            continue
        try:
            context = GmailReadContextResult.model_validate(payload)
        except ValueError:
            continue
        if context.resource_type == "message":
            message = context.message
            if message is None or message.message_id != context.resource_id:
                continue
            if output.message_id and message.message_id != output.message_id:
                continue
            if output.thread_id and message.thread_id != output.thread_id:
                continue
            if output.message_id or output.thread_id:
                candidates.append(context)
        elif output.thread_id and context.resource_id == output.thread_id:
            candidates.append(context)
    # The exact selected message is narrower than a containing thread read.
    message_candidates = [item for item in candidates if item.resource_type == "message"]
    selected = message_candidates or candidates[:1]
    if not selected:
        return None
    identities = {
        (item.resource_id, item.thread_id, item.account_identity_sha256,
         item.source_snapshot_sha256)
        for item in selected
    }
    if len(identities) != 1:
        return None
    base = selected[-1]
    for entry in entries:
        payload = entry.get("output")
        if (entry.get("tool_name") == "read_gmail_context" and isinstance(payload, Mapping)
                and payload.get("resource_id") == base.resource_id
                and payload.get("source_restart_required") is True):
            return None
    merged_evidence: list[GmailSourceBodyEvidence] = []
    seen_windows: set[tuple[str, str, int, int, str]] = set()
    for context in reversed(selected):
        for item in context.body_evidence:
            if ((item.message_id and context.message and
                 item.message_id != context.message.message_id)
                    or (item.thread_id and item.thread_id != context.thread_id)):
                return None
            coverage = item.coverage
            if coverage and (
                coverage.end_char - coverage.start_char != len(item.source_text)
                or not 0 <= coverage.start_char <= coverage.end_char <= coverage.full_char_count
            ):
                return None
            key = (item.message_id, item.part_path,
                   coverage.start_char if coverage else 0,
                   coverage.end_char if coverage else len(item.source_text), item.source_text)
            if key not in seen_windows:
                seen_windows.add(key)
                merged_evidence.append(item)
    merged_evidence.sort(key=lambda item: (
        item.received_at, item.message_id, item.part_path,
        item.coverage.start_char if item.coverage else 0,
    ))
    part_windows: dict[tuple[str, str], list[GmailSourceBodyEvidence]] = {}
    for item in merged_evidence:
        part_windows.setdefault((item.message_id, item.part_path), []).append(item)
    complete = bool(part_windows)
    for windows in part_windows.values():
        cursor = 0
        full_counts = {item.coverage.full_char_count for item in windows if item.coverage}
        if len(full_counts) > 1:
            return None
        for index, item in enumerate(windows):
            coverage = item.coverage
            if coverage is None:
                complete = False
                continue
            if coverage.start_char > cursor:
                complete = False
            for previous in windows[:index]:
                if previous.coverage is None:
                    continue
                start = max(coverage.start_char, previous.coverage.start_char)
                end = min(coverage.end_char, previous.coverage.end_char)
                if start < end and (
                    item.source_text[start - coverage.start_char:end - coverage.start_char]
                    != previous.source_text[
                        start - previous.coverage.start_char:end - previous.coverage.start_char
                    ]
                ):
                    return None
            cursor = max(cursor, coverage.end_char)
        complete = complete and bool(full_counts) and cursor == next(iter(full_counts), -1)
        complete = complete and any(
            item.coverage and not item.coverage.has_more and item.content_complete
            for item in windows
        )
    preview = merged_evidence[:16]
    limitations = list(dict.fromkeys(
        limitation for context in selected for limitation in context.triage_limitations
    ))[:19]
    if len(preview) < len(merged_evidence):
        limitations.append(
            f"Selected-context preview includes {len(preview)} of {len(merged_evidence)} "
            "read source windows; all windows are retained in source_evidence_pages."
        )
    body_complete = complete and len(preview) == len(merged_evidence)
    return GmailSelectedContextResult.model_validate({
        **base.model_dump(mode="json"),
        "summary": selected[0].summary or base.summary,
        "thread_context": selected[0].thread_context or base.thread_context,
        "body_evidence": preview,
        "body_content_complete": body_complete,
        "body_content_status": (
            "conflicting" if any(item.body_content_status == "conflicting" for item in selected)
            else "complete" if body_complete else "partial"
        ),
        "triage_limitations": limitations,
        "source_evidence_pages": [
            GmailSourceEvidencePage(windows=merged_evidence[start:start + 16])
            for start in range(0, len(merged_evidence), 16)
        ],
        "source_read_count": len(selected),
        "source_windows_read": len(merged_evidence),
        "source_windows_retained": len(merged_evidence),
        "source_evidence_complete": complete,
    })


def gmail_context_source_refs(context: GmailReadContextResult) -> list[WorkItemSourceRef]:
    """Keep the bounded provider excerpt separate from unread linked-page evidence."""
    identity = (
        f"{context.resource_type}:{context.resource_id}:"
        f"{context.account_identity_sha256}:{context.source_snapshot_sha256}"
    )
    source_id = "gmail-context:" + hashlib.sha256(identity.encode()).hexdigest()[:20]
    message = context.message
    path = (
        f"thread/{quote(message.thread_id, safe='')}/message/{quote(message.message_id, safe='')}"
        if message is not None
        else f"thread/{quote(context.resource_id, safe='')}"
    )
    evidence = (
        [window for page in context.source_evidence_pages for window in page.windows]
        if isinstance(context, GmailSelectedContextResult) and context.source_evidence_pages
        else context.body_evidence
    )
    pages = [SourceEvidencePage(
        evidence_excerpt=item.source_text,
        provenance_json=json.dumps({
            **item.model_dump(mode="json", exclude={"source_text"}),
            "account_identity_sha256": context.account_identity_sha256,
            "source_snapshot_sha256": context.source_snapshot_sha256,
        }, ensure_ascii=False, sort_keys=True),
    ) for item in evidence]
    body_windows = []
    for item in evidence:
        if not item.source_text:
            continue
        coverage = item.coverage
        label = (
            f"[{item.part_path} chars {coverage.start_char}:{coverage.end_char}]"
            if coverage
            else f"[{item.part_path}]"
        )
        body_windows.append(f"{label} {item.source_text}")
    retained_body_windows: list[str] = []
    retained_chars = 0
    for value in body_windows:
        if retained_chars + len(value) > 9_000:
            break
        retained_body_windows.append(value)
        retained_chars += len(value)
    refs = [
        WorkItemSourceRef(
            source_id=source_id,
            provider_candidate_id=context.resource_id,
            title=context.subject or "Selected Gmail context",
            url=context.source_url or f"gmail://{path}",
            source_type="gmail_message" if message is not None else "gmail_thread",
            provider="gmail",
            extraction_status="provider_context_read",
            source_quality="selected_provider_context",
            supported_claim=(
                "This bounded context came from the exact selected Gmail provider read."
            ),
            evidence_pages=pages,
            evidence_excerpt="\n\n".join(
                dict.fromkeys(
                    value
                    for value in (
                        context.summary,
                        *retained_body_windows,
                        context.thread_context,
                    )
                    if value
                )
            )[:12_000],
            key_facts=[
                value
                for value in (
                    f"message_id={message.message_id}" if message is not None else "",
                    f"thread_id={context.thread_id or context.resource_id}",
                    (
                        f"source_snapshot_sha256={context.source_snapshot_sha256}"
                        if context.source_snapshot_sha256
                        else ""
                    ),
                    f"body_content_complete={str(context.body_content_complete).lower()}",
                    (
                        "source_evidence_complete=" + str(
                            context.source_evidence_complete
                            if isinstance(context, GmailSelectedContextResult)
                            else context.body_content_complete
                        ).lower()
                    ),
                    f"source_windows_read={len(evidence)}",
                    f"source_windows_retained={len(pages)}",
                    f"source_windows_in_excerpt={len(retained_body_windows)}",
                    (
                        "evidence_excerpt_complete="
                        f"{str(len(body_windows) == len(retained_body_windows)).lower()}"
                    ),
                    "Full retained sanitized source windows are available through "
                    "read_work_item_source_evidence using evidence_access; excerpt is a preview.",
                )
                if value
            ],
        )
    ]
    for link in context.extracted_links:
        if link.suspicious:
            continue
        refs.append(
            WorkItemSourceRef(
                source_id="gmail-link:" + hashlib.sha256(link.url.encode()).hexdigest()[:20],
                provider_candidate_id=context.resource_id,
                title="Link in selected Gmail context",
                url=link.url,
                source_type="gmail_extracted_link",
                provider="gmail",
                extraction_status="link_only",
                source_quality="unread_link",
                supported_claim=(
                    "This URL appeared in the selected Gmail context. Its page contents have "
                    "not been retrieved or verified."
                ),
            )
        )
    return refs
