from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCORECARD = ROOT / "docs" / "ANU174_LINEAR_TASK_EVIDENCE.md"


def _task_rows(text: str) -> list[tuple[str, str]]:
    return re.findall(
        r"^\| (L174-\d{2}) \| .*? \| (PASS|PARTIAL|PENDING) \|",
        text,
        flags=re.MULTILINE,
    )


def test_anu174_linear_scorecard_has_exactly_twenty_unique_rows() -> None:
    text = SCORECARD.read_text(encoding="utf-8")
    rows = _task_rows(text)

    assert [task_id for task_id, _ in rows] == [
        f"L174-{index:02d}" for index in range(1, 21)
    ]
    assert len({task_id for task_id, _ in rows}) == 20


def test_anu174_linear_scorecard_totals_match_rows() -> None:
    text = SCORECARD.read_text(encoding="utf-8")
    rows = _task_rows(text)
    statuses = ("PASS", "PARTIAL", "PENDING")
    counts = {
        status: sum(row_status == status for _, row_status in rows) for status in statuses
    }

    assert counts == {"PASS": 16, "PARTIAL": 4, "PENDING": 0}
    assert "- Total: **20**" in text
    assert "Promptfoo remains deferred" in text
    smoke_doc = (ROOT / "docs" / "BASIC_AGENT_EXECUTION_SMOKE_TASKS.md").read_text(
        encoding="utf-8"
    )
    assert "cannot close" in smoke_doc
