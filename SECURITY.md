# Security Policy

`clifwrap` sits in front of CLIs that may use account-scoped API keys, OAuth tokens, or browser-login credentials. Treat wrapper configuration, state, logs, and release artifacts as security-sensitive.

## Supported Versions

Until the project reaches `1.0`, only the latest released version is supported for security fixes.

## Automated Analysis

The repository runs CodeQL analysis for Python on pushes, pull requests, a weekly schedule, and manual dispatch. Findings should be triaged before a release is marked production-ready.

Pull requests also run dependency review and fail when dependency changes introduce high-severity vulnerable packages.

Dependabot opens grouped weekly update pull requests for Python dependencies and GitHub Actions so security and platform patches flow through the same reviewed CI path as source changes.

## Reporting a Vulnerability

Report suspected vulnerabilities through GitHub private vulnerability reporting or GitHub Security Advisories for `github.com/th3w1zard1/clifwrap`.

If the GitHub repository is not available yet, contact the maintainers privately through the same trusted channel used to receive release artifacts. Do not open a public issue with secret values, tokens, or exploit details.

## Secret Handling Expectations

- Do not commit API keys, OAuth tokens, browser-login credentials, or generated env files.
- Prefer `env:` references, `env_files`, or command-backed secret lookups over literal config values.
- `clifwrap account list --json`, `clifwrap doctor --json`, and release verification are designed not to print secret values.
- Upstream provider CLIs may still print their own diagnostics; review logs before sharing them.

## Local State

By default, config and state live under:

```text
~/.config/clifwrap/config.toml
~/.local/state/clifwrap/
```

The state directory can contain default-account choices, queue metadata, usage-cache data, recovery-hook failures, and original executable backups for installed shims. Protect it with the same care as other local developer credentials and tooling state.
