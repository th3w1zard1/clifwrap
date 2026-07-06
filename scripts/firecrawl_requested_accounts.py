#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


DEFAULT_SPEC = Path(__file__).with_name("firecrawl_requested_accounts.toml")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import the requested Firecrawl accounts through clifwrap's declarative spec importer.")
    parser.add_argument("--spec", default=str(DEFAULT_SPEC), help="TOML account spec to import.")
    parser.add_argument("--env-file", help="Override the spec env_file.")
    parser.add_argument("--apply", action="store_true", help="Write changes. Without this, only print the planned import.")
    args = parser.parse_args()

    command = ["clifwrap", "account", "import-spec", args.spec]
    if args.env_file:
        command.extend(["--env-file", args.env_file])
    if args.apply:
        command.append("--apply")
    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
