from __future__ import annotations

import json
from pathlib import Path

from scripts import run_presentation_index_validation as runner


def test_runner_preview_has_no_model_or_write(tmp_path: Path, monkeypatch, capsys) -> None:
    output = tmp_path / "receipt.json"
    monkeypatch.setattr(
        "sys.argv",
        ["run_presentation_index_validation.py", "--output", str(output)],
    )

    assert runner.main() == 0
    capsys.readouterr()
    payload = json.loads(output.read_text(encoding="utf-8"))

    assert payload["status"] == "preview"
    assert payload["openai_requests"] == 0
    assert payload["provider_writes"] == 0
    assert payload["parent_modified"] is False
