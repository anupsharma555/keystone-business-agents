"""Explicit source catalogs constrain reader admission and availability claims."""

import json
from types import SimpleNamespace

import pytest

from keystone_agents.agents.business_research_analyst import _supplied_web_source_accesses
from keystone_agents.business_research_analyst.supplied_context import (
    project_explicit_research_evidence,
    supplied_research_input,
)
from keystone_agents.schemas.source_evidence import SourceEvidencePage
from keystone_agents.schemas.web_source import WebSourceAccess
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemArtifactRef,
    WorkItemFact,
    WorkItemKind,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemTarget,
)

URL = "https://example.org/selected-study"


def _access(source_id="selected", url=URL, available=True):
    return WebSourceAccess(
        source_id=source_id,
        selected_url=url,
        resolved_url=url,
        snapshot_sha256="a" * 64,
        total_chars=100,
        start_char=0,
        end_char=20,
        next_start_char=20 if available else None,
        available=available,
    )


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("preview", False),
        ("web", True),
        ("unavailable_web", False),
        ("pages", True),
    ],
)
def test_retained_reader_flag_matches_admitted_evidence(kind, expected):
    source = WorkItemSourceRef(
        source_id="selected",
        url=URL,
        provider="trafilatura",
        source_type="public_article",
        evidence_excerpt="A retained bounded study excerpt.",
        web_source_access=_access(available=kind == "web") if "web" in kind else None,
        evidence_pages=[SourceEvidencePage(evidence_excerpt="Retained page.")]
        if kind == "pages"
        else [],
    )
    item = WorkItem(
        id="wi_scope_test",
        kind=WorkItemKind.RESEARCH_BRIEF,
        title="Selected review",
        request_text="Review this selected source.",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(name="Selected study", object_type="topic"),
        sources=[source],
    )
    typed, _ = supplied_research_input(
        item,
        raw_request=item.request_text,
        sources=(source,),
        orchestration_context={},
    )
    assert json.loads(typed.source_context)["retained_source_read_enabled"] is expected
    readers = _supplied_web_source_accesses(typed)
    assert len(readers) == (1 if kind == "web" else 0)


def test_reader_catalog_rejects_historical_artifact_handle_before_model():
    packet = {
        "source_catalog": [{"source_id": "selected", "url": URL}],
        "selected_artifacts": [
            {
                "metadata": {
                    "web_source_access": _access(
                        "historical",
                        "https://example.org/historical-study",
                    ).model_dump(mode="json")
                }
            }
        ],
    }
    with pytest.raises(ValueError, match="outside the supplied source catalog"):
        _supplied_web_source_accesses(SimpleNamespace(source_context=json.dumps(packet)))


def test_matching_reader_catalog_and_legacy_without_catalog_remain_supported():
    access = _access().model_dump(mode="json")
    for catalog in ({}, {"source_catalog": [{"source_id": "selected", "url": URL}]}):
        packet = {**catalog, "source": {"source_id": "selected", "web_source_access": access}}
        assert _supplied_web_source_accesses(
            SimpleNamespace(source_context=json.dumps(packet))
        ) == (_access(),)
    with pytest.raises(ValueError, match="explicit source identities"):
        _supplied_web_source_accesses(
            SimpleNamespace(
                source_context=json.dumps(
                    {
                        "source_catalog": "invalid",
                        "web_source_access": access,
                    }
                )
            )
        )


def test_explicit_stage_excludes_saved_evidence_without_mutating_task_or_guidance():
    selected = WorkItemSourceRef(
        source_id="selected", url=URL, provider="operator_supplied",
        source_type="public_article", evidence_excerpt="CURRENT_SELECTED_EVIDENCE",
    )
    historical = WorkItemSourceRef(
        source_id="historical", url="https://example.org/historical-study",
        provider="operator_supplied", evidence_excerpt="STALE_PROJECT_EVIDENCE",
    )
    project = {
        "project_id": "synthetic-project", "name": "Evidence review",
        "objective": "Keep an evidence-bounded review for later discussion.",
        "approved_for_agent_use": True, "contains_phi": False,
        "allowed_actions": ["read"], "blocked_actions": ["send"],
        "source_refs": [historical.model_dump(mode="json")],
    }
    item = WorkItem(
        id="wi_scope_projection", kind=WorkItemKind.RESEARCH_BRIEF,
        title="Selected review", request_text="Review only the selected article.",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        target=WorkItemTarget(name="Selected study", object_type="topic", metadata={
            "project_context": project,
        }),
        sources=[selected, historical],
        facts=[WorkItemFact(
            key="historical", value="STALE_APPROVED_FACT",
            approval_state="approved_for_research", source_refs=[historical],
        ), WorkItemFact(
            key="same-source-prior-interpretation", value="STALE_SAME_SOURCE_FACT",
            approval_state="approved_for_research", source_refs=[selected],
        )],
        artifact_refs=[WorkItemArtifactRef(
            artifact_id="synthetic-old-artifact", artifact_type="research_brief",
            selected=True, summary="STALE_ARTIFACT_SUMMARY",
        )],
    )
    before = item.model_dump(mode="json")
    view = project_explicit_research_evidence(item, (selected,))
    guidance = {"approved_operator_context": {"facts": [{
        "value": "Reuse cached source material where appropriate.",
    }]}}
    typed, _ = supplied_research_input(
        view, raw_request=item.request_text, sources=(selected,),
        orchestration_context=guidance,
    )
    payload = json.loads(typed.source_context)
    assert "CURRENT_SELECTED_EVIDENCE" in typed.source_context
    for stale in (
        "STALE_PROJECT_EVIDENCE", "STALE_APPROVED_FACT", "STALE_SAME_SOURCE_FACT",
        "STALE_ARTIFACT_SUMMARY", historical.url,
    ):
        assert stale not in typed.source_context
    pack = payload["context_pack"]
    assert pack["approved_facts"] == []
    assert pack["project_context"]["source_refs"] == []
    assert pack["project_context"]["objective"] == project["objective"]
    assert pack["project_context"]["allowed_actions"] == ["read"]
    assert pack["project_context"]["blocked_actions"] == ["send"]
    assert payload["orchestrator_context"] == guidance
    assert item.model_dump(mode="json") == before

    # Ordinary supplied-context synthesis keeps the existing approved-fact policy.
    ordinary, _ = supplied_research_input(
        item, raw_request=item.request_text, sources=(selected,), orchestration_context={},
    )
    assert "STALE_APPROVED_FACT" in ordinary.source_context
