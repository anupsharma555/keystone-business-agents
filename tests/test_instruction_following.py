from types import SimpleNamespace

import pytest

from keystone_agents.instruction_following import (
    InstructionFollowingRepairInput,
    InstructionFollowingRepairOutput,
    InstructionFollowingResolution,
    exact_output_validation_required,
    interpreted_output_constraints_text,
    output_constraints_from_plan,
    raw_request_output_contract,
    resolve_instruction_following_response,
    resolved_output_constraints,
    validate_output_constraints,
)
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.schemas.output_constraints import InterpretedOutputConstraints


def _plan(constraints: InterpretedOutputConstraints) -> ManualRequestPlan:
    return ManualRequestPlan(
        source="llm",
        ask_shape=AskShapePolicy(output_constraints=constraints),
    )


def test_exact_answer_word_count_ignores_source_section() -> None:
    constraints = InterpretedOutputConstraints(
        interpretation="exact 5-word answer",
        scope="answer",
        word_count_mode="exact",
        word_count=5,
    )

    validation = validate_output_constraints(
        "*Answer:*\nAbridge turns conversations into notes.\n\n"
        "*Useful reference:*\nhttps://www.abridge.com",
        constraints,
    )

    assert validation.passed is True
    assert validation.word_count == 5


def test_exact_word_count_treats_visible_url_as_one_word() -> None:
    constraints = InterpretedOutputConstraints(
        scope="draft_body",
        word_count_mode="exact",
        word_count=5,
        include_source_urls=True,
    )

    validation = validate_output_constraints(
        "Read the source at https://example.test/path.",
        constraints,
    )

    assert validation.passed is True
    assert validation.word_count == 5


def test_word_count_range_enforces_both_bounds() -> None:
    constraints = InterpretedOutputConstraints(
        scope="draft_body",
        minimum_words=100,
        maximum_words=130,
    )

    below = validate_output_constraints(" ".join(["word"] * 94), constraints)
    passing = validate_output_constraints(" ".join(["word"] * 115), constraints)
    above = validate_output_constraints(" ".join(["word"] * 131), constraints)

    assert below.word_count == 94
    assert below.violations == ["word count 94 is below minimum 100"]
    assert passing.passed is True
    assert above.violations == ["word count 131 exceeds maximum 130"]


def test_objective_validator_covers_counts_sections_urls_and_style() -> None:
    constraints = InterpretedOutputConstraints(
        scope="entire_response",
        minimum_items=2,
        maximum_items=2,
        required_sections=["Evidence"],
        require_section_headings=True,
        forbidden_phrases=["guaranteed"],
        forbid_em_dash=True,
        include_source_urls=True,
    )

    validation = validate_output_constraints(
        "Evidence\n- Supported fact: https://example.test/one\n- Unknown remains.",
        constraints,
    )

    assert validation.passed is True
    assert validation.item_count == 2


def test_source_url_requirement_is_conditional_when_bounded_read_has_no_matches() -> None:
    constraints = InterpretedOutputConstraints(include_source_urls=True)

    validation = validate_output_constraints(
        "No matching saved announcement items were found.",
        constraints,
        source_urls_applicable=False,
    )

    assert validation.passed is True
    assert validation.violations == []
    assert validation.satisfied_constraints == [
        "visible source URL conditional on matched results; no matches"
    ]


def test_objective_validator_enforces_exact_unique_source_url_count() -> None:
    constraints = InterpretedOutputConstraints(
        scope="entire_response",
        source_url_count_mode="exact",
        source_url_count=2,
        include_source_urls=True,
    )

    passing = validate_output_constraints(
        "Sources: https://example.test/one | https://example.test/two "
        "| duplicate https://example.test/one",
        constraints,
    )
    failing = validate_output_constraints(
        "Source: https://example.test/one",
        constraints,
    )

    assert passing.passed is True
    assert passing.source_url_count == 2
    assert failing.passed is False
    assert failing.violations == ["source URL count 1 does not satisfy exact 2"]


@pytest.mark.parametrize(
    "duplicate_variant",
    [
        "https://example.test/a.",
        "https://example.test/a#section",
        "https://example.test/a/?utm_source=slack",
        "http://EXAMPLE.test:80/a/",
    ],
)
def test_source_url_count_canonicalizes_duplicate_presentation_variants(
    duplicate_variant: str,
) -> None:
    constraints = InterpretedOutputConstraints(
        source_url_count_mode="exact",
        source_url_count=2,
    )

    validation = validate_output_constraints(
        f"https://example.test/a {duplicate_variant}",
        constraints,
    )

    assert validation.passed is False
    assert validation.source_url_count == 1


@pytest.mark.parametrize(
    "malformed_url",
    [
        "https://example.test:abc/path",
        "https://example.test:99999/path",
        "https://[example.test/path",
    ],
)
def test_source_url_count_ignores_malformed_urls(malformed_url: str) -> None:
    constraints = InterpretedOutputConstraints(
        source_url_count_mode="exact",
        source_url_count=1,
    )

    validation = validate_output_constraints(malformed_url, constraints)

    assert validation.passed is False
    assert validation.source_url_count == 0


def test_instruction_following_v1_omits_unused_source_url_count() -> None:
    resolution = InstructionFollowingResolution(
        response_text="Abridge turns conversations into notes.",
        validation=validate_output_constraints(
            "Abridge turns conversations into notes.",
            InterpretedOutputConstraints(
                word_count_mode="exact",
                word_count=5,
            ),
        ),
    )

    assert "source_url_count" not in resolution.metadata()["validation"]


def test_raw_request_resolves_conditional_sentence_contract_from_agent_decision() -> None:
    request = (
        "Decide whether a reply is warranted. If yes, give exactly two sentences "
        "I can paste here. If no, explain why in one sentence."
    )

    positive = raw_request_output_contract(request, condition_outcome=True)
    negative = raw_request_output_contract(request, condition_outcome=False)
    unresolved = raw_request_output_contract(request)

    assert positive.constraints.sentence_count == 2
    assert positive.paste_ready_copy is True
    assert negative.constraints.sentence_count == 1
    assert negative.paste_ready_copy is False
    assert unresolved.constraints.sentence_count is None
    assert unresolved.exact_output_requested is True
    assert unresolved.branch_resolved is False


def test_raw_request_resolves_adjectival_conditional_sentence_contract() -> None:
    request = (
        "Decide whether I should answer. If so, write a two-sentence reply I can "
        "paste here; if not, explain why in one sentence."
    )

    positive = raw_request_output_contract(request, condition_outcome=True)
    negative = raw_request_output_contract(request, condition_outcome=False)

    assert positive.constraints.sentence_count_mode == "exact"
    assert positive.constraints.sentence_count == 2
    assert positive.paste_ready_copy is True
    assert negative.constraints.sentence_count_mode == "exact"
    assert negative.constraints.sentence_count == 1


def test_adjectival_sentence_description_is_not_an_output_contract() -> None:
    contract = raw_request_output_contract(
        "The email contains a two-sentence reply. Summarize its conclusion."
    )

    assert contract.exact_output_requested is False
    assert contract.constraints.sentence_count is None


def test_raw_request_sentence_count_is_validated_when_planner_omits_it() -> None:
    request = "Give exactly two sentences I can paste into the project channel."

    passing = resolve_instruction_following_response(
        "The implementation now preserves raw constraints. The next run should verify it.",
        original_request=request,
        manual_plan=None,
        live=False,
    )
    failing = resolve_instruction_following_response(
        "The implementation now preserves raw constraints.",
        original_request=request,
        manual_plan=None,
        live=False,
    )

    assert passing.validation.applicable is True
    assert passing.validation.passed is True
    assert passing.validation.sentence_count == 2
    assert failing.validation.applicable is True
    assert failing.validation.passed is False
    assert failing.validation.violations == ["sentence count 1 does not satisfy exact 2"]
    assert (
        exact_output_validation_required(
            None,
            original_request=request,
        )
        is True
    )


def test_conditional_negative_branch_validates_one_sentence_explanation() -> None:
    request = (
        "Decide whether a response is needed. If yes, return exactly two sentences "
        "I can copy. If no, explain why in one sentence."
    )

    resolution = resolve_instruction_following_response(
        "No reply is needed because the sender did not request action.",
        original_request=request,
        manual_plan=None,
        live=False,
        condition_outcome=False,
    )

    assert resolution.validation.applicable is True
    assert resolution.validation.passed is True
    assert resolution.validation.sentence_count == 1


@pytest.mark.parametrize(
    ("request_text", "condition_outcome", "response", "expected_words"),
    [
        (
            "If a reply is needed, write exactly ten words. Otherwise, summarize "
            "in exactly five words.",
            True,
            "Thanks for sharing this update; I will reply with details.",
            10,
        ),
        (
            "If a reply is needed, write exactly ten words. Otherwise, summarize "
            "in exactly five words.",
            False,
            "Thanks for sharing this update.",
            5,
        ),
        (
            "Otherwise, summarize in exactly five words. If a reply is needed, "
            "write exactly ten words.",
            False,
            "Thanks for sharing this update.",
            5,
        ),
        (
            "Otherwise, summarize in exactly five words. If a reply is needed, "
            "write exactly ten words.",
            True,
            "Thanks for sharing this update; I will reply with details.",
            10,
        ),
    ],
)
def test_selected_conditional_branch_owns_recovered_word_count(
    request_text: str,
    condition_outcome: bool,
    response: str,
    expected_words: int,
) -> None:
    resolution = resolve_instruction_following_response(
        response,
        original_request=request_text,
        manual_plan=_plan(
            InterpretedOutputConstraints(
                scope="answer",
                word_count_mode="exact",
                word_count=5 if condition_outcome is False else 10,
            )
        ),
        live=False,
        condition_outcome=condition_outcome,
    )

    assert resolution.validation.passed is True
    assert resolution.validation.word_count == expected_words


def test_unresolved_conditional_count_does_not_replace_trustworthy_plan_binding() -> None:
    request = (
        "If a reply is needed, write exactly ten words. Otherwise, summarize in exactly five words."
    )

    constraints = resolved_output_constraints(
        _plan(
            InterpretedOutputConstraints(
                scope="answer",
                word_count_mode="exact",
                word_count=5,
            )
        ),
        original_request=request,
    )
    validation = validate_output_constraints(
        "Thanks for sharing this update.",
        constraints,
    )

    assert constraints.word_count == 5
    assert validation.passed is True


@pytest.mark.parametrize(
    ("condition_outcome", "response", "expected_items"),
    [
        (True, "- First\n- Second\n- Third", 3),
        (False, "- First\n- Second", 2),
    ],
)
def test_selected_conditional_branch_owns_recovered_item_count(
    condition_outcome: bool,
    response: str,
    expected_items: int,
) -> None:
    request = (
        "If a reply is needed, return exactly three bullets. Otherwise, return exactly two bullets."
    )

    resolution = resolve_instruction_following_response(
        response,
        original_request=request,
        manual_plan=None,
        live=False,
        condition_outcome=condition_outcome,
    )

    assert resolution.validation.passed is True
    assert resolution.validation.item_count == expected_items


def test_raw_counts_preserve_independent_semantic_scope_bindings() -> None:
    request = (
        "Return an Answer of exactly four words and a Detailed Summary with "
        "exactly two bullets. Include exactly one source URL outside the Answer."
    )
    plan = _plan(
        InterpretedOutputConstraints(
            scope="answer",
            word_scope="answer",
            item_scope="entire_response",
            source_scope="entire_response",
            word_count_mode="exact",
            word_count=4,
            item_count_mode="exact",
            minimum_items=2,
            maximum_items=2,
            source_url_count_mode="exact",
            source_url_count=1,
            include_source_urls=True,
        )
    )
    constraints = resolved_output_constraints(
        plan,
        original_request=request,
    )
    response = (
        "Answer:\nAlpha beta gamma delta.\n\n"
        "Detailed Summary:\n- First detail.\n- Second detail.\n\n"
        "Sources:\nhttps://example.test/evidence"
    )

    validation = validate_output_constraints(response, constraints)

    assert constraints.scope == "answer"
    assert constraints.word_scope == "answer"
    assert constraints.item_scope == "entire_response"
    assert constraints.source_scope == "entire_response"
    assert validation.passed is True
    assert validation.word_count == 4
    assert validation.item_count == 2
    assert validation.source_url_count == 1

    item_violation = validate_output_constraints(
        response.replace("- Second detail.", "- Second detail.\n- Third detail."),
        constraints,
    )
    assert item_violation.passed is False
    assert "item count 3 exceeds maximum 2" in item_violation.violations


def test_raw_sentence_contract_ignores_unrelated_conditions_and_source_counts() -> None:
    unrelated_condition = raw_request_output_contract(
        "If sources are available, cite them. Return exactly two sentences."
    )
    source_description = raw_request_output_contract(
        "The source memo contains exactly two sentences. Summarize its conclusion."
    )

    assert unrelated_condition.conditional is False
    assert unrelated_condition.constraints.sentence_count == 2
    assert source_description.exact_output_requested is False
    assert source_description.constraints.sentence_count is None


def test_required_heading_accepts_markdown_or_inline_content() -> None:
    constraints = InterpretedOutputConstraints(
        required_sections=["Evidence"],
        require_section_headings=True,
    )

    assert validate_output_constraints("## Evidence\nSupported fact.", constraints).passed is True
    assert validate_output_constraints("**Evidence:** Supported fact.", constraints).passed is True


def test_required_heading_does_not_accept_an_unlabeled_prose_prefix() -> None:
    constraints = InterpretedOutputConstraints(
        required_sections=["Evidence"],
        require_section_headings=True,
    )

    validation = validate_output_constraints(
        "Evidence shows that the result is supported.", constraints
    )

    assert validation.passed is False
    assert validation.violations == ["missing required section: Evidence"]


@pytest.mark.parametrize("bullet", ["+", "•"])
def test_item_count_accepts_ordinary_equivalent_bullet_markers(bullet: str) -> None:
    constraints = InterpretedOutputConstraints(
        item_count_mode="exact",
        minimum_items=2,
        maximum_items=2,
    )

    validation = validate_output_constraints(
        f"{bullet} First supported item.\n{bullet} Second supported item.",
        constraints,
    )

    assert validation.passed is True
    assert validation.item_count == 2


def test_answer_scope_accepts_markdown_heading_and_excludes_source_section() -> None:
    constraints = InterpretedOutputConstraints(
        scope="answer",
        word_count_mode="exact",
        word_count=5,
        include_source_urls=True,
    )

    validation = validate_output_constraints(
        "## Answer\nAbridge turns conversations into notes.\n\n"
        "## Useful reference\nhttps://example.test/source",
        constraints,
    )

    assert validation.passed is True
    assert validation.word_count == 5


def test_draft_scope_accepts_markdown_heading_and_excludes_limitation_section() -> None:
    constraints = InterpretedOutputConstraints(
        scope="draft_body",
        word_count_mode="exact",
        word_count=6,
    )

    validation = validate_output_constraints(
        "## Email draft\nThanks for the thoughtful follow-up today.\n\n"
        "## Contact limitation\nNo recipient is approved.",
        constraints,
    )

    assert validation.passed is True
    assert validation.word_count == 6


def test_inline_citation_only_suffix_is_outside_answer_word_count() -> None:
    constraints = InterpretedOutputConstraints(
        scope="answer",
        word_count_mode="exact",
        word_count=5,
        include_source_urls=True,
    )

    validation = validate_output_constraints(
        "Abridge turns conversations into notes. Source: https://example.test/source",
        constraints,
    )

    assert validation.passed is True
    assert validation.word_count == 5


def test_inline_source_label_does_not_hide_substantive_words() -> None:
    constraints = InterpretedOutputConstraints(
        scope="answer",
        word_count_mode="exact",
        word_count=5,
        include_source_urls=True,
    )

    validation = validate_output_constraints(
        "Abridge turns conversations into notes. Source: This remains uncertain. "
        "https://example.test/source",
        constraints,
    )

    assert validation.passed is False
    assert validation.word_count > 5


@pytest.mark.parametrize(
    "response",
    [
        "Dr. Rivera confirmed the result. The report is ready.",
        "The U.S. market result is supported. The report is ready.",
        "1. The first result is supported.\n2. The second result is supported.",
    ],
)
def test_sentence_count_accepts_ordinary_equivalent_prose_layouts(response: str) -> None:
    constraints = InterpretedOutputConstraints(
        sentence_count_mode="exact",
        sentence_count=2,
    )

    validation = validate_output_constraints(response, constraints)

    assert validation.passed is True
    assert validation.sentence_count == 2


def test_suffix_title_period_remains_a_real_sentence_boundary() -> None:
    response = "The contact is Jordan Lee Jr. Please wait."

    exact_two = validate_output_constraints(
        response,
        InterpretedOutputConstraints(
            sentence_count_mode="exact",
            sentence_count=2,
        ),
    )
    exact_one = validate_output_constraints(
        response,
        InterpretedOutputConstraints(
            sentence_count_mode="exact",
            sentence_count=1,
        ),
    )

    assert exact_two.passed is True
    assert exact_two.sentence_count == 2
    assert exact_one.passed is False
    assert exact_one.sentence_count == 2


@pytest.mark.parametrize("title", ["Dr.", "Prof."])
def test_prefix_title_before_name_does_not_add_sentence(title: str) -> None:
    response = f"{title} Rivera confirmed the result. Please wait."
    validation = validate_output_constraints(
        response,
        InterpretedOutputConstraints(
            sentence_count_mode="exact",
            sentence_count=2,
        ),
    )

    assert validation.passed is True
    assert validation.sentence_count == 2


def test_suffix_title_within_sentence_does_not_add_sentence() -> None:
    response = "Jordan Lee Jr. confirmed the result. Please wait."
    validation = validate_output_constraints(
        response,
        InterpretedOutputConstraints(
            sentence_count_mode="exact",
            sentence_count=2,
        ),
    )

    assert validation.passed is True
    assert validation.sentence_count == 2


@pytest.mark.parametrize(
    "response",
    [
        ("__Answer__\nAlpha beta gamma delta.\n\n__Sources__\nhttps://example.test/source"),
        ("> Answer\nAlpha beta gamma delta.\n\n> Sources\nhttps://example.test/source"),
        ("__Answer:__ Alpha beta gamma delta.\n\n__Sources:__ https://example.test/source"),
    ],
)
def test_answer_scope_uses_the_same_supported_section_grammar(response: str) -> None:
    validation = validate_output_constraints(
        response,
        InterpretedOutputConstraints(
            scope="answer",
            word_count_mode="exact",
            word_count=4,
            include_source_urls=True,
        ),
    )

    assert validation.passed is True
    assert validation.word_count == 4


def test_draft_scope_uses_blockquote_section_boundaries() -> None:
    validation = validate_output_constraints(
        "> Email draft\nThanks for the update today.\n\n"
        "> Contact limitation\nNo recipient is approved.",
        InterpretedOutputConstraints(
            scope="draft_body",
            word_count_mode="exact",
            word_count=5,
        ),
    )

    assert validation.passed is True
    assert validation.word_count == 5


def test_entire_response_scope_does_not_drop_supported_sections() -> None:
    response = "> Answer\nAlpha beta gamma delta.\n\n> Sources\nhttps://example.test/source"
    validation = validate_output_constraints(
        response,
        InterpretedOutputConstraints(
            scope="entire_response",
            word_count_mode="exact",
            word_count=7,
            include_source_urls=True,
        ),
    )

    assert validation.passed is True
    assert validation.word_count == 7


def test_source_like_prose_is_not_treated_as_a_section_boundary() -> None:
    validation = validate_output_constraints(
        "__Answer__\nAlpha beta gamma delta.\nSources clarify the result today.",
        InterpretedOutputConstraints(
            scope="answer",
            word_count_mode="exact",
            word_count=4,
        ),
    )

    assert validation.passed is False
    assert validation.word_count == 9


def test_source_url_count_deduplicates_slack_and_bare_link_forms() -> None:
    constraints = InterpretedOutputConstraints(
        source_url_count_mode="exact",
        source_url_count=1,
        include_source_urls=True,
    )

    validation = validate_output_constraints(
        "<https://example.test/source|Source> https://EXAMPLE.test/source/",
        constraints,
    )

    assert validation.passed is True
    assert validation.source_url_count == 1


def test_public_source_url_helper_normalizes_bare_markdown_slack_and_punctuation() -> None:
    from keystone_agents.instruction_following import canonical_source_urls

    assert canonical_source_urls(
        " ".join(
            (
                "https://EXAMPLE.test/source/.",
                "[source](https://example.test/source)",
                "<https://example.test/source|labeled source>",
                "(https://example.test/source),",
            )
        )
    ) == ("https://example.test/source",)


def test_source_url_count_preserves_distinct_gmail_fragment_identities() -> None:
    constraints = InterpretedOutputConstraints(
        source_url_count_mode="exact",
        source_url_count=2,
    )

    distinct = validate_output_constraints(
        "https://mail.google.com/mail/#all/synthetic-a "
        "https://mail.google.com/mail/u/0/#inbox/synthetic-b",
        constraints,
    )
    duplicate = validate_output_constraints(
        "https://mail.google.com/mail/#all/synthetic-a "
        "https://mail.google.com/mail/#inbox/synthetic-a",
        constraints,
    )

    assert distinct.passed is True
    assert distinct.source_url_count == 2
    assert duplicate.passed is False
    assert duplicate.source_url_count == 1


def test_visible_source_requirement_rejects_malformed_url() -> None:
    constraints = InterpretedOutputConstraints(include_source_urls=True)

    validation = validate_output_constraints(
        "Source: https://example.test:abc/source",
        constraints,
    )

    assert validation.passed is False
    assert validation.violations == ["visible source URL missing"]


def test_advisory_content_components_do_not_become_literal_heading_blockers() -> None:
    constraints = InterpretedOutputConstraints(
        interpretation="Cover supported facts, the validation gap, and an internal note.",
        required_sections=[
            "Supported claims",
            "Validation gap",
            "Internal Slack recommendation",
        ],
        require_section_headings=False,
    )

    validation = validate_output_constraints(
        "Northstar sells referral-navigation software. Its audited outcomes remain "
        "unverified, so request source-backed outcome evidence before proceeding.",
        constraints,
    )

    assert validation.applicable is False
    assert validation.passed is True


def test_draft_body_scope_measures_draft_without_wrapper_metadata() -> None:
    constraints = InterpretedOutputConstraints(
        scope="draft_body",
        word_count_mode="maximum",
        word_count=6,
    )

    validation = validate_output_constraints(
        "*Email draft:*\nThanks for the thoughtful follow-up today.",
        constraints,
    )

    assert validation.passed is True
    assert validation.word_count == 6


def test_draft_body_scope_excludes_separate_contact_gap() -> None:
    constraints = InterpretedOutputConstraints(
        scope="draft_body",
        minimum_words=85,
        maximum_words=105,
    )
    short_introduction = " ".join(["word"] * 77)

    validation = validate_output_constraints(
        short_introduction + "\n\nContact gap: No individual contact or email is approved.",
        constraints,
    )

    assert validation.passed is False
    assert validation.word_count == 77
    assert validation.violations == ["word count 77 is below minimum 85"]


def test_labeled_introduction_scope_stops_before_contact_limitation() -> None:
    constraints = InterpretedOutputConstraints(
        scope="draft_body",
        word_count_mode="exact",
        word_count=5,
    )

    validation = validate_output_constraints(
        "Introduction: Cedar Grove supports community clinics.\n\n"
        "Contact limitation: No recipient is approved.",
        constraints,
    )

    assert validation.passed is True
    assert validation.word_count == 5


def test_requested_contact_gap_is_required_even_when_word_count_passes() -> None:
    constraints = InterpretedOutputConstraints(
        scope="draft_body",
        minimum_words=85,
        maximum_words=105,
        required_sections=["Contact gap"],
        require_section_headings=True,
    )

    validation = validate_output_constraints(" ".join(["word"] * 90), constraints)

    assert validation.passed is False
    assert validation.word_count == 90
    assert validation.violations == ["missing required section: Contact gap"]


def test_repair_prompt_targets_middle_of_word_range_for_draft_body() -> None:
    repair_input = InstructionFollowingRepairInput(
        original_request="Write a 90-100 word introduction, then add Contact gap.",
        interpreted_constraints=InterpretedOutputConstraints(
            scope="draft_body",
            minimum_words=90,
            maximum_words=100,
            required_sections=["Contact gap"],
            require_section_headings=True,
        ),
        candidate_response="Short introduction.\n\nContact gap: None approved.",
        bounded_evidence="Approved supplied facts only.",
    )

    prompt = repair_input.to_prompt()

    assert "aim for 95 words" in prompt
    assert "inside the allowed 90-100 range" in prompt
    assert "Count only the draft_body text" in prompt


def test_failed_constraint_gets_one_llm_repair(monkeypatch) -> None:
    constraints = InterpretedOutputConstraints(
        interpretation="exact 5-word answer",
        scope="answer",
        word_count_mode="exact",
        word_count=5,
    )
    calls: list[object] = []

    def fake_run_typed_sdk_agent(**kwargs):
        calls.append(kwargs["typed_input"])
        return SimpleNamespace(
            output=InstructionFollowingRepairOutput(
                response_text="*Answer:*\nAbridge turns conversations into notes."
            ),
            usage={"available": True, "total_tokens": 50},
            cost={"available": True, "total_cost_usd": 0.001},
            request_cache={},
            execution_telemetry={
                "schema": "keystone.execution_telemetry.v1",
                "telemetry_id": "private-telemetry-id",
                "run_id": "private-run-id",
                "started_at_utc": "2026-07-27T12:00:00Z",
                "status": "completed",
                "total_duration_ms": 125.0,
                "first_feedback_ms": 120.0,
                "final_response_ms": 120.0,
                "spans": [
                    {
                        "schema": "keystone.execution_stage_span.v1",
                        "span_id": "private-span-id",
                        "telemetry_id": "private-telemetry-id",
                        "run_id": "private-run-id",
                        "parent_span_id": "",
                        "sequence": 1,
                        "turn_index": 1,
                        "attempt_index": 1,
                        "stage": "sdk.model_attempt",
                        "status": "ok",
                        "started_offset_ms": 0.0,
                        "finished_offset_ms": 120.0,
                        "duration_ms": 120.0,
                        "error_kind": "",
                        "attributes": [],
                    }
                ],
            },
        )

    monkeypatch.setattr(
        "keystone_agents.instruction_following.run_typed_sdk_agent",
        fake_run_typed_sdk_agent,
    )

    resolution = resolve_instruction_following_response(
        "Abridge is a healthcare company that makes ambient AI documentation tools.",
        original_request="Summarize Abridge in 5 words.",
        manual_plan=_plan(constraints),
        live=True,
    )

    assert len(calls) == 1
    assert resolution.repair_attempted is True
    assert resolution.repair_succeeded is True
    assert resolution.validation.passed is True
    assert resolution.response_text.endswith("Abridge turns conversations into notes.")
    repair_timing = resolution.metadata()["repair_execution_telemetry"]
    assert repair_timing["total_duration_ms"] == 125.0
    assert "spans" not in repair_timing
    assert "private" not in str(repair_timing)


def test_compliant_response_skips_repair(monkeypatch) -> None:
    constraints = InterpretedOutputConstraints(
        scope="answer",
        word_count_mode="exact",
        word_count=5,
    )

    def unexpected_call(**_kwargs):
        raise AssertionError("repair should not run")

    monkeypatch.setattr(
        "keystone_agents.instruction_following.run_typed_sdk_agent",
        unexpected_call,
    )

    resolution = resolve_instruction_following_response(
        "Abridge turns conversations into notes.",
        original_request="Summarize Abridge in 5 words.",
        manual_plan=_plan(constraints),
        live=True,
    )

    assert resolution.validation.passed is True
    assert resolution.repair_attempted is False


@pytest.mark.parametrize(
    "request_text",
    [
        "Summarize the supplied findings in exactly two bullet points.",
        "Give me two short bullets summarizing the supplied findings.",
        "Respond with exactly two items based on the supplied findings.",
    ],
)
def test_raw_exact_item_contract_recovers_missing_planner_bounds(
    request_text: str,
) -> None:
    incomplete_plan = {
        "ask_shape": {
            "output_constraints": {
                "scope": "entire_response",
                "item_count_mode": "exact",
            }
        }
    }

    passing = resolve_instruction_following_response(
        "- Alpha is supported.\n- Beta is supported.",
        original_request=request_text,
        manual_plan=incomplete_plan,
        live=False,
    )
    failing = resolve_instruction_following_response(
        "- Alpha is supported.\n- Beta is supported.\n- Gamma is extra.",
        original_request=request_text,
        manual_plan=incomplete_plan,
        live=False,
    )

    assert passing.validation.passed is True
    assert passing.validation.item_count == 2
    assert passing.repair_attempted is False
    assert failing.validation.passed is False
    assert failing.validation.item_count == 3
    assert "item count 3 exceeds maximum 2" in failing.validation.violations
    assert (
        "item_count_metadata_recovered_from_raw_request"
        in (failing.metadata()["constraint_admission_warnings"])
    )


def test_malformed_word_metadata_cannot_erase_valid_item_and_source_constraints() -> None:
    partially_invalid_plan = {
        "ask_shape": {
            "output_constraints": {
                "scope": "entire_response",
                "word_count_mode": "exact",
                "item_count_mode": "exact",
                "minimum_items": 2,
                "maximum_items": 2,
                "include_source_urls": True,
            }
        }
    }

    passing = resolve_instruction_following_response(
        "- Alpha is supported.\n- Beta is supported.\nSource: https://example.test/report",
        original_request=(
            "Summarize the supplied findings in exactly two bullet points and "
            "include the source link."
        ),
        manual_plan=partially_invalid_plan,
        live=False,
    )
    failing = resolve_instruction_following_response(
        "- Alpha is supported.\n- Beta is supported.\n- Gamma is extra.",
        original_request=(
            "Summarize the supplied findings in exactly two bullet points and "
            "include the source link."
        ),
        manual_plan=partially_invalid_plan,
        live=False,
    )

    assert passing.validation.passed is True
    assert passing.validation.item_count == 2
    assert passing.repair_attempted is False
    assert failing.validation.passed is False
    assert failing.validation.item_count == 3
    assert "item count 3 exceeds maximum 2" in failing.validation.violations
    assert "visible source URL missing" in failing.validation.violations
    assert (
        "nonbinding_planner_word_count_metadata_ignored"
        in (failing.metadata()["constraint_admission_warnings"])
    )


def test_one_sided_exact_item_bound_is_normalized_without_raw_count() -> None:
    one_sided_plan = {
        "ask_shape": {
            "output_constraints": {
                "scope": "entire_response",
                "item_count_mode": "exact",
                "maximum_items": 2,
            }
        }
    }

    resolution = resolve_instruction_following_response(
        "- Alpha is supported.\n- Beta is supported.\n- Gamma is extra.",
        original_request="Summarize the supplied findings.",
        manual_plan=one_sided_plan,
        live=False,
    )

    assert resolution.validation.passed is False
    assert resolution.validation.item_count == 3
    assert resolution.validation.violations == ["item count 3 exceeds maximum 2"]
    assert (
        "item_count_exact_normalized_from_maximum"
        in (resolution.metadata()["constraint_admission_warnings"])
    )


@pytest.mark.parametrize(
    "item_metadata",
    [
        {
            "item_count_mode": "exact",
            "minimum_items": "unknown",
            "maximum_items": 2,
        },
        {
            "item_count_mode": "exact",
            "minimum_items": 2,
            "maximum_items": 3,
        },
    ],
)
def test_raw_exact_items_override_unknown_or_inconsistent_planner_numbers(
    item_metadata,
) -> None:
    resolution = resolve_instruction_following_response(
        "- Alpha is supported.\n- Beta is supported.\n- Gamma is extra.",
        original_request="Return exactly two bullets from the supplied findings.",
        manual_plan={
            "ask_shape": {
                "output_constraints": {
                    "scope": "entire_response",
                    **item_metadata,
                }
            }
        },
        live=False,
    )

    assert resolution.validation.passed is False
    assert resolution.validation.item_count == 3
    assert resolution.validation.violations == ["item count 3 exceeds maximum 2"]
    assert (
        "item_count_metadata_recovered_from_raw_request"
        in (resolution.metadata()["constraint_admission_warnings"])
    )


def test_unrecoverable_planner_item_count_is_diagnostic_not_fatal() -> None:
    incomplete_plan = {
        "ask_shape": {
            "output_constraints": {
                "interpretation": "Keep the answer concise.",
                "item_count_mode": "exact",
                "style_requirements": ["concise"],
            }
        }
    }

    resolution = resolve_instruction_following_response(
        "Alpha is supported.",
        original_request="Summarize the supplied finding.",
        manual_plan=incomplete_plan,
        live=False,
    )

    assert resolution.validation.applicable is False
    assert resolution.validation.passed is True
    assert resolution.repair_attempted is False
    assert resolution.metadata()["constraint_admission_warnings"] == [
        "unresolved_item_count_exact_missing_bounds"
    ]


@pytest.mark.parametrize(
    ("raw_constraints", "expected_mode", "expected_minimum", "expected_maximum"),
    [
        (
            {
                "item_count_mode": "exact",
                "minimum_items": 2,
                "maximum_items": 2,
            },
            "exact",
            2,
            2,
        ),
        (
            {"item_count_mode": "exact", "maximum_items": 2},
            "exact",
            2,
            2,
        ),
        (
            {"item_count_mode": "maximum", "maximum_items": 3},
            "maximum",
            None,
            3,
        ),
        (
            {"item_count_mode": "minimum", "minimum_items": 1},
            "minimum",
            1,
            None,
        ),
        (
            {"item_count_mode": "under", "maximum_items": 3},
            "under",
            None,
            3,
        ),
    ],
)
def test_valid_legacy_item_modes_survive_constraint_admission(
    raw_constraints,
    expected_mode,
    expected_minimum,
    expected_maximum,
) -> None:
    constraints = output_constraints_from_plan(
        {"ask_shape": {"output_constraints": raw_constraints}}
    )

    assert constraints.item_count_mode == expected_mode
    assert constraints.minimum_items == expected_minimum
    assert constraints.maximum_items == expected_maximum


def test_strict_evidence_response_does_not_expand_to_meet_minimum(monkeypatch) -> None:
    constraints = InterpretedOutputConstraints(
        scope="draft_body",
        minimum_words=94,
        maximum_words=104,
        required_sections=["Contact gap"],
        require_section_headings=True,
    )

    def unexpected_call(**_kwargs):
        raise AssertionError("strict evidence must not be padded by a repair call")

    monkeypatch.setattr(
        "keystone_agents.instruction_following.run_typed_sdk_agent",
        unexpected_call,
    )

    response = " ".join(["grounded"] * 80) + "\n\nContact gap: No contact or email is approved."
    resolution = resolve_instruction_following_response(
        response,
        original_request="Use only these facts for a 94-104 word introduction.",
        manual_plan=_plan(constraints),
        bounded_evidence="Approved supplied facts only.",
        live=True,
        allow_length_expansion_repair=False,
    )

    assert resolution.validation.passed is False
    assert resolution.validation.word_count == 80
    assert resolution.repair_attempted is False
    assert resolution.repair_skipped_reason == ("strict_evidence_length_expansion_disabled")
    assert resolution.metadata()["repair_skipped_reason"] == (
        "strict_evidence_length_expansion_disabled"
    )


def test_advisory_style_interpretation_is_not_a_deterministic_gate() -> None:
    constraints = InterpretedOutputConstraints(
        interpretation=(
            "Brief Slack-ready response with a short note on what warrants a reply "
            "and one concise draft reply."
        ),
        scope="entire_response",
        style_requirements=["keep it concise", "draft only", "Slack readable"],
    )

    validation = validate_output_constraints(
        "A natural model-produced assessment and draft.",
        constraints,
    )

    assert constraints.is_explicit() is True
    assert constraints.has_deterministic_requirements() is False
    assert validation.applicable is False
    assert validation.passed is True


def test_specialist_constraint_handoff_names_llm_interpretation() -> None:
    plan = _plan(
        InterpretedOutputConstraints(
            interpretation="exact 20-word answer",
            scope="answer",
            word_count_mode="exact",
            word_count=20,
        )
    )

    text = interpreted_output_constraints_text(plan)

    assert "raw operator request remains authoritative" in text
    assert '"word_count": 20' in text
    assert "deterministic helpers will only validate" in text
