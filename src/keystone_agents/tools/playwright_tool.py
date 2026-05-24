"""Optional read-only Playwright rendering tool."""

from __future__ import annotations

import os
import re
import time
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from keystone_agents.guardrails import keystone_tool_guardrail_kwargs
from keystone_agents.sdk import function_tool

DEFAULT_PLAYWRIGHT_TIMEOUT_SECONDS = 15
DEFAULT_PLAYWRIGHT_MAX_TEXT_CHARS = 20_000
DEFAULT_PLAYWRIGHT_MAX_LINKS = 50
DEFAULT_PLAYWRIGHT_IMAGE_DIR = "artifacts/playwright-images"
PLAYWRIGHT_ENABLED_ENV = "KEYSTONE_PLAYWRIGHT_ENABLED"
PLAYWRIGHT_IMAGE_DIR_ENV = "KEYSTONE_PLAYWRIGHT_IMAGE_DIR"


def _enabled() -> bool:
    return os.getenv(PLAYWRIGHT_ENABLED_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def _valid_http_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _bounded_timeout_ms(timeout_seconds: int | float | None) -> int:
    try:
        seconds = float(timeout_seconds or DEFAULT_PLAYWRIGHT_TIMEOUT_SECONDS)
    except (TypeError, ValueError):
        seconds = DEFAULT_PLAYWRIGHT_TIMEOUT_SECONDS
    return int(max(1.0, min(seconds, 30.0)) * 1000)


def _bounded_text(text: Any, max_chars: int = DEFAULT_PLAYWRIGHT_MAX_TEXT_CHARS) -> str:
    return str(text or "")[: max(1000, min(int(max_chars or DEFAULT_PLAYWRIGHT_MAX_TEXT_CHARS), 50_000))]


def _is_missing_browser_error(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "executable doesn't exist" in message or "playwright install" in message


def _blocked_payload(url: str, *, reason: str, live: bool) -> dict[str, Any]:
    return {
        "provider": "playwright",
        "url": url,
        "final_url": url,
        "status": "blocked",
        "title": "",
        "text_or_markdown": "",
        "links": [],
        "html_length": 0,
        "latency_ms": 0,
        "error": reason,
        "metadata": {
            "live": live,
            "read_only": True,
            "backend_browser": True,
            "persistent_profile": False,
            "env_flag": PLAYWRIGHT_ENABLED_ENV,
            "image_dir": str(_image_dir_path()),
        },
        "send_enabled": False,
    }


def _image_dir_path() -> Path:
    configured = os.getenv(PLAYWRIGHT_IMAGE_DIR_ENV, "").strip()
    image_dir = Path(configured or DEFAULT_PLAYWRIGHT_IMAGE_DIR)
    if image_dir.is_absolute():
        return Path(DEFAULT_PLAYWRIGHT_IMAGE_DIR)
    parts = image_dir.parts
    if not parts or parts[0] != "artifacts":
        return Path(DEFAULT_PLAYWRIGHT_IMAGE_DIR)
    return image_dir


def _safe_image_path(url: str) -> str:
    parsed = urlparse(url)
    host = re.sub(r"[^A-Za-z0-9._-]+", "-", parsed.netloc.lower()).strip("-") or "page"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    digest = sha256(url.encode("utf-8")).hexdigest()[:10]
    image_dir = _image_dir_path()
    image_dir.mkdir(parents=True, exist_ok=True)
    return str(image_dir / f"{stamp}-{host}-{digest}.png")


def render_page_impl(
    url: str,
    *,
    timeout_seconds: int = DEFAULT_PLAYWRIGHT_TIMEOUT_SECONDS,
    live: bool = False,
    max_text_chars: int = DEFAULT_PLAYWRIGHT_MAX_TEXT_CHARS,
    capture_screenshot: bool = False,
) -> dict[str, Any]:
    """Render one HTTP(S) page with Playwright in a bounded, read-only browser."""

    normalized_url = str(url or "").strip()
    if not _valid_http_url(normalized_url):
        return _blocked_payload(normalized_url, reason="Only http/https URLs are allowed.", live=live)
    timeout_ms = _bounded_timeout_ms(timeout_seconds)
    if not live:
        return {
            "provider": "playwright",
            "url": normalized_url,
            "final_url": normalized_url,
            "status": "dry-run",
            "title": "",
            "text_or_markdown": "",
            "links": [],
            "html_length": 0,
            "latency_ms": 0,
            "error": None,
            "metadata": {
                "timeout_seconds": timeout_ms // 1000,
                "read_only": True,
                "backend_browser": True,
                "browser_launched": False,
                "persistent_profile": False,
                "image_dir": str(_image_dir_path()),
                "screenshot_path": None,
            },
            "send_enabled": False,
        }
    if not _enabled():
        return _blocked_payload(
            normalized_url,
            reason=f"{PLAYWRIGHT_ENABLED_ENV}=true is required for live Playwright rendering.",
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

    browser = None
    try:
        with sync_playwright() as playwright:
            browser_channel = "playwright-chromium"
            try:
                browser = playwright.chromium.launch(headless=True)
            except PlaywrightError as exc:
                if not _is_missing_browser_error(exc):
                    raise
                browser = playwright.chromium.launch(headless=True, channel="chrome")
                browser_channel = "local-chrome"
            context = browser.new_context(
                accept_downloads=False,
                java_script_enabled=True,
                ignore_https_errors=False,
                viewport={"width": 1365, "height": 900},
            )
            page = context.new_page()
            response = page.goto(normalized_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(500)
            title = page.title()
            final_url = page.url
            html = page.content()
            text = page.locator("body").inner_text(timeout=timeout_ms)
            screenshot_path = None
            if capture_screenshot:
                screenshot_path = _safe_image_path(final_url)
                page.screenshot(path=screenshot_path, full_page=True, timeout=timeout_ms)
            links = page.eval_on_selector_all(
                "a[href]",
                """els => els.slice(0, 50).map(a => ({
                    url: a.href || "",
                    text: (a.innerText || a.textContent || "").trim().slice(0, 200)
                }))""",
            )
            context.close()
            browser.close()
            browser = None
            status_code = int(response.status) if response is not None else 0
            return {
                "provider": "playwright",
                "url": normalized_url,
                "final_url": final_url,
                "status": "success" if 200 <= status_code < 400 else "http_error",
                "title": title,
                "text_or_markdown": _bounded_text(text, max_text_chars),
                "links": links[:DEFAULT_PLAYWRIGHT_MAX_LINKS],
                "html_length": len(html),
                "latency_ms": int((time.perf_counter() - started_at) * 1000),
                "error": None,
                "metadata": {
                    "status_code": status_code,
                    "timeout_seconds": timeout_ms // 1000,
                    "read_only": True,
                    "backend_browser": True,
                    "browser_launched": True,
                    "browser_channel": browser_channel,
                    "persistent_profile": False,
                    "text_truncated": len(str(text or "")) > max_text_chars,
                    "image_dir": str(_image_dir_path()),
                    "screenshot_path": screenshot_path,
                },
                "send_enabled": False,
            }
    except PlaywrightError as exc:  # type: ignore[name-defined]
        return {
            **_blocked_payload(normalized_url, reason=f"Playwright render failed: {exc}", live=live),
            "latency_ms": int((time.perf_counter() - started_at) * 1000),
        }
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


@function_tool(**keystone_tool_guardrail_kwargs())
def render_page(
    url: str,
    timeout_seconds: int = DEFAULT_PLAYWRIGHT_TIMEOUT_SECONDS,
    live: bool = False,
    max_text_chars: int = DEFAULT_PLAYWRIGHT_MAX_TEXT_CHARS,
    capture_screenshot: bool = False,
) -> dict[str, Any]:
    """Read-only Playwright page render for JS-heavy public HTTP(S) pages."""

    return render_page_impl(
        url,
        timeout_seconds=timeout_seconds,
        live=live,
        max_text_chars=max_text_chars,
        capture_screenshot=capture_screenshot,
    )
