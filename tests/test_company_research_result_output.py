from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import scripts.run_company_research as company_cli


def test_fixture_safety_overrides_env_backed_live_defaults() -> None:
    args = Namespace(
        fixture="tests/fixtures/sample_company_neuroflow.json",
        compare_fixture=None,
        live_search=True,
        dry_run=False,
    )

    result = company_cli._apply_fixture_safety_defaults(args, ["--fixture", args.fixture])

    assert result.live_search is False
    assert result.dry_run is True


def test_fixture_safety_preserves_explicit_live_flags() -> None:
    args = Namespace(
        fixture="tests/fixtures/sample_company_neuroflow.json",
        compare_fixture=None,
        live_search=True,
        dry_run=False,
    )

    result = company_cli._apply_fixture_safety_defaults(
        args,
        ["--fixture", args.fixture, "--live-search", "--no-dry-run"],
    )

    assert result.live_search is True
    assert result.dry_run is False


def test_company_research_parser_accepts_atomic_result_output(tmp_path: Path) -> None:
    target = tmp_path / "company-result.json"

    args = company_cli.build_parser().parse_args(
        ["--company", "Example Health", "--result-output", str(target)]
    )

    assert args.result_output == target


def test_company_research_result_output_is_atomic_and_complete(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "company-result.json"
    payload = {
        "output_type": "CompanyResearchFocusedBrief",
        "output": {"company_name": "Example Health", "sources": [{"url": "https://x.test"}]},
        "retrieval": {"mode": "supplied_sources", "raw_search_result_count": 1},
        "usage": {"requests": 1, "input_tokens": 100, "output_tokens": 20},
        "cost": {"estimated_usd": 0.02},
    }

    company_cli._write_result_output_atomic(target, payload)

    assert json.loads(target.read_text()) == payload
    assert not target.with_suffix(".json.tmp").exists()


def test_company_research_sdk_main_persists_before_rendering(
    tmp_path: Path, monkeypatch
) -> None:
    target = tmp_path / "sdk-result.json"
    payload = {
        "agent_name": "business_research_analyst",
        "live_sdk": True,
        "output_type": "CompanyResearchFocusedBrief",
        "output": {"company_name": "Example Health", "sources": []},
        "retrieval": {"mode": "supplied_sources"},
        "usage": {"requests": 1},
        "cost": {"estimated_usd": 0.01},
    }
    events: list[str] = []
    monkeypatch.setattr(company_cli, "_run_sdk_synthesis", lambda _args: payload)
    monkeypatch.setattr(
        company_cli,
        "_write_result_output_atomic",
        lambda path, value: events.append(f"persist:{path.name}:{value['output_type']}"),
    )
    monkeypatch.setattr("builtins.print", lambda *_args, **_kwargs: events.append("print"))
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_company_research.py",
            "--company",
            "Example Health",
            "--live-sdk",
            "--json",
            "--result-output",
            str(target),
        ],
    )

    assert company_cli.main() == 0
    assert events == [
        "persist:sdk-result.json:CompanyResearchFocusedBrief",
        "print",
    ]
