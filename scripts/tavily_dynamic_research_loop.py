#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import random
import re
import shlex
import subprocess
import textwrap
import time
from dataclasses import dataclass
from datetime import datetime


TOPICS = [
    "how ancient concrete can survive seawater",
    "how glaciers make blue ice",
    "how lightning forms inside thunderclouds",
    "how mechanical watches store and release energy",
    "how migratory birds navigate using magnetic fields",
    "how radio telescopes combine signals using interferometry",
    "how seed vaults preserve crop diversity",
    "how ship ballast water spreads invasive species",
    "how urban beekeeping affects local pollination",
    "how urban trees change local street temperatures",
    "origins of the QWERTY keyboard layout",
    "what makes aerogel such a good insulator",
    "what makes lithium iron phosphate batteries durable",
    "why cats knead blankets",
    "why deep sea fish often look red or black",
    "why old books smell sweet and musty",
    "why some deserts have singing sand dunes",
    "why some mushrooms glow in the dark",
    "why sourdough starter becomes more acidic over time",
    "why vinyl records crackle",
]

ACCOUNT_RE = re.compile(r"^(?P<default>[* ])\s*(?P<name>.+?)\s+\[(?P<state>enabled|disabled)\]\s+")
DEFAULT_RE = re.compile(r"default account:\s*(?P<name>.+)$")
FAILOVER_RE = re.compile(r"retrying with (?P<name>\S+)")


@dataclass(frozen=True)
class Account:
    name: str
    enabled: bool
    is_default: bool


def run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def parse_accounts(output: str) -> list[Account]:
    accounts: list[Account] = []
    for raw_line in output.splitlines():
        match = ACCOUNT_RE.match(raw_line)
        if not match:
            continue
        accounts.append(
            Account(
                name=match.group("name").strip(),
                enabled=match.group("state") == "enabled",
                is_default=match.group("default") == "*",
            )
        )
    return accounts


def discover_accounts() -> list[Account]:
    proc = run(["tvly", "auth", "list"])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or "tvly auth list failed")
    accounts = parse_accounts(proc.stdout)
    enabled = [account for account in accounts if account.enabled]
    if len(enabled) < 2:
        raise RuntimeError("Need at least two enabled wrapper accounts to prove fallback dynamically.")
    return enabled


def default_account() -> str:
    proc = run(["tvly", "auth", "default"])
    text = (proc.stdout or proc.stderr).strip()
    match = DEFAULT_RE.search(text)
    if not match:
        return ""
    return match.group("name").strip()


def set_default(account: str) -> None:
    proc = run(["tvly", "auth", "use", account])
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip() or f"failed to set default to {account}")


def next_candidate(accounts: list[Account], last_name: str | None) -> Account:
    names = [account.name for account in accounts]
    if last_name in names:
        index = (names.index(last_name) + 1) % len(accounts)
    else:
        current = default_account()
        index = (names.index(current) + 1) % len(accounts) if current in names else 0
    return accounts[index]


def choose_topic(used: set[str]) -> str:
    remaining = [topic for topic in TOPICS if topic not in used]
    if not remaining:
        used.clear()
        remaining = list(TOPICS)
    topic = random.choice(remaining)
    used.add(topic)
    return topic


def print_results(stdout: str) -> None:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        print("Outcome: command succeeded, but stdout was not JSON. Raw stdout follows:", flush=True)
        print(textwrap.indent(stdout.strip(), "  "), flush=True)
        return
    results = payload.get("results", []) if isinstance(payload, dict) else []
    print(f"Outcome: success. Tavily returned {len(results)} results.", flush=True)
    for index, item in enumerate(results[:3], start=1):
        title = item.get("title") or "<untitled>"
        url = item.get("url") or item.get("link") or "<no url>"
        content = (item.get("content") or item.get("snippet") or "").replace("\n", " ").strip()
        if len(content) > 260:
            content = content[:257] + "..."
        print(f"  Result {index}: {title}", flush=True)
        print(f"    Source: {url}", flush=True)
        if content:
            print(f"    Informative plain-language note: {content}", flush=True)
    answer = payload.get("answer") if isinstance(payload, dict) else None
    if answer:
        print("  Tavily direct answer:", flush=True)
        print(textwrap.indent(str(answer), "    "), flush=True)


def account_names(accounts: list[Account]) -> str:
    return ", ".join(account.name for account in accounts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run verbose Tavily searches until wrapper failover is observed.")
    parser.add_argument("--interval", type=float, default=20.0, help="Seconds to wait between queries.")
    parser.add_argument("--max-queries", type=int, default=0, help="Stop after this many queries. Zero means run until interrupted.")
    parser.add_argument("--stop-after-failover", action="store_true", help="Exit after the first observed failover proof.")
    args = parser.parse_args()

    accounts = discover_accounts()
    print("Dynamic Tavily proof loop starting.", flush=True)
    print("No account label is hardcoded in this script; enabled account names were discovered from `tvly auth list`.", flush=True)
    print(f"Discovered enabled accounts in wrapper order: {account_names(accounts)}", flush=True)
    print("The script will rotate the wrapper default through those discovered accounts until a real Tavily retry/failover is observed.", flush=True)
    print("-" * 100, flush=True)

    used_topics: set[str] = set()
    last_candidate: str | None = None
    failover_seen = False
    query_number = 0

    while args.max_queries <= 0 or query_number < args.max_queries:
        query_number += 1
        accounts = discover_accounts()
        candidate = next_candidate(accounts, last_candidate)
        last_candidate = candidate.name
        set_default(candidate.name)

        before = default_account()
        topic = choose_topic(used_topics)
        command = ["tvly", "--json", "search", topic]

        print(f"[{datetime.now().isoformat(timespec='seconds')}] Dynamic Tavily research query #{query_number}", flush=True)
        print(f"Random topic selected: {topic}", flush=True)
        print(f"Discovered candidate selected for this query: {candidate.name}", flush=True)
        print(f"Wrapper default before query: {before}", flush=True)
        print("Why this account is being tried first: it is the next enabled account discovered from wrapper state, not a name embedded in the script.", flush=True)
        print("Actual command: " + " ".join(shlex.quote(part) for part in command), flush=True)

        proc = run(command)
        failover_chain: list[str] = []
        if proc.stderr.strip():
            print("Messages from wrapper/upstream stderr:", flush=True)
            for line in proc.stderr.strip().splitlines():
                print("  " + line, flush=True)
                match = FAILOVER_RE.search(line)
                if match:
                    failover_chain.append(match.group("name"))

        after = default_account()
        if failover_chain:
            failover_seen = True
            print(f"Observed retry/failover chain ending with: {failover_chain[-1]}", flush=True)
            print(f"Wrapper default after query: {after}", flush=True)
            print(f"Default-auth update proof: before={before}, after={after}.", flush=True)
            if after == failover_chain[-1]:
                print("Plain-language proof: the account that received the successful retry is now persisted as the wrapper default.", flush=True)
            else:
                print("Plain-language warning: failover happened, but the persisted default does not match the last retry target.", flush=True)
        else:
            print("No fallback was needed on this query; the selected account completed the request.", flush=True)
            print(f"Wrapper default after query: {after}", flush=True)

        if proc.returncode == 0:
            print_results(proc.stdout)
        else:
            print(f"Outcome: command failed with exit code {proc.returncode}.", flush=True)
            if proc.stdout.strip():
                print("Stdout:", flush=True)
                print(textwrap.indent(proc.stdout.strip(), "  "), flush=True)

        print("-" * 100, flush=True)
        if failover_seen and args.stop_after_failover:
            return 0
        if args.interval > 0:
            time.sleep(args.interval)
    return 0 if failover_seen else 1


if __name__ == "__main__":
    raise SystemExit(main())
