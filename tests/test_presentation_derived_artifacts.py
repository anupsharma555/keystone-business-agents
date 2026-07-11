from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from keystone_agents.tools import internal_data_tools
from keystone_agents.tools.internal_data_tools import (
    presentation_delete_test_artifact_local_impl,
    presentation_extract_slide_copy_local_impl,
)


def _write_test_deck(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            '<p:sld xmlns:p="p" xmlns:a="a"><a:t>Clinical AI</a:t></p:sld>',
        )


def _fake_renderer(command: list[str], *, timeout: int) -> None:
    assert timeout > 0
    if "--convert-to" in command:
        assert any(value.startswith("-env:UserInstallation=file://") for value in command)
        output_dir = Path(command[command.index("--outdir") + 1])
        source = Path(command[-1])
        (output_dir / f"{source.stem}.pdf").write_bytes(b"%PDF-deck")
        return
    if Path(command[0]).name == "pdfseparate":
        page = command[command.index("-f") + 1]
        Path(command[-1].replace("%d", page)).write_bytes(b"%PDF-page")
        return
    if Path(command[0]).name == "pdftoppm":
        Path(command[-1] + ".png").write_bytes(b"\x89PNG\r\n\x1a\nrendered")
        return
    raise AssertionError(command)


@pytest.mark.parametrize("output_format", ["png", "pdf"])
def test_derived_slide_copy_verifies_parent_and_reversible_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output_format: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    library = tmp_path / "library"
    source = library / "deck.pptx"
    _write_test_deck(source)
    source_before = source.read_bytes()
    monkeypatch.setenv("KEYSTONE_PRESENTATION_LIBRARY_ROOT", str(library))
    monkeypatch.setenv("KEYSTONE_PRESENTATION_ALLOW_DERIVED_WRITES", "true")
    monkeypatch.setenv(
        "KEYSTONE_PRESENTATION_DERIVED_ROOT", "artifacts/presentation-derived"
    )
    monkeypatch.setattr(internal_data_tools, "_run_bounded_command", _fake_renderer)
    monkeypatch.setattr(
        internal_data_tools.shutil,
        "which",
        lambda name: f"/tools/{name}",
    )

    created = presentation_extract_slide_copy_local_impl(
        "deck.pptx",
        1,
        output_format=output_format,
        output_name=f"KBA_TEST_SLIDE-derived.{output_format}",
        approval_reference="approved:create",
        live=True,
    )
    deleted = presentation_delete_test_artifact_local_impl(
        str(created["artifact_path"]),
        approval_reference="approved:delete",
        live=True,
    )

    assert created["status"] == "success"
    assert created["verification"]["parent_hash_match"] is True
    assert created["verification"]["parent_mtime_match"] is True
    assert created["verification"]["file_signature_valid"] is True
    assert source.read_bytes() == source_before
    assert deleted["status"] == "success"
    assert deleted["verification"]["artifact_absent_after"] is True


def test_derived_slide_copy_dry_run_and_cleanup_marker_guard() -> None:
    preview = presentation_extract_slide_copy_local_impl(
        "deck.pptx",
        2,
        output_name="KBA_TEST_SLIDE-preview",
    )

    assert preview["status"] == "dry-run"
    assert preview["derived_copy_created"] is False
    assert preview["parent_modified"] is False
    with pytest.raises(ValueError, match="KBA_TEST_SLIDE"):
        presentation_delete_test_artifact_local_impl("artifacts/ordinary.png")
