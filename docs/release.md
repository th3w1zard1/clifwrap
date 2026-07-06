# Release Process

Releases are automated through GitHub Actions.

## Continuous Integration

Every push and pull request runs on Python 3.11, 3.12, 3.13, and 3.14:

```bash
python -m nox -s tests-3.11
python -m nox -s tests-3.12
python -m nox -s tests-3.13
python -m nox -s tests-3.14
python -m nox -s lint compile build
```

The workflow invokes the same Nox sessions defined in `noxfile.py` so local and hosted validation do not drift. Generated docs such as `docs/cli-reference.md` and `docs/provider-catalog.md` are checked during release verification.

The Pages workflow always builds an HTML pytest report, JUnit XML, rendered project docs, schema files, and a `release-summary.json` proof file. Public repositories deploy those files to `https://clifwrap.github.io`; private repositories upload the generated `site/` directory as a normal Actions artifact because GitHub Pages for private repositories depends on the account plan.

The CodeQL workflow runs Python code scanning on pushes, pull requests, a weekly schedule, and manual dispatch when the repository is public or otherwise has code scanning enabled. Private repositories without GitHub Advanced Security skip the scan instead of failing every push.

The Dependency Review workflow runs on pull requests and blocks newly introduced high-severity vulnerable dependencies when GitHub Advanced Security dependency review is available. Private repositories without that feature skip the job.

Dependabot opens grouped weekly pull requests for Python packaging dependencies and GitHub Actions updates. The grouped cadence keeps dependency maintenance visible without creating a separate pull request for every transitive update.

For local release-quality validation, run:

```bash
python -m pip install -e ".[dev,release]"
python scripts/verify_release.py
```

For contributor automation, `nox` runs the same lint, test, compile, build, and Pages-generation sessions that CI uses:

```bash
python -m pip install -e ".[dev]"
nox
nox -s release-verify -- --require-actionlint
```

Install `actionlint` locally and pass `--require-actionlint` when you want the local run to fail if GitHub Actions semantic linting cannot run. CI downloads and runs `actionlint` automatically.

The local verifier also writes and validates `dist/SHA256SUMS` and `dist/RELEASE-MANIFEST.json` for the locally built release artifacts before removing generated files. The manifest schema is versioned in `docs/schemas/release-manifest.v1.json` and published by Pages at `https://clifwrap.github.io/schemas/release-manifest.v1.json`.

Pass `--summary-json <path>` to write a machine-readable proof summary after all checks pass. The summary includes timestamps, Python/runtime platform details, selected verifier options, completed check names, and release artifact names observed before cleanup.

The verifier also enforces workflow contracts that are easy to weaken accidentally: CI and release validation must both cover Python 3.11, 3.12, 3.13, and 3.14; release validation must be serialized per release tag; binary assets must cover Linux, macOS, and Windows on amd64 and arm64; and the release can only be marked stable after validation, packages, binaries, and checksums complete.

## Release Please

`release-please` owns version bumps, changelog updates, tags, and GitHub release creation from merged conventional commits.

Configuration lives in:

- `release-please-config.json`
- `.release-please-manifest.json`

## Binary Artifacts

Release validation builds PyInstaller one-file binaries for:

- Linux amd64
- Linux arm64
- macOS amd64 on the `macos-15-intel` GitHub-hosted runner
- macOS arm64 on the `macos-15` GitHub-hosted runner
- Windows amd64
- Windows arm64

The binary entrypoint is `packaging/pyinstaller/entrypoint.py`.

After Python distributions and platform binary archives are uploaded, the release workflow downloads the uploaded `clifwrap-*` assets, writes `SHA256SUMS`, writes a structured `RELEASE-MANIFEST.json` with each artifact name, size, and SHA-256 digest, uploads both files to the same release, and only then clears the prerelease flag.

## Manual Releases

Manual GitHub releases are forced to `prerelease` at validation start. The release workflow runs tests across Python 3.11, 3.12, 3.13, and 3.14, then builds artifacts. Only after validation and artifact upload succeed does the workflow mark the release as stable.

Release validation uses a per-tag concurrency group with `cancel-in-progress: false`, so a second manual dispatch for the same tag queues behind the active validation instead of racing it or canceling a partially completed artifact upload.
