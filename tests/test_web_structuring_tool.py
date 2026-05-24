from __future__ import annotations

from keystone_agents.tools.web_structuring_tool import structure_web_data_for_schema_impl


def test_structure_web_data_for_schema_maps_json_records() -> None:
    result = structure_web_data_for_schema_impl(
        target_schema_json=(
            '{"name":"company_watch","fields":['
            '{"name":"company","aliases":["Company Name"],"required":true},'
            '{"name":"amount","aliases":["Funding"],"value_type":"currency"}]}'
        ),
        source_json='[{"Company Name":"Acme Health","Funding":"$10M","Other":"ignored"}]',
        source_id="source:1",
        source_url="https://example.com",
    )

    assert result.status == "success"
    assert result.source_type == "structured_json"
    assert result.records[0].fields == {"company": "Acme Health", "amount": "$10M"}
    assert result.records[0].source_url == "https://example.com"
    assert result.unmapped_fields == ["Other"]
    assert result.send_enabled is False


def test_structure_web_data_for_schema_handles_unstructured_text_with_issues() -> None:
    result = structure_web_data_for_schema_impl(
        target_schema_json=(
            '{"fields":['
            '{"name":"company","aliases":["Company"],"required":true},'
            '{"name":"location","required":true}]}'
        ),
        source_text="Company: Acme Health\nNotes: behavioral health pilot",
        source_id="source:text",
    )

    assert result.status == "partial"
    assert result.source_type == "text"
    assert result.records[0].fields == {"company": "Acme Health"}
    assert "Missing required field: location" in result.records[0].issues
