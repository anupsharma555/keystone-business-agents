"""Bounded supplied evidence for registered Research SDK synthesis, without retrieval."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import quote

from keystone_agents.models import ResearchSDKInput
from keystone_agents.operator_failures import (
    OperatorReadableFailureError,
    WorkItemContextRequiredError,
)
from keystone_agents.schemas.research import ResearchBrief, ResearchSourceCitation
from keystone_agents.schemas.source_evidence import compact_source_evidence_context
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemBlocker,
    WorkItemNextAction,
    WorkItemRoute,
    WorkItemSourceRef,
)
from keystone_agents.work_items import build_research_context_pack


def supplied_research_sources(
    work_item: WorkItem, *, inline_context: str = "",
) -> tuple[WorkItemSourceRef, ...]:
    """Normalize identities before inference; reject incomplete or conflicting evidence."""
    raw = list(work_item.sources)
    for artifact in work_item.artifact_refs:
        if artifact.selected:
            raw.extend(
                WorkItemSourceRef.model_validate(value)
                for value in artifact.metadata.get("source_refs", [])
                if isinstance(value, dict)
            )
    if not raw and inline_context.strip():
        # This is an internal reference to operator input, never an invented website.
        identity = hashlib.sha256(inline_context.encode()).hexdigest()[:20]
        raw = [WorkItemSourceRef(
            source_id=f"operator-note:{identity}", title="Operator-supplied context",
            url=f"provided://operator-note/{identity}", provider="operator_supplied",
            source_type="user_provided", extraction_status="supplied_material",
            evidence_excerpt=inline_context,
        )]
    sources: dict[str, WorkItemSourceRef] = {}
    urls: dict[str, str] = {}
    for source in raw:
        if (
            source.url.startswith("fixture://") or source.provider in {"fixture", "promptfoo"}
            or source.source_type == "fixture"
        ):
            continue
        if not (
            source.evidence_excerpt.strip() or source.supported_claim.strip() or source.key_facts
        ):
            continue
        identity = source.source_id.strip() or source.url.strip()
        if not identity or len(identity) > 200:
            raise OperatorReadableFailureError.input_contract(
                "Supplied research requires bounded, explicit source identities."
            )
        url = source.url.strip() or f"provided://source/{quote(identity, safe='')}"
        normalized = source.model_copy(update={"source_id": identity, "url": url})
        if identity in sources:
            previous = sources[identity]
            if any(getattr(previous, key) != getattr(normalized, key) for key in (
                "url", "supported_claim", "evidence_excerpt", "key_facts", "evidence_access",
            )):
                raise OperatorReadableFailureError.input_contract(
                    "One supplied source identity refers to conflicting evidence."
                )
            continue
        if url in urls and urls[url] != identity:
            raise OperatorReadableFailureError.input_contract(
                "A supplied URL has conflicting source identities."
            )
        sources[identity] = normalized
        urls[url] = identity
    if not sources:
        raise WorkItemContextRequiredError(
            (WorkItemBlocker(
                code="source_sufficiency_required",
                message="Supply readable source evidence before Research synthesis.",
            ),),
            WorkItemNextAction(
                action="add_source_backed_context", agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
                description="Add or select readable source evidence for the requested research.",
            ),
        )
    if len(sources) > 12:
        raise OperatorReadableFailureError.input_contract(
            "Supply between one and twelve non-fixture sources for research."
        )
    readable = sum(len(source.evidence_excerpt) + len(source.supported_claim)
                   + sum(len(fact) for fact in source.key_facts) for source in sources.values())
    if not readable or readable > 24_000:
        raise OperatorReadableFailureError.input_contract(
            "Supplied research requires readable evidence within the 24000-character bound."
        )
    return tuple(sources.values())


def supplied_research_input(
    work_item: WorkItem, *, raw_request: str, sources: tuple[WorkItemSourceRef, ...],
    orchestration_context: dict[str, Any],
) -> tuple[ResearchSDKInput, tuple[ResearchSourceCitation, ...]]:
    context_item = work_item.model_copy(update={"sources": list(sources)})
    pack = build_research_context_pack(context_item)
    if not pack.can_synthesize:
        blockers = tuple(pack.blockers) or tuple(
            blocker for gate in pack.readiness_gates if gate.required and not gate.ready
            for blocker in gate.blockers
        )
        if blockers:
            raise WorkItemContextRequiredError(blockers, pack.allowed_next_action)
        raise OperatorReadableFailureError.input_contract(
            "Supplied Research context did not pass its existing readiness gates."
        )
    citations = tuple(ResearchSourceCitation(
        source_id=source.source_id, title=source.title or "Supplied source",
        url=source.url, source_type=source.source_type,
    ) for source in sources)
    context = dict(orchestration_context)
    context.pop("context_pack", None)  # The complete typed pack below carries this evidence.
    context.pop("raw_request", None)
    target_type = work_item.target.object_type
    if target_type not in {
        "company", "institute", "conference", "lab", "person", "zotero_collection",
        "zotero_article", "article_collection", "github_repository_collection", "topic", "other",
    }:
        target_type = "other"
    return ResearchSDKInput(
        target_name=work_item.target.name or work_item.target.url,
        target_type=target_type, research_goal=raw_request,
        source_context=json.dumps(compact_source_evidence_context({
            "raw_request": raw_request,
            "context_pack": pack.model_dump(mode="json"),
            "orchestrator_context": context,
            "source_catalog": [source.model_dump(mode="json") for source in citations],
            "retrieval_enabled": False,
            "retained_source_read_enabled": any(source.evidence_pages for source in sources),
        }), ensure_ascii=True, sort_keys=True),
        local_context_source_ids=tuple(source.source_id for source in citations),
    ), citations


def render_supplied_research_brief(brief: ResearchBrief) -> str:
    """Format model-authored fields and citations; never synthesize source claims."""
    lines = [brief.summary]
    for title, values in (
        ("Findings", brief.key_findings), ("Inferences", brief.inferences),
        ("Limitations", [*brief.limitations, *brief.unknowns]), ("Next steps", brief.next_steps),
    ):
        if values:
            lines.extend(["", f"**{title}:**", *[f"- {value}" for value in values]])
    if brief.sources:
        lines.extend(["", "**Sources:**", *[
            f"- [{source.title}]({source.url})" for source in brief.sources
        ]])
    return "\n".join(lines)
