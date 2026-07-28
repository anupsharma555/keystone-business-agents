from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path
from types import SimpleNamespace

import scripts.run_company_research as company_cli
from keystone_agents.manual_request import infer_manual_request_plan


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


def test_typed_comparison_plan_populates_existing_two_company_path() -> None:
    request = (
        "Business Research Analyst, compare Callyope and Kintsugi for voice-based "
        "mental-health assessment. Give me 5 concise but substantive bullets and "
        "include one official source URL for each company."
    )
    plan = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )

    assert company_cli._manual_plan_comparison_targets(
        plan.model_dump(mode="json")
    ) == ("Callyope", "Kintsugi")
    args = company_cli.build_parser().parse_args(["--request-text", request])
    args.manual_request_plan = plan.model_dump(mode="json")

    resolved = company_cli._apply_manual_request_plan(args)

    assert resolved.company == "Callyope"
    assert resolved.compare_company == "Kintsugi"


def test_required_entities_do_not_create_comparison_without_request_provenance() -> None:
    plan = {
        "target_agent": "business_research_analyst",
        "intent": "company_research",
        "target_type": "company",
        "primary_target": "Callyope",
        "objective": "Research the company and its market.",
        "required_entities": ["Callyope", "Kintsugi"],
    }

    assert company_cli._manual_plan_comparison_targets(plan) is None


def test_bounded_comparison_keeps_compact_retrieval_lane() -> None:
    request = (
        "Compare Callyope and Kintsugi. Give me 5 concise but substantive bullets. "
        "Include one official source URL for each company."
    )
    plan = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )
    args = company_cli.build_parser().parse_args(
        ["--company", "Callyope", "--compare-company", "Kintsugi"]
    )
    args.manual_request_plan = plan.model_dump(mode="json")

    result = company_cli._apply_interpreted_retrieval_mode(args)

    assert result.quick_retrieval is True
    assert result.max_results == 2


def test_comparison_bullet_summary_includes_one_official_domain_per_company() -> None:
    request = (
        "Compare Callyope and Kintsugi. Give me 5 concise but substantive bullets. "
        "Include one official source URL for each company."
    )
    plan = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )
    payload = {
        "output_type": "CompanyResearchFocusedBrief",
        "manual_request_plan": plan.model_dump(mode="json"),
        "comparison_entities": ["Callyope", "Kintsugi"],
        "retrieval": {
            "primary": {"resolved_company_url": "https://www.callyope.com"},
            "comparison": {"resolved_company_url": "https://www.kintsugihealth.com"},
        },
        "verified_source_evidence": [
            {
                "entity": "Callyope",
                "resolved_official_url": "https://www.callyope.com",
                "sources": [
                    {
                        "source_id": "company_a:official",
                        "title": "Callyope product",
                        "url": "https://www.callyope.com/faq",
                        "source_type": "company_page",
                    }
                ],
                "official_sources": [
                    {
                        "source_id": "company_a:official",
                        "title": "Callyope product",
                        "url": "https://www.callyope.com/faq",
                        "source_type": "company_page",
                    }
                ],
            },
            {
                "entity": "Kintsugi",
                "resolved_official_url": "https://www.kintsugihealth.com",
                "sources": [
                    {
                        "source_id": "company_b:official",
                        "title": "Kintsugi product",
                        "url": "https://www.kintsugihealth.com/technology",
                        "source_type": "company_page",
                    }
                ],
                "official_sources": [
                    {
                        "source_id": "company_b:official",
                        "title": "Kintsugi product",
                        "url": "https://www.kintsugihealth.com/technology",
                        "source_type": "company_page",
                    }
                ],
            },
        ],
        "output": {
            "company_name": "Callyope vs Kintsugi",
            "answer": (
                "- Products: Callyope supports psychiatric monitoring; Kintsugi "
                "focuses on voice biomarkers.\n"
                "- Shared modalities: Both analyze acoustic and linguistic voice signals.\n"
                "- Difference: Their supported workflows and evidence claims differ.\n"
                "- Verified: Each product claim is limited to its cited official page.\n"
                "- Uncertainty: Comparative clinical performance is not established."
            ),
            "sources": [
                {
                    "source_id": "callyope:official",
                    "title": "Callyope product",
                    "url": "https://www.callyope.com/faq",
                },
                {
                    "source_id": "callyope:second",
                    "title": "Callyope second page",
                    "url": "https://www.callyope.com/about",
                },
                {
                    "source_id": "kintsugi:official",
                    "title": "Kintsugi product",
                    "url": "https://www.kintsugihealth.com/technology",
                },
                {
                    "source_id": "kintsugi:invented",
                    "title": "Invented official-looking page",
                    "url": "https://www.kintsugihealth.com/invented",
                },
            ],
        },
    }

    summary = company_cli._company_research_sdk_human_summary(payload)
    company_cli._attach_company_research_display_text(payload, summary)
    company_cli._attach_company_research_output_constraint_validation(payload)

    assert summary.count("\n- *") == 4
    assert "https://www.callyope.com/faq" in summary
    assert "https://www.kintsugihealth.com/technology" in summary
    assert "https://www.callyope.com/about" not in summary
    assert "https://www.kintsugihealth.com/invented" not in summary
    assert payload["output_constraint_validation"]["passed"] is True
    assert (
        "one official source domain per comparison company"
        in payload["output_constraint_validation"]["satisfied_constraints"]
    )


def test_comparison_source_namespaces_rewrite_all_profile_references() -> None:
    profile = company_cli.research_company_fixture(
        company_name="Example Health",
        company_url=None,
        lead_name=None,
        linkedin_url=None,
        fixture_json=Path("tests/fixtures/sample_company_neuroflow.json"),
    )

    company_a = company_cli._namespace_company_profile_sources(
        profile,
        namespace="company_a",
    )
    company_b = company_cli._namespace_company_profile_sources(
        profile,
        namespace="company_b",
    )

    company_a_ids = {source.source_id for source in company_a.sources}
    company_b_ids = {source.source_id for source in company_b.sources}
    assert company_a_ids
    assert company_b_ids
    assert company_a_ids.isdisjoint(company_b_ids)
    assert all(claim.source_id in company_a_ids for claim in company_a.claims)
    assert all(feature.source_id in company_a_ids for feature in company_a.features)
    assert all(
        set(data_point.source_ids) <= company_a_ids
        for data_point in company_a.research_data_points
    )


def test_focused_comparison_run_passes_both_profiles_to_single_synthesis(
    monkeypatch,
) -> None:
    request = (
        "Compare Callyope and Kintsugi in 5 substantive bullets covering products, "
        "modalities, differences, verified claims, and uncertainties. Include one "
        "official URL for each company."
    )
    plan = infer_manual_request_plan(
        request,
        requested_agent="business_research_analyst",
    )
    args = company_cli.build_parser().parse_args(
        [
            "--company",
            "Callyope",
            "--compare-company",
            "Kintsugi",
            "--focused-brief",
            "--run-sdk",
            "--request-text",
            request,
        ]
    )
    args.manual_request_plan = plan.model_dump(mode="json")
    base_profile = company_cli.research_company_fixture(
        company_name="Callyope",
        company_url="https://www.callyope.com",
        lead_name=None,
        linkedin_url=None,
        fixture_json=Path("tests/fixtures/sample_company_neuroflow.json"),
    )
    company_a = company_cli._namespace_company_profile_sources(
        base_profile.model_copy(update={"name": "Callyope"}),
        namespace="company_a",
    )
    company_b = company_cli._namespace_company_profile_sources(
        base_profile.model_copy(
            update={"name": "Kintsugi", "website": "https://www.kintsugihealth.com"}
        ),
        namespace="company_b",
    )
    comparison = company_cli.compare_company_profiles_for_decision(
        company_a,
        company_b,
        decision_goal=request,
        criteria=company_cli.parse_company_research_comparison_criteria(None),
        requested_output_format=None,
    )
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        company_cli,
        "resolve_sdk_execution",
        lambda *_args, **_kwargs: (object(), False),
    )
    monkeypatch.setattr(
        company_cli,
        "_retrieve_company_comparison",
        lambda _args: (
            comparison,
            {
                "primary": {"resolved_company_url": "https://www.callyope.com"},
                "comparison": {
                    "resolved_company_url": "https://www.kintsugihealth.com"
                },
            },
            company_a,
            company_b,
        ),
    )

    def fake_run_retrieved_sdk_synthesis(**kwargs):
        normalized = kwargs["normalize"](kwargs["retrieve"]())
        captured["normalized"] = normalized
        captured["output_type"] = kwargs["output_type"]
        return SimpleNamespace(final_output=object())

    monkeypatch.setattr(
        company_cli,
        "run_retrieved_sdk_synthesis",
        fake_run_retrieved_sdk_synthesis,
    )
    monkeypatch.setattr(
        company_cli,
        "sdk_synthesis_payload",
        lambda *_args, **_kwargs: {
            "output_type": "CompanyResearchFocusedBrief",
            "output": {
                "company_name": "Callyope vs Kintsugi",
                "answer": "- Products: Evidence-backed comparison.",
                "sources": [],
            },
        },
    )

    payload = company_cli._run_sdk_synthesis(args)

    normalized = captured["normalized"]
    assert isinstance(normalized, company_cli.BusinessResearchFocusedBriefSDKInput)
    assert normalized.company_name == "Callyope vs Kintsugi"
    assert "company_a:" in normalized.source_context
    assert "company_b:" in normalized.source_context
    assert captured["output_type"] is company_cli.CompanyResearchFocusedBrief
    assert payload["comparison_entities"] == ["Callyope", "Kintsugi"]
    assert [entry["entity"] for entry in payload["verified_source_evidence"]] == [
        "Callyope",
        "Kintsugi",
    ]


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
