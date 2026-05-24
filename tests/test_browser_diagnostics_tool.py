from __future__ import annotations

from keystone_agents.tools.browser_diagnostics_tool import (
    capture_browser_diagnostics_impl,
    summarize_rendered_page_diagnostics_impl,
)


def test_browser_diagnostics_dry_run_is_backend_and_read_only() -> None:
    result = capture_browser_diagnostics_impl("https://example.com", live=False)

    assert result["status"] == "dry-run"
    assert result["diagnostic_type"] == "browser_diagnostics"
    assert result["metadata"]["backend_browser"] is True
    assert result["metadata"]["persistent_profile"] is False
    assert result["metadata"]["browser_launched"] is False
    assert result["send_enabled"] is False


def test_browser_diagnostics_blocks_non_http_urls() -> None:
    result = capture_browser_diagnostics_impl("file:///tmp/secret.html", live=True)

    assert result["status"] == "blocked"
    assert "http/https" in result["error"]
    assert result["send_enabled"] is False


def test_browser_diagnostics_requires_live_env_flag(monkeypatch) -> None:
    monkeypatch.delenv("KEYSTONE_PLAYWRIGHT_ENABLED", raising=False)

    result = capture_browser_diagnostics_impl("https://example.com", live=True)

    assert result["status"] == "blocked"
    assert "KEYSTONE_PLAYWRIGHT_ENABLED=true" in result["error"]
    assert result["send_enabled"] is False


def test_summarize_rendered_page_diagnostics_flags_relevant_issues() -> None:
    summary = summarize_rendered_page_diagnostics_impl(
        {
            "url": "https://example.com",
            "final_url": "https://example.com/",
            "status": "success",
            "status_code": 200,
            "console_messages": [{"type": "warning", "text": "deprecated api"}],
            "page_errors": [{"message": "ReferenceError"}],
            "failed_requests": [{"url": "https://example.com/app.js"}],
            "network_summary": {"failed_request_count": 1},
        }
    )

    assert summary["status"] == "needs_review"
    assert summary["issue_count"] == 3
    assert [issue["severity"] for issue in summary["issues"]] == ["high", "medium", "low"]
    assert summary["metadata"]["backend_browser"] is True
    assert summary["send_enabled"] is False


def test_summarize_rendered_page_diagnostics_uses_notes_for_low_only_findings() -> None:
    summary = summarize_rendered_page_diagnostics_impl(
        {
            "url": "https://example.com",
            "final_url": "https://example.com/",
            "status": "success",
            "status_code": 200,
            "console_messages": [{"type": "error", "text": "favicon 404"}],
            "page_errors": [],
            "failed_requests": [],
            "network_summary": {"failed_request_count": 0},
        }
    )

    assert summary["status"] == "notes"
    assert summary["issue_count"] == 1
    assert summary["issues"][0]["severity"] == "low"
