# clifwrap

[![CI](https://github.com/clifwrap/clifwrap/actions/workflows/ci.yml/badge.svg)](https://github.com/clifwrap/clifwrap/actions/workflows/ci.yml)

`clifwrap` installs reversible shims in front of existing CLIs like `tvly` and `firecrawl`, then retries the same command against alternate accounts when a configured account is exhausted, rate-limited, unauthenticated, or otherwise matches a retry rule.

The defaults are intentionally conservative:

- No config means pure passthrough.
- No wrapped shim means your original CLI stays untouched.
- Uninstall restores the original binary in place.
- Provider-specific behavior ships in `providers.toml`, not in provider-name branches in the wrapper runtime.

## What It Solves

- `tvly` can authenticate from `TAVILY_API_KEY`, `~/.tavily/config.json`, or OAuth tokens under `~/.mcp-auth/`.
- `firecrawl` can authenticate from `FIRECRAWL_API_KEY`, per-command `--api-key`, or stored credentials under `~/.config/firecrawl-cli/credentials.json`.
- Both support account-specific environment overrides, so the wrapper can fail over without mutating the user’s main login state.

For CLIs with a configured interactive mode, the wrapper can run a line-by-line shell and reissue each typed command through the same account failover engine as non-interactive commands.

## Install

```bash
pipx install .
mkdir -p ~/.config/clifwrap
clifwrap sample-config > ~/.config/clifwrap/config.toml
clifwrap install tvly firecrawl
```

This replaces the discovered `tvly` and `firecrawl` executables with managed shims and moves the originals into the wrapper state directory under their original command names.

If your config already has `[providers.*]` entries, `clifwrap install` with no arguments installs all configured providers. `clifwrap uninstall` with no arguments restores every shim recorded in wrapper state.

Installs are idempotent. Re-running `clifwrap install <provider>` detects an existing managed shim and recorded backup instead of wrapping the shim again.

You can also build config incrementally:

```bash
clifwrap init
clifwrap account add tvly primary --env-file ~/.config/secrets.env --env-ref TAVILY_API_KEY=TAVILY_PRIMARY
clifwrap account add tvly backup --env-file ~/.config/secrets.env --env-ref TAVILY_API_KEY=TAVILY_BACKUP
clifwrap account add somecli dynamic --env-command SOMECLI_TOKEN='secret-tool lookup service somecli account dynamic'
clifwrap account list tvly
clifwrap account list tvly --json
clifwrap doctor --check
clifwrap install
```

To undo:

```bash
clifwrap uninstall tvly firecrawl
pipx uninstall clifwrap
```

`clifwrap uninstall` refuses to proceed if the recorded original backup is missing or if the current target is no longer a managed `clifwrap` shim. That prevents cleanup commands from deleting or overwriting an unrelated replacement executable.

## Config

Config lives at `~/.config/clifwrap/config.toml` by default. Override it with `CLIFWRAP_CONFIG=/path/to/config.toml`.

### Minimal Tavily

```toml
[[providers.tvly.accounts]]
name = "acct-a"
env_files = ["~/.config/secrets.env"]
env = { TAVILY_API_KEY = "env:TAVILY_API_KEY" }

[[providers.tvly.accounts]]
name = "acct-b"
env_files = ["~/.config/secrets.env"]
env = { TAVILY_API_KEY = "env:TAVILY_API_KEY2" }
```

### Minimal Firecrawl

```toml
[[providers.firecrawl.accounts]]
name = "team-a"
env_files = ["~/.config/secrets.env"]
env = { FIRECRAWL_API_KEY = "env:FIRECRAWL_API_KEY_A" }

[[providers.firecrawl.accounts]]
name = "team-b"
env_files = ["~/.config/secrets.env"]
env = { FIRECRAWL_API_KEY = "env:FIRECRAWL_API_KEY_B" }
```

The built-in Tavily and Firecrawl retry rules, auth-management command names, and usage endpoints live in the packaged catalog at `src/clifwrap/providers.toml`. User config only needs account definitions unless you want to override provider behavior.

More configuration details live in [docs/configuration.md](docs/configuration.md).

### Capacity Control and Backpressure

Use `capacity_control` to gate requests before they hit the upstream CLI:

```toml
[providers.somecli]
status_command = ["somecli", "usage", "--json"]

[providers.somecli.capacity_control]
default_action = "queue"
unknown_capacity_action = "allow"
reserve_threshold = 5
default_cost = 1
queue_retention_seconds = 86400
queue_max_items = 100
snapshot_ttl_seconds = 60
command_costs = { search = 2, extract = 5 }
remediation_message = "Provision more credits or enable another account."
remediation_commands = ["clifwrap account add somecli <name> --env-ref SOMECLI_TOKEN=ENVVAR"]
```

`default_action` applies when the wrapper can read capacity and every enabled account is below `reserve_threshold + estimated_cost`. Valid values are `execute`, `queue`, and `shed`.

`unknown_capacity_action` applies when usage lookup is unavailable for every enabled account. Valid values are `allow`, `queue`, and `shed`.

Command costs are metadata, not runtime constants. The shipped Tavily and Firecrawl defaults are conservative estimates stored in `providers.toml` so you can override them without editing code.

When capacity control admits a specific account, retries for that request stay scoped to that capacity-approved account. This prevents a retryable upstream failure from consuming below-reserve fallback accounts.

### Wrapper Auth Management

Native auth commands still pass through:

```bash
tvly auth
firecrawl login
```

Wrapper account management is exposed under explicit subcommands:

```bash
tvly auth list
tvly logins
tvly logins use acct-b
tvly credentials default
tvly auth use acct-b
tvly auth add acct-c --env-file ~/.config/secrets.env --env-ref TAVILY_API_KEY=TAVILY_API_KEY3
tvly auth add acct-d --env-command TAVILY_API_KEY='secret-tool lookup service tavily account acct-d'
tvly auth disable acct-c
tvly auth enable acct-c
tvly auth remove acct-c

firecrawl login accounts
firecrawl accounts
firecrawl login use team-b
firecrawl login add team-c --env-file ~/.config/secrets.env --env-ref FIRECRAWL_API_KEY=FIRECRAWL_API_KEY_C
firecrawl login add team-d --env-command FIRECRAWL_API_KEY='secret-tool lookup service firecrawl account team-d'
firecrawl login disable team-c
firecrawl login remove team-c
```

For Tavily, the built-in wrapper aliases include `accounts`, `logins`, and `credentials`. For Firecrawl, wrapper-managed auth stays under `login` plus any configured aliases. A bare alias such as `tvly logins` or `firecrawl accounts` lists configured accounts; a bare canonical auth command such as `tvly auth` or `firecrawl login` still passes through to the upstream CLI.

`clifwrap account list --json` returns provider, name, enabled/default flags, env key names, env-command key names, env file paths, and prepare-command metadata. It intentionally does not print secret values.

Firecrawl’s upstream email/browser login is interactive and stores one active credential. For repeatable multi-account failover, give the wrapper one credential source per account; the wrapper injects `FIRECRAWL_API_KEY` for the selected account before calling the upstream CLI. Sources can be env refs, env files, command-backed lookups, or credentials captured by a helper.

For the four requested Firecrawl accounts, keep only the account labels and reusable storage template declarative in `scripts/firecrawl_requested_accounts.toml`, then run:

```bash
clifwrap account import-spec scripts/firecrawl_requested_accounts.toml --apply
```

The spec can include a `[validation]` table with a usage endpoint, auth header, auth scheme, content type, and response path. `clifwrap account import-spec` validates each candidate API key before adding it and never prints key values. If a key is missing or invalid, it reports the missing account without creating a fake config entry. Account-specific env-var names are optional; specs can use `env_name_template` to derive storage names instead of hardcoding a list for every account.

If you need to obtain those four keys through Firecrawl's interactive login flow instead of pre-populating env vars, run:

```bash
python scripts/firecrawl_login_sequence.py
```

The helper reads the account labels and storage template from `scripts/firecrawl_requested_accounts.toml`, runs one upstream `firecrawl login` per missing account, captures the resulting `~/.config/firecrawl-cli/credentials.json` API key into the configured env file, then calls `clifwrap account import-spec ... --apply`. It does not print secret values. Use `--method manual` if browser login is not appropriate, and `--replace-existing` to refresh already captured keys.

When a command fails with a declarative retry condition and the next account succeeds, the wrapper records the next account as the default in wrapper state. Future commands start there. `CLIFWRAP_ACCOUNT=<name>` still overrides the default for a single invocation.

### Optional Auth Commands

If a CLI needs a preparatory login step instead of plain environment injection, attach a per-account `prepare_command`:

```toml
[[providers.somecli.accounts]]
name = "persisted-login"
env = { SOMECLI_TOKEN = "env:SOMECLI_TOKEN_A" }
env_command = { SOMECLI_REFRESHED_TOKEN = ["secret-tool", "lookup", "service", "somecli"] }
prepare_on = "once"
prepare_command = ["somecli", "login", "--token", "${SOMECLI_TOKEN}"]
```

`env_command` runs before each attempt and uses stdout as the environment value. `prepare_command` runs immediately before that account is attempted. Set `prepare_on = "once"` to make it idempotent for the rendered command, `prepare_on = "always"` to run every attempt, or `prepare_on = "never"` to temporarily disable it.

To temporarily start from a specific configured account:

```bash
CLIFWRAP_ACCOUNT=secondary tvly search "autoclaw cli"
```

## Status

`clifwrap status` prints status for every provider configured in `config.toml`. Use `clifwrap status <provider>` for one provider.

Add `--json` for machine-readable output, either across all configured providers or for one provider:

```bash
clifwrap status --json
clifwrap status --json firecrawl
```

Add `--check` for CI or monitoring probes. It exits `1` when any reported provider has low-fallback state or a persisted recovery-hook error, while still printing the normal human or JSON output:

```bash
clifwrap status --check
clifwrap status --json --check firecrawl
```

`clifwrap status tvly` calls Tavily’s documented `GET /usage` endpoint when the account exposes `TAVILY_API_KEY`, then prints per-key or plan usage and remaining quota.

`clifwrap status firecrawl` calls Firecrawl’s `GET /v2/team/credit-usage` endpoint when the account exposes `FIRECRAWL_API_KEY`.

When remaining counts are available, status also prints total remaining capacity across all available configured accounts.

For arbitrary providers, define a `status_command`. If it prints JSON with `remaining`, `limit`, and optionally `used`, `clifwrap status` will include that account in the total:

```toml
[providers.somecli]
status_command = ["somecli", "usage", "--json"]
```

For HTTP usage lookups, set `timeout_seconds` under `[providers.<name>.usage]` or override it at execution time with `CLIFWRAP_PROVIDER_<NAME>_USAGE_TIMEOUT_SECONDS`.

When `capacity_control` is enabled, `clifwrap status` also reports:

- Current capacity health for the provider.
- Queue backlog and expired queued items.
- The provider's configured remediation message, when present.

`clifwrap status --check` exits nonzero when queue backlog, queue-state errors, or low-capacity health are present, in addition to the existing low-fallback and recovery-hook signals.

## Doctor

`clifwrap doctor` is a read-only local diagnostic for config, state, installed shims, and queue health:

```bash
clifwrap doctor
clifwrap doctor --json --check
```

Use it before manually editing wrapper state. `--check` exits nonzero when config parsing fails, install state is malformed, a recorded shim target is missing or unmanaged, an original backup is missing, a default account points at a missing or disabled account, or queue state is malformed.

For narrower config-only checks, use:

```bash
clifwrap config paths
clifwrap config paths --json
clifwrap config validate
clifwrap config validate --json
```

`config paths` prints the resolved config, state, and shim-bin paths after environment overrides. `config validate` parses `config.toml` with the same loader used by wrapped commands and reports provider/account counts without printing secret values.

## Queue Commands

When a command is queued, `clifwrap` stores the provider, argv, enqueue time, replay count, reason, and policy snapshot in wrapper state and returns exit code `73`.

```bash
clifwrap queue list
clifwrap queue list firecrawl --json
clifwrap queue run
clifwrap queue run tvly --id <queue-id>
clifwrap queue drop firecrawl --expired
```

`clifwrap queue run` rechecks current capacity before replaying each item. If the command is still blocked, the item stays queued and its replay metadata is updated instead of creating a duplicate entry. If replay succeeds, the item is removed.

Runtime overrides are available for the capacity policy:

```bash
CLIFWRAP_PROVIDER_FIRECRAWL_CAPACITY_DEFAULT_ACTION=queue \
CLIFWRAP_PROVIDER_FIRECRAWL_CAPACITY_UNKNOWN_ACTION=allow \
CLIFWRAP_PROVIDER_FIRECRAWL_CAPACITY_RESERVE_THRESHOLD=50 \
CLIFWRAP_PROVIDER_FIRECRAWL_CAPACITY_DEFAULT_COST=5 \
CLIFWRAP_PROVIDER_FIRECRAWL_CAPACITY_COMMAND_COSTS="crawl=25,scrape=5,search=5" \
CLIFWRAP_PROVIDER_FIRECRAWL_QUEUE_RETENTION_SECONDS=3600 \
CLIFWRAP_PROVIDER_FIRECRAWL_QUEUE_MAX_ITEMS=250 \
firecrawl crawl https://example.com
```

## Low-Fallback Alerts

For `tvly` and `firecrawl`, `clifwrap` warns when fewer than three unused enabled accounts remain. For custom providers, enable the same behavior with a provider-level monitor:

```toml
[providers.somecli.fallback_monitor]
threshold = 3
action = "warn"
journald = true
syslog = true
stderr = true
recovery_command = ["notify-send", "somecli fallbacks are low"]
```

`unused_fallbacks` means enabled accounts minus the currently active account. When the pool is low, `clifwrap status <provider>` prints the enabled count, fallback count, active account, and a remediation hint. During wrapped command execution, the same warning is sent to journald via `systemd-cat` when enabled and available, to syslog when enabled, and to interactive `stderr` when enabled.

Set `action = "fail"` to stop wrapped command execution while the pool is below threshold. The command exits with status `75` after emitting the same alert and recovery hook. The default is `action = "warn"` so existing workflows continue running.

The recovery command is optional and runs in the background once per distinct low-pool state. It receives `CLIFWRAP_PROVIDER`, `CLIFWRAP_LOW_FALLBACK_MESSAGE`, `CLIFWRAP_LOW_FALLBACK_ENABLED`, `CLIFWRAP_LOW_FALLBACK_UNUSED`, `CLIFWRAP_LOW_FALLBACK_THRESHOLD`, `CLIFWRAP_LOW_FALLBACK_ACTIVE`, and `CLIFWRAP_LOW_FALLBACK_ENABLED_NAMES`. If the hook cannot be started, `clifwrap status <provider>` reports the last launch failure until a later recovery launch succeeds.

Runtime overrides use provider-specific env vars:

```bash
CLIFWRAP_PROVIDER_FIRECRAWL_FALLBACK_THRESHOLD=2 \
CLIFWRAP_PROVIDER_FIRECRAWL_FALLBACK_ACTION=warn \
CLIFWRAP_PROVIDER_FIRECRAWL_FALLBACK_JOURNALD=true \
CLIFWRAP_PROVIDER_FIRECRAWL_FALLBACK_SYSLOG=true \
CLIFWRAP_PROVIDER_FIRECRAWL_FALLBACK_STDERR=true \
CLIFWRAP_PROVIDER_FIRECRAWL_FALLBACK_RECOVERY_COMMAND="notify-send,firecrawl fallbacks are low" \
firecrawl search "example"
```

## Notes

- Wrapped commands buffer each failed attempt and only print the final successful output, plus short failover notices on `stderr`.
- If no account matches or no retry condition is met, the wrapper returns the original command’s exit code and output unchanged.
- The wrapper does not edit upstream config files unless you explicitly use a `prepare_command` that does so. Wrapper defaults, queue state, usage cache, and alert metadata are stored separately under `~/.local/state/clifwrap/`.

## Documentation

- [Configuration](docs/configuration.md)
- [CLI reference](docs/cli-reference.md)
- [Built-in provider catalog](docs/provider-catalog.md)
- [Migration to clifwrap](docs/migration.md)
- [Operations runbook](docs/operations.md)
- [Release process](docs/release.md)
- [Research notes](docs/RESEARCH.md)
- [Security policy](SECURITY.md)

## Release Automation

The repository is intended to live at `github.com/clifwrap/clifwrap`, with project reports published to `https://clifwrap.github.io`.

Release automation uses `release-please` plus validation workflows. Manually created GitHub releases are marked prerelease while tests, PyInstaller builds, artifact uploads, `SHA256SUMS`, and `RELEASE-MANIFEST.json` generation run; the workflow only clears prerelease after those gates succeed.
