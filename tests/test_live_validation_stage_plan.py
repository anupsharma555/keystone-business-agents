from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

STAGE_PLAN = Path("artifacts/test-pack/next-live-serial-validation-stage.json")


def test_next_live_stage_is_serial_bounded_and_freshly_gated() -> None:
    plan = json.loads(STAGE_PLAN.read_text(encoding="utf-8"))

    assert plan["approval_status"] == "fresh_ceiling_required"
    assert plan["execution_ready"] is False
    assert plan["remaining_blocker"] == (
        "fresh explicit 5-request / $0.15 ceiling approval"
    )
    assert plan["prior_stage_status"] == "eight-call allowance exhausted"
    assert plan["model"] == "gpt-5.4-mini"
    assert plan["serial_execution"] is True
    assert plan["expected_openai_requests"] == 5
    assert plan["hard_max_openai_requests"] == 5
    assert plan["combined_budget_usd"] == 0.15
    assert plan["billing_baseline"]["credit_balance_usd"] == 2.93
    assert plan["billing_baseline"]["observed_at"] == "2026-07-11T07:51:37.551Z"
    assert [item["expected_requests"] for item in plan["run_order"]] == [1, 1, 3]
    assert sum(item["expected_requests"] for item in plan["run_order"]) == 5
    assert sum(Decimal(str(item["budget_usd"])) for item in plan["run_order"]) == Decimal(
        "0.15"
    )
    assert plan["offline_preflight"]["status"] == "pass"
    assert plan["offline_preflight"]["scripts_present"] is True
    assert plan["offline_preflight"]["output_and_session_paths_clear"] is True
    assert plan["offline_preflight"]["secrets_printed"] is False
    assert all(plan["offline_preflight"]["credential_presence"].values())


def test_next_live_stage_stops_on_first_failure_retry_or_missing_evidence() -> None:
    plan = json.loads(STAGE_PLAN.read_text(encoding="utf-8"))
    stop_text = " ".join(plan["stage_stop_conditions"]).lower()

    for required in (
        "first failed or partial",
        "any retry",
        "unexpected request count",
        "missing usage",
        "unplanned side effect",
        "$0.15 ceiling",
    ):
        assert required in stop_text
