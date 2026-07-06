#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


DEFAULT_SPEC = Path(__file__).with_name("firecrawl_requested_accounts.toml")
DEFAULT_CREDENTIALS = Path("~/.config/firecrawl-cli/credentials.json").expanduser()


def _quote_env(value: str) -> str:
    return json.dumps(value)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"unsupported TOML value: {value!r}")


def _load_spec(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def _expand(path: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(path)))


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return slug or "account"


def _render_env_name(template: str, *, provider: str, account: str, target_env: str) -> str:
    return template.format(
        provider=provider,
        provider_slug=_slug(provider).upper(),
        account=account,
        account_slug=_slug(account).upper(),
        target_env=target_env,
        target_env_slug=_slug(target_env).upper(),
    )


def _env_names_for(spec: dict, account: dict) -> list[str]:
    explicit = account.get("env_names")
    if explicit:
        return list(explicit)
    template = spec.get("env_name_template") or "CLIFWRAP_{provider_slug}_{account_slug}_{target_env_slug}"
    return [
        _render_env_name(
            template,
            provider=spec["provider"],
            account=account["label"] if "label" in account else account["name"],
            target_env=spec["target_env"],
        )
    ]


def _read_env_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    names: set[str] = set()
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _value = line.split("=", 1)
        if key.strip():
            names.add(key.strip())
    return names


def _upsert_env(path: Path, key: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    replacement = f"{key}={_quote_env(value)}"
    updated = False
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        prefix = "export " if stripped.startswith("export ") else ""
        candidate = stripped[len("export ") :].strip() if prefix else stripped
        if candidate.startswith(f"{key}="):
            lines[index] = f"{prefix}{replacement}"
            updated = True
            break
    if not updated:
        lines.append(replacement)
    path.write_text("\n".join(lines) + "\n")
    path.chmod(path.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR)


def _read_api_key(credentials_path: Path) -> str:
    payload = json.loads(credentials_path.read_text())
    api_key = payload.get("apiKey")
    if not isinstance(api_key, str) or not api_key:
        raise RuntimeError(f"{credentials_path} does not contain a usable apiKey")
    return api_key


def _write_selected_spec(original: dict, accounts: list[dict]) -> Path:
    handle = tempfile.NamedTemporaryFile("w", suffix=".toml", prefix="clifwrap-firecrawl-", delete=False)
    with handle:
        for key in ("provider", "target_env", "env_file", "env_name_template", "set_default"):
            if key in original:
                handle.write(f"{key} = {_toml_value(original[key])}\n")
        if "validation" in original:
            handle.write("\n[validation]\n")
            for key, value in original["validation"].items():
                handle.write(f"{key} = {_toml_value(value)}\n")
        for account in accounts:
            handle.write("\n[[accounts]]\n")
            for key, value in account.items():
                handle.write(f"{key} = {_toml_value(value)}\n")
    return Path(handle.name)


def _run_login(method: str | None, *, timeout_seconds: float | None) -> tuple[int, bool]:
    command = ["firecrawl", "login"]
    if method:
        command.extend(["--method", method])
    env = os.environ.copy()
    env["CLIFWRAP_BYPASS"] = "1"
    try:
        return subprocess.run(command, env=env, timeout=timeout_seconds).returncode, False
    except subprocess.TimeoutExpired:
        return 124, True


def _logout_current_credentials() -> None:
    env = os.environ.copy()
    env["CLIFWRAP_BYPASS"] = "1"
    subprocess.run(["firecrawl", "logout"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture multiple Firecrawl logins into declarative account sources used by clifwrap account import-spec."
    )
    parser.add_argument("--spec", default=str(DEFAULT_SPEC), help="TOML account spec to use for labels and storage template.")
    parser.add_argument("--env-file", help="Override the spec env_file destination.")
    parser.add_argument("--credentials", default=str(DEFAULT_CREDENTIALS), help="Firecrawl credentials.json path to read after each login.")
    parser.add_argument("--method", choices=["browser", "manual"], default="browser", help="Upstream Firecrawl login method.")
    parser.add_argument("--replace-existing", action="store_true", help="Re-login even when a stored source already exists for the account.")
    parser.add_argument("--assume-ready", action="store_true", help="Do not pause before each account login.")
    parser.add_argument("--max-accounts", type=int, help="Only process the first N accounts from the spec.")
    parser.add_argument("--login-timeout-seconds", type=float, help="Maximum time to keep each upstream login process open.")
    parser.add_argument("--continue-on-timeout", action="store_true", help="Continue to the next account when a login times out without new credentials.")
    parser.add_argument("--skip-import", action="store_true", help="Only capture keys; do not run clifwrap account import-spec --apply at the end.")
    args = parser.parse_args()

    spec_path = Path(args.spec)
    spec = _load_spec(spec_path)
    env_file = _expand(args.env_file or spec.get("env_file") or "~/.config/secrets.env")
    credentials_path = _expand(args.credentials)
    accounts = spec.get("accounts", [])
    if args.max_accounts is not None:
        accounts = accounts[: args.max_accounts]
    existing = _read_env_names(env_file)

    for account in accounts:
        label = account["label"] if "label" in account else account["name"]
        env_names = _env_names_for(spec, account)
        selected_env = env_names[0]
        already_configured = next((name for name in env_names if name in existing), None)
        if already_configured and not args.replace_existing:
            print(f"{label}: already has {already_configured}; skipping login and not printing the secret.", flush=True)
            continue
        print(f"{label}: prepare the upstream Firecrawl login for this account.", flush=True)
        print(f"{label}: after login succeeds, this script will store the API key as {selected_env} in {env_file}.", flush=True)
        if not args.assume_ready:
            input(f"Press Enter when ready to login as {label}...")
        _logout_current_credentials()
        before = credentials_path.read_text() if credentials_path.exists() else ""
        status, timed_out = _run_login(args.method, timeout_seconds=args.login_timeout_seconds)
        if status != 0 and not timed_out:
            print(f"{label}: firecrawl login exited with status {status}; stopping.", file=sys.stderr)
            return status
        if not credentials_path.exists():
            print(f"{label}: login completed but {credentials_path} was not created.", file=sys.stderr)
            return 1
        after = credentials_path.read_text()
        if after == before:
            if timed_out:
                print(f"{label}: login timed out after {args.login_timeout_seconds} seconds and credentials did not change.", file=sys.stderr)
                if args.continue_on_timeout:
                    continue
                print(f"{label}: stopping; pass --continue-on-timeout to open remaining account pages anyway.", file=sys.stderr)
                return 124
            print(f"{label}: warning: credentials file did not change; capturing current stored API key anyway.", flush=True)
        api_key = _read_api_key(credentials_path)
        _upsert_env(env_file, selected_env, api_key)
        existing.add(selected_env)
        print(f"{label}: captured API key into {selected_env}; secret value was not printed.", flush=True)

    if args.skip_import:
        return 0
    import_spec_path = _write_selected_spec(spec, accounts) if args.max_accounts is not None else spec_path
    command = ["clifwrap", "account", "import-spec", str(import_spec_path), "--env-file", str(env_file), "--apply"]
    return subprocess.run(command).returncode


if __name__ == "__main__":
    raise SystemExit(main())
