from __future__ import annotations

from copy import deepcopy

import pytest

from keystone_agents.runtime.execution_attempt import start_execution_attempt
from keystone_agents.runtime.request_budget import activate_model_request_budget
from keystone_agents.storage.sqlite_store import SQLiteStore


def _preflight(*, execution_id: str = "planner-execution") -> dict:
    return {
        "agent_name": "manual_request_planner",
        "run_stage": "orchestrator_preflight.manual_request_planner",
        "usage": {"requests": 1, "total_tokens": 200, "provider_request_count_confirmed": True},
        "cost": {"estimated_usd": 0.002},
        "execution_telemetry": {"run_id": execution_id} if execution_id else {},
    }


def _child(events: list[dict], *, requests: int = 2) -> dict:
    return {
        "usage": {
            "requests": requests,
            "total_tokens": 300,
            "provider_request_count_confirmed": True,
        },
        "cost": {"estimated_usd": 0.003},
        "orchestrator_preflight": {"sdk_usage_events": events},
    }


def _repair(
    *,
    trace_id: str = "sdk_run_repair_1234567890",
    input_tokens: int = 60,
    output_tokens: int = 10,
) -> dict:
    return {
        "repair_attempted": True,
        "repair_succeeded": True,
        "repair_usage": {
            "available": True,
            "complete": True,
            "provider_request_count_confirmed": True,
            "requests": 1,
            "input_tokens": input_tokens,
            "cached_input_tokens": 0,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "repair_cost": {"estimated_usd": 0.001},
        "repair_request_cache": {
            "trace_summary": {"trace_id": trace_id, "event_id": 3},
        },
        "repair_execution_telemetry": {
            "status": "completed",
            "total_duration_ms": 10.0,
        },
    }


def _finalize(store: SQLiteStore, rows: list[int], *, consumed: int) -> dict:
    with activate_model_request_budget(8) as ledger:
        attempt = start_execution_attempt(
            store=store,
            request_text="Read the synthetic evidence.",
            route_hint="airtable_context_agent",
            live=True,
            max_model_requests=8,
        )
        for _ in range(consumed):
            ledger.consume(stage="synthetic:llm_start")
        return attempt.finalize(status="completed", exit_code=0, linked_agent_run_ids=rows)


def test_entry_usage_includes_preflight_once_across_copied_child_outputs(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'accounting.db'}")
    event = _preflight()
    first = store.save_agent_run(agent_name="airtable_context_agent", output=_child([event]))
    second = store.save_agent_run(
        agent_name="chief_of_staff", output=_child([deepcopy(event)], requests=1)
    )

    result = _finalize(store, [first, second, first], consumed=4)

    stages = result["decision_trace"]["stages"]
    assert len(stages) == 3
    assert stages[0]["agent"] == "manual_request_planner"
    assert result["decision_trace"]["agent_run_count"] == 2
    assert result["request_budget"]["observed_model_requests"] == 4
    assert result["request_budget"]["observed_count_confirmed"] is True
    assert sum(stage["consumption"]["usage"]["total_tokens"] for stage in stages) == 800
    assert sum(stage["consumption"]["cost"]["estimated_usd"] for stage in stages) == 0.008
    assert store.get_agent_run(result["attempt_id"])["output"] == result


def test_entry_usage_counts_distinct_instruction_repair_once(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'repair-accounting.db'}")
    child_output = _child([_preflight()])
    child_output["instruction_following"] = _repair()
    child = store.save_agent_run(
        agent_name="preprints_context_agent",
        output=child_output,
    )

    result = _finalize(store, [child], consumed=4)

    assert [stage["agent"] for stage in result["decision_trace"]["stages"]] == [
        "manual_request_planner",
        "preprints_context_agent",
        "instruction_following_repair",
    ]
    assert result["request_budget"]["observed_model_requests"] == 4
    assert result["request_budget"]["usage_reconciled_with_ledger"] is True
    assert result["request_budget"]["observed_count_confirmed"] is True


def test_instruction_repair_alias_copy_is_not_counted_twice(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'repair-alias.db'}")
    repair = _repair()
    child_output = _child([_preflight()])
    child_output["instruction_following"] = repair
    child_output["public_result"] = {"instruction_following": deepcopy(repair)}
    child = store.save_agent_run(
        agent_name="preprints_context_agent",
        output=child_output,
    )

    result = _finalize(store, [child], consumed=4)

    repair_stages = [
        stage
        for stage in result["decision_trace"]["stages"]
        if stage["agent"] == "instruction_following_repair"
    ]
    assert len(repair_stages) == 1
    assert result["request_budget"]["observed_model_requests"] == 4
    assert result["request_budget"]["observed_count_confirmed"] is True


def test_distinct_identical_token_repairs_keep_separate_trace_identities(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'distinct-repairs.db'}")
    event = _preflight()
    first_output = _child([event], requests=1)
    first_output["instruction_following"] = _repair(
        trace_id="sdk_run_repair_first_1234567890",
    )
    second_output = _child([deepcopy(event)], requests=1)
    second_output["instruction_following"] = _repair(
        trace_id="sdk_run_repair_second_1234567890",
    )
    first = store.save_agent_run(agent_name="first_specialist", output=first_output)
    second = store.save_agent_run(agent_name="second_specialist", output=second_output)

    result = _finalize(store, [first, second], consumed=5)

    repair_stages = [
        stage
        for stage in result["decision_trace"]["stages"]
        if stage["agent"] == "instruction_following_repair"
    ]
    assert len(repair_stages) == 2
    assert result["request_budget"]["observed_model_requests"] == 5
    assert result["request_budget"]["usage_reconciled_with_ledger"] is True


def test_missing_instruction_repair_usage_remains_incomplete(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'missing-repair-usage.db'}")
    child_output = _child([_preflight()], requests=1)
    child_output["instruction_following"] = {
        "repair_attempted": True,
        "repair_succeeded": False,
        "repair_request_cache": {
            "trace_summary": {"trace_id": "sdk_run_missing_repair", "event_id": 3},
        },
    }
    child = store.save_agent_run(agent_name="synthetic_specialist", output=child_output)

    result = _finalize(store, [child], consumed=3)

    assert result["request_budget"]["observed_model_requests"] == 2
    assert result["request_budget"]["observed_count_confirmed"] is False
    assert result["request_budget"]["usage_reconciled_with_ledger"] is False
    assert "instruction_repair_usage_missing" in result["unavailable_evidence"]


def test_conflicting_instruction_repair_identity_remains_incomplete(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'conflicting-repair.db'}")
    event = _preflight()
    first_output = _child([event], requests=1)
    first_output["instruction_following"] = _repair(input_tokens=60)
    second_output = _child([deepcopy(event)], requests=1)
    second_output["instruction_following"] = _repair(input_tokens=61)
    first = store.save_agent_run(agent_name="first_specialist", output=first_output)
    second = store.save_agent_run(agent_name="second_specialist", output=second_output)

    result = _finalize(store, [first, second], consumed=4)

    assert result["request_budget"]["observed_model_requests"] == 4
    assert result["request_budget"]["observed_count_confirmed"] is False
    assert "instruction_repair_usage_identity_conflict" in result["unavailable_evidence"]


def test_captured_preflight_does_not_hide_unreported_later_dispatch(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'captured-partial.db'}")
    with activate_model_request_budget(3) as ledger:
        attempt = start_execution_attempt(
            store=store, request_text="Synthetic request", route_hint="orchestrator",
            live=True, max_model_requests=3,
        )
        ledger.consume(stage="preflight:llm_start")
        attempt.record_preflight({"sdk_usage_events": [_preflight()]})
        ledger.consume(stage="unreported_later:llm_start")
        result = attempt.finalize(
            status="failed", exit_code=1, failure=RuntimeError("Synthetic later failure"),
        )
    assert result["request_budget"]["consumed"] == 2
    assert result["request_budget"]["observed_model_requests"] == 1
    assert result["request_budget"]["observed_count_confirmed"] is False
    assert result["request_budget"]["usage_reconciled_with_ledger"] is False
    terminal_consumption = result["decision_trace"]["stages"][-1]["consumption"]
    assert terminal_consumption["model_request_count_confirmed"] is False


def test_preflight_already_linked_as_own_run_is_not_counted_twice(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'linked-preflight.db'}")
    event = _preflight()
    planner = store.save_agent_run(agent_name="manual_request_planner", output=event)
    child = store.save_agent_run(agent_name="airtable_context_agent", output=_child([event]))

    result = _finalize(store, [planner, child], consumed=3)

    assert len(result["decision_trace"]["stages"]) == 2
    assert result["request_budget"]["observed_model_requests"] == 3
    assert result["request_budget"]["observed_count_confirmed"] is True


def test_later_preflight_stays_adjacent_to_its_specialist_stage(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'ordered-preflight.db'}")
    first = store.save_agent_run(
        agent_name="airtable_context_agent",
        output=_child([_preflight(execution_id="first-planner")], requests=1),
    )
    second = store.save_agent_run(
        agent_name="chief_of_staff",
        output=_child([_preflight(execution_id="second-planner")], requests=1),
    )

    result = _finalize(store, [first, second], consumed=4)

    assert [stage["agent"] for stage in result["decision_trace"]["stages"]] == [
        "manual_request_planner", "airtable_context_agent",
        "manual_request_planner", "chief_of_staff",
    ]


@pytest.mark.parametrize("identified", [True, False])
def test_distinct_preflight_attempts_with_equal_usage_are_preserved(tmp_path, identified) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'repeated-preflight.db'}")
    events = [
        _preflight(execution_id="planner-first" if identified else ""),
        _preflight(execution_id="planner-second" if identified else ""),
    ]
    child = store.save_agent_run(
        agent_name="airtable_context_agent", output=_child(events, requests=1)
    )

    result = _finalize(store, [child], consumed=3)

    assert len(result["decision_trace"]["stages"]) == 3
    assert result["request_budget"]["observed_model_requests"] == 3


def test_unreported_consumed_attempt_is_not_mislabeled_confirmed_usage(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'partial-usage.db'}")
    child = store.save_agent_run(agent_name="airtable_context_agent", output=_child([_preflight()]))

    result = _finalize(store, [child], consumed=4)

    budget = result["request_budget"]
    assert budget["consumed"] == 4
    assert budget["observed_model_requests"] == 3
    assert budget["observed_count_confirmed"] is False
    assert budget["usage_reconciled_with_ledger"] is False
    assert budget["within_ceiling"] is True
    assert "request_usage_reconciliation_incomplete" in result["unavailable_evidence"]
    assert result["decision_trace"]["evaluation_status"] != "pass"


def test_missing_linked_row_prevents_confirmed_total(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'missing-usage.db'}")
    child = store.save_agent_run(agent_name="airtable_context_agent", output=_child([]))

    result = _finalize(store, [child, child + 100], consumed=2)

    assert result["request_budget"]["observed_model_requests"] == 2
    assert result["request_budget"]["observed_count_confirmed"] is False
    assert "linked_agent_run_record_not_found" in result["unavailable_evidence"]


def test_placeholder_usage_does_not_confirm_a_zero_call_preflight(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'unavailable-usage.db'}")
    event = _preflight()
    event["usage"] = {"requests": 0, "available": False}
    child = store.save_agent_run(agent_name="airtable_context_agent", output=_child([event]))

    result = _finalize(store, [child], consumed=2)

    assert result["request_budget"]["observed_model_requests"] == 2
    assert result["request_budget"]["observed_count_confirmed"] is False
    assert result["decision_trace"]["stages"][0]["consumption"][
        "model_request_count_confirmed"
    ] is False


def test_conflicting_copies_of_one_preflight_do_not_confirm_total(tmp_path) -> None:
    store = SQLiteStore(f"sqlite:///{tmp_path / 'conflicting-usage.db'}")
    original = _preflight()
    conflicting = deepcopy(original)
    conflicting["usage"]["requests"] = 2
    first = store.save_agent_run(
        agent_name="airtable_context_agent", output=_child([original], requests=1)
    )
    second = store.save_agent_run(
        agent_name="chief_of_staff", output=_child([conflicting], requests=1)
    )

    result = _finalize(store, [first, second], consumed=3)

    assert len(result["decision_trace"]["stages"]) == 3
    assert result["request_budget"]["observed_count_confirmed"] is False
    assert "conflicting_copied_preflight_usage" in result["unavailable_evidence"]


@pytest.mark.parametrize(
    "trace_ids",
    [["sdk_run_1234567890abcdef"], ["sdk_run_1234567890abcdef", "sdk_run_abcdef1234567890"]],
)
def test_captured_trace_identity_deduplicates_copies_but_not_equal_usage(tmp_path, trace_ids):
    store = SQLiteStore(f"sqlite:///{tmp_path / 'projected-preflight.db'}")
    events = []
    for trace_id in trace_ids:
        event = _preflight(execution_id="")
        event["request_cache"] = {
            "trace_summary": {"trace_id": trace_id, "event_id": 7, "raw_input": "PRIVATE_SENTINEL"},
            "raw_prompt": "PRIVATE_SENTINEL",
        }
        events.append(event)
    with activate_model_request_budget(8) as ledger:
        attempt = start_execution_attempt(
            store=store, request_text="Synthetic request", route_hint="orchestrator",
            live=True, max_model_requests=8,
        )
        for _ in events:
            ledger.consume(stage="preflight:llm_start")
        attempt.record_preflight({"sdk_usage_events": events})
        observed = store.get_agent_run(attempt.run_id)["output"]
        for event in observed["orchestrator_preflight_observation"]["sdk_usage_events"]:
            assert set(event["request_cache"]["trace_summary"]) == {"trace_id", "event_id"}
            assert "PRIVATE_SENTINEL" not in str(event)
        child = store.save_agent_run(agent_name="gmail_triage", output=_child(deepcopy(events)))
        for _ in range(2):
            ledger.consume(stage="child:llm_start")
        result = attempt.finalize(status="completed", exit_code=0, linked_agent_run_id=child)

    assert result["decision_trace"]["model_request_count"] == len(events) + 2
    assert result["request_budget"]["usage_reconciled_with_ledger"] is True
    assert result["request_budget"]["observed_count_confirmed"] is True
    assert len(result["decision_trace"]["stages"]) == len(events) + 1


@pytest.mark.parametrize(
    "trace_id,event_id",
    [("https://example.test/private", 7), ("x" * 161, 7),
     ("sdk_run_example", -1), ("sdk_run_example", True)],
)
def test_preflight_identity_projection_rejects_unbounded_or_nonopaque_values(
    tmp_path, trace_id, event_id,
):
    store = SQLiteStore(f"sqlite:///{tmp_path / 'unsafe-identity.db'}")
    attempt = start_execution_attempt(
        store=store, request_text="Synthetic request", route_hint="orchestrator", live=False,
    )
    event = _preflight(execution_id="")
    event["request_cache"] = {"trace_summary": {"trace_id": trace_id, "event_id": event_id}}
    attempt.record_preflight({"sdk_usage_events": [event]})
    observed = store.get_agent_run(attempt.run_id)["output"]["orchestrator_preflight_observation"]
    assert "trace_summary" not in observed["sdk_usage_events"][0]["request_cache"]
