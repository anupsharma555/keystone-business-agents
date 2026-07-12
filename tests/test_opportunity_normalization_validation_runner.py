from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from keystone_agents.execution_identity import create_validation_execution_identity
from keystone_agents.models import TypedAgentRunResult
from keystone_agents.schemas.opportunity import OpportunityScoutResult
from scripts.run_opportunity_normalization_validation import (
    EXPECTED_MODEL,
    EXPECTED_REQUESTS,
    EXPECTED_URLS,
    SOURCE_PACKET,
    _build_payload,
    _load_packet,
    _revalidate_saved_receipt,
    _typed_input,
    _write_result_atomic,
    build_parser,
)


def _source(url: str, source_id: str) -> dict[str, object]:
    return {
        "source_id": source_id,
        "title": "Hack for Humanity | Summer 2026: AI for Mental and Physical Health",
        "url": url,
        "source_type": "conference",
        "supported_signal": "Official first-party hackathon evidence.",
        "source_quality": {
            "url": url,
            "title": "Hack for Humanity Summer 2026",
            "source_type": "conference",
            "credibility_score": 90,
            "domain_credibility_score": 90,
            "recency_score": 95,
            "relevance_score": 95,
            "overall_score": 92,
            "rationale": "Official first-party event page.",
        },
    }


def _quality() -> dict[str, object]:
    return {
        "source_count": 2,
        "independent_source_count": 2,
        "average_credibility_score": 90,
        "average_domain_credibility_score": 90,
        "average_recency_score": 95,
        "average_relevance_score": 95,
        "overall_score": 92,
        "high_quality_source_count": 2,
        "low_quality_source_count": 0,
        "rationale": "Two official pages on one independent domain.",
    }


def _passing_result() -> TypedAgentRunResult[OpportunityScoutResult]:
    sources = [
        _source(url, f"source-{index}")
        for index, url in enumerate(sorted(EXPECTED_URLS), start=1)
    ]
    bundle = {
        "bundle_id": "hack-for-humanity-summer-2026",
        "company_name": "Hack for Humanity Summer 2026",
        "source_category": "conference",
        "summary": "Official event and dates pages describe one upcoming health hackathon.",
        "sources": sources,
        "source_quality_summary": _quality(),
        "missing_evidence": ["Geographic and participant eligibility details are unknown."],
    }
    record = {
        "company_name": "Hack for Humanity Summer 2026",
        "entity_kind": "hackathon",
        "opportunity_type": "hackathon or challenge opportunity",
        "usa_relevance": "unknown",
        "priority_score": 55,
        "why_now_signal": "The submission window is upcoming on August 7, 2026.",
        "recommended_next_step": (
            "Conditionally review whether a small prototype or portfolio submission fits."
        ),
        "sources": sources,
        "source_quality_summary": _quality(),
        "source_bundles": [bundle],
        "missing_evidence": ["Geographic and participant eligibility details are unknown."],
        "disqualification_reasons": [
            "Not a consulting or revenue opportunity.",
            "No established cash prize.",
        ],
        "keystone_fit_reason": "Conditional portfolio fit only.",
        "outside_consulting_likelihood": 5,
        "handoff_to_business_research_analyst": False,
        "approved_for_outreach": False,
    }
    output = OpportunityScoutResult.model_validate(
        {
            "topic": "Assess supplied Hack for Humanity packet.",
            "dry_run": False,
            "search_provider": "none",
            "search_queries": ["planned but not executed"],
            "raw_search_result_count": 0,
            "records": [record],
            "source_bundles": [bundle],
            "source_quality_summary": _quality(),
            "outreach_generated": False,
        }
    )
    return TypedAgentRunResult(
        agent_name="opportunity_scout",
        output=output,
        raw_result=SimpleNamespace(new_items=[]),
        live=True,
        usage={"available": True, "requests": 1},
        cost={"available": True, "estimated_usd": 0.01},
        budget_guard={"enforced": True, "exceeded": False, "budget_usd": 0.05},
        request_cache={"rate_limit_retries": 0},
    )


def _identity():
    return create_validation_execution_identity(
        scenario="opportunity_same_packet_top_level_normalization",
        route="opportunity_scout",
        now=datetime(2026, 7, 11, 12, 30, tzinfo=UTC),
        nonce="a1b2c3d4",
    )


def test_source_packet_retains_exact_two_pages_and_one_domain(require_local_evidence) -> None:
    require_local_evidence(SOURCE_PACKET)
    packet = _load_packet(SOURCE_PACKET)

    assert {source["url"] for source in packet["sources"]} == EXPECTED_URLS
    assert packet["source_domain"] == "hack-for-humanity-summer-26.devpost.com"
    assert all(source["content_sha256"] for source in packet["sources"])
    assert len(packet["evidence_caveats"]) >= 5


def test_runner_defaults_to_one_request_and_ten_cent_budget(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["run_opportunity_normalization_validation.py"])
    args = build_parser().parse_args()

    assert args.model == EXPECTED_MODEL
    assert args.max_openai_requests == EXPECTED_REQUESTS
    assert args.budget_usd == 0.10


def test_typed_input_forbids_search_tools_and_outreach(require_local_evidence) -> None:
    require_local_evidence(SOURCE_PACKET)
    typed_input = _typed_input(_load_packet(SOURCE_PACKET))

    assert "attach_tools\": false" in typed_input.context
    assert "search_provider\": \"none" in typed_input.context
    assert "do not draft outreach" in typed_input.topic.lower()
    assert "geography is unknown" in typed_input.context.lower()


def test_typed_input_can_bind_an_exact_pilot_ask(require_local_evidence) -> None:
    packet = _load_packet(SOURCE_PACKET)
    pilot_ask = (
        "Assess the selected current opportunity for KNI fit, separate confirmed facts "
        "from interpretation, show the retained sources, and recommend the next safe action."
    )

    typed_input = _typed_input(packet, operator_request=pilot_ask)

    assert typed_input.topic == pilot_ask
    assert packet["operator_request"] != pilot_ask


def test_passing_payload_requires_both_normalization_levels_and_safety() -> None:
    payload = _build_payload(
        _passing_result(),
        execution_identity=_identity(),
        model=EXPECTED_MODEL,
        request_ceiling=EXPECTED_REQUESTS,
        budget_usd=0.05,
    )

    assert payload["status"] == "pass"
    assert all(payload["checks"].values())
    assert payload["safety"]["tool_count"] == 0
    assert payload["safety"]["provider_writes"] == 0


def test_unknown_geography_and_explicit_negated_boundaries_are_not_false_failures() -> None:
    result = _passing_result()
    record = result.final_output.records[0]
    record.usa_relevance = "Unknown from supplied pages."
    record.role_country = "Unknown"
    record.disqualification_reasons = [
        "Not a consulting or revenue opportunity.",
        "No established cash prize.",
    ]

    payload = _build_payload(
        result,
        execution_identity=_identity(),
        model=EXPECTED_MODEL,
        request_ceiling=EXPECTED_REQUESTS,
        budget_usd=0.05,
    )

    assert payload["checks"]["geography_unknown"] is True
    assert payload["checks"]["no_consulting_revenue_or_cash_inference"] is True


def test_upcoming_timing_can_be_carried_by_typed_novelty() -> None:
    result = _passing_result()
    record = result.final_output.records[0]
    record.why_now_signal = "The submission window begins soon."
    record.novelty = "Current and upcoming."

    payload = _build_payload(
        result,
        execution_identity=_identity(),
        model=EXPECTED_MODEL,
        request_ceiling=EXPECTED_REQUESTS,
        budget_usd=0.05,
    )

    assert payload["checks"]["upcoming_timing_retained"] is True


def test_payload_fails_on_request_overrun_or_tool_use() -> None:
    result = _passing_result()
    object.__setattr__(result, "usage", {"available": True, "requests": 2})
    result.raw_result.new_items.append(SimpleNamespace(type="tool_call_item"))

    payload = _build_payload(
        result,
        execution_identity=_identity(),
        model=EXPECTED_MODEL,
        request_ceiling=EXPECTED_REQUESTS,
        budget_usd=0.05,
    )

    assert payload["status"] == "partial"
    assert payload["checks"]["exact_request_count"] is False
    assert payload["checks"]["no_tools"] is False


def test_receipt_is_written_atomically(tmp_path: Path) -> None:
    receipt = tmp_path / "opportunity.json"
    _write_result_atomic(receipt, {"status": "pass", "requests": 1})

    assert json.loads(receipt.read_text(encoding="utf-8"))["requests"] == 1
    assert not receipt.with_suffix(".json.tmp").exists()


def test_saved_receipt_can_be_revalidated_without_model_call(tmp_path: Path) -> None:
    result = _passing_result()
    receipt = tmp_path / "saved.json"
    saved = {
        "status": "partial",
        "output": result.final_output.model_dump(mode="json"),
        "usage": result.usage,
        "cost": result.cost,
        "budget_guard": result.budget_guard,
        "request_cache": result.request_cache,
        "execution_identity": _identity().receipt(),
    }
    receipt.write_text(json.dumps(saved), encoding="utf-8")

    restored, identity, initial_status = _revalidate_saved_receipt(receipt)

    assert restored.final_output.records[0].company_name.startswith("Hack for Humanity")
    assert identity.run_id == saved["execution_identity"]["run_id"]
    assert initial_status == "partial"


def test_main_uses_same_execution_identity_for_trace_and_receipt(
    tmp_path: Path,
    monkeypatch,
    capsys,
    require_local_evidence,
) -> None:
    require_local_evidence(SOURCE_PACKET)
    import scripts.run_opportunity_normalization_validation as runner

    captured: dict[str, object] = {}

    def fake_sdk(*_args, **kwargs):
        captured.update(kwargs)
        return _passing_result()

    output = tmp_path / "opportunity-live.json"
    monkeypatch.setattr(runner, "run_typed_sdk_agent", fake_sdk)
    monkeypatch.setattr(runner, "load_settings", lambda **_kwargs: None)
    monkeypatch.setattr(
        "sys.argv",
        ["run_opportunity_normalization_validation.py", "--output", str(output)],
    )

    assert runner.main() == 0
    payload = json.loads(capsys.readouterr().out)

    assert captured["trace_metadata"]["run_id"] == payload["execution_identity"]["run_id"]
    assert captured["trace_metadata"]["case_id"] == payload["execution_identity"]["case_id"]
    assert json.loads(output.read_text(encoding="utf-8")) == payload
