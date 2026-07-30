"""Reusable safety guardrails for Keystone agents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from keystone_agents.sdk import (
    Agent,
    GuardrailFunctionOutput,
    RunContextWrapper,
    ToolGuardrailFunctionOutput,
    ToolGuardrailViolation,
    ToolInputGuardrailData,
    ToolOutputGuardrailData,
    input_guardrail,
    load_prompt,
    output_guardrail,
    tool_input_guardrail,
    tool_output_guardrail,
)

DraftPolicy = Literal["normal", "acknowledge_only", "no_substantive_reply"]


@dataclass(frozen=True)
class GuardrailAssessment:
    """Deterministic guardrail result usable by fixture mode and SDK wrappers."""

    allowed: bool
    manual_review_required: bool
    risk_flags: tuple[str, ...]
    reasons: tuple[str, ...]
    draft_policy: DraftPolicy = "normal"


_PHI_PATTERNS = (
    re.compile(r"\b(?:mrn|medical record number)\b", re.IGNORECASE),
    re.compile(r"\b(?:dob|date of birth)\b", re.IGNORECASE),
    re.compile(r"\bnamed\s+patient\s+story\b", re.IGNORECASE),
    re.compile(
        r"\bpatient\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\b[^.\n]{0,180}\b"
        r"(?:diagnos(?:is|ed)|treatment|medication|therapy|depression|anxiety|bipolar|"
        r"schizophrenia|psychiatric)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:diagnos(?:is|ed)|treatment plan|medication|therapy)\b[^.\n]{0,180}\b"
        r"(?:for|of)\s+patient\b",
        re.IGNORECASE,
    ),
)
_LEGAL_PATTERNS = (
    re.compile(
        r"\b(?:contract|indemnification|nda|baa|hipaa agreement|terms|liability|"
        r"signature requested|sign(?:ature)? requested)\b",
        re.IGNORECASE,
    ),
)
_FINANCE_PATTERNS = (re.compile(r"\b(?:invoice|payment|wire|bank|ach|tax|w-?9)\b", re.IGNORECASE),)
_CREDENTIAL_CONTEXT_PATTERNS = (re.compile(r"\b(?:password|login|credential)\b", re.IGNORECASE),)
_SECURITY_PATTERNS = (
    re.compile(
        r"\b(?:verify account|urgent payment|suspicious link)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:click this link|account suspension|verify now)\b", re.IGNORECASE),
    re.compile(r"\bhttps?://[^\s]*(?:bit\.ly|tinyurl|login|verify|reset)[^\s]*", re.IGNORECASE),
)
_URGENT_PAYMENT_PATTERN = re.compile(
    r"\b(?:urgent|immediate|immediately|today)\b[^\n.]{0,180}"
    r"\b(?:payment|wire|bank|ach|credential|login)\b",
    re.IGNORECASE,
)
_ADVICE_PATTERNS = (
    re.compile(r"\b(?:medical|legal|tax|regulatory)\s+advice\b", re.IGNORECASE),
    re.compile(r"\blegal\s+position\b", re.IGNORECASE),
    re.compile(r"\b(?:prescribe|treatment plan|liability opinion|tax strategy)\b", re.IGNORECASE),
    re.compile(
        r"\bdiagnose\b[^.\n]{0,120}\b(?:patient|depression|anxiety|bipolar|"
        r"schizophrenia|psychiatric|medical|condition|illness|disorder)\b",
        re.IGNORECASE,
    ),
)
_NEGATED_ADVICE_CONTEXT_RE = re.compile(
    r"\b(?:not|no|never|cannot|can't|do not|don't|without|avoid)\b[^.\n]{0,120}$",
    re.IGNORECASE,
)
_UNSUPPORTED_CLAIM_PATTERNS = (
    re.compile(r"\bwe have helped voice-?ai teams\b", re.IGNORECASE),
    re.compile(r"\bwe have worked with companies like yours\b", re.IGNORECASE),
    re.compile(r"\bproven results\b", re.IGNORECASE),
    re.compile(r"\bguaranteed roi\b", re.IGNORECASE),
    re.compile(r"\bprior client claims?\b", re.IGNORECASE),
    re.compile(
        r"\b(?:keystone|we)\s+(?:have\s+|has\s+)?"
        r"(?:helped|worked with|partnered with|delivered|reduced|improved|increased|saved)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:our\s+(?:clients|customers|case studies|track record)|"
        r"clients|case studies|track record)\b",
        re.IGNORECASE,
    ),
)
_NEGATED_UNSUPPORTED_CLAIM_CONTEXT_RE = re.compile(
    r"\b(?:do not|don't|never|must not|avoid|without|absent|forbidden)\b[^.\n]{0,140}$",
    re.IGNORECASE,
)
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}", re.IGNORECASE),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_-]?key|secret|token|authorization)\s*[:=]\s*"
        r"['\"]?[A-Za-z0-9_./+=-]{8,}",
        re.IGNORECASE,
    ),
)
_SEND_LIKE_PATTERNS = (
    re.compile(r"\bsend_email\b", re.IGNORECASE),
    re.compile(r"\bgmail\.users\.messages\.send\b", re.IGNORECASE),
    re.compile(r"/send\b", re.IGNORECASE),
    re.compile(r"\bauto[-_ ]?send\b", re.IGNORECASE),
    re.compile(r"\b(?:send|auto_send|send_enabled)\s*[:=]\s*(?:true|1|yes)\b", re.IGNORECASE),
    re.compile(
        r"\b(?:send|deliver)\s+(?:the\s+)?(?:email|outbound|message)\s+"
        r"(?:now|automatically)\b",
        re.IGNORECASE,
    ),
)
_OUTREACH_CLAIM_SCAN_SKIP_KEYS = {
    "avoided_phrases",
    "blocked_facts",
    "copy_must_not_contain",
    "customers",
    "forbidden_terms",
    "tone_forbidden_terms",
    "unsupported_claim_explanations",
    "unsupported_claims_flagged",
}
_SEND_LIKE_SCAN_SKIP_KEYS = {
    "blocked_actions",
    "blocked_side_effects",
    "forbidden_actions",
    "forbidden_side_effects",
    "no_send_policy",
    "safety_gates_applied",
    "safety_notes",
    "safety_policy",
}
_OUTREACH_CLAIM_AGENT_NAMES = {"outreach_composer"}


def stringify_payload(value: Any) -> str:
    """Convert SDK inputs or Pydantic outputs to text for deterministic scanning."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(f"{key}: {stringify_payload(item)}" for key, item in value.items())
    if isinstance(value, list | tuple | set):
        return "\n".join(stringify_payload(item) for item in value)
    if hasattr(value, "model_dump"):
        return stringify_payload(value.model_dump())
    public_fields = {
        name: getattr(value, name)
        for name in (
            "subject",
            "body",
            "email_body",
            "linkedin_note",
            "summary",
            "reasoning",
            "recommended_action",
            "draft_reply",
            "risk_flags",
        )
        if hasattr(value, name)
    }
    if public_fields:
        return stringify_payload(public_fields)
    return str(value)


def stringify_payload_for_outreach_claim_scan(value: Any) -> str:
    """Stringify payload content that represents asserted outreach copy or claims."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(
            f"{key}: {stringify_payload_for_outreach_claim_scan(item)}"
            for key, item in value.items()
            if key not in _OUTREACH_CLAIM_SCAN_SKIP_KEYS
        )
    if isinstance(value, list | tuple | set):
        return "\n".join(stringify_payload_for_outreach_claim_scan(item) for item in value)
    if hasattr(value, "model_dump"):
        return stringify_payload_for_outreach_claim_scan(value.model_dump())
    return stringify_payload(value)


def stringify_payload_for_send_like_scan(value: Any) -> str:
    """Stringify payload fields that can represent actual send-like side effects."""

    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(
            f"{key}: {stringify_payload_for_send_like_scan(item)}"
            for key, item in value.items()
            if key not in _SEND_LIKE_SCAN_SKIP_KEYS
        )
    if isinstance(value, list | tuple | set):
        return "\n".join(stringify_payload_for_send_like_scan(item) for item in value)
    if hasattr(value, "model_dump"):
        return stringify_payload_for_send_like_scan(value.model_dump())
    return stringify_payload(value)


def _should_check_sdk_outreach_claims(agent: Any | None) -> bool:
    """Limit SDK outreach-claim scans to agents that generate outreach copy."""

    if agent is None:
        return True
    return str(getattr(agent, "name", "")).strip().lower() in _OUTREACH_CLAIM_AGENT_NAMES


def _profile_and_context(input_context: str = "") -> str:
    try:
        profile = load_prompt("keystone_profile.md")
    except Exception:
        profile = ""
    return "\n".join([profile, input_context])


def assess_unsupported_outreach_claims(
    text: str,
    *,
    input_context: str = "",
) -> tuple[str, ...]:
    """Return unsupported outreach claim snippets not present in profile or caller context."""

    allowed_context = _profile_and_context(input_context).lower()
    flagged: list[str] = []
    for pattern in _UNSUPPORTED_CLAIM_PATTERNS:
        for match in pattern.finditer(text):
            claim = match.group(0)
            if _NEGATED_UNSUPPORTED_CLAIM_CONTEXT_RE.search(text[: match.start()]):
                continue
            if claim.lower() not in allowed_context:
                flagged.append(claim)
    return tuple(dict.fromkeys(flagged))


def assess_text_guardrails(
    text: str,
    *,
    input_context: str = "",
    check_outreach_claims: bool = True,
) -> GuardrailAssessment:
    """Run all Keystone safety guardrails against text."""

    risk_flags: list[str] = []
    reasons: list[str] = []
    draft_policy: DraftPolicy = "normal"

    if any(pattern.search(text) for pattern in _PHI_PATTERNS):
        risk_flags.append("possible_phi")
        reasons.append("possible PHI or patient-specific content")
        draft_policy = "no_substantive_reply"

    if any(pattern.search(text) for pattern in _LEGAL_PATTERNS):
        risk_flags.append("legal_review")
        reasons.append("legal or contract content")
        if draft_policy == "normal":
            draft_policy = "acknowledge_only"

    finance_detected = any(pattern.search(text) for pattern in _FINANCE_PATTERNS)
    if finance_detected:
        risk_flags.append("finance_review")
        reasons.append("financial content")

    credential_context_detected = any(
        pattern.search(text) for pattern in _CREDENTIAL_CONTEXT_PATTERNS
    )
    if credential_context_detected:
        risk_flags.append("credential_context")
        reasons.append("credential or login context")
        if draft_policy == "normal":
            draft_policy = "acknowledge_only"

    security_detected = any(pattern.search(text) for pattern in _SECURITY_PATTERNS)
    if security_detected or (finance_detected and _URGENT_PAYMENT_PATTERN.search(text)):
        risk_flags.append("security")
        reasons.append("security or suspicious request")
        draft_policy = "no_substantive_reply"

    if _contains_unnegated_professional_advice(text):
        risk_flags.append("professional_advice")
        reasons.append("medical, legal, tax, or regulatory advice")
        draft_policy = "no_substantive_reply"

    unsupported_claims = (
        assess_unsupported_outreach_claims(text, input_context=input_context)
        if check_outreach_claims
        else ()
    )
    if unsupported_claims:
        risk_flags.append("unsupported_claim")
        reasons.extend(f"unsupported outreach claim: {claim}" for claim in unsupported_claims)

    risk_flags = list(dict.fromkeys(risk_flags))
    reasons = list(dict.fromkeys(reasons))
    blocking_flags = {"possible_phi", "security", "professional_advice", "unsupported_claim"}
    allowed = not bool(blocking_flags.intersection(risk_flags))

    return GuardrailAssessment(
        allowed=allowed,
        manual_review_required=bool(risk_flags),
        risk_flags=tuple(risk_flags),
        reasons=tuple(reasons),
        draft_policy=draft_policy,
    )


def assess_tool_payload_guardrails(
    tool_name: str,
    payload: Any,
    *,
    output: bool = False,
) -> GuardrailAssessment:
    """Assess a tool input or output before external side effects or data exposure."""

    text = f"tool_name: {tool_name}\n{stringify_payload_for_send_like_scan(payload)}"
    claim_scan_text = (
        f"tool_name: {tool_name}\n{stringify_payload_for_outreach_claim_scan(payload)}"
    )
    assessment = assess_text_guardrails(
        claim_scan_text,
        check_outreach_claims=not output,
    )
    risk_flags = list(assessment.risk_flags)
    reasons = list(assessment.reasons)

    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        risk_flags.append("secret")
        reasons.append("secret-like content")

    if any(pattern.search(text) for pattern in _SEND_LIKE_PATTERNS):
        risk_flags.append("send_like_action")
        reasons.append("send-like action")

    risk_flags = list(dict.fromkeys(risk_flags))
    reasons = list(dict.fromkeys(reasons))
    blocking_flags = {
        "possible_phi",
        "professional_advice",
        "secret",
        "security",
        "send_like_action",
        "unsupported_claim",
    }

    return GuardrailAssessment(
        allowed=not bool(blocking_flags.intersection(risk_flags)),
        manual_review_required=bool(risk_flags),
        risk_flags=tuple(risk_flags),
        reasons=tuple(reasons),
        draft_policy=assessment.draft_policy,
    )


def _tool_rejection_message(tool_name: str, assessment: GuardrailAssessment) -> str:
    reasons = ", ".join(assessment.reasons) or "safety policy"
    return f"Tool '{tool_name}' blocked by Keystone safety guardrails: {reasons}."


def _tool_guardrail_output(
    *,
    tool_name: str,
    payload: Any,
    output: bool = False,
) -> ToolGuardrailFunctionOutput:
    assessment = assess_tool_payload_guardrails(tool_name, payload, output=output)
    info = {
        "tool_name": tool_name,
        "risk_flags": assessment.risk_flags,
        "reasons": assessment.reasons,
        "output": output,
    }
    if assessment.allowed:
        return ToolGuardrailFunctionOutput.allow(info)
    return ToolGuardrailFunctionOutput.reject_content(
        _tool_rejection_message(tool_name, assessment),
        info,
    )


def _tool_name_from_data(data: ToolInputGuardrailData | ToolOutputGuardrailData) -> str:
    return str(getattr(data.context, "tool_name", "") or "tool")


def _tool_input_from_data(data: ToolInputGuardrailData) -> Any:
    value = getattr(data.context, "tool_input", None)
    if value is not None:
        return value
    return getattr(data.context, "tool_arguments", "")


@tool_input_guardrail(name="keystone_tool_input_safety")
def keystone_tool_input_guardrail(data: ToolInputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Reject unsafe tool inputs before external reads, writes, drafts, or posts."""

    return _tool_guardrail_output(
        tool_name=_tool_name_from_data(data),
        payload=_tool_input_from_data(data),
        output=False,
    )


@tool_output_guardrail(name="keystone_tool_output_safety")
def keystone_tool_output_guardrail(data: ToolOutputGuardrailData) -> ToolGuardrailFunctionOutput:
    """Reject unsafe tool outputs before they are returned to an agent."""

    return _tool_guardrail_output(
        tool_name=_tool_name_from_data(data),
        payload=data.output,
        output=True,
    )


def keystone_tool_guardrails() -> dict[str, list[Any]]:
    """Return SDK-compatible tool input and output guardrails."""

    return {
        "input": [keystone_tool_input_guardrail],
        "output": [keystone_tool_output_guardrail],
    }


def keystone_tool_guardrail_kwargs() -> dict[str, list[Any]]:
    """Return keyword arguments for `function_tool` with Keystone tool guardrails."""

    guardrails = keystone_tool_guardrails()
    return {
        "tool_input_guardrails": guardrails["input"],
        "tool_output_guardrails": guardrails["output"],
    }


def enforce_tool_input_guardrails(tool_name: str, payload: Any) -> None:
    """Run deterministic local tool input guardrails for direct tool calls."""

    assessment = assess_tool_payload_guardrails(tool_name, payload, output=False)
    if not assessment.allowed:
        raise ToolGuardrailViolation(_tool_rejection_message(tool_name, assessment))


def enforce_tool_output_guardrails(tool_name: str, output: Any) -> Any:
    """Run deterministic local tool output guardrails for direct tool calls."""

    assessment = assess_tool_payload_guardrails(tool_name, output, output=True)
    if not assessment.allowed:
        raise ToolGuardrailViolation(_tool_rejection_message(tool_name, assessment))
    return output


def enforce_public_source_output_guardrails(tool_name: str, output: Any) -> Any:
    """Allow public source evidence while still blocking secrets and PHI."""

    text = f"tool_name: {tool_name}\n{stringify_payload(output)}"
    risk_flags: list[str] = []
    reasons: list[str] = []
    if any(pattern.search(text) for pattern in _PHI_PATTERNS):
        risk_flags.append("possible_phi")
        reasons.append("possible PHI or patient-specific content")
    if any(pattern.search(text) for pattern in _SECRET_PATTERNS):
        risk_flags.append("secret")
        reasons.append("secret-like content")
    if risk_flags:
        assessment = GuardrailAssessment(
            allowed=False,
            manual_review_required=True,
            risk_flags=tuple(dict.fromkeys(risk_flags)),
            reasons=tuple(dict.fromkeys(reasons)),
            draft_policy="no_substantive_reply",
        )
        raise ToolGuardrailViolation(_tool_rejection_message(tool_name, assessment))
    return output


def acknowledgement_only_reply(sender_name: str = "") -> str:
    """Return a non-substantive acknowledgement for legal or contract content."""

    greeting = f"Hi {sender_name}," if sender_name else "Hi,"
    return (
        f"{greeting}\n\n"
        "Thanks for sending this. I received it and will review it before responding further.\n\n"
        "Best,\nKeystone"
    )


def _contains_unnegated_professional_advice(text: str) -> bool:
    for pattern in _ADVICE_PATTERNS:
        for match in pattern.finditer(text):
            if _NEGATED_ADVICE_CONTEXT_RE.search(text[: match.start()]):
                continue
            return True
    return False


@input_guardrail(name="keystone_input_safety", run_in_parallel=False)
def keystone_input_guardrail(
    ctx: RunContextWrapper[None],
    agent: Agent,
    input: str | list[Any],
) -> GuardrailFunctionOutput:
    assessment = assess_text_guardrails(
        stringify_payload(input),
        check_outreach_claims=False,
    )
    return GuardrailFunctionOutput(
        output_info={"risk_flags": assessment.risk_flags, "reasons": assessment.reasons},
        tripwire_triggered=not assessment.allowed,
    )


@output_guardrail(name="keystone_output_safety")
def keystone_output_guardrail(
    ctx: RunContextWrapper[None],
    agent: Agent,
    output: Any,
) -> GuardrailFunctionOutput:
    check_outreach_claims = _should_check_sdk_outreach_claims(agent)
    scan_text = (
        stringify_payload_for_outreach_claim_scan(output)
        if check_outreach_claims
        else stringify_payload(output)
    )
    assessment = assess_text_guardrails(
        scan_text,
        check_outreach_claims=check_outreach_claims,
    )
    return GuardrailFunctionOutput(
        output_info={"risk_flags": assessment.risk_flags, "reasons": assessment.reasons},
        tripwire_triggered=not assessment.allowed,
    )


@output_guardrail(name="keystone_internal_artifact_output_safety")
def keystone_internal_artifact_output_guardrail(
    ctx: RunContextWrapper[None],
    agent: Agent,
    output: Any,
) -> GuardrailFunctionOutput:
    """Apply safety checks without treating an internal brief as outbound copy."""

    assessment = assess_text_guardrails(
        stringify_payload(output),
        check_outreach_claims=False,
    )
    return GuardrailFunctionOutput(
        output_info={"risk_flags": assessment.risk_flags, "reasons": assessment.reasons},
        tripwire_triggered=not assessment.allowed,
    )


def keystone_guardrails(
    *,
    internal_artifact: bool = False,
) -> dict[str, list[Any]]:
    """Return SDK-compatible guardrails for agent construction."""

    return {
        "input": [keystone_input_guardrail],
        "output": [
            (
                keystone_internal_artifact_output_guardrail
                if internal_artifact
                else keystone_output_guardrail
            )
        ],
    }
