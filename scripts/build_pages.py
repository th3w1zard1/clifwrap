#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import re
import shutil
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DEFAULT_SITE = ROOT / "site"

STYLE = """
:root {
  --ink: #18211f;
  --muted: #60706b;
  --line: #d9e2df;
  --surface: #f6f4ed;
  --panel: #ffffff;
  --accent: #0f766e;
  --accent-ink: #073d39;
}
* { box-sizing: border-box; }
body {
  margin: 0;
  background:
    radial-gradient(circle at top left, rgba(15, 118, 110, 0.14), transparent 32rem),
    linear-gradient(135deg, #fbfaf5 0%, var(--surface) 100%);
  color: var(--ink);
  font-family: "Avenir Next", "Segoe UI", sans-serif;
  line-height: 1.6;
}
main { width: min(1040px, calc(100% - 2rem)); margin: 0 auto; padding: 3rem 0 4rem; }
header.hero, article {
  background: rgba(255, 255, 255, 0.84);
  border: 1px solid var(--line);
  border-radius: 24px;
  box-shadow: 0 24px 80px rgba(24, 33, 31, 0.08);
  padding: clamp(1.5rem, 4vw, 3rem);
}
h1 { font-size: clamp(2.25rem, 7vw, 5rem); line-height: 0.95; margin: 0 0 1rem; letter-spacing: -0.06em; }
h2 { margin-top: 2rem; font-size: 1.5rem; }
p, li { color: var(--muted); }
a { color: var(--accent); font-weight: 700; text-decoration-thickness: 0.08em; text-underline-offset: 0.18em; }
code { background: #eef5f3; color: var(--accent-ink); border-radius: 0.35rem; padding: 0.1rem 0.3rem; }
pre { overflow: auto; background: #10211e; color: #e9fffa; border-radius: 18px; padding: 1rem; }
pre code { background: transparent; color: inherit; padding: 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 1rem; margin-top: 1.5rem; }
.card { display: block; min-height: 9rem; background: var(--panel); border: 1px solid var(--line); border-radius: 18px; padding: 1.2rem; text-decoration: none; }
.card strong { display: block; color: var(--ink); font-size: 1.1rem; margin-bottom: 0.35rem; }
.badge { display: inline-block; border: 1px solid var(--line); border-radius: 999px; padding: 0.25rem 0.65rem; color: var(--muted); margin: 0 0.35rem 0.35rem 0; }
nav { margin-bottom: 1rem; }
"""


def title_from_markdown(path: Path) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem.replace("-", " ").title()


def inline(value: str) -> str:
    escaped = html.escape(value)
    escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
    return re.sub(r"\[([^\]]+)\]\(([^)]+)\)", lambda match: link(match.group(1), match.group(2)), escaped)


def link(label: str, href: str) -> str:
    if href.endswith(".md"):
        href = href[:-3] + ".html"
    return f'<a href="{html.escape(href, quote=True)}">{label}</a>'


def markdown_to_html(text: str) -> str:
    parts: list[str] = []
    paragraph: list[str] = []
    in_code = False
    list_open = False

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            parts.append("<p>" + inline(" ".join(paragraph)) + "</p>")
            paragraph = []

    def close_list() -> None:
        nonlocal list_open
        if list_open:
            parts.append("</ul>")
            list_open = False

    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            flush_paragraph()
            close_list()
            parts.append("</code></pre>" if in_code else "<pre><code>")
            in_code = not in_code
            continue
        if in_code:
            parts.append(html.escape(raw))
            continue
        if not line.strip():
            flush_paragraph()
            close_list()
            continue
        if line.startswith("#"):
            flush_paragraph()
            close_list()
            level = min(len(line) - len(line.lstrip("#")), 4)
            parts.append(f"<h{level}>{inline(line[level:].strip())}</h{level}>")
            continue
        if line.startswith("- "):
            flush_paragraph()
            if not list_open:
                parts.append("<ul>")
                list_open = True
            parts.append(f"<li>{inline(line[2:].strip())}</li>")
            continue
        paragraph.append(line.strip())
    flush_paragraph()
    close_list()
    if in_code:
        parts.append("</code></pre>")
    return "\n".join(parts)


def page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html.escape(title)} - clifwrap</title>
  <style>{STYLE}</style>
</head>
<body><main>{body}</main></body>
</html>
"""


def junit_summary(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return "JUnit XML is present but could not be parsed."

    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    if not suites:
        return "JUnit XML is present but contains no test suite counts."
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for suite in suites:
        for key in totals:
            value = suite.attrib.get(key, "0")
            try:
                totals[key] += int(value)
            except ValueError:
                return "JUnit XML is present but contains non-integer test counts."
    passed = totals["tests"] - totals["failures"] - totals["errors"] - totals["skipped"]
    status = "passing" if totals["failures"] == 0 and totals["errors"] == 0 else "failing"
    return (
        f"{totals['tests']} tests, {passed} passed, {totals['failures']} failures, "
        f"{totals['errors']} errors, {totals['skipped']} skipped ({status})."
    )


def build(site: Path) -> None:
    site.mkdir(parents=True, exist_ok=True)
    docs_out = site / "docs"
    docs_out.mkdir(exist_ok=True)
    schemas_in = DOCS / "schemas"
    schemas_out = site / "schemas"
    if schemas_in.exists():
        schemas_out.mkdir(exist_ok=True)
        for schema in sorted(schemas_in.glob("*.json")):
            shutil.copy2(schema, schemas_out / schema.name)

    doc_cards: list[str] = []
    for markdown in sorted(DOCS.glob("*.md")):
        title = title_from_markdown(markdown)
        target = docs_out / f"{markdown.stem}.html"
        target.write_text(
            page(title, f'<nav><a href="../index.html">clifwrap</a></nav><article>{markdown_to_html(markdown.read_text(encoding="utf-8"))}</article>'),
            encoding="utf-8",
        )
        doc_cards.append(f'<a class="card" href="docs/{target.name}"><strong>{html.escape(title)}</strong><span>{html.escape(markdown.name)}</span></a>')

    reports = []
    if (site / "pytest.html").exists():
        reports.append('<a class="card" href="pytest.html"><strong>pytest HTML</strong><span>Latest rendered test report.</span></a>')
    junit = site / "junit.xml"
    if junit.exists():
        reports.append(f'<a class="card" href="junit.xml"><strong>JUnit XML</strong><span>{html.escape(junit_summary(junit) or "Machine-readable pytest results.")}</span></a>')
    if (site / "release-summary.json").exists():
        reports.append('<a class="card" href="release-summary.json"><strong>Release Summary JSON</strong><span>Machine-readable local release verification proof.</span></a>')
    if not reports:
        reports.append('<span class="card"><strong>Reports pending</strong><span>Run pytest with HTML/JUnit outputs to populate this section.</span></span>')

    body = f"""
<header class="hero">
  <span class="badge">quota-aware CLI failover</span>
  <span class="badge">release proof included</span>
  <h1>clifwrap</h1>
  <p>Transparent account failover, quota-aware scheduling, and wrapper-managed auth for CLIs such as Tavily and Firecrawl.</p>
  <div class="grid">{''.join(reports)}<a class="card" href="https://github.com/th3w1zard1/clifwrap"><strong>GitHub</strong><span>Source repository and releases.</span></a></div>
</header>
<h2>Documentation</h2>
<div class="grid">{''.join(doc_cards)}</div>
"""
    (site / "index.html").write_text(page("Reports and docs", body), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the static clifwrap GitHub Pages site.")
    parser.add_argument("--site", default=str(DEFAULT_SITE), help="Output directory. Existing pytest reports are preserved.")
    args = parser.parse_args()
    build(Path(args.site))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
