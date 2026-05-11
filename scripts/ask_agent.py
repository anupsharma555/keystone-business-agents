"""Speak to a Keystone agent from natural-language CLI text."""

from __future__ import annotations

import sys

from keystone_agents.cli import main

if __name__ == "__main__":
    raise SystemExit(main(["ask", *sys.argv[1:]]))
