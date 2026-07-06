from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any


APP_NAME = "clifwrap"
CONFIG_ENV = "CLIFWRAP_CONFIG"
STATE_ENV = "CLIFWRAP_STATE_DIR"
BIN_ENV = "CLIFWRAP_BIN_DIR"
BYPASS_ENV = "CLIFWRAP_BYPASS"
SHIM_ENV = "CLIFWRAP_SHIM_NAME"
CURRENT_ACCOUNT_ENV = "CLIFWRAP_ACCOUNT"
HOME_DIR = Path.home()
DEFAULT_CONFIG_PATH = HOME_DIR / ".config" / APP_NAME / "config.toml"
DEFAULT_STATE_DIR = HOME_DIR / ".local" / "state" / APP_NAME
DEFAULT_BIN_DIR = HOME_DIR / ".local" / "bin"


def expand_path(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def config_path() -> Path:
    return expand_path(os.environ.get(CONFIG_ENV)) or DEFAULT_CONFIG_PATH


def state_dir() -> Path:
    return expand_path(os.environ.get(STATE_ENV)) or DEFAULT_STATE_DIR


def shim_bin_dir() -> Path:
    return expand_path(os.environ.get(BIN_ENV)) or DEFAULT_BIN_DIR


@dataclass
class AccountConfig:
    name: str
    enabled: bool = True
    env_files: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    env_command: dict[str, list[str]] = field(default_factory=dict)
    prepare_command: list[str] | None = None
    prepare_on: str = "always"


@dataclass
class ProviderConfig:
    name: str
    command: list[str] | None = None
    retry_exit_codes: list[int] = field(default_factory=list)
    retry_patterns: list[str] = field(default_factory=list)
    never_retry_patterns: list[str] = field(default_factory=list)
    retry_on_any_error: bool = False
    interactive_mode: str | None = None
    status_command: list[str] | None = None
    passthrough_commands: list[str] = field(default_factory=list)
    auth_management: "AuthManagementConfig | None" = None
    fallback_monitor: "FallbackMonitorConfig | None" = None
    usage: "UsageConfig | None" = None
    capacity_control: "CapacityControlConfig | None" = None
    accounts: list[AccountConfig] = field(default_factory=list)


@dataclass
class WrapperConfig:
    providers: dict[str, ProviderConfig] = field(default_factory=dict)


@dataclass
class AuthManagementConfig:
    command: str
    aliases: list[str] = field(default_factory=list)


@dataclass
class FallbackMonitorConfig:
    threshold: int = 3
    action: str = "warn"
    journald: bool = False
    syslog: bool = True
    stderr: bool = True
    recovery_command: list[str] | None = None


@dataclass
class UsageConfig:
    url: str
    auth_env: str
    timeout_seconds: float = 15.0
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    content_type: str | None = None
    used_path: str | None = None
    limit_path: str | None = None
    remaining_path: str | None = None
    fallback_used_path: str | None = None
    fallback_limit_path: str | None = None
    label: str = "usage"
    fallback_label: str | None = None


@dataclass
class CapacityControlConfig:
    default_action: str = "queue"
    unknown_capacity_action: str = "allow"
    reserve_threshold: int = 0
    default_cost: int = 1
    command_costs: dict[str, int] = field(default_factory=dict)
    queue_retention_seconds: int = 86400
    queue_max_items: int = 100
    snapshot_ttl_seconds: int = 60
    remediation_message: str | None = None
    remediation_commands: list[str] = field(default_factory=list)


def _string_dict(value: Any, *, field_name: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a table")
    out: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(item, str):
            raise ValueError(f"{field_name}.{key} must be a string")
        out[str(key)] = item
    return out


def _command_dict(value: Any, *, field_name: str) -> dict[str, list[str]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a table")
    out: dict[str, list[str]] = {}
    for key, item in value.items():
        out[str(key)] = _command_list(item, field_name=f"{field_name}.{key}")
    return out


def _command_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(part, str) for part in value):
        raise ValueError(f"{field_name} must be a string or string array")
    return list(value)


def _int_dict(value: Any, *, field_name: str) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must be a table")
    out: dict[str, int] = {}
    for key, item in value.items():
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError(f"{field_name}.{key} must be a non-negative integer")
        out[str(key)] = int(item)
    return out


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _env_int(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    try:
        return int(value.strip())
    except ValueError:
        raise ValueError(f"{name} must be an integer")


def _env_float(name: str) -> float | None:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    try:
        parsed = float(value.strip())
    except ValueError:
        raise ValueError(f"{name} must be a number")
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _env_nonnegative_int(name: str) -> int | None:
    value = _env_int(name)
    if value is None:
        return None
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to zero")
    return value


def _env_command_list(name: str) -> list[str]:
    value = os.environ.get(name)
    if not value:
        return []
    return [item for item in (part.strip() for part in value.split(",")) if item]


def _env_int_map(name: str) -> dict[str, int]:
    value = os.environ.get(name)
    if value is None or not value.strip():
        return {}
    raw = value.strip()
    try:
        parsed = tomllib.loads(f"value = {raw}\n")["value"]
    except tomllib.TOMLDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return _int_dict(parsed, field_name=name)
    out: dict[str, int] = {}
    for part in raw.split(","):
        item = part.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"{name} items must be key=value pairs or a TOML inline table")
        key, number = item.split("=", 1)
        key = key.strip()
        try:
            parsed_number = int(number.strip())
        except ValueError as exc:
            raise ValueError(f"{name}.{key} must be an integer") from exc
        if parsed_number < 0:
            raise ValueError(f"{name}.{key} must be a non-negative integer")
        out[key] = parsed_number
    return out


def _load_accounts(raw_accounts: Any) -> list[AccountConfig]:
    if raw_accounts is None:
        return []
    if not isinstance(raw_accounts, list):
        raise ValueError("accounts must be an array of tables")
    accounts: list[AccountConfig] = []
    for index, raw_account in enumerate(raw_accounts):
        if not isinstance(raw_account, dict):
            raise ValueError(f"accounts[{index}] must be a table")
        name = raw_account.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(f"accounts[{index}].name must be a non-empty string")
        accounts.append(
            AccountConfig(
                name=name,
                enabled=bool(raw_account.get("enabled", True)),
                env_files=[str(item) for item in raw_account.get("env_files", [])],
                env=_string_dict(raw_account.get("env"), field_name=f"accounts[{index}].env"),
                env_command=_command_dict(raw_account.get("env_command"), field_name=f"accounts[{index}].env_command"),
                prepare_command=_command_list(raw_account.get("prepare_command"), field_name=f"accounts[{index}].prepare_command")
                or None,
                prepare_on=str(raw_account.get("prepare_on", "always")),
            )
        )
    return accounts


def _auth_management_from_raw(raw_auth: Any, *, field_name: str) -> AuthManagementConfig | None:
    if raw_auth is None:
        return None
    if not isinstance(raw_auth, dict):
        raise ValueError(f"{field_name} must be a table")
    command = raw_auth.get("command")
    if not isinstance(command, str) or not command:
        raise ValueError(f"{field_name}.command must be a non-empty string")
    aliases = _command_list(raw_auth.get("aliases"), field_name=f"{field_name}.aliases")
    return AuthManagementConfig(command=command, aliases=aliases)


def _fallback_monitor_from_raw(raw_monitor: Any, *, field_name: str) -> FallbackMonitorConfig | None:
    if raw_monitor is None:
        return None
    if not isinstance(raw_monitor, dict):
        raise ValueError(f"{field_name} must be a table")
    threshold_value = raw_monitor.get("threshold", 3)
    if not isinstance(threshold_value, int):
        raise ValueError(f"{field_name}.threshold must be an integer")
    action = str(raw_monitor.get("action", "warn"))
    if action not in {"warn", "fail"}:
        raise ValueError(f"{field_name}.action must be 'warn' or 'fail'")
    recovery_command = _command_list(raw_monitor.get("recovery_command"), field_name=f"{field_name}.recovery_command")
    return FallbackMonitorConfig(
        threshold=threshold_value,
        action=action,
        journald=bool(raw_monitor.get("journald", False)),
        syslog=bool(raw_monitor.get("syslog", True)),
        stderr=bool(raw_monitor.get("stderr", True)),
        recovery_command=recovery_command or None,
    )


def _usage_from_raw(raw_usage: Any, *, field_name: str) -> UsageConfig | None:
    if raw_usage is None:
        return None
    if not isinstance(raw_usage, dict):
        raise ValueError(f"{field_name} must be a table")
    url = raw_usage.get("url")
    auth_env = raw_usage.get("auth_env")
    if not isinstance(url, str) or not url:
        raise ValueError(f"{field_name}.url must be a non-empty string")
    if not isinstance(auth_env, str) or not auth_env:
        raise ValueError(f"{field_name}.auth_env must be a non-empty string")
    timeout_seconds = raw_usage.get("timeout_seconds", 15.0)
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError(f"{field_name}.timeout_seconds must be a positive number")
    return UsageConfig(
        url=url,
        auth_env=auth_env,
        timeout_seconds=float(timeout_seconds),
        auth_header=str(raw_usage.get("auth_header", "Authorization")),
        auth_scheme=str(raw_usage.get("auth_scheme", "Bearer")),
        content_type=raw_usage.get("content_type"),
        used_path=raw_usage.get("used_path"),
        limit_path=raw_usage.get("limit_path"),
        remaining_path=raw_usage.get("remaining_path"),
        fallback_used_path=raw_usage.get("fallback_used_path"),
        fallback_limit_path=raw_usage.get("fallback_limit_path"),
        label=str(raw_usage.get("label", "usage")),
        fallback_label=raw_usage.get("fallback_label"),
    )


def _capacity_control_from_raw(raw_capacity: Any, *, field_name: str) -> CapacityControlConfig | None:
    if raw_capacity is None:
        return None
    if not isinstance(raw_capacity, dict):
        raise ValueError(f"{field_name} must be a table")
    default_action = str(raw_capacity.get("default_action", "queue"))
    if default_action not in {"execute", "queue", "shed"}:
        raise ValueError(f"{field_name}.default_action must be 'execute', 'queue', or 'shed'")
    unknown_capacity_action = str(raw_capacity.get("unknown_capacity_action", "allow"))
    if unknown_capacity_action not in {"allow", "queue", "shed"}:
        raise ValueError(f"{field_name}.unknown_capacity_action must be 'allow', 'queue', or 'shed'")
    reserve_threshold = raw_capacity.get("reserve_threshold", 0)
    default_cost = raw_capacity.get("default_cost", 1)
    queue_retention_seconds = raw_capacity.get("queue_retention_seconds", 86400)
    queue_max_items = raw_capacity.get("queue_max_items", 100)
    snapshot_ttl_seconds = raw_capacity.get("snapshot_ttl_seconds", 60)
    for label, value in {
        "reserve_threshold": reserve_threshold,
        "default_cost": default_cost,
        "queue_retention_seconds": queue_retention_seconds,
        "queue_max_items": queue_max_items,
        "snapshot_ttl_seconds": snapshot_ttl_seconds,
    }.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field_name}.{label} must be a non-negative integer")
    if default_cost < 1:
        raise ValueError(f"{field_name}.default_cost must be at least 1")
    if queue_retention_seconds < 1:
        raise ValueError(f"{field_name}.queue_retention_seconds must be at least 1")
    if queue_max_items < 1:
        raise ValueError(f"{field_name}.queue_max_items must be at least 1")
    if snapshot_ttl_seconds < 1:
        raise ValueError(f"{field_name}.snapshot_ttl_seconds must be at least 1")
    remediation_message = raw_capacity.get("remediation_message")
    if remediation_message is not None and not isinstance(remediation_message, str):
        raise ValueError(f"{field_name}.remediation_message must be a string")
    return CapacityControlConfig(
        default_action=default_action,
        unknown_capacity_action=unknown_capacity_action,
        reserve_threshold=reserve_threshold,
        default_cost=default_cost,
        command_costs=_int_dict(raw_capacity.get("command_costs"), field_name=f"{field_name}.command_costs"),
        queue_retention_seconds=queue_retention_seconds,
        queue_max_items=queue_max_items,
        snapshot_ttl_seconds=snapshot_ttl_seconds,
        remediation_message=remediation_message,
        remediation_commands=_command_list(raw_capacity.get("remediation_commands"), field_name=f"{field_name}.remediation_commands"),
    )


def _provider_from_raw(name: str, raw_provider: dict[str, Any]) -> ProviderConfig:
    return ProviderConfig(
        name=name,
        command=_command_list(raw_provider.get("command"), field_name=f"providers.{name}.command") or None,
        retry_exit_codes=[int(item) for item in raw_provider.get("retry_exit_codes", [])],
        retry_patterns=[str(item).lower() for item in raw_provider.get("retry_patterns", [])],
        never_retry_patterns=[str(item).lower() for item in raw_provider.get("never_retry_patterns", [])],
        retry_on_any_error=bool(raw_provider.get("retry_on_any_error", False)),
        interactive_mode=raw_provider.get("interactive_mode"),
        status_command=_command_list(raw_provider.get("status_command"), field_name=f"providers.{name}.status_command") or None,
        passthrough_commands=_command_list(raw_provider.get("passthrough_commands"), field_name=f"providers.{name}.passthrough_commands"),
        auth_management=_auth_management_from_raw(raw_provider.get("auth_management"), field_name=f"providers.{name}.auth_management"),
        fallback_monitor=_fallback_monitor_from_raw(raw_provider.get("fallback_monitor"), field_name=f"providers.{name}.fallback_monitor"),
        usage=_usage_from_raw(raw_provider.get("usage"), field_name=f"providers.{name}.usage"),
        capacity_control=_capacity_control_from_raw(raw_provider.get("capacity_control"), field_name=f"providers.{name}.capacity_control"),
        accounts=_load_accounts(raw_provider.get("accounts")),
    )


def _load_provider_table(text: str) -> dict[str, ProviderConfig]:
    raw = tomllib.loads(text)
    provider_table = raw.get("providers", {})
    if not isinstance(provider_table, dict):
        raise ValueError("[providers] must be a table")
    providers: dict[str, ProviderConfig] = {}
    for name, raw_provider in provider_table.items():
        if not isinstance(raw_provider, dict):
            raise ValueError(f"providers.{name} must be a table")
        providers[str(name)] = _provider_from_raw(str(name), raw_provider)
    return providers


def catalog_provider(name: str) -> ProviderConfig | None:
    try:
        text = resources.files(__package__).joinpath("providers.toml").read_text()
    except FileNotFoundError:
        return None
    return _load_provider_table(text).get(name)


def merged_provider(name: str, raw: ProviderConfig | None) -> ProviderConfig:
    catalog = catalog_provider(name)
    if catalog is None and raw is None:
        return ProviderConfig(name=name)
    if catalog is None:
        provider = raw or ProviderConfig(name=name)
    elif raw is None:
        provider = catalog
    else:
        provider = ProviderConfig(
            name=name,
            command=raw.command or catalog.command,
            retry_exit_codes=raw.retry_exit_codes or catalog.retry_exit_codes,
            retry_patterns=raw.retry_patterns or catalog.retry_patterns,
            never_retry_patterns=raw.never_retry_patterns or catalog.never_retry_patterns,
            retry_on_any_error=raw.retry_on_any_error or catalog.retry_on_any_error,
            interactive_mode=raw.interactive_mode or catalog.interactive_mode,
            status_command=raw.status_command or catalog.status_command,
            passthrough_commands=raw.passthrough_commands or catalog.passthrough_commands,
            auth_management=raw.auth_management or catalog.auth_management,
            fallback_monitor=raw.fallback_monitor or catalog.fallback_monitor,
            usage=raw.usage or catalog.usage,
            capacity_control=raw.capacity_control or catalog.capacity_control,
            accounts=raw.accounts,
        )
    auth = provider.auth_management
    env_command = os.environ.get(f"CLIFWRAP_PROVIDER_{name.upper()}_AUTH_COMMAND")
    env_aliases = _env_command_list(f"CLIFWRAP_PROVIDER_{name.upper()}_AUTH_ALIASES")
    passthrough_override = _env_command_list(f"CLIFWRAP_PROVIDER_{name.upper()}_PASSTHROUGH_COMMANDS")
    fallback_threshold_env_name = f"CLIFWRAP_PROVIDER_{name.upper()}_FALLBACK_THRESHOLD"
    fallback_recovery_env_name = f"CLIFWRAP_PROVIDER_{name.upper()}_FALLBACK_RECOVERY_COMMAND"
    fallback_syslog_env_name = f"CLIFWRAP_PROVIDER_{name.upper()}_FALLBACK_SYSLOG"
    fallback_stderr_env_name = f"CLIFWRAP_PROVIDER_{name.upper()}_FALLBACK_STDERR"
    fallback_action_env_name = f"CLIFWRAP_PROVIDER_{name.upper()}_FALLBACK_ACTION"
    fallback_journald_env_name = f"CLIFWRAP_PROVIDER_{name.upper()}_FALLBACK_JOURNALD"
    usage_timeout = _env_float(f"CLIFWRAP_PROVIDER_{name.upper()}_USAGE_TIMEOUT_SECONDS")
    capacity = provider.capacity_control
    capacity_default_action_env = f"CLIFWRAP_PROVIDER_{name.upper()}_CAPACITY_DEFAULT_ACTION"
    capacity_unknown_action_env = f"CLIFWRAP_PROVIDER_{name.upper()}_CAPACITY_UNKNOWN_ACTION"
    capacity_reserve_env = f"CLIFWRAP_PROVIDER_{name.upper()}_CAPACITY_RESERVE_THRESHOLD"
    capacity_default_cost_env = f"CLIFWRAP_PROVIDER_{name.upper()}_CAPACITY_DEFAULT_COST"
    capacity_costs_env = f"CLIFWRAP_PROVIDER_{name.upper()}_CAPACITY_COMMAND_COSTS"
    capacity_retention_env = f"CLIFWRAP_PROVIDER_{name.upper()}_QUEUE_RETENTION_SECONDS"
    capacity_max_items_env = f"CLIFWRAP_PROVIDER_{name.upper()}_QUEUE_MAX_ITEMS"
    capacity_snapshot_ttl_env = f"CLIFWRAP_PROVIDER_{name.upper()}_CAPACITY_SNAPSHOT_TTL_SECONDS"
    capacity_remediation_message_env = f"CLIFWRAP_PROVIDER_{name.upper()}_CAPACITY_REMEDIATION_MESSAGE"
    capacity_remediation_commands_env = f"CLIFWRAP_PROVIDER_{name.upper()}_CAPACITY_REMEDIATION_COMMANDS"
    capacity_default_action = os.environ.get(capacity_default_action_env)
    if capacity_default_action and capacity_default_action not in {"execute", "queue", "shed"}:
        raise ValueError(f"{capacity_default_action_env} must be 'execute', 'queue', or 'shed'")
    capacity_unknown_action = os.environ.get(capacity_unknown_action_env)
    if capacity_unknown_action and capacity_unknown_action not in {"allow", "queue", "shed"}:
        raise ValueError(f"{capacity_unknown_action_env} must be 'allow', 'queue', or 'shed'")
    capacity_reserve = _env_nonnegative_int(capacity_reserve_env)
    capacity_default_cost = _env_nonnegative_int(capacity_default_cost_env)
    capacity_retention = _env_nonnegative_int(capacity_retention_env)
    capacity_max_items = _env_nonnegative_int(capacity_max_items_env)
    capacity_snapshot_ttl = _env_nonnegative_int(capacity_snapshot_ttl_env)
    capacity_costs = _env_int_map(capacity_costs_env)
    capacity_remediation_commands = _env_command_list(capacity_remediation_commands_env)
    capacity_remediation_message = os.environ.get(capacity_remediation_message_env)
    fallback_threshold = _env_int(f"CLIFWRAP_PROVIDER_{name.upper()}_FALLBACK_THRESHOLD")
    fallback_action = os.environ.get(fallback_action_env_name)
    if fallback_action and fallback_action not in {"warn", "fail"}:
        raise ValueError(f"{fallback_action_env_name} must be 'warn' or 'fail'")
    fallback_recovery = _env_command_list(f"CLIFWRAP_PROVIDER_{name.upper()}_FALLBACK_RECOVERY_COMMAND")
    fallback_journald = _env_bool(f"CLIFWRAP_PROVIDER_{name.upper()}_FALLBACK_JOURNALD", provider.fallback_monitor.journald if provider.fallback_monitor else False)
    fallback_syslog = _env_bool(f"CLIFWRAP_PROVIDER_{name.upper()}_FALLBACK_SYSLOG", provider.fallback_monitor.syslog if provider.fallback_monitor else True)
    fallback_stderr = _env_bool(f"CLIFWRAP_PROVIDER_{name.upper()}_FALLBACK_STDERR", provider.fallback_monitor.stderr if provider.fallback_monitor else True)
    if auth or env_command or env_aliases:
        provider.auth_management = AuthManagementConfig(
            command=env_command or (auth.command if auth else "auth"),
            aliases=env_aliases or (auth.aliases if auth else []),
        )
    if passthrough_override:
        provider.passthrough_commands = passthrough_override
    if (
        fallback_threshold is not None
        or fallback_recovery
        or provider.fallback_monitor
        or fallback_syslog_env_name in os.environ
        or fallback_stderr_env_name in os.environ
        or fallback_threshold_env_name in os.environ
        or fallback_recovery_env_name in os.environ
        or fallback_action_env_name in os.environ
        or fallback_journald_env_name in os.environ
    ):
        provider.fallback_monitor = FallbackMonitorConfig(
            threshold=fallback_threshold if fallback_threshold is not None else (provider.fallback_monitor.threshold if provider.fallback_monitor else 3),
            action=fallback_action or (provider.fallback_monitor.action if provider.fallback_monitor else "warn"),
            journald=fallback_journald,
            syslog=fallback_syslog,
            stderr=fallback_stderr,
            recovery_command=fallback_recovery or (provider.fallback_monitor.recovery_command if provider.fallback_monitor else None),
        )
    if usage_timeout is not None and provider.usage:
        provider.usage.timeout_seconds = usage_timeout
    if (
        capacity
        or capacity_default_action is not None
        or capacity_unknown_action is not None
        or capacity_reserve is not None
        or capacity_default_cost is not None
        or capacity_retention is not None
        or capacity_max_items is not None
        or capacity_snapshot_ttl is not None
        or capacity_costs
        or capacity_remediation_message is not None
        or capacity_remediation_commands
    ):
        provider.capacity_control = CapacityControlConfig(
            default_action=capacity_default_action or (capacity.default_action if capacity else "queue"),
            unknown_capacity_action=capacity_unknown_action or (capacity.unknown_capacity_action if capacity else "allow"),
            reserve_threshold=capacity_reserve if capacity_reserve is not None else (capacity.reserve_threshold if capacity else 0),
            default_cost=capacity_default_cost if capacity_default_cost is not None else (capacity.default_cost if capacity else 1),
            command_costs=capacity_costs or (dict(capacity.command_costs) if capacity else {}),
            queue_retention_seconds=capacity_retention if capacity_retention is not None else (capacity.queue_retention_seconds if capacity else 86400),
            queue_max_items=capacity_max_items if capacity_max_items is not None else (capacity.queue_max_items if capacity else 100),
            snapshot_ttl_seconds=capacity_snapshot_ttl if capacity_snapshot_ttl is not None else (capacity.snapshot_ttl_seconds if capacity else 60),
            remediation_message=capacity_remediation_message if capacity_remediation_message is not None else (capacity.remediation_message if capacity else None),
            remediation_commands=capacity_remediation_commands or (list(capacity.remediation_commands) if capacity else []),
        )
        if provider.capacity_control.default_cost < 1:
            raise ValueError(f"{capacity_default_cost_env} must be at least 1")
        if provider.capacity_control.queue_retention_seconds < 1:
            raise ValueError(f"{capacity_retention_env} must be at least 1")
        if provider.capacity_control.queue_max_items < 1:
            raise ValueError(f"{capacity_max_items_env} must be at least 1")
        if provider.capacity_control.snapshot_ttl_seconds < 1:
            raise ValueError(f"{capacity_snapshot_ttl_env} must be at least 1")
    return provider


def load_config() -> WrapperConfig:
    path = config_path()
    if not path.exists():
        return WrapperConfig()
    return WrapperConfig(providers=_load_provider_table(path.read_text()))
