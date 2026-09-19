"""Route-specific evidence binding for the shared agent decision validator."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

from keystone_agents.runtime.decision_validation import (
    AgentDecisionContract,
    SpecialistDecisionEvidence,
    model_tool_output_payloads,
)
from keystone_agents.tools.search_provider import search_result_candidate_id

_ORCHESTRATOR_ROUTE_CANDIDATES = (
    "gmail_triage",
    "business_research_analyst",
    "opportunity_scout",
    "outreach_composer",
    "chief_of_staff",
    "airtable_context_agent",
    "google_workspace_context_agent",
    "zotero_context_agent",
    "rss_context_agent",
    "preprints_context_agent",
    "clarification",
)
_CHIEF_WORKFLOW_CANDIDATES = (
    "calendar-read",
    "gmail-summary",
    "gmail-triage",
    "business-agents-route",
    "slack-runtime-review",
    "slack-cross-channel-review",
    "slack-article-review",
    "slack-follow-up-review",
    "slack-docs-review",
    "slack-command",
    "project-context-review",
    "research-direction-review",
    "budget-resource-review",
    "meeting-prep",
    "portfolio-review",
    "artifact-write-plan",
    "google-drive-management",
    "reference-capture",
    "memory-review",
    "clarification",
)


def business_research_decision_contract() -> AgentDecisionContract:
    return AgentDecisionContract(
        route="business_research_analyst",
        decision_stage="research_source_selection",
        evidence_resolver=_research_evidence,
        tool_evidence_resolver=_web_search_tool_evidence,
    )


def business_research_context_decision_contract(retrieved: Any) -> AgentDecisionContract:
    """Bind research selection to source identities supplied before synthesis."""

    sources = list(getattr(retrieved, "sources", []) or [])
    candidates = tuple(
        dict.fromkeys(
            _research_source_candidate_id(source)
            for source in sources
            if _research_source_candidate_id(source)
        )
    )
    provider_identities = {
        _research_source_candidate_id(source): (
            str(getattr(source, "url", "") or "").strip(),
        )
        for source in sources
        if _research_source_candidate_id(source)
        and str(getattr(source, "url", "") or "").strip()
    }

    def evidence(output: Any) -> SpecialistDecisionEvidence:
        required = _research_required_candidate_ids(output)
        return SpecialistDecisionEvidence.build(
            candidates,
            required_selected_ids=required,
            selection_required=bool(candidates),
            provider_identities_by_candidate=provider_identities,
        )

    return AgentDecisionContract(
        route="business_research_analyst",
        decision_stage="research_source_selection",
        evidence_resolver=evidence,
        pre_model_candidate_ids=candidates,
        pre_model_context_source="retrieved_source_bundle",
    )


def supplied_research_brief_decision_contract(sources: Iterable[Any]) -> AgentDecisionContract:
    """Bind a tool-free brief to the exact source catalog visible before its choice."""
    sources = tuple(sources)
    expected = {str(source.source_id): str(source.url) for source in sources}
    if len(expected) != len(sources) or any(not key or not url for key, url in expected.items()):
        raise ValueError("Supplied research source identities must be unique and complete.")
    contract = business_research_context_decision_contract(SimpleNamespace(sources=sources))

    def consistency(output: Any) -> tuple[str, str] | None:
        returned = list(getattr(output, "sources", []) or [])
        returned_ids = [str(source.source_id) for source in returned]
        cited = _research_required_source_ids(output)
        if (
            len(returned_ids) != len(set(returned_ids))
            or any(expected.get(source.source_id) != source.url for source in returned)
            or any(identity not in expected or identity not in returned_ids for identity in cited)
            or any(str(note).startswith("Some model-cited source ids were not returned")
                   for note in getattr(output, "unknowns", []) or [])
        ):
            return (
                "supplied_research_citation_mismatch",
                "Use only supplied source IDs and copy their exact URLs. Every fact/article "
                "citation must identify a returned source from that catalog; do not invent "
                "or silently discard unresolved citations.",
            )
        if not str(getattr(output, "summary", "") or "").strip():
            return "supplied_research_summary_missing", "Return your source-grounded summary."
        if not returned and not getattr(
            getattr(output, "decision", None), "needs_more_context", False
        ):
            return (
                "supplied_research_sources_missing",
                "Cite the supplied evidence or request more context.",
            )
        return None

    return replace(contract, output_consistency_validator=consistency)


def business_research_comparison_decision_contract() -> AgentDecisionContract:
    return AgentDecisionContract(
        route="business_research_analyst",
        decision_stage="research_comparison_selection",
        evidence_resolver=_research_comparison_evidence,
        max_selected=2,
    )


def opportunity_scout_decision_contract() -> AgentDecisionContract:
    return AgentDecisionContract(
        route="opportunity_scout",
        decision_stage="opportunity_candidate_selection",
        evidence_resolver=_opportunity_evidence,
        tool_evidence_resolver=_web_search_tool_evidence,
        max_selected=5,
        output_normalizer=_normalize_opportunity_deterministic_scores,
        output_consistency_validator=_opportunity_handoff_consistency_issue,
    )


def opportunity_scout_synthesis_decision_contract(
    retrieved: Any,
) -> AgentDecisionContract:
    """Bind compact Scout judgments to the exact retrieved candidate packet."""

    records = list(getattr(retrieved, "records", []) or [])
    candidates = [_opportunity_record_id(record) for record in records]
    provider_identities: dict[str, tuple[str, ...]] = {}
    for record, record_id in zip(records, candidates, strict=False):
        source_urls = tuple(
            str(getattr(source, "url", "") or "").strip()
            for source in list(getattr(record, "sources", []) or [])
            if str(getattr(source, "url", "") or "").strip()
        )
        if record_id and source_urls:
            provider_identities[record_id] = source_urls
    for candidate in [
        *list(getattr(retrieved, "review_candidates", []) or []),
        *list(getattr(retrieved, "filtered_candidates", []) or []),
    ]:
        source_url = str(getattr(candidate, "source_url", "") or "").strip()
        candidate_id = source_url or str(
            getattr(candidate, "company_name", "") or ""
        ).strip()
        if candidate_id:
            candidates.append(candidate_id)
        if candidate_id and source_url:
            provider_identities[candidate_id] = (source_url,)

    def evidence(output: Any) -> SpecialistDecisionEvidence:
        selected = [
            str(getattr(item, "record_key", "") or "").strip()
            for item in list(getattr(output, "decisions", []) or [])
            if bool(getattr(item, "include", False))
        ]
        return SpecialistDecisionEvidence.build(
            candidates,
            required_selected_ids=selected,
            selection_required=bool(selected),
            exact_required_selection=True,
            provider_identities_by_candidate=provider_identities,
        )

    return AgentDecisionContract(
        route="opportunity_scout",
        decision_stage="opportunity_candidate_selection",
        evidence_resolver=evidence,
        max_selected=5,
        pre_model_candidate_ids=tuple(dict.fromkeys(candidates)),
        pre_model_context_source="retrieved_opportunity_packet",
    )


def outreach_composer_decision_contract(
    allowed_candidate_ids: Any = (),
    *,
    required_context_ids: Any | None = None,
) -> AgentDecisionContract:
    raw_allowed = (
        (allowed_candidate_ids,)
        if isinstance(allowed_candidate_ids, str)
        else (allowed_candidate_ids or ())
    )
    allowed = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in raw_allowed
            if str(value or "").strip()
        )
    )
    raw_required = (
        (("keystone_profile",) if "keystone_profile" in allowed else ())
        if required_context_ids is None
        else (
            (required_context_ids,)
            if isinstance(required_context_ids, str)
            else (required_context_ids or ())
        )
    )
    required = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in raw_required
            if str(value or "").strip()
        )
    )
    return AgentDecisionContract(
        route="outreach_composer",
        decision_stage="outreach_evidence_selection",
        evidence_resolver=(
            (
                lambda output: _outreach_evidence(
                    output,
                    allowed_candidate_ids=allowed,
                    required_context_ids=required,
                )
            )
            if allowed
            else _outreach_evidence
        ),
        pre_model_candidate_ids=allowed,
        mandatory_pre_model_context_ids=required,
        pre_model_context_source="approved_outreach_context",
    )


def outreach_variant_set_decision_contract(
    allowed_candidate_ids: Any,
) -> AgentDecisionContract:
    """Bind a multi-variant draft set to one shared approved evidence choice."""

    raw_allowed = (
        (allowed_candidate_ids,)
        if isinstance(allowed_candidate_ids, str)
        else (allowed_candidate_ids or ())
    )
    allowed = tuple(
        dict.fromkeys(
            str(value or "").strip()
            for value in raw_allowed
            if str(value or "").strip()
        )
    )
    def evidence(output: Any) -> SpecialistDecisionEvidence:
        selected: list[str] = []
        for variant in list(getattr(output, "variants", []) or []):
            draft = getattr(variant, "draft", None)
            selected.extend(list(getattr(draft, "source_ids_used", []) or []))
            selected.extend(list(getattr(draft, "example_ids_used", []) or []))
        selected = list(dict.fromkeys(value for value in selected if value))
        return SpecialistDecisionEvidence.build(
            allowed or selected,
            required_selected_ids=selected,
            mandatory_selected_ids=(
                ("keystone_profile",) if "keystone_profile" in allowed else ()
            ),
            selection_required=bool(selected),
            exact_required_selection=True,
        )

    return AgentDecisionContract(
        route="outreach_composer",
        decision_stage="outreach_evidence_selection",
        evidence_resolver=evidence,
        pre_model_candidate_ids=allowed,
        mandatory_pre_model_context_ids=(
            ("keystone_profile",) if "keystone_profile" in allowed else ()
        ),
        pre_model_context_source="approved_outreach_variant_context",
    )


def orchestrator_decision_contract(
    allowed_route_candidates: Iterable[object] | None = None,
) -> AgentDecisionContract:
    bounded_candidates_supplied = allowed_route_candidates is not None
    candidates = tuple(
        dict.fromkeys(
            _route_token(value)
            for value in (allowed_route_candidates or _ORCHESTRATOR_ROUTE_CANDIDATES)
            if _route_token(value) in _ORCHESTRATOR_ROUTE_CANDIDATES
        )
    ) or _ORCHESTRATOR_ROUTE_CANDIDATES

    def evidence(output: Any) -> SpecialistDecisionEvidence:
        return _orchestrator_evidence(output, candidate_ids=candidates)

    return AgentDecisionContract(
        route="orchestrator",
        decision_stage="orchestrator_route_selection",
        decision_owner="orchestrator",
        evidence_resolver=evidence,
        max_selected=len(candidates),
        pre_model_candidate_ids=candidates if bounded_candidates_supplied else (),
        pre_model_context_source=(
            "orchestrator_route_candidate_universe"
            if bounded_candidates_supplied
            else ""
        ),
        pre_model_source_visibility_required=False,
        output_consistency_validator=_orchestrator_output_consistency_issue,
        allow_plausible_alternatives_after_selection=True,
        max_decision_repairs=1,
    )


def chief_of_staff_decision_contract() -> AgentDecisionContract:
    return AgentDecisionContract(
        route="chief_of_staff",
        decision_stage="chief_delegation_selection",
        decision_owner="chief_of_staff",
        evidence_resolver=_chief_evidence,
        max_selected=8,
        output_consistency_validator=_chief_output_consistency_issue,
    )


def context_agent_decision_contract(
    route: str,
    *,
    require_substantive_summary: bool = False,
    provider_candidate_ids: Iterable[object] | None = None,
) -> AgentDecisionContract:
    provider_candidates = (
        tuple(
            dict.fromkeys(
                " ".join(str(value or "").split())
                for value in provider_candidate_ids
                if str(value or "").strip()
            )
        )
        if provider_candidate_ids is not None
        else None
    )
    contracts = {
        "airtable_context_agent": AgentDecisionContract(
            route="airtable_context_agent",
            decision_stage="airtable_record_selection",
            evidence_resolver=lambda output: _airtable_evidence(
                output,
                provider_candidate_ids=provider_candidates,
            ),
            max_selected=10,
        ),
        "google_workspace_context_agent": AgentDecisionContract(
            route="google_workspace_context_agent",
            decision_stage="workspace_artifact_selection",
            evidence_resolver=lambda output: _workspace_evidence(
                output,
                provider_candidate_ids=provider_candidates,
            ),
            max_selected=10,
        ),
        "zotero_context_agent": AgentDecisionContract(
            route="zotero_context_agent",
            decision_stage="zotero_item_selection",
            evidence_resolver=lambda output: _zotero_evidence(
                output,
                provider_candidate_ids=provider_candidates,
            ),
            max_selected=10,
            output_consistency_validator=(
                _zotero_substantive_summary_issue
                if require_substantive_summary
                else None
            ),
        ),
    }
    try:
        return contracts[str(route).strip()]
    except KeyError as exc:
        raise KeyError(f"No shared decision contract for context route: {route}") from exc


def calendar_action_interpreter_decision_contract(
    allowed_operations: Any = (),
) -> AgentDecisionContract:
    """Require the Calendar interpreter to own its operation choice."""

    raw_allowed = (
        (allowed_operations,)
        if isinstance(allowed_operations, str)
        else (allowed_operations or ("read", "create", "update", "delete"))
    )
    candidates = tuple(
        dict.fromkeys(
            [
                *(
                    str(value or "").strip()
                    for value in raw_allowed
                    if str(value or "").strip()
                    in {"read", "create", "update", "delete"}
                ),
                "none",
            ]
        )
    )

    def evidence(output: Any) -> SpecialistDecisionEvidence:
        operation = str(getattr(output, "operation", "") or "").strip()
        return SpecialistDecisionEvidence.build(
            candidates,
            required_selected_ids=(operation,) if operation in candidates else (),
            selection_required=True,
            require_complete_assessments=False,
            exact_required_selection=True,
        )

    return AgentDecisionContract(
        route="calendar_action_interpreter",
        decision_stage="calendar_action_interpretation",
        evidence_resolver=evidence,
        pre_model_candidate_ids=candidates,
        pre_model_context_source="operator_request_and_authorized_operations",
        pre_model_source_visibility_required=False,
    )


def _research_evidence(output: Any) -> SpecialistDecisionEvidence:
    sources = list(getattr(output, "sources", []) or [])
    candidates = [_direct_web_source_candidate_id(source) for source in sources]
    provider_identities = {
        _direct_web_source_candidate_id(source): (
            str(getattr(source, "url", "") or "").strip(),
        )
        for source in sources
        if _direct_web_source_candidate_id(source)
        and str(getattr(source, "url", "") or "").strip()
    }
    required = _research_required_candidate_ids(output, canonicalize_web=True)
    return SpecialistDecisionEvidence.build(
        candidates,
        required_selected_ids=required,
        selection_required=bool(candidates),
        provider_identities_by_candidate=provider_identities,
    )


def _research_required_source_ids(output: Any) -> list[str]:
    required = list(getattr(output, "source_ids_used", []) or [])
    required.extend(
        getattr(claim, "source_id", "")
        for claim in list(getattr(output, "claims", []) or [])
    )
    required.extend(
        source_id
        for fact in list(getattr(output, "facts", []) or [])
        for source_id in list(getattr(fact, "source_ids", []) or [])
    )
    required.extend(
        source_id
        for article in list(getattr(output, "article_summaries", []) or [])
        for source_id in list(getattr(article, "source_ids", []) or [])
    )
    return list(
        dict.fromkeys(
            str(source_id or "").strip()
            for source_id in required
            if str(source_id or "").strip()
        )
    )


def _research_source_candidate_id(source: Any) -> str:
    """Keep provider evidence identity separate from citation identity."""

    return str(
        getattr(source, "provider_candidate_id", "")
        or getattr(source, "source_id", "")
        or ""
    ).strip()


def _direct_web_source_candidate_id(source: Any) -> str:
    provider_candidate_id = str(
        getattr(source, "provider_candidate_id", "") or ""
    ).strip()
    if provider_candidate_id:
        return provider_candidate_id
    source_url = str(getattr(source, "url", "") or "").strip()
    return _canonical_web_candidate_id(source_url) or _research_source_candidate_id(source)


def _canonical_web_candidate_id(source_url: object) -> str:
    url = str(source_url or "").strip()
    if not re.match(r"^https?://", url, flags=re.IGNORECASE):
        return ""
    return search_result_candidate_id(url)


def _web_search_tool_evidence(
    raw_results: Iterable[Any],
) -> SpecialistDecisionEvidence:
    """Bind web-selection agents to exact candidates returned in their tool loop."""

    candidate_ids: list[str] = []
    provider_identities: dict[str, tuple[str, ...]] = {}
    for payload in model_tool_output_payloads(
        raw_results,
        tool_names=("search_web",),
    ):
        if isinstance(payload, list | tuple):
            items = list(payload)
        elif isinstance(payload, dict):
            raw_items = payload.get("results") or payload.get("candidates") or []
            items = list(raw_items) if isinstance(raw_items, list | tuple) else []
        else:
            items = []
        for item in items:
            if hasattr(item, "model_dump"):
                item = item.model_dump(mode="json")
            if not isinstance(item, dict):
                continue
            source_url = str(item.get("link") or item.get("url") or "").strip()
            candidate_id = str(item.get("candidate_id") or "").strip()
            candidate_id = candidate_id or _canonical_web_candidate_id(source_url)
            if not candidate_id:
                continue
            candidate_ids.append(candidate_id)
            if source_url:
                provider_identities[candidate_id] = tuple(
                    dict.fromkeys(
                        (*provider_identities.get(candidate_id, ()), source_url)
                    )
                )
    return SpecialistDecisionEvidence.build(
        candidate_ids,
        provider_identities_by_candidate=provider_identities,
    )


def _research_required_candidate_ids(
    output: Any,
    *,
    canonicalize_web: bool = False,
) -> list[str]:
    """Map cited source IDs to the raw evidence IDs selected by the agent."""

    sources = list(getattr(output, "sources", []) or [])
    source_by_id = {
        str(getattr(source, "source_id", "") or "").strip(): source
        for source in sources
        if str(getattr(source, "source_id", "") or "").strip()
    }
    return list(
        dict.fromkeys(
            (
                _direct_web_source_candidate_id(source_by_id.get(source_id))
                if canonicalize_web
                else _research_source_candidate_id(source_by_id.get(source_id))
            )
            or source_id
            for source_id in _research_required_source_ids(output)
            if source_id
        )
    )


def _research_comparison_evidence(output: Any) -> SpecialistDecisionEvidence:
    recommendation = str(getattr(output, "recommended_company", "") or "")
    required = [recommendation] if recommendation in {"company_a", "company_b"} else []
    if recommendation == "tie":
        required = ["company_a", "company_b"]
    return SpecialistDecisionEvidence.build(
        ("company_a", "company_b"),
        required_selected_ids=required,
        selection_required=recommendation != "unclear",
        exact_required_selection=bool(required),
    )


def _opportunity_record_id(record: Any) -> str:
    canonical = str(getattr(record, "canonical_entity_key", "") or "").strip()
    if canonical:
        return canonical
    sources = list(getattr(record, "sources", []) or [])
    source_id = str(getattr(sources[0], "source_id", "") or "").strip() if sources else ""
    if source_id:
        return source_id
    return str(getattr(record, "entity_name", "") or getattr(record, "company_name", "") or "")


def _opportunity_evidence(output: Any) -> SpecialistDecisionEvidence:
    records = list(getattr(output, "records", []) or [])
    required_by_record = [
        _opportunity_record_candidate_ids(record) for record in records
    ]
    required = [
        candidate_id
        for record_candidate_ids in required_by_record
        for candidate_id in record_candidate_ids
    ]
    candidates = [*required]
    provider_identities: dict[str, tuple[str, ...]] = {}
    for record, record_candidate_ids in zip(records, required_by_record, strict=False):
        for source, candidate_id in zip(
            list(getattr(record, "sources", []) or []),
            record_candidate_ids,
            strict=False,
        ):
            source_url = str(getattr(source, "url", "") or "").strip()
            if candidate_id and source_url:
                provider_identities[candidate_id] = (source_url,)
    for candidate in [
        *list(getattr(output, "review_candidates", []) or []),
        *list(getattr(output, "filtered_candidates", []) or []),
    ]:
        source_url = str(getattr(candidate, "source_url", "") or "").strip()
        candidate_id = _canonical_web_candidate_id(source_url) or str(
            getattr(candidate, "company_name", "") or ""
        ).strip()
        candidates.append(candidate_id)
        if candidate_id and source_url:
            provider_identities[candidate_id] = (source_url,)
    return SpecialistDecisionEvidence.build(
        candidates,
        required_selected_ids=required,
        selection_required=bool(records),
        exact_required_selection=True,
        provider_identities_by_candidate=provider_identities,
    )


def _opportunity_record_candidate_ids(record: Any) -> list[str]:
    """Return every raw evidence identity supporting one selected record."""

    sources = list(getattr(record, "sources", []) or [])
    provider_candidate_ids = list(
        dict.fromkeys(
            str(getattr(source, "provider_candidate_id", "") or "").strip()
            for source in sources
            if str(getattr(source, "provider_candidate_id", "") or "").strip()
        )
    )
    if provider_candidate_ids:
        return provider_candidate_ids
    source_urls = list(
        dict.fromkeys(
            str(getattr(source, "url", "") or "").strip()
            for source in sources
            if _canonical_web_candidate_id(
                str(getattr(source, "url", "") or "").strip()
            )
        )
    )
    if source_urls:
        return [_canonical_web_candidate_id(source_url) for source_url in source_urls]
    fallback = _opportunity_record_id(record)
    return [fallback] if fallback else []


def _opportunity_handoff_consistency_issue(output: Any) -> tuple[str, str] | None:
    """Bind exact scoring to the helper while keeping semantic handoff model-owned."""

    required_fields = {
        "handoff_reason",
        "business_research_analyst_handoff_recommendation",
        "research_needed",
    }
    for record in list(getattr(output, "records", []) or []):
        if not all(
            hasattr(record, field_name)
            for field_name in (
                "company_name",
                "opportunity_type",
                "source_signals",
                "priority_score",
                "outside_consulting_likelihood",
            )
        ):
            continue
        from keystone_agents.agents.opportunity_scout import score_opportunity_impl

        score_packet = score_opportunity_impl(
            company_name=str(getattr(record, "company_name", "") or ""),
            opportunity_type=str(getattr(record, "opportunity_type", "") or ""),
            signals=list(getattr(record, "source_signals", []) or []),
        )
        parsed_score = json.loads(score_packet)
        if int(getattr(record, "priority_score", -1)) != int(
            parsed_score.get("priority_score") or 0
        ) or int(getattr(record, "outside_consulting_likelihood", -1)) != int(
            parsed_score.get("outside_consulting_likelihood") or 0
        ):
            return (
                "opportunity_deterministic_score_mismatch",
                "priority_score and outside_consulting_likelihood must match the "
                "deterministic score_opportunity result for the returned entity, "
                "opportunity type, and source signals.",
            )
        if not bool(getattr(record, "handoff_to_business_research_analyst", False)):
            continue
        model_fields_set = set(getattr(record, "model_fields_set", set()) or set())
        missing = sorted(required_fields - model_fields_set)
        if missing:
            return (
                "opportunity_handoff_reasoning_missing",
                "When Business Research follow-up is selected, Opportunity Scout must "
                "explicitly provide handoff_reason, the bounded Analyst recommendation, "
                "and research_needed; schema defaults cannot make that decision.",
            )
    return None


def _normalize_opportunity_deterministic_scores(output: Any) -> dict[str, Any]:
    """Apply exact helper arithmetic without changing the agent's semantic choice."""

    from keystone_agents.agents.opportunity_scout import score_opportunity_impl
    from keystone_agents.schemas.opportunity import OpportunityScoreBreakdown

    changes: list[dict[str, Any]] = []
    records = list(getattr(output, "records", []) or [])
    for record_index, record in enumerate(records):
        if not all(
            hasattr(record, field_name)
            for field_name in (
                "company_name",
                "opportunity_type",
                "source_signals",
                "priority_score",
                "outside_consulting_likelihood",
            )
        ):
            continue
        packet = json.loads(
            score_opportunity_impl(
                company_name=str(getattr(record, "company_name", "") or ""),
                opportunity_type=str(getattr(record, "opportunity_type", "") or ""),
                signals=list(getattr(record, "source_signals", []) or []),
            )
        )
        normalized = {
            "priority_score": int(packet.get("priority_score") or 0),
            "outside_consulting_likelihood": int(
                packet.get("outside_consulting_likelihood") or 0
            ),
            "score_breakdown": OpportunityScoreBreakdown.model_validate(
                packet.get("score_breakdown") or {}
            ),
            "score_rationale": str(packet.get("score_rationale") or ""),
        }
        changed_fields = [
            field_name
            for field_name, value in normalized.items()
            if getattr(record, field_name, None) != value
        ]
        for field_name, value in normalized.items():
            setattr(record, field_name, value)
        if changed_fields:
            changes.append(
                {
                    "record_index": record_index,
                    "changed_fields": changed_fields,
                }
            )
    return {
        "schema": "keystone.deterministic_output_normalization.v1",
        "route": "opportunity_scout",
        "normalizer": "score_opportunity",
        "record_count": len(records),
        "adjusted_record_count": len(changes),
        "changes": changes,
        "semantic_selection_changed": False,
    }


def _outreach_evidence(
    output: Any,
    *,
    allowed_candidate_ids: tuple[str, ...] = (),
    required_context_ids: tuple[str, ...] = (),
) -> SpecialistDecisionEvidence:
    selected = [
        getattr(fact, "source_id", "")
        for fact in list(getattr(output, "facts_used", []) or [])
    ]
    if not selected:
        selected = list(getattr(output, "source_ids_used", []) or [])
    selected = [
        *selected,
        *list(getattr(output, "example_ids_used", []) or []),
    ]
    candidates = list(allowed_candidate_ids or tuple(selected))
    return SpecialistDecisionEvidence.build(
        candidates,
        required_selected_ids=selected,
        mandatory_selected_ids=required_context_ids,
        selection_required=bool(selected),
        exact_required_selection=True,
    )


def _route_token(value: object) -> str:
    token = "_".join(str(value or "").strip().lower().replace("-", " ").split())
    aliases = {
        "business_research_analyst_agent": "business_research_analyst",
        "business_research": "business_research_analyst",
        "gmail": "gmail_triage",
        "chief": "chief_of_staff",
        "cos": "chief_of_staff",
    }
    return aliases.get(token, token)


def _orchestrator_evidence(
    output: Any,
    *,
    candidate_ids: Iterable[object] = _ORCHESTRATOR_ROUTE_CANDIDATES,
) -> SpecialistDecisionEvidence:
    allowed_candidates = tuple(
        dict.fromkeys(
            _route_token(value)
            for value in candidate_ids
            if _route_token(value) in _ORCHESTRATOR_ROUTE_CANDIDATES
        )
    )
    route = _route_token(getattr(output, "route", ""))
    workflow = [
        _route_token(item) for item in list(getattr(output, "workflow", []) or [])
    ]
    required = [
        item
        for item in dict.fromkeys([route, *workflow])
        if item in _ORCHESTRATOR_ROUTE_CANDIDATES
    ]
    return SpecialistDecisionEvidence.build(
        allowed_candidates,
        required_selected_ids=required,
        selection_required=True,
        require_complete_assessments=False,
        exact_required_selection=True,
    )


def _orchestrator_output_consistency_issue(output: Any) -> tuple[str, str] | None:
    """Reject contradictory manager fields without choosing a replacement route."""

    route = _route_token(getattr(output, "route", ""))
    target = _route_token(getattr(output, "target_agent", ""))
    if target and route and target != route:
        return (
            "orchestrator_route_target_conflict",
            "route and target_agent must identify the same selected owner.",
        )
    workflow = [
        _route_token(item) for item in list(getattr(output, "workflow", []) or [])
    ]
    invalid_workflow = [
        item for item in workflow if item not in _ORCHESTRATOR_ROUTE_CANDIDATES
    ]
    if invalid_workflow:
        return (
            "orchestrator_workflow_contains_unsupported_owner",
            (
                "workflow may contain only registered KBA agent routes. Repair or "
                "remove: " + ", ".join(dict.fromkeys(invalid_workflow)) + "."
            ),
        )
    if len(workflow) != len(set(workflow)):
        return (
            "orchestrator_workflow_contains_duplicate_owner",
            "workflow must list each selected agent once, in execution order.",
        )
    decision_rows = [
        ("decision", getattr(output, "decision", None)),
        *[
            (f"provider_context_decisions[{index}]", decision)
            for index, decision in enumerate(
                list(getattr(output, "provider_context_decisions", []) or [])
            )
        ],
    ]
    conflicting_rows = [
        label
        for label, decision in decision_rows
        if decision is not None
        and bool(getattr(decision, "needs_more_context", False))
        and bool(
            str(getattr(decision, "selected_candidate_id", "") or "").strip()
            or list(getattr(decision, "selected_candidate_ids", []) or [])
            or any(
                getattr(item, "disposition", "") == "selected"
                for item in list(getattr(decision, "candidate_assessments", []) or [])
            )
        )
    ]
    if conflicting_rows:
        return (
            "orchestrator_needs_more_context_selection_conflict",
            (
                "A decision cannot claim selected identities while also setting "
                "needs_more_context=true. Repair: " + ", ".join(conflicting_rows) + "."
            ),
        )
    return None


def _chief_evidence(output: Any) -> SpecialistDecisionEvidence:
    recommended_route = getattr(output, "recommended_route", None)
    workflow_type = str(getattr(recommended_route, "workflow_type", "") or "").strip()
    durable = getattr(output, "durable_handoff", None)
    durable_agent = _route_token(getattr(durable, "agent", "")) if durable else ""
    context_agents = [
        _route_token(getattr(item, "agent", ""))
        for item in list(getattr(output, "context_handoffs", []) or [])
    ]
    candidates = [
        *(f"workflow:{item}" for item in _CHIEF_WORKFLOW_CANDIDATES),
        *_ORCHESTRATOR_ROUTE_CANDIDATES,
    ]
    required = [
        *( [f"workflow:{workflow_type}"] if workflow_type else [] ),
        *( [durable_agent] if durable_agent else [] ),
        *(item for item in context_agents if item),
    ]
    return SpecialistDecisionEvidence.build(
        candidates,
        required_selected_ids=required,
        selection_required=True,
        require_complete_assessments=False,
        exact_required_selection=True,
    )


def _chief_output_consistency_issue(output: Any) -> tuple[str, str] | None:
    """Reject internally contradictory Chief workflow and handoff fields."""

    recommended_route = getattr(output, "recommended_route", None)
    workflow_type = str(getattr(recommended_route, "workflow_type", "") or "").strip()
    durable = getattr(output, "durable_handoff", None)
    durable_agent = _route_token(getattr(durable, "agent", "")) if durable else ""
    workflow_owner = {
        "gmail-summary": "gmail_triage",
        "gmail-triage": "gmail_triage",
    }.get(workflow_type, "")
    if workflow_owner and durable_agent and durable_agent != workflow_owner:
        return (
            "chief_workflow_handoff_conflict",
            (
                f"The {workflow_type} workflow cannot hand off to {durable_agent}; "
                f"its compatible durable owner is {workflow_owner}."
            ),
        )
    for context_handoff in list(getattr(output, "context_handoffs", []) or []):
        before_agent = _route_token(getattr(context_handoff, "before_agent", ""))
        if before_agent and durable_agent and before_agent != durable_agent:
            return (
                "chief_context_handoff_target_conflict",
                (
                    "A before_durable_handoff context stage must name the same durable "
                    "specialist selected by Chief of Staff."
                ),
            )
    return None


def _zotero_substantive_summary_issue(output: Any) -> tuple[str, str] | None:
    """Require the specialist to author the requested abstract synthesis itself."""

    decision = getattr(output, "decision", None)
    selected = list(getattr(decision, "selected_candidate_ids", []) or [])
    needs_more_context = bool(getattr(decision, "needs_more_context", False))
    if not selected or needs_more_context:
        return None
    summary = " ".join(str(getattr(output, "summary", "") or "").split())
    if not summary:
        return (
            "zotero_substantive_summary_missing",
            (
                "The selected Zotero evidence must be synthesized in summary. Do not "
                "put the requested answer only in relevant_evidence or diagnostics."
            ),
        )
    procedural_summary = bool(
        re.search(
            r"\b(?:selected|identified|found)\b.*\b(?:summari[sz]ed|summary)\b",
            summary,
            re.IGNORECASE,
        )
        or re.search(
            r"\b(?:summary|response)\b.*\b(?:word limit|requested format|constrained)\b",
            summary,
            re.IGNORECASE,
        )
    )
    if procedural_summary:
        return (
            "zotero_substantive_summary_missing",
            (
                "summary must contain the specialist's substantive answer grounded in "
                "the selected Zotero evidence, not a description of the selection or "
                "formatting process."
            ),
        )
    return None


def _airtable_evidence(
    output: Any,
    *,
    provider_candidate_ids: Iterable[object] | None = None,
) -> SpecialistDecisionEvidence:
    candidates = (
        list(provider_candidate_ids)
        if provider_candidate_ids is not None
        else list(getattr(output, "candidate_record_ids", []) or [])
    )
    recommended = str(getattr(output, "recommended_record_identity", "") or "").strip()
    required = [recommended] if recommended else []
    return SpecialistDecisionEvidence.build(
        candidates,
        required_selected_ids=required,
        selection_required=bool(recommended),
        exact_required_selection=bool(recommended),
    )


def _workspace_evidence(
    output: Any,
    *,
    provider_candidate_ids: Iterable[object] | None = None,
) -> SpecialistDecisionEvidence:
    source_ids = [
        getattr(source, "source_id", "")
        for source in list(getattr(output, "sources", []) or [])
    ]
    candidates = (
        list(provider_candidate_ids)
        if provider_candidate_ids is not None
        else [
            *source_ids,
            *list(getattr(output, "relevant_files", []) or []),
            *list(getattr(output, "relevant_docs", []) or []),
            *list(getattr(output, "relevant_sheets", []) or []),
            *list(getattr(output, "relevant_folders", []) or []),
        ]
    )
    recommended = str(getattr(output, "recommended_target", "") or "").strip()
    required = [recommended] if recommended else []
    return SpecialistDecisionEvidence.build(
        candidates,
        required_selected_ids=required,
        selection_required=bool(recommended),
        exact_required_selection=bool(recommended),
    )


def _zotero_evidence(
    output: Any,
    *,
    provider_candidate_ids: Iterable[object] | None = None,
) -> SpecialistDecisionEvidence:
    item_keys = list(getattr(output, "zotero_item_keys", []) or [])
    collection_keys = list(getattr(output, "collection_keys", []) or [])
    source_ids = list(getattr(output, "source_ids", []) or [])
    candidates = (
        list(provider_candidate_ids)
        if provider_candidate_ids is not None
        else [*item_keys, *collection_keys, *source_ids]
    )
    required = [*item_keys, *collection_keys]
    return SpecialistDecisionEvidence.build(
        candidates,
        required_selected_ids=required,
        selection_required=bool(required),
        exact_required_selection=False,
    )


__all__ = [
    "business_research_comparison_decision_contract",
    "business_research_context_decision_contract",
    "calendar_action_interpreter_decision_contract",
    "business_research_decision_contract",
    "chief_of_staff_decision_contract",
    "context_agent_decision_contract",
    "orchestrator_decision_contract",
    "opportunity_scout_decision_contract",
    "opportunity_scout_synthesis_decision_contract",
    "outreach_composer_decision_contract",
    "outreach_variant_set_decision_contract",
]
