"""Selected articles reach one tool-free synthesis through real extraction contracts."""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from keystone_agents.agents import orchestrator
from keystone_agents.entrypoints import cli_impl as cli
from keystone_agents.runtime import article_answer_evidence
from keystone_agents.schemas.orchestrator import OrchestratorGeneralAnswer, OrchestratorResult
from keystone_agents.storage.sqlite_store import SQLiteStore
from keystone_agents.tools import website_extraction_tool as extraction

ARTICLE_URL = "https://public.example/articles/research-note"
ARTICLE_TEXT = (
    "The article describes a small study of appointment reminders. "
    "Attendance improved in the observed sample, but allocation was not randomized. "
    "The authors recommend a larger randomized evaluation before drawing causal conclusions."
)


def _select_article(monkeypatch: pytest.MonkeyPatch, request: str) -> None:
    monkeypatch.setattr(
        orchestrator, "_route_ambiguous_with_llm",
        lambda *_args, **_kwargs: OrchestratorResult(
            route="orchestrator", workflow=[], routing_mode="llm",
            selected_source_url=ARTICLE_URL,
            rationale="Summarize the one supplied public article without broader research.",
        ),
    )
    preflight = orchestrator.run_orchestrator_preflight(request, live_orchestrator=True)
    assert preflight.execution_allowed
    assert preflight.manual_request_plan.expected_artifact_type == "source_summary"
    assert preflight.manual_request_plan.provider_operations == ["read"]
    assert not preflight.manual_request_plan.requires_live_search
    monkeypatch.setattr(cli, "run_orchestrator_preflight", lambda *_args, **_kwargs: preflight)
    monkeypatch.setattr(
        cli, "_run_ask_work_item",
        lambda *_args, **_kwargs: pytest.fail("Article answers must not create a WorkItem."),
    )
    monkeypatch.setattr(cli, "cli_default_live_research", lambda: False)


def _install_extractor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, text: str | None,
) -> list[str]:
    """Keep bundle building, source normalization, and snapshot projection real."""
    calls: list[str] = []

    def synthetic_extract(url: str, **kwargs: Any) -> extraction.WebsiteExtractionResult:
        calls.append(url)
        assert kwargs["guardrail_context"] == "public_web_source"
        assert kwargs["live"] is True
        if text is None:
            raise extraction.WebsiteExtractionError("The selected page is unavailable.")
        return extraction.WebsiteExtractionResult(
            url=url, title="Appointment reminder research note",
            provider=str(kwargs["provider"]), status="success", text_or_markdown=text,
            claims=["The article describes a small study of appointment reminders."],
        )

    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")
    monkeypatch.setenv("KEYSTONE_WEBSITE_EXTRACTOR", "trafilatura")
    monkeypatch.setenv("KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK", "")
    monkeypatch.setattr(extraction, "_web_snapshot_dir", lambda: tmp_path / "snapshots")
    monkeypatch.setattr(
        article_answer_evidence, "build_selected_url_source_bundle",
        partial(extraction.build_selected_url_source_bundle, extractor=synthetic_extract),
    )
    return calls


def _scripted_sdk(
    monkeypatch: pytest.MonkeyPatch, answer: str,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_sdk(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(
            output=OrchestratorGeneralAnswer(answer=answer, needs_source=False),
            usage={"requests": 1}, cost={}, request_cache={},
            raw_result=SimpleNamespace(new_items=[]),
        )

    monkeypatch.setattr(cli, "run_typed_sdk_agent", fake_sdk)
    return calls


def _run_article(tmp_path: Path, request: str) -> tuple[int, str]:
    database_url = f"sqlite:///{tmp_path / 'article-answer.db'}"
    code = cli.main([
        "ask", "--live-sdk", "--json", "--max-openai-requests", "2",
        "--database-url", database_url, "--input", request,
    ])
    return code, database_url


def test_short_article_extraction_reaches_one_tool_free_synthesis_and_saved_result(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    request = f"Summarize this article briefly: {ARTICLE_URL}"
    _select_article(monkeypatch, request)
    extraction_calls = _install_extractor(monkeypatch, tmp_path, text=ARTICLE_TEXT)
    synthesis_calls = _scripted_sdk(
        monkeypatch,
        "The small reminder study found improved attendance but cannot establish causality. "
        f"Source: {ARTICLE_URL}",
    )
    code, database_url = _run_article(tmp_path, request)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["status"] == "completed"
    assert payload["public_result"]["completion_confirmed"] is True
    assert payload["public_result"]["text"] == payload["slack_display_text"]
    assert ARTICLE_URL in payload["slack_display_text"]
    assert extraction_calls == [ARTICLE_URL]
    assert len(synthesis_calls) == 1
    call = synthesis_calls[0]
    assert call["agent"].tools == []
    assert call["max_turns"] == 1
    assert call["tool_call_budget_contract"] is None
    assert call["typed_input"]["request"] == request
    assert call["typed_input"]["answer_scope"] == "one_selected_public_article"
    evidence = call["typed_input"]["article_evidence"]
    assert evidence["extracted_text"] == ARTICLE_TEXT
    assert evidence["source_url"] == ARTICLE_URL
    assert evidence["evidence_scope"] == "complete_extracted_text"
    assert evidence["access"]["content_complete"] is True
    assert evidence["broad_search_performed"] is False
    assert evidence["external_write_performed"] is False
    assert payload["article_answer_evidence"] == evidence
    assert payload["send_enabled"] is False
    assert payload["side_effects"] == {
        "external_write_performed": False, "slack_message_posted": False,
    }
    assert payload["tool_execution"]["selected_tool_names"] == []
    assert payload["tool_execution"]["model_called_tool_names"] == []
    assert payload["tool_execution"]["workflow_called_helper_names"] == [
        "build_selected_url_source_bundle",
    ]
    saved = SQLiteStore(database_url).get_agent_run(payload["agent_run_id"])
    assert saved is not None
    assert saved["output"]["article_answer_evidence"] == evidence
    assert saved["output"]["slack_display_text"] == payload["slack_display_text"]


def test_article_unavailable_blocks_before_synthesis(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    request = f"Briefly explain the article at {ARTICLE_URL}"
    _select_article(monkeypatch, request)
    extraction_calls = _install_extractor(monkeypatch, tmp_path, text=None)
    monkeypatch.setattr(
        cli, "run_typed_sdk_agent",
        lambda **_kwargs: pytest.fail("An unavailable article must not reach synthesis."),
    )
    code, database_url = _run_article(tmp_path, request)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["status"] == "blocked"
    assert payload["block_kind"] == "selected_article_unavailable"
    assert payload["public_result"]["completion_confirmed"] is False
    assert extraction_calls == [ARTICLE_URL]
    assert "could not be read" in payload["slack_display_text"].lower()
    saved = SQLiteStore(database_url).get_agent_run(payload["agent_run_id"])
    assert saved is not None
    assert saved["status"] == "blocked"
    assert saved["model"] == "not_requested"
    assert saved["output"]["public_result"]["completion_confirmed"] is False


def test_article_extraction_live_gate_blocks_before_provider_or_synthesis(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    request = f"Summarize this article: {ARTICLE_URL}"
    _select_article(monkeypatch, request)
    extraction_calls = _install_extractor(monkeypatch, tmp_path, text=ARTICLE_TEXT)
    monkeypatch.delenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION")
    monkeypatch.setattr(
        cli, "run_typed_sdk_agent",
        lambda **_kwargs: pytest.fail("Disabled extraction must not reach synthesis."),
    )
    code, _database_url = _run_article(tmp_path, request)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["status"] == "blocked"
    assert payload["block_kind"] == "selected_article_unavailable"
    assert payload["public_result"]["completion_confirmed"] is False
    assert extraction_calls == []


@pytest.mark.parametrize("citation", ["", "https://public.example/articles/other"])
def test_article_answer_without_selected_source_citation_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    tmp_path: Path, citation: str,
) -> None:
    request = f"Summarize the article at {ARTICLE_URL}"
    _select_article(monkeypatch, request)
    _install_extractor(monkeypatch, tmp_path, text=ARTICLE_TEXT)
    calls = _scripted_sdk(
        monkeypatch, f"The study establishes a strong reminder effect. {citation}",
    )
    code, _database_url = _run_article(tmp_path, request)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["status"] == "blocked"
    assert payload["public_result"]["completion_confirmed"] is False
    assert "strong reminder effect" not in payload["slack_display_text"]
    assert len(calls) == 1


def test_partial_article_evidence_is_bounded_and_visible_to_reader(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path,
) -> None:
    request = f"Briefly summarize the article at {ARTICLE_URL}"
    _select_article(monkeypatch, request)
    _install_extractor(monkeypatch, tmp_path, text=ARTICLE_TEXT * 50)
    calls = _scripted_sdk(
        monkeypatch,
        f"The available text describes an observational reminder study. Source: {ARTICLE_URL}",
    )
    code, _database_url = _run_article(tmp_path, request)
    payload = json.loads(capsys.readouterr().out)
    assert code == 0, payload
    assert payload["status"] == "completed"
    assert len(calls) == 1
    evidence = calls[0]["typed_input"]["article_evidence"]
    assert 0 < len(evidence["extracted_text"]) < len(ARTICLE_TEXT * 50)
    assert evidence["evidence_scope"] == "extracted_text_excerpt"
    assert evidence["access"]["content_complete"] is False
    assert any("bounded excerpt" in item for item in evidence["limitations"])
    assert "Evidence limit:" in payload["slack_display_text"]
    assert "bounded excerpt" in payload["public_result"]["text"]
