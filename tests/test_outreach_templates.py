from __future__ import annotations

import json

import pytest

from keystone_agents.outreach_templates import (
    OutreachTemplateNotFoundError,
    list_outreach_templates,
    load_outreach_template,
    load_outreach_template_context,
    template_prompt_json,
)
from keystone_agents.schemas.outreach import OutreachTemplateContext
from keystone_agents.schemas.outreach_template import OutreachTemplateRecord
from keystone_agents.tools.outreach_template_tool import (
    list_outreach_templates as list_outreach_templates_tool,
)
from keystone_agents.tools.outreach_template_tool import (
    load_outreach_template as load_outreach_template_tool,
)


def test_outreach_template_schema_validation() -> None:
    template = load_outreach_template("low_pressure_intro")

    assert isinstance(template, OutreachTemplateRecord)
    assert template.template_id == "low_pressure_intro"
    assert template.approval_scope == "external_use"
    assert template.send_enabled is False
    assert template.required_context
    assert template.structure_blocks
    assert "\u2014" not in template_prompt_json(template)
    safety = " ".join(template.safety_constraints).lower()
    for required in ("approval", "send", "source", "phi", "unsupported", "em dash"):
        assert required in safety


def test_outreach_template_loading_and_context_conversion() -> None:
    templates = list_outreach_templates()
    context = load_outreach_template_context("research_workflow_intro")

    assert {template.template_id for template in templates} >= {
        "low_pressure_intro",
        "research_workflow_intro",
    }
    assert isinstance(context, OutreachTemplateContext)
    assert context.template_id == "research_workflow_intro"
    assert context.template_version == "v1"
    assert context.approved is True
    assert context.provides_factual_claims is False
    assert context.send_enabled is False
    assert "clinical research workflow outreach" in context.fit_reason


def test_unknown_outreach_template_id_raises_clear_error() -> None:
    with pytest.raises(OutreachTemplateNotFoundError) as exc:
        load_outreach_template("missing_template")

    assert "Unknown outreach template" in str(exc.value)
    assert "low_pressure_intro@v1" in str(exc.value)


def test_outreach_template_tools_return_safe_json() -> None:
    listing = json.loads(list_outreach_templates_tool())
    loaded = json.loads(load_outreach_template_tool("low_pressure_intro"))

    assert listing["send_enabled"] is False
    assert loaded["send_enabled"] is False
    assert loaded["drafting_context"]["template_id"] == "low_pressure_intro"
    assert loaded["drafting_context"]["provides_factual_claims"] is False
