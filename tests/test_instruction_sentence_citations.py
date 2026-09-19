"""Citation-only suffixes do not add prose sentences or bypass URL validation."""

from types import SimpleNamespace

import pytest

import keystone_agents.instruction_following as instruction_following
from keystone_agents.instruction_following import (
    InstructionFollowingRepairOutput,
    resolve_instruction_following_response,
    validate_output_constraints,
)
from keystone_agents.schemas.output_constraints import InterpretedOutputConstraints

TWO_SENTENCES = "Your request is still awaiting an answer. Wait before sending another reminder."
URL = "https://mail.google.com/mail/#all/synthetic-thread"
CONSTRAINTS = InterpretedOutputConstraints(
    sentence_count_mode="exact",
    sentence_count=2,
    include_source_urls=True,
)


@pytest.mark.parametrize(
    "footer",
    [
        f"Gmail link: {URL}",
        f"Source: {URL}",
        f"**Source:** {URL}",
        URL,
        f"[Gmail source]({URL})",
        f"Sources: [Gmail]({URL}); [Reference](https://example.test/report)",
        f"Sources:\n- [Gmail]({URL})\n- https://example.test/report",
        f"Sources:\n• [Gmail]({URL})\n+ https://example.test/report",
    ],
)
def test_two_prose_sentences_with_citation_footer_pass(footer):
    result = validate_output_constraints(f"{TWO_SENTENCES}\n\n{footer}", CONSTRAINTS)
    assert result.passed
    assert result.sentence_count == 2


@pytest.mark.parametrize(
    "suffix",
    [
        f" Gmail link: {URL}",
        f" Source: {URL}",
        f" **Reference:** [{URL}]({URL})",
        f" References: [Gmail message]({URL}).",
        f" Gmail: <{URL}|Open in Gmail>",
        f" Sources: <{URL}>; <https://example.test/report?view=full#finding|Report>.",
        f" {URL}",
    ],
)
def test_two_prose_sentences_with_inline_trailing_citation_pass(suffix):
    response = TWO_SENTENCES + suffix
    result = validate_output_constraints(response, CONSTRAINTS)
    assert result.passed
    assert result.sentence_count == 2
    assert result.checked_text == response


@pytest.mark.parametrize(
    "suffix",
    [
        f" Gmail link: {URL}",
        f" [Gmail source]({URL})",
        f" References: <{URL}|Message>.",
    ],
)
def test_one_prose_sentence_with_inline_trailing_citation_pass(suffix):
    constraints = CONSTRAINTS.model_copy(update={"sentence_count": 1})
    response = "The selected message contains the requested evidence." + suffix
    result = validate_output_constraints(response, constraints)
    assert result.passed
    assert result.sentence_count == 1
    assert result.checked_text == response


@pytest.mark.parametrize(
    "third",
    [
        f"Source: The sender already answered your question. {URL}",
        f"Gmail link: The request was denied. {URL}",
        f"The sender has confirmed receipt.\nSource: {URL}",
        f"Source: [Gmail]({URL}) confirms receipt.",
        f"References: {URL} and this confirms the result.",
        f"Gmail: {URL} describes the selected message.",
    ],
)
def test_source_prefix_does_not_hide_substantive_third_sentence(third):
    result = validate_output_constraints(f"{TWO_SENTENCES}\n{third}", CONSTRAINTS)
    assert not result.passed
    assert result.sentence_count >= 3


def test_source_label_without_url_still_fails_url_validation():
    result = validate_output_constraints(f"{TWO_SENTENCES}\nSource:", CONSTRAINTS)
    assert not result.passed
    assert any("URL" in violation or "url" in violation for violation in result.violations)


def test_inline_citation_does_not_make_missing_or_extra_prose_valid():
    missing_url = validate_output_constraints(
        f"{TWO_SENTENCES} Gmail link:",
        CONSTRAINTS,
    )
    extra_prose = validate_output_constraints(
        f"{TWO_SENTENCES} A third substantive sentence. Gmail link: {URL}",
        CONSTRAINTS,
    )
    assert not missing_url.passed
    assert "visible source URL missing" in missing_url.violations
    assert not extra_prose.passed
    assert extra_prose.sentence_count == 3


def test_inline_url_keeps_prose_and_source_validation():
    result = validate_output_constraints(
        f"The thread at {URL} contains your request. There is no later reply.",
        CONSTRAINTS,
    )
    assert result.passed
    assert result.sentence_count == 2


def test_instruction_repair_uses_bounded_evidence_and_remains_tool_free(monkeypatch):
    retained = "x" * 12000
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs)
        assert kwargs["agent"].tools == []
        assert kwargs["agent"].handoffs == []
        assert kwargs["typed_input"].bounded_evidence == retained
        return SimpleNamespace(
            output=InstructionFollowingRepairOutput(
                response_text=f"{TWO_SENTENCES} Gmail link: {URL}",
            ),
            usage={"requests": 1},
            cost={"estimated_usd": 0.001},
            request_cache={},
            execution_telemetry={},
        )

    monkeypatch.setattr(instruction_following, "run_typed_sdk_agent", fake_run)
    resolution = resolve_instruction_following_response(
        "Only one sentence.",
        original_request="Return exactly two sentences and include the source URL.",
        manual_plan={
            "ask_shape": {"output_constraints": CONSTRAINTS.model_dump(mode="json")}
        },
        bounded_evidence=retained + "unretained-marker",
        live=True,
    )

    assert len(calls) == 1
    assert resolution.repair_succeeded
    assert resolution.validation.passed
