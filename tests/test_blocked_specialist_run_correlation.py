"""Regression proof for blocked child run identity in the public terminal packet."""

from __future__ import annotations

import json
from types import SimpleNamespace

from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.planning.compatibility import infer_manual_request_plan
from keystone_agents.storage.sqlite_store import SQLiteStore


def test_blocked_specialist_public_result_carries_durable_agent_run_id(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'blocked-specialist.db'}"
    child_payload = {
        "status": "blocked",
        "block_kind": "gmail_agent_decision_validation_failed",
        "agent_name": "gmail_triage",
        "human_summary": "The bounded Gmail decision did not pass validation.",
        "output": {
            "summary": "The bounded Gmail decision did not pass validation.",
            "send_enabled": False,
            "draft_created": False,
        },
        "request_cache": {
            "decision_ownership": {
                "validator_outcome": {
                    "status": "rejected",
                    "reason_code": "candidate_assessments_incomplete",
                }
            }
        },
    }
    monkeypatch.setattr(
        cli,
        "run_isolated_child_process",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(child_payload),
            stderr="",
        ),
    )

    request = "Find the current interview conversation and keep Gmail unchanged."
    exit_code = cli._run_ask_script_live(
        "gmail_triage",
        request,
        ["unused-child-command"],
        json_output=True,
        manual_plan=infer_manual_request_plan(
            request,
            requested_agent="gmail_triage",
        ),
        database_url=database_url,
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "blocked"
    assert isinstance(payload["agent_run_id"], int)
    assert payload["public_result"]["run_id"] == str(payload["agent_run_id"])
    stored = SQLiteStore(database_url).get_agent_run(payload["agent_run_id"])
    assert stored is not None
    assert stored["status"] == "blocked"
