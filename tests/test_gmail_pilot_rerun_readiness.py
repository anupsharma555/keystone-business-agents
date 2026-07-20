from __future__ import annotations

import json
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

from keystone_agents.gmail_pilot_rerun_readiness import gmail_pilot_rerun_readiness


def test_gmail_pilot_rerun_contract_is_exact_bounded_and_no_write() -> None:
    readiness = gmail_pilot_rerun_readiness()

    assert readiness.case_id == "selected_gmail_thread_followup"
    assert len(readiness.natural_request_sha256) == 64
    assert readiness.backend == "langgraph"
    assert readiness.model == "gpt-5.4-mini"
    assert readiness.max_openai_requests == 2
    assert readiness.max_cost_usd == 0.10
    assert len(readiness.proof_nodeids) == 5
    assert len(readiness.required_human_review_checks) == 5
    assert any("more than two OpenAI requests" in item for item in readiness.live_stop_conditions)
    assert any("Gmail write" in item for item in readiness.live_stop_conditions)


def test_gmail_pilot_rerun_proof_nodes_exist() -> None:
    for nodeid in gmail_pilot_rerun_readiness().proof_nodeids:
        path_value, separator, test_name = nodeid.partition("::")
        assert separator and test_name.startswith("test_")
        text = Path(path_value).read_text(encoding="utf-8")
        assert f"def {test_name}(" in text


def test_gmail_pilot_rerun_runner_claims_only_offline_readiness(tmp_path: Path, capsys) -> None:
    script = Path("scripts/run_gmail_pilot_rerun_readiness.py")
    spec = spec_from_file_location("run_gmail_pilot_rerun_readiness", script)
    assert spec and spec.loader
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    output = tmp_path / "readiness.json"

    assert module.main(["--output", str(output)]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["status"] == "offline_ready_live_not_started"
    assert payload["proofs_passed"] is True
    assert payload["offline_no_repair_path_proven"] is True
    assert payload["human_review_passed"] is False
    assert payload["developer_intervention_free_live_run_proven"] is False
    assert payload["fresh_live_observation_required"] is True
    assert payload["openai_api_requests"] == 0
    assert payload["gmail_reads"] == 0
    assert payload["gmail_writes"] == 0
    assert payload["slack_posts"] == 0
    assert json.loads(capsys.readouterr().out) == payload
