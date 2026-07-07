from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from . import __version__
from .accounts import append_account, ensure_config_exists, ensure_default_account_valid, import_account_spec, list_accounts, parse_assignments, parse_command_assignments, remove_account, rename_account, set_account_enabled
from .config import SHIM_ENV, config_path, load_config, shim_bin_dir, state_dir
from .install import install_shim, is_managed_shim, load_state, uninstall_shim
from .runtime import replay_queue, run_app, status_all, status_for
from .state import drop_queue_items, get_default_account, list_queue_items, set_default_account


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="clifwrap", description="Transparent CLI failover wrapper.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    install_parser = subparsers.add_parser("install", help="Install managed shims in front of existing CLIs.")
    install_parser.add_argument("apps", nargs="*", help="Commands to wrap.")

    uninstall_parser = subparsers.add_parser("uninstall", help="Restore original CLIs.")
    uninstall_parser.add_argument("apps", nargs="*", help="Commands to unwrap")

    status_parser = subparsers.add_parser("status", help="Show configured account status for wrapped apps.")
    status_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    status_parser.add_argument("--check", action="store_true", help="Exit nonzero when any reported provider has low fallback or recovery-hook error state.")
    status_parser.add_argument("app", nargs="?", help="Wrapped command name. Omit to show every configured provider.")

    doctor_parser = subparsers.add_parser("doctor", help="Inspect local config, state, shims, and queue health.")
    doctor_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    doctor_parser.add_argument("--check", action="store_true", help="Exit nonzero when doctor finds config, shim, or queue issues.")

    init_parser = subparsers.add_parser("init", help="Create an empty config file if it does not exist.")
    init_parser.add_argument("--force", action="store_true", help="Overwrite the existing config with an empty config.")

    config_parser = subparsers.add_parser("config", help="Inspect and validate clifwrap configuration.")
    config_subparsers = config_parser.add_subparsers(dest="config_command")
    config_paths = config_subparsers.add_parser("paths", help="Show resolved config, state, and shim-bin paths.")
    config_paths.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    config_validate = config_subparsers.add_parser("validate", help="Validate config.toml with the runtime parser.")
    config_validate.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    account_parser = subparsers.add_parser("account", help="Manage provider accounts in config.toml.")
    account_subparsers = account_parser.add_subparsers(dest="account_command")
    account_list = account_subparsers.add_parser("list", help="List configured accounts.")
    account_list.add_argument("app", nargs="?", help="Optional provider to list.")
    account_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON without secret values.")
    account_add = account_subparsers.add_parser("add", help="Append a provider account to config.toml.")
    account_add.add_argument("app", help="Provider command.")
    account_add.add_argument("name", help="Account label.")
    account_add.add_argument("--env-file", action="append", default=[], help="Read KEY=VALUE secrets from this file.")
    account_add.add_argument("--env", action="append", default=[], help="Persist KEY=VALUE directly in config.")
    account_add.add_argument("--env-ref", action="append", default=[], help="Store KEY=ENVVAR as KEY = env:ENVVAR.")
    account_add.add_argument("--env-command", action="append", default=[], help="Store KEY='command args...' and resolve it before each attempt.")
    account_add.add_argument("--prepare-command", nargs="+", help="Auth/preparation command to run before this account.")
    account_add.add_argument("--prepare-on", default="always", choices=["always", "once", "never"], help="When to run --prepare-command.")
    account_add.add_argument("--disabled", action="store_true", help="Add the account disabled.")
    account_use = account_subparsers.add_parser("use", help="Set the default account for a provider.")
    account_use.add_argument("app", help="Provider command.")
    account_use.add_argument("name", help="Account label.")
    account_default = account_subparsers.add_parser("default", help="Show the default account for a provider.")
    account_default.add_argument("app", help="Provider command.")
    account_rename = account_subparsers.add_parser("rename", help="Rename an account in config.toml.")
    account_rename.add_argument("app", help="Provider command.")
    account_rename.add_argument("old_name", help="Current account label.")
    account_rename.add_argument("new_name", help="New account label.")
    account_import = account_subparsers.add_parser("import-spec", help="Import provider accounts from a declarative TOML spec.")
    account_import.add_argument("spec", help="Path to account spec TOML.")
    account_import.add_argument("--env-file", help="Override spec env_file.")
    account_import.add_argument("--apply", action="store_true", help="Write changes. Without this, only print the planned import.")
    for command_name, help_text in {
        "enable": "Enable an account in config.toml.",
        "disable": "Disable an account in config.toml.",
        "remove": "Remove an account from config.toml.",
    }.items():
        account_edit = account_subparsers.add_parser(command_name, help=help_text)
        account_edit.add_argument("app", help="Provider command.")
        account_edit.add_argument("name", help="Account label.")

    queue_parser = subparsers.add_parser("queue", help="Inspect and manage deferred work.")
    queue_subparsers = queue_parser.add_subparsers(dest="queue_command")
    queue_list = queue_subparsers.add_parser("list", help="List queued work.")
    queue_list.add_argument("app", nargs="?", help="Optional provider to filter.")
    queue_list.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    queue_run = queue_subparsers.add_parser("run", help="Replay queued work.")
    queue_run.add_argument("app", nargs="?", help="Optional provider to filter.")
    queue_run.add_argument("--id", help="Replay only one queue item.")
    queue_run.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    queue_drop = queue_subparsers.add_parser("drop", help="Drop queued work.")
    queue_drop.add_argument("app", nargs="?", help="Optional provider to filter.")
    queue_drop.add_argument("ids", nargs="*", help="Specific queue item ids to remove.")
    queue_drop.add_argument("--expired", action="store_true", help="Drop only expired queue items.")
    queue_drop.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    subparsers.add_parser("sample-config", help="Print a sample config.toml")

    return parser


SAMPLE_CONFIG = """version = 1

[[providers.somecli.accounts]]
name = "primary"
env = { SOMECLI_TOKEN = "env:SOMECLI_TOKEN_PRIMARY" }

[[providers.somecli.accounts]]
name = "secondary"
env = { SOMECLI_TOKEN = "env:SOMECLI_TOKEN_SECONDARY" }

[providers.somecli.fallback_monitor]
threshold = 3
action = "warn"
journald = true
syslog = true
stderr = true
recovery_command = ["notify-send", "somecli fallbacks are low"]

[providers.somecli.capacity_control]
default_action = "queue"
unknown_capacity_action = "allow"
reserve_threshold = 5
default_cost = 1
queue_retention_seconds = 86400
queue_max_items = 100
snapshot_ttl_seconds = 60
command_costs = { search = 2, extract = 5 }
remediation_message = "Provision more capacity or enable another account."
remediation_commands = ["clifwrap account add somecli <name> --env-ref SOMECLI_TOKEN=ENVVAR"]

[[providers.somecli.accounts]]
name = "persisted-login"
prepare_on = "once"
env = { SOMECLI_TOKEN = "env:SOMECLI_TOKEN" }
prepare_command = ["somecli", "login", "--token", "${SOMECLI_TOKEN}"]
"""


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "shim":
        app = os.environ.get(SHIM_ENV)
        if not app:
            raise SystemExit("missing shim app name")
        return run_app(app, argv[1:])
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "install":
        apps = args.apps or sorted(load_config().providers)
        if not apps:
            raise SystemExit("No apps supplied and no [providers] configured")
        for app in apps:
            shim = install_shim(app, bin_dir=shim_bin_dir())
            print(f"{app}: installed shim at {shim.target}")
            print(f"{app}: backup stored at {shim.backup}")
        return 0
    if args.command == "uninstall":
        apps = args.apps or sorted(load_state())
        if not apps:
            raise SystemExit("No apps supplied and no installed shims recorded")
        for app in apps:
            shim = uninstall_shim(app)
            print(f"{app}: restored original command at {shim.target}")
        return 0
    if args.command == "status":
        if args.app is None:
            return status_all(json_output=args.json, check=args.check)
        return status_for(args.app, json_output=args.json, check=args.check)
    if args.command == "doctor":
        return _doctor(json_output=args.json, check=args.check)
    if args.command == "init":
        path = ensure_config_exists(force=args.force)
        print(f"config: {path}")
        return 0
    if args.command == "config":
        if args.config_command == "paths":
            return _config_paths(json_output=args.json)
        if args.config_command == "validate":
            return _config_validate(json_output=args.json)
        raise SystemExit("config requires a subcommand")
    if args.command == "account":
        if args.account_command == "list":
            rows = list_accounts(args.app)
            if args.json:
                payload = {
                    "accounts": [
                        {
                            "provider": provider_name,
                            "name": account.name,
                            "enabled": account.enabled,
                            "default": get_default_account(provider_name) == account.name,
                            "env_keys": sorted(account.env),
                            "env_command_keys": sorted(account.env_command),
                            "env_files": list(account.env_files),
                            "prepare_on": account.prepare_on,
                            "has_prepare_command": bool(account.prepare_command),
                        }
                        for provider_name, account in rows
                    ]
                }
                sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                return 0
            if not rows:
                print("no configured accounts")
                return 0
            for provider_name, account in rows:
                state = "enabled" if account.enabled else "disabled"
                env_keys = ", ".join(sorted({*account.env, *account.env_command})) or "no env"
                print(f"{provider_name}:{account.name} [{state}] {env_keys}")
            return 0
        if args.account_command == "add":
            env = parse_assignments(args.env)
            env_refs = parse_assignments(args.env_ref)
            env_command = parse_command_assignments(args.env_command)
            path = append_account(
                args.app,
                args.name,
                env_files=args.env_file,
                env=env,
                env_refs=env_refs,
                env_command=env_command,
                prepare_command=args.prepare_command,
                prepare_on=args.prepare_on,
                enabled=not args.disabled,
            )
            print(f"account added: {args.app}:{args.name}")
            print(f"config: {path}")
            return 0
        if args.account_command == "use":
            names = {account.name for provider_name, account in list_accounts(args.app) if provider_name == args.app and account.enabled}
            if args.name not in names:
                raise SystemExit(f"no enabled account named {args.name!r} for provider {args.app!r}")
            set_default_account(args.app, args.name)
            print(f"{args.app}: default account set to {args.name}")
            return 0
        if args.account_command == "default":
            default = get_default_account(args.app)
            print(f"{args.app}: default account: {default}" if default else f"{args.app}: no default account")
            return 0
        if args.account_command == "enable":
            path = set_account_enabled(args.app, args.name, enabled=True)
            print(f"{args.app}: account enabled: {args.name}")
            print(f"config: {path}")
            return 0
        if args.account_command == "disable":
            path = set_account_enabled(args.app, args.name, enabled=False)
            print(f"{args.app}: account disabled: {args.name}")
            reconciled = ensure_default_account_valid(args.app)
            print(f"{args.app}: default account set to {reconciled}" if reconciled else f"{args.app}: default account cleared")
            print(f"config: {path}")
            return 0
        if args.account_command == "remove":
            path = remove_account(args.app, args.name)
            print(f"{args.app}: account removed: {args.name}")
            reconciled = ensure_default_account_valid(args.app)
            print(f"{args.app}: default account set to {reconciled}" if reconciled else f"{args.app}: default account cleared")
            print(f"config: {path}")
            return 0
        if args.account_command == "rename":
            path = rename_account(args.app, args.old_name, args.new_name)
            if get_default_account(args.app) == args.old_name:
                set_default_account(args.app, args.new_name)
            print(f"{args.app}: account renamed: {args.old_name} -> {args.new_name}")
            print(f"config: {path}")
            return 0
        if args.account_command == "import-spec":
            results = import_account_spec(Path(args.spec), env_file_override=args.env_file, dry_run=not args.apply)
            for result in results:
                print(f"{result.account}: {result.status}: {result.detail}")
            return 0 if all(result.status not in {"missing", "invalid"} for result in results) else 2
        raise SystemExit("account requires a subcommand")
    if args.command == "queue":
        if args.queue_command == "list":
            try:
                items = list_queue_items(args.app)
            except ValueError as exc:
                if args.json:
                    sys.stdout.write(json.dumps({"error": str(exc), "items": []}, indent=2, sort_keys=True) + "\n")
                    return 1
                raise SystemExit(str(exc))
            payload = {
                "items": [
                    {
                        "id": item.id,
                        "provider": item.provider,
                        "argv": item.argv,
                        "enqueued_at": item.enqueued_at,
                        "replay_count": item.replay_count,
                        "reason": item.reason,
                        "expires_at": item.expires_at,
                        "last_replayed_at": item.last_replayed_at,
                    }
                    for item in items
                ]
            }
            if args.json:
                sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                return 0
            if not items:
                print("no queued items")
                return 0
            for item in sorted(items, key=lambda current: (current.enqueued_at, current.id)):
                print(
                    f"{item.id} {item.provider} replay_count={item.replay_count} "
                    f"argv={' '.join(item.argv)} reason={item.reason}"
                )
            return 0
        if args.queue_command == "run":
            return replay_queue(args.app, item_id=args.id, json_output=args.json)
        if args.queue_command == "drop":
            try:
                dropped = drop_queue_items(set(args.ids) if args.ids else None, provider=args.app, expired_only=args.expired)
            except ValueError as exc:
                if args.json:
                    sys.stdout.write(json.dumps({"dropped": [], "error": str(exc)}, indent=2, sort_keys=True) + "\n")
                    return 1
                raise SystemExit(str(exc))
            payload = {
                "dropped": [
                    {"id": item.id, "provider": item.provider, "argv": item.argv}
                    for item in dropped
                ]
            }
            if args.json:
                sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                return 0
            if not dropped:
                print("no queued items dropped")
                return 0
            for item in dropped:
                print(f"dropped {item.id} {item.provider} {' '.join(item.argv)}")
            return 0
        raise SystemExit("queue requires a subcommand")
    if args.command == "sample-config":
        sys.stdout.write(SAMPLE_CONFIG)
        return 0
    parser.print_help()
    print()
    print(f"Config path: {config_path()}")
    return 0


def _config_paths_payload() -> dict[str, str]:
    return {
        "config": str(config_path()),
        "state_dir": str(state_dir()),
        "bin_dir": str(shim_bin_dir()),
    }


def _config_paths(*, json_output: bool) -> int:
    payload = _config_paths_payload()
    if json_output:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return 0
    print(f"config: {payload['config']}")
    print(f"state: {payload['state_dir']}")
    print(f"bin: {payload['bin_dir']}")
    return 0


def _config_validate_payload() -> dict[str, object]:
    path = config_path()
    try:
        config = load_config()
    except Exception as exc:
        return {
            "path": str(path),
            "exists": path.exists(),
            "valid": False,
            "error": str(exc),
            "providers": 0,
            "accounts": 0,
            "enabled_accounts": 0,
        }
    accounts = [account for provider in config.providers.values() for account in provider.accounts]
    return {
        "path": str(path),
        "exists": path.exists(),
        "valid": True,
        "error": None,
        "providers": len(config.providers),
        "accounts": len(accounts),
        "enabled_accounts": sum(1 for account in accounts if account.enabled),
    }


def _config_validate(*, json_output: bool) -> int:
    payload = _config_validate_payload()
    if json_output:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    elif payload["valid"]:
        print(f"config: {payload['path']} ({'present' if payload['exists'] else 'missing, using defaults'})")
        print(
            f"valid: providers={payload['providers']} accounts={payload['accounts']} "
            f"enabled_accounts={payload['enabled_accounts']}"
        )
    else:
        print(f"config: {payload['path']} ({'present' if payload['exists'] else 'missing'})")
        print(f"invalid: {payload['error']}")
    return 0 if payload["valid"] else 1


def _doctor_payload() -> dict[str, object]:
    config_file = config_path()
    state_directory = state_dir()
    bin_directory = shim_bin_dir()
    issues: list[str] = []
    providers = []
    try:
        config = load_config()
        config_error = None
    except Exception as exc:
        config = None
        config_error = str(exc)
        issues.append(f"config is invalid: {exc}")
    try:
        install_state = load_state()
    except Exception as exc:
        install_state = {}
        issues.append(f"install state is invalid: {exc}")
    queue_error = None
    queue_pending = 0
    queue_expired = 0
    try:
        queue_items = list_queue_items()
        queue_pending = len(queue_items)
        now = int(time.time())
        queue_expired = sum(1 for item in queue_items if item.expires_at <= now)
    except ValueError as exc:
        queue_error = str(exc)
        issues.append(queue_error)
    if config is not None:
        for provider_name, provider in sorted(config.providers.items()):
            enabled_accounts = [account.name for account in provider.accounts if account.enabled]
            default_account = get_default_account(provider_name)
            if default_account and default_account not in {account.name for account in provider.accounts}:
                issues.append(f"{provider_name}: default account {default_account!r} is not configured")
            elif default_account and default_account not in enabled_accounts:
                issues.append(f"{provider_name}: default account {default_account!r} is disabled")
            shim_info = install_state.get(provider_name)
            shim_payload: dict[str, object]
            if shim_info:
                target = Path(shim_info.get("target", ""))
                backup = Path(shim_info.get("backup", ""))
                target_exists = target.exists()
                backup_exists = backup.exists()
                target_managed = is_managed_shim(target)
                if not target_exists:
                    issues.append(f"{provider_name}: recorded shim target is missing at {target}")
                elif not target_managed:
                    issues.append(f"{provider_name}: recorded shim target is not managed at {target}")
                if not backup_exists:
                    issues.append(f"{provider_name}: recorded original backup is missing at {backup}")
                shim_payload = {
                    "recorded": True,
                    "target": str(target),
                    "backup": str(backup),
                    "target_exists": target_exists,
                    "target_managed": target_managed,
                    "backup_exists": backup_exists,
                }
            else:
                shim_payload = {
                    "recorded": False,
                    "target": None,
                    "backup": None,
                    "target_exists": False,
                    "target_managed": False,
                    "backup_exists": False,
                }
            providers.append(
                {
                    "name": provider_name,
                    "accounts": len(provider.accounts),
                    "enabled_accounts": len(enabled_accounts),
                    "default_account": default_account,
                    "has_capacity_control": provider.capacity_control is not None,
                    "has_fallback_monitor": provider.fallback_monitor is not None,
                    "shim": shim_payload,
                }
            )
    return {
        "paths": {
            "config": str(config_file),
            "state_dir": str(state_directory),
            "bin_dir": str(bin_directory),
        },
        "config": {
            "exists": config_file.exists(),
            "valid": config_error is None,
            "error": config_error,
        },
        "providers": providers,
        "queue": {
            "pending": queue_pending,
            "expired": queue_expired,
            "error": queue_error,
        },
        "issues": issues,
    }


def _doctor(*, json_output: bool, check: bool) -> int:
    payload = _doctor_payload()
    if json_output:
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    else:
        paths = payload["paths"]
        config_state = payload["config"]
        queue_state = payload["queue"]
        assert isinstance(paths, dict)
        assert isinstance(config_state, dict)
        assert isinstance(queue_state, dict)
        print(f"config: {paths['config']} ({'valid' if config_state['valid'] else 'invalid'})")
        print(f"state: {paths['state_dir']}")
        print(f"bin: {paths['bin_dir']}")
        for provider in payload["providers"]:
            assert isinstance(provider, dict)
            shim = provider["shim"]
            assert isinstance(shim, dict)
            shim_state = "installed" if shim["recorded"] and shim["target_managed"] and shim["backup_exists"] else "not installed"
            print(
                f"{provider['name']}: accounts={provider['accounts']} enabled={provider['enabled_accounts']} "
                f"default={provider['default_account'] or '-'} shim={shim_state}"
            )
        print(f"queue: pending={queue_state['pending']} expired={queue_state['expired']}")
        issues = payload["issues"]
        if issues:
            print("issues:")
            for issue in issues:
                print(f"- {issue}")
        else:
            print("issues: none")
    return 1 if check and payload["issues"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
