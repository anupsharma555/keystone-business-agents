from pathlib import Path

from keystone_agents.slack_entrypoint_readiness import SLACK_ENTRYPOINT_READINESS_CASES


def test_slack_entrypoint_readiness_has_exact_direct_and_graph_probes() -> None:
    assert [case.probe_id for case in SLACK_ENTRYPOINT_READINESS_CASES] == [
        "SLACK-DIRECT-01",
        "SLACK-GRAPH-01",
    ]
    assert [case.backend for case in SLACK_ENTRYPOINT_READINESS_CASES] == [
        "direct_specialist",
        "langgraph",
    ]
    assert [case.live_slack_evidence_proven for case in SLACK_ENTRYPOINT_READINESS_CASES] == [
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


def test_slack_entrypoint_readiness_budget_is_bounded() -> None:
    assert sum(case.max_openai_requests for case in SLACK_ENTRYPOINT_READINESS_CASES) == 11
    assert sum(case.max_cost_usd for case in SLACK_ENTRYPOINT_READINESS_CASES) == 0.60


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
        "11 OpenAI requests and $0.60",
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
