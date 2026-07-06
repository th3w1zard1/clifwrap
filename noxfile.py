"""Local automation sessions for contributors and release maintainers."""

from __future__ import annotations

import os
from pathlib import Path

import nox

PYTHON_VERSIONS = ["3.11", "3.12", "3.13", "3.14"]
CHECK_PATHS = ["src", "tests", "scripts", "packaging"]

nox.options.sessions = ["lint", "tests", "compile", "docs", "build", "pages"]


def install_dev(session: nox.Session) -> None:
    session.install("-e", ".[dev]")


@nox.session
def lint(session: nox.Session) -> None:
    """Run Ruff over the repository."""
    install_dev(session)
    session.run("python", "-m", "ruff", "check", ".")


@nox.session(python=PYTHON_VERSIONS)
def tests(session: nox.Session) -> None:
    """Run pytest with CI-compatible JUnit and HTML reports."""
    install_dev(session)
    reports = Path("reports") / f"python-{session.python}"
    reports.mkdir(parents=True, exist_ok=True)
    session.run(
        "python",
        "-m",
        "pytest",
        f"--junitxml={reports / 'junit.xml'}",
        f"--html={reports / 'pytest.html'}",
        "--self-contained-html",
        *session.posargs,
    )


@nox.session(name="compile")
def compile_sources(session: nox.Session) -> None:
    """Byte-compile the same source roots checked by CI."""
    session.run("python", "-m", "compileall", *CHECK_PATHS)


@nox.session
def docs(session: nox.Session) -> None:
    """Check generated documentation artifacts."""
    install_dev(session)
    session.run("python", "scripts/generate_cli_reference.py", "--check")
    session.run("python", "scripts/generate_provider_catalog.py", "--check")


@nox.session
def build(session: nox.Session) -> None:
    """Build the source distribution and wheel."""
    session.install("build>=1.2")
    session.run("python", "-m", "build")


@nox.session
def pages(session: nox.Session) -> None:
    """Generate the GitHub Pages pytest report and docs site."""
    install_dev(session)
    session.run("python", "scripts/verify_release.py", "--skip-tests", "--skip-pyinstaller", "--summary-json", "site/release-summary.json", env={"PATH": os.environ.get("PATH", "")})
    session.run("python", "-m", "pytest", "--junitxml=site/junit.xml", "--html=site/pytest.html", "--self-contained-html")
    session.run("python", "scripts/build_pages.py", "--site", "site")


@nox.session(name="release-verify")
def release_verify(session: nox.Session) -> None:
    """Run the full local release verifier.

    Pass verifier flags after ``--``, for example:
    ``nox -s release-verify -- --require-actionlint``.
    """
    session.install("-e", ".[dev,release]")
    session.run("python", "scripts/verify_release.py", *session.posargs, env={"PATH": os.environ.get("PATH", "")})
