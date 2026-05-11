"""Tool wrappers for repo-backed outreach templates."""

from __future__ import annotations

import json

from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.outreach_templates import (
    list_outreach_templates as list_outreach_template_records,
)
from keystone_agents.outreach_templates import (
    load_outreach_template as load_outreach_template_record,
)
from keystone_agents.outreach_templates import outreach_template_to_context
from keystone_agents.sdk import function_tool


def _json_payload(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


@function_tool(**keystone_tool_guardrail_kwargs())
def list_outreach_templates(
    channel: str | None = None,
    use_case: str | None = None,
    stage: str | None = None,
) -> str:
    """List approved repo-backed outreach templates without network or storage calls."""

    records = list_outreach_template_records(
        channel=channel,
        use_case=use_case,
        stage=stage,
    )
    return _json_payload(
        {
            "mode": "repo_outreach_templates",
            "object_type": "outreach_template",
            "approved_only": True,
            "send_enabled": False,
            "templates": [record.prompt_context() for record in records],
        }
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def load_outreach_template(template_id: str, version: str | None = None) -> str:
    """Load one approved repo-backed outreach template as advisory drafting context."""

    record = load_outreach_template_record(template_id, version=version)
    context = outreach_template_to_context(record)
    return _json_payload(
        {
            "mode": "repo_outreach_template",
            "object_type": "outreach_template",
            "approved_only": True,
            "send_enabled": False,
            "template": record.prompt_context(),
            "drafting_context": context.model_dump(mode="json"),
        }
    )


__all__ = ["list_outreach_templates", "load_outreach_template"]
