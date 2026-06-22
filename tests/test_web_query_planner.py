from __future__ import annotations

from argparse import Namespace

from keystone_agents.agents.web_query_planner import resolve_web_query_plan
from keystone_agents.schemas.web_query_plan import WebQueryPlan


def test_web_query_planner_returns_fallback_without_live() -> None:
    plan = resolve_web_query_plan(
        subject="Curebase",
        request_text="research recent partnerships",
        fallback_queries=[
            "Curebase official website",
            "Curebase recent news",
        ],
        live=False,
    )

    assert plan.source == "heuristic"
    assert plan.queries == ["Curebase official website", "Curebase recent news"]


def test_company_research_query_builder_uses_live_query_plan(monkeypatch) -> None:
    import scripts.run_company_research as cli

    captured: dict[str, object] = {}

    def fake_resolve_web_query_plan(**kwargs):
        captured.update(kwargs)
        return WebQueryPlan(
            source="llm",
            subject="Curebase",
            request_text=str(kwargs.get("request_text") or ""),
            queries=[
                "Curebase 2026 partnerships",
                "Curebase independent coverage clinical trials",
            ],
        )

    monkeypatch.setattr(cli, "resolve_web_query_plan", fake_resolve_web_query_plan)
    args = Namespace(
        improvement_case=None,
        live_search_plan=True,
        request_text="research Curebase for recent clinical trial partnerships",
        research_goal="",
        notes="",
    )

    queries = cli._company_query_builder_for_args(args)("Curebase", None)  # noqa: SLF001

    assert queries == [
        "Curebase 2026 partnerships",
        "Curebase independent coverage clinical trials",
    ]
    assert captured["subject"] == "Curebase"
    assert captured["live"] is True
    assert "clinical trial partnerships" in str(captured["request_text"])
