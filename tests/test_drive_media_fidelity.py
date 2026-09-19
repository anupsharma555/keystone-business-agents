"""Offline source and SDK-input checks for mixed-media Drive reads."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from keystone_agents.tools import internal_data_tools as tool


def _pdf(pages: list[tuple[str, bool]]) -> bytes:
    writer = PdfWriter()
    for text, image in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        resources = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 60 690 Td ({text}) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
        if image:
            bitmap = DecodedStreamObject()
            bitmap.set_data(b"\xff")
            bitmap.update(
                {
                    NameObject("/Type"): NameObject("/XObject"),
                    NameObject("/Subtype"): NameObject("/Image"),
                    NameObject("/Width"): NumberObject(1),
                    NameObject("/Height"): NumberObject(1),
                    NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                    NameObject("/BitsPerComponent"): NumberObject(8),
                }
            )
            resources[NameObject("/XObject")] = DictionaryObject(
                {NameObject("/Scan"): writer._add_object(bitmap)}
            )
        page[NameObject("/Resources")] = resources
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _ocr(monkeypatch: pytest.MonkeyPatch, *, fail_page: int = 0) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(tool.shutil, "which", lambda name: "/fixture/" + name)

    def raster(args: list[str], **_: Any) -> None:
        Path(args[-1]).with_suffix(".png").write_bytes(b"fixture raster")

    monkeypatch.setattr(tool, "_run_bounded_command", raster)

    def ocr(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        page = int(Path(args[1]).stem.rsplit("-", 1)[-1])
        calls.append(page)
        return subprocess.CompletedProcess(
            args,
            int(page == fail_page),
            stdout=f"NOT APPROVED for page {page}. Validation is pending.",
            stderr="",
        )

    monkeypatch.setattr(tool.subprocess, "run", ocr)
    return calls


def _provider(monkeypatch: pytest.MonkeyPatch, content: bytes) -> None:
    class Request:
        def execute(self) -> dict[str, Any]:
            return {
                "id": "syntheticMedia",
                "name": "fixture.pdf",
                "mimeType": "application/pdf",
                "size": str(len(content)),
                "webViewLink": "https://example.org/source.pdf",
            }

    class Drive:
        def files(self) -> Drive:
            return self

        def get(self, **_: Any) -> Request:
            return Request()

    for name, value in [
        ("_google_workspace_services", {"drive": Drive()}),
        ("_find_drive_folder_path", "syntheticFolder"),
        ("_assert_configured_google_account", None),
        ("_assert_drive_file_under_folder", None),
        ("_start_google_workspace_read_attempt", None),
        ("_download_drive_media_bounded", content),
    ]:
        monkeypatch.setattr(tool, name, lambda *args, _value=value, **kwargs: _value)


@pytest.mark.parametrize(
    "pages", [[("Overview.", False), ("", True)], [("Overview.", True)], [("", True)]]
)
def test_mixed_pdf_preserves_image_qualification(
    monkeypatch: pytest.MonkeyPatch, pages: list[tuple[str, bool]]
) -> None:
    calls = _ocr(monkeypatch)
    coverage: dict[str, Any] = {}
    text, _, blocker, count = tool._extract_drive_media_text(
        _pdf(pages),
        mime_type="application/pdf",
        filename="fixture.pdf",
        max_pages=8,
        coverage=coverage,
    )
    assert "NOT APPROVED" in text and "Validation is pending" in text
    assert calls == [i + 1 for i, (_, image) in enumerate(pages) if image]
    assert not blocker and count == len(pages)
    assert coverage["failed_pages"] == []
    assert "figure meaning" in coverage["scope"]


def test_ocr_failure_retains_other_page_and_exposes_missing_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ocr(monkeypatch, fail_page=2)
    _provider(monkeypatch, _pdf([("", True), ("", True)]))
    result = tool.google_drive_media_ocr_read_impl("syntheticMedia", live=True)
    assert "NOT APPROVED for page 1" in result["text"]
    assert result["status"] == "partial" and not result["content_complete"]
    assert result["coverage"]["failed_pages"] == [2]
    assert "Page 2" in result["blocker"]


def test_returned_page_requests_reach_late_constraint(monkeypatch: pytest.MonkeyPatch) -> None:
    _provider(
        monkeypatch,
        _pdf(
            [
                ("Early context.", False),
                ("NOT APPROVED without review.", False),
                ("Expires after evaluation.", False),
            ]
        ),
    )
    result = tool.google_drive_media_ocr_read_impl("syntheticMedia", max_pages=1, live=True)
    outputs = [result]
    while result["next_request"]:
        result = tool.google_drive_media_ocr_read_impl(**result["next_request"], live=True)
        outputs.append(result)
        assert len(outputs) <= 3
    assert [r["coverage"]["start_page"] for r in outputs] == [1, 2, 3]
    assert "NOT APPROVED" in outputs[1]["text"]
    assert outputs[0]["truncated"] and not outputs[-1]["truncated"]
    assert len({r["content_sha256"] for r in outputs}) == 1


def test_char_windows_preserve_boundary_and_pin_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    _provider(monkeypatch, _pdf([("Alpha " * 110 + "NOT APPROVED. Review required.", False)]))
    whole = tool.google_drive_media_ocr_read_impl("syntheticMedia", live=True)["text"]
    result = tool.google_drive_media_ocr_read_impl("syntheticMedia", max_chars=500, live=True)
    pieces = [result["text"]]
    while result["next_request"]:
        result = tool.google_drive_media_ocr_read_impl(**result["next_request"], live=True)
        pieces.append(result["text"])
    assert "".join(pieces) == whole
    with pytest.raises(ValueError, match="changed"):
        tool.google_drive_media_ocr_read_impl(
            "syntheticMedia", expected_content_sha256="0" * 64, live=True
        )
    with pytest.raises(ValueError, match="Text continuation"):
        tool.google_drive_media_ocr_read_impl(
            "syntheticMedia",
            start_char=500,
            expected_content_sha256=result["content_sha256"],
            live=True,
        )


def test_actual_workspace_sdk_input_contains_mixed_media_qualification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agents import Runner
    from test_context_agent_fake_model_matrix import (
        FakeModel,
        FakeProvider,
        _structured_message,
        _tool_call,
    )

    from keystone_agents.agents.google_workspace_context import build_google_workspace_context_agent
    from keystone_agents.sdk import build_local_run_config

    _ocr(monkeypatch)
    _provider(monkeypatch, _pdf([("Pilot overview.", True)]))
    model = FakeModel(
        [
            [
                _tool_call(
                    "google_drive_media_ocr_read",
                    {"file_id_or_url": "syntheticMedia", "live": True},
                    call_id="media-read",
                )
            ],
            [_structured_message({"summary": "Offline transport proof."})],
        ]
    )
    agent = build_google_workspace_context_agent().clone(output_type=None)
    Runner.run_sync(
        agent,
        "Read the selected PDF and retain its conditions.",
        max_turns=3,
        run_config=build_local_run_config(model_provider=FakeProvider(model)),
    )
    initial = json.dumps(model.calls[0]["input"], default=str)
    deciding = json.dumps(model.calls[-1]["input"], default=str)
    assert "NOT APPROVED" not in initial
    assert "NOT APPROVED" in deciding and "Validation is pending" in deciding
    assert "https://example.org/source.pdf" in deciding
    assert "ocr_accuracy_verified" in deciding


def test_parser_failure_recovers_ocr_with_bounded_page_continuation(monkeypatch) -> None:
    import pypdf

    content = _pdf([("", True), ("", True)])
    _provider(monkeypatch, content)

    def broken_reader(*_args, **_kwargs):
        raise ValueError("synthetic parser failure")

    monkeypatch.setattr(pypdf, "PdfReader", broken_reader)
    monkeypatch.setattr(tool.shutil, "which", lambda name: "/fixture/" + name)
    rendered = []

    def raster(args, **_kwargs):
        rendered.append(args)
        Path(args[-1]).with_suffix(".png").write_bytes(b"fixture raster")

    def command(args, **_kwargs):
        if Path(args[0]).name == "pdfinfo":
            return subprocess.CompletedProcess(args, 0, stdout="Pages: 2\n")
        page = int(Path(args[1]).stem.rsplit("-", 1)[-1])
        return subprocess.CompletedProcess(args, 0, stdout=f"Page {page}: NOT APPROVED.")

    monkeypatch.setattr(tool, "_run_bounded_command", raster)
    monkeypatch.setattr(tool.subprocess, "run", command)
    first = tool.google_drive_media_ocr_read_impl("syntheticMedia", max_pages=1, live=True)
    second = tool.google_drive_media_ocr_read_impl(**first["next_request"], live=True)
    assert "Page 1: NOT APPROVED" in first["text"]
    assert "Page 2: NOT APPROVED" in second["text"]
    assert first["coverage"]["page_count_source"] == "pdfinfo"
    assert first["truncated"] and not second["truncated"]
    assert second["next_request"] is None
    assert [args[args.index("-f") + 1] for args in rendered] == ["1", "2"]
    assert all(args[args.index("-f") + 1] == args[args.index("-l") + 1] for args in rendered)
    assert all(
        int(args[args.index("-scale-to") + 1]) ** 2 <= tool.GOOGLE_DRIVE_OCR_MAX_PIXELS
        for args in rendered
    )


def test_parser_failure_without_fallback_tool_reports_partial(monkeypatch) -> None:
    import pypdf

    content = _pdf([("", True)])
    _provider(monkeypatch, content)

    def broken_reader(*_args, **_kwargs):
        raise ValueError("synthetic parser failure")

    monkeypatch.setattr(pypdf, "PdfReader", broken_reader)
    monkeypatch.setattr(tool.shutil, "which", lambda _name: None)
    result = tool.google_drive_media_ocr_read_impl("syntheticMedia", live=True)
    assert result["status"] == "partial"
    assert not result["content_complete"] and result["next_request"] is None
    assert "requires pdfinfo" in result["blocker"]


@pytest.mark.parametrize("ocr_available", [False, True])
def test_image_metadata_error_never_discards_embedded_evidence(monkeypatch, ocr_available) -> None:
    import pypdf

    content = _pdf([("NOT APPROVED without validation.", True)])
    _provider(monkeypatch, content)
    real_reader = pypdf.PdfReader
    reader = real_reader(io.BytesIO(content))
    page_type = type(reader.pages[0])

    def broken_images(_self):
        raise ValueError("synthetic image inspection failure")

    monkeypatch.setattr(page_type, "images", property(broken_images))
    if ocr_available:
        _ocr(monkeypatch, fail_page=1)
    else:
        monkeypatch.setattr(tool.shutil, "which", lambda _name: None)
    result = tool.google_drive_media_ocr_read_impl("syntheticMedia", live=True)
    assert "NOT APPROVED without validation" in result["text"]
    assert result["status"] == "partial" and not result["content_complete"]
    assert result["coverage"]["pages"][0]["image_inspection_failed"]
    assert "image metadata unavailable" in result["blocker"]
