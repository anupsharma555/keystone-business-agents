from __future__ import annotations

import json
from pathlib import Path

import pytest

from keystone_agents.pilot_receipt_replay import (
    PilotReceiptReplay,
    build_pilot_receipt_replays,
)
from keystone_agents.slack_action_contract import business_agent_result_display_text

ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS = ROOT / "artifacts" / "test-pack"


def _replays():
    return build_pilot_receipt_replays(
        opportunity_evidence_path=(
            ARTIFACTS / "controlled-pilot-opportunity-compact-revalidated.json"
        ),
        research_model_path=ARTIFACTS / "controlled-pilot-research-plan-model-v2.json",
        research_plan_path=ARTIFACTS / "controlled-pilot-research-plan-v2.json",
        weekly_evidence_path=ARTIFACTS / "weekly-chief-packet-live.json",
    )


def test_saved_pilot_receipts_prepare_answer_first_zero_model_slack_replays() -> None:
    replays = _replays()

    assert {replay.case_id for replay in replays} == {
        "current_opportunity_assessment",
        "research_to_internal_doc",
        "weekly_project_brief",
    }
    for replay in replays:
        assert replay.human_summary
        assert len(replay.human_summary) <= 3000
        assert replay.visible_sources
        assert "*Source" in replay.human_summary
        assert not replay.human_summary.startswith(("route:", "status:", "model:"))
        assert replay.original_openai_requests == 1
        assert replay.original_estimated_cost_usd > 0
        assert replay.replay_openai_requests == 0
        assert replay.repeated_model_synthesis is False
        assert replay.provider_writes == 0
        assert replay.slack_posted is False
        assert replay.slack_permalink == ""
        assert replay.pilot_observation_claimed is False
        assert (
            business_agent_result_display_text(
                {
                    "route": replay.route,
                    "output_type": replay.output_type,
                    "human_summary": replay.human_summary,
                    "status": "ready_for_authorized_slack_transport",
                }
            )
            == replay.human_summary
        )


def test_replay_refuses_a_non_catalog_request_hash() -> None:
    payload = _replays()[0].model_dump(mode="json")
    payload["natural_request_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="does not match the catalog"):
        PilotReceiptReplay.model_validate(payload)


def test_replay_refuses_tampered_opportunity_evidence(tmp_path: Path) -> None:
    source = ARTIFACTS / "controlled-pilot-opportunity-compact-revalidated.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["safety"]["provider_writes"] = 1
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="no-write boundary"):
        build_pilot_receipt_replays(
            opportunity_evidence_path=tampered,
            research_model_path=ARTIFACTS / "controlled-pilot-research-plan-model-v2.json",
            research_plan_path=ARTIFACTS / "controlled-pilot-research-plan-v2.json",
            weekly_evidence_path=ARTIFACTS / "weekly-chief-packet-live.json",
        )


def test_replay_refuses_weekly_evidence_outside_privacy_boundary(tmp_path: Path) -> None:
    source = ARTIFACTS / "weekly-chief-packet-live.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["raw_private_context_transmitted"] = True
    tampered = tmp_path / "weekly-tampered.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="privacy-minimized boundary"):
        build_pilot_receipt_replays(
            opportunity_evidence_path=(
                ARTIFACTS / "controlled-pilot-opportunity-compact-revalidated.json"
            ),
            research_model_path=ARTIFACTS / "controlled-pilot-research-plan-model-v2.json",
            research_plan_path=ARTIFACTS / "controlled-pilot-research-plan-v2.json",
            weekly_evidence_path=tampered,
        )
