"""Joined latest Workspace research-note synthesis and disposable Doc proof."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from keystone_agents.agents.business_research_analyst import (
    run_business_research_analyst_research_brief_sdk,
)
from keystone_agents.models import ResearchSDKInput, TypedAgentRunResult
from keystone_agents.research_doc_lifecycle import execute_research_doc_lifecycle
from keystone_agents.schemas.research import ResearchBrief
from keystone_agents.sdk import private_context_sdk_profile
from keystone_agents.tools.internal_data_tools import (
    google_doc_read_impl,
    google_drive_list_folder_impl,
    google_drive_search_files_impl,
)

GOOGLE_DOC_MIME = "application/vnd.google-apps.document"
GOOGLE_FOLDER_MIME = "application/vnd.google-apps.folder"
RESEARCH_FOLDER = "KNIOps/Research"
MAX_NOTE_CHARS = 12_000
MAX_OPENAI_REQUESTS = 1
MAX_COST_USD = Decimal("0.05")


@dataclass(frozen=True)
class SelectedResearchNote:
    document_id: str
    title: str
    modified_time: str
    text: str

    @property
    def source_id(self) -> str:
        return f"google-doc:{self.document_id}"

    @property
    def source_url(self) -> str:
        return f"https://docs.google.com/document/d/{self.document_id}/edit"


def resolve_latest_research_note(
    *,
    live: bool,
    search_files: Callable[..., dict[str, Any]] = google_drive_search_files_impl,
    list_folder: Callable[..., dict[str, Any]] = google_drive_list_folder_impl,
    read_doc: Callable[..., dict[str, Any]] = google_doc_read_impl,
) -> SelectedResearchNote:
    """Resolve one unique latest Google Doc and retain its body only in-process."""

    if not live:
        raise ValueError("Latest Workspace research-note resolution requires live reads.")
    root_search = search_files("research", folder_path="KNIOps", max_items=10, live=True)
    folders = [
        item
        for item in root_search.get("items") or []
        if str(item.get("name") or "").strip().casefold() == "research"
        and _mime_type(item) == GOOGLE_FOLDER_MIME
    ]
    if len(folders) != 1:
        raise RuntimeError("Expected one exact KNIOps/Research folder.")

    listing = list_folder(RESEARCH_FOLDER, max_items=50, live=True)
    documents = [item for item in listing.get("items") or [] if _mime_type(item) == GOOGLE_DOC_MIME]
    if not documents:
        raise RuntimeError("KNIOps/Research contains no Google Docs.")
    documents.sort(key=lambda item: str(item.get("modified_time") or ""), reverse=True)
    latest_modified = str(documents[0].get("modified_time") or "")
    latest = [item for item in documents if str(item.get("modified_time") or "") == latest_modified]
    if not latest_modified or len(latest) != 1:
        raise RuntimeError("Latest KNIOps research-note identity is ambiguous.")
    selected = latest[0]
    selected_id = str(selected.get("id") or "")
    result = read_doc(selected_id, folder_path=RESEARCH_FOLDER, max_chars=MAX_NOTE_CHARS, live=True)
    text = str(result.get("text") or "").strip()
    if not (
        selected_id
        and result.get("status") == "success"
        and result.get("document_id") == selected_id
        and text
        and result.get("truncated") is False
    ):
        raise RuntimeError("Selected research-note read verification failed.")
    return SelectedResearchNote(
        document_id=selected_id,
        title=str(result.get("title") or selected.get("name") or "Research note").strip(),
        modified_time=latest_modified,
        text=text,
    )


def research_note_brief_plan(note: SelectedResearchNote) -> dict[str, Any]:
    return {
        "schema": "keystone.workspace_research_note_brief.plan.v1",
        "source_document_id_hash": _hash(note.document_id),
        "source_title_hash": _hash(note.title),
        "source_content_sha256": hashlib.sha256(note.text.encode()).hexdigest(),
        "source_modified_time": note.modified_time,
        "source_char_count": len(note.text),
        "source_body_persisted": False,
        "specialist": "business_research_analyst",
        "writer": "google_workspace_context_agent",
        "max_openai_requests": MAX_OPENAI_REQUESTS,
        "max_cost_usd": str(MAX_COST_USD),
        "model_tools_attached": False,
        "live_search": False,
        "send_enabled": False,
        "live_workspace_write_required_for_full_pass": True,
    }


def execute_research_note_brief_workflow(
    *,
    note: SelectedResearchNote,
    suffix: str,
    approval_reference: str,
    live_sdk: bool,
    live_workspace_writes: bool,
    approved_private_context: bool = False,
    sdk_runner: Callable[..., TypedAgentRunResult[ResearchBrief]] = (
        run_business_research_analyst_research_brief_sdk
    ),
) -> dict[str, Any]:
    """Synthesize one brief, then create/modify/clean one exact marked Doc."""

    plan = research_note_brief_plan(note)
    if not live_sdk:
        return {"status": "validated_offline", "plan": plan}
    if not approved_private_context:
        raise ValueError("Research-note synthesis requires explicit private-context approval.")

    typed_input = ResearchSDKInput(
        target_name=note.title,
        target_type="topic",
        research_goal=(
            "Turn the supplied selected KNI research note into a concise one-page internal "
            "brief. Preserve the source identity, separate findings from interpretation, "
            "state limitations, and propose practical next steps. Use only the supplied note."
        ),
        source_context=json.dumps(
            {
                "source_id": note.source_id,
                "title": note.title,
                "url": note.source_url,
                "modified_time": note.modified_time,
                "content": note.text,
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
        local_context_source_ids=(note.source_id,),
    )
    with private_context_sdk_profile() as data_profile:
        sdk_result = sdk_runner(
            typed_input,
            live=True,
            attach_tools=False,
            max_turns=1,
        )
    brief = sdk_result.output
    _validate_brief(brief, note)
    _validate_usage(sdk_result)
    persisted = {
        "output_type": "ResearchBrief",
        "output": brief.model_dump(mode="json"),
        "usage": dict(sdk_result.usage or {}),
        "cost": dict(sdk_result.cost or {}),
    }
    lifecycle = execute_research_doc_lifecycle(
        persisted,
        suffix=suffix,
        folder_path="KNIOps",
        approval_reference=approval_reference,
        live=live_workspace_writes,
    )
    workflow_status = {
        "passed": "passed",
        "dry-run": "synthesis_passed_write_pending",
    }.get(str(lifecycle.get("status") or ""), "failed")
    return {
        "status": workflow_status,
        "plan": plan,
        "brief_summary_sha256": hashlib.sha256(brief.summary.encode()).hexdigest(),
        "brief_source_count": len(brief.sources),
        "source_identity_preserved": note.source_id in brief.source_ids_used,
        "data_handling": data_profile.audit_metadata(),
        "usage": dict(sdk_result.usage or {}),
        "cost": dict(sdk_result.cost or {}),
        "lifecycle": lifecycle,
        "send_enabled": False,
    }


def _validate_brief(brief: ResearchBrief, note: SelectedResearchNote) -> None:
    if brief.send_enabled or brief.raw_source_content_included:
        raise RuntimeError("Research-note brief violated no-send/raw-content boundaries.")
    if not brief.summary or not brief.key_findings:
        raise RuntimeError("Research-note brief is not substantive.")
    if note.source_id not in brief.source_ids_used:
        raise RuntimeError("Research-note brief omitted the selected source identity.")
    cited = {source.source_id for source in brief.sources}
    if note.source_id not in cited:
        raise RuntimeError("Research-note brief omitted the selected source citation.")
    rendered_length = len(brief.summary) + sum(len(item) for item in brief.key_findings)
    if rendered_length > 6_000:
        raise RuntimeError("Research-note brief exceeded the one-page validation ceiling.")


def _validate_usage(result: TypedAgentRunResult[ResearchBrief]) -> None:
    usage = dict(result.usage or {})
    requests = usage.get("requests", usage.get("request_count"))
    if requests != MAX_OPENAI_REQUESTS:
        raise RuntimeError("Research-note synthesis must use exactly one model request.")
    cost = dict(result.cost or {})
    observed = cost.get("estimated_usd", cost.get("actual_usd"))
    if observed is None or Decimal(str(observed)) > MAX_COST_USD:
        raise RuntimeError("Research-note synthesis is missing cost evidence or exceeded budget.")


def _mime_type(item: dict[str, Any]) -> str:
    return str(item.get("mime_type") or item.get("mimeType") or "")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]
