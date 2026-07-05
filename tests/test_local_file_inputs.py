from __future__ import annotations

from pathlib import Path

import pytest

from keystone_agents.local_file_inputs import (
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
