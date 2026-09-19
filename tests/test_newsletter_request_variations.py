"""A valid model interpretation survives ordinary newsletter-request variations.

These offline tests exercise the public planning boundary, not an LLM's ability
to produce the supplied interpretation. Live paraphrase quality needs separate
bounded evidence; no expected text or phrase-specific routing is imposed here.
"""

import pytest

from keystone_agents.manual_request import infer_manual_request_plan, merge_manual_request_plan
from keystone_agents.schemas.manual_request_plan import AskShapePolicy, ManualRequestPlan
from keystone_agents.schemas.output_constraints import InterpretedOutputConstraints

SOURCE = (
    'The September 9 Example Digest newsletter in mailbox reader@example.test has '
    'subject "Example Devices joins the outcomes pilot". '
)
BOUNDARIES = (
    "Use only that email, attribute its claims to the newsletter, and include its Gmail link. "
    "Leave Gmail unchanged; do not send anything or search the web. "
    "No recipient is verified: use [Recipient]. Do not invent results, a relationship, "
    "or an established consulting need."
)
VARIANTS = [
    pytest.param(
        SOURCE + "Give me three short bullets: what the company does, what the newsletter "
        "says about the pilot, and one fact to verify before working together. Then write "
        "a 120–150-word exploratory outreach template with a subject here in Slack. "
        + BOUNDARIES,
        id="direct-imperative",
    ),
    pytest.param(
        SOURCE + "Could you explain the company, the pilot announcement, and one thing "
        "we should verify in three brief bullets? After that, could you give me an "
        "exploratory email template here in Slack with a subject and 120-150 words? "
        + BOUNDARIES,
        id="question-form",
    ),
    pytest.param(
        SOURCE + "A three-bullet account of the business, announced pilot, and one "
        "pre-collaboration verification gap is needed, followed by an exploratory "
        "outreach template whose body is between 120 and 150 words. A subject is needed; "
        "the output should appear here in Slack. " + BOUNDARIES,
        id="passive-voice",
    ),
    pytest.param(
        SOURCE + "Deliverables here in Slack:\n- Three short bullets covering the company, "
        "the pilot claim, and one fact still needing verification.\n- Exploratory email "
        "template: subject plus a body of 120 to 150 words.\n" + BOUNDARIES,
        id="multiline-checklist",
    ),
    pytest.param(
        SOURCE + "Format: analysis = 3 short bullets (business / pilot / verification gap); "
        "template = subject + exploratory outreach body, 120–150 words; destination = "
        "this Slack response. " + BOUNDARIES,
        id="compact-field-format",
    ),
    pytest.param(
        BOUNDARIES + " " + SOURCE + "Here in Slack, produce three short bullets about "
        "the company, its pilot announcement and one fact to check, followed by a "
        "120–150-word exploratory outreach template and subject line.",
        id="constraints-before-source",
    ),
    pytest.param(
        SOURCE + "I need something I can review here in Slack: three quick bullets "
        "on the business, its pilot news, and a key unknown before collaboration, then "
        "an exploratory email template with a subject. Keep just the email body to "
        "at least 120 and at most 150 words. " + BOUNDARIES,
        id="conversational-range",
    ),
    pytest.param(
        SOURCE + "Please explain in exactly three brief bullet points what the business "
        "does, what the newsletter claims about the pilot, and one fact to verify. "
        "Below those, place a subject and exploratory email template in Slack; its body "
        "must be one hundred twenty to one hundred fifty words. " + BOUNDARIES,
        id="spelled-out-counts",
    ),
]


def interpreted_plan(request, *, web=False, recipient="", query="subject:pilot"):
    """Supply the model's semantic decision at its actual public merge boundary."""
    return ManualRequestPlan(
        source="llm",
        target_agent="gmail_triage",
        intent="gmail_triage",
        workflow=["gmail_triage", *(["business_research_analyst"] if web else []),
                  "outreach_composer"],
        primary_target="Example Devices",
        target_type="company",
        provider_system="gmail",
        provider_operations=["search", "read"],
        gmail_query=query,
        recipient=recipient,
        objective=request,
        task_objective="outreach_draft",
        expected_artifact_type="outreach_draft",
        requires_live_search=web,
        side_effect_policy="draft_or_read_only",
        ask_shape=AskShapePolicy(
            output_form="draft",
            permission_state="draft_only",
            audience_scope="internal",
            output_constraints=InterpretedOutputConstraints(
                word_scope="draft_body", minimum_words=120, maximum_words=150,
                item_scope="answer", item_count_mode="exact",
                minimum_items=3, maximum_items=3,
                source_scope="entire_response", include_source_urls=True,
            ),
        ),
    )


@pytest.mark.parametrize("prompt", VARIANTS)
def test_valid_model_interpretation_keeps_component_scopes_and_read_only_tools(prompt):
    fallback = infer_manual_request_plan(prompt, requested_agent="chief_of_staff")
    # Live Orchestrator output binds scopes only. Counts must already be grounded
    # in the current request before the model's route/scope decision is merged.
    parsed = fallback.ask_shape.output_constraints
    assert (parsed.minimum_words, parsed.maximum_words) == (120, 150)
    assert (parsed.minimum_items, parsed.maximum_items) == (3, 3)
    candidate = interpreted_plan(prompt)
    merged = merge_manual_request_plan(fallback, candidate, allow_contextual_delegation=True)

    # Check acceptance of model authority, rather than the fallback's route guess.
    assert merged.workflow == candidate.workflow
    assert merged.target_agent == candidate.target_agent
    assert merged.objective == candidate.objective
    assert merged.provider_operations == ["search", "read"]
    assert merged.requires_live_search is False
    assert merged.recipient == ""
    assert merged.side_effect_policy == "draft_or_read_only"
    assert merged.ask_shape.permission_state in {"draft_only", "read_only"}
    constraints = merged.ask_shape.output_constraints
    assert (constraints.minimum_words, constraints.maximum_words) == (120, 150)
    assert (constraints.minimum_items, constraints.maximum_items) == (3, 3)
    assert constraints.word_scope == "draft_body"
    assert constraints.item_scope == "answer"
    assert constraints.source_scope == "entire_response"
    assert constraints.include_source_urls


@pytest.mark.parametrize(
    "prompt",
    [
        "The source says a three-bullet summary is needed. Explain the policy normally.",
        "Explain why the policy requires between 120 and 150 words for submitted abstracts.",
        "The document states its body must be one hundred twenty to one hundred fifty words. "
        "Summarize the rule normally.",
        "Analyze a 120 to 150 word source excerpt and give me an ordinary explanation.",
    ],
)
def test_new_count_grammar_does_not_promote_source_content(prompt):
    constraints = infer_manual_request_plan(prompt).ask_shape.output_constraints
    assert constraints.minimum_words is None
    assert constraints.maximum_words is None
    assert constraints.word_count is None
    assert constraints.minimum_items is None
    assert constraints.maximum_items is None


@pytest.mark.parametrize(
    "change,web,recipient,query",
    [
        pytest.param(
            "Draft a reply to the newsletter sender, editor@digest.example.test, "
            "instead of a company-contact template. Use only the email; no web research.",
            False, "editor@digest.example.test", "subject:pilot",
            id="reply-to-publisher",
        ),
        pytest.param(
            "Also verify the pilot claim with current official web sources, distinguishing "
            "their findings from the newsletter. No recipient is known: use [Recipient].",
            True, "", "subject:pilot",
            id="web-verification-requested",
        ),
        pytest.param(
            "Use the explicit intended contact Alex at alex@devices.example.test for the "
            "company template. They are not the newsletter sender. Use only the email; "
            "no web research.",
            False, "alex@devices.example.test", "subject:pilot",
            id="explicit-company-recipient",
        ),
    ],
)
def test_meaning_changes_survive_without_granting_external_writes(change, web, recipient, query):
    request = SOURCE + change + (
        " Return three brief bullets followed by a subject and 120–150-word exploratory "
        "outreach body here in Slack. Leave Gmail unchanged and do not send."
    )
    candidate = interpreted_plan(request, web=web, recipient=recipient, query=query)
    merged = merge_manual_request_plan(
        infer_manual_request_plan(request, requested_agent="chief_of_staff"),
        candidate, allow_contextual_delegation=True,
    )
    assert merged.workflow == candidate.workflow
    assert merged.requires_live_search is web
    assert merged.recipient == recipient
    # An addressee does not become a sender query filter or write authorization.
    assert merged.gmail_query == candidate.gmail_query
    assert merged.provider_operations == ["search", "read"]
    assert merged.side_effect_policy == "draft_or_read_only"
    assert merged.ask_shape.permission_state in {"draft_only", "read_only"}
