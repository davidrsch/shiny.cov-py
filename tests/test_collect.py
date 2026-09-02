"""Tests for the standalone collector (Cypress-driven runs, no pytest)."""

import json
import pathlib

from shinycov.collect import _read_interactions, _read_manifest


def test_read_manifest_returns_none_when_missing(tmp_path: pathlib.Path):
    assert _read_manifest(tmp_path / ".shiny.cov") is None


def test_read_manifest_parses_json(tmp_path: pathlib.Path):
    out = tmp_path / ".shiny.cov"
    out.mkdir()
    (out / "manifest.json").write_text(
        json.dumps({"inputs": [{"id": "go"}], "outputs": [], "tabs": [],
                    "conditional": [], "modules": []})
    )
    manifest = _read_manifest(out)
    assert manifest is not None
    assert manifest["inputs"] == [{"id": "go"}]


def test_read_manifest_handles_corrupt_file(tmp_path: pathlib.Path):
    out = tmp_path / ".shiny.cov"
    out.mkdir()
    (out / "manifest.json").write_text("{ not json")
    assert _read_manifest(out) is None


def test_read_interactions_returns_empty_when_missing(tmp_path: pathlib.Path):
    assert _read_interactions(tmp_path / ".shiny.cov") == []


def test_read_interactions_parses_json(tmp_path: pathlib.Path):
    out = tmp_path / ".shiny.cov"
    out.mkdir()
    (out / "interactions.json").write_text(
        json.dumps([{"selector": "#go", "action": "click", "value": None}])
    )
    assert _read_interactions(out) == [
        {"selector": "#go", "action": "click", "value": None}
    ]
