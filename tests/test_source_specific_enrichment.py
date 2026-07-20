from __future__ import annotations

from types import SimpleNamespace

from keystone_agents.source_specific_enrichment import (
    classify_source_reference,
    enrich_source_reference,
)


def test_classify_source_reference_detects_known_source_families() -> None:
    assert (
        classify_source_reference(url="https://clinicaltrials.gov/study/NCT06976697")
        == "clinical_trials"
    )
    assert classify_source_reference(url="https://pubmed.ncbi.nlm.nih.gov/39433921/") == "pubmed"
    assert classify_source_reference(url="https://doi.org/10.1038/s41591-024-03305-y") == "doi"
    assert classify_source_reference(url="", source_id="zotero:item:AP9SKRPZ") == "zotero"


def test_clinical_trials_enrichment_extracts_structured_trial_fields() -> None:
    def fake_get(url: str, **_kwargs):
        assert url.endswith("/NCT06976697")
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "protocolSection": {
                    "identificationModule": {
                        "nctId": "NCT06976697",
                        "briefTitle": "Home-Based tDCS Treatment Of MDD",
                    },
                    "statusModule": {"overallStatus": "RECRUITING"},
                    "designModule": {
                        "studyType": "INTERVENTIONAL",
                        "enrollmentInfo": {"count": 200, "type": "ESTIMATED"},
                    },
                    "eligibilityModule": {
                        "minimumAge": "22 Years",
                        "maximumAge": "70 Years",
                        "sex": "ALL",
                    },
                }
            },
        )

    result = enrich_source_reference(
        url="https://clinicaltrials.gov/study/NCT06976697",
        title="Study Details",
        source_id="zotero:item:AP9SKRPZ",
        live=True,
        http_get=fake_get,
    )

    assert result.status == "success"
    assert result.metadata["nct_id"] == "NCT06976697"
    assert "Overall status: RECRUITING" in result.structured_facts
    assert "Enrollment: 200 (ESTIMATED)" in result.structured_facts


def test_crossref_enrichment_removes_presentation_suffix_from_doi() -> None:
    requested_urls: list[str] = []

    def fake_get(url: str, **_kwargs):
        requested_urls.append(url)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "message": {
                    "title": ["Interoperability article"],
                    "author": [
                        {"given": "Ada", "family": "Lovelace"},
                        {"name": "Example Consortium"},
                    ],
                    "container-title": ["Journal of Interoperability"],
                    "publisher": "Example Publisher",
                    "type": "journal-article",
                }
            },
        )

    result = enrich_source_reference(
        url="https://doi.org/10.3389/fmed.2026.1736785/full",
        title="Interoperability article",
        source_id="zotero:item:ITEM1",
        live=True,
        http_get=fake_get,
    )

    assert requested_urls == ["https://api.crossref.org/works/10.3389/fmed.2026.1736785"]
    assert result.status == "success"
    assert result.metadata["doi"] == "10.3389/fmed.2026.1736785"
    assert result.metadata["authors"] == ["Ada Lovelace", "Example Consortium"]
    assert result.metadata["publication_title"] == "Journal of Interoperability"
    assert "Authors: Ada Lovelace; Example Consortium" in result.structured_facts
    assert "Container: Journal of Interoperability" in result.structured_facts
