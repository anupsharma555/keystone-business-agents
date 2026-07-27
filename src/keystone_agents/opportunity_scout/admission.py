"""Shared admission signals for formal Opportunity Scout requests."""

from __future__ import annotations

import re


def requested_formal_opportunity_kinds(request_text: str) -> set[str]:
    """Return the bounded formal-program kinds named by the operator."""

    lower = str(request_text or "").lower()
    kinds: set[str] = set()
    if re.search(r"\b(?:grants?|nofo|foa|rfa|funding opportunity|award)\b", lower):
        kinds.add("grant")
    if re.search(r"\b(?:rfps?|request\s+for\s+proposals?|solicitations?|procurement)\b", lower):
        kinds.add("rfp")
    if re.search(r"\b(?:pilot(?:s| programs?)?|demonstrations?|challenge)\b", lower):
        kinds.add("pilot")
    if re.search(r"\b(?:accelerators?|incubators?)\b", lower):
        kinds.add("accelerator")
    if re.search(r"\b(?:call[- ]for[- ]proposals?|calls?\s+for\s+proposals|cfps?)\b", lower):
        kinds.add("call_for_proposals")
    if re.search(r"\bcalls?\s+for\s+applications\b", lower):
        kinds.add("call_for_applications")
    return kinds


def official_program_source_required(request_text: str) -> bool:
    """Return whether the operator explicitly requires an official source."""

    return bool(
        re.search(
            r"\b(?:official|program[- ]owned|sponsor[- ]owned)\s+"
            r"(?:program\s+)?sources?\b",
            str(request_text or ""),
            flags=re.I,
        )
    )
