from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from keystone_agents import local_file_inputs
from keystone_agents.local_file_inputs import (
    local_file_input_bundle_from_operator_input,
    local_file_input_bundle_from_text,
    read_supported_local_file,
)


def test_local_file_input_bundle_builds_pdf_and_image_response_parts(tmp_path: Path) -> None:
    pdf_path = tmp_path / "receipt.pdf"
    image_path = tmp_path / "receipt.png"
    pdf_path.write_bytes(b"%PDF-1.4\nreceipt fixture")
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfixture")

    bundle = local_file_input_bundle_from_text(
        f"Use the receipt PDF {pdf_path}. Also inspect image {image_path})"
    )

    assert bundle.has_inputs is True
    assert [attachment.kind for attachment in bundle.attachments] == [
        "input_file",
        "input_image",
    ]
    assert bundle.attachments[0].filename == "receipt.pdf"
    assert bundle.attachments[0].mime_type == "application/pdf"
    assert bundle.attachments[0].input_part["file_data"].startswith(
        "data:application/pdf;base64,"
    )
    assert bundle.attachments[1].filename == "receipt.png"
    assert bundle.attachments[1].mime_type == "image/png"
    assert bundle.attachments[1].input_part["image_url"].startswith(
        "data:image/png;base64,"
    )

    response_input = bundle.response_input("read the files")
    content = response_input[0]["content"]
    assert content[0] == {"type": "input_text", "text": "read the files"}
    assert "do not infer from filename alone" in content[1]["text"]
    assert content[2]["type"] == "input_file"
    assert content[3]["type"] == "input_image"


def test_local_file_input_bundle_deduplicates_paths_and_reports_blocked_files(
    tmp_path: Path,
) -> None:
    pdf_path = tmp_path / "receipt.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\nreceipt fixture")
    blocked_dir = tmp_path / ".config"
    blocked_dir.mkdir()
    blocked_path = blocked_dir / "token.pdf"
    blocked_path.write_bytes(b"%PDF-1.4\nsecret fixture")

    bundle = local_file_input_bundle_from_text(
        f"Read {pdf_path}, then {pdf_path}; also {blocked_path}"
    )

    assert [attachment.filename for attachment in bundle.attachments] == ["receipt.pdf"]
    assert len(bundle.diagnostics) == 1
    assert "token.pdf: ValueError" in bundle.diagnostics[0]
    assert "safety policy" in bundle.diagnostics[0]


def test_read_supported_local_file_rejects_unsafe_and_unsupported_paths(
    tmp_path: Path,
) -> None:
    unsupported = tmp_path / "receipt.txt"
    unsupported.write_text("receipt text", encoding="utf-8")
    blocked_dir = tmp_path / ".ssh"
    blocked_dir.mkdir()
    blocked_file = blocked_dir / "receipt.pdf"
    blocked_file.write_bytes(b"%PDF-1.4\nsecret fixture")

    with pytest.raises(ValueError, match="unsupported extension"):
        read_supported_local_file(unsupported)
    with pytest.raises(ValueError, match="safety policy"):
        read_supported_local_file(blocked_file)


def test_local_file_input_bundle_does_not_read_paths_from_structured_context(tmp_path) -> None:
    path = tmp_path / "context.pdf"
    path.write_bytes(b"%PDF-1.4\nsynthetic context")

    bundle = local_file_input_bundle_from_text({"external_document": str(path)})

    assert not bundle.has_inputs


def test_local_file_reader_rejects_symlink_to_blocked_target(tmp_path) -> None:
    blocked = tmp_path / ".ssh"
    blocked.mkdir()
    target = blocked / "synthetic.pdf"
    target.write_bytes(b"%PDF-1.4\nsynthetic marker")
    alias = tmp_path / "report.pdf"
    alias.symlink_to(target)

    with pytest.raises(ValueError, match="safety policy"):
        read_supported_local_file(alias)


def test_local_file_reader_rejects_extension_spoofing(tmp_path) -> None:
    path = tmp_path / "report.pdf"
    path.write_bytes(b"synthetic text that is not a PDF")

    with pytest.raises(ValueError, match="signature"):
        read_supported_local_file(path)


def test_local_file_reader_rejects_oversized_file_before_reading(tmp_path, monkeypatch) -> None:
    path = tmp_path / "large.pdf"
    path.write_bytes(b"%PDF-1.4\n" + b"x" * 64)
    monkeypatch.setattr(local_file_inputs, "MAX_LOCAL_INPUT_FILE_BYTES", 32)

    original_fdopen = local_file_inputs.os.fdopen

    class RejectContentRead:
        def __init__(self, *args, **kwargs):
            self.handle = original_fdopen(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, *_args):
            pytest.fail("oversized file content must not be loaded")

    monkeypatch.setattr(local_file_inputs.os, "fdopen", RejectContentRead)
    with pytest.raises(ValueError, match="limit"):
        read_supported_local_file(path)


@pytest.mark.parametrize("typed", [False, True])
def test_operator_input_attachments_ignore_other_context_fields(tmp_path, typed) -> None:
    requested = tmp_path / "requested.pdf"
    requested.write_bytes(b"%PDF-1.4\nrequested")
    unrelated = tmp_path / "unrelated.pdf"
    unrelated.write_bytes(b"%PDF-1.4\nunrelated")
    payload = {
        "raw_request": f"Inspect {requested}",
        "external_document": str(unrelated),
        "attachment_paths": [str(unrelated)],
    }
    value = SimpleNamespace(**payload) if typed else payload

    bundle = local_file_input_bundle_from_operator_input(value)

    assert [attachment.path for attachment in bundle.attachments] == [requested.resolve()]


def test_ingress_can_supply_an_explicit_attachment_independently_of_text(tmp_path) -> None:
    attachment = tmp_path / "selected report.pdf"
    attachment.write_bytes(b"%PDF-1.4\nselected")
    context = {"external_document": str(attachment)}

    assert not local_file_input_bundle_from_operator_input(context).has_inputs
    bundle = local_file_input_bundle_from_operator_input(
        context, attachment_paths=[attachment],
    )
    assert [item.path for item in bundle.attachments] == [attachment.resolve()]


def test_sdk_input_keeps_context_text_without_attaching_its_files(tmp_path) -> None:
    from keystone_agents.run import sdk_input_from_typed_input

    path = tmp_path / "context.pdf"
    path.write_bytes(b"%PDF-1.4\nsynthetic context")
    payload = {"raw_request": "Summarize the evidence.", "external_document": str(path)}

    result = sdk_input_from_typed_input(payload, live=True, provider="openai")

    assert isinstance(result, str)
    assert str(path) in result
    assert "file_data" not in result


def test_sdk_input_accepts_explicit_ingress_attachment_refs(tmp_path) -> None:
    from keystone_agents.run import sdk_input_from_typed_input

    path = tmp_path / "selected.pdf"
    path.write_bytes(b"%PDF-1.4\nsynthetic selected content")

    result = sdk_input_from_typed_input(
        {"raw_request": "Inspect the attachment."},
        live=True,
        provider="openai",
        trusted_attachment_paths=(path,),
    )

    assert isinstance(result, list)
    assert any(part.get("type") == "input_file" for part in result[0]["content"])


@pytest.mark.parametrize("replace_parent", [False, True])
def test_local_file_reader_rejects_symlink_replacement_after_validation(
    tmp_path, monkeypatch, replace_parent,
) -> None:
    parent = tmp_path / "inputs"
    parent.mkdir()
    requested = parent / "report.pdf"
    requested.write_bytes(b"%PDF-1.4\npermitted")
    forbidden = tmp_path / ".ssh"
    forbidden.mkdir()
    (forbidden / "report.pdf").write_bytes(b"%PDF-1.4\nforbidden synthetic content")
    original_read = local_file_inputs._read_local_file_bounded

    def replace_then_read(path):
        if replace_parent:
            parent.rename(tmp_path / "original-inputs")
            parent.symlink_to(forbidden, target_is_directory=True)
        else:
            requested.unlink()
            requested.symlink_to(forbidden / "report.pdf")
        return original_read(path)

    monkeypatch.setattr(local_file_inputs, "_read_local_file_bounded", replace_then_read)

    with pytest.raises(OSError):
        read_supported_local_file(requested)


@pytest.mark.parametrize(
    ("suffix", "header", "mime_type"),
    [
        (".pdf", b"%PDF-1.4\n", "application/pdf"),
        (".png", b"\x89PNG\r\n\x1a\n", "image/png"),
        (".jpg", b"\xff\xd8\xff", "image/jpeg"),
        (".jpeg", b"\xff\xd8\xff", "image/jpeg"),
        (".gif", b"GIF89a", "image/gif"),
        (".webp", b"RIFFxxxxWEBP", "image/webp"),
    ],
)
def test_supported_signature_retains_exact_bytes(tmp_path, suffix, header, mime_type) -> None:
    path = tmp_path / f"selected{suffix}"
    path.write_bytes(header)

    result = read_supported_local_file(path)

    assert result.data == header
    assert result.mime_type == mime_type
