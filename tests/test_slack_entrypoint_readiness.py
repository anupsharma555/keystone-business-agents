import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from keystone_agents import cli
from keystone_agents.slack_entrypoint_readiness import (
    SLACK_ENTRYPOINT_READINESS_CASES,
    SlackEntrypointReadinessCase,
)


def test_slack_entrypoint_readiness_has_existing_proofs_and_constraint_probe() -> None:
    assert [case.probe_id for case in SLACK_ENTRYPOINT_READINESS_CASES] == [
        "SLACK-DIRECT-01",
        "SLACK-GRAPH-01",
        "SLACK-DIRECT-ZOTERO-01",
        "SLACK-DIRECT-CONSTRAINT-01",
    ]
    assert [case.backend for case in SLACK_ENTRYPOINT_READINESS_CASES] == [
        "direct_specialist",
        "langgraph",
        "direct_specialist",
        "direct_specialist",
    ]
    assert [case.live_slack_evidence_proven for case in SLACK_ENTRYPOINT_READINESS_CASES] == [
        True,
        True,
        True,
        True,
    ]


def test_slack_entrypoint_readiness_requires_visible_answer_trace_and_safety_evidence() -> None:
    evidence = {
        item
        for case in SLACK_ENTRYPOINT_READINESS_CASES
        for item in case.required_visible_evidence
    }
    assert "slack_permalink" in evidence
    assert "answer_first_human_summary" in evidence
    assert "visible_source_urls_or_source_limit" in evidence
    assert "selected_gmail_identity_without_raw_body" in evidence
    assert "gmail_research_outreach_node_path" in evidence
    assert "one_final_response" in evidence
    assert "usage_trace_and_cost_receipt" in evidence
    assert "no_search_draft_send_or_external_write" in evidence
    for case in (
        item for item in SLACK_ENTRYPOINT_READINESS_CASES if item.live_slack_evidence_proven
    ):
        assert any(
            ref.startswith("slack_permalink:evidence:") for ref in case.live_evidence_refs
        )
        assert any(
            ref.startswith(("run:", "workitem:")) for ref in case.live_evidence_refs
        )
        assert "review:pass" in case.live_evidence_refs
        assert any(ref.startswith("usage:requests=") for ref in case.live_evidence_refs)
        assert any(ref.startswith("cost:estimated_usd=") for ref in case.live_evidence_refs)
        assert "safety:no_external_side_effects" in case.live_evidence_refs


def test_live_slack_proof_flag_fails_closed_without_inspectable_refs() -> None:
    with pytest.raises(ValueError, match="inspectable refs"):
        SlackEntrypointReadinessCase(
            probe_id="invalid",
            title="Invalid",
            backend="direct_specialist",
            prompt="Review this.",
            proof_nodeids=("tests/test_slack_entrypoint_readiness.py::test_invalid",),
            required_visible_evidence=("slack_permalink",),
            max_openai_requests=1,
            max_cost_usd=0.01,
            live_slack_evidence_proven=True,
        )


def test_live_slack_evidence_refs_resolve_to_documented_permalink_sections() -> None:
    for case in (
        item for item in SLACK_ENTRYPOINT_READINESS_CASES if item.live_slack_evidence_proven
    ):
        ref = next(
            item
            for item in case.live_evidence_refs
            if item.startswith("slack_permalink:evidence:")
        )
        path_value, separator, anchor = ref.removeprefix(
            "slack_permalink:evidence:"
        ).partition("#")
        assert separator and anchor
        text = Path(path_value).read_text(encoding="utf-8")

        def normalize(value: str) -> str:
            return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())

        heading = normalize("### " + anchor)
        normalized_lines = [normalize(line) for line in text.splitlines()]
        index = normalized_lines.index(heading)
        section_lines: list[str] = []
        for line in text.splitlines()[index + 1 :]:
            if line.startswith("### "):
                break
            section_lines.append(line)
        section = "\n".join(section_lines)
        assert "Slack permalink: https://" in section
        assert "Result: pass" in section


def test_slack_entrypoint_readiness_budget_is_bounded() -> None:
    assert sum(case.max_openai_requests for case in SLACK_ENTRYPOINT_READINESS_CASES) == 18
    assert sum(case.max_cost_usd for case in SLACK_ENTRYPOINT_READINESS_CASES) == 0.80


def test_constraint_probe_preserves_exact_answer_and_source_boundary() -> None:
    case = next(
        item
        for item in SLACK_ENTRYPOINT_READINESS_CASES
        if item.probe_id == "SLACK-DIRECT-CONSTRAINT-01"
    )

    assert case.prompt == "@KNI BA, who is Abridge and summarize the company in 20 words."
    assert case.live_slack_evidence_proven is True
    assert "llm_interpreted_exact_20_word_answer_contract" in case.required_visible_evidence
    assert "exact_20_word_visible_answer" in case.required_visible_evidence
    assert "source_visibility_covered_by_direct_research_probe" in case.required_visible_evidence
    assert "no_generic_detailed_summary_expansion" in case.required_visible_evidence


def test_constraint_probe_request_ceiling_covers_conditional_llm_repair() -> None:
    case = next(
        item
        for item in SLACK_ENTRYPOINT_READINESS_CASES
        if item.probe_id == "SLACK-DIRECT-CONSTRAINT-01"
    )
    estimate = cli._estimate_ask_openai_requests(
        SimpleNamespace(
            context_file="",
            agent=None,
            max_manager_steps=3,
            live_search=True,
        ),
        input_text="who is Abridge and summarize the company in 20 words.",
        live_sdk=True,
        live_manual_plan=True,
        requested_route="business_research_analyst",
    )

    assert estimate["min"] == 2
    assert estimate["max"] == case.max_openai_requests == 4
    assert "conditional_instruction_following_repair" in estimate["stages"]


def test_graph_probe_asks_for_output_shape_without_repeating_safety_boilerplate() -> None:
    graph = next(
        case for case in SLACK_ENTRYPOINT_READINESS_CASES if case.probe_id == "SLACK-GRAPH-01"
    )
    normalized = graph.prompt.lower()
    assert "current conversation state" in normalized
    assert "kni-specific collaboration next step" in normalized
    assert "reply only if" in normalized
    assert "approval status that actually applies" in normalized
    for redundant_phrase in (
        "do not send",
        "without sending",
        "posting elsewhere",
        "writing externally",
        "do not draft outreach",
    ):
        assert redundant_phrase not in normalized


def test_slack_entrypoint_proof_nodeids_exist() -> None:
    for case in SLACK_ENTRYPOINT_READINESS_CASES:
        for nodeid in case.proof_nodeids:
            path_value, separator, test_name = nodeid.partition("::")
            assert separator and test_name.startswith("test_")
            path = Path(path_value)
            assert path.is_file(), nodeid
            assert f"def {test_name}(" in path.read_text(encoding="utf-8"), nodeid


def test_slack_entrypoint_live_plan_preserves_budget_and_visible_evidence_boundary() -> None:
    text = " ".join(Path("docs/SLACK_ENTRYPOINT_LIVE_PLAN.md").read_text().split())
    for marker in (
        "3 OpenAI requests and $0.10",
        "8 OpenAI requests and $0.50",
        "3 OpenAI requests and $0.05",
        "4 OpenAI requests and $0.15",
        "18 OpenAI requests and $0.80",
        "Slack permalink and local run/WorkItem ID",
        "Answer-first visible body",
        "One final response per probe",
        "Usage, trace, request count, retry count, and cost receipt",
        "Fixture and no-live readiness evidence cannot be substituted",
    ):
        assert marker in text


def test_package_exposes_slack_entrypoint_no_live_gate() -> None:
    package = Path("package.json").read_text(encoding="utf-8")
    assert '"test:slack-entrypoints:no-live"' in package
    assert "scripts/run_slack_entrypoint_readiness.py" in package


def test_slack_entrypoint_runner_reports_live_count_from_case_state() -> None:
    script = Path("scripts/run_slack_entrypoint_readiness.py").read_text()
    assert "live_count = sum(case.live_slack_evidence_proven" in script
    assert "{live_count}/{len(SLACK_ENTRYPOINT_READINESS_CASES)}" in script
