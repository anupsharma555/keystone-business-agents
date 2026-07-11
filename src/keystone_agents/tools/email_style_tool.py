"""Local-only email style profile loading tools."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from keystone_agents.guardrails import assess_text_guardrails, keystone_tool_guardrail_kwargs
from keystone_agents.schemas.approval import ApprovalScope, ApprovalState
from keystone_agents.schemas.email_style import (
    EmailStyleProfile,
    EmailStyleProfileBuildResult,
    EmailStyleSampleSummary,
)
from keystone_agents.sdk import function_tool
from keystone_agents.storage.sqlite_store import database_url_from_env, redact_secrets
from keystone_agents.tools.storage_tool import StorageTool

PROJECT_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_ROOT = PROJECT_ROOT / "tests" / "fixtures"
DEFAULT_EMAIL_STYLE_PROFILE = "sample_email_style_profile_anup_approved"
DEFAULT_SENT_EMAIL_STYLE_SAMPLES = "sample_sent_email_style_messages"
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s<>'\"`]+", re.I)
PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)\d{3}[-.\s]?\d{4}\b")
SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")
STYLE_PHRASES = (
    "compare notes",
    "non-sensitive context",
    "if useful",
    "happy to",
    "brief conversation",
    "a few times that work",
    "practical fit",
    "thanks for reaching out",
)
SALESY_AVOIDED_PHRASES = ("act now", "guaranteed", "proven results", "limited time")
STYLE_SAMPLE_EXCLUSION_FLAGS = {
    "finance_review",
    "long_or_forwarded_content",
    "possible_phi",
    "professional_advice",
    "quoted_or_forwarded_content",
    "secret",
    "security",
}
STYLE_SAMPLE_MAX_WORDS = 220


@dataclass(frozen=True)
class SentEmailStyleSample:
    """In-memory sent-email sample. Do not persist raw body."""

    source_id: str
    body: str
    subject: str = ""
    source_url: str = ""


def _resolve_fixture_path(fixture: str | Path, *, default_suffix: str = ".json") -> Path:
    raw_path = Path(fixture)
    candidates = [raw_path]
    if raw_path.suffix == "":
        candidates.append(raw_path.with_suffix(default_suffix))
        candidates.append(FIXTURE_ROOT / f"{raw_path.name}{default_suffix}")
    else:
        candidates.append(FIXTURE_ROOT / raw_path.name)

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Email style profile fixture not found: {fixture}")


def _approved_profile(profile: EmailStyleProfile) -> EmailStyleProfile:
    if not profile.approved_for_drafting:
        raise ValueError("Email style profile is not approved for drafting.")
    return profile


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\u2014", "-").split()).strip()


def _redact_sent_text(value: str) -> str:
    redacted = redact_secrets(value)
    redacted = EMAIL_RE.sub("[REDACTED_EMAIL]", redacted)
    redacted = URL_RE.sub("[REDACTED_URL]", redacted)
    redacted = PHONE_RE.sub("[REDACTED_PHONE]", redacted)
    return _clean_text(redacted)


def _sent_text_sensitive_flags(value: str) -> list[str]:
    assessment = assess_text_guardrails(value, check_outreach_claims=False)
    flags = list(assessment.risk_flags)
    if redact_secrets(value) != value:
        flags.append("secret")
    return list(dict.fromkeys(flags))


def _safe_subject(value: str) -> str:
    flags = _sent_text_sensitive_flags(value)
    if STYLE_SAMPLE_EXCLUSION_FLAGS.intersection(flags):
        return "[REDACTED_SUBJECT]"
    redacted = _redact_sent_text(value)
    return _clean_text(redacted)[:120]


def _safe_profile_notes(value: str) -> str:
    if not value:
        return ""
    flags = _sent_text_sensitive_flags(value)
    if STYLE_SAMPLE_EXCLUSION_FLAGS.intersection(flags):
        return "Profile notes redacted because they contained sensitive content."
    return _redact_sent_text(value)


def _summary(value: str, *, max_words: int = 26) -> str:
    del max_words
    redactions = []
    for flag in _sent_text_sensitive_flags(value):
        redactions.append(f"[REDACTED_{flag.upper()}]")
    if EMAIL_RE.search(value):
        redactions.append("[REDACTED_EMAIL]")
    if URL_RE.search(value):
        redactions.append("[REDACTED_URL]")
    if PHONE_RE.search(value):
        redactions.append("[REDACTED_PHONE]")
    if redact_secrets(value) != value:
        redactions.append("[REDACTED_SECRET]")
    redactions = list(dict.fromkeys(redactions))
    paragraphs = [paragraph for paragraph in value.split("\n\n") if paragraph.strip()]
    sentences = _sentences(value)
    words = _clean_text(value).split()
    return (
        f"{len(words)} words across {len(paragraphs) or 1} paragraphs and "
        f"{len(sentences) or 1} sentences; redactions: {', '.join(redactions) or 'none'}."
    )


def _body_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


def _sample_summary(sample: SentEmailStyleSample) -> EmailStyleSampleSummary:
    flags = _sent_text_sensitive_flags(f"{sample.subject}\n{sample.body}")
    if len(_clean_text(sample.body).split()) > STYLE_SAMPLE_MAX_WORDS:
        flags.append("long_or_forwarded_content")
    if re.search(
        r"(?:^|\n)(?:-{2,}\s*)?(?:forwarded message|original message|from:)\s*[:\-]?",
        sample.body,
        flags=re.I,
    ) or re.match(r"\s*fwd\s*:", sample.subject, flags=re.I):
        flags.append("quoted_or_forwarded_content")
    flags = list(dict.fromkeys(flags))
    return EmailStyleSampleSummary(
        source_id=sample.source_id,
        source_url=sample.source_url,
        subject_summary=_safe_subject(sample.subject),
        body_hash=_body_hash(sample.body),
        body_length=len(sample.body),
        redacted_summary=_summary(sample.body),
        sensitive_flags=flags,
        used_for_profile=not bool(STYLE_SAMPLE_EXCLUSION_FLAGS.intersection(flags)),
    )


def _extract_greeting(body: str) -> str | None:
    for line in body.splitlines():
        cleaned = _clean_text(line).strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered.startswith("hi "):
            return "Hi {name},"
        if lowered.startswith("hello "):
            return "Hello {name},"
        if lowered.startswith("dear "):
            return "Dear {name},"
        return None
    return None


def _extract_signoff(body: str) -> str | None:
    known = {
        "best": "Best,",
        "thanks": "Thanks,",
        "thank you": "Thank you,",
        "regards": "Regards,",
        "sincerely": "Sincerely,",
        "warmly": "Warmly,",
    }
    lines = [_clean_text(line).strip().rstrip(",") for line in body.splitlines()]
    for line in reversed([line for line in lines if line]):
        lowered = line.lower()
        if lowered in known:
            return known[lowered]
    inline_matches = list(
        re.finditer(
            r"\b(sincerely|thank\s+you|thanks|regards|warmly|best)\s*,?\b",
            body,
            flags=re.I,
        )
    )
    if inline_matches:
        return known[" ".join(inline_matches[-1].group(1).lower().split())]
    return None


def _sentences(body: str) -> list[str]:
    cleaned = _clean_text(body)
    return [sentence.strip() for sentence in SENTENCE_RE.split(cleaned) if sentence.strip()]


def _average_sentence_words(samples: list[SentEmailStyleSample]) -> int:
    counts = [len(sentence.split()) for sample in samples for sentence in _sentences(sample.body)]
    if not counts:
        return 16
    return max(4, min(40, round(sum(counts) / len(counts))))


def _sentence_length_label(average_words: int, samples: list[SentEmailStyleSample]) -> str:
    counts = [len(sentence.split()) for sample in samples for sentence in _sentences(sample.body)]
    if counts and max(counts) - min(counts) >= 16:
        return "varied"
    if average_words <= 14:
        return "short"
    return "medium"


def _cta_style(samples: list[SentEmailStyleSample]) -> str:
    combined = "\n".join(sample.body.lower() for sample in samples)
    if "few times" in combined or "calendar" in combined:
        return "calendar_offer"
    if "please send" in combined or "send any non-sensitive context" in combined:
        return "context_request"
    if combined.count("?") >= len(samples):
        return "direct_question"
    return "soft_question"


def _directness(samples: list[SentEmailStyleSample]) -> str:
    combined = "\n".join(sample.body.lower() for sample in samples)
    direct_markers = sum(
        combined.count(marker) for marker in ("please send", "can you", "could you")
    )
    if direct_markers >= len(samples):
        return "high"
    if "if useful" in combined or "happy to" in combined:
        return "medium"
    return "medium"


def _formality(samples: list[SentEmailStyleSample]) -> str:
    combined = "\n".join(sample.body.lower() for sample in samples)
    if "dear " in combined or "sincerely" in combined:
        return "formal"
    if "hey " in combined:
        return "casual"
    return "professional"


def _formatting_preferences(samples: list[SentEmailStyleSample]) -> list[str]:
    preferences = ["plain text"]
    paragraph_counts = [
        len([paragraph for paragraph in sample.body.split("\n\n") if paragraph.strip()])
        for sample in samples
    ]
    if paragraph_counts and sum(paragraph_counts) / len(paragraph_counts) >= 3:
        preferences.append("short paragraphs")
    return preferences


def _preferred_phrases(samples: list[SentEmailStyleSample]) -> list[str]:
    combined = "\n".join(sample.body.lower() for sample in samples)
    phrases = [phrase for phrase in STYLE_PHRASES if phrase in combined]
    if not phrases:
        return ["non-sensitive context"]
    return phrases[:6]


def _top_or_default(values: list[str], default: str) -> list[str]:
    if not values:
        return [default]
    ranked = [item for item, _ in Counter(values).most_common(3)]
    return ranked or [default]


def load_sent_email_style_samples_fixture(
    fixture: str | Path = DEFAULT_SENT_EMAIL_STYLE_SAMPLES,
) -> list[SentEmailStyleSample]:
    """Load local fake sent-email samples for style profiling tests and dry runs."""

    path = _resolve_fixture_path(fixture)
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        source = f"fixture://{path.name}"
        rows = data
    elif isinstance(data, dict):
        source = str(data.get("source") or f"fixture://{path.name}")
        rows = data.get("samples", [])
    else:
        raise ValueError("sent email style fixture must be a JSON object or list")
    if not isinstance(rows, list):
        raise ValueError("sent email style fixture samples must be a list")

    samples: list[SentEmailStyleSample] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            continue
        source_id = str(row.get("source_id") or f"fixture:sent_style:{index}")
        source_url = str(row.get("source_url") or source)
        if not source_url:
            source_url = f"fixture://{path.name}"
        samples.append(
            SentEmailStyleSample(
                source_id=source_id,
                source_url=source_url,
                subject=str(row.get("subject") or ""),
                body=str(row.get("body") or ""),
            )
        )
    return samples


def build_email_style_profile_from_samples(
    samples: list[SentEmailStyleSample],
    *,
    profile_id: str = "sent-default",
    source: str = "fixture",
    source_id: str = "fixture:sent_email_style_samples",
    source_url: str = "fixture://sent_email_style_samples",
    approval_state: ApprovalState | str = ApprovalState.PENDING,
    notes: str = "",
) -> EmailStyleProfileBuildResult:
    """Derive an aggregate profile from sent-email samples without retaining raw bodies."""

    if not samples:
        raise ValueError("At least one sent-email style sample is required.")
    summaries = [_sample_summary(sample) for sample in samples]
    usable = [
        sample
        for sample, summary in zip(samples, summaries, strict=True)
        if summary.used_for_profile
    ]
    if not usable:
        raise ValueError("No sent-email samples were safe enough for style profiling.")

    average_words = _average_sentence_words(usable)
    greetings = _top_or_default(
        [greeting for sample in usable if (greeting := _extract_greeting(sample.body))],
        "Hi {name},",
    )
    signoffs = _top_or_default(
        [signoff for sample in usable if (signoff := _extract_signoff(sample.body))],
        "Sincerely,",
    )
    excluded_count = len(samples) - len(usable)
    limitations = [
        "Raw sent-email bodies were used only in memory and are not included in output.",
        "Generated profiles default to pending and are ignored until approved_for_drafting.",
        "Style profiles guide drafts only; they do not send email or relax approval gates.",
    ]
    if excluded_count:
        limitations.append(
            f"{excluded_count} sent-email sample(s) were excluded because sensitivity "
            "or sample-quality guardrails flagged them."
        )
    profile = EmailStyleProfile(
        profile_id=profile_id,
        source=source,
        source_id=source_id,
        source_url=source_url,
        confidence=min(0.9, 0.45 + (0.08 * len(usable))),
        approval_state=approval_state,
        approval_scope=ApprovalScope.DRAFTING,
        greeting_patterns=greetings,
        signoffs=signoffs,
        sentence_length=_sentence_length_label(average_words, usable),
        average_sentence_words=average_words,
        directness=_directness(usable),
        cta_style=_cta_style(usable),
        formality=_formality(usable),
        formatting_preferences=_formatting_preferences(usable),
        preferred_phrases=_preferred_phrases(usable),
        avoided_phrases=list(SALESY_AVOIDED_PHRASES),
        approved_sample_snippets=[],
        notes=_safe_profile_notes(notes)
        or (
            "Derived aggregate sent-email style profile. Raw sent-email bodies were not "
            "stored; sample summaries are redacted."
        ),
        raw_sent_email_bodies_included=False,
        send_enabled=False,
        sent=False,
    )
    return EmailStyleProfileBuildResult(
        profile=profile,
        sample_summaries=summaries,
        sample_count=len(samples),
        usable_sample_count=len(usable),
        excluded_sample_count=excluded_count,
        limitations=limitations,
    )


def load_email_style_profile_fixture(
    fixture: str | Path = DEFAULT_EMAIL_STYLE_PROFILE,
) -> EmailStyleProfile:
    """Load one approved aggregate style profile from a local JSON fixture."""

    path = _resolve_fixture_path(fixture)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("source", f"fixture://{path.name}")
    data.setdefault("source_url", f"fixture://{path.name}")
    return _approved_profile(EmailStyleProfile.model_validate(data))


def load_email_style_profile_from_storage(
    profile_id: str = "default",
    *,
    database_url: str | None = None,
) -> EmailStyleProfile | None:
    """Load one approved aggregate style profile from local SQLite storage."""

    rows = StorageTool(database_url or database_url_from_env()).list_email_style_profiles(
        profile_id=profile_id,
        approved_only=True,
    )
    if not rows:
        return None
    return EmailStyleProfile.model_validate(rows[0])


def load_email_style_profile_model(
    profile: str | Path = DEFAULT_EMAIL_STYLE_PROFILE,
    *,
    database_url: str | None = None,
) -> EmailStyleProfile:
    """Load an approved style profile from local storage when available, else fixture."""

    profile_id = str(profile).strip()
    if database_url:
        stored = load_email_style_profile_from_storage(profile_id, database_url=database_url)
        if stored is not None:
            return stored
    return load_email_style_profile_fixture(profile)


@function_tool(**keystone_tool_guardrail_kwargs())
def load_email_style_profile(
    profile: str = DEFAULT_EMAIL_STYLE_PROFILE,
    database_url: str | None = None,
) -> str:
    """Load approved aggregate email style data from local fixture or SQLite only."""

    loaded = load_email_style_profile_model(profile, database_url=database_url)
    payload: dict[str, Any] = {
        "mode": "local_email_style_profile",
        "object_type": "email_style_profile",
        "approved_only": True,
        "send_enabled": False,
        "raw_sent_email_bodies_included": False,
        "profile": loaded.safe_prompt_context(),
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)
