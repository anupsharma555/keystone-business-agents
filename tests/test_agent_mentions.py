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


def test_parse_kni_slack_user_mention() -> None:
    mention = parse_agent_mention("<@U123> opportunity scout find psychiatry AI roles")

    assert mention.route == "opportunity_scout"
    assert mention.input_text == "find psychiatry AI roles"


def test_parse_without_mention_defaults_to_orchestrator_context() -> None:
    mention = parse_agent_mention("research Curebase")

    assert mention.explicit is False
    assert mention.route is None
    assert mention.input_text == "research Curebase"
