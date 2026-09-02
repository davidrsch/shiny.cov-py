"""Tests for the UI-blended Cobertura export (shinycov.to_cobertura)."""

from __future__ import annotations

import pathlib
import xml.etree.ElementTree as ET

import coverage

from shinycov.collect import _line_hits, _write_cobertura_xml
from shinycov.plugin import _blend_ui_coverage


def _run_and_measure(tmp_path: pathlib.Path) -> coverage.Coverage:
    app = tmp_path / "app.py"
    app.write_text(
        "def foo():\n    return 1\n\ndef bar():\n    return 2\n",
        encoding="utf-8",
    )
    cov = coverage.Coverage(data_file=str(tmp_path / ".coverage"))
    cov.start()
    ns: dict = {}
    exec(compile(app.read_text(), str(app), "exec"), ns)
    ns["foo"]()
    cov.stop()
    cov.save()
    return cov


def test_line_hits_are_boolean(tmp_path: pathlib.Path):
    cov = _run_and_measure(tmp_path)
    app = str(tmp_path / "app.py")
    hits = _line_hits(cov, app)
    # Line 1 (def foo), 2 (return 1), 4 (def bar) executed; line 5 missed.
    assert hits[1] == 1
    assert hits[2] == 1
    assert hits[4] == 1
    assert hits[5] == 0


def test_cobertura_includes_blended_ui_elements(tmp_path: pathlib.Path):
    cov = _run_and_measure(tmp_path)

    out_dir = tmp_path / ".shiny.cov"
    out_dir.mkdir()
    manifest = {
        "inputs": [{"id": "go", "type": "shiny.actionButtonInput", "label": ""}],
        "outputs": [],
        "tabs": [],
        "conditional": [],
        "modules": [],
    }
    interactions = [{"selector": "#go", "action": "click", "value": None}]
    _blend_ui_coverage(cov, manifest, interactions, out_dir)

    out = tmp_path / "cobertura.xml"
    _write_cobertura_xml(cov, str(out))

    root = ET.parse(out).getroot()
    assert root.tag == "coverage"
    # app.py: 4 executable lines (3 covered) + ui_elements.py: 1 line (1
    # covered, the generated `go = True` statement) = 5 valid, 4 covered.
    assert root.attrib["lines-valid"] == "5"
    assert root.attrib["lines-covered"] == "4"
    assert float(root.attrib["line-rate"]) == 4 / 5

    classes = root.findall(".//class")
    filenames = [el.attrib["filename"] for el in classes]
    assert any(name.endswith("ui_elements.py") for name in filenames)

    app_class = next(el for el in classes if el.attrib["filename"].endswith("app.py"))
    lines = {int(el.attrib["number"]): int(el.attrib["hits"]) for el in app_class.findall(".//line")}
    assert lines[1] == 1
    assert lines[2] == 1
    assert lines[5] == 0
