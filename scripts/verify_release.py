#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
RELEASE_MANIFEST_SCHEMA_URL = "https://th3w1zard1.github.io/clifwrap/schemas/release-manifest.v1.json"
RELEASE_MANIFEST_SCHEMA = ROOT / "docs" / "schemas" / "release-manifest.v1.json"


def run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    print("+ " + " ".join(command), flush=True)
    subprocess.run(command, cwd=cwd, env=env, check=True)


def output(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> str:
    print("+ " + " ".join(command), flush=True)
    completed = subprocess.run(command, cwd=cwd, env=env, check=True, stdout=subprocess.PIPE, text=True)
    print(completed.stdout, end="")
    return completed.stdout.strip()


def project_version() -> str:
    try:
        import tomllib
    except ImportError as exc:
        raise SystemExit("Python 3.11+ is required for release verification.") from exc
    payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(payload["project"]["version"])


def assert_cli_version(command: list[str]) -> None:
    expected = f"clifwrap {project_version()}"
    actual = output([*command, "--version"])
    if actual != expected:
        raise SystemExit(f"{' '.join(command)} --version mismatch: expected {expected!r}, got {actual!r}")


def isolated_runtime_env(root: Path) -> dict[str, str]:
    runtime_root = root / "runtime"
    config_dir = runtime_root / "config"
    state = runtime_root / "state"
    bin_dir = runtime_root / "bin"
    config_dir.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    bin_dir.mkdir(parents=True, exist_ok=True)
    return os.environ | {
        "CLIFWRAP_CONFIG": str(config_dir / "config.toml"),
        "CLIFWRAP_STATE_DIR": str(state),
        "CLIFWRAP_BIN_DIR": str(bin_dir),
    }


def clean_artifacts() -> None:
    for path in (
        ROOT / "build",
        ROOT / "dist",
        ROOT / "site",
        ROOT / "clifwrap.spec",
        ROOT / "src" / "clifwrap.egg-info",
        ROOT / ".pytest_cache",
        ROOT / ".ruff_cache",
    ):
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache)


def parse_workflows() -> None:
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit("PyYAML is required for workflow validation; install with `python -m pip install -e '.[dev]'`.") from exc
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        payload = yaml.safe_load(path.read_text())
        if not isinstance(payload, dict) or "jobs" not in payload:
            raise SystemExit(f"{path} is not a valid workflow document")
        print(f"workflow ok: {path.relative_to(ROOT)}")


def workflow_contracts() -> None:
    try:
        import yaml
    except ImportError as exc:
        raise SystemExit("PyYAML is required for workflow contract validation; install with `python -m pip install -e '.[dev]'`.") from exc

    expected_python_versions = ["3.11", "3.12", "3.13", "3.14"]

    ci_path = ROOT / ".github" / "workflows" / "ci.yml"
    ci = _read_workflow(ci_path, yaml)
    ci_jobs = _workflow_jobs(ci, "ci.yml")
    ci_versions = ci_jobs["test"].get("strategy", {}).get("matrix", {}).get("python-version")
    if ci_versions != expected_python_versions:
        raise SystemExit(f"ci.yml Python matrix drifted: expected {expected_python_versions}, got {ci_versions}")
    ci_text = ci_path.read_text(encoding="utf-8")
    for required in ("python -m nox -s lint", "python -m nox -s compile", "python -m nox -s build", "actions/upload-artifact"):
        if required not in ci_text:
            raise SystemExit(f"ci.yml is missing required validation fragment: {required}")

    release_pr_path = ROOT / ".github" / "workflows" / "release-pr-validation.yml"
    release_pr_text = release_pr_path.read_text(encoding="utf-8")
    release_pr = _read_workflow(release_pr_path, yaml)
    release_pr_jobs = _workflow_jobs(release_pr, "release-pr-validation.yml")
    release_pr_validate = release_pr_jobs.get("validate")
    if not isinstance(release_pr_validate, dict):
        raise SystemExit("release-pr-validation.yml missing validate job")
    release_pr_versions = release_pr_validate.get("strategy", {}).get("matrix", {}).get("python-version")
    if release_pr_versions != expected_python_versions:
        raise SystemExit(f"release-pr-validation.yml Python matrix drifted: expected {expected_python_versions}, got {release_pr_versions}")
    for required in (
        "workflow_dispatch",
        "Release pull request head SHA or ref to validate.",
        "pull_request_target",
        "github.event_name == 'workflow_dispatch'",
        "github.event.pull_request.user.login == 'github-actions[bot]'",
        "github.event.pull_request.head.repo.full_name == github.repository",
        "startsWith(github.event.pull_request.head.ref, 'release-please--branches--main--components--')",
        "ref: ${{ github.event_name == 'workflow_dispatch' && inputs.ref || github.event.pull_request.head.sha }}",
        "persist-credentials: false",
        "python -m nox -s lint",
        "python -m nox -s compile",
        "python -m nox -s build",
        "actions/upload-artifact",
    ):
        if required not in release_pr_text:
            raise SystemExit(f"release-pr-validation.yml is missing required validation fragment: {required}")
    if "cache: pip" in release_pr_text:
        raise SystemExit("release-pr-validation.yml must not use setup-python pip caching with a read-only token")

    release_path = ROOT / ".github" / "workflows" / "release.yml"
    release_text = release_path.read_text(encoding="utf-8")
    release = _read_workflow(release_path, yaml)
    jobs = _workflow_jobs(release, "release.yml")

    for required_job in ("resolve", "validate", "package", "binaries", "checksums", "publish"):
        if required_job not in jobs:
            raise SystemExit(f"release.yml missing required job: {required_job}")

    release_versions = jobs["validate"].get("strategy", {}).get("matrix", {}).get("python-version")
    if release_versions != expected_python_versions:
        raise SystemExit(f"release.yml validation matrix drifted: expected {expected_python_versions}, got {release_versions}")

    if "group: release-validation-${{ github.event.release.tag_name || inputs.tag || github.ref }}" not in release_text:
        raise SystemExit("release.yml must serialize validation by release tag")
    if "cancel-in-progress: false" not in release_text:
        raise SystemExit("release.yml must not cancel an in-flight release validation")
    prerelease_true = 'gh release edit "$TAG" --repo "$GITHUB_REPOSITORY" --prerelease=true'
    prerelease_false = 'gh release edit "$TAG" --repo "$GITHUB_REPOSITORY" --prerelease=false'
    if not _job_has_run_fragment(jobs["resolve"], prerelease_true):
        raise SystemExit("release.yml must mark releases prerelease before validation starts")
    if not _job_has_run_fragment(jobs["publish"], prerelease_false):
        raise SystemExit("release.yml must clear prerelease only after validation succeeds")
    for job_name, job in jobs.items():
        if job_name == "resolve":
            continue
        if _job_has_run_fragment(job, prerelease_true):
            raise SystemExit(f"release.yml must only mark releases prerelease in resolve job, found in {job_name}")
    for job_name, job in jobs.items():
        if job_name == "publish":
            continue
        if _job_has_run_fragment(job, prerelease_false):
            raise SystemExit(f"release.yml must only clear prerelease in publish job after validation succeeds, found in {job_name}")

    for job_name in ("package", "binaries"):
        needs = set(_as_list(jobs[job_name].get("needs")))
        if needs != {"resolve", "validate"}:
            raise SystemExit(f"release.yml {job_name} job must depend on resolve and validate")
    if set(_as_list(jobs["checksums"].get("needs"))) != {"resolve", "package", "binaries"}:
        raise SystemExit("release.yml checksums job must depend on resolve, package, and binaries")
    for required in ("Generate RELEASE-MANIFEST.json", "gh release upload \"$TAG\" release-assets/RELEASE-MANIFEST.json --repo \"$GITHUB_REPOSITORY\" --clobber"):
        if required not in release_text:
            raise SystemExit(f"release.yml is missing required release manifest fragment: {required}")
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps", []):
            if not isinstance(step, dict):
                continue
            run = step.get("run", "")
            if isinstance(run, str) and "python - <<'PY'" in run and step.get("shell") != "bash":
                step_name = step.get("name", "<unnamed>")
                raise SystemExit(f"release.yml {job_name}/{step_name} heredoc step must use shell: bash")

    publish_needs = set(_as_list(jobs["publish"].get("needs")))
    expected_publish_needs = {"resolve", "validate", "package", "binaries", "checksums"}
    if publish_needs != expected_publish_needs:
        missing = ", ".join(sorted(expected_publish_needs - publish_needs))
        extra = ", ".join(sorted(publish_needs - expected_publish_needs))
        raise SystemExit(f"release.yml publish job has wrong dependencies; missing={missing or '-'} extra={extra or '-'}")

    binary_matrix = jobs["binaries"].get("strategy", {}).get("matrix", {}).get("include", [])
    if not isinstance(binary_matrix, list):
        raise SystemExit("release.yml binaries job must use an explicit include matrix")
    assets = {entry.get("asset") for entry in binary_matrix if isinstance(entry, dict)}
    expected_assets = {
        "clifwrap-linux-amd64",
        "clifwrap-linux-arm64",
        "clifwrap-macos-amd64",
        "clifwrap-macos-arm64",
        "clifwrap-windows-amd64",
        "clifwrap-windows-arm64",
    }
    if assets != expected_assets:
        missing = ", ".join(sorted(expected_assets - assets))
        extra = ", ".join(sorted(asset for asset in assets - expected_assets if asset))
        raise SystemExit(f"release.yml binary assets have drifted; missing={missing or '-'} extra={extra or '-'}")

    pages_text = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    for required in ("python -m nox -s pages", "actions/upload-artifact", "github.event.repository.private == false", "path: site", "actions/deploy-pages"):
        if required not in pages_text:
            raise SystemExit(f"pages.yml is missing required Pages contract fragment: {required}")

    codeql_text = (ROOT / ".github" / "workflows" / "codeql.yml").read_text(encoding="utf-8")
    if "github.event.repository.private == false" not in codeql_text:
        raise SystemExit("codeql.yml must skip private repos that do not have code scanning enabled")

    dependency_review_text = (ROOT / ".github" / "workflows" / "dependency-review.yml").read_text(encoding="utf-8")
    if "github.event.repository.private == false" not in dependency_review_text:
        raise SystemExit("dependency-review.yml must skip private repos that do not have Advanced Security dependency review")

    release_please_text = (ROOT / ".github" / "workflows" / "release-please.yml").read_text(encoding="utf-8")
    release_please = _read_workflow(ROOT / ".github" / "workflows" / "release-please.yml", yaml)
    if "workflow_dispatch" not in release_please_text:
        raise SystemExit("release-please.yml must support manual reruns")
    release_please_permissions = release_please.get("permissions")
    if not isinstance(release_please_permissions, dict) or release_please_permissions.get("actions") != "write":
        raise SystemExit("release-please.yml must grant actions: write so created releases can dispatch validation")
    release_please_jobs = _workflow_jobs(release_please, "release-please.yml")
    release_please_steps = release_please_jobs.get("release-please", {}).get("steps", [])
    if not any(isinstance(step, dict) and step.get("id") == "release" and step.get("uses") == "googleapis/release-please-action@v5" for step in release_please_steps):
        raise SystemExit("release-please.yml must expose release-please outputs with step id 'release'")
    for required in (
        "if: ${{ steps.release.outputs.release_created }}",
        "TAG: ${{ steps.release.outputs.tag_name }}",
        "gh workflow run release-pr-validation.yml --repo \"$GITHUB_REPOSITORY\" --ref main -f ref=\"$head_sha\"",
        "gh release edit \"$TAG\" --repo \"$GITHUB_REPOSITORY\" --prerelease=true",
        "gh workflow run release.yml --repo \"$GITHUB_REPOSITORY\" --ref main -f tag=\"$TAG\"",
    ):
        if required not in release_please_text:
            raise SystemExit(f"release-please.yml is missing required release gate fragment: {required}")
    release_please_config = json.loads((ROOT / "release-please-config.json").read_text(encoding="utf-8"))
    package_config = release_please_config.get("packages", {}).get(".", {})
    extra_files = package_config.get("extra-files", [])
    if not any(isinstance(entry, dict) and entry.get("path") == "src/clifwrap/__init__.py" for entry in extra_files):
        raise SystemExit("release-please-config.json must update the frozen CLI version fallback")
    if "x-release-please-version" not in (ROOT / "src" / "clifwrap" / "__init__.py").read_text(encoding="utf-8"):
        raise SystemExit("src/clifwrap/__init__.py must mark the frozen CLI version for release-please")
    release_docs = (ROOT / "docs" / "release.md").read_text(encoding="utf-8")
    for required in (
        "Release Please workflow immediately gates that release through validation",
        "marks the created release as `prerelease`",
        "dispatches `release.yml` with the created tag",
        "releases created by a workflow token should not rely on follow-on release events",
    ):
        if required not in release_docs:
            raise SystemExit(f"docs/release.md is missing required Release Please gate documentation: {required}")

    print("workflow contracts ok")


def _read_workflow(path: Path, yaml_module: object) -> dict[str, object]:
    payload = yaml_module.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit(f"{path.name} is not a workflow mapping")
    return payload


def _workflow_jobs(payload: dict[str, object], name: str) -> dict[str, object]:
    jobs = payload.get("jobs")
    if not isinstance(jobs, dict):
        raise SystemExit(f"{name} is missing jobs")
    return jobs


def _job_has_run_fragment(job: object, fragment: str) -> bool:
    if not isinstance(job, dict):
        return False
    steps = job.get("steps")
    if not isinstance(steps, list):
        return False
    return any(isinstance(step, dict) and isinstance(step.get("run"), str) and fragment in step["run"] for step in steps)


def _as_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def actionlint_workflows(*, required: bool) -> None:
    executable = shutil.which("actionlint")
    if executable is None:
        message = "actionlint is not installed; skipping GitHub Actions semantic lint"
        if required:
            raise SystemExit(message)
        print(message)
        return
    run([executable, "-color"])


def inspect_archives() -> None:
    sdist = next(DIST.glob("clifwrap-*.tar.gz"))
    wheel = next(DIST.glob("clifwrap-*.whl"))
    required_sdist = {
        ".github/dependabot.yml",
        ".github/workflows/ci.yml",
        ".github/workflows/codeql.yml",
        ".github/workflows/dependency-review.yml",
        ".github/workflows/pages.yml",
        ".github/workflows/release-pr-validation.yml",
        ".github/workflows/release-please.yml",
        ".github/workflows/release.yml",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "MANIFEST.in",
        "README.md",
        "SECURITY.md",
        "docs/RESEARCH.md",
        "docs/cli-reference.md",
        "docs/configuration.md",
        "docs/index.md",
        "docs/migration.md",
        "docs/operations.md",
        "docs/plans/2026-06-29-001-feat-quota-aware-gating-plan.md",
        "docs/provider-catalog.md",
        "docs/release.md",
        "docs/schemas/release-manifest.v1.json",
        "noxfile.py",
        "packaging/pyinstaller/entrypoint.py",
        "scripts/build_pages.py",
        "scripts/firecrawl_login_sequence.py",
        "scripts/firecrawl_requested_accounts.py",
        "scripts/firecrawl_requested_accounts.toml",
        "scripts/generate_cli_reference.py",
        "scripts/generate_provider_catalog.py",
        "scripts/tavily_dynamic_research_loop.py",
        "scripts/verify_release.py",
        "src/clifwrap/py.typed",
        "tests/test_wrapper.py",
    }
    with tarfile.open(sdist) as archive:
        names = {name.split("/", 1)[1] for name in archive.getnames() if "/" in name}
    missing = sorted(required_sdist - names)
    if missing:
        raise SystemExit(f"{sdist.name} is missing required files: {', '.join(missing)}")
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = set(archive.namelist())
    for required in ("clifwrap/providers.toml", "clifwrap/py.typed"):
        if required not in wheel_names:
            raise SystemExit(f"{wheel.name} is missing {required}")
    print(f"archive ok: {sdist.name}")
    print(f"archive ok: {wheel.name}")


def checksums_smoke() -> None:
    artifacts = sorted(path for path in DIST.glob("clifwrap-*") if path.is_file())
    if not artifacts:
        raise SystemExit("No clifwrap release artifacts found for checksum smoke")
    checksum_path = DIST / "SHA256SUMS"
    lines = []
    for artifact in artifacts:
        digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
        lines.append(f"{digest}  {artifact.name}")
    checksum_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        digest, name = line.split(maxsplit=1)
        name = name.strip()
        if hashlib.sha256((DIST / name).read_bytes()).hexdigest() != digest:
            raise SystemExit(f"Checksum validation failed for {name}")
    print(f"checksums ok: {checksum_path.relative_to(ROOT)}")


def release_manifest_smoke() -> None:
    artifacts = sorted(path for path in DIST.glob("clifwrap-*") if path.is_file())
    checksum_path = DIST / "SHA256SUMS"
    if not artifacts:
        raise SystemExit("No clifwrap release artifacts found for release manifest")
    if not checksum_path.exists():
        raise SystemExit("SHA256SUMS must exist before RELEASE-MANIFEST.json is generated")

    checksum_entries = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        digest, name = line.split(maxsplit=1)
        checksum_entries[name.strip()] = digest

    manifest = {
        "schema": RELEASE_MANIFEST_SCHEMA_URL,
        "project": "clifwrap",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "artifacts": [
            {
                "name": artifact.name,
                "size": artifact.stat().st_size,
                "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
            }
            for artifact in artifacts
        ],
    }
    manifest_path = DIST / "RELEASE-MANIFEST.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    validate_release_manifest_schema(payload)
    seen_names = []
    for artifact in payload.get("artifacts", []):
        name = artifact.get("name")
        digest = artifact.get("sha256")
        size = artifact.get("size")
        if not isinstance(name, str) or not isinstance(digest, str) or not isinstance(size, int):
            raise SystemExit("RELEASE-MANIFEST.json contains a malformed artifact entry")
        path = DIST / name
        if not path.exists():
            raise SystemExit(f"RELEASE-MANIFEST.json references missing artifact {name}")
        if path.stat().st_size != size:
            raise SystemExit(f"RELEASE-MANIFEST.json size mismatch for {name}")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            raise SystemExit(f"RELEASE-MANIFEST.json digest mismatch for {name}")
        if checksum_entries.get(name) != digest:
            raise SystemExit(f"RELEASE-MANIFEST.json digest does not match SHA256SUMS for {name}")
        seen_names.append(name)
    expected_names = [artifact.name for artifact in artifacts]
    if seen_names != expected_names:
        raise SystemExit("RELEASE-MANIFEST.json artifact order or contents do not match dist artifacts")
    print(f"release manifest ok: {manifest_path.relative_to(ROOT) if manifest_path.is_relative_to(ROOT) else manifest_path}")


def validate_release_manifest_schema(payload: object) -> None:
    schema = json.loads(RELEASE_MANIFEST_SCHEMA.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("RELEASE-MANIFEST.json must be a JSON object")
    if payload.get("schema") != schema.get("$id") or payload.get("schema") != RELEASE_MANIFEST_SCHEMA_URL:
        raise SystemExit("RELEASE-MANIFEST.json schema URL does not match published schema")
    if payload.get("project") != "clifwrap":
        raise SystemExit("RELEASE-MANIFEST.json project must be clifwrap")
    if not isinstance(payload.get("generated_at"), str) or "T" not in payload["generated_at"]:
        raise SystemExit("RELEASE-MANIFEST.json generated_at must be an ISO-8601 UTC timestamp")
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise SystemExit("RELEASE-MANIFEST.json artifacts must be a non-empty array")
    allowed_keys = {"schema", "project", "tag", "generated_at", "artifacts"}
    extra = sorted(set(payload) - allowed_keys)
    if extra:
        raise SystemExit(f"RELEASE-MANIFEST.json has unknown top-level keys: {', '.join(extra)}")


def artifact_names() -> list[str]:
    return sorted(path.name for path in DIST.glob("*") if path.is_file()) if DIST.exists() else []


def wheel_smoke() -> None:
    wheel = next(DIST.glob("clifwrap-*.whl"))
    with tempfile.TemporaryDirectory(prefix="clifwrap-wheel-") as tmp:
        tmp_path = Path(tmp)
        venv = tmp_path / "venv"
        run([sys.executable, "-m", "venv", str(venv)])
        if sys.platform == "win32":
            python = venv / "Scripts" / "python.exe"
            clifwrap = venv / "Scripts" / "clifwrap.exe"
        else:
            python = venv / "bin" / "python"
            clifwrap = venv / "bin" / "clifwrap"
        run([str(python), "-m", "pip", "install", str(wheel)])
        assert_cli_version([str(clifwrap)])
        run([str(clifwrap), "sample-config"])
        run([str(clifwrap), "doctor", "--json", "--check"], env=isolated_runtime_env(tmp_path))


def pages_smoke() -> None:
    site = ROOT / "site"
    site.mkdir(exist_ok=True)
    pytest_report = site / "pytest.html"
    junit_report = site / "junit.xml"
    summary_report = site / "release-summary.json"
    if not pytest_report.exists():
        pytest_report.write_text("<html><body>pytest placeholder</body></html>", encoding="utf-8")
    if not junit_report.exists():
        junit_report.write_text("<testsuite tests='0'></testsuite>", encoding="utf-8")
    if not summary_report.exists():
        summary_report.write_text('{"success": true, "checks": ["pages-smoke"]}\n', encoding="utf-8")
    run([sys.executable, "scripts/build_pages.py", "--site", str(site)])
    required = [
        site / "index.html",
        site / "pytest.html",
        site / "junit.xml",
        site / "release-summary.json",
        site / "schemas" / "release-manifest.v1.json",
        site / "docs" / "configuration.html",
        site / "docs" / "release.html",
    ]
    missing = [str(path.relative_to(ROOT)) for path in required if not path.exists()]
    if missing:
        raise SystemExit(f"Pages build missing: {', '.join(missing)}")
    index = (site / "index.html").read_text(encoding="utf-8")
    if "release-summary.json" not in index:
        raise SystemExit("Pages build did not link release-summary.json")
    print("pages ok: site/index.html")


def pyinstaller_smoke() -> None:
    if shutil.which("pyinstaller") is None and not _module_available("PyInstaller"):
        raise SystemExit("PyInstaller is required for binary smoke; install with `python -m pip install -e '.[release]'` or pass --skip-pyinstaller.")
    run([sys.executable, "-m", "PyInstaller", "--onefile", "--name", "clifwrap", "--collect-data", "clifwrap", "packaging/pyinstaller/entrypoint.py"])
    binary = DIST / ("clifwrap.exe" if sys.platform == "win32" else "clifwrap")
    assert_cli_version([str(binary)])
    run([str(binary), "sample-config"])
    with tempfile.TemporaryDirectory(prefix="clifwrap-binary-") as tmp:
        run([str(binary), "doctor", "--json", "--check"], env=isolated_runtime_env(Path(tmp)))


def _module_available(name: str) -> bool:
    return subprocess.run([sys.executable, "-c", f"import {name}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def require_test_report_dependencies() -> None:
    if not _module_available("pytest_html"):
        raise SystemExit("pytest-html is required to generate Pages test reports; install with `python -m pip install -e '.[dev,release]'`.")


def write_summary(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"summary ok: {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run clifwrap release-quality local verification.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip pytest.")
    parser.add_argument("--skip-pyinstaller", action="store_true", help="Skip the local PyInstaller binary smoke test.")
    parser.add_argument("--require-actionlint", action="store_true", help="Fail when actionlint is not installed locally.")
    parser.add_argument("--keep-artifacts", action="store_true", help="Keep build/dist/cache outputs after verification.")
    parser.add_argument("--summary-json", help="Write a machine-readable verification summary to this path after all checks pass.")
    args = parser.parse_args()

    started_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    checks: list[str] = []
    clean_artifacts()
    parse_workflows()
    checks.append("workflow-parse")
    workflow_contracts()
    checks.append("workflow-contracts")
    actionlint_workflows(required=args.require_actionlint)
    checks.append("actionlint")
    run([sys.executable, "-m", "ruff", "check", "."])
    checks.append("ruff")
    if not args.skip_tests:
        require_test_report_dependencies()
        (ROOT / "site").mkdir(exist_ok=True)
        run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--junitxml=site/junit.xml",
                "--html=site/pytest.html",
                "--self-contained-html",
            ]
        )
        checks.append("pytest")
    run([sys.executable, "-m", "compileall", "src", "tests", "scripts", "packaging"])
    checks.append("compileall")
    run([sys.executable, "scripts/generate_cli_reference.py", "--check"])
    checks.append("cli-reference")
    run([sys.executable, "scripts/generate_provider_catalog.py", "--check"])
    checks.append("provider-catalog")
    run([sys.executable, "-m", "build"])
    checks.append("build")
    inspect_archives()
    checks.append("archive-inspection")
    wheel_smoke()
    checks.append("wheel-smoke")
    pages_smoke()
    checks.append("pages-smoke")
    if not args.skip_pyinstaller:
        pyinstaller_smoke()
        checks.append("pyinstaller-smoke")
    checksums_smoke()
    checks.append("checksums")
    release_manifest_smoke()
    checks.append("release-manifest")
    artifacts = artifact_names()
    if not args.keep_artifacts:
        clean_artifacts()
    if args.summary_json:
        write_summary(
            Path(args.summary_json),
            {
                "success": True,
                "started_at": started_at,
                "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "python": {
                    "executable": sys.executable,
                    "version": platform.python_version(),
                    "implementation": platform.python_implementation(),
                },
                "platform": {
                    "system": platform.system(),
                    "machine": platform.machine(),
                    "platform": platform.platform(),
                },
                "options": {
                    "skip_tests": args.skip_tests,
                    "skip_pyinstaller": args.skip_pyinstaller,
                    "require_actionlint": args.require_actionlint,
                    "keep_artifacts": args.keep_artifacts,
                },
                "checks": checks,
                "artifacts": artifacts,
            },
        )
    print("clifwrap release verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
