"""Repo-backed outreach template library."""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from keystone_agents.schemas.outreach import OutreachTemplateContext
from keystone_agents.schemas.outreach_template import OutreachTemplateRecord

TEMPLATE_ROOT = Path(__file__).resolve().parent / "templates" / "outreach"


class OutreachTemplateNotFoundError(KeyError):
    """Raised when a requested outreach template is not present in the repo library."""


def _template_paths(template_root: Path = TEMPLATE_ROOT) -> Iterable[Path]:
    if not template_root.exists():
        return ()
    return sorted(template_root.glob("*.json"))


def _load_template_file(path: Path) -> OutreachTemplateRecord:
    return OutreachTemplateRecord.model_validate_json(path.read_text(encoding="utf-8"))


def list_outreach_templates(
    *,
    channel: str | None = None,
    use_case: str | None = None,
    stage: str | None = None,
    template_root: Path = TEMPLATE_ROOT,
) -> list[OutreachTemplateRecord]:
    """List schema-validated outreach templates from canonical repo files."""

    records = [_load_template_file(path) for path in _template_paths(template_root)]
    if channel:
        records = [record for record in records if record.channel == channel]
    if use_case:
        records = [record for record in records if record.use_case == use_case]
    if stage:
        records = [record for record in records if record.stage == stage]
    return sorted(records, key=lambda record: (record.template_id, record.version))


def load_outreach_template(
    template_id: str,
    *,
    version: str | None = None,
    template_root: Path = TEMPLATE_ROOT,
) -> OutreachTemplateRecord:
    """Load one schema-validated outreach template from canonical repo files."""

    requested_id = str(template_id or "").strip()
    requested_version = str(version or "").strip()
    matches = [
        record
        for record in list_outreach_templates(template_root=template_root)
        if record.template_id == requested_id
        and (not requested_version or record.version == requested_version)
    ]
    if matches:
        return sorted(matches, key=lambda record: record.version)[-1]

    available = ", ".join(
        f"{record.template_id}@{record.version}"
        for record in list_outreach_templates(template_root=template_root)
    )
    suffix = f" Available templates: {available}." if available else " No templates found."
    raise OutreachTemplateNotFoundError(f"Unknown outreach template '{requested_id}'." + suffix)


def outreach_template_to_context(record: OutreachTemplateRecord) -> OutreachTemplateContext:
    """Convert a canonical template record into prompt-safe drafting context."""

    structure_guidance = [f"{block.label}: {block.guidance}" for block in record.structure_blocks]
    fit_reason = (
        "Good for " + "; ".join(record.good_for)
        if record.good_for
        else "Selected as approved outreach structure guidance."
    )
    return OutreachTemplateContext(
        template_id=record.template_id,
        template_version=record.version,
        name=record.name,
        stage=record.stage,
        tone_guidance=record.tone_constraints,
        structure_guidance=structure_guidance,
        pacing_guidance=f"Keep draft copy under {record.max_words} words.",
        cta_guidance=record.cta_style,
        follow_up_pattern=(
            "No automated follow-up. Any follow-up is a data-only recommendation "
            "requiring human approval."
        ),
        fit_reason=fit_reason,
        approved=True,
        provides_factual_claims=False,
        send_enabled=False,
    )


def load_outreach_template_context(
    template_id: str,
    *,
    version: str | None = None,
    template_root: Path = TEMPLATE_ROOT,
) -> OutreachTemplateContext:
    """Load one template as prompt-safe drafting context."""

    return outreach_template_to_context(
        load_outreach_template(template_id, version=version, template_root=template_root)
    )


def list_outreach_template_contexts(
    *,
    channel: str | None = None,
    use_case: str | None = None,
    stage: str | None = None,
    template_root: Path = TEMPLATE_ROOT,
) -> list[OutreachTemplateContext]:
    """List prompt-safe drafting contexts for matching templates."""

    return [
        outreach_template_to_context(record)
        for record in list_outreach_templates(
            channel=channel,
            use_case=use_case,
            stage=stage,
            template_root=template_root,
        )
    ]


def template_prompt_json(record: OutreachTemplateRecord | OutreachTemplateContext) -> str:
    """Return stable JSON for SDK prompt context."""

    if isinstance(record, OutreachTemplateRecord):
        payload: dict[str, Any] = record.prompt_context()
    else:
        payload = record.model_dump(mode="json")
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


__all__ = [
    "TEMPLATE_ROOT",
    "OutreachTemplateNotFoundError",
    "list_outreach_templates",
    "load_outreach_template",
    "outreach_template_to_context",
    "load_outreach_template_context",
    "list_outreach_template_contexts",
    "template_prompt_json",
]
