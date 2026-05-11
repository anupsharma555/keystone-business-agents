from __future__ import annotations

import json
from pathlib import Path

from keystone_agents.cli import main


def _database_url(tmp_path: Path) -> str:
    return f"sqlite:///{tmp_path / 'cli_work_items.db'}"


def test_cli_work_items_advance_and_show(tmp_path: Path, capsys) -> None:
    database_url = _database_url(tmp_path)

    exit_code = main(
        [
            "work-items",
            "advance",
            "--input",
            "research Lindus Health",
            "--database-url",
            database_url,
        ]
    )

    output = capsys.readouterr().out
    work_item_id = next(
        line.split(":", maxsplit=1)[1].strip()
        for line in output.splitlines()
        if line.startswith("WorkItem:")
    )
    assert exit_code == 0
    assert "Route: business_research_analyst" in output

    show_exit = main(["work-items", "show", work_item_id, "--database-url", database_url])

    assert show_exit == 0
    assert "Status: in_progress" in capsys.readouterr().out


def test_cli_work_items_list_json(tmp_path: Path, capsys) -> None:
    database_url = _database_url(tmp_path)
    main(
        [
            "work-items",
            "advance",
            "--input",
            "find behavioral health AI companies",
            "--database-url",
            database_url,
        ]
    )
    capsys.readouterr()

    exit_code = main(["work-items", "list", "--database-url", database_url, "--json"])

    assert exit_code == 0
    assert '"kind": "opportunity"' in capsys.readouterr().out


def test_cli_work_items_artifact_selection_and_approval(tmp_path: Path, capsys) -> None:
    database_url = _database_url(tmp_path)
    main(
        [
            "work-items",
            "advance",
            "--input",
            "research NeuroFlow",
            "--database-url",
            database_url,
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    work_item_id = payload["work_item"]["id"]
    artifact_id = payload["artifact_refs"][0]["artifact_id"]

    approve_exit = main(
        [
            "work-items",
            "approve-context",
            work_item_id,
            "--artifact",
            f"company_profile:{artifact_id}",
            "--database-url",
            database_url,
        ]
    )

    assert approve_exit == 0
    assert "Approved company_profile" in capsys.readouterr().out

    artifacts_exit = main(["work-items", "artifacts", work_item_id, "--database-url", database_url])

    assert artifacts_exit == 0
    artifacts_output = capsys.readouterr().out
    assert "approved_for_drafting" in artifacts_output
    assert "yes" in artifacts_output

    timeline_exit = main(["work-items", "timeline", work_item_id, "--database-url", database_url])

    assert timeline_exit == 0
    assert "context_approved" in capsys.readouterr().out


def test_cli_work_items_continue_resolves_single_active_item(tmp_path: Path, capsys) -> None:
    database_url = _database_url(tmp_path)
    main(
        [
            "work-items",
            "advance",
            "--input",
            "research NeuroFlow",
            "--database-url",
            database_url,
        ]
    )
    first_output = capsys.readouterr().out
    work_item_id = next(
        line.split(":", maxsplit=1)[1].strip()
        for line in first_output.splitlines()
        if line.startswith("WorkItem:")
    )

    exit_code = main(
        ["work-items", "advance", "--input", "continue", "--database-url", database_url]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert f"WorkItem: {work_item_id}" in output
    assert "Route: opportunity_scout" in output
