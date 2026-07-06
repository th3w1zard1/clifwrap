# Contributing

`clifwrap` is intentionally conservative because it sits in front of real CLIs and credentials.

## Local Checks

Run the same baseline checks as CI:

```bash
python -m pip install -e ".[dev]"
python scripts/verify_release.py --skip-pyinstaller
```

The same checks are also available through Nox:

```bash
python -m pip install -e ".[dev]"
nox
nox -s release-verify -- --require-actionlint
```

If you have `actionlint` installed, `verify_release.py` runs it automatically. Use `--require-actionlint` when validating workflow semantics before release.

For a local PyInstaller smoke test:

```bash
python -m pip install -e ".[release]"
python scripts/verify_release.py
```

## Design Rules

- Keep provider-specific behavior in `providers.toml` or user configuration, not hardcoded in generic runtime paths.
- Keep installs idempotent. Re-running `clifwrap install` must not wrap an existing managed shim.
- Keep uninstalls conservative. If the backup is missing or the target no longer contains the managed shim marker, fail before modifying files.
- Preserve passthrough behavior when no managed accounts or policies apply.
- Do not log secret values.
- Add regression tests for stdin, retry, queue, and auth-management behavior before changing wrapper execution flow.

## Release Rules

Release automation is documented in [docs/release.md](docs/release.md). Manual releases must remain prerelease until validation workflows pass and artifacts are uploaded.
