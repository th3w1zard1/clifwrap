from __future__ import annotations

import json
import os
import re
import shlex
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .config import AccountConfig, config_path, load_config
from .state import clear_default_account, get_default_account, set_default_account


@dataclass(frozen=True)
class AccountSpecEntry:
    name: str
    env_names: tuple[str, ...]
    enabled: bool = True


@dataclass(frozen=True)
class AccountSpec:
    provider: str
    target_env: str
    env_file: str | None = None
    env_name_template: str | None = None
    set_default: bool = False
    validation: "AccountValidationSpec | None" = None
    accounts: tuple[AccountSpecEntry, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AccountValidationSpec:
    url: str
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    content_type: str | None = None
    remaining_path: str | None = None


@dataclass(frozen=True)
class ImportResult:
    account: str
    status: str
    detail: str


def _quote(value: str) -> str:
    return json.dumps(value)


def _toml_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    return _quote(value)


def _provider_header(app: str) -> str:
    return f"[providers.{_toml_key(app)}]"


def _account_header(app: str) -> str:
    return f"[[providers.{_toml_key(app)}.accounts]]"


def _inline_table(values: dict[str, str]) -> str:
    items = [f"{key} = {_quote(value)}" for key, value in sorted(values.items())]
    return "{ " + ", ".join(items) + " }"


def _command_array(values: list[str]) -> str:
    return "[" + ", ".join(_quote(value) for value in values) + "]"


def _command_dict(values: dict[str, list[str]]) -> str:
    items = [f"{key} = {_command_array(value)}" for key, value in sorted(values.items())]
    return "{ " + ", ".join(items) + " }"


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_")
    return slug or "account"


def _render_env_name_template(template: str, *, provider: str, account: str, target_env: str) -> str:
    return template.format(
        provider=provider,
        provider_slug=_slug(provider).upper(),
        account=account,
        account_slug=_slug(account).upper(),
        target_env=target_env,
        target_env_slug=_slug(target_env).upper(),
    )


def _candidate_env_names(spec: AccountSpec, account: AccountSpecEntry) -> tuple[str, ...]:
    if account.env_names:
        return account.env_names
    if spec.env_name_template:
        return (_render_env_name_template(spec.env_name_template, provider=spec.provider, account=account.name, target_env=spec.target_env),)
    return ()


def ensure_config_exists(*, force: bool = False) -> Path:
    path = config_path()
    if path.exists() and not force:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("version = 1\n\n")
    return path


def _load_env_names(path: str | None) -> set[str]:
    return set(_load_env_values(path))


def _load_env_values(path: str | None) -> dict[str, str]:
    if not path:
        return dict(os.environ)
    resolved = Path(os.path.expandvars(os.path.expanduser(path)))
    values: dict[str, str] = {}
    if not resolved.exists():
        return values
    for raw in resolved.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _value = line.split("=", 1)
        key = key.strip()
        if key:
            try:
                parts = shlex.split(_value, comments=False, posix=True)
            except ValueError:
                continue
            values[key] = parts[0] if parts else ""
    return values


def _validation_from_raw(raw_validation: object) -> AccountValidationSpec | None:
    if raw_validation is None:
        return None
    if not isinstance(raw_validation, dict):
        raise ValueError("spec validation must be a table")
    url = raw_validation.get("url")
    if not isinstance(url, str) or not url:
        raise ValueError("spec validation.url must be a non-empty string")
    return AccountValidationSpec(
        url=url,
        auth_header=str(raw_validation.get("auth_header", "Authorization")),
        auth_scheme=str(raw_validation.get("auth_scheme", "Bearer")),
        content_type=raw_validation.get("content_type"),
        remaining_path=raw_validation.get("remaining_path"),
    )


def load_account_spec(path: Path) -> AccountSpec:
    raw = tomllib.loads(path.read_text())
    provider = raw.get("provider")
    target_env = raw.get("target_env")
    if not isinstance(provider, str) or not provider:
        raise ValueError("spec provider must be a non-empty string")
    if not isinstance(target_env, str) or not target_env:
        raise ValueError("spec target_env must be a non-empty string")
    env_file = raw.get("env_file")
    if env_file is not None and not isinstance(env_file, str):
        raise ValueError("spec env_file must be a string when present")
    env_name_template = raw.get("env_name_template")
    if env_name_template is not None and not isinstance(env_name_template, str):
        raise ValueError("spec env_name_template must be a string when present")
    raw_accounts = raw.get("accounts", [])
    if not isinstance(raw_accounts, list):
        raise ValueError("spec must contain [[accounts]] entries")
    accounts: list[AccountSpecEntry] = []
    for index, raw_account in enumerate(raw_accounts):
        if not isinstance(raw_account, dict):
            raise ValueError(f"accounts[{index}] must be a table")
        name = raw_account.get("name", raw_account.get("label"))
        env_names = raw_account.get("env_names", [])
        if not isinstance(name, str) or not name:
            raise ValueError(f"accounts[{index}] must define a non-empty name or label")
        if not isinstance(env_names, list) or not all(isinstance(item, str) and item for item in env_names):
            raise ValueError(f"accounts[{index}].env_names must be a string array when present")
        accounts.append(AccountSpecEntry(name=name, env_names=tuple(env_names), enabled=bool(raw_account.get("enabled", True))))
    return AccountSpec(
        provider=provider,
        target_env=target_env,
        env_file=env_file,
        env_name_template=env_name_template,
        set_default=bool(raw.get("set_default", False)),
        validation=_validation_from_raw(raw.get("validation")),
        accounts=tuple(accounts),
    )


def _value_at_path(payload: dict, path: str | None) -> object:
    if not path:
        return None
    current: object = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _validate_account_secret(validation: AccountValidationSpec | None, value: str) -> str | None:
    if not validation:
        return None
    headers = {validation.auth_header: f"{validation.auth_scheme} {value}"}
    if validation.content_type:
        headers["Content-Type"] = validation.content_type
    request = urllib.request.Request(validation.url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return f"validation failed ({type(exc).__name__})"
    if validation.remaining_path:
        remaining = _value_at_path(payload, validation.remaining_path)
        return f"validated, remaining {remaining}"
    return "validated"


def account_names(app: str) -> list[str]:
    config = load_config()
    provider = config.providers.get(app)
    if not provider:
        return []
    return [account.name for account in provider.accounts]


def list_accounts(app: str | None = None) -> list[tuple[str, AccountConfig]]:
    config = load_config()
    rows: list[tuple[str, AccountConfig]] = []
    for provider_name, provider in sorted(config.providers.items()):
        if app and provider_name != app:
            continue
        for account in provider.accounts:
            rows.append((provider_name, account))
    return rows


def ensure_default_account_valid(app: str) -> str | None:
    enabled_names = [account.name for provider_name, account in list_accounts(app) if provider_name == app and account.enabled]
    current = get_default_account(app)
    if current in enabled_names:
        return current
    if enabled_names:
        set_default_account(app, enabled_names[0])
        return enabled_names[0]
    clear_default_account(app)
    return None


def import_account_spec(path: Path, *, env_file_override: str | None = None, dry_run: bool = False) -> list[ImportResult]:
    spec = load_account_spec(path)
    env_file = env_file_override or spec.env_file
    env_values = _load_env_values(env_file)
    available_env_names = set(env_values)
    results: list[ImportResult] = []
    default_candidate: str | None = None
    for account in spec.accounts:
        candidate_env_names = _candidate_env_names(spec, account)
        selected = next((name for name in candidate_env_names if name in available_env_names), None)
        if not selected:
            detail = "no secret source found for this account; capture/login it or add the account with clifwrap account add"
            if account.env_names:
                detail = f"expected one of {', '.join(account.env_names)}"
            results.append(
                ImportResult(
                    account=account.name,
                    status="missing",
                    detail=detail,
                )
            )
            continue
        validation_detail = _validate_account_secret(spec.validation, env_values[selected])
        if validation_detail and validation_detail.startswith("validation failed"):
            results.append(ImportResult(account=account.name, status="invalid", detail=f"{selected}: {validation_detail}"))
            continue
        existing = account.name in account_names(spec.provider)
        if dry_run:
            status = "would-update" if _account_import_needs_update(spec.provider, account.name, spec.target_env, selected, env_file, account.enabled) else "exists"
            if not existing:
                status = "would-add"
            detail = f"{spec.target_env}=env:{selected}"
            if validation_detail:
                detail += f" ({validation_detail})"
            results.append(ImportResult(account=account.name, status=status, detail=detail))
        elif existing:
            update_account_secret_source(
                spec.provider,
                account.name,
                target_env=spec.target_env,
                env_ref=selected,
                env_file=env_file,
                enabled=account.enabled,
            )
            detail = f"{spec.target_env}=env:{selected}"
            if validation_detail:
                detail += f" ({validation_detail})"
            results.append(ImportResult(account=account.name, status="updated", detail=detail))
        else:
            append_account(
                spec.provider,
                account.name,
                env_files=[env_file] if env_file else None,
                env_refs={spec.target_env: selected},
                enabled=account.enabled,
            )
            detail = f"{spec.target_env}=env:{selected}"
            if validation_detail:
                detail += f" ({validation_detail})"
            results.append(ImportResult(account=account.name, status="added", detail=detail))
        if account.enabled and default_candidate is None:
            default_candidate = account.name
    if spec.set_default and default_candidate and not dry_run:
        if get_default_account(spec.provider) != default_candidate:
            set_default_account(spec.provider, default_candidate)
        results.append(ImportResult(account=default_candidate, status="default", detail=f"{spec.provider} default account"))
    return results


def _account_import_needs_update(app: str, name: str, target_env: str, env_ref: str, env_file: str | None, enabled: bool) -> bool:
    provider = load_config().providers.get(app)
    if not provider:
        return False
    existing = next((account for account in provider.accounts if account.name == name), None)
    if not existing:
        return False
    if existing.enabled != enabled:
        return True
    if existing.env.get(target_env) != f"env:{env_ref}":
        return True
    return bool(env_file and env_file not in existing.env_files)


def append_account(
    app: str,
    name: str,
    *,
    env_files: list[str] | None = None,
    env: dict[str, str] | None = None,
    env_refs: dict[str, str] | None = None,
    env_command: dict[str, list[str]] | None = None,
    prepare_command: list[str] | None = None,
    prepare_on: str = "always",
    enabled: bool = True,
) -> Path:
    if prepare_on not in {"always", "once", "never"}:
        raise ValueError("prepare_on must be one of: always, once, never")
    path = ensure_config_exists()
    existing = account_names(app)
    if name in existing:
        raise ValueError(f"Account {name!r} already exists for provider {app!r}")
    merged_env: dict[str, str] = {}
    merged_env.update(env or {})
    for key, ref in (env_refs or {}).items():
        merged_env[key] = f"env:{ref}"

    lines: list[str] = []
    text = path.read_text()
    if text and not text.endswith("\n"):
        lines.append("")
    provider_header = _provider_header(app)
    if provider_header not in text:
        lines.extend([provider_header, ""])
    lines.append(_account_header(app))
    lines.append(f"name = {_quote(name)}")
    if not enabled:
        lines.append("enabled = false")
    if env_files:
        lines.append(f"env_files = {_command_array(env_files)}")
    if merged_env:
        lines.append(f"env = {_inline_table(merged_env)}")
    if env_command:
        lines.append(f"env_command = {_command_dict(env_command)}")
    if prepare_command:
        lines.append(f"prepare_on = {_quote(prepare_on)}")
        lines.append(f"prepare_command = {_command_array(prepare_command)}")
    lines.append("")
    with path.open("a") as handle:
        handle.write("\n".join(lines))
        if lines:
            handle.write("\n")
    return path


def _account_block_bounds(text: str, app: str, name: str) -> tuple[int, int, list[str]]:
    lines = text.splitlines(keepends=True)
    header = _account_header(app)
    starts = [index for index, line in enumerate(lines) if line.strip() == header]
    for position, start in enumerate(starts):
        end = starts[position + 1] if position + 1 < len(starts) else len(lines)
        block_body = "".join(lines[start + 1 : end])
        try:
            parsed = tomllib.loads("[[accounts]]\n" + block_body)
        except tomllib.TOMLDecodeError:
            continue
        accounts = parsed.get("accounts", [])
        if accounts and accounts[0].get("name") == name:
            return start, end, lines
    raise ValueError(f"Account {name!r} does not exist for provider {app!r}")


def _render_account_block(app: str, account: dict[str, object]) -> list[str]:
    lines = [_account_header(app)]
    name = account.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"Account for provider {app!r} does not have a valid name")
    lines.append(f"name = {_quote(name)}")
    if account.get("enabled") is False:
        lines.append("enabled = false")
    env_files = account.get("env_files")
    if isinstance(env_files, list) and all(isinstance(item, str) for item in env_files) and env_files:
        lines.append(f"env_files = {_command_array(env_files)}")
    env = account.get("env")
    if isinstance(env, dict):
        rendered_env = {str(key): str(value) for key, value in env.items()}
        if rendered_env:
            lines.append(f"env = {_inline_table(rendered_env)}")
    env_command = account.get("env_command")
    if isinstance(env_command, dict):
        rendered_commands = {
            str(key): [str(part) for part in value]
            for key, value in env_command.items()
            if isinstance(value, list) and all(isinstance(part, str) for part in value)
        }
        if rendered_commands:
            lines.append(f"env_command = {_command_dict(rendered_commands)}")
    prepare_on = account.get("prepare_on")
    prepare_command = account.get("prepare_command")
    if isinstance(prepare_on, str) and prepare_on != "always":
        lines.append(f"prepare_on = {_quote(prepare_on)}")
    if isinstance(prepare_command, list) and all(isinstance(part, str) for part in prepare_command):
        lines.append(f"prepare_command = {_command_array(prepare_command)}")
    lines.append("")
    return lines


def update_account_secret_source(
    app: str,
    name: str,
    *,
    target_env: str,
    env_ref: str,
    env_file: str | None,
    enabled: bool,
) -> Path:
    path = config_path()
    if not path.exists():
        raise ValueError("Config does not exist")
    text = path.read_text()
    start, end, lines = _account_block_bounds(text, app, name)
    block_body = "".join(lines[start + 1 : end])
    parsed = tomllib.loads("[[accounts]]\n" + block_body)
    account = dict(parsed["accounts"][0])
    account["enabled"] = enabled
    if env_file:
        current_env_files = account.get("env_files")
        env_files = list(current_env_files) if isinstance(current_env_files, list) else []
        if env_file not in env_files:
            env_files.append(env_file)
        account["env_files"] = env_files
    env = account.get("env")
    if not isinstance(env, dict):
        env = {}
    env[target_env] = f"env:{env_ref}"
    account["env"] = env
    replacement = [line + "\n" for line in _render_account_block(app, account)]
    lines[start:end] = replacement
    path.write_text("".join(lines))
    return path


def remove_account(app: str, name: str) -> Path:
    path = config_path()
    if not path.exists():
        raise ValueError("Config does not exist")
    text = path.read_text()
    start, end, lines = _account_block_bounds(text, app, name)
    del lines[start:end]
    path.write_text("".join(lines))
    return path


def rename_account(app: str, old_name: str, new_name: str) -> Path:
    if not new_name:
        raise ValueError("New account name must be non-empty")
    if new_name in account_names(app):
        raise ValueError(f"Account {new_name!r} already exists for provider {app!r}")
    path = config_path()
    if not path.exists():
        raise ValueError("Config does not exist")
    text = path.read_text()
    start, end, lines = _account_block_bounds(text, app, old_name)
    for index in range(start + 1, end):
        if lines[index].strip().startswith("name"):
            prefix = lines[index].split("=", 1)[0]
            lines[index] = f"{prefix}= {_quote(new_name)}\n"
            path.write_text("".join(lines))
            return path
    raise ValueError(f"Account {old_name!r} does not have a name field")


def set_account_enabled(app: str, name: str, *, enabled: bool) -> Path:
    path = config_path()
    if not path.exists():
        raise ValueError("Config does not exist")
    text = path.read_text()
    start, end, lines = _account_block_bounds(text, app, name)
    enabled_line = f"enabled = {str(enabled).lower()}\n"
    for index in range(start + 1, end):
        if lines[index].strip().startswith("enabled"):
            lines[index] = enabled_line
            break
    else:
        insert_at = start + 1
        for index in range(start + 1, end):
            if lines[index].strip().startswith("name"):
                insert_at = index + 1
                break
        lines.insert(insert_at, enabled_line)
    path.write_text("".join(lines))
    return path


def parse_assignments(values: list[str], *, prefix: str = "") -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"{prefix}{value!r} must use KEY=VALUE syntax")
        key, item = value.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"{prefix}{value!r} has an empty key")
        parsed[key] = item
    return parsed


def parse_command_assignments(values: list[str]) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for key, command in parse_assignments(values, prefix="--env-command ").items():
        try:
            parts = shlex.split(command, comments=False, posix=True)
        except ValueError as exc:
            raise ValueError(f"--env-command {key}: {exc}") from exc
        if not parts:
            raise ValueError(f"--env-command {key} must contain a command")
        parsed[key] = parts
    return parsed
