from __future__ import annotations

from types import SimpleNamespace

import scripts.run_airtable_synthetic_record_synthesis as runner
from keystone_agents.schemas.operational_context import AirtableContextResult


def test_synthetic_record_synthesis_cleans_up_and_stays_no_write(monkeypatch) -> None:
    state = {"record": False}

    def fake_write(*_args, **_kwargs):
        state["record"] = True
        return {"record_id": "rec-test", "verification": {"passed": True}}

    def fake_read(*_args, **_kwargs):
        return {"records": [{"id": "rec-test", "fields": {"Amount": 42}}]}

    def fake_delete(*_args, **_kwargs):
        state["record"] = False
        return {"verification": {"passed": True, "record_absent_after": True}}

    monkeypatch.setattr(runner, "airtable_write_record_impl", fake_write)
    monkeypatch.setattr(runner, "airtable_read_records_impl", fake_read)
    monkeypatch.setattr(runner, "airtable_delete_test_record_impl", fake_delete)

    def fake_model(*_args, **_kwargs):
        return SimpleNamespace(
            final_output=AirtableContextResult(
                summary=(
                    "Software expense: $42.00 plus $3.36 tax equals $45.36, paid by "
                    "business debit card; receipt is missing."
                ),
                relevant_tables=["Business Expenses"],
                recommended_actions=["Attach the missing receipt."],
            ),
            usage={"requests": 1},
            cost={"estimated_usd": 0.001},
            request_cache={"rate_limit_retries": 0},
        )

    result = runner.execute(
        model="gpt-5.4-mini",
        budget_usd=0.05,
        suffix="fixture",
        model_runner=fake_model,
    )

    assert result["status"] == "pass"
    assert result["checks"]["provider_deleted"] is True
    assert result["safety"]["record_absent_after"] is True
    assert state["record"] is False
    assert "rec-test" not in str(result)
