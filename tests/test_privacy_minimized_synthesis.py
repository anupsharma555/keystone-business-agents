from __future__ import annotations

import pytest

from keystone_agents.privacy_minimized_synthesis import (
    PrivacyMinimizedAssertion,
    PrivacyMinimizedFact,
    PrivacyMinimizedSynthesisPacket,
    build_concept_signal_packet,
    packet_for_model,
    validate_privacy_minimized_packet,
)


def test_builder_emits_only_allowlisted_concepts_and_hashes() -> None:
    raw = (
        "Private proposal for Person Name at person@example.com under "
        "/Users/example/private.docx covers clinical AI evaluation and data strategy."
    )
    packet = build_concept_signal_packet(
        workflow="capability_summary",
        sources={"private/provider/path": raw},
        taxonomy={
            "clinical_ai_evaluation": ("clinical ai evaluation",),
            "data_strategy": ("data strategy",),
        },
        constraints=("human_review_required", "no_external_action"),
    )

    outbound = packet_for_model(packet)
    rendered = str(outbound)
    assert {fact["concept"] for fact in outbound["facts"]} == {
        "clinical_ai_evaluation",
        "data_strategy",
    }
    assert "Person Name" not in rendered
    assert "person@example.com" not in rendered
    assert "/Users/example" not in rendered
    assert "private/provider/path" not in rendered
    assert packet.proof_scope == "sanitized_context_proof"
    assert outbound["source_ids"] == [
        f"sanitized-source:{item}" for item in outbound["source_hashes"]
    ]


@pytest.mark.parametrize(
    "unsafe_value",
    (
        "person@example.com",
        "https://private.example/path",
        "/Users/example/private.docx",
        "$42.00",
        "thread_private123",
        "[redacted person]",
    ),
)
def test_validator_rejects_non_minimized_values(unsafe_value: str) -> None:
    packet = PrivacyMinimizedSynthesisPacket(
        workflow="weekly_packet",
        source_count=1,
        source_hashes=("a" * 16,),
        facts=(
            PrivacyMinimizedFact(
                concept="operational_follow_up",
                source_hashes=("a" * 16,),
            ),
        ),
        constraints=("no_external_action",),
    ).model_dump(mode="json", by_alias=True)
    packet["facts"][0]["concept"] = unsafe_value

    with pytest.raises(ValueError):
        validate_privacy_minimized_packet(packet)


def test_validator_rejects_exact_raw_value_even_when_pattern_safe() -> None:
    packet = PrivacyMinimizedSynthesisPacket(
        workflow="weekly_packet",
        source_count=1,
        source_hashes=("b" * 16,),
        facts=(
            PrivacyMinimizedFact(
                concept="private_project_alpha",
                source_hashes=("b" * 16,),
            ),
        ),
        constraints=("no_external_action",),
    )

    with pytest.raises(ValueError, match="forbidden raw value"):
        validate_privacy_minimized_packet(
            packet,
            forbidden_raw_values=("private_project_alpha",),
        )


def test_builder_fails_when_no_allowlisted_concept_is_present() -> None:
    with pytest.raises(ValueError, match="No allowlisted semantic concepts"):
        build_concept_signal_packet(
            workflow="research_brief",
            sources={"source": "Unclassified private material."},
            taxonomy={"evaluation_design": ("evaluation design",)},
            constraints=("human_review_required",),
        )


def test_assertion_layer_accepts_only_bounded_tokens_and_hashes() -> None:
    packet = PrivacyMinimizedSynthesisPacket(
        workflow="research_brief",
        source_count=1,
        source_hashes=("c" * 16,),
        facts=(
            PrivacyMinimizedFact(
                concept="research_operations",
                source_hashes=("c" * 16,),
            ),
        ),
        assertions=(
            PrivacyMinimizedAssertion(
                subject="source_material",
                predicate="contains",
                object="research_operations",
                source_hashes=("c" * 16,),
            ),
        ),
        constraints=("no_external_action",),
    )

    outbound = packet_for_model(packet)

    assert outbound["assertions"] == [
        {
            "subject": "source_material",
            "predicate": "contains",
            "object": "research_operations",
            "count": 1,
            "source_hashes": ["c" * 16],
        }
    ]
