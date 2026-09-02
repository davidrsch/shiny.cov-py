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
import pathlib
from typing import Any

import coverage

from .plugin import _blend_ui_coverage, _combine_with_retry, finalize, OUTPUT_DIRNAME


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
    cov = coverage.Coverage()
    _combine_with_retry(cov)
    manifest = _read_manifest(out_dir)
    interactions = _read_interactions(out_dir)
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
    args = parser.parse_args(argv)
    percent = collect(args.root)
    if args.cobertura:
        to_cobertura(args.root, args.cobertura)
        print(f"shiny.cov: wrote Cobertura report to {args.cobertura}")
    return 0 if percent is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
