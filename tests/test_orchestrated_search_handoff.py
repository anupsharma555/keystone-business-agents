from __future__ import annotations

import json
import sys

import pytest

import keystone_agents.workflows as workflows
from keystone_agents.schemas.company_profile import CompanyProfile
from keystone_agents.schemas.opportunity import OpportunityScoutResult


def test_orchestrated_search_handoff_passes_hint_to_company_research(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_retrieve_company_profile_live(
        *,
        company: str,
        company_url: str | None = None,
        request_text: str | None = None,
        requested_provider: str | None = None,
        max_results: int = 5,
        retrieval_hint=None,
    ) -> tuple[CompanyProfile, dict[str, object]]:
        captured.update(
            {
                "company": company,
                "company_url": company_url,
                "request_text": request_text,
                "requested_provider": requested_provider,
                "max_results": max_results,
                "retrieval_hint": retrieval_hint,
            }
        )
        return (
            CompanyProfile(
                name=company,
                website=company_url,
                description="Fixture company profile.",
            ),
            {"mode": "live_search", "search_provider": "searxng"},
        )

    monkeypatch.setattr(
        workflows,
        "retrieve_company_profile_live",
        fake_retrieve_company_profile_live,
    )

    result = workflows.run_orchestrated_search_handoff(
        "https://www.neuroflow.com",
        live_search=True,
    )

    assert result.orchestrator_decision.route == "business_research_analyst"
    assert result.specialist_executed is True
    assert result.specialist_output_type == "CompanyProfile"
    assert captured["company"] == "Neuroflow"
    assert captured["company_url"] == "https://www.neuroflow.com"
    assert captured["request_text"] == "https://www.neuroflow.com"
    assert result.specialist_request["company_name"] == "Neuroflow"
    retrieval_hint = captured["retrieval_hint"]
    assert retrieval_hint is not None
    assert retrieval_hint.source == "request_heuristic"


def test_orchestrated_search_handoff_passes_hint_to_opportunity_scout(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_opportunity_scout_live(
        *,
        topic: str | None = None,
        max_results: int = 5,
        requested_provider: str | None = None,
        fallback_provider: str | None = None,
        retrieval_hint=None,
        save: bool = False,
        existing_state=None,
    ) -> tuple[OpportunityScoutResult, dict[str, object]]:
        captured.update(
            {
                "topic": topic,
                "max_results": max_results,
                "requested_provider": requested_provider,
                "fallback_provider": fallback_provider,
                "retrieval_hint": retrieval_hint,
                "save": save,
                "existing_state": existing_state,
            }
        )
        return (
            OpportunityScoutResult(topic=topic, dry_run=False, records=[]),
            {"mode": "live_search", "search_provider": "searxng"},
        )

    monkeypatch.setattr(workflows, "run_opportunity_scout_live", fake_run_opportunity_scout_live)

    result = workflows.run_orchestrated_search_handoff(
        "find 5 behavioral health AI companies",
        live_search=True,
        max_results=3,
    )

    assert result.orchestrator_decision.route == "opportunity_scout"
    assert result.specialist_executed is True
    assert result.specialist_output_type == "OpportunityScoutResult"
    assert captured["topic"] == "find 5 behavioral health AI companies"
    assert captured["max_results"] == 3
    retrieval_hint = captured["retrieval_hint"]
    assert retrieval_hint is not None
    assert retrieval_hint.source == "request_heuristic"


def test_orchestrated_search_handoff_skips_non_search_route() -> None:
    result = workflows.run_orchestrated_search_handoff(
        {"subject": "Hello", "from": "person@example.com", "body": "Hi"},
        live_search=False,
    )

    assert result.orchestrator_decision.route == "gmail_triage"
    assert result.specialist_executed is False
    assert any("search-oriented specialist handoff runner" in note for note in result.audit_notes)


def test_orchestrated_search_handoff_cli_runs_fixture_company_research(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import scripts.run_orchestrated_search_handoff as cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_orchestrated_search_handoff.py",
            "--input",
            "https://www.neuroflow.com",
            "--json",
        ],
    )

    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["orchestrator_decision"]["route"] == "business_research_analyst"
    assert payload["specialist_executed"] is True
    assert payload["specialist_output_type"] == "CompanyProfile"
    assert payload["send_enabled"] is False


def test_orchestrated_search_handoff_cli_live_search_requires_no_dry_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_orchestrated_search_handoff as cli

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_orchestrated_search_handoff.py",
            "--input",
            "find behavioral health AI companies",
            "--live-search",
        ],
    )

    with pytest.raises(SystemExit, match="--live-search requires --no-dry-run"):
        cli.main()
