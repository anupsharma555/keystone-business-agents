#!/usr/bin/env python3
"""Print a dashboard-safe Exa usage snapshot for API usage reports."""

from __future__ import annotations

import json

from keystone_agents.config import with_cli_environment
from keystone_agents.exa_usage import exa_usage_snapshot


@with_cli_environment()
def main() -> None:
    snapshot = exa_usage_snapshot()
    print(json.dumps(snapshot.model_dump(mode="json"), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
