"""Parse bounded operator-supplied facts for draft-only outreach."""

from __future__ import annotations

import re
from dataclasses import dataclass

INLINE_OUTREACH_FACT_LABELS = (
    "Approved inline context",
    "Approved source context",
    "Approved context",
    "Context approved for drafting",
    "Approved facts",
    "Approved evidence",
    "Approved background",
    "Approved grounding",
    "Source-backed facts",
    "Source backed facts",
    "Source-backed context",
    "Source backed context",
    "Source-backed evidence",
    "Source backed evidence",
    "Source context",
    "Facts",
    "Context",
    "Evidence",
    "Background",
    "Grounding",
    "Rationale",
)

_FACT_NOUN = r"(?:context|facts|evidence|background|grounding|rationale)"
_DIRECT_AUTHORITY_LABEL_RE = re.compile(
    rf"\b(?P<label>(?:"
    rf"(?:these\s+|the\s+following\s+)?approved"
    rf"(?:\s+(?:inline|source|source-backed|source\s+backed)){{0,2}}\s+{_FACT_NOUN}"
    rf"|source[-\s]+backed\s+{_FACT_NOUN}"
    rf"|context\s+approved\s+for\s+(?:drafting|draft-only\s+use|draft\s+only\s+use)"
    rf"))\s*:",
    flags=re.I,
)
_USE_AUTHORITY_RE = re.compile(
    rf"\b(?P<label>(?:use|using)\s+(?:only\s+)?"
    rf"(?:this\s+|these\s+|those\s+|the\s+following\s+)?"
    rf"(?:operator[-\s]+)?(?:"
    rf"approved(?:\s+(?:inline|source|source-backed|source\s+backed)){{0,2}}"
    rf"|supplied|provided)\s+{_FACT_NOUN})\b",
    flags=re.I,
)
_BARE_FACT_LABEL_RE = re.compile(
    rf"\b(?P<label>{_FACT_NOUN})\s*:",
    flags=re.I,
)
_COMPOSITION_SEPARATOR_RE = re.compile(
    r"\b(?:draft|write|compose|prepare)\b[^:]{0,240}:\s*",
    flags=re.I,
)
_NEGATED_AUTHORITY_RE = re.compile(
    r"\b(?:not|never)\s+(?:yet\s+)?"
    r"(?:(?:these|the\s+following)\s+)?(?:approved|source[-\s]+backed)\b",
    flags=re.I,
)
_QUOTED_CONTROL_RE = re.compile(
    r"\b(?:quoted\s+(?:source\s+)?|source\s+)(?:control\s+)?instructions?\s*:"
    r"|\b(?:ignore|disregard|override)\b[^.;]{0,100}\b(?:prior|previous|system)?\s*"
    r"(?:instructions?|rules?|policy)\b",
    flags=re.I,
)
_FIELD_STOP_RE = re.compile(
    r"\s+\b(?:goal|ask|body|caveats?|constraints?|instructions?|"
    r"target\s+(?:recipient|contact|company|organization)|recipient(?:\s+contact)?|"
    r"company|organization|sources?|citations?)\s*:",
    flags=re.I,
)
_CONTROL_SEGMENT_RE = re.compile(
    r"^(?:the\s+desired\s+response\s+is\b|could\s+you\b|can\s+you\b|"
    r"would\s+you\b|please\b|i\s+just\s+need\b|give\s+me\b|"
    r"(?:write|draft|compose|prepare|return|invite|keep)\b|"
    r"caveats?\b|constraints?\b|instructions?\b|"
    r"(?:do\s+not|don't|dont|never|without)\b)",
    flags=re.I,
)
_DIRECT_AUTHORITY_CONNECTOR_RE = re.compile(
    r"\b(?:with|using|use|from)\s*$|\bbased\s+on\s*$",
    flags=re.I,
)
_REPORTED_AUTHORITY_ORIGIN_RE = re.compile(
    r"\b(?:quoted\s+source(?:\s+text)?|source(?:\s+text)?|example(?:\s+text)?|"
    r"document|message)\b[^.!?;\n]{0,80}\b"
    r"(?:says?|states?|reads?|contains?|quotes?|shows?)\b[^.!?;\n]{0,80}$",
    flags=re.I,
)
_TRAILING_CONTROL_CLAUSE_RE = re.compile(
    r"\s+(?P<control>"
    r"(?:keep\s+(?:it\s+)?|make\s+(?:it\s+)?)?draft[- ]only\b"
    r"|(?:please\s+)?(?:do\s+not|don't|dont|never)\s+"
    r"(?:post|send|publish|share|save|create|modify|update|write|access)\b"
    r")",
    flags=re.I,
)


@dataclass(frozen=True, slots=True)
class InlineOutreachFactPacket:
    """Content-only facts plus the explicit authority that admitted them."""

    authority_kind: str
    authority_text: str
    source_label: str
    fact_block: str
    facts: tuple[str, ...]


def parse_inline_outreach_fact_packet(text: str) -> InlineOutreachFactPacket | None:
    """Return an explicitly authorized fact packet without granting an action."""

    cleaned = normalize_inline_outreach_request_text(text)
    if not cleaned:
        return None

    authority_candidate = _first_valid_authority(cleaned)
    if authority_candidate is None:
        return None
    authority_form, authority_match = authority_candidate

    fact_start: int | None = None
    if authority_form == "direct":
        fact_start = authority_match.end()
    else:
        adjacent_colon = re.match(r"\s*:\s*", cleaned[authority_match.end() :])
        if adjacent_colon is not None:
            fact_start = authority_match.end() + adjacent_colon.end()
        else:
            bare_label = _BARE_FACT_LABEL_RE.search(cleaned, authority_match.end())
            if bare_label is not None:
                fact_start = bare_label.end()
            else:
                separator = _COMPOSITION_SEPARATOR_RE.search(
                    cleaned,
                    authority_match.end(),
                )
                if separator is not None:
                    fact_start = separator.end()
    if fact_start is None:
        return None

    raw_fact_block = cleaned[fact_start:].strip(" .;,:")
    if not raw_fact_block or _QUOTED_CONTROL_RE.search(raw_fact_block):
        return None
    field_stop = _FIELD_STOP_RE.search(raw_fact_block)
    if field_stop is not None:
        raw_fact_block = raw_fact_block[: field_stop.start()].strip(" .;,:")
    facts = _split_facts(raw_fact_block)
    if len(re.findall(r"[A-Za-z][A-Za-z'-]*", " ".join(facts))) < 5:
        return None

    authority_text = authority_match.group("label").strip()
    authority_lower = authority_text.lower()
    if "source-backed" in authority_lower or "source backed" in authority_lower:
        authority_kind = "source_backed"
    elif "approved" in authority_lower:
        authority_kind = "operator_approved"
    else:
        authority_kind = "operator_supplied_draft_only"
    return InlineOutreachFactPacket(
        authority_kind=authority_kind,
        authority_text=authority_text,
        source_label=authority_text,
        fact_block="; ".join(facts),
        facts=tuple(facts),
    )


def _unwrap_outer_transport_quotes(text: str) -> str:
    """Ignore one CLI/Slack transport quote pair, not embedded quoted content."""

    quote_pairs = {'"': '"', "“": "”"}
    expected_closer = quote_pairs.get(text[:1])
    if expected_closer is not None and text.endswith(expected_closer):
        return text[1:-1].strip()
    return text


def normalize_inline_outreach_request_text(text: str) -> str:
    """Return a semantic copy while preserving lines and removing one transport wrapper."""

    return _unwrap_outer_transport_quotes(_normalize_layout(text))


def _normalize_layout(text: str) -> str:
    """Normalize horizontal spacing while retaining meaningful line boundaries."""

    normalized = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    normalized = re.sub(r"[\t\f\v ]+", " ", normalized)
    normalized = re.sub(r" *\n *", "\n", normalized)
    return re.sub(r"\n{3,}", "\n\n", normalized)


def _first_valid_authority(text: str) -> tuple[str, re.Match[str]] | None:
    candidates = [
        *(('direct', match) for match in _DIRECT_AUTHORITY_LABEL_RE.finditer(text)),
        *(("use", match) for match in _USE_AUTHORITY_RE.finditer(text)),
    ]
    for authority_form, match in sorted(candidates, key=lambda item: item[1].start()):
        if (
            _authority_is_negated(text, match)
            or _inside_authority_quotes(text, match.start())
            or _authority_is_reported_source_text(text, match)
        ):
            continue
        if authority_form == "direct" and not _direct_authority_has_operator_origin(
            text,
            match,
        ):
            continue
        if authority_form == "use":
            authority_lower = match.group("label").lower()
            if (
                "approved" not in authority_lower
                and " only " not in f" {authority_lower} "
            ):
                continue
        return authority_form, match
    return None


def _direct_authority_has_operator_origin(text: str, match: re.Match[str]) -> bool:
    prefix = text[: match.start()].rstrip()
    return bool(
        not prefix
        or prefix.endswith((".", ";", ",", ":", "!", "?"))
        or _DIRECT_AUTHORITY_CONNECTOR_RE.search(prefix)
    )


def _authority_is_reported_source_text(text: str, match: re.Match[str]) -> bool:
    prefix = text[max(0, match.start() - 180) : match.start()].rstrip()
    return bool(_REPORTED_AUTHORITY_ORIGIN_RE.search(prefix))


def _authority_is_negated(text: str, match: re.Match[str]) -> bool:
    window = text[max(0, match.start() - 48) : match.end()]
    return bool(_NEGATED_AUTHORITY_RE.search(window))


def _inside_authority_quotes(text: str, position: int) -> bool:
    prefix = text[:position]
    straight = len(re.findall(r'(?<!\\)"', prefix)) % 2 == 1
    smart = prefix.count("“") > prefix.count("”")
    backtick = len(re.findall(r"(?<!\\)`", prefix)) % 2 == 1
    return straight or smart or backtick


def _split_facts(text: str) -> list[str]:
    facts: list[str] = []
    for raw_fact in re.split(
        r"\s*(?:;|\n| \d+[\).] | - |(?<=[.!?])\s+)\s*",
        str(text or ""),
    ):
        candidate = raw_fact.strip(" .;")
        if not candidate:
            continue
        if _CONTROL_SEGMENT_RE.match(candidate):
            break
        fact = _strip_trailing_control_clause(candidate).strip(" .;")
        if len(fact) < 12:
            continue
        facts.append(fact[:360])
        if len(facts) >= 6:
            break
    return list(dict.fromkeys(facts))


def _strip_trailing_control_clause(text: str) -> str:
    """Remove a trailing draft/delivery instruction without erasing factual negation."""

    for match in _TRAILING_CONTROL_CLAUSE_RE.finditer(text):
        if _inside_authority_quotes(text, match.start()):
            continue
        control = match.group("control").strip()
        lowered = control.lower()
        explicit_directive = lowered.startswith(("please ", "keep ", "make "))
        implied_sentence_boundary = bool(control[:1].isupper())
        if not explicit_directive and not implied_sentence_boundary:
            # Lowercase negation inside an already-started segment is declarative or
            # ambiguous source text. Preserve it verbatim rather than guessing from
            # the preceding noun or silently turning a fragment into a supported fact.
            continue
        return text[: match.start()].rstrip(" .;,:")
    return text


__all__ = [
    "INLINE_OUTREACH_FACT_LABELS",
    "InlineOutreachFactPacket",
    "normalize_inline_outreach_request_text",
    "parse_inline_outreach_fact_packet",
]
