from __future__ import annotations

from keystone_agents.tools.playwright_tool import _is_missing_browser_error, render_page_impl


def test_playwright_render_page_dry_run_does_not_launch_browser() -> None:
    result = render_page_impl("https://example.com", live=False)

    assert result["status"] == "dry-run"
    assert result["provider"] == "playwright"
    assert result["metadata"]["backend_browser"] is True
    assert result["metadata"]["browser_launched"] is False
    assert result["metadata"]["persistent_profile"] is False
    assert result["metadata"]["image_dir"] == "artifacts/playwright-images"
    assert result["metadata"]["screenshot_path"] is None
    assert result["send_enabled"] is False


def test_playwright_render_page_blocks_non_http_urls() -> None:
    result = render_page_impl("file:///tmp/private.html", live=True)

    assert result["status"] == "blocked"
    assert "http/https" in result["error"]
    assert result["send_enabled"] is False


def test_playwright_render_page_requires_explicit_live_flag(monkeypatch) -> None:
    monkeypatch.delenv("KEYSTONE_PLAYWRIGHT_ENABLED", raising=False)

    result = render_page_impl("https://example.com", live=True)

    assert result["status"] == "blocked"
    assert "KEYSTONE_PLAYWRIGHT_ENABLED=true" in result["error"]


def test_playwright_image_dir_is_artifacts_scoped(monkeypatch) -> None:
    monkeypatch.setenv("KEYSTONE_PLAYWRIGHT_IMAGE_DIR", "/tmp/outside")

    result = render_page_impl("https://example.com", live=False, capture_screenshot=True)

    assert result["metadata"]["image_dir"] == "artifacts/playwright-images"
    assert result["metadata"]["screenshot_path"] is None


def test_playwright_missing_managed_browser_error_uses_local_chrome_fallback() -> None:
    assert _is_missing_browser_error(
        RuntimeError("Executable doesn't exist at playwright-cache/ms-playwright")
    )
    assert _is_missing_browser_error(RuntimeError("Please run: playwright install"))
    assert not _is_missing_browser_error(RuntimeError("navigation timeout"))
