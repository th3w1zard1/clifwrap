# Configuration

`clifwrap` keeps runtime policy in TOML and lets environment variables override execution-time details. The default config path is:

```text
~/.config/clifwrap/config.toml
```

Override paths with:

```bash
CLIFWRAP_CONFIG=/path/to/config.toml
CLIFWRAP_STATE_DIR=/path/to/state
CLIFWRAP_BIN_DIR=/path/to/bin
```

## Providers

Provider behavior is declarative. Built-in metadata for `tvly` and `firecrawl` lives in `src/clifwrap/providers.toml`; user config should normally define only accounts and local policy overrides.

```toml
[[providers.tvly.accounts]]
name = "primary"
env_files = ["~/.config/secrets.env"]
env = { TAVILY_API_KEY = "env:TAVILY_API_KEY" }

[[providers.tvly.accounts]]
name = "backup"
env_files = ["~/.config/secrets.env"]
env = { TAVILY_API_KEY = "env:TAVILY_API_KEY_BACKUP" }
```

Account names are user labels. They are not generated from provider-specific hardcoded conventions.

Provider names are also treated as data. `clifwrap account add` writes normal TOML keys for simple provider names and quoted TOML keys when a command name contains characters such as dots, so a command like `some.cli` remains one provider instead of becoming nested TOML tables.

Provider tables can define:

- `command`: command array used instead of the provider name when executing upstream.
- `retry_exit_codes`: exit codes that allow retrying the same command on another account.
- `retry_patterns`: lower-cased stderr/stdout snippets that allow retry.
- `never_retry_patterns`: lower-cased snippets that block retry even if another retry rule matches.
- `retry_on_any_error`: retry any nonzero upstream exit unless blocked by `never_retry_patterns`.
- `interactive_mode`: currently `line-repl` for line-by-line wrapped interactive shells.
- `status_command`: command array that returns JSON usage data with `remaining`, `limit`, and optionally `used`.
- `passthrough_commands`: upstream subcommands that should bypass wrapper-managed auth aliases.

## Accounts

Each account is a table under `[[providers.<name>.accounts]]`.

```toml
[[providers.somecli.accounts]]
name = "team-alpha"
enabled = true
env_files = ["~/.config/secrets.env"]
env = { SOMECLI_TOKEN = "env:SOMECLI_TEAM_ALPHA" }
env_command = { SOMECLI_SESSION = ["secret-tool", "lookup", "service", "somecli", "account", "team-alpha"] }
prepare_command = ["somecli", "login", "--token", "${SOMECLI_TOKEN}"]
prepare_on = "once"
```

Supported account fields:

- `name`: user-controlled label. Labels are data and should describe ownership or purpose.
- `enabled`: defaults to `true`; disabled accounts stay configured but are skipped for execution.
- `env_files`: files containing shell-style `KEY=value` secret definitions.
- `env`: literal values or `env:OTHER_ENV_VAR` references injected for this account.
- `env_command`: command arrays whose stdout becomes the named environment value before each attempt.
- `prepare_command`: optional command run before attempting the account.
- `prepare_on`: `always`, `once`, or `never`.

`CLIFWRAP_ACCOUNT=<name>` starts a single wrapped command from a specific enabled account without changing config.

## Auth Management

Wrapper-managed auth aliases are separate from upstream auth commands. This lets commands such as `tvly logins` or `firecrawl accounts` manage wrapper accounts while a bare canonical auth command can still pass through to the real CLI.

```toml
[providers.somecli.auth_management]
command = "login"
aliases = ["accounts", "logins"]
```

Environment overrides:

- `CLIFWRAP_PROVIDER_<NAME>_AUTH_COMMAND`: replace the canonical wrapper-managed auth command.
- `CLIFWRAP_PROVIDER_<NAME>_AUTH_ALIASES`: comma-separated aliases.
- `CLIFWRAP_PROVIDER_<NAME>_PASSTHROUGH_COMMANDS`: comma-separated upstream subcommands that bypass wrapper auth handling.

## Capacity Control

Capacity control gates work before the original CLI receives the request. This prevents fallback accounts from being consumed below reserve unless capacity is unknown and policy explicitly allows execution.

```toml
[providers.somecli.capacity_control]
default_action = "queue"
unknown_capacity_action = "allow"
reserve_threshold = 5
default_cost = 1
command_costs = { search = 2, extract = 5 }
queue_retention_seconds = 86400
queue_max_items = 100
remediation_message = "Provision more credits or enable another account."
remediation_commands = ["clifwrap account add somecli <name> --env-ref SOMECLI_TOKEN=ENVVAR"]
```

Valid `default_action` values are `execute`, `queue`, and `shed`.

Valid `unknown_capacity_action` values are `allow`, `queue`, and `shed`.

Capacity fields:

- `default_action`: action when every known account is below `reserve_threshold + estimated_cost`.
- `unknown_capacity_action`: action when no account can produce usage data.
- `reserve_threshold`: capacity floor to preserve.
- `default_cost`: estimated cost when the subcommand has no explicit cost.
- `command_costs`: per-subcommand cost map.
- `queue_retention_seconds`: how long queued items remain replayable.
- `queue_max_items`: maximum queued items per provider before new work is shed.
- `snapshot_ttl_seconds`: usage-cache TTL used while making admission decisions.
- `remediation_message`: operator-facing guidance surfaced by status and queue decisions.
- `remediation_commands`: team-approved commands to provision or enable more capacity.

Environment overrides:

- `CLIFWRAP_PROVIDER_<NAME>_CAPACITY_DEFAULT_ACTION`
- `CLIFWRAP_PROVIDER_<NAME>_CAPACITY_UNKNOWN_ACTION`
- `CLIFWRAP_PROVIDER_<NAME>_CAPACITY_RESERVE_THRESHOLD`
- `CLIFWRAP_PROVIDER_<NAME>_CAPACITY_DEFAULT_COST`
- `CLIFWRAP_PROVIDER_<NAME>_CAPACITY_COMMAND_COSTS`: `key=value,key=value` or a TOML inline table.
- `CLIFWRAP_PROVIDER_<NAME>_QUEUE_RETENTION_SECONDS`
- `CLIFWRAP_PROVIDER_<NAME>_QUEUE_MAX_ITEMS`
- `CLIFWRAP_PROVIDER_<NAME>_CAPACITY_SNAPSHOT_TTL_SECONDS`
- `CLIFWRAP_PROVIDER_<NAME>_CAPACITY_REMEDIATION_MESSAGE`
- `CLIFWRAP_PROVIDER_<NAME>_CAPACITY_REMEDIATION_COMMANDS`: comma-separated command strings.

## Usage Lookups

HTTP usage lookups power `clifwrap status` and capacity control.

```toml
[providers.somecli.usage]
url = "https://api.example.test/usage"
auth_env = "SOMECLI_TOKEN"
timeout_seconds = 15
auth_header = "Authorization"
auth_scheme = "Bearer"
content_type = "application/json"
remaining_path = "data.remaining"
limit_path = "data.limit"
used_path = "data.used"
label = "credits"
```

Supported fields:

- `url`: HTTP endpoint. Environment defaults inside braces are supported, for example `{API_URL:https://api.example.test}`.
- `auth_env`: account environment key that contains the credential.
- `timeout_seconds`: positive HTTP timeout.
- `auth_header`: header name, default `Authorization`.
- `auth_scheme`: header scheme, default `Bearer`.
- `content_type`: optional `Content-Type` header.
- `used_path`, `limit_path`, `remaining_path`: dotted JSON paths.
- `fallback_used_path`, `fallback_limit_path`: alternate dotted paths when primary limit data is unavailable.
- `label` and `fallback_label`: display labels for status output.

Environment override:

- `CLIFWRAP_PROVIDER_<NAME>_USAGE_TIMEOUT_SECONDS`

## Fallback Monitoring

Fallback monitoring reports when the enabled account pool is too small.

```toml
[providers.somecli.fallback_monitor]
threshold = 3
action = "warn"
journald = true
syslog = true
stderr = true
recovery_command = ["notify-send", "somecli fallbacks are low"]
```

Valid `action` values are `warn` and `fail`. `fail` blocks wrapped command execution while the fallback pool is below threshold.

Environment overrides:

- `CLIFWRAP_PROVIDER_<NAME>_FALLBACK_THRESHOLD`
- `CLIFWRAP_PROVIDER_<NAME>_FALLBACK_ACTION`
- `CLIFWRAP_PROVIDER_<NAME>_FALLBACK_JOURNALD`
- `CLIFWRAP_PROVIDER_<NAME>_FALLBACK_SYSLOG`
- `CLIFWRAP_PROVIDER_<NAME>_FALLBACK_STDERR`
- `CLIFWRAP_PROVIDER_<NAME>_FALLBACK_RECOVERY_COMMAND`: comma-separated command parts.

Recovery commands receive these environment variables:

- `CLIFWRAP_PROVIDER`
- `CLIFWRAP_LOW_FALLBACK_MESSAGE`
- `CLIFWRAP_LOW_FALLBACK_ENABLED`
- `CLIFWRAP_LOW_FALLBACK_UNUSED`
- `CLIFWRAP_LOW_FALLBACK_THRESHOLD`
- `CLIFWRAP_LOW_FALLBACK_ACTIVE`
- `CLIFWRAP_LOW_FALLBACK_ENABLED_NAMES`

## Idempotent Installs

`clifwrap install <provider>` is designed to be idempotent. If the target command is already a managed shim and the original backup is recorded, another install returns the same shim instead of wrapping the wrapper.

Use:

```bash
clifwrap uninstall <provider>
```

to restore the original command.
