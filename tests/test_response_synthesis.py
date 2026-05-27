from keystone_agents.response_synthesis import (
    UserFacingResponseSynthesis,
    UserFacingResponseSynthesisInput,
    format_user_response_synthesis,
    latest_user_request,
)


def test_format_user_response_synthesis_demotes_status_and_removes_redundant_sections() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="OpenEvidence research status",
        answer=(
            "I have a partial, source-backed read on OpenEvidence, "
            "but this run is not decision-ready yet.\n\n"
            "What the sources support\n"
            "- OpenEvidence describes itself as a medical knowledge platform.\n"
            "- A March 2026 release claims a usage milestone.\n\n"
            "What remains unknown\n"
            "- Independent validation of the milestone.\n\n"
            "What should happen next\n"
            "- Deepen the research with current-year independent sources.\n\n"
            "Key points\n"
            "- Supported: OpenEvidence is active in 2026.\n\n"
            "Next step\n"
            "Run a deeper pass."
        ),
        key_points=["Supported: OpenEvidence is active in 2026."],
        caveats=["The current evidence is concentrated in company-controlled sources."],
        next_step="Run a deeper 2026 source pass.",
    )

    text = format_user_response_synthesis(synthesis)

    assert text.startswith("OpenEvidence research status\n\nWhat the sources support")
    assert "What should happen next" not in text
    assert "Key points" not in text
    assert "Next step" not in text
    assert "Run notes" in text
    assert text.index("What the sources support") < text.index("Run notes")
    assert "I have a partial, source-backed read on OpenEvidence" in text
    assert text.index("I have a partial, source-backed read on OpenEvidence") > text.index(
        "Run notes"
    )
    assert "company-controlled sources" in text


def test_format_user_response_synthesis_removes_trailing_followup_offer() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="OpenEvidence partnerships",
        answer=(
            "The source-backed partnership evidence is concentrated in OpenEvidence's "
            "own named collaborations with journals and medical societies.\n\n"
            "If you want, I can next turn this into a partnership table."
        ),
    )

    text = format_user_response_synthesis(synthesis)

    assert "If you want" not in text
    assert "partnership evidence" in text


def test_latest_user_request_extracts_thread_followup() -> None:
    text = (
        "business research analyst research OpenEvidence in 2026.\n"
        "Previous result: broad company profile.\n"
        "Follow-up: can you look specifically at partnerships with journals?"
    )

    assert latest_user_request(text) == (
        "can you look specifically at partnerships with journals?"
    )


def test_response_synthesis_prompt_requires_source_urls_for_link_requests() -> None:
    prompt = UserFacingResponseSynthesisInput(
        user_request=(
            "workitem opportunity scout continue this prior Slack thread.\n"
            "User follow-up: ok provide a weblink for each one of these 3"
        ),
        latest_user_request="ok provide a weblink for each one of these 3",
        agent_name="opportunity_scout",
        route="opportunity_scout",
        status="in_progress",
        advanced=True,
        deterministic_summary="Opportunity Scout attached 3 source-backed records.",
        artifacts=[
            {
                "artifact_type": "opportunity",
                "title": "Mantra Health",
                "summary": "Partnership signal.",
                "metadata": {
                    "source_refs": [
                        {
                            "title": "Mantra Health Launches Beacon",
                            "url": "https://example.com/mantra",
                        }
                    ]
                },
            }
        ],
    ).to_prompt()

    assert "include the available source URLs" in prompt
    assert "https://example.com/mantra" in prompt
