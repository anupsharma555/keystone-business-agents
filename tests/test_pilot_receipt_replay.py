from __future__ import annotations

import json
from pathlib import Path

import pytest

from keystone_agents.controlled_pilot import controlled_pilot_cases
from keystone_agents.pilot_receipt_replay import (
    PilotReceiptReplay,
    build_pilot_receipt_replays,
)
from keystone_agents.slack_action_contract import business_agent_result_display_text


def _fixture_paths(tmp_path: Path) -> dict[str, Path]:
    cases = {case.case_id: case for case in controlled_pilot_cases()}
    payloads = {
        "opportunity": {
            "status": "pass",
            "checks": {"complete": True},
            "natural_request": cases["current_opportunity_assessment"].natural_ask,
            "safety": {
                "provider_writes": 0,
                "external_action_performed": False,
            },
            "output": {
                "opportunity_name": "Synthetic opportunity",
                "interpretation": "The supplied evidence supports a bounded review.",
                "keystone_fit": "Potential evaluation and evidence-design fit.",
                "next_safe_action": "Review the evidence gap before outreach.",
                "retained_sources": [
                    {
                        "title": "Synthetic source",
                        "url": "https://example.test/opportunity",
                    }
                ],
            },
            "usage": {"requests": 1},
            "cost": {"estimated_usd": 0.01},
        },
        "research_model": {
            "status": "pass",
            "safety": {"send_enabled": False, "provider_writes": 0},
            "output": {
                "company_name": "Synthetic Health",
                "product": "Synthetic care-navigation software.",
                "why_it_matters": "Relevant to evidence and evaluation design.",
                "facts": [
                    {
                        "text": "The company offers care-navigation software.",
                        "source_ids": ["source-1"],
                        "confidence": 0.9,
                    }
                ],
                "unknowns": ["Independent outcomes were not supplied."],
                "source_ids_used": ["source-1"],
                "sources": [
                    {
                        "source_id": "source-1",
                        "title": "Synthetic company page",
                        "url": "https://example.test/company",
                        "source_type": "company_site",
                    }
                ],
            },
            "usage": {"requests": 1},
            "cost": {"estimated_usd": 0.01},
        },
        "research_plan": {
            "status": "pass",
            "safety": {"provider_writes": 0},
            "doc": {
                "executed": False,
                "approval_required_before_create": True,
                "folder_path": "KNI/Test",
                "title": "Synthetic research brief",
            },
        },
        "weekly": {
            "status": "success",
            "context_mode": "privacy_minimized_assertions",
            "raw_private_context_transmitted": False,
            "provider_writes": 0,
            "send_enabled": False,
            "request_count_bound": 1,
            "packet": {
                "summary": "Synthetic weekly operating brief.",
                "synthesis": (
                    "Completed runs and outcomes. Carry forward open work. "
                    "Next actions. Source basis: 8 follow-up, 5 pending_review, "
                    "7 one_time_calendar, and 14 recurring_calendar."
                ),
                "time_window": "Synthetic prior week",
                "recommended_actions": ["Review the remaining follow-up items."],
                "approval_required": True,
                "human_review_required": True,
                "send_enabled": False,
                "slack_post_allowed": False,
                "write_requests": [],
            },
            "usage": {"requests": 1},
            "cost": {"estimated_usd": 0.01},
        },
    }
    paths: dict[str, Path] = {}
    for name, payload in payloads.items():
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths[name] = path
    return paths


def _replays(tmp_path: Path):
    paths = _fixture_paths(tmp_path)
    return build_pilot_receipt_replays(
        opportunity_evidence_path=paths["opportunity"],
        research_model_path=paths["research_model"],
        research_plan_path=paths["research_plan"],
        weekly_evidence_path=paths["weekly"],
    )


def test_saved_pilot_receipts_prepare_answer_first_zero_model_slack_replays(
    tmp_path: Path,
) -> None:
    replays = _replays(tmp_path)

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


def test_replay_refuses_a_non_catalog_request_hash(tmp_path: Path) -> None:
    payload = _replays(tmp_path)[0].model_dump(mode="json")
    payload["natural_request_sha256"] = "0" * 64

    with pytest.raises(ValueError, match="does not match the catalog"):
        PilotReceiptReplay.model_validate(payload)


def test_replay_refuses_tampered_opportunity_evidence(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    source = paths["opportunity"]
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["safety"]["provider_writes"] = 1
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="no-write boundary"):
        build_pilot_receipt_replays(
            opportunity_evidence_path=tampered,
            research_model_path=paths["research_model"],
            research_plan_path=paths["research_plan"],
            weekly_evidence_path=paths["weekly"],
        )


def test_replay_refuses_weekly_evidence_outside_privacy_boundary(tmp_path: Path) -> None:
    paths = _fixture_paths(tmp_path)
    source = paths["weekly"]
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["raw_private_context_transmitted"] = True
    tampered = tmp_path / "weekly-tampered.json"
    tampered.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="privacy-minimized boundary"):
        build_pilot_receipt_replays(
            opportunity_evidence_path=paths["opportunity"],
            research_model_path=paths["research_model"],
            research_plan_path=paths["research_plan"],
            weekly_evidence_path=tampered,
        )
