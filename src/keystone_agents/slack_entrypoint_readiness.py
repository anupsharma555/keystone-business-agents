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
    live_evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.live_slack_evidence_proven:
            return
        required_ref_kinds = {
            "slack": any(
                ref.startswith("slack_permalink:evidence:") for ref in self.live_evidence_refs
            ),
            "local_identity": any(
                ref.startswith(("run:", "workitem:")) for ref in self.live_evidence_refs
            ),
            "review": "review:pass" in self.live_evidence_refs,
            "usage": any(ref.startswith("usage:requests=") for ref in self.live_evidence_refs),
            "cost": any(
                ref.startswith("cost:estimated_usd=") for ref in self.live_evidence_refs
            ),
            "safety": "safety:no_external_side_effects" in self.live_evidence_refs,
        }
        missing = [name for name, present in required_ref_kinds.items() if not present]
        if missing:
            raise ValueError(
                "Live Slack evidence requires inspectable refs for: " + ", ".join(missing)
            )


SLACK_ENTRYPOINT_READINESS_CASES: tuple[SlackEntrypointReadinessCase, ...] = (
    SlackEntrypointReadinessCase(
        probe_id="SLACK-DIRECT-01",
        title="Direct named Business Research answer-first proof",
        backend="direct_specialist",
        prompt=(
            '@KNI business research analyst "research Suki AI. Return a concise '
            "brief covering what the company does, current signals, KNI fit, "
            'evidence gaps, recommendation, and visible source URLs."'
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
        live_slack_evidence_proven=True,
        live_evidence_refs=(
            "slack_permalink:evidence:docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md#direct-business-research",
            "run:sbar_613ad9e3000b4ef39b0323bf836685b4",
            "run:kba-456",
            "review:pass",
            "usage:requests=3",
            "cost:estimated_usd=0.054459",
            "safety:no_external_side_effects",
        ),
    ),
    SlackEntrypointReadinessCase(
        probe_id="SLACK-GRAPH-01",
        title="Connector-backed Gmail collaboration review graph proof",
        backend="langgraph",
        prompt=(
            "@KNI review the latest Gmail thread from the configured exact test sender, "
            "including all messages and the original inquiry. Identify the current "
            "conversation state, recommend the most useful KNI-specific collaboration "
            "next step using only that thread and approved KNI context, and include a "
            "reply only if replying now would move the relationship forward. Return a "
            "concise summary, supporting evidence, and any approval status that actually "
            "applies."
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
            "answer_first_review_or_recommendation",
            "correct_approval_or_no_approval_state",
            "one_final_response",
            "usage_trace_and_cost_receipt",
            "no_search_draft_send_or_external_write",
        ),
        max_openai_requests=8,
        max_cost_usd=0.50,
        live_slack_evidence_proven=True,
        live_evidence_refs=(
            "slack_permalink:evidence:docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md#connector-backed-gmail-graph",
            "workitem:wi_b8a3b23a97ed41a5804f4c56c7b3f852",
            "review:pass",
            "usage:requests=2",
            "cost:estimated_usd=0.044811",
            "safety:no_external_side_effects",
        ),
    ),
    SlackEntrypointReadinessCase(
        probe_id="SLACK-DIRECT-ZOTERO-01",
        title="Direct Zotero authenticated provider-read proof",
        backend="direct_specialist",
        prompt=(
            "@KNI BA use Zotero to select the most recently added journal article. "
            "Return only its exact title, authors, and publication title. Do not use "
            "web search, full text, or modify Zotero."
        ),
        proof_nodeids=(
            "tests/test_cli.py::"
            "test_named_ba_zotero_read_runs_preflight_then_source_owner",
            "tests/test_cli.py::"
            "test_zotero_latest_article_preflight_reads_exact_pdf_only_on_demand",
            "tests/test_agent_registry.py::"
            "test_direct_zotero_read_catalog_is_request_scoped",
        ),
        required_visible_evidence=(
            "slack_permalink",
            "local_run_id",
            "zotero_context_agent_route",
            "authenticated_provider_read",
            "requested_field_projection",
            "explicit_unavailable_fields",
            "one_final_response",
            "usage_trace_request_count_and_cost_receipt",
            "no_web_full_text_or_external_side_effect",
        ),
        max_openai_requests=3,
        max_cost_usd=0.05,
        live_slack_evidence_proven=True,
        live_evidence_refs=(
            "slack_permalink:evidence:docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md#direct-zotero-provider-read",
            "run:5692",
            "review:pass",
            "usage:requests=1",
            "cost:estimated_usd=0.01259775",
            "safety:no_external_side_effects",
        ),
    ),
    SlackEntrypointReadinessCase(
        probe_id="SLACK-DIRECT-CONSTRAINT-01",
        title="Direct Business Research exact natural-ask constraint proof",
        backend="direct_specialist",
        prompt="@KNI BA, who is Abridge and summarize the company in 20 words.",
        proof_nodeids=(
            "tests/test_manual_request_plan.py::"
            "test_manual_plan_preserves_requested_summary_word_limit",
            "tests/test_manual_request_plan.py::"
            "test_llm_interpreted_output_constraints_override_heuristic_fallback",
            "tests/test_pipeline.py::"
            "test_company_research_focused_brief_honors_requested_summary_word_limit",
            "tests/test_instruction_following.py::"
            "test_failed_constraint_gets_one_llm_repair",
            "tests/test_cli.py::"
            "test_direct_specialist_routes_share_one_llm_constraint_repair",
            "tests/test_cli.py::"
            "test_cli_explicit_scout_company_summary_runs_business_research_owner",
        ),
        required_visible_evidence=(
            "slack_permalink",
            "local_run_id",
            "business_research_analyst_route",
            "llm_interpreted_exact_20_word_answer_contract",
            "exact_20_word_visible_answer",
            "source_visibility_covered_by_direct_research_probe",
            "no_generic_detailed_summary_expansion",
            "one_final_response",
            "usage_trace_request_count_and_cost_receipt",
            "no_unintended_side_effect",
        ),
        max_openai_requests=10,
        max_cost_usd=0.15,
        live_slack_evidence_proven=True,
        live_evidence_refs=(
            "slack_permalink:evidence:docs/ANU60_LIVE_SLACK_EVIDENCE_TEMPLATE.md#direct-business-research-exact-constraint",
            "run:5851",
            "review:pass",
            "usage:requests=1",
            "cost:estimated_usd=0.01005225",
            "safety:no_external_side_effects",
        ),
    ),
)
