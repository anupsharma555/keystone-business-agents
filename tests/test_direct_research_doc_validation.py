from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_focused_brief_agent,
)
from keystone_agents.schemas.company_profile import (
    CompanyBriefSourceCitation,
    CompanyResearchFocusedBrief,
)
from keystone_agents.tools.website_extraction_tool import WebsiteExtractionResult
from scripts.run_direct_research_doc_validation import (
    _normalize_brief_source_identity,
    _require_workspace_write_gates,
    execute_validation,
)


def test_direct_research_agent_can_disable_all_tools() -> None:
    agent = build_business_research_analyst_focused_brief_agent(attach_tools=False)

    assert agent.tools == []


def test_direct_extraction_preserves_live_source_identity() -> None:
    from scripts.run_direct_research_doc_validation import _profile_from_extraction

    profile = _profile_from_extraction(
        "NeuroFlow",
        "https://www.neuroflow.com/",
        _extraction(),
    )

    assert [source.source_id for source in profile.sources] == [
        "direct:official-company-page"
    ]


def test_direct_result_normalizes_model_source_ids_by_exact_url() -> None:
    from scripts.run_direct_research_doc_validation import _profile_from_extraction

    profile = _profile_from_extraction(
        "NeuroFlow",
        "https://www.neuroflow.com/",
        _extraction(),
    )
    normalized = _normalize_brief_source_identity(_brief(), profile)

    assert normalized.source_ids_used == []
    assert [source.source_id for source in normalized.sources] == [
        "direct:official-company-page"
    ]


def test_direct_join_requires_all_workspace_gates_before_execution(monkeypatch) -> None:
    monkeypatch.setenv("KEYSTONE_GOOGLE_WORKSPACE_ALLOW_TEST_LIFECYCLE", "true")
    monkeypatch.delenv("GOOGLE_WORKSPACE_WRITES_ENABLED", raising=False)

    try:
        _require_workspace_write_gates()
    except SystemExit as exc:
        assert "GOOGLE_WORKSPACE_WRITES_ENABLED" in str(exc)
    else:
        raise AssertionError("missing generic Workspace write gate must fail closed")


def _extraction() -> WebsiteExtractionResult:
    return WebsiteExtractionResult(
        url="https://www.neuroflow.com/",
        title="NeuroFlow",
        provider="trafilatura",
        status="success",
        text_or_markdown=(
            "NeuroFlow provides behavioral health analytics and engagement infrastructure "
            "for healthcare organizations. It supports population risk identification."
        ),
        claims=[
            "NeuroFlow provides behavioral health analytics and engagement infrastructure.",
            "NeuroFlow supports healthcare organizations with population risk identification.",
        ],
    )


def _brief() -> CompanyResearchFocusedBrief:
    return CompanyResearchFocusedBrief(
        company_name="NeuroFlow",
        product="Behavioral health analytics and engagement infrastructure.",
        customers="Healthcare organizations.",
        traction_signals="The official page describes population risk support.",
        leadership="Unknown from the supplied page.",
        why_it_matters="Relevant to behavioral-health analytics and evaluation workflows.",
        sources=[
            CompanyBriefSourceCitation(
                source_id="direct:official-company-page",
                title="NeuroFlow",
                url="https://www.neuroflow.com/",
                source_type="company_site",
            )
        ],
        unknowns=["Independent validation and current leadership were not supplied."],
    )


def test_direct_research_join_persists_before_verified_doc(tmp_path: Path) -> None:
    research_output = tmp_path / "research.json"
    captured: dict[str, object] = {}

    def fake_doc(payload, **kwargs):
        captured["payload"] = payload
        captured["kwargs"] = kwargs
        return {
            "status": "passed",
            "failure": "",
            "openai_requests": 0,
            "same_document_identity": True,
            "source_url_count": 1,
            "receipts": {"trash_readback": {"passed": True, "trashed": True}},
            "send_enabled": False,
        }

    result = execute_validation(
        company="NeuroFlow",
        company_url="https://www.neuroflow.com/",
        model="gpt-5.4-mini",
        budget_usd=0.05,
        folder_path="KNIOps",
        research_output=research_output,
        extractor=lambda *_args, **_kwargs: _extraction(),
        model_runner=lambda *_args, **_kwargs: SimpleNamespace(
            final_output=_brief(),
            usage={"requests": 1, "input_tokens": 100, "output_tokens": 30},
            cost={"estimated_usd": 0.02},
            request_cache={"rate_limit_retries": 0},
        ),
        doc_runner=fake_doc,
    )

    assert result["status"] == "pass"
    assert result["research"]["official_source_preserved"] is True
    assert result["research"]["live_search"] is False
    assert research_output.exists()
    assert captured["payload"]["output_type"] == "CompanyResearchFocusedBrief"
    assert captured["kwargs"]["live"] is True


def test_direct_research_stops_before_doc_when_source_is_not_preserved(tmp_path: Path) -> None:
    doc_called = False

    def fake_doc(*_args, **_kwargs):
        nonlocal doc_called
        doc_called = True
        return {}

    brief = _brief().model_copy(
        update={
            "sources": [
                CompanyBriefSourceCitation(
                    source_id="wrong",
                    title="Wrong",
                    url="https://example.test/",
                )
            ]
        }
    )
    result = execute_validation(
        company="NeuroFlow",
        company_url="https://www.neuroflow.com/",
        model="gpt-5.4-mini",
        budget_usd=0.05,
        folder_path="KNIOps",
        research_output=tmp_path / "research.json",
        extractor=lambda *_args, **_kwargs: _extraction(),
        model_runner=lambda *_args, **_kwargs: SimpleNamespace(
            final_output=brief,
            usage={"requests": 1},
            cost={"estimated_usd": 0.02},
            request_cache={"rate_limit_retries": 0},
        ),
        doc_runner=fake_doc,
    )

    assert result["status"] == "partial"
    assert result["doc"]["executed"] is False
    assert doc_called is False
