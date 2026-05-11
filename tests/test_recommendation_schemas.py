from __future__ import annotations

import pytest
from pydantic import ValidationError

from keystone_agents.schemas.recommendation import (
    OpportunityContactPath,
    OpportunityEntity,
    RecommendationIntakeResult,
    RecommendationScore,
)


@pytest.mark.parametrize(
    "entity_type",
    [
        "company",
        "person",
        "institute",
        "lab",
        "conference",
        "grant",
        "accelerator",
        "rfp",
        "funder",
        "publication_group",
        "other",
    ],
)
def test_supported_entity_types(entity_type: str) -> None:
    entity = OpportunityEntity(entity_type=entity_type, name=f"Example {entity_type}")

    assert entity.entity_type == entity_type


def test_opportunity_entity_supports_contact_paths_and_source_links() -> None:
    entity = OpportunityEntity(
        entity_type="grant",
        name="NIMH Translational Tools FOA",
        url="example.nih.gov/grants/notice",
        source_urls=[
            "https://example.nih.gov/grants/notice",
            " https://example.nih.gov/grants/notice ",
        ],
        contact_paths=[
            OpportunityContactPath(
                path_type="website",
                label="Apply through grants portal",
                url="example.nih.gov/apply",
                confidence=0.8,
            )
        ],
    )

    assert entity.url == "https://example.nih.gov/grants/notice"
    assert entity.source_urls == ["https://example.nih.gov/grants/notice"]
    assert entity.contact_paths[0].url == "https://example.nih.gov/apply"


def test_recommendation_intake_result_keeps_no_send_defaults() -> None:
    result = RecommendationIntakeResult(
        input_text="Find public collaboration opportunities.",
        entity=OpportunityEntity(entity_type="conference", name="Clinical AI Forum"),
        score=RecommendationScore(fit_score=80, evidence_score=60),
        source_links=["example.org/search"],
    )

    assert result.send_enabled is False
    assert result.sent is False
    assert result.approval_required is True
    assert result.source_links == ["https://example.org/search"]


def test_recommendation_schemas_reject_send_side_effect_flags() -> None:
    with pytest.raises(ValidationError, match="send"):
        RecommendationIntakeResult(
            input_text="Find public collaboration opportunities.",
            entity=OpportunityEntity(entity_type="conference", name="Clinical AI Forum"),
            send_enabled=True,
        )


def test_score_fields_are_bounded() -> None:
    score = RecommendationScore(priority_score=100, fit_score=0)

    assert score.priority_score == 100

    with pytest.raises(ValidationError):
        RecommendationScore(priority_score=101)
