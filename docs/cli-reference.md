# CLI Reference

This file is generated from the shipped `argparse` command surface.
Run `python scripts/generate_cli_reference.py --write` after changing CLI arguments.

## `clifwrap`

```text
usage: clifwrap [-h] [--version]
                {install,uninstall,status,doctor,init,config,account,queue,sample-config} ...

Transparent CLI failover wrapper.

positional arguments:
  {install,uninstall,status,doctor,init,config,account,queue,sample-config}
    install             Install managed shims in front of existing CLIs.
    uninstall           Restore original CLIs.
    status              Show configured account status for wrapped apps.
    doctor              Inspect local config, state, shims, and queue health.
    init                Create an empty config file if it does not exist.
    config              Inspect and validate clifwrap configuration.
    account             Manage provider accounts in config.toml.
    queue               Inspect and manage deferred work.
    sample-config       Print a sample config.toml

options:
  -h, --help            show this help message and exit
  --version             show program's version number and exit
```

## `clifwrap account`

```text
usage: clifwrap account [-h] {list,add,use,default,rename,import-spec,enable,disable,remove} ...

positional arguments:
  {list,add,use,default,rename,import-spec,enable,disable,remove}
    list                List configured accounts.
    add                 Append a provider account to config.toml.
    use                 Set the default account for a provider.
    default             Show the default account for a provider.
    rename              Rename an account in config.toml.
    import-spec         Import provider accounts from a declarative TOML spec.
    enable              Enable an account in config.toml.
    disable             Disable an account in config.toml.
    remove              Remove an account from config.toml.

options:
  -h, --help            show this help message and exit
```

## `clifwrap account add`

```text
usage: clifwrap account add [-h] [--env-file ENV_FILE] [--env ENV] [--env-ref ENV_REF]
                            [--env-command ENV_COMMAND]
                            [--prepare-command PREPARE_COMMAND [PREPARE_COMMAND ...]]
                            [--prepare-on {always,once,never}] [--disabled]
                            app name

positional arguments:
  app                   Provider command.
  name                  Account label.

options:
  -h, --help            show this help message and exit
  --env-file ENV_FILE   Read KEY=VALUE secrets from this file.
  --env ENV             Persist KEY=VALUE directly in config.
  --env-ref ENV_REF     Store KEY=ENVVAR as KEY = env:ENVVAR.
  --env-command ENV_COMMAND
                        Store KEY='command args...' and resolve it before each attempt.
  --prepare-command PREPARE_COMMAND [PREPARE_COMMAND ...]
                        Auth/preparation command to run before this account.
  --prepare-on {always,once,never}
                        When to run --prepare-command.
  --disabled            Add the account disabled.
```

## `clifwrap account default`

```text
usage: clifwrap account default [-h] app

positional arguments:
  app         Provider command.

options:
  -h, --help  show this help message and exit
```

## `clifwrap account disable`

```text
usage: clifwrap account disable [-h] app name

positional arguments:
  app         Provider command.
  name        Account label.

options:
  -h, --help  show this help message and exit
```

## `clifwrap account enable`

```text
usage: clifwrap account enable [-h] app name

positional arguments:
  app         Provider command.
  name        Account label.

options:
  -h, --help  show this help message and exit
```

## `clifwrap account import-spec`

```text
usage: clifwrap account import-spec [-h] [--env-file ENV_FILE] [--apply] spec

positional arguments:
  spec                 Path to account spec TOML.

options:
  -h, --help           show this help message and exit
  --env-file ENV_FILE  Override spec env_file.
  --apply              Write changes. Without this, only print the planned import.
```

## `clifwrap account list`

```text
usage: clifwrap account list [-h] [--json] [app]

positional arguments:
  app         Optional provider to list.

options:
  -h, --help  show this help message and exit
  --json      Emit machine-readable JSON without secret values.
```

## `clifwrap account remove`

```text
usage: clifwrap account remove [-h] app name

positional arguments:
  app         Provider command.
  name        Account label.

options:
  -h, --help  show this help message and exit
```

## `clifwrap account rename`

```text
usage: clifwrap account rename [-h] app old_name new_name

positional arguments:
  app         Provider command.
  old_name    Current account label.
  new_name    New account label.

options:
  -h, --help  show this help message and exit
```

## `clifwrap account use`

```text
usage: clifwrap account use [-h] app name

positional arguments:
  app         Provider command.
  name        Account label.

options:
  -h, --help  show this help message and exit
```

## `clifwrap config`

```text
usage: clifwrap config [-h] {paths,validate} ...

positional arguments:
  {paths,validate}
    paths           Show resolved config, state, and shim-bin paths.
    validate        Validate config.toml with the runtime parser.

options:
  -h, --help        show this help message and exit
```

## `clifwrap config paths`

```text
usage: clifwrap config paths [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json      Emit machine-readable JSON.
```

## `clifwrap config validate`

```text
usage: clifwrap config validate [-h] [--json]

options:
  -h, --help  show this help message and exit
  --json      Emit machine-readable JSON.
```

## `clifwrap doctor`

```text
usage: clifwrap doctor [-h] [--json] [--check]

options:
  -h, --help  show this help message and exit
  --json      Emit machine-readable JSON.
  --check     Exit nonzero when doctor finds config, shim, or queue issues.
```

## `clifwrap init`

```text
usage: clifwrap init [-h] [--force]

options:
  -h, --help  show this help message and exit
  --force     Overwrite the existing config with an empty config.
```

## `clifwrap install`

```text
usage: clifwrap install [-h] [apps ...]

positional arguments:
  apps        Commands to wrap.

options:
  -h, --help  show this help message and exit
```

## `clifwrap queue`

```text
usage: clifwrap queue [-h] {list,run,drop} ...

positional arguments:
  {list,run,drop}
    list           List queued work.
    run            Replay queued work.
    drop           Drop queued work.

options:
  -h, --help       show this help message and exit
```

## `clifwrap queue drop`

```text
usage: clifwrap queue drop [-h] [--expired] [--json] [app] [ids ...]

positional arguments:
  app         Optional provider to filter.
  ids         Specific queue item ids to remove.

options:
  -h, --help  show this help message and exit
  --expired   Drop only expired queue items.
  --json      Emit machine-readable JSON.
```

## `clifwrap queue list`

```text
usage: clifwrap queue list [-h] [--json] [app]

positional arguments:
  app         Optional provider to filter.

options:
  -h, --help  show this help message and exit
  --json      Emit machine-readable JSON.
```

## `clifwrap queue run`

```text
usage: clifwrap queue run [-h] [--id ID] [--json] [app]

positional arguments:
  app         Optional provider to filter.

options:
  -h, --help  show this help message and exit
  --id ID     Replay only one queue item.
  --json      Emit machine-readable JSON.
```

## `clifwrap sample-config`

```text
usage: clifwrap sample-config [-h]

options:
  -h, --help  show this help message and exit
```

## `clifwrap status`

```text
usage: clifwrap status [-h] [--json] [--check] [app]

positional arguments:
  app         Wrapped command name. Omit to show every configured provider.

options:
  -h, --help  show this help message and exit
  --json      Emit machine-readable JSON.
  --check     Exit nonzero when any reported provider has low fallback or recovery-hook error
              state.
```

## `clifwrap uninstall`

```text
usage: clifwrap uninstall [-h] [apps ...]

positional arguments:
  apps        Commands to unwrap

options:
  -h, --help  show this help message and exit
```
