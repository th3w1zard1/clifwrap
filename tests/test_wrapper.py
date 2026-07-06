from __future__ import annotations

import os
import json
import importlib.util
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import tomllib
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
DYNAMIC_LOOP = ROOT / "scripts" / "tavily_dynamic_research_loop.py"
FIRECRAWL_BOOTSTRAP = ROOT / "scripts" / "firecrawl_requested_accounts.py"
FIRECRAWL_BOOTSTRAP_SPEC = ROOT / "scripts" / "firecrawl_requested_accounts.toml"
CLI_REFERENCE = ROOT / "docs" / "cli-reference.md"
PROVIDER_CATALOG = ROOT / "docs" / "provider-catalog.md"
RELEASE_MANIFEST_SCHEMA = ROOT / "docs" / "schemas" / "release-manifest.v1.json"


def make_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class WrapperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.home = Path(self.temp_dir.name) / "home"
        self.home.mkdir()
        self.bin_dir = self.home / ".local" / "bin"
        self.bin_dir.mkdir(parents=True)
        self.config_dir = self.home / ".config" / "clifwrap"
        self.config_dir.mkdir(parents=True)
        self.state_dir = self.home / ".local" / "state" / "clifwrap"
        self.state_dir.mkdir(parents=True)
        self.env = os.environ.copy()
        self.env["HOME"] = str(self.home)
        self.env["PATH"] = f"{self.bin_dir}:{self.env['PATH']}"
        self.env["PYTHONPATH"] = str(ROOT / "src")
        self.env["CLIFWRAP_CONFIG"] = str(self.config_dir / "config.toml")
        self.env["CLIFWRAP_STATE_DIR"] = str(self.state_dir)
        self.env["CLIFWRAP_BIN_DIR"] = str(self.bin_dir)
        make_executable(
            self.bin_dir / "clifwrap",
            f"#!/bin/sh\nPYTHONPATH='{ROOT / 'src'}' exec '{sys.executable}' -m clifwrap \"$@\"\n",
        )

    def _run(self, *args: str, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "clifwrap", *args],
            cwd=ROOT,
            env=self.env,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_provider_specific_literals_stay_out_of_generic_wrapper_modules(self) -> None:
        generic_modules = [
            ROOT / "src" / "clifwrap" / "__main__.py",
            ROOT / "src" / "clifwrap" / "accounts.py",
            ROOT / "src" / "clifwrap" / "config.py",
            ROOT / "src" / "clifwrap" / "runtime.py",
            ROOT / "src" / "clifwrap" / "scheduling.py",
            ROOT / "src" / "clifwrap" / "state.py",
        ]
        combined = "\n".join(path.read_text().lower() for path in generic_modules)
        for forbidden in ("tavily", "tvly", "firecrawl", "tavily_api_key", "firecrawl_api_key"):
            self.assertNotIn(forbidden, combined)

    def test_dynamic_tavily_loop_discovers_accounts_without_local_name_literals(self) -> None:
        spec = importlib.util.spec_from_file_location("tavily_dynamic_research_loop", DYNAMIC_LOOP)
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        output = textwrap.dedent(
            """\
            tvly: 3 configured account(s)
              alpha [enabled] TAVILY_API_KEY
            * beta [enabled] TAVILY_API_KEY
              gamma [disabled] TAVILY_API_KEY
            """
        )
        accounts = module.parse_accounts(output)
        self.assertEqual([account.name for account in accounts], ["alpha", "beta", "gamma"])
        self.assertEqual([account.enabled for account in accounts], [True, True, False])
        self.assertEqual([account.is_default for account in accounts], [False, True, False])
        source = DYNAMIC_LOOP.read_text()
        for forbidden in ("tavily-1", "tavily-2", "tavily-3"):
            self.assertNotIn(forbidden, source)

    def test_firecrawl_requested_account_bootstrap_avoids_account_specific_env_names(self) -> None:
        source = FIRECRAWL_BOOTSTRAP.read_text()
        spec = tomllib.loads(FIRECRAWL_BOOTSTRAP_SPEC.read_text())
        labels = [account["label"] for account in spec["accounts"]]
        self.assertEqual(labels, ["firecrawl-primary", "firecrawl-secondary", "firecrawl-tertiary", "firecrawl-quaternary"])
        spec_text = FIRECRAWL_BOOTSTRAP_SPEC.read_text()
        self.assertIsNone(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", spec_text))
        self.assertIsNone(re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", source))
        self.assertIn("env_name_template", spec)
        for account in spec["accounts"]:
            self.assertNotIn("env_names", account)
        self.assertNotIn("FIRECRAWL_API_KEY_BODEN", spec_text)
        self.assertNotIn("FIRECRAWL_API_KEY_HALOMASTAR", spec_text)
        self.assertNotIn("FIRECRAWL_API_KEY_NIMBUS", spec_text)
        self.assertIn('["clifwrap", "account", "import-spec", args.spec]', source)
        self.assertNotIn("https://api.firecrawl.dev", source)
        self.assertIsNone(re.search(r"fc-[A-Za-z0-9_-]{8,}", source))

    def test_install_and_uninstall_are_reversible(self) -> None:
        target = self.bin_dir / "tvly"
        make_executable(
            target,
            "#!/usr/bin/env python3\nimport sys\nprint('original tvly')\n",
        )
        install = self._run("install", "tvly")
        self.assertEqual(install.returncode, 0, install.stderr)
        self.assertIn("backup stored at", install.stdout)
        shim_text = target.read_text()
        self.assertIn("managed-by=clifwrap", shim_text)
        uninstall = self._run("uninstall", "tvly")
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
        self.assertIn("original tvly", target.read_text())

    def test_help_hides_internal_shim_command(self) -> None:
        help_result = self._run("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertNotIn("==SUPPRESS==", help_result.stdout)
        self.assertNotIn(",shim}", help_result.stdout)
        self.assertNotIn(" shim ", help_result.stdout)
        missing_env = self._run("shim")
        self.assertNotEqual(missing_env.returncode, 0)
        self.assertIn("missing shim app name", missing_env.stderr)

    def test_generated_cli_reference_is_current_and_public_only(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("generate_cli_reference", ROOT / "scripts" / "generate_cli_reference.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        rendered = module.generate()
        self.assertEqual(CLI_REFERENCE.read_text(), rendered)
        self.assertIn("## `clifwrap account add`", rendered)
        self.assertIn("## `clifwrap queue run`", rendered)
        self.assertNotIn("## `clifwrap shim`", rendered)
        self.assertNotIn("missing shim app name", rendered)

    def test_generated_provider_catalog_is_current(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("generate_provider_catalog", ROOT / "scripts" / "generate_provider_catalog.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        rendered = module.generate()
        self.assertEqual(PROVIDER_CATALOG.read_text(), rendered)
        self.assertIn("## `tvly`", rendered)
        self.assertIn("## `firecrawl`", rendered)
        self.assertIn("TAVILY_API_KEY", rendered)
        self.assertIn("FIRECRAWL_API_KEY", rendered)
        self.assertIn("Capacity Control", rendered)

    def test_release_verifier_requires_public_docs_workflows_and_scripts(self) -> None:
        source = (ROOT / "scripts" / "verify_release.py").read_text()
        for path in (
            ".github/workflows/ci.yml",
            ".github/workflows/codeql.yml",
            ".github/workflows/dependency-review.yml",
            ".github/workflows/pages.yml",
            ".github/workflows/release-pr-validation.yml",
            ".github/workflows/release-please.yml",
            ".github/workflows/release.yml",
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
            "scripts/build_pages.py",
            "scripts/firecrawl_login_sequence.py",
            "scripts/firecrawl_requested_accounts.py",
            "scripts/firecrawl_requested_accounts.toml",
            "scripts/generate_cli_reference.py",
            "scripts/generate_provider_catalog.py",
            "scripts/tavily_dynamic_research_loop.py",
            "scripts/verify_release.py",
        ):
            self.assertIn(path, source)
        self.assertIn("--summary-json", source)
        self.assertIn("release-summary.json", source)
        self.assertIn("Pages build did not link release-summary.json", source)
        self.assertIn("workflow_contracts()", source)
        self.assertIn("workflow-contracts", source)
        self.assertIn("ci.yml Python matrix drifted", source)
        self.assertIn("release-pr-validation.yml Python matrix drifted", source)
        self.assertIn("release-pr-validation.yml is missing required validation fragment", source)
        self.assertIn("release-pr-validation.yml must not use setup-python pip caching", source)
        self.assertIn("release.yml validation matrix drifted", source)
        self.assertIn("release.yml must serialize validation by release tag", source)
        self.assertIn("release.yml checksums job must depend on resolve, package, and binaries", source)
        self.assertIn("release_manifest_smoke()", source)
        self.assertIn("validate_release_manifest_schema(payload)", source)
        self.assertIn("release-manifest", source)
        self.assertIn("RELEASE-MANIFEST.json", source)
        self.assertIn("site / \"schemas\" / \"release-manifest.v1.json\"", source)
        self.assertIn("release.yml is missing required release manifest fragment", source)
        self.assertIn("heredoc step must use shell: bash", source)
        self.assertIn("codeql.yml must skip private repos", source)
        self.assertIn("dependency-review.yml must skip private repos", source)
        self.assertIn("release-please.yml must support manual reruns", source)
        self.assertIn("release-please-config.json must update the frozen CLI version fallback", source)
        self.assertIn("src/clifwrap/__init__.py must mark the frozen CLI version for release-please", source)
        self.assertIn('gh release edit \\"$TAG\\" --repo \\"$GITHUB_REPOSITORY\\" --prerelease=true', source)
        self.assertIn('prerelease_false = \'gh release edit "$TAG" --repo "$GITHUB_REPOSITORY" --prerelease=false\'', source)
        self.assertIn("must only clear prerelease in publish job after validation succeeds", source)
        self.assertIn("clifwrap-windows-arm64", source)
        self.assertIn("python -m nox -s pages", source)
        self.assertIn("persist-credentials: false", source)

    def test_release_workflow_contract_checker_passes_current_workflows(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("verify_release", ROOT / "scripts" / "verify_release.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module.workflow_contracts()

    def test_release_workflow_contract_checker_rejects_early_stable_release(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("verify_release", ROOT / "scripts" / "verify_release.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)

        repo = Path(self.temp_dir.name) / "release-contract-repo"
        shutil.copytree(ROOT / ".github", repo / ".github")
        shutil.copytree(ROOT / "docs", repo / "docs")
        shutil.copytree(ROOT / "src", repo / "src")
        shutil.copy2(ROOT / "release-please-config.json", repo / "release-please-config.json")
        release_workflow = repo / ".github" / "workflows" / "release.yml"
        release_text = release_workflow.read_text()
        release_text = release_text.replace(
            "      - name: Upload SHA256SUMS",
            '      - name: Incorrectly clear stable too early\n'
            '        run: gh release edit "$TAG" --repo "$GITHUB_REPOSITORY" --prerelease=false\n'
            "      - name: Upload SHA256SUMS",
        )
        release_workflow.write_text(release_text)

        with mock.patch.object(module, "ROOT", repo):
            with self.assertRaisesRegex(SystemExit, "must only clear prerelease in publish job"):
                module.workflow_contracts()

    def test_release_summary_writer_outputs_machine_readable_evidence(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("verify_release", ROOT / "scripts" / "verify_release.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        summary_path = Path(self.temp_dir.name) / "summary.json"
        module.write_summary(
            summary_path,
            {
                "success": True,
                "checks": ["workflow-parse", "ruff"],
                "artifacts": ["clifwrap-0.1.0.tar.gz"],
            },
        )
        payload = json.loads(summary_path.read_text())
        self.assertTrue(payload["success"])
        self.assertEqual(payload["checks"], ["workflow-parse", "ruff"])
        self.assertEqual(payload["artifacts"], ["clifwrap-0.1.0.tar.gz"])

    def test_release_manifest_smoke_records_artifact_digests(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("verify_release", ROOT / "scripts" / "verify_release.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        dist = Path(self.temp_dir.name) / "dist"
        dist.mkdir()
        artifact = dist / "clifwrap-0.1.0.tar.gz"
        artifact.write_bytes(b"release bytes")
        digest = module.hashlib.sha256(artifact.read_bytes()).hexdigest()
        (dist / "SHA256SUMS").write_text(f"{digest}  {artifact.name}\n", encoding="utf-8")

        with mock.patch.object(module, "DIST", dist):
            module.release_manifest_smoke()

        payload = json.loads((dist / "RELEASE-MANIFEST.json").read_text())
        self.assertEqual(payload["project"], "clifwrap")
        self.assertEqual(payload["artifacts"][0]["name"], artifact.name)
        self.assertEqual(payload["artifacts"][0]["sha256"], digest)
        self.assertEqual(payload["artifacts"][0]["size"], len(b"release bytes"))

    def test_release_manifest_schema_matches_manifest_url(self) -> None:
        schema = json.loads(RELEASE_MANIFEST_SCHEMA.read_text())
        self.assertEqual(schema["$id"], "https://th3w1zard1.github.io/clifwrap/schemas/release-manifest.v1.json")
        self.assertEqual(schema["properties"]["schema"]["const"], schema["$id"])
        self.assertEqual(schema["properties"]["project"]["const"], "clifwrap")
        self.assertIn("artifacts", schema["required"])

    def test_pages_builder_links_release_summary_when_present(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("build_pages", ROOT / "scripts" / "build_pages.py")
        self.assertIsNotNone(spec)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        site = Path(self.temp_dir.name) / "site"
        site.mkdir()
        (site / "pytest.html").write_text("<html>pytest</html>")
        (site / "junit.xml").write_text('<testsuite tests="3" failures="1" errors="0" skipped="1" />')
        (site / "release-summary.json").write_text('{"success": true}\n')
        module.build(site)
        index = (site / "index.html").read_text()
        self.assertIn("pytest.html", index)
        self.assertIn("junit.xml", index)
        self.assertIn("3 tests, 1 passed, 1 failures, 0 errors, 1 skipped (failing).", index)
        self.assertIn("release-summary.json", index)
        self.assertIn("Release Summary JSON", index)
        self.assertEqual((site / "schemas" / "release-manifest.v1.json").read_text(), RELEASE_MANIFEST_SCHEMA.read_text())

    def test_configuration_docs_cover_runtime_override_surface(self) -> None:
        docs = (ROOT / "docs" / "configuration.md").read_text()
        for token in (
            "CLIFWRAP_CONFIG",
            "CLIFWRAP_STATE_DIR",
            "CLIFWRAP_BIN_DIR",
            "CLIFWRAP_ACCOUNT",
            "CLIFWRAP_PROVIDER_<NAME>_AUTH_COMMAND",
            "CLIFWRAP_PROVIDER_<NAME>_AUTH_ALIASES",
            "CLIFWRAP_PROVIDER_<NAME>_PASSTHROUGH_COMMANDS",
            "CLIFWRAP_PROVIDER_<NAME>_USAGE_TIMEOUT_SECONDS",
            "CLIFWRAP_PROVIDER_<NAME>_CAPACITY_DEFAULT_ACTION",
            "CLIFWRAP_PROVIDER_<NAME>_CAPACITY_UNKNOWN_ACTION",
            "CLIFWRAP_PROVIDER_<NAME>_CAPACITY_RESERVE_THRESHOLD",
            "CLIFWRAP_PROVIDER_<NAME>_CAPACITY_DEFAULT_COST",
            "CLIFWRAP_PROVIDER_<NAME>_CAPACITY_COMMAND_COSTS",
            "CLIFWRAP_PROVIDER_<NAME>_QUEUE_RETENTION_SECONDS",
            "CLIFWRAP_PROVIDER_<NAME>_QUEUE_MAX_ITEMS",
            "CLIFWRAP_PROVIDER_<NAME>_CAPACITY_SNAPSHOT_TTL_SECONDS",
            "CLIFWRAP_PROVIDER_<NAME>_CAPACITY_REMEDIATION_MESSAGE",
            "CLIFWRAP_PROVIDER_<NAME>_CAPACITY_REMEDIATION_COMMANDS",
            "CLIFWRAP_PROVIDER_<NAME>_FALLBACK_THRESHOLD",
            "CLIFWRAP_PROVIDER_<NAME>_FALLBACK_ACTION",
            "CLIFWRAP_PROVIDER_<NAME>_FALLBACK_JOURNALD",
            "CLIFWRAP_PROVIDER_<NAME>_FALLBACK_SYSLOG",
            "CLIFWRAP_PROVIDER_<NAME>_FALLBACK_STDERR",
            "CLIFWRAP_PROVIDER_<NAME>_FALLBACK_RECOVERY_COMMAND",
            "CLIFWRAP_LOW_FALLBACK_MESSAGE",
            "env_command",
            "prepare_on",
            "status_command",
            "retry_patterns",
            "never_retry_patterns",
        ):
            self.assertIn(token, docs)

    def test_install_is_idempotent_and_does_not_double_wrap(self) -> None:
        target = self.bin_dir / "firecrawl"
        make_executable(
            target,
            "#!/usr/bin/env python3\nprint('original firecrawl')\n",
        )
        first = self._run("install", "firecrawl")
        self.assertEqual(first.returncode, 0, first.stderr)
        first_target = target.read_text()
        first_backup = (self.state_dir / "originals" / "firecrawl").read_text()
        second = self._run("install", "firecrawl")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(target.read_text(), first_target)
        self.assertEqual((self.state_dir / "originals" / "firecrawl").read_text(), first_backup)
        self.assertEqual(target.read_text().count("# managed-by=clifwrap"), 1)

    def test_uninstall_refuses_missing_backup_without_removing_shim(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(target, "#!/usr/bin/env python3\nprint('original')\n")
        install = self._run("install", "somecli")
        self.assertEqual(install.returncode, 0, install.stderr)
        backup = self.state_dir / "originals" / "somecli"
        backup.unlink()
        uninstall = self._run("uninstall", "somecli")
        self.assertNotEqual(uninstall.returncode, 0)
        self.assertIn("original backup is missing", uninstall.stderr)
        self.assertIn("managed-by=clifwrap", target.read_text())

    def test_uninstall_refuses_to_overwrite_unmanaged_target(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(target, "#!/usr/bin/env python3\nprint('original')\n")
        install = self._run("install", "somecli")
        self.assertEqual(install.returncode, 0, install.stderr)
        target.write_text("#!/usr/bin/env python3\nprint('replacement')\n")
        uninstall = self._run("uninstall", "somecli")
        self.assertNotEqual(uninstall.returncode, 0)
        self.assertIn("not a managed clifwrap shim", uninstall.stderr)
        self.assertIn("replacement", target.read_text())
        self.assertTrue((self.state_dir / "originals" / "somecli").exists())

    def test_install_shim_uses_current_module_not_external_clifwrap_on_path(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(
            target,
            "#!/usr/bin/env python3\nprint('original somecli')\n",
        )
        external_bin = Path(self.temp_dir.name) / "external-bin"
        external_bin.mkdir()
        make_executable(
            external_bin / "clifwrap",
            "#!/bin/sh\necho stale-clifwrap >&2\nexit 99\n",
        )
        env = self.env | {"PATH": f"{external_bin}:{self.env['PATH']}"}
        install = subprocess.run(
            [sys.executable, "-m", "clifwrap", "install", "somecli"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(install.returncode, 0, install.stderr)
        shim_text = target.read_text()
        self.assertIn(f'exec "{Path(sys.executable).resolve()}" -m clifwrap shim "$@"', shim_text)
        self.assertNotIn("stale-clifwrap", shim_text)

    def test_doctor_reports_paths_providers_shims_and_queue(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(target, "#!/usr/bin/env python3\nprint('original somecli')\n")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [[providers.somecli.accounts]]
                name = "primary"
                env = { SOMECLI_TOKEN = "env:SOMECLI_TOKEN_PRIMARY" }
                """
            )
        )
        install = self._run("install", "somecli")
        self.assertEqual(install.returncode, 0, install.stderr)
        doctor = self._run("doctor", "--json", "--check")
        self.assertEqual(doctor.returncode, 0, doctor.stderr)
        payload = json.loads(doctor.stdout)
        self.assertEqual(payload["paths"]["config"], str(self.config_dir / "config.toml"))
        self.assertEqual(payload["paths"]["state_dir"], str(self.state_dir))
        self.assertEqual(payload["paths"]["bin_dir"], str(self.bin_dir))
        self.assertEqual(payload["config"], {"exists": True, "valid": True, "error": None})
        self.assertEqual(payload["queue"], {"pending": 0, "expired": 0, "error": None})
        self.assertEqual(payload["issues"], [])
        self.assertEqual(len(payload["providers"]), 1)
        provider = payload["providers"][0]
        self.assertEqual(provider["name"], "somecli")
        self.assertEqual(provider["accounts"], 1)
        self.assertEqual(provider["enabled_accounts"], 1)
        self.assertTrue(provider["shim"]["recorded"])
        self.assertTrue(provider["shim"]["target_exists"])
        self.assertTrue(provider["shim"]["target_managed"])
        self.assertTrue(provider["shim"]["backup_exists"])

    def test_config_paths_reports_resolved_runtime_paths(self) -> None:
        paths = self._run("config", "paths", "--json")
        self.assertEqual(paths.returncode, 0, paths.stderr)
        payload = json.loads(paths.stdout)
        self.assertEqual(payload["config"], str(self.config_dir / "config.toml"))
        self.assertEqual(payload["state_dir"], str(self.state_dir))
        self.assertEqual(payload["bin_dir"], str(self.bin_dir))
        human = self._run("config", "paths")
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn(f"config: {self.config_dir / 'config.toml'}", human.stdout)
        self.assertIn(f"state: {self.state_dir}", human.stdout)
        self.assertIn(f"bin: {self.bin_dir}", human.stdout)

    def test_config_validate_reports_valid_summary_without_secrets(self) -> None:
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [[providers.somecli.accounts]]
                name = "primary"
                env = { SOMECLI_TOKEN = "secret-value" }

                [[providers.somecli.accounts]]
                name = "disabled"
                enabled = false
                """
            )
        )
        validate = self._run("config", "validate", "--json")
        self.assertEqual(validate.returncode, 0, validate.stderr)
        payload = json.loads(validate.stdout)
        self.assertTrue(payload["valid"])
        self.assertTrue(payload["exists"])
        self.assertEqual(payload["providers"], 1)
        self.assertEqual(payload["accounts"], 2)
        self.assertEqual(payload["enabled_accounts"], 1)
        self.assertNotIn("secret-value", validate.stdout)
        human = self._run("config", "validate")
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("valid: providers=1 accounts=2 enabled_accounts=1", human.stdout)

    def test_config_validate_fails_for_invalid_config(self) -> None:
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [[providers.somecli.accounts]]
                enabled = true
                """
            )
        )
        validate = self._run("config", "validate", "--json")
        self.assertEqual(validate.returncode, 1)
        payload = json.loads(validate.stdout)
        self.assertFalse(payload["valid"])
        self.assertIn("name must be a non-empty string", payload["error"])
        human = self._run("config", "validate")
        self.assertEqual(human.returncode, 1)
        self.assertIn("invalid:", human.stdout)

    def test_doctor_check_fails_for_broken_installed_shim_state(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(target, "#!/usr/bin/env python3\nprint('original somecli')\n")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [[providers.somecli.accounts]]
                name = "primary"
                env = { SOMECLI_TOKEN = "env:SOMECLI_TOKEN_PRIMARY" }
                """
            )
        )
        install = self._run("install", "somecli")
        self.assertEqual(install.returncode, 0, install.stderr)
        (self.state_dir / "originals" / "somecli").unlink()
        doctor = self._run("doctor", "--check")
        self.assertEqual(doctor.returncode, 1)
        self.assertIn("recorded original backup is missing", doctor.stdout)

    def test_low_fallback_monitor_reports_when_unused_fallbacks_drop_below_threshold(self) -> None:
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.fallback_monitor]
                threshold = 3
                syslog = false
                stderr = false

                [[providers.somecli.accounts]]
                name = "primary"

                [[providers.somecli.accounts]]
                name = "backup"
                """
            )
        )
        status = self._run("status", "somecli")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("somecli: 2 configured account(s)", status.stdout)
        self.assertIn("unused_fallbacks=1", status.stdout)
        self.assertIn("threshold=3", status.stdout)
        self.assertIn("remediation=add or enable accounts with `clifwrap account add somecli <name>`", status.stdout)

    def test_status_without_app_reports_all_configured_providers(self) -> None:
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.alpha.fallback_monitor]
                threshold = 3
                syslog = false
                stderr = false

                [[providers.alpha.accounts]]
                name = "primary"

                [[providers.beta.accounts]]
                name = "primary"
                """
            )
        )
        status = self._run("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("alpha: 1 configured account(s)", status.stdout)
        self.assertIn("[clifwrap] alpha low fallback pool", status.stdout)
        self.assertIn("beta: 1 configured account(s)", status.stdout)

    def test_status_without_app_handles_empty_config(self) -> None:
        (self.config_dir / "config.toml").write_text("version = 1\n")
        status = self._run("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(status.stdout.strip(), "no configured providers")

    def test_status_json_reports_low_fallback_state(self) -> None:
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.fallback_monitor]
                threshold = 3
                syslog = false
                stderr = false

                [[providers.somecli.accounts]]
                name = "primary"

                [[providers.somecli.accounts]]
                name = "backup"
                """
            )
        )
        status = self._run("status", "--json", "somecli")
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["provider"], "somecli")
        self.assertEqual(payload["configured_accounts"], 2)
        self.assertEqual(payload["low_fallback"]["unused_fallbacks"], 1)
        self.assertEqual(payload["low_fallback"]["threshold"], 3)
        self.assertEqual([account["name"] for account in payload["accounts"]], ["primary", "backup"])

    def test_status_json_without_app_reports_all_configured_providers(self) -> None:
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [[providers.alpha.accounts]]
                name = "primary"

                [[providers.beta.accounts]]
                name = "primary"
                """
            )
        )
        status = self._run("status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        payload = json.loads(status.stdout)
        self.assertEqual([provider["provider"] for provider in payload["providers"]], ["alpha", "beta"])

    def test_status_json_without_app_handles_empty_config(self) -> None:
        (self.config_dir / "config.toml").write_text("version = 1\n")
        status = self._run("status", "--json")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout), {"providers": []})

    def test_status_check_fails_when_low_fallback_exists(self) -> None:
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.fallback_monitor]
                threshold = 3
                syslog = false
                stderr = false

                [[providers.somecli.accounts]]
                name = "primary"
                """
            )
        )
        status = self._run("status", "--check", "somecli")
        self.assertEqual(status.returncode, 1)
        self.assertIn("low fallback pool", status.stdout)

    def test_status_check_passes_when_provider_is_healthy(self) -> None:
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.fallback_monitor]
                threshold = 1
                syslog = false
                stderr = false

                [[providers.somecli.accounts]]
                name = "primary"

                [[providers.somecli.accounts]]
                name = "backup"
                """
            )
        )
        status = self._run("status", "--check", "somecli")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertNotIn("low fallback pool", status.stdout)

    def test_status_json_check_keeps_valid_json_when_unhealthy(self) -> None:
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.fallback_monitor]
                threshold = 3
                syslog = false
                stderr = false

                [[providers.somecli.accounts]]
                name = "primary"
                """
            )
        )
        status = self._run("status", "--json", "--check", "somecli")
        self.assertEqual(status.returncode, 1)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["low_fallback"]["unused_fallbacks"], 0)

    def test_low_fallback_monitor_env_overrides_config_threshold(self) -> None:
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.fallback_monitor]
                threshold = 3
                syslog = false
                stderr = false

                [[providers.somecli.accounts]]
                name = "primary"

                [[providers.somecli.accounts]]
                name = "backup"
                """
            )
        )
        env = self.env.copy()
        env["CLIFWRAP_PROVIDER_SOMECLI_FALLBACK_THRESHOLD"] = "1"
        env["CLIFWRAP_PROVIDER_SOMECLI_FALLBACK_SYSLOG"] = "false"
        env["CLIFWRAP_PROVIDER_SOMECLI_FALLBACK_STDERR"] = "false"
        proc = subprocess.run(
            [sys.executable, "-m", "clifwrap", "status", "somecli"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("somecli: 2 configured account(s)", proc.stdout)
        self.assertNotIn("low fallback pool", proc.stdout)

    def test_low_fallback_monitor_runs_background_recovery_once_per_state(self) -> None:
        from clifwrap.config import load_config
        from clifwrap.runtime import _check_low_fallbacks, _provider_for

        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.fallback_monitor]
                threshold = 3
                syslog = false
                stderr = false
                recovery_command = ["some-recovery", "run"]

                [[providers.somecli.accounts]]
                name = "primary"

                [[providers.somecli.accounts]]
                name = "backup"
                """
            )
        )
        with mock.patch.dict(os.environ, self.env, clear=True):
            provider = _provider_for("somecli", load_config())
            with mock.patch("clifwrap.runtime.subprocess.Popen") as popen:
                popen.return_value = mock.Mock(pid=1234)
                with mock.patch("clifwrap.runtime._emit_syslog"):
                    with mock.patch("clifwrap.runtime._emit_stderr"):
                        _check_low_fallbacks(provider)
                        _check_low_fallbacks(provider)
        self.assertEqual(popen.call_count, 1)
        alert_state = json.loads((self.state_dir / "alerts" / "low-fallback" / "somecli.json").read_text())
        self.assertIn("signature", alert_state)

    def test_low_fallback_monitor_persists_recovery_launch_failure(self) -> None:
        from clifwrap.config import load_config
        from clifwrap.runtime import _check_low_fallbacks, _provider_for

        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.fallback_monitor]
                threshold = 3
                syslog = false
                stderr = false
                recovery_command = ["missing-recovery"]

                [[providers.somecli.accounts]]
                name = "primary"

                [[providers.somecli.accounts]]
                name = "backup"
                """
            )
        )
        with mock.patch.dict(os.environ, self.env, clear=True):
            provider = _provider_for("somecli", load_config())
            with mock.patch("clifwrap.runtime.subprocess.Popen", side_effect=OSError("boom")):
                with mock.patch("clifwrap.runtime._emit_stderr"):
                    _check_low_fallbacks(provider)
        alert_state = json.loads((self.state_dir / "alerts" / "low-fallback" / "somecli.json").read_text())
        self.assertEqual(alert_state["recovery_error"], "boom")
        status = self._run("status", "somecli")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("low-fallback recovery hook last failed", status.stdout)
        self.assertIn("boom", status.stdout)

    def test_low_fallback_monitor_successful_recovery_clears_previous_failure(self) -> None:
        from clifwrap.config import load_config
        from clifwrap.runtime import _check_low_fallbacks, _provider_for

        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.fallback_monitor]
                threshold = 3
                syslog = false
                stderr = false
                recovery_command = ["some-recovery", "run"]

                [[providers.somecli.accounts]]
                name = "primary"

                [[providers.somecli.accounts]]
                name = "backup"
                """
            )
        )
        alert_dir = self.state_dir / "alerts" / "low-fallback"
        alert_dir.mkdir(parents=True)
        (alert_dir / "somecli.json").write_text(json.dumps({"recovery_error": "old", "recovery_failed_at": "1"}) + "\n")
        with mock.patch.dict(os.environ, self.env, clear=True):
            provider = _provider_for("somecli", load_config())
            with mock.patch("clifwrap.runtime.subprocess.Popen") as popen:
                popen.return_value = mock.Mock(pid=1234)
                with mock.patch("clifwrap.runtime._emit_stderr"):
                    _check_low_fallbacks(provider)
        alert_state = json.loads((alert_dir / "somecli.json").read_text())
        self.assertNotIn("recovery_error", alert_state)
        self.assertNotIn("recovery_failed_at", alert_state)

    def test_low_fallback_monitor_can_emit_to_journald(self) -> None:
        from clifwrap.config import load_config
        from clifwrap.runtime import _check_low_fallbacks, _provider_for

        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.fallback_monitor]
                threshold = 3
                journald = true
                syslog = false
                stderr = false

                [[providers.somecli.accounts]]
                name = "primary"

                [[providers.somecli.accounts]]
                name = "backup"
                """
            )
        )
        with mock.patch.dict(os.environ, self.env, clear=True):
            provider = _provider_for("somecli", load_config())
            with mock.patch("clifwrap.runtime.shutil.which", return_value="/usr/bin/systemd-cat"):
                with mock.patch("clifwrap.runtime.subprocess.run") as run:
                    _check_low_fallbacks(provider)
        run.assert_called_once()
        self.assertIn("--identifier=clifwrap", run.call_args.args[0])
        self.assertIn("--priority=warning", run.call_args.args[0])
        self.assertIn("low fallback pool", run.call_args.kwargs["input"])

    def test_low_fallback_monitor_journald_env_override(self) -> None:
        from clifwrap.config import load_config
        from clifwrap.runtime import _check_low_fallbacks, _provider_for

        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.fallback_monitor]
                threshold = 3
                journald = true
                syslog = false
                stderr = false

                [[providers.somecli.accounts]]
                name = "primary"

                [[providers.somecli.accounts]]
                name = "backup"
                """
            )
        )
        env = self.env.copy()
        env["CLIFWRAP_PROVIDER_SOMECLI_FALLBACK_JOURNALD"] = "false"
        with mock.patch.dict(os.environ, env, clear=True):
            provider = _provider_for("somecli", load_config())
            with mock.patch("clifwrap.runtime.shutil.which", return_value="/usr/bin/systemd-cat"):
                with mock.patch("clifwrap.runtime.subprocess.run") as run:
                    _check_low_fallbacks(provider)
        run.assert_not_called()

    def test_low_fallback_monitor_fail_action_blocks_wrapped_command(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(target, "#!/usr/bin/env python3\nprint('should-not-run')\n")
        self._run("install", "somecli")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.fallback_monitor]
                threshold = 3
                action = "fail"
                syslog = false
                stderr = false

                [[providers.somecli.accounts]]
                name = "primary"

                [[providers.somecli.accounts]]
                name = "backup"
                """
            )
        )
        proc = subprocess.run(
            ["somecli", "run"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 75)
        self.assertEqual(proc.stdout, "")
        self.assertNotIn("should-not-run", proc.stdout)
        self.assertIn("low fallback pool", proc.stderr)

    def test_low_fallback_monitor_action_env_override_allows_wrapped_command(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(target, "#!/usr/bin/env python3\nprint('ran')\n")
        self._run("install", "somecli")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.fallback_monitor]
                threshold = 3
                action = "fail"
                syslog = false
                stderr = false

                [[providers.somecli.accounts]]
                name = "primary"

                [[providers.somecli.accounts]]
                name = "backup"
                """
            )
        )
        env = self.env.copy()
        env["CLIFWRAP_PROVIDER_SOMECLI_FALLBACK_ACTION"] = "warn"
        proc = subprocess.run(
            ["somecli", "run"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "ran")

    def test_failover_uses_second_account(self) -> None:
        target = self.bin_dir / "tvly"
        make_executable(
            target,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                key = os.environ.get("TAVILY_API_KEY")
                if key == "bad":
                    print("This request exceeds your plan's set usage limit.", file=sys.stderr)
                    raise SystemExit(3)
                print(f"ok:{key}:{' '.join(sys.argv[1:])}")
                """
            ),
        )
        self._run("install", "tvly")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.tvly]
                retry_exit_codes = [3]
                retry_patterns = ["usage limit"]

                [[providers.tvly.accounts]]
                name = "first"
                env = { TAVILY_API_KEY = "bad" }

                [[providers.tvly.accounts]]
                name = "second"
                env = { TAVILY_API_KEY = "good" }
                """
            )
        )
        proc = subprocess.run(
            ["tvly", "search", "query"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ok:good:search query", proc.stdout)
        self.assertIn("retrying with second", proc.stderr)
        defaults = json.loads((self.state_dir / "defaults.json").read_text())
        self.assertEqual(defaults["tvly"], "second")
        again = subprocess.run(
            ["tvly", "search", "query"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("ok:good:search query", again.stdout)
        self.assertNotIn("retrying with second", again.stderr)

    def test_account_env_selects_starting_account(self) -> None:
        target = self.bin_dir / "tvly"
        make_executable(
            target,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                print(os.environ.get("TAVILY_API_KEY"))
                """
            ),
        )
        self._run("install", "tvly")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [[providers.tvly.accounts]]
                name = "first"
                env = { TAVILY_API_KEY = "one" }

                [[providers.tvly.accounts]]
                name = "second"
                env = { TAVILY_API_KEY = "two" }
                """
            )
        )
        env = self.env.copy()
        env["CLIFWRAP_ACCOUNT"] = "second"
        proc = subprocess.run(
            ["tvly", "auth"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "two")

    def test_managed_tvly_auth_keeps_plain_auth_passthrough_and_sets_default(self) -> None:
        target = self.bin_dir / "tvly"
        make_executable(
            target,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                print(f"upstream:{' '.join(sys.argv[1:])}:{os.environ.get('TAVILY_API_KEY')}")
                """
            ),
        )
        self._run("install", "tvly")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [[providers.tvly.accounts]]
                name = "first"
                env = { TAVILY_API_KEY = "one" }

                [[providers.tvly.accounts]]
                name = "second"
                env = { TAVILY_API_KEY = "two" }
                """
            )
        )
        plain = subprocess.run(
            ["tvly", "auth"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(plain.stdout.strip(), "upstream:auth:one")
        listed = subprocess.run(
            ["tvly", "auth", "list"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("tvly: 2 configured account(s)", listed.stdout)
        used = subprocess.run(
            ["tvly", "auth", "use", "second"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(used.returncode, 0, used.stderr)
        self.assertIn("default account set to second", used.stdout)
        proc = subprocess.run(
            ["tvly", "search", "query"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "upstream:search query:two")

    def test_managed_firecrawl_login_uses_catalog(self) -> None:
        target = self.bin_dir / "firecrawl"
        make_executable(
            target,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                print(f"upstream:{' '.join(sys.argv[1:])}:{os.environ.get('FIRECRAWL_API_KEY')}")
                """
            ),
        )
        self._run("install", "firecrawl")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.firecrawl]
                passthrough_commands = ["login", "logout"]

                [[providers.firecrawl.accounts]]
                name = "primary"
                env = { FIRECRAWL_API_KEY = "fc-one" }

                [[providers.firecrawl.accounts]]
                name = "backup"
                env = { FIRECRAWL_API_KEY = "fc-two" }
                """
            )
        )
        plain = subprocess.run(
            ["firecrawl", "login"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(plain.stdout.strip(), "upstream:login:None")
        logged_out = subprocess.run(
            ["firecrawl", "logout"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(logged_out.returncode, 0, logged_out.stderr)
        self.assertEqual(logged_out.stdout.strip(), "upstream:logout:None")
        listed = subprocess.run(
            ["firecrawl", "login", "accounts"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("firecrawl: 2 configured account(s)", listed.stdout)
        used = subprocess.run(
            ["firecrawl", "login", "use", "backup"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(used.returncode, 0, used.stderr)
        proc = subprocess.run(
            ["firecrawl", "scrape", "https://example.com"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "upstream:scrape https://example.com:fc-two")
        renamed = subprocess.run(
            ["firecrawl", "login", "rename", "backup", "renamed-backup"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(renamed.returncode, 0, renamed.stderr)
        self.assertIn("account renamed: backup -> renamed-backup", renamed.stdout)
        default_after_rename = subprocess.run(
            ["firecrawl", "login", "default"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(default_after_rename.returncode, 0, default_after_rename.stderr)
        self.assertIn("default account: renamed-backup", default_after_rename.stdout)
        disabled = subprocess.run(
            ["firecrawl", "login", "disable", "renamed-backup"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(disabled.returncode, 0, disabled.stderr)
        self.assertIn("account disabled: renamed-backup", disabled.stdout)
        self.assertIn("default account set to primary", disabled.stdout)
        default_after_disable = subprocess.run(
            ["firecrawl", "login", "default"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(default_after_disable.returncode, 0, default_after_disable.stderr)
        self.assertIn("default account: primary", default_after_disable.stdout)
        reenabled = subprocess.run(
            ["firecrawl", "login", "enable", "renamed-backup"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(reenabled.returncode, 0, reenabled.stderr)
        self.assertIn("account enabled: renamed-backup", reenabled.stdout)
        removed = subprocess.run(
            ["firecrawl", "login", "remove", "renamed-backup"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertIn("account removed: renamed-backup", removed.stdout)
        self.assertIn("default account set to primary", removed.stdout)
        self.assertNotIn("name = \"renamed-backup\"", (self.config_dir / "config.toml").read_text())
        env_command = f"FIRECRAWL_API_KEY={sys.executable} -c \"print('fc-dynamic')\""
        added_dynamic = subprocess.run(
            ["firecrawl", "login", "add", "dynamic", "--env-command", env_command],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(added_dynamic.returncode, 0, added_dynamic.stderr)
        self.assertIn("account added: dynamic", added_dynamic.stdout)
        use_dynamic = subprocess.run(
            ["firecrawl", "login", "use", "dynamic"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(use_dynamic.returncode, 0, use_dynamic.stderr)
        dynamic_proc = subprocess.run(
            ["firecrawl", "scrape", "https://example.com"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(dynamic_proc.returncode, 0, dynamic_proc.stderr)
        self.assertEqual(dynamic_proc.stdout.strip(), "upstream:scrape https://example.com:fc-dynamic")

    def test_wrapper_only_auth_alias_entrypoint_is_configurable(self) -> None:
        target = self.bin_dir / "firecrawl"
        make_executable(
            target,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                print(f"upstream:{' '.join(sys.argv[1:])}:{os.environ.get('FIRECRAWL_API_KEY')}")
                """
            ),
        )
        self._run("install", "firecrawl")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.firecrawl]
                passthrough_commands = ["login", "logout"]

                [providers.firecrawl.auth_management]
                command = "login"
                aliases = ["accounts", "auths"]

                [[providers.firecrawl.accounts]]
                name = "primary"
                env = { FIRECRAWL_API_KEY = "fc-one" }
                """
            )
        )
        listed = subprocess.run(
            ["firecrawl", "accounts", "list"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("firecrawl: 1 configured account(s)", listed.stdout)
        bare_alias = subprocess.run(
            ["firecrawl", "accounts"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(bare_alias.returncode, 0, bare_alias.stderr)
        self.assertIn("firecrawl: 1 configured account(s)", bare_alias.stdout)
        native_login = subprocess.run(
            ["firecrawl", "login"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(native_login.returncode, 0, native_login.stderr)
        self.assertEqual(native_login.stdout.strip(), "upstream:login:None")
        default = subprocess.run(
            ["firecrawl", "auths", "default"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertIn("no default account", default.stdout)
        used = subprocess.run(
            ["firecrawl", "accounts", "use", "primary"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(used.returncode, 0, used.stderr)
        self.assertIn("default account set to primary", used.stdout)
        renamed = subprocess.run(
            ["firecrawl", "auths", "rename", "primary", "main"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(renamed.returncode, 0, renamed.stderr)
        self.assertIn("account renamed: primary -> main", renamed.stdout)

    def test_env_var_overrides_auth_aliases_and_passthrough_commands(self) -> None:
        target = self.bin_dir / "firecrawl"
        make_executable(
            target,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                print(f"upstream:{' '.join(sys.argv[1:])}:{os.environ.get('FIRECRAWL_API_KEY')}")
                """
            ),
        )
        self._run("install", "firecrawl")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.firecrawl]

                [providers.firecrawl.auth_management]
                command = "login"
                aliases = ["accounts"]

                [[providers.firecrawl.accounts]]
                name = "primary"
                env = { FIRECRAWL_API_KEY = "fc-one" }
                """
            )
        )
        env = self.env.copy()
        env["CLIFWRAP_PROVIDER_FIRECRAWL_AUTH_ALIASES"] = "logins, credentials"
        env["CLIFWRAP_PROVIDER_FIRECRAWL_PASSTHROUGH_COMMANDS"] = "login,logout"
        listed = subprocess.run(
            ["firecrawl", "logins", "list"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("firecrawl: 1 configured account(s)", listed.stdout)
        plain = subprocess.run(
            ["firecrawl", "login"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(plain.stdout.strip(), "upstream:login:None")

    def test_missing_account_env_ref_fails_over_to_next_account(self) -> None:
        target = self.bin_dir / "firecrawl"
        make_executable(
            target,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                print(f"ok:{os.environ.get('FIRECRAWL_API_KEY')}:{' '.join(sys.argv[1:])}")
                """
            ),
        )
        self._run("install", "firecrawl")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [[providers.firecrawl.accounts]]
                name = "missing"
                env = { FIRECRAWL_API_KEY = "env:FIRECRAWL_MISSING" }

                [[providers.firecrawl.accounts]]
                name = "present"
                env = { FIRECRAWL_API_KEY = "fc-present" }
                """
            )
        )
        proc = subprocess.run(
            ["firecrawl", "scrape", "https://example.com"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "ok:fc-present:scrape https://example.com")
        self.assertIn("missing failed (account configuration error); retrying with present", proc.stderr)
        defaults = json.loads((self.state_dir / "defaults.json").read_text())
        self.assertEqual(defaults["firecrawl"], "present")

    def test_tvly_auth_alias_entrypoints_are_configurable(self) -> None:
        target = self.bin_dir / "tvly"
        make_executable(
            target,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                print(f"upstream:{' '.join(sys.argv[1:])}:{os.environ.get('TAVILY_API_KEY')}")
                """
            ),
        )
        self._run("install", "tvly")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.tvly]
                passthrough_commands = ["auth"]

                [providers.tvly.auth_management]
                command = "auth"
                aliases = ["accounts", "logins"]

                [[providers.tvly.accounts]]
                name = "primary"
                env = { TAVILY_API_KEY = "tvly-one" }
                """
            )
        )
        listed = subprocess.run(
            ["tvly", "accounts", "list"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("tvly: 1 configured account(s)", listed.stdout)
        default = subprocess.run(
            ["tvly", "logins", "default"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertIn("no default account", default.stdout)
        used = subprocess.run(
            ["tvly", "accounts", "use", "primary"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(used.returncode, 0, used.stderr)
        self.assertIn("default account set to primary", used.stdout)
        proc = subprocess.run(
            ["tvly", "search", "query"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("upstream:search query:tvly-one", proc.stdout)

    def test_tvly_env_overrides_auth_aliases_and_passthrough(self) -> None:
        target = self.bin_dir / "tvly"
        make_executable(
            target,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                print(f"upstream:{' '.join(sys.argv[1:])}:{os.environ.get('TAVILY_API_KEY')}")
                """
            ),
        )
        self._run("install", "tvly")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.tvly]

                [providers.tvly.auth_management]
                command = "auth"
                aliases = ["accounts"]

                [[providers.tvly.accounts]]
                name = "primary"
                env = { TAVILY_API_KEY = "tvly-one" }
                """
            )
        )
        env = self.env.copy()
        env["CLIFWRAP_PROVIDER_TVLY_AUTH_ALIASES"] = "logins, credentials"
        env["CLIFWRAP_PROVIDER_TVLY_PASSTHROUGH_COMMANDS"] = "auth,whoami"
        listed = subprocess.run(
            ["tvly", "logins", "list"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("tvly: 1 configured account(s)", listed.stdout)
        plain = subprocess.run(
            ["tvly", "auth"],
            cwd=ROOT,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(plain.returncode, 0, plain.stderr)
        self.assertEqual(plain.stdout.strip(), "upstream:auth:None")

    def test_tvly_catalog_built_in_logins_alias_is_wrapper_managed(self) -> None:
        target = self.bin_dir / "tvly"
        make_executable(
            target,
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                print(f"upstream:{' '.join(sys.argv[1:])}:{os.environ.get('TAVILY_API_KEY')}")
                """
            ),
        )
        self._run("install", "tvly")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [[providers.tvly.accounts]]
                name = "primary"
                env = { TAVILY_API_KEY = "tvly-one" }

                [[providers.tvly.accounts]]
                name = "backup"
                env = { TAVILY_API_KEY = "tvly-two" }
                """
            )
        )
        used = subprocess.run(
            ["tvly", "logins", "use", "backup"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(used.returncode, 0, used.stderr)
        self.assertIn("default account set to backup", used.stdout)
        listed = subprocess.run(
            ["tvly", "logins"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("tvly: 2 configured account(s)", listed.stdout)
        self.assertIn("* backup [enabled] TAVILY_API_KEY", listed.stdout)
        default = subprocess.run(
            ["tvly", "credentials", "default"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertIn("default account: backup", default.stdout)

    def test_passthrough_without_accounts(self) -> None:
        target = self.bin_dir / "firecrawl"
        make_executable(
            target,
            "#!/usr/bin/env python3\nimport sys\nprint(sys.argv[0])\nprint('firecrawl:' + ' '.join(sys.argv[1:]))\n",
        )
        self._run("install", "firecrawl")
        proc = subprocess.run(
            ["firecrawl", "search", "docs"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(Path(proc.stdout.splitlines()[0]).name, "firecrawl")
        self.assertIn("firecrawl:search docs", proc.stdout)

    def test_no_arg_tavily_passthrough_without_accounts(self) -> None:
        target = self.bin_dir / "tvly"
        make_executable(
            target,
            "#!/usr/bin/env python3\nimport sys\nprint('original-repl')\n",
        )
        self._run("install", "tvly")
        proc = subprocess.run(
            ["tvly"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "original-repl")

    def test_install_honors_configured_bin_dir(self) -> None:
        other_bin = Path(self.temp_dir.name) / "other-bin"
        other_bin.mkdir()
        target = other_bin / "somecli"
        make_executable(target, "#!/usr/bin/env python3\nprint('somecli')\n")
        self.env["PATH"] = f"{other_bin}:{self.env['PATH']}"
        self.env["CLIFWRAP_BIN_DIR"] = str(other_bin)
        install = self._run("install", "somecli")
        self.assertEqual(install.returncode, 0, install.stderr)
        self.assertIn(str(other_bin / "somecli"), install.stdout)
        self.assertIn("managed-by=clifwrap", target.read_text())

    def test_install_refuses_to_overwrite_unmanaged_command_when_backup_exists(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(target, "#!/usr/bin/env python3\nprint('current')\n")
        backup_dir = self.state_dir / "originals"
        backup_dir.mkdir(parents=True)
        make_executable(backup_dir / "somecli", "#!/usr/bin/env python3\nprint('backup')\n")
        install = self._run("install", "somecli")
        self.assertNotEqual(install.returncode, 0)
        self.assertIn("Refusing to install somecli", install.stderr)
        self.assertIn("print('current')", target.read_text())

    def test_relative_symlink_command_stays_executable_after_install(self) -> None:
        package_dir = self.home / "packages" / "somecli"
        package_dir.mkdir(parents=True)
        real_target = package_dir / "index.py"
        make_executable(real_target, "#!/usr/bin/env python3\nprint('relative-link-ok')\n")
        symlink = self.bin_dir / "somecli"
        symlink.symlink_to(Path("../../packages/somecli/index.py"))
        install = self._run("install", "somecli")
        self.assertEqual(install.returncode, 0, install.stderr)
        proc = subprocess.run(
            ["somecli"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "relative-link-ok")

    def test_install_and_uninstall_without_args_use_config_and_state(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(target, "#!/usr/bin/env python3\nprint('somecli')\n")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli]
                retry_patterns = ["quota"]
                """
            )
        )
        install = self._run("install")
        self.assertEqual(install.returncode, 0, install.stderr)
        self.assertIn("somecli: installed shim", install.stdout)
        self.assertIn("managed-by=clifwrap", target.read_text())
        uninstall = self._run("uninstall")
        self.assertEqual(uninstall.returncode, 0, uninstall.stderr)
        self.assertIn("somecli: restored original command", uninstall.stdout)
        self.assertIn("print('somecli')", target.read_text())

    def test_prepare_command_once_is_idempotent(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(target, "#!/usr/bin/env python3\nprint('ok')\n")
        counter = Path(self.temp_dir.name) / "prepare-count.txt"
        script = (
            "from pathlib import Path; "
            "p=Path(__import__('os').environ['COUNT_FILE']); "
            "n=int(p.read_text()) if p.exists() else 0; "
            "p.write_text(str(n+1))"
        )
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                f"""\
                [providers.somecli]

                [[providers.somecli.accounts]]
                name = "first"
                prepare_on = "once"
                env = {{ COUNT_FILE = "{counter}" }}
                prepare_command = ["{sys.executable}", "-c", "{script}"]
                """
            )
        )
        self._run("install", "somecli")
        for _ in range(2):
            proc = subprocess.run(
                ["somecli"],
                cwd=ROOT,
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(counter.read_text(), "1")

    def test_generic_status_command_reports_total_remaining(self) -> None:
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                f"""\
                [providers.somecli]
                status_command = ["{sys.executable}", "-c", "import os,json; print(json.dumps({{'remaining': int(os.environ['LEFT']), 'limit': 50, 'used': 50-int(os.environ['LEFT'])}}))"]

                [[providers.somecli.accounts]]
                name = "first"
                env = {{ LEFT = "12" }}

                [[providers.somecli.accounts]]
                name = "second"
                env = {{ LEFT = "38" }}
                """
            )
        )
        status = self._run("status", "somecli")
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("- first: 38/50 used, remaining 12", status.stdout)
        self.assertIn("- second: 12/50 used, remaining 38", status.stdout)
        self.assertIn("total remaining across available accounts: 50", status.stdout)

    def test_tavily_status_uses_account_plan_when_key_limit_is_null(self) -> None:
        from clifwrap.runtime import status_for

        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [[providers.tvly.accounts]]
                name = "first"
                env = { TAVILY_API_KEY = "tvly-test" }
                """
            )
        )
        payload = {
            "key": {"usage": 1000, "limit": None},
            "account": {"plan_usage": 250, "plan_limit": 1000},
        }
        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch("clifwrap.runtime._http_json", return_value=payload):
                output = StringIO()
                with redirect_stdout(output):
                    status = status_for("tvly")
        self.assertEqual(status, 0)
        self.assertIn("- first: plan 250/1000 used, remaining 750", output.getvalue())
        self.assertIn("total remaining across available accounts: 750", output.getvalue())

    def test_usage_status_uses_configured_timeout(self) -> None:
        from clifwrap.runtime import status_for

        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.usage]
                url = "https://example.test/usage"
                auth_env = "SOMECLI_TOKEN"
                remaining_path = "remaining"
                timeout_seconds = 2.5

                [[providers.somecli.accounts]]
                name = "first"
                env = { SOMECLI_TOKEN = "token" }
                """
            )
        )
        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch("clifwrap.runtime._http_json", return_value={"remaining": 7}) as http_json:
                output = StringIO()
                with redirect_stdout(output):
                    status = status_for("somecli")
        self.assertEqual(status, 0)
        self.assertEqual(http_json.call_args.kwargs["timeout"], 2.5)
        self.assertIn("- first: usage remaining 7", output.getvalue())

    def test_usage_status_timeout_env_override(self) -> None:
        from clifwrap.runtime import status_for

        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.usage]
                url = "https://example.test/usage"
                auth_env = "SOMECLI_TOKEN"
                remaining_path = "remaining"
                timeout_seconds = 10

                [[providers.somecli.accounts]]
                name = "first"
                env = { SOMECLI_TOKEN = "token" }
                """
            )
        )
        env = self.env.copy()
        env["CLIFWRAP_PROVIDER_SOMECLI_USAGE_TIMEOUT_SECONDS"] = "1.25"
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("clifwrap.runtime._http_json", return_value={"remaining": 7}) as http_json:
                output = StringIO()
                with redirect_stdout(output):
                    status = status_for("somecli")
        self.assertEqual(status, 0)
        self.assertEqual(http_json.call_args.kwargs["timeout"], 1.25)
        self.assertIn("- first: usage remaining 7", output.getvalue())

    def test_account_add_and_list_manage_config(self) -> None:
        init = self._run("init")
        self.assertEqual(init.returncode, 0, init.stderr)
        secrets = Path(self.temp_dir.name) / "secrets.env"
        secrets.write_text('TAVILY_PRIMARY="secret-value"\n')
        add = self._run(
            "account",
            "add",
            "tvly",
            "primary",
            "--env-file",
            str(secrets),
            "--env-ref",
            "TAVILY_API_KEY=TAVILY_PRIMARY",
        )
        self.assertEqual(add.returncode, 0, add.stderr)
        add_backup = self._run("account", "add", "tvly", "backup", "--env-ref", "TAVILY_API_KEY=TAVILY_BACKUP")
        self.assertEqual(add_backup.returncode, 0, add_backup.stderr)
        config_text = (self.config_dir / "config.toml").read_text()
        self.assertIn("[[providers.tvly.accounts]]", config_text)
        self.assertIn(f'env_files = ["{secrets}"]', config_text)
        self.assertIn('env = { TAVILY_API_KEY = "env:TAVILY_PRIMARY" }', config_text)
        self.assertNotIn("secret-value", config_text)
        listed = self._run("account", "list", "tvly")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("tvly:primary [enabled] TAVILY_API_KEY", listed.stdout)
        use = self._run("account", "use", "tvly", "primary")
        self.assertEqual(use.returncode, 0, use.stderr)
        self.assertIn("default account set to primary", use.stdout)
        default = self._run("account", "default", "tvly")
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertIn("default account: primary", default.stdout)
        rename = self._run("account", "rename", "tvly", "primary", "renamed-primary")
        self.assertEqual(rename.returncode, 0, rename.stderr)
        self.assertIn("account renamed: primary -> renamed-primary", rename.stdout)
        renamed_default = self._run("account", "default", "tvly")
        self.assertEqual(renamed_default.returncode, 0, renamed_default.stderr)
        self.assertIn("default account: renamed-primary", renamed_default.stdout)
        disable = self._run("account", "disable", "tvly", "renamed-primary")
        self.assertEqual(disable.returncode, 0, disable.stderr)
        self.assertIn("account disabled: renamed-primary", disable.stdout)
        self.assertIn("default account set to backup", disable.stdout)
        default_after_disable = self._run("account", "default", "tvly")
        self.assertEqual(default_after_disable.returncode, 0, default_after_disable.stderr)
        self.assertIn("default account: backup", default_after_disable.stdout)
        disabled_list = self._run("account", "list", "tvly")
        self.assertIn("tvly:renamed-primary [disabled] TAVILY_API_KEY", disabled_list.stdout)
        json_list = self._run("account", "list", "tvly", "--json")
        self.assertEqual(json_list.returncode, 0, json_list.stderr)
        payload = json.loads(json_list.stdout)
        self.assertEqual(
            payload,
            {
                "accounts": [
                    {
                        "default": False,
                        "enabled": False,
                        "env_command_keys": [],
                        "env_files": [str(secrets)],
                        "env_keys": ["TAVILY_API_KEY"],
                        "has_prepare_command": False,
                        "name": "renamed-primary",
                        "prepare_on": "always",
                        "provider": "tvly",
                    },
                    {
                        "default": True,
                        "enabled": True,
                        "env_command_keys": [],
                        "env_files": [],
                        "env_keys": ["TAVILY_API_KEY"],
                        "has_prepare_command": False,
                        "name": "backup",
                        "prepare_on": "always",
                        "provider": "tvly",
                    },
                ]
            },
        )
        self.assertNotIn("secret-value", json_list.stdout)
        enable = self._run("account", "enable", "tvly", "renamed-primary")
        self.assertEqual(enable.returncode, 0, enable.stderr)
        remove_backup = self._run("account", "remove", "tvly", "backup")
        self.assertEqual(remove_backup.returncode, 0, remove_backup.stderr)
        self.assertIn("default account set to renamed-primary", remove_backup.stdout)
        remove = self._run("account", "remove", "tvly", "renamed-primary")
        self.assertEqual(remove.returncode, 0, remove.stderr)
        self.assertIn("default account cleared", remove.stdout)
        final_default = self._run("account", "default", "tvly")
        self.assertEqual(final_default.returncode, 0, final_default.stderr)
        self.assertIn("no default account", final_default.stdout)
        self.assertNotIn("[[providers.tvly.accounts]]", (self.config_dir / "config.toml").read_text())

    def test_account_add_rejects_duplicates(self) -> None:
        first = self._run("account", "add", "firecrawl", "work", "--env-ref", "FIRECRAWL_API_KEY=FC_WORK")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self._run("account", "add", "firecrawl", "work", "--env-ref", "FIRECRAWL_API_KEY=FC_OTHER")
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already exists", second.stderr)

    def test_account_add_quotes_non_bare_provider_names(self) -> None:
        added = self._run("account", "add", "some.cli", "primary", "--env-ref", "SOMECLI_TOKEN=SOMECLI_TOKEN")
        self.assertEqual(added.returncode, 0, added.stderr)
        config_text = (self.config_dir / "config.toml").read_text()
        self.assertIn('[providers."some.cli"]', config_text)
        self.assertIn('[[providers."some.cli".accounts]]', config_text)
        listed = self._run("account", "list", "some.cli")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("some.cli:primary [enabled] SOMECLI_TOKEN", listed.stdout)

    def test_account_add_supports_env_command(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(target, "#!/usr/bin/env python3\nimport os\nprint(os.environ.get('SOMECLI_TOKEN'))\n")
        self._run("install", "somecli")
        command_assignment = f"SOMECLI_TOKEN={sys.executable} -c \"print('dynamic-token')\""
        added = self._run("account", "add", "somecli", "dynamic", "--env-command", command_assignment)
        self.assertEqual(added.returncode, 0, added.stderr)
        self.assertIn("account added: somecli:dynamic", added.stdout)
        listed = self._run("account", "list", "somecli")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("somecli:dynamic [enabled] SOMECLI_TOKEN", listed.stdout)
        config_text = (self.config_dir / "config.toml").read_text()
        self.assertIn("env_command", config_text)
        proc = subprocess.run(
            ["somecli"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "dynamic-token")

    def test_account_import_spec_is_declarative_and_defaults_first_available_account(self) -> None:
        init = self._run("init")
        self.assertEqual(init.returncode, 0, init.stderr)
        secrets = Path(self.temp_dir.name) / "secrets.env"
        secrets.write_text('TOKEN_TWO="secret-value"\n')
        spec = Path(self.temp_dir.name) / "accounts.toml"
        spec.write_text(
            textwrap.dedent(
                f"""\
                provider = "somecli"
                target_env = "SOMECLI_TOKEN"
                env_file = "{secrets}"
                set_default = true

                [[accounts]]
                label = "first"
                env_names = ["TOKEN_ONE"]

                [[accounts]]
                label = "second"
                env_names = ["TOKEN_TWO"]
                """
            )
        )
        dry_run = self._run("account", "import-spec", str(spec))
        self.assertEqual(dry_run.returncode, 2, dry_run.stderr)
        self.assertIn("first: missing: expected one of TOKEN_ONE", dry_run.stdout)
        self.assertIn("second: would-add: SOMECLI_TOKEN=env:TOKEN_TWO", dry_run.stdout)
        self.assertNotIn("[[providers.somecli.accounts]]", (self.config_dir / "config.toml").read_text())

        applied = self._run("account", "import-spec", str(spec), "--apply")
        self.assertEqual(applied.returncode, 2, applied.stderr)
        self.assertIn("second: added: SOMECLI_TOKEN=env:TOKEN_TWO", applied.stdout)
        self.assertIn("second: default: somecli default account", applied.stdout)
        config_text = (self.config_dir / "config.toml").read_text()
        self.assertIn("[[providers.somecli.accounts]]", config_text)
        self.assertIn('name = "second"', config_text)
        self.assertIn(f'env_files = ["{secrets}"]', config_text)
        self.assertIn('env = { SOMECLI_TOKEN = "env:TOKEN_TWO" }', config_text)
        default = self._run("account", "default", "somecli")
        self.assertEqual(default.returncode, 0, default.stderr)
        self.assertIn("default account: second", default.stdout)

    def test_account_import_spec_can_derive_env_names_from_template(self) -> None:
        init = self._run("init")
        self.assertEqual(init.returncode, 0, init.stderr)
        secrets = Path(self.temp_dir.name) / "secrets.env"
        secrets.write_text('CLIFWRAP_SOMECLI_TEAM_ALPHA_SOMECLI_TOKEN="secret-value"\n')
        spec = Path(self.temp_dir.name) / "accounts.toml"
        spec.write_text(
            textwrap.dedent(
                f"""\
                provider = "somecli"
                target_env = "SOMECLI_TOKEN"
                env_file = "{secrets}"
                env_name_template = "CLIFWRAP_{{provider_slug}}_{{account_slug}}_{{target_env_slug}}"
                set_default = true

                [[accounts]]
                label = "team alpha"

                [[accounts]]
                label = "team beta"
                """
            )
        )
        dry_run = self._run("account", "import-spec", str(spec))
        self.assertEqual(dry_run.returncode, 2, dry_run.stderr)
        self.assertIn("team alpha: would-add: SOMECLI_TOKEN=env:CLIFWRAP_SOMECLI_TEAM_ALPHA_SOMECLI_TOKEN", dry_run.stdout)
        self.assertIn("team beta: missing: no secret source found for this account", dry_run.stdout)
        self.assertNotIn("expected one of", dry_run.stdout)

    def test_account_import_spec_reconciles_existing_accounts(self) -> None:
        init = self._run("init")
        self.assertEqual(init.returncode, 0, init.stderr)
        secrets = Path(self.temp_dir.name) / "secrets.env"
        secrets.write_text('TOKEN_OLD="old-secret"\nTOKEN_NEW="new-secret"\n')
        spec = Path(self.temp_dir.name) / "accounts.toml"
        spec.write_text(
            textwrap.dedent(
                f"""\
                provider = "somecli"
                target_env = "SOMECLI_TOKEN"
                env_file = "{secrets}"

                [[accounts]]
                label = "team"
                env_names = ["TOKEN_OLD"]
                """
            )
        )
        first = self._run("account", "import-spec", str(spec), "--apply")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn("team: added: SOMECLI_TOKEN=env:TOKEN_OLD", first.stdout)

        spec.write_text(
            textwrap.dedent(
                f"""\
                provider = "somecli"
                target_env = "SOMECLI_TOKEN"
                env_file = "{secrets}"

                [[accounts]]
                label = "team"
                env_names = ["TOKEN_NEW"]
                enabled = false
                """
            )
        )
        second = self._run("account", "import-spec", str(spec), "--apply")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("team: updated: SOMECLI_TOKEN=env:TOKEN_NEW", second.stdout)
        config_text = (self.config_dir / "config.toml").read_text()
        self.assertEqual(config_text.count('name = "team"'), 1)
        self.assertIn('enabled = false', config_text)
        self.assertIn('env = { SOMECLI_TOKEN = "env:TOKEN_NEW" }', config_text)
        self.assertNotIn("TOKEN_OLD", config_text)
        self.assertNotIn("old-secret", config_text)
        self.assertNotIn("new-secret", config_text)

    def test_account_import_spec_dry_run_reports_existing_updates(self) -> None:
        init = self._run("init")
        self.assertEqual(init.returncode, 0, init.stderr)
        secrets = Path(self.temp_dir.name) / "secrets.env"
        secrets.write_text('TOKEN_OLD="old-secret"\nTOKEN_NEW="new-secret"\n')
        added = self._run("account", "add", "somecli", "team", "--env-file", str(secrets), "--env-ref", "SOMECLI_TOKEN=TOKEN_OLD")
        self.assertEqual(added.returncode, 0, added.stderr)
        spec = Path(self.temp_dir.name) / "accounts.toml"
        spec.write_text(
            textwrap.dedent(
                f"""\
                provider = "somecli"
                target_env = "SOMECLI_TOKEN"
                env_file = "{secrets}"

                [[accounts]]
                label = "team"
                env_names = ["TOKEN_NEW"]
                """
            )
        )
        dry_run = self._run("account", "import-spec", str(spec))
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertIn("team: would-update: SOMECLI_TOKEN=env:TOKEN_NEW", dry_run.stdout)
        config_text = (self.config_dir / "config.toml").read_text()
        self.assertIn('env = { SOMECLI_TOKEN = "env:TOKEN_OLD" }', config_text)
        self.assertNotIn("TOKEN_NEW", config_text)

    def test_account_import_spec_preserves_existing_account_metadata(self) -> None:
        init = self._run("init")
        self.assertEqual(init.returncode, 0, init.stderr)
        secrets = Path(self.temp_dir.name) / "secrets.env"
        secrets.write_text('TOKEN_OLD="old-secret"\nTOKEN_NEW="new-secret"\n')
        added = self._run(
            "account",
            "add",
            "somecli",
            "team",
            "--env-file",
            str(secrets),
            "--env-ref",
            "SOMECLI_TOKEN=TOKEN_OLD",
            "--env-command",
            f"SOMECLI_SESSION={sys.executable} -c \"print('session')\"",
            "--prepare-on",
            "once",
            "--prepare-command",
            "somecli",
            "login",
        )
        self.assertEqual(added.returncode, 0, added.stderr)
        spec = Path(self.temp_dir.name) / "accounts.toml"
        spec.write_text(
            textwrap.dedent(
                f"""\
                provider = "somecli"
                target_env = "SOMECLI_TOKEN"
                env_file = "{secrets}"

                [[accounts]]
                label = "team"
                env_names = ["TOKEN_NEW"]
                """
            )
        )
        applied = self._run("account", "import-spec", str(spec), "--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn("team: updated: SOMECLI_TOKEN=env:TOKEN_NEW", applied.stdout)
        config_text = (self.config_dir / "config.toml").read_text()
        self.assertIn('env = { SOMECLI_TOKEN = "env:TOKEN_NEW" }', config_text)
        self.assertIn("env_command", config_text)
        self.assertIn("SOMECLI_SESSION", config_text)
        self.assertIn('prepare_on = "once"', config_text)
        self.assertIn("prepare_command", config_text)
        self.assertIn('prepare_command = ["somecli", "login"]', config_text)

    def test_account_import_spec_can_validate_secrets_before_import(self) -> None:
        from clifwrap.accounts import import_account_spec

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def read(self) -> bytes:
                return b'{"data":{"remainingCredits":42}}'

        secrets = Path(self.temp_dir.name) / "secrets.env"
        secrets.write_text('TOKEN_ONE="secret-value"\nTOKEN_BAD="bad-value"\n')
        spec = Path(self.temp_dir.name) / "accounts.toml"
        spec.write_text(
            textwrap.dedent(
                f"""\
                provider = "somecli"
                target_env = "SOMECLI_TOKEN"
                env_file = "{secrets}"
                set_default = true

                [validation]
                url = "https://example.test/usage"
                remaining_path = "data.remainingCredits"

                [[accounts]]
                label = "first"
                env_names = ["TOKEN_ONE"]
                """
            )
        )
        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch("clifwrap.accounts.urllib.request.urlopen", return_value=FakeResponse()):
                results = import_account_spec(spec, dry_run=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "would-add")
        self.assertIn("validated, remaining 42", results[0].detail)

        with mock.patch.dict(os.environ, self.env, clear=True):
            with mock.patch("clifwrap.accounts.urllib.request.urlopen", side_effect=OSError("nope")):
                results = import_account_spec(spec, dry_run=True)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "invalid")
        self.assertIn("validation failed", results[0].detail)

    def test_account_import_spec_cli_exits_nonzero_for_invalid_results(self) -> None:
        from clifwrap.__main__ import main
        from clifwrap.accounts import ImportResult

        output = StringIO()
        with mock.patch(
            "clifwrap.__main__.import_account_spec",
            return_value=[ImportResult(account="team", status="invalid", detail="TOKEN: validation failed")],
        ):
            with redirect_stdout(output):
                rc = main(["account", "import-spec", "unused.toml", "--apply"])
        self.assertEqual(rc, 2)
        self.assertIn("team: invalid: TOKEN: validation failed", output.getvalue())

    def test_capacity_control_config_loads_and_env_overrides(self) -> None:
        from clifwrap.config import load_config, merged_provider

        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.capacity_control]
                default_action = "queue"
                unknown_capacity_action = "allow"
                reserve_threshold = 5
                default_cost = 2
                queue_retention_seconds = 120
                queue_max_items = 8
                snapshot_ttl_seconds = 30
                command_costs = { search = 3 }
                remediation_message = "add capacity"

                [[providers.somecli.accounts]]
                name = "primary"
                """
            )
        )
        env = self.env | {
            "CLIFWRAP_PROVIDER_SOMECLI_CAPACITY_DEFAULT_ACTION": "shed",
            "CLIFWRAP_PROVIDER_SOMECLI_CAPACITY_RESERVE_THRESHOLD": "9",
            "CLIFWRAP_PROVIDER_SOMECLI_CAPACITY_COMMAND_COSTS": "search=7,extract=11",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            raw = load_config().providers["somecli"]
            provider = merged_provider("somecli", raw)
        assert provider.capacity_control is not None
        self.assertEqual(provider.capacity_control.default_action, "shed")
        self.assertEqual(provider.capacity_control.unknown_capacity_action, "allow")
        self.assertEqual(provider.capacity_control.reserve_threshold, 9)
        self.assertEqual(provider.capacity_control.default_cost, 2)
        self.assertEqual(provider.capacity_control.command_costs["search"], 7)
        self.assertEqual(provider.capacity_control.command_costs["extract"], 11)

    def test_invalid_capacity_control_action_is_rejected(self) -> None:
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.capacity_control]
                default_action = "maybe"
                """
            )
        )
        proc = self._run("status", "somecli")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("default_action", proc.stderr)

    def test_capacity_control_selects_alternate_account_before_request(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(
            target,
            "#!/usr/bin/env python3\nimport os\nprint(os.environ.get('CLIFWRAP_ACCOUNT'))\n",
        )
        self._run("install", "somecli")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                f"""\
                [providers.somecli]
                status_command = ["{sys.executable}", "-c", "import os,json; print(json.dumps({{'remaining': int(os.environ['LEFT'])}}))"]

                [providers.somecli.capacity_control]
                default_action = "shed"
                reserve_threshold = 3
                default_cost = 2

                [[providers.somecli.accounts]]
                name = "first"
                env = {{ LEFT = "1" }}

                [[providers.somecli.accounts]]
                name = "second"
                env = {{ LEFT = "10" }}
                """
            )
        )
        self._run("account", "use", "somecli", "first")
        proc = subprocess.run(
            ["somecli", "search"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "second")

    def test_unknown_capacity_allow_executes_upstream(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(
            target,
            "#!/usr/bin/env python3\nprint('ran upstream')\n",
        )
        self._run("install", "somecli")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [providers.somecli.capacity_control]
                default_action = "shed"
                unknown_capacity_action = "allow"
                reserve_threshold = 5
                default_cost = 2

                [[providers.somecli.accounts]]
                name = "only"
                """
            )
        )
        proc = subprocess.run(
            ["somecli", "search"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ran upstream", proc.stdout)
        self.assertIn("capacity unknown", proc.stderr)

    def test_passthrough_without_accounts_preserves_stdin(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(
            target,
            "#!/usr/bin/env python3\nimport sys\nprint(sys.stdin.read())\n",
        )
        self._run("install", "somecli")
        (self.config_dir / "config.toml").write_text("[providers.somecli]\n")
        proc = subprocess.run(
            ["somecli"],
            cwd=ROOT,
            env=self.env,
            input="body from pipe",
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "body from pipe")

    def test_capacity_admission_restricts_retry_failover_to_approved_account(self) -> None:
        target = self.bin_dir / "somecli"
        backup_marker = Path(self.temp_dir.name) / "backup-ran.txt"
        make_executable(
            target,
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import os
                import sys
                from pathlib import Path

                account = os.environ.get("CLIFWRAP_ACCOUNT")
                if account == "primary":
                    print("quota exhausted after admission", file=sys.stderr)
                    raise SystemExit(9)
                Path({str(backup_marker)!r}).write_text("backup ran")
                print("backup ran")
                """
            ),
        )
        self._run("install", "somecli")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                f"""\
                [providers.somecli]
                retry_exit_codes = [9]
                status_command = ["{sys.executable}", "-c", "import os,json; print(json.dumps({{'remaining': int(os.environ['LEFT'])}}))"]

                [providers.somecli.capacity_control]
                default_action = "queue"
                reserve_threshold = 5
                default_cost = 2

                [[providers.somecli.accounts]]
                name = "primary"
                env = {{ LEFT = "10" }}

                [[providers.somecli.accounts]]
                name = "backup"
                env = {{ LEFT = "1" }}
                """
            )
        )
        proc = subprocess.run(
            ["somecli", "search"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 9)
        self.assertIn("quota exhausted after admission", proc.stderr)
        self.assertFalse(backup_marker.exists())

    def test_capacity_default_execute_preserves_normal_retry_failover(self) -> None:
        target = self.bin_dir / "somecli"
        backup_marker = Path(self.temp_dir.name) / "default-execute-backup-ran.txt"
        make_executable(
            target,
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import os
                import sys
                from pathlib import Path

                account = os.environ.get("CLIFWRAP_ACCOUNT")
                if account == "primary":
                    print("primary below reserve failed", file=sys.stderr)
                    raise SystemExit(9)
                Path({str(backup_marker)!r}).write_text(account or "")
                print(account)
                """
            ),
        )
        self._run("install", "somecli")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                f"""\
                [providers.somecli]
                retry_exit_codes = [9]
                status_command = ["{sys.executable}", "-c", "import os,json; print(json.dumps({{'remaining': int(os.environ['LEFT'])}}))"]

                [providers.somecli.capacity_control]
                default_action = "execute"
                reserve_threshold = 5
                default_cost = 2

                [[providers.somecli.accounts]]
                name = "primary"
                env = {{ LEFT = "1" }}

                [[providers.somecli.accounts]]
                name = "backup"
                env = {{ LEFT = "1" }}
                """
            )
        )
        proc = subprocess.run(
            ["somecli", "search"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "backup")
        self.assertEqual(backup_marker.read_text(), "backup")

    def test_queue_decision_persists_and_queue_list_reports_item(self) -> None:
        target = self.bin_dir / "somecli"
        touched = Path(self.temp_dir.name) / "upstream-ran.txt"
        make_executable(
            target,
            f"#!/usr/bin/env python3\nfrom pathlib import Path\nPath({str(touched)!r}).write_text('ran')\n",
        )
        self._run("install", "somecli")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                f"""\
                [providers.somecli]
                status_command = ["{sys.executable}", "-c", "import os,json; print(json.dumps({{'remaining': int(os.environ['LEFT'])}}))"]

                [providers.somecli.capacity_control]
                default_action = "queue"
                reserve_threshold = 5
                default_cost = 2
                queue_retention_seconds = 120

                [[providers.somecli.accounts]]
                name = "only"
                env = {{ LEFT = "1" }}
                """
            )
        )
        proc = subprocess.run(
            ["somecli", "search", "random"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 73, proc.stderr)
        self.assertFalse(touched.exists())
        listed = self._run("queue", "list", "somecli", "--json")
        payload = json.loads(listed.stdout)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["provider"], "somecli")
        self.assertEqual(payload["items"][0]["argv"], ["search", "random"])

    def test_queue_run_replays_and_clears_item_when_capacity_recovers(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(
            target,
            "#!/usr/bin/env python3\nprint('replayed')\n",
        )
        self._run("install", "somecli")
        self.env["LEFT"] = "1"
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                f"""\
                [providers.somecli]
                status_command = ["{sys.executable}", "-c", "import os,json; print(json.dumps({{'remaining': int(os.environ['LEFT'])}}))"]

                [providers.somecli.capacity_control]
                default_action = "queue"
                reserve_threshold = 5
                default_cost = 2
                queue_retention_seconds = 120

                [[providers.somecli.accounts]]
                name = "only"
                env = {{ LEFT = "env:LEFT" }}
                """
            )
        )
        first = subprocess.run(
            ["somecli", "search"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(first.returncode, 73, first.stderr)
        self.env["LEFT"] = "20"
        replay = self._run("queue", "run", "somecli", "--json")
        payload = json.loads(replay.stdout)
        self.assertEqual(payload["results"][0]["status"], "executed")
        listed = self._run("queue", "list", "somecli", "--json")
        self.assertEqual(json.loads(listed.stdout)["items"], [])

    def test_queue_replay_restricts_retry_failover_to_approved_account(self) -> None:
        target = self.bin_dir / "somecli"
        backup_marker = Path(self.temp_dir.name) / "queue-backup-ran.txt"
        make_executable(
            target,
            textwrap.dedent(
                f"""\
                #!/usr/bin/env python3
                import os
                import sys
                from pathlib import Path

                account = os.environ.get("CLIFWRAP_ACCOUNT")
                if account == "primary":
                    print("primary replay failed", file=sys.stderr)
                    raise SystemExit(9)
                Path({str(backup_marker)!r}).write_text("backup ran")
                print("backup replay ran")
                """
            ),
        )
        self._run("install", "somecli")
        self.env["PRIMARY_LEFT"] = "1"
        self.env["BACKUP_LEFT"] = "1"
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                f"""\
                [providers.somecli]
                retry_exit_codes = [9]
                status_command = ["{sys.executable}", "-c", "import os,json; print(json.dumps({{'remaining': int(os.environ['LEFT'])}}))"]

                [providers.somecli.capacity_control]
                default_action = "queue"
                reserve_threshold = 5
                default_cost = 2
                queue_retention_seconds = 120

                [[providers.somecli.accounts]]
                name = "primary"
                env = {{ LEFT = "env:PRIMARY_LEFT" }}

                [[providers.somecli.accounts]]
                name = "backup"
                env = {{ LEFT = "env:BACKUP_LEFT" }}
                """
            )
        )
        queued = subprocess.run(
            ["somecli", "search"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(queued.returncode, 73, queued.stderr)
        self.env["PRIMARY_LEFT"] = "10"
        self.env["BACKUP_LEFT"] = "1"
        replay = self._run("queue", "run", "somecli", "--json")
        payload = json.loads(replay.stdout)
        self.assertEqual(payload["results"][0]["status"], "failed")
        self.assertEqual(payload["results"][0]["exit_code"], 9)
        self.assertFalse(backup_marker.exists())

    def test_queue_run_keeps_blocked_item_pending_without_duplication(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(
            target,
            "#!/usr/bin/env python3\nprint('unexpected upstream execution')\n",
        )
        self._run("install", "somecli")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                f"""\
                [providers.somecli]
                status_command = ["{sys.executable}", "-c", "import os,json; print(json.dumps({{'remaining': 1}}))"]

                [providers.somecli.capacity_control]
                default_action = "queue"
                reserve_threshold = 5
                default_cost = 2
                queue_retention_seconds = 120

                [[providers.somecli.accounts]]
                name = "only"
                """
            )
        )
        queued = subprocess.run(
            ["somecli", "search"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(queued.returncode, 73, queued.stderr)
        replay = self._run("queue", "run", "somecli", "--json")
        payload = json.loads(replay.stdout)
        self.assertEqual(payload["results"][0]["status"], "blocked")
        listed = self._run("queue", "list", "somecli", "--json")
        items = json.loads(listed.stdout)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["replay_count"], 1)

    def test_queue_run_missing_explicit_id_returns_not_found(self) -> None:
        empty = self._run("queue", "run", "--id", "missing")
        self.assertEqual(empty.returncode, 1)
        self.assertEqual(empty.stdout.strip(), "missing: not_found")

        scoped_json = self._run("queue", "run", "somecli", "--id", "missing", "--json")
        self.assertEqual(scoped_json.returncode, 1)
        payload = json.loads(scoped_json.stdout)
        self.assertEqual(payload["results"], [{"id": "missing", "provider": "somecli", "status": "not_found"}])

    def test_status_json_check_reports_queue_and_capacity_health(self) -> None:
        target = self.bin_dir / "somecli"
        make_executable(
            target,
            "#!/usr/bin/env python3\nprint('unexpected upstream execution')\n",
        )
        self._run("install", "somecli")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                f"""\
                [providers.somecli]
                status_command = ["{sys.executable}", "-c", "import os,json; print(json.dumps({{'remaining': 1}}))"]

                [providers.somecli.capacity_control]
                default_action = "queue"
                reserve_threshold = 5
                default_cost = 2
                queue_retention_seconds = 120

                [[providers.somecli.accounts]]
                name = "only"
                """
            )
        )
        subprocess.run(
            ["somecli", "search"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        status = self._run("status", "--json", "--check", "somecli")
        self.assertEqual(status.returncode, 1)
        payload = json.loads(status.stdout)
        self.assertEqual(payload["queue"]["pending"], 1)
        self.assertTrue(payload["capacity_health"]["unhealthy"])

    def test_queue_drop_expired_items_removes_only_expired_entries(self) -> None:
        from clifwrap.state import enqueue_queue_item

        with mock.patch.dict(os.environ, self.env, clear=True):
            enqueue_queue_item(
                "somecli",
                ["search", "old"],
                None,
                "expired item",
                {"default_action": "queue"},
                retention_seconds=1,
            )
            enqueue_queue_item(
                "somecli",
                ["search", "fresh"],
                None,
                "fresh item",
                {"default_action": "queue"},
                retention_seconds=120,
            )
        queue_path = self.state_dir / "queue.json"
        payload = json.loads(queue_path.read_text())
        payload["items"][0]["expires_at"] = 1
        queue_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

        dropped = self._run("queue", "drop", "somecli", "--expired", "--json")
        self.assertEqual(dropped.returncode, 0, dropped.stderr)
        dropped_payload = json.loads(dropped.stdout)
        self.assertEqual(len(dropped_payload["dropped"]), 1)
        self.assertEqual(dropped_payload["dropped"][0]["argv"], ["search", "old"])

        remaining = self._run("queue", "list", "somecli", "--json")
        remaining_payload = json.loads(remaining.stdout)
        self.assertEqual(len(remaining_payload["items"]), 1)
        self.assertEqual(remaining_payload["items"][0]["argv"], ["search", "fresh"])

    def test_queue_decision_prunes_expired_items_before_queue_limit(self) -> None:
        from clifwrap.state import enqueue_queue_item

        target = self.bin_dir / "somecli"
        make_executable(
            target,
            "#!/usr/bin/env python3\nprint('unexpected upstream execution')\n",
        )
        self._run("install", "somecli")
        with mock.patch.dict(os.environ, self.env, clear=True):
            enqueue_queue_item(
                "somecli",
                ["search", "expired"],
                None,
                "expired item",
                {"default_action": "queue"},
                retention_seconds=1,
            )
        queue_path = self.state_dir / "queue.json"
        payload = json.loads(queue_path.read_text())
        payload["items"][0]["expires_at"] = 1
        queue_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                f"""\
                [providers.somecli]
                status_command = ["{sys.executable}", "-c", "import json; print(json.dumps({{'remaining': 1}}))"]

                [providers.somecli.capacity_control]
                default_action = "queue"
                reserve_threshold = 5
                default_cost = 2
                queue_retention_seconds = 120
                queue_max_items = 1

                [[providers.somecli.accounts]]
                name = "only"
                """
            )
        )
        queued = subprocess.run(
            ["somecli", "search", "new"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(queued.returncode, 73, queued.stderr)
        self.assertIn("queued as", queued.stderr)
        listed = self._run("queue", "list", "somecli", "--json")
        items = json.loads(listed.stdout)["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["argv"], ["search", "new"])

    def test_queue_list_surfaces_malformed_queue_state(self) -> None:
        queue_path = self.state_dir / "queue.json"
        queue_path.write_text("{not json\n")
        proc = self._run("queue", "list", "somecli")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Malformed queue state", proc.stderr)

    def test_queue_run_surfaces_malformed_queue_state_as_json_error(self) -> None:
        queue_path = self.state_dir / "queue.json"
        queue_path.write_text("{not json\n")
        proc = self._run("queue", "run", "somecli", "--json")
        self.assertEqual(proc.returncode, 1)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["results"], [])
        self.assertIn("Malformed queue state", payload["error"])
        self.assertNotIn("Traceback", proc.stderr)

    def test_wrapped_queue_decision_surfaces_malformed_queue_state_without_traceback(self) -> None:
        target = self.bin_dir / "somecli"
        touched = Path(self.temp_dir.name) / "unexpected-upstream.txt"
        make_executable(
            target,
            f"#!/usr/bin/env python3\nfrom pathlib import Path\nPath({str(touched)!r}).write_text('ran')\n",
        )
        self._run("install", "somecli")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                f"""\
                [providers.somecli]
                status_command = ["{sys.executable}", "-c", "import json; print(json.dumps({{'remaining': 1}}))"]

                [providers.somecli.capacity_control]
                default_action = "queue"
                reserve_threshold = 5
                default_cost = 2

                [[providers.somecli.accounts]]
                name = "only"
                """
            )
        )
        (self.state_dir / "queue.json").write_text("{not json\n")
        proc = subprocess.run(
            ["somecli", "search"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 74)
        self.assertFalse(touched.exists())
        self.assertIn("[clifwrap] queue state error for somecli", proc.stderr)
        self.assertIn("Malformed queue state", proc.stderr)
        self.assertNotIn("Traceback", proc.stderr)

    def test_queue_list_surfaces_invalid_stdin_payload(self) -> None:
        queue_path = self.state_dir / "queue.json"
        queue_path.write_text(
            json.dumps(
                {
                    "items": [
                        {
                            "id": "badstdin",
                            "provider": "somecli",
                            "argv": ["search"],
                            "stdin_b64": "not valid base64!",
                            "enqueued_at": 1,
                            "replay_count": 0,
                            "reason": "bad payload",
                            "policy_snapshot": {},
                            "expires_at": 9999999999,
                        }
                    ]
                }
            )
            + "\n"
        )
        proc = self._run("queue", "list", "somecli")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("invalid stdin_b64", proc.stderr)

    def test_status_reports_queue_state_error_without_deleting_data(self) -> None:
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                """\
                [[providers.somecli.accounts]]
                name = "primary"
                """
            )
        )
        queue_path = self.state_dir / "queue.json"
        queue_path.write_text("{not json\n")
        status = self._run("status", "--json", "--check", "somecli")
        self.assertEqual(status.returncode, 1)
        payload = json.loads(status.stdout)
        self.assertIn("Malformed queue state", payload["queue"]["error"])
        self.assertTrue(queue_path.exists())

    def test_queue_commands_work_without_an_installed_shim(self) -> None:
        from clifwrap.state import enqueue_queue_item

        with mock.patch.dict(os.environ, self.env, clear=True):
            enqueue_queue_item(
                "somecli",
                ["search", "offline"],
                None,
                "seeded without shim",
                {"default_action": "queue"},
                retention_seconds=120,
            )
        listed = self._run("queue", "list", "somecli", "--json")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        payload = json.loads(listed.stdout)
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["provider"], "somecli")

    def test_builtin_provider_capacity_metadata_loads_without_runtime_branches(self) -> None:
        from clifwrap.config import merged_provider

        tvly = merged_provider("tvly", None)
        firecrawl = merged_provider("firecrawl", None)
        assert tvly.capacity_control is not None
        assert firecrawl.capacity_control is not None
        self.assertEqual(tvly.capacity_control.default_action, "queue")
        self.assertEqual(firecrawl.capacity_control.default_action, "queue")
        self.assertIn("search", tvly.capacity_control.command_costs)
        self.assertIn("scrape", firecrawl.capacity_control.command_costs)

    def test_env_file_values_feed_account_env(self) -> None:
        target = self.bin_dir / "tvly"
        make_executable(
            target,
            "#!/usr/bin/env python3\nimport os\nprint(os.environ.get('TAVILY_API_KEY'))\n",
        )
        secrets = Path(self.temp_dir.name) / "secrets.env"
        secrets.write_text('TAVILY_PRIMARY="secret-value"\n')
        self._run("install", "tvly")
        (self.config_dir / "config.toml").write_text(
            textwrap.dedent(
                f"""\
                [[providers.tvly.accounts]]
                name = "primary"
                env_files = ["{secrets}"]
                env = {{ TAVILY_API_KEY = "env:TAVILY_PRIMARY" }}
                """
            )
        )
        proc = subprocess.run(
            ["tvly", "auth"],
            cwd=ROOT,
            env=self.env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "secret-value")


if __name__ == "__main__":
    unittest.main()
