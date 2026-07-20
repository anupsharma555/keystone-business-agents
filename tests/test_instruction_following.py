from types import SimpleNamespace

from keystone_agents.instruction_following import (
    InstructionFollowingRepairOutput,
    interpreted_output_constraints_text,
    resolve_instruction_following_response,
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


def test_required_heading_accepts_markdown_or_inline_content() -> None:
    constraints = InterpretedOutputConstraints(
        required_sections=["Evidence"],
        require_section_headings=True,
    )

    assert validate_output_constraints(
        "## Evidence\nSupported fact.", constraints
    ).passed is True
    assert validate_output_constraints(
        "**Evidence:** Supported fact.", constraints
    ).passed is True


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
