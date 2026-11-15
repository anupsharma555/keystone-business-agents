"""Route candidates and completed Slack evidence retain their separate identities."""

from __future__ import annotations

import json

import pytest

from keystone_agents.agent_decision_contracts import orchestrator_decision_contract
from keystone_agents.runtime.decision_validation import (
    build_cumulative_decision_repair_evidence_replay,
)

ROUTES = ("orchestrator", "business_research_analyst", "clarification")


def _channel_output() -> dict:
    return {
        "ok": True,
        "operation": "read",
        "snapshot": {
            "channel_id": "C111TEST", "oldest_ts": "1700000000",
            "request_ts": "1700604800",
        },
        "messages": [{
            "candidate_id": "message:1700500000.000001",
            "excerpt": "Evidence and limitations. " * 300,
            "source_links": [
                {"url": f"https://example.org/article/{i}", "ordinal": i}
                for i in range(1, 201)
            ],
            "authority": "historical_context_only",
            "text_truncated": True,
        }],
        "coverage": {"pages": 1, "truncated": True, "errors": []},
    }


def _replay(output: dict, **overrides):
    evidence = orchestrator_decision_contract(ROUTES).evidence_resolver(None)
    args = {
        "model_input_candidate_ids": ROUTES,
        "original_model_input_text": json.dumps({"allowed_routes": ROUTES}),
        "attested_read_outputs": ({
            "tool_name": "read_slack_channel_context", "output": output,
        },),
    }
    args.update(overrides)
    return build_cumulative_decision_repair_evidence_replay((), evidence, **args)


def test_route_repair_preserves_full_completed_channel_evidence() -> None:
    original = _channel_output()
    replay = _replay(original)
    assert replay.ready
    assert replay.candidate_ids == ROUTES
    assert replay.telemetry()["candidate_universe_source"] == "original_model_input"
    entries = json.loads(replay.prompt_context.split("Replayed tool evidence: ", 1)[1])
    assert entries == [{"tool_name": "read_slack_channel_context", "output": original}]
    assert replay.compaction_applied is False
    # Telemetry contains route identities and counts, never message/source content.
    assert "example.org" not in json.dumps(replay.telemetry())
    assert "Evidence and limitations" not in json.dumps(replay.telemetry())


@pytest.mark.parametrize("overrides", [
    {"model_input_candidate_ids": ROUTES[:2]},
    {"original_model_input_text": json.dumps({"allowed_routes": ROUTES[:2]})},
    # A prose prefix is not the exact declared owner identity.
    {"original_model_input_text": json.dumps({"allowed_routes": [
        "orchestrator_suffix", "business_research_analyst_suffix", "clarification_suffix",
    ]})},
])
def test_route_repair_rejects_changed_or_invisible_route_universe(overrides: dict) -> None:
    replay = _replay(_channel_output(), **overrides)
    assert not replay.ready
    assert replay.reason_code == "decision_repair_model_input_candidate_universe_unavailable"


def test_receipt_owned_default_does_not_accept_model_input_routing_aliases() -> None:
    replay = _replay(_channel_output(), model_input_candidate_ids=())
    assert not replay.ready
    assert replay.reason_code == "decision_repair_verified_candidate_universe_unavailable"


def test_route_repair_fails_closed_instead_of_truncating_evidence() -> None:
    original = _channel_output()
    original["messages"][0]["excerpt"] = "x" * 48_000
    replay = _replay(original)
    assert not replay.ready
    assert replay.reason_code == "decision_repair_evidence_size_limit_exceeded"


def test_route_repair_rejects_evidence_requiring_secret_field_removal() -> None:
    original = _channel_output()
    original["credential"] = "synthetic-sensitive-field"
    replay = _replay(original)
    assert not replay.ready
    assert replay.reason_code == "decision_repair_channel_evidence_not_preserved"


def test_route_repair_does_not_admit_unrelated_provider_read_tools() -> None:
    replay = _replay(_channel_output(), attested_read_outputs=({
        "tool_name": "read_other_provider", "output": _channel_output(),
    },))
    assert not replay.ready
    assert replay.reason_code == "decision_repair_model_input_read_tool_unsupported"


def test_route_repair_rejects_excess_outputs_instead_of_dropping_any() -> None:
    outputs = []
    for i in range(13):
        output = _channel_output()
        output["coverage"]["pages"] = i + 1
        outputs.append({"tool_name": "read_slack_channel_context", "output": output})
    replay = _replay(_channel_output(), attested_read_outputs=outputs)
    assert not replay.ready
    assert replay.reason_code == "decision_repair_evidence_output_limit_exceeded"
