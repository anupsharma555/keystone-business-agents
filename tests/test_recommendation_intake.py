from __future__ import annotations

import json

from keystone_agents.agents.orchestrator import route_request
from keystone_agents.recommendations import (
    qualify_recommendation,
    recommendation_intake_markdown,
    save_recommendation_intake_result,
)
from keystone_agents.schemas.recommendation import (
    OpportunityContactPath,
    OpportunityEntity,
    RecommendationIntakeResult,
    RecommendationScore,
)
from keystone_agents.storage.sqlite_store import SQLiteStore


def test_recommendation_schema_blocks_send_flags() -> None:
    entity = OpportunityEntity(
        entity_type="conference",
        name="Clinical AI Summit",
        url="clinicalai.example",
        contact_paths=[
            OpportunityContactPath(
                path_type="website",
                url="clinicalai.example/contact",
                confidence=0.8,
            )
        ],
    )
    result = RecommendationIntakeResult(
        input_text="Consider Clinical AI Summit for evidence generation outreach.",
        entity=entity,
        score=RecommendationScore(fit_score=80, timing_score=70, evidence_score=60),
    )

    assert entity.url == "https://clinicalai.example"
    assert result.send_enabled is False
    assert result.sent is False
    assert result.score.priority_score > 0


def test_qualifies_non_company_recommendation_for_more_research() -> None:
    result = qualify_recommendation(
        "Consider the Anxiety and Depression Association conference for clinical AI "
        "evidence and mental health research operations outreach."
    )

    assert result.entity.entity_type == "conference"
    assert result.decision in {"maybe", "needs_more_research", "pursue"}
    assert result.recommended_next_step in {"account_research", "contact_research", "monitor"}
    assert "clinical ai" in result.why_relevant.lower()
    assert result.send_enabled is False
    assert "Need a confirmed contact path before drafting outreach." in result.unknowns


def test_qualifies_person_recommendation_with_linkedin_contact_path() -> None:
    result = qualify_recommendation(
        "Evaluate person Dr. Ada Smith for neuroscience evidence collaboration "
        "https://www.linkedin.com/in/ada-smith"
    )

    assert result.entity.entity_type == "person"
    assert result.entity.contact_paths[0].path_type == "linkedin"
    assert result.recommended_next_step in {
        "draft_linkedin",
        "contact_research",
        "account_research",
    }
    assert result.source_links == ["https://www.linkedin.com/in/ada-smith"]


def test_recommendation_markdown_is_human_readable() -> None:
    result = qualify_recommendation(
        "Evaluate Mentavi as a mental health AI company for validation outreach.",
        source_links=["https://www.mentavi.com"],
    )
    markdown = recommendation_intake_markdown(result)

    assert "# Recommendation Intake" in markdown
    assert "## Decision" in markdown
    assert "## Entity" in markdown
    assert "## Suggested Searches" in markdown
    assert "No email, LinkedIn message, or external action is sent." in markdown


def test_recommendation_save_writes_entity_memory(tmp_path) -> None:
    database_url = f"sqlite:///{tmp_path / 'recommendations.db'}"
    result = qualify_recommendation(
        "Evaluate Mentavi as a mental health AI company for validation outreach.",
        source_links=["https://www.mentavi.com"],
    )

    storage = save_recommendation_intake_result(result, database_url=database_url)
    records = SQLiteStore(database_url).retrieve_memory(
        "mental health validation",
        object_key=result.entity.canonical_key,
        approved_only=True,
    )

    assert storage["agent_run"]["status"] == "saved"
    assert storage["memory_ids"]
    assert records
    assert records[0].content["entity_kind"] == result.entity.entity_type
    assert records[0].send_enabled is False


def test_recommendation_cli_outputs_json(tmp_path) -> None:
    from tests.test_pipeline import _run_cli_json

    database_url = f"sqlite:///{tmp_path / 'recommendations.db'}"
    payload = _run_cli_json(
        "run_recommendation_intake.py",
        "Evaluate Mentavi as a mental health AI company for validation outreach.",
        "--source-link",
        "https://www.mentavi.com",
        "--save",
        "--database-url",
        database_url,
    )

    assert payload["entity"]["name"] == "Mentavi"
    assert payload["orchestrator_route"]["route"] == "opportunity_scout"
    assert payload["storage"]["memory_ids"]
    assert json.dumps(payload).find("send_enabled") != -1


def test_orchestrator_routes_recommendation_intake_to_opportunity_scout() -> None:
    result = route_request(
        "Evaluate this conference recommendation as a worthwhile outreach opportunity."
    )

    assert result.route == "opportunity_scout"
    assert result.send_enabled is False
