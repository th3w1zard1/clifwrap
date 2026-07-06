# Operations Runbook

This runbook covers routine checks and recovery actions for `clifwrap` installations.

## Local Health Check

Run the status command before and after account or policy changes:

```bash
clifwrap config paths
clifwrap config validate
clifwrap doctor --check
clifwrap doctor --json --check
clifwrap status --check
clifwrap status --json --check
```

`config paths` prints the resolved `CLIFWRAP_CONFIG`, `CLIFWRAP_STATE_DIR`, and `CLIFWRAP_BIN_DIR` destinations. `config validate` checks only the TOML config parser and returns nonzero for invalid config without inspecting shims or queue state.

`doctor` checks local paths, config parsing, installed shim records, original backups, default account pointers, and queue-state readability.

`status --check` exits nonzero when a provider has low fallback capacity, an unhealthy queue, a persisted recovery-hook failure, or capacity that is below the configured policy threshold.

## Account Inventory

List accounts through wrapper-owned commands instead of relying on provider-specific login state:

```bash
clifwrap account list
clifwrap account list --json
tvly logins
firecrawl accounts
```

Use account labels that describe ownership or purpose. Do not encode secret names or provider-specific assumptions in labels; bind secrets through env files, env refs, or command-backed lookups.

The JSON account inventory is safe for automation logs: it includes account labels and configured key names, but not secret values.

## Queue Management

When capacity is low and policy is set to `queue`, wrapped commands are persisted under the wrapper state directory:

```bash
clifwrap queue list --json
clifwrap queue run
clifwrap queue drop --expired
```

`queue run` rechecks capacity before replay. If capacity is still below reserve, the item stays queued and its replay metadata is updated instead of duplicating work.

## Shim Recovery

Installs are idempotent:

```bash
clifwrap install tvly firecrawl
```

Uninstall refuses unsafe recovery if the target is no longer a managed shim or the recorded original backup is missing:

```bash
clifwrap uninstall tvly firecrawl
```

If an upstream CLI was replaced manually, inspect the configured `CLIFWRAP_BIN_DIR` and state directory before forcing any filesystem changes. The safe path is to restore the original executable first, then rerun `clifwrap install`.

Run `clifwrap doctor --json --check` before and after recovery so the target, backup, and managed-shim marker are verified from the wrapper's own state.

## Release Verification

Before cutting or publishing a release, run:

```bash
python -m pip install -e ".[dev,release]"
python scripts/verify_release.py --require-actionlint
```

The verifier checks workflow syntax, GitHub Actions linting, Ruff, pytest reports, compileall, package archives, wheel install smoke, Pages generation, and a PyInstaller binary smoke test. It removes generated artifacts unless `--keep-artifacts` is passed.

Use `--summary-json release-summary.json` when a CI job or maintainer handoff needs a compact machine-readable proof record after validation succeeds.
