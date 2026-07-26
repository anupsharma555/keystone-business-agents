"""Lazy public Keystone command-line entrypoint."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from importlib import import_module
from types import ModuleType
from typing import Any

_COMMAND_HELP = (
    ("init-db", "Initialize local SQLite storage."),
    ("health", "Run an offline health check."),
    ("route", "Route a request in deterministic dry-run mode."),
    (
        "ask",
        "Send a natural-language request to @KNI or a named Keystone agent.",
    ),
    ("pipeline", "Run the dry-run fixture pipeline."),
    ("evals", "Run deterministic static evals."),
    ("approvals", "List approval queue items."),
    ("work-items", "Inspect and advance WorkItems."),
    ("automations", "Inspect Keystone automations."),
    ("agents", "Inspect registered agents."),
)
_IMPLEMENTATION: ModuleType | None = None
_FACADE_OWNED_NAMES = frozenset(
    {
        "_COMMAND_HELP",
        "_FACADE_OWNED_NAMES",
        "_IMPLEMENTATION",
        "_LazyCliModule",
        "_implementation",
        "_root_help_parser",
        "build_parser",
        "main",
    }
)


class _LazyCliModule(ModuleType):
    """Mirror compatibility monkeypatches into the loaded implementation."""

    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        implementation = self.__dict__.get("_IMPLEMENTATION")
        if (
            isinstance(implementation, ModuleType)
            and name not in _FACADE_OWNED_NAMES
            and hasattr(implementation, name)
        ):
            setattr(implementation, name, value)

    def __delattr__(self, name: str) -> None:
        implementation = self.__dict__.get("_IMPLEMENTATION")
        if (
            isinstance(implementation, ModuleType)
            and name not in _FACADE_OWNED_NAMES
            and hasattr(implementation, name)
        ):
            delattr(implementation, name)
        super().__delattr__(name)


def _implementation() -> ModuleType:
    global _IMPLEMENTATION
    if _IMPLEMENTATION is None:
        implementation = import_module("keystone_agents.entrypoints.cli_impl")
        facade_values = vars(sys.modules[__name__])
        for name, value in facade_values.items():
            if name not in _FACADE_OWNED_NAMES and hasattr(implementation, name):
                setattr(implementation, name, value)
        _IMPLEMENTATION = implementation
    return _IMPLEMENTATION


def _root_help_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Keystone business-agent operations.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command, help_text in _COMMAND_HELP:
        subparsers.add_parser(command, help=help_text)
    return parser


def build_parser() -> argparse.ArgumentParser:
    """Build the complete parser only when a command needs it."""

    return _implementation().build_parser()


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI, keeping root help independent of the heavy runtime."""

    values = list(sys.argv[1:] if argv is None else argv)
    if values in (["-h"], ["--help"]):
        _root_help_parser().parse_args(values)
    return int(_implementation().main(values))


def __getattr__(name: str) -> Any:
    """Preserve legacy helper imports while loading them only on demand."""

    return getattr(_implementation(), name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(dir(_implementation())))


__all__ = ["build_parser", "main"]

sys.modules[__name__].__class__ = _LazyCliModule


if __name__ == "__main__":
    raise SystemExit(main())
