"""Canonical URLs for the local Promptfoo eval dashboard."""

from __future__ import annotations

import os
from urllib.parse import quote

DEFAULT_DASHBOARD_URL = "http://127.0.0.1:8769/dashboard"


def eval_dashboard_url() -> str:
    """Return the canonical operator-facing dashboard URL."""
    value = os.environ.get("KEYSTONE_PROMPTFOO_DASHBOARD_URL", DEFAULT_DASHBOARD_URL)
    return str(value or DEFAULT_DASHBOARD_URL).strip().rstrip("/") or DEFAULT_DASHBOARD_URL


def eval_dashboard_case_url(case_id: str) -> str:
    """Return the canonical dashboard URL scoped to one eval case."""
    base_url = eval_dashboard_url()
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}case={quote(str(case_id or '').strip())}"


def eval_review_case_url(case_id: str) -> str:
    """Return the canonical standalone human-review intake URL for one eval case."""
    dashboard_url = eval_dashboard_url()
    if dashboard_url.endswith("/dashboard"):
        base_url = f"{dashboard_url[:-len('/dashboard')]}/review"
    else:
        base_url = dashboard_url.replace("/dashboard?", "/review?", 1)
    separator = "&" if "?" in base_url else "?"
    return f"{base_url}{separator}case={quote(str(case_id or '').strip())}"
