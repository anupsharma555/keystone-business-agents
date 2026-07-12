from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "run_anu175_no_live_smoke.py"
SPEC = importlib.util.spec_from_file_location("run_anu175_no_live_smoke", SCRIPT_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_smoke_command_uses_only_focused_offline_proofs() -> None:
    command = MODULE.smoke_command()

    assert command[1:3] == ["-m", "pytest"]
    assert command[-1] == "-q"
    assert len(MODULE.PROOF_NODEIDS) == 4
    assert all(nodeid.startswith("tests/") for nodeid in MODULE.PROOF_NODEIDS)


def test_receipt_makes_no_live_and_comparison_boundaries_explicit(tmp_path: Path) -> None:
    output = tmp_path / "receipt.json"

    MODULE._write_receipt(str(output), command=MODULE.smoke_command(), passed=True)
    receipt = json.loads(output.read_text(encoding="utf-8"))

    assert receipt["issue"] == "ANU-175"
    assert receipt["passed"] is True
    assert receipt["openai_api_requests"] == 0
    assert receipt["live_connectors"] is False
    assert receipt["provider_writes"] == 0
    assert receipt["external_side_effects"] is False
    assert "does not replace" in receipt["comparative_claim_boundary"]
