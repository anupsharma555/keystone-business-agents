"""Offline source-to-tool-to-SDK proofs; scripted models do not prove interpretation."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from keystone_agents import live_retrieval
from keystone_agents.agents.business_research_analyst import (
    build_business_research_analyst_research_brief_agent,
)
from keystone_agents.source_enrichment import build_source_bundle, normalize_source_record
from keystone_agents.tools import website_extraction_tool as web

URL = "https://example.org/selected-study"
RESOLVED = "https://example.org/revised-study"
QUALIFIERS = (
    "Eligibility requires independent review; the pilot is NOT open to all applicants.",
    "The primary endpoint was NOT met; exploratory improvement is not confirmatory.",
    "The current deadline is 30 November; the earlier October deadline is withdrawn.",
    "The quoted positive result conflicts with the final independent assessment.",
)


@pytest.fixture(autouse=True)
def isolated_web_state(monkeypatch, tmp_path):
    monkeypatch.setenv("KEYSTONE_RUNTIME_STATE_DIR", str(tmp_path / "web-state"))
    monkeypatch.setenv("KEYSTONE_ENABLE_WEBSITE_EXTRACTION", "true")


def extraction(text: str, *, url: str = RESOLVED, claims=None):
    return web.WebsiteExtractionResult(
        url=url,
        provider="trafilatura",
        status="success",
        title="Synthetic source",
        text_or_markdown=text,
        claims=["Synthetic source describes a preliminary research evaluation."]
        if claims is None
        else claims,
        metadata={"final_url": url},
    )


def read_rest(excerpt, access):
    text = excerpt
    positions = []
    while access.next_start_char is not None:
        result = web.read_web_source_window_impl(
            source_id=access.source_id,
            selected_url=access.selected_url,
            expected_snapshot_sha256=access.snapshot_sha256,
            start_char=access.next_start_char,
            max_chars=701,
        )
        assert result["status"] == "success"
        next_access = web.WebSourceAccess.model_validate(result["web_source_access"])
        assert next_access.start_char == access.end_char
        assert next_access.end_char > next_access.start_char
        assert next_access.snapshot_sha256 == access.snapshot_sha256
        assert next_access.selected_url == access.selected_url
        assert next_access.resolved_url == access.resolved_url
        assert len(json.dumps(result)) < 14000
        text += result["text"]
        positions.append((next_access.start_char, next_access.end_char))
        access = next_access
    assert access.end_char == len(text)
    return text, positions


@pytest.mark.parametrize("qualification", QUALIFIERS)
@pytest.mark.parametrize("producer", ["selected", "company"])
def test_late_qualifications_are_reachable_from_both_producers(
    monkeypatch, qualification, producer
):
    text = "Background research context. " * 500 + qualification + " Final evidence note."
    result = extraction(text)
    if producer == "selected":
        bundle = web.build_selected_url_source_bundle(
            company_name="Synthetic source",
            selected_urls=[URL],
            live_extraction=True,
            max_text_chars_per_source=8000,
            extractor=lambda *a, **k: result,
        )
        source = bundle.source_bundle.sources[0]
        excerpt, access = source.evidence_excerpt, source.web_source_access
    else:
        monkeypatch.setattr(live_retrieval, "extract_website_content", lambda *a, **k: result)
        monkeypatch.setattr(live_retrieval, "agent_html_review_enabled", lambda: False)
        rows, errors, _stats = live_retrieval._extract_company_website_inputs(
            company="Synthetic source",
            company_url=URL,
            search_results=[],
            provider="trafilatura",
            max_pages=1,
            discover_internal_pages=False,
        )
        assert not errors
        source = normalize_source_record(rows[0], company_name="Synthetic source")
        excerpt, access = source.evidence_excerpt, source.web_source_access
    assert qualification not in excerpt
    assert access and not access.content_complete and access.available
    restored, positions = read_rest(excerpt, access)
    assert restored == text and qualification in restored
    assert len(positions) > 1


@pytest.mark.parametrize("length", [0, 199, 200, 3999, 4000, 4001, 8000, 8001])
def test_exact_caps_empty_and_complete_sources(length):
    text = "a" * length
    excerpt, access = web.project_web_source(
        extraction(text), source_id="source:1", selected_url=URL
    )
    assert excerpt == text[:4000]
    assert access.content_complete == (0 < length <= 4000)
    assert access.total_chars == length
    if length > 4000:
        assert read_rest(excerpt, access)[0] == text


def test_boundary_spanning_citation_is_exact_and_can_be_read_with_overlap():
    citation = "NOT eligible without [independent review](https://example.org/evidence?q=1#limits)."
    text = "x" * 3990 + citation + "y" * 6000
    excerpt, access = web.project_web_source(
        extraction(text), source_id="source:1", selected_url=URL
    )
    assert read_rest(excerpt, access)[0] == text
    result = web.read_web_source_window_impl(
        source_id=access.source_id,
        selected_url=URL,
        expected_snapshot_sha256=access.snapshot_sha256,
        start_char=3980,
        max_chars=200,
    )
    assert citation in result["text"]


def test_snapshot_change_identity_missing_and_tampered_content(monkeypatch, tmp_path):
    _, old = web.project_web_source(
        extraction("old source " * 1000), source_id="source:1", selected_url=URL
    )
    _, new = web.project_web_source(
        extraction("new source " * 1000), source_id="source:1", selected_url=URL
    )
    assert old.snapshot_sha256 != new.snapshot_sha256
    kwargs = dict(
        source_id=old.source_id, selected_url=URL, expected_snapshot_sha256=old.snapshot_sha256
    )
    assert web.read_web_source_window_impl(**kwargs)["text"].startswith("old source")
    with pytest.raises(web.WebsiteExtractionError, match="identity"):
        web.read_web_source_window_impl(**{**kwargs, "source_id": "wrong-source"})
    with pytest.raises(web.WebsiteExtractionError, match="identity"):
        web.read_web_source_window_impl(**{**kwargs, "selected_url": RESOLVED})
    with pytest.raises(web.WebsiteExtractionError, match="beyond"):
        web.read_web_source_window_impl(**kwargs, start_char=50000)
    with pytest.raises(web.WebsiteExtractionError, match="SHA-256"):
        web.read_web_source_window_impl(**{**kwargs, "expected_snapshot_sha256": "../secret"})
    path = web._web_snapshot_dir() / f"{old.snapshot_sha256}.json"
    path.write_text(path.read_text().replace("old source", "bad source"))
    with pytest.raises(web.WebsiteExtractionError, match="integrity"):
        web.read_web_source_window_impl(**kwargs)
    monkeypatch.setenv("KEYSTONE_RUNTIME_STATE_DIR", str(tmp_path / "other-state"))
    assert web.read_web_source_window_impl(**kwargs)["status"] == "unavailable"


def test_model_visible_response_budget_includes_urls_claims_and_unicode():
    urls = [f"https://example.org/source-{i}?value=" + "a" * 1950 for i in range(8)]
    text = "研究 " * 20000
    result = web.build_selected_url_source_bundle(
        company_name="Synthetic source",
        selected_urls=urls,
        live_extraction=True,
        extractor=lambda url, **kw: extraction(text, url=url),
        max_text_chars_per_source=8000,
    )
    assert len(json.dumps(result.model_dump(mode="json"))) <= web.MAX_WEB_BUNDLE_RESPONSE_CHARS
    included = {d.selected_url for d in result.diagnostics}
    assert included | set(result.deferred_selected_urls) == set(urls)
    assert included.isdisjoint(result.deferred_selected_urls)
    assert result.source_bundle.sources
    for source in result.source_bundle.sources:
        access = source.web_source_access
        assert access and access.next_start_char
        read = web.read_web_source_window_impl(
            source_id=access.source_id,
            selected_url=access.selected_url,
            expected_snapshot_sha256=access.snapshot_sha256,
            start_char=access.next_start_char,
            max_chars=8000,
        )
        assert len(json.dumps(read)) < 14000


def test_empty_claim_list_does_not_discard_readable_evidence_and_merge_preserves_access():
    result = web.build_selected_url_source_bundle(
        company_name="Synthetic source",
        selected_urls=[URL],
        live_extraction=True,
        extractor=lambda *a, **k: extraction("Arbitrary evidence retained." * 500, claims=[]),
    )
    extracted = result.source_bundle.sources[0]
    search = normalize_source_record(
        {
            "url": URL,
            "source_id": "search:1",
            "title": "Search hit",
            "supported_claims": ["Synthetic research context."],
        },
        company_name="Synthetic source",
    )
    merged = build_source_bundle(company_name="Synthetic source", sources=[search, extracted])
    assert len(merged.sources) == 1
    assert merged.sources[0].web_source_access == extracted.web_source_access
    assert merged.sources[0].evidence_excerpt == extracted.evidence_excerpt


def test_same_url_different_snapshots_are_not_merged_under_one_version():
    sources = []
    for qualification in QUALIFIERS[:2]:
        bundle = web.build_selected_url_source_bundle(
            company_name="Synthetic source",
            selected_urls=[URL],
            live_extraction=True,
            extractor=lambda *a, selected=qualification, **k: extraction(
                selected, claims=[selected]
            ),
        )
        sources.extend(bundle.source_bundle.sources)
    merged = build_source_bundle(company_name="Synthetic source", sources=sources)
    assert len(merged.sources) == 2
    assert len({source.web_source_access.snapshot_sha256 for source in merged.sources}) == 2
    assert all(len(source.supported_claims) == 1 for source in merged.sources)


def test_installed_parser_preserves_links_tables_notes_and_revision_semantics():
    html = """<article><h1>Synthetic source</h1>
    <p>Source research evidence is preliminary; independent evaluation remains required.</p>
    <table><tr><th>Site</th><th>Count</th><th>Status</th></tr>
    <tr><td>Alpha</td><td>0</td><td>NOT approved</td></tr>
    <tr><td>Beta</td><td>37</td><td>Only after review</td></tr></table>
    <p>Release: <del>Approved.</del> <ins>NOT approved; review pending.</ins></p>
    <blockquote><p>Quoted outcome does NOT establish causal benefit.</p></blockquote>
    <p>Exploratory result<sup><a href="#note1">1</a></sup>.</p>
    <section role="doc-endnotes"><p id="note1">Primary endpoint was NOT met.</p></section>
    <p>Read <a href="https://example.org/review-evidence">the supporting review</a> for details.</p>
    <p>These research observations require further validation before use.</p></article>"""
    result = web.extract_website_content_from_html(html, url=URL, company_name="Synthetic source")
    assert "[the supporting review](https://example.org/review-evidence)" in result.text_or_markdown
    assert "~~Approved.~~" in result.text_or_markdown
    assert "NOT approved; review pending." in result.text_or_markdown
    assert "Primary endpoint was NOT met." in result.text_or_markdown
    assert "Quoted outcome does NOT establish causal benefit." in result.text_or_markdown
    rows = result.text_or_markdown.splitlines()
    assert any("Alpha" in row and "0" in row and "NOT approved" in row for row in rows)
    assert any("Beta" in row and "37" in row and "Only after review" in row for row in rows)


@pytest.mark.parametrize(
    "request_text",
    [
        "Which eligibility restrictions does this selected source describe?",
        "Compare the main outcome with the later qualification in this selected source.",
        "Check the current date and any withdrawn date in the selected source.",
    ],
)
def test_actual_research_sdk_receives_production_continuation_results(monkeypatch, request_text):
    from agents import Runner
    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )

    from keystone_agents.sdk import build_local_run_config

    text = "Preliminary research background. " * 400 + "\n".join(QUALIFIERS)
    original = web.build_selected_url_source_bundle
    monkeypatch.setattr(
        web,
        "build_selected_url_source_bundle",
        lambda **kw: original(
            **kw,
            extractor=lambda *a, **k: extraction(text),
        ),
    )

    class ScriptedModel(Model):
        def __init__(self):
            self.inputs = []
            self.completed = False

        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            **kwargs,
        ):
            self.inputs.append(input)
            assert "read_web_source_window" in [tool.name for tool in tools]
            outputs = (
                [
                    row
                    for row in input
                    if isinstance(row, dict) and row.get("type") == "function_call_output"
                ]
                if isinstance(input, list)
                else []
            )
            if not outputs:
                name = "extract_selected_urls_to_source_bundle"
                arguments = {
                    "company_name": "Synthetic source",
                    "selected_urls": [URL],
                    "live_extraction": True,
                }
            else:
                raw = outputs[-1]["output"]
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    data = ast.literal_eval(raw)
                if "source_bundle" in data:
                    access = data["source_bundle"]["sources"][0]["web_source_access"]
                else:
                    access = data["web_source_access"]
                if access["next_start_char"] is None:
                    self.completed = True
                    return ModelResponse(
                        output=[
                            ResponseOutputMessage(
                                id="synthetic-final",
                                type="message",
                                role="assistant",
                                status="completed",
                                content=[
                                    ResponseOutputText(
                                        type="output_text",
                                        annotations=[],
                                        text=json.dumps(
                                            {
                                                "target_name": "Synthetic source",
                                                "summary": "Saved source read completed.",
                                            }
                                        ),
                                    )
                                ],
                            )
                        ],
                        usage=Usage(requests=1),
                        response_id="synthetic-final",
                    )
                name = "read_web_source_window"
                arguments = {
                    "source_id": access["source_id"],
                    "selected_url": access["selected_url"],
                    "expected_snapshot_sha256": access["snapshot_sha256"],
                    "start_char": access["next_start_char"],
                    "max_chars": 4000,
                }
            return ModelResponse(
                output=[
                    ResponseFunctionToolCall(
                        type="function_call",
                        name=name,
                        call_id=f"synthetic-{len(self.inputs)}",
                        arguments=json.dumps(arguments),
                        status="completed",
                    )
                ],
                usage=Usage(requests=1),
                response_id=f"synthetic-{len(self.inputs)}",
            )

        def stream_response(self, *a, **k):
            raise NotImplementedError

    model = ScriptedModel()

    class Provider(ModelProvider):
        def get_model(self, model_name):
            return model

    agent = build_business_research_analyst_research_brief_agent(
        request_text=request_text,
        tool_tier="deep_retrieval",
        manual_request_plan={
            "source": "test",
            "target_agent": "business_research_analyst",
            "requested_agent": "business_research_analyst",
            "intent": "company_research",
            "primary_target": URL,
            "expected_artifact_type": "source_summary",
            "ask_shape": {"permission_state": "read_only", "strict_filter_mode": "exact"},
            "requires_live_search": False,
        },
    )
    result = Runner.run_sync(
        agent, request_text, run_config=build_local_run_config(Provider()), max_turns=12
    )
    assert result.final_output.target_name == "Synthetic source" and model.completed
    actual_final_input = json.dumps(model.inputs[-1])
    assert all(qualification in actual_final_input for qualification in QUALIFIERS)
    assert URL in actual_final_input and RESOLVED in actual_final_input
    assert "snapshot_sha256" in actual_final_input
    assert len(model.inputs) >= 4
    capture = os.environ.get("KBA_WEB_PROOF_DIR")
    if capture:
        label = str(len(request_text))
        (Path(capture) / f"selected-sdk-inputs-{label}.json").write_text(
            json.dumps(
                {
                    "request": request_text,
                    "actual_sdk_inputs": model.inputs,
                    "final_output": result.final_output.model_dump(mode="json"),
                    "proof": "Scripted SDK model with production tools; no interpretation proof.",
                },
                indent=2,
            )
        )


def test_company_projection_reaches_actual_focused_sdk_with_only_local_reads(monkeypatch):
    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.usage import Usage
    from openai.types.responses import (
        ResponseFunctionToolCall,
        ResponseOutputMessage,
        ResponseOutputText,
    )

    from keystone_agents.agents.business_research_analyst import (
        focused_brief_input_from_profile,
        run_business_research_analyst_focused_brief_sdk,
    )
    from keystone_agents.schemas.company_profile import CompanyProfile
    from keystone_agents.sdk import build_local_run_config

    text = "Research report background. " * 450 + "\n".join(QUALIFIERS)
    monkeypatch.setattr(live_retrieval, "extract_website_content", lambda *a, **k: extraction(text))
    monkeypatch.setattr(live_retrieval, "agent_html_review_enabled", lambda: False)
    rows, errors, _stats = live_retrieval._extract_company_website_inputs(
        company="Synthetic source",
        company_url=URL,
        search_results=[],
        provider="trafilatura",
        max_pages=1,
        discover_internal_pages=False,
    )
    assert not errors
    source = normalize_source_record(rows[0], company_name="Synthetic source")
    profile = CompanyProfile(name="Synthetic source", sources=[source])
    typed = focused_brief_input_from_profile(
        profile, brief_goal="Check qualifications in the report."
    )
    access = source.web_source_access
    assert access and all(q not in typed.source_context for q in QUALIFIERS)
    outputs = []
    for start in range(access.next_start_char, access.total_chars, 4000):
        outputs.append(
            [
                ResponseFunctionToolCall(
                    type="function_call",
                    name="read_web_source_window",
                    call_id=f"read-{start}",
                    arguments=json.dumps(
                        {
                            "source_id": access.source_id,
                            "selected_url": access.selected_url,
                            "expected_snapshot_sha256": access.snapshot_sha256,
                            "start_char": start,
                            "max_chars": 4000,
                        }
                    ),
                    status="completed",
                )
            ]
        )
    outputs.append(
        [
            ResponseOutputMessage(
                id="synthetic-focused",
                type="message",
                role="assistant",
                status="completed",
                content=[
                    ResponseOutputText(
                        type="output_text",
                        annotations=[],
                        text=json.dumps(
                            {
                                "company_name": "Synthetic source",
                                "answer": "The saved-source read completed.",
                                "source_ids_used": [source.source_id],
                                "sources": [
                                    {
                                        "source_id": source.source_id,
                                        "title": source.title,
                                        "url": source.url,
                                        "source_type": source.source_type,
                                    }
                                ],
                                "decision": {
                                    "decision_owner": "specialist_agent",
                                    "decision_stage": "research_source_selection",
                                    "selected_candidate_ids": [source.source_id],
                                    "candidate_assessments": [
                                        {
                                            "candidate_id": source.source_id,
                                            "disposition": "selected",
                                            "rationale": "Read the supplied snapshot.",
                                        }
                                    ],
                                    "reasoning": "Used the supplied saved-source evidence.",
                                    "needs_more_context": False,
                                },
                            }
                        ),
                    )
                ],
            )
        ]
    )

    class ModelStub(Model):
        def __init__(self):
            self.inputs = []

        async def get_response(
            self,
            system_instructions,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            **kwargs,
        ):
            assert [tool.name for tool in tools] == ["read_web_source_window"]
            self.inputs.append(input)
            return ModelResponse(
                output=outputs.pop(0),
                usage=Usage(requests=1),
                response_id=f"focused-{len(self.inputs)}",
            )

        def stream_response(self, *a, **k):
            raise NotImplementedError

    model = ModelStub()

    class Provider(ModelProvider):
        def get_model(self, model_name):
            return model

    result = run_business_research_analyst_focused_brief_sdk(
        typed,
        run_config=build_local_run_config(Provider()),
        live=False,
        attach_tools=False,
        compact_instructions=True,
        max_turns=12,
    )
    assert result.output.company_name == "Synthetic source"
    actual_input = json.dumps(model.inputs[-1])
    assert all(q in actual_input for q in QUALIFIERS)
    assert access.snapshot_sha256 in actual_input
    capture = os.environ.get("KBA_WEB_PROOF_DIR")
    if capture:
        (Path(capture) / "company-focused-sdk-inputs.json").write_text(
            json.dumps(
                {
                    "actual_sdk_inputs": model.inputs,
                    "final_output": result.output.model_dump(mode="json"),
                    "proof": "Company projection with scoped reads and scripted SDK model.",
                },
                indent=2,
            )
        )


def test_local_read_scope_cannot_open_a_different_saved_source():
    _, allowed = web.project_web_source(extraction("Allowed source " * 1000), selected_url=URL)
    _, other = web.project_web_source(extraction("Other source " * 1000), selected_url=RESOLVED)
    tool = web.scoped_web_source_read_tool([allowed])
    with pytest.raises(web.WebsiteExtractionError, match="outside this input scope"):
        tool(
            source_id=other.source_id,
            selected_url=other.selected_url,
            expected_snapshot_sha256=other.snapshot_sha256,
        )


def test_saved_web_snapshot_survives_a_new_process():
    import subprocess
    import sys

    text = "Persisted extracted evidence. " * 400 + QUALIFIERS[1]
    _, access = web.project_web_source(extraction(text), selected_url=URL)
    arguments = {
        "source_id": access.source_id,
        "selected_url": access.selected_url,
        "expected_snapshot_sha256": access.snapshot_sha256,
        "start_char": len(text) - len(QUALIFIERS[1]),
        "max_chars": 500,
    }
    program = (
        "import json,sys; "
        "from keystone_agents.tools.website_extraction_tool import read_web_source_window_impl; "
        "print(json.dumps(read_web_source_window_impl(**json.loads(sys.stdin.read()))))"
    )
    result = subprocess.run(
        [sys.executable, "-c", program],
        input=json.dumps(arguments),
        text=True,
        capture_output=True,
        check=True,
        env=dict(os.environ),
    )
    read = json.loads(result.stdout)
    assert read["text"] == QUALIFIERS[1]
    assert read["reached_end"]
    assert read["web_source_access"]["snapshot_sha256"] == access.snapshot_sha256


def test_snapshot_limits_and_invalid_coverage_are_explicit(monkeypatch):
    from pydantic import ValidationError

    monkeypatch.setattr(web, "MAX_WEB_SNAPSHOT_CHARS", 100)
    excerpt, access = web.project_web_source(extraction("x" * 200), selected_url=URL, max_chars=50)
    assert len(excerpt) == 50 and not access.available and not access.content_complete
    assert access.next_start_char is None and "limit" in access.limitation
    for changes in ({"end_char": 300}, {"content_complete": True}, {"next_start_char": 70}):
        with pytest.raises(ValidationError):
            web.WebSourceAccess.model_validate({**access.model_dump(), **changes})


def test_selected_error_and_short_source_preserve_per_source_status():
    def extract(url, **kwargs):
        if url == RESOLVED:
            raise web.WebsiteExtractionError("Synthetic provider extraction failure.")
        return extraction("Source has a short, qualified research result.", url=url)

    result = web.build_selected_url_source_bundle(
        company_name="Synthetic source",
        selected_urls=[URL, RESOLVED],
        live_extraction=True,
        extractor=extract,
    )
    assert result.extracted_source_count == 1
    assert result.source_bundle.sources[0].web_source_access.content_complete
    assert result.diagnostics[1].selected_url == RESOLVED
    assert result.diagnostics[1].status == "error"


def test_url_identifiers_cannot_overflow_response_without_any_text():
    urls = [f"https://example.org/{i}?query=" + "研" * 1900 for i in range(8)]
    with pytest.raises(web.WebsiteExtractionError, match="smaller batches"):
        web.build_selected_url_source_bundle(
            company_name="Synthetic source",
            selected_urls=urls,
            live_extraction=True,
            extractor=lambda *a, **k: pytest.fail(
                "Oversized identifiers must fail before extraction"
            ),
        )


def test_company_response_total_includes_records_errors_and_deferred_metadata(monkeypatch):
    urls = [f"https://example.org/{i}?query=" + "a" * 1900 for i in range(8)]
    monkeypatch.setattr(live_retrieval, "_company_website_extraction_urls", lambda **kw: urls)
    monkeypatch.setattr(
        live_retrieval,
        "extract_website_content",
        lambda url, **kw: extraction(
            "Research evidence " * 2000,
            url=url,
        ),
    )
    monkeypatch.setattr(live_retrieval, "agent_html_review_enabled", lambda: False)
    result = live_retrieval._extract_company_website_inputs(
        company="Synthetic source",
        company_url=URL,
        search_results=[],
        provider="trafilatura",
        max_pages=8,
        discover_internal_pages=False,
    )
    assert len(json.dumps(result)) <= web.MAX_WEB_BUNDLE_RESPONSE_CHARS
    rows, errors, stats = result
    assert not errors and rows
    assert {row["url"] for row in rows} | set(stats["deferred_selected_urls"]) == set(urls)
