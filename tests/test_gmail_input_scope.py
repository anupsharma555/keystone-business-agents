"""Advisory metadata stays visible without activating unrelated procedures."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from keystone_agents.agents import gmail_triage as gmail
from keystone_agents.models import GmailTriageSDKInput
from keystone_agents.runtime.request_budget import activate_model_request_budget


@pytest.mark.parametrize("compact", [False, True])
def test_gmail_task_selects_skills_while_advisory_context_remains_visible(monkeypatch, compact):
    captured = []

    class CapturedBeforeModel(Exception):
        pass

    def capture(**kwargs):
        captured.append(kwargs)
        raise CapturedBeforeModel

    monkeypatch.setattr(gmail, "run_typed_sdk_agent", capture)
    task = "Find the email about the software workshop and summarize its details."
    base = GmailTriageSDKInput(subject="", body="", request=task)
    enriched = replace(
        base,
        advisory_context="Prior task: inspect this workshop. Audit: artifacts, approvals, handoff.",
        email_style_profile="For approved outbound work, write concise drafts.",
        founder_fit_context="Background context for relevance assessment.",
    )
    for value in (base, enriched):
        with pytest.raises(CapturedBeforeModel):
            gmail.run_gmail_triage_sdk(value, compact_instructions=compact)
    assert captured[0]["agent"].instructions == captured[1]["agent"].instructions
    prompt = captured[1]["typed_input"].to_prompt()
    assert task in prompt
    assert enriched.advisory_context in prompt
    assert enriched.email_style_profile in prompt
    assert enriched.founder_fit_context in prompt
    assert {tool.name for tool in captured[1]["agent"].tools} == {
        "query_gmail_message_summaries", "read_gmail_context",
    }


@pytest.mark.parametrize("selection", [False, True])
def test_mini_search_selection_has_explicit_low_reasoning(selection, monkeypatch):
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL", "gpt-5.4-mini")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER", "openai")
    agent = gmail.build_gmail_triage_agent(provider_selection_mode=selection)
    reasoning = agent.model_settings.reasoning
    assert (reasoning.effort if reasoning else None) == ("low" if selection else None)


def test_search_reasoning_default_does_not_change_other_providers(monkeypatch):
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL_PROVIDER", "gemini")
    monkeypatch.setenv("KEYSTONE_GMAIL_TRIAGE_MODEL", "gemini-2.5-flash")
    agent = gmail.build_gmail_triage_agent(provider_selection_mode=True)
    assert agent.model_settings.reasoning is None


@pytest.mark.parametrize(
    ("operator_request", "query_hint"),
    [
        (
            'Find the email with the quoted subject "Quarterly outcomes update".',
            'subject:"Quarterly outcomes update"',
        ),
        (
            "Find the Quarterly outcomes update subject.",
            "subject:(Quarterly outcomes update)",
        ),
        (
            "Find Alex's partnership note.",
            "from:alex@example.test partnership",
        ),
        (
            "Find the ORBIT pilot note received after September 1.",
            "after:2026/09/01 ORBIT pilot",
        ),
        (
            "Find the archived outcomes note.",
            'subject:"Archived outcomes note"',
        ),
    ],
)
def test_gmail_query_hint_is_typed_advice_without_replacing_raw_request(
    operator_request: str,
    query_hint: str,
) -> None:
    prompt = GmailTriageSDKInput(
        subject="",
        body="",
        request=operator_request,
        gmail_query_hint=query_hint,
    ).to_prompt()

    assert operator_request in prompt
    assert query_hint in prompt
    assert "not authority" in prompt
    assert prompt.index(operator_request) < prompt.index(query_hint)
    if "archived" in operator_request.lower():
        assert "INBOX" not in query_hint


def test_gmail_runner_exposes_enforced_capacity_to_direct_and_nested_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[GmailTriageSDKInput] = []

    class CapturedBeforeModel(Exception):
        pass

    def capture(**kwargs):
        captured.append(kwargs["typed_input"])
        raise CapturedBeforeModel

    monkeypatch.setattr(gmail, "run_typed_sdk_agent", capture)
    with activate_model_request_budget(3):
        with pytest.raises(CapturedBeforeModel):
            gmail.run_gmail_triage_sdk(
                GmailTriageSDKInput(
                    subject="",
                    body="",
                    request="Find the exact message and summarize it.",
                ),
                provider_selection_required=True,
            )
        with pytest.raises(CapturedBeforeModel):
            gmail.run_gmail_triage_sdk(
                "Chief-routed Gmail request with no preselected identity.",
                provider_selection_required=True,
            )

    assert len(captured) == 2
    for typed_input in captured:
        assert typed_input.model_request_capacity is not None
        assert typed_input.model_request_capacity.limit == 3
        assert typed_input.model_request_capacity.remaining == 3
        prompt = typed_input.to_prompt()
        assert "reserve one request for the final typed response" in prompt
        assert "corrective query" in prompt


def test_agent_owned_cli_carries_planned_query_as_typed_advice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.run_gmail_triage as cli

    observed: dict[str, GmailTriageSDKInput] = {}

    class CapturedBeforeModel(Exception):
        pass

    def capture(typed_input, **_kwargs):
        observed["typed_input"] = typed_input
        raise CapturedBeforeModel

    monkeypatch.setattr(cli, "run_gmail_triage_sdk", capture)
    args = SimpleNamespace(
        request="Find the Example Health ORBIT pilot announcement.",
        gmail_query='subject:"Example Health joined the ORBIT pilot"',
        compact_instructions=False,
    )

    with pytest.raises(CapturedBeforeModel):
        cli._run_agent_owned_live_gmail_synthesis(
            args,
            run_config=None,
            live=True,
            style_context="",
            founder_context="",
            preflight_context="",
        )

    typed_input = observed["typed_input"]
    assert typed_input.request == args.request
    assert typed_input.gmail_query_hint == args.gmail_query
    assert args.request in typed_input.to_prompt()
    assert args.gmail_query in typed_input.to_prompt()
