"""Isolated V2 mechanics and optional tool-free model comparisons.

Scripted controls prove the harness, not a model-quality improvement. Live
variants remain serial, budgeted, and separate from production agent routing.
"""

from __future__ import annotations

import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter
from typing import Any, TypedDict
from uuid import uuid4

from pydantic import ValidationError

from keystone_agents.guardrails import keystone_guardrails
from keystone_agents.local_file_inputs import read_supported_local_file
from keystone_agents.model_provider import get_runtime_agent_model_config
from keystone_agents.operator_failures import redact_operator_text
from keystone_agents.run import (
    run_typed_sdk_agent,
    sdk_input_from_typed_input,
    sdk_run_failure_metadata,
)
from keystone_agents.runtime.durable_execution import ExecutionConflict
from keystone_agents.runtime.provenance import current_runtime_fingerprint
from keystone_agents.runtime.request_budget import (
    ModelRequestBudgetExhausted,
    activate_model_request_budget,
    current_model_request_budget,
)
from keystone_agents.schemas.experiment_v2 import (
    ExperimentAnswer,
    ExperimentCase,
    ExperimentCheck,
    ExperimentDefinition,
    ExperimentObservation,
    ExperimentReport,
    ExperimentReview,
)
from keystone_agents.sdk import (
    build_model_settings,
    build_sdk_agent,
    compose_direct_instructions,
    load_prompt,
)
from keystone_agents.storage.sqlite_store import redact_secrets

CATALOG_PATH = Path(__file__).resolve().parents[3] / "evals/static/v2_experiments.json"
LAYOUT_FIXTURE_DIRECTORY = CATALOG_PATH.parent.parent / "fixtures"


class ExperimentContractError(ValueError):
    """A safe diagnostic code for a violated experiment contract."""


def _validation_diagnostics(error: ValidationError, output_type: Any) -> list[dict]:
    schema = output_type.model_json_schema()
    allowed_fields = set(schema.get("properties", {}))
    for definition in schema.get("$defs", {}).values():
        allowed_fields.update(definition.get("properties", {}))
    return [
        {
            "type": item["type"],
            "location": [
                part if isinstance(part, int) or part in allowed_fields else "<unknown_field>"
                for part in item["loc"][:8]
            ],
            "message": redact_operator_text(item["msg"], max_chars=400),
        }
        for item in error.errors(include_input=False, include_context=False, include_url=False)[:12]
    ]


def _diagnostic_output_schema(output_type: Any, failures: list[dict]):
    from agents.agent_output import AgentOutputSchema
    from agents.exceptions import ModelBehaviorError

    class DiagnosticOutputSchema(AgentOutputSchema):
        def validate_json(self, json_str: str) -> Any:
            try:
                return super().validate_json(json_str)
            except ModelBehaviorError:
                # SDK privacy defaults remove payload-bearing validation causes.
                # Diagnose the same schema locally, without changing acceptance or
                # logging settings and without retaining model JSON/input/context.
                try:
                    output_type.model_validate_json(json_str, strict=True)
                except ValidationError as error:
                    if len(failures) < 4:
                        failures.append({
                            "attempt": len(failures) + 1,
                            "output_type": output_type.__name__,
                            "errors": _validation_diagnostics(error, output_type),
                        })
                json_str = "<redacted>"
                raise

    return DiagnosticOutputSchema(output_type)


def _bounded_failure_metadata(exc: BaseException) -> dict:
    metadata = sdk_run_failure_metadata(exc)

    def bounded(value: Any, depth: int = 0) -> Any:
        if depth > 5:
            return "<omitted>"
        if isinstance(value, dict):
            return {
                str(key)[:80]: bounded(item, depth + 1)
                for key, item in list(value.items())[:40]
            }
        if isinstance(value, list):
            return [bounded(item, depth + 1) for item in value[:12]]
        if isinstance(value, str):
            return redact_operator_text(value, max_chars=400)
        return value if value is None or isinstance(value, (int, float, bool)) else "<omitted>"

    # Exclude tool receipts, provider identities, raw results and arbitrary telemetry.
    selected = {
        key: metadata[key]
        for key in (
            "schema", "agent_name", "provider", "model", "run_mode", "failure_kind",
            "attempt_count", "usage", "cost",
        )
        if key in metadata
    }
    cache = metadata.get("request_cache", {})
    selected["request_cache"] = {
        key: cache[key]
        for key in (
            "failed_model_attempts", "structured_output_retries",
            "structured_output_retry_session_reset", "decision_repairs",
            "rate_limit_retries", "tool_corrections",
        )
        if key in cache
    }
    return bounded(redact_secrets(selected)) if metadata else {}


def _failure_status(exc: BaseException) -> str:
    budget_blocked = isinstance(exc, ModelRequestBudgetExhausted) or (
        isinstance(exc, ExecutionConflict)
        and str(exc) == "Durable model request budget exhausted."
    )
    return "blocked" if budget_blocked else "failed"


def _safe_failure_detail(exc: BaseException, validation_failures: list[dict]) -> str:
    if _failure_status(exc) == "blocked":
        return (
            redact_operator_text(exc, max_chars=1200)
            if isinstance(exc, ModelRequestBudgetExhausted)
            else "Durable model request budget exhausted before dispatch."
        )
    if validation_failures:
        messages = [
            f"{'.'.join(map(str, item['location'])) or 'root'}: {item['message']}"
            for item in validation_failures[-1]["errors"][:3]
        ]
        return redact_operator_text("; ".join(messages), max_chars=1200)
    if isinstance(exc, ExperimentContractError):
        return redact_operator_text(exc, max_chars=1200)
    return "Detailed exception text omitted because it may contain model or input content."


@contextmanager
def _experiment_budget(limit: int):
    parent = current_model_request_budget()
    lease = parent.reserve_child(stage="v2_experiments") if parent is not None else None
    effective = min(limit, lease.allocated) if lease and lease.allocated is not None else limit
    consumed = 0
    try:
        with activate_model_request_budget(effective) as ledger:
            try:
                yield ledger
            finally:
                consumed = ledger.consumed
    finally:
        if lease is not None:
            lease.settle(actual_requests=consumed)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def load_catalog(
    path: Path = CATALOG_PATH,
) -> tuple[list[ExperimentDefinition], list[ExperimentCase]]:
    data = json.loads(path.read_text())
    definitions = [ExperimentDefinition.model_validate(item) for item in data["experiments"]]
    cases = [ExperimentCase.model_validate(item) for item in data["cases"]]
    if len({item.experiment_id for item in definitions}) != len(definitions):
        raise ValueError("Duplicate experiment IDs.")
    if len({item.case_id for item in cases}) != len(cases):
        raise ValueError("Duplicate case IDs.")
    return definitions, cases


def validate_answer(answer: ExperimentAnswer, case: ExperimentCase) -> list[str]:
    """Validate evidence identity and shape, never claim semantic truth."""
    allowed = {source.source_id for source in case.sources}
    issues = []
    if len({claim.claim_id for claim in answer.claims}) != len(answer.claims):
        issues.append("duplicate_claim_identity")
    if any(set(claim.source_ids) - allowed for claim in answer.claims):
        issues.append("unknown_claim_source")
    return issues


def validate_review(
    review: ExperimentReview, answer: ExperimentAnswer, case: ExperimentCase
) -> list[str]:
    claims = {claim.claim_id for claim in answer.claims}
    obligations = {item.obligation_id for item in case.obligations}
    sources = {source.source_id for source in case.sources}
    issues = []
    if any(finding.claim_id and finding.claim_id not in claims for finding in review.findings):
        issues.append("unknown_review_claim")
    if any(
        finding.obligation_id and finding.obligation_id not in obligations
        for finding in review.findings
    ):
        issues.append("unknown_review_obligation")
    if any(set(finding.source_ids) - sources for finding in review.findings):
        issues.append("unknown_review_source")
    return issues


def _check(name: str, passed: bool, detail: str) -> ExperimentCheck:
    return ExperimentCheck(name=name, passed=passed, detail=detail)


def render_answer(answer: ExperimentAnswer, *, structured: bool) -> str:
    facts = "\n".join(f"- {claim.text} [{', '.join(claim.source_ids)}]" for claim in answer.claims)
    limitations = (
        "\n".join(f"- {item}" for item in answer.limitations) or "No additional limitation stated."
    )
    if structured:
        return (
            f"Recommendation\n{answer.recommendation}\n\nEvidence\n{facts}\n\n"
            f"Uncertainty\n{limitations}\n\nNext step\n{answer.next_step}"
        )
    return f"{answer.recommendation}\n\n{facts}\n\n{limitations}\n\n{answer.next_step}"


def context_profile(case: ExperimentCase, *, compact: bool) -> dict:
    packet = {
        "current_request": case.request,
        "sources": [s.model_dump() for s in case.sources],
        "obligations": [item.model_dump() for item in case.obligations],
    }
    if compact:
        return {"packet": packet, "authority": "Current request and source packet only."}
    return {
        "packet": packet,
        "copied_context": packet,
        "nested_context": packet,
        "authority": "Current request and source packet only.",
    }


def document_layout_fixture() -> tuple[dict, Path]:
    """Verify the fixed synthetic document before admitting its page image."""
    root = LAYOUT_FIXTURE_DIRECTORY.resolve()
    manifest = json.loads((root / "v2_document_layout.json").read_text())
    page = root / "v2_document_layout.png"
    if page.is_symlink() or page.resolve().parent != root:
        raise ExperimentContractError("layout_page_outside_fixture_directory")
    raw = read_supported_local_file(page)
    gold = json.dumps(manifest["gold"], sort_keys=True, separators=(",", ":"))
    extracted = (root / "v2_document_layout.txt").read_text()
    if (
        hashlib.sha256(raw.data).hexdigest() != manifest["page_png_sha256"]
        or hashlib.sha256(gold.encode()).hexdigest() != manifest["gold_sha256"]
        or hashlib.sha256(extracted.encode()).hexdigest() != manifest["extracted_text_sha256"]
        or extracted != manifest["extracted_text"]
    ):
        raise ExperimentContractError("layout_fixture_integrity_mismatch")
    png_metadata = {}
    offset = 8
    while offset + 12 <= len(raw.data):
        length = int.from_bytes(raw.data[offset : offset + 4], "big")
        if offset + 12 + length > len(raw.data):
            raise ExperimentContractError("layout_png_chunk_invalid")
        kind = raw.data[offset + 4 : offset + 8]
        if kind == b"tEXt":
            key, value = raw.data[offset + 8 : offset + 8 + length].split(b"\0", 1)
            png_metadata[key.decode()] = value.decode()
        offset += length + 12
    if (
        png_metadata.get("DocumentID") != manifest["gold"]["document_id"]
        or png_metadata.get("PageNumber") != str(manifest["gold"]["page"])
        or png_metadata.get("GoldSHA256") != manifest["gold_sha256"]
    ):
        raise ExperimentContractError("layout_page_provenance_mismatch")
    return manifest, page


def document_layout_input(
    case: ExperimentCase, *, with_images: bool
) -> tuple[dict, tuple[str, ...], dict]:
    manifest, page = document_layout_fixture()
    if case.document_fixture != "v2_document_layout" or len(case.sources) != 1:
        raise ExperimentContractError("layout_case_requires_fixed_document")
    if (
        case.sources[0].source_id != manifest["source_id"]
        or case.sources[0].text != manifest["extracted_text"]
    ):
        raise ExperimentContractError("layout_extraction_does_not_match_document")
    provenance = {
        "document_id": manifest["gold"]["document_id"],
        "page": manifest["gold"]["page"],
        "source_id": manifest["source_id"],
        "gold_sha256": manifest["gold_sha256"],
        "page_png_sha256": manifest["page_png_sha256"],
        "extracted_text_sha256": manifest["extracted_text_sha256"],
        "controlled_extraction_omission": True,
    }
    payload = {
        "request": case.request,
        "obligations": [item.model_dump() for item in case.obligations],
        "sources": [source.model_dump() for source in case.sources],
        "document_page": {**provenance, "page_image_attached": with_images},
        "evidence_instruction": (
            "Use extracted text and the selected page image, if attached, as evidence for "
            "the same source_id. Do not infer missing cell values from filenames or hashes. "
            "If a cell is unavailable in the provided input, state the uncertainty."
        ),
    }
    return payload, (str(page),) if with_images else (), provenance


def _document_layout_checks(case: ExperimentCase) -> list[ExperimentCheck]:
    text_input, _, text_provenance = document_layout_input(case, with_images=False)
    image_input, trusted, image_provenance = document_layout_input(case, with_images=True)
    text_only = sdk_input_from_typed_input(text_input, live=True, provider="openai")
    with_image = sdk_input_from_typed_input(
        image_input, live=True, provider="openai", trusted_attachment_paths=trusted
    )
    images = [
        part
        for item in with_image
        if isinstance(item, dict)
        for part in item.get("content", [])
        if part.get("type") == "input_image"
    ]
    page_hash = (
        hashlib.sha256(base64.b64decode(images[0]["image_url"].split(",", 1)[1])).hexdigest()
        if images
        else ""
    )
    extracted = case.sources[0].text
    return [
        _check(
            "same_underlying_document",
            text_provenance == image_provenance,
            "Both variants use the same document/page, extracted text, and gold fingerprint.",
        ),
        _check(
            "decisive_cells_absent_from_text",
            extracted.count("[cell not extracted]") == 2
            and "Not measured" not in extracted
            and " | No | " not in extracted,
            "The controlled text extraction omits the prospective-evaluation status/result cells.",
        ),
        _check(
            "native_page_image_bytes_verified",
            len(images) == 1 and page_hash == image_provenance["page_png_sha256"],
            "The shared SDK serializer attaches exactly the verified page PNG.",
        ),
        _check(
            "text_only_has_no_image",
            isinstance(text_only, str),
            "Text-only input has no media; nested content cannot grant attachment access.",
        ),
        _check(
            "gold_cells_not_leaked_in_text_prompt",
            "Not measured" not in json.dumps(text_input),
            "Gold cell values are not hidden in the text-only prompt metadata.",
        ),
    ]


class _PhaseState(TypedDict, total=False):
    source_ids: list[str]
    phase: str


def _phase_checkpoint_checks(case: ExperimentCase) -> list[ExperimentCheck]:
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.graph import END, START, StateGraph

    acquired = 0
    decisions = 0

    def acquire(_state: _PhaseState) -> dict:
        nonlocal acquired
        acquired += 1
        return {"source_ids": [s.source_id for s in case.sources], "phase": "evidence_ready"}

    def decide(state: _PhaseState) -> dict:
        nonlocal decisions
        decisions += 1
        if decisions == 1:
            raise RuntimeError("Injected failure after evidence acquisition")
        return {"phase": "review_ready", "source_ids": state["source_ids"]}

    def review(state: _PhaseState) -> dict:
        if state["source_ids"] != [source.source_id for source in case.sources]:
            raise ValueError("The resumed phase lost source identity.")
        return {"phase": "complete"}

    builder = StateGraph(_PhaseState)
    builder.add_node("research_evidence", acquire)
    builder.add_node("research_decision", decide)
    builder.add_node("research_review", review)
    builder.add_edge(START, "research_evidence")
    builder.add_edge("research_evidence", "research_decision")
    builder.add_edge("research_decision", "research_review")
    builder.add_edge("research_review", END)
    with TemporaryDirectory(prefix="kba-v2-phases-") as directory:
        path = str(Path(directory) / "checkpoints.sqlite3")
        config = {"configurable": {"thread_id": "synthetic-phase-case"}}
        with SqliteSaver.from_conn_string(path) as saver:
            graph = builder.compile(checkpointer=saver)
            try:
                graph.invoke({}, config)
            except RuntimeError:
                pass
        # Reopen the saver and graph: the completed source phase must survive.
        with SqliteSaver.from_conn_string(path) as saver:
            result = builder.compile(checkpointer=saver).invoke(None, config)
    return [
        _check("completed_evidence_not_repeated", acquired == 1, f"acquisition_calls={acquired}"),
        _check("failed_phase_resumed", decisions == 2, f"decision_attempts={decisions}"),
        _check(
            "same_agent_evidence_preserved",
            result["source_ids"] == [s.source_id for s in case.sources],
            "Three phase nodes share one Research owner; zero model calls.",
        ),
        _check(
            "phase_review_completed",
            result["phase"] == "complete",
            "The same-owner review runs after the resumed decision.",
        ),
    ]


def _diagnostic_replay_checks(case: ExperimentCase) -> list[ExperimentCheck]:
    from keystone_agents.orchestration.checkpoints import (
        fork_graph_checkpoint,
        inspect_graph_execution,
        run_durable_graph,
    )
    from keystone_agents.runtime.durable_execution import ExecutionConflict
    from keystone_agents.schemas.work_item import WorkflowRunRequest

    with TemporaryDirectory(prefix="kba-v2-replay-") as directory:
        database_url = f"sqlite:///{directory}/business.sqlite3"
        request = WorkflowRunRequest(
            request_text="Summarize this synthetic review status without provider actions.",
            database_url=database_url,
            manual_request_plan={
                "source": "canonical",
                "target_agent": "chief_of_staff",
                "intent": "route_request",
                "workflow": [],
            },
            model_request_limit=0,
        )
        outcome = run_durable_graph(request)
        before = inspect_graph_execution(outcome.execution_id, database_url=database_url)
        checkpoint_id = before["history"][-1]["checkpoint_id"]
        fork = fork_graph_checkpoint(
            outcome.execution_id,
            checkpoint_id,
            database_url=database_url,
            changes={"request_text": case.request},
        )
        rejected = False
        try:
            fork_graph_checkpoint(
                outcome.execution_id,
                checkpoint_id,
                database_url=database_url,
                changes={"live_sdk": True},
            )
        except ExecutionConflict:
            rejected = True
        after = inspect_graph_execution(outcome.execution_id, database_url=database_url)
    return [
        _check("fork_scope_cannot_expand", rejected, "A live-capability patch was rejected."),
        _check(
            "source_execution_unchanged",
            fingerprint(before) == fingerprint(after),
            "Read-only inspection/fork preserved the source execution.",
        ),
        _check(
            "fork_is_non_executing",
            not fork["execution_enabled"] and not fork["request"]["live_sdk"],
            "No approvals or live provider capabilities transfer to the diagnostic fork.",
        ),
    ]


def mechanics(definition: ExperimentDefinition, case: ExperimentCase) -> list[ExperimentCheck]:
    answer = case.scripted_answer
    checks = [
        _check(
            "valid_source_identity_control",
            not validate_answer(answer, case),
            "This checks citation identity, not truth of the scripted claims.",
        )
    ]
    bad = answer.model_copy(deep=True)
    bad.claims[0].source_ids = ["not-in-packet"]
    checks.append(
        _check(
            "unknown_source_negative_control",
            bool(validate_answer(bad, case)),
            "An invented source must be rejected.",
        )
    )
    identity = definition.experiment_id
    if identity == "same_agent_stages":
        checks.extend(_phase_checkpoint_checks(case))
    elif identity in {"independent_critic", "targeted_repair"}:
        finding = {
            "claim_id": answer.claims[0].claim_id,
            "source_ids": answer.claims[0].source_ids,
            "reason": "Scripted review of this exact claim and source.",
            "repair": "revise",
        }
        review = ExperimentReview(findings=[finding], recommendation="revise")
        checks.append(
            _check(
                "bound_review_control",
                not validate_review(review, answer, case),
                "Advisory criticism requires both claim and evidence identities.",
            )
        )
        invalid = review.model_copy(deep=True)
        invalid.findings[0].claim_id = "unknown-claim"
        checks.append(
            _check(
                "unbound_critic_rejected",
                bool(validate_review(invalid, answer, case)),
                "Critic confidence cannot replace claim binding.",
            )
        )
        if case.obligations:
            omitted = answer.model_copy(update={"claims": []})
            omission_review = ExperimentReview(
                findings=[
                    {
                        "obligation_id": case.obligations[0].obligation_id,
                        "reason": "The requested evidence explanation is omitted.",
                        "repair": "revise",
                    }
                ],
                recommendation="revise",
            )
            checks.append(
                _check(
                    "omitted_deliverable_is_targetable",
                    not omitted.claims and not validate_review(omission_review, omitted, case),
                    "Requested work remains reviewable when no candidate claim represents it.",
                )
            )
            unknown = omission_review.model_copy(deep=True)
            unknown.findings[0].obligation_id = "not-requested"
            checks.append(
                _check(
                    "unknown_obligation_rejected",
                    bool(validate_review(unknown, omitted, case)),
                    "The critic cannot invent extra requested deliverables.",
                )
            )
        refused = False
        try:
            ExperimentReview(recommendation="accept", grants_authority=True)
        except ValueError:
            refused = True
        checks.append(
            _check("critic_cannot_grant_authority", refused, "Review never approves execution.")
        )
    elif identity == "complementary_research":
        groups = [case.sources[::2], case.sources[1::2]]
        with ThreadPoolExecutor(max_workers=2) as pool:
            packets = list(pool.map(lambda group: [s.model_dump() for s in group], groups))
        merged = {source["source_id"]: source for packet in packets for source in packet}
        checks.append(
            _check(
                "parallel_union_preserves_sources",
                set(merged) == {s.source_id for s in case.sources},
                "Two concurrent fixture investigators preserve the shared source universe.",
            )
        )
        duplicated = packets + packets[:1]
        unique = {s["source_id"] for packet in duplicated for s in packet}
        checks.append(
            _check(
                "duplicates_do_not_inflate_evidence",
                len(unique) == len(case.sources),
                "Repeated investigators do not add independent sources or votes.",
            )
        )

        def failed_investigator():
            raise RuntimeError("Injected branch failure")

        with ThreadPoolExecutor(max_workers=2) as pool:
            good = pool.submit(lambda: packets[0])
            bad = pool.submit(failed_investigator)
            surviving = good.result()
            failed = False
            try:
                bad.result()
            except RuntimeError:
                failed = True
        checks.append(
            _check(
                "partial_branch_is_explicit",
                failed and surviving == packets[0],
                "A failed branch preserves successful evidence without claiming full coverage.",
            )
        )
    elif identity == "context_memory":
        full = context_profile(case, compact=False)
        compact = context_profile(case, compact=True)
        checks.append(
            _check(
                "same_packet_less_duplication",
                full["packet"] == compact["packet"]
                and len(json.dumps(compact)) < len(json.dumps(full)),
                f"controlled_legacy_chars={len(json.dumps(full))}; "
                f"compact_chars={len(json.dumps(compact))}",
            )
        )
    elif identity == "checkpoint_replay":
        checks.extend(_diagnostic_replay_checks(case))
    elif identity == "layout_evidence":
        checks.extend(_document_layout_checks(case))
        variants = [render_answer(answer, structured=value) for value in (False, True)]
        required = [*answer.limitations, *(s for claim in answer.claims for s in claim.source_ids)]
        checks.append(
            _check(
                "layout_preserves_evidence",
                all(token in text for token in required for text in variants),
                "Both layouts contain the same claims, evidence IDs, and limitations.",
            )
        )
        dropped = variants[1].replace(answer.claims[0].source_ids[0], "")
        checks.append(
            _check(
                "dropped_citation_negative_control",
                not all(token in dropped for token in required),
                "Removing one required source identity fails the same layout coverage check.",
            )
        )
    else:
        raise ValueError("No mechanical driver is registered for this experiment.")
    return checks


def _model_variant(
    observation: ExperimentObservation,
    case: ExperimentCase,
    *,
    live: bool,
    run_config: Any,
    model: str | None,
    ledger: Any,
) -> None:
    variant = observation.variant
    trusted_attachment_paths: tuple[str, ...] = ()
    if observation.experiment_id == "layout_evidence":
        packet, trusted_attachment_paths, provenance = document_layout_input(
            case, with_images=variant == "text_plus_page_images"
        )
        observation.input_provenance = provenance
    else:
        packet = context_profile(case, compact=variant == "compact_context")
    author_instructions = ""
    author_input = {}

    def phase(owner: str, name: str, payload: dict, *, review: bool = False):
        nonlocal author_instructions, author_input
        output_type = ExperimentReview if review else ExperimentAnswer
        instructions = (
            author_instructions + "\n\n" + load_prompt("v2_experiment_self_review.md")
            if review and owner == "researcher"
            else compose_direct_instructions(
                "v2_experiment_review.md" if review else "v2_experiment_agent.md"
            )
        )
        agent = build_sdk_agent(
            name=f"v2_experiment_{owner}",
            instructions=instructions,
            output_type=output_type,
            tools=[],
            guardrails=keystone_guardrails(),
            model=model,
            model_settings=build_model_settings(max_tokens=1800),
            enforce_tool_policy=False,
        )
        model_input = {"owner": owner, "phase": name, **payload}
        serialized_input = json.dumps(model_input)
        if name == "initial_answer":
            author_instructions = instructions
            author_input = json.loads(serialized_input)
        model_config = get_runtime_agent_model_config(agent.name, model_override=model)
        if trusted_attachment_paths and model_config.provider != "openai":
            raise ExperimentContractError("native_page_image_requires_openai_input_support")
        stage = {
            "owner": owner,
            "phase": name,
            "input_fingerprint": fingerprint(model_input),
            "input_chars": len(serialized_input),
            "status": "started",
            "tool_count": 0,
            "configured_model": str(agent.model),
            "input_image_count": len(trusted_attachment_paths),
            "input_document_provenance": observation.input_provenance,
            "instructions_fingerprint": fingerprint(instructions),
        }
        observation.stages.append(stage)
        validation_failures: list[dict] = []
        agent.output_type = _diagnostic_output_schema(output_type, validation_failures)
        started = perf_counter()
        before = ledger.consumed
        try:
            result = run_typed_sdk_agent(
                agent=agent,
                typed_input=model_input,
                output_type=output_type,
                live=live or (run_config is not None and bool(trusted_attachment_paths)),
                run_config=run_config,
                config=model_config,
                session=None,
                inherit_env_session=False,
                tracing_disabled=True,
                max_turns=1,
                trusted_attachment_paths=trusted_attachment_paths,
                workflow_name="Isolated KBA V2 experiment",
            )
            stage.update(status="completed", usage=result.usage, cost=result.cost)
            return result.output
        except Exception as exc:
            failure = _bounded_failure_metadata(exc)
            stage.update(
                status=_failure_status(exc),
                error_type=type(exc).__name__,
                error_detail=_safe_failure_detail(exc, validation_failures),
                sdk_run_failure=failure,
                usage=failure.get("usage", {"available": False}),
                cost=failure.get("cost", {"available": False}),
                model_request_admission_blocked=_failure_status(exc) == "blocked",
            )
            raise
        finally:
            if validation_failures:
                stage["validation_failures"] = validation_failures
            stage["elapsed_ms"] = round((perf_counter() - started) * 1000, 3)
            stage["model_requests"] = ledger.consumed - before

    if variant == "complementary_investigators":
        investigators = []
        for index, sources in enumerate((case.sources[::2], case.sources[1::2])):
            if not sources:
                continue
            answer = phase(
                f"investigator_{index + 1}",
                "independent_evidence_assessment",
                {
                    "request": case.request,
                    "sources": [s.model_dump() for s in sources],
                    "obligations": [item.model_dump() for item in case.obligations],
                },
            )
            if validate_answer(answer, case.model_copy(update={"sources": sources})):
                raise ExperimentContractError("investigator_source_outside_assigned_packet")
            investigators.append(answer)
        packet["investigator_results"] = [item.model_dump() for item in investigators]
        observation.answer = phase("researcher", "evidence_adjudication", packet)
    else:
        observation.answer = phase("researcher", "initial_answer", packet)
    errors = validate_answer(observation.answer, case)
    if errors:
        raise ExperimentContractError(",".join(errors))
    if variant in {"self_review", "independent_critic", "targeted_repair"}:
        owner = (
            "researcher"
            if variant == "self_review"
            else "critic"
            if variant == "independent_critic"
            else "supervisor"
        )
        review_input = (
            {
                "prior_author_input": author_input,
                "prior_author_answer": observation.answer.model_dump(),
                "context_mode": "explicit_same_author_continuation",
            }
            if variant == "self_review"
            else {
                "request": case.request,
                "obligations": [item.model_dump() for item in case.obligations],
                "sources": [s.model_dump() for s in case.sources],
                "answer": observation.answer.model_dump(),
                "context_mode": "fresh_evidence_review",
            }
        )
        observation.review = phase(
            owner,
            "evidence_review",
            review_input,
            review=True,
        )
        errors = validate_review(observation.review, observation.answer, case)
        if errors:
            raise ExperimentContractError(",".join(errors))
        if observation.review.recommendation == "revise":
            observation.answer = phase(
                "researcher",
                "one_targeted_revision",
                {
                    "request": case.request,
                    "obligations": [item.model_dump() for item in case.obligations],
                    "sources": [s.model_dump() for s in case.sources],
                    "answer": observation.answer.model_dump(),
                    "review": observation.review.model_dump(),
                },
            )
            if validate_answer(observation.answer, case):
                raise ExperimentContractError("revision_source_outside_packet")
        elif observation.review.recommendation in {"needs_source", "stop"}:
            observation.status = "blocked"
            observation.error = (
                "Review needs evidence or operator resolution; no provider tool is available."
            )
    observation.presentations = {
        "plain": render_answer(observation.answer, structured=False),
        "sectioned": render_answer(observation.answer, structured=True),
    }


def run_experiments(
    *,
    experiment_id: str = "all",
    case_id: str | None = None,
    variant: str | None = None,
    split: str = "all",
    live: bool = False,
    max_model_requests: int | None = None,
    model: str | None = None,
    run_config: Any = None,
    catalog_path: Path = CATALOG_PATH,
) -> ExperimentReport:
    definitions, cases = load_catalog(catalog_path)
    definitions = [item for item in definitions if experiment_id in {"all", item.experiment_id}]
    cases = [
        item
        for item in cases
        if (case_id is None or item.case_id == case_id) and split in {"all", item.split}
    ]
    if not definitions or not cases:
        raise ValueError("No experiment/case matches the requested selection.")
    if variant and any(variant not in item.variants for item in definitions):
        raise ValueError("Variant is not declared for every selected experiment.")
    use_models = live or run_config is not None
    if use_models and (max_model_requests is None or max_model_requests <= 0):
        raise ValueError("Model experiments require an explicit positive max_model_requests.")
    if live and (experiment_id == "all" or case_id is None):
        raise ValueError("Live experiments require one exact experiment and case.")
    observations = []
    runtime = {
        "openai_agents_version": version("openai-agents"),
        "openai_version": version("openai"),
        "source_fingerprint": current_runtime_fingerprint()["source_sha256"],
    }
    mode = "fake_model" if run_config is not None else "live_model" if live else "scripted_control"
    with _experiment_budget(max_model_requests if use_models else 0) as ledger:
        for definition in definitions:
            for case in cases:
                if (
                    case.applicable_experiments
                    and definition.experiment_id not in case.applicable_experiments
                ):
                    continue
                # Mechanical controls are run once per case, not scored as model quality.
                try:
                    checks = mechanics(definition, case)
                except Exception as exc:
                    checks = [_check("mechanical_prerequisite", False, type(exc).__name__)]
                provenance = {}
                if case.document_fixture and all(check.passed for check in checks):
                    _, _, provenance = document_layout_input(case, with_images=False)
                variants = (
                    [variant]
                    if variant
                    else definition.variants
                    if use_models
                    else ["mechanical_controls"]
                )
                for selected in variants:
                    observation = ExperimentObservation(
                        observation_id=uuid4().hex,
                        experiment_id=definition.experiment_id,
                        case_id=case.case_id,
                        split=case.split,
                        variant=selected,
                        input_fingerprint=fingerprint(
                            {
                                "request": case.request,
                                "obligations": [item.model_dump() for item in case.obligations],
                                "sources": [s.model_dump() for s in case.sources],
                                "document_provenance": provenance,
                            }
                        ),
                        input_provenance=provenance,
                        budget_limit=ledger.limit,
                        mode=mode,
                        checks=checks,
                    )
                    start = perf_counter()
                    before = ledger.consumed
                    try:
                        if not all(check.passed for check in checks):
                            observation.status = "failed"
                            observation.error = (
                                "A mechanical prerequisite failed; model phase skipped."
                            )
                        elif use_models:
                            _model_variant(
                                observation,
                                case,
                                live=live,
                                run_config=run_config,
                                model=model,
                                ledger=ledger,
                            )
                    except Exception as exc:
                        observation.status = _failure_status(exc)
                        stage_detail = observation.stages[-1].get("error_detail") if (
                            observation.stages and observation.stages[-1]["status"] != "completed"
                        ) else None
                        observation.error = (
                            f"{type(exc).__name__}: "
                            f"{stage_detail or _safe_failure_detail(exc, [])}"
                        )
                    observation.model_requests = ledger.consumed - before
                    observation.elapsed_ms = round((perf_counter() - start) * 1000, 3)
                    observations.append(observation)
        total_requests = ledger.consumed
    if not observations:
        raise ValueError("The selected case does not apply to this experiment.")
    return ExperimentReport(
        run_id=uuid4().hex,
        created_at=datetime.now(UTC).isoformat(),
        mode=mode,
        definitions=definitions,
        observations=observations,
        model_requests=total_requests,
        budget_limit=ledger.limit,
        requested_budget_limit=max_model_requests,
        runtime=runtime,
        limitations=[
            "Scripted controls do not measure semantic model quality or production lift.",
            "Baseline is one isolated author, not a complete production KBA benchmark.",
            "Model variants run serially. Parallel live latency benefits remain unmeasured.",
            "Source identity checks do not prove truth; human assessment is required.",
            "No provider tools, inherited sessions, provider writes, or automatic rollout.",
        ],
    )


def blinded_presentations(report: ExperimentReport) -> dict[str, dict[str, str]]:
    output = {}
    for observation in report.observations:
        for layout, text in observation.presentations.items():
            label = fingerprint([report.run_id, observation.observation_id, layout])[:12]
            output[label] = {"case_id": observation.case_id, "text": text}
    return output


def apply_human_ratings(report: ExperimentReport, ratings: dict[str, dict]) -> ExperimentReport:
    allowed = blinded_presentations(report)
    if report.mode != "live_model":
        raise ValueError("Scripted/fake outputs cannot be promoted to measured model quality.")
    if not isinstance(ratings, dict) or any(not isinstance(v, dict) for v in ratings.values()):
        raise ValueError("Ratings must map exact blinded output IDs to rubric objects.")
    for label, values in ratings.items():
        if label not in allowed or set(values) != {
            "correctness",
            "usefulness",
            "evidence_visibility",
        }:
            raise ValueError(
                "Rating must reference an exact blinded output and three rubric dimensions."
            )
        if any(type(value) is not int or not 1 <= value <= 5 for value in values.values()):
            raise ValueError("Human ratings must be integers from 1 to 5.")
    return report.model_copy(
        update={
            "human_ratings": ratings,
            "quality_status": "HUMAN_RATED" if ratings else "UNMEASURED",
        }
    )


def report_markdown(report: ExperimentReport) -> str:
    lines = [
        "# KBA V2 experiment report",
        "",
        f"Mode: **{report.mode}**. Model quality: **{report.quality_status}**.",
        f"Model requests: {report.model_requests}; provider writes: 0.",
        "",
        "| Experiment | Case | Variant | Status | Mechanical checks | Model requests |",
        "|---|---|---|---|---|---|",
    ]
    for row in report.observations:
        lines.append(
            f"| {row.experiment_id} | {row.case_id} | {row.variant} | {row.status} | "
            f"{sum(c.passed for c in row.checks)}/{len(row.checks)} | {row.model_requests} |"
        )
    lines.extend(["", "## Proof boundaries", "", *(f"- {value}" for value in report.limitations)])
    for row in report.observations:
        if row.error or row.answer is not None:
            lines.extend(["", f"## {row.case_id}: {row.variant}", ""])
            if row.error:
                lines.append(f"Execution status: {row.status}; diagnostic: {row.error}.")
            if row.presentations:
                lines.extend(
                    [
                        "Candidate output (not a quality verdict):",
                        "",
                        row.presentations["sectioned"],
                    ]
                )
            if row.review:
                lines.extend(["", f"Advisory review: {row.review.recommendation}."])
                lines.extend(
                    f"- {finding.claim_id or 'obligation:' + finding.obligation_id} "
                    f"[{', '.join(finding.source_ids)}]: {finding.reason}"
                    for finding in row.review.findings
                )
    if report.human_ratings:
        lines.extend(
            [
                "",
                f"Human ratings cover {len(report.human_ratings)} exact blinded outputs.",
                "Unrated outputs remain unmeasured; no aggregate quality lift is inferred.",
            ]
        )
    return "\n".join(lines) + "\n"


def write_report(report: ExperimentReport, directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(report.model_dump_json(indent=2) + "\n")
    (directory / "report.md").write_text(report_markdown(report))
    (directory / "blinded_outputs.json").write_text(
        json.dumps(blinded_presentations(report), indent=2) + "\n"
    )
