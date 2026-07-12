from __future__ import annotations

from scripts.check_staged_publish_files import blocked_staged_paths


def test_staged_publish_guard_blocks_local_diagnostic_surfaces() -> None:
    assert blocked_staged_paths(
        (
            ".playwright-cli/page.yml",
            ".playwright-mcp/page.png",
            "artifacts/test-pack/private.json",
            "output/playwright/page.png",
            "kni-slack-dashboard-chief-artifact-tests.png",
        )
    ) == (
        ".playwright-cli/page.yml",
        ".playwright-mcp/page.png",
        "artifacts/test-pack/private.json",
        "kni-slack-dashboard-chief-artifact-tests.png",
        "output/playwright/page.png",
    )


def test_staged_publish_guard_allows_source_and_intentional_docs_assets() -> None:
    assert blocked_staged_paths(
        (
            "scripts/check_staged_publish_files.py",
            "tests/test_check_staged_publish_files.py",
            "docs/assets/architecture.png",
            "src/keystone_agents/reporting.py",
            "nested/kni-slack-dashboard-example.png",
        )
    ) == ()


def test_staged_publish_guard_normalizes_dot_prefix_and_deduplicates() -> None:
    assert blocked_staged_paths(
        (
            "./output/page.png",
            "output/page.png",
            "",
            "   ",
        )
    ) == ("output/page.png",)
