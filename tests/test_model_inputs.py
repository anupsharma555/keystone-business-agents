from __future__ import annotations

from keystone_agents.models import BusinessResearchSDKInput, OpportunityScoutSDKInput
from keystone_agents.schemas.retrieval import RetrievalHint


def test_account_research_sdk_input_prompt_can_surface_retrieval_hint() -> None:
    prompt = BusinessResearchSDKInput(
        company_name="Curebase",
        retrieval_hint=RetrievalHint(
            source="orchestrator",
            needs_structured_enrichment=True,
            reasons=["leadership context likely matters"],
        ),
    ).to_prompt()

    assert "Optional retrieval guidance from the control plane" in prompt
    assert '"source": "orchestrator"' in prompt
    assert '"needs_structured_enrichment": true' in prompt


def test_opportunity_scout_sdk_input_prompt_can_surface_retrieval_hint() -> None:
    prompt = OpportunityScoutSDKInput(
        topic="behavioral health AI",
        retrieval_hint=RetrievalHint(
            source="sdk_synthesis",
            needs_precision_search=True,
            reasons=["strict recency filters"],
        ),
    ).to_prompt()

    assert "Optional retrieval guidance from the control plane" in prompt
    assert '"source": "sdk_synthesis"' in prompt
    assert '"needs_precision_search": true' in prompt
