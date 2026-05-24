"""Optional backend-browser diagnostics helpers."""

from __future__ import annotations

import json
import time
from collections import Counter
from typing import Any

from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.sdk import function_tool
from keystone_agents.tools.playwright_tool import (
    DEFAULT_PLAYWRIGHT_MAX_TEXT_CHARS,
    DEFAULT_PLAYWRIGHT_TIMEOUT_SECONDS,
    PLAYWRIGHT_ENABLED_ENV,
    _blocked_payload,
    _bounded_text,
    _bounded_timeout_ms,
    _enabled,
    _is_missing_browser_error,
    _safe_image_path,
    _valid_http_url,
)

DEFAULT_BROWSER_DIAGNOSTIC_MAX_EVENTS = 100


def _bounded_events(events: list[dict[str, Any]], max_events: int) -> list[dict[str, Any]]:
    try:
        limit = int(max_events or DEFAULT_BROWSER_DIAGNOSTIC_MAX_EVENTS)
    except (TypeError, ValueError):
        limit = DEFAULT_BROWSER_DIAGNOSTIC_MAX_EVENTS
    return events[: max(1, min(limit, 300))]


def _launch_backend_chromium(playwright: Any) -> tuple[Any, str]:
    browser_channel = "playwright-chromium"
    try:
        return playwright.chromium.launch(headless=True), browser_channel
    except Exception as exc:
        if not _is_missing_browser_error(exc):
            raise
        return playwright.chromium.launch(headless=True, channel="chrome"), "local-chrome"


def capture_browser_diagnostics_impl(
    url: str,
    *,
    timeout_seconds: int = DEFAULT_PLAYWRIGHT_TIMEOUT_SECONDS,
    live: bool = False,
    max_events: int = DEFAULT_BROWSER_DIAGNOSTIC_MAX_EVENTS,
    capture_screenshot: bool = False,
    max_text_chars: int = DEFAULT_PLAYWRIGHT_MAX_TEXT_CHARS,
) -> dict[str, Any]:
    """Capture read-only console/network diagnostics for one HTTP(S) page."""

    normalized_url = str(url or "").strip()
    if not _valid_http_url(normalized_url):
        return _blocked_payload(
            normalized_url, reason="Only http/https URLs are allowed.", live=live
        )
    timeout_ms = _bounded_timeout_ms(timeout_seconds)
    if not live:
        return {
            "provider": "playwright",
            "diagnostic_type": "browser_diagnostics",
            "url": normalized_url,
            "final_url": normalized_url,
            "status": "dry-run",
            "title": "",
            "status_code": 0,
            "console_messages": [],
            "page_errors": [],
            "failed_requests": [],
            "network_summary": {
                "request_count": 0,
                "response_count": 0,
                "failed_request_count": 0,
                "status_buckets": {},
                "resource_type_counts": {},
            },
            "text_sample": "",
            "latency_ms": 0,
            "error": None,
            "metadata": {
                "timeout_seconds": timeout_ms // 1000,
                "read_only": True,
                "backend_browser": True,
                "browser_launched": False,
                "persistent_profile": False,
                "screenshot_path": None,
            },
            "send_enabled": False,
        }
    if not _enabled():
        return _blocked_payload(
            normalized_url,
            reason=f"{PLAYWRIGHT_ENABLED_ENV}=true is required for live Playwright diagnostics.",
            live=live,
        )

    started_at = time.perf_counter()
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        return _blocked_payload(
            normalized_url,
            reason=f"Playwright is not installed: {exc}",
            live=live,
        )

    console_messages: list[dict[str, Any]] = []
    page_errors: list[dict[str, Any]] = []
    failed_requests: list[dict[str, Any]] = []
    response_statuses: Counter[str] = Counter()
    resource_types: Counter[str] = Counter()
    request_count = 0
    response_count = 0
    browser = None

    def record_console(message: Any) -> None:
        console_messages.append(
            {
                "type": str(getattr(message, "type", "") or ""),
                "text": _bounded_text(getattr(message, "text", "") or "", 1_000),
                "location": dict(getattr(message, "location", {}) or {}),
            }
        )

    def record_page_error(error: BaseException) -> None:
        page_errors.append({"message": _bounded_text(str(error), 2_000)})

    def record_request(request: Any) -> None:
        nonlocal request_count
        request_count += 1
        resource_types[str(getattr(request, "resource_type", "") or "unknown")] += 1

    def record_response(response: Any) -> None:
        nonlocal response_count
        response_count += 1
        status = int(getattr(response, "status", 0) or 0)
        response_statuses[f"{status // 100}xx" if status else "unknown"] += 1

    def record_request_failed(request: Any) -> None:
        failure = request.failure or {}
        failed_requests.append(
            {
                "url": str(getattr(request, "url", "") or "")[:500],
                "method": str(getattr(request, "method", "") or ""),
                "resource_type": str(getattr(request, "resource_type", "") or "unknown"),
                "failure": _bounded_text(
                    failure.get("errorText", "") if isinstance(failure, dict) else failure, 1_000
                ),
            }
        )

    try:
        with sync_playwright() as playwright:
            browser, browser_channel = _launch_backend_chromium(playwright)
            context = browser.new_context(
                accept_downloads=False,
                java_script_enabled=True,
                ignore_https_errors=False,
                viewport={"width": 1365, "height": 900},
            )
            page = context.new_page()
            page.on("console", record_console)
            page.on("pageerror", record_page_error)
            page.on("request", record_request)
            page.on("response", record_response)
            page.on("requestfailed", record_request_failed)

            response = page.goto(normalized_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(750)
            final_url = page.url
            title = page.title()
            text_sample = page.locator("body").inner_text(timeout=timeout_ms)
            screenshot_path = None
            if capture_screenshot:
                screenshot_path = _safe_image_path(final_url)
                page.screenshot(path=screenshot_path, full_page=True, timeout=timeout_ms)
            context.close()
            browser.close()
            browser = None
            status_code = int(response.status) if response is not None else 0
            failed = _bounded_events(failed_requests, max_events)
            console = _bounded_events(console_messages, max_events)
            errors = _bounded_events(page_errors, max_events)
            return {
                "provider": "playwright",
                "diagnostic_type": "browser_diagnostics",
                "url": normalized_url,
                "final_url": final_url,
                "status": "success" if 200 <= status_code < 400 else "http_error",
                "title": title,
                "status_code": status_code,
                "console_messages": console,
                "page_errors": errors,
                "failed_requests": failed,
                "network_summary": {
                    "request_count": request_count,
                    "response_count": response_count,
                    "failed_request_count": len(failed_requests),
                    "status_buckets": dict(response_statuses),
                    "resource_type_counts": dict(resource_types),
                },
                "text_sample": _bounded_text(text_sample, max_text_chars),
                "latency_ms": int((time.perf_counter() - started_at) * 1000),
                "error": None,
                "metadata": {
                    "timeout_seconds": timeout_ms // 1000,
                    "read_only": True,
                    "backend_browser": True,
                    "browser_launched": True,
                    "browser_channel": browser_channel,
                    "persistent_profile": False,
                    "console_truncated": len(console_messages) > len(console),
                    "page_errors_truncated": len(page_errors) > len(errors),
                    "failed_requests_truncated": len(failed_requests) > len(failed),
                    "screenshot_path": screenshot_path,
                },
                "send_enabled": False,
            }
    except PlaywrightError as exc:  # type: ignore[name-defined]
        return {
            **_blocked_payload(
                normalized_url,
                reason=f"Playwright diagnostics failed: {exc}",
                live=live,
            ),
            "diagnostic_type": "browser_diagnostics",
            "latency_ms": int((time.perf_counter() - started_at) * 1000),
        }
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def summarize_rendered_page_diagnostics_impl(diagnostics: dict[str, Any]) -> dict[str, Any]:
    """Summarize browser diagnostics into an agent-readable risk list."""

    network = diagnostics.get("network_summary") if isinstance(diagnostics, dict) else {}
    network = network if isinstance(network, dict) else {}
    console_messages = diagnostics.get("console_messages") if isinstance(diagnostics, dict) else []
    page_errors = diagnostics.get("page_errors") if isinstance(diagnostics, dict) else []
    console_count = len(console_messages) if isinstance(console_messages, list) else 0
    page_error_count = len(page_errors) if isinstance(page_errors, list) else 0
    failed_request_count = int(network.get("failed_request_count") or 0)
    status = (
        str(diagnostics.get("status") or "unknown") if isinstance(diagnostics, dict) else "unknown"
    )
    status_code = int(diagnostics.get("status_code") or 0) if isinstance(diagnostics, dict) else 0

    issues: list[dict[str, Any]] = []
    if status != "success":
        issues.append(
            {
                "severity": "high",
                "issue": "Page did not return a successful rendered status.",
                "evidence": {"status": status, "status_code": status_code},
            }
        )
    if page_error_count:
        issues.append(
            {
                "severity": "high",
                "issue": "JavaScript page errors were observed.",
                "evidence": {"page_error_count": page_error_count},
            }
        )
    if failed_request_count:
        issues.append(
            {
                "severity": "medium",
                "issue": "One or more network requests failed during render.",
                "evidence": {"failed_request_count": failed_request_count},
            }
        )
    if console_count:
        issues.append(
            {
                "severity": "low",
                "issue": "Console messages were observed during render.",
                "evidence": {"console_message_count": console_count},
            }
        )

    summary_status = "clean"
    if any(issue["severity"] in {"high", "medium"} for issue in issues):
        summary_status = "needs_review"
    elif issues:
        summary_status = "notes"

    return {
        "diagnostic_type": "browser_diagnostics_summary",
        "url": diagnostics.get("url") if isinstance(diagnostics, dict) else "",
        "final_url": diagnostics.get("final_url") if isinstance(diagnostics, dict) else "",
        "status": summary_status,
        "status_code": status_code,
        "issue_count": len(issues),
        "issues": issues,
        "network_summary": network,
        "metadata": {
            "read_only": True,
            "backend_browser": True,
            "persistent_profile": False,
        },
        "send_enabled": False,
    }


@function_tool(**keystone_tool_guardrail_kwargs())
def capture_browser_diagnostics(
    url: str,
    timeout_seconds: int = DEFAULT_PLAYWRIGHT_TIMEOUT_SECONDS,
    live: bool = False,
    max_events: int = DEFAULT_BROWSER_DIAGNOSTIC_MAX_EVENTS,
    capture_screenshot: bool = False,
    max_text_chars: int = DEFAULT_PLAYWRIGHT_MAX_TEXT_CHARS,
) -> dict[str, Any]:
    """Capture read-only backend-browser console/network diagnostics."""

    return capture_browser_diagnostics_impl(
        url,
        timeout_seconds=timeout_seconds,
        live=live,
        max_events=max_events,
        capture_screenshot=capture_screenshot,
        max_text_chars=max_text_chars,
    )


@function_tool(**keystone_tool_guardrail_kwargs())
def summarize_rendered_page_diagnostics(diagnostics_json: str) -> dict[str, Any]:
    """Summarize rendered browser diagnostics for agent reasoning."""

    try:
        diagnostics = json.loads(diagnostics_json or "{}")
    except json.JSONDecodeError as exc:
        return {
            "diagnostic_type": "browser_diagnostics_summary",
            "status": "blocked",
            "issue_count": 1,
            "issues": [
                {
                    "severity": "high",
                    "issue": "Diagnostics input was not valid JSON.",
                    "evidence": {"error": str(exc)},
                }
            ],
            "metadata": {
                "read_only": True,
                "backend_browser": True,
                "persistent_profile": False,
            },
            "send_enabled": False,
        }
    if not isinstance(diagnostics, dict):
        diagnostics = {}
    return summarize_rendered_page_diagnostics_impl(diagnostics)
