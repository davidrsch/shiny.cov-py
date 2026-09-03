"""Standalone collector for Cypress-driven py-shiny coverage.

When a py-shiny app is tested with Cypress there is no pytest session, so the
plugin's `pytest_sessionfinish` hook never runs. Instead:

  * the app is launched under `coverage run` (with `[run] patch = ["subprocess"]`
    so the app subprocess is measured),
  * the Cypress adapter (shiny.cov-cypress's `plugin.js`/`support.js`) writes
    `.shiny.cov/manifest.json` and `.shiny.cov/interactions.json`,
  * this module's `shinycov` command combines the `.coverage.*` data with that
    UI manifest/interaction log and reports the same blended coverage the
    pytest plugin produces.

Usage:

    coverage run -m shiny run app.py --port 3333 --host 127.0.0.1 &
    cypress run
    shinycov .          # or: python -m shinycov.collect .
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import shutil
from typing import Any

import coverage

from . import modules
from .plugin import _blend_ui_coverage, _combine_with_retry, finalize, OUTPUT_DIRNAME

# coverage.py's own parallel-file suffix (see coverage.sqldata.SUFFIX_PATTERN):
# `.HOST.pidPID.XRANDOMx[.HHASHh]`. Replicated here so source_counts() can tell
# a parallel file (`.coverage.<host>.pid…`) from a source-tagged base file
# (`.coverage.cypress`) without importing coverage internals.
_PARALLEL_SUFFIX = re.compile(
    r"\.([^.]+)\.pid(\d+)\.X(\w+?)x(\.H(\w+?)h)?$"
)


def _read_manifest(out_dir: pathlib.Path) -> dict[str, Any] | None:
    path = out_dir / "manifest.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _read_interactions(out_dir: pathlib.Path) -> list[dict[str, Any]]:
    path = out_dir / "interactions.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return data if isinstance(data, list) else []


def collect(root: str | pathlib.Path = ".") -> float | None:
    """Combine `.coverage.*` data with the Cypress UI log and report it."""
    root = pathlib.Path(root)
    out_dir = root / OUTPUT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    cov = coverage.Coverage()
    _combine_with_retry(cov)
    manifest = _read_manifest(out_dir)
    interactions = _read_interactions(out_dir)
    return finalize(cov, manifest, interactions, out_dir)


def _line_hits(cov: coverage.Coverage, filename: str) -> dict[int, int]:
    """Map executable line number -> hit count for one measured file.

    coverage.py records line execution as a boolean (a line ran or it did
    not), not as an execution count the way covr does, so hits are 0 or 1.
    """
    try:
        _name, executable, _excluded, missing, _branches = cov.analysis2(filename)
    except coverage.misc.CoverageException:
        return {}
    missing = set(missing)
    return {line: (0 if line in missing else 1) for line in executable}


def to_cobertura(
    root: str | pathlib.Path = ".", filename: str = "cobertura.xml"
) -> str:
    """Write a UI-blended Cobertura XML report for the combined coverage.

    Combines `.coverage.*` data, merges the UI manifest/interaction log the
    same way `collect()` does, and emits one `<line>` per executable source
    line with `hits` 0 or 1 (coverage.py tracks execution, not counts). The
    generated UI-elements file is included, so the report reflects the same
    blended number as `collect()`.
    """
    root = pathlib.Path(root)
    out_dir = root / OUTPUT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)
    cov = coverage.Coverage()
    _combine_with_retry(cov)
    manifest = _read_manifest(out_dir)
    interactions = _read_interactions(out_dir)
    boundaries = modules.read_boundaries(out_dir)
    if manifest is not None and boundaries:
        manifest = modules.attach_boundaries(manifest, boundaries)
    _blend_ui_coverage(cov, manifest, interactions, out_dir)
    return _write_cobertura_xml(cov, filename)


def _write_cobertura_xml(cov: coverage.Coverage, filename: str) -> str:
    """Emit Cobertura XML for an already-loaded, UI-blended `cov`."""
    files = sorted(cov.get_data().measured_files())
    rows: dict[str, dict[int, int]] = {}
    for f in files:
        hits = _line_hits(cov, f)
        if hits:
            rows[f] = hits

    n = sum(len(hits) for hits in rows.values())
    covered = sum(sum(hits.values()) for hits in rows.values())
    rate = covered / n if n else 0.0

    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append(
        '<coverage line-rate="%s" branch-rate="0" lines-covered="%d" '
        'lines-valid="%d" branches-covered="0" branches-valid="0" '
        'complexity="0" version="1.0" timestamp="%s">'
        % (rate, covered, n, _cobertura_timestamp())
    )
    xml.append("  <sources></sources>")
    xml.append("  <packages>")
    xml.append(
        '    <package name="shiny.cov" line-rate="%s" branch-rate="0" '
        'complexity="0">' % rate
    )
    xml.append("      <classes>")

    for f in files:
        hits = rows.get(f)
        if not hits:
            continue
        f_covered = sum(hits.values())
        f_rate = f_covered / len(hits)
        xml.append(
            '        <class name="%s" filename="%s" line-rate="%s" '
            'branch-rate="0" complexity="0">' % (pathlib.Path(f).name, f, f_rate)
        )
        xml.append("          <methods/>")
        xml.append("          <lines>")
        for line in sorted(hits):
            xml.append(
                '            <line number="%d" hits="%d" branch="false"/>'
                % (line, hits[line])
            )
        xml.append("          </lines>")
        xml.append("        </class>")

    xml.append("      </classes>")
    xml.append("    </package>")
    xml.append("  </packages>")
    xml.append("</coverage>")

    pathlib.Path(filename).write_text("\n".join(xml) + "\n", encoding="utf-8")
    return filename


def _cobertura_timestamp() -> str:
    import datetime

    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _source_of(name: str) -> str:
    """Classify a `.coverage*` file basename into its source tag.

    `.coverage` and `.coverage.<host>.pid...` (an untagged parallel file)
    both map to "total"; `.coverage.cypress[.<suffix>]` maps to "cypress".
    """
    if name == ".coverage":
        return "total"
    rest = name[len(".coverage") :]
    if not rest:
        return "total"
    match = _PARALLEL_SUFFIX.search(rest)
    if match and match.start() == 0:
        return "total"
    if match:
        rest = rest[: match.start()]
    return rest.lstrip(".") or "total"


def _source_line_summary(root: pathlib.Path, base_name: str) -> tuple[int, int]:
    """(executable lines, covered lines) for one source's combined data."""
    cov = _load_source(root, base_name)
    if cov is None:
        return 0, 0

    expressions = 0
    hits = 0
    for filename in cov.get_data().measured_files():
        try:
            _name, executable, _excluded, missing, _branches = cov.analysis2(filename)
        except coverage.misc.CoverageException:
            continue
        expressions += len(executable)
        hits += len(executable) - len(set(missing))
    return expressions, hits


def _load_source(root: pathlib.Path, base_name: str) -> coverage.Coverage | None:
    """Load one source's combined coverage data, without consuming it."""
    base = root / base_name
    cov = coverage.Coverage(data_file=str(base))
    try:
        cov.combine(strict=True, keep=True)
    except coverage.misc.CoverageException:
        if not base.exists():
            return None
        cov.load()
    return cov


def source_coverage(
    root: str | pathlib.Path = ".",
) -> dict[str, dict[int, dict[str, int]]]:
    """Per-file, per-line, per-source hit counts (mirrors R's source_coverage()).

    Returns `{filename: {line: {source: hits}}}` where `hits` is 0 or 1
    (coverage.py records execution, not counts). The per-line total is the
    number of sources that covered that line.
    """
    root = pathlib.Path(root)
    sources: dict[str, coverage.Coverage] = {}
    seen: set[str] = set()
    for path in sorted(root.glob(".coverage*")):
        if path.name.endswith("-journal"):
            continue
        source = _source_of(path.name)
        if source in seen:
            continue
        base_name = ".coverage" if source == "total" else f".coverage.{source}"
        cov = _load_source(root, base_name)
        if cov is not None:
            seen.add(source)
            sources[source] = cov

    result: dict[str, dict[int, dict[str, int]]] = {}
    for source, cov in sources.items():
        for filename in cov.get_data().measured_files():
            try:
                _name, executable, _excluded, missing, _branches = cov.analysis2(filename)
            except coverage.misc.CoverageException:
                continue
            missing = set(missing)
            for line in executable:
                result.setdefault(filename, {}).setdefault(line, {})[source] = (
                    0 if line in missing else 1
                )
    return result


_SCOR_CSS = (
    "#source{padding:1em 0}"
    "#source p{display:flex;gap:.5em}"
    "#source p .n{float:none;width:3.5rem;margin-left:0;padding-right:0;flex:0 0 3.5rem}"
    "#source p .scov{float:none;flex:0 0 3rem;text-align:right;font-weight:bold}"
    "#source p .scov-src{float:none;flex:0 0 4rem;text-align:right}"
    "#source p .t{width:auto;flex:1 1 auto;margin-left:0}"
    ".sticky-top{position:sticky;top:0;z-index:10}"
    "header p.text{display:none}"
    "#help_panel_wrapper{display:none}"
    ".scov-bar{background:#f8f8f8;color:#000;border-bottom:1px solid #ccc;padding:.3em 0}"
    ".scov-header{display:flex;gap:.5em;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:.85em}"
    ".scov-header .h-n{flex:0 0 3.5rem;text-align:right}"
    ".scov-header .h-count{flex:0 0 3rem;text-align:right;font-weight:bold}"
    ".scov-header .h-src{flex:0 0 4rem;text-align:right}"
    ".scov-header .h-t{flex:1 1 auto}"
    ".scov-toggle{font-family:sans-serif;font-size:.85em;display:block;margin:.4em 0;color:#666}"
    "@media (prefers-color-scheme: dark){"
    ".scov-bar{background:#000;color:#eee;border-color:#333}"
    ".scov-toggle{color:#aaa}"
    "}"
)

_LINE_ANCHOR_RE = re.compile(
    r'(<p class="[^"]*">\s*<span class="n"><a id="t(\d+)"[^>]*>\d+</a></span>)'
)

_INDEX_ROW_RE = re.compile(r'<tr class="region">.*?</tr>', re.DOTALL)


def _is_app_file(root: pathlib.Path, rel_path: str) -> bool:
    try:
        rel = pathlib.Path(os.path.abspath(rel_path)).resolve().relative_to(root)
    except ValueError:
        return False
    return not any(p == OUTPUT_DIRNAME for p in rel.parts) and not (
        rel.parts
        and (rel.parts[-1].startswith("test_") or rel.parts[-1].endswith("_test.py"))
    )


def _decorate_coverage_html(out_dir: pathlib.Path, root: pathlib.Path) -> None:
    """Inject per-line hit counts, a fixed header, and a source toggle into
    coverage.py's own HTML report, and hide non-app files from its index."""
    htmlcov = out_dir / "htmlcov"
    status_path = htmlcov / "status.json"
    if not status_path.exists():
        return
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    line_hits = modules.read_line_hits(out_dir)
    sources = sorted(line_hits)
    if not sources:
        return

    url_to_file: dict[str, str] = {}
    for entry in status.get("files", {}).values():
        index = entry.get("index")
        if index and index.get("url"):
            url_to_file[index["url"]] = index.get("file", "")

    # Drop non-app files (ui_elements.py, the plugin's own files, tests) from
    # the index while keeping their contribution to the overall percentage.
    index_file = htmlcov / "index.html"
    if index_file.exists():
        html = index_file.read_text(encoding="utf-8")

        def _row(match: re.Match[str]) -> str:
            href = re.search(r'href="([^"]+)"', match.group(0))
            if href and not _is_app_file(root, url_to_file.get(href.group(1), "")):
                return ""
            return match.group(0)

        index_file.write_text(_INDEX_ROW_RE.sub(_row, html), encoding="utf-8")

    # Decorate each app file's per-file page.
    for url, rel_path in url_to_file.items():
        if not _is_app_file(root, rel_path):
            continue
        html_file = htmlcov / url
        if not html_file.exists():
            continue
        abs_path = os.path.abspath(rel_path)

        def _repl(match: re.Match[str]) -> str:
            lineno = int(match.group(2))
            spans = []
            total = 0
            for source in sources:
                count = line_hits.get(source, {}).get((abs_path, lineno), 0)
                total += count
                spans.append(f'<span class="scov-src src-col">{count}</span>')
            return match.group(1) + f'<span class="scov">{total}</span>' + "".join(spans)

        html = html_file.read_text(encoding="utf-8")
        html = _LINE_ANCHOR_RE.sub(_repl, html)
        header = (
            '<div class="scov-bar">'
            '<label class="scov-toggle"><input type="checkbox" id="shinycov-toggle-sources" checked> '
            "show test source columns</label>"
            '<div class="scov-header"><span class="h-n">#</span><span class="h-count">count</span>'
            + "".join(f'<span class="h-src src-col">{s}</span>' for s in sources)
            + '<span class="h-t">source</span></div>'
            "</div>"
        )
        html = html.replace('<header>', '<div class="sticky-top"><header>', 1)
        html = html.replace('<main id="source">', header + '</div><main id="source">', 1)
        toggle_js = (
            "<script>var sc=document.getElementById('shinycov-toggle-sources');"
            "if(sc){function su(){var v=sc.checked?'':'none';"
            "document.querySelectorAll('.src-col').forEach(function(e){e.style.display=v});}"
            "sc.addEventListener('change',su);}</script>"
        )
        html = html.replace("</head>", f"<style>{_SCOR_CSS}</style></head>", 1)
        html = html.replace("</body>", toggle_js + "</body>", 1)
        html_file.write_text(html, encoding="utf-8")


def render_report_html(root: str | pathlib.Path = ".") -> str:
    """Emit coverage.py's styled HTML report, decorated with per-line hit counts.

    Combines all `.coverage.*` data, blends the UI manifest/interaction log,
    writes coverage.py's own HTML report under `.shiny.cov/htmlcov/`, then
    injects a per-line `count` column plus one column per test source, a fixed
    header, and a checkbox that hides/shows the per-source columns (the
    `count` column always stays). Non-app files are hidden from the index.
    """
    root = pathlib.Path(root).resolve()
    out_dir = root / OUTPUT_DIRNAME
    out_dir.mkdir(parents=True, exist_ok=True)

    cov = coverage.Coverage(data_file=str(root / ".coverage"))
    _combine_with_retry(cov)
    manifest = _read_manifest(out_dir)
    interactions = _read_interactions(out_dir)
    boundaries = modules.read_boundaries(out_dir)
    if manifest is not None and boundaries:
        manifest = modules.attach_boundaries(manifest, boundaries)
    _blend_ui_coverage(cov, manifest, interactions, out_dir)

    # Keep the report focused on the app's own code.
    cov.config.report_omit = [
        "*/shinycov/*",
        "*/site-packages/*",
        "test_*.py",
        "*sitecustomize.py",
    ]

    html_dir = out_dir / "htmlcov"
    # Remove a previous report first so stale per-file pages (e.g. files a
    # previous run measured but this run omitted) don't linger.
    shutil.rmtree(html_dir, ignore_errors=True)
    cov.html_report(directory=str(html_dir))
    _decorate_coverage_html(out_dir, root)
    return str(html_dir / "index.html")


def source_counts(root: str | pathlib.Path = ".") -> list[dict[str, Any]]:
    """Per-source coverage summary, mirroring R's `source_counts()`.

    Returns one row per test source that produced coverage (e.g. `pytest`,
    `cypress`), with `expressions` (executable lines) and `hits` (covered
    lines). A run with a single untagged source reports only `total`.

    Unlike R's `source_counts()`, `hits` counts covered lines rather than
    execution counts: coverage.py records whether a line ran, not how many
    times.
    """
    root = pathlib.Path(root)
    by_source: dict[str, list[pathlib.Path]] = {}
    for path in root.glob(".coverage*"):
        if path.name.endswith("-journal"):
            continue
        by_source.setdefault(_source_of(path.name), []).append(path)

    rows = []
    for source in sorted(by_source):
        base_name = ".coverage" if source == "total" else f".coverage.{source}"
        expressions, hits = _source_line_summary(root, base_name)
        rows.append({"source": source, "expressions": expressions, "hits": hits})
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shinycov",
        description="Blend coverage.py data with the Cypress UI log and report it.",
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=".",
        help="app directory containing .shiny.cov/ (default: current directory)",
    )
    parser.add_argument(
        "--cobertura",
        metavar="FILE",
        default=None,
        help="also write a UI-blended Cobertura XML report to FILE",
    )
    parser.add_argument(
        "--sources",
        action="store_true",
        help="print the per-source coverage breakdown (source_counts())",
    )
    parser.add_argument(
        "--setup",
        action="store_true",
        help="install the module-boundary hook into .shiny.cov/ and print the env",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="write coverage.py's HTML report decorated with per-source line counts",
    )
    args = parser.parse_args(argv)

    if args.setup:
        out_dir = pathlib.Path(args.root) / OUTPUT_DIRNAME
        modules.install(out_dir)
        print(
            f'export PYTHONPATH="{out_dir}:$PYTHONPATH" '
            f'SHINYCOV_OUTPUT_DIR="{out_dir}" SHINYCOV_SOURCE=cypress'
        )
        return 0

    percent = collect(args.root)
    if args.cobertura:
        to_cobertura(args.root, args.cobertura)
        print(f"shiny.cov: wrote Cobertura report to {args.cobertura}")
    if args.html:
        index = render_report_html(args.root)
        print(f"shiny.cov: wrote annotated HTML report to {index}")
    if args.sources:
        for row in source_counts(args.root):
            print(
                f"shiny.cov: source {row['source']}: "
                f"{row['hits']}/{row['expressions']} lines"
            )
    return 0 if percent is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
