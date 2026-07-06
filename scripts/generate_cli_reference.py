#!/usr/bin/env python3
"""Generate the command reference from clifwrap's argparse parser."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
OUTPUT = ROOT / "docs" / "cli-reference.md"


def _parser() -> argparse.ArgumentParser:
    os.environ.setdefault("COLUMNS", "100")
    sys.path.insert(0, str(SRC))
    from clifwrap.__main__ import _parser as build_parser

    return build_parser()


def _subparser_actions(parser: argparse.ArgumentParser) -> list[argparse._SubParsersAction]:  # type: ignore[attr-defined]
    return [action for action in parser._actions if isinstance(action, argparse._SubParsersAction)]  # type: ignore[attr-defined]


def _walk(parser: argparse.ArgumentParser, command: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], argparse.ArgumentParser]]:
    rows = [(command, parser)]
    for action in _subparser_actions(parser):
        for name, child in sorted(action.choices.items()):
            rows.extend(_walk(child, (*command, name)))
    return rows


def generate() -> str:
    parser = _parser()
    sections = [
        "# CLI Reference",
        "",
        "This file is generated from the shipped `argparse` command surface.",
        "Run `python scripts/generate_cli_reference.py --write` after changing CLI arguments.",
        "",
    ]
    for command, command_parser in _walk(parser):
        title = "clifwrap" if not command else "clifwrap " + " ".join(command)
        sections.extend(
            [
                f"## `{title}`",
                "",
                "```text",
                command_parser.format_help().rstrip(),
                "```",
                "",
            ]
        )
    return "\n".join(sections)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate docs/cli-reference.md from the real clifwrap parser.")
    parser.add_argument("--write", action="store_true", help="Write the generated reference.")
    parser.add_argument("--check", action="store_true", help="Fail if the checked-in reference is stale.")
    args = parser.parse_args()

    rendered = generate()
    if args.write:
        OUTPUT.write_text(rendered, encoding="utf-8")
        print(f"wrote {OUTPUT.relative_to(ROOT)}")
        return 0
    if args.check:
        existing = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if existing != rendered:
            raise SystemExit("docs/cli-reference.md is stale; run `python scripts/generate_cli_reference.py --write`")
        print("CLI reference ok: docs/cli-reference.md")
        return 0
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
