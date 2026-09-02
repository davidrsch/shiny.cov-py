"""Tests for module-boundary detection (shinycov.modules).

The end-to-end test runs a real py-shiny module app in a subprocess and
asserts the app-side `sitecustomize.py` hook writes `modules.json`, the same
ground-truth shape R's `shiny::moduleServer()` hook produces.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from shinycov import modules


def test_attach_boundaries_uses_longest_prefix():
    snapshot = {
        "inputs": [{"id": "app-name"}, {"id": "app-inner-slider"}, {"id": "plain"}],
        "outputs": [{"id": "app-out"}],
        "tabs": [],
        "conditional": [],
        "modules": [],
    }
    modules.attach_boundaries(snapshot, ["app", "app-inner"])

    assert snapshot["modules"] == ["app", "app-inner"]
    by_id = {el["id"]: el["module"] for el in snapshot["inputs"]}
    assert by_id["app-name"] == "app"
    assert by_id["app-inner-slider"] == "app-inner"
    assert by_id["plain"] == ""
    assert snapshot["outputs"][0]["module"] == "app"


def test_attach_boundaries_is_noop_without_boundaries():
    snapshot = {
        "inputs": [{"id": "x"}],
        "outputs": [],
        "tabs": [],
        "conditional": [],
        "modules": [],
    }
    modules.attach_boundaries(snapshot, [])
    assert "module" not in snapshot["inputs"][0]
    assert snapshot["modules"] == []


def test_install_writes_hook_and_sets_env(tmp_path: pathlib.Path):
    out = tmp_path / ".shiny.cov"
    old_path = os.environ.get("PYTHONPATH")
    old_out = os.environ.get("SHINYCOV_OUTPUT_DIR")
    try:
        modules.install(out)
        assert (out / "sitecustomize.py").exists()
        assert os.environ["PYTHONPATH"].split(os.pathsep)[0] == str(out)
        assert os.environ["SHINYCOV_OUTPUT_DIR"] == str(out)
    finally:
        if old_path is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = old_path
        if old_out is None:
            os.environ.pop("SHINYCOV_OUTPUT_DIR", None)
        else:
            os.environ["SHINYCOV_OUTPUT_DIR"] = old_out


def test_read_line_hits_parses_per_source_counts(tmp_path: pathlib.Path):
    out = tmp_path / ".shiny.cov"
    out.mkdir()
    (out / "linehits.pytest.json").write_text(
        json.dumps({"/x/app.py:1": 3, "/x/app.py:2": 5})
    )
    assert modules.read_line_hits(out) == {
        "pytest": {("/x/app.py", 1): 3, ("/x/app.py", 2): 5}
    }


def test_read_boundaries_handles_missing_and_corrupt(tmp_path: pathlib.Path):
    out = tmp_path / ".shiny.cov"
    assert modules.read_boundaries(out) == []

    out.mkdir()
    (out / "modules.json").write_text("{ not json")
    assert modules.read_boundaries(out) == []

    (out / "modules.json").write_text(json.dumps(["app", "app-inner"]))
    assert modules.read_boundaries(out) == ["app", "app-inner"]


_MODULE_APP = '''
from shiny import App, module, render, ui


@module.ui
def counter_ui(label: str = "Increment"):
    return ui.card(
        ui.input_action_button("button", label),
        ui.output_text("out"),
    )


@module.server
def counter_server(input, output, session):
    @render.text
    def out():
        return "ok"


app_ui = ui.page_fluid(counter_ui("counter1"), counter_ui("counter2"))


def server(input, output, session):
    counter_server("counter1")
    counter_server("counter2")


app = App(app_ui, server)
'''


def test_module_boundaries_recorded_from_app_subprocess(pytester: pytest.Pytester):
    # Launching the app is enough: the sitecustomize hook records boundaries
    # when the module UI is constructed during startup, before any browser
    # interaction, so no Playwright page is needed here.
    pytester.makepyfile(
        app=_MODULE_APP,
        test_app='''
from shiny.pytest import create_app_fixture
from shiny.run import ShinyAppProc

app = create_app_fixture("app.py")


def test_app_starts(app: ShinyAppProc):
    assert "127.0.0.1" in app.url
''',
    )
    result = pytester.runpytest_subprocess()
    result.assert_outcomes(passed=1)

    modules_file = pytester.path / ".shiny.cov" / "modules.json"
    assert modules_file.exists()
    assert sorted(json.loads(modules_file.read_text())) == ["counter1", "counter2"]
