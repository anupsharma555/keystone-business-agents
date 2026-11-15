from copy import deepcopy

import scripts.run_company_research as cli


def _payload(topic=True):
    return {
        "output_type": "CompanyResearchFocusedBrief",
        "manual_request_plan": {"target_type": "topic" if topic else "company"},
        "output": {
            "company_name": "Example subject", "answer": "The timetable changed.",
            "product": "Not applicable for this request.",
            "customers": "Not applicable for this request.",
            "traction_signals": "The operator published revised service times.",
            "facts": [{"text": "Weekend services start later.", "source_ids": ["internal:1"]}],
            "unknowns": ["Leadership is not applicable to this request and remains unknown.",
                         "The revised times do not cover holiday service."],
            "sources": [{"source_id": "internal:1", "title": "Published timetable",
                         "url": "https://example.test/timetable"}],
        },
    }


def test_topic_answer_omits_placeholders_and_resolves_citations_without_mutation():
    payload = _payload()
    before = deepcopy(payload)
    text = cli._company_research_sdk_human_summary(payload)
    assert "Healthcare buyer fit" not in text
    assert "Product/workflow" not in text
    assert "Leadership" not in text
    assert "holiday service" in text
    assert "internal:1" not in text
    assert "Weekend services start later. ([1])" in text
    assert "*Sources:*\n1. <https://example.test/timetable|Published timetable>" in text
    assert payload == before


def test_company_context_remains_and_substantive_nonapplicability_is_preserved():
    payload = _payload(False)
    payload["output"]["product"] = "Scheduling software."
    payload["output"]["customers"] = "Transit agencies."
    text = cli._company_research_sdk_human_summary(payload)
    assert "Product/workflow: Scheduling software." in text
    assert "Healthcare buyer fit: Transit agencies." in text
    payload = _payload()
    payload["output"]["unknowns"] = ["These results are not applicable to holiday services."]
    assert "not applicable to holiday services" in cli._company_research_sdk_human_summary(payload)


def test_mixed_missing_citation_preserves_valid_link_and_reports_gap():
    payload = _payload()
    payload["output"]["facts"][0]["source_ids"].append("internal:missing")
    text = cli._company_research_sdk_human_summary(payload)
    assert "<https://example.test/timetable|Published timetable>" in text
    assert "Weekend services start later. ([1]; Source verification needed)" in text
    assert "Source verification needed" in text
    assert "internal:" not in text


def test_explicit_answer_only_constraint_bypasses_optional_details():
    payload = _payload()
    payload["manual_request_plan"]["ask_shape"] = {
        "output_constraints": {
            "scope": "answer", "sentence_count_mode": "exact", "sentence_count": 1,
        },
    }
    text = cli._company_research_sdk_human_summary(payload)
    assert "The timetable changed." in text
    assert "Detailed Summary" not in text
    assert "https://example.test/timetable" in text


def test_long_details_remain_bounded_with_visible_excerpt_and_intact_source_url():
    payload = _payload()
    payload["output"]["product"] = "Long descriptive context. " * 1000
    payload["output"]["facts"][0]["text"] = "Qualified evidence. " * 1000
    payload["output"]["unknowns"] = ["Important limitations remain. " * 1000]
    text = cli._company_research_sdk_human_summary(payload)
    assert len(text) < 2200
    assert "[excerpt; qualifications may follow]" in text
    assert "<https://example.test/timetable|Published timetable>" in text


def test_sources_deduplicate_urls_and_keep_all_displayed_fact_citations():
    payload = _payload()
    sources = payload["output"]["sources"]
    facts = payload["output"]["facts"]
    sources.extend(
        {"source_id": f"internal:{number}", "title": f"Source {number}",
         "url": f"https://example.test/source/{number}"}
        for number in range(2, 8)
    )
    sources.append({"source_id": "internal:duplicate", "title": "Duplicate",
                    "url": "https://www.example.test/timetable/"})
    facts[:] = [
        {"text": "Finding one.", "source_ids": ["internal:duplicate", "internal:2"]},
        {"text": "Finding two.", "source_ids": ["internal:3", "internal:4"]},
        {"text": "Finding three.", "source_ids": ["internal:5", "internal:6"]},
    ]

    text = cli._company_research_sdk_human_summary(payload)

    assert "Finding one. ([1]; [2])" in text
    assert "Finding three. ([5]; [6])" in text
    assert text.count("example.test/timetable|") == 1
    assert "1. <https://example.test/timetable|Published timetable>" in text
    assert "6. <https://example.test/source/6|Source 6>" in text
    assert "https://example.test/source/7" not in text
    assert "internal:" not in text


def test_verified_source_alias_resolves_matching_url_without_admitting_other_urls():
    payload = _payload()
    payload["output"]["sources"].append({
        "source_id": "internal:unverified", "title": "Unverified",
        "url": "https://unverified.test/page",
    })
    payload["output"]["facts"][0]["source_ids"] = [
        "internal:1", "internal:unverified",
    ]
    payload["verified_source_evidence"] = [{
        "entity": "Example subject",
        "sources": [{"source_id": "verified:1", "title": "Verified timetable",
                     "url": "https://www.example.test/timetable/"}],
    }]

    text = cli._company_research_sdk_human_summary(payload)

    assert "Weekend services start later. ([1]; Source verification needed)" in text
    assert text.count("www.example.test/timetable/|") == 1
    assert "1. <https://www.example.test/timetable/|Verified timetable>" in text
    assert "unverified.test" not in text
    assert "internal:" not in text


def test_fourth_fact_reference_is_visible_and_later_missing_reference_is_flagged():
    payload = _payload()
    payload["output"]["sources"].extend(
        {"source_id": f"internal:{number}", "title": f"Source {number}",
         "url": f"https://example.test/source/{number}"}
        for number in range(2, 5)
    )
    payload["output"]["facts"][0]["source_ids"] = [
        "internal:1", "internal:2", "internal:3", "internal:4", "internal:missing",
    ]

    text = cli._company_research_sdk_human_summary(payload)

    assert "Weekend services start later. ([1]; [2]; [3]; [4]; Source verification needed)" in text
    assert "4. <https://example.test/source/4|Source 4>" in text
    assert "internal:" not in text


def test_omitted_references_are_disclosed_and_unresolved_ids_still_flagged():
    payload = _payload()
    payload["output"]["facts"][0]["source_ids"] = [
        *(["internal:1"] * 12), "internal:missing",
    ]

    text = cli._company_research_sdk_human_summary(payload)

    assert "[1]; Source verification needed; Additional source references omitted" in text
    assert "internal:" not in text


def test_quick_research_uses_only_a_citation_retained_in_the_short_answer():
    payload = _payload()
    payload["input"] = "Quick question: What changed in the timetable?"
    payload["manual_request_plan"]["ask_shape"] = {"evidence_depth": "quick"}
    payload["verified_source_evidence"] = [{
        "entity": "Example subject",
        "sources": [{"source_id": "verified:1", "title": "Published timetable",
                     "url": "https://example.test/timetable"}],
    }]
    payload["output"]["answer"] = (
        "Weekend services start later. Source: https://example.test/timetable"
    )

    quick = cli._company_research_sdk_human_summary(payload)

    assert quick == payload["output"]["answer"]
    assert "Detailed Summary" not in quick
    assert quick.count("https://example.test/timetable") == 1

    payload["output"]["answer"] = (
        "The revised schedule requires careful review. " * 12
        + "Source: https://example.test/timetable"
    )
    long_answer = cli._company_research_sdk_human_summary(payload)
    assert "*Detailed Summary:*" in long_answer
    assert "https://example.test/timetable" in long_answer

    payload["output"]["answer"] = "Weekend services start later."
    uncited = cli._company_research_sdk_human_summary(payload)
    assert "*Detailed Summary:*" in uncited
    assert "https://example.test/timetable" in uncited
