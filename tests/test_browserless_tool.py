from __future__ import annotations

import json

import pytest

from keystone_agents.tools.browserless_tool import (
    BrowserlessTool,
    ScrapeResult,
    extract_company_signals,
    fetch_company_page,
    fetch_rendered_page,
)


def test_fetch_rendered_page_dry_run_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BROWSERLESS_API_KEY", raising=False)

    result = fetch_rendered_page("https://example.com")

    assert isinstance(result, ScrapeResult)
    assert result.url == "https://example.com"
    assert result.status == "dry-run"
    assert "No network request was made" in result.text_or_markdown
    assert result.source == "browserless:dry-run"


def test_browserless_missing_key_raises_only_in_live_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("BROWSERLESS_API_KEY", raising=False)

    fetch_rendered_page("https://example.com", live=False)
    with pytest.raises(RuntimeError, match="BROWSERLESS_API_KEY"):
        fetch_rendered_page("https://example.com", live=True)


def test_browserless_error_does_not_print_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "browserless-secret-test-value"
    monkeypatch.setenv("BROWSERLESS_API_KEY", secret)

    with pytest.raises(NotImplementedError) as exc:
        fetch_rendered_page("https://example.com", live=True)

    assert secret not in str(exc.value)


def test_browserless_class_wrapper_uses_dry_run() -> None:
    result = BrowserlessTool().render("https://example.com")

    assert result["status"] == "dry-run"
    assert result["source"] == "browserless:dry-run"


def test_fetch_company_page_extracts_fixture_claims() -> None:
    fixture = json.dumps(
        {
            "name": "Curebase",
            "sources": [
                {
                    "source_type": "website",
                    "html": "<html><script>ignore()</script><body>Curebase page.</body></html>",
                    "supported_claims": ["Curebase describes decentralized trials."],
                }
            ],
        }
    )

    payload = json.loads(fetch_company_page("https://example.com", fixture_json=fixture))

    assert payload["mode"] == "dry_run"
    assert payload["title"] == "Curebase"
    assert payload["clean_text"] == "Curebase page."
    assert payload["claims"] == ["Curebase describes decentralized trials."]


def test_fetch_company_page_uses_description_fallback_and_rejects_live() -> None:
    fixture = json.dumps(
        {
            "name": "Curebase",
            "description": "Curebase supports trial operations.",
            "fit": "Relevant to clinical evidence generation.",
            "sources": [],
        }
    )

    payload = json.loads(fetch_company_page("https://example.com", fixture_json=fixture))

    assert payload["claims"] == [
        "Curebase supports trial operations.",
        "Relevant to clinical evidence generation.",
    ]
    with pytest.raises(RuntimeError, match="explicit integration implementation"):
        fetch_company_page("https://example.com", dry_run=False)


def test_extract_company_signals_uses_fixture_and_rejects_live() -> None:
    fixture = json.dumps(
        {
            "name": "Curebase",
            "website": "https://example.com",
            "description": "Curebase supports decentralized clinical trial operations.",
            "fit_summary": "Clinical evidence generation fit.",
            "clinical_ai_relevance": 70,
            "consulting_fit_score": 80,
            "confidence_score": 0.8,
            "sources": [
                {
                    "source_id": "fixture:curebase",
                    "title": "Fixture",
                    "url": "fixture://curebase",
                    "source_type": "fixture",
                    "supported_claims": ["Curebase supports decentralized trials."],
                    "confidence": 0.8,
                }
            ],
        }
    )

    payload = json.loads(extract_company_signals("Curebase", fixture_json=fixture))

    assert payload["name"] == "Curebase"
    assert payload["consulting_fit_score"] >= 80
    with pytest.raises(RuntimeError, match="explicit live tools"):
        extract_company_signals("Curebase", dry_run=False)
