"""Tests for the standalone collector (Cypress-driven runs, no pytest)."""

import json
import os
import pathlib

import coverage

from shinycov.collect import (
    _read_interactions,
    _read_manifest,
    _source_of,
    main,
    source_counts,
)


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


def test_source_of_classifies_coverage_files():
    assert _source_of(".coverage") == "total"
    assert _source_of(".coverage.pytest") == "pytest"
    assert _source_of(".coverage.cypress") == "cypress"
    # Untagged parallel files (host.pid.random) map to "total".
    assert _source_of(".coverage.host.pid123.Xabcx") == "total"
    assert _source_of(".coverage.host.pid123.Xabcx.Hdefh") == "total"
    # A tagged parallel file keeps its source tag.
    assert _source_of(".coverage.cypress.host.pid123.Xabcx") == "cypress"


def _write_source_data(tmp_path: pathlib.Path, source: str, filename: str):
    cov = coverage.Coverage(data_file=str(tmp_path / f".coverage.{source}"))
    cov.start()
    path = tmp_path / filename
    path.write_text("def f():\n    return 1\n", encoding="utf-8")
    ns: dict = {}
    exec(compile(path.read_text(), str(path), "exec"), ns)
    ns["f"]()
    cov.stop()
    cov.save()


def test_setup_flag_installs_module_hook(tmp_path, monkeypatch, capsys):
    old_path = os.environ.get("PYTHONPATH")
    old_out = os.environ.get("SHINYCOV_OUTPUT_DIR")
    try:
        monkeypatch.chdir(tmp_path)
        assert main([".", "--setup"]) == 0
        assert (tmp_path / ".shiny.cov" / "sitecustomize.py").exists()
        assert "export PYTHONPATH" in capsys.readouterr().out
    finally:
        if old_path is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old_path
        if old_out is None:
            os.environ.pop("SHINYCOV_OUTPUT_DIR", None)
        else:
            os.environ["SHINYCOV_OUTPUT_DIR"] = old_out


def test_source_counts_reports_each_source(tmp_path: pathlib.Path):
    _write_source_data(tmp_path, "pytest", "pytest_app.py")
    _write_source_data(tmp_path, "cypress", "cypress_app.py")

    rows = source_counts(tmp_path)
    by_source = {row["source"]: row for row in rows}

    assert set(by_source) == {"pytest", "cypress"}
    # Each file has two executable lines (def f, return 1); both run.
    assert by_source["pytest"]["expressions"] == 2
    assert by_source["pytest"]["hits"] == 2
    assert by_source["cypress"]["expressions"] == 2
    assert by_source["cypress"]["hits"] == 2
