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
from keystone_agents.privacy_minimized_synthesis import (
    PrivacyMinimizedAssertion,
    build_concept_signal_packet,
    packet_for_model,
    sanitized_source_id,
    validate_privacy_minimized_packet,
)
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

_RESEARCH_NOTE_TAXONOMY = {
    "research_operations": ("research", "study", "project"),
    "agent_workflow": ("agent", "workflow", "automation"),
    "communication_workflow": ("email", "slack", "outreach", "reply"),
    "source_provenance": ("source", "citation", "provenance", "reference"),
    "evaluation_design": ("evaluation design", "study design", "evaluation framework"),
    "evidence_quality": ("evidence quality", "evidence", "validation"),
    "measurement_strategy": ("measurement", "metric", "outcome"),
    "clinical_workflow": ("clinical workflow", "workflow", "care delivery"),
    "data_integration": ("data integration", "interoperability", "data pipeline"),
    "implementation_planning": ("implementation", "operational", "deployment"),
    "limitations_present": ("limitation", "uncertainty", "caveat"),
    "next_steps_present": ("next step", "recommendation", "follow-up"),
}


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
        "transmission_mode": "privacy_minimized_assertions",
        "supported_context_modes": ["privacy_minimized_assertions"],
        "privacy_minimized_preview_available": True,
        "proof_scope": "sanitized_context_proof",
    }


def research_note_brief_privacy_preview(note: SelectedResearchNote) -> dict[str, Any]:
    """Materialize the exact identity-free research context without a model call."""

    minimized_packet = _research_note_privacy_packet(note)
    minimized_source_id = sanitized_source_id(minimized_packet.source_hashes[0])
    signal_count = len(minimized_packet.facts)
    signal_sufficient = _research_assertion_layer_sufficient(minimized_packet)
    return {
        "status": (
            "privacy_minimized_preview"
            if signal_sufficient
            else "privacy_minimized_preview_insufficient"
        ),
        "proof_scope": "sanitized_context_proof",
        "plan": research_note_brief_plan(note),
        "bundle": {
            "target_name": "Selected internal research note",
            "target_type": "topic",
            "privacy_minimized_context": packet_for_model(minimized_packet),
            "local_context_source_ids": [minimized_source_id],
        },
        "local_source_mapping_verified": True,
        "signal_count": signal_count,
        "signal_sufficient": signal_sufficient,
        "openai_requests_made": 0,
        "provider_writes": 0,
    }


def execute_research_note_brief_workflow(
    *,
    note: SelectedResearchNote,
    suffix: str,
    approval_reference: str,
    live_sdk: bool,
    live_workspace_writes: bool,
    approved_privacy_minimized_context: bool = False,
    approved_private_context: bool = False,
    sdk_runner: Callable[..., TypedAgentRunResult[ResearchBrief]] = (
        run_business_research_analyst_research_brief_sdk
    ),
) -> dict[str, Any]:
    """Synthesize one brief, then create/modify/clean one exact marked Doc."""

    plan = research_note_brief_plan(note)
    if not live_sdk:
        return {"status": "validated_offline", "plan": plan}
    if approved_privacy_minimized_context and approved_private_context:
        raise ValueError("Choose exactly one research-note context mode.")
    if approved_private_context:
        raise ValueError(
            "Trusted-private research-note synthesis is disabled; use the typed "
            "privacy-minimized assertion packet."
        )
    if not approved_privacy_minimized_context and not approved_private_context:
        raise ValueError(
            "Research-note synthesis requires approval for the typed privacy-minimized "
            "assertion packet."
        )

    minimized_packet = _research_note_privacy_packet(note)
    if not _research_assertion_layer_sufficient(minimized_packet):
        raise ValueError(
            "Privacy-minimized research context is too sparse for a faithful brief; "
            "add richer typed non-identifying assertions before model execution."
        )
    expected_source_id = sanitized_source_id(minimized_packet.source_hashes[0])
    target_name = "Selected internal research note"
    research_goal = (
        "Turn the supplied privacy-minimized facts and typed assertions into a concise "
        "one-page internal brief. Separate observed categories from interpretation, state "
        "that raw source text was not provided, preserve explicit limitation and next-action "
        "status, and do not invent details beyond the assertion layer. When the typed "
        "assertion says limitation_status=not_observed, include the exact sentence "
        "'No explicit limitation was observed in the typed assertion layer.'"
    )
    source_context = json.dumps(
        packet_for_model(minimized_packet), ensure_ascii=True, sort_keys=True
    )
    context_mode = "privacy_minimized_assertions"
    proof_scope = "sanitized_context_proof"

    typed_input = ResearchSDKInput(
        target_name=target_name,
        target_type="topic",
        research_goal=research_goal,
        source_context=source_context,
        local_context_source_ids=(expected_source_id,),
    )
    with private_context_sdk_profile() as data_profile:
        sdk_result = sdk_runner(
            typed_input,
            live=True,
            attach_tools=False,
            max_turns=1,
        )
    brief = _apply_assertion_contract(sdk_result.output, minimized_packet=minimized_packet)
    _validate_brief(brief, expected_source_id, minimized_packet=minimized_packet)
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
        "proof_scope": proof_scope,
        "context_mode": context_mode,
        "plan": plan,
        "brief_summary_sha256": hashlib.sha256(brief.summary.encode()).hexdigest(),
        "brief_source_count": len(brief.sources),
        "source_identity_preserved": expected_source_id in brief.source_ids_used,
        "local_source_mapping_verified": True,
        "data_handling": data_profile.audit_metadata(),
        "usage": dict(sdk_result.usage or {}),
        "cost": dict(sdk_result.cost or {}),
        "lifecycle": lifecycle,
        "send_enabled": False,
    }


def _validate_brief(
    brief: ResearchBrief,
    minimized_source_id: str,
    *,
    minimized_packet: Any,
) -> None:
    if brief.send_enabled or brief.raw_source_content_included:
        raise RuntimeError("Research-note brief violated no-send/raw-content boundaries.")
    if not brief.summary or not brief.key_findings:
        raise RuntimeError("Research-note brief is not substantive.")
    if minimized_source_id not in brief.source_ids_used:
        raise RuntimeError("Research-note brief omitted the sanitized source identity.")
    cited = {source.source_id for source in brief.sources}
    if minimized_source_id not in cited:
        raise RuntimeError("Research-note brief omitted the sanitized source citation.")
    rendered_length = len(brief.summary) + sum(len(item) for item in brief.key_findings)
    if rendered_length > 6_000:
        raise RuntimeError("Research-note brief exceeded the one-page validation ceiling.")
    rendered = " ".join(
        [
            brief.summary,
            *brief.key_findings,
            *brief.inferences,
            *brief.limitations,
            *brief.next_steps,
        ]
    ).casefold()
    concepts = {fact.concept for fact in minimized_packet.facts}
    covered = {
        concept
        for concept in concepts
        if concept.replace("_", " ") in rendered
    }
    if len(covered) < min(3, len(concepts)):
        raise RuntimeError("Research-note brief omitted typed assertion categories.")
    assertions = {(item.predicate, item.object) for item in minimized_packet.assertions}
    if ("limitation_status", "not_observed") in assertions and not any(
        phrase in rendered
        for phrase in (
            "no explicit limitation",
            "limitation was not observed",
            "limitations were not provided",
        )
    ):
        raise RuntimeError("Research-note brief omitted the not-observed limitation status.")
    if ("next_action_status", "present") in assertions and not brief.next_steps:
        raise RuntimeError("Research-note brief omitted the asserted next action.")
    if not any(
        phrase in rendered for phrase in ("raw source", "abstracted", "typed assertion")
    ):
        raise RuntimeError("Research-note brief omitted its abstracted-evidence caveat.")


def _apply_assertion_contract(brief: ResearchBrief, *, minimized_packet: Any) -> ResearchBrief:
    """Materialize deterministic typed statuses that the model may phrase implicitly."""

    assertions = {(item.predicate, item.object) for item in minimized_packet.assertions}
    canonical_status = "No explicit limitation was observed in the typed assertion layer."
    if (
        ("limitation_status", "not_observed") in assertions
        and canonical_status not in brief.limitations
    ):
        brief = brief.model_copy(
            update={
                "limitations": [*brief.limitations, canonical_status]
            }
        )
    normalized_sources = []
    for source in brief.sources:
        if source.source_id.startswith("sanitized-source:"):
            source_hash = source.source_id.removeprefix("sanitized-source:")
            source = source.model_copy(
                update={
                    "title": "Sanitized internal research assertion source",
                    "url": f"urn:sha256:{source_hash}",
                    "source_type": "privacy_minimized_assertion",
                }
            )
        normalized_sources.append(source)
    if normalized_sources != brief.sources:
        brief = brief.model_copy(update={"sources": normalized_sources})
    return brief


def _research_note_privacy_packet(note: SelectedResearchNote) -> Any:
    minimized = build_concept_signal_packet(
        workflow="research_brief",
        sources={f"{note.document_id}:{note.title}": note.text},
        taxonomy=_RESEARCH_NOTE_TAXONOMY,
        constraints=(
            "human_review_required",
            "abstracted_evidence_only",
            "one_page_maximum",
            "no_external_action",
        ),
    )
    concepts = {fact.concept for fact in minimized.facts}
    density = (
        "substantive" if len(concepts) >= 4 else "limited" if len(concepts) >= 2 else "sparse"
    )
    assertions = tuple(
        PrivacyMinimizedAssertion(
            subject="source_material",
            predicate="contains",
            object=fact.concept,
            count=fact.evidence_count,
            source_hashes=fact.source_hashes or minimized.source_hashes,
        )
        for fact in minimized.facts
    ) + (
        PrivacyMinimizedAssertion(
            subject="source_material",
            predicate="evidence_density",
            object=density,
            count=len(concepts),
            source_hashes=minimized.source_hashes,
        ),
        PrivacyMinimizedAssertion(
            subject="source_material",
            predicate="limitation_status",
            object=("present" if "limitations_present" in concepts else "not_observed"),
            count=1 if "limitations_present" in concepts else 0,
            source_hashes=minimized.source_hashes,
        ),
        PrivacyMinimizedAssertion(
            subject="source_material",
            predicate="next_action_status",
            object=("present" if "next_steps_present" in concepts else "not_observed"),
            count=1 if "next_steps_present" in concepts else 0,
            source_hashes=minimized.source_hashes,
        ),
    )
    return validate_privacy_minimized_packet(
        minimized.model_copy(update={"assertions": assertions}),
        forbidden_raw_values=(f"{note.document_id}:{note.title}", note.text),
    )


def _research_assertion_layer_sufficient(packet: Any) -> bool:
    concepts = {fact.concept for fact in packet.facts}
    domain_concepts = {
        "research_operations",
        "agent_workflow",
        "communication_workflow",
        "source_provenance",
        "evaluation_design",
        "evidence_quality",
        "measurement_strategy",
        "clinical_workflow",
        "data_integration",
        "implementation_planning",
    }
    assertions = {(item.predicate, item.object) for item in packet.assertions}
    return bool(
        len(concepts) >= 3
        and concepts & domain_concepts
        and ("next_action_status", "present") in assertions
        and any(predicate == "limitation_status" for predicate, _ in assertions)
    )


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
