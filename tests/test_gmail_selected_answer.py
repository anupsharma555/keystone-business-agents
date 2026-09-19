"""Provider-owned links reach the visible answer before output validation."""

from types import SimpleNamespace

import pytest

import scripts.run_gmail_triage as script
from keystone_agents.instruction_following import resolve_instruction_following_response
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.planning.compatibility import infer_manual_request_plan
from keystone_agents.presentation.renderers import (
    render_gmail_selected_answer,
    render_gmail_triage_report,
)
from keystone_agents.schemas.email_triage import EmailTriageResult
from keystone_agents.schemas.gmail_query import GmailReadContextResult

SOURCE = "https://mail.google.com/mail/?authuser=reader%40example.test#all/selected"


def selected_result():
    return EmailTriageResult(
        thread_id="selected", subject="Software workshop", sender_email="publisher@example.test",
        category="newsletter", confidence=0.9, summary="The workshop is in October.",
        reasoning="The selected email supplies the details.",
        recommended_action="Review the event.",
        decision={
            "decision_owner": "specialist_agent", "decision_stage": "gmail_candidate_selection",
            "selected_candidate_id": "selected", "selected_candidate_ids": ["selected"],
            "candidate_assessments": [{"candidate_id": "selected", "disposition": "selected",
                                       "rationale": "The matching thread was read."}],
            "reasoning": "The matching thread was read.",
        },
    )


def source_context():
    return GmailReadContextResult(
        status="read", resource_type="thread", resource_id="selected", source_url=SOURCE,
        provider_read_performed=True,
    )


@pytest.mark.parametrize("with_source", [False, True])
def test_direct_synthesis_uses_selected_provider_link_without_paid_repair(monkeypatch, with_source):
    output = selected_result()
    def run(_input, **kwargs):
        if with_source:
            kwargs["selected_context_callback"](source_context())
        return TypedAgentRunResult(
            agent_name="gmail_triage", output=output, raw_result=None, live=False,
        )
    monkeypatch.setattr(script, "run_gmail_triage_sdk", run)
    monkeypatch.setattr(script, "load_specialist_execution_context_from_env", lambda: {})
    request = "Find the workshop email and include its Gmail link."
    args = script.build_parser().parse_args(["--request", request, "--json"])
    args.save = False
    payload = script._run_agent_owned_live_gmail_synthesis(
        args, run_config=SimpleNamespace(model="synthetic"), live=False,
        style_context="", founder_context="", preflight_context="",
    )
    text = payload["public_result"]["text"]
    assert output.summary in text
    assert (SOURCE in text) is with_source
    resolution = resolve_instruction_following_response(
        text, original_request=request,
        manual_plan=infer_manual_request_plan(request), live=False,
    )
    assert resolution.validation.passed is with_source
    assert not resolution.repair_attempted
    assert payload["output"]["summary"] == output.summary


def test_selected_answer_preserves_draft_and_deduplicates_only_exact_source():
    output = selected_result().model_copy(update={"draft_reply": "Hello, could we compare notes?"})
    rendered = render_gmail_selected_answer(output, selected_context=source_context())
    assert output.draft_reply in rendered and SOURCE in rendered
    output = output.model_copy(update={"summary": f"Other source: {SOURCE}-other"})
    rendered = render_gmail_selected_answer(output, selected_context=source_context())
    assert f"Source: {SOURCE}\n" in rendered + "\n"
    output = output.model_copy(update={"summary": f"Verified source: <{SOURCE}>"})
    rendered = render_gmail_selected_answer(output, selected_context=source_context())
    assert rendered.count(SOURCE) == 1


def test_operator_answer_reaches_visible_output_instead_of_neutral_summary():
    answer = (
        "You asked to join. There is no later reply in the three-message timeline; wait for now."
    )
    output = selected_result().model_copy(update={"operator_answer": answer})
    rendered = render_gmail_selected_answer(output, selected_context=source_context())
    assert rendered.startswith(answer) and SOURCE in rendered
    assert output.summary not in rendered
    report = render_gmail_triage_report({
        **output.model_dump(), "thread_summary_result": {"summary": "Neutral context only"},
    })
    assert answer in report


def test_unresolved_repair_drops_stale_operator_answer():
    from keystone_agents.agents.gmail_triage import _unresolved_gmail_repair_result
    from keystone_agents.schemas.email_triage import GmailNeedsMoreContextRepair
    initial = selected_result().model_copy(update={"operator_answer": "A stale answer."})
    output = _unresolved_gmail_repair_result(initial, GmailNeedsMoreContextRepair(
        reasoning="No matching conversation was verified.",
        requested_context=["Identify the intended conversation."],
    ))
    assert output.operator_answer == ""
    assert render_gmail_selected_answer(output) == ""
