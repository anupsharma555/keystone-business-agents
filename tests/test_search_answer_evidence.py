"""Offline proof that a searched answer cites only completed search results."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from keystone_agents.receipts.normalization import normalize_tool_output_receipt
from keystone_agents.runtime.search_answer_evidence import validate_search_answer_evidence
from keystone_agents.tools.search_provider import SearchResult


def _search_run(links: list[str]) -> tuple[list[SimpleNamespace], list[dict], list[dict]]:
    results = [SearchResult(title=f"Result {index}", link=link) for index, link in enumerate(links)]
    raw = [SimpleNamespace(new_items=[
        {"type": "function_call", "call_id": "search-1", "name": "search_web"},
        {"type": "function_call_output", "call_id": "search-1",
         "output": json.dumps([result.model_dump(mode="json") for result in results])},
    ])]
    invocations = [
        {"tool_name": "search_web", "invocation_index": 1, "status": "started"},
        {"tool_name": "search_web", "invocation_index": 1, "status": "completed"},
    ]
    receipt = normalize_tool_output_receipt("search_web", results)
    assert receipt is not None
    return raw, invocations, [receipt]


def test_completed_search_requires_exact_returned_visible_source() -> None:
    raw, invocations, receipts = _search_run([
        "http://example.test/report/?utm_source=search",
        "https://example.test/other",
    ])
    verdict = validate_search_answer_evidence(
        raw, invocations, receipts,
        "The report describes the result: https://example.test/report",
    )

    assert verdict.passed is True
    assert verdict.returned_url_count == 2
    assert verdict.visible_urls == ("https://example.test/report",)


@pytest.mark.parametrize(
    ("links", "answer", "expected"),
    [
        ([], "No source is available.", "search_receipt_missing"),
        (["not-a-url"], "Result: https://example.test/report", "search_result_missing"),
        (["https://example.test/report"], "A factual answer with no link.",
         "visible_source_missing"),
        (["https://example.test/report"],
         "https://example.test/report and https://example.test/uncited",
         "source_not_returned"),
    ],
)
def test_search_answer_rejects_empty_invalid_or_unreturned_sources(
    links: list[str], answer: str, expected: str,
) -> None:
    raw, invocations, receipts = _search_run(links)
    verdict = validate_search_answer_evidence(raw, invocations, receipts, answer)

    assert verdict.passed is False
    assert verdict.reason == expected


def test_search_answer_requires_completed_invocation_and_retained_receipt() -> None:
    raw, invocations, receipts = _search_run(["https://example.test/report"])
    answer = "Result: https://example.test/report"

    assert validate_search_answer_evidence(
        raw, [{**invocations[0], "status": "failed"}], receipts, answer,
    ).reason == "search_call_missing"
    assert validate_search_answer_evidence(
        raw, invocations, [], answer,
    ).reason == "search_receipt_missing"
    assert validate_search_answer_evidence(
        [], invocations, receipts, answer,
    ).reason == "search_result_missing"


def test_search_answer_rejects_second_attempt_even_if_first_succeeded() -> None:
    raw, invocations, receipts = _search_run(["https://example.test/report"])
    second_attempt = [
        *invocations,
        {"tool_name": "search_web", "invocation_index": 2, "status": "started"},
        {"tool_name": "search_web", "invocation_index": 2, "status": "blocked"},
    ]

    verdict = validate_search_answer_evidence(
        raw, second_attempt, receipts, "Result: https://example.test/report",
    )
    assert verdict.passed is False
    assert verdict.reason == "search_call_count_invalid"

    mismatched_completion = [
        invocations[0], {**invocations[1], "invocation_index": 2},
    ]
    assert validate_search_answer_evidence(
        raw, mismatched_completion, receipts,
        "Result: https://example.test/report",
    ).reason == "search_call_count_invalid"


def test_search_answer_does_not_promote_unattested_or_hosted_only_output() -> None:
    raw, invocations, receipts = _search_run(["https://example.test/report"])
    answer = "Result: https://example.test/report"
    wrong_receipt = normalize_tool_output_receipt(
        "search_web", [SearchResult(title="Other", link="https://example.test/other")]
    )
    assert wrong_receipt is not None

    assert validate_search_answer_evidence(
        raw, invocations, [wrong_receipt], answer,
    ).reason == "search_result_missing"
    hosted_only = [SimpleNamespace(new_items=[
        {"type": "web_search_call", "call_id": "hosted-1", "status": "completed"},
    ])]
    assert validate_search_answer_evidence(
        hosted_only, invocations, receipts, answer,
    ).reason == "search_result_missing"
