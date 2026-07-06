from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .accounts import append_account, ensure_default_account_valid, list_accounts, parse_assignments, parse_command_assignments, remove_account, rename_account, set_account_enabled
from .config import BYPASS_ENV, CURRENT_ACCOUNT_ENV, AccountConfig, ProviderConfig, WrapperConfig, load_config, merged_provider, state_dir
from .install import original_command_for
from .scheduling import admission_decision, capacity_snapshots, has_capacity_control
from .state import QueueItem, drop_queue_items, get_default_account, list_queue_items, replace_queue_item, set_default_account


@dataclass
class AttemptResult:
    exit_code: int
    stdout: bytes
    stderr: bytes
    account_name: str | None
    retryable: bool
    reason: str | None = None


@dataclass
class LowFallbackStatus:
    enabled_accounts: list[AccountConfig]
    default_account: str | None
    fallback_count: int
    threshold: int
    message: str
    signature: str


_STDIN_UNSET = object()
MANAGED_AUTH_ACTIONS = {"accounts", "list", "default", "use", "add", "rename", "enable", "disable", "remove"}


def _enabled_accounts(provider: ProviderConfig) -> list[AccountConfig]:
    return [account for account in provider.accounts if account.enabled]


def _active_account_name(provider: ProviderConfig, enabled: list[AccountConfig]) -> str | None:
    selected = os.environ.get(CURRENT_ACCOUNT_ENV) or get_default_account(provider.name)
    if selected and any(account.name == selected for account in enabled):
        return selected
    return enabled[0].name if enabled else None


def _fallback_monitor(provider: ProviderConfig) -> LowFallbackStatus | None:
    monitor = provider.fallback_monitor
    if not monitor:
        return None
    enabled = _enabled_accounts(provider)
    fallback_count = max(len(enabled) - 1, 0)
    active = _active_account_name(provider, enabled)
    if fallback_count >= monitor.threshold:
        return None
    signature = hashlib.sha256(
        json.dumps(
            {
                "provider": provider.name,
                "enabled": [account.name for account in enabled],
                "active": active,
                "fallback_count": fallback_count,
                "threshold": monitor.threshold,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    names = ", ".join(account.name for account in enabled) or "no enabled accounts"
    active_text = active or "none"
    message = (
        f"[clifwrap] {provider.name} low fallback pool: "
        f"enabled_accounts={len(enabled)}, unused_fallbacks={fallback_count}, threshold={monitor.threshold}, "
        f"active={active_text}, enabled={names}; "
        f"remediation=add or enable accounts with `clifwrap account add {provider.name} <name>` "
        f"or lower CLIFWRAP_PROVIDER_{provider.name.upper()}_FALLBACK_THRESHOLD if this is expected"
    )
    return LowFallbackStatus(
        enabled_accounts=enabled,
        default_account=active,
        fallback_count=fallback_count,
        threshold=monitor.threshold,
        message=message,
        signature=signature,
    )


def _alert_state_path(provider: str) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in provider)
    return state_dir() / "alerts" / "low-fallback" / f"{safe}.json"


def _load_alert_state(provider: str) -> dict[str, str]:
    path = _alert_state_path(provider)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {str(key): str(value) for key, value in payload.items() if isinstance(value, str)}


def _save_alert_state(provider: str, state: dict[str, str]) -> None:
    path = _alert_state_path(provider)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _emit_syslog(message: str) -> bool:
    try:
        import syslog
    except Exception:
        return False
    try:
        syslog.openlog("clifwrap", syslog.LOG_PID, syslog.LOG_USER)
        syslog.syslog(syslog.LOG_WARNING, message)
        return True
    except Exception:
        return False


def _emit_journald(message: str) -> bool:
    systemd_cat = shutil.which("systemd-cat")
    if not systemd_cat:
        return False
    try:
        subprocess.run(
            [systemd_cat, "--identifier=clifwrap", "--priority=warning"],
            input=message + "\n",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        return True
    except Exception:
        return False


def _emit_stderr(message: str, *, always: bool = False) -> None:
    if always or sys.stderr.isatty():
        sys.stderr.write(message + "\n")


def _run_recovery_command(provider: ProviderConfig, status: LowFallbackStatus) -> None:
    monitor = provider.fallback_monitor
    if not monitor or not monitor.recovery_command:
        return
    alert_state = _load_alert_state(provider.name)
    if alert_state.get("signature") == status.signature:
        return
    env = dict(os.environ)
    env.update(
        {
            "CLIFWRAP_PROVIDER": provider.name,
            "CLIFWRAP_LOW_FALLBACK_MESSAGE": status.message,
            "CLIFWRAP_LOW_FALLBACK_ENABLED": str(len(status.enabled_accounts)),
            "CLIFWRAP_LOW_FALLBACK_UNUSED": str(status.fallback_count),
            "CLIFWRAP_LOW_FALLBACK_THRESHOLD": str(status.threshold),
            "CLIFWRAP_LOW_FALLBACK_ACTIVE": status.default_account or "",
            "CLIFWRAP_LOW_FALLBACK_ENABLED_NAMES": ",".join(account.name for account in status.enabled_accounts),
            BYPASS_ENV: "1",
        }
    )
    try:
        subprocess.Popen(monitor.recovery_command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        detail = str(exc)
        alert_state["signature"] = status.signature
        alert_state["recovery_error"] = detail
        alert_state["recovery_failed_at"] = str(int(time.time()))
        _save_alert_state(provider.name, alert_state)
        _emit_stderr(f"[clifwrap] {provider.name} low-fallback recovery hook failed: {detail}", always=True)
        return
    alert_state["signature"] = status.signature
    alert_state["triggered_at"] = str(int(time.time()))
    alert_state.pop("recovery_error", None)
    alert_state.pop("recovery_failed_at", None)
    _save_alert_state(provider.name, alert_state)


def _check_low_fallbacks(provider: ProviderConfig) -> LowFallbackStatus | None:
    status = _fallback_monitor(provider)
    if not status:
        return None
    monitor = provider.fallback_monitor
    if monitor and monitor.journald:
        _emit_journald(status.message)
    if monitor and monitor.syslog:
        _emit_syslog(status.message)
    if monitor and monitor.stderr:
        _emit_stderr(status.message)
    _run_recovery_command(provider, status)
    return status


def _resolve_value(value: str, env: dict[str, str]) -> str:
    if value.startswith("env:"):
        name = value[4:]
        resolved = env.get(name)
        if resolved is None:
            raise RuntimeError(f"Missing environment variable {name}")
        return resolved
    return os.path.expandvars(value)


def _load_env_file(path: str) -> dict[str, str]:
    resolved_path = Path(os.path.expandvars(os.path.expanduser(path)))
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(resolved_path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise RuntimeError(f"{resolved_path}:{line_number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            raise RuntimeError(f"{resolved_path}:{line_number}: empty key")
        try:
            parts = shlex.split(value, comments=False, posix=True)
        except ValueError as exc:
            raise RuntimeError(f"{resolved_path}:{line_number}: {exc}") from exc
        values[key] = parts[0] if parts else ""
    return values


def _interpolate(parts: list[str], env: dict[str, str]) -> list[str]:
    rendered: list[str] = []
    for part in parts:
        current = part
        for key, value in env.items():
            current = current.replace(f"${{{key}}}", value)
        rendered.append(current)
    return rendered


def _account_env(account: AccountConfig) -> dict[str, str]:
    source_env = dict(os.environ)
    for env_file in account.env_files:
        source_env.update(_load_env_file(env_file))
    resolved = {key: _resolve_value(value, source_env) for key, value in account.env.items()}
    for key, command in account.env_command.items():
        proc = subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        resolved[key] = proc.stdout.strip()
    return resolved


def _provider_for(app: str, config: WrapperConfig) -> ProviderConfig:
    raw = config.providers.get(app)
    return merged_provider(app, raw)


def _original_command(app: str, provider: ProviderConfig) -> list[str]:
    if provider.command:
        return provider.command
    command = original_command_for(app)
    if command is None:
        raise FileNotFoundError(f"Could not resolve original command for '{app}'")
    return command


def _is_retryable(provider: ProviderConfig, result: AttemptResult) -> AttemptResult:
    combined = b"\n".join([result.stdout, result.stderr]).decode("utf-8", errors="ignore").lower()
    for pattern in provider.never_retry_patterns:
        if pattern and pattern in combined:
            result.retryable = False
            result.reason = f"matched non-retryable pattern: {pattern}"
            return result
    for pattern in provider.retry_patterns:
        if pattern and pattern in combined:
            result.retryable = True
            result.reason = f"matched retry pattern: {pattern}"
            return result
    if result.exit_code in provider.retry_exit_codes:
        result.retryable = True
        result.reason = f"matched retry exit code: {result.exit_code}"
        return result
    if provider.retry_on_any_error and result.exit_code != 0:
        result.retryable = True
        result.reason = "provider.retry_on_any_error"
        return result
    result.retryable = False
    return result


def _prepare_marker(provider: ProviderConfig, account: AccountConfig, command: list[str]) -> tuple[Path, str]:
    payload = json.dumps({"provider": provider.name, "account": account.name, "command": command}, sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    safe_provider = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in provider.name)
    safe_account = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in account.name)
    marker = state_dir() / "prepared" / safe_provider / f"{safe_account}.json"
    return marker, digest


def _prepare_account(provider: ProviderConfig, account: AccountConfig, merged_env: dict[str, str]) -> None:
    if not account.prepare_command:
        return
    if account.prepare_on == "never":
        return
    command = _interpolate(account.prepare_command, merged_env)
    if account.prepare_on == "once":
        marker, digest = _prepare_marker(provider, account, command)
        if marker.exists():
            try:
                if json.loads(marker.read_text()).get("digest") == digest:
                    return
            except (OSError, json.JSONDecodeError):
                pass
    subprocess.run(command, env=merged_env, check=True)
    if account.prepare_on == "once":
        marker, digest = _prepare_marker(provider, account, command)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps({"digest": digest}, indent=2) + "\n")


def _capture_stdin() -> bytes | None:
    if sys.stdin.isatty():
        return None
    return sys.stdin.buffer.read()


def _exec_original(app: str, command: list[str], args: list[str], env: dict[str, str]) -> None:
    argv = [app, *command[1:], *args]
    os.execvpe(command[0], argv, env)


def _run_once(
    app: str,
    command: list[str],
    args: list[str],
    env: dict[str, str],
    stdin_data: bytes | None,
    *,
    account_name: str | None,
) -> AttemptResult:
    argv = [app, *command[1:], *args]
    proc = subprocess.run(
        argv,
        input=stdin_data,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        executable=command[0],
    )
    return AttemptResult(
        exit_code=proc.returncode,
        stdout=proc.stdout,
        stderr=proc.stderr,
        account_name=account_name,
        retryable=False,
    )


def _print_failover(account_name: str, reason: str | None, next_name: str) -> None:
    message = f"[clifwrap] {account_name} failed"
    if reason:
        message += f" ({reason})"
    message += f"; retrying with {next_name}\n"
    sys.stderr.write(message)


def _ordered_accounts(provider: ProviderConfig, allowed_names: set[str] | None = None) -> list[AccountConfig]:
    accounts = [account for account in provider.accounts if account.enabled]
    if allowed_names is not None:
        accounts = [account for account in accounts if account.name in allowed_names]
    selected = os.environ.get(CURRENT_ACCOUNT_ENV) or get_default_account(provider.name)
    if not selected:
        return accounts
    for index, account in enumerate(accounts):
        if account.name == selected:
            return accounts[index:] + accounts[:index]
    return accounts


def _account_capacity_status(provider: ProviderConfig, account: AccountConfig) -> tuple[str, int | None]:
    env = _account_env(account)
    usage_status = _usage_status(provider, account, env)
    if usage_status:
        return usage_status
    return _generic_status(provider, account, env)


def _admission(provider: ProviderConfig, args: list[str], stdin_data: bytes | None, *, existing_item: QueueItem | None = None):
    accounts = _ordered_accounts(provider)
    active = _active_account_name(provider, accounts)
    snapshots = capacity_snapshots(provider, accounts, lambda account: _account_capacity_status(provider, account))
    decision = admission_decision(
        provider,
        args,
        snapshots,
        active_account_name=active,
        stdin_data=stdin_data,
        existing_item=existing_item,
    )
    if decision.action == "execute" and decision.account_name and decision.account_name != active:
        set_default_account(provider.name, decision.account_name)
    return decision


def _run_attempts(
    app: str,
    provider: ProviderConfig,
    args: list[str],
    stdin_data: bytes | None | object = _STDIN_UNSET,
    *,
    emit_output: bool = True,
    allowed_account_names: set[str] | None = None,
) -> int:
    command = _original_command(app, provider)
    accounts = _ordered_accounts(provider, allowed_account_names)
    if not accounts:
        env = dict(os.environ)
        env[BYPASS_ENV] = "1"
        _exec_original(app, command, args, env)
    if stdin_data is _STDIN_UNSET:
        stdin_data = _capture_stdin()

    last_result: AttemptResult | None = None
    for index, account in enumerate(accounts):
        env = dict(os.environ)
        try:
            account_env = _account_env(account)
        except Exception as exc:
            result = AttemptResult(
                exit_code=1,
                stdout=b"",
                stderr=f"[clifwrap] {account.name} configuration error: {exc}\n".encode("utf-8"),
                account_name=account.name,
                retryable=True,
                reason="account configuration error",
            )
            last_result = result
            if index + 1 < len(accounts):
                set_default_account(provider.name, accounts[index + 1].name)
                _print_failover(account.name, result.reason, accounts[index + 1].name)
                continue
            sys.stderr.buffer.write(result.stderr)
            return result.exit_code
        env.update(account_env)
        env[BYPASS_ENV] = "1"
        env[CURRENT_ACCOUNT_ENV] = account.name
        try:
            _prepare_account(provider, account, env)
        except Exception as exc:
            result = AttemptResult(
                exit_code=1,
                stdout=b"",
                stderr=f"[clifwrap] {account.name} preparation error: {exc}\n".encode("utf-8"),
                account_name=account.name,
                retryable=True,
                reason="account preparation error",
            )
            last_result = result
            if index + 1 < len(accounts):
                set_default_account(provider.name, accounts[index + 1].name)
                _print_failover(account.name, result.reason, accounts[index + 1].name)
                continue
            sys.stderr.buffer.write(result.stderr)
            return result.exit_code
        result = _run_once(app, command, args, env, stdin_data, account_name=account.name)
        if result.exit_code == 0:
            if emit_output:
                sys.stdout.buffer.write(result.stdout)
                sys.stderr.buffer.write(result.stderr)
            return 0
        result = _is_retryable(provider, result)
        last_result = result
        if result.retryable and index + 1 < len(accounts):
            set_default_account(provider.name, accounts[index + 1].name)
            _print_failover(account.name, result.reason, accounts[index + 1].name)
            continue
        if emit_output:
            sys.stdout.buffer.write(result.stdout)
            sys.stderr.buffer.write(result.stderr)
        return result.exit_code
    return last_result.exit_code if last_result else 1


def _run_line_repl(app: str, provider: ProviderConfig) -> int:
    version_command = [*_original_command(app, provider), "--version"]
    version_result = subprocess.run(version_command, env={**os.environ, BYPASS_ENV: "1"}, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    version = version_result.stdout.strip() or app
    sys.stderr.write("\n")
    sys.stderr.write(f"clifwrap active for {app}\n")
    sys.stderr.write(f"version: {version}\n")
    if provider.accounts:
        sys.stderr.write("accounts: " + ", ".join(account.name for account in provider.accounts if account.enabled) + "\n")
    sys.stderr.write('tips: type an upstream command, "help", or "exit"\n\n')
    while True:
        try:
            line = input("> ")
        except EOFError:
            sys.stderr.write("\n")
            return 0
        except KeyboardInterrupt:
            sys.stderr.write("\n")
            return 130
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in {"exit", "quit", "q"}:
            sys.stderr.write("Goodbye!\n")
            return 0
        if stripped in {"help", "?"}:
            sys.stderr.write(f"commands are forwarded to {app}; wrapper commands: help/exit\n")
            continue
        try:
            parsed = shlex.split(stripped)
        except ValueError as exc:
            sys.stderr.write(f"parse error: {exc}\n")
            continue
        if parsed and parsed[0] == app:
            parsed = parsed[1:]
        exit_code = _run_attempts(app, provider, parsed)
        if exit_code not in (0, 3, 4, 130):
            sys.stderr.write(f"[clifwrap] command exited with status {exit_code}\n")


def _managed_auth_usage(app: str, command: str) -> str:
    return (
        f"usage: {app} {command} list|accounts|default|use <account>|add <account>|rename <old> <new>|enable <account>|disable <account>|remove <account> "
        "[--env-file PATH] [--env KEY=VALUE] [--env-ref KEY=ENVVAR] [--env-command KEY='command args...'] [--disabled]\n"
    )


def _should_passthrough_command(provider: ProviderConfig, args: list[str]) -> bool:
    if not args:
        return False
    if args[0] not in provider.passthrough_commands:
        return False
    auth = provider.auth_management
    if auth and args[0] == auth.command and len(args) >= 2:
        return args[1] not in MANAGED_AUTH_ACTIONS
    return True


def _managed_auth_names(provider: ProviderConfig) -> set[str]:
    auth = provider.auth_management
    if not auth:
        return set()
    return {auth.command, *auth.aliases}


def _handle_managed_auth(app: str, provider: ProviderConfig, args: list[str]) -> int | None:
    auth = provider.auth_management
    if not auth or not args or args[0] not in _managed_auth_names(provider):
        return None
    if len(args) == 1:
        if args[0] == auth.command:
            return None
        action = "list"
        action_args = args
    else:
        action = args[1]
        action_args = args
    if action not in MANAGED_AUTH_ACTIONS:
        return None
    accounts = [account for name, account in list_accounts(app) if name == app]
    default = get_default_account(app)
    if action in {"accounts", "list"}:
        if not accounts:
            sys.stdout.write(f"{app}: no configured accounts\n")
            return 0
        sys.stdout.write(f"{app}: {len(accounts)} configured account(s)\n")
        for account in accounts:
            marker = "*" if account.name == default else " "
            state = "enabled" if account.enabled else "disabled"
            env_keys = ", ".join(sorted({*account.env, *account.env_command})) or "no env"
            sys.stdout.write(f"{marker} {account.name} [{state}] {env_keys}\n")
        return 0
    if action == "default":
        sys.stdout.write(f"{app}: default account: {default}\n" if default else f"{app}: no default account\n")
        return 0
    if action == "use":
        if len(action_args) != 3:
            sys.stderr.write(_managed_auth_usage(app, auth.command))
            return 2
        wanted = action_args[2]
        if wanted not in {account.name for account in accounts if account.enabled}:
            sys.stderr.write(f"{app}: no enabled account named {wanted!r}\n")
            return 2
        set_default_account(app, wanted)
        sys.stdout.write(f"{app}: default account set to {wanted}\n")
        return 0
    if action in {"enable", "disable", "remove"}:
        if len(action_args) != 3:
            sys.stderr.write(_managed_auth_usage(app, auth.command))
            return 2
        name = action_args[2]
        try:
            if action == "enable":
                path = set_account_enabled(app, name, enabled=True)
                sys.stdout.write(f"{app}: account enabled: {name}\nconfig: {path}\n")
            elif action == "disable":
                path = set_account_enabled(app, name, enabled=False)
                reconciled = ensure_default_account_valid(app)
                sys.stdout.write(f"{app}: account disabled: {name}\n")
                sys.stdout.write(f"{app}: default account set to {reconciled}\n" if reconciled else f"{app}: default account cleared\n")
                sys.stdout.write(f"config: {path}\n")
            else:
                path = remove_account(app, name)
                reconciled = ensure_default_account_valid(app)
                sys.stdout.write(f"{app}: account removed: {name}\n")
                sys.stdout.write(f"{app}: default account set to {reconciled}\n" if reconciled else f"{app}: default account cleared\n")
                sys.stdout.write(f"config: {path}\n")
        except Exception as exc:
            sys.stderr.write(f"{app}: {exc}\n")
            return 1
        return 0
    if action == "rename":
        if len(action_args) != 4:
            sys.stderr.write(_managed_auth_usage(app, auth.command))
            return 2
        old_name, new_name = action_args[2], action_args[3]
        try:
            path = rename_account(app, old_name, new_name)
            if get_default_account(app) == old_name:
                set_default_account(app, new_name)
            sys.stdout.write(f"{app}: account renamed: {old_name} -> {new_name}\nconfig: {path}\n")
        except Exception as exc:
            sys.stderr.write(f"{app}: {exc}\n")
            return 1
        return 0
    if action == "add":
        if len(action_args) < 3:
            sys.stderr.write(_managed_auth_usage(app, auth.command))
            return 2
        name = action_args[2]
        env_files: list[str] = []
        env_values: list[str] = []
        env_refs: list[str] = []
        env_commands: list[str] = []
        enabled = True
        index = 3
        while index < len(action_args):
            item = action_args[index]
            if item == "--env-file" and index + 1 < len(action_args):
                env_files.append(action_args[index + 1])
                index += 2
                continue
            if item == "--env" and index + 1 < len(action_args):
                env_values.append(action_args[index + 1])
                index += 2
                continue
            if item == "--env-ref" and index + 1 < len(action_args):
                env_refs.append(action_args[index + 1])
                index += 2
                continue
            if item == "--env-command" and index + 1 < len(action_args):
                env_commands.append(action_args[index + 1])
                index += 2
                continue
            if item == "--disabled":
                enabled = False
                index += 1
                continue
            sys.stderr.write(_managed_auth_usage(app, auth.command))
            return 2
        try:
            path = append_account(
                app,
                name,
                env_files=env_files,
                env=parse_assignments(env_values),
                env_refs=parse_assignments(env_refs),
                env_command=parse_command_assignments(env_commands),
                enabled=enabled,
            )
        except Exception as exc:
            sys.stderr.write(f"{app}: {exc}\n")
            return 1
        sys.stdout.write(f"{app}: account added: {name}\nconfig: {path}\n")
        return 0
    return None


def run_app(app: str, args: list[str]) -> int:
    if os.environ.get(BYPASS_ENV) == "1":
        command = original_command_for(app)
        if command is None:
            raise FileNotFoundError(f"Could not resolve original command for '{app}'")
        _exec_original(app, command, args, os.environ.copy())
    config = load_config()
    provider = _provider_for(app, config)
    managed_auth = _handle_managed_auth(app, provider, args)
    if managed_auth is not None:
        return managed_auth
    low_fallback = _check_low_fallbacks(provider)
    if low_fallback and provider.fallback_monitor and provider.fallback_monitor.action == "fail":
        sys.stderr.write(low_fallback.message + "\n")
        return 75
    if _should_passthrough_command(provider, args):
        command = _original_command(app, provider)
        env = dict(os.environ)
        env[BYPASS_ENV] = "1"
        _exec_original(app, command, args, env)
    if provider.interactive_mode == "line-repl" and _enabled_accounts(provider) and not args and sys.stdin.isatty() and sys.stdout.isatty():
        return _run_line_repl(app, provider)
    stdin_data: bytes | None | object = _STDIN_UNSET
    allowed_account_names: set[str] | None = None
    if args and has_capacity_control(provider) and _enabled_accounts(provider):
        stdin_data = _capture_stdin()
        decision = _admission(provider, args, stdin_data)
        if decision.action == "queue":
            item = decision.queue_item
            sys.stderr.write(f"[clifwrap] deferred {app} {' '.join(args)}: {decision.reason}\n")
            if item:
                sys.stderr.write(f"[clifwrap] queued as {item.id}\n")
            if decision.remediation_message:
                sys.stderr.write(f"[clifwrap] {decision.remediation_message}\n")
            return 73
        if decision.action == "shed":
            sys.stderr.write(f"[clifwrap] blocked {app} {' '.join(args)}: {decision.reason}\n")
            if decision.remediation_message:
                sys.stderr.write(f"[clifwrap] {decision.remediation_message}\n")
            return 69
        if decision.unknown_capacity:
            sys.stderr.write(f"[clifwrap] capacity unknown for {app}; continuing because policy allows it\n")
        elif decision.account_name:
            allowed_account_names = {decision.account_name}
    return _run_attempts(app, provider, args, stdin_data, allowed_account_names=allowed_account_names)


def _queue_item_pending_update(item: QueueItem, reason: str) -> QueueItem:
    item.replay_count += 1
    item.last_replayed_at = int(time.time())
    item.reason = reason
    return item


def replay_queue(provider_name: str | None = None, *, item_id: str | None = None, json_output: bool = False) -> int:
    config = load_config()
    items = list_queue_items(provider_name)
    if item_id is not None:
        items = [item for item in items if item.id == item_id]
    if not items:
        payload = {"results": []}
        if json_output:
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write("no queued items\n")
        return 0
    results: list[dict[str, object]] = []
    exit_code = 0
    now = int(time.time())
    for item in sorted(items, key=lambda current: (current.enqueued_at, current.id)):
        result: dict[str, object] = {"id": item.id, "provider": item.provider, "argv": item.argv}
        if item.expires_at <= now:
            result["status"] = "expired"
            results.append(result)
            exit_code = max(exit_code, 1)
            continue
        provider = _provider_for(item.provider, config)
        stdin_data = base64.b64decode(item.stdin_b64) if item.stdin_b64 else None
        allowed_account_names: set[str] | None = None
        if provider.capacity_control and _enabled_accounts(provider):
            decision = _admission(provider, item.argv, stdin_data, existing_item=item)
            if decision.action != "execute":
                result["status"] = "blocked"
                result["reason"] = decision.reason
                results.append(result)
                exit_code = max(exit_code, 1)
                continue
            if not decision.unknown_capacity and decision.account_name:
                allowed_account_names = {decision.account_name}
        updated = _queue_item_pending_update(item, "replay started")
        replace_queue_item(updated)
        rc = _run_attempts(
            item.provider,
            provider,
            item.argv,
            stdin_data,
            emit_output=not json_output,
            allowed_account_names=allowed_account_names,
        )
        result["status"] = "executed" if rc == 0 else "failed"
        result["exit_code"] = rc
        if rc == 0:
            drop_queue_items({item.id})
        else:
            updated.reason = f"replay execution failed with exit {rc}"
            replace_queue_item(updated)
            exit_code = max(exit_code, rc)
        results.append(result)
    if json_output:
        sys.stdout.write(json.dumps({"results": results}, indent=2, sort_keys=True) + "\n")
        return exit_code
    for result in results:
        line = f"{result['id']} {result['provider']}: {result['status']}"
        if "reason" in result:
            line += f" ({result['reason']})"
        if "exit_code" in result:
            line += f" exit={result['exit_code']}"
        sys.stdout.write(line + "\n")
    return exit_code


def _http_json(url: str, *, headers: dict[str, str], timeout: float = 15.0) -> dict:
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _value_at_path(payload: dict, path: str | None) -> object:
    if not path:
        return None
    current: object = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _path_number(payload: dict, path: str | None) -> int | None:
    value = _value_at_path(payload, path)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _render_url(template: str, env: dict[str, str]) -> str:
    rendered = template
    for key, value in env.items():
        rendered = rendered.replace(f"${{{key}}}", value)
    while "{" in rendered and "}" in rendered:
        start = rendered.find("{")
        end = rendered.find("}", start)
        if end == -1:
            break
        expression = rendered[start + 1 : end]
        if ":" in expression:
            key, fallback = expression.split(":", 1)
            value = env.get(key, fallback)
        else:
            value = env.get(expression, "")
        rendered = rendered[:start] + value + rendered[end + 1 :]
    return rendered


def _numeric_value(payload: dict, names: tuple[str, ...]) -> int | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _usage_status(provider: ProviderConfig, account: AccountConfig, env: dict[str, str]) -> tuple[str, int | None] | None:
    usage = provider.usage
    if not usage:
        return None
    key = env.get(usage.auth_env)
    if not key:
        return f"usage unavailable (requires {usage.auth_env})", None
    headers = {usage.auth_header: f"{usage.auth_scheme} {key}"}
    if usage.content_type:
        headers["Content-Type"] = usage.content_type
    payload = _http_json(_render_url(usage.url, env), headers=headers, timeout=usage.timeout_seconds)
    label = usage.label
    used = _path_number(payload, usage.used_path)
    limit = _path_number(payload, usage.limit_path)
    if limit is None and (usage.fallback_used_path or usage.fallback_limit_path):
        fallback_used = _path_number(payload, usage.fallback_used_path)
        fallback_limit = _path_number(payload, usage.fallback_limit_path)
        if fallback_used is not None or fallback_limit is not None:
            used = fallback_used
            limit = fallback_limit
            label = usage.fallback_label or label
    remaining = _path_number(payload, usage.remaining_path)
    if remaining is None and used is not None and limit not in (None, 0):
        remaining = limit - used
    parts: list[str] = []
    if used is not None and limit is not None:
        parts.append(f"{label} {used}/{limit} used")
    elif remaining is not None and limit is not None:
        parts.append(f"{label} remaining {remaining} / {limit}")
    elif remaining is not None:
        parts.append(f"{label} remaining {remaining}")
    if remaining is not None and not any(part.startswith(f"{label} remaining") for part in parts):
        parts.append(f"remaining {remaining}")
    if not parts:
        parts.append("usage lookup returned no recognized fields")
    return ", ".join(parts), remaining


def _generic_status(provider: ProviderConfig, account: AccountConfig, env: dict[str, str]) -> tuple[str, int | None]:
    if not provider.status_command:
        return "configured", None
    command = _interpolate(provider.status_command, env)
    proc = subprocess.run(command, env={**os.environ, **env, BYPASS_ENV: "1"}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    output = proc.stdout.strip()
    if proc.returncode != 0:
        detail = proc.stderr.strip() or output or f"status command exited {proc.returncode}"
        return f"status command failed: {detail}", None
    if not output:
        return "status command returned no output", None
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return output.splitlines()[0], None
    remaining = _numeric_value(payload, ("remaining", "remainingCredits", "credits_remaining", "requests_remaining"))
    limit = _numeric_value(payload, ("limit", "planCredits", "credits_limit", "requests_limit"))
    used = _numeric_value(payload, ("used", "usage", "credits_used", "requests_used"))
    if remaining is None and used is not None and limit is not None:
        remaining = limit - used
    parts: list[str] = []
    if used is not None and limit is not None:
        parts.append(f"{used}/{limit} used")
    if remaining is not None:
        parts.append(f"remaining {remaining}")
    if not parts:
        parts.append(output.splitlines()[0])
    return ", ".join(parts), remaining


def _status_snapshot(app: str, provider: ProviderConfig) -> dict:
    accounts: list[dict] = []
    remaining_values: list[int] = []
    status = _fallback_monitor(provider)
    alert_state = _load_alert_state(app)
    queue_error = None
    queue_items: list[QueueItem] = []
    try:
        queue_items = list_queue_items(app)
    except ValueError as exc:
        queue_error = str(exc)
    queue_total = len(queue_items)
    queue_expired = sum(1 for item in queue_items if item.expires_at <= int(time.time()))
    payload = {
        "provider": app,
        "configured_accounts": len(provider.accounts),
        "low_fallback": None,
        "recovery_error": None,
        "capacity_control": None,
        "capacity_health": None,
        "queue": {
            "pending": queue_total,
            "expired": queue_expired,
            "error": queue_error,
        },
        "accounts": accounts,
        "total_remaining": None,
    }
    if status:
        payload["low_fallback"] = {
            "enabled_accounts": len(status.enabled_accounts),
            "unused_fallbacks": status.fallback_count,
            "threshold": status.threshold,
            "active": status.default_account,
            "enabled_names": [account.name for account in status.enabled_accounts],
            "message": status.message,
        }
    if alert_state.get("recovery_error"):
        payload["recovery_error"] = {
            "failed_at": alert_state.get("recovery_failed_at", "unknown"),
            "error": alert_state["recovery_error"],
        }
    if provider.capacity_control:
        payload["capacity_control"] = {
            "default_action": provider.capacity_control.default_action,
            "unknown_capacity_action": provider.capacity_control.unknown_capacity_action,
            "reserve_threshold": provider.capacity_control.reserve_threshold,
            "default_cost": provider.capacity_control.default_cost,
            "remediation_message": provider.capacity_control.remediation_message,
            "remediation_commands": provider.capacity_control.remediation_commands,
        }
    for account in provider.accounts:
        row = {
            "name": account.name,
            "enabled": account.enabled,
            "detail": None,
            "remaining": None,
            "error": None,
        }
        accounts.append(row)
        if not account.enabled:
            row["detail"] = "disabled"
            continue
        try:
            env = _account_env(account)
        except Exception as exc:
            row["detail"] = "configuration error"
            row["error"] = str(exc)
            continue
        try:
            usage_status = _usage_status(provider, account, env)
        except Exception as exc:
            row["detail"] = "usage lookup failed"
            row["error"] = str(exc)
            continue
        if usage_status:
            detail, remaining = usage_status
        else:
            detail, remaining = _generic_status(provider, account, env)
        row["detail"] = detail
        row["remaining"] = remaining
        if remaining is not None:
            remaining_values.append(remaining)
    if remaining_values:
        payload["total_remaining"] = sum(remaining_values)
    if provider.capacity_control:
        required = provider.capacity_control.reserve_threshold + provider.capacity_control.default_cost
        ready_accounts = [account["name"] for account in accounts if account["enabled"] and isinstance(account["remaining"], int) and account["remaining"] >= required]
        unknown_accounts = [account["name"] for account in accounts if account["enabled"] and account["remaining"] is None]
        unhealthy = queue_error is not None or bool(queue_total) or not ready_accounts
        if unknown_accounts and not ready_accounts:
            message = (
                f"[clifwrap] {app} capacity is unknown or below the configured reserve "
                f"(required remaining {required}, queue backlog {queue_total})"
            )
        elif ready_accounts:
            message = f"[clifwrap] {app} capacity healthy: {', '.join(ready_accounts)} meet required remaining {required}"
        else:
            message = f"[clifwrap] {app} no account currently meets required remaining {required}; queue backlog {queue_total}"
        payload["capacity_health"] = {
            "required_remaining": required,
            "ready_accounts": ready_accounts,
            "unknown_accounts": unknown_accounts,
            "queue_backlog": queue_total,
            "message": message,
            "unhealthy": unhealthy,
        }
    return payload


def _render_status_snapshot(payload: dict) -> None:
    app = payload["provider"]
    if payload["configured_accounts"]:
        sys.stdout.write(f"{app}: {payload['configured_accounts']} configured account(s)\n")
    low_fallback = payload.get("low_fallback")
    if low_fallback:
        sys.stdout.write(low_fallback["message"] + "\n")
    recovery_error = payload.get("recovery_error")
    if recovery_error:
        sys.stdout.write(f"[clifwrap] {app} low-fallback recovery hook last failed at {recovery_error['failed_at']}: {recovery_error['error']}\n")
    capacity_health = payload.get("capacity_health")
    if capacity_health:
        sys.stdout.write(capacity_health["message"] + "\n")
    queue_state = payload.get("queue")
    if queue_state and (queue_state.get("pending") or queue_state.get("error")):
        if queue_state.get("error"):
            sys.stdout.write(f"[clifwrap] {app} queue state error: {queue_state['error']}\n")
        else:
            sys.stdout.write(
                f"[clifwrap] {app} queue backlog: pending={queue_state['pending']}, expired={queue_state['expired']}\n"
            )
    for account in payload["accounts"]:
        if not account["enabled"]:
            sys.stdout.write(f"- {account['name']}: disabled\n")
            continue
        if account["error"]:
            sys.stdout.write(f"- {account['name']}: {account['detail']}: {account['error']}\n")
            continue
        sys.stdout.write(f"- {account['name']}: {account['detail']}\n")
    if payload["total_remaining"] is not None:
        sys.stdout.write(f"total remaining across available accounts: {payload['total_remaining']}\n")


def _snapshot_unhealthy(payload: dict) -> bool:
    queue_state = payload.get("queue") or {}
    capacity_health = payload.get("capacity_health") or {}
    return bool(
        payload.get("low_fallback")
        or payload.get("recovery_error")
        or queue_state.get("error")
        or queue_state.get("pending")
        or capacity_health.get("unhealthy")
    )


def status_for(app: str, *, json_output: bool = False, check: bool = False) -> int:
    config = load_config()
    provider = _provider_for(app, config)
    if not provider.accounts and not provider.fallback_monitor and not provider.capacity_control:
        if json_output:
            sys.stdout.write(json.dumps({"provider": app, "configured_accounts": 0, "low_fallback": None, "recovery_error": None, "accounts": [], "total_remaining": None}, indent=2) + "\n")
            return 0
        sys.stdout.write(f"{app}: no configured accounts\n")
        return 0
    snapshot = _status_snapshot(app, provider)
    if json_output:
        sys.stdout.write(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
        return 1 if check and _snapshot_unhealthy(snapshot) else 0
    _render_status_snapshot(snapshot)
    return 1 if check and _snapshot_unhealthy(snapshot) else 0


def status_all(*, json_output: bool = False, check: bool = False) -> int:
    config = load_config()
    apps = sorted(config.providers)
    if not apps:
        if json_output:
            sys.stdout.write(json.dumps({"providers": []}, indent=2) + "\n")
            return 0
        sys.stdout.write("no configured providers\n")
        return 0
    if json_output:
        snapshots = [_status_snapshot(app, _provider_for(app, config)) for app in apps]
        payload = {"providers": snapshots}
        sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        return 1 if check and any(_snapshot_unhealthy(snapshot) for snapshot in snapshots) else 0
    exit_code = 0
    for index, app in enumerate(apps):
        if index:
            sys.stdout.write("\n")
        exit_code = max(exit_code, status_for(app, check=check))
    return exit_code
