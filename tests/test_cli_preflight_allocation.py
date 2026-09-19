"""Optional preflight repair cannot spend the specialist's protected allowance."""

import json
import shutil
from pathlib import Path

import pytest

from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.runtime.request_budget import current_model_request_budget


def _exercise_cli(
    tmp_path, monkeypatch, capsys, *, ceiling, fail=None, context_file=None, prompt=None,
):
    captured = {}
    original_estimate = cli._estimate_ask_openai_requests

    def estimate(*args, **kwargs):
        captured["parent"] = current_model_request_budget()
        return original_estimate(*args, **kwargs)

    def preflight(request, **kwargs):
        ledger = current_model_request_budget()
        captured.update(request=request, allocation=ledger.limit, preflight_calls=0)
        ledger.consume(stage="orchestrator:initial")
        captured["preflight_calls"] += 1
        if fail == "exception":
            raise RuntimeError("Synthetic preflight failure after one model request")
        if fail == "extra_repair":
            ledger.consume(stage="orchestrator:optional_repair")
            captured["preflight_calls"] += 1
        return object()

    def after_preflight(_result):
        parent = current_model_request_budget()
        assert parent is captured["parent"]
        captured["specialist_remaining"] = parent.remaining
        # Stand in only for the two mandatory model boundaries; routing is not
        # tested or overridden by this budget-allocation regression.
        parent.consume(stage="specialist:initial")
        parent.consume(stage="specialist:finish")
        raise RuntimeError("Synthetic stop after protected specialist boundaries")

    monkeypatch.setattr(cli, "_estimate_ask_openai_requests", estimate)
    monkeypatch.setattr(cli, "run_orchestrator_preflight", preflight)
    monkeypatch.setattr(cli, "_capture_entry_preflight_observation", after_preflight)
    arguments = [
        "ask", "--agent", "chief_of_staff", "--live-sdk", "--json",
        "--database-url", f"sqlite:///{tmp_path / 'allocation.sqlite3'}",
        "--max-openai-requests", str(ceiling),
    ]
    if context_file is not None:
        arguments.extend(["--context-file", str(context_file)])
    arguments.append(prompt or "Describe the Gmail mailbox query schema. Do not read messages.")
    result = cli.main(arguments)
    captured["exit"] = result
    captured["output"] = json.loads(capsys.readouterr().out)
    captured["snapshot"] = captured["parent"].snapshot()
    return captured


@pytest.mark.parametrize("ceiling,allocation", [(3, 1), (4, 2)])
def test_cli_initial_preflight_preserves_two_specialist_calls(
    tmp_path, monkeypatch, capsys, ceiling, allocation,
):
    result = _exercise_cli(tmp_path, monkeypatch, capsys, ceiling=ceiling)
    assert result["allocation"] == allocation
    assert result["preflight_calls"] == 1
    assert result["specialist_remaining"] == ceiling - 1
    assert result["snapshot"]["limit"] == ceiling
    assert result["snapshot"]["consumed"] == 3
    assert result["snapshot"]["reserved"] == 0


@pytest.mark.parametrize("failure", ["exception", "extra_repair"])
def test_cli_preflight_exception_charges_once_and_releases_protected_capacity(
    tmp_path, monkeypatch, capsys, failure,
):
    result = _exercise_cli(tmp_path, monkeypatch, capsys, ceiling=3, fail=failure)
    assert result["allocation"] == 1
    assert result["preflight_calls"] == 1
    assert "specialist_remaining" not in result
    assert result["snapshot"]["consumed"] == 1
    assert result["snapshot"]["remaining"] == 2
    assert result["snapshot"]["reserved"] == 0


def test_cli_allows_optional_preflight_repair_only_with_affordable_headroom(
    tmp_path, monkeypatch, capsys,
):
    result = _exercise_cli(tmp_path, monkeypatch, capsys, ceiling=4, fail="extra_repair")
    assert result["preflight_calls"] == 2
    assert result["specialist_remaining"] == 2
    assert result["snapshot"]["consumed"] == 4
    assert result["snapshot"]["reserved"] == 0


def test_cli_does_not_spend_when_initial_preflight_and_specialist_minimum_do_not_fit(
    tmp_path, monkeypatch, capsys,
):
    result = _exercise_cli(tmp_path, monkeypatch, capsys, ceiling=2)
    assert "preflight_calls" not in result
    assert result["snapshot"]["consumed"] == 0
    assert result["output"]["status"] == "blocked"


def test_saved_followup_replay_reaches_one_call_preflight_without_touching_saved_state(
    tmp_path, monkeypatch, capsys, require_local_evidence,
):
    packet = Path(".keystone/v2/slack-two-turn-reproof-20260911")
    profile_path = require_local_evidence(packet / "acceptance-profile.json")
    profile = json.loads(profile_path.read_text())
    contexts = sorted(Path(profile["state_dir"]).glob("accepted-context-*.json"))
    followups = [path for path in contexts if (lambda value:
        value.get("request_ts") != value.get("thread_ts"))(json.loads(path.read_text()))]
    if not followups:
        pytest.skip("Private accepted follow-up context is not retained in this checkout")
    original = followups[-1].read_bytes()
    copied = tmp_path / "followup-context.json"
    shutil.copyfile(followups[-1], copied)
    context = json.loads(original)
    result = _exercise_cli(
        tmp_path, monkeypatch, capsys, ceiling=3,
        context_file=copied, prompt=context["request_text"],
    )
    assert result["request"] == "Make that one sentence, keeping the Gmail link."
    assert result["allocation"] == 2 and result["preflight_calls"] == 1
    assert result["specialist_remaining"] == 2
    assert result["snapshot"]["consumed"] == 3
    assert followups[-1].read_bytes() == original


def test_planned_fresh_gmail_read_still_blocks_before_provider_when_capacity_is_insufficient(
    tmp_path, monkeypatch, capsys,
):
    from keystone_agents.agents.orchestrator import OrchestratorPreflight
    from keystone_agents.schemas.manual_request_plan import ManualRequestPlan
    from keystone_agents.schemas.orchestrator import OrchestratorResult

    request = "Find a different newsletter in Gmail."
    wrapper = (
        "business agents continue this prior Slack thread.\n"
        "Previous result title: Gmail Triage\nPrevious result: A saved source-backed summary.\n"
        f"User follow-up: {request}\nContinue the same agent task."
    )
    context = tmp_path / "thread.json"
    context.write_text(json.dumps({
        "schema": "keystone.slack.history_context.v1", "source": "slack_app_mention_history",
        "channel_id": "C_SYNTHETIC", "thread_ts": "123.000001", "request_ts": "123.000002",
        "request_text": wrapper, "thread_messages": [], "thread_fetch_status": "ok",
    }))
    calls = []

    def planned_read(current_request, **kwargs):
        calls.append(current_request)
        current_model_request_budget().consume(stage="orchestrator:initial")
        return OrchestratorPreflight(
            request_text=current_request, selected_agent="gmail_triage",
            manual_request_plan=ManualRequestPlan(
                source="canonical:test", requested_agent="chief_of_staff",
                target_agent="gmail_triage", intent="gmail_triage", objective=current_request,
                provider_system="gmail", provider_operations=["search", "read"],
                provider_read_scope="bounded_collection", target_type="gmail_message_collection",
                task_objective="gmail_triage", expected_artifact_type="gmail_triage_report",
            ),
            route_result=OrchestratorResult(route="gmail_triage", routing_mode="llm"),
            sdk_usage_events=[{"agent_name": "orchestrator", "usage": {"requests": 1}}],
        )

    monkeypatch.setattr(cli, "run_orchestrator_preflight", planned_read)
    monkeypatch.setattr(cli, "_run_ask_script_live", lambda *_args, **_kwargs:
                        pytest.fail("No provider child may run before the planned budget fits"))
    code = cli.main([
        "ask", "--agent", "chief_of_staff", "--live-sdk", "--json",
        "--max-openai-requests", "3", "--context-file", str(context),
        "--database-url", f"sqlite:///{tmp_path / 'planned.sqlite3'}", wrapper,
    ])
    payload = json.loads(capsys.readouterr().out)
    assert code == 2 and calls == [request]
    assert payload["status"] == "blocked"
    assert payload["openai_requests_made"] == 1
    gmail = next(row for row in payload["estimated_requests"]["stage_rows"]
                 if row["stage"] == "gmail_triage_direct_sdk")
    assert gmail["min_requests"] == 3
