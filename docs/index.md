# clifwrap

`clifwrap` is a transparent wrapper for CLIs that depend on account-scoped credentials, quotas, or rate limits. It installs reversible shims, applies declarative account configuration, checks capacity before requests, and fails over only when policy allows it.

Start with:

```bash
pipx install clifwrap
clifwrap init
clifwrap account add tvly primary --env-file ~/.config/secrets.env --env-ref TAVILY_API_KEY=TAVILY_API_KEY
clifwrap install tvly
```

Core documentation:

- [Configuration](configuration.md)
- [CLI reference](cli-reference.md)
- [Built-in provider catalog](provider-catalog.md)
- [Migration to clifwrap](migration.md)
- [Operations runbook](operations.md)
- [Release process](release.md)
- [Research notes](RESEARCH.md)

Project status, pytest reports, JUnit XML, release-summary JSON, and rendered docs are published from CI to GitHub Pages.
