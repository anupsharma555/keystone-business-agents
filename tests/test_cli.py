from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import keystone_agents.cli as cli
from keystone_agents.cli import main
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.orchestrator.preflight_context import (
    MANUAL_REQUEST_PLAN_ENV,
    ORCHESTRATOR_PREFLIGHT_ENV,
    ORCHESTRATOR_ROUTE_RESULT_ENV,
)
from keystone_agents.schemas.work_item import (
    WorkItem,
    WorkItemKind,
    WorkItemRoute,
    WorkItemStatus,
    WorkflowRunResult,
)
from keystone_agents.storage.sqlite_store import SQLiteStore


def _fake_orchestrator_preflight(
    request_text,
    *,
    requested_agent=None,
    live_manual_plan=False,
    **kwargs,
):
    del live_manual_plan, kwargs
    plan = infer_manual_request_plan(request_text, requested_agent=requested_agent)
    result = cli.route_request(request_text, manual_plan=plan)
    return cli.OrchestratorPreflight(
        request_text=str(request_text or ""),
        requested_agent=plan.requested_agent,
        advisory_only=plan.requested_agent not in {None, "orchestrator"},
        selected_agent=str(plan.requested_agent or result.route),
        blocked_by_orchestrator=bool(result.refused),
        execution_allowed=not bool(result.refused),
        block_kind="send" if result.refused else "",
        block_reason=result.stop_reason or "",
        manual_request_plan=plan,
        route_result=result,
    )


def test_cli_health_smoke(capsys) -> None:
    exit_code = main(["health", "--database-url", ":memory:"])

    assert exit_code == 0
    assert "Overall status:" in capsys.readouterr().out


def test_cli_init_db_uses_explicit_database_url(tmp_path: Path, capsys) -> None:
    database_path = tmp_path / "keystone.db"

    exit_code = main(["init-db", "--database-url", f"sqlite:///{database_path}"])

    assert exit_code == 0
    assert database_path.exists()
    assert "Initialized SQLite database:" in capsys.readouterr().out


def test_cli_automations_list_and_audit(tmp_path: Path, capsys) -> None:
    database_url = f"sqlite:///{tmp_path / 'automations.db'}"

    list_exit = main(["automations", "list", "--database-url", database_url])
    audit_exit = main(["automations", "audit", "--database-url", database_url, "--json"])

    assert list_exit == 0
    assert audit_exit == 0
    output = capsys.readouterr().out
    assert "Weekly Opportunity Scan" in output
    assert "automation_specs" in output


def test_cli_route_stays_dry_run(capsys) -> None:
    exit_code = main(["route", "--input", "research Curebase"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: orchestrator" in output
    assert "Send enabled: False" in output


def test_cli_ask_routes_unmentioned_input_through_work_item(tmp_path: Path, capsys) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask.db'}"
    exit_code = main(["ask", "--database-url", database_url, "research", "Curebase"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "WorkItem:" in output
    assert "Route: business_research_analyst" in output
    assert "Manual plan: business_research_analyst / company_research" in output
    assert "Artifacts: company_profile:" in output


def test_cli_work_items_advance_zotero_collection_outputs_research_brief(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    cache_dir = tmp_path / "zotero-cache"
    cache_dir.mkdir()
    collection_key = "LTA3U8I8"
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps({"collections": {"LH 01 - REACH-tDCS & Lindus Trial Context": collection_key}}),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "data": {
                            "key": "ITEM1",
                            "title": "Remote tDCS randomized trial",
                            "url": "https://pubmed.ncbi.nlm.nih.gov/example/",
                            "DOI": "10.1000/example",
                            "abstractNote": "A randomized trial tested home-based tDCS for MDD.",
                            "itemType": "journalArticle",
                            "collections": [collection_key],
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_CACHE", str(cache_dir))
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "work-items",
            "advance",
            "--input",
            (
                "Ask the Business Research Analyst to summarize the Zotero collection "
                "'LH 01 - REACH-tDCS & Lindus Trial Context' with one paragraph per source"
            ),
            "--database-url",
            database_url,
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Route: business_research_analyst" in output
    assert "Business Research Analyst attached a source-backed Zotero research brief" in output
    assert "Artifacts: research_brief:" in output
    assert "Remote tDCS randomized trial" in output


def test_cli_work_items_advance_uses_orchestrator_preflight_and_manager_loop(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "work-items",
            "advance",
            "--json",
            "--input",
            "research Curebase",
            "--database-url",
            database_url,
            "--max-manager-steps",
            "1",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["route"] == "business_research_analyst"
    assert payload["manual_request_plan"]["target_agent"] == "business_research_analyst"
    assert payload["orchestrator_preflight"]["selected_agent"] == "business_research_analyst"
    assert payload["orchestrator_preflight"]["preflight_memo"]["raw_request"] == (
        "research Curebase"
    )

    events = SQLiteStore(database_url).list_work_item_events(payload["work_item"]["id"])
    review_events = [event for event in events if event.event_type == "manager_loop_review"]
    assert review_events
    assert review_events[0].metadata["route"] == "business_research_analyst"
    assert review_events[0].metadata["review_decision"] in {"pass", "warn", "block"}


def test_cli_work_items_advance_preflight_uses_existing_specialist_route(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"
    store = SQLiteStore(database_url)
    work_item = WorkItem(
        kind=WorkItemKind.COMPANY_RESEARCH,
        status=WorkItemStatus.BLOCKED,
        title="Research: OpenEvidence",
        request_text="business research analyst research OpenEvidence",
        current_route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
        last_agent=WorkItemRoute.BUSINESS_RESEARCH_ANALYST.value,
    )
    store.save_work_item(work_item)
    captured: dict[str, object] = {}

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        captured["request_text"] = request_text
        captured["requested_agent"] = requested_agent
        captured["live_manual_plan"] = live_manual_plan
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_advance_work_item_manager_loop(request, **_kwargs):
        captured["manual_request_plan"] = request.manual_request_plan
        return WorkflowRunResult(
            work_item=work_item,
            route=WorkItemRoute.BUSINESS_RESEARCH_ANALYST,
            status=WorkItemStatus.BLOCKED,
            advanced=True,
            human_summary="Specialist continuation accepted.",
            manual_request_plan=request.manual_request_plan,
            orchestrator_preflight=request.orchestrator_preflight,
        )

    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(
        cli,
        "advance_work_item_manager_loop",
        fake_advance_work_item_manager_loop,
    )

    exit_code = main(
        [
            "work-items",
            "advance",
            work_item.id,
            "--json",
            "--live-sdk",
            "--input",
            "business research analyst research OpenEvidence\nFollow-up: look at partnerships",
            "--database-url",
            database_url,
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert captured["requested_agent"] == "business_research_analyst"
    assert captured["live_manual_plan"] is True
    assert captured["manual_request_plan"]["target_agent"] == "business_research_analyst"
    assert payload["orchestrator_preflight"]["selected_agent"] == "business_research_analyst"


def test_cli_work_items_advance_zotero_article_outputs_research_brief(
    tmp_path: Path,
    capsys,
    monkeypatch,
) -> None:
    cache_dir = tmp_path / "zotero-cache"
    cache_dir.mkdir()
    (cache_dir / "zotero_collections.json").write_text(
        json.dumps({"collections": {"LH 01 - REACH-tDCS": "LTA3U8I8"}}),
        encoding="utf-8",
    )
    (cache_dir / "zotero_items.json").write_text(
        json.dumps(
            {
                "items": [
                    {
                        "data": {
                            "key": "AP9SKRPZ",
                            "title": (
                                "Study Details | NCT06976697 | Home-Based tDCS "
                                "Treatment Of Major Depressive Disorder"
                            ),
                            "url": "https://clinicaltrials.gov/study/NCT06976697",
                            "abstractNote": "",
                            "itemType": "webpage",
                            "collections": ["LTA3U8I8"],
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KEYSTONE_ZOTERO_IMPORT_CACHE", str(cache_dir))
    database_url = f"sqlite:///{tmp_path / 'work-items.db'}"

    exit_code = main(
        [
            "work-items",
            "advance",
            "--input",
            (
                "Ask the Business Research Analysit to find and summarize the Zotero "
                "article on the Lindus SOOMA trial. Include one paragraph summary, "
                "methods/design, inclusion/exclusion, and other relevant trial info"
            ),
            "--database-url",
            database_url,
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Route: business_research_analyst" in output
    assert "Business Research Analyst attached a source-backed Zotero research brief" in output
    assert "Artifacts: research_brief:" in output
    assert "NCT06976697" in output


def test_cli_ask_kni_mention_uses_work_item(tmp_path: Path, capsys) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-kni.db'}"
    exit_code = main(
        [
            "ask",
            "--database-url",
            database_url,
            "@KNI",
            "business",
            "agent",
            "analyst",
            "research",
            "Lindus",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "WorkItem:" in output
    assert "Route: business_research_analyst" in output
    assert "Advanced: True" in output


def test_cli_ask_generic_kni_opportunity_to_outreach_runs_workflow(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-loop.db'}"
    exit_code = main(
        [
            "ask",
            "--json",
            "--database-url",
            database_url,
            "@KNI",
            "run",
            "one",
            "opportunity-to-outreach",
            "loop",
            "for",
            "behavioral",
            "health",
            "AI.",
            "Top",
            "1",
            "only.",
            "Post",
            "approval",
            "to",
            "this",
            "channel.",
            "Draft",
            "only,",
            "do",
            "not",
            "send.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["workflow"] == "opportunity_to_outreach_loop"
    assert payload["selected_agent"] == "orchestrator"
    assert payload["topic"] == "behavioral health AI"
    assert payload["top_n"] == 1
    assert payload["send_enabled"] is False
    assert len(payload["output"]["items"]) == 1
    posts = payload["output"]["storage"]["slack_approval_posts"]
    assert len(posts) == 1
    assert posts[0]["status"] == "dry-run"
    assert posts[0]["send_enabled"] is False


def test_cli_ask_agent_override_keeps_direct_dry_run(capsys) -> None:
    exit_code = main(["ask", "--agent", "business_research_analyst", "research", "Lindus"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: Business Research Analyst" in output
    assert "Mode: dry_run" in output


def test_cli_ask_agent_override_selects_specialist_json(capsys) -> None:
    exit_code = main(
        [
            "ask",
            "--agent",
            "opportunity_scout",
            "--json",
            "find psychiatry AI opportunities",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"selected_agent": "opportunity_scout"' in output
    assert '"send_enabled": false' in output


def test_cli_ask_chief_of_staff_outputs_deterministic_plan(capsys) -> None:
    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "see my email and send an update to the #onboarding channel",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload["output"]["recommended_route"]["workflow_type"] == "gmail-summary"
    assert payload["output"]["recommended_route"]["target_channel"] == "onboarding"
    assert payload["output"]["slack_post_allowed"] is False


def test_cli_ask_chief_of_staff_dry_run_uses_orchestrator_manual_plan(
    monkeypatch,
    capsys,
) -> None:
    captured: dict[str, object] = {}

    class FakeRoute:
        workflow_type = "chief_of_staff"
        command_text = "@KNI chief of staff review"
        target_channel = ""

    class FakeChiefResult:
        summary = "Chief of Staff used parent Orchestrator plan."
        recommended_route = FakeRoute()
        slack_post_allowed = False

        def model_dump(self, **_kwargs):
            return {
                "summary": self.summary,
                "recommended_route": {
                    "workflow_type": self.recommended_route.workflow_type,
                    "command_text": self.recommended_route.command_text,
                    "target_channel": self.recommended_route.target_channel,
                },
                "slack_post_allowed": self.slack_post_allowed,
            }

    def fake_plan_chief_of_staff_request(*_args, **kwargs):
        captured["manual_request_plan"] = kwargs.get("manual_request_plan")
        return FakeChiefResult()

    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "plan_chief_of_staff_request", fake_plan_chief_of_staff_request)

    exit_code = main(
        [
            "ask",
            "--json",
            "--agent",
            "chief_of_staff",
            "review",
            "the",
            "state",
            "of",
            "KNI",
            "2026",
            "and",
            "why",
            "the",
            "prior",
            "answer",
            "was",
            "unrelated",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    manual_plan = captured["manual_request_plan"]

    assert exit_code == 0
    assert manual_plan is not None
    assert manual_plan.target_agent == "chief_of_staff"
    assert payload["orchestrator_preflight"]["manual_request_plan"]["target_agent"] == (
        "chief_of_staff"
    )
    assert payload["output"]["summary"] == "Chief of Staff used parent Orchestrator plan."


def test_cli_ask_kni_chief_of_staff_uses_chief_work_item(tmp_path: Path, capsys) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-chief.db'}"

    exit_code = main(
        [
            "ask",
            "--database-url",
            database_url,
            "@KNI",
            "chief",
            "of",
            "staff",
            "summarize",
            "open",
            "Slack",
            "follow-ups",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "WorkItem:" in output
    assert "Route: chief_of_staff" in output
    assert "Artifacts: chief_of_staff_plan:" in output


def test_cli_ask_chief_of_staff_reference_capture_persists_memory(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'keystone.db'}"
    url = "https://braininitiative.nih.gov/news-events/blog/register-now-nih-brain-neuroai-workshop"

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "--database-url",
            database_url,
            "keep this for future reference:",
            "NIH AI conference with virtual attendees:",
            url,
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload["output"]["recommended_route"]["workflow_type"] == "reference-capture"
    assert payload["output"]["summary"].startswith("Saved reference for future use:")

    memories = SQLiteStore(database_url).retrieve_memory(
        "NIH AI conference",
        memory_types=["operator_reference"],
    )
    assert len(memories) == 1
    assert memories[0].content["url"] == url


def test_cli_ask_agent_override_auto_live_sdk_in_live_mode(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "ResearchBrief",
                    "send_enabled": False,
                    "output": {"summary": "live brief"},
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "--agent", "business_research_analyst", "research", "Lindus"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: Business Research Analyst" in output
    assert "Output type: ResearchBrief" in output
    assert "Orchestrator review:" in output
    assert calls
    assert "scripts/run_company_research.py" in calls[0]
    assert "--live-sdk" in calls[0]


def test_cli_ask_cost_tracking_directive_is_recorded_without_reaching_child(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []
    preflight_inputs: list[str] = []

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        preflight_inputs.append(str(request_text))
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "ResearchBrief",
                    "send_enabled": False,
                    "output": {"summary": "live brief"},
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "business_research_analyst",
            "--json",
            "research",
            "Lindus.",
            "Also",
            "keep",
            "track",
            "of",
            "this",
            "run",
            "costs.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["cost_tracking_requested"] is True
    assert preflight_inputs == ["research Lindus"]
    assert calls
    assert "Also keep track" not in " ".join(calls[0])


def test_cli_ask_live_payload_surfaces_missing_information(
    monkeypatch,
    capsys,
) -> None:
    def fake_run(command, **kwargs):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "ResearchBrief",
                    "send_enabled": False,
                    "output": {
                        "summary": "partial",
                        "unknowns": ["I did not have source-backed leadership evidence."],
                        "limitations": ["Only one source was available."],
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "--agent", "business_research_analyst", "research", "Lindus"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Missing information:" in output
    assert "source-backed leadership evidence" in output
    assert "Only one source was available" in output


def test_cli_ask_kni_explicit_agent_auto_live_sdk_in_live_mode(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run_orchestrator_preflight(
        request_text,
        *,
        requested_agent=None,
        live_manual_plan=False,
        **kwargs,
    ):
        assert requested_agent == "opportunity_scout"
        assert live_manual_plan is True
        return _fake_orchestrator_preflight(
            request_text,
            requested_agent=requested_agent,
            live_manual_plan=live_manual_plan,
            **kwargs,
        )

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "OpportunityScoutResult",
                    "send_enabled": False,
                    "output": {"records": []},
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_ENABLE_LIVE_RESEARCH", "true")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_run_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(["ask", "@KNI", "opportunity", "scout", "find", "AI", "partners"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: Opportunity Scout Agent" in output
    assert "Output type: OpportunityScoutResult" in output
    assert "Orchestrator review:" in output
    assert "WorkItem:" not in output
    assert calls
    assert "scripts/run_opportunity_scout.py" in calls[0]
    assert "--live-search" in calls[0]
    assert "--live-sdk" in calls[0]


def test_cli_ask_explicit_chief_of_staff_runs_orchestrator_preflight_advise_only(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []
    child_envs: list[dict[str, str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        child_envs.append(dict(kwargs.get("env") or {}))
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "output_type": "ChiefOfStaffResult",
                    "send_enabled": False,
                    "output": {
                        "summary": "Architecture review for agent routing.",
                        "recommended_actions": ["Keep raw request visible to specialists."],
                        "audit_notes": ["No external write attempted."],
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--json",
            "@KNI",
            "chief",
            "of",
            "staff",
            "review",
            "the",
            "agent",
            "architecture",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "chief_of_staff"
    assert payload["orchestrator_preflight"]["advisory_only"] is True
    assert payload["orchestrator_preflight"]["manual_request_plan"]["target_agent"] == (
        "chief_of_staff"
    )
    assert payload["orchestrator_preflight"]["route_result"]["route"] == "chief_of_staff"
    assert calls
    assert "scripts/run_chief_of_staff.py" in calls[0]
    assert child_envs
    assert ORCHESTRATOR_PREFLIGHT_ENV in child_envs[0]
    assert MANUAL_REQUEST_PLAN_ENV in child_envs[0]
    assert ORCHESTRATOR_ROUTE_RESULT_ENV in child_envs[0]


def test_cli_ask_explicit_agent_send_request_blocked_by_orchestrator_preflight(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "send outreach email to this lead",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "blocked"
    assert payload["status"] == "blocked"
    assert payload["send_enabled"] is False
    assert payload["orchestrator_preflight"]["blocked_by_orchestrator"] is True
    assert payload["orchestrator_preflight"]["execution_allowed"] is False
    assert payload["block_kind"] == "send"
    assert payload["orchestrator_preflight"]["route_result"]["refused"] is True
    assert calls == []


def test_cli_ask_preflight_blocked_omits_raw_workflow_state(
    monkeypatch,
    capsys,
) -> None:
    def fake_preflight_with_slack_state(*args, **kwargs):
        preflight = _fake_orchestrator_preflight(*args, **kwargs)
        preflight.route_result = preflight.route_result.model_copy(
            update={
                "workflow_state_summary": {
                    "recent_slack_thread": [{"summary": "private Slack refusal context"}],
                    "prior_agent_runs": [{"summary": "prior operator correction"}],
                }
            }
        )
        return preflight

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", fake_preflight_with_slack_state)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "send outreach email to this lead",
        ]
    )

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exit_code == 0
    assert payload["status"] == "blocked"
    assert "workflow_state_summary" not in payload_text
    assert "private Slack refusal context" not in payload_text
    assert "prior operator correction" not in payload_text


def test_cli_ask_gmail_reply_without_thread_context_is_blocked(
    monkeypatch,
    capsys,
) -> None:
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", fake_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "gmail_triage",
            "--live-sdk",
            "--json",
            "Reply politely and confirm next week works.",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_agent"] == "gmail_triage"
    assert payload["status"] == "blocked"
    assert payload["block_kind"] == "missing_gmail_context"
    assert payload["requires_gmail_context"] is True
    assert payload["send_enabled"] is False
    assert payload["agent_execution_plan"]["operation"] == "draft_reply"
    assert "No synthetic email was created" in payload["message"]
    assert calls == []


def test_cli_ask_live_child_timeout_returns_structured_payload(
    monkeypatch,
    capsys,
) -> None:
    def timeout_run(command, **kwargs):
        raise cli.subprocess.TimeoutExpired(command, kwargs.get("timeout", 1))

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setenv("KEYSTONE_CHILD_AGENT_TIMEOUT_SECONDS", "2")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", timeout_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "review the agent architecture",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["status"] == "timeout"
    assert payload["timeout_seconds"] == 2.0
    assert payload["send_enabled"] is False
    assert payload["output"]["error_type"] == "timeout"


def test_cli_ask_live_child_failure_returns_redacted_structured_payload(
    monkeypatch,
    capsys,
) -> None:
    def failed_run(_command, **_kwargs):
        fake_stdout_token = "sk-" + ("y" * 20)
        fake_stderr_token = "sk-" + ("x" * 24)
        return SimpleNamespace(
            returncode=7,
            stdout=f"partial stdout token={fake_stdout_token}",
            stderr=f"failed token={fake_stderr_token}",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", failed_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "review the agent architecture",
        ]
    )

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exit_code == 7
    assert payload["status"] == "failed"
    assert payload["child_returncode"] == 7
    assert payload["send_enabled"] is False
    assert payload["output"]["error_type"] == "child_process_failed"
    assert payload["output"]["returncode"] == 7
    assert "sk-" + ("x" * 24) not in payload_text
    assert "sk-" + ("y" * 20) not in payload_text
    assert "[REDACTED]" in payload_text


def test_cli_ask_live_child_malformed_json_returns_redacted_structured_payload(
    monkeypatch,
    capsys,
) -> None:
    def malformed_run(_command, **_kwargs):
        fake_stdout_token = "sk-" + ("x" * 24)
        fake_stderr_token = "sk-" + ("y" * 20)
        return SimpleNamespace(
            returncode=0,
            stdout=f"not json token={fake_stdout_token}",
            stderr=f"warning token={fake_stderr_token}",
        )

    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")
    monkeypatch.setattr(cli, "run_orchestrator_preflight", _fake_orchestrator_preflight)
    monkeypatch.setattr(cli, "run_isolated_child_process", malformed_run)

    exit_code = main(
        [
            "ask",
            "--agent",
            "chief_of_staff",
            "--json",
            "review the agent architecture",
        ]
    )

    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert exit_code == 1
    assert payload["status"] == "failed"
    assert payload["child_returncode"] == 0
    assert payload["send_enabled"] is False
    assert payload["output"]["error_type"] == "child_process_malformed_json"
    assert "parse_error" in payload["output"]
    assert "sk-" + ("x" * 24) not in payload_text
    assert "sk-" + ("y" * 20) not in payload_text
    assert "[REDACTED]" in payload_text


def test_cli_ask_no_live_sdk_opt_out_keeps_work_item_in_live_mode(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'ask-live-opt-out.db'}"
    monkeypatch.setenv("KEYSTONE_LIVE_MODE", "true")
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "false")

    exit_code = main(
        [
            "ask",
            "--no-live-sdk",
            "--database-url",
            database_url,
            "@KNI",
            "business",
            "research",
            "analyst",
            "research",
            "Lindus",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "WorkItem:" in output
    assert "Route: business_research_analyst" in output


def test_cli_agents_list_prints_registry_cards(capsys) -> None:
    exit_code = main(["agents", "list"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "gmail_triage" in output
    assert "OpportunityScoutResult" in output
    assert "Live flags" in output


def test_cli_agents_list_json(capsys) -> None:
    exit_code = main(["agents", "list", "--json"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"route_name": "orchestrator"' in output
    assert '"tool_policy"' in output
    assert '"allowed_tool_names"' in output
