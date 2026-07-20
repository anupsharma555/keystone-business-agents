"""Shared user-facing response synthesis for WorkItem results."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from keystone_agents.schemas.request_coverage import RequestCoverage
from keystone_agents.schemas.work_item import WorkflowRunResult
from keystone_agents.source_triage import SourceTriageSummary
from keystone_agents.visible_sources import append_visible_source_urls_to_text


class UserFacingResponseSynthesis(BaseModel):
    """Structured response text synthesized from bounded WorkItem state."""

    title: str = ""
    answer: str
    synthesis: str = ""
    source_evidence: list[str] = Field(default_factory=list)
    terms: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    key_points: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    next_step: str = ""


class UserFacingResponseSynthesisInput(BaseModel):
    """Compact bounded input for final response synthesis."""

    user_request: str
    latest_user_request: str = ""
    agent_name: str
    route: str
    status: str
    advanced: bool
    deterministic_summary: str
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    ordered_sources: list[dict[str, Any]] = Field(default_factory=list)
    source_data_summaries: list[str] = Field(default_factory=list)
    provider_results: list[dict[str, Any]] = Field(default_factory=list)
    source_context_notes: list[str] = Field(default_factory=list)
    source_triage_notes: list[str] = Field(default_factory=list)
    source_triage: list[SourceTriageSummary] = Field(default_factory=list)
    request_coverage: list[RequestCoverage] = Field(default_factory=list)
    request_coverage_required: bool = False
    blockers: list[dict[str, Any]] = Field(default_factory=list)
    next_action: dict[str, Any] | None = None
    manual_plan: dict[str, Any] | None = None
    orchestrator_reviews: list[dict[str, Any]] = Field(default_factory=list)
    audit_notes: list[str] = Field(default_factory=list)

    def to_prompt(self) -> str:
        return "\n".join(
            [
                "Synthesize a clear user-facing response from this bounded WorkItem state.",
                "",
                "Rules:",
                "- Use only the facts in the payload.",
                "- Do not invent sources, counts, amounts, deadlines, or conclusions.",
                "- Treat latest_user_request as the primary objective when present.",
                "- Treat prior thread, WorkItem, artifact, and profile context as background evidence.",
                "- If the latest request is narrower than the artifact set, answer the narrower request first.",
                "- Do not summarize every artifact merely because it is available; select what is relevant.",
                "- Do not present route metadata, artifact ids, or workflow status as the main answer.",
                "- Lead with the substantive answer, not run status or evaluator framing.",
                "- Answer in KNI's operator voice; do not write as an unnamed evaluator.",
                "- Avoid phrases like 'the evidence does answer the core request' or 'the review flagged'.",
                "- State what the current sources support and what remains unknown.",
                "- Treat payload sources and provider_results as the bounded retrieved context for Answer and the Detailed Summary; do not synthesize from route metadata alone.",
                "- Treat source_data_summaries as the primary factual substrate for the Detailed Summary; summarize what those source summaries say before explaining Keystone/operator relevance.",
                "- source_data_summaries are compact evidence statements, not metadata. Use them to write a detailed answer, not a provider-status report.",
                "- For deep/source-backed search, synthesize the retrieved link content across selected sources into a narrative summary. Do not merely restate source titles, provider snippets, or URLs.",
                "- If selected link content was read/extracted, use that extracted content as the basis for the summary. If links were not read or extraction failed, say the summary is limited to snippets.",
                "- provider_results are search-lane candidates, not verified extracted claims unless they also appear in sources or artifact source refs. Use them to explain recall, precision, and follow-up search direction when requested.",
                "- Treat source_context_notes as a hard pre-synthesis warning about whether identified web links were actually read/extracted before synthesis.",
                "- Treat source_triage_notes as the source-selection contract for Answer and the Detailed Summary. Retained sources may support claims, rejected sources must not support claims, and deepen sources require page reading/extraction before detailed factual summary.",
                "- Treat request_coverage as the specialist audit of the interpreted ask. If it is partial or blocked, present a precise partial answer or blocker and its next safe action, not a complete result.",
                "- If request_coverage_required=true but no assessed coverage is present, do not claim that exact filters, requested output form, or stop conditions were verified.",
                "- Treat manual_plan.ask_shape.output_constraints as the LLM planner's interpreted completion contract. Reason from it together with the raw request and satisfy its response scope, counts, sections, source visibility, forbidden content, and style requirements.",
                "- When output_constraints requests a narrow answer-only response, put the compliant response in answer and leave synthesis and optional sections empty unless the request explicitly requires them. Shared Detailed Summary defaults must not override a narrower operator ask.",
                "- If source_triage_notes says broaden/deepen is recommended, state what is missing or thin before making strong conclusions; use retained sources first and avoid padding with weak adjacent sources.",
                "- If source_context_notes says extracted page evidence is missing, do not write a detailed factual synthesis from provider snippets alone; state the limitation and recommend reading/extracting the relevant URLs.",
                "- If source_context_notes says selected sources do not match the request focus, do not treat broad or adjacent sources as answering the focused ask; state the mismatch and use focused provider candidates only as follow-up targets unless they were extracted.",
                "- If the latest request asks to summarize link N, source N, or a source from the prior Slack thread, resolve N from ordered_sources and focus on that prior source first. Do not broaden into a new topic brief or provider comparison unless the linked source is missing.",
                "- Preserve requested output shape for narrow follow-ups, especially bullet counts such as 3-5 bullets.",
                "- When comparing search lanes, describe what each provider_result lane contributed in the answer or synthesis, then keep provider status/counts in metadata outside the synthesis.",
                "- When source-backed claims are present, include primary source links where available; prefer official, job posting, filing, publication, or other primary URLs over secondary summaries.",
                "- When the latest request asks for citations, sources, links, URLs, or weblinks, include the available source URLs from payload sources or artifact metadata.",
                "- When naming artifacts, companies, opportunities, or comparison rows, cite source URLs attached to those same artifacts; do not pair a named item with unrelated global sources.",
                "- When the request asks for a table, comparison table, or compact comparison and the payload contains multiple relevant artifacts, use a compact Markdown table.",
                "- If requested source URLs are unavailable in the payload, say that explicitly instead of implying they were not found.",
                "- Do not say search was limited when retrieval diagnostics show broad search execution; say independent corroboration or selected evidence is limited instead.",
                "- Put partial, blocked, not decision-ready, and approval/no-send notes in caveats.",
                "- Leave key_points and next_step empty when they would repeat the answer, blockers, or caveats.",
                "- Do not expose raw Orchestrator review feedback, evaluator gap labels, or workflow metadata in the user-facing answer.",
                "- If the result is blocked or off-target, explain why in caveats instead of opening with it.",
                "- Keep approval/no-send/write safety boundaries intact.",
                "- Do not use 'If you want...' follow-up offers; include only concrete next actions required by the request or safety state.",
                "- Put the short direct answer in `answer`.",
                "- Put the detailed, source-aware answer in the `synthesis` field; it will be rendered as the visible `Detailed Summary` section.",
                "- For search/research requests, the Detailed Summary should be richer than generic LLM search: summarize the selected source data first, then explain Keystone/operator relevance, disagreements, gaps, and uncertainty.",
                "- For search/research requests, the Detailed Summary must begin with a narrative summary paragraph that explains what the selected evidence means. Do not begin with a source list, provider list, metadata, or bullets.",
                "- In the Detailed Summary, explain the most relevant findings, what the provider lanes or selected sources added, and what remains uncertain. Do not put provider counts, route status, workflow ids, or credit diagnostics there.",
                "- Put 3-6 source-link bullets in `source_evidence` for search/research answers when source URLs are available. Each bullet should include the source title or organization, URL, and one sentence on what that source contributed.",
                "- Put glossary bullets in `terms` when the latest request asks for terms or when central acronyms, agency names, technical terms, provider names, or field shorthand may not be obvious.",
                "- Put concrete operator next steps in `recommended_actions` when the user asks for next steps or when the findings naturally require action. Keep internal workflow review labels out of this field.",
                "",
                "Payload JSON:",
                json.dumps(self.model_dump(mode="json"), ensure_ascii=True, sort_keys=True),
            ]
        )


def run_typed_sdk_agent(**kwargs: Any) -> Any:
    """Lazy wrapper kept patchable without importing keystone_agents.run at module load."""

    from keystone_agents.run import run_typed_sdk_agent as _run_typed_sdk_agent

    return _run_typed_sdk_agent(**kwargs)


def build_user_response_synthesis_agent(model: str | None = None):
    from keystone_agents.sdk import build_model_settings, build_sdk_agent, compose_instructions

    return build_sdk_agent(
        name="user_response_synthesizer",
        instructions=compose_instructions(
            "keystone_profile.md",
            "safety_policy.md",
            "writing_style.md",
            "slack-posting-rules.md",
        ),
        output_type=UserFacingResponseSynthesis,
        tools=[],
        model=model,
        model_settings=build_model_settings(reasoning_effort="low", max_tokens=1400),
        enforce_tool_policy=False,
    )


def synthesize_user_facing_work_item_response(
    result: WorkflowRunResult,
    *,
    user_request: str,
    live: bool,
    session: Any | None = None,
) -> UserFacingResponseSynthesis:
    """Run the final live synthesis layer for a deterministic WorkItem result."""

    sdk_result = synthesize_user_facing_work_item_response_sdk_result(
        result,
        user_request=user_request,
        live=live,
        session=session,
    )
    return sdk_result.output


def synthesize_user_facing_work_item_response_sdk_result(
    result: WorkflowRunResult,
    *,
    user_request: str,
    live: bool,
    session: Any | None = None,
) -> Any:
    """Run final response synthesis and return the typed SDK result envelope."""

    synthesis_input = _user_response_synthesis_input(result, user_request=user_request)
    return run_typed_sdk_agent(
        agent=build_user_response_synthesis_agent(),
        typed_input=synthesis_input,
        output_type=UserFacingResponseSynthesis,
        live=live,
        session=session,
        workflow_name="Keystone user-facing response synthesis",
        max_turns=2,
    )


def _user_response_synthesis_input(
    result: WorkflowRunResult,
    *,
    user_request: str,
) -> UserFacingResponseSynthesisInput:
    return UserFacingResponseSynthesisInput(
        user_request=user_request,
        latest_user_request=latest_user_request(user_request),
        agent_name=result.route.value,
        route=result.route.value,
        status=result.status.value,
        advanced=result.advanced,
        deterministic_summary=result.human_summary,
        artifacts=[
            {
                "artifact_type": artifact.artifact_type,
                "title": artifact.title,
                "summary": artifact.summary,
                "approval_state": artifact.approval_state,
                "metadata": _compact_mapping(artifact.metadata),
            }
            for artifact in result.artifact_refs[:8]
        ],
        sources=response_synthesis_sources(result),
        ordered_sources=response_synthesis_ordered_sources(result),
        source_data_summaries=response_synthesis_source_data_summaries(result),
        provider_results=response_synthesis_provider_results(result),
        source_context_notes=response_synthesis_source_context_notes(result),
        source_triage_notes=response_synthesis_source_triage_notes(result),
        source_triage=response_synthesis_source_triage(result),
        request_coverage=response_synthesis_request_coverage(result),
        request_coverage_required=_request_coverage_required(result.manual_request_plan),
        blockers=[
            {
                "code": blocker.code,
                "message": blocker.message,
                "severity": blocker.severity,
            }
            for blocker in result.blockers[:6]
        ],
        next_action=(
            result.next_action.model_dump(mode="json") if result.next_action is not None else None
        ),
        manual_plan=result.manual_request_plan,
        orchestrator_reviews=_compact_orchestrator_reviews(result),
        audit_notes=result.audit_notes[:8],
    )


def response_synthesis_sources(result: WorkflowRunResult) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    triage_filter = _response_synthesis_triage_filter(result)

    def add_source(source: Any, *, artifact_title: str = "") -> None:
        if not isinstance(source, dict):
            return
        url = str(source.get("url") or "").strip()
        title = str(source.get("title") or "").strip()
        source_id = str(source.get("source_id") or "").strip()
        if not url or url in seen_urls or _is_internal_fixture_url(url):
            return
        if (
            (source_id and source_id in triage_filter["rejected_ids"])
            or url in triage_filter["rejected_urls"]
            or (source_id and source_id in triage_filter["deepen_ids"])
            or url in triage_filter["deepen_urls"]
        ):
            return
        if (
            triage_filter["retained_ids"]
            and source_id
            and source_id not in triage_filter["retained_ids"]
        ):
            return
        if (
            triage_filter["retained_urls"]
            and not source_id
            and url not in triage_filter["retained_urls"]
        ):
            return
        key_facts = _source_key_facts(source)
        supported_claim = str(source.get("supported_claim") or "").strip()
        if not supported_claim and key_facts:
            supported_claim = key_facts[0]
        seen_urls.add(url)
        sources.append(
            {
                "title": title,
                "url": url,
                "source_type": str(source.get("source_type") or ""),
                "source_id": source_id,
                "supported_claim": supported_claim,
                "extraction_status": str(source.get("extraction_status") or ""),
                "evidence_excerpt": str(source.get("evidence_excerpt") or "")[:900],
                "key_facts": key_facts[:5],
                "artifact_title": artifact_title,
            }
        )

    for artifact in result.artifact_refs[:8]:
        metadata = artifact.metadata if isinstance(artifact.metadata, dict) else {}
        refs = metadata.get("source_refs")
        if isinstance(refs, list):
            for ref in refs[:8]:
                add_source(ref, artifact_title=artifact.title)
    for source in result.work_item.sources:
        add_source(
            {
                "title": source.title,
                "url": source.url,
                "source_type": source.source_type,
                "source_id": source.source_id,
                "supported_claim": source.supported_claim,
                "extraction_status": source.extraction_status,
                "evidence_excerpt": source.evidence_excerpt[:900],
                "key_facts": source.key_facts[:5],
            }
        )
        if len(sources) >= 12:
            break
    if len(sources) < 12:
        context_pack = result.context_pack if isinstance(result.context_pack, dict) else {}
        for key in ("source_context_sample", "ordered_sources"):
            context_sources = context_pack.get(key)
            if not isinstance(context_sources, list):
                continue
            for source in context_sources[:8]:
                add_source(source)
                if len(sources) >= 12:
                    break
            if len(sources) >= 12:
                break
    return sources[:12]


def _response_synthesis_triage_filter(result: WorkflowRunResult) -> dict[str, set[str]]:
    retained_ids: set[str] = set()
    rejected_ids: set[str] = set()
    deepen_ids: set[str] = set()
    retained_urls: set[str] = set()
    rejected_urls: set[str] = set()
    deepen_urls: set[str] = set()
    for diagnostics in _result_retrieval_diagnostics(result):
        triage = diagnostics.get("source_triage")
        if not isinstance(triage, dict):
            continue
        retained_ids.update(
            str(item or "").strip()
            for item in (triage.get("retained_source_ids") or [])
            if str(item or "").strip()
        )
        rejected_ids.update(
            str(item or "").strip()
            for item in (triage.get("rejected_source_ids") or [])
            if str(item or "").strip()
        )
        deepen_ids.update(
            str(item or "").strip()
            for item in (triage.get("deepen_source_ids") or [])
            if str(item or "").strip()
        )
        retained_urls.update(
            str(item or "").strip()
            for item in (triage.get("retained_urls") or [])
            if str(item or "").strip()
        )
        rejected_urls.update(
            str(item or "").strip()
            for item in (triage.get("rejected_urls") or [])
            if str(item or "").strip()
        )
        deepen_urls.update(
            str(item or "").strip()
            for item in (triage.get("deepen_urls") or [])
            if str(item or "").strip()
        )
        decisions = triage.get("decisions")
        if isinstance(decisions, list):
            for item in decisions:
                if not isinstance(item, dict):
                    continue
                decision = str(item.get("decision") or "").strip().lower()
                url = str(item.get("url") or "").strip()
                source_id = str(item.get("source_id") or "").strip()
                if decision == "deepen":
                    if source_id:
                        deepen_ids.add(source_id)
                    if url:
                        deepen_urls.add(url)
                    continue
                if not url:
                    continue
                if decision == "retain":
                    retained_urls.add(url)
                elif decision == "reject":
                    rejected_urls.add(url)
    return {
        "retained_ids": retained_ids,
        "rejected_ids": rejected_ids,
        "deepen_ids": deepen_ids,
        "retained_urls": retained_urls,
        "rejected_urls": rejected_urls,
        "deepen_urls": deepen_urls,
    }


def _source_key_facts(source: dict[str, Any]) -> list[str]:
    facts: list[str] = []
    for key in ("supported_claims", "key_facts"):
        values = source.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            value = str(item or "").strip()
            if value and value not in facts:
                facts.append(value)
    return facts


def response_synthesis_source_data_summaries(result: WorkflowRunResult) -> list[str]:
    """Return compact deterministic source-data summaries for final synthesis."""

    summaries: list[str] = []
    for source in response_synthesis_sources(result)[:8]:
        summary = _source_data_summary(source)
        if summary:
            summaries.append(summary)
    return summaries


def _source_data_summary(source: dict[str, Any]) -> str:
    url = str(source.get("url") or "").strip()
    title = str(source.get("title") or "").strip() or url or "Source"
    status = str(source.get("extraction_status") or "").strip()
    facts = _source_data_facts(source)
    if not facts:
        return ""
    status_suffix = f" ({status})" if status else ""
    source_suffix = f" Source: {url}" if url else ""
    return f"{title[:180]}{status_suffix}: {' '.join(facts[:5])}{source_suffix}"


def _source_data_facts(source: dict[str, Any]) -> list[str]:
    facts: list[str] = []
    excerpt = str(source.get("evidence_excerpt") or "").strip()
    if excerpt:
        facts.extend(_source_excerpt_sentences(excerpt))
    claim = str(source.get("supported_claim") or "").strip()
    if claim:
        facts.append(_first_sentence(claim, max_chars=240))
    claims = source.get("supported_claims")
    if isinstance(claims, list):
        for item in claims:
            value = str(item or "").strip()
            if value:
                facts.append(_first_sentence(value, max_chars=220))
    key_facts = source.get("key_facts")
    if isinstance(key_facts, list):
        for item in key_facts:
            value = str(item or "").strip()
            if value:
                facts.append(_first_sentence(value, max_chars=220))
    for item in _source_key_facts(source):
        value = str(item or "").strip()
        if value:
            facts.append(_first_sentence(value, max_chars=220))
    return _dedupe_text_items(facts)[:5]


def _source_excerpt_sentences(
    excerpt: str,
    *,
    max_sentences: int = 3,
    max_chars: int = 700,
) -> list[str]:
    cleaned = " ".join(str(excerpt or "").split()).strip()
    if not cleaned:
        return []
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", cleaned) if part.strip()]
    if not parts:
        return [cleaned[:max_chars]]
    selected: list[str] = []
    used_chars = 0
    for part in parts:
        remaining = max_chars - used_chars
        if remaining <= 0 or len(selected) >= max_sentences:
            break
        sentence = part if len(part) <= remaining else part[: remaining - 1].rstrip() + "..."
        selected.append(sentence)
        used_chars += len(sentence) + 1
    return selected


def _dedupe_text_items(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = " ".join(str(item or "").split()).strip()
        if not value:
            continue
        normalized = value.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(value)
    return deduped


def response_synthesis_ordered_sources(result: WorkflowRunResult) -> list[dict[str, Any]]:
    """Return source order hints for final synthesis of natural source follow-ups."""

    context_pack = result.context_pack if isinstance(result.context_pack, dict) else {}
    ordered = context_pack.get("ordered_sources")
    if isinstance(ordered, list):
        normalized = [
            _compact_ordered_source(item, fallback_index=index)
            for index, item in enumerate(ordered[:12], start=1)
            if isinstance(item, dict)
        ]
        normalized = [
            item
            for item in normalized
            if item.get("url")
            and not _is_internal_fixture_url(str(item.get("url") or ""))
        ]
        if normalized:
            return normalized
    return [
        _compact_ordered_source(source, fallback_index=index)
        for index, source in enumerate(response_synthesis_sources(result), start=1)
    ][:12]


def _compact_ordered_source(source: dict[str, Any], *, fallback_index: int) -> dict[str, Any]:
    index = source.get("index")
    try:
        resolved_index = int(index)
    except (TypeError, ValueError):
        resolved_index = fallback_index
    return {
        "index": resolved_index,
        "reference": str(source.get("reference") or f"source {resolved_index}"),
        "title": str(source.get("title") or "")[:180],
        "url": str(source.get("url") or ""),
        "source_type": str(source.get("source_type") or ""),
        "extraction_status": str(source.get("extraction_status") or ""),
        "supported_claim": str(source.get("supported_claim") or "")[:360],
        "evidence_excerpt": str(source.get("evidence_excerpt") or "")[:700],
    }


def _is_internal_fixture_url(url: str) -> bool:
    """Keep synthetic test/source identifiers out of user-facing source lists."""

    return str(url or "").strip().lower().startswith("fixture://")


def response_synthesis_provider_results(result: WorkflowRunResult) -> list[dict[str, Any]]:
    """Return compact top search-lane samples for final answer synthesis."""

    provider_results: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for diagnostics in _result_retrieval_diagnostics(result):
        samples = diagnostics.get("provider_result_samples")
        if not isinstance(samples, dict):
            continue
        for provider, items in samples.items():
            provider_name = str(provider or "").strip()
            if not provider_name or not isinstance(items, list):
                continue
            added_for_provider = 0
            for item in items:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                title = str(item.get("title") or "").strip()
                if not url and not title:
                    continue
                key = (provider_name, url or title)
                if key in seen:
                    continue
                seen.add(key)
                provider_results.append(
                    {
                        "provider": provider_name,
                        "title": title,
                        "url": url,
                        "snippet": str(item.get("snippet") or "")[:500],
                    }
                )
                added_for_provider += 1
                if added_for_provider >= 3 or len(provider_results) >= 16:
                    break
            if len(provider_results) >= 16:
                break
        if len(provider_results) >= 16:
            break
    return provider_results


def response_synthesis_source_context_notes(result: WorkflowRunResult) -> list[str]:
    """Describe whether source-backed synthesis has extracted page context."""

    sources = response_synthesis_sources(result)
    provider_results = response_synthesis_provider_results(result)
    notes: list[str] = []
    for diagnostics in _result_retrieval_diagnostics(result):
        source_focus = diagnostics.get("source_focus")
        note = _source_focus_warning_note(source_focus)
        if note:
            notes.append(note)
    context_focus_note = _source_focus_warning_note(_result_context_pack_source_focus(result))
    if context_focus_note and context_focus_note not in notes:
        notes.append(context_focus_note)
    if not sources:
        if not provider_results:
            return notes
        return [
            *notes,
            (
                "Search provider URLs were discovered, but no selected source URLs or "
                "extracted page evidence reached final synthesis."
            ),
        ]
    sources_with_evidence = [
        source
        for source in sources
        if str(source.get("evidence_excerpt") or "").strip()
        or str(source.get("supported_claim") or "").strip()
        or any(str(item or "").strip() for item in (source.get("key_facts") or []))
    ]
    if not sources_with_evidence:
        return [
            *notes,
            (
                "Selected source URLs reached final synthesis, but they do not include "
                "extracted evidence excerpts, supported claims, or key facts."
            ),
        ]
    extracted_count = sum(1 for source in sources if _source_is_extracted(source))
    if extracted_count == 0:
        notes.append(
            f"{len(sources_with_evidence)} selected source URL(s) include claims or "
            "snippets, but no selected source is marked as extracted/read."
        )
        return notes
    notes.append(
        f"{extracted_count}/{len(sources)} selected source URL(s) are marked "
        "as extracted/read for synthesis."
    )
    return notes


def response_synthesis_source_triage_notes(result: WorkflowRunResult) -> list[str]:
    """Return compact source-selection notes for final answer synthesis."""

    notes: list[str] = []
    for triage in response_synthesis_source_triage(result):
        action = triage.recommended_action.strip()
        counts = triage.decision_counts
        parts: list[str] = []
        if isinstance(counts, dict):
            for key in ("retain", "review", "deepen", "reject"):
                value = counts.get(key)
                if value:
                    parts.append(f"{key}: {int(value)}")
        count_text = ", ".join(parts)
        needs_more = triage.needs_broaden_or_deepen
        gaps = triage.recall_gaps[:3]
        if action or count_text or needs_more or gaps:
            note = "Source triage"
            if action:
                note += f": {action}"
            if count_text:
                note += f"; decisions: {count_text}"
            if needs_more:
                note += "; broaden/deepen recommended before strong final synthesis"
            if gaps:
                note += "; gaps: " + "; ".join(gaps)
            notes.append(note)
    return list(dict.fromkeys(notes))[:4]


def response_synthesis_source_triage(result: WorkflowRunResult) -> list[SourceTriageSummary]:
    """Return deduplicated typed source-selection contracts for final synthesis."""

    summaries: list[SourceTriageSummary] = []
    seen: set[str] = set()
    for diagnostics in _result_retrieval_diagnostics(result):
        summary = SourceTriageSummary.from_payload(diagnostics.get("source_triage"))
        if not summary.has_evidence():
            continue
        key = summary.model_dump_json(exclude_defaults=True)
        if key in seen:
            continue
        seen.add(key)
        summaries.append(summary)
    return summaries[:4]


def response_synthesis_request_coverage(result: WorkflowRunResult) -> list[RequestCoverage]:
    """Collect validated specialist request-coverage envelopes from bounded results."""

    candidates: list[Any] = []
    for artifact in [*result.artifact_refs, *result.work_item.artifact_refs]:
        if isinstance(artifact.metadata, dict):
            candidates.extend(_nested_values_for_key(artifact.metadata, "request_coverage"))
    for nested in result.nested_specialist_results:
        if isinstance(nested, dict):
            candidates.extend(_nested_values_for_key(nested, "request_coverage"))
    coverage_rows: list[RequestCoverage] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            coverage = RequestCoverage.model_validate(candidate)
        except (TypeError, ValueError):
            continue
        key = coverage.model_dump_json(exclude_defaults=True)
        if key in seen:
            continue
        seen.add(key)
        coverage_rows.append(coverage)
    return coverage_rows[:6]


def _nested_values_for_key(
    value: Any,
    key: str,
    *,
    depth: int = 0,
) -> list[Any]:
    if depth > 4:
        return []
    found: list[Any] = []
    if isinstance(value, dict):
        if key in value:
            found.append(value[key])
        for nested in value.values():
            if isinstance(nested, dict | list):
                found.extend(_nested_values_for_key(nested, key, depth=depth + 1))
    elif isinstance(value, list):
        for nested in value[:12]:
            if isinstance(nested, dict | list):
                found.extend(_nested_values_for_key(nested, key, depth=depth + 1))
    return found


def _request_coverage_required(manual_plan: dict[str, Any] | None) -> bool:
    if not isinstance(manual_plan, dict):
        return False
    ask_shape = manual_plan.get("ask_shape")
    if not isinstance(ask_shape, dict):
        return False
    output_constraints = ask_shape.get("output_constraints")
    output_constraints_explicit = bool(
        isinstance(output_constraints, dict)
        and any(
            output_constraints.get(key) not in (None, "", [], False, "unspecified")
            for key in (
                "interpretation",
                "scope",
                "word_count_mode",
                "word_count",
                "sentence_count_mode",
                "sentence_count",
                "item_count_mode",
                "minimum_items",
                "maximum_items",
                "required_sections",
                "require_section_headings",
                "forbidden_phrases",
                "forbid_em_dash",
                "include_source_urls",
                "style_requirements",
            )
        )
    )
    return bool(
        str(ask_shape.get("stop_condition") or "").strip()
        or output_constraints_explicit
        or str(ask_shape.get("strict_filter_mode") or "")
        not in {"", "unspecified"}
        or str(ask_shape.get("output_form") or "") not in {"", "unspecified"}
    )


def response_synthesis_metadata_lines(result: WorkflowRunResult) -> list[str]:
    """Return compact deterministic metadata lines for Slack/CLI search outputs."""

    diagnostics = _result_retrieval_diagnostics(result)
    provider_results = response_synthesis_provider_results(result)
    sources = response_synthesis_sources(result)
    source_notes = response_synthesis_source_context_notes(result)
    lines: list[str] = []

    query_line = _metadata_search_query_line(diagnostics)
    if query_line:
        lines.append(query_line)

    provider_line = _metadata_provider_line(diagnostics, provider_results)
    if provider_line:
        lines.append(provider_line)

    usage_line = _metadata_provider_usage_line(diagnostics, provider_results)
    if usage_line:
        lines.append(usage_line)

    source_context_line = _metadata_source_context_line(sources, source_notes)
    if source_context_line:
        lines.append(source_context_line)

    focus_line = _metadata_source_focus_line(
        diagnostics
    ) or _metadata_context_pack_source_focus_line(result)
    if focus_line:
        lines.append(focus_line)

    top_urls_line = _metadata_provider_top_urls_line(provider_results)
    if top_urls_line:
        lines.append(top_urls_line)

    error_line = _metadata_error_line(diagnostics)
    if error_line:
        lines.append(error_line)

    return lines[:7]


def _metadata_search_query_line(diagnostics: list[dict[str, Any]]) -> str:
    for item in diagnostics:
        queries = item.get("search_queries")
        if isinstance(queries, list):
            cleaned = [str(query).strip() for query in queries if str(query).strip()]
            if not cleaned:
                continue
            if len(cleaned) == 1:
                return f"Search query: `{cleaned[0][:180]}`"
            shown = "; ".join(query[:120] for query in cleaned[:3])
            suffix = f"; +{len(cleaned) - 3} more" if len(cleaned) > 3 else ""
            return f"Search queries: `{shown}{suffix}`"
    return ""


def _metadata_provider_line(
    diagnostics: list[dict[str, Any]],
    provider_results: list[dict[str, Any]],
) -> str:
    providers: list[str] = []
    for item in diagnostics:
        summary = str(item.get("provider_summary") or "").strip()
        if summary:
            providers.extend(_split_provider_names(summary))
        used = item.get("providers_used")
        if isinstance(used, list):
            providers.extend(str(provider).strip() for provider in used if str(provider).strip())
    providers.extend(
        str(item.get("provider") or "").strip()
        for item in provider_results
        if str(item.get("provider") or "").strip()
    )
    providers = list(dict.fromkeys(provider for provider in providers if provider))
    if not providers:
        return ""
    return f"Search providers: {', '.join(providers[:8])}"


def _metadata_provider_usage_line(
    diagnostics: list[dict[str, Any]],
    provider_results: list[dict[str, Any]],
) -> str:
    usage_by_provider: dict[str, str] = {}
    for item in diagnostics:
        usage = item.get("provider_usage")
        if not isinstance(usage, dict):
            continue
        for provider, provider_usage in usage.items():
            if not isinstance(provider_usage, dict):
                continue
            attempted = int(provider_usage.get("requests_attempted") or 0)
            succeeded = int(provider_usage.get("requests_succeeded") or 0)
            credits = int(provider_usage.get("credits_used") or 0)
            parts = [f"{succeeded}/{attempted} ok" if attempted else f"{succeeded} ok"]
            if credits:
                parts.append(f"{credits} credits")
            usage_by_provider[str(provider)] = ", ".join(parts)
    if usage_by_provider:
        return "Provider usage: " + "; ".join(
            f"{provider}: {summary}" for provider, summary in list(usage_by_provider.items())[:6]
        )

    counts: dict[str, int] = {}
    for item in provider_results:
        provider = str(item.get("provider") or "").strip()
        if provider:
            counts[provider] = counts.get(provider, 0) + 1
    if counts:
        return "Provider samples: " + "; ".join(
            f"{provider}: {count} shown" for provider, count in list(counts.items())[:6]
        )
    return ""


def _metadata_source_context_line(
    sources: list[dict[str, Any]],
    source_notes: list[str],
) -> str:
    if not sources and not source_notes:
        return ""
    evidence_count = sum(
        1
        for source in sources
        if str(source.get("evidence_excerpt") or "").strip()
        or str(source.get("supported_claim") or "").strip()
        or any(str(item or "").strip() for item in (source.get("key_facts") or []))
    )
    extracted_count = sum(1 for source in sources if _source_is_extracted(source))
    if sources:
        statuses = _source_status_summary(sources)
        status_suffix = f"; statuses: {statuses}" if statuses else ""
        return (
            f"Source context: {extracted_count}/{len(sources)} selected URLs extracted/read; "
            f"{evidence_count}/{len(sources)} include evidence/claims{status_suffix}"
        )
    return f"Source context: {source_notes[0]}"


def _source_is_extracted(source: dict[str, Any]) -> bool:
    status = str(source.get("extraction_status") or "").strip().lower()
    return status in {
        "success",
        "extracted",
        "read",
        "extracted/read",
        "article_read",
        "page_read",
    }


def _source_status_summary(sources: list[dict[str, Any]]) -> str:
    statuses: list[str] = []
    for source in sources:
        status = str(source.get("extraction_status") or "").strip()
        if status:
            statuses.append(status)
    return ", ".join(list(dict.fromkeys(statuses))[:5])


def _metadata_source_focus_line(diagnostics: list[dict[str, Any]]) -> str:
    for item in diagnostics:
        focus = item.get("source_focus")
        if not isinstance(focus, dict) or not focus.get("status"):
            continue
        selected = int(focus.get("selected_source_count") or 0)
        matching = int(focus.get("matching_source_count") or 0)
        terms = ", ".join(str(term) for term in (focus.get("terms") or [])[:5])
        suffix = f"; terms: {terms}" if terms else ""
        return (
            f"Source focus: {focus.get('status')}; {matching}/{selected} selected matched{suffix}"
        )
    return ""


def _metadata_context_pack_source_focus_line(result: WorkflowRunResult) -> str:
    focus = _result_context_pack_source_focus(result)
    if not focus:
        return ""
    sample_count = int(focus.get("sample_count") or 0)
    matching = int(focus.get("matching_sample_count") or 0)
    terms = ", ".join(str(term) for term in (focus.get("terms") or [])[:5])
    suffix = f"; terms: {terms}" if terms else ""
    return (
        f"Source focus: {focus.get('status')}; "
        f"{matching}/{sample_count} sample sources matched{suffix}"
    )


def _result_context_pack_source_focus(result: WorkflowRunResult) -> dict[str, Any]:
    context_pack = result.context_pack if isinstance(result.context_pack, dict) else {}
    focus = context_pack.get("source_context_focus")
    return focus if isinstance(focus, dict) else {}


def _source_focus_warning_note(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    status = str(value.get("status") or "")
    if status not in {"no_selected_source_matches_focus", "no_sample_source_matches_focus"}:
        return ""
    terms = ", ".join(str(item) for item in (value.get("terms") or [])[:6])
    noun = (
        "sampled source evidence"
        if status == "no_sample_source_matches_focus"
        else "selected source URLs"
    )
    return (
        f"{noun.capitalize()} did not match the request focus terms"
        f"{f' ({terms})' if terms else ''}; do not treat broad sources as "
        "satisfying the focused research ask."
    )


def _metadata_provider_top_urls_line(provider_results: list[dict[str, Any]]) -> str:
    if not provider_results:
        return ""
    top_by_provider: dict[str, str] = {}
    for item in provider_results:
        provider = str(item.get("provider") or "").strip()
        url = str(item.get("url") or "").strip()
        if provider and url and provider not in top_by_provider:
            top_by_provider[provider] = url
        if len(top_by_provider) >= 4:
            break
    if not top_by_provider:
        return ""
    return "Provider top URLs: " + "; ".join(
        f"{provider}: {_compact_provider_metadata_url(url)}"
        for provider, url in top_by_provider.items()
    )


def _compact_provider_metadata_url(url: str, *, max_length: int = 72) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.netloc and "://" not in raw:
        parsed = urlparse(f"https://{raw}")
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return _truncate_middle(raw.split("#", 1)[0].split("?", 1)[0], max_length)
    path = parsed.path or ""
    compact = f"{host}{path}" if path else host
    if len(compact) > max_length:
        segments = [segment for segment in path.split("/") if segment]
        if segments:
            suffix = segments[-1]
            if path.endswith("/") and not suffix.endswith("/"):
                suffix = f"{suffix}/"
            compact = f"{host}/.../{suffix}"
    return _truncate_middle(compact, max_length)


def _truncate_middle(value: str, max_length: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_length:
        return text
    if max_length <= 8:
        return text[:max_length]
    prefix_length = max_length // 2 - 2
    suffix_length = max_length - prefix_length - 3
    return f"{text[:prefix_length]}...{text[-suffix_length:]}"


def _metadata_error_line(diagnostics: list[dict[str, Any]]) -> str:
    errors: list[str] = []
    for item in diagnostics:
        value = item.get("errors")
        if isinstance(value, list):
            errors.extend(str(error).strip() for error in value if str(error).strip())
        elif isinstance(value, dict):
            errors.extend(
                f"{key}: {summary}" for key, summary in value.items() if str(summary).strip()
            )
    errors = list(dict.fromkeys(errors))
    if not errors:
        return ""
    return "Search errors: " + "; ".join(errors[:3])


def _sources_are_snippet_only_without_extraction(sources: Any | None) -> bool:
    if not isinstance(sources, list) or not sources:
        return False
    evidence_sources = [
        source
        for source in sources
        if isinstance(source, dict)
        and (
            str(source.get("evidence_excerpt") or "").strip()
            or str(source.get("supported_claim") or "").strip()
            or any(str(item or "").strip() for item in (source.get("key_facts") or []))
        )
    ]
    if not evidence_sources:
        return False
    return not any(_source_is_extracted(source) for source in evidence_sources)


def _synthesis_acknowledges_source_context_limit(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    if not normalized:
        return False
    return bool(
        re.search(
            r"\b(?:snippet[- ]only|not extracted|not read|no selected source is marked "
            r"as extracted|limited basis|limited to (?:the )?(?:snippet|search result|"
            r"retrieved excerpt)|page extraction (?:failed|unavailable|missing))\b",
            normalized,
        )
    )


def _split_provider_names(value: str) -> list[str]:
    return [item.strip() for item in value.replace("+", ",").split(",") if item.strip()]


def _result_retrieval_diagnostics(result: WorkflowRunResult) -> list[dict[str, Any]]:
    diagnostics: list[dict[str, Any]] = []
    for artifact in [*result.artifact_refs, *result.work_item.artifact_refs]:
        metadata = artifact.metadata if isinstance(artifact.metadata, dict) else {}
        source_triage = metadata.get("source_triage")
        if isinstance(source_triage, dict):
            diagnostics.append({"source_triage": source_triage})
        for value in (
            metadata.get("retrieval_diagnostics"),
            metadata.get("retrieval"),
            metadata.get("live_search_metadata"),
        ):
            if isinstance(value, dict):
                nested = value.get("retrieval_diagnostics")
                diagnostics.append(nested if isinstance(nested, dict) else value)
    deduped: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for item in diagnostics:
        item_id = id(item)
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        deduped.append(item)
    return deduped


def latest_user_request(user_request: str) -> str:
    """Return the newest operator ask from a thread-continuation style request."""

    text = str(user_request or "").strip()
    if not text:
        return ""
    markers = (
        "\nFollow-up:",
        "\nFollow up:",
        "\nLatest request:",
        "\nCurrent request:",
        "\nNew request:",
    )
    lower = text.lower()
    best_index = -1
    best_marker = ""
    for marker in markers:
        index = lower.rfind(marker.lower())
        if index > best_index:
            best_index = index
            best_marker = marker
    if best_index >= 0:
        latest = text[best_index + len(best_marker) :].strip()
        return latest or text
    flexible_markers = (
        r"\n\s*(?:@\S+\s+)?(?:user\s+)?follow[- ]?up(?:\s+[^:\n]{1,80})?:\s*",
        r"\n\s*(?:@\S+\s+)?(?:user\s+)?follow[- ]?up\s+",
    )
    for pattern in flexible_markers:
        matches = list(re.finditer(pattern, text, flags=re.IGNORECASE))
        if matches:
            latest = text[matches[-1].end() :].strip()
            return latest or text
    return text


def format_user_response_synthesis(
    synthesis: UserFacingResponseSynthesis,
    *,
    sources: Any | None = None,
    metadata_lines: list[str] | None = None,
    low_metadata: bool = False,
) -> str:
    """Render synthesized response fields into compact Slack-readable text."""

    answer = _strip_redundant_answer_sections(synthesis.answer.strip())
    answer = _strip_trailing_followup_offer(answer)
    answer, demoted_notes = _demote_leading_status_sentence(answer)
    source_focus_mismatch = _metadata_lines_indicate_source_focus_mismatch(metadata_lines)
    fallback_answer = "" if source_focus_mismatch else _fallback_answer_from_sources(sources)
    if fallback_answer and _looks_like_workflow_only_answer(answer):
        answer = fallback_answer
    elif source_focus_mismatch and _looks_like_workflow_only_answer(answer):
        demoted_notes.append(
            "Source focus mismatch: selected source evidence did not match the request focus."
        )
        answer = (
            "The selected source context does not match the request focus closely "
            "enough to support a substantive answer."
        )
    raw_synthesis = str(getattr(synthesis, "synthesis", "") or "")
    synthesis_text = _strip_trailing_followup_offer(
        _strip_redundant_answer_sections(raw_synthesis.strip())
    )
    if not synthesis_text and not source_focus_mismatch:
        synthesis_text = _fallback_synthesis_from_sources(sources)
    if (
        synthesis_text
        and _sources_are_snippet_only_without_extraction(sources)
        and not _synthesis_acknowledges_source_context_limit(synthesis_text)
    ):
        demoted_notes.append(
            "Source extraction limitation: selected source URLs were snippet-only or "
            "not extracted/read, so detailed page-level claims were not used."
        )
        synthesis_text = _fallback_synthesis_from_sources(sources)
    if synthesis_text and _source_backed_synthesis_lacks_summary(synthesis_text, sources):
        summary = _source_summary_from_sources(sources)
        if summary:
            synthesis_text = f"{summary}\n\n{synthesis_text}"
    caveats = [
        *demoted_notes,
        *(item.strip() for item in getattr(synthesis, "caveats", []) if item.strip()),
    ]

    lines: list[str] = []
    title = synthesis.title.strip()
    if title and not _looks_like_workflow_only_answer(title):
        lines.append(title)
        lines.append("")
    if answer.strip():
        if synthesis_text:
            lines.extend(["*Answer:*", answer.strip()])
        else:
            lines.append(answer.strip())
    if synthesis_text:
        lines.extend(["", "*Detailed Summary:*", synthesis_text])
    source_evidence = _source_evidence_lines(
        getattr(synthesis, "source_evidence", []),
        sources=sources,
        rendered_text="\n".join(lines),
    )
    if source_evidence:
        lines.extend(["", "Source evidence"])
        lines.extend(f"* {item}" for item in source_evidence)
    terms = _clean_section_items(getattr(synthesis, "terms", []), rendered_text="\n".join(lines))
    if terms and not low_metadata:
        lines.extend(["", "Terms"])
        lines.extend(f"* {item}" for item in terms)
    recommended_actions = _clean_section_items(
        getattr(synthesis, "recommended_actions", []),
        rendered_text="\n".join(lines),
    )
    next_step = str(getattr(synthesis, "next_step", "") or "")
    if not recommended_actions and next_step.strip():
        recommended_actions = _clean_section_items(
            [next_step],
            rendered_text="\n".join(lines),
        )
    if recommended_actions:
        lines.extend(["", "Recommended actions"])
        lines.extend(f"* {item}" for item in recommended_actions)
    if caveats and not low_metadata:
        lines.extend(["", "Run notes"])
        lines.extend(f"* {item}" for item in caveats)
    metadata = (
        []
        if low_metadata
        else _clean_section_items(metadata_lines or [], rendered_text="\n".join(lines))
    )
    body_text = "\n".join(line for line in lines if line is not None).strip()
    body_text = append_visible_source_urls_to_text(body_text, sources)
    if metadata and not _has_section_heading(body_text, "metadata"):
        metadata_text = "\n".join(["Metadata", *(f"* {item}" for item in metadata[:7])])
        text = f"{body_text}\n\n{metadata_text}" if body_text else metadata_text
    else:
        text = body_text
    text = _repair_ambiguous_apa_terms(text)
    return text


def low_metadata_requested(text: str) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    return bool(
        re.search(
            r"\b(?:low[- ]metadata|minimal\s+metadata|no\s+metadata|metadata[- ]light)\b",
            normalized,
        )
        or re.search(
            r"\b(?:concise|compact|short)\s+internal\s+handoff\b",
            normalized,
        )
    )


def _source_evidence_lines(
    items: list[str],
    *,
    sources: Any | None,
    rendered_text: str,
) -> list[str]:
    if _has_section_heading(rendered_text, "source evidence"):
        return []
    cleaned = _clean_section_items(items, rendered_text=rendered_text)
    if cleaned:
        return cleaned[:6]
    if not isinstance(sources, list):
        return []
    lines: list[str] = []
    seen_urls: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        title = str(source.get("title") or "").strip() or "Source"
        note = _source_evidence_note(source)
        line = f"{title}: {url}"
        if note:
            line = f"{line} - {note}"
        lines.append(line)
        if len(lines) >= 6:
            break
    return lines


def _fallback_synthesis_from_sources(sources: Any | None) -> str:
    if not isinstance(sources, list):
        return ""
    source_details: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        note = _source_evidence_note(source)
        if not note:
            continue
        seen_urls.add(url)
        title = str(source.get("title") or "").strip() or "Source"
        status = str(source.get("extraction_status") or "").strip().lower()
        source_details.append((title, note, status))
        if len(source_details) >= 4:
            break
    if not source_details:
        return ""
    summary = _source_set_summary_paragraph(source_details)
    bullets = [f"- {title}: {note}" for title, note, _status in source_details]
    limitation = ""
    if any(
        status and not _source_status_is_extracted(status)
        for _title, _note, status in source_details
    ):
        limitation = (
            "\n\nExtraction note: some selected links still have snippet-only source "
            "context, so page-level claims should stay limited until those URLs are "
            "read/extracted."
        )
    return (
        "\n".join(
            [
                summary,
                "",
                "Key source details:",
                *bullets,
            ]
        )
        + limitation
    )


def _source_backed_synthesis_lacks_summary(text: str, sources: Any | None) -> bool:
    if not isinstance(sources, list) or not sources:
        return False
    if not _fallback_answer_from_sources(sources):
        return False
    stripped = str(text or "").strip()
    if not stripped:
        return False
    first_block = stripped.split("\n\n", 1)[0].strip()
    if not first_block:
        return True
    if first_block.lower().startswith(("summary:", "the selected", "across the selected")):
        return False
    if first_block.startswith(("-", "*")):
        return True
    first_sentence = _first_sentence(first_block, max_chars=500)
    return len(first_sentence.split()) < 18


def _source_summary_from_sources(sources: Any | None) -> str:
    if not isinstance(sources, list):
        return ""
    source_details: list[tuple[str, str, str]] = []
    seen_urls: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        url = str(source.get("url") or "").strip()
        if not url or url in seen_urls:
            continue
        note = _source_evidence_note(source)
        if not note:
            continue
        seen_urls.add(url)
        title = str(source.get("title") or "").strip() or "Source"
        status = str(source.get("extraction_status") or "").strip().lower()
        source_details.append((title, note, status))
        if len(source_details) >= 4:
            break
    if not source_details:
        return ""
    return _source_set_summary_paragraph(source_details)


def _source_status_is_extracted(status: str) -> bool:
    return status.strip().lower() in {
        "success",
        "extracted",
        "read",
        "extracted/read",
        "article_read",
        "page_read",
    }


def _source_set_summary_paragraph(source_details: list[tuple[str, str, str]]) -> str:
    notes = [note for _title, note, _status in source_details if note]
    if not notes:
        return "The retrieved source set supports a source-backed answer, but details are limited."
    combined = " ".join(notes).lower()
    themes: list[str] = []
    theme_checks = (
        ("clinical or provider evaluation", ("study", "evaluation", "trial", "clinical")),
        ("behavioral-health or psychiatry relevance", ("behavioral", "mental health", "psychiatr")),
        ("workflow and documentation burden", ("documentation", "scribe", "note", "workflow")),
        (
            "implementation or adoption signal",
            ("clinic", "outpatient", "provider", "adoption", "market"),
        ),
        (
            "vendor or product-positioning signal",
            ("vendor", "ehr", "platform", "market positioning"),
        ),
    )
    for label, markers in theme_checks:
        if any(marker in combined for marker in markers):
            themes.append(label)
    if not themes:
        themes = ["source-backed evidence", "follow-up verification needs"]
    source_count = len(source_details)
    title_examples = ", ".join(title for title, _note, _status in source_details[:2])
    if source_count == 1:
        return (
            f"{title_examples} supports the core answer around {themes[0]}. "
            "Use its concrete claims as the factual basis and keep any broader interpretation "
            "limited to what the source actually says."
        )
    detail_sentences = [
        _first_sentence(note, max_chars=220).rstrip(".")
        for note in notes[:2]
        if _first_sentence(note, max_chars=220).strip()
    ]
    detail_text = ". ".join(dict.fromkeys(detail_sentences))
    if detail_text:
        detail_text = f" {detail_text}."
    evidence_weight = (
        " Official, extracted, or otherwise primary pages should carry the main factual "
        "weight; secondary summaries and vendor positioning are useful context only when "
        "they name concrete mechanisms rather than broad safety claims."
    )
    return (
        f"Across the retrieved sources, the strongest evidence concerns "
        f"{', '.join(themes[:3])}.{detail_text}{evidence_weight}"
    )


def _fallback_answer_from_sources(sources: Any | None) -> str:
    if not isinstance(sources, list):
        return ""
    for source in sources:
        if not isinstance(source, dict):
            continue
        note = _source_evidence_note(source)
        if not note:
            continue
        return f"Selected source context supports this point: {note}"
    return ""


def _metadata_lines_indicate_source_focus_mismatch(lines: list[str] | None) -> bool:
    text = " ".join(str(line or "").lower() for line in lines or [])
    return "no_sample_source_matches_focus" in text or "no_selected_source_matches_focus" in text


def _source_evidence_note(source: dict[str, Any]) -> str:
    excerpt = str(source.get("evidence_excerpt") or "").strip()
    if excerpt and _source_is_extracted(source):
        return _first_sentence(excerpt)
    for key in ("supported_claim",):
        value = str(source.get(key) or "").strip()
        if value:
            return _first_sentence(value)
    for item in _source_key_facts(source):
        value = str(item or "").strip()
        if value:
            return _first_sentence(value)
    if excerpt:
        return _first_sentence(excerpt)
    return ""


def _clean_section_items(items: list[str], *, rendered_text: str) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = str(item or "").strip().lstrip("-* ").strip()
        value = _strip_trailing_followup_offer(value)
        if not value or _looks_like_followup_offer(value):
            continue
        normalized = " ".join(value.lower().split())
        if not normalized or normalized in seen:
            continue
        if normalized in " ".join(rendered_text.lower().split()):
            continue
        seen.add(normalized)
        cleaned.append(value)
    return cleaned


def _has_section_heading(text: str, heading: str) -> bool:
    expected = heading.strip().lower()
    for line in str(text or "").splitlines():
        if _normalized_heading(line) == expected:
            return True
    return False


def _first_sentence(text: str, *, max_chars: int = 180) -> str:
    sentence, _remainder = _split_first_sentence(str(text or "").strip())
    sentence = sentence or str(text or "").strip()
    if len(sentence) <= max_chars:
        return sentence
    return sentence[: max_chars - 1].rstrip() + "..."


def _repair_ambiguous_apa_terms(text: str) -> str:
    """Avoid collapsing psychology and psychiatry orgs into one APA expansion."""

    if "APA: American Psychiatric Association" not in text:
        return text
    has_psychological = "www.apa.org" in text or "https://apa.org" in text
    has_psychiatric = "www.psychiatry.org" in text or "https://psychiatry.org" in text
    if has_psychological and has_psychiatric:
        return text.replace(
            "APA: American Psychiatric Association.",
            (
                "APA: American Psychological Association for `apa.org` sources; "
                "American Psychiatric Association for `psychiatry.org` sources."
            ),
        )
    if has_psychological:
        return text.replace(
            "APA: American Psychiatric Association.",
            "APA: American Psychological Association.",
        )
    return text


_REDUNDANT_ANSWER_SECTION_HEADINGS = {
    "key points",
    "next step",
    "next steps",
    "what should happen next",
}
_ANSWER_SECTION_BOUNDARY_HEADINGS = {
    "caveats",
    "limitations",
    "metadata",
    "run metadata",
    "run notes",
    "sources",
    "source links",
    "what remains unknown",
    "what the sources support",
}
_LEADING_STATUS_MARKERS = (
    "partial",
    "not decision-ready",
    "not decision ready",
    "blocked",
    "run is not",
    "run was not",
    "not complete",
)


def _strip_redundant_answer_sections(text: str) -> str:
    """Remove sections duplicated by WorkItem metadata and footer notes."""

    if not text.strip():
        return ""
    kept: list[str] = []
    skipping = False
    for line in text.splitlines():
        heading = _normalized_heading(line)
        if heading in _REDUNDANT_ANSWER_SECTION_HEADINGS:
            skipping = True
            continue
        if skipping and heading in _ANSWER_SECTION_BOUNDARY_HEADINGS:
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


def _demote_leading_status_sentence(text: str) -> tuple[str, list[str]]:
    """Move an opening run-status sentence into footer notes when possible."""

    stripped = text.strip()
    if not stripped:
        return "", []
    first_paragraph, separator, rest = stripped.partition("\n\n")
    sentence, remainder = _split_first_sentence(first_paragraph)
    if not sentence or not _looks_like_run_status_sentence(sentence):
        return stripped, []

    remaining_paragraph = remainder.strip()
    remaining_parts = [part for part in (remaining_paragraph, rest.strip()) if part]
    if not remaining_parts:
        return stripped, []
    return "\n\n".join(remaining_parts).strip(), [sentence.strip()]


def _strip_trailing_followup_offer(text: str) -> str:
    """Remove generic trailing offers that distract from the completed answer."""

    stripped = text.strip()
    if not stripped:
        return ""
    paragraphs = stripped.split("\n\n")
    while paragraphs and _looks_like_followup_offer(paragraphs[-1]):
        paragraphs.pop()
    return "\n\n".join(paragraphs).strip()


def _looks_like_followup_offer(paragraph: str) -> bool:
    lower = " ".join(str(paragraph or "").lower().split())
    return lower.startswith(
        (
            "if you want,",
            "if useful,",
            "i can next ",
            "next, i can ",
        )
    )


def _looks_like_workflow_only_answer(text: str) -> bool:
    lower = " ".join(str(text or "").lower().split()).strip(" .")
    if not lower:
        return False
    return lower in {
        "search completed",
        "search run completed",
        "read-only search completed",
        "read only search completed",
        "search run completed in read-only mode",
        "search run completed in read only mode",
        "run completed",
        "business agents run completed",
        "business agents workitem advanced",
        "workitem advanced",
    } or bool(
        re.fullmatch(
            r"(?:read[- ]only\s+)?(?:web\s+)?search(?:\s+run)?\s+completed(?:\s+in\s+read[- ]only\s+mode)?",
            lower,
        )
    )


def _split_first_sentence(text: str) -> tuple[str, str]:
    for index, character in enumerate(text):
        if character in ".!?":
            return text[: index + 1].strip(), text[index + 1 :].strip()
    return text.strip(), ""


def _looks_like_run_status_sentence(sentence: str) -> bool:
    lower = sentence.lower()
    if not any(marker in lower for marker in _LEADING_STATUS_MARKERS):
        return False
    return any(
        marker in lower
        for marker in (
            "run",
            "decision-ready",
            "decision ready",
            "source-backed read",
            "source backed read",
            "blocked",
        )
    )


def _normalized_heading(line: str) -> str:
    stripped = line.strip().strip("*#").strip().rstrip(":").strip()
    if not stripped or stripped.startswith(("-", "*")):
        return ""
    if len(stripped) > 80:
        return ""
    return stripped.lower()


def _compact_mapping(value: dict[str, Any], *, max_items: int = 8) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, item in value.items():
        if len(compact) >= max_items:
            break
        if key in {"retrieval_diagnostics", "source_refs", "sources"}:
            compact[key] = item
            continue
        if isinstance(item, str | int | float | bool) or item is None:
            compact[key] = item
    return compact


def _compact_orchestrator_reviews(result: WorkflowRunResult) -> list[dict[str, Any]]:
    metadata = result.work_item.target.metadata
    raw_reviews = metadata.get("orchestrator_reviews") if isinstance(metadata, dict) else None
    if not isinstance(raw_reviews, list):
        return []
    reviews: list[dict[str, Any]] = []
    for item in raw_reviews[-5:]:
        if not isinstance(item, dict):
            continue
        reviews.append(
            {
                key: item[key]
                for key in (
                    "step",
                    "route",
                    "status",
                    "review_status",
                    "overall_score",
                    "approval_boundary_ok",
                )
                if key in item and item[key] not in (None, "", [])
            }
        )
    return reviews
