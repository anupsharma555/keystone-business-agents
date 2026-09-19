"""Offline source-to-tool and real SDK-loop Zotero fidelity checks."""

from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from keystone_agents.schemas.zotero_reads import (
    ZoteroChildrenContinuation,
    ZoteroMetadataContinuation,
    ZoteroNoteContinuation,
    ZoteroPdfContinuation,
)
from keystone_agents.tools import zotero_context_tools as z

QUALIFIER = "External validation did NOT establish benefit."
NOTE_QUALIFIER = "NOT APPROVED. Validation is pending."


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(
        z,
        "context_env_value",
        lambda name: "synthetic-key" if name == "ZOTERO_API_KEY" else "123456",
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Network is forbidden in Zotero fidelity tests")

    monkeypatch.setattr(z, "urlopen", forbidden)


class Response:
    def __init__(self, items, headers):
        self.payload = json.dumps(items).encode()
        self.headers = headers

    def read(self, *_args):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def item(key, abstract=""):
    return {
        "key": key,
        "version": 4,
        "data": {"itemType": "journalArticle", "title": key, "abstractNote": abstract},
    }


def install_pages(monkeypatch, *, version=5, total=3, path="/users/123456/items"):
    calls = []

    def provider(request, **_kwargs):
        parsed = urlsplit(request.full_url)
        params = parse_qs(parsed.query)
        calls.append({"path": parsed.path, "params": params})
        start = int(params.get("start", ["0"])[0])
        headers = {"Total-Results": str(total), "Last-Modified-Version": str(version)}
        if start == 0:
            headers["Link"] = f'<https://api.zotero.org{path}?limit=2&start=2>; rel="next"'
            return Response([item("FIRST"), item("SECOND")], headers)
        return Response([item("THIRD", QUALIFIER)], headers)

    monkeypatch.setattr(z, "urlopen", provider)
    return calls


def test_next_page_preserves_query_scope_and_negative_qualifier(monkeypatch):
    path = "/groups/123456/collections/COLL1/items/top"
    calls = install_pages(monkeypatch, path=path)
    first = json.loads(
        z.zotero_read_api_metadata(
            library_type="group",
            library_id="123456",
            collection_key="COLL1",
            query='Café "Northstar" – validation',
            tag="methods review",
            limit=2,
            item_type="journalArticle",
            sort="date",
            direction="desc",
            top_level_only=True,
            require_abstract=True,
            live=True,
        )
    )
    assert first["status"] == "partial"
    assert first["absence_scope"] == "current_page"
    assert first["coverage"]["total_results"] == 3
    assert first["available_item_count"] == 0
    second = json.loads(
        z.zotero_read_api_metadata(
            continuation=ZoteroMetadataContinuation.model_validate(first["continuation"]),
            live=True,
        )
    )
    assert calls[0]["path"] == calls[1]["path"] == path
    assert {k: v for k, v in calls[1]["params"].items() if k != "start"} == calls[0]["params"]
    assert second["items"][0]["data"]["abstractNote"] == QUALIFIER
    assert second["selected_item_key"] == "THIRD"
    assert second["coverage"]["complete"] is True
    assert second["continuation"] is None


def test_metadata_continuation_rejects_version_scope_and_foreign_links(monkeypatch):
    install_pages(monkeypatch)
    first = json.loads(z.zotero_read_api_metadata(limit=2, require_abstract=True, live=True))
    cursor = ZoteroMetadataContinuation.model_validate(first["continuation"])
    with pytest.raises(ValueError, match="cannot change query"):
        z.zotero_read_api_metadata(continuation=cursor, query="other", live=True)
    install_pages(monkeypatch, version=6)
    with pytest.raises(RuntimeError, match="version changed"):
        z.zotero_read_api_metadata(continuation=cursor, live=True)
    install_pages(monkeypatch, path="/users/999999/items")
    with pytest.raises(RuntimeError, match="does not match"):
        z.zotero_read_api_metadata(limit=2, live=True)


def test_metadata_caps_and_rank_across_pages(monkeypatch):
    def read(_path, **kwargs):
        start = kwargs["params"].get("start", 0)
        return z._ZoteroAPIItems(
            [item(str(start + n)) for n in range(2)],
            {"Total-Results": "4", "Last-Modified-Version": "3"},
        )

    monkeypatch.setattr(z, "_read_zotero_api_json", read)
    first = json.loads(z.zotero_read_api_metadata(limit=2, selection_rank=3, live=True))
    second = json.loads(z.zotero_read_api_metadata(continuation=first["continuation"], live=True))
    assert second["selected_item_key"] == "2"
    cursor = ZoteroMetadataContinuation.model_validate(first["continuation"])
    cursor.pages_read = 9
    cursor.next_start = 18
    monkeypatch.setattr(
        z,
        "_read_zotero_api_json",
        lambda *_a, **_k: z._ZoteroAPIItems(
            [item("18"), item("19")],
            {"Total-Results": "2000", "Last-Modified-Version": "3"},
        ),
    )
    result = json.loads(z.zotero_read_api_metadata(continuation=cursor, live=True))
    assert result["continuation"] is None
    assert result["coverage"]["has_more"] is True
    assert "budget" in " ".join(result["coverage"]["limitations"])


def test_metadata_unknown_total_is_page_local(monkeypatch):
    monkeypatch.setattr(z, "_read_zotero_api_json", lambda *_a, **_k: [])
    result = json.loads(z.zotero_read_api_metadata(require_abstract=True, live=True))
    assert result["absence_scope"] == "current_page"
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["limitations"]


def note(raw=None):
    return {
        "key": "NOTE1",
        "version": 7,
        "data": {
            "itemType": "note",
            "parentItem": "PARENT1",
            "note": raw
            or "<p>Background. " + "neutral evidence. " * 1000 + NOTE_QUALIFIER + "</p>",
        },
    }


def install_note(monkeypatch, record=None):
    record = record or note()
    calls = []

    def read(path, **_kwargs):
        calls.append(path)
        return (
            z._ZoteroAPIItems([record], {"Total-Results": "1", "Last-Modified-Version": "9"})
            if path.endswith("/children")
            else record
        )

    monkeypatch.setattr(z, "_read_zotero_api_json", read)
    return calls


def test_long_note_later_window_preserves_qualification_and_source_identity(monkeypatch):
    calls = install_note(monkeypatch)
    first = json.loads(z.zotero_read_item_children(parent_item_key="PARENT1", live=True))
    child = first["children"][0]
    assert len(child["note_text"]) == 12000
    assert NOTE_QUALIFIER not in child["note_text"]
    assert child["note_text_coverage"]["complete"] is False
    cursor = ZoteroNoteContinuation.model_validate(child["note_continuation"])
    second = json.loads(
        z.zotero_read_item_children(parent_item_key="PARENT1", note_continuation=cursor, live=True)
    )
    later = second["children"][0]
    assert NOTE_QUALIFIER in later["note_text"]
    assert later["note_continuation"] is None
    assert later["note_text_coverage"]["char_start"] == 12000
    assert (
        later["note_text_coverage"]["source_sha256"] == child["note_text_coverage"]["source_sha256"]
    )
    assert calls[-1] == "/users/123456/items/NOTE1"


@pytest.mark.parametrize("change", ["key", "version", "body", "parent", "type"])
def test_note_continuation_rejects_stale_or_different_source(monkeypatch, change):
    record = note()
    install_note(monkeypatch, record)
    result = json.loads(z.zotero_read_item_children(parent_item_key="PARENT1", live=True))
    cursor = result["children"][0]["note_continuation"]
    if change == "key":
        record["key"] = "OTHER"
    elif change == "version":
        record["version"] += 1
    elif change == "body":
        record["data"]["note"] += "new content"
    elif change == "type":
        record["data"]["itemType"] = "webpage"
    else:
        record["data"]["parentItem"] = "OTHER"
    with pytest.raises(RuntimeError, match="changed|does not belong"):
        z.zotero_read_item_children(parent_item_key="PARENT1", note_continuation=cursor, live=True)


def test_children_pagination_and_note_budget(monkeypatch):
    record = note("<p>" + "neutral " * 17000 + NOTE_QUALIFIER + "</p>")

    def read(path, **kwargs):
        if not path.endswith("/children"):
            return record
        start = kwargs["params"].get("start", 0)
        child = (
            record
            if start
            else {
                "key": "PDF1",
                "version": 1,
                "data": {"itemType": "attachment", "parentItem": "PARENT1"},
            }
        )
        return z._ZoteroAPIItems([child], {"Total-Results": "2", "Last-Modified-Version": "5"})

    monkeypatch.setattr(z, "_read_zotero_api_json", read)
    first = json.loads(z.zotero_read_item_children(parent_item_key="PARENT1", limit=1, live=True))
    assert first["coverage"]["has_more"]
    second = json.loads(
        z.zotero_read_item_children(
            parent_item_key="PARENT1",
            continuation=ZoteroChildrenContinuation.model_validate(first["continuation"]),
            live=True,
        )
    )
    child = second["children"][0]
    assert child["item_key"] == "NOTE1"
    for _ in range(9):
        next_result = json.loads(
            z.zotero_read_item_children(
                parent_item_key="PARENT1", note_continuation=child["note_continuation"], live=True
            )
        )
        child = next_result["children"][0]
    assert child["note_continuation"] is None
    assert child["note_text_coverage"]["has_more"]
    assert child["note_text_coverage"]["limitations"]


def pdf_bytes(pages):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

    writer = PdfWriter()
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    for text, image in pages:
        page = writer.add_blank_page(width=612, height=792)
        resources = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        commands = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET\n" if text else ""
        if image:
            raster = DecodedStreamObject()
            raster.set_data(b"\x00\x00\x00")
            raster.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(1),
                    NameObject("/Height"): NumberObject(1),
                    NameObject("/ColorSpace"): NameObject("/DeviceRGB"),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                }
            )
            resources[NameObject("/XObject")] = DictionaryObject(
                {NameObject("/Im1"): writer._add_object(raster)}
            )
            commands += "q 100 0 0 100 72 400 cm /Im1 Do Q\n"
        page[NameObject("/Resources")] = resources
        content = DecodedStreamObject()
        content.set_data(commands.encode())
        page[NameObject("/Contents")] = writer._add_object(content)
    result = io.BytesIO()
    writer.write(result)
    return result.getvalue()


def install_pdf(monkeypatch, raw):
    record = {
        "key": "PDF1",
        "version": 8,
        "data": {
            "itemType": "attachment",
            "parentItem": "PARENT1",
            "filename": "source.pdf",
            "contentType": "application/pdf",
        },
    }
    monkeypatch.setattr(z, "_read_zotero_api_json", lambda *_a, **_k: record)
    monkeypatch.setattr(z, "_read_zotero_api_bytes", lambda *_a, **_k: (raw, "application/pdf"))
    return record


@pytest.mark.parametrize(
    "pages, expected",
    [
        ([(NOTE_QUALIFIER, False)], "success"),
        ([("", True)], "partial"),
        ([("Available prefix", True)], "partial"),
        ([("Available prefix", False), ("", True)], "partial"),
    ],
)
def test_pdf_reports_selectable_text_and_unread_visual_coverage(monkeypatch, pages, expected):
    install_pdf(monkeypatch, pdf_bytes(pages))
    result = json.loads(
        z.zotero_read_pdf_attachment_text(
            parent_item_key="PARENT1", attachment_item_key="PDF1", live=True
        )
    )
    assert result["status"] == expected
    assert len(result["pages"]) == len(pages)
    assert result["visual_content_read"] is False
    assert result["ocr_supported"] is False
    if expected == "partial":
        assert result["coverage"] == "partial"
        assert any(p["raster_content_detected"] for p in result["pages"])
        assert any("does not support OCR" in limitation for limitation in result["limitations"])
    else:
        assert NOTE_QUALIFIER in result["text"]


def test_pdf_page_and_character_continuation_preserves_later_text(monkeypatch):
    raw = pdf_bytes([("prefix " * 220 + NOTE_QUALIFIER, False), ("Last page qualifier.", False)])
    install_pdf(monkeypatch, raw)
    result = json.loads(
        z.zotero_read_pdf_attachment_text(
            parent_item_key="PARENT1",
            attachment_item_key="PDF1",
            max_pages=1,
            max_chars=1000,
            live=True,
        )
    )
    assert result["truncated"] and len(result["text"]) == 1000
    assert NOTE_QUALIFIER not in result["text"]
    seen = [result["text"]]
    while result["continuation"]:
        result = json.loads(
            z.zotero_read_pdf_attachment_text(
                parent_item_key="PARENT1",
                attachment_item_key="PDF1",
                continuation=ZoteroPdfContinuation.model_validate(result["continuation"]),
                live=True,
            )
        )
        seen.append(result["text"])
    assert NOTE_QUALIFIER in "".join(seen)
    assert "Last page qualifier." in seen[-1]
    assert result["pages"][0]["page_number"] == 2
    assert result["truncated"] is False


def test_pdf_continuation_rejects_changed_bytes_and_parent(monkeypatch):
    record = install_pdf(monkeypatch, pdf_bytes([("First", False), ("Second", False)]))
    first = json.loads(
        z.zotero_read_pdf_attachment_text(
            parent_item_key="PARENT1", attachment_item_key="PDF1", max_pages=1, live=True
        )
    )
    cursor = first["continuation"]
    monkeypatch.setattr(
        z,
        "_read_zotero_api_bytes",
        lambda *_a, **_k: (pdf_bytes([("changed", False)]), "application/pdf"),
    )
    with pytest.raises(RuntimeError, match="bytes changed"):
        z.zotero_read_pdf_attachment_text(
            parent_item_key="PARENT1", attachment_item_key="PDF1", continuation=cursor, live=True
        )
    record["data"]["parentItem"] = "OTHER"
    with pytest.raises(RuntimeError, match="does not belong"):
        z.zotero_read_pdf_attachment_text(
            parent_item_key="PARENT1", attachment_item_key="PDF1", live=True
        )


def test_dry_run_and_credentials_never_touch_transport(monkeypatch):
    monkeypatch.setattr(z, "context_env_value", lambda _name: "")
    for tool, arguments in [
        (z.zotero_read_api_metadata, {}),
        (z.zotero_read_item_children, {"parent_item_key": "PARENT1"}),
        (
            z.zotero_read_pdf_attachment_text,
            {"parent_item_key": "PARENT1", "attachment_item_key": "PDF1"},
        ),
    ]:
        result = json.loads(tool(**arguments, live=False))
        assert result["status"] == "dry-run"
        assert result["send_enabled"] is False
        with pytest.raises(RuntimeError, match="requires?|require"):
            tool(**arguments, live=True)


def _latest_tool_output(model_input):
    return json.loads(
        next(
            item["output"]
            for item in reversed(model_input)
            if isinstance(item, dict) and item.get("type") == "function_call_output"
        )
    )


def _save_proof(name, payload):
    directory = os.environ.get("KEYSTONE_ZOTERO_FIDELITY_PROOF_DIR")
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / f"{name}.json").write_text(
            json.dumps(payload, indent=2, default=str) + "\n"
        )


@pytest.mark.parametrize("kind", ["metadata", "note", "pdf"])
def test_source_reaches_actual_sdk_input_and_persisted_session(monkeypatch, tmp_path, kind):
    from test_context_agent_fake_model_matrix import (
        FakeModel,
        FakeProvider,
        _structured_message,
        _tool_call,
    )

    from keystone_agents.agents.zotero_context import build_zotero_context_agent
    from keystone_agents.run import run_typed_sdk_agent
    from keystone_agents.schemas.operational_context import ZoteroContextResult
    from keystone_agents.sdk import build_local_run_config, build_sqlite_session

    if kind == "metadata":
        install_pages(monkeypatch)
        tool_name = "zotero_read_api_metadata"
        arguments = {"limit": 2, "require_abstract": True, "live": True}
        summary = QUALIFIER
    elif kind == "note":
        install_note(monkeypatch)
        tool_name = "zotero_read_item_children"
        arguments = {"parent_item_key": "PARENT1", "live": True}
        summary = NOTE_QUALIFIER
    else:
        install_pdf(monkeypatch, pdf_bytes([("Available text.", False), ("", True)]))
        tool_name = "zotero_read_pdf_attachment_text"
        arguments = {"parent_item_key": "PARENT1", "attachment_item_key": "PDF1", "live": True}
        summary = (
            "Selectable text is available; page 2 contains unread visual content. "
            "OCR is unsupported."
        )

    class SourceModel(FakeModel):
        async def get_response(self, *args, **kwargs):
            model_input = kwargs.get("input", args[1] if len(args) > 1 else None)
            step = len(self.calls)
            if step == 0:
                self.outputs = [[_tool_call(tool_name, arguments, call_id="read-source")]]
            elif step == 1 and kind != "pdf":
                output = _latest_tool_output(model_input)
                cursor = (
                    output["continuation"]
                    if kind == "metadata"
                    else output["children"][0]["note_continuation"]
                )
                continuation_arguments = (
                    {"continuation": cursor, "live": True}
                    if kind == "metadata"
                    else {"parent_item_key": "PARENT1", "note_continuation": cursor, "live": True}
                )
                self.outputs = [
                    [_tool_call(tool_name, continuation_arguments, call_id="continue-source")]
                ]
            else:
                source = _latest_tool_output(model_input)
                encoded = json.dumps(source)
                if kind == "pdf":
                    assert source["pages"][1]["raster_content_detected"]
                    assert source["status"] == "partial"
                    assert source["ocr_supported"] is False
                else:
                    assert summary in encoded
                self.outputs = [
                    [
                        _structured_message(
                            {"mode": "llm", "summary": summary, "relevant_evidence": [summary]}
                        )
                    ]
                ]
            return await super().get_response(*args, **kwargs)

    model = SourceModel([])
    session = build_sqlite_session(f"zotero-{kind}", tmp_path / "session.sqlite")
    agent = build_zotero_context_agent(
        request_text="Read Zotero article metadata, notes and PDF evidence. Make no changes.",
        tool_tier="core_read",
    )
    result = run_typed_sdk_agent(
        agent=agent,
        typed_input="Read the bounded source and retain its qualifications and coverage.",
        output_type=ZoteroContextResult,
        run_config=build_local_run_config(FakeProvider(model)),
        session=session,
    )
    persisted = asyncio.run(session.get_items())
    persisted_text = json.dumps(persisted, default=str)
    assert result.final_output.summary == summary
    assert summary in persisted_text
    if kind == "pdf":
        assert "raster_content_detected" in persisted_text
        assert "does not support OCR" in persisted_text
    else:
        assert "continuation" in persisted_text
    _save_proof(
        f"{kind}-sdk-input",
        {
            "proof_boundary": (
                "Real SDK tool loop and SQLiteSession with scripted model/injected provider; "
                "no semantic quality or network claim."
            ),
            "model_calls": [
                {"input": call["input"], "tool_names": call["tool_names"]} for call in model.calls
            ],
            "persisted_session": persisted,
            "final_output": result.final_output.model_dump(mode="json"),
        },
    )


def test_recovered_note_reaches_actual_parent_model_tool_response(monkeypatch, tmp_path):
    from test_context_agent_fake_model_matrix import (
        FakeModel,
        FakeProvider,
        _structured_message,
        _tool_call,
    )

    from keystone_agents.agents.chief_of_staff import build_chief_of_staff_agent
    from keystone_agents.run import run_typed_sdk_agent
    from keystone_agents.schemas.chief_of_staff import ChiefOfStaffResult, ChiefSpecialistToolInput
    from keystone_agents.sdk import build_local_run_config, build_sqlite_session

    install_note(monkeypatch)
    parent_calls = []
    child_calls = []

    class NestedSourceModel(FakeModel):
        async def get_response(self, *args, **kwargs):
            model_input = kwargs.get("input", args[1] if len(args) > 1 else None)
            model_tools = kwargs.get("tools", args[3] if len(args) > 3 else [])
            is_parent = any(
                tool.name == "list_chief_of_staff_context_sources" for tool in model_tools
            )
            if is_parent:
                parent_calls.append(model_input)
                if len(parent_calls) == 1:
                    child_input = ChiefSpecialistToolInput(
                        raw_operator_request=(
                            "Read all text of the stored Zotero note for PARENT1, "
                            "including qualifications."
                        ),
                        specialist_task=(
                            "Read the note and its later continuation, "
                            "preserving source/version and limitations."
                        ),
                        provider_call_context={
                            "provider": "zotero",
                            "operation": "read",
                            "parent_item_key": "PARENT1",
                        },
                        side_effect_boundaries=["no_nested_live_write"],
                    ).model_dump(mode="json")
                    self.outputs = [
                        [
                            _tool_call(
                                "zotero_context_agent_as_specialist_tool",
                                child_input,
                                call_id="parent-zotero-note",
                            )
                        ]
                    ]
                else:
                    envelope = _latest_tool_output(model_input)
                    assert envelope["validation_status"] == "ok", envelope
                    assert NOTE_QUALIFIER in json.dumps(envelope), envelope
                    assert "version 7" in json.dumps(envelope)
                    assert "normalized_text_start=12000" in json.dumps(envelope)
                    assert "source_sha256=" in json.dumps(envelope)
                    assert "HTML layout not verified" in json.dumps(envelope)
                    self.outputs = [
                        [
                            _structured_message(
                                {
                                    "mode": "llm",
                                    "summary": NOTE_QUALIFIER,
                                    "synthesis": "Stored note version 7: " + NOTE_QUALIFIER,
                                }
                            )
                        ]
                    ]
            else:
                child_calls.append(model_input)
                if len(child_calls) == 1:
                    self.outputs = [
                        [
                            _tool_call(
                                "zotero_read_item_children",
                                {"parent_item_key": "PARENT1", "live": True},
                                call_id="child-note",
                            )
                        ]
                    ]
                elif len(child_calls) == 2:
                    source = _latest_tool_output(model_input)
                    self.outputs = [
                        [
                            _tool_call(
                                "zotero_read_item_children",
                                {
                                    "parent_item_key": "PARENT1",
                                    "note_continuation": source["children"][0]["note_continuation"],
                                    "live": True,
                                },
                                call_id="child-note-tail",
                            )
                        ]
                    ]
                else:
                    source = _latest_tool_output(model_input)
                    assert NOTE_QUALIFIER in source["children"][0]["note_text"]
                    coverage = source["children"][0]["note_text_coverage"]
                    payload = {
                        "mode": "llm",
                        "summary": NOTE_QUALIFIER,
                        "zotero_item_keys": ["NOTE1"],
                        "relevant_evidence": [
                            "Stored note version 7: "
                            + NOTE_QUALIFIER
                            + " source_sha256="
                            + coverage["source_sha256"]
                            + " normalized_text_start="
                            + str(coverage["char_start"])
                            + " normalized_text_end="
                            + str(coverage["char_end"])
                        ],
                        "decision": {
                            "decision_owner": "specialist_agent",
                            "decision_stage": "zotero_item_selection",
                            "selected_candidate_ids": ["NOTE1"],
                            "candidate_assessments": [
                                {
                                    "candidate_id": "NOTE1",
                                    "disposition": "selected",
                                    "rationale": "Exact note contains the requested qualification.",
                                },
                                {
                                    "candidate_id": "PARENT1",
                                    "disposition": "excluded",
                                    "rationale": "Parent scopes the note; the note is the source.",
                                },
                            ],
                            "reasoning": "The later note window contains the qualification.",
                            "limitations": [
                                "Plain text from note version 7; HTML layout not verified."
                            ],
                            "needs_more_context": False,
                        },
                    }
                    self.outputs = [[_structured_message(payload)]]
            return await super().get_response(*args, **kwargs)

    model = NestedSourceModel([])
    agent = build_chief_of_staff_agent(include_specialist_tools=True)
    session = build_sqlite_session("zotero-parent", tmp_path / "parent.sqlite")
    result = run_typed_sdk_agent(
        agent=agent,
        typed_input=(
            "Read the stored Zotero note under PARENT1 and retain its qualifications. "
            "Do not write anything."
        ),
        output_type=ChiefOfStaffResult,
        run_config=build_local_run_config(FakeProvider(model)),
        session=session,
        max_turns=6,
    )
    assert result.final_output.summary == NOTE_QUALIFIER
    assert len(parent_calls) == 2 and len(child_calls) == 3
    persisted = asyncio.run(session.get_items())
    assert NOTE_QUALIFIER in json.dumps(persisted, default=str)
    assert "normalized_text_start=12000" in json.dumps(persisted, default=str)
    tool = next(t for t in agent.tools if t.name == "zotero_context_agent_as_specialist_tool")
    record = tool.nested_execution_records["parent-zotero-note"]
    assert record["handoff"]["terminal_status"] == "completed"
    assert "NOTE1" in record["candidate_universe"]
    _save_proof(
        "note-parent-input",
        {
            "proof_boundary": (
                "Actual nested SDK child and parent tool response; scripted models. "
                "SQLite parent-session persistence; no WorkItem/LangGraph traversal."
            ),
            "parent_inputs": parent_calls,
            "child_inputs": child_calls,
            "nested_record": record,
            "persisted_parent_session": persisted,
            "final_output": result.final_output.model_dump(mode="json"),
        },
    )


def test_child_evidence_handoff_retains_limits_and_reports_omissions():
    from types import SimpleNamespace

    from keystone_agents.specialist_agent_tools import (
        _sanitize_nested_public_envelope,
        extract_nested_specialist_result,
    )

    output = {
        "summary": "Bounded synthetic result",
        "relevant_evidence": ["NOTE1 " + "x" * 1100] * 11,
        "decision": {"limitations": ["NOTE1 visual content is unread."]},
    }
    envelope = extract_nested_specialist_result(
        run_result=SimpleNamespace(final_output=output),
        route_name="zotero_context_agent",
        tool_name="zotero_context_agent_as_specialist_tool",
    )
    fields = {entry.key: entry.value for entry in envelope.human_work_context}
    coverage = json.loads(fields["relevant_evidence_coverage"])
    assert coverage == {
        "total_entries": 11,
        "retained_entries": 10,
        "omitted_entries": 1,
        "truncated_entries": 10,
        "complete": False,
    }
    assert "visual content is unread" in fields["decision_limitations_1"]
    sanitized = _sanitize_nested_public_envelope(
        envelope,
        route_name="zotero_context_agent",
        internal_record={"candidate_universe": ["NOTE1"]},
    )
    serialized = sanitized.model_dump_json()
    assert "NOTE1" not in serialized
    assert "visual content is unread" in serialized


def test_nested_identity_contract_includes_exact_pdf_attachment(monkeypatch):
    from keystone_agents import specialist_agent_tools

    monkeypatch.setattr(
        specialist_agent_tools,
        "sdk_tool_output_payloads",
        lambda _result: [{"output": {"parent_item_key": "PARENT1", "attachment_item_key": "PDF1"}}],
    )
    assert specialist_agent_tools._nested_provider_candidate_ids(
        "zotero_context_agent",
        object(),
    ) == ("PARENT1", "PDF1")
