from __future__ import annotations

from keystone_agents.agent_mentions import parse_agent_mention


def test_parse_kni_orchestrator_mention() -> None:
    mention = parse_agent_mention("@KNI orchestrator agent route this request")

    assert mention.explicit is True
    assert mention.route == "orchestrator"
    assert mention.agent_name == "Keystone Orchestrator Agent"
    assert mention.input_text == "route this request"


def test_parse_kni_business_agent_analyst_alias() -> None:
    mention = parse_agent_mention("@KNI business agent analyst research Lindus Health")

    assert mention.explicit is True
    assert mention.route == "business_research_analyst"
    assert mention.agent_name == "Business Research Analyst"
    assert mention.input_text == "research Lindus Health"


def test_parse_kni_keystone_ask_alias() -> None:
    mention = parse_agent_mention("@KNI keystone ask business research analyst research Lindus")

    assert mention.explicit is True
    assert mention.route == "business_research_analyst"
    assert mention.input_text == "research Lindus"


def test_parse_kni_slack_user_mention() -> None:
    mention = parse_agent_mention("<@U123> opportunity scout find psychiatry AI roles")

    assert mention.route == "opportunity_scout"
    assert mention.input_text == "find psychiatry AI roles"


def test_parse_kni_chief_of_staff_alias() -> None:
    mention = parse_agent_mention("@KNI chief of staff route calendar update to #meetings")

    assert mention.route == "chief_of_staff"
    assert mention.agent_name == "KNI Chief of Staff Agent"
    assert mention.input_text == "route calendar update to #meetings"


def test_parse_kni_context_agent_aliases() -> None:
    airtable = parse_agent_mention("@KNI airtable context agent inspect tracker schema")
    workspace = parse_agent_mention("@KNI google workspace context plan eval artifact")
    zotero = parse_agent_mention("@KNI zotero context agent map collection criteria")

    assert airtable.route == "airtable_context_agent"
    assert airtable.agent_name == "Airtable Context Agent"
    assert airtable.input_text == "inspect tracker schema"
    assert workspace.route == "google_workspace_context_agent"
    assert workspace.agent_name == "Google Workspace Context Agent"
    assert workspace.input_text == "plan eval artifact"
    assert zotero.route == "zotero_context_agent"
    assert zotero.agent_name == "Zotero Context Agent"
    assert zotero.input_text == "map collection criteria"


def test_parse_without_mention_defaults_to_orchestrator_context() -> None:
    mention = parse_agent_mention("research Curebase")

    assert mention.explicit is False
    assert mention.route is None
    assert mention.input_text == "research Curebase"
