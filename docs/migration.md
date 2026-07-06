# Migration to clifwrap

The project, package, command, config directory, state directory, and environment variables were renamed to `clifwrap`.

## Rename Map

| Old | New |
| --- | --- |
| `cli-fallback-wrapper` | `clifwrap` |
| Python package `cli_fallback_wrapper` | `clifwrap` |
| CLI command `clifw` | `clifwrap` |
| `~/.config/cli-fallback-wrapper` | `~/.config/clifwrap` |
| `~/.local/state/cli-fallback-wrapper` | `~/.local/state/clifwrap` |
| `CLIFW_*` | `CLIFWRAP_*` |

No legacy command alias is installed by default. This avoids ambiguity in shell shims and release artifacts.

## Manual Migration

```bash
mkdir -p ~/.config/clifwrap ~/.local/state/clifwrap
cp -a ~/.config/cli-fallback-wrapper/config.toml ~/.config/clifwrap/config.toml
cp -a ~/.local/state/cli-fallback-wrapper/. ~/.local/state/clifwrap/
clifwrap install
```

Then replace exported `CLIFW_*` variables with `CLIFWRAP_*`.
