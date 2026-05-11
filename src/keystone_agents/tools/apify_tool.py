"""Safe Apify actor placeholder and profile SDK tool wrapper."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from keystone_agents.config import load_settings
from keystone_agents.guardrails import (
    enforce_tool_input_guardrails,
    enforce_tool_output_guardrails,
    keystone_tool_guardrail_kwargs,
)
from keystone_agents.sdk import function_tool


@dataclass(frozen=True)
class ApifyTool:
    """Apify wrapper.

    Respect site terms, privacy requirements, and legal constraints before enabling
    any live actor or dataset access. Dry-run is the only implemented mode here.
    """

    live: bool = False

    def run_actor(self, actor_id: str, input_data: dict[str, Any]) -> dict[str, Any]:
        return run_actor(actor_id=actor_id, input_payload=input_data, live=self.live)


def _require_apify_token() -> str:
    api_token = load_settings().apify_api_token
    if not api_token:
        raise RuntimeError(
            "APIFY_API_TOKEN is required for live Apify access. "
            "Dry-run mode does not require credentials."
        )
    return api_token


def run_actor(actor_id: str, input_payload: dict[str, Any], live: bool = False) -> dict[str, Any]:
    """Run an Apify actor.

    Respect site terms, privacy requirements, and legal constraints before enabling
    live actors. This phase provides deterministic dry-run behavior only.
    """

    enforce_tool_input_guardrails(
        "apify_run_actor",
        {"actor_id": actor_id, "input_payload": input_payload, "live": live},
    )
    if not live:
        return enforce_tool_output_guardrails(
            "apify_run_actor",
            {
                "mode": "dry_run",
                "status": "dry-run",
                "actor_id": actor_id,
                "input_payload": input_payload,
                "default_dataset_id": "dry-run-dataset",
            },
        )

    _require_apify_token()
    raise NotImplementedError("Live Apify actor execution is not implemented yet.")


def get_dataset_items(dataset_id: str, live: bool = False) -> list[dict[str, Any]]:
    """Return Apify dataset items.

    Respect site terms, privacy requirements, and legal constraints before enabling
    live dataset access. This phase provides deterministic dry-run behavior only.
    """

    enforce_tool_input_guardrails(
        "apify_get_dataset_items",
        {"dataset_id": dataset_id, "live": live},
    )
    if not live:
        return enforce_tool_output_guardrails(
            "apify_get_dataset_items",
            [
                {
                    "mode": "dry_run",
                    "dataset_id": dataset_id,
                    "item_id": "dry-run-item-1",
                    "title": "Dry-run Apify dataset item",
                    "source": "apify:dry-run",
                }
            ],
        )

    _require_apify_token()
    raise NotImplementedError("Live Apify dataset access is not implemented yet.")


@function_tool(**keystone_tool_guardrail_kwargs())
def fetch_linkedin_or_profile_placeholder(
    company_name: str,
    linkedin_url: str | None = None,
    fixture_json: str | None = None,
    dry_run: bool = True,
) -> str:
    """Return fixture LinkedIn/profile data or a no-claims placeholder."""

    if not dry_run:
        raise RuntimeError(
            "Live LinkedIn/profile fetching requires an explicit integration implementation."
        )

    claims: list[str] = []
    if fixture_json and fixture_json.strip().startswith("{"):
        fixture = json.loads(fixture_json)
        linkedin_url = linkedin_url or fixture.get("linkedin_url")
        for source in fixture.get("sources") or []:
            if isinstance(source, dict) and source.get("source_type") == "linkedin":
                claims.extend(source.get("supported_claims") or source.get("claims") or [])

    return json.dumps(
        {
            "mode": "dry_run",
            "company_name": company_name,
            "linkedin_url": linkedin_url,
            "claims": claims,
            "placeholder": not bool(claims),
        },
        sort_keys=True,
    )
