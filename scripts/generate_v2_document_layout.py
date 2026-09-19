"""Regenerate the synthetic V2 table page; Pillow is a development-only dependency."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def generate(destination: Path) -> None:
    from PIL import Image, ImageDraw, ImageFont, PngImagePlugin, __version__

    destination.mkdir(parents=True, exist_ok=True)
    gold = {
        "document_id": "synthetic-validation-table-v1",
        "page": 1,
        "title": "Synthetic validation summary",
        "columns": ["Evaluation", "Performed?", "Result"],
        "rows": [
            ["Retrospective development", "Yes", "AUROC 0.91"],
            ["External retrospective holdout", "Yes", "AUROC 0.88"],
            ["Prospective clinical outcomes", "No", "Not measured"],
        ],
        "footer": "Synthetic fixture only. No real participants, organizations, or patient data.",
    }
    canonical_gold = json.dumps(gold, sort_keys=True, separators=(",", ":"))
    gold_hash = hashlib.sha256(canonical_gold.encode()).hexdigest()
    extracted = (
        "Synthetic validation summary\n"
        "Evaluation | Performed? | Result\n"
        "Retrospective development | Yes | AUROC 0.91\n"
        "External retrospective holdout | Yes | AUROC 0.88\n"
        "Prospective clinical outcomes | [cell not extracted] | [cell not extracted]\n"
        "Synthetic fixture only.\n"
    )
    image = Image.new("RGB", (1500, 660), "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.load_default(size=42)
    font = ImageFont.load_default(size=27)
    small_font = ImageFont.load_default(size=20)
    draw.text((70, 48), gold["title"], fill="#122a43", font=title_font)
    draw.text(
        (70, 107),
        "Document: synthetic-validation-table-v1 | Page 1",
        fill="#42566a",
        font=small_font,
    )
    columns = [70, 690, 990, 1430]
    top = 165
    row_height = 95
    for row_index, row in enumerate([gold["columns"], *gold["rows"]]):
        y = top + row_index * row_height
        draw.rectangle(
            (columns[0], y, columns[-1], y + row_height),
            fill="#e5edf5" if row_index == 0 else "#f7f9fb" if row_index % 2 == 0 else "white",
            outline="#60788e",
            width=2,
        )
        for column_index, value in enumerate(row):
            draw.text((columns[column_index] + 18, y + 32), value, fill="#122a43", font=font)
        for x in columns[1:-1]:
            draw.line((x, y, x, y + row_height), fill="#60788e", width=2)
    draw.text((70, 590), gold["footer"], fill="#42566a", font=small_font)
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("DocumentID", gold["document_id"])
    metadata.add_text("PageNumber", "1")
    metadata.add_text("GoldSHA256", gold_hash)
    png = destination / "v2_document_layout.png"
    image.save(png, pnginfo=metadata, optimize=False)
    (destination / "v2_document_layout.txt").write_text(extracted, encoding="utf-8")
    manifest = {
        "schema": "keystone.v2_document_layout_fixture.v1",
        "fixture_id": "v2_document_layout",
        "source_id": "layout-page-1",
        "gold": gold,
        "gold_sha256": gold_hash,
        "page_png": png.name,
        "page_png_sha256": hashlib.sha256(png.read_bytes()).hexdigest(),
        "page_width": 1500,
        "page_height": 660,
        "extracted_text": extracted,
        "extracted_text_sha256": hashlib.sha256(extracted.encode()).hexdigest(),
        "controlled_omission": {"row": 3, "columns": [2, 3], "values": ["No", "Not measured"]},
        "generation": {
            "script": "scripts/generate_v2_document_layout.py",
            "pillow_version": __version__,
        },
        "proof_boundary": (
            "Deliberately incomplete text extraction; not output from a real OCR provider."
        ),
    }
    (destination / "v2_document_layout.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    generate(Path(__file__).resolve().parents[1] / "evals/fixtures")
