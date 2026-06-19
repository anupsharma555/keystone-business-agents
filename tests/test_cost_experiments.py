from __future__ import annotations

import json

from keystone_agents.cost_experiments import (
    annotate_actual_cost_for_work_item,
    annotate_actual_costs_for_database_selection,
    compare_sdk_cost_records,
    list_sdk_session_run_groups,
    load_sdk_cost_records_from_database,
    load_work_item_cost_summary,
    render_sdk_cost_comparison_markdown,
    render_sdk_session_groups_markdown,
    render_work_item_cost_summary_markdown,
    sdk_cost_record_from_payload,
)
from keystone_agents.schemas.work_item import WorkItem, WorkItemEvent, WorkItemKind
from keystone_agents.storage.sqlite_store import SQLiteStore


def _database_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'cost_experiments.db'}"


def _save_cost_work_item(store: SQLiteStore, work_item_id: str) -> None:
    store.save_work_item(
        WorkItem(
            id=work_item_id,
            kind=WorkItemKind.OPPORTUNITY,
            title="Cost experiment WorkItem",
        )
    )


def _sdk_payload(
    *,
    input_tokens: int,
    cached_input_tokens: int,
    estimated_usd: float,
    static_prefix: str = "static-prefix",
    prompt_chars: int = 1000,
    session_attached: bool = True,
    session_hash: str = "session-a",
) -> dict:
    return {
        "_sdk_usage": {
            "available": True,
            "requests": 1,
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "cache_hit_rate": round(cached_input_tokens / input_tokens, 4),
            "output_tokens": 500,
            "reasoning_output_tokens": 0,
            "total_tokens": input_tokens + 500,
        },
        "_sdk_cost": {
            "source": "local_pricing_table",
            "estimated_usd": estimated_usd,
            "components_usd": {
                "input": estimated_usd / 2,
                "cached_input": estimated_usd / 10,
                "output": estimated_usd / 2,
            },
        },
        "_sdk_request_cache": {
            "request_layout": "static_agent_prefix_then_dynamic_typed_input",
            "static_prefix_sha256": static_prefix,
            "instructions_sha256": "instructions",
            "tool_names_sha256": "tools",
            "output_schema_sha256": "schema",
            "dynamic_prompt_sha256": f"prompt-{prompt_chars}",
            "dynamic_prompt_chars": prompt_chars,
            "tool_count": 5,
            "session_attached": session_attached,
            "session_scope": "slack",
            "session_source": "derived",
            "session_id_hash": session_hash,
        },
    }


def test_compare_sdk_cost_records_reports_cache_and_prefix_stability() -> None:
    records = [
        sdk_cost_record_from_payload(
            _sdk_payload(input_tokens=10_000, cached_input_tokens=0, estimated_usd=0.16),
            run_id="first",
            actual_usd="0.15",
        ),
        sdk_cost_record_from_payload(
            _sdk_payload(
                input_tokens=11_000,
                cached_input_tokens=8_800,
                estimated_usd=0.06,
                prompt_chars=1200,
            ),
            run_id="repeat",
            actual_usd="0.055",
        ),
    ]

    summary = compare_sdk_cost_records(records)

    assert summary["cache_summary"]["last_cache_hit_rate"] == 0.8
    assert summary["cache_summary"]["estimated_usd_delta"] == -0.1
    assert summary["request_cache_summary"]["static_prefix_stable"] is True
    assert summary["request_cache_summary"]["session_attached_all"] is True
    assert summary["request_cache_summary"]["session_hash_stable"] is True
    assert summary["request_cache_summary"]["dynamic_prompt_chars_delta"] == 200
    assert summary["cache_readiness"]["status"] == "pass"
    assert summary["cache_readiness"]["cache_friendly"] is True
    assert summary["actual_cost_comparisons"][0]["actual_usd"] == 0.15
    assert summary["runs"][1]["run_id"] == "repeat"

    markdown = render_sdk_cost_comparison_markdown(summary)
    assert "SDK Cost Cache Comparison" in markdown
    assert "Last cache hit rate: 0.8" in markdown
    assert "Static prefix stable: yes" in markdown
    assert "Session hash stable: yes" in markdown
    assert "Cache Readiness" in markdown
    assert "Cache-friendly: yes" in markdown
    assert "| repeat | 0.8 | 11000 | 8800 | 500 | $0.06 | $0.055 | yes |" in markdown
    assert "OpenAI Platform Comparison" in markdown


def test_compare_sdk_cost_records_flags_static_prefix_drift() -> None:
    records = [
        sdk_cost_record_from_payload(
            _sdk_payload(input_tokens=10_000, cached_input_tokens=0, estimated_usd=0.16),
            run_id="first",
        ),
        sdk_cost_record_from_payload(
            _sdk_payload(
                input_tokens=10_000,
                cached_input_tokens=0,
                estimated_usd=0.16,
                static_prefix="changed-prefix",
            ),
            run_id="repeat",
        ),
    ]

    summary = compare_sdk_cost_records(records)

    assert summary["request_cache_summary"]["static_prefix_stable"] is False
    assert summary["request_cache_summary"]["static_prefix_count"] == 2
    assert summary["cache_readiness"]["status"] == "warn"
    assert "Static prompt prefix changed between runs." in summary["cache_readiness"]["issues"]
    assert any(
        "Keep agent instructions" in item for item in summary["cache_readiness"]["recommendations"]
    )


def test_load_sdk_cost_records_from_database_uses_agent_run_rows(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    first = store.save_agent_run(
        agent_name="gmail_triage",
        input_summary="first run",
        output=_sdk_payload(input_tokens=10_000, cached_input_tokens=0, estimated_usd=0.16),
        model="sdk-live",
        dry_run=False,
    )
    second = store.save_agent_run(
        agent_name="gmail_triage",
        input_summary="repeat run",
        output=_sdk_payload(
            input_tokens=11_000,
            cached_input_tokens=8_800,
            estimated_usd=0.06,
            prompt_chars=1200,
        ),
        model="sdk-live",
        dry_run=False,
    )

    records = load_sdk_cost_records_from_database(
        database_url=_database_url(tmp_path),
        run_ids=[str(first), str(second)],
        actual_usd=["0.15", "0.055"],
    )

    assert [record["run_id"] for record in records] == [str(first), str(second)]
    assert records[0]["agent_name"] == "gmail_triage"
    assert json.dumps(records)
    summary = compare_sdk_cost_records(records)
    assert summary["cache_summary"]["last_cache_hit_rate"] == 0.8


def test_annotate_actual_costs_for_database_selection_persists_comparison(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    first = store.save_agent_run(
        agent_name="gmail_triage",
        input_summary="first run",
        output=_sdk_payload(input_tokens=10_000, cached_input_tokens=0, estimated_usd=0.16),
        model="sdk-live",
        dry_run=False,
    )
    second = store.save_agent_run(
        agent_name="gmail_triage",
        input_summary="repeat run",
        output=_sdk_payload(
            input_tokens=11_000,
            cached_input_tokens=8_800,
            estimated_usd=0.06,
            prompt_chars=1200,
        ),
        model="sdk-live",
        dry_run=False,
    )

    updated = annotate_actual_costs_for_database_selection(
        database_url=_database_url(tmp_path),
        run_ids=[str(first), str(second)],
        actual_usd=["0.15", "0.055"],
        actual_reference_id="platform-window-1",
    )
    records = load_sdk_cost_records_from_database(
        database_url=_database_url(tmp_path),
        run_ids=[str(first), str(second)],
    )
    summary = compare_sdk_cost_records(records)

    assert [item["id"] for item in updated] == [first, second]
    assert records[0]["cost"]["actual_usd"] == 0.15
    assert records[0]["cost"]["estimate_vs_actual"]["reference_id"] == "platform-window-1"
    assert summary["actual_cost_comparisons"][0]["actual_usd"] == 0.15


def test_work_item_cost_summary_aggregates_retrieval_and_sdk_events(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    work_item_id = "wi_cost_test"
    _save_cost_work_item(store, work_item_id)
    store.save_work_item_event(
        work_item_id,
        WorkItemEvent(
            event_type="advance_started",
            metadata={
                "cost_profile": "slack_conservative",
                "allow_manager_loop_repair": False,
                "include_contact_enrichment": False,
                "hosted_web_search_max_calls": 1,
                "reuse_existing_research": True,
            },
        ),
    )
    store.save_work_item_event(
        work_item_id,
        WorkItemEvent(
            event_type="workflow_retrieval_usage",
            metadata={
                "query_count": 8,
                "raw_search_result_count": 21,
                "provider_usage": {
                    "searxng": {
                        "requests_attempted": 8,
                        "requests_succeeded": 8,
                        "raw_result_count": 20,
                    },
                    "agents-web-search": {
                        "requests_attempted": 1,
                        "requests_succeeded": 1,
                        "credits_used": 1,
                        "input_tokens": 1000,
                        "cached_input_tokens": 250,
                        "output_tokens": 100,
                        "estimated_usd": 0.012,
                    },
                },
            },
        ),
    )
    store.save_work_item_event(
        work_item_id,
        WorkItemEvent(
            event_type="workflow_sdk_usage",
            metadata={
                "agent_name": "user_response_synthesizer",
                "usage": {
                    "input_tokens": 2000,
                    "cached_input_tokens": 1000,
                    "output_tokens": 300,
                    "reasoning_output_tokens": 50,
                    "cache_hit_rate": 0.5,
                },
                "cost": {
                    "estimated_usd": 0.03,
                    "source": "local_pricing_table",
                    "pricing_provider": "openai",
                    "pricing_model": "gpt-5.4-mini",
                },
            },
        ),
    )
    store.save_work_item_event(
        work_item_id,
        WorkItemEvent(
            event_type="manager_loop_efficiency",
            metadata={
                "step_count": 1,
                "repair_count": 0,
                "final_synthesis_executed": True,
            },
        ),
    )

    summary = load_work_item_cost_summary(
        database_url=_database_url(tmp_path),
        work_item_id=work_item_id,
        actual_usd="0.05",
        actual_reference_id="platform-window",
    )
    markdown = render_work_item_cost_summary_markdown(summary)

    assert summary["available"] is True
    assert summary["retrieval"]["query_count"] == 8
    assert summary["retrieval"]["searxng_requests"] == 8
    assert summary["retrieval"]["agents_web_search_calls"] == 1
    assert summary["retrieval"]["cache_hit_rate"] == 0.25
    assert summary["sdk"]["input_tokens"] == 2000
    assert summary["sdk"]["cached_input_tokens"] == 1000
    assert summary["sdk"]["providers"] == ["openai"]
    assert summary["sdk"]["models"] == ["gpt-5.4-mini"]
    assert summary["run_controls"]["latest_cost_profile"] == "slack_conservative"
    assert summary["run_controls"]["latest_hosted_web_search_max_calls"] == 1
    assert summary["run_controls"]["latest_reuse_existing_research"] is True
    assert summary["estimated_usd"] == 0.042
    assert summary["actual_comparison"]["actual_usd"] == 0.05
    assert summary["actual_comparison"]["actual_minus_estimate_usd"] == 0.008
    assert summary["actual_comparison"]["estimate_coverage_rate"] == 0.84
    assert summary["diagnosis"]["status"] == "pass"
    assert "WorkItem Cost Summary" in markdown
    assert "Cost profile: slack_conservative" in markdown
    assert "Models: gpt-5.4-mini" in markdown
    assert "Hosted web-search calls: 1" in markdown
    assert "Actual USD: $0.05" in markdown
    assert "Actual minus estimate USD: $0.008" in markdown
    assert "Estimate coverage: 84%" in markdown


def test_work_item_cost_summary_warns_when_slack_conservative_uses_non_mini_openai(
    tmp_path,
) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    work_item_id = "wi_non_mini_model"
    _save_cost_work_item(store, work_item_id)
    store.save_work_item_event(
        work_item_id,
        WorkItemEvent(
            event_type="advance_started",
            metadata={
                "cost_profile": "slack_conservative",
                "allow_manager_loop_repair": False,
                "include_contact_enrichment": False,
                "hosted_web_search_max_calls": 1,
                "reuse_existing_research": True,
            },
        ),
    )
    store.save_work_item_event(
        work_item_id,
        WorkItemEvent(
            event_type="workflow_sdk_usage",
            metadata={
                "agent_name": "user_response_synthesizer",
                "usage": {
                    "input_tokens": 2000,
                    "cached_input_tokens": 1000,
                    "output_tokens": 300,
                    "cache_hit_rate": 0.5,
                },
                "cost": {
                    "estimated_usd": 0.08,
                    "source": "local_pricing_table",
                    "pricing_provider": "openai",
                    "pricing_model": "gpt-5.4",
                },
            },
        ),
    )

    summary = load_work_item_cost_summary(
        database_url=_database_url(tmp_path),
        work_item_id=work_item_id,
    )

    assert summary["sdk"]["models"] == ["gpt-5.4"]
    assert summary["diagnosis"]["status"] == "warn"
    assert (
        "Slack cost-controlled run used non-mini OpenAI model(s): gpt-5.4."
        in summary["diagnosis"]["notes"]
    )


def test_work_item_actual_cost_annotation_persists_platform_comparison(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    work_item_id = "wi_actual_cost"
    _save_cost_work_item(store, work_item_id)
    store.save_work_item_event(
        work_item_id,
        WorkItemEvent(
            event_type="workflow_sdk_usage",
            metadata={
                "agent_name": "user_response_synthesizer",
                "usage": {
                    "input_tokens": 2000,
                    "cached_input_tokens": 1000,
                    "output_tokens": 300,
                    "cache_hit_rate": 0.5,
                },
                "cost": {
                    "estimated_usd": 0.03,
                    "source": "local_pricing_table",
                    "pricing_provider": "openai",
                    "pricing_model": "gpt-5.4-mini",
                },
            },
        ),
    )

    annotation = annotate_actual_cost_for_work_item(
        database_url=_database_url(tmp_path),
        work_item_id=work_item_id,
        actual_usd="0.07",
        actual_reference_id="platform-window",
    )
    summary = load_work_item_cost_summary(
        database_url=_database_url(tmp_path),
        work_item_id=work_item_id,
    )

    assert annotation["work_item_id"] == work_item_id
    assert annotation["actual_comparison"]["actual_usd"] == 0.07
    assert summary["actual_event_count"] == 1
    assert summary["actual_comparison"]["actual_usd"] == 0.07
    assert summary["actual_comparison"]["actual_minus_estimate_usd"] == 0.04
    assert summary["actual_comparison"]["reference_id"] == "platform-window"


def test_work_item_cost_summary_warns_when_search_run_lacks_retrieval_usage(
    tmp_path,
) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    work_item_id = "wi_missing_retrieval_usage"
    _save_cost_work_item(store, work_item_id)
    store.save_work_item_event(
        work_item_id,
        WorkItemEvent(
            event_type="advance_started",
            metadata={
                "cost_profile": "slack_opportunity_balanced",
                "hosted_web_search_max_calls": 2,
            },
        ),
    )
    store.save_work_item_event(
        work_item_id,
        WorkItemEvent(
            event_type="workflow_sdk_usage",
            metadata={
                "agent_name": "user_response_synthesizer",
                "usage": {"input_tokens": 1000, "output_tokens": 200},
                "cost": {
                    "estimated_usd": 0.01,
                    "pricing_provider": "openai",
                    "pricing_model": "gpt-5.4-mini",
                },
            },
        ),
    )

    summary = load_work_item_cost_summary(
        database_url=_database_url(tmp_path),
        work_item_id=work_item_id,
    )

    assert summary["diagnosis"]["status"] == "warn"
    assert (
        "No retrieval usage was recorded for a research/search-oriented run."
        in summary["diagnosis"]["notes"]
    )


def test_load_sdk_cost_records_from_database_can_select_latest_session(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_agent_run(
        agent_name="gmail_triage",
        input_summary="older unrelated thread",
        output=_sdk_payload(
            input_tokens=9_000,
            cached_input_tokens=0,
            estimated_usd=0.12,
            session_hash="session-old",
        ),
        model="sdk-live",
        dry_run=False,
    )
    first = store.save_agent_run(
        agent_name="gmail_triage",
        input_summary="first run in selected thread",
        output=_sdk_payload(
            input_tokens=10_000,
            cached_input_tokens=0,
            estimated_usd=0.16,
            session_hash="session-selected",
        ),
        model="sdk-live",
        dry_run=False,
    )
    second = store.save_agent_run(
        agent_name="gmail_triage",
        input_summary="follow-up in selected thread",
        output=_sdk_payload(
            input_tokens=11_000,
            cached_input_tokens=8_800,
            estimated_usd=0.06,
            prompt_chars=1200,
            session_hash="session-selected",
        ),
        model="sdk-live",
        dry_run=False,
    )

    by_anchor = load_sdk_cost_records_from_database(
        database_url=_database_url(tmp_path),
        same_session_as_run_id=str(second),
        limit=2,
    )
    latest = load_sdk_cost_records_from_database(
        database_url=_database_url(tmp_path),
        latest_session=True,
        limit=2,
    )

    assert [record["run_id"] for record in by_anchor] == [str(first), str(second)]
    assert [record["run_id"] for record in latest] == [str(first), str(second)]
    assert by_anchor[0]["request_cache"]["session_id_hash"] == "session-selected"


def test_load_sdk_cost_records_from_database_can_select_latest_repeated_session(
    tmp_path,
) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    first = store.save_agent_run(
        agent_name="gmail_triage",
        input_summary="first run in selected thread",
        output=_sdk_payload(
            input_tokens=10_000,
            cached_input_tokens=0,
            estimated_usd=0.16,
            session_hash="session-selected",
        ),
        model="sdk-live",
        dry_run=False,
    )
    second = store.save_agent_run(
        agent_name="gmail_triage",
        input_summary="follow-up in selected thread",
        output=_sdk_payload(
            input_tokens=11_000,
            cached_input_tokens=8_800,
            estimated_usd=0.06,
            prompt_chars=1200,
            session_hash="session-selected",
        ),
        model="sdk-live",
        dry_run=False,
    )
    one_off = store.save_agent_run(
        agent_name="opportunity_scout",
        input_summary="new unrelated one-off",
        output=_sdk_payload(
            input_tokens=5_000,
            cached_input_tokens=0,
            estimated_usd=0.04,
            session_hash="session-new-one-off",
        ),
        model="sdk-live",
        dry_run=False,
    )

    latest = load_sdk_cost_records_from_database(
        database_url=_database_url(tmp_path),
        latest_session=True,
        limit=2,
    )
    repeated = load_sdk_cost_records_from_database(
        database_url=_database_url(tmp_path),
        latest_repeated_session=True,
        limit=2,
    )

    assert [record["run_id"] for record in latest] == [str(one_off)]
    assert [record["run_id"] for record in repeated] == [str(first), str(second)]


def test_list_sdk_session_run_groups_reports_recent_safe_groups(tmp_path) -> None:
    store = SQLiteStore(_database_url(tmp_path))
    store.save_agent_run(
        agent_name="gmail_triage",
        input_summary="older unrelated thread",
        output=_sdk_payload(
            input_tokens=9_000,
            cached_input_tokens=0,
            estimated_usd=0.12,
            session_hash="session-old",
        ),
        model="sdk-live",
        dry_run=False,
    )
    first = store.save_agent_run(
        agent_name="gmail_triage",
        input_summary="first run in selected thread",
        output=_sdk_payload(
            input_tokens=10_000,
            cached_input_tokens=0,
            estimated_usd=0.16,
            session_hash="session-selected",
        ),
        model="sdk-live",
        dry_run=False,
    )
    second = store.save_agent_run(
        agent_name="gmail_triage",
        input_summary="follow-up in selected thread",
        output=_sdk_payload(
            input_tokens=11_000,
            cached_input_tokens=8_800,
            estimated_usd=0.06,
            prompt_chars=1200,
            session_hash="session-selected",
        ),
        model="sdk-live",
        dry_run=False,
    )

    groups = list_sdk_session_run_groups(
        database_url=_database_url(tmp_path),
        limit=2,
        min_runs=2,
    )

    assert groups[0]["session_id_hash"] == "session-selected"
    assert len(groups) == 1
    assert groups[0]["session_scope"] == "slack"
    assert groups[0]["run_count"] == 2
    assert groups[0]["first_run_id"] == str(first)
    assert groups[0]["last_run_id"] == str(second)
    assert groups[0]["agents"] == ["gmail_triage"]
    assert groups[0]["aggregate_cache_hit_rate"] == 0.419
    assert groups[0]["estimated_usd"] == 0.22
    markdown = render_sdk_session_groups_markdown(groups)
    assert "SDK Session Groups" in markdown
    assert "| session-selected | slack | 2 |" in markdown
    assert "1715366400" not in markdown
