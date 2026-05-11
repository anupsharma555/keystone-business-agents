"""Safe web scraping placeholder."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from keystone_agents.guardrails import enforce_tool_input_guardrails, enforce_tool_output_guardrails


@dataclass(frozen=True)
class WebScrapeTool:
    live: bool = False

    def fetch_text(self, url: str) -> dict[str, Any]:
        enforce_tool_input_guardrails("web_scrape_fetch_text", {"url": url, "live": self.live})
        if not self.live:
            return enforce_tool_output_guardrails(
                "web_scrape_fetch_text",
                {"status": "dry-run", "url": url, "text": ""},
            )
        raise NotImplementedError("Live web scraping is not implemented yet.")
