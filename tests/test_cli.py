from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import keystone_agents.cli as cli
from keystone_agents.cli import main
from keystone_agents.manual_request import infer_manual_request_plan
from keystone_agents.storage.sqlite_store import SQLiteStore


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


def test_cli_ask_chief_of_staff_reference_capture_persists_memory(
    tmp_path: Path,
    capsys,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'keystone.db'}"
    url = (
        "https://braininitiative.nih.gov/news-events/blog/"
        "register-now-nih-brain-neuroai-workshop"
    )

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

    def fake_resolve_manual_request_plan(request_text, *, requested_agent=None, live=False):
        assert live is True
        return infer_manual_request_plan(request_text, requested_agent=requested_agent)

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
    monkeypatch.setattr(cli, "resolve_manual_request_plan", fake_resolve_manual_request_plan)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    exit_code = main(["ask", "--agent", "business_research_analyst", "research", "Lindus"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: Business Research Analyst" in output
    assert "Output type: ResearchBrief" in output
    assert calls
    assert "scripts/run_company_research.py" in calls[0]
    assert "--live-sdk" in calls[0]


def test_cli_ask_live_payload_surfaces_missing_information(
    monkeypatch,
    capsys,
) -> None:
    def fake_resolve_manual_request_plan(request_text, *, requested_agent=None, live=False):
        return infer_manual_request_plan(request_text, requested_agent=requested_agent)

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
    monkeypatch.setattr(cli, "resolve_manual_request_plan", fake_resolve_manual_request_plan)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

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

    def fake_resolve_manual_request_plan(request_text, *, requested_agent=None, live=False):
        assert requested_agent == "opportunity_scout"
        assert live is True
        return infer_manual_request_plan(request_text, requested_agent=requested_agent)

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
    monkeypatch.setattr(cli, "resolve_manual_request_plan", fake_resolve_manual_request_plan)
    monkeypatch.setattr(cli.subprocess, "run", fake_run)

    exit_code = main(["ask", "@KNI", "opportunity", "scout", "find", "AI", "partners"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "Agent: Opportunity Scout Agent" in output
    assert "Output type: OpportunityScoutResult" in output
    assert "WorkItem:" not in output
    assert calls
    assert "scripts/run_opportunity_scout.py" in calls[0]
    assert "--live-search" in calls[0]
    assert "--live-sdk" in calls[0]


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


def test_cli_agents_list_json(capsys) -> None:
    exit_code = main(["agents", "list", "--json"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert '"route_name": "orchestrator"' in output
