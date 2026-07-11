from __future__ import annotations

from types import SimpleNamespace

from keystone_agents.tools.public_contact_tool import discover_public_company_contacts_impl
from keystone_agents.tools.search_provider import SearchResult

ABOUT_HTML = """
<html><body><section><h2>Our Leadership Team</h2>
<div><h3>Alex Rivera</h3><p>Chief Commercial Officer</p></div>
<div><h3>Jamie Chen</h3><p>Chief Medical Officer</p></div>
</section></body></html>
"""
CONTACT_HTML = """
<html><body><h1>Contact us</h1><p>General inquiries: info@sample.example</p></body></html>
"""


def _get(url: str, **_kwargs):
    return SimpleNamespace(
        status_code=200,
        text=ABOUT_HTML if "about" in url else CONTACT_HTML,
    )


def test_public_contact_discovery_prefers_official_commercial_leader() -> None:
    result = discover_public_company_contacts_impl(
        "Sample Health",
        "https://sample.example/about",
        "https://sample.example/contact",
        live=True,
        http_get=_get,
        search_results=[
            SearchResult(
                title="Alex Rivera - Sample Health | LinkedIn",
                link="https://www.linkedin.com/in/alex-rivera",
                snippet="Chief Commercial Officer - Sample Health",
                source="exa",
            )
        ],
    )

    assert result.status == "pass"
    assert result.search_requests == 0
    assert result.candidates[0].name == "Alex Rivera"
    assert result.candidates[0].role == "Chief Commercial Officer"
    assert result.candidates[0].profile_url.endswith("alex-rivera")
    assert result.candidates[0].search_corroborated is True
    assert result.corroborated_candidate_count == 1
    assert "info@sample.example" in result.candidates[0].contact_path
    assert result.no_draft is True
    assert result.send_enabled is False
    assert result.inferred_personal_email is False


def test_public_contact_discovery_blocks_without_relevant_official_role() -> None:
    result = discover_public_company_contacts_impl(
        "Sample Health",
        "https://sample.example/about",
        "https://sample.example/contact",
        live=True,
        http_get=lambda url, **kwargs: SimpleNamespace(
            status_code=200,
            text=(
                "<html><body><h3>Jamie Chen</h3><p>Chief Medical Officer</p></body></html>"
                if "about" in url
                else CONTACT_HTML
            ),
        ),
        search_results=[],
    )

    assert result.status == "blocked"
    assert result.candidates == []
    assert result.blocker


def test_public_contact_discovery_is_network_inert_by_default() -> None:
    result = discover_public_company_contacts_impl(
        "Sample Health",
        "https://sample.example/about",
        "https://sample.example/contact",
    )

    assert result.status == "dry-run"
    assert result.search_requests == 0
