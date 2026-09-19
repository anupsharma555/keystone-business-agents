"""Content-free diagnostics captured before SDK output-error redaction."""

from __future__ import annotations

import re
from collections.abc import Mapping
from copy import copy, deepcopy
from pathlib import Path
from typing import Any, get_args

from pydantic import TypeAdapter, ValidationError
from pydantic_core import ErrorType

_ERROR_TYPES = frozenset(get_args(ErrorType))
_SCHEMA_ROOT = Path(__file__).resolve().parents[1] / "schemas"
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
_PLACEHOLDERS = {"<dynamic_key>", "<unknown_field>"}
_MAX_FAILURES = 8
_REVIEWED_RULES = {
    (
        "src/keystone_agents/schemas/decision_ownership.py",
        "_validate_selection_shape",
        "Selected identity fields must match selected assessments.",
    ): "decision_selected_set_mismatch",
}
_RULE_FEEDBACK = {
    "decision_selected_set_mismatch": (
        "The exact set union of non-empty selected_candidate_id and selected_candidate_ids "
        "must equal the candidate_id values in candidate_assessments whose disposition is "
        "'selected'. Choose the identities, dispositions, and alternatives yourself using "
        "the original request and available evidence; do not drop requested work or invent "
        "evidence to satisfy this rule."
    ),
}


def _reviewed_rule(error: Any, validator: Mapping[str, Any]) -> str:
    if type(error) is not ValueError or len(error.args) != 1 or type(error.args[0]) is not str:
        return ""
    return _REVIEWED_RULES.get(
        (validator.get("file"), validator.get("function"), error.args[0]), ""
    )


def _branches(schema: Any, root: Mapping[str, Any], depth: int = 0) -> list[dict[str, Any]]:
    if not isinstance(schema, dict) or depth >= 12:
        return []
    reference = schema.get("$ref")
    if isinstance(reference, str) and reference.startswith("#/"):
        target: Any = root
        for key in reference[2:].split("/"):
            target = (
                target.get(key.replace("~1", "/").replace("~0", "~"), {})
                if isinstance(target, dict)
                else {}
            )
        return _branches(target, root, depth + 1)
    branches = [schema]
    for kind in ("anyOf", "oneOf", "allOf"):
        for child in schema.get(kind, []):
            branches.extend(_branches(child, root, depth + 1))
    return branches


def _field_path(location: Any, schema: dict[str, Any]) -> list[str | int]:
    candidates = _branches(schema, schema)
    safe: list[str | int] = []
    for part in tuple(location or ())[:8]:
        children = []
        if type(part) is int and 0 <= part <= 1_000_000:
            for candidate in candidates:
                if "items" in candidate:
                    children.append(candidate["items"])
                prefix = candidate.get("prefixItems", [])
                if part < len(prefix):
                    children.append(prefix[part])
            safe.append(part if children else "<dynamic_key>")
        else:
            for candidate in candidates:
                properties = candidate.get("properties", {})
                if isinstance(part, str) and part in properties and _NAME.fullmatch(part):
                    children.append(properties[part])
            if children:
                safe.append(part)
            else:
                children = [
                    c["additionalProperties"]
                    for c in candidates
                    if isinstance(c.get("additionalProperties"), dict)
                ]
                safe.append("<dynamic_key>" if children else "<unknown_field>")
        if children:
            candidates = [branch for child in children for branch in _branches(child, schema)]
    return safe


def _validator_location(error: Any) -> dict[str, Any] | None:
    frame = error.__traceback__ if isinstance(error, BaseException) else None
    result = None
    while frame is not None:
        code = frame.tb_frame.f_code
        try:
            relative = Path(code.co_filename).resolve().relative_to(_SCHEMA_ROOT)
        except (ValueError, OSError):
            pass
        else:
            if relative.suffix == ".py" and _NAME.fullmatch(code.co_name):
                result = {
                    "file": f"src/keystone_agents/schemas/{relative.as_posix()}",
                    "function": code.co_name,
                    "line": frame.tb_lineno,
                }
        frame = frame.tb_next
    return result


class OutputValidationDiagnostics:
    """One invocation's bounded schema-only errors; never retains error objects."""

    def __init__(self, output_type: Any) -> None:
        name = getattr(output_type, "__name__", "")
        self.output_type = name if _NAME.fullmatch(name) else "structured_output"
        self.schema = TypeAdapter(output_type).json_schema()
        self.attempt = 1
        self.failures: list[dict[str, Any]] = []

    def capture(self, error: ValidationError) -> None:
        if len(self.failures) >= _MAX_FAILURES:
            return
        errors = []
        for item in error.errors(include_input=False, include_context=True, include_url=False)[:12]:
            code = item.get("type")
            detail: dict[str, Any] = {
                "type": code if code in _ERROR_TYPES else "custom_validation_error",
                "location": _field_path(item.get("loc"), self.schema),
            }
            context = item.get("ctx")
            validator = (
                _validator_location(context.get("error")) if isinstance(context, dict) else None
            )
            if validator is not None:
                detail["validator"] = validator
                rule = _reviewed_rule(context.get("error"), validator)
                if rule:
                    detail["rule_code"] = rule
            errors.append(detail)
        self.failures.append(
            {
                "attempt": self.attempt,
                "output_type": self.output_type,
                "error_count": min(error.error_count(), 1_000_000),
                "errors": errors,
            }
        )

    def snapshot(self) -> dict[str, Any]:
        return {"schema": "keystone.output_validation.v1", "failures": deepcopy(self.failures)}

    def record_unobserved_schema_error(self) -> None:
        if len(self.failures) < _MAX_FAILURES and not any(
            failure["attempt"] == self.attempt for failure in self.failures
        ):
            self.failures.append(
                {
                    "attempt": self.attempt,
                    "output_type": self.output_type,
                    "status": "no_schema_diagnostic_observed",
                    "errors": [],
                }
            )


class _DiagnosticAdapter:
    def __init__(self, adapter: Any, collector: OutputValidationDiagnostics) -> None:
        self.adapter = adapter
        self.collector = collector

    def __getattr__(self, name: str) -> Any:
        return getattr(object.__getattribute__(self, "adapter"), name)

    def __deepcopy__(self, memo: dict[int, Any]) -> _DiagnosticAdapter:
        # SDK identity inspection deep-copies schema dataclass fields. The adapter
        # is run-local; retain its collector rather than creating a detached copy.
        memo[id(self)] = self
        return self

    def validate_json(self, *args: Any, **kwargs: Any) -> Any:
        try:
            return self.adapter.validate_json(*args, **kwargs)
        except ValidationError as error:
            try:
                self.collector.capture(error)
            except Exception:
                pass  # Diagnostics cannot alter the SDK's validation/error contract.
            raise


def agent_with_output_diagnostics(agent: Any, collector: OutputValidationDiagnostics) -> Any:
    """Copy only the output adapter; preserve JSON schema, callbacks and acceptance."""
    try:
        from agents.agent_output import AgentOutputSchema, AgentOutputSchemaBase
    except ImportError:
        return agent
    declared = getattr(agent, "output_type", None)
    if declared is None or declared is str:
        return agent
    schema = (
        declared if isinstance(declared, AgentOutputSchemaBase) else AgentOutputSchema(declared)
    )
    if not isinstance(schema, AgentOutputSchema):
        return agent  # Custom validation contracts remain untouched.
    local_schema = copy(schema)
    collector.schema = deepcopy(schema.json_schema())
    local_schema._type_adapter = _DiagnosticAdapter(schema._type_adapter, collector)
    local_agent = copy(agent)
    local_agent.output_type = local_schema
    return local_agent


def sanitized_output_diagnostics(value: Any) -> dict[str, Any]:
    """Rebound internally-produced diagnostics for retained trace projection."""
    if not isinstance(value, Mapping) or value.get("schema") != "keystone.output_validation.v1":
        return {}
    failures = []
    for failure in list(value.get("failures") or [])[:_MAX_FAILURES]:
        if not isinstance(failure, Mapping):
            continue
        errors = []
        for error in list(failure.get("errors") or [])[:12]:
            if not isinstance(error, Mapping):
                continue
            code = error.get("type")
            detail: dict[str, Any] = {
                "type": code if code in _ERROR_TYPES else "custom_validation_error",
                "location": [
                    part
                    if (type(part) is int and 0 <= part <= 1_000_000)
                    or (isinstance(part, str) and (part in _PLACEHOLDERS or _NAME.fullmatch(part)))
                    else "<unknown_field>"
                    for part in list(error.get("location") or [])[:8]
                ],
            }
            validator = error.get("validator")
            if isinstance(validator, Mapping):
                path = validator.get("file")
                function = validator.get("function")
                line = validator.get("line")
                if (
                    isinstance(path, str)
                    and re.fullmatch(r"src/keystone_agents/schemas/[A-Za-z0-9_/]+\.py", path)
                    and isinstance(function, str)
                    and _NAME.fullmatch(function)
                    and type(line) is int
                    and 0 < line <= 1_000_000
                ):
                    detail["validator"] = {"file": path, "function": function, "line": line}
                    rule = error.get("rule_code")
                    if isinstance(rule, str) and any(
                        rule == known_rule and (path, function) == source[:2]
                        for source, known_rule in _REVIEWED_RULES.items()
                    ):
                        detail["rule_code"] = rule
            errors.append(detail)
        attempt = failure.get("attempt")
        name = failure.get("output_type")
        failures.append(
            {
                "attempt": attempt if type(attempt) is int and 0 < attempt <= 100 else 0,
                "output_type": name
                if isinstance(name, str) and _NAME.fullmatch(name)
                else "structured_output",
                "errors": errors,
                **(
                    {"status": "no_schema_diagnostic_observed"}
                    if failure.get("status") == "no_schema_diagnostic_observed"
                    else {}
                ),
            }
        )
    return {"schema": "keystone.output_validation.v1", "failures": failures}


def structured_output_retry_feedback(value: Any, *, attempt: int) -> str:
    """Explain only reviewed static rules or sanitized paths/types for this retry."""
    diagnostics = sanitized_output_diagnostics(value)
    errors = [
        error
        for failure in diagnostics.get("failures", [])
        if failure.get("attempt") == attempt
        for error in failure.get("errors", [])
    ]
    if not errors:
        return ""
    lines = [
        "Structured output correction under the existing schema:",
        "Return a corrected response for the same task using the original evidence, "
        "permissions, and retained tool-result receipts.",
    ]
    explained = set()
    for error in errors[:6]:
        path = ".".join(str(part) for part in error["location"]) or "root"
        lines.append(f"- {path}: {error['type']}.")
        rule = error.get("rule_code")
        if rule in _RULE_FEEDBACK and rule not in explained:
            lines.append(f"- {rule}: {_RULE_FEEDBACK[rule]}")
            explained.add(rule)
    lines.append("This correction does not authorize additional provider actions.")
    return "\n\n" + "\n".join(lines)[:4096]
