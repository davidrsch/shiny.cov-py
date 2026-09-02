"""Confirms vendor/discover_bindings_source.py still matches the canonical
R-package source it's generated from. Only meaningful inside a monorepo
checkout that has the sibling shiny.cov-r/ package present alongside this
one; when it's absent (e.g. this package cloned standalone) both tests
skip rather than fail, since tests/ and scripts/ aren't part of the
published sdist/wheel and this check has nothing to compare against.
Mirrors shiny.cov-cypress/test/vendor-sync.test.js.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import sync_discover_bindings as sync  # noqa: E402

pytestmark = pytest.mark.skipif(
    not sync.CANONICAL_PATH.exists(),
    reason=(
        "sibling shiny.cov-r/ package not present -- this check only runs "
        "meaningfully when developing with the monorepo layout available; "
        "not required for this package's own CI."
    ),
)


def test_vendored_source_is_in_sync_with_canonical_r_package_source():
    canonical_source = sync.CANONICAL_PATH.read_text(encoding="utf-8")
    expected = sync.render_vendored_module(canonical_source)
    actual = sync.OUTPUT_PATH.read_text(encoding="utf-8")

    assert actual == expected, (
        "vendored discover_bindings_source.py is out of sync with the canonical "
        "R-package source -- run `python scripts/sync_discover_bindings.py` from "
        "shiny.cov-py/ and commit the result."
    )


def test_vendored_module_actually_matches_the_live_import():
    # Belt-and-suspenders against the previous test somehow comparing two
    # stale copies of the same file: re-import the generated module and
    # check its constant against a fresh read of the canonical source too.
    from shinycov.vendor import discover_bindings_source
    importlib.reload(discover_bindings_source)

    canonical_source = sync.CANONICAL_PATH.read_text(encoding="utf-8")
    assert discover_bindings_source.DISCOVER_BINDINGS_SOURCE == canonical_source
