from __future__ import annotations

from keystone_agents.tools.html_review_tool import (
    HtmlReviewResult,
    deterministic_html_review,
    extract_research_claims_from_html,
    run_agent_html_review,
)


def test_deterministic_html_review_extracts_bounded_claims() -> None:
    result = deterministic_html_review(
        html_or_text=(
            "<html><body><p>Mentavi offers clinician-reviewed mental health "
            "diagnostic evaluations for ADHD.</p></body></html>"
        ),
        subject="Mentavi",
        url="https://mentavi.com",
    )

    assert result.status == "success"
    assert result.provider == "agents-sdk-html-review"
    assert any("diagnostic evaluations" in claim for claim in result.claims)


def test_extract_research_claims_from_html_tool_returns_json() -> None:
    payload = extract_research_claims_from_html(
        html_or_text="Headway supports mental health providers with insurance workflows.",
        subject="Headway",
        url="https://headway.co",
    )

    result = HtmlReviewResult.model_validate_json(payload)
    assert result.url == "https://headway.co"
    assert any("mental health providers" in claim for claim in result.claims)


def test_run_agent_html_review_uses_injected_runner_without_live_model_call() -> None:
    def fake_runner(prompt: str, payload: dict[str, object]) -> dict[str, object]:
        assert "Use only the supplied text" in prompt
        assert payload["subject"] == "Curebase"
        return {
            "url": payload["url"],
            "subject": payload["subject"],
            "claims": ["Curebase provides clinical trial software for research teams."],
        }

    result = run_agent_html_review(
        html_or_text="Curebase provides clinical trial software for research teams.",
        subject="Curebase",
        url="https://www.curebase.com",
        live=False,
        runner=fake_runner,
    )

    assert result.claims == ["Curebase provides clinical trial software for research teams."]
