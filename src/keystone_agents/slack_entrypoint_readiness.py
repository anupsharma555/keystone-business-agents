"""Readiness contract for the two remaining live Slack entrypoint proofs."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SlackEntrypointReadinessCase:
    probe_id: str
    title: str
    backend: str
    prompt: str
    proof_nodeids: tuple[str, ...]
    required_visible_evidence: tuple[str, ...]
    max_openai_requests: int
    max_cost_usd: float
    live_slack_evidence_proven: bool = False


SLACK_ENTRYPOINT_READINESS_CASES: tuple[SlackEntrypointReadinessCase, ...] = (
    SlackEntrypointReadinessCase(
        probe_id="SLACK-DIRECT-01",
        title="Direct named Business Research answer-first proof",
        backend="direct_specialist",
        prompt=(
            '@KNI business research analyst "research Suki AI for a concise '
            "source-backed fit check; include visible source URLs; do not draft "
            'outreach, send, post elsewhere, schedule, or write files."'
        ),
        proof_nodeids=(
            "tests/test_slack_action_contract.py::"
            "test_result_display_text_prefers_renderer_human_summary_over_stale_display_fields",
            "tests/test_reporting.py::test_company_profile_report_renders_with_sources",
        ),
        required_visible_evidence=(
            "slack_permalink",
            "local_run_id",
            "business_research_analyst_route",
            "answer_first_human_summary",
            "visible_source_urls_or_source_limit",
            "no_metadata_before_answer",
            "one_final_response",
            "usage_and_cost_receipt",
            "no_unintended_side_effect",
        ),
        max_openai_requests=3,
        max_cost_usd=0.10,
    ),
    SlackEntrypointReadinessCase(
        probe_id="SLACK-GRAPH-01",
        title="Connector-backed Gmail research draft graph proof",
        backend="langgraph",
        prompt=(
            "@KNI read the latest Gmail thread from the configured exact test sender, "
            "research the sender organization using only the selected thread context, "
            "and prepare a draft reply for review without sending, creating a Gmail "
            "draft, searching the web, posting elsewhere, or writing externally."
        ),
        proof_nodeids=(
            "tests/test_workflow_runner.py::"
            "test_live_gmail_retrieval_promotes_selected_thread_without_raw_body",
            "tests/test_langgraph_workflow.py::"
            "test_backend_selected_manager_loop_uses_graph_for_gmail_research_outreach_checkpoint",
            "tests/test_langgraph_workflow.py::"
            "test_langgraph_storage_events_render_final_run_report",
        ),
        required_visible_evidence=(
            "slack_permalink",
            "work_item_id",
            "selected_gmail_identity_without_raw_body",
            "gmail_research_outreach_node_path",
            "answer_first_review_only_draft",
            "approval_checkpoint",
            "one_final_response",
            "usage_trace_and_cost_receipt",
            "no_search_draft_send_or_external_write",
        ),
        max_openai_requests=8,
        max_cost_usd=0.50,
    ),
)
