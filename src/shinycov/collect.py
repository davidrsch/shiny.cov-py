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

from .plugin import _combine_with_retry, finalize, OUTPUT_DIRNAME


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
    args = parser.parse_args(argv)
    percent = collect(args.root)
    return 0 if percent is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
