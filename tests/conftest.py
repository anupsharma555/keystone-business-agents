from __future__ import annotations

import os
import socket
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

NETWORK_BLOCKED_MESSAGE = "Network calls are disabled during pytest; mock live integrations."
LOCAL_HOSTS = {"", "0.0.0.0", "127.0.0.1", "::1", "localhost", None}

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("KEYSTONE_TEST_MODE", "1")
_TEST_STATE_DIRECTORY = tempfile.TemporaryDirectory(prefix="kba-pytest-state-")
_TEST_DATABASE_PATH = Path(_TEST_STATE_DIRECTORY.name) / "keystone_agents.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DATABASE_PATH}"

LIVE_ENV_KEYS = (
    "KEYSTONE_LIVE_MODE",
    "KEYSTONE_ENABLE_LIVE_GMAIL",
    "KEYSTONE_ENABLE_LIVE_SLACK",
    "KEYSTONE_ENABLE_LIVE_RESEARCH",
    "KEYSTONE_ENABLE_LIVE_CRM",
    "KEYSTONE_DRY_RUN",
    "KEYSTONE_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "SERPER_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_WEBHOOK_URL",
    "APIFY_API_TOKEN",
    "BROWSERLESS_API_KEY",
    "FIRECRAWL_API_KEY",
    "FIRECRAWL_BASE_URL",
    "KEYSTONE_TAVILY_MONTHLY_CREDIT_LIMIT",
    "KEYSTONE_TAVILY_MONTHLY_SOFT_LIMIT",
    "KEYSTONE_TAVILY_CREDIT_ENFORCEMENT",
    "KEYSTONE_TAVILY_USAGE_PATH",
    "KEYSTONE_ENABLE_WEBSITE_EXTRACTION",
    "KEYSTONE_WEBSITE_EXTRACTOR",
    "KEYSTONE_WEBSITE_EXTRACTOR_FALLBACK",
    "KEYSTONE_FIRECRAWL_EXTRACTION_MAX_CALLS_PER_RUN",
    "KEYSTONE_WEBSITE_EXTRACTION_MAX_PAGES",
)


@pytest.fixture
def require_local_evidence() -> Callable[[str | Path], Path]:
    """Return a local-only evidence path or skip in clean clones such as CI."""

    def require(path_value: str | Path) -> Path:
        path = Path(path_value)
        if not path.is_file():
            pytest.skip(f"local-only operational evidence is unavailable: {path}")
        return path

    return require


def _host_value(value: object) -> object:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return value


def _is_local_host(value: object) -> bool:
    return _host_value(value) in LOCAL_HOSTS


@pytest.fixture(autouse=True)
def disable_dotenv_loading(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep local `.env` credentials out of tests unless a test opts in explicitly."""

    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("KEYSTONE_TEST_MODE", "1")


@pytest.fixture(autouse=True)
def isolate_live_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep operator live-mode environment out of unit tests and subprocess CLIs."""

    for key in LIVE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("KEYSTONE_DRY_RUN", "true")


@pytest.fixture(autouse=True)
def block_live_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail fast if a test accidentally reaches an external network path."""

    original_getaddrinfo: Callable[..., Any] = socket.getaddrinfo
    original_connect = socket.socket.connect

    def guarded_getaddrinfo(
        host: str | bytes | None,
        port: str | int | None,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> list[tuple[Any, ...]]:
        if _is_local_host(host):
            return original_getaddrinfo(host, port, family, type, proto, flags)
        raise AssertionError(NETWORK_BLOCKED_MESSAGE)

    def guarded_connect(self: socket.socket, address: object) -> object:
        host = address[0] if isinstance(address, tuple) and address else address
        if _is_local_host(host):
            return original_connect(self, address)
        raise AssertionError(NETWORK_BLOCKED_MESSAGE)

    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)


@pytest.fixture
def fake_supplied_gmail_sdk(monkeypatch: pytest.MonkeyPatch):
    """Run the real supplied-message SDK boundary with scripted graph-control output."""
    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.usage import Usage
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText

    from keystone_agents import workflow_runner
    from keystone_agents.agents.gmail_triage import EmailFixture, triage_email_fixture
    from keystone_agents.sdk import build_local_run_config

    actual = workflow_runner.run_gmail_triage_sdk
    calls = []

    class ScriptedModel(Model):
        def __init__(self, output):
            self.output = output

        async def get_response(self, *args, **kwargs):
            return ModelResponse(
                output=[ResponseOutputMessage(
                    id="synthetic-gmail-output", type="message", role="assistant",
                    status="completed", content=[ResponseOutputText(
                        type="output_text", text=self.output.model_dump_json(), annotations=[],
                    )],
                )],
                usage=Usage(requests=1, input_tokens=100, output_tokens=100, total_tokens=200),
                response_id="synthetic-gmail-response",
            )

        def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    class Provider(ModelProvider):
        def __init__(self, output):
            self.model = ScriptedModel(output)

        def get_model(self, model_name):
            return self.model

    def invoke(typed_input, **kwargs):
        assert kwargs["attach_tools"] is False
        assert kwargs["provider_selection_required"] is False
        assert kwargs["provider_context_read_required"] is False
        calls.append(typed_input)
        output = triage_email_fixture(EmailFixture(
            subject=typed_input.subject, body=typed_input.body,
            sender_name=typed_input.sender_name, sender_email=typed_input.sender_email,
            message_id=typed_input.message_id, thread_id=typed_input.thread_id,
        )).model_copy(update={
            "reasoning": "Scripted SDK output for graph wiring; not live model-quality evidence.",
        })
        return actual(typed_input, **{
            **kwargs, "run_config": build_local_run_config(Provider(output)),
        })

    monkeypatch.setattr(workflow_runner, "run_gmail_triage_sdk", invoke)
    return calls


@pytest.fixture
def supplied_research_fake_sdk(monkeypatch):
    """Explicit native-SDK control for supplied Research, never a live quality claim."""
    import json

    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.usage import Usage
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText

    from keystone_agents import workflow_runner
    from keystone_agents.agents.business_research_analyst import (
        run_business_research_analyst_research_brief_sdk,
    )
    from keystone_agents.schemas.research import ResearchBrief
    from keystone_agents.sdk import build_local_run_config

    class ResearchCalls(list):
        source_predicate = None

    calls = ResearchCalls()
    actual = run_business_research_analyst_research_brief_sdk

    class ScriptedModel(Model):
        def __init__(self, typed_input):
            self.typed_input = typed_input

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            packet = json.loads(self.typed_input.source_context)
            sources = packet["source_catalog"]
            serialized = json.dumps(input)
            assert not tools and not handoffs
            assert self.typed_input.research_goal in serialized
            assert all(source["source_id"] in serialized for source in sources)
            calls.append({"typed_input": self.typed_input, "model_input": serialized})
            selected_sources = [source for source in sources
                                if calls.source_predicate is None or calls.source_predicate(source)]
            assert selected_sources, (
                "The positive Research control needs substantive source evidence."
            )
            ids = [source["source_id"] for source in selected_sources]
            output = ResearchBrief(
                target_name=self.typed_input.target_name, target_type=self.typed_input.target_type,
                summary=(f"Scripted research interpretation for {self.typed_input.target_name}: "
                         "prioritize a bounded validation review before making a commitment."),
                facts=[{"text": "The supplied material describes the proposed workflow.",
                        "source_ids": ids}],
                limitations=["This scripted result proves SDK wiring, not model quality."],
                source_ids_used=ids, sources=selected_sources,
                decision={
                    "decision_owner": "specialist_agent",
                    "decision_stage": "research_source_selection",
                    "selected_candidate_ids": ids, "needs_more_context": False,
                    "reasoning": "Use the supplied evidence only.",
                    "candidate_assessments": [{
                        "candidate_id": source["source_id"],
                        "disposition": "selected" if source["source_id"] in ids else "excluded",
                        "rationale": "Substantive evidence." if source["source_id"] in ids
                        else "Structural context is not factual company evidence.",
                    } for source in sources],
                },
            )
            return ModelResponse(
                output=[ResponseOutputMessage(
                    id=f"research-scripted-{len(calls)}", type="message", role="assistant",
                    status="completed", content=[ResponseOutputText(
                        type="output_text", text=output.model_dump_json(), annotations=[],
                    )],
                )], usage=Usage(requests=1, input_tokens=30, output_tokens=20),
                response_id=f"research-scripted-response-{len(calls)}",
            )

        def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    class Provider(ModelProvider):
        def __init__(self, model):
            self.model = model

        def get_model(self, model_name):
            return self.model

    def invoke(typed_input, **kwargs):
        assert kwargs["attach_tools"] is False
        assert kwargs["provider_retrieval_required"] is False
        return actual(typed_input, **{
            **kwargs, "run_config": build_local_run_config(Provider(ScriptedModel(typed_input))),
        })

    monkeypatch.setattr(workflow_runner, "run_business_research_analyst_research_brief_sdk", invoke)
    return calls


@pytest.fixture
def supplied_opportunity_fake_sdk(monkeypatch):
    """Explicit real-SDK graph-wiring control, not live Opportunity quality evidence."""
    import json
    from types import SimpleNamespace

    from agents.models.interface import Model, ModelProvider, ModelResponse
    from agents.usage import Usage
    from openai.types.responses import ResponseOutputMessage, ResponseOutputText

    from keystone_agents import workflow_runner
    from keystone_agents.schemas.opportunity import OpportunityScoutResult
    from keystone_agents.sdk import build_local_run_config

    actual = workflow_runner.run_opportunity_scout_sdk
    control = SimpleNamespace(calls=[], subject="", count=None)

    class ScriptedModel(Model):
        def __init__(self, typed_input):
            self.typed_input = typed_input

        async def get_response(self, system_instructions, input, model_settings, tools,
                               output_schema, handoffs, tracing, **kwargs):
            assert tools == [] and handoffs == []
            packet = json.loads(self.typed_input.context)
            sources = packet["supplied_sources"]
            source = next(source for source in sources if source["evidence_excerpt"])
            identity = source["provider_candidate_id"]
            subject = control.subject or packet["context_pack"]["target"]["name"]
            count = control.count or self.typed_input.max_results
            serialized = input if isinstance(input, str) else json.dumps(input)
            assert all(value["provider_candidate_id"] in serialized for value in sources)
            control.calls.append({"typed_input": self.typed_input, "model_input": serialized})
            records = [{
                "company_name": subject, "opportunity_type": "clinical AI",
                "opportunity_kind": "other", "opportunity_status": "unknown",
                "detail_verification_status": "unverified", "priority_score": 50,
                "outside_consulting_likelihood": 50,
                "why_now_signal": source["evidence_excerpt"],
                "keystone_fit_reason": (
                    "The supplied material supports a proposed review direction."
                ),
                "recommended_next_step": (
                    f"Review proposed direction {index + 1} before any external action."
                ),
                "missing_evidence": ["This proposed direction and its outcome remain unverified."],
                "sources": [{
                    "source_id": source["source_id"], "provider_candidate_id": identity,
                    "title": source["title"], "url": source["url"], "source_type": "unknown",
                    "supported_signal": source["evidence_excerpt"],
                }],
                "source_signals": [source["evidence_excerpt"]],
                "handoff_to_business_research_analyst": False,
            } for index in range(count)]
            output = OpportunityScoutResult.model_validate({
                "topic": subject, "search_provider": "source-provided", "dry_run": True,
                "human_summary": (
                    f"Model assessed {subject} and proposed {count} review-only directions "
                    f"from supplied evidence. Source: {source['url']}"
                ),
                "records": records,
                "decision": {
                    "decision_owner": "specialist_agent",
                    "decision_stage": "opportunity_candidate_selection",
                    "selected_candidate_ids": [identity], "needs_more_context": False,
                    "reasoning": "A scripted model selected a supplied evidence reference.",
                    "limitations": ["This proves SDK wiring, not live reasoning quality."],
                    "candidate_assessments": [{
                        "candidate_id": value["provider_candidate_id"],
                        "disposition": (
                            "selected" if value["provider_candidate_id"] == identity else "excluded"
                        ),
                        "rationale": "Scripted selection for this orchestration control.",
                    } for value in sources],
                },
            })
            return ModelResponse(
                output=[ResponseOutputMessage(
                    id=f"opportunity-scripted-{len(control.calls)}",
                    type="message", role="assistant",
                    status="completed", content=[ResponseOutputText(
                        type="output_text", text=output.model_dump_json(), annotations=[],
                    )],
                )], usage=Usage(requests=1, input_tokens=30, output_tokens=20, total_tokens=50),
                response_id=f"opportunity-scripted-response-{len(control.calls)}",
            )

        def stream_response(self, *args, **kwargs):
            raise NotImplementedError

    class Provider(ModelProvider):
        def __init__(self, model):
            self.model = model

        def get_model(self, model_name):
            return self.model

    def invoke(typed_input, **kwargs):
        assert kwargs["attach_tools"] is False
        assert kwargs["provider_retrieval_required"] is False
        return actual(typed_input, **{
            **kwargs, "run_config": build_local_run_config(Provider(ScriptedModel(typed_input))),
        })

    monkeypatch.setattr(workflow_runner, "run_opportunity_scout_sdk", invoke)
    return control
