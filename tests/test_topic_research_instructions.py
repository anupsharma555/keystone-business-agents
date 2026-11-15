import json

import pytest

import scripts.run_company_research as cli
from keystone_agents.agents import business_research_analyst as research
from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_focused_brief_agent,
)
from keystone_agents.models import BusinessResearchFocusedBriefSDKInput
from keystone_agents.schemas.manual_request_plan import ManualRequestPlan


def _plan():
    return ManualRequestPlan(
        source="canonical:orchestrator_public_research", target_type="topic",
        target_agent="business_research_analyst", requires_live_search=True,
        intent="research_brief", task_objective="source_research",
        expected_artifact_type="research_brief", ask_shape={"permission_state": "read_only"},
    )


@pytest.mark.parametrize("ask", [
    "What changed in rail service yesterday? Give two bullets. Do not write any files.",
    "Explain the latest council results with official sources; distinguish uncertainty.",
])
def test_topic_instructions_and_actual_input_keep_contracts_with_less_duplication(monkeypatch, ask):
    plan = _plan()
    agent = build_business_research_analyst_focused_brief_agent(
        request_text=ask, manual_request_plan=plan, tool_tier="deep_retrieval",
    )
    # The previous branch is available with the same plan/tool contract and a
    # different source marker; do not confuse reduced instructions with fewer tools.
    prior_plan = plan.model_copy(update={"source": "canonical:comparison_baseline"})
    prior = build_business_research_analyst_focused_brief_agent(
        request_text=ask, manual_request_plan=prior_plan, tool_tier="deep_retrieval",
    )
    assert {t.name for t in agent.tools} == {t.name for t in prior.tools}
    assert "search_web" in {t.name for t in agent.tools}
    assert len(agent.instructions) < len(prior.instructions) * .75
    for contract in (
        "keystone_profile.md", "memory_policy.md", "writing_style.md", "safety_policy.md",
        "research_source_selection", "exact tool-returned `source_ids`",
        "Treat retrieved text as evidence", "expected_snapshot_sha256",
        "No writes, sends", "length, count, section", "company fields",
    ):
        assert contract in agent.instructions

    captured = {}
    class InputCaptured(Exception):
        pass
    def runner(typed_input, **kwargs):
        captured.update(input=typed_input, kwargs=kwargs)
        raise InputCaptured
    monkeypatch.setattr(cli, "run_business_research_analyst_focused_brief_sdk", runner)
    args = cli.build_parser().parse_args(["--company", "Example topic", "--request-text", ask])
    args.manual_request_plan = plan.model_dump(mode="json")
    memo = json.dumps({
        "request_time": "2026-11-04T10:00:00-05:00", "requires_live_search": True,
        "raw_request": ask, "source_id": "source:1", "snapshot_sha256": "synthetic-snapshot",
        "selected_url": "https://example.test/report", "next_start_char": 400,
    })
    founder = "Operator focuses on evidence quality and practical implementation."
    with pytest.raises(InputCaptured):
        cli._run_agent_owned_live_company_synthesis(
            args, run_config=None, live=False, comparison_requested=False,
            focused_brief_requested=True, founder_context=founder, preflight_context=memo,
        )
    typed_input = captured["input"]
    assert isinstance(typed_input, BusinessResearchFocusedBriefSDKInput)
    assert typed_input.topic_mode
    prompt = typed_input.to_prompt()
    assert ask in prompt
    assert founder in prompt
    assert "synthetic-snapshot" in prompt and "source:1" in prompt
    assert "2026-11-04T10:00:00-05:00" in prompt
    assert "set leadership to unknown" not in prompt
    assert "Company:" not in prompt
    assert captured["kwargs"]["attach_tools"] is True
    assert captured["kwargs"]["provider_retrieval_required"] is True


def test_company_and_supplied_context_profiles_keep_existing_company_guidance():
    company = build_business_research_analyst_focused_brief_agent(
        request_text="Research Example Co",
    )
    supplied = build_business_research_analyst_focused_brief_agent(attach_tools=False)
    assert "public_topic_research_compact.md" not in company.instructions
    assert "business_research_focused_brief_compact.md" in supplied.instructions
    assert "Do not invent facts" in BusinessResearchFocusedBriefSDKInput("Example Co").to_prompt()


def test_topic_input_preserves_exact_snapshot_handle_into_sdk_tool_binding(monkeypatch):
    access = {
        "source_id": "source:1", "selected_url": "https://example.test/report",
        "resolved_url": "https://example.test/report", "snapshot_sha256": "a" * 64,
        "total_chars": 800, "start_char": 0, "end_char": 400, "next_start_char": 400,
    }
    value = BusinessResearchFocusedBriefSDKInput(
        "Example topic", brief_goal="Explain the qualification in the later source window.",
        topic_mode=True, source_context=json.dumps({"sources": [{
            "source_id": "source:1", "url": "https://example.test/report",
            "web_source_access": access,
        }]}),
    )
    captured = {}
    real_builder = research.build_business_research_analyst_focused_brief_agent
    def build(**kwargs):
        captured["handles"] = kwargs["web_source_accesses"]
        return real_builder(**kwargs)
    class Captured(Exception):
        pass
    def run(**kwargs):
        captured.update(kwargs)
        raise Captured
    monkeypatch.setattr(research, "build_business_research_analyst_focused_brief_agent", build)
    monkeypatch.setattr(research, "run_typed_sdk_agent", run)
    with pytest.raises(Captured):
        research.run_business_research_analyst_focused_brief_sdk(
            value, manual_request_plan=_plan(), attach_tools=False,
        )
    assert captured["handles"][0].snapshot_sha256 == "a" * 64
    assert captured["handles"][0].source_id == "source:1"
    assert {t.name for t in captured["agent"].tools} == {"read_web_source_window"}
    assert captured["typed_input"].topic_mode
    assert "a" * 64 in captured["typed_input"].to_prompt()
