#!/usr/bin/env python3
"""Generate built-in provider catalog documentation from providers.toml."""

from __future__ import annotations

import argparse
import sys
import tomllib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CATALOG = ROOT / "src" / "clifwrap" / "providers.toml"
OUTPUT = ROOT / "docs" / "provider-catalog.md"


def _inline(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(f"`{item}`" for item in value) or "-"
    if isinstance(value, dict):
        return ", ".join(f"`{key}={item}`" for key, item in sorted(value.items())) or "-"
    return f"`{value}`"


def _table(rows: list[tuple[str, Any]]) -> list[str]:
    lines = ["| Field | Value |", "| --- | --- |"]
    lines.extend(f"| `{key}` | {_inline(value)} |" for key, value in rows)
    return lines


def generate() -> str:
    raw = tomllib.loads(CATALOG.read_text(encoding="utf-8"))
    providers = raw.get("providers", {})
    if not isinstance(providers, dict):
        raise SystemExit("providers.toml does not contain a [providers] table")

    lines = [
        "# Built-In Provider Catalog",
        "",
        "This file is generated from `src/clifwrap/providers.toml`.",
        "Run `python scripts/generate_provider_catalog.py --write` after changing built-in provider metadata.",
        "",
    ]
    for provider_name, provider in sorted(providers.items()):
        if not isinstance(provider, dict):
            continue
        lines.extend([f"## `{provider_name}`", ""])
        base_rows = [
            ("interactive_mode", provider.get("interactive_mode")),
            ("passthrough_commands", provider.get("passthrough_commands")),
            ("retry_exit_codes", provider.get("retry_exit_codes")),
            ("retry_patterns", provider.get("retry_patterns")),
            ("never_retry_patterns", provider.get("never_retry_patterns")),
        ]
        lines.extend(_table([(key, value) for key, value in base_rows if value is not None]))
        lines.append("")

        for section in ("auth_management", "fallback_monitor", "usage", "capacity_control"):
            section_value = provider.get(section)
            if not isinstance(section_value, dict):
                continue
            title = section.replace("_", " ").title()
            lines.extend([f"### {title}", ""])
            lines.extend(_table(list(section_value.items())))
            lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate docs/provider-catalog.md from providers.toml.")
    parser.add_argument("--write", action="store_true", help="Write the generated provider catalog.")
    parser.add_argument("--check", action="store_true", help="Fail if the checked-in provider catalog is stale.")
    args = parser.parse_args()

    rendered = generate()
    if args.write:
        OUTPUT.write_text(rendered, encoding="utf-8")
        print(f"wrote {OUTPUT.relative_to(ROOT)}")
        return 0
    if args.check:
        existing = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if existing != rendered:
            raise SystemExit("docs/provider-catalog.md is stale; run `python scripts/generate_provider_catalog.py --write`")
        print("Provider catalog ok: docs/provider-catalog.md")
        return 0
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
