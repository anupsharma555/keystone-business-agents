from keystone_agents.response_synthesis import (
    UserFacingResponseSynthesis,
    UserFacingResponseSynthesisInput,
    _user_response_synthesis_input,
    format_user_response_synthesis,
    latest_user_request,
    low_metadata_requested,
    response_synthesis_metadata_lines,
    response_synthesis_ordered_sources,
    response_synthesis_provider_results,
    response_synthesis_source_context_notes,
    response_synthesis_source_data_summaries,
    response_synthesis_source_triage_notes,
    response_synthesis_sources,
)
from keystone_agents.schemas.work_item import (
    WorkflowRunResult,
    WorkItem,
    WorkItemArtifactRef,
    WorkItemKind,
    WorkItemRoute,
    WorkItemSourceRef,
    WorkItemStatus,
)
from keystone_agents.visible_sources import (
    append_visible_source_urls_to_output,
    append_visible_source_urls_to_text,
)


def test_response_synthesis_receives_manual_plan_ask_shape() -> None:
    result = WorkflowRunResult(
        work_item=WorkItem(kind=WorkItemKind.RESEARCH_BRIEF, title="Exact review"),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=False,
        human_summary="No exact source-backed match was retained.",
        manual_request_plan={
            "ask_shape": {
                "strict_filter_mode": "exact",
                "output_form": "table",
                "permission_state": "read_only",
                "stop_condition": "return_zero_without_broadening_if_no_exact_match",
                "output_constraints": {
                    "interpretation": "exact two-row table",
                    "scope": "entire_response",
                    "item_count_mode": "exact",
                    "minimum_items": 2,
                    "maximum_items": 2,
                },
            }
        },
    )

    synthesis_input = _user_response_synthesis_input(
        result, user_request="Return exact matches in a table or zero."
    )

    assert synthesis_input.manual_plan is not None
    assert synthesis_input.manual_plan["ask_shape"]["strict_filter_mode"] == "exact"
    assert synthesis_input.manual_plan["ask_shape"]["output_form"] == "table"
    assert (
        synthesis_input.manual_plan["ask_shape"]["stop_condition"]
        == "return_zero_without_broadening_if_no_exact_match"
    )
    assert (
        synthesis_input.manual_plan["ask_shape"]["output_constraints"]["maximum_items"]
        == 2
    )
    prompt = synthesis_input.to_prompt()
    assert "manual_plan.ask_shape.output_constraints" in prompt
    assert '"maximum_items": 2' in prompt
    assert synthesis_input.request_coverage_required is True


def test_response_synthesis_prompt_keeps_pipeline_terms_out_of_reader_copy() -> None:
    synthesis_input = UserFacingResponseSynthesisInput(
        user_request="Find competitors.",
        agent_name="business_research_analyst",
        route="business_research_analyst",
        status="blocked",
        advanced=False,
        deterministic_summary="Two provisional candidates have bounded evidence.",
    )

    prompt = synthesis_input.to_prompt()

    assert "Never refer to the payload" in prompt
    assert "partial or blocked research result must still answer" in prompt
    assert "Put missing official sources" in prompt


def test_reader_summary_prefers_bounded_claim_over_raw_page_navigation() -> None:
    rendered = format_user_response_synthesis(
        UserFacingResponseSynthesis(
            title="Callyope review",
            answer="Callyope is a plausible multimodal behavioral-health competitor.",
            synthesis="",
        ),
        sources=[
            {
                "title": "Callyope FAQ",
                "url": "https://www.callyope.com/faq",
                "supported_claim": (
                    "Callyope combines voice, language, clinical history, sleep, "
                    "and activity signals."
                ),
                "evidence_excerpt": (
                    "## FAQs How is Callyope different from general-purpose AI tools? "
                    "Sign in. Open a support ticket."
                ),
                "extraction_status": "extracted",
            }
        ],
        show_metadata=False,
    )

    assert "combines voice, language, clinical history" in rendered
    assert "## FAQs" not in rendered
    assert "Sign in" not in rendered
    assert "support ticket" not in rendered


def test_response_synthesis_receives_validated_partial_request_coverage() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="company_profile",
        artifact_id="coverage-1",
        metadata={
            "request_coverage": {
                "interpreted_request": "Return exact official-source matches in a table.",
                "status": "partial",
                "satisfied_dimensions": ["table"],
                "unmet_dimensions": ["official-source coverage"],
                "output_form_status": "satisfied",
                "stop_condition_status": "blocked",
                "next_safe_action": "Read the official source before finalizing.",
            }
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.COMPANY_RESEARCH,
            title="Coverage review",
            artifact_refs=[artifact],
        ),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="A partial table is available.",
        manual_request_plan={
            "ask_shape": {
                "strict_filter_mode": "exact",
                "output_form": "table",
                "stop_condition": "stop_before_unverified_claims",
            }
        },
    )

    synthesis_input = _user_response_synthesis_input(
        result, user_request="Return exact official-source matches in a table."
    )

    assert synthesis_input.request_coverage_required is True
    assert len(synthesis_input.request_coverage) == 1
    assert synthesis_input.request_coverage[0].status == "partial"
    assert synthesis_input.request_coverage[0].unmet_dimensions == [
        "official-source coverage"
    ]
    prompt = synthesis_input.to_prompt()
    assert "present a precise partial answer or blocker" in prompt


def test_visible_sources_appends_structured_urls_to_plain_text() -> None:
    text = append_visible_source_urls_to_text(
        "Confirmed: the Q2 estimated tax payment date is June 15, 2026.",
        [
            {
                "title": "IRS Publication 505",
                "url": "https://www.irs.gov/publications/p505",
            }
        ],
    )

    assert "Sources: IRS Publication 505: https://www.irs.gov/publications/p505" in text


def test_visible_sources_does_not_duplicate_when_text_already_has_url() -> None:
    text = append_visible_source_urls_to_text(
        "Confirmed from https://www.irs.gov/publications/p505.",
        [
            {
                "title": "IRS Publication 505",
                "url": "https://www.irs.gov/publications/p505",
            }
        ],
    )

    assert text.count("https://www.irs.gov/publications/p505") == 1


def test_response_synthesis_excludes_internal_fixture_urls() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="company_profile",
        artifact_id="fixture-source",
        source_agent="business_research_analyst",
        title="Supplied Slack facts",
        summary="Operator-supplied context.",
        metadata={
            "source_refs": [
                {
                    "title": "Source-provided Slack facts",
                    "url": "fixture://source-provided/slack-context",
                    "supported_claim": "Facts came from the operator's Slack note.",
                }
            ]
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="Supplied context",
            artifact_refs=[artifact],
        ),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Supplied context reviewed.",
    )

    assert response_synthesis_sources(result) == []
    assert response_synthesis_ordered_sources(result) == []


def test_response_synthesis_excludes_local_operational_urls() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="gmail_triage_report",
        artifact_id="provider-result",
        source_agent="gmail_triage",
        title="Verified Gmail result",
        summary="No reply-worthy candidate was found.",
        metadata={
            "source_refs": [
                {
                    "title": "Prior Slack eval dashboard",
                    "url": "http://127.0.0.1:8769/dashboard?case=prior-failure",
                    "supported_claim": "The prior Slack attempt failed.",
                }
            ],
            "retrieval_diagnostics": {
                "provider_result_samples": {
                    "slack": [
                        {
                            "title": "Prior review",
                            "url": "http://localhost:8769/review?case=prior-failure",
                            "snippet": "Internal review metadata.",
                        }
                    ]
                }
            },
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.GMAIL_THREAD,
            title="Gmail follow-up",
            artifact_refs=[artifact],
            sources=[
                WorkItemSourceRef(
                    source_type="slack_thread_link",
                    provider="slack",
                    title="Prior Slack review",
                    url="file:///tmp/prior-slack-review.html",
                )
            ],
        ),
        route=WorkItemRoute.GMAIL_TRIAGE,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="No reply-worthy candidate was found.",
        context_pack={
            "ordered_sources": [
                {
                    "index": 1,
                    "title": "Prior local review",
                    "url": "http://[::1]:8769/review",
                }
            ]
        },
    )

    assert response_synthesis_sources(result) == []
    assert response_synthesis_ordered_sources(result) == []
    assert response_synthesis_provider_results(result) == []


def test_visible_sources_updates_mapping_summary_from_structured_sources() -> None:
    output = append_visible_source_urls_to_output(
        {
            "summary": "Confirmed: Q2 estimated taxes are due June 15, 2026.",
            "sources": [
                {
                    "title": "IRS Publication 505",
                    "url": "https://www.irs.gov/publications/p505",
                }
            ],
        }
    )

    assert output["summary"].endswith(
        "Sources: IRS Publication 505: https://www.irs.gov/publications/p505"
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


def test_format_user_response_synthesis_appends_structured_source_urls() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="Q2 estimated tax due date",
        answer="The IRS source supports June 15, 2026 as the Q2 due date.",
    )

    text = format_user_response_synthesis(
        synthesis,
        sources=[
            {
                "title": "IRS Publication 505",
                "url": "https://www.irs.gov/publications/p505",
            }
        ],
    )

    assert "https://www.irs.gov/publications/p505" in text


def test_format_user_response_synthesis_renders_answer_and_synthesis_sections() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="OpenAI mental health safety brief",
        answer="OpenAI's source-backed mental-health work is primarily product-safety work.",
        synthesis=(
            "The selected sources support crisis-routing and sensitive-conversation "
            "response improvements, while provider-lane candidates can guide deeper "
            "follow-up on policy and partnership claims."
        ),
        caveats=["No clinical efficacy claim is supported by the retrieved context."],
    )

    text = format_user_response_synthesis(synthesis)

    assert text.startswith("OpenAI mental health safety brief")
    assert "*Answer:*" in text
    assert "*Detailed Summary:*" in text
    assert "provider-lane candidates" in text
    assert text.index("*Answer:*") < text.index("*Detailed Summary:*")
    assert text.index("*Detailed Summary:*") < text.index("Run notes")


def test_source_backed_caveats_render_as_limitations_after_answer() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="Deliberate AI competitor research",
        answer="The strongest provisional candidates are Limbic and Ksana Health.",
        synthesis=(
            "Limbic overlaps in behavioral-health AI, while Ksana Health combines "
            "smartphone, wearable, and EHR signals for behavioral-health modeling."
        ),
        caveats=[
            "Deliberate AI's target users and explicit modality mix need stronger "
            "official-source confirmation."
        ],
    )

    text = format_user_response_synthesis(
        synthesis,
        sources=[
            {
                "title": "Limbic",
                "url": "https://limbic.ai/",
                "supported_claim": "Limbic provides behavioral-health AI.",
            }
        ],
    )

    assert "Limitations" in text
    assert "Run notes" not in text
    assert text.index("*Answer:*") < text.index("Limitations")
    assert "target users and explicit modality mix" in text


def test_reader_response_can_keep_diagnostics_internal() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="Competitor review",
        answer="Ellipsis Health is a provisional candidate.",
        synthesis="The available source evidence supports a bounded comparison.",
        caveats=["Direct product validation is still limited."],
    )

    text = format_user_response_synthesis(
        synthesis,
        sources=[{"title": "Ellipsis Health", "url": "https://example.com/ellipsis"}],
        metadata_lines=["Source context: 1/1 selected URL extracted/read"],
        show_metadata=False,
    )

    assert "Ellipsis Health is a provisional candidate." in text
    assert "Limitations" in text
    assert "\nMetadata\n" not in text
    assert "Source context:" not in text


def test_format_user_response_synthesis_renders_source_terms_and_actions() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="AI chatbot youth safety brief",
        answer="Youth-facing AI chatbot safety concerns center on crisis handling and privacy.",
        synthesis=(
            "The retrieved source set supports caution around mental-health chatbot use "
            "by minors, especially where escalation and privacy controls are weak."
        ),
        source_evidence=[
            (
                "APA advisory: https://example.com/apa - Explains why generic chatbots "
                "should not be treated as clinical mental health care."
            )
        ],
        terms=["APA: American Psychological Association."],
        recommended_actions=["Track youth-specific safety standards and escalation requirements."],
    )

    text = format_user_response_synthesis(synthesis)

    assert "Source evidence" in text
    assert "APA advisory: https://example.com/apa" in text
    assert "Terms" in text
    assert "APA: American Psychological Association." in text
    assert "Recommended actions" in text
    assert "Track youth-specific safety standards" in text
    assert text.index("*Detailed Summary:*") < text.index("Source evidence")
    assert text.index("Source evidence") < text.index("Terms")
    assert text.index("Terms") < text.index("Recommended actions")


def test_format_user_response_synthesis_appends_compact_metadata_footer() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="OpenAI mental health safety brief",
        answer="OpenAI's mental-health-related work is mainly safety work.",
        synthesis="The selected source supports sensitive-conversation safety work.",
    )

    text = format_user_response_synthesis(
        synthesis,
        metadata_lines=[
            "Search query: `OpenAI mental health safety`",
            "Search providers: searxng, agents-web-search, exa",
            "Source context: 1/1 selected URLs extracted/read; 1/1 include evidence/claims",
        ],
    )

    assert "Metadata" in text
    assert "* Search query: `OpenAI mental health safety`" in text
    assert "* Search providers: searxng, agents-web-search, exa" in text
    assert text.index("*Detailed Summary:*") < text.index("Metadata")


def test_format_user_response_synthesis_can_suppress_metadata_sections() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="Recommended handoff",
        answer="Route this to Chief of Staff first.",
        synthesis="The next step is scope clarification before any outreach.",
        terms=["PHI: Protected health information."],
        recommended_actions=["Confirm requester and scope."],
        caveats=["No source links are available in the payload."],
    )

    text = format_user_response_synthesis(
        synthesis,
        metadata_lines=["Source focus: no_source_context_sample"],
        low_metadata=True,
    )

    assert "*Answer:*" in text
    assert "*Detailed Summary:*" in text
    assert "Recommended actions" in text
    assert "Terms" not in text
    assert "Run notes" not in text
    assert "Metadata" not in text


def test_low_metadata_requested_detects_operator_instruction() -> None:
    assert low_metadata_requested("Keep the main answer concise and low-metadata.")
    assert low_metadata_requested("Use minimal metadata in the Slack reply.")
    assert low_metadata_requested("Return a concise internal handoff with the next owner.")
    assert not low_metadata_requested("Use compact source metadata for research.")


def test_format_user_response_synthesis_keeps_metadata_last_after_source_url_repair() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="OpenAI mental health safety brief",
        answer="OpenAI's mental-health-related work is mainly safety work.",
        synthesis="The selected source supports sensitive-conversation safety work.",
    )

    text = format_user_response_synthesis(
        synthesis,
        sources=[
            {
                "title": "OpenAI update",
                "url": "https://openai.com/index/update-on-mental-health-related-work/",
            }
        ],
        metadata_lines=["Search providers: searxng, exa"],
    )

    assert "Source evidence" in text
    assert "OpenAI update: https://openai.com/index/update-on-mental-health-related-work/" in text
    assert "Metadata\n* Search providers: searxng, exa" in text
    assert text.index("Source evidence") < text.index("Metadata")
    assert text.rstrip().endswith("* Search providers: searxng, exa")


def test_format_user_response_synthesis_builds_source_evidence_from_structured_sources() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="OpenAI mental health safety brief",
        answer="OpenAI's mental-health-related work is mainly safety work.",
        synthesis="The selected sources support product-safety work rather than a therapy product.",
    )

    text = format_user_response_synthesis(
        synthesis,
        sources=[
            {
                "title": "OpenAI update",
                "url": "https://openai.com/example",
                "supported_claim": "OpenAI describes sensitive-conversation improvements.",
            }
        ],
    )

    assert "Source evidence" in text
    assert (
        "OpenAI update: https://openai.com/example - "
        "OpenAI describes sensitive-conversation improvements."
    ) in text


def test_format_user_response_synthesis_falls_back_to_source_based_synthesis() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="OpenAI mental health source follow-up",
        answer="OpenAI's mental-health-related work is mainly safety work.",
    )

    text = format_user_response_synthesis(
        synthesis,
        sources=[
            {
                "title": "OpenAI update",
                "url": "https://openai.com/index/update-on-mental-health-related-work/",
                "supported_claim": (
                    "OpenAI describes mental-health-related sensitive-conversation work."
                ),
                "extraction_status": "snippet_only",
            }
        ],
    )

    assert "*Answer:*" in text
    assert "*Detailed Summary:*" in text
    assert "Key source details:" in text
    assert "OpenAI update:" in text
    assert "OpenAI update (snippet_only)" not in text
    assert "Source evidence" in text
    assert text.index("*Answer:*") < text.index("*Detailed Summary:*")
    assert text.index("*Detailed Summary:*") < text.index("Source evidence")


def test_format_user_response_synthesis_prepends_summary_to_bullet_only_synthesis() -> None:
    sources = [
        {
            "title": "JMIR mental health provider documentation study",
            "url": "https://formative.jmir.org/2026/1/e84628",
            "supported_claim": (
                "A behavioral-health-provider study evaluates AI-powered documentation."
            ),
            "extraction_status": "article_read",
        },
        {
            "title": "ClinicalTrials.gov ambient AI scribe outpatient clinic trial",
            "url": "https://clinicaltrials.gov/study/NCT07302906",
            "supported_claim": (
                "A trial registry describes outpatient-clinic evaluation of ambient AI scribes."
            ),
            "extraction_status": "article_read",
        },
    ]

    text = format_user_response_synthesis(
        UserFacingResponseSynthesis(
            title="Ambient documentation in behavioral health",
            answer=(
                "Public signals show evaluation in behavioral health and adjacent "
                "outpatient clinical settings."
            ),
            synthesis=(
                "- JMIR: direct behavioral-health-provider evaluation signal.\n"
                "- ClinicalTrials.gov: active outpatient-clinic evaluation signal."
            ),
        ),
        sources=sources,
    )

    synthesis_section = text.split("*Detailed Summary:*", 1)[1].split("Source evidence", 1)[0]

    assert synthesis_section.strip().startswith("Across the retrieved sources")
    assert "behavioral-health or psychiatry relevance" in synthesis_section
    assert "JMIR: direct behavioral-health-provider evaluation signal" in synthesis_section
    assert "The detailed summary should treat these links" not in synthesis_section
    assert "not as a citation list" not in synthesis_section
    assert text.index("Across the retrieved sources") < text.index("- JMIR:")
    assert "Source evidence" in text


def test_format_user_response_synthesis_replaces_status_only_answer_from_sources() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="OpenAI mental health source follow-up",
        answer="Search run completed in read-only mode.",
    )

    text = format_user_response_synthesis(
        synthesis,
        sources=[
            {
                "title": "OpenAI update",
                "url": "https://openai.com/index/update-on-mental-health-related-work/",
                "supported_claim": (
                    "OpenAI describes mental-health-related sensitive-conversation work."
                ),
                "extraction_status": "extracted",
            }
        ],
    )

    assert "*Answer:*" in text
    assert "Selected source context supports this point" in text
    assert "Search run completed in read-only mode." not in text
    assert "Run notes" not in text


def test_format_user_response_synthesis_does_not_fallback_from_off_focus_sources() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="OpenAI mental health source follow-up",
        answer="Search run completed in read-only mode.",
    )

    text = format_user_response_synthesis(
        synthesis,
        sources=[
            {
                "title": "OpenAI infrastructure update",
                "url": "https://example.com/infrastructure",
                "supported_claim": "OpenAI announced an infrastructure partnership.",
                "extraction_status": "extracted",
            }
        ],
        metadata_lines=[
            (
                "Source focus: no_sample_source_matches_focus; "
                "0/1 sample sources matched; terms: mental, health"
            )
        ],
    )

    assert "does not match the request focus closely enough" in text
    assert "Selected source context supports this point" not in text
    assert "selected source context supports the following" not in text
    assert "OpenAI infrastructure update: https://example.com/infrastructure" in text
    assert "Search run completed in read-only mode." not in text
    assert "Source focus: no_sample_source_matches_focus" in text


def test_format_user_response_synthesis_repairs_ambiguous_apa_term() -> None:
    synthesis = UserFacingResponseSynthesis(
        title="APA advisory",
        answer="The advisory is source-backed.",
        synthesis=(
            "The APA source is https://www.apa.org/topics/artificial-intelligence-machine-learning/"
            "health-advisory-chatbots-wellness-apps and a related psychiatry source is "
            "https://www.psychiatry.org/news-room/apa-blogs/ai-survey."
        ),
        terms=["APA: American Psychiatric Association."],
    )

    text = format_user_response_synthesis(synthesis)

    assert "American Psychological Association for `apa.org` sources" in text
    assert "American Psychiatric Association for `psychiatry.org` sources" in text


def test_latest_user_request_extracts_thread_followup() -> None:
    text = (
        "business research analyst research OpenEvidence in 2026.\n"
        "Previous result: broad company profile.\n"
        "Follow-up: can you look specifically at partnerships with journals?"
    )

    assert latest_user_request(text) == ("can you look specifically at partnerships with journals?")


def test_latest_user_request_extracts_labeled_followup_variant() -> None:
    text = (
        "opportunity scout prior strict search.\n"
        "Previous result: three adjacent opportunities.\n"
        "Follow-up retest after source-link patch: answer only from prior state."
    )

    assert latest_user_request(text) == "answer only from prior state."


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


def test_response_synthesis_sources_prioritize_artifact_aligned_refs() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="opportunity",
        artifact_id="1",
        source_agent="opportunity_scout",
        title="Sagent Behavioral Health",
        summary="Measurement-based care partnership.",
        metadata={
            "source_refs": [
                {
                    "title": "Sagent partners with Greenspace",
                    "url": "https://example.com/sagent",
                    "supported_claim": "Sagent is scaling measurement-based care.",
                    "key_facts": ["Sagent MBC signal."],
                }
            ]
        },
    )
    work_item = WorkItem(
        kind=WorkItemKind.OPPORTUNITY,
        title="Opportunity scan",
        current_route=WorkItemRoute.OPPORTUNITY_SCOUT,
        sources=[
            WorkItemSourceRef(
                title="Unrelated first global source",
                url="https://example.com/unrelated",
                supported_claim="Different company.",
            )
        ],
        artifact_refs=[artifact],
    )
    result = WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.OPPORTUNITY_SCOUT,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Deterministic summary",
    )

    synthesis_input = _user_response_synthesis_input(
        result,
        user_request="Compare companies in a compact comparison table with source URLs.",
    )

    assert synthesis_input.sources[0]["artifact_title"] == "Sagent Behavioral Health"
    assert synthesis_input.sources[0]["url"] == "https://example.com/sagent"
    assert synthesis_input.sources[1]["url"] == "https://example.com/unrelated"
    prompt = synthesis_input.to_prompt()
    assert "use a compact Markdown table" in prompt
    assert "do not pair a named item with unrelated global sources" in prompt


def test_response_synthesis_includes_provider_lane_results_in_prompt() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="company_profile",
        artifact_id="1",
        source_agent="business_research_analyst",
        title="OpenAI",
        summary="Company profile.",
        metadata={
            "source_refs": [
                {
                    "title": "OpenAI safety page",
                    "url": "https://openai.com/safety/",
                    "supported_claim": "OpenAI publishes safety guidance.",
                    "extraction_status": "extracted",
                    "evidence_excerpt": (
                        "The extracted OpenAI safety page describes sensitive-conversation "
                        "response improvements and evaluation work."
                    ),
                }
            ],
            "retrieval_diagnostics": {
                "provider_summary": "searxng+agents-web-search+exa+tavily",
                "search_queries": ["OpenAI mental health safety"],
                "source_triage": {
                    "decision_counts": {
                        "retain": 1,
                        "deepen": 1,
                        "reject": 1,
                    },
                    "retained_count": 1,
                    "deepen_count": 1,
                    "rejected_count": 1,
                    "retained_source_ids": ["selected:1"],
                    "deepen_source_ids": ["selected:2"],
                    "rejected_source_ids": ["selected:3"],
                    "needs_broaden_or_deepen": True,
                    "recommended_action": "broaden_or_deepen_before_final_synthesis",
                    "recall_gaps": ["missing expected source lane: press_news"],
                },
                "provider_usage": {
                    "exa": {
                        "requests_attempted": 1,
                        "requests_succeeded": 1,
                        "credits_used": 1,
                    }
                },
                "provider_result_samples": {
                    "searxng": [
                        {
                            "title": "OpenAI safety",
                            "url": "https://openai.com/safety/",
                            "snippet": "Safety overview.",
                        }
                    ],
                    "agents-web-search": [
                        {
                            "title": "OpenAI mental health note",
                            "url": "https://example.com/agents-openai-mental-health",
                            "snippet": "Mental health policy discussion.",
                        }
                    ],
                    "exa": [
                        {
                            "title": "OpenAI health policy analysis",
                            "url": "https://example.com/exa-openai-health",
                            "snippet": "Semantic result about health policy.",
                        }
                    ],
                    "tavily": [
                        {
                            "title": "OpenAI safety partnership",
                            "url": "https://example.com/tavily-openai-safety",
                            "snippet": "Deeper search result.",
                        }
                    ],
                },
            },
        },
    )
    work_item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        title="OpenAI brief",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        artifact_refs=[artifact],
    )
    result = WorkflowRunResult(
        work_item=work_item,
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Business Research Analyst attached a source-backed company profile.",
    )

    provider_results = response_synthesis_provider_results(result)
    source_data_summaries = response_synthesis_source_data_summaries(result)
    source_triage_notes = response_synthesis_source_triage_notes(result)
    metadata_lines = response_synthesis_metadata_lines(result)
    synthesis_input = _user_response_synthesis_input(
        result,
        user_request=(
            "Give me a source-backed brief on OpenAI mental health safety work "
            "and compare what deeper search added."
        ),
    )
    prompt = synthesis_input.to_prompt()

    assert {item["provider"] for item in provider_results} == {
        "searxng",
        "agents-web-search",
        "exa",
        "tavily",
    }
    assert synthesis_input.provider_results == provider_results
    assert synthesis_input.source_data_summaries == source_data_summaries
    assert synthesis_input.source_triage_notes == source_triage_notes
    assert len(synthesis_input.source_triage) == 1
    assert synthesis_input.source_triage[0].decision_counts == {
        "retain": 1,
        "deepen": 1,
        "reject": 1,
    }
    assert synthesis_input.source_triage[0].deepen_source_ids == ["selected:2"]
    assert synthesis_input.source_triage_notes == [
        (
            "Source triage: broaden_or_deepen_before_final_synthesis; "
            "decisions: retain: 1, deepen: 1, reject: 1; "
            "broaden/deepen recommended before strong final synthesis; "
            "gaps: missing expected source lane: press_news"
        )
    ]
    assert synthesis_input.source_data_summaries == [
        (
            "OpenAI safety page (extracted): The extracted OpenAI safety page "
            "describes sensitive-conversation response improvements and evaluation "
            "work. OpenAI publishes safety guidance. Source: https://openai.com/safety/"
        )
    ]
    assert (
        synthesis_input.sources[0]["evidence_excerpt"]
        == "The extracted OpenAI safety page describes sensitive-conversation response improvements and evaluation work."
    )
    assert synthesis_input.source_context_notes == [
        "1/1 selected source URL(s) are marked as extracted/read for synthesis."
    ]
    assert "provider_results are search-lane candidates" in prompt
    assert "source_triage_notes as the source-selection contract" in prompt
    assert "broaden_or_deepen_before_final_synthesis" in prompt
    assert "source_data_summaries as the primary factual substrate" in prompt
    assert "OpenAI safety page (extracted)" in prompt
    assert "sensitive-conversation response improvements" in prompt
    assert "marked as extracted/read for synthesis" in prompt
    assert "https://example.com/exa-openai-health" in prompt
    assert "https://example.com/tavily-openai-safety" in prompt
    assert "Treat payload sources and provider_results as the bounded retrieved context" in prompt
    assert "visible `Detailed Summary` section" in prompt
    assert "Detailed Summary should be richer than generic LLM search" in prompt
    assert "Search query: `OpenAI mental health safety`" in metadata_lines
    assert "Search providers: searxng, agents-web-search, exa, tavily" in metadata_lines
    assert "Provider usage: exa: 1/1 ok, 1 credits" in metadata_lines
    assert (
        "Source context: 1/1 selected URLs extracted/read; "
        "1/1 include evidence/claims; statuses: extracted"
    ) in metadata_lines
    assert any(line.startswith("Provider top URLs:") for line in metadata_lines)


def test_response_synthesis_excludes_source_triage_rejected_source_refs() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id="1",
        source_agent="chief_of_staff",
        title="Safety brief",
        summary="Source-backed brief.",
        metadata={
            "source_refs": [
                {
                    "source_id": "selected:1",
                    "title": "Retained teen safety source",
                    "url": "https://example.com/retained",
                    "supported_claim": "The source describes teen safety controls.",
                    "evidence_excerpt": "The source describes escalation and guardian controls.",
                    "extraction_status": "success",
                },
                {
                    "title": "Rejected unrelated infrastructure source",
                    "url": "https://example.com/rejected",
                    "supported_claim": "The source is about cloud infrastructure.",
                    "evidence_excerpt": "The page discusses enterprise cloud infrastructure only.",
                    "extraction_status": "success",
                },
            ],
            "retrieval_diagnostics": {
                "source_triage": {
                    "recommended_action": "synthesize_from_retained_sources",
                    "retained_source_ids": ["selected:1"],
                    "rejected_source_ids": ["selected:2"],
                    "rejected_urls": ["https://example.com/rejected"],
                    "decision_counts": {"retain": 1, "reject": 1},
                }
            },
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="Safety brief",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
            artifact_refs=[artifact],
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Chief of Staff attached a source-backed brief.",
    )

    sources = response_synthesis_sources(result)
    summaries = response_synthesis_source_data_summaries(result)
    notes = response_synthesis_source_triage_notes(result)

    assert [source["source_id"] for source in sources] == ["selected:1"]
    assert sources[0]["url"] == "https://example.com/retained"
    assert len(summaries) == 1
    assert "escalation and guardian controls" in summaries[0]
    assert "cloud infrastructure" not in summaries[0]
    assert notes == [
        "Source triage: synthesize_from_retained_sources; decisions: retain: 1, reject: 1"
    ]


def test_response_synthesis_uses_top_level_source_triage_metadata() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id="1",
        source_agent="chief_of_staff",
        title="Safety brief",
        summary="Source-backed brief.",
        metadata={
            "source_refs": [
                {
                    "source_id": "selected:1",
                    "title": "Retained source",
                    "url": "https://example.com/retained",
                    "supported_claim": "The source describes safety escalation.",
                    "evidence_excerpt": "The retained source describes escalation workflows.",
                    "extraction_status": "success",
                },
                {
                    "source_id": "selected:2",
                    "title": "Rejected source",
                    "url": "https://example.com/rejected",
                    "supported_claim": "The source describes unrelated infrastructure.",
                    "evidence_excerpt": "The rejected source discusses cloud infrastructure.",
                    "extraction_status": "success",
                },
            ],
            "source_triage": {
                "recommended_action": "synthesize_from_retained_sources",
                "retained_source_ids": ["selected:1"],
                "rejected_source_ids": ["selected:2"],
                "decision_counts": {"retain": 1, "reject": 1},
            },
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="Safety brief",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
            artifact_refs=[artifact],
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Chief of Staff attached a source-backed brief.",
    )

    sources = response_synthesis_sources(result)
    summaries = response_synthesis_source_data_summaries(result)
    notes = response_synthesis_source_triage_notes(result)

    assert [source["source_id"] for source in sources] == ["selected:1"]
    assert "escalation workflows" in summaries[0]
    assert "cloud infrastructure" not in summaries[0]
    assert notes == [
        "Source triage: synthesize_from_retained_sources; decisions: retain: 1, reject: 1"
    ]


def test_response_synthesis_excludes_deepen_sources_until_extracted() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id="1",
        source_agent="chief_of_staff",
        title="Ambient scribe brief",
        summary="Source-backed brief.",
        metadata={
            "source_refs": [
                {
                    "source_id": "selected:1",
                    "title": "Snippet-only ambient scribe source",
                    "url": "https://example.com/snippet-only",
                    "supported_claim": "The source appears to discuss ambient scribes.",
                    "evidence_excerpt": "Snippet says ambient scribes may be evaluated in clinics.",
                    "extraction_status": "snippet_only",
                },
                {
                    "source_id": "selected:2",
                    "title": "Read mental health documentation study",
                    "url": "https://example.com/read-study",
                    "supported_claim": "The study reports a mental health documentation evaluation.",
                    "evidence_excerpt": (
                        "The extracted page reports a mixed-methods evaluation of AI-powered "
                        "documentation in mental health provider workflows."
                    ),
                    "extraction_status": "article_read",
                },
            ],
            "source_triage": {
                "recommended_action": "broaden_or_deepen_before_final_synthesis",
                "retained_source_ids": ["selected:2"],
                "deepen_source_ids": ["selected:1"],
                "deepen_urls": ["https://example.com/snippet-only"],
                "decision_counts": {"retain": 1, "deepen": 1},
                "needs_broaden_or_deepen": True,
            },
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="Ambient scribe brief",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
            artifact_refs=[artifact],
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Chief of Staff attached a source-backed brief.",
    )

    sources = response_synthesis_sources(result)
    summaries = response_synthesis_source_data_summaries(result)
    notes = response_synthesis_source_triage_notes(result)

    assert [source["source_id"] for source in sources] == ["selected:2"]
    assert "mixed-methods evaluation" in summaries[0]
    assert "Snippet says" not in " ".join(summaries)
    assert notes == [
        (
            "Source triage: broaden_or_deepen_before_final_synthesis; "
            "decisions: retain: 1, deepen: 1; broaden/deepen recommended "
            "before strong final synthesis"
        )
    ]


def test_provider_top_url_metadata_is_compact_but_source_evidence_keeps_full_urls() -> None:
    full_url = (
        "https://www.calmhsa.org/wp-content/uploads/2025/07/"
        "Behavioral-Health-Clinical-AI-Tools-RFP-2.pdf"
    )
    artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id="1",
        source_agent="chief_of_staff",
        title="CalMHSA RFP",
        summary="Search result.",
        metadata={
            "source_refs": [
                {
                    "title": "CalMHSA Behavioral Health Clinical AI Tools RFP",
                    "url": full_url,
                    "source_type": "primary_pdf",
                    "supported_claim": ("The RFP describes behavioral-health clinical AI tools."),
                    "extraction_status": "article_read",
                    "evidence_excerpt": (
                        "CalMHSA requested proposals for behavioral-health clinical AI tools."
                    ),
                }
            ],
            "retrieval_diagnostics": {
                "provider_summary": "searxng+agents-web-search+exa+tavily",
                "search_queries": [
                    "CalMHSA behavioral health clinical AI tools RFP measurement based care"
                ],
                "provider_result_samples": {
                    "searxng": [
                        {
                            "title": "CalMHSA RFP PDF",
                            "url": full_url,
                            "snippet": "RFP PDF.",
                        }
                    ],
                    "exa": [
                        {
                            "title": "CalMHSA RFP landing page",
                            "url": (
                                "https://www.calmhsa.org/bidsandco/"
                                "request-for-proposals-rfp-behavioral-health-"
                                "clinical-ai-tool/"
                            ),
                            "snippet": "RFP landing page.",
                        }
                    ],
                },
            },
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="CalMHSA RFP",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
            artifact_refs=[artifact],
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Search completed.",
    )

    metadata_lines = response_synthesis_metadata_lines(result)
    top_urls_line = next(line for line in metadata_lines if line.startswith("Provider top URLs:"))
    rendered = format_user_response_synthesis(
        UserFacingResponseSynthesis(
            title="CalMHSA RFP brief",
            answer="The source-backed result is the CalMHSA behavioral-health AI RFP.",
            synthesis=("The selected source evidence points to the official RFP artifact."),
        ),
        sources=response_synthesis_sources(result),
        metadata_lines=metadata_lines,
    )

    assert full_url in rendered
    assert full_url not in top_urls_line
    assert "calmhsa.org/.../Behavioral-Health-Clinical-AI-Tools-RFP-2.pdf" in top_urls_line
    assert "https://" not in top_urls_line


def test_response_synthesis_normalizes_company_source_record_claims() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="company_profile",
        artifact_id="1",
        source_agent="business_research_analyst",
        title="OpenAI",
        summary="Company profile.",
        metadata={
            "source_refs": [
                {
                    "title": "OpenAI mental health work update",
                    "url": "https://openai.com/index/update-on-mental-health-related-work/",
                    "source_type": "company_site",
                    "supported_claims": [
                        "OpenAI describes mental-health-related safety work for ChatGPT.",
                        "OpenAI says it is improving responses in emotionally sensitive conversations.",
                    ],
                    "extraction_status": "snippet_only",
                }
            ],
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.COMPANY_RESEARCH,
            title="OpenAI brief",
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            artifact_refs=[artifact],
        ),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Business Research Analyst attached a company profile.",
    )

    synthesis_input = _user_response_synthesis_input(
        result,
        user_request="Summarize what OpenAI is doing about mental health.",
    )
    rendered = format_user_response_synthesis(
        UserFacingResponseSynthesis(
            title="OpenAI mental health brief",
            answer="Business Research completed.",
        ),
        sources=synthesis_input.sources,
    )

    assert synthesis_input.sources[0]["supported_claim"] == (
        "OpenAI describes mental-health-related safety work for ChatGPT."
    )
    assert synthesis_input.sources[0]["key_facts"] == [
        "OpenAI describes mental-health-related safety work for ChatGPT.",
        "OpenAI says it is improving responses in emotionally sensitive conversations.",
    ]
    assert "mental-health-related safety work for ChatGPT" in rendered
    assert "https://openai.com/index/update-on-mental-health-related-work/" in rendered


def test_response_synthesis_source_data_summaries_prioritize_extracted_evidence() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id="1",
        source_agent="chief_of_staff",
        title="OpenAI mental health",
        summary="Source-backed brief.",
        metadata={
            "source_refs": [
                {
                    "title": "OpenAI mental health work update",
                    "url": "https://openai.com/index/update-on-mental-health-related-work/",
                    "source_type": "company_site",
                    "supported_claim": (
                        "OpenAI describes mental-health-related safety work for ChatGPT."
                    ),
                    "supported_claims": [
                        "OpenAI says it is improving sensitive-conversation handling.",
                        "OpenAI references clinician guidance and crisis resources.",
                    ],
                    "key_facts": [
                        "The update describes product safety work rather than a clinical product.",
                    ],
                    "evidence_excerpt": (
                        "OpenAI says it is improving how ChatGPT responds in sensitive "
                        "conversations and recognizes warning signs over time. The post "
                        "also discusses external expert input and safety evaluations."
                    ),
                    "extraction_status": "article_read",
                }
            ],
            "retrieval_diagnostics": {
                "provider_summary": "searxng+exa",
                "provider_result_samples": {
                    "exa": [
                        {
                            "title": "OpenAI mental health work update",
                            "url": (
                                "https://openai.com/index/update-on-mental-health-related-work/"
                            ),
                            "snippet": "OpenAI mental health safety work.",
                        }
                    ],
                },
            },
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="OpenAI mental health",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
            artifact_refs=[artifact],
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Chief of Staff attached a source-backed brief.",
    )

    summaries = response_synthesis_source_data_summaries(result)
    synthesis_input = _user_response_synthesis_input(
        result,
        user_request="Summarize what OpenAI is doing about mental health.",
    )
    prompt = synthesis_input.to_prompt()

    assert summaries == synthesis_input.source_data_summaries
    assert len(summaries) == 1
    assert summaries[0].startswith(
        "OpenAI mental health work update (article_read): OpenAI says it is "
        "improving how ChatGPT responds in sensitive conversations"
    )
    assert "OpenAI describes mental-health-related safety work for ChatGPT" in summaries[0]
    assert "improving sensitive-conversation handling" in summaries[0]
    assert "Source: https://openai.com/index/update-on-mental-health-related-work/" in summaries[0]
    assert "source_data_summaries" in prompt
    assert "workflow status" not in summaries[0].lower()


def test_response_synthesis_source_data_summaries_keep_multiple_extracted_details() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id="1",
        source_agent="chief_of_staff",
        title="Teen safety source set",
        summary="Source-backed brief.",
        metadata={
            "source_refs": [
                {
                    "title": "Teen safety update",
                    "url": "https://example.com/teen-safety",
                    "supported_claim": "The source describes teen safety controls.",
                    "supported_claims": [
                        "The rollout is framed as safety support rather than clinical care."
                    ],
                    "evidence_excerpt": (
                        "The product adds teen-specific defaults and account controls. "
                        "It also describes escalation paths when high-risk signals appear. "
                        "The page says parents or trusted adults can receive safety-related "
                        "notifications in defined situations. A final section discusses "
                        "evaluation and rollout limitations."
                    ),
                    "extraction_status": "success",
                }
            ],
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="Teen safety",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
            artifact_refs=[artifact],
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Chief of Staff attached a source-backed brief.",
    )

    summaries = response_synthesis_source_data_summaries(result)

    assert len(summaries) == 1
    assert "teen-specific defaults and account controls" in summaries[0]
    assert "escalation paths when high-risk signals appear" in summaries[0]
    assert "trusted adults can receive safety-related notifications" in summaries[0]
    assert "The source describes teen safety controls" in summaries[0]
    assert "framed as safety support rather than clinical care" in summaries[0]
    assert "evaluation and rollout limitations" not in summaries[0]
    assert "Source: https://example.com/teen-safety" in summaries[0]


def test_response_synthesis_warns_when_provider_urls_lack_extracted_source_context() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id="1",
        source_agent="chief_of_staff",
        title="Search diagnostics",
        summary="Provider lanes found candidate links.",
        metadata={
            "retrieval_diagnostics": {
                "provider_summary": "searxng+exa",
                "provider_result_samples": {
                    "searxng": [
                        {
                            "title": "OpenAI safety",
                            "url": "https://openai.com/safety/",
                            "snippet": "Safety overview.",
                        }
                    ],
                    "exa": [
                        {
                            "title": "OpenAI mental health analysis",
                            "url": "https://example.com/exa-openai-mental-health",
                            "snippet": "Mental health analysis.",
                        }
                    ],
                },
            },
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="OpenAI mental health",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
            artifact_refs=[artifact],
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Search completed.",
    )

    notes = response_synthesis_source_context_notes(result)
    synthesis_input = _user_response_synthesis_input(
        result,
        user_request="What is OpenAI doing about mental health? Use deepened search.",
    )
    prompt = synthesis_input.to_prompt()

    assert notes == [
        (
            "Search provider URLs were discovered, but no selected source URLs or "
            "extracted page evidence reached final synthesis."
        )
    ]
    assert synthesis_input.source_context_notes == notes
    assert "no selected source URLs or extracted page evidence" in prompt
    assert "do not write a detailed factual synthesis from provider snippets alone" in prompt


def test_response_synthesis_distinguishes_snippet_only_source_context() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id="1",
        source_agent="business_research_analyst",
        title="OpenAI",
        summary="Snippet-backed source set.",
        metadata={
            "source_refs": [
                {
                    "title": "OpenAI mental health update",
                    "url": "https://openai.com/index/update-on-mental-health-related-work/",
                    "source_type": "search_result",
                    "supported_claim": "OpenAI describes mental-health-related safety work.",
                    "extraction_status": "snippet_only",
                }
            ],
            "retrieval_diagnostics": {
                "provider_summary": "searxng+exa",
                "provider_result_samples": {
                    "exa": [
                        {
                            "title": "OpenAI mental health update",
                            "url": (
                                "https://openai.com/index/update-on-mental-health-related-work/"
                            ),
                            "snippet": "OpenAI mental health safety work.",
                        }
                    ],
                },
            },
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.COMPANY_RESEARCH,
            title="OpenAI mental health",
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            artifact_refs=[artifact],
        ),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Search completed.",
    )

    sources = _user_response_synthesis_input(
        result,
        user_request="What is OpenAI doing about mental health?",
    ).sources
    notes = response_synthesis_source_context_notes(result)
    metadata_lines = response_synthesis_metadata_lines(result)

    assert sources[0]["extraction_status"] == "snippet_only"
    assert notes == [
        (
            "1 selected source URL(s) include claims or snippets, but no selected source "
            "is marked as extracted/read."
        )
    ]
    assert (
        "Source context: 0/1 selected URLs extracted/read; "
        "1/1 include evidence/claims; statuses: snippet_only"
    ) in metadata_lines


def test_format_user_response_synthesis_demotes_detailed_claims_from_snippet_only_sources() -> None:
    sources = [
        {
            "title": "OpenAI mental health update",
            "url": "https://openai.com/index/update-on-mental-health-related-work/",
            "source_type": "search_result",
            "supported_claim": "OpenAI describes mental-health-related safety work.",
            "extraction_status": "snippet_only",
        }
    ]

    rendered = format_user_response_synthesis(
        UserFacingResponseSynthesis(
            title="OpenAI mental health update",
            answer="OpenAI is actively shipping mental-health safeguards.",
            synthesis=(
                "OpenAI's full article explains clinician guidance, crisis routing, "
                "safety evaluations, and product roadmap details."
            ),
        ),
        sources=sources,
        metadata_lines=[
            (
                "Source context: 0/1 selected URLs extracted/read; "
                "1/1 include evidence/claims; statuses: snippet_only"
            )
        ],
    )

    assert "*Detailed Summary:*" in rendered
    assert "OpenAI update" not in rendered
    assert "full article explains clinician guidance" not in rendered
    assert "snippet-only source" in rendered
    assert "OpenAI describes mental-health-related safety work" in rendered
    assert "Source evidence" in rendered
    assert "Limitations" in rendered
    assert "Source extraction limitation" in rendered


def test_response_synthesis_warns_for_source_only_snippet_context() -> None:
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="OpenAI mental health",
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            sources=[
                WorkItemSourceRef(
                    title="OpenAI mental health update",
                    url="https://openai.com/index/update-on-mental-health-related-work/",
                    source_type="company_site",
                    supported_claim="OpenAI describes mental-health-related safety work.",
                    extraction_status="snippet_only",
                )
            ],
        ),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Source-linked summary.",
    )

    synthesis_input = _user_response_synthesis_input(
        result,
        user_request="Summarize the first source from the prior thread.",
    )
    metadata_lines = response_synthesis_metadata_lines(result)

    assert synthesis_input.provider_results == []
    assert synthesis_input.source_context_notes == [
        (
            "1 selected source URL(s) include claims or snippets, but no selected source "
            "is marked as extracted/read."
        )
    ]
    assert (
        "Source context: 0/1 selected URLs extracted/read; "
        "1/1 include evidence/claims; statuses: snippet_only"
    ) in metadata_lines


def test_response_synthesis_input_uses_context_pack_ordered_sources() -> None:
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="OpenAI mental health",
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        ),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Source-linked summary.",
        context_pack={
            "ordered_sources": [
                {
                    "index": 1,
                    "reference": "source 1",
                    "title": "OpenAI mental health update",
                    "url": "https://openai.com/index/update-on-mental-health-related-work/",
                    "source_type": "company_site",
                    "extraction_status": "article_read",
                    "supported_claim": ("OpenAI describes mental-health-related safety work."),
                    "evidence_excerpt": (
                        "OpenAI is improving responses in sensitive conversations."
                    ),
                }
            ]
        },
    )

    synthesis_input = _user_response_synthesis_input(
        result,
        user_request="Can u summarize link 1?",
    )
    prompt = synthesis_input.to_prompt()

    assert response_synthesis_ordered_sources(result) == [
        {
            "index": 1,
            "reference": "source 1",
            "title": "OpenAI mental health update",
            "url": "https://openai.com/index/update-on-mental-health-related-work/",
            "source_type": "company_site",
            "extraction_status": "article_read",
            "supported_claim": "OpenAI describes mental-health-related safety work.",
            "evidence_excerpt": "OpenAI is improving responses in sensitive conversations.",
        }
    ]
    assert synthesis_input.ordered_sources == response_synthesis_ordered_sources(result)
    assert "resolve N from ordered_sources" in prompt
    assert '"ordered_sources"' in prompt
    assert '"reference": "source 1"' in prompt


def test_response_synthesis_sources_include_context_pack_source_samples() -> None:
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="OpenAI mental health",
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        ),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Source-linked summary.",
        context_pack={
            "source_context_sample": [
                {
                    "title": "OpenAI mental health update",
                    "url": "https://openai.com/index/update-on-mental-health-related-work/",
                    "source_type": "company_site",
                    "extraction_status": "article_read",
                    "supported_claim": ("OpenAI describes mental-health-related safety work."),
                    "evidence_excerpt": (
                        "OpenAI says it is improving sensitive-conversation handling "
                        "and consulting external clinicians."
                    ),
                    "key_facts": ["The update is about ChatGPT safety, not a therapy product."],
                }
            ],
            "ordered_sources": [
                {
                    "index": 1,
                    "reference": "source 1",
                    "title": "OpenAI mental health update",
                    "url": "https://openai.com/index/update-on-mental-health-related-work/",
                    "source_type": "company_site",
                    "extraction_status": "article_read",
                    "supported_claim": ("OpenAI describes mental-health-related safety work."),
                    "evidence_excerpt": (
                        "OpenAI says it is improving sensitive-conversation handling."
                    ),
                }
            ],
        },
    )

    synthesis_input = _user_response_synthesis_input(
        result,
        user_request="Can u summarize link 1?",
    )
    metadata_lines = response_synthesis_metadata_lines(result)

    assert synthesis_input.sources == [
        {
            "title": "OpenAI mental health update",
            "url": "https://openai.com/index/update-on-mental-health-related-work/",
            "source_type": "company_site",
            "source_id": "",
            "supported_claim": "OpenAI describes mental-health-related safety work.",
            "extraction_status": "article_read",
            "evidence_excerpt": (
                "OpenAI says it is improving sensitive-conversation handling "
                "and consulting external clinicians."
            ),
            "key_facts": ["The update is about ChatGPT safety, not a therapy product."],
            "artifact_title": "",
        }
    ]
    assert synthesis_input.source_data_summaries == [
        (
            "OpenAI mental health update (article_read): OpenAI says it is "
            "improving sensitive-conversation handling and consulting external "
            "clinicians. OpenAI describes mental-health-related safety work. The "
            "update is about ChatGPT safety, not a therapy product. Source: "
            "https://openai.com/index/update-on-mental-health-related-work/"
        )
    ]
    assert synthesis_input.source_context_notes == [
        "1/1 selected source URL(s) are marked as extracted/read for synthesis."
    ]
    assert (
        "Source context: 1/1 selected URLs extracted/read; "
        "1/1 include evidence/claims; statuses: article_read"
    ) in metadata_lines


def test_format_user_response_synthesis_repairs_workflow_answer_from_context_pack_sources() -> None:
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="OpenAI mental health",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Search completed.",
        context_pack={
            "source_context_sample": [
                {
                    "title": "OpenAI mental health update",
                    "url": "https://openai.com/index/update-on-mental-health-related-work/",
                    "source_type": "company_site",
                    "extraction_status": "article_read",
                    "supported_claim": (
                        "OpenAI describes mental-health-related safety work for ChatGPT."
                    ),
                    "evidence_excerpt": (
                        "OpenAI says it is improving sensitive-conversation handling "
                        "and consulting external clinicians."
                    ),
                }
            ],
        },
    )

    rendered = format_user_response_synthesis(
        UserFacingResponseSynthesis(
            title="Business Agents WorkItem Advanced",
            answer="Search completed.",
            synthesis="",
        ),
        sources=response_synthesis_sources(result),
        metadata_lines=response_synthesis_metadata_lines(result),
    )

    assert "*Answer:*" in rendered
    assert "Business Agents WorkItem Advanced" not in rendered
    assert "Search completed." not in rendered
    assert "OpenAI says it is improving sensitive-conversation handling" in rendered
    assert "Source evidence" in rendered
    assert "https://openai.com/index/update-on-mental-health-related-work/" in rendered
    assert "Source context: 1/1 selected URLs extracted/read" in rendered


def test_response_synthesis_ordered_sources_falls_back_to_sources() -> None:
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="OpenAI mental health",
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            sources=[
                WorkItemSourceRef(
                    title="OpenAI mental health update",
                    url="https://openai.com/index/update-on-mental-health-related-work/",
                    source_type="company_site",
                    supported_claim="OpenAI describes mental-health-related safety work.",
                    extraction_status="snippet_only",
                )
            ],
        ),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Source-linked summary.",
    )

    ordered = response_synthesis_ordered_sources(result)

    assert ordered[0]["index"] == 1
    assert ordered[0]["reference"] == "source 1"
    assert ordered[0]["url"] == ("https://openai.com/index/update-on-mental-health-related-work/")


def test_response_synthesis_uses_context_pack_source_focus_without_retrieval_diagnostics() -> None:
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="OpenAI mental health",
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            sources=[
                WorkItemSourceRef(
                    title="General AI infrastructure update",
                    url="https://example.com/infrastructure",
                    source_type="web",
                    supported_claim="The source discusses cloud infrastructure partnerships.",
                    extraction_status="extracted",
                )
            ],
        ),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        human_summary="Source-linked summary.",
        context_pack={
            "source_context_focus": {
                "status": "no_sample_source_matches_focus",
                "terms": ["mental", "health"],
                "sample_count": 1,
                "matching_sample_count": 0,
                "matching_urls": [],
            }
        },
    )

    synthesis_input = _user_response_synthesis_input(
        result,
        user_request="What is OpenAI doing about mental health?",
    )
    metadata_lines = response_synthesis_metadata_lines(result)

    assert synthesis_input.source_context_notes[0] == (
        "Sampled source evidence did not match the request focus terms "
        "(mental, health); do not treat broad sources as satisfying the focused "
        "research ask."
    )
    assert (
        "Source focus: no_sample_source_matches_focus; 0/1 sample sources matched; terms: mental, health"
        in metadata_lines
    )


def test_response_synthesis_hides_stale_empty_focus_after_context_evidence_arrives() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="chief_context_evidence",
        artifact_id="context-1",
        source_agent=WorkItemRoute.CHIEF_OF_STAFF.value,
        title="Chief context evidence",
        metadata={
            "complete": True,
            "source_refs": [
                {
                    "title": "Gmail message summary",
                    "source_type": "gmail",
                    "source_id": "message-1",
                    "supported_claim": "A provider-backed Gmail message was read.",
                    "extraction_status": "read",
                }
            ],
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.RESEARCH_BRIEF,
            title="Chief context review",
            current_route=WorkItemRoute.CHIEF_OF_STAFF,
            artifact_refs=[artifact],
        ),
        route=WorkItemRoute.CHIEF_OF_STAFF,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Provider-backed review.",
        context_pack={
            "source_context_focus": {
                "status": "no_source_context_sample",
                "terms": [],
                "sample_count": 0,
                "matching_sample_count": 0,
                "matching_urls": [],
            }
        },
    )

    metadata_lines = response_synthesis_metadata_lines(result)

    assert not any(
        "Source focus: no_source_context_sample" in line for line in metadata_lines
    )


def test_response_synthesis_warns_when_selected_sources_miss_request_focus() -> None:
    artifact = WorkItemArtifactRef(
        artifact_type="research_brief",
        artifact_id="1",
        source_agent="business_research_analyst",
        title="OpenAI",
        summary="Broad OpenAI source set.",
        metadata={
            "source_refs": [
                {
                    "title": "OpenAI and Amazon announce strategic partnership",
                    "url": "https://www.businesswire.com/news/home/openai-amazon-partnership",
                    "source_type": "news",
                    "supported_claim": "OpenAI expands enterprise AI infrastructure with AWS.",
                }
            ],
            "retrieval_diagnostics": {
                "provider_summary": "searxng+exa",
                "source_focus": {
                    "status": "no_selected_source_matches_focus",
                    "terms": ["mental", "health"],
                    "selected_source_count": 1,
                    "matching_source_count": 0,
                    "top_selected_urls": [
                        "https://www.businesswire.com/news/home/openai-amazon-partnership"
                    ],
                },
                "provider_result_samples": {
                    "exa": [
                        {
                            "title": "Update on mental-health-related work",
                            "url": (
                                "https://openai.com/index/update-on-mental-health-related-work/"
                            ),
                            "snippet": "OpenAI mental health safety work.",
                        }
                    ],
                },
            },
        },
    )
    result = WorkflowRunResult(
        work_item=WorkItem(
            kind=WorkItemKind.COMPANY_RESEARCH,
            title="OpenAI mental health",
            current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            artifact_refs=[artifact],
        ),
        route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        status=WorkItemStatus.DONE,
        advanced=True,
        artifact_refs=[artifact],
        human_summary="Search completed.",
    )

    notes = response_synthesis_source_context_notes(result)
    prompt = _user_response_synthesis_input(
        result,
        user_request="What is OpenAI doing about mental health?",
    ).to_prompt()

    assert any("did not match the request focus terms (mental, health)" in note for note in notes)
    assert "do not treat broad sources as satisfying the focused research ask" in prompt
